"""
Plivo Routes Blueprint
=======================
Handles all Plivo webhooks: /voice, /process-speech, /language-selection, /call-status
Supports dynamic language selection per call via DTMF input
"""

import json
from flask import Blueprint, request, Response
from plivo import plivoxml

from config import (
    active_calls,
    pending_responses,
    operator_connections,
    customer_speech_queue,
    call_history,
    Config,
)
from app.views.operator_chat import notify_operator

# ===========================================
# LANGUAGE CONFIGURATION
# ===========================================

SUPPORTED_LANGUAGES = {
    "1": {"code": "hi", "plivo_stt": "hi-IN", "plivo_tts": "hi-IN", "voice": "Polly.Aditi", "name": "Hindi", "native": "हिंदी"},
    "2": {"code": "en", "plivo_stt": "en-IN", "plivo_tts": "en-US", "voice": "Polly.Matthew", "name": "English", "native": "English"},
    "3": {"code": "kn", "plivo_stt": "kn-IN", "plivo_tts": "kn-IN", "voice": "Polly.Aditi", "name": "Kannada", "native": "ಕನ್ನಡ"},
    "4": {"code": "mr", "plivo_stt": "mr-IN", "plivo_tts": "mr-IN", "voice": "Polly.Aditi", "name": "Marathi", "native": "मराठी"},
    "5": {"code": "ta", "plivo_stt": "ta-IN", "plivo_tts": "ta-IN", "voice": "Polly.Aditi", "name": "Tamil", "native": "தமிழ்"},
    "6": {"code": "te", "plivo_stt": "te-IN", "plivo_tts": "te-IN", "voice": "Polly.Aditi", "name": "Telugu", "native": "తెలుగు"},
    "7": {"code": "ur", "plivo_stt": "hi-IN", "plivo_tts": "hi-IN", "voice": "Polly.Aditi", "name": "Urdu", "native": "اردو"},
}

LANGUAGE_SELECTION_PROMPT = """
Welcome to SBI Support! Please select your preferred language.
Press 1 for Hindi.
Press 2 for English.
Press 3 for Kannada.
Press 4 for Marathi.
Press 5 for Tamil.
Press 6 for Telugu.
Press 7 for Urdu.
"""

CONNECTING_MESSAGE = "Thank you. Please wait while we connect you to our executive."


def get_call_language(call_uuid: str) -> dict:
    """Get language settings for a specific call"""
    if call_uuid in active_calls:
        lang_code = active_calls[call_uuid].get("language", "en")
        for digit, lang_info in SUPPORTED_LANGUAGES.items():
            if lang_info["code"] == lang_code:
                return lang_info
    return SUPPORTED_LANGUAGES["2"]


def notify_call_ended(call_uuid, status):
    """Notify operator that call has ended via WebSocket"""
    if call_uuid in operator_connections:
        try:
            ws = operator_connections[call_uuid]
            ws.send(json.dumps({
                "type": "call_ended",
                "call_uuid": call_uuid,
                "status": status,
                "message": f"Call ended: {status}"
            }))
            print(f"📴 Notified operator: Call {call_uuid} ended ({status})")
        except Exception as e:
            print(f"❌ Error notifying operator of call end: {e}")


plivo_bp = Blueprint('plivo', __name__)


@plivo_bp.route("/answer", methods=["POST", "GET"])
def answer():
    """Alias for /voice endpoint"""
    return voice()


@plivo_bp.route("/voice", methods=["POST", "GET"])
def voice():
    """
    Plivo webhook for INBOUND and OUTBOUND calls (answer_url)
    First step: Ask customer to select their preferred language via DTMF
    """
    if Config.USE_MEDIA_STREAMS:
        from app.views.media_streams import voice_stream
        return voice_stream()
    
    call_uuid = request.values.get("CallUUID", "unknown")
    from_number = request.values.get("From", "unknown")
    to_number = request.values.get("To", "unknown")
    direction = request.values.get("Direction", "inbound")
    call_status = request.values.get("CallStatus", "unknown")
    request_uuid = request.values.get("RequestUUID", "")
    
    print(f"📞 Call answered: {call_uuid} from {from_number} to {to_number} (direction: {direction}, status: {call_status})")
    
    # Handle outbound call migration from request_uuid to CallUUID
    if request_uuid and request_uuid in active_calls:
        call_info = active_calls.pop(request_uuid)
        call_info["status"] = "in-progress"
        active_calls[call_uuid] = call_info
        print(f"🔄 Migrated outbound call: {request_uuid} → {call_uuid}")
        
        if request_uuid in pending_responses:
            pending_responses[call_uuid] = pending_responses.pop(request_uuid)
        if request_uuid in operator_connections:
            operator_connections[call_uuid] = operator_connections.pop(request_uuid)
            try:
                operator_connections[call_uuid].send(json.dumps({
                    "type": "call_uuid_update",
                    "old_uuid": request_uuid,
                    "new_uuid": call_uuid,
                    "message": "Call connected - UUID updated"
                }))
                operator_connections[call_uuid].send(json.dumps({
                    "type": "call_status",
                    "call_uuid": call_uuid,
                    "status": "in-progress",
                    "message": "Call answered - conversation started"
                }))
            except Exception as e:
                print(f"❌ Error notifying operator: {e}")
    elif call_uuid not in active_calls:
        active_calls[call_uuid] = {
            "status": "active", 
            "from": from_number,
            "to": to_number,
            "type": "inbound" if direction == "inbound" else "outbound",
            "direction": direction,
            "stream": False,
            "language": None
        }
    
    response = plivoxml.ResponseElement()
    
    get_input = plivoxml.GetInputElement(
        action=f"{Config.BASE_URL}/language-selection",
        method="POST",
        input_type="dtmf",
        digit_end_timeout="5",
        num_digits="1",
        redirect="true"
    )
    get_input.add(plivoxml.SpeakElement(
        LANGUAGE_SELECTION_PROMPT.strip(),
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
    
    print(f"📤 Voice XML (Language Selection): {response.to_string()}")
    return Response(response.to_string(), mimetype="application/xml")


@plivo_bp.route("/language-selection", methods=["POST", "GET"])
def language_selection():
    """
    Process customer's language selection (DTMF input)
    """
    from app.views.media_streams import start_audio_stream
    
    call_uuid = request.values.get("CallUUID", "unknown")
    digit = request.values.get("Digits", "")
    
    print(f"🌐 Language selection for {call_uuid}: digit pressed = '{digit}'")
    
    response = plivoxml.ResponseElement()
    
    if digit in SUPPORTED_LANGUAGES:
        lang_info = SUPPORTED_LANGUAGES[digit]
        lang_code = lang_info["code"]
        plivo_tts = lang_info["plivo_tts"]
        plivo_stt = lang_info["plivo_stt"]
        voice = lang_info["voice"]
        lang_name = lang_info["name"]
        
        if call_uuid in active_calls:
            active_calls[call_uuid]["language"] = lang_code
            active_calls[call_uuid]["plivo_tts"] = plivo_tts
            active_calls[call_uuid]["plivo_stt"] = plivo_stt
            active_calls[call_uuid]["voice"] = voice
            active_calls[call_uuid]["stream"] = True
            print(f"🌐 Language set for {call_uuid}: {lang_name} ({lang_code})")
            
            if call_uuid in operator_connections:
                try:
                    operator_connections[call_uuid].send(json.dumps({
                        "type": "language_selected",
                        "call_uuid": call_uuid,
                        "language": lang_name,
                        "language_code": lang_code,
                        "message": f"Customer selected {lang_name}"
                    }))
                except:
                    pass
        
        call_info = active_calls.get(call_uuid, {})
        is_inbound = call_info.get("type") == "inbound" or call_info.get("direction") == "inbound"
        
        if is_inbound:
            print(f"📞 Inbound call {call_uuid} - notifying operator to answer")
            
            if call_uuid in active_calls:
                active_calls[call_uuid]["status"] = "ringing"
                active_calls[call_uuid]["waiting_for_operator"] = True
            
            response.add(plivoxml.SpeakElement(
                CONNECTING_MESSAGE,
                voice=voice,
                language=plivo_tts
            ))
            response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/wait-for-operator/{call_uuid}"))
        else:
            stream_result = start_audio_stream(call_uuid)
            
            if stream_result.get("success"):
                print(f"✅ Audio Stream started for {call_uuid}")
                
                if call_uuid in active_calls:
                    active_calls[call_uuid]["status"] = "in-progress"
                
                if call_uuid in operator_connections:
                    try:
                        operator_connections[call_uuid].send(json.dumps({
                            "type": "call_status",
                            "call_uuid": call_uuid,
                            "status": "in-progress",
                            "message": "Call answered - conversation started"
                        }))
                    except Exception as e:
                        print(f"❌ Error sending in-progress status: {e}")
                
                response.add(plivoxml.WaitElement(length="300"))
                response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/keep-alive/{call_uuid}"))
            else:
                print(f"❌ Failed to start Audio Stream: {stream_result.get('error')}")
                response.add(plivoxml.SpeakElement(
                    "Audio stream could not be started. Using fallback mode.",
                    voice="Polly.Matthew",
                    language="en-US"
                ))
                response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/voice"))
    else:
        response.add(plivoxml.SpeakElement(
            "Sorry, that's not a valid option. Please try again.",
            voice="Polly.Matthew",
            language="en-US"
        ))
        response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/voice"))
    
    print(f"📤 Language Selection XML: {response.to_string()}")
    return Response(response.to_string(), mimetype="application/xml")


@plivo_bp.route("/wait-for-operator/<call_uuid>", methods=["POST", "GET"])
def wait_for_operator(call_uuid):
    """
    Wait for operator to answer inbound call.
    Plays hold music and checks periodically if operator answered.
    Times out after 60 seconds.
    """
    import time
    from app.views.media_streams import start_audio_stream
    
    print(f"⏳ Waiting for operator to answer {call_uuid}")
    
    response = plivoxml.ResponseElement()
    call_info = active_calls.get(call_uuid, {})
    
    if call_info.get("operator_answered"):
        print(f"✅ Operator answered {call_uuid} - starting audio stream")
        
        # Get language settings
        lang_info = get_call_language(call_uuid)
        voice = lang_info.get("voice", "Polly.Matthew")
        plivo_tts = lang_info.get("plivo_tts", "en-US")
        
        # Notify customer that operator is connected
        response.add(plivoxml.SpeakElement(
            "Our executive is now connected. Please go ahead.",
            voice=voice,
            language=plivo_tts
        ))
        
        # Start Audio Stream
        stream_result = start_audio_stream(call_uuid)
        
        if stream_result.get("success"):
            active_calls[call_uuid]["status"] = "in-progress"
            active_calls[call_uuid]["waiting_for_operator"] = False
            
            # Notify operator that call is now in-progress
            if call_uuid in operator_connections:
                try:
                    operator_connections[call_uuid].send(json.dumps({
                        "type": "call_status",
                        "call_uuid": call_uuid,
                        "status": "in-progress",
                        "message": "Call answered - conversation started"
                    }))
                    print(f"📡 Sent in-progress status to operator: {call_uuid}")
                except Exception as e:
                    print(f"❌ Error sending in-progress status: {e}")
            
            response.add(plivoxml.WaitElement(length="300"))
            response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/keep-alive/{call_uuid}"))
        else:
            # Fallback to GetInput
            plivo_stt = lang_info.get("plivo_stt", "en-IN")
            get_input = plivoxml.GetInputElement(
                action=f"{Config.BASE_URL}/process-speech",
                method="POST",
                input_type="speech",
                language=plivo_stt
            )
            response.add(get_input)
            response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/process-speech"))
    else:
        # Operator hasn't answered yet - keep waiting
        # Check if call has been waiting too long (60 seconds timeout)
        wait_start = call_info.get("wait_start_time")
        if not wait_start:
            active_calls[call_uuid]["wait_start_time"] = time.time()
            wait_start = time.time()
        
        elapsed = time.time() - wait_start
        
        if elapsed > 60:  # 60 second timeout
            print(f"⏰ Timeout waiting for operator on {call_uuid}")
            response.add(plivoxml.SpeakElement(
                "We apologize, but all our executives are currently busy. Please try again later.",
                voice="Polly.Matthew",
                language="en-US"
            ))
            response.add(plivoxml.HangupElement())
        else:
            # Play waiting message and check again
            response.add(plivoxml.WaitElement(length="3"))  # Wait 3 seconds
            response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/wait-for-operator/{call_uuid}"))
    
    return Response(response.to_string(), mimetype="application/xml")


@plivo_bp.route("/answer-inbound/<call_uuid>", methods=["POST", "GET"])
def answer_inbound(call_uuid):
    """
    Operator answers an inbound call.
    Called when operator clicks 'Answer' button in UI.
    """
    print(f"📞 Operator answering inbound call: {call_uuid}")
    
    if call_uuid in active_calls:
        active_calls[call_uuid]["operator_answered"] = True
        active_calls[call_uuid]["status"] = "in-progress"
        active_calls[call_uuid]["waiting_for_operator"] = False
        
        # Notify via WebSocket if connected
        if call_uuid in operator_connections:
            try:
                operator_connections[call_uuid].send(json.dumps({
                    "type": "call_answered",
                    "call_uuid": call_uuid,
                    "message": "Call connected - you can now speak with the customer"
                }))
            except:
                pass
        
        return json.dumps({"success": True, "message": "Call answered", "call_uuid": call_uuid}), 200, {'Content-Type': 'application/json'}
    else:
        return json.dumps({"success": False, "error": "Call not found"}), 404, {'Content-Type': 'application/json'}


@plivo_bp.route("/keep-alive/<call_uuid>", methods=["POST", "GET"])
def keep_alive(call_uuid):
    """
    Keep the call alive while Audio Stream is active.
    This endpoint is redirected to every 5 minutes to prevent call timeout.
    """
    from app.views.media_streams import audio_streams
    
    print(f"🔄 Keep-alive check for {call_uuid}")
    
    response = plivoxml.ResponseElement()
    
    # Check if audio stream is still active
    if call_uuid in audio_streams and audio_streams[call_uuid].get("is_active", False):
        print(f"✅ Audio stream still active for {call_uuid}, continuing...")
        response.add(plivoxml.WaitElement(length="300"))
        response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/keep-alive/{call_uuid}"))
    elif call_uuid in active_calls:
        # Call exists but stream not active - keep waiting
        print(f"⏳ Call {call_uuid} still active, continuing...")
        response.add(plivoxml.WaitElement(length="300"))
        response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/keep-alive/{call_uuid}"))
    else:
        print(f"❌ Audio stream ended for {call_uuid}, hanging up")
        response.add(plivoxml.SpeakElement(
            "Thank you for calling. Goodbye!",
            voice="Polly.Matthew",
            language="en-US"
        ))
        response.add(plivoxml.HangupElement())
    
    return Response(response.to_string(), mimetype="application/xml")


@plivo_bp.route("/call-status", methods=["POST", "GET"])
def call_status():
    """
    Plivo call status webhook (ring_url, hangup_url)
    
    For OUTBOUND calls:
    - Plivo sends CallUUID (different from request_uuid we got from API)
    - We need to find the request_uuid to notify the correct operator
    - Request params: RequestUUID contains the original request_uuid
    
    Updates call status, notifies operator, and cleans up when call ends
    """
    from datetime import datetime
    
    call_uuid = request.values.get("CallUUID")
    request_uuid = request.values.get("RequestUUID", "")  # For outbound call mapping
    status = request.values.get("CallStatus", request.values.get("Event", "unknown"))
    direction = request.values.get("Direction", "")
    
    print(f"📞 Call Status: CallUUID={call_uuid}, RequestUUID={request_uuid}, Status={status}, Direction={direction}")
    print(f"   📊 Active calls: {list(active_calls.keys())}")
    print(f"   🔌 Operator connections: {list(operator_connections.keys())}")
    
    # For outbound calls, find the operator connection using request_uuid
    # because operator connected with request_uuid before CallUUID was assigned
    operator_key = None
    if call_uuid in operator_connections:
        operator_key = call_uuid
    elif request_uuid and request_uuid in operator_connections:
        operator_key = request_uuid
        print(f"   🔗 Found operator via request_uuid: {request_uuid}")
    else:
        # Search active_calls for matching request_uuid
        for key, info in active_calls.items():
            if info.get("request_uuid") == request_uuid:
                operator_key = key
                print(f"   🔗 Found operator via active_calls mapping: {key}")
                break
    
    # Update active call status
    active_call_key = None
    if call_uuid in active_calls:
        active_call_key = call_uuid
    elif request_uuid and request_uuid in active_calls:
        active_call_key = request_uuid
    
    if active_call_key:
        active_calls[active_call_key]["status"] = status
        
        # Track when call was answered (for duration calculation)
        if status == "answer":
            active_calls[active_call_key]["answered_at"] = datetime.now().isoformat()
    
    # Map 'answer' to 'in-progress' for frontend compatibility
    frontend_status = "in-progress" if status == "answer" else status
    
    # Notify operator of status change via WebSocket
    if operator_key and operator_key in operator_connections:
        try:
            ws = operator_connections[operator_key]
            ws.send(json.dumps({
                "type": "call_status",
                "call_uuid": call_uuid,
                "status": frontend_status,
                "message": f"Call status: {frontend_status}"
            }))
            print(f"📡 Sent status update to operator ({operator_key}): {frontend_status}")
        except Exception as e:
            print(f"❌ Error sending status update: {e}")
    else:
        print(f"   ⚠️ No operator connection found for CallUUID={call_uuid} or RequestUUID={request_uuid}")
    
    # Notify operator on terminal statuses
    if status in ["completed", "hangup", "busy", "no-answer", "cancel", "failed", "canceled"]:
        # Notify via the correct operator key
        if operator_key and operator_key in operator_connections:
            notify_call_ended(operator_key, status)
        else:
            notify_call_ended(call_uuid, status)
        
        # Add to call history - check both call_uuid and request_uuid keys
        call_info = None
        if call_uuid in active_calls:
            call_info = active_calls[call_uuid]
        elif active_call_key and active_call_key in active_calls:
            call_info = active_calls[active_call_key]
        
        if call_info:
            history_entry = {
                "call_uuid": call_uuid,
                "type": call_info.get("type", "unknown"),
                "direction": call_info.get("direction", "unknown"),
                "from": call_info.get("from", "unknown"),
                "to": call_info.get("to", ""),
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "answered_at": call_info.get("answered_at"),
                "ended_by": "system"
            }
            call_history.append(history_entry)
            print(f"📝 Added to call history: {call_uuid} - {status}")
        
        # Clean up all possible keys
        keys_to_cleanup = [call_uuid, request_uuid, active_call_key, operator_key]
        for key in keys_to_cleanup:
            if key and key in active_calls:
                del active_calls[key]
            if key and key in pending_responses:
                pending_responses[key].clear()
                del pending_responses[key]
            if key and key in customer_speech_queue:
                customer_speech_queue[key].clear()
                del customer_speech_queue[key]
            if key and key in operator_connections:
                del operator_connections[key]
    
    return Response("OK", status=200)


@plivo_bp.route("/process-speech", methods=["POST", "GET"])
def process_speech():
    """
    Process customer speech in REAL-TIME:
    1. Get transcribed customer speech (in customer's selected language)
    2. Send to operator immediately via WebSocket (translated to operator language)
    3. Check for operator response and play if available (in customer's language)
    4. Always continue listening (no delays)
    """
    call_uuid = request.values.get("CallUUID", "unknown")
    customer_speech = request.values.get("Speech", "")
    confidence = request.values.get("Confidence", "0")
    
    # Get per-call language settings
    lang_info = get_call_language(call_uuid)
    customer_lang = lang_info["code"]
    plivo_tts = lang_info["plivo_tts"]
    plivo_stt = lang_info["plivo_stt"]
    voice = lang_info["voice"]
    lang_name = lang_info["name"]
    
    response = plivoxml.ResponseElement()
    
    # If customer said something, send to operator immediately
    if customer_speech and customer_speech.strip():
        print(f"🎤 Customer said ({lang_name}): '{customer_speech}' (confidence: {confidence})")
        notify_operator(call_uuid, customer_speech, customer_lang)
    
    # Check if operator has responded (check queue)
    if call_uuid in pending_responses and len(pending_responses[call_uuid]) > 0:
        # Get next message from queue (FIFO)
        customer_response = pending_responses[call_uuid].popleft()
        print(f"🔊 Playing to customer ({lang_name}): {customer_response} [Queue remaining: {len(pending_responses[call_uuid])}]")
        
        # Play operator response in customer's selected language (TTS)
        response.add(plivoxml.SpeakElement(customer_response, voice=voice, language=plivo_tts))
        
        # Immediately start gathering again after playing (STT)
        get_input = plivoxml.GetInputElement(
            action=f"{Config.BASE_URL}/process-speech",
            method="POST",
            input_type="speech",
            language=plivo_stt
        )
        response.add(get_input)
    else:
        # No operator response yet - just keep listening (STT)
        get_input = plivoxml.GetInputElement(
            action=f"{Config.BASE_URL}/process-speech",
            method="POST",
            input_type="speech",
            language=plivo_stt
        )
        response.add(get_input)
    
    # Always loop back to keep the conversation going
    response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/process-speech"))
    
    return Response(response.to_string(), mimetype="application/xml")


@plivo_bp.route("/check-response/<call_uuid>", methods=["POST", "GET"])
def check_response(call_uuid):
    """
    Legacy endpoint - redirects to real-time process-speech flow
    """
    response = plivoxml.ResponseElement()
    response.add(plivoxml.RedirectElement(f"{Config.BASE_URL}/process-speech"))
    return Response(response.to_string(), mimetype="application/xml")
