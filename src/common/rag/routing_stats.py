"""
RAG 路由统计聚合器

从 unified_rag_service.py 抽取的 _RAGRoutingStatsCollector 实现。

阶段四改造：集成 MetricsStore 进行持久化，snapshot 时自动写入磁盘。
"""
import threading
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class RAGRoutingStatsCollector:
    """路由统计聚合器。

    记录四类指标：
    - pipeline_routes: 流水线路由选择（如 modular / agentic）
    - execution_routes: 执行路由选择
    - execution_executors: 实际执行的 executor
    - handoff_reasons: 路由切换原因

    用法：
    - result.metadata["_routing_stats_capture"] = {
          "pipeline_route_name": "modular",
          "execution_route_name": "deep",
          "execution_executor": "react",
          "handoff_reason": "low_confidence"
      }
    - stats.record_from_result(result)
    - stats.snapshot() → Dict
    """

    def __init__(self, persist_path: Optional[str] = None, flush_interval: int = 60):
        self._stats: Dict[str, Dict[str, int]] = {
            "pipeline_routes": {},
            "execution_routes": {},
            "execution_executors": {},
            "handoff_reasons": {},
        }
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._flush_interval = flush_interval
        self._last_flush = 0.0
        # 阶段四：使用 MetricsStore 持久化
        self._metrics_store = None
        if persist_path:
            try:
                from .metrics_store import get_metrics_store
                self._metrics_store = get_metrics_store()
            except Exception as e:
                logger.debug(f"MetricsStore 不可用，仅本地保留: {e}")

    def record_from_result(self, result: Optional[Any]) -> None:
        if not result:
            return
        metadata = getattr(result, "metadata", None) or {}
        if not isinstance(metadata, dict):
            return
        capture = metadata.get("_routing_stats_capture", {}) or {}
        self._increment("pipeline_routes", capture.get("pipeline_route_name", ""))
        self._increment("execution_routes", capture.get("execution_route_name", ""))
        self._increment("execution_executors", capture.get("execution_executor", ""))
        self._increment("handoff_reasons", capture.get("handoff_reason", ""))

    def _increment(self, bucket_name: str, key: str) -> None:
        if not key:
            return
        with self._lock:
            bucket = self._stats.setdefault(bucket_name, {})
            bucket[key] = int(bucket.get(key, 0) or 0) + 1

    def snapshot(self, force_flush: bool = False) -> Dict[str, Any]:
        """获取统计快照，可选强制持久化。"""
        with self._lock:
            snap = {
                "pipeline_routes": dict(self._stats.get("pipeline_routes", {}) or {}),
                "execution_routes": dict(self._stats.get("execution_routes", {}) or {}),
                "execution_executors": dict(
                    self._stats.get("execution_executors", {}) or {}
                ),
                "handoff_reasons": dict(self._stats.get("handoff_reasons", {}) or {}),
            }
        # 定期持久化到磁盘
        if self._metrics_store and (
            force_flush or time.time() - self._last_flush > self._flush_interval
        ):
            try:
                self._metrics_store.update_batch({
                    "routing_stats_pipeline_routes": snap["pipeline_routes"],
                    "routing_stats_execution_routes": snap["execution_routes"],
                    "routing_stats_execution_executors": snap["execution_executors"],
                    "routing_stats_handoff_reasons": snap["handoff_reasons"],
                })
                self._last_flush = time.time()
            except Exception as e:
                logger.warning(f"持久化路由统计失败: {e}")
        # 旧版持久化方式（fallback）
        elif self._persist_path and (
            force_flush or time.time() - self._last_flush > self._flush_interval
        ):
            self._flush_to_disk(snap)
            self._last_flush = time.time()
        return snap

    def reset(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = {}

    def _flush_to_disk(self, snapshot: Dict[str, Any]) -> None:
        try:
            import json
            import os
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"持久化路由统计失败: {e}")
