"""Memory management utilities for handling limited server memory."""
import gc
import sys
import logging
from functools import wraps
from datetime import datetime
import psutil
import os

logger = logging.getLogger(__name__)

# Cache size management
MAX_CACHE_SIZE_MB = 100  # Keep cache under 100MB
_CACHE_SIZE_BYTES = MAX_CACHE_SIZE_MB * 1024 * 1024


def get_memory_usage_mb():
    """Get current process memory usage in MB."""
    try:
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    except Exception as e:
        logger.warning(f"Could not get memory usage: {e}")
        return 0


def log_memory_status():
    """Log current memory usage."""
    usage_mb = get_memory_usage_mb()
    logger.info(f"[MEMORY] Current usage: {usage_mb:.1f}MB")
    return usage_mb


def aggressive_gc():
    """Force garbage collection and return freed memory estimate."""
    before = get_memory_usage_mb()
    gc.collect()
    after = get_memory_usage_mb()
    freed = before - after
    if freed > 0:
        logger.info(f"[GC] Freed {freed:.1f}MB (before: {before:.1f}MB, after: {after:.1f}MB)")
    return freed


def estimate_object_size(obj) -> int:
    """Estimate the size of a Python object in bytes."""
    try:
        return sys.getsizeof(obj)
    except Exception:
        return 0


def cleanup_on_request_end(f):
    """Decorator to ensure aggressive GC after each request."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            before_mem = get_memory_usage_mb()
            result = f(*args, **kwargs)
            after_mem = get_memory_usage_mb()
            
            # If memory usage grew significantly, trigger GC
            if after_mem > before_mem + 50:  # More than 50MB growth
                logger.info(f"[CLEANUP] Memory grew by {after_mem - before_mem:.1f}MB, triggering GC")
                aggressive_gc()
            
            return result
        except Exception as e:
            logger.error(f"Error in request handler: {e}")
            aggressive_gc()
            raise
    
    return decorated_function


class CacheManager:
    """Manage cache size and eviction."""
    
    def __init__(self, max_size_mb=100):
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.cache = {}
        self.access_times = {}
    
    def get_total_size(self):
        """Calculate total cache size in bytes."""
        total = 0
        for key, value in self.cache.items():
            total += estimate_object_size(value)
        return total
    
    def set(self, key, value):
        """Store value in cache, evicting if necessary."""
        current_size = self.get_total_size()
        value_size = estimate_object_size(value)
        
        # Check if adding this would exceed limit
        if current_size + value_size > self.max_size_bytes:
            self._evict_lru()
        
        self.cache[key] = value
        self.access_times[key] = datetime.now()
        logger.debug(f"[CACHE] Stored key={key}, size={value_size / (1024*1024):.1f}MB")
    
    def get(self, key):
        """Retrieve value from cache."""
        if key in self.cache:
            self.access_times[key] = datetime.now()
            return self.cache[key]
        return None
    
    def exists(self, key):
        """Check if key exists in cache."""
        return key in self.cache
    
    def delete(self, key):
        """Remove key from cache."""
        if key in self.cache:
            del self.cache[key]
            del self.access_times[key]
            logger.debug(f"[CACHE] Deleted key={key}")
    
    def clear(self):
        """Clear entire cache."""
        self.cache.clear()
        self.access_times.clear()
        logger.info("[CACHE] Cleared all entries")
        aggressive_gc()
    
    def _evict_lru(self):
        """Evict least recently used entry."""
        if not self.access_times:
            return
        
        # Find LRU key
        lru_key = min(self.access_times.keys(), key=lambda k: self.access_times[k])
        
        # Remove it
        del self.cache[lru_key]
        del self.access_times[lru_key]
        
        logger.info(f"[CACHE] Evicted LRU key={lru_key}")
        aggressive_gc()
    
    def get_stats(self):
        """Get cache statistics."""
        size_mb = self.get_total_size() / (1024 * 1024)
        return {
            'entries': len(self.cache),
            'size_mb': size_mb,
            'max_mb': self.max_size_bytes / (1024 * 1024)
        }
