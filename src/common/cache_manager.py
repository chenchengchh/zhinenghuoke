from threading import Lock
from typing import Dict, Optional

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None


class UnifiedCacheManager:
    def __init__(self):
        self._caches: Dict[str, dict] = {}
        self._locks: Dict[str, Lock] = {}

    def get_cache(self, name: str, maxsize: int = 1000, ttl: int = 300):
        if name not in self._caches:
            self._locks[name] = Lock()
            if TTLCache is not None:
                self._caches[name] = TTLCache(maxsize=maxsize, ttl=ttl)
            else:
                self._caches[name] = {}
        return self._caches[name]

    def get(self, name: str, key: str, default=None):
        cache = self.get_cache(name)
        if isinstance(cache, dict):
            return cache.get(key, default)
        return cache.get(key, default)

    def set(self, name: str, key: str, value):
        cache = self.get_cache(name)
        cache[key] = value

    def clear(self, name: Optional[str] = None):
        if name:
            cache = self._caches.get(name)
            if cache is not None:
                cache.clear()
        else:
            for cache in self._caches.values():
                cache.clear()


_cache_manager_instance = None


def get_cache_manager() -> UnifiedCacheManager:
    global _cache_manager_instance
    if _cache_manager_instance is None:
        _cache_manager_instance = UnifiedCacheManager()
    return _cache_manager_instance
