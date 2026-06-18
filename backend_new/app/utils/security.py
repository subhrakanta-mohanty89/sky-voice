"""
Security Utilities Module
=========================
Provides security utilities including:
- Input sanitization
- Rate limiting
- Phone number validation
- Request validation
"""

import re
import time
import threading
import html
import hashlib
import hmac
import secrets
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple, Callable
from functools import wraps
from datetime import datetime, timedelta

from config import Config


class RateLimiter:
    """
    Token bucket rate limiter with sliding window.
    
    Thread-safe implementation that limits requests per key (IP, user, etc.)
    """
    
    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
        burst_size: Optional[int] = None
    ):
        """
        Initialize rate limiter.
        
        Args:
            max_requests: Maximum requests per window
            window_seconds: Time window in seconds
            burst_size: Maximum burst size (defaults to max_requests)
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.burst_size = burst_size or max_requests
        
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.RLock()
    
    def is_allowed(self, key: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if a request is allowed for the given key.
        
        Returns:
            Tuple of (is_allowed, info_dict)
        """
        current_time = time.time()
        window_start = current_time - self.window_seconds
        
        with self._lock:
            # Clean up old requests
            self._requests[key] = [
                t for t in self._requests[key] if t > window_start
            ]
            
            request_count = len(self._requests[key])
            remaining = max(0, self.max_requests - request_count)
            
            info = {
                'allowed': request_count < self.max_requests,
                'current': request_count,
                'limit': self.max_requests,
                'remaining': remaining,
                'reset_at': window_start + self.window_seconds,
                'retry_after': None
            }
            
            if request_count < self.max_requests:
                self._requests[key].append(current_time)
                return True, info
            else:
                # Calculate when the oldest request will expire
                if self._requests[key]:
                    oldest = min(self._requests[key])
                    info['retry_after'] = int(oldest + self.window_seconds - current_time) + 1
                return False, info
    
    def get_stats(self, key: str) -> Dict[str, Any]:
        """Get rate limit stats for a key."""
        current_time = time.time()
        window_start = current_time - self.window_seconds
        
        with self._lock:
            requests = [t for t in self._requests[key] if t > window_start]
            return {
                'current': len(requests),
                'limit': self.max_requests,
                'remaining': max(0, self.max_requests - len(requests)),
                'window_seconds': self.window_seconds
            }
    
    def reset(self, key: str) -> None:
        """Reset rate limit for a key."""
        with self._lock:
            self._requests[key] = []
    
    def reset_all(self) -> None:
        """Reset all rate limits."""
        with self._lock:
            self._requests.clear()


# Global rate limiter instances
api_rate_limiter = RateLimiter(
    max_requests=Config.RATE_LIMIT_PER_MINUTE,
    window_seconds=60
)

ws_rate_limiter = RateLimiter(
    max_requests=200,  # Higher limit for WebSocket messages
    window_seconds=60
)


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded."""
    
    def __init__(self, message: str, retry_after: Optional[int] = None):
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)


def rate_limit(
    limiter: RateLimiter,
    key_func: Optional[Callable] = None,
    on_exceeded: Optional[Callable] = None
):
    """
    Decorator for rate limiting function calls.
    
    Args:
        limiter: RateLimiter instance
        key_func: Function to extract key from args/kwargs (default: first arg)
        on_exceeded: Function to call when rate limit exceeded
    
    Usage:
        @rate_limit(api_rate_limiter, key_func=lambda req: req.remote_addr)
        def api_endpoint(request):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get key
            if key_func:
                key = key_func(*args, **kwargs)
            elif args:
                key = str(args[0])
            else:
                key = "default"
            
            allowed, info = limiter.is_allowed(key)
            
            if not allowed:
                if on_exceeded:
                    return on_exceeded(info)
                raise RateLimitExceeded(
                    f"Rate limit exceeded. Retry after {info['retry_after']} seconds.",
                    retry_after=info['retry_after']
                )
            
            return func(*args, **kwargs)
        
        wrapper.rate_limiter = limiter
        return wrapper
    return decorator


class InputSanitizer:
    """
    Input sanitization utilities for security.
    """
    
    # Patterns for validation
    PHONE_PATTERN = re.compile(r'^\+?[1-9]\d{6,14}$')
    EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    UUID_PATTERN = re.compile(r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$')
    
    # Dangerous characters/patterns
    SQL_INJECTION_PATTERNS = [
        r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|ALTER|CREATE)\b)",
        r"(--|\;|\/\*|\*\/)",
        r"(\bOR\b\s+\d+\s*=\s*\d+)",
        r"(\bAND\b\s+\d+\s*=\s*\d+)"
    ]
    
    @classmethod
    def sanitize_string(cls, value: str, max_length: int = 1000) -> str:
        """
        Sanitize a string input.
        
        Args:
            value: Input string
            max_length: Maximum allowed length
        
        Returns:
            Sanitized string
        """
        if not value:
            return ""
        
        # Truncate
        value = value[:max_length]
        
        # Escape HTML
        value = html.escape(value)
        
        return value.strip()
    
    @classmethod
    def validate_phone_number(cls, phone: str) -> bool:
        """Validate phone number format."""
        if not phone:
            return False
        cleaned = re.sub(r'[^\d+]', '', phone)
        return bool(cls.PHONE_PATTERN.match(cleaned))
    
    @classmethod
    def validate_email(cls, email: str) -> bool:
        """Validate email format."""
        if not email:
            return False
        return bool(cls.EMAIL_PATTERN.match(email.strip().lower()))
    
    @classmethod
    def validate_uuid(cls, uuid: str) -> bool:
        """Validate UUID format."""
        if not uuid:
            return False
        return bool(cls.UUID_PATTERN.match(uuid.strip()))
    
    @classmethod
    def check_sql_injection(cls, value: str) -> bool:
        """
        Check if value contains potential SQL injection.
        
        Returns:
            True if potential injection detected
        """
        if not value:
            return False
        
        value_upper = value.upper()
        for pattern in cls.SQL_INJECTION_PATTERNS:
            if re.search(pattern, value_upper, re.IGNORECASE):
                return True
        return False
    
    @classmethod
    def sanitize_phone_number(cls, phone: str) -> str:
        """Sanitize phone number by removing non-digit characters except +."""
        if not phone:
            return ""
        return re.sub(r'[^\d+]', '', phone)


class IPUtils:
    """Utilities for IP address handling."""
    
    @staticmethod
    def get_client_ip(request) -> str:
        """
        Get client IP address from request, handling proxies.
        
        Args:
            request: Flask request object
        
        Returns:
            Client IP address
        """
        # Check for X-Forwarded-For header (behind proxy)
        if request.headers.get('X-Forwarded-For'):
            # Take the first IP in the chain
            return request.headers.get('X-Forwarded-For').split(',')[0].strip()
        
        # Check for X-Real-IP header
        if request.headers.get('X-Real-IP'):
            return request.headers.get('X-Real-IP')
        
        # Fall back to remote address
        return request.remote_addr or '127.0.0.1'
    
    @staticmethod
    def is_private_ip(ip: str) -> bool:
        """Check if IP is private/internal."""
        private_patterns = [
            r'^10\.',
            r'^172\.(1[6-9]|2[0-9]|3[0-1])\.',
            r'^192\.168\.',
            r'^127\.',
            r'^localhost$',
        ]
        
        for pattern in private_patterns:
            if re.match(pattern, ip):
                return True
        return False


class TokenGenerator:
    """Secure token generation utilities."""
    
    @staticmethod
    def generate_api_key(length: int = 32) -> str:
        """Generate a secure API key."""
        return secrets.token_urlsafe(length)
    
    @staticmethod
    def generate_session_token(length: int = 64) -> str:
        """Generate a secure session token."""
        return secrets.token_hex(length)
    
    @staticmethod
    def hash_password(password: str, salt: Optional[bytes] = None) -> Tuple[str, str]:
        """
        Hash a password with salt.
        
        Returns:
            Tuple of (hash, salt)
        """
        if salt is None:
            salt = secrets.token_bytes(32)
        
        password_hash = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode(),
            salt,
            100000
        )
        
        return password_hash.hex(), salt.hex()
    
    @staticmethod
    def verify_password(password: str, password_hash: str, salt: str) -> bool:
        """Verify a password against its hash."""
        new_hash, _ = TokenGenerator.hash_password(password, bytes.fromhex(salt))
        return hmac.compare_digest(new_hash, password_hash)


class SignatureVerifier:
    """Request signature verification for webhooks."""
    
    def __init__(self, secret_key: str):
        self.secret_key = secret_key
    
    def create_signature(self, payload: str) -> str:
        """Create HMAC signature for payload."""
        return hmac.new(
            self.secret_key.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
    
    def verify_signature(self, payload: str, signature: str) -> bool:
        """Verify HMAC signature."""
        expected = self.create_signature(payload)
        return hmac.compare_digest(expected, signature)
