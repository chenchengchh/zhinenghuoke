"""
增强智能客服服务 - 意图识别缓存

线程安全的意图识别缓存实现，O(1)淘汰策略
"""
import hashlib
import time
import copy
import threading
from collections import OrderedDict
from typing import Dict, Optional, Any


class IntentCache:
    """意图识别缓存（线程安全，O(1)淘汰）"""

    def __init__(self, max_size=1000, ttl=300):
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self.max_size = max_size
        self.ttl = ttl
        self._lock = threading.Lock()

    def _make_key(self, message: str, session_id: str, context_signature: str = "") -> str:
        return hashlib.md5(f"{session_id}:{message}:{context_signature}".encode()).hexdigest()

    def get(self, message: str, session_id: str, context_signature: str = "") -> Optional[Dict]:
        key = self._make_key(message, session_id, context_signature)
        with self._lock:
            if key in self._cache:
                if time.time() - self._timestamps[key] < self.ttl:
                    self._cache.move_to_end(key)
                    return copy.deepcopy(self._cache[key])
                else:
                    del self._cache[key]
                    del self._timestamps[key]
        return None

    def set(self, message: str, session_id: str, result: Dict, context_signature: str = ""):
        key = self._make_key(message, session_id, context_signature)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_size:
                try:
                    oldest_key, _ = self._cache.popitem(last=False)
                    self._timestamps.pop(oldest_key, None)
                except KeyError:
                    self._cache.clear()
                    self._timestamps.clear()
            self._cache[key] = copy.deepcopy(result)
            self._timestamps[key] = time.time()

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()
