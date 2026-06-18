"""
Media Streams Blueprint
=======================
PRODUCTION-LEVEL REAL-TIME DUBBING

Handles Plivo Audio Streams with Deepgram STT + Google Cloud TTS.
"""

import os
import json
import base64
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, Response
from plivo import plivoxml

from config import (
    active_calls,
    pending_responses,
    operator_connections,
    Config,
    plivo_client,
)
from app.services.translation import translate_text
from app.services.tts import fast_tts, prewarm_tts_sessions, CHUNK_SIZE
from app.services.stt import (
    get_or_create_stream,
    close_stream,
    send_audio_to_stream as send_audio_to_deepgram,
    DEEPGRAM_API_KEY,
)
from app.views.operator_chat import notify_operator

# Thread pool for parallel processing
MEDIA_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="Media")

# Configuration
STT_MIN_CONFIDENCE = float(os.getenv("STT_MIN_CONFIDENCE", "0.4"))
STT_MIN_TRANSCRIPT_LENGTH = int(os.getenv("STT_MIN_TRANSCRIPT_LENGTH", "2"))

# Noise phrases to filter
NOISE_PHRASES = {
    "hmm", "uh", "um", "ah", "oh", "hm", "mm", "ahem",
    "हम्म", "आह", "उह",
    "హ్మ్మ్", "ఆహ్", "ఓహ్",
    "ஹ்ம்ம்", "ஆ", "ஓ",
}

# Google TTS voice mapping
GOOGLE_TTS_VOICES = {
    "hi": {"language_code": "hi-IN", "name": "hi-IN-Neural2-B"},
    "en": {"language_code": "en-US", "name": "en-US-Neural2-D"},
    "te": {"language_code": "te-IN", "name": "te-IN-Standard-B"},
    "ta": {"language_code": "ta-IN", "name": "ta-IN-Standard-B"},
    "kn": {"language_code": "kn-IN", "name": "kn-IN-Standard-B"},
    "ml": {"language_code": "ml-IN", "name": "ml-IN-Standard-B"},
    "mr": {"language_code": "mr-IN", "name": "mr-IN-Standard-B"},
    "bn": {"language_code": "bn-IN", "name": "bn-IN-Standard-B"},
    "gu": {"language_code": "gu-IN", "name": "gu-IN-Standard-B"},
    "ur": {"language_code": "hi-IN", "name": "hi-IN-Neural2-B"},
}

# Latency monitoring
_latency_stats = {"count": 0, "total_ms": 0, "max_ms": 0}
_stats_lock = threading.Lock()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logger.info(f"[MEDIA] Production real-time dubbing (Deepgram: {'configured' if DEEPGRAM_API_KEY else 'MISSING'})")

# Pre-warm TTS sessions on module load
try:
    prewarm_tts_sessions(["hi", "en", "te", "ta"])
except Exception as e:
    logger.warning(f"[MEDIA] TTS pre-warm failed: {e}")

media_streams_bp = Blueprint('media_streams', __name__)

# Thread-safe storage
audio_streams = {}
plivo_websockets = {}
streams_lock = threading.Lock()
audio_send_queues = {}


def start_audio_stream(call_uuid: str) -> dict:
    """Start Audio Stream via Plivo REST API."""
    try:
        wss_url = Config.BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')
        stream_url = f"{wss_url}/media-stream/{call_uuid}"
        
        logger.info(f"[{call_uuid}] Starting Audio Stream via API: {stream_url}")
        
        response = plivo_client.calls.start_stream(
            call_uuid,
            service_url=stream_url,
            bidirectional=True,
            audio_track="inbound",
            stream_timeout=86400,
            content_type="audio/x-mulaw;rate=8000"
        )
        
        logger.info(f"[{call_uuid}] Audio Stream started: {response}")
        return {"success": True, "stream_id": getattr(response, 'stream_id', 'unknown')}
        
    except Exception as e:
        logger.error(f"[{call_uuid}] Failed to start Audio Stream: {e}")
        return {"success": False, "error": str(e)}


def stop_audio_stream(call_uuid: str, stream_id: str = None) -> bool:
    """Stop Audio Stream via Plivo REST API."""
    try:
        if stream_id:
            plivo_client.calls.delete_specific_stream(call_uuid, stream_id)
        else:
            plivo_client.calls.delete_all_streams(call_uuid)
        logger.info(f"[{call_uuid}] Audio Stream stopped")
        return True
    except Exception as e:
        logger.error(f"[{call_uuid}] Failed to stop Audio Stream: {e}")
        return False


def generate_tts_audio(text: str, language_code: str = "hi") -> bytes:
    """Generate TTS audio using production streaming module."""
    if not text or not text.strip():
        return b''
    
    t_start = time.time()
    audio = fast_tts(text, language_code)
    
    t_elapsed = time.time() - t_start
    logger.info(f"[TTS] Generated {len(audio)} bytes in {t_elapsed:.3f}s for '{text[:30]}...'")
    
    with _stats_lock:
        _latency_stats["count"] += 1
        _latency_stats["total_ms"] += t_elapsed * 1000
        _latency_stats["max_ms"] = max(_latency_stats["max_ms"], t_elapsed * 1000)
    
    return audio


def send_audio_to_plivo(call_uuid: str, audio_data: bytes, chunked: bool = True) -> bool:
    """Send audio back to Plivo via WebSocket."""
    if call_uuid not in plivo_websockets:
        logger.warning(f"[{call_uuid}] No Plivo WebSocket connection to send audio")
        return False
    
    try:
        ws = plivo_websockets[call_uuid]
        
        if chunked and len(audio_data) > CHUNK_SIZE:
            for i in range(0, len(audio_data), CHUNK_SIZE):
                chunk = audio_data[i:i + CHUNK_SIZE]
                audio_b64 = base64.b64encode(chunk).decode('utf-8')
                ws.send(json.dumps({
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw;rate=8000",
                        "payload": audio_b64
                    }
                }))
                time.sleep(0.02)
        else:
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            ws.send(json.dumps({
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-mulaw;rate=8000",
                    "payload": audio_b64
                }
            }))
        
        return True
        
    except Exception as e:
        logger.error(f"[{call_uuid}] Error sending audio to Plivo: {e}")
        return False


def voice_stream():
    """Alternative voice endpoint for Media Streams mode."""
    call_uuid = request.values.get("CallUUID", "unknown")
    from_number = request.values.get("From", "unknown")
    to_number = request.values.get("To", "unknown")
    direction = request.values.get("Direction", "inbound")
    
    logger.info(f"📞 [STREAM MODE] Call: {call_uuid} from {from_number} (direction: {direction})")
    
    if call_uuid not in active_calls:
        active_calls[call_uuid] = {
            "status": "active",
            "from": from_number,
            "to": to_number,
            "type": "inbound" if direction == "inbound" else "outbound",
            "direction": direction,
            "stream": True,
            "language": None
        }
    
    response = plivoxml.ResponseElement()
    
    # Language selection
    get_input = plivoxml.GetInputElement(
        action=f"{Config.BASE_URL}/language-selection",
        method="POST",
        input_type="dtmf",
        digit_end_timeout="5",
        num_digits="1",
        redirect="true"
    )
    get_input.add(plivoxml.SpeakElement(
        "Welcome. Press 1 for Hindi, 2 for English, 3 for Kannada, 4 for Marathi, 5 for Tamil, 6 for Telugu, 7 for Urdu.",
        voice="Polly.Matthew",
        language="en-US"
    ))
    response.add(get_input)
    
    response.add(plivoxml.SpeakElement(
        "Sorry, I didn't receive any input.",
        voice="Polly.Matthew",
        language="en-US"
    ))
    response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/voice"))
    
    return Response(response.to_string(), mimetype="application/xml")


def handle_final_transcript(call_uuid: str, transcript: str, confidence: float):
    """Handle final transcript from Deepgram."""
    if not transcript or transcript.lower().strip() in NOISE_PHRASES:
        return
    
    if len(transcript) < STT_MIN_TRANSCRIPT_LENGTH:
        return
    
    if confidence < STT_MIN_CONFIDENCE:
        logger.debug(f"[{call_uuid}] Low confidence ({confidence:.2f}): {transcript}")
        return
    
    call_info = active_calls.get(call_uuid, {})
    customer_lang = call_info.get("language", "hi")
    
    logger.info(f"[{call_uuid}] 🎤 Customer ({customer_lang}): '{transcript}'")
    
    # Notify operator
    notify_operator(call_uuid, transcript, customer_lang)


def handle_pending_responses(call_uuid: str):
    """Check and process pending responses for a call."""
    if call_uuid not in pending_responses:
        return
    
    call_info = active_calls.get(call_uuid, {})
    customer_lang = call_info.get("language", "hi")
    
    while pending_responses[call_uuid]:
        message = pending_responses[call_uuid].popleft()
        logger.info(f"[{call_uuid}] 💬 Playing to customer: '{message[:50]}...'")
        
        audio = generate_tts_audio(message, customer_lang)
        if audio:
            send_audio_to_plivo(call_uuid, audio)


def register_media_stream_websocket(sock):
    """Register Media Stream WebSocket handler."""
    
    @sock.route('/media-stream/<call_uuid>')
    def media_stream_handler(ws, call_uuid):
        """Handle Plivo Media Stream WebSocket."""
        logger.info(f"[{call_uuid}] 🔌 Media Stream WebSocket connected")
        
        with streams_lock:
            plivo_websockets[call_uuid] = ws
            audio_send_queues[call_uuid] = deque()
        
        call_info = active_calls.get(call_uuid, {})
        customer_lang = call_info.get("language", "hi")
        
        # Create Deepgram stream
        deepgram_stream = get_or_create_stream(
            call_uuid,
            language=customer_lang,
            on_final_transcript=lambda t, c: handle_final_transcript(call_uuid, t, c),
        )
        
        # Start response checker thread
        response_checker_running = True
        
        def response_checker():
            while response_checker_running:
                try:
                    handle_pending_responses(call_uuid)
                except Exception as e:
                    logger.error(f"[{call_uuid}] Response checker error: {e}")
                time.sleep(0.1)
        
        response_thread = threading.Thread(target=response_checker, daemon=True)
        response_thread.start()
        
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break
                
                try:
                    msg = json.loads(data)
                    event_type = msg.get("event")
                    
                    if event_type == "media":
                        audio_b64 = msg.get("media", {}).get("payload", "")
                        if audio_b64:
                            audio_data = base64.b64decode(audio_b64)
                            send_audio_to_deepgram(call_uuid, audio_data)
                    
                    elif event_type == "start":
                        stream_sid = msg.get("streamSid")
                        logger.info(f"[{call_uuid}] Stream started: {stream_sid}")
                    
                    elif event_type == "stop":
                        logger.info(f"[{call_uuid}] Stream stopped")
                        break
                    
                except json.JSONDecodeError:
                    logger.warning(f"[{call_uuid}] Invalid JSON received")
                except Exception as e:
                    logger.error(f"[{call_uuid}] Error processing message: {e}")
        
        except Exception as e:
            logger.error(f"[{call_uuid}] WebSocket error: {e}")
        
        finally:
            response_checker_running = False
            
            with streams_lock:
                if call_uuid in plivo_websockets:
                    del plivo_websockets[call_uuid]
                if call_uuid in audio_send_queues:
                    del audio_send_queues[call_uuid]
            
            close_stream(call_uuid)
            logger.info(f"[{call_uuid}] 🔌 Media Stream WebSocket disconnected")


def get_latency_stats() -> dict:
    """Get TTS latency statistics."""
    with _stats_lock:
        count = _latency_stats["count"]
        avg_ms = _latency_stats["total_ms"] / count if count > 0 else 0
        return {
            "count": count,
            "avg_latency_ms": round(avg_ms, 1),
            "max_latency_ms": round(_latency_stats["max_ms"], 1),
        }


def get_active_streams() -> list:
    """Get list of active media streams."""
    with streams_lock:
        return [
            {
                "call_uuid": call_uuid,
                "has_websocket": call_uuid in plivo_websockets,
                "queue_size": len(audio_send_queues.get(call_uuid, [])),
            }
            for call_uuid in audio_streams
        ]


def cleanup_all_streams():
    """Clean up all active streams."""
    with streams_lock:
        for call_uuid in list(plivo_websockets.keys()):
            try:
                close_stream(call_uuid)
            except Exception as e:
                logger.error(f"[{call_uuid}] Cleanup error: {e}")
        
        plivo_websockets.clear()
        audio_streams.clear()
        audio_send_queues.clear()
    
    logger.info("[MEDIA] All streams cleaned up")
