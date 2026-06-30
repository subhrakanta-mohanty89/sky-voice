"""
Sara — Twilio Media Streams WebSocket bridge
============================================
Per-call duplex pipeline:

  Twilio  ──μ-law 8 kHz, base64─→  WS handler  ──raw μ-law─→  Deepgram STT
                                                                  │ final transcript
                                                                  ▼
  Twilio  ←─μ-law 8 kHz, base64──  WS handler  ←─raw μ-law──  Deepgram Aura TTS
                                                                  ▲
                                                                  │ reply text
                                                              SaraSession
                                                              (state machine + RAG/LLM)

flask-sock gives us a synchronous WebSocket inside Flask, which is the
right primitive here — Twilio's Media Stream is also synchronous-ish
(20 ms frames), and Deepgram's SDK ships its own background thread for
the STT side. We use one thread per call for outbound TTS playback so
the STT loop never blocks.

Endpoint registered: ``/ws/sara/<call_sid>``.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from queue import Empty, Queue
from typing import Optional

from app.models import call_store
from app.services.deepgram_client import (
    DeepgramSTT,
    beep_mulaw,
    synthesize_mulaw_stream,
)
from app.services.email import is_configured as mail_is_configured
from app.services.email import send_email
from app.services.realtime import broadcast_event
from app.services.sara_agent import SaraSession
from app.services.tenant_context import (
    get_tenant,
    resolve_tenant_id_for_call,
    sara_config,
)

logger = logging.getLogger(__name__)

TWILIO_FRAME_BYTES = 160

TWILIO_FRAME_INTERVAL = 0.02

# While Sara is silently accumulating a spelled ID/ZIP/phone (the caller's
# utterance arrives as several STT fragments), we keep the mic open and say
# nothing. If the caller then goes quiet for this long, the watchdog nudges
# them so the call can't stall in silence. Must comfortably exceed the
# natural pause between spoken characters so we never talk over the caller.
COLLECT_NUDGE_SECONDS = 4.0

# After Sara asks a question (and plays the "your turn" beep) she opens a
# listening window of this many seconds. If the caller stays silent for the
# whole window she re-asks; after MAX_NO_RESPONSE_RETRIES silent windows she
# politely wraps up so the call can't hang open forever.
RESPONSE_WINDOW_SECONDS = 8.0
MAX_NO_RESPONSE_RETRIES = 2

# Active Sara sessions keyed by call_sid — exposed so other modules
# (admin transfer endpoint, post-call hooks) can look them up.
_active: dict[str, "SaraBridge"] = {}
_active_lock = threading.Lock()


def get_session(call_sid: str) -> Optional["SaraBridge"]:
    with _active_lock:
        return _active.get(call_sid)


def session_count() -> int:
    with _active_lock:
        return len(_active)


# --------------------------------------------------------------------------- #
#  Per-call bridge
# --------------------------------------------------------------------------- #

class SaraBridge:
    """One instance per inbound Twilio Media Stream connection."""

    def __init__(self, ws, call_sid: str):
        self.ws = ws
        self.call_sid = call_sid
        self.stream_sid: Optional[str] = None
        # Resolve the owning tenant from the tracked call so Sara uses the
        # workspace's own voice, knowledge book, summary recipients and
        # company name (falling back to the platform .env defaults).
        self.tenant_id = resolve_tenant_id_for_call(call_sid)
        self.sara = sara_config(get_tenant(self.tenant_id))
        self.session = SaraSession(call_sid=call_sid, tenant_id=self.tenant_id)

        self._stt: Optional[DeepgramSTT] = None
        self._send_q: Queue = Queue()  # base64-encoded μ-law frames + marks
        self._sender_thread: Optional[threading.Thread] = None
        self._tts_lock = threading.Lock()  # only one TTS render at a time

        self._stopped = threading.Event()
        self._speaking = threading.Event()  # set while Sara is talking
        self._speaking_until: float = 0.0   # local fallback if Twilio never echoes the mark
        self._done_after_speak = False

        # Spelled-code accumulation: when the session is gathering an ID/ZIP
        # across multiple STT fragments it asks us to stay silent and keep
        # listening. ``_awaiting_input`` arms the silence watchdog in
        # ``_on_media`` so a quiet caller still gets re-prompted.
        self._awaiting_input = False
        self._last_final_ts = time.time()

        # Response window: armed after every question Sara asks. The
        # ``_on_media`` watchdog re-asks via ``_handle_no_response`` when the
        # deadline passes with no caller speech.
        self._awaiting_response = False
        self._response_deadline = 0.0
        self._no_response_count = 0

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Block until the WebSocket closes."""
        with _active_lock:
            _active[self.call_sid] = self
        try:
            self._sender_thread = threading.Thread(
                target=self._sender_loop,
                name=f"sara-tx-{self.call_sid[:8]}",
                daemon=True,
            )
            self._sender_thread.start()
            self._inbound_loop()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            if self._stt is not None:
                self._stt.stop()
        except Exception:  # noqa: BLE001
            pass
        with _active_lock:
            _active.pop(self.call_sid, None)
        # Best-effort email summary.
        try:
            if not self.session.finished_at:
                self.session.finished_at = time.time()
            self._email_summary()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] email_summary failed", self.call_sid)
        # Tell the rest of the system the AI call ended.
        try:
            call = call_store.get(self.call_sid)
            payload = call.to_dict() if call else {"call_uuid": self.call_sid}
            payload["sara"] = True
            broadcast_event("call.ended", payload, tenant_id=self.tenant_id)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    #  Inbound loop — receives JSON envelopes from Twilio
    # ------------------------------------------------------------------ #

    def _inbound_loop(self) -> None:
        while True:
            try:
                msg = self.ws.receive(timeout=60)
            except Exception:  # noqa: BLE001
                logger.info("[%s] WS receive error — closing", self.call_sid)
                return
            if msg is None:
                logger.info("[%s] WS closed by client", self.call_sid)
                return
            try:
                env = json.loads(msg)
            except Exception:  # noqa: BLE001
                continue
            event = env.get("event")
            if event == "start":
                self._on_start(env)
            elif event == "media":
                self._on_media(env)
            elif event == "mark":
                self._on_mark(env)
            elif event == "stop":
                logger.info("[%s] Twilio sent stop", self.call_sid)
                return

    # ------------------------------------------------------------------ #

    def _on_start(self, env: dict) -> None:
        start = env.get("start") or {}
        self.stream_sid = start.get("streamSid")
        # custom parameters passed via <Parameter> in the TwiML.
        params = start.get("customParameters") or {}
        self.session.from_number = params.get("from", "") or self.session.from_number
        self.session.to_number = params.get("to", "") or self.session.to_number
        self.session.transferred_from_agent = params.get("from_agent")
        logger.info(
            "\U0001F916 [sara-bridge] %s STREAM-START stream_sid=%s from=%s to=%s xfer=%s",
            self.call_sid, self.stream_sid,
            self.session.from_number, self.session.to_number,
            self.session.transferred_from_agent,
        )

        # Wire up Deepgram STT — on every final transcript, feed it to
        # the state machine and queue Sara's reply for playback. We
        # use ``en-US`` (not the generic ``en``) so Deepgram tunes its
        # acoustic model for North-American English; this measurably
        # improves transcription of US numbers, ZIPs and proper nouns.
        self._stt = DeepgramSTT(
            call_sid=self.call_sid,
            on_final=self._on_final_transcript,
            language="en-US",
        )
        ok = self._stt.start()
        if not ok:
            self._speak_and_finish(
                "I'm sorry, I'm having trouble hearing you. "
                "Please call back in a few minutes. Goodbye."
            )
            return

        # Open with Sara's greeting.
        greeting = self.session.greeting()
        self._enqueue_say(greeting)

    def _on_media(self, env: dict) -> None:
        if self._stt is None:
            return
        # Drop inbound audio while Sara is speaking to suppress her own
        # voice echoing back through the caller's leg. We rely on a
        # deterministic deadline (set when the TTS audio is queued)
        # rather than waiting for Twilio's mark echo, so the mic
        # always re-opens even if the echo is delayed or lost.
        if self._speaking.is_set():
            now = time.time()
            deadline = self._speaking_until
            if deadline and now >= deadline:
                self._speaking.clear()
            else:
                return
        # Silence watchdog: if the session is waiting for the caller to
        # finish spelling a code and they've gone quiet, re-ask so the call
        # doesn't stall. Only fires when we're not currently speaking.
        if self._awaiting_input and not self._speaking.is_set():
            if time.time() - self._last_final_ts > COLLECT_NUDGE_SECONDS:
                self._awaiting_input = False
                self._last_final_ts = time.time()
                self._nudge_collection()
        # Response-window watchdog: after a question + beep we give the caller
        # RESPONSE_WINDOW_SECONDS to start answering. If the window elapses in
        # silence, re-ask (or wrap up after too many tries). Disabled while a
        # spelled code is mid-collection — that has its own nudge above.
        if (
            self._awaiting_response
            and not self._awaiting_input
            and not self._speaking.is_set()
            and self._response_deadline
            and time.time() >= self._response_deadline
        ):
            self._handle_no_response()
        media = env.get("media") or {}
        payload = media.get("payload")
        if not payload:
            return
        try:
            audio = base64.b64decode(payload)
        except Exception:  # noqa: BLE001
            return
        self._stt.feed_audio(audio)

    def _on_mark(self, env: dict) -> None:
        mark = (env.get("mark") or {}).get("name")
        if mark == "sara-utterance-end":
            self._speaking.clear()
            if self._done_after_speak:
                logger.info(
                    "\U0001F916 [sara-bridge] %s WRAP-UP-DONE \u2014 closing WS",
                    self.call_sid,
                )
                try:
                    self.ws.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #

    def _on_final_transcript(self, text: str) -> None:
        # Don't try to advance the conversation while Sara is still
        # speaking — wait for the playback to finish.
        if self._speaking.is_set():
            return
        # The caller spoke — close the no-response window and reset the
        # silent-retry counter.
        self._awaiting_response = False
        self._response_deadline = 0.0
        self._no_response_count = 0
        self._last_final_ts = time.time()
        logger.info(
            "\U0001F3A4 [sara-bridge] %s USER-SAID %r (state=%s)",
            self.call_sid, text[:120], self.session.state.name,
        )
        try:
            reply, done = self.session.on_user_speech(text)
        except Exception:  # noqa: BLE001
            logger.exception("\U0001F916 [sara-bridge] %s on_user_speech failed", self.call_sid)
            return
        # The session may ask us to keep listening: it's accumulating a
        # spelled ID/ZIP/phone split across several STT fragments. In that
        # case ``reply`` is empty — don't speak, leave the mic open, and let
        # the watchdog nudge if the caller stalls.
        self._awaiting_input = bool(getattr(self.session, "awaiting_more", False))
        if not reply:
            return
        logger.info(
            "\U0001F4AC [sara-bridge] %s SARA-SAYS %r (done=%s)",
            self.call_sid, reply[:120], done,
        )
        if done:
            self._speak_and_finish(reply)
        else:
            self._enqueue_say(reply)

    def _nudge_collection(self) -> None:
        """Re-ask for a spelled code after the caller has gone quiet."""
        try:
            prompt = self.session.nudge_collection()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] nudge_collection failed", self.call_sid)
            return
        if prompt:
            logger.info(
                "\u23f3 [sara-bridge] %s COLLECT-NUDGE %r",
                self.call_sid, prompt[:80],
            )
            self._enqueue_say(prompt)

    def _handle_no_response(self) -> None:
        """Caller stayed silent for the whole response window. Re-ask the
        current question (re-arming the window) or, after too many silent
        windows, wrap up so the call can't hang open forever.
        """
        self._awaiting_response = False
        self._response_deadline = 0.0
        self._no_response_count += 1
        if self._no_response_count > MAX_NO_RESPONSE_RETRIES:
            logger.info(
                "\u23f3 [sara-bridge] %s NO-RESPONSE giving up after %d tries",
                self.call_sid, self._no_response_count - 1,
            )
            self._speak_and_finish(
                "I'm sorry, I haven't been able to hear you. I'll have a "
                "team member follow up with you. Goodbye."
            )
            return
        try:
            prompt = self.session.current_prompt()
        except Exception:  # noqa: BLE001
            logger.exception("[%s] current_prompt failed", self.call_sid)
            prompt = ""
        message = (
            ("I didn't hear anything. " + prompt).strip()
            if prompt else "Are you still there?"
        )
        logger.info(
            "\u23f3 [sara-bridge] %s NO-RESPONSE re-ask (try %d) %r",
            self.call_sid, self._no_response_count, message[:80],
        )
        self._enqueue_say(message)

    # ------------------------------------------------------------------ #
    #  Outbound — TTS rendering + frame-paced playback
    # ------------------------------------------------------------------ #

    def _enqueue_say(self, text: str) -> None:
        if not text:
            return
        # A normal prompt — Sara asks, plays the "your turn" beep, then opens
        # the response window.
        threading.Thread(
            target=self._render_and_queue,
            args=(text, False, True),
            name=f"sara-tts-{self.call_sid[:8]}",
            daemon=True,
        ).start()

    def _speak_and_finish(self, text: str) -> None:
        # Terminal line (goodbye) — no beep, no response window.
        threading.Thread(
            target=self._render_and_queue,
            args=(text, True, False),
            name=f"sara-tts-{self.call_sid[:8]}",
            daemon=True,
        ).start()

    def _render_and_queue(
        self, text: str, finish_after: bool, prompt: bool = False,
    ) -> None:
        with self._tts_lock:
            logger.info(
                "🔉 [sara-bridge] %s RENDER-START len=%d finish_after=%s prompt=%s",
                self.call_sid, len(text or ""), finish_after, prompt,
            )
            t0 = time.time()
            # Mute the inbound STT BEFORE we touch the network. The
            # initial deadline is a generous over-estimate (English
            # spoken pace is ~14 chars/sec → 0.07 s per char; we use
            # 0.09 + 1 s headroom) so the mic stays closed throughout
            # the entire HTTP round-trip and playback. We refine the
            # deadline as soon as we know the real byte count.
            self._speaking.set()
            est_seconds = max(1.0, len(text or "") * 0.09) + 1.0
            self._speaking_until = time.time() + est_seconds

            total_bytes = 0
            first_chunk_t: Optional[float] = None
            tail = b""  # carry-over partial frame between chunks
            try:
                for chunk in synthesize_mulaw_stream(text, voice=self.sara.tts_voice or None):
                    if not chunk:
                        continue
                    if first_chunk_t is None:
                        first_chunk_t = time.time()
                        logger.info(
                            "🔉 [sara-bridge] %s TTS-FIRST-BYTE after=%.2fs",
                            self.call_sid, first_chunk_t - t0,
                        )
                    total_bytes += len(chunk)
                    buf = tail + chunk
                    n_frames = len(buf) // TWILIO_FRAME_BYTES
                    for j in range(n_frames):
                        frame = buf[
                            j * TWILIO_FRAME_BYTES : (j + 1) * TWILIO_FRAME_BYTES
                        ]
                        self._send_q.put(("media", frame))
                    tail = buf[n_frames * TWILIO_FRAME_BYTES :]
            except Exception:  # noqa: BLE001
                logger.exception(
                    "🔉 [sara-bridge] %s RENDER-FAIL exception", self.call_sid,
                )
                self._speaking.clear()
                self._speaking_until = 0.0
                return

            # Flush any trailing partial frame, padded with μ-law
            # silence (0xFF) so Twilio always receives full 160-byte
            # frames.
            if tail:
                pad = b"\xff" * (TWILIO_FRAME_BYTES - len(tail))
                self._send_q.put(("media", tail + pad))
                total_bytes += len(pad)

            if total_bytes == 0:
                logger.warning(
                    "🔉 [sara-bridge] %s RENDER-EMPTY (TTS returned 0 bytes) text=%r",
                    self.call_sid, (text or "")[:120],
                )
                self._speaking.clear()
                self._speaking_until = 0.0
                return

            # Play a short beep after a question so the caller gets a clear
            # "your turn to speak" cue before the listening window opens.
            # Terminal goodbyes (finish_after, prompt=False) get no beep.
            if prompt:
                beep = beep_mulaw()
                for j in range(0, len(beep), TWILIO_FRAME_BYTES):
                    self._send_q.put(("media", beep[j : j + TWILIO_FRAME_BYTES]))
                total_bytes += len(beep)

            # Refine the deadline now that we know exactly how much
            # audio Twilio will play. 220 ms grace covers the sender
            # loop's outstanding queue + Twilio buffering + carrier
            # delivery before we let the mic re-open. Shorter than the
            # previous 350 ms so the user feels the gap close fast.
            playback_seconds = total_bytes / 8000.0
            self._speaking_until = time.time() + playback_seconds + 0.22
            ttfb = (first_chunk_t - t0) if first_chunk_t else 0.0
            logger.info(
                "🔉 [sara-bridge] %s SPEAKING bytes=%d duration=%.2fs ttfb=%.2fs render=%.2fs",
                self.call_sid, total_bytes, playback_seconds,
                ttfb, time.time() - t0,
            )
            # Arm the response window: once playback (speech + beep) ends the
            # caller has RESPONSE_WINDOW_SECONDS to start answering before the
            # no-response watchdog re-asks the question.
            if prompt and not finish_after:
                self._awaiting_response = True
                self._response_deadline = (
                    self._speaking_until + RESPONSE_WINDOW_SECONDS
                )
            self._send_q.put(("mark", "sara-utterance-end"))
            if finish_after:
                self._done_after_speak = True

    def _sender_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                kind, payload = self._send_q.get(timeout=0.5)
            except Empty:
                continue
            if self.stream_sid is None:
                # Drop until Twilio has identified the stream.
                continue
            try:
                if kind == "media":
                    msg = {
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {
                            "payload": base64.b64encode(payload).decode("ascii"),
                        },
                    }
                    self.ws.send(json.dumps(msg))
                    time.sleep(TWILIO_FRAME_INTERVAL)
                elif kind == "mark":
                    msg = {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": payload},
                    }
                    self.ws.send(json.dumps(msg))
            except Exception:  # noqa: BLE001
                logger.info("[%s] sender_loop write failed — exiting",
                            self.call_sid)
                return

    # ------------------------------------------------------------------ #

    def _email_summary(self) -> None:
        # Recipients = every company admin's email (pulled live from the DB)
        # plus any extra addresses configured on the tenant. This means a
        # workspace doesn't have to hand-maintain a recipient list — adding
        # an admin is enough to start receiving Sara's call summaries.
        from app.services.tenant_context import summary_recipients
        recipients = summary_recipients(self.tenant_id, self.sara.summary_to)
        if not recipients or not mail_is_configured():
            logger.info(
                "\U0001F4E7 [sara-bridge] %s SKIP-EMAIL (to=%r mailgun=%s)",
                self.call_sid, recipients, mail_is_configured(),
            )
            return
        body = self.session.summary_email_body()
        firm = (self.sara.company_name or "Sara").strip()
        subj = f"[{firm}] Call summary — {self.session.from_number or self.call_sid}"
        for addr in recipients:
            try:
                send_email(to=addr, subject=subj, text=body)
                logger.info(
                    "\U0001F4E7 [sara-bridge] %s EMAIL-SENT to=%s",
                    self.call_sid, addr,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "\U0001F4E7 [sara-bridge] %s EMAIL-FAILED to=%s",
                    self.call_sid, addr,
                )
