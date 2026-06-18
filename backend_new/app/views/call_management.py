"""
Call Management Blueprint
=========================
Handles: /make-call, /active-calls, /end-call, /hold-call, /unhold-call, /call-history
"""

import json
from flask import Blueprint, request, jsonify
from datetime import datetime
from collections import deque

from config import (
    plivo_client,
    Config,
    active_calls,
    operator_connections,
    call_history,
    pending_responses,
)

call_bp = Blueprint('calls', __name__)


@call_bp.route("/make-call", methods=["POST"])
def make_call():
    """
    Initiate an outbound call
    
    POST /make-call
    Body: {"to": "+919876543210"}
    """
    data = request.get_json() or {}
    to_number = data.get("to")
    
    if not to_number:
        return jsonify({"error": "Missing 'to' phone number"}), 400
    
    try:
        call = plivo_client.calls.create(
            from_=Config.PLIVO_PHONE_NUMBER,
            to_=to_number,
            answer_url=f"{Config.BASE_URL}/voice",
            answer_method="POST",
            ring_url=f"{Config.BASE_URL}/call-status",
            ring_method="POST",
            hangup_url=f"{Config.BASE_URL}/call-status",
            hangup_method="POST"
        )
        
        request_uuid = call.request_uuid
        call_uuid = request_uuid
        
        active_calls[call_uuid] = {
            "status": "initiated",
            "from": to_number,
            "to": to_number,
            "type": "outbound",
            "direction": "outbound",
            "request_uuid": request_uuid
        }
        
        ws_host = Config.BASE_URL.replace('https://', '').replace('http://', '')
        
        return jsonify({
            "success": True,
            "call_uuid": call_uuid,
            "to": to_number,
            "from": Config.PLIVO_PHONE_NUMBER,
            "websocket_url": f"ws://{ws_host}/operator-ws/{call_uuid}",
            "message": "Call initiated. Connect to WebSocket to chat with caller."
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@call_bp.route("/active-calls", methods=["GET"])
def get_active_calls():
    """
    List all active calls with WebSocket URLs
    
    GET /active-calls
    """
    ws_host = Config.BASE_URL.replace('https://', '').replace('http://', '')
    
    calls = []
    for call_uuid, info in active_calls.items():
        lang_code = info.get("language")
        lang_names = {
            "hi": "Hindi", "en": "English", "te": "Telugu",
            "ta": "Tamil", "kn": "Kannada", "mr": "Marathi",
            "ml": "Malayalam", "ur": "Urdu"
        }
        language_name = lang_names.get(lang_code) if lang_code else None
        
        calls.append({
            "call_uuid": call_uuid,
            "status": info.get("status"),
            "from": info.get("from"),
            "to": info.get("to"),
            "type": info.get("type", "unknown"),
            "direction": info.get("direction", "unknown"),
            "operator_answered": info.get("operator_answered", False),
            "operator_connected": call_uuid in operator_connections,
            "is_on_hold": info.get("is_on_hold", False),
            "waiting_for_operator": info.get("waiting_for_operator", False),
            "language": language_name,
            "websocket_url": f"ws://{ws_host}/operator-ws/{call_uuid}"
        })
    
    return jsonify({"active_calls": calls})


@call_bp.route("/answer-call/<call_uuid>", methods=["POST"])
def answer_call(call_uuid):
    """
    Answer/Accept an inbound call
    
    POST /answer-call/<call_uuid>
    """
    if call_uuid not in active_calls:
        return jsonify({
            "error": "Call not found or already ended",
            "call_uuid": call_uuid
        }), 404
    
    active_calls[call_uuid]["operator_answered"] = True
    
    ws_host = Config.BASE_URL.replace('https://', '').replace('http://', '')
    
    return jsonify({
        "success": True,
        "call_uuid": call_uuid,
        "from": active_calls[call_uuid].get("from"),
        "type": active_calls[call_uuid].get("type"),
        "websocket_url": f"ws://{ws_host}/operator-ws/{call_uuid}",
        "message": "Call answered. Connect to WebSocket to start conversation."
    })


@call_bp.route("/hold-call/<call_uuid>", methods=["POST"])
def hold_call(call_uuid):
    """
    Put a call on hold
    
    POST /hold-call/<call_uuid>
    """
    if call_uuid not in active_calls:
        return jsonify({
            "error": "Call not found or already ended",
            "call_uuid": call_uuid
        }), 404
    
    active_calls[call_uuid]["is_on_hold"] = True
    
    if call_uuid in operator_connections:
        try:
            ws = operator_connections[call_uuid]
            ws.send(json.dumps({
                "type": "call_on_hold",
                "call_uuid": call_uuid,
                "message": "Call placed on hold"
            }))
        except Exception as e:
            print(f"Error notifying hold status: {e}")
    
    if call_uuid not in pending_responses:
        pending_responses[call_uuid] = deque()
    
    hold_messages = {
        "hi": "कृपया प्रतीक्षा करें। आपकी कॉल होल्ड पर है।",
        "te": "దయచేసి వెచ్చి ఉండండి. మీ కాల్ హోల్డ్లో ఉంది.",
        "ta": "தயவு செய்து காத்திருங்கள். உங்கள் அழைப்பு பிடியில் உள்ளது.",
        "kn": "ದಯವಿಟ್ಟು ಕಾಯ್ತಿರಿ. ನಿಮ್ಮ ಕಾಲ್ ಹೋಲ್ಡ್‌ನಲ್ಲಿದೆ.",
        "ml": "ദയവായി കാത്തിരിക്കുക. നിങ്ങളുടെ കാൾ ഹോൾഡിൽ ആണ്.",
        "mr": "कृपया थांबा. तुमचा कॉल होल्डवर आहे.",
        "ur": "براہ کرم انتظار کریں۔ آپ کی کال ہولڈ پر ہے۔",
        "en": "Please wait. Your call is on hold."
    }
    
    customer_lang = active_calls.get(call_uuid, {}).get("language", "en")
    hold_message = hold_messages.get(customer_lang, hold_messages["en"])
    pending_responses[call_uuid].append(hold_message)
    
    print(f"⏸️ Call {call_uuid} placed on hold")
    
    return jsonify({
        "success": True,
        "call_uuid": call_uuid,
        "is_on_hold": True,
        "message": "Call placed on hold"
    })


@call_bp.route("/unhold-call/<call_uuid>", methods=["POST"])
def unhold_call(call_uuid):
    """
    Resume a call from hold
    
    POST /unhold-call/<call_uuid>
    """
    if call_uuid not in active_calls:
        return jsonify({
            "error": "Call not found or already ended",
            "call_uuid": call_uuid
        }), 404
    
    active_calls[call_uuid]["is_on_hold"] = False
    
    if call_uuid in operator_connections:
        try:
            ws = operator_connections[call_uuid]
            ws.send(json.dumps({
                "type": "call_resumed",
                "call_uuid": call_uuid,
                "message": "Call resumed from hold"
            }))
        except Exception as e:
            print(f"Error notifying resume status: {e}")
    
    if call_uuid not in pending_responses:
        pending_responses[call_uuid] = deque()
    
    resume_messages = {
        "hi": "धन्यवाद आपकी प्रतीक्षा के लिए। अब मैं आपकी सहायता कर सकता हूँ।",
        "te": "వెచ్చి ఉన్నందుకు ధన్యవాదాలు. ఇప్పుడు నేను మీకు సహాయం చేయగలను.",
        "ta": "காத்திருந்ததற்கு நன்றி. இப்போது நான் உங்களுக்கு உதவலாம்.",
        "kn": "ಕಾಯ್ತಿರುವುದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಈಗ ನಾನು ನಿಮಗೆ ಸಹಾಯ ಮಾಡಬಲ್ಲಿ.",
        "ml": "കാത്തിരുന്നതിന് നന്ദി. ഇപ്പോൾ ഞാൻ നിങ്ങളെ സഹായിക്കാം.",
        "mr": "वाट पाहिल्याबद्दल धन्यवाद. आता मी तुम्हाला मदत करू शकतो.",
        "ur": "انتظار کرنے کا شکریہ۔ اب میں آپ کی مدد کر سکتا ہوں۔",
        "en": "Thank you for waiting. I can help you now."
    }
    
    customer_lang = active_calls.get(call_uuid, {}).get("language", "en")
    resume_message = resume_messages.get(customer_lang, resume_messages["en"])
    pending_responses[call_uuid].append(resume_message)
    
    print(f"▶️ Call {call_uuid} resumed from hold")
    
    return jsonify({
        "success": True,
        "call_uuid": call_uuid,
        "is_on_hold": False,
        "message": "Call resumed from hold"
    })


@call_bp.route("/end-call/<call_uuid>", methods=["POST"])
def end_call(call_uuid):
    """
    End an active call
    
    POST /end-call/<call_uuid>
    """
    try:
        call_info = active_calls.get(call_uuid, {})
        
        plivo_client.calls.hangup(call_uuid=call_uuid)
        
        if call_info:
            history_entry = {
                "call_uuid": call_uuid,
                "type": call_info.get("type", "unknown"),
                "direction": call_info.get("direction", "unknown"),
                "from": call_info.get("from", "unknown"),
                "to": call_info.get("to", ""),
                "status": "completed",
                "timestamp": datetime.now().isoformat(),
                "ended_by": "operator"
            }
            call_history.append(history_entry)
            print(f"📝 Added to call history: {call_uuid}")
        
        if call_uuid in active_calls:
            del active_calls[call_uuid]
        if call_uuid in pending_responses:
            del pending_responses[call_uuid]
        if call_uuid in operator_connections:
            del operator_connections[call_uuid]
        
        print(f"📴 Call {call_uuid} ended by operator")
        
        return jsonify({
            "success": True,
            "call_uuid": call_uuid,
            "message": "Call ended successfully"
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@call_bp.route("/call-history", methods=["GET"])
def get_call_history():
    """
    Get call history
    
    GET /call-history
    """
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    
    # Return from in-memory history
    history_slice = call_history[offset:offset + limit]
    
    return jsonify({
        "call_history": history_slice,
        "total": len(call_history),
        "limit": limit,
        "offset": offset
    })
