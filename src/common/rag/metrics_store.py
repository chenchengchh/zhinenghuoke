"""
RAG 指标持久化存储

将关键指标（RAG 路由、缓存命中率、检索延迟）定期写入磁盘，
支持重启后历史数据分析。
"""
import os
import json
import time
import threading
import logging
from typing import Dict, Any, Optional
from functools import lru_cache

logger = logging.getLogger(__name__)


class MetricsStore:
    """线程安全的指标文件持久化器。"""

    def __init__(self, file_path: str = "data/rag_metrics.json"):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        """从文件加载历史指标。"""
        if not os.path.exists(self.file_path):
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"加载指标文件失败: {e}")
            return {}

    def _flush(self) -> None:
        """写入磁盘。"""
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            tmp_path = self.file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            # 原子替换
            if os.path.exists(self.file_path):
                os.replace(tmp_path, self.file_path)
            else:
                os.rename(tmp_path, self.file_path)
        except Exception as e:
            logger.warning(f"持久化指标失败: {e}")

    def set(self, key: str, value: Any) -> None:
        """设置一个指标值。"""
        with self._lock:
            self._data[key] = value
            self._data[f"{key}_updated_at"] = time.time()
            self._flush()

    def get(self, key: str, default: Any = None) -> Any:
        """获取一个指标值。"""
        with self._lock:
            return self._data.get(key, default)

    def increment(self, key: str, delta: int = 1) -> int:
        """原子递增一个计数器，返回新值。"""
        with self._lock:
            current = int(self._data.get(key, 0) or 0)
            new_value = current + delta
            self._data[key] = new_value
            self._data[f"{key}_updated_at"] = time.time()
            self._flush()
            return new_value

    def update_batch(self, updates: Dict[str, Any]) -> None:
        """批量更新多个指标。"""
        with self._lock:
            timestamp = time.time()
            for key, value in updates.items():
                self._data[key] = value
                self._data[f"{key}_updated_at"] = timestamp
            self._flush()

    def get_all(self) -> Dict[str, Any]:
        """获取所有指标。"""
        with self._lock:
            return dict(self._data)

    def clear(self) -> None:
        """清空所有指标（同时清空文件）。"""
        with self._lock:
            self._data.clear()
            self._flush()


@lru_cache(maxsize=1)
def get_metrics_store() -> MetricsStore:
    """获取指标存储单例（lru_cache 线程安全）。"""
    return MetricsStore()
