"""
评论 API 拦截器

将评论接口监听从爬虫主流程中拆出，降低页面动作与网络解析的耦合。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base_api_interceptor import BatchAPIInterceptor


class CommentAPIInterceptor(BatchAPIInterceptor):
    """拦截抖音评论与回复评论接口，按批次缓存供爬虫主流程消费。"""

    _INTERCEPTOR_NAME = "评论"
    _MAX_BATCHES = 500

    # -- URL 分类 ----------------------------------------------------------

    @staticmethod
    def classify_url(url: str) -> str:
        normalized = (url or "").lower()
        if "aweme/v1/web/comment/list/reply" in normalized:
            return "reply"
        if "aweme/v1/web/comment/list" in normalized:
            return "comment"
        return ""

    # -- 队列统计 ----------------------------------------------------------

    def get_queue_stats(self) -> Dict[str, Any]:
        with self._lock:
            pending_total = len(self._batches)
            pending_comment = 0
            pending_reply = 0
            latest_timestamp = 0.0
            latest_comment_timestamp = 0.0
            latest_reply_timestamp = 0.0
            latest_batch_id = 0

            for batch in self._batches:
                ts = float(batch.get("timestamp") or 0.0)
                if ts > latest_timestamp:
                    latest_timestamp = ts
                batch_id = int(batch.get("batch_id", 0) or 0)
                if batch_id > latest_batch_id:
                    latest_batch_id = batch_id
                kind = str(batch.get("kind") or "")
                if kind == "comment":
                    pending_comment += 1
                    if ts > latest_comment_timestamp:
                        latest_comment_timestamp = ts
                elif kind == "reply":
                    pending_reply += 1
                    if ts > latest_reply_timestamp:
                        latest_reply_timestamp = ts

            return {
                "pending_total": pending_total,
                "pending_comment": pending_comment,
                "pending_reply": pending_reply,
                "latest_timestamp": latest_timestamp,
                "latest_comment_timestamp": latest_comment_timestamp,
                "latest_reply_timestamp": latest_reply_timestamp,
                "latest_batch_id": latest_batch_id,
                "dropped_batches_count": int(self._dropped_batches_count or 0),
                "peak_pending_batches": int(self._peak_pending_batches or 0),
                "last_dropped_batch_id": int(self._last_dropped_batch_id or 0),
            }
