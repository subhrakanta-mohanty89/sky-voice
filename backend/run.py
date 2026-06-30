"""Entry point for running the Sky Voice AI Twilio backend.

Usage:
    python run.py             # dev server with reloader off (Twilio webhooks)
    flask --app run run       # via flask CLI
    gunicorn -k gthread -w 4 run:app   # production
"""

from __future__ import annotations

import logging

from app import create_app
from config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
app = create_app()


if __name__ == "__main__":
    settings.assert_required()
    if not settings.is_twilio_configured():
        logger.warning(
            "Twilio credentials are not fully configured — call-related "
            "endpoints will return HTTP 503 until you populate .env."
        )
    if not settings.public_base_url:
        logger.warning(
            "PUBLIC_BASE_URL is empty. Twilio cannot reach this server. "
            "Run `ngrok http %s` and set PUBLIC_BASE_URL.",
            settings.port,
        )

    logger.info("🚀  Sky Voice AI Twilio backend listening on %s:%s", settings.host, settings.port)
    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        use_reloader=False,
        threaded=True,
    )
