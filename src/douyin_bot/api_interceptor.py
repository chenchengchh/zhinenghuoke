"""
API拦截器

用于拦截和监控网页中的网络请求，捕获抖音消息API的响应数据
"""

from typing import Optional, Callable, Dict, Any, List
import re
import time
import threading
from loguru import logger
from src.config.settings import API_INTERCEPT_INBOUND_WINDOW_SECONDS
from .base_api_interceptor import BaseAPIInterceptor


_SHARED_INTERCEPTORS: Dict[int, Dict[str, Any]] = {}
_SHARED_INTERCEPTORS_LOCK = threading.RLock()


class APIInterceptor(BaseAPIInterceptor):
    """
    API拦截器

    负责拦截网页中的API请求，
    主要用于消息抓取和数据分析
    """

    _INTERCEPTOR_NAME = "消息"

    def __init__(self, page):
        super().__init__(page)
        self._intercepted_requests: List[Dict[str, Any]] = []
        self._captured_messages: List[Dict[str, Any]] = []
        self._max_list_size = 5000
        self._capture_seq = 0
        self._last_consumed_capture_seq = 0

    def clear(self):
        """清空拦截记录（线程安全）"""
        with self._lock:
            self._intercepted_requests.clear()
            self._captured_messages.clear()

    @classmethod
    def classify_url(cls, url: str) -> str:
        """对 URL 进行分类，匹配消息相关 API 返回标识字符串。"""
        intercept_patterns = [
            "api.douyin.com",
            "aweme.douyin.com",
            "chat.douyin.com"
        ]
        return next((pattern for pattern in intercept_patterns if pattern in url), "")

    def should_intercept(self, url: str) -> bool:
        """判断是否应该拦截该URL"""
        if not self._enabled:
            return False
        return bool(self.classify_url(url))

    def _on_response(self, response):
        """处理HTTP响应，捕获消息相关API"""
        if not self._enabled:
            return
        try:
            url = response.url
            if not self.should_intercept(url):
                return

            if response.status != 200:
                return

            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return

            try:
                data = response.json()
            except Exception:
                return

            with self._lock:
                self._intercepted_requests.append({
                    "url": url,
                    "status": response.status,
                    "data": data,
                    "timestamp": time.time()
                })
                if len(self._intercepted_requests) > self._max_list_size:
                    trim_count = len(self._intercepted_requests) - int(self._max_list_size * 0.8)
                    self._intercepted_requests = self._intercepted_requests[trim_count:]

            messages = self._extract_messages(url, data)
            if messages:
                with self._lock:
                    self._append_captured_messages_locked(messages)
                    if len(self._captured_messages) > self._max_list_size:
                        trim_count = len(self._captured_messages) - int(self._max_list_size * 0.8)
                        self._captured_messages = self._captured_messages[trim_count:]
                logger.debug(f"API拦截器捕获 {len(messages)} 条消息")

        except Exception as e:
            logger.debug(f"处理API响应失败: {e}")

    @staticmethod
    def _normalize_direction(msg_data: Dict[str, Any]) -> str:
        """尽量从 API 负载中识别消息方向，无法确认时返回 unknown。"""
        direction_candidates = [
            msg_data.get("direction"),
            msg_data.get("message_direction"),
            msg_data.get("msg_direction"),
            msg_data.get("flow"),
            msg_data.get("chat_direction"),
        ]
        for raw in direction_candidates:
            if raw is None:
                continue
            if isinstance(raw, bool):
                return "outbound" if raw else "inbound"
            text = str(raw).strip().lower()
            if text in {"inbound", "incoming", "receive", "received", "recv", "from_user", "user", "customer"}:
                return "inbound"
            if text in {"outbound", "outgoing", "send", "sent", "self", "me", "assistant", "agent", "bot", "service"}:
                return "outbound"
            if text in {"system", "notification", "notice"}:
                return "system"
            if text in {"0", "1"}:
                return "inbound" if text == "0" else "outbound"

        bool_candidates = [
            msg_data.get("is_self"),
            msg_data.get("self"),
            msg_data.get("from_self"),
            msg_data.get("is_sender_self"),
            msg_data.get("outgoing"),
            msg_data.get("is_outgoing"),
        ]
        for raw in bool_candidates:
            if isinstance(raw, bool):
                return "outbound" if raw else "inbound"

        from_user = msg_data.get("from_user")
        nested_candidates = []
        if isinstance(from_user, dict):
            nested_candidates.extend([
                from_user.get("is_self"),
                from_user.get("self"),
                from_user.get("role"),
                from_user.get("user_type"),
                from_user.get("nickname"),
            ])
        for raw in nested_candidates:
            if raw is None:
                continue
            if isinstance(raw, bool):
                return "outbound" if raw else "inbound"
            text = str(raw).strip().lower()
            if text in {"我", "self", "me", "assistant", "agent", "bot", "service", "客服", "官方"}:
                return "outbound"
            if text in {"user", "customer", "客户", "访客"}:
                return "inbound"

        role_candidates = [
            msg_data.get("role"),
            msg_data.get("sender_role"),
            msg_data.get("sender_type"),
            msg_data.get("message_type"),
            msg_data.get("msg_type"),
        ]
        for raw in role_candidates:
            if raw is None:
                continue
            text = str(raw).strip().lower()
            if any(token in text for token in ("system", "notice", "notification", "通知")):
                return "system"
            if any(token in text for token in ("assistant", "agent", "bot", "客服", "官方")):
                return "outbound"
            if any(token in text for token in ("user", "customer", "客户", "访客")):
                return "inbound"

        return "unknown"

    @staticmethod
    def _extract_content(msg_data: Dict[str, Any]) -> str:
        for key in ("content", "text", "message", "body"):
            value = msg_data.get(key)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _normalize_preview_content(content: Any) -> str:
        text = " ".join(str(content or "").strip().split())
        if not text:
            return ""
        text = re.sub(
            r"^(?:刚刚|昨天|\d{1,2}:\d{2}(?::\d{2})?|\d+分钟前|\d+小时前|\d+天前|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})[\s:：-]*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        return " ".join(text.split())

    @staticmethod
    def _extract_user_id(from_user: Any, msg_data: Dict[str, Any]) -> str:
        if isinstance(from_user, dict):
            for key in ("sec_uid", "uid", "user_id", "id", "im_user_id", "to_user_id"):
                value = from_user.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        for key in ("sec_uid", "uid", "user_id", "from_user_id", "sender_id", "userId"):
            value = msg_data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _extract_conversation_id(msg_data: Dict[str, Any]) -> str:
        for key in ("conversation_id", "conv_id", "conversationId", "chat_id", "dialog_id", "session_id"):
            value = msg_data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _extract_messages(self, url: str, data: Dict) -> List[Dict[str, Any]]:
        """从API响应中提取消息（防御性取值，防止嵌套属性非dict崩溃）"""
        messages = []
        try:
            if "chat" in url or "message" in url:
                msg_list = None
                if isinstance(data, dict):
                    inner_data = data.get("data")
                    if isinstance(inner_data, dict):
                        msg_list = (inner_data.get("messages") or
                                   inner_data.get("list"))
                    if msg_list is None:
                        msg_list = data.get("messages", [])
                    if not isinstance(msg_list, list):
                        msg_list = []
                if isinstance(msg_list, list):
                    for msg_data in msg_list:
                        if isinstance(msg_data, dict):
                            from_user = msg_data.get("from_user")
                            nickname = ""
                            if isinstance(from_user, dict):
                                nickname = from_user.get("nickname", "")
                            elif isinstance(from_user, str):
                                nickname = from_user
                            content = self._normalize_preview_content(self._extract_content(msg_data))
                            customer_id = self._extract_user_id(from_user, msg_data)
                            conversation_id = self._extract_conversation_id(msg_data)
                            messages.append({
                                "customer_name": nickname,
                                "customer_id": customer_id,
                                "sender_id": str(msg_data.get("sender_id", "") or "").strip(),
                                "conversation_id": conversation_id,
                                "content": content,
                                "last_message_content": content,
                                "timestamp": msg_data.get("create_time", 0),
                                "last_message_time": str(msg_data.get("create_time", "") or ""),
                                "msg_id": msg_data.get("msg_id", ""),
                                "direction": "",
                                "source": "api_intercept"
                            })
        except Exception as e:
            logger.debug(f"提取消息失败: {e}")
        return messages

    def _ensure_capture_tracking_locked(self) -> None:
        """确保消息序列号跟踪正常（修复旧消息缺失的_capture_seq）"""
        for msg in self._captured_messages:
            capture_seq = int(msg.get("_capture_seq") or 0)
            if capture_seq <= 0:
                self._capture_seq += 1
                msg["_capture_seq"] = self._capture_seq
            elif capture_seq > self._capture_seq:
                self._capture_seq = capture_seq

    def _append_captured_messages_locked(self, messages: List[Dict[str, Any]]) -> None:
        self._ensure_capture_tracking_locked()
        for msg in messages:
            normalized = dict(msg)
            self._capture_seq += 1
            normalized["_capture_seq"] = self._capture_seq
            self._captured_messages.append(normalized)

    def get_intercepted_requests(self) -> List[Dict[str, Any]]:
        """获取拦截的请求列表（线程安全）"""
        with self._lock:
            return self._intercepted_requests.copy()

    def get_captured_messages(self, *, consume: bool = True) -> List[Dict[str, Any]]:
        """获取捕获的消息列表（线程安全）

        默认按队列语义消费，避免同一批历史消息被后续轮询重复重放。
        仅保留近期入站消息，并对完全重复的 API 记录去重，避免把我方回执覆盖客户新消息。
        """
        with self._lock:
            self._ensure_capture_tracking_locked()
            messages = [msg.copy() for msg in self._captured_messages]
            last_consumed_capture_seq = int(self._last_consumed_capture_seq or 0)

        current_ts = time.time()
        deduped_messages = {}
        max_seen_capture_seq = last_consumed_capture_seq
        for msg in messages:
            capture_seq = int(msg.get("_capture_seq") or 0)
            if capture_seq > max_seen_capture_seq:
                max_seen_capture_seq = capture_seq
            if consume and capture_seq and capture_seq <= last_consumed_capture_seq:
                continue

            direction = str(msg.get("direction") or "").strip().lower()
            if direction and direction not in ("inbound", "unknown"):
                continue

            conversation_key = str(msg.get("conversation_id") or msg.get("customer_name") or "").strip()
            if not conversation_key:
                continue

            msg_ts = msg.get("timestamp", 0) or 0
            # 兼容秒级和毫秒级时间戳
            if msg_ts > 1e11:
                msg_ts = msg_ts / 1000.0

            # 过滤掉超出窗口的历史消息，防止因为打开聊天框触发历史记录拉取而导致对旧消息疯狂回复
            if (
                API_INTERCEPT_INBOUND_WINDOW_SECONDS > 0
                and msg_ts > 0
                and (current_ts - msg_ts > API_INTERCEPT_INBOUND_WINDOW_SECONDS)
            ):
                continue

            msg_id = str(msg.get("msg_id") or "").strip()
            content = self._normalize_preview_content(
                msg.get("content") or msg.get("last_message_content") or ""
            )
            dedup_key = (
                f"mid:{msg_id}"
                if msg_id
                else f"sig:{conversation_key}|{content}|{int(msg_ts) if msg_ts else 0}"
            )
            previous = deduped_messages.get(dedup_key)
            if previous is None or float(previous.get("timestamp") or 0) < float(msg.get("timestamp") or 0):
                deduped_messages[dedup_key] = msg

        if consume and max_seen_capture_seq > last_consumed_capture_seq:
            with self._lock:
                self._ensure_capture_tracking_locked()
                self._last_consumed_capture_seq = max(
                    int(getattr(self, "_last_consumed_capture_seq", 0) or 0),
                    max_seen_capture_seq,
                )
                self._captured_messages = [
                    item
                    for item in self._captured_messages
                    if int(item.get("_capture_seq") or 0) > self._last_consumed_capture_seq
                ]

        return sorted(
            deduped_messages.values(),
            key=lambda item: float(item.get("timestamp") or 0),
        )

    def get_messages_for_conversation(
        self,
        customer_name: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        min_capture_seq: int = 0,
    ) -> List[Dict[str, Any]]:
        """获取指定会话的捕获消息（线程安全）

        优先使用 conversation_id，其次 customer_id，最后才回退到 customer_name，
        避免重名/改名时串会话。
        """
        normalized_name = str(customer_name or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_customer_id = str(customer_id or "").strip()
        normalized_min_capture_seq = max(int(min_capture_seq or 0), 0)
        with self._lock:
            self._ensure_capture_tracking_locked()
            messages = self._captured_messages.copy()
        matched: List[Dict[str, Any]] = []
        for message in messages:
            capture_seq = int(message.get("_capture_seq") or 0)
            if normalized_min_capture_seq and capture_seq <= normalized_min_capture_seq:
                continue
            message_conversation_id = str(message.get("conversation_id") or "").strip()
            message_customer_id = str(message.get("customer_id") or message.get("sender_id") or "").strip()
            message_customer_name = str(message.get("customer_name") or "").strip()

            if normalized_conversation_id:
                if message_conversation_id == normalized_conversation_id:
                    matched.append(message)
                continue

            if normalized_customer_id:
                if message_customer_id == normalized_customer_id:
                    matched.append(message)
                continue

            if normalized_name and message_customer_name == normalized_name:
                matched.append(message)
        return matched

    def get_current_capture_seq(self) -> int:
        """获取当前捕获序号，便于调用方只检查发送后新增的平台记录。"""
        with self._lock:
            self._ensure_capture_tracking_locked()
            return int(self._capture_seq or 0)


def acquire_shared_api_interceptor(page) -> APIInterceptor:
    page_key = id(page)
    with _SHARED_INTERCEPTORS_LOCK:
        bucket = _SHARED_INTERCEPTORS.get(page_key)
        if bucket is None:
            bucket = {"interceptor": APIInterceptor(page), "ref_count": 0}
            _SHARED_INTERCEPTORS[page_key] = bucket
        bucket["ref_count"] = int(bucket.get("ref_count", 0) or 0) + 1
        interceptor = bucket["interceptor"]
    interceptor.enable()
    return interceptor


def release_shared_api_interceptor(page, interceptor: Optional[APIInterceptor] = None) -> None:
    page_key = id(page)
    should_disable = False
    with _SHARED_INTERCEPTORS_LOCK:
        bucket = _SHARED_INTERCEPTORS.get(page_key)
        if bucket is None:
            target = interceptor
        else:
            bucket["ref_count"] = max(0, int(bucket.get("ref_count", 1) or 1) - 1)
            if bucket["ref_count"] == 0:
                target = bucket.get("interceptor")
                _SHARED_INTERCEPTORS.pop(page_key, None)
                should_disable = True
            else:
                target = None
    if target is not None and (should_disable or interceptor is not None):
        try:
            target.disable()
        except Exception:
            pass


def get_shared_api_interceptor(page) -> Optional[APIInterceptor]:
    with _SHARED_INTERCEPTORS_LOCK:
        bucket = _SHARED_INTERCEPTORS.get(id(page))
        if not bucket:
            return None
        return bucket.get("interceptor")
