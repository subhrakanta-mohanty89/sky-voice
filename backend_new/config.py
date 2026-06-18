"""
Configuration Module
====================
All configuration, shared clients, and environment validation.

Features:
- SQLite/PostgreSQL database configuration
- Environment variable validation
- Typed configuration objects
- Centralized client management
"""

import os
import logging
from typing import TypedDict, Optional, Dict, Any, List
from collections import deque
from dotenv import load_dotenv
import plivo
from google.cloud import texttospeech_v1 as texttospeech

load_dotenv()

logger = logging.getLogger(__name__)


# ===========================================
# TYPE DEFINITIONS
# ===========================================

class CallInfo(TypedDict, total=False):
    """Type definition for call information."""
    call_uuid: str
    status: str
    type: str  # 'inbound' or 'outbound'
    direction: str
    from_number: str
    to_number: str
    language: str
    plivo_tts: str
    plivo_stt: str
    voice: str
    stream: bool
    operator_answered: bool
    waiting_for_operator: bool
    start_time: float
    end_time: Optional[float]
    duration: Optional[int]
    recording_url: Optional[str]
    transcript: List[Dict[str, Any]]
    sentiment_score: Optional[float]


class LanguageConfig(TypedDict):
    """Type definition for language configuration."""
    plivo: str
    voice: str
    name: str


# ===========================================
# BASE DIRECTORY (module level for correct __file__ resolution)
# ===========================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ===========================================
# CONFIGURATION ERROR
# ===========================================

class ConfigurationError(Exception):
    """Raised when required configuration is missing."""
    pass


# ===========================================
# CONFIG CLASS
# ===========================================

class Config:
    """Application configuration loaded from environment variables."""
    
    # ===========================================
    # ENVIRONMENT VALIDATION
    # ===========================================
    
    @staticmethod
    def validate_environment() -> Dict[str, str]:
        """
        Validate required environment variables on startup.
        Returns dict of warnings for optional but recommended vars.
        """
        required_vars = {
            "PLIVO_AUTH_ID": "Plivo authentication ID",
            "PLIVO_AUTH_TOKEN": "Plivo authentication token",
            "PLIVO_PHONE_NUMBER": "Plivo phone number",
            "BASE_URL": "Public base URL for webhooks",
            "DEEPGRAM_API_KEY": "Deepgram API key for STT",
        }
        
        recommended_vars = {
            "AI_API_KEY": "OpenAI API key for translations",
            "GOOGLE_APPLICATION_CREDENTIALS": "Google Cloud credentials file",
            "API_SECRET_KEY": "API secret key for authentication",
        }
        
        missing_required = []
        missing_recommended = []
        
        for var, description in required_vars.items():
            if not os.getenv(var):
                missing_required.append(f"  - {var}: {description}")
        
        for var, description in recommended_vars.items():
            if not os.getenv(var):
                missing_recommended.append(f"  - {var}: {description}")
        
        if missing_required:
            error_msg = "❌ Missing required environment variables:\n" + "\n".join(missing_required)
            logger.error(error_msg)
            raise ConfigurationError(error_msg)
        
        warnings = {}
        if missing_recommended:
            for line in missing_recommended:
                var_name = line.split(":")[0].strip().replace("  - ", "")
                warnings[var_name] = line
            logger.warning("⚠️ Missing recommended environment variables:\n" + "\n".join(missing_recommended))
        
        logger.info("✅ Environment validation passed")
        return warnings
    
    # ===========================================
    # ENVIRONMENT VARIABLES
    # ===========================================
    
    # Media Streams mode
    USE_MEDIA_STREAMS = os.getenv("USE_MEDIA_STREAMS", "false").lower() == "true"
    
    # Plivo
    PLIVO_AUTH_ID = os.getenv("PLIVO_AUTH_ID")
    PLIVO_AUTH_TOKEN = os.getenv("PLIVO_AUTH_TOKEN")
    PLIVO_PHONE_NUMBER = os.getenv("PLIVO_PHONE_NUMBER")
    
    # Server
    BASE_URL = os.getenv("BASE_URL")
    PORT = int(os.getenv("PORT", 5000))
    
    # Database - SQLAlchemy URI
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///calling_system.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = os.getenv("SQLALCHEMY_ECHO", "false").lower() == "true"
    
    # Legacy DATABASE_URL for compatibility
    DATABASE_URL = SQLALCHEMY_DATABASE_URI
    
    # Security
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    API_SECRET_KEY = os.getenv("API_SECRET_KEY", "dev-secret-key-change-in-production")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "jwt-dev-key-change-in-production")
    RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    
    # Monitoring
    ENABLE_METRICS = os.getenv("ENABLE_METRICS", "true").lower() == "true"
    ENABLE_CALL_RECORDING = os.getenv("ENABLE_CALL_RECORDING", "true").lower() == "true"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    # Cache settings
    TRANSLATION_CACHE_SIZE = int(os.getenv("TRANSLATION_CACHE_SIZE", "500"))
    TRANSLATION_CACHE_TTL = int(os.getenv("TRANSLATION_CACHE_TTL", "3600"))
    TTS_CACHE_SIZE = int(os.getenv("TTS_CACHE_SIZE", "100"))
    
    # Google Cloud (for TTS only - STT uses Deepgram)
    # Auto-resolve filename to full path in backend_new folder
    _google_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "quickhyr-b518cdc9a0d4.json")
    GOOGLE_APPLICATION_CREDENTIALS = (
        _google_creds if os.path.isabs(_google_creds) 
        else os.path.join(_BASE_DIR, _google_creds)
    )
    
    # Google TTS Voices (Male voices)
    GOOGLE_TTS_VOICE_HI = os.getenv("GOOGLE_TTS_VOICE_HI", "hi-IN-Wavenet-C")
    GOOGLE_TTS_VOICE_EN = os.getenv("GOOGLE_TTS_VOICE_EN", "en-US-Neural2-D")
    
    # Languages
    CUSTOMER_LANG = os.getenv("CUSTOMER_LANG", "hi")  # Customer language code
    OPERATOR_LANG = os.getenv("SUPPORT_LANG", "en")  # Operator language code
    
    # Welcome message
    WELCOME_MESSAGE_EN = os.getenv("WELCOME_MESSAGE")
    AGENT_NAME = os.getenv("AGENT_NAME", "Subhrakanta")
    COMPANY_NAME = os.getenv("COMPANY_NAME", "SBI")
    
    # Language to Plivo language code mapping
    LANGUAGE_MAPPING: Dict[str, LanguageConfig] = {
        "hi": {"plivo": "hi-IN", "voice": "Polly.Aditi", "name": "Hindi"},
        "te": {"plivo": "te-IN", "voice": "Polly.Aditi", "name": "Telugu"},
        "ta": {"plivo": "ta-IN", "voice": "Polly.Aditi", "name": "Tamil"},
        "kn": {"plivo": "kn-IN", "voice": "Polly.Aditi", "name": "Kannada"},
        "ml": {"plivo": "ml-IN", "voice": "Polly.Aditi", "name": "Malayalam"},
        "mr": {"plivo": "mr-IN", "voice": "Polly.Aditi", "name": "Marathi"},
        "ur": {"plivo": "hi-IN", "voice": "Polly.Aditi", "name": "Urdu"},
        "en": {"plivo": "en-US", "voice": "Polly.Matthew", "name": "English"}
    }
    
    # Get current customer language settings
    @classmethod
    def get_customer_plivo_lang(cls) -> str:
        return cls.LANGUAGE_MAPPING.get(cls.CUSTOMER_LANG, cls.LANGUAGE_MAPPING["hi"])["plivo"]
    
    @classmethod
    def get_customer_voice(cls) -> str:
        return cls.LANGUAGE_MAPPING.get(cls.CUSTOMER_LANG, cls.LANGUAGE_MAPPING["hi"])["voice"]
    
    @classmethod
    def get_customer_lang_name(cls) -> str:
        return cls.LANGUAGE_MAPPING.get(cls.CUSTOMER_LANG, cls.LANGUAGE_MAPPING["hi"])["name"]


# Set Google credentials (if file exists)
if os.path.exists(Config.GOOGLE_APPLICATION_CREDENTIALS):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = Config.GOOGLE_APPLICATION_CREDENTIALS
else:
    print(f"⚠️ Google credentials file not found: {Config.GOOGLE_APPLICATION_CREDENTIALS}")


# ===========================================
# SHARED CLIENTS
# ===========================================

# Plivo client
plivo_client = plivo.RestClient(Config.PLIVO_AUTH_ID, Config.PLIVO_AUTH_TOKEN)

# Google Cloud TTS client (lazy initialization to avoid startup errors)
tts_client = None

def get_tts_client():
    """Get or create Google Cloud TTS client."""
    global tts_client
    if tts_client is None:
        try:
            tts_client = texttospeech.TextToSpeechClient()
        except Exception as e:
            print(f"⚠️ Failed to initialize TTS client: {e}")
            return None
    return tts_client


# ===========================================
# SHARED STATE (In-memory - backed by SQLite)
# ===========================================

# Active calls: {call_uuid: CallInfo}
active_calls: Dict[str, CallInfo] = {}

# Operator WebSocket connections: {call_uuid: websocket}
operator_connections: Dict[str, Any] = {}

# Message queues for each call (FIFO)
pending_responses: Dict[str, deque] = {}
customer_speech_queue: Dict[str, deque] = {}

# Call history (will be loaded from database)
call_history: List[Dict[str, Any]] = []


# ===========================================
# LEGACY COMPATIBILITY
# ===========================================

# These are for backward compatibility with existing code
PLIVO_AUTH_ID = Config.PLIVO_AUTH_ID
PLIVO_AUTH_TOKEN = Config.PLIVO_AUTH_TOKEN
PLIVO_PHONE_NUMBER = Config.PLIVO_PHONE_NUMBER
BASE_URL = Config.BASE_URL
DATABASE_URL = Config.DATABASE_URL
USE_MEDIA_STREAMS = Config.USE_MEDIA_STREAMS
ENABLE_METRICS = Config.ENABLE_METRICS
LOG_LEVEL = Config.LOG_LEVEL
OPERATOR_LANG = Config.OPERATOR_LANG
CUSTOMER_LANG = Config.CUSTOMER_LANG
CUSTOMER_LANG_NAME = Config.get_customer_lang_name()
CUSTOMER_PLIVO_LANG = Config.get_customer_plivo_lang()
CUSTOMER_VOICE = Config.get_customer_voice()
GOOGLE_TTS_VOICE_HI = Config.GOOGLE_TTS_VOICE_HI
GOOGLE_TTS_VOICE_EN = Config.GOOGLE_TTS_VOICE_EN
LANGUAGE_MAPPING = Config.LANGUAGE_MAPPING
API_SECRET_KEY = Config.API_SECRET_KEY
RATE_LIMIT_PER_MINUTE = Config.RATE_LIMIT_PER_MINUTE
AGENT_NAME = Config.AGENT_NAME
COMPANY_NAME = Config.COMPANY_NAME


# Validation function alias
def validate_environment():
    return Config.validate_environment()
