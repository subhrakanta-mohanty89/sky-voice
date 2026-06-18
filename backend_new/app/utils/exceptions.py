"""
Custom Exceptions Module
========================
Provides structured error handling with error codes and context.
"""

from typing import Optional, Dict, Any
from enum import Enum


class ErrorCode(Enum):
    """Enumeration of error codes for categorizing exceptions."""
    # General errors (1000-1099)
    UNKNOWN_ERROR = 1000
    CONFIGURATION_ERROR = 1001
    VALIDATION_ERROR = 1002
    RATE_LIMIT_ERROR = 1003
    
    # AI/ML Service errors (2000-2099)
    AI_SERVICE_ERROR = 2000
    TRANSLATION_ERROR = 2001
    STT_ERROR = 2002
    TTS_ERROR = 2003
    SENTIMENT_ANALYSIS_ERROR = 2004
    
    # Deepgram errors (2100-2199)
    DEEPGRAM_CONNECTION_ERROR = 2100
    DEEPGRAM_AUTHENTICATION_ERROR = 2101
    DEEPGRAM_TRANSCRIPTION_ERROR = 2102
    DEEPGRAM_STREAM_ERROR = 2103
    
    # Google Cloud errors (2200-2299)
    GOOGLE_TTS_ERROR = 2200
    GOOGLE_AUTHENTICATION_ERROR = 2201
    
    # OpenAI errors (2300-2399)
    OPENAI_API_ERROR = 2300
    OPENAI_RATE_LIMIT_ERROR = 2301
    OPENAI_AUTHENTICATION_ERROR = 2302
    
    # Call/Plivo errors (3000-3099)
    CALL_ERROR = 3000
    CALL_NOT_FOUND = 3001
    CALL_INITIATION_ERROR = 3002
    CALL_CONNECTION_ERROR = 3003
    PLIVO_API_ERROR = 3004
    INVALID_PHONE_NUMBER = 3005
    
    # WebSocket errors (4000-4099)
    WEBSOCKET_ERROR = 4000
    WEBSOCKET_CONNECTION_ERROR = 4001
    WEBSOCKET_AUTHENTICATION_ERROR = 4002
    
    # Database errors (5000-5099)
    DATABASE_ERROR = 5000
    DATABASE_CONNECTION_ERROR = 5001
    RECORD_NOT_FOUND = 5002
    DUPLICATE_RECORD = 5003
    
    # Audio processing errors (6000-6099)
    AUDIO_ERROR = 6000
    AUDIO_FORMAT_ERROR = 6001
    AUDIO_STREAM_ERROR = 6002


class CallingSystemError(Exception):
    """
    Base exception for all calling system errors.
    Provides structured error information with code, message, and context.
    """
    
    def __init__(
        self,
        message: str,
        error_code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.original_exception = original_exception
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for JSON response."""
        return {
            'error': True,
            'error_code': self.error_code.value,
            'error_name': self.error_code.name,
            'message': self.message,
            'details': self.details
        }
    
    def __str__(self) -> str:
        return f"[{self.error_code.name}] {self.message}"


class ConfigurationError(CallingSystemError):
    """Raised when there's a configuration problem."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=message,
            error_code=ErrorCode.CONFIGURATION_ERROR,
            details=details
        )


class RateLimitError(CallingSystemError):
    """Raised when rate limit is exceeded."""
    
    def __init__(
        self,
        message: str = "Rate limit exceeded",
        retry_after: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        details = details or {}
        if retry_after:
            details['retry_after'] = retry_after
        
        super().__init__(
            message=message,
            error_code=ErrorCode.RATE_LIMIT_ERROR,
            details=details
        )
        self.retry_after = retry_after


class AIServiceError(CallingSystemError):
    """Raised when AI service fails."""
    
    def __init__(
        self,
        message: str,
        service: str = "unknown",
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        details['service'] = service
        
        super().__init__(
            message=message,
            error_code=ErrorCode.AI_SERVICE_ERROR,
            details=details,
            original_exception=original_exception
        )


class STTError(CallingSystemError):
    """Raised when speech-to-text fails."""
    
    def __init__(
        self,
        message: str,
        provider: str = "deepgram",
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        details['provider'] = provider
        
        super().__init__(
            message=message,
            error_code=ErrorCode.STT_ERROR,
            details=details,
            original_exception=original_exception
        )


class TTSError(CallingSystemError):
    """Raised when text-to-speech fails."""
    
    def __init__(
        self,
        message: str,
        provider: str = "google",
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        details['provider'] = provider
        
        super().__init__(
            message=message,
            error_code=ErrorCode.TTS_ERROR,
            details=details,
            original_exception=original_exception
        )


class TranslationError(CallingSystemError):
    """Raised when translation fails."""
    
    def __init__(
        self,
        message: str,
        source_language: Optional[str] = None,
        target_language: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        if source_language:
            details['source_language'] = source_language
        if target_language:
            details['target_language'] = target_language
        
        super().__init__(
            message=message,
            error_code=ErrorCode.TRANSLATION_ERROR,
            details=details,
            original_exception=original_exception
        )


class CallError(CallingSystemError):
    """Raised when call operation fails."""
    
    def __init__(
        self,
        message: str,
        call_uuid: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        if call_uuid:
            details['call_uuid'] = call_uuid
        
        super().__init__(
            message=message,
            error_code=ErrorCode.CALL_ERROR,
            details=details,
            original_exception=original_exception
        )


class WebSocketError(CallingSystemError):
    """Raised when WebSocket operation fails."""
    
    def __init__(
        self,
        message: str,
        connection_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        if connection_id:
            details['connection_id'] = connection_id
        
        super().__init__(
            message=message,
            error_code=ErrorCode.WEBSOCKET_ERROR,
            details=details,
            original_exception=original_exception
        )


class DatabaseError(CallingSystemError):
    """Raised when database operation fails."""
    
    def __init__(
        self,
        message: str,
        operation: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        if operation:
            details['operation'] = operation
        
        super().__init__(
            message=message,
            error_code=ErrorCode.DATABASE_ERROR,
            details=details,
            original_exception=original_exception
        )


class AudioError(CallingSystemError):
    """Raised when audio processing fails."""
    
    def __init__(
        self,
        message: str,
        format: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_exception: Optional[Exception] = None
    ):
        details = details or {}
        if format:
            details['format'] = format
        
        super().__init__(
            message=message,
            error_code=ErrorCode.AUDIO_ERROR,
            details=details,
            original_exception=original_exception
        )


def create_error_response(exception: Exception) -> Dict[str, Any]:
    """
    Create a standardized error response from an exception.
    
    Args:
        exception: The exception to convert
    
    Returns:
        Dictionary suitable for JSON response
    """
    if isinstance(exception, CallingSystemError):
        return exception.to_dict()
    
    return {
        'error': True,
        'error_code': ErrorCode.UNKNOWN_ERROR.value,
        'error_name': ErrorCode.UNKNOWN_ERROR.name,
        'message': str(exception),
        'details': {}
    }
