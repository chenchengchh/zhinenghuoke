"""
API 拦截器基类

提供生命周期管理（enable/disable/clear）和批次队列管理（drain/peek/trim）的公共逻辑，
子类只需实现 classify_url / _on_response / clear 等抽象方法即可。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from loguru import logger


# ---------------------------------------------------------------------------
# BaseAPIInterceptor — 生命周期管理基类
# ---------------------------------------------------------------------------

class BaseAPIInterceptor(ABC):
    """所有 API 拦截器的公共基类，封装 enable/disable / handler 注册等生命周期逻辑。"""

    _INTERCEPTOR_NAME: str = "API"

    def __init__(self, page):
        self.page = page
        self._enabled = False
        self._handler_registered = False
        self._lock = threading.RLock()

    # -- 生命周期 ----------------------------------------------------------

    def enable(self):
        """启用拦截，注册 Playwright 响应监听器。"""
        if self._enabled:
            return
        self._enabled = True
        self._register_response_handler()
        logger.info(f"{self._INTERCEPTOR_NAME} API 拦截器已启用")

    def disable(self):
        """禁用拦截，注销 Playwright 响应监听器。"""
        if self._handler_registered and self.page:
            try:
                self.page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self._handler_registered = False
        self._enabled = False
        logger.info(f"{self._INTERCEPTOR_NAME} API 拦截器已禁用")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def _register_response_handler(self):
        """注册 Playwright 响应监听器。"""
        if self._handler_registered or not self.page:
            return
        try:
            self.page.on("response", self._on_response)
            self._handler_registered = True
            logger.info(f"{self._INTERCEPTOR_NAME} API 响应监听器已注册")
        except Exception as e:
            logger.warning(f"注册{self._INTERCEPTOR_NAME} API 响应监听器失败: {e}")

    # -- 抽象方法 ----------------------------------------------------------

    @abstractmethod
    def clear(self):
        """清空拦截记录（线程安全）。"""

    @classmethod
    @abstractmethod
    def classify_url(cls, url: str) -> str:
        """对 URL 进行分类，返回分类标识；不匹配时返回空字符串。"""

    @abstractmethod
    def _on_response(self, response):
        """Playwright response 事件回调。"""


# ---------------------------------------------------------------------------
# BatchAPIInterceptor — 批次队列中间基类
# ---------------------------------------------------------------------------

class BatchAPIInterceptor(BaseAPIInterceptor):
    """在 BaseAPIInterceptor 基础上增加批次队列管理（drain/peek/trim），
    子类只需关注 classify_url 与 _build_batch 即可。
    """

    _MAX_BATCHES: int = 300

    def __init__(self, page):
        super().__init__(page)
        self._batches: List[Dict[str, Any]] = []
        self._batch_seq = 0
        self._dropped_batches_count = 0
        self._peak_pending_batches = 0
        self._last_dropped_batch_id = 0

    # -- 队列操作 ----------------------------------------------------------

    def clear(self):
        with self._lock:
            self._batches.clear()
            self._batch_seq = 0
            self._dropped_batches_count = 0
            self._peak_pending_batches = 0
            self._last_dropped_batch_id = 0

    def drain_batches(self) -> List[Dict[str, Any]]:
        """消费式获取所有批次并清空队列。"""
        with self._lock:
            batches = list(self._batches)
            self._batches.clear()
            return batches

    def peek_batches(self) -> List[Dict[str, Any]]:
        """非消费式查看所有批次。"""
        with self._lock:
            return list(self._batches)

    def get_queue_stats(self) -> Dict[str, Any]:
        """基础队列统计，子类可覆盖追加字段。"""
        with self._lock:
            pending_total = len(self._batches)
            latest_timestamp = 0.0
            latest_batch_id = 0
            for batch in self._batches:
                ts = float(batch.get("timestamp") or 0.0)
                if ts > latest_timestamp:
                    latest_timestamp = ts
                batch_id = int(batch.get("batch_id", 0) or 0)
                if batch_id > latest_batch_id:
                    latest_batch_id = batch_id
            return {
                "pending_total": pending_total,
                "latest_timestamp": latest_timestamp,
                "latest_batch_id": latest_batch_id,
                "dropped_batches_count": int(self._dropped_batches_count or 0),
                "peak_pending_batches": int(self._peak_pending_batches or 0),
                "last_dropped_batch_id": int(self._last_dropped_batch_id or 0),
            }

    # -- 响应解析 ----------------------------------------------------------

    @staticmethod
    def _parse_response_text(raw_text: str) -> Dict[str, Any] | None:
        """尝试从原始响应文本中解析 JSON（兼容 BOM / 前缀垃圾字符）。"""
        text = str(raw_text or "").strip()
        if not text:
            return None
        decoder = json.JSONDecoder()
        candidate_starts = []
        for index, char in enumerate(text):
            if char in "{[":
                candidate_starts.append(index)
                if len(candidate_starts) >= 6:
                    break
        for start in candidate_starts:
            try:
                parsed, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _extract_response_payload(self, response, status: int, content_type: str) -> Dict[str, Any] | None:
        """从 response 对象中提取 JSON 负载，优先 response.json()，回退手动解析。"""
        if status != 200:
            return None
        try:
            json_data = response.json()
            if isinstance(json_data, dict):
                return json_data
        except Exception:
            pass
        try:
            raw_text = response.text()
        except Exception as e:
            logger.debug(f"读取{self._INTERCEPTOR_NAME}接口响应文本失败: {e}")
            return None
        parsed = self._parse_response_text(raw_text)
        if parsed is not None:
            return parsed
        if raw_text:
            logger.debug(
                f"{self._INTERCEPTOR_NAME}接口响应未解析为 JSON: "
                f"content_type={content_type!r}, text_head={raw_text[:160]!r}"
            )
        return None

    # -- 模板方法 ----------------------------------------------------------

    def _on_response(self, response):
        """模板方法：classify_url -> extract_payload -> build_batch -> append -> trim。"""
        if not self._enabled:
            return
        try:
            kind_or_pattern = self.classify_url(response.url)
            if not kind_or_pattern:
                return

            status = int(response.status or 0)
            content_type = (response.headers or {}).get("content-type", "")
            data = self._extract_response_payload(response, status, content_type)
            if data is None:
                return

            query = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query)
            self._batch_seq += 1
            batch = self._build_batch(
                batch_id=self._batch_seq,
                kind_or_pattern=kind_or_pattern,
                url=response.url,
                status=status,
                content_type=content_type,
                query=query,
                data=data,
                timestamp=time.time(),
            )
            with self._lock:
                self._batches.append(batch)
                self._peak_pending_batches = max(self._peak_pending_batches, len(self._batches))
                if len(self._batches) > self._MAX_BATCHES:
                    trim = len(self._batches) - int(self._MAX_BATCHES * 0.7)
                    dropped_batches = self._batches[:trim]
                    self._dropped_batches_count += len(dropped_batches)
                    if dropped_batches:
                        self._last_dropped_batch_id = int(dropped_batches[-1].get("batch_id", 0) or 0)
                        logger.warning(
                            f"{self._INTERCEPTOR_NAME} API 批次队列达到上限，裁剪旧批次: "
                            f"dropped_now={len(dropped_batches)}, "
                            f"dropped_total={self._dropped_batches_count}, "
                            f"last_dropped_batch_id={self._last_dropped_batch_id}, "
                            f"max_batches={self._MAX_BATCHES}"
                        )
                    self._batches = self._batches[trim:]
        except Exception as e:
            logger.debug(f"处理{self._INTERCEPTOR_NAME} API 响应失败: {e}")

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
        """构建批次字典，子类可覆盖以追加字段。"""
        return {
            "batch_id": batch_id,
            "kind": kind_or_pattern,
            "url": url,
            "status": status,
            "content_type": content_type,
            "query": query,
            "data": data,
            "timestamp": timestamp,
        }
