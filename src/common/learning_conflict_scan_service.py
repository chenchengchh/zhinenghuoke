from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, List


class LearningConflictScanService:
    """学习域知识冲突扫描服务，负责缓存与后台刷新。"""

    def __init__(self, repository, knowledge_base=None, conflict_detector_factory=None):
        self.repository = repository
        self.knowledge_base = knowledge_base
        self.conflict_detector_factory = conflict_detector_factory
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, Any] = {
            "timestamp": 0.0,
            "signature": None,
            "payload": None,
            "completed_at": None,
        }
        self._scan_in_progress = False

    def detect(self, limit: int = 50, offset: int = 0, force_refresh: bool = False) -> Dict[str, Any]:
        items = self.repository.list_conflict_scan_items() if self.repository else []
        signature = self.repository.get_conflict_signature(items) if self.repository else (0, "", 0)
        now = time.monotonic()
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        with self._cache_lock:
            cached_signature = self._cache.get("signature")
            cached_timestamp = self._cache.get("timestamp", 0.0)
            cached_payload = self._cache.get("payload")
            if (
                not force_refresh
                and cached_payload is not None
                and cached_signature == signature
                and (now - cached_timestamp) < 120.0
            ):
                return self._slice_payload(cached_payload, limit, offset)

            if not self._scan_in_progress:
                self._scan_in_progress = True
                threading.Thread(
                    target=self._build_cache,
                    args=(items, signature),
                    daemon=True,
                    name="learning-conflict-scan",
                ).start()

            if cached_payload is not None:
                payload = self._slice_payload(cached_payload, limit, offset)
                payload["scan_status"] = "refreshing"
                payload["message"] = "知识冲突结果正在后台刷新。"
                return payload

        return {
            "total": 0,
            "conflicts": [],
            "stats": {},
            "scan_status": "building",
            "last_completed_at": self._cache.get("completed_at"),
            "offset": offset,
            "limit": limit,
            "returned": 0,
            "has_more": False,
            "message": "知识冲突扫描已在后台启动，请稍后重试。",
        }

    def _slice_payload(self, payload: Dict[str, Any], limit: int, offset: int) -> Dict[str, Any]:
        conflicts = list(payload.get("conflicts", []) or [])
        total = len(conflicts)
        sliced = conflicts[offset: offset + limit]
        result = dict(payload)
        result["total"] = total
        result["conflicts"] = sliced
        result["offset"] = offset
        result["limit"] = limit
        result["returned"] = len(sliced)
        result["has_more"] = (offset + len(sliced)) < total
        return result

    def _build_cache(self, items: List[Any], signature: tuple[int, str, int]) -> None:
        try:
            detector_factory = self.conflict_detector_factory
            if detector_factory is None:
                from src.common.conflict_detector import get_conflict_detector

                detector_factory = get_conflict_detector

            detector = detector_factory(self.knowledge_base)
            conflicts = detector.detect_all_conflicts(items)
            completed_at = datetime.now().isoformat()
            payload = {
                "total": len(conflicts),
                "conflicts": [conflict.to_dict() for conflict in conflicts],
                "stats": detector.get_statistics(),
                "scan_status": "ready",
                "last_completed_at": completed_at,
            }
            with self._cache_lock:
                self._cache["timestamp"] = time.monotonic()
                self._cache["signature"] = signature
                self._cache["payload"] = payload
                self._cache["completed_at"] = completed_at
        except Exception:
            with self._cache_lock:
                self._cache["payload"] = {
                    "total": 0,
                    "conflicts": [],
                    "stats": {},
                    "scan_status": "error",
                    "last_completed_at": self._cache.get("completed_at"),
                }
        finally:
            with self._cache_lock:
                self._scan_in_progress = False


__all__ = ["LearningConflictScanService"]
