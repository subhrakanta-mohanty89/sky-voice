"""
Sky Voice AI — Twilio backend configuration.

Centralised env-var loader with validation. Import the singleton
:data:`settings` everywhere instead of touching ``os.environ`` directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _bool(env_name: str, default: bool = False) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(env_name: str, default: str = "") -> List[str]:
    raw = os.getenv(env_name, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


class ConfigurationError(RuntimeError):
    """Raised when a required env var is missing."""


@dataclass(frozen=True)
class Settings:
    # --- server ---------------------------------------------------------
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "5050"))
    debug: bool = os.getenv("FLASK_ENV", "production").lower() == "development"
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    secret_key: str = os.getenv("APP_SECRET_KEY", "dev-only-change-me")
    allowed_origins: List[str] = field(
        default_factory=lambda: _csv(
            "ALLOWED_ORIGINS",
            "http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173",
        )
    )

    # --- twilio core ----------------------------------------------------
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_api_key_sid: str = os.getenv("TWILIO_API_KEY_SID", "")
    twilio_api_key_secret: str = os.getenv("TWILIO_API_KEY_SECRET", "")
    twilio_twiml_app_sid: str = os.getenv("TWILIO_TWIML_APP_SID", "")
    # Optional. Only needed for native mobile (React Native / iOS / Android)
    # incoming-call PUSH notifications. When set, it is attached to the Voice
    # access token's grant. Unused by the browser/desktop SDK, so leaving this
    # empty keeps current web behaviour unchanged.
    twilio_push_credential_sid: str = os.getenv("TWILIO_PUSH_CREDENTIAL_SID", "")
    twilio_caller_id: str = os.getenv("TWILIO_CALLER_ID", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    # --- routing --------------------------------------------------------
    incoming_ring_timeout: int = int(os.getenv("INCOMING_RING_TIMEOUT", "25"))
    fallback_forward_number: str = os.getenv("FALLBACK_FORWARD_NUMBER", "")
    hold_music_url: str = os.getenv(
        "HOLD_MUSIC_URL",
        "https://com.twilio.sounds.music.s3.amazonaws.com/MARKOVICHAMP-Borghestral.mp3",
    )

    # --- concurrency limits ---------------------------------------------
    # Hard cap on simultaneous active calls (across all agents). Extra
    # callers are queued or forwarded. Set to 0 for unlimited.
    max_concurrent_calls: int = int(os.getenv("MAX_CONCURRENT_CALLS", "10"))

    # --- call-center pipeline (queue + auto-routing) --------------------
    # ``parallel`` rings every available agent at once (legacy behaviour).
    # ``longest_idle`` rings only the agent who's been idle the longest,
    # which scales further but adds a few seconds of ring per hop.
    ring_strategy: str = os.getenv("RING_STRATEGY", "parallel").lower()
    # Customers wait this long in the on-hold queue before being sent to
    # voicemail / the fallback PSTN number.
    queue_max_wait_seconds: int = int(os.getenv("QUEUE_MAX_WAIT_SECONDS", "180"))
    # How often the on-hold call's TwiML re-evaluates whether an agent is
    # free. The active redirect from the leg-status webhook makes this a
    # safety net rather than the primary dispatch mechanism.
    queue_poll_seconds: int = int(os.getenv("QUEUE_POLL_SECONDS", "20"))
    # Spoken announcement on the very first hold round.
    queue_greeting: str = os.getenv(
        "QUEUE_GREETING",
        "All of our support agents are currently on calls. "
        "Please hold and the next available agent will be with you shortly.",
    )

    # --- IVR (welcome menu) --------------------------------------------
    # Played to the caller before any routing happens. Default is OFF
    # so callers go straight through to a human agent (or to Sara if no
    # agent is available). Set IVR_ENABLED=1 to re-enable the menu.
    ivr_enabled: bool = _bool("IVR_ENABLED", False)
    ivr_company_name: str = os.getenv("IVR_COMPANY_NAME", "K and K Legal")
    # TTS voice + locale used for the welcome and menu prompts.
    ivr_voice: str = os.getenv("IVR_VOICE", "Polly.Joanna")
    ivr_language: str = os.getenv("IVR_LANGUAGE", "en-US")
    # Seconds Twilio waits for a digit before re-playing the menu.
    ivr_gather_timeout: int = int(os.getenv("IVR_GATHER_TIMEOUT", "8"))
    # Max times we'll re-play the menu when the caller does nothing /
    # presses 9 to repeat. After that, fall through to the agent flow.
    ivr_max_repeats: int = int(os.getenv("IVR_MAX_REPEATS", "3"))

    # --- behaviour flags ------------------------------------------------
    record_calls: bool = _bool("RECORD_CALLS", False)
    validate_twilio_signature: bool = _bool("VALIDATE_TWILIO_SIGNATURE", True)

    # --- auth -----------------------------------------------------------
    auth_jwt_ttl: int = int(os.getenv("AUTH_JWT_TTL", str(7 * 24 * 3600)))
    first_user_is_admin: bool = _bool("FIRST_USER_IS_ADMIN", True)

    # --- database -------------------------------------------------------
    database_url: str = os.getenv("DATABASE_URL", "")

    # --- Sara (AI voice agent) ------------------------------------------
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    stt_model: str = os.getenv("STT_MODEL", "nova-3")
    tts_voice: str = os.getenv("TTS_VOICE", "aura-asteria-en")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")
    ai_base_url: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    sara_summary_to: str = os.getenv(
        "SARA_SUMMARY_TO",
        "info@kandklegal.com,subhrakanta@miti.us",
    )
    sara_auto_fallback: bool = _bool("SARA_AUTO_FALLBACK", True)
    # Default OFF: every inbound call first checks whether any agent or admin
    # is available and rings them; Sara (the AI) only answers when nobody on
    # the team is free. Set SARA_ANSWER_FIRST=1 to make Sara answer EVERY
    # inbound call directly, skipping the availability check, IVR menu and
    # hold queue.
    sara_answer_first: bool = _bool("SARA_ANSWER_FIRST", False)
    # Absolute path to the K&K Legal knowledge book.
    sara_knowledge_pdf: str = os.getenv(
        "SARA_KNOWLEDGE_PDF",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "K&K_Legal_Associates_Knowledge_Book.pdf",
        ),
    )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def is_twilio_configured(self) -> bool:
        # twilio_caller_id is intentionally not required here — outbound
        # caller IDs are managed via the SIM store (one or more numbers
        # added through the UI). The env var, when set, is just a seed
        # for the first SIM on a fresh deploy.
        return bool(
            self.twilio_account_sid
            and self.twilio_auth_token
            and self.twilio_api_key_sid
            and self.twilio_api_key_secret
            and self.twilio_twiml_app_sid
        )

    def is_sara_configured(self) -> bool:
        """Sara needs at minimum a Deepgram key. LLM is optional."""
        return bool(self.deepgram_api_key)

    def webhook_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.public_base_url}{path}"

    def assert_required(self) -> None:
        """Raise if mandatory configuration is missing.

        We only block startup on truly mandatory fields. Twilio creds are
        validated lazily so the dev can boot the API and inspect routes
        before having credentials, but any call-related route will reject
        if Twilio isn't fully configured.
        """
        missing: List[str] = []
        if not self.secret_key or self.secret_key == "dev-only-change-me":
            logger.warning(
                "APP_SECRET_KEY is using its default value — set a unique "
                "random string in .env before deploying."
            )

        if missing:
            raise ConfigurationError(
                "Missing required environment variables: " + ", ".join(missing)
            )


settings = Settings()
