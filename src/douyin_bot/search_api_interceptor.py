"""
搜索 API 拦截器

将关键词搜索结果监听从爬虫主流程中拆出，统一管理搜索接口批次与队列统计。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base_api_interceptor import BatchAPIInterceptor


class SearchAPIInterceptor(BatchAPIInterceptor):
    """拦截抖音综合搜索接口，按批次缓存供爬虫主流程消费。"""

    _INTERCEPTOR_NAME = "搜索"
    _MAX_BATCHES = 300

    SEARCH_URL_PATTERNS = (
        "aweme/v1/web/search/item",
        "aweme/v1/web/general/search",
    )

    # -- URL 分类 ----------------------------------------------------------

    @classmethod
    def classify_url(cls, url: str) -> str:
        normalized = str(url or "").lower()
        return next((pattern for pattern in cls.SEARCH_URL_PATTERNS if pattern in normalized), "")

    # -- 批次构建 ----------------------------------------------------------

    def _build_batch(
        self,
        *,
        batch_id: int,
        kind_or_pattern: str,
        url: str,
        status: int,
        content_type: str,
        query: Dict[str, List[str]],
        data: Dict[str, Any],
        timestamp: float,
    ) -> Dict[str, Any]:
        return {
            "batch_id": batch_id,
            "kind": "search",
            "matched_pattern": kind_or_pattern,
            "url": url,
            "status": status,
            "content_type": content_type,
            "query": query,
            "data": data,
            "timestamp": timestamp,
        }

    # -- 队列统计 ----------------------------------------------------------

    def get_queue_stats(self) -> Dict[str, Any]:
        with self._lock:
            pending_total = len(self._batches)
            latest_timestamp = 0.0
            latest_batch_id = 0
            latest_url = ""
            for batch in self._batches:
                ts = float(batch.get("timestamp") or 0.0)
                if ts > latest_timestamp:
                    latest_timestamp = ts
                    latest_url = str(batch.get("url", "") or "")
                batch_id = int(batch.get("batch_id", 0) or 0)
                if batch_id > latest_batch_id:
                    latest_batch_id = batch_id
            return {
                "pending_total": pending_total,
                "latest_timestamp": latest_timestamp,
                "latest_batch_id": latest_batch_id,
                "latest_url": latest_url,
                "dropped_batches_count": int(self._dropped_batches_count or 0),
                "peak_pending_batches": int(self._peak_pending_batches or 0),
                "last_dropped_batch_id": int(self._last_dropped_batch_id or 0),
            }
