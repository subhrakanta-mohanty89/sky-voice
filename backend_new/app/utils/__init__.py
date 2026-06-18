"""
Utils Package
=============
Helper functions, security, caching, metrics, and exceptions.
"""

from app.utils.helpers import (
    setup_logging,
    GracefulShutdown,
    shutdown_handler,
    on_shutdown,
    with_operation_tracking,
    retry,
    timeout,
    TimeoutError,
    utcnow,
    format_duration,
    parse_phone_number,
    get_env,
    debug_print,
    CallContext,
)

from app.utils.security import (
    RateLimiter,
    api_rate_limiter,
    ws_rate_limiter,
    rate_limit,
    RateLimitExceeded,
    InputSanitizer,
    IPUtils,
)

from app.utils.cache import (
    LRUCacheWithTTL,
    cached,
    translation_cache,
    tts_cache,
    start_cache_cleanup_thread,
    stop_cache_cleanup_thread,
    get_all_cache_stats,
)

from app.utils.metrics import (
    MetricsCollector,
    metrics,
    MetricNames,
    timed,
    LatencyTracker,
)

from app.utils.exceptions import (
    ErrorCode,
    CallingSystemError,
    ConfigurationError,
    RateLimitError,
    AIServiceError,
    STTError,
    TTSError,
    TranslationError,
    CallError,
    WebSocketError,
    DatabaseError,
    AudioError,
    create_error_response,
)

__all__ = [
    # helpers
    'setup_logging',
    'GracefulShutdown',
    'shutdown_handler',
    'on_shutdown',
    'with_operation_tracking',
    'retry',
    'timeout',
    'TimeoutError',
    'utcnow',
    'format_duration',
    'parse_phone_number',
    'get_env',
    'debug_print',
    'CallContext',
    # security
    'RateLimiter',
    'api_rate_limiter',
    'ws_rate_limiter',
    'rate_limit',
    'RateLimitExceeded',
    'InputSanitizer',
    'IPUtils',
    # cache
    'LRUCacheWithTTL',
    'cached',
    'translation_cache',
    'tts_cache',
    'start_cache_cleanup_thread',
    'stop_cache_cleanup_thread',
    'get_all_cache_stats',
    # metrics
    'MetricsCollector',
    'metrics',
    'MetricNames',
    'timed',
    'LatencyTracker',
    # exceptions
    'ErrorCode',
    'CallingSystemError',
    'ConfigurationError',
    'RateLimitError',
    'AIServiceError',
    'STTError',
    'TTSError',
    'TranslationError',
    'CallError',
    'WebSocketError',
    'DatabaseError',
    'AudioError',
    'create_error_response',
]
