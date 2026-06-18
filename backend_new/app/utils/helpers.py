"""
Helper Utilities
================
Common utility functions and graceful shutdown handling.
"""

import os
import sys
import signal
import atexit
import logging
import threading
import re
from typing import Callable, List, Optional, Any, Dict
from datetime import datetime
from functools import wraps


def setup_logging(
    level: str = "INFO",
    format_string: Optional[str] = None,
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Set up logging configuration.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_string: Custom format string
        log_file: Optional file to write logs to
    
    Returns:
        Configured logger
    """
    if format_string is None:
        format_string = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        file_handler = logging.FileHandler(log_file)
        handlers.append(file_handler)
    
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=format_string,
        handlers=handlers
    )
    
    return logging.getLogger("calling_system")


class GracefulShutdown:
    """
    Handles graceful shutdown of the application.
    
    Features:
    - Register cleanup functions
    - Handle SIGTERM and SIGINT
    - Wait for active operations to complete
    - Thread-safe
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._cleanup_handlers: List[Callable] = []
        self._is_shutting_down = False
        self._shutdown_event = threading.Event()
        self._active_operations = 0
        self._operations_lock = threading.Lock()
        
        # Register signal handlers
        self._setup_signal_handlers()
        
        # Register atexit handler
        atexit.register(self._atexit_handler)
        
        self._initialized = True
    
    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown."""
        # Disabled - let Flask handle Ctrl+C naturally
        pass
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        signal_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
        print(f"\n🛑 Received {signal_name}, initiating graceful shutdown...")
        self.shutdown()
    
    def _atexit_handler(self):
        """Handle program exit."""
        if not self._is_shutting_down:
            self.shutdown()
    
    def register_cleanup(self, handler: Callable, priority: int = 50) -> None:
        """
        Register a cleanup handler.
        
        Args:
            handler: Cleanup function to call on shutdown
            priority: Lower numbers run first (default: 50)
        """
        self._cleanup_handlers.append((priority, handler))
        self._cleanup_handlers.sort(key=lambda x: x[0])
    
    def is_shutting_down(self) -> bool:
        """Check if shutdown is in progress."""
        return self._is_shutting_down
    
    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for shutdown signal.
        
        Returns True if shutdown was signaled, False if timeout.
        """
        return self._shutdown_event.wait(timeout)
    
    def start_operation(self) -> None:
        """Mark start of an operation that should complete before shutdown."""
        with self._operations_lock:
            self._active_operations += 1
    
    def end_operation(self) -> None:
        """Mark end of an operation."""
        with self._operations_lock:
            self._active_operations = max(0, self._active_operations - 1)
    
    def shutdown(self, timeout: float = 30.0) -> None:
        """
        Perform graceful shutdown.
        
        Args:
            timeout: Maximum time to wait for operations to complete
        """
        if self._is_shutting_down:
            return
        
        self._is_shutting_down = True
        self._shutdown_event.set()
        
        print("🔄 Starting graceful shutdown...")
        
        # Wait for active operations
        wait_start = datetime.now()
        while self._active_operations > 0:
            elapsed = (datetime.now() - wait_start).total_seconds()
            if elapsed >= timeout:
                print(f"⚠️ Timeout waiting for {self._active_operations} operations")
                break
            threading.Event().wait(0.1)
        
        # Run cleanup handlers
        print(f"🧹 Running {len(self._cleanup_handlers)} cleanup handlers...")
        for priority, handler in self._cleanup_handlers:
            try:
                handler()
            except Exception as e:
                print(f"❌ Cleanup handler error: {e}")
        
        print("✅ Graceful shutdown complete")


# Global shutdown handler
shutdown_handler = GracefulShutdown()


def on_shutdown(priority: int = 50):
    """
    Decorator to register a function as a shutdown handler.
    
    Usage:
        @on_shutdown(priority=10)
        def cleanup_resources():
            ...
    """
    def decorator(func: Callable) -> Callable:
        shutdown_handler.register_cleanup(func, priority)
        return func
    return decorator


def with_operation_tracking(func: Callable) -> Callable:
    """
    Decorator to track operations for graceful shutdown.
    
    Usage:
        @with_operation_tracking
        def process_call():
            ...
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        shutdown_handler.start_operation()
        try:
            return func(*args, **kwargs)
        finally:
            shutdown_handler.end_operation()
    return wrapper


def retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    Decorator for retrying failed operations with exponential backoff.
    
    Args:
        max_attempts: Maximum number of attempts
        delay: Initial delay between retries (seconds)
        backoff: Backoff multiplier
        exceptions: Tuple of exceptions to catch
    
    Usage:
        @retry(max_attempts=3, delay=1.0)
        def flaky_api_call():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        threading.Event().wait(current_delay)
                        current_delay *= backoff
            
            raise last_exception
        return wrapper
    return decorator


class TimeoutError(Exception):
    """Exception raised when operation times out."""
    pass


def timeout(seconds: float):
    """
    Decorator to add timeout to a function.
    
    Note: This uses threading and may not work with all functions.
    
    Usage:
        @timeout(5.0)
        def slow_operation():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = [None]
            exception = [None]
            
            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    exception[0] = e
            
            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            thread.join(seconds)
            
            if thread.is_alive():
                raise TimeoutError(f"Function {func.__name__} timed out after {seconds} seconds")
            
            if exception[0]:
                raise exception[0]
            
            return result[0]
        return wrapper
    return decorator


def utcnow() -> datetime:
    """Get current UTC time."""
    return datetime.utcnow()


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def parse_phone_number(phone: str) -> Dict[str, str]:
    """
    Parse phone number into components.
    
    Returns:
        Dictionary with country_code, national_number, formatted
    """
    # Clean the number
    cleaned = re.sub(r'[^\d+]', '', phone)
    
    # Handle different formats
    if cleaned.startswith('+91'):
        return {
            'country_code': '+91',
            'national_number': cleaned[3:],
            'formatted': f"+91 {cleaned[3:5]} {cleaned[5:9]} {cleaned[9:]}"
        }
    elif cleaned.startswith('+1'):
        return {
            'country_code': '+1',
            'national_number': cleaned[2:],
            'formatted': f"+1 ({cleaned[2:5]}) {cleaned[5:8]}-{cleaned[8:]}"
        }
    else:
        return {
            'country_code': '',
            'national_number': cleaned,
            'formatted': cleaned
        }


def get_env(key: str, default: Any = None, required: bool = False) -> Any:
    """
    Get environment variable with type conversion.
    
    Args:
        key: Environment variable name
        default: Default value if not set
        required: Raise error if not set
    
    Returns:
        Environment variable value
    """
    value = os.environ.get(key)
    
    if value is None:
        if required:
            raise ValueError(f"Required environment variable {key} is not set")
        return default
    
    # Type conversion based on default
    if isinstance(default, bool):
        return value.lower() in ('true', '1', 'yes', 'on')
    elif isinstance(default, int):
        return int(value)
    elif isinstance(default, float):
        return float(value)
    
    return value


def debug_print(*args, enabled: bool = True, prefix: str = "DEBUG"):
    """Conditional debug print."""
    if enabled:
        print(f"[{prefix}]", *args)


class CallContext:
    """
    Context manager for call operations.
    Provides consistent handling of call lifecycle.
    """
    
    def __init__(self, call_uuid: str):
        self.call_uuid = call_uuid
        self.start_time = None
        self.end_time = None
        self.metadata: Dict[str, Any] = {}
    
    def __enter__(self):
        self.start_time = datetime.utcnow()
        shutdown_handler.start_operation()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = datetime.utcnow()
        shutdown_handler.end_operation()
        
        # Log duration
        duration = (self.end_time - self.start_time).total_seconds()
        print(f"📞 Call {self.call_uuid} completed in {format_duration(duration)}")
        
        return False  # Don't suppress exceptions
    
    @property
    def duration(self) -> float:
        """Get call duration in seconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        elif self.start_time:
            return (datetime.utcnow() - self.start_time).total_seconds()
        return 0
