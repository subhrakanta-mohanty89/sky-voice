"""
Services Package
================
Business logic and external service integrations.
"""

from app.services.translation import translate_text, get_translate_client
from app.services.tts import text_to_speech, fast_tts, fast_tts_async, prewarm_tts_sessions
from app.services.stt import (
    DeepgramStreamer,
    get_or_create_stream,
    close_stream,
    send_audio_to_stream,
    get_stt_stats,
    get_active_stream_count,
    DEEPGRAM_API_KEY,
)
from app.services.ai_client import ai_chat, ai_translate, ensure_ai_client, AIServiceError

__all__ = [
    # translation
    'translate_text',
    'get_translate_client',
    # tts
    'text_to_speech',
    'fast_tts',
    'fast_tts_async',
    'prewarm_tts_sessions',
    # stt
    'DeepgramStreamer',
    'get_or_create_stream',
    'close_stream',
    'send_audio_to_stream',
    'get_stt_stats',
    'get_active_stream_count',
    'DEEPGRAM_API_KEY',
    # ai_client
    'ai_chat',
    'ai_translate',
    'ensure_ai_client',
    'AIServiceError',
]
