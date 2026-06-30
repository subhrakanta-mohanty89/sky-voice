"""Flask application factory for the Sky Voice AI Twilio backend."""

from __future__ import annotations

import logging
import re

from flask import Flask, jsonify
from flask_cors import CORS

from config import settings

# The desktop shell (``frontend-exe``) serves the SAME React bundle from a
# local HTTP server bound to ``127.0.0.1`` on a RANDOM free port, and the
# Android shell (``mobile-apk``, a Capacitor wrapper) serves it from the fixed
# origin ``https://localhost`` (no port) — iOS would use ``capacitor://localhost``.
# We can't whitelist a fixed port for the desktop app, so allow any loopback
# origin plus the Capacitor schemes, in addition to the explicitly-configured
# web origins. Safe because CORS runs with ``supports_credentials=False`` (no
# cookies) — the JWT travels in the Authorization header set by our own bundle,
# which other local origins can't read.
_LOOPBACK_ORIGINS = [
    re.compile(r"^https?://localhost(:\d+)?$"),
    re.compile(r"^https?://127\.0\.0\.1(:\d+)?$"),
    re.compile(r"^capacitor://localhost$"),
]

from .db import init_schema
from .extensions import sock
from .routes import register_blueprints
from .ws.handlers import register_websocket_routes

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Build and return the Flask app."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["JSON_SORT_KEYS"] = False

    # CORS — allow the configured web frontends plus the desktop app
    # (any loopback origin; see _LOOPBACK_ORIGINS above) to call /api/*.
    CORS(
        app,
        resources={r"/api/*": {"origins": [*settings.allowed_origins, *_LOOPBACK_ORIGINS]}},
        supports_credentials=False,
    )

    sock.init_app(app)

    # Bootstrap the users table on first start (no-op if DATABASE_URL is unset).
    try:
        db_ready = init_schema()
    except Exception:
        logger.exception("Postgres init_schema failed — falling back to in-memory user store")
        db_ready = False

    # Ensure the built-in default tenant row exists so single-workspace /
    # .env deployments and any pre-multi-tenant rows resolve cleanly.
    try:
        from .services.tenant_context import seed_default_tenant
        seed_default_tenant()
    except Exception:
        logger.exception("seed_default_tenant failed (non-fatal)")

    register_blueprints(app)
    register_websocket_routes(sock)

    # If no SIMs exist yet and the legacy TWILIO_CALLER_ID env var is set,
    # seed it as the first SIM so the UI isn't empty on a fresh deploy.
    try:
        from .services.sim_service import seed_default_sim
        seed_default_sim()
    except Exception:
        logger.exception("seed_default_sim failed (non-fatal)")

    @app.errorhandler(404)
    def _not_found(_err):  # noqa: ANN001
        return jsonify({"success": False, "error": "not_found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_err):  # noqa: ANN001
        return jsonify({"success": False, "error": "method_not_allowed"}), 405

    @app.errorhandler(500)
    def _server_error(err):  # noqa: ANN001
        logger.exception("Unhandled server error: %s", err)
        return jsonify({"success": False, "error": "internal_error"}), 500

    logger.info(
        "Flask app initialised — Twilio configured: %s, public base URL: %s",
        settings.is_twilio_configured(),
        settings.public_base_url or "(not set)",
    )
    logger.info("User store backend: %s", "postgres" if db_ready else "in-memory")
    logger.info(
        "🤖 [boot] Sara: configured=%s auto_fallback=%s "
        "deepgram_key_set=%s ai_key_set=%s tts_voice=%s stt_model=%s "
        "summary_to=%s",
        settings.is_sara_configured(),
        settings.sara_auto_fallback,
        bool(settings.deepgram_api_key),
        bool(settings.ai_api_key),
        settings.tts_voice or "(default)",
        settings.stt_model or "(default)",
        settings.sara_summary_to or "(unset)",
    )

    # Pre-render every static line Sara might say so the first call
    # doesn't pay 500 ms-2 s of TTS HTTP latency for each prompt. This
    # is the single biggest factor in making the conversation feel
    # snappy and human. Done in a background thread so a slow Aura
    # response never blocks Cloud Run's startup probe.
    if settings.is_sara_configured():
        try:
            import threading
            from .services.deepgram_client import warm_tts_cache
            from .services.sara_agent import PROMPTS

            lines = [p for p in PROMPTS.values() if p]
            # "Of course. <prompt>" — what Sara plays when the caller
            # asks for a repeat. Pre-warmed so the repeat is instant.
            lines.extend(["Of course. " + p for p in PROMPTS.values() if p])
            # Common dynamic phrases that show up almost every call.
            lines.extend([
                "Sorry, I didn't catch that — could you repeat?",
                "Sorry, I didn't catch the plan name. Is it MetLife or ARAG?",
                "I'm sorry, I'm having trouble hearing you. Please call back in a few minutes. Goodbye.",
            ])
            threading.Thread(
                target=warm_tts_cache,
                args=(lines,),
                name="sara-tts-warm",
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001
            logger.exception("🔉 [deepgram-tts] cache warm failed (non-fatal)")
    return app
