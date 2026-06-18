"""
Enhanced Caching Module
=======================
Provides LRU cache with TTL for translations and TTS.
"""

import hashlib
import time
import threading
from collections import OrderedDict
from typing import Optional, Dict, Any, Callable, TypeVar, Generic
from functools import wraps

from config import Config

# Type variable for generic cache
T = TypeVar('T')


class LRUCacheWithTTL(Generic[T]):
    """
    Thread-safe LRU cache with TTL (Time To Live) support.
    
    Features:
    - Least Recently Used eviction policy
    - Time-based expiration
    - Thread safety
    - Hit/miss statistics
    """
    
    def __init__(
        self,
        max_size: int = 100,
        ttl_seconds: int = 3600,
        name: str = "cache"
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.name = name
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        
        # Statistics
        self._hits = 0
        self._misses = 0
        self._evictions = 0
    
    def _make_key(self, *args, **kwargs) -> str:
        """Create a cache key from arguments."""
        key_data = str(args) + str(sorted(kwargs.items()))
        return hashlib.sha256(key_data.encode()).hexdigest()[:32]
    
    def get(self, key: str) -> Optional[T]:
        """Get an item from cache."""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            
            entry = self._cache[key]
            
            # Check TTL
            if entry['expires_at'] < time.time():
                del self._cache[key]
                self._misses += 1
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            self._hits += 1
            
            return entry['value']
    
    def set(self, key: str, value: T, ttl: Optional[int] = None) -> None:
        """Set an item in cache."""
        with self._lock:
            expires_at = time.time() + (ttl if ttl else self.ttl_seconds)
            
            # If key exists, update and move to end
            if key in self._cache:
                self._cache[key] = {
                    'value': value,
                    'expires_at': expires_at,
                    'created_at': time.time()
                }
                self._cache.move_to_end(key)
                return
            
            # Evict if necessary
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
                self._evictions += 1
            
            # Add new entry
            self._cache[key] = {
                'value': value,
                'expires_at': expires_at,
                'created_at': time.time()
            }
    
    def delete(self, key: str) -> bool:
        """Delete an item from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    def clear(self) -> None:
        """Clear all items from cache."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0
    
    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        removed = 0
        current_time = time.time()
        
        with self._lock:
            keys_to_delete = [
                k for k, v in self._cache.items()
                if v['expires_at'] < current_time
            ]
            for key in keys_to_delete:
                del self._cache[key]
                removed += 1
        
        return removed
    
    @property
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                'name': self.name,
                'size': len(self._cache),
                'max_size': self.max_size,
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': f"{hit_rate:.1f}%",
                'evictions': self._evictions,
                'ttl_seconds': self.ttl_seconds
            }
    
    def __len__(self) -> int:
        return len(self._cache)
    
    def __contains__(self, key: str) -> bool:
        with self._lock:
            if key not in self._cache:
                return False
            if self._cache[key]['expires_at'] < time.time():
                del self._cache[key]
                return False
            return True


def cached(
    cache: LRUCacheWithTTL,
    key_func: Optional[Callable[..., str]] = None
):
    """
    Decorator for caching function results.
    
    Args:
        cache: LRUCacheWithTTL instance
        key_func: Optional function to generate cache key from arguments
    
    Usage:
        @cached(translation_cache)
        def translate(text, source, target):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key
            if key_func:
                key = key_func(*args, **kwargs)
            else:
                key = cache._make_key(*args, **kwargs)
            
            # Check cache
            result = cache.get(key)
            if result is not None:
                return result
            
            # Call function and cache result
            result = func(*args, **kwargs)
            if result is not None:
                cache.set(key, result)
            
            return result
        
        # Add cache access to wrapper
        wrapper.cache = cache
        wrapper.cache_clear = cache.clear
        wrapper.cache_stats = lambda: cache.stats
        
        return wrapper
    return decorator


# ===========================================
# GLOBAL CACHE INSTANCES
# ===========================================

translation_cache = LRUCacheWithTTL(
    max_size=Config.TRANSLATION_CACHE_SIZE,
    ttl_seconds=Config.TRANSLATION_CACHE_TTL,
    name="translation"
)

tts_cache = LRUCacheWithTTL(
    max_size=Config.TTS_CACHE_SIZE,
    ttl_seconds=7200,  # 2 hours for TTS
    name="tts"
)


# ===========================================
# CACHE CLEANUP THREAD
# ===========================================

_cleanup_thread = None
_cleanup_stop_event = threading.Event()


def _cache_cleanup_worker(interval_seconds: int):
    """Background worker to clean up expired cache entries."""
    while not _cleanup_stop_event.wait(interval_seconds):
        try:
            removed_translation = translation_cache.cleanup_expired()
            removed_tts = tts_cache.cleanup_expired()
            
            if removed_translation > 0 or removed_tts > 0:
                print(f"🧹 Cache cleanup: removed {removed_translation} translations, {removed_tts} TTS entries")
        except Exception as e:
            print(f"❌ Cache cleanup error: {e}")


def start_cache_cleanup_thread(interval_seconds: int = 300):
    """Start the cache cleanup background thread."""
    global _cleanup_thread
    
    if _cleanup_thread is not None and _cleanup_thread.is_alive():
        return
    
    _cleanup_stop_event.clear()
    _cleanup_thread = threading.Thread(
        target=_cache_cleanup_worker,
        args=(interval_seconds,),
        daemon=True,
        name="CacheCleanup"
    )
    _cleanup_thread.start()
    print(f"🧹 Cache cleanup thread started (interval: {interval_seconds}s)")


def stop_cache_cleanup_thread():
    """Stop the cache cleanup background thread."""
    global _cleanup_thread
    
    _cleanup_stop_event.set()
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)
        _cleanup_thread = None
    print("🧹 Cache cleanup thread stopped")


def get_all_cache_stats() -> Dict[str, Any]:
    """Get statistics for all caches."""
    return {
        "translation": translation_cache.stats,
        "tts": tts_cache.stats
    }
