"""
Operator Chat Blueprint
=======================
PRODUCTION-LEVEL real-time operator communication

Handles: WebSocket /operator-ws, REST /send-message
"""

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from collections import deque

from config import (
    operator_connections,
    pending_responses,
    customer_speech_queue,
    active_calls,
    Config,
)
from app.services.translation import translate_text

operator_bp = Blueprint('operator', __name__)

# Thread pool for concurrent processing
OPERATOR_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="Operator")


def get_call_language_info(call_uuid: str) -> dict:
    """Get language settings for a specific call"""
    if call_uuid in active_calls:
        call_info = active_calls[call_uuid]
        return {
            "code": call_info.get("language", "en"),
            "name": get_language_name(call_info.get("language", "en"))
        }
    return {"code": "en", "name": "English"}


def get_language_name(lang_code: str) -> str:
    """Get human-readable language name from code"""
    names = {
        "hi": "Hindi", "en": "English", "te": "Telugu",
        "ta": "Tamil", "kn": "Kannada", "mr": "Marathi",
        "ml": "Malayalam", "ur": "Urdu"
    }
    return names.get(lang_code, "English")


@operator_bp.route("/send-message/<call_uuid>", methods=["POST"])
def send_message(call_uuid):
    """
    Send message to caller via REST API
    
    POST /send-message/<call_uuid>
    Body: {"text": "Hello, how can I help?"}
    """
    data = request.get_json() or {}
    operator_text = data.get("text", "")
    
    if not operator_text:
        return jsonify({"error": "Missing 'text' field"}), 400
    
    lang_info = get_call_language_info(call_uuid)
    customer_lang = lang_info["code"]
    
    customer_text = translate_text(operator_text, customer_lang, source_language=Config.OPERATOR_LANG)
    
    if call_uuid not in pending_responses:
        pending_responses[call_uuid] = deque()
    
    pending_responses[call_uuid].append(customer_text)
    
    print(f"💬 Message queued for {call_uuid}: {operator_text} → {customer_text} ({lang_info['name']}) [Queue size: {len(pending_responses[call_uuid])}]")
    
    return jsonify({
        "success": True,
        "call_uuid": call_uuid,
        "original": operator_text,
        "translated": customer_text,
        "language": lang_info["name"]
    })


def notify_operator(call_uuid, customer_text, customer_lang=None):
    """
    PRODUCTION: Fast customer speech notification to operator.
    
    Flow:
    1. Customer speaks (in their language)
    2. Translate to English (parallel processing)
    3. Send to operator WebSocket (instant)
    """
    t_start = time.time()
    
    def process_and_send():
        t_process_start = time.time()
        
        lang = customer_lang
        if lang is None:
            lang_info = get_call_language_info(call_uuid)
            lang = lang_info["code"]
        
        lang_name = get_language_name(lang)
        
        if call_uuid in operator_connections:
            try:
                t_translate_start = time.time()
                operator_text = translate_text(customer_text, Config.OPERATOR_LANG, source_language=lang)
                t_translate = time.time() - t_translate_start
                
                ws = operator_connections[call_uuid]
                ws.send(json.dumps({
                    "type": "customer_speech",
                    "call_uuid": call_uuid,
                    "text": operator_text,
                    "original_text": customer_text,
                    "original_hindi": customer_text,
                    "language": Config.OPERATOR_LANG,
                    "customer_language": lang_name,
                    "latency_ms": round((time.time() - t_start) * 1000)
                }))
                
                t_total = time.time() - t_start
                print(f"📤 [{t_translate:.3f}s] {lang_name}→EN: '{customer_text[:30]}' → '{operator_text[:30]}' (total: {t_total:.3f}s)")
                
            except Exception as e:
                print(f"❌ Error notifying operator: {e}")
        else:
            if call_uuid not in customer_speech_queue:
                customer_speech_queue[call_uuid] = deque()
            customer_speech_queue[call_uuid].append({"text": customer_text, "lang": lang})
            print(f"📥 Queued ({lang_name}): '{customer_text[:30]}...'")
    
    OPERATOR_POOL.submit(process_and_send)


def register_websocket(sock):
    """
    Register WebSocket routes with Flask-Sock
    """
    
    @sock.route('/operator-ws/<call_uuid>')
    def operator_websocket(ws, call_uuid):
        """
        WebSocket for operator to chat with caller - REAL-TIME BIDIRECTIONAL
        """
        print(f"👨‍💼 Operator connected for call: {call_uuid}")
        operator_connections[call_uuid] = ws
        
        lang_info = get_call_language_info(call_uuid)
        customer_lang = lang_info["code"]
        lang_name = lang_info["name"]
        
        is_inbound = False
        is_streaming = False
        if call_uuid in active_calls:
            call_info = active_calls[call_uuid]
            is_inbound = call_info.get("type") == "inbound" or call_info.get("direction") == "inbound"
            is_streaming = call_info.get("stream", False)
            call_info["operator_answered"] = True
        
        try:
            ws.send(json.dumps({
                "type": "connected",
                "call_uuid": call_uuid,
                "customer_language": lang_name,
                "customer_language_code": customer_lang,
                "is_inbound": is_inbound,
                "is_streaming": is_streaming,
                "message": f"Connected to call (Customer language: {lang_name})"
            }))
            
            # Send any queued customer speech
            if call_uuid in customer_speech_queue:
                while customer_speech_queue[call_uuid]:
                    queued = customer_speech_queue[call_uuid].popleft()
                    queued_text = queued["text"]
                    queued_lang = queued.get("lang", customer_lang)
                    queued_lang_name = get_language_name(queued_lang)
                    
                    operator_text = translate_text(queued_text, Config.OPERATOR_LANG, source_language=queued_lang)
                    
                    ws.send(json.dumps({
                        "type": "customer_speech",
                        "call_uuid": call_uuid,
                        "text": operator_text,
                        "original_text": queued_text,
                        "original_hindi": queued_text,
                        "language": Config.OPERATOR_LANG,
                        "customer_language": queued_lang_name,
                        "queued": True
                    }))
            
            # Listen for operator messages
            while True:
                data = ws.receive()
                if data is None:
                    break
                
                try:
                    msg = json.loads(data)
                    
                    if msg.get("type") == "operator_message":
                        operator_text = msg.get("text", "")
                        
                        if operator_text:
                            lang_info = get_call_language_info(call_uuid)
                            customer_lang = lang_info["code"]
                            
                            customer_text = translate_text(operator_text, customer_lang, source_language=Config.OPERATOR_LANG)
                            
                            if call_uuid not in pending_responses:
                                pending_responses[call_uuid] = deque()
                            pending_responses[call_uuid].append(customer_text)
                            
                            ws.send(json.dumps({
                                "type": "message_sent",
                                "call_uuid": call_uuid,
                                "original": operator_text,
                                "translated": customer_text,
                                "language": lang_info["name"],
                                "queued": True
                            }))
                            
                            print(f"💬 WS Message: '{operator_text}' → '{customer_text}' ({lang_info['name']})")
                    
                    elif msg.get("type") == "ping":
                        ws.send(json.dumps({"type": "pong"}))
                    
                except json.JSONDecodeError:
                    print(f"❌ Invalid JSON from operator: {data[:50]}")
                
        except Exception as e:
            print(f"❌ WebSocket error for {call_uuid}: {e}")
        
        finally:
            if call_uuid in operator_connections:
                del operator_connections[call_uuid]
            print(f"👨‍💼 Operator disconnected from call: {call_uuid}")
