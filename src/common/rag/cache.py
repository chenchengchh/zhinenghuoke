"""
RAG 结果缓存子模块

从 unified_rag_service.py 抽取的 _RAGResultCache 实现。
提供 TTL + LRU 风格的过期清理。
"""
import time
import copy
import threading
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class RAGResultCache:
    """RAG 结果 TTL 缓存。

    支持特性：
    - TTL 过期自动清理
    - 超额时按 (时间分数*0.6 + 置信度分数*0.4) 综合评分淘汰低分条目
    - 线程安全
    - 损坏条目隔离，不会因为单条数据问题导致整体清理失败
    """

    def __init__(
        self,
        ttl_seconds: int = 600,
        max_size: int = 1000,
        target_size: int = 500,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self.target_size = target_size
        self._store: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            cached = self._store.get(key)
            if not cached:
                return None
            try:
                cached_result, cached_time = cached
            except (ValueError, TypeError) as e:
                # 损坏的条目，清理
                self._store.pop(key, None)
                logger.warning(f"清理损坏的缓存条目 key={key}: {e}")
                return None
            if time.time() - cached_time >= self.ttl_seconds:
                self._store.pop(key, None)
                return None
            return copy.deepcopy(cached_result)

    def put(self, key: str, result: Any) -> None:
        with self._lock:
            self._store[key] = (copy.deepcopy(result), time.time())
            self._cleanup_locked()

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def smart_cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        """清理过期和超额缓存条目。

        保护逻辑：
        - 解析 value 元组时使用 try/except 捕获损坏条目
        - 计算分数时同样包 try/except，保证清理流程对未知类型安全降级
        """
        current_time = time.time()
        expired_keys = []
        for key, value in self._store.items():
            try:
                cached_result, cached_time = value
                if current_time - cached_time >= self.ttl_seconds:
                    expired_keys.append(key)
            except (ValueError, TypeError) as e:
                expired_keys.append(key)
                logger.warning(f"清理损坏的缓存条目 key={key}: {e}")

        for key in expired_keys:
            self._store.pop(key, None)

        if len(self._store) <= self.max_size:
            return

        cache_items = []
        for key, value in self._store.items():
            try:
                cached_result, cached_time = value
                time_score = 1.0 - ((current_time - cached_time) / self.ttl_seconds)
                time_score = max(0.0, min(1.0, time_score))
                confidence_score = (
                    getattr(cached_result, "confidence", 0.5)
                    if hasattr(cached_result, "confidence")
                    else 0.5
                )
                total_score = time_score * 0.6 + confidence_score * 0.4
                cache_items.append((key, total_score))
            except Exception as e:
                logger.warning(f"计算缓存分数失败 key={key}: {e}")
                cache_items.append((key, 0.0))

        cache_items.sort(key=lambda item: item[1])
        remove_count = len(self._store) - self.target_size
        for key, _ in cache_items[:remove_count]:
            self._store.pop(key, None)
