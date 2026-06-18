"""
Metrics and Monitoring Module
=============================
Provides Prometheus-compatible metrics for monitoring the calling system.
"""

import time
import threading
from collections import defaultdict
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta
from functools import wraps
import statistics

from config import Config


class MetricNames:
    """Standard metric names for the calling system."""
    # Request metrics
    HTTP_REQUESTS_TOTAL = "http_requests_total"
    HTTP_RESPONSES_TOTAL = "http_responses_total"
    HTTP_REQUEST_DURATION = "http_request_duration_seconds"
    
    # Call metrics
    CALLS_TOTAL = "calls_total"
    CALLS_ACTIVE = "calls_active"
    CALL_DURATION = "call_duration_seconds"
    
    # STT metrics
    STT_REQUESTS_TOTAL = "stt_requests_total"
    STT_LATENCY = "stt_latency_ms"
    STT_ERRORS_TOTAL = "stt_errors_total"
    
    # TTS metrics
    TTS_REQUESTS_TOTAL = "tts_requests_total"
    TTS_LATENCY = "tts_latency_ms"
    TTS_ERRORS_TOTAL = "tts_errors_total"
    
    # Translation metrics
    TRANSLATION_REQUESTS_TOTAL = "translation_requests_total"
    TRANSLATION_LATENCY = "translation_latency_ms"
    TRANSLATION_ERRORS_TOTAL = "translation_errors_total"
    TRANSLATION_CACHE_HITS = "translation_cache_hits_total"
    TRANSLATION_CACHE_MISSES = "translation_cache_misses_total"
    
    # Error metrics
    ERRORS_TOTAL = "errors_total"
    RATE_LIMIT_EXCEEDED_TOTAL = "rate_limit_exceeded_total"
    
    # WebSocket metrics
    WS_CONNECTIONS_ACTIVE = "ws_connections_active"
    WS_MESSAGES_TOTAL = "ws_messages_total"


class MetricsCollector:
    """
    Prometheus-compatible metrics collector.
    
    Collects:
    - Counters (monotonically increasing values)
    - Gauges (values that can go up or down)
    - Histograms (distribution of values)
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        
        # Counters
        self._counters: Dict[str, float] = defaultdict(float)
        
        # Gauges
        self._gauges: Dict[str, float] = defaultdict(float)
        
        # Histograms (store all values)
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._histogram_max_samples = 1000
        
        # Labels for metrics
        self._labels: Dict[str, Dict[str, str]] = {}
        
        # Start time
        self._start_time = time.time()
    
    # Counter operations
    def counter_inc(self, name: str, value: float = 1, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter."""
        if not Config.ENABLE_METRICS:
            return
        key = self._make_key(name, labels)
        with self._lock:
            self._counters[key] += value
            if labels:
                self._labels[key] = labels
    
    def counter_get(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Get a counter value."""
        key = self._make_key(name, labels)
        with self._lock:
            return self._counters.get(key, 0)
    
    # Gauge operations
    def gauge_set(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge value."""
        if not Config.ENABLE_METRICS:
            return
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] = value
            if labels:
                self._labels[key] = labels
    
    def gauge_inc(self, name: str, value: float = 1, labels: Optional[Dict[str, str]] = None) -> None:
        """Increment a gauge."""
        if not Config.ENABLE_METRICS:
            return
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] += value
            if labels:
                self._labels[key] = labels
    
    def gauge_dec(self, name: str, value: float = 1, labels: Optional[Dict[str, str]] = None) -> None:
        """Decrement a gauge."""
        if not Config.ENABLE_METRICS:
            return
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] -= value
            if labels:
                self._labels[key] = labels
    
    def gauge_get(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Get a gauge value."""
        key = self._make_key(name, labels)
        with self._lock:
            return self._gauges.get(key, 0)
    
    # Histogram operations
    def histogram_observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record an observation in a histogram."""
        if not Config.ENABLE_METRICS:
            return
        key = self._make_key(name, labels)
        with self._lock:
            self._histograms[key].append(value)
            # Limit samples
            if len(self._histograms[key]) > self._histogram_max_samples:
                self._histograms[key] = self._histograms[key][-self._histogram_max_samples:]
            if labels:
                self._labels[key] = labels
    
    def histogram_get(self, name: str, labels: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        """Get histogram statistics."""
        key = self._make_key(name, labels)
        with self._lock:
            values = self._histograms.get(key, [])
            if not values:
                return {'count': 0, 'sum': 0, 'avg': 0, 'min': 0, 'max': 0, 'p50': 0, 'p95': 0, 'p99': 0}
            
            sorted_values = sorted(values)
            count = len(values)
            return {
                'count': count,
                'sum': sum(values),
                'avg': statistics.mean(values),
                'min': min(values),
                'max': max(values),
                'p50': sorted_values[int(count * 0.5)],
                'p95': sorted_values[int(count * 0.95)] if count >= 20 else sorted_values[-1],
                'p99': sorted_values[int(count * 0.99)] if count >= 100 else sorted_values[-1]
            }
    
    def _make_key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        """Create a unique key for a metric."""
        if labels:
            label_str = ','.join(f'{k}={v}' for k, v in sorted(labels.items()))
            return f"{name}{{{label_str}}}"
        return name
    
    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics in a dictionary format."""
        with self._lock:
            return {
                'uptime_seconds': time.time() - self._start_time,
                'counters': dict(self._counters),
                'gauges': dict(self._gauges),
                'histograms': {k: self.histogram_get(k) for k in self._histograms.keys()}
            }
    
    def get_prometheus_format(self) -> str:
        """Get metrics in Prometheus text format."""
        lines = []
        lines.append(f"# Uptime: {time.time() - self._start_time:.0f}s")
        lines.append("")
        
        with self._lock:
            # Counters
            for name, value in self._counters.items():
                lines.append(f"{name} {value}")
            
            # Gauges
            for name, value in self._gauges.items():
                lines.append(f"{name} {value}")
            
            # Histograms (simplified)
            for name in self._histograms.keys():
                stats = self.histogram_get(name)
                base_name = name.split('{')[0]
                lines.append(f"{base_name}_count {stats['count']}")
                lines.append(f"{base_name}_sum {stats['sum']}")
        
        return '\n'.join(lines)
    
    def reset(self) -> None:
        """Reset all metrics."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._start_time = time.time()


# Global metrics collector
metrics = MetricsCollector()


def timed(metric_name: str, labels: Optional[Dict[str, str]] = None):
    """
    Decorator to measure function execution time.
    
    Usage:
        @timed("api_call_duration")
        def api_call():
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                duration = (time.time() - start) * 1000  # ms
                metrics.histogram_observe(metric_name, duration, labels)
        return wrapper
    return decorator


class LatencyTracker:
    """
    Context manager for tracking operation latency.
    
    Usage:
        with LatencyTracker("stt_latency") as tracker:
            # do work
            pass
        print(f"Took {tracker.duration_ms}ms")
    """
    
    def __init__(self, metric_name: str, labels: Optional[Dict[str, str]] = None):
        self.metric_name = metric_name
        self.labels = labels
        self.start_time = None
        self.end_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.time()
        if Config.ENABLE_METRICS:
            metrics.histogram_observe(
                self.metric_name,
                self.duration_ms,
                self.labels
            )
        return False
    
    @property
    def duration_ms(self) -> float:
        """Get duration in milliseconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        elif self.start_time:
            return (time.time() - self.start_time) * 1000
        return 0
