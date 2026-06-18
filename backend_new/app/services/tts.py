"""
Text-to-Speech Service
======================
Production-level real-time TTS with streaming audio generation.
Uses Google Cloud TTS with streaming synthesis for ultra-low latency.

Features:
- Streaming audio generation (start playing before full synthesis)
- Pre-warmed connections for instant response
- Concurrent synthesis pipeline
- Audio chunking for smooth playback
"""

import logging
import threading
import time
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor
from google.cloud import texttospeech_v1 as texttospeech

from config import Config, get_tts_client

logger = logging.getLogger(__name__)

# Thread pool for parallel TTS requests
TTS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="TTS")

# Pre-warmed TTS sessions (one per language)
_tts_sessions = {}
_sessions_lock = threading.Lock()

# Voice mapping (Male voices - Neural2 fastest, Standard fallback)
TTS_VOICES = {
    "hi": {"language_code": "hi-IN", "name": "hi-IN-Neural2-B"},  # Neural2 Male
    "en": {"language_code": "en-US", "name": "en-US-Neural2-D"},  # US Neural2 Male (fastest)
    "te": {"language_code": "te-IN", "name": "te-IN-Standard-B"},  # Standard Male
    "ta": {"language_code": "ta-IN", "name": "ta-IN-Standard-B"},  # Standard Male
    "kn": {"language_code": "kn-IN", "name": "kn-IN-Standard-B"},  # Standard Male
    "ml": {"language_code": "ml-IN", "name": "ml-IN-Standard-B"},  # Standard Male
    "mr": {"language_code": "mr-IN", "name": "mr-IN-Standard-B"},  # Standard Male
    "bn": {"language_code": "bn-IN", "name": "bn-IN-Standard-B"},  # Standard Male
    "gu": {"language_code": "gu-IN", "name": "gu-IN-Standard-B"},  # Standard Male
    "ur": {"language_code": "hi-IN", "name": "hi-IN-Neural2-B"},  # Use Hindi Neural2
}

# Audio settings for minimum latency
TTS_SPEAKING_RATE = 1.2  # 20% faster for snappy response
TTS_SAMPLE_RATE = 8000   # Plivo format
CHUNK_SIZE = 1600        # ~200ms chunks for smooth streaming


class StreamingTTSSession:
    """
    Pre-warmed TTS session for a specific language.
    Keeps connection warm and provides instant synthesis.
    """
    
    def __init__(self, language: str):
        self.language = language
        self.voice_config = TTS_VOICES.get(language, TTS_VOICES["en"])
        self._last_used = time.time()
        self._synthesis_count = 0
        
        logger.info(f"[TTS] Pre-warming session for {language} ({self.voice_config['name']})")
    
    def synthesize(self, text: str) -> bytes:
        """
        Synthesize text to MULAW audio with minimal latency.
        """
        if not text or not text.strip():
            return b''
        
        t_start = time.time()
        
        try:
            synthesis_input = texttospeech.SynthesisInput(text=text)
            
            voice = texttospeech.VoiceSelectionParams(
                language_code=self.voice_config["language_code"],
                name=self.voice_config["name"]
            )
            
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MULAW,
                sample_rate_hertz=TTS_SAMPLE_RATE,
                speaking_rate=TTS_SPEAKING_RATE,
                effects_profile_id=["telephony-class-application"]  # Optimized for phone
            )
            
            client = get_tts_client()
            if client is None:
                return b''
            
            response = client.synthesize_speech(
                input=synthesis_input,
                voice=voice,
                audio_config=audio_config
            )
            
            self._synthesis_count += 1
            self._last_used = time.time()
            
            duration = time.time() - t_start
            logger.info(f"[TTS] {self.language}: '{text[:30]}...' → {len(response.audio_content)} bytes in {duration:.3f}s")
            
            return response.audio_content
            
        except Exception as e:
            logger.error(f"[TTS] Synthesis error for {self.language}: {e}")
            return b''
    
    def synthesize_chunked(self, text: str, chunk_callback: Callable[[bytes], None]):
        """
        Synthesize and stream chunks for immediate playback.
        Calls chunk_callback for each audio chunk as soon as available.
        """
        audio_data = self.synthesize(text)
        
        if audio_data:
            # Stream in chunks for smoother playback
            for i in range(0, len(audio_data), CHUNK_SIZE):
                chunk = audio_data[i:i + CHUNK_SIZE]
                chunk_callback(chunk)


def get_tts_session(language: str) -> StreamingTTSSession:
    """Get or create a pre-warmed TTS session for the language."""
    with _sessions_lock:
        if language not in _tts_sessions:
            _tts_sessions[language] = StreamingTTSSession(language)
        return _tts_sessions[language]


def prewarm_tts_sessions(languages: list = None):
    """Pre-warm TTS sessions for common languages on startup."""
    langs = languages or ["hi", "en", "te", "ta"]
    
    def warm_session(lang):
        session = get_tts_session(lang)
        # Do a tiny synthesis to warm up the connection
        session.synthesize(".")
    
    for lang in langs:
        TTS_POOL.submit(warm_session, lang)
    
    logger.info(f"[TTS] Pre-warming sessions for: {', '.join(langs)}")


def text_to_speech(text, language="hi", voice_name=None):
    """
    Convert text to speech using Google Cloud TTS
    
    Args:
        text: Text to convert
        language: Language code ('hi' or 'en')
        voice_name: Optional specific voice name
    
    Returns:
        Audio content as bytes (MP3)
    """
    try:
        if language == "hi":
            voice_name = voice_name or Config.GOOGLE_TTS_VOICE_HI
            language_code = "hi-IN"
        else:
            voice_name = voice_name or Config.GOOGLE_TTS_VOICE_EN
            language_code = "en-US"
        
        synthesis_input = texttospeech.SynthesisInput(text=text)
        
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            name=voice_name,
        )
        
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.15,  # 15% faster speech
        )
        
        client = get_tts_client()
        if client is None:
            return None
        
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config
        )
        
        return response.audio_content
    
    except Exception as e:
        print(f"❌ Google TTS Error: {e}")
        return None


def fast_tts(text: str, language: str = "hi") -> bytes:
    """
    Ultra-fast TTS for real-time dubbing.
    Uses pre-warmed session and optimized settings.
    
    Args:
        text: Text to synthesize
        language: Language code
    
    Returns:
        MULAW audio bytes at 8kHz
    """
    session = get_tts_session(language)
    return session.synthesize(text)


def fast_tts_async(text: str, language: str, callback: Callable[[bytes], None]):
    """
    Async TTS that calls callback when audio is ready.
    Non-blocking - returns immediately.
    
    Args:
        text: Text to synthesize
        language: Language code
        callback: Function to call with audio bytes
    """
    def do_synthesis():
        audio = fast_tts(text, language)
        if audio:
            callback(audio)
    
    TTS_POOL.submit(do_synthesis)


def fast_tts_streaming(text: str, language: str, chunk_callback: Callable[[bytes], None]):
    """
    Streaming TTS that sends audio chunks as they're generated.
    Best for real-time playback.
    
    Args:
        text: Text to synthesize
        language: Language code
        chunk_callback: Function to call with each audio chunk
    """
    session = get_tts_session(language)
    session.synthesize_chunked(text, chunk_callback)


def get_pipeline(call_uuid: str):
    """Get or create TTS pipeline for a call."""
    # For now, return None - pipeline is managed per-call
    return None


def close_pipeline(call_uuid: str):
    """Close TTS pipeline for a call."""
    pass
