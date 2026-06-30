"""
Deepgram client — STT (nova-3) + Aura TTS
==========================================
Thin wrapper around the official ``deepgram-sdk`` for streaming STT and
the Aura HTTP endpoint for ultra-natural TTS. Both directions speak the
same audio format Twilio Media Streams use natively — μ-law 8 kHz mono
— so no resampling or audioop conversions are needed end-to-end.

  *  :class:`DeepgramSTT` — opens a Live STT WebSocket per call, accepts
     raw μ-law frames via :meth:`feed_audio`, and dispatches final
     transcripts to a callback.

  *  :func:`synthesize_mulaw` — synchronous HTTP call to Aura that
     returns raw μ-law 8 kHz audio bytes for a given text. Frame the
     bytes in 320-byte chunks (≈20 ms) and base64-encode each frame
     before pushing to Twilio's ``media`` outbound message.

This module assumes ``settings.deepgram_api_key`` is populated; check
``settings.is_sara_configured()`` before invoking either entry point.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from typing import Callable, Iterator, Optional

import requests
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveTranscriptionEvents,
)

from config import settings

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


_TTS_CACHE: dict[str, bytes] = {}
_TTS_CACHE_LOCK = threading.Lock()
_TTS_CACHE_MAX = 128


def _cache_key(text: str, voice: Optional[str]) -> str:
    return f"{(voice or settings.tts_voice).strip().lower()}|{(text or '').strip().lower()}"


# Phonetic respellings so Aura speaks brand names the way callers expect.
# Aura otherwise mangles the CamelCase "MetLife" and the acronym "ARAG".
# Keys are matched case-insensitively on word boundaries; the value is what
# Aura should actually pronounce. These affect ONLY the spoken audio — the
# real strings in transcripts/emails are untouched.
_PRONUNCIATION = {
    "metlife": "Met Life",   # → "MET-life"
    "arag": "A-Rag",         # → "AY-rag"
}
_PRONUNCIATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _PRONUNCIATION) + r")\b",
    re.IGNORECASE,
)


def _apply_pronunciation(text: str) -> str:
    """Rewrite brand names to phonetic spellings for the TTS engine."""
    if not text:
        return text
    return _PRONUNCIATION_RE.sub(
        lambda m: _PRONUNCIATION[m.group(0).lower()], text,
    )


# --------------------------------------------------------------------------- #
#  Beep tone — μ-law 8 kHz (Twilio-native)
# --------------------------------------------------------------------------- #
#
# A short tone played right after each of Sara's questions so the caller hears
# a clear "your turn to speak" cue before the listening window opens. Generated
# in-process as raw μ-law 8 kHz — the exact format Twilio Media Streams expect
# — so it needs no external asset and no resampling. We hand-roll the G.711
# encoder rather than depend on the standard-library ``audioop`` module, which
# was removed in Python 3.13.

# Exponent (segment) lookup for the top 8 bits of a biased sample. This is the
# canonical G.711 table 0,0,1,1,2,2,2,2,3,...,7 expressed as bit-lengths.
_ULAW_EXP_LUT = [max(0, x.bit_length() - 1) for x in range(256)]
_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635
# μ-law byte that encodes linear silence (a sample of 0).
_ULAW_SILENCE = 0xFF

_BEEP_CACHE: dict[tuple, bytes] = {}
_BEEP_LOCK = threading.Lock()


def _linear_to_ulaw(sample: int) -> int:
    """Encode one signed 16-bit PCM sample to a μ-law byte (G.711)."""
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _ULAW_BIAS
    exponent = _ULAW_EXP_LUT[(sample >> 7) & 0xFF]
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def beep_mulaw(
    freq: float = 1000.0,
    duration_ms: int = 180,
    amplitude: float = 0.35,
    sample_rate: int = 8000,
) -> bytes:
    """Return a short raw μ-law 8 kHz beep tone.

    The bytes are ready to be sliced into 160-byte (~20 ms) Twilio frames; the
    result is padded to a whole number of frames with μ-law silence so the
    sender loop only ever ships full frames. A few-millisecond fade in/out at
    each edge avoids an audible click. Results are cached so repeat plays are
    effectively free.
    """
    cache_key = (freq, duration_ms, amplitude, sample_rate)
    with _BEEP_LOCK:
        cached = _BEEP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    n = max(1, int(sample_rate * duration_ms / 1000))
    peak = amplitude * 32767.0
    step = 2.0 * math.pi * freq / sample_rate
    fade = max(1, min(120, n // 4))  # samples to ramp at each edge
    out = bytearray(n)
    for i in range(n):
        env = 1.0
        if i < fade:
            env = i / fade
        elif i >= n - fade:
            env = max(0.0, (n - 1 - i) / fade)
        out[i] = _linear_to_ulaw(int(peak * env * math.sin(step * i)))

    rem = len(out) % 160
    if rem:
        out.extend(bytes([_ULAW_SILENCE]) * (160 - rem))

    data = bytes(out)
    with _BEEP_LOCK:
        _BEEP_CACHE[cache_key] = data
    return data


# --------------------------------------------------------------------------- #
#  STT — streaming transcription
# --------------------------------------------------------------------------- #

class DeepgramSTT:
    """Per-call live STT session.

    Internally uses the Deepgram SDK's threaded WebSocket client. Audio
    is fed in via :meth:`feed_audio`; final transcripts surface through
    the ``on_final`` callback (interim transcripts are silently
    discarded — Sara only acts on speech-final events to avoid talking
    over the caller).
    """

    def __init__(
        self,
        call_sid: str,
        on_final: Callable[[str], None],
        language: str = "en-US",
    ) -> None:
        self.call_sid = call_sid
        self.language = language
        self._on_final = on_final
        self._lock = threading.Lock()
        self._connection = None
        self._opened = False

        # Deepgram streams an utterance as a series of finalised
        # (``is_final``) segments terminated by ``speech_final`` or an
        # ``UtteranceEnd`` event. We buffer the segments and flush them as
        # one complete utterance so the agent acts on whole sentences.
        self._final_buf: list[str] = []
        self._buf_lock = threading.Lock()

        client_opts = DeepgramClientOptions(
            options={"keepalive": "true"},
        )
        self._dg = DeepgramClient(settings.deepgram_api_key, client_opts)

    # ------------------------------------------------------------------ #
    def _new_connection(self):
        """Create a fresh Deepgram live-STT connection with every event
        handler wired up (transcript, utterance-end, error, close)."""
        conn = self._dg.listen.websocket.v("1")
        conn.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
        conn.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)
        conn.on(LiveTranscriptionEvents.Error, self._on_error)
        conn.on(LiveTranscriptionEvents.Close, self._on_close)
        return conn

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        with self._lock:
            if self._opened:
                return True
            try:
                # ``v("1")`` returns the v1 namespace, ``.live`` is the
                # synchronous (threaded) WebSocket client.
                self._connection = self._new_connection()

                # Try the requested model first; if Deepgram rejects it
                # (e.g. nova-3 not accepted on the legacy phone-call
                # μ-law 8 kHz path on a particular account / region),
                # fall back to nova-2-phonecall which is universally
                # supported.
                attempts = []
                primary = (settings.stt_model or "nova-3").strip()
                attempts.append(primary)
                if primary != "nova-2-phonecall":
                    attempts.append("nova-2-phonecall")
                if "nova-2" not in attempts:
                    attempts.append("nova-2")

                last_err: Optional[Exception] = None
                for model in attempts:
                    options = LiveOptions(
                        model=model,
                        language=self.language,
                        smart_format=True,
                        encoding="mulaw",
                        sample_rate=8000,
                        channels=1,
                        # Real-time conversational STT. ``interim_results``
                        # plus ``utterance_end_ms`` give a robust,
                        # word-timing-based end-of-speech signal that keeps
                        # working on noisy / transcoded phone audio where
                        # pure acoustic ``endpointing`` never finds a clean
                        # silence boundary — the old config silently
                        # buffered the whole turn and only flushed it when
                        # the caller hung up, so Sara never replied.
                        interim_results=True,
                        utterance_end_ms=1000,
                        vad_events=True,
                        endpointing=300,
                        no_delay=True,
                    )
                    try:
                        ok = self._connection.start(options)
                    except Exception as exc:  # noqa: BLE001
                        last_err = exc
                        logger.warning(
                            "[%s] Deepgram STT start raised for model=%s: %s",
                            self.call_sid, model, exc,
                        )
                        # Re-create the connection object — once .start()
                        # has failed, the SDK leaves it in a half-open
                        # state that won't accept another .start().
                        self._connection = self._new_connection()
                        continue
                    if ok:
                        self._opened = True
                        logger.info(
                            "🤖 [deepgram-stt] %s CONNECTED model=%s",
                            self.call_sid, model,
                        )
                        return True
                    logger.warning(
                        "[%s] Deepgram STT rejected model=%s — trying next.",
                        self.call_sid, model,
                    )
                    self._connection = self._new_connection()

                logger.error(
                    "🤖 [deepgram-stt] %s ALL-MODELS-FAILED tried=%s last_err=%s",
                    self.call_sid, attempts, last_err,
                )
                self._opened = False
                return False
            except Exception as exc:  # noqa: BLE001
                logger.exception("[%s] Deepgram STT start error: %s",
                                 self.call_sid, exc)
                self._opened = False
                return False

    # ------------------------------------------------------------------ #
    def feed_audio(self, mulaw_bytes: bytes) -> None:
        if not self._opened or self._connection is None or not mulaw_bytes:
            return
        try:
            self._connection.send(mulaw_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Deepgram STT feed_audio error: %s",
                           self.call_sid, exc)

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        with self._lock:
            if not self._opened or self._connection is None:
                return
            try:
                self._connection.finish()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._opened = False
                self._connection = None
                logger.info("[%s] Deepgram STT closed", self.call_sid)

    # ------------------------------------------------------------------ #
    def _on_transcript(self, _client, result, **_kwargs) -> None:
        try:
            channel = getattr(result, "channel", None)
            if channel is None:
                return
            alts = getattr(channel, "alternatives", None) or []
            if not alts:
                return
            text = (getattr(alts[0], "transcript", "") or "").strip()
            is_final = bool(getattr(result, "is_final", False))
            speech_final = bool(getattr(result, "speech_final", False))
            # Interim hypotheses are ignored — we act only on complete
            # utterances. Finalised (``is_final``) segments are buffered
            # and flushed together when Deepgram marks the end of the
            # utterance (``speech_final``) or an ``UtteranceEnd`` event
            # fires (see ``_on_utterance_end``).
            if is_final and text:
                with self._buf_lock:
                    self._final_buf.append(text)
            if speech_final:
                self._flush_utterance("speech_final")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] STT transcript handler error",
                             self.call_sid)

    def _on_utterance_end(self, *_args, **_kwargs) -> None:
        # Word-timing-gap end-of-utterance. This is the safety net that
        # fires even when acoustic endpointing never detects silence on a
        # noisy phone line, so the agent always hears the caller in real
        # time instead of only when the call ends.
        #
        # NB: the Deepgram SDK delivers the payload as a keyword argument
        # named after the event (``utterance_end=``), and the exact set of
        # positional/keyword args varies between SDK versions. We don't use
        # the payload here, so swallow everything to stay version-proof —
        # a strict signature silently raised inside the SDK's dispatcher
        # and the flush never ran.
        self._flush_utterance("utterance_end")

    def _flush_utterance(self, reason: str) -> None:
        with self._buf_lock:
            if not self._final_buf:
                return
            text = " ".join(self._final_buf).strip()
            self._final_buf.clear()
        if not text:
            return
        logger.info("🎤 [deepgram-stt] %s FINAL (%s) %r",
                    self.call_sid, reason, text[:160])
        try:
            self._on_final(text)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] on_final callback raised", self.call_sid)

    def _on_error(self, _client, error, **_kwargs) -> None:
        # Surface as much of the Deepgram error as we can — the SDK
        # gives us a typed object whose useful fields vary by version.
        details = getattr(error, "description", None) or getattr(error, "message", None) or repr(error)
        logger.warning("🤖 [deepgram-stt] %s ERROR %s", self.call_sid, details)

    def _on_close(self, *_args, **_kwargs) -> None:
        # Same version-proofing as ``_on_utterance_end`` — the SDK passes
        # the payload as ``close=`` and we don't need it.
        logger.info("[%s] Deepgram STT remote close", self.call_sid)
        self._opened = False


# --------------------------------------------------------------------------- #
#  TTS — Deepgram Aura, μ-law 8 kHz (Twilio-native)
# --------------------------------------------------------------------------- #

def synthesize_mulaw(
    text: str,
    voice: Optional[str] = None,
    timeout: int = 10,
) -> bytes:
    """Render ``text`` to raw μ-law 8 kHz audio via Deepgram Aura.

    Returns an empty bytes object on any error so callers can no-op
    cleanly mid-conversation rather than crashing the bridge.

    The returned bytes are ready to be sliced into 320-byte frames
    (~20 ms each) and base64-encoded for a Twilio ``media`` outbound
    message.
    """
    if not text or not text.strip():
        return b""
    if not settings.deepgram_api_key:
        logger.warning("synthesize_mulaw called without DEEPGRAM_API_KEY")
        return b""

    # Speak brand names (MetLife / ARAG) with the correct pronunciation.
    text = _apply_pronunciation(text)
    key = _cache_key(text, voice)
    with _TTS_CACHE_LOCK:
        cached = _TTS_CACHE.get(key)
    if cached is not None:
        return cached

    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "application/json",
    }
    params = {
        "model": voice or settings.tts_voice,
        "encoding": "mulaw",
        "sample_rate": "8000",
        "container": "none",  # raw bytes, no WAV header
    }
    try:
        resp = requests.post(
            DEEPGRAM_TTS_URL,
            headers=headers,
            params=params,
            json={"text": text.strip()},
            timeout=timeout,
        )
        resp.raise_for_status()
        audio = resp.content
    except requests.RequestException as exc:
        logger.warning("Deepgram Aura TTS failed (%s): %s", text[:40], exc)
        return b""

    with _TTS_CACHE_LOCK:
        if len(_TTS_CACHE) >= _TTS_CACHE_MAX:
            # Evict the oldest entry. Python's dict preserves insertion
            # order so popitem(last=False) would be cleaner, but a
            # plain dict's pop() is enough — we just iterate once.
            try:
                victim = next(iter(_TTS_CACHE))
                _TTS_CACHE.pop(victim, None)
            except StopIteration:
                pass
        _TTS_CACHE[key] = audio
    return audio


def warm_tts_cache(lines: list[str]) -> int:
    """Best-effort: render each line ahead of time so the first call
    that needs them plays instantly. Returns the number of newly
    cached lines (0 if cache was already warm or TTS is unavailable).
    Intended to be called once at boot, in a daemon thread.
    """
    if not settings.deepgram_api_key:
        return 0
    new = 0
    for line in lines:
        if not line or not line.strip():
            continue
        key = _cache_key(line, None)
        with _TTS_CACHE_LOCK:
            if key in _TTS_CACHE:
                continue
        audio = synthesize_mulaw(line)
        if audio:
            new += 1
    if new:
        logger.info("🔉 [deepgram-tts] cache warm — %d line(s) pre-rendered", new)
    return new


# --------------------------------------------------------------------------- #
#  TTS — streaming variant for lowest time-to-first-audio
# --------------------------------------------------------------------------- #

def synthesize_mulaw_stream(
    text: str,
    voice: Optional[str] = None,
    chunk_size: int = 320,
    timeout: int = 10,
) -> Iterator[bytes]:
    """Stream μ-law audio bytes from Aura as they arrive.

    Yields ``chunk_size`` byte chunks (default 320 = two Twilio frames)
    using a chunked HTTP response. The caller can push each chunk
    straight to Twilio's media stream without waiting for the full
    response body — this typically cuts time-to-first-audio from
    500–2000 ms down to 150–300 ms (just the Aura time-to-first-byte).

    On cache hit, yields the cached bytes in one go and returns
    immediately. On cache miss, opens a streaming HTTP request,
    accumulates the bytes for caching, and yields chunks as they
    arrive from the wire.

    Yields nothing on error so callers can fall back to silence.
    """
    if not text or not text.strip():
        return
    if not settings.deepgram_api_key:
        logger.warning("synthesize_mulaw_stream called without DEEPGRAM_API_KEY")
        return

    # Speak brand names (MetLife / ARAG) with the correct pronunciation.
    text = _apply_pronunciation(text)
    key = _cache_key(text, voice)
    with _TTS_CACHE_LOCK:
        cached = _TTS_CACHE.get(key)
    if cached is not None:
        # Cache hit — emit the whole buffer in `chunk_size` pieces so
        # the caller's frame loop sees a uniform stream of small
        # chunks regardless of cache hit/miss.
        for i in range(0, len(cached), chunk_size):
            yield cached[i : i + chunk_size]
        return

    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "application/json",
    }
    params = {
        "model": voice or settings.tts_voice,
        "encoding": "mulaw",
        "sample_rate": "8000",
        "container": "none",  # raw bytes, no WAV header
    }
    accumulator = bytearray()
    try:
        with requests.post(
            DEEPGRAM_TTS_URL,
            headers=headers,
            params=params,
            json={"text": text.strip()},
            timeout=timeout,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                accumulator.extend(chunk)
                yield chunk
    except requests.RequestException as exc:
        logger.warning(
            "Deepgram Aura TTS stream failed (%s): %s", text[:40], exc,
        )
        return

    if accumulator:
        full = bytes(accumulator)
        with _TTS_CACHE_LOCK:
            if len(_TTS_CACHE) >= _TTS_CACHE_MAX:
                try:
                    victim = next(iter(_TTS_CACHE))
                    _TTS_CACHE.pop(victim, None)
                except StopIteration:
                    pass
            _TTS_CACHE[key] = full
