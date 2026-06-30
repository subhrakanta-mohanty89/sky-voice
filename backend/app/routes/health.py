"""Health + service-info endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify

from config import settings

health_bp = Blueprint("health", __name__)


@health_bp.get("/")
@health_bp.get("/health")
@health_bp.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "sky-ai-twilio-backend",
        "twilio_configured": settings.is_twilio_configured(),
        "public_base_url": settings.public_base_url or None,
        "endpoints": {
            "auth": [
                "POST   /api/v1/auth/signup",
                "POST   /api/v1/auth/verify-otp",
                "POST   /api/v1/auth/resend-otp",
                "POST   /api/v1/auth/login",
                "POST   /api/v1/auth/logout",
                "GET    /api/v1/auth/me",
                "PATCH  /api/v1/auth/profile",
                "POST   /api/v1/auth/change-password",
                "DELETE /api/v1/auth/account",
                "POST   /api/v1/auth/forgot-password",
                "POST   /api/v1/auth/reset-password",
                "GET    /api/v1/auth/team",
                "POST   /api/v1/auth/team",
                "PATCH  /api/v1/auth/team/<user_id>",
                "DELETE /api/v1/auth/team/<user_id>",
            ],
            "voice_token": ["POST /api/v1/token"],
            "agents": [
                "GET    /api/v1/agents",
                "POST   /api/v1/agents",
                "PATCH  /api/v1/agents/<identity>",
                "DELETE /api/v1/agents/<identity>",
            ],
            "calls": [
                "GET  /api/v1/calls",
                "GET  /api/v1/calls/history",
                "GET  /api/v1/calls/<sid>",
                "POST /api/v1/calls",
                "POST /api/v1/calls/<sid>/hangup",
                "POST /api/v1/calls/<sid>/forward",
                "POST /api/v1/calls/<sid>/transfer",
                "POST /api/v1/calls/<sid>/hold",
                "POST /api/v1/calls/<sid>/unhold",
            ],
            "twilio_webhooks": [
                "POST /twilio/voice/incoming",
                "POST /twilio/voice/outgoing",
                "POST /twilio/voice/status",
                "POST /twilio/voice/dial-status",
                "POST /twilio/voice/recording",
            ],
            "websockets": ["WS /ws/admin?token=<jwt>"],
        },
    })
