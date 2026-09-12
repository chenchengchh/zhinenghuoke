"""
网络消息检测器

通过拦截 HTTP API 响应和 WebSocket 消息，直接获取平台真实消息数据，
替代不可靠的 DOM 推断方案。

核心改进：
- 方向判定：sender_id == my_user_id 精确匹配（确定性），替代 DOM CSS 类名推断
- 去重：msg_id 精确去重，替代不稳定的 DOM 内容签名
- 不依赖未读数、CSS 类名、文本前缀等 DOM 推断逻辑
"""

import hashlib
import json
import time
import threading
from typing import List, Dict, Any, Optional, Callable, Tuple

from loguru import logger
from src.douyin_bot.api_interceptor import (
    APIInterceptor,
    acquire_shared_api_interceptor,
    release_shared_api_interceptor,
)
from src.common.conversation_id import build_conversation_id


class NetworkMessage:
    __slots__ = (
        "msg_id", "conversation_id", "customer_id", "customer_name",
        "sender_id", "content", "direction", "direction_source",
        "direction_confidence", "timestamp", "source", "raw_data",
    )

    def __init__(
        self,
        *,
        msg_id: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        customer_name: str = "",
        sender_id: str = "",
        content: str = "",
        direction: str = "unknown",
        direction_source: str = "unresolved",
        direction_confidence: str = "low",
        timestamp: float = 0.0,
        source: str = "unknown",
        raw_data: Optional[Dict[str, Any]] = None,
    ):
        self.msg_id = msg_id
        self.conversation_id = conversation_id
        self.customer_id = customer_id
        self.customer_name = customer_name
        self.sender_id = sender_id
        self.content = content
        self.direction = direction
        self.direction_source = direction_source
        self.direction_confidence = direction_confidence
        self.timestamp = timestamp
        self.source = source
        self.raw_data = raw_data


def determine_direction(
    msg_data: Dict[str, Any],
    my_user_id: str,
) -> Tuple[str, str, str]:
    """
    基于网络数据判定消息方向（核心改进）

    优先级：
    1. sender_id == my_user_id → outbound (确定性)
    2. sender_id 存在且 != my_user_id → inbound (高置信)
    3. is_self == True → outbound (高置信)
    4. API direction 字段 → 按字段值 (中等置信)
    5. 无法判定 → unknown (低置信)

    Returns:
        (direction, direction_source, direction_confidence)
    """
    sender_id = str(msg_data.get("sender_id", "") or "").strip()

    if sender_id and my_user_id:
        if sender_id == my_user_id:
            return "outbound", "sender_id_match", "high"
        return "inbound", "sender_id_mismatch", "high"

    if sender_id and not my_user_id:
        logger.debug(f"sender_id={sender_id} 存在但 my_user_id 为空，无法通过 sender_id 判定方向")

    is_self_candidates = [
        msg_data.get("is_self"),
        msg_data.get("self"),
        msg_data.get("from_self"),
        msg_data.get("is_sender_self"),
        msg_data.get("outgoing"),
        msg_data.get("is_outgoing"),
    ]
    bool_values = [raw for raw in is_self_candidates if isinstance(raw, bool)]
    if bool_values:
        if all(bool_values):
            return "outbound", "is_self_flag", "high"
        if not any(bool_values):
            return "inbound", "is_self_flag", "high"
        majority = sum(bool_values) > len(bool_values) / 2
        if sum(bool_values) == len(bool_values) / 2:
            return "unknown", "is_self_flag", "low"
        return ("outbound" if majority else "inbound"), "is_self_flag", "medium"

    from_user = msg_data.get("from_user")
    if isinstance(from_user, dict):
        for key in ("is_self", "self", "from_self"):
            val = from_user.get(key)
            if isinstance(val, bool):
                return ("outbound" if val else "inbound"), "is_self_flag", "high"

    direction = APIInterceptor._normalize_direction(msg_data)
    if direction != "unknown":
        return direction, "api_field", "medium"

    return "unknown", "unresolved", "low"


class MessageNormalizer:
    """将原始 API/WebSocket 数据标准化为 NetworkMessage"""

    def __init__(self, my_user_id_resolver: Callable[[], Optional[str]]):
        self._my_user_id_resolver = my_user_id_resolver

    def normalize(self, raw_msg: Dict[str, Any], my_user_id: str = "") -> Optional[NetworkMessage]:
        if not isinstance(raw_msg, dict):
            return None

        content = APIInterceptor._normalize_preview_content(
            APIInterceptor._extract_content(raw_msg)
        )
        if not content:
            return None

        msg_id = str(raw_msg.get("msg_id", "") or "").strip()

        direction, direction_source, direction_confidence = determine_direction(
            raw_msg, my_user_id
        )

        from_user = raw_msg.get("from_user")
        customer_id = APIInterceptor._extract_user_id(from_user, raw_msg)
        customer_name = ""
        if isinstance(from_user, dict):
            customer_name = str(from_user.get("nickname", "") or "").strip()
        elif isinstance(from_user, str):
            customer_name = from_user.strip()
        if not customer_name:
            customer_name = str(raw_msg.get("customer_name", "") or "").strip()

        conversation_id = APIInterceptor._extract_conversation_id(raw_msg)

        sender_id = str(raw_msg.get("sender_id", "") or "").strip()

        timestamp = 0.0
        raw_ts = raw_msg.get("create_time", 0) or raw_msg.get("timestamp", 0) or 0
        if raw_ts:
            ts_val = float(raw_ts)
            if ts_val > 1e11:
                ts_val = ts_val / 1000.0
            timestamp = ts_val

        source = str(raw_msg.get("source", "") or "unknown").strip()

        return NetworkMessage(
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            customer_name=customer_name,
            sender_id=sender_id,
            content=content,
            direction=direction,
            direction_source=direction_source,
            direction_confidence=direction_confidence,
            timestamp=timestamp,
            source=source,
        )


class MessageDeduplicator:
    """基于 msg_id 和内容签名的消息去重器"""

    def __init__(self, window_seconds: int = 600):
        self._seen_msg_ids: Dict[str, float] = {}
        self._seen_signatures: Dict[str, float] = {}
        self._window_seconds = window_seconds
        self._lock = threading.RLock()
        self._operation_count = 0

    def filter_new(self, messages: List[NetworkMessage]) -> List[NetworkMessage]:
        now = time.time()
        new_messages: List[NetworkMessage] = []

        with self._lock:
            self._cleanup_expired_locked(now)

            for msg in messages:
                if msg.msg_id:
                    if msg.msg_id in self._seen_msg_ids:
                        continue
                    self._seen_msg_ids[msg.msg_id] = now

                sig = self._build_signature(msg)
                if sig in self._seen_signatures:
                    if msg.msg_id:
                        self._seen_msg_ids[msg.msg_id] = now
                    continue

                if not msg.msg_id:
                    content_sig = self._build_content_signature(msg.customer_name, msg.content)
                    if content_sig in self._seen_signatures:
                        continue

                self._seen_signatures[sig] = now
                new_messages.append(msg)

        return new_messages

    def mark_seen(self, msg_id: str) -> None:
        if not msg_id:
            return
        with self._lock:
            self._seen_msg_ids[msg_id] = time.time()
            self._maybe_cleanup()

    def _mark_signature_seen(self, msg: NetworkMessage) -> None:
        """标记消息签名为已见（用于发送后防止回显）"""
        with self._lock:
            sig = self._build_signature(msg)
            self._seen_signatures[sig] = time.time()
            self._maybe_cleanup()

    def _maybe_cleanup(self) -> None:
        self._operation_count += 1
        if self._operation_count >= 100:
            self._operation_count = 0
            self._cleanup_expired_locked(time.time())

    def _build_signature(self, msg: NetworkMessage) -> str:
        seed = "|".join([
            msg.conversation_id or "",
            msg.customer_id or "",
            msg.content,
            f"{msg.timestamp:.3f}" if msg.timestamp else "0",
        ])
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:24]

    def _build_content_signature(self, customer_name: str, content: str) -> str:
        """基于客户名+内容的鲁棒签名（不依赖时间戳和ID，用于发送后回显防护）"""
        seed = "|".join([customer_name or "", content])
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:24]

    def _cleanup_expired_locked(self, now: float) -> None:
        cutoff = now - self._window_seconds
        self._seen_msg_ids = {
            k: v for k, v in self._seen_msg_ids.items() if v > cutoff
        }
        self._seen_signatures = {
            k: v for k, v in self._seen_signatures.items() if v > cutoff
        }


class RPAMessageAdapter:
    """将 NetworkMessage 转换为 rpa_engine 下游管道兼容的字典格式"""

    @staticmethod
    def to_rpa_message(nm: NetworkMessage, platform: str = "douyin") -> Dict[str, Any]:
        conversation_id = nm.conversation_id or build_conversation_id(
            nm.customer_name, platform
        )
        return {
            "customer_name": nm.customer_name,
            "last_message_content": nm.content,
            "content": nm.content,
            "direction": nm.direction,
            "conversation_id": conversation_id,
            "customer_id": nm.customer_id,
            "sender_id": nm.sender_id,
            "msg_id": nm.msg_id,
            "timestamp": nm.timestamp,
            "is_new": True,
            "signal_source": f"network_{nm.source}",
            "direction_confidence": nm.direction_confidence,
            "direction_source": nm.direction_source,
        }


class WebSocketHandler:
    """监听 WebSocket 消息，获取实时推送（增强数据源）"""

    def __init__(self, page):
        self.page = page
        self._captured_messages: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._enabled = False
        self._handler_registered = False
        self._max_list_size = 2000

    def enable(self) -> bool:
        if not self.page:
            return False
        try:
            self.page.on("websocket", self._on_websocket)
            self._enabled = True
            self._handler_registered = True
            logger.info("WebSocket 监听器已注册")
            return True
        except Exception as e:
            logger.warning(f"WebSocket 监听注册失败: {e}")
            return False

    def disable(self) -> None:
        if self._handler_registered and self.page:
            try:
                self.page.remove_listener("websocket", self._on_websocket)
            except Exception:
                pass
            self._handler_registered = False
        self._enabled = False

    def _on_websocket(self, ws) -> None:
        try:
            ws.on("message", lambda payload: self._on_ws_message(payload, ws))
        except Exception as e:
            logger.debug(f"WebSocket 消息监听绑定失败: {e}")

    def _on_ws_message(self, payload, ws) -> None:
        try:
            if isinstance(payload, bytes):
                try:
                    payload = payload.decode("utf-8")
                except Exception:
                    return
            if not isinstance(payload, str):
                return

            data = json.loads(payload)
            messages = self._extract_messages_from_ws(data, ws)
            if messages:
                with self._lock:
                    self._captured_messages.extend(messages)
                    if len(self._captured_messages) > self._max_list_size:
                        trim = len(self._captured_messages) - int(self._max_list_size * 0.8)
                        self._captured_messages = self._captured_messages[trim:]
        except (json.JSONDecodeError, TypeError):
            pass
        except Exception as e:
            logger.debug(f"WebSocket 消息处理失败: {e}")

    def _extract_messages_from_ws(
        self, data: Any, ws
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        if not isinstance(data, dict):
            return messages

        try:
            payload = data.get("payload", data.get("data", {}))
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    return messages

            if not isinstance(payload, dict):
                return messages

            msg_list = payload.get("messages") or payload.get("list") or []
            if not isinstance(msg_list, list):
                msg_data = payload.get("message") or payload.get("msg")
                if isinstance(msg_data, dict):
                    msg_list = [msg_data]
                else:
                    return messages

            for msg_data in msg_list:
                if not isinstance(msg_data, dict):
                    continue
                from_user = msg_data.get("from_user")
                nickname = ""
                if isinstance(from_user, dict):
                    nickname = from_user.get("nickname", "")
                elif isinstance(from_user, str):
                    nickname = from_user

                content = APIInterceptor._normalize_preview_content(
                    APIInterceptor._extract_content(msg_data)
                )
                if not content:
                    continue

                customer_id = APIInterceptor._extract_user_id(from_user, msg_data)
                conversation_id = APIInterceptor._extract_conversation_id(msg_data)

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
                    "source": "websocket",
                })
        except Exception as e:
            logger.debug(f"WebSocket 消息提取失败: {e}")

        return messages

    def get_captured_messages(self, *, consume: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            messages = self._captured_messages.copy()
            if consume:
                self._captured_messages.clear()
            return messages


class NetworkMessageDetector:
    """
    网络消息检测器（主入口）

    整合 HTTP API 和 WebSocket 两个数据源，
    标准化消息、判定方向、去重后输出新入站消息。
    """

    def __init__(
        self,
        page,
        my_user_id_resolver: Callable[[], Optional[str]],
        *,
        dedup_window_seconds: int = 600,
    ):
        self.page = page
        self._my_user_id_resolver = my_user_id_resolver
        self._normalizer = MessageNormalizer(my_user_id_resolver)
        self._deduplicator = MessageDeduplicator(window_seconds=dedup_window_seconds)
        self._api_interceptor: Optional[APIInterceptor] = None
        self._ws_handler: Optional[WebSocketHandler] = None
        self._enabled = False
        self._lock = threading.RLock()

        self._stats_total_raw = 0
        self._stats_total_normalized = 0
        self._stats_total_inbound = 0
        self._stats_total_deduped = 0
        self._stats_last_log_time = 0.0

    def enable(self) -> bool:
        with self._lock:
            if self._enabled:
                return True

            try:
                if self.page and not self.page.is_closed():
                    self._api_interceptor = acquire_shared_api_interceptor(self.page)

                    try:
                        self._ws_handler = WebSocketHandler(self.page)
                        ws_ok = self._ws_handler.enable()
                        if not ws_ok:
                            self._ws_handler = None
                    except Exception:
                        self._ws_handler = None

                    self._enabled = True
                    logger.info("网络消息检测器已启用")
                    return True
                else:
                    logger.warning("页面不可用，网络消息检测器启用失败")
                    return False
            except Exception as e:
                logger.error(f"网络消息检测器启用失败: {e}")
                if self._api_interceptor:
                    try:
                        release_shared_api_interceptor(self.page, self._api_interceptor)
                    except Exception:
                        pass
                    self._api_interceptor = None
                self._enabled = False
                return False

    def disable(self) -> None:
        with self._lock:
            if self._api_interceptor:
                try:
                    release_shared_api_interceptor(self.page, self._api_interceptor)
                except Exception:
                    pass
                self._api_interceptor = None

            if self._ws_handler:
                try:
                    self._ws_handler.disable()
                except Exception:
                    pass
                self._ws_handler = None

            self._enabled = False
            logger.info("网络消息检测器已禁用")

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def api_interceptor(self) -> Optional[APIInterceptor]:
        return self._api_interceptor

    def get_new_inbound_messages(self) -> List[NetworkMessage]:
        """
        获取新的入站消息（主链路调用入口）

        流程：
        1. 从 HTTP API 和 WebSocket 两个数据源获取原始消息
        2. 标准化为 NetworkMessage（含方向判定）
        3. 过滤仅保留 inbound 消息
        4. 去重
        """
        if not self._enabled:
            return []

        my_user_id = self._my_user_id_resolver() or ""

        raw_messages = self._collect_raw_messages()

        normalized: List[NetworkMessage] = []
        for raw_msg in raw_messages:
            nm = self._normalizer.normalize(raw_msg, my_user_id)
            if nm is None:
                continue
            normalized.append(nm)

        collect_only_unknown = [nm for nm in normalized if nm.direction == "unknown"]
        inbound = [nm for nm in normalized if nm.direction == "inbound"]

        new_messages = self._deduplicator.filter_new(inbound)

        self._update_stats(len(raw_messages), len(normalized), len(inbound), len(new_messages))
        if collect_only_unknown:
            logger.info(
                f"网络消息检测器捕获 {len(collect_only_unknown)} 条 unknown 方向消息，"
                "已按 collect-only 处理，不进入自动回复主链"
            )

        return new_messages

    def mark_message_seen(self, msg_id: str) -> None:
        self._deduplicator.mark_seen(msg_id)

    def mark_sent_message_seen(self, customer_name: str, content: str) -> None:
        """标记已发送消息为已见（公开接口，用于发送后回显防护）
        
        使用基于 customer_name+content 的鲁棒签名，不依赖时间戳和ID，
        确保即使网络拦截到的消息字段不完整也能正确去重。
        """
        try:
            sig = self._deduplicator._build_content_signature(customer_name, content)
            with self._deduplicator._lock:
                self._deduplicator._seen_signatures[sig] = time.time()
        except Exception as e:
            logger.debug(f"标记发送消息已见失败: {e}")

    def rollback_message_seen(self, customer_name: str, content: str, msg_id: str = "") -> None:
        """回滚消息去重标记（回调失败时使用，允许下次重新检测）"""
        try:
            with self._deduplicator._lock:
                sig = self._deduplicator._build_content_signature(customer_name, content)
                self._deduplicator._seen_signatures.pop(sig, None)
                if msg_id:
                    self._deduplicator._seen_msg_ids.pop(msg_id, None)
        except Exception as e:
            logger.debug(f"回滚消息去重标记失败: {e}")

    def _collect_raw_messages(self) -> List[Dict[str, Any]]:
        raw_messages: List[Dict[str, Any]] = []

        if self._api_interceptor:
            try:
                api_msgs = self._api_interceptor.get_captured_messages(consume=True)
                if api_msgs:
                    raw_messages.extend(api_msgs)
            except Exception as e:
                logger.debug(f"API 拦截器获取消息失败: {e}")

        if self._ws_handler:
            try:
                ws_msgs = self._ws_handler.get_captured_messages(consume=True)
                if ws_msgs:
                    raw_messages.extend(ws_msgs)
            except Exception as e:
                logger.debug(f"WebSocket 获取消息失败: {e}")

        return raw_messages

    def _update_stats(
        self,
        raw_count: int,
        normalized_count: int,
        inbound_count: int,
        new_count: int,
    ) -> None:
        self._stats_total_raw += raw_count
        self._stats_total_normalized += normalized_count
        self._stats_total_inbound += inbound_count
        self._stats_total_deduped += new_count

        now = time.time()
        if new_count > 0 or now - self._stats_last_log_time >= 60:
            if new_count > 0 or raw_count > 0:
                logger.info(
                    f"网络消息检测统计: raw={raw_count}, normalized={normalized_count}, "
                    f"inbound={inbound_count}, new={new_count}, "
                    f"cumulative_raw={self._stats_total_raw}, "
                    f"cumulative_new={self._stats_total_deduped}"
                )
            self._stats_last_log_time = now
