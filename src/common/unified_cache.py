"""
统一缓存管理器

提供 TTL + LRU 风格的安全缓存，替代各模块散落的自实现缓存逻辑。

特性：
- TTL 过期自动清理
- 超额时按访问时间淘汰最旧条目（LRU 简化版）
- 线程安全（RLock）
- 统计信息：命中率、平均延迟、大小
- 命名空间隔离（不同业务用不同 cache_name）
"""
import time
import threading
import logging
from typing import Any, Optional, Dict, Tuple, List
from collections import OrderedDict
from functools import lru_cache

logger = logging.getLogger(__name__)


class SafeTTLCache:
    """带 TTL 的 LRU 缓存。

    内部使用 OrderedDict 实现 LRU 淘汰（删除最旧项）。
    """

    def __init__(self, maxsize: int = 1000, ttl: int = 300):
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(1, int(ttl))
        self._store: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()
        self._lock = threading.RLock()
        # 统计
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expirations": 0,
            "writes": 0,
        }

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，过期或不存在返回 None。"""
        with self._lock:
            item = self._store.get(key)
            if not item:
                self._stats["misses"] += 1
                return None
            value, ts = item
            if time.time() - ts >= self.ttl:
                # 过期清理
                del self._store[key]
                self._stats["expirations"] += 1
                self._stats["misses"] += 1
                return None
            # LRU：移到末尾表示最近使用
            self._store.move_to_end(key)
            self._stats["hits"] += 1
            return value

    def set(self, key: str, value: Any) -> None:
        """设置缓存值。"""
        with self._lock:
            # 若 key 已存在，先删除以重置位置
            if key in self._store:
                del self._store[key]
            self._store[key] = (value, time.time())
            self._stats["writes"] += 1
            # 超额淘汰
            while len(self._store) > self.maxsize:
                # 弹出最早插入的（即最少使用）
                self._store.popitem(last=False)
                self._stats["evictions"] += 1

    def delete(self, key: str) -> bool:
        """删除指定 key，返回是否存在。"""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """清空缓存（保留统计）。"""
        with self._lock:
            self._store.clear()

    def cleanup_expired(self) -> int:
        """主动清理过期条目，返回清理数量。"""
        removed = 0
        with self._lock:
            current_time = time.time()
            expired_keys = [
                key for key, (_, ts) in self._store.items()
                if current_time - ts >= self.ttl
            ]
            for key in expired_keys:
                del self._store[key]
                removed += 1
                self._stats["expirations"] += 1
        return removed

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def stats(self) -> Dict[str, Any]:
        """获取统计信息。"""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = self._stats["hits"] / total if total > 0 else 0.0
            return {
                **self._stats,
                "size": len(self._store),
                "maxsize": self.maxsize,
                "ttl": self.ttl,
                "hit_rate": round(hit_rate, 4),
            }

    def reset_stats(self) -> None:
        """重置统计。"""
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0

    def __len__(self) -> int:
        return self.size

    def __contains__(self, key: str) -> bool:
        """支持 `key in cache` 语法。"""
        return self.get(key) is not None


class UnifiedCacheManager:
    """统一管理多个命名缓存。"""

    def __init__(self):
        self._caches: Dict[str, SafeTTLCache] = {}
        self._lock = threading.RLock()
        # 默认配置
        self._defaults: Dict[str, Dict[str, int]] = {
            "rag_retriever": {"maxsize": 1000, "ttl": 300},
            "hybrid_retriever": {"maxsize": 1000, "ttl": 300},
            "llm_response": {"maxsize": 500, "ttl": 600},
            "embedding": {"maxsize": 5000, "ttl": 3600},
            "intent_detection": {"maxsize": 2000, "ttl": 300},
        }

    def get_cache(
        self,
        name: str,
        maxsize: Optional[int] = None,
        ttl: Optional[int] = None,
    ) -> SafeTTLCache:
        """获取或创建命名缓存。"""
        with self._lock:
            if name not in self._caches:
                default = self._defaults.get(name, {"maxsize": 1000, "ttl": 300})
                self._caches[name] = SafeTTLCache(
                    maxsize=maxsize or default["maxsize"],
                    ttl=ttl or default["ttl"],
                )
                logger.debug(f"创建命名缓存: {name}")
            return self._caches[name]

    def clear_cache(self, name: str) -> bool:
        """清空指定命名缓存。"""
        with self._lock:
            if name in self._caches:
                self._caches[name].clear()
                return True
            return False

    def clear_all(self) -> None:
        """清空所有缓存。"""
        with self._lock:
            for cache in self._caches.values():
                cache.clear()

    def cleanup_all_expired(self) -> Dict[str, int]:
        """清理所有缓存的过期条目。"""
        with self._lock:
            return {
                name: cache.cleanup_expired()
                for name, cache in self._caches.items()
            }

    def all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有缓存的统计信息。"""
        with self._lock:
            return {name: cache.stats() for name, cache in self._caches.items()}

    def list_caches(self) -> List[str]:
        """列出所有缓存名称。"""
        with self._lock:
            return list(self._caches.keys())


@lru_cache(maxsize=1)
def get_unified_cache_manager() -> UnifiedCacheManager:
    """获取统一缓存管理器单例（lru_cache 线程安全）。"""
    return UnifiedCacheManager()
