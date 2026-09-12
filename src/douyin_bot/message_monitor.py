"""
抖音消息监控模块
负责监听和同步抖音私信消息
"""

import time
import re
import hashlib
import threading
from datetime import datetime, timedelta
from typing import List, Callable, Optional, Dict, Any
from loguru import logger
from src.common.conversation_id import build_conversation_id
from src.web.inbound_decision_engine import InboundDecisionEngine
from .api_interceptor import acquire_shared_api_interceptor, release_shared_api_interceptor
from src.config.settings import DOUYIN_CHAT_URL
from .dom_selectors import get_conversations_js as _dom_get_conversations_js


class MessageMonitor:
    """消息监控类，负责监听抖音消息并同步到数据库

    [REFACTOR-INST:convergence] 发送与会话点击链路已统一委托给 rpa_engine.DouYinRPAEngine，
    本类只保留：监控新消息、消息状态机、会话状态同步。
    """

    INVALID_PATTERNS = [
        '已撤回', '正在输入', '在线', '离线', '正在加载', '点击', '滑动的',
        '系统消息', '系统通知', '上拉', '下拉',
        '[图片]', '[语音]', '[视频]', '[表情]', '[文件]', '[链接]',
        '[红包]', '[位置]', '[名片]', '[小程序]', '[商品]',
        '对方回复或关注你之前', '只能发送一条文字消息', '抖音自律公约',
    ]

    TIME_PATTERNS = [
        r'^\d{1,2}:\d{2}$',
        r'^\d{1,2}:\d{2}:\d{2}$',
        r'^昨天$',
        r'^\d+分钟前$',
        r'^\d+小时前$',
        r'^\d+天前$',
        r'^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$',
        r'^\d+$',
    ]

    BOT_REPLY_PATTERNS = [
        'Thinking Process',
        'Analyze the Request',
        'Professional Sales Consultant',
        'General Service Sales',
        '序号：',
        '序号:',
        '对公转账需要：公司名称',
        '方便的话也可以留个接收资料的联系方式',
        '更完整的资料、详细说明',
        '继续为您处理',
    ]

    SYSTEM_DYNAMIC_PATTERNS = [
        '加入了群聊',
        '查看历史消息',
        '赞了对方分享的',
        '赞了你分享的',
        '个人主页',
        '新成员可查看历史消息',
        '分享的 视频',
        '分享的视频',
    ]

    MAX_CONVERSATION_STATES = 500
    STATE_EXPIRE_SECONDS = 86400
    DOM_DUPLICATE_REEMIT_SECONDS = 300

    def __init__(self, page, platform: str = "douyin", browser_manager=None, chat_session_coordinator=None):
        self.page = page
        self.platform = platform
        self.browser_manager = browser_manager
        self._chat_session_coordinator = chat_session_coordinator
        self._running = False
        self._callback = None
        self.last_send_error = ""
        self._conversation_states: Dict[str, Dict[str, Any]] = {}
        self._states_lock = threading.RLock()
        self._chat_page = None

        self._chat_page_ensured = False
        self._ensure_chat_page_attempts = 0
        self._max_ensure_attempts = 10
        self._ensure_failed_logged = False
        self._ensure_verify_counter = 0

        self.api_interceptor = acquire_shared_api_interceptor(page) if page else None
        # [REFACTOR-INST:convergence] smart_finder/mode_selector 已废弃，输入框定位统一由
        # rpa_engine.DouYinRPAEngine + InputLocatorService 提供，模式选择由 OperationMode 管理。

    def start(self, callback: Optional[Callable] = None):
        """启动消息监控"""
        logger.info("启动消息监控...")
        self._callback = callback
        
        # 在启动时准备聊天页面（在主线程中执行）
        chat_ready = self._prepare_chat_page()
        if not chat_ready:
            self._running = False
            logger.warning("消息监控启动失败：聊天页面未准备就绪")
            return False

        init_ok = self._initialize_message_states()
        if not init_ok:
            self._running = False
            logger.warning("消息监控启动失败：会话状态初始化未完成")
            return False

        self._running = True
        logger.info("消息监控已启动")
        return True

    def _prepare_chat_page(self):
        """在主线程中准备聊天页面"""
        try:
            coordinator = getattr(self, "_chat_session_coordinator", None)
            if coordinator and coordinator.ensure_chat_page(monitor=self):
                logger.info("通过聊天会话协调层准备聊天页面成功")
                return True

            # 重置状态
            self._chat_page_ensured = False
            self._ensure_chat_page_attempts = 0
            self._ensure_failed_logged = False

            # 检查当前页面是否已经在聊天页面
            if self.page and not self.page.is_closed():
                try:
                    current_url = self.page.url or ""
                    if "/chat" in current_url and "douyin.com" in current_url:
                        self._chat_page = self.page
                        self._chat_page_ensured = True
                        logger.info(f"当前页面已是聊天页面: {current_url}")
                        return True
                except Exception as e:
                    logger.debug(f"检查当前页面URL失败: {e}")

            # 检查浏览器上下文中是否已有聊天页面
            if self.browser_manager and self.browser_manager.context:
                for page in self.browser_manager.context.pages:
                    try:
                        if not page.is_closed():
                            page_url = page.url or ""
                            if "/chat" in page_url and "douyin.com" in page_url:
                                self.page = page
                                self._chat_page = page
                                self._chat_page_ensured = True
                                logger.info(f"找到已有的聊天页面: {page_url}")
                                return True
                    except Exception:
                        continue

            # 导航当前页面到聊天页面
            if self.page and not self.page.is_closed():
                logger.info("导航到聊天页面...")
                self.page.goto(DOUYIN_CHAT_URL, timeout=30000)
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                try:
                    self.page.wait_for_selector('[data-e2e="conversation-item"], [class*="conversationItem"]', timeout=5000)
                except Exception:
                    logger.warning("聊天页会话列表未加载，判定聊天页面未就绪")
                    self._chat_page_ensured = False
                    return False
                current_url = self.page.url or ""
                if "/chat" in current_url and "douyin.com" in current_url:
                    self._chat_page = self.page
                    self._chat_page_ensured = True
                    logger.info(f"已导航到聊天页面: {current_url}")
                    return True
                else:
                    logger.warning(f"导航到聊天页面失败，当前URL: {current_url}")
                    self._chat_page_ensured = False
                    return False
            else:
                logger.warning("页面不可用，无法导航到聊天页面")
                self._chat_page_ensured = False
                return False

        except Exception as e:
            logger.error(f"准备聊天页面失败: {e}")
            self._chat_page_ensured = False
            return False

    def _initialize_message_states(self):
        """初始化消息状态"""
        try:
            if not self._check_page_valid():
                logger.warning("页面无效，跳过初始化")
                return False

            if not self._ensure_chat_page():
                logger.warning("无法确保在聊天页面，跳过初始化")
                return False

            conversations = self._get_conversations_with_direction()
            current_time = time.time()

            for conv in conversations:
                customer_name = conv.get("customer_name", "")
                last_message = self._strip_preview_time_noise(conv.get("last_message_content", ""))
                last_message_time = conv.get("last_message_time", "")
                direction = conv.get("direction", "inbound")
                customer_id = conv.get("customer_id", "")
                conversation_id = conv.get("conversation_id", "")

                # 跳过出站消息的初始化，避免把发送的消息当作初始状态
                # 但我们仍然需要记录它，以防止它被误判为新消息
                if customer_name:
                    state_key = self._resolve_conversation_state_key(
                        customer_name=customer_name,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                    )
                    signature = self._build_message_signature(
                        customer_name=customer_name,
                        conversation_id=conversation_id,
                        content=last_message,
                        direction=direction,
                        message_time=last_message_time,
                    )
                    inbound_signature = self._build_inbound_identity_signature(
                        customer_name=customer_name,
                        conversation_id=conversation_id,
                        content=last_message,
                    )
                    self._conversation_states[state_key] = {
                        "content": last_message,
                        "time": current_time,
                        "is_outbound": direction == "outbound",
                        "message_time": last_message_time,
                        "direction": direction,
                        "has_unread": bool(conv.get("has_unread", False)),
                        "last_signature": signature,
                        "last_inbound_signature": inbound_signature if direction == "inbound" else "",
                        "last_inbound_emitted_at": current_time if direction == "inbound" and self._is_valid_message(last_message) else 0,
                        **self._merge_identity_fields(
                            customer_name,
                            self._conversation_states.get(state_key, {}),
                            customer_id=customer_id,
                            conversation_id=conversation_id,
                        ),
                    }

            logger.info(f"已初始化 {len(self._conversation_states)} 个会话的消息状态")
            return True

        except Exception as e:
            logger.error(f"初始化消息状态失败: {e}")
            return False

    def stop(self):
        """停止消息监控"""
        self._running = False
        if self.api_interceptor is not None and self.page is not None:
            try:
                release_shared_api_interceptor(self.page, self.api_interceptor)
            except Exception:
                pass
            self.api_interceptor = None
        logger.info("消息监控已停止")

    def _check_page_valid(self) -> bool:
        """检查页面是否有效"""
        try:
            return self.page and not self.page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _fuzzy_match_name(text: str, target: str) -> bool:
        """安全匹配昵称：仅允许精确匹配或脱敏手机号匹配，避免先点到相似昵称。"""
        if not text or not target:
            return False
        norm_text = text.strip().replace(' ', '').replace('\u200b', '')
        norm_target = target.strip().replace(' ', '').replace('\u200b', '')
        if norm_text == norm_target:
            return True
        digit_text = ''.join(ch for ch in norm_text if ch.isdigit())
        digit_target = ''.join(ch for ch in norm_target if ch.isdigit())
        if digit_text and digit_target and (('*' in norm_text) or ('*' in norm_target) or norm_target.isdigit() or norm_text.isdigit()):
            if digit_text == digit_target:
                return True
            if len(digit_text) >= 7 and len(digit_target) >= 7:
                prefix_len = min(4, len(digit_text), len(digit_target))
                suffix_len = min(4, len(digit_text), len(digit_target))
                if (
                    digit_text[:prefix_len] == digit_target[:prefix_len]
                    and digit_text[-suffix_len:] == digit_target[-suffix_len:]
                ):
                    return True
        return False

    @staticmethod
    def _merge_identity_fields(
        customer_name: str,
        existing_state: Optional[Dict[str, Any]] = None,
        customer_id: str = "",
        conversation_id: str = "",
    ) -> Dict[str, Any]:
        existing_state = existing_state or {}
        aliases = []
        seen = set()
        for candidate in [customer_name, *(existing_state.get("aliases", []) or [])]:
            text = str(candidate or "").strip()
            if not text:
                continue
            normalized = text.lower().replace(" ", "").replace("\u200b", "")
            if normalized in seen:
                continue
            seen.add(normalized)
            aliases.append(text)
        merged_customer_id = str(customer_id or existing_state.get("customer_id") or "").strip()
        merged_conversation_id = str(conversation_id or existing_state.get("conversation_id") or "").strip()
        return {
            "aliases": aliases,
            "customer_id": merged_customer_id,
            "conversation_id": merged_conversation_id,
        }

    def _get_conversations_with_direction(self) -> List[dict]:
        """获取会话列表，包含消息方向信息

        [REFACTOR-INST:convergence] JS 主体收敛到 dom_selectors.get_conversations_js()，
        本方法只负责页面调度与默认值补全（customer_id / conversation_id）。
        """
        conversations = []
        try:
            if not self._ensure_chat_page():
                logger.warning("无法确保在聊天页面")
                return conversations

            try:
                self.page.wait_for_selector('[data-e2e="conversation-item"], [class*="conversationItem"], [class*="chat-item"]', timeout=5000)
            except Exception:
                self.page.wait_for_timeout(2000)

            page_url = self.page.url if self.page else "No page"
            logger.debug(f"当前页面URL: {page_url}")

            # 复用 dom_selectors 中维护的会话列表 JS
            result = self.page.evaluate(_dom_get_conversations_js())
            if result:
                # 补全 message_monitor 期望的字段（customer_id / conversation_id）
                for conv in result:
                    conv.setdefault("customer_id", "")
                    conv.setdefault("conversation_id", "")
                conversations = result
                logger.info(f"evaluate返回了 {len(conversations)} 个会话")
                for i, conv in enumerate(conversations[:3]):
                    logger.info(f"  会话{i+1}: {conv.get('customer_name', 'N/A')[:20]} | {conv.get('last_message_content', 'N/A')[:30]} | direction={conv.get('direction', 'N/A')}")
            else:
                logger.warning("evaluate返回空结果")

        except Exception as e:
            logger.error(f"获取会话列表失败: {e}")

        return conversations

    def sync_conversations(self) -> List[dict]:
        """同步会话列表"""
        try:
            conversations = self._get_conversations_with_direction()
            for conv in conversations:
                customer_name = conv.get("customer_name", "")
                customer_id = str(conv.get("customer_id", "") or "").strip()
                raw_conversation_id = str(conv.get("conversation_id", "") or "").strip()
                if raw_conversation_id:
                    conv["conversation_id"] = raw_conversation_id
                elif customer_name or customer_id:
                    conv["conversation_id"] = build_conversation_id(
                        customer_name,
                        "douyin",
                        customer_id,
                    )
            return conversations
        except Exception as e:
            logger.error(f"同步会话列表失败: {e}")
            return []

    @staticmethod
    def _normalize_direction(raw_direction: Any, default: str = "unknown") -> str:
        from src.common.utils import normalize_direction
        return normalize_direction(raw_direction, default)

    @classmethod
    def _looks_like_outbound_assistant_message(cls, content: str, customer_name: str = "") -> bool:
        text = str(content or "").strip()
        if not text:
            return False
        if any(pattern in text for pattern in cls.BOT_REPLY_PATTERNS):
            return True
        if text in {"期待为您服务~", "有需要随时找我~", "还有什么需要帮助的吗？", "有什么问题随时问我哦~"}:
            return True
        if text.startswith(("亲爱的", "您好", "您好！")) and any(
            marker in text for marker in ("服务", "资料", "联系方式", "客服", "转账", "远程协助")
        ):
            return True
        normalized_customer = str(customer_name or "").strip()
        if normalized_customer and text.startswith(f"{normalized_customer}哈喽"):
            return True
        return False

    @classmethod
    def _normalize_message_direction(
        cls,
        content: str,
        raw_direction: Any,
        *,
        customer_name: str = "",
    ) -> str:
        normalized = cls._normalize_direction(raw_direction, default="unknown")
        if normalized != "outbound" and cls._looks_like_outbound_assistant_message(content, customer_name):
            return "outbound"
        return normalized

    @staticmethod
    def _normalize_message_text(content: Any) -> str:
        return " ".join(str(content or "").strip().split())

    @classmethod
    def _strip_preview_time_noise(cls, content: Any) -> str:
        text = str(content or "").strip()
        if not text:
            return ""

        cleaned_lines: List[str] = []
        leading_time_pattern = re.compile(
            r"^(?:刚刚|昨天|\d{1,2}:\d{2}(?::\d{2})?|\d+分钟前|\d+小时前|\d+天前|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})[\s:：-]*",
            re.IGNORECASE,
        )
        for raw_line in text.splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            if any(re.match(pattern, line) for pattern in cls.TIME_PATTERNS):
                continue
            normalized_line = leading_time_pattern.sub("", line).strip()
            if not normalized_line:
                continue
            cleaned_lines.append(normalized_line)

        if cleaned_lines:
            return " ".join(cleaned_lines)
        return cls._normalize_message_text(text)

    @classmethod
    def _normalize_state_key_part(cls, value: Any) -> str:
        return cls._normalize_message_text(value).lower().replace(" ", "").replace("\u200b", "")

    def _build_conversation_state_key(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
    ) -> str:
        normalized_conversation_id = self._normalize_state_key_part(conversation_id)
        if normalized_conversation_id:
            return f"conv:{normalized_conversation_id}"

        normalized_customer_id = self._normalize_state_key_part(customer_id)
        if normalized_customer_id:
            return f"cust:{normalized_customer_id}"

        return str(customer_name or "").strip()

    def _resolve_conversation_state_key(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
    ) -> str:
        preferred_key = self._build_conversation_state_key(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
        if preferred_key in self._conversation_states:
            return preferred_key

        normalized_conversation_id = self._normalize_state_key_part(conversation_id)
        normalized_customer_id = self._normalize_state_key_part(customer_id)
        normalized_customer_name = self._normalize_state_key_part(customer_name)

        for existing_key, state in self._conversation_states.items():
            state = state or {}
            if (
                normalized_conversation_id
                and self._normalize_state_key_part(state.get("conversation_id", "")) == normalized_conversation_id
            ):
                return existing_key
            if (
                normalized_customer_id
                and self._normalize_state_key_part(state.get("customer_id", "")) == normalized_customer_id
            ):
                return existing_key

        if normalized_conversation_id or normalized_customer_id:
            return preferred_key

        if normalized_customer_name:
            for existing_key, state in self._conversation_states.items():
                state = state or {}
                aliases = list(state.get("aliases", []) or [])
                aliases.append(existing_key.split(":", 1)[-1])
                if any(
                    self._normalize_state_key_part(alias) == normalized_customer_name
                    for alias in aliases
                    if alias
                ):
                    return existing_key

        return preferred_key

    def _get_conversation_state(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
    ) -> Dict[str, Any]:
        state_key = self._resolve_conversation_state_key(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
        return self._conversation_states.get(state_key, {})

    def _build_message_signature(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        content: str,
        direction: str,
        message_time: str = "",
        msg_id: str = "",
    ) -> str:
        if msg_id:
            return f"mid:{str(msg_id).strip()}"
        normalized_message_time = self._get_inbound_decision_engine()._normalize_message_time_for_signature(
            message_time
        )
        normalized = "|".join(
            [
                self._normalize_message_text(conversation_id),
                self._normalize_message_text(customer_name),
                self._normalize_message_text(direction),
                self._normalize_message_text(content),
                normalized_message_time,
            ]
        )
        return f"sig:{hashlib.md5(normalized.encode('utf-8')).hexdigest()[:24]}"

    def _build_inbound_identity_signature(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        content: str,
        msg_id: str = "",
    ) -> str:
        return self._get_inbound_decision_engine().build_inbound_identity_signature(
            customer_name=customer_name,
            conversation_id=conversation_id,
            content=content,
            msg_id=msg_id,
        )

    def _build_dom_message_id(self, *, customer_name: str, conversation_id: str, content: str) -> str:
        seed = "|".join(
            [
                self._normalize_message_text(conversation_id),
                self._normalize_message_text(customer_name),
                self._normalize_message_text(content),
            ]
        )
        return f"dom_{hashlib.md5(seed.encode('utf-8')).hexdigest()[:24]}"

    def _get_inbound_decision_engine(self) -> InboundDecisionEngine:
        engine = getattr(self, "_inbound_decision_engine", None)
        if engine is None:
            engine = InboundDecisionEngine(
                normalize_preview_message_reference=self._strip_preview_time_noise,
                is_valid_message=self._is_valid_message,
            )
            self._inbound_decision_engine = engine
        return engine

    @staticmethod
    def _is_relative_message_time_label(message_time: Any) -> bool:
        return InboundDecisionEngine.is_relative_message_time_label(message_time)

    def _should_emit_inbound_message(
        self,
        *,
        prev_state: Dict[str, Any],
        inbound_signature: str,
        current_signature: str,
        has_unread: bool,
        current_time: float,
        current_message_time: str = "",
        current_content: str = "",
    ) -> bool:
        decision = self._get_inbound_decision_engine().classify_source_inbound_state(
            prev_state=prev_state,
            inbound_signature=inbound_signature,
            current_signature=current_signature,
            has_unread=has_unread,
            current_message_time=current_message_time,
            current_content=current_content,
        )
        return decision == "new"

    def fetch_messages(self) -> List[dict]:
        """获取新消息（DOM操作在锁外执行，仅状态更新在锁内）

        修复：API拦截路径不再提前return，继续执行DOM轮询以同步所有会话状态，
        避免未被API捕获的会话因状态未更新而在后续轮询中重复上报或漏报。
        """
        try:
            with self._states_lock:
                self._cleanup_expired_states()

            api_validated_messages: List[dict] = []
            current_time = time.time()

            if hasattr(self, 'api_interceptor') and self.api_interceptor:
                api_messages = self.api_interceptor.get_captured_messages()
                if api_messages:
                    logger.info(f"API拦截器捕获 {len(api_messages)} 条消息")
                    with self._states_lock:
                        for msg in api_messages:
                            cn = msg.get("customer_name", "")
                            customer_id = str(msg.get("customer_id") or msg.get("sender_id") or "").strip()
                            explicit_conversation_id = str(msg.get("conversation_id") or "").strip()
                            content = self._strip_preview_time_noise(
                                msg.get("last_message_content", msg.get("content", ""))
                            )
                            platform = getattr(self, "platform", "douyin")
                            conversation_id = explicit_conversation_id or build_conversation_id(cn, platform)
                            msg_id = str(msg.get("msg_id") or "").strip()
                            state_key = self._resolve_conversation_state_key(
                                customer_name=cn,
                                conversation_id=explicit_conversation_id,
                                customer_id=customer_id,
                            )
                            message_time = (
                                msg.get("last_message_time")
                                or msg.get("created_at")
                                or msg.get("timestamp", "")
                            )
                            direction = self._normalize_message_direction(
                                content,
                                msg.get("direction"),
                                customer_name=cn,
                            )
                            if not cn:
                                continue

                            prev_state = self._conversation_states.get(state_key, {})
                            signature = self._build_message_signature(
                                customer_name=cn,
                                conversation_id=conversation_id,
                                content=content,
                                direction=direction,
                                message_time=message_time,
                                msg_id=msg_id,
                            )
                            inbound_signature = self._build_inbound_identity_signature(
                                customer_name=cn,
                                conversation_id=conversation_id,
                                content=content,
                                msg_id=msg_id,
                            )
                            is_outbound = direction != "inbound"
                            emit_inbound = (
                                direction == "inbound"
                                and content
                                and self._is_valid_message(content)
                                and self._should_emit_inbound_message(
                                    prev_state=prev_state,
                                    inbound_signature=inbound_signature,
                                    current_signature=signature,
                                    has_unread=bool(msg.get("has_unread", False)),
                                    current_time=current_time,
                                    current_message_time=message_time,
                                    current_content=content,
                                )
                            )
                            self._conversation_states[state_key] = {
                                "content": content,
                                "time": current_time,
                                "is_outbound": is_outbound,
                                "sent_by_us": False,
                                "message_time": message_time,
                                "direction": direction,
                                "last_signature": signature,
                                "last_inbound_signature": (
                                    inbound_signature if emit_inbound or direction == "inbound"
                                    else prev_state.get("last_inbound_signature", "")
                                ),
                                "last_inbound_emitted_at": (
                                    current_time if emit_inbound
                                    else prev_state.get("last_inbound_emitted_at", 0)
                                ),
                                "has_unread": bool(msg.get("has_unread", False)),
                                **self._merge_identity_fields(
                                    cn,
                                    prev_state,
                                    customer_id=customer_id,
                                    conversation_id=explicit_conversation_id,
                                ),
                            }
                            if emit_inbound:
                                normalized_msg = {**msg, "direction": "inbound"}
                                normalized_msg["msg_id"] = msg_id or self._build_dom_message_id(
                                    customer_name=cn,
                                    conversation_id=conversation_id,
                                    content=content,
                                )
                                if customer_id:
                                    normalized_msg["customer_id"] = customer_id
                                normalized_msg["conversation_id"] = conversation_id
                                normalized_msg["signal_source"] = str(
                                    msg.get("signal_source")
                                    or msg.get("source")
                                    or "api_intercept"
                                ).strip()
                                api_validated_messages.append(normalized_msg)

            if not self._ensure_chat_page():
                return api_validated_messages

            conversations = self._get_conversations_with_direction()
            if not conversations:
                return api_validated_messages

            dom_new_messages: List[dict] = []

            with self._states_lock:
                for conv in conversations:
                    customer_name = conv.get("customer_name", "")
                    last_message = self._strip_preview_time_noise(conv.get("last_message_content", ""))
                    last_message_time = conv.get("last_message_time", "")
                    has_unread = bool(conv.get("has_unread", False))
                    customer_id = conv.get("customer_id", "")
                    explicit_conversation_id = str(conv.get("conversation_id", "") or "").strip()
                    conversation_id = explicit_conversation_id or build_conversation_id(customer_name, "douyin")
                    state_key = self._resolve_conversation_state_key(
                        customer_name=customer_name,
                        conversation_id=explicit_conversation_id,
                        customer_id=customer_id,
                    )
                    direction = self._normalize_message_direction(
                        last_message,
                        conv.get("direction", "inbound"),
                        customer_name=customer_name,
                    )

                    if not customer_name or not last_message:
                        continue

                    if state_key not in self._conversation_states:
                        signature = self._build_message_signature(
                            customer_name=customer_name,
                            conversation_id=conversation_id,
                            content=last_message,
                            direction=direction,
                            message_time=last_message_time,
                        )
                        inbound_signature = self._build_inbound_identity_signature(
                            customer_name=customer_name,
                            conversation_id=conversation_id,
                            content=last_message,
                        )
                        self._conversation_states[state_key] = {
                            "content": last_message,
                            "time": current_time,
                            "message_time": last_message_time,
                            "direction": direction,
                            "has_unread": has_unread,
                            "last_signature": signature,
                            "last_inbound_signature": inbound_signature if direction == "inbound" else "",
                            "last_inbound_emitted_at": current_time if direction == "inbound" and self._is_valid_message(last_message) else 0,
                            **self._merge_identity_fields(
                                customer_name,
                                customer_id=customer_id,
                                conversation_id=conv.get("conversation_id", ""),
                            ),
                        }
                        if direction != "inbound":
                            self._conversation_states[state_key]["is_outbound"] = True
                            continue
                        should_emit_initial_inbound = has_unread and self._is_valid_message(last_message)
                        if should_emit_initial_inbound:
                            dom_new_messages.append({
                                "customer_name": customer_name,
                                "last_message_content": last_message,
                                "direction": "inbound",
                                "conversation_id": conversation_id,
                                "timestamp": current_time,
                                "msg_id": self._build_dom_message_id(
                                    customer_name=customer_name,
                                    conversation_id=conversation_id,
                                    content=last_message,
                                ),
                                "is_new": True,
                                "is_first_message": True,
                                "signal_source": "conversation_preview",
                            })
                            self._conversation_states[state_key]["is_outbound"] = False
                        else:
                            logger.debug(
                                f"首次发现会话仅建立基线不发射消息: customer={customer_name}, "
                                f"conversation_id={conversation_id or '-'}, has_unread={has_unread}"
                            )
                        continue

                    prev_state = self._conversation_states[state_key]
                    prev_signature = str(prev_state.get("last_signature", "") or "")
                    if not prev_signature:
                        prev_signature = self._build_message_signature(
                            customer_name=customer_name,
                            conversation_id=str(
                                prev_state.get("conversation_id", "")
                                or conv.get("conversation_id", "")
                                or conversation_id
                            ),
                            content=str(prev_state.get("content", "") or ""),
                            direction=str(prev_state.get("direction", direction) or direction),
                            message_time=str(prev_state.get("message_time", "") or ""),
                        )
                    signature = self._build_message_signature(
                        customer_name=customer_name,
                        conversation_id=conversation_id,
                        content=last_message,
                        direction=direction,
                        message_time=last_message_time,
                    )
                    inbound_signature = self._build_inbound_identity_signature(
                        customer_name=customer_name,
                        conversation_id=conversation_id,
                        content=last_message,
                    )
                    same_current_signature = signature == prev_signature
                    if same_current_signature and has_unread == bool(prev_state.get("has_unread", False)):
                        self._conversation_states[state_key]["time"] = current_time
                        continue

                    if prev_signature.startswith("mid:"):
                        dom_inbound_sig = self._build_inbound_identity_signature(
                            customer_name=customer_name,
                            conversation_id=conversation_id,
                            content=last_message,
                        )
                        self._conversation_states[state_key].update({
                            "content": last_message,
                            "time": current_time,
                            "message_time": last_message_time,
                            "direction": direction,
                            "has_unread": has_unread,
                            "last_signature": signature,
                            "last_inbound_signature": (
                                dom_inbound_sig if direction == "inbound"
                                else prev_state.get("last_inbound_signature", "")
                            ),
                            **self._merge_identity_fields(
                                customer_name,
                                prev_state,
                                customer_id=customer_id,
                                conversation_id=conv.get("conversation_id", ""),
                            ),
                        })
                        continue

                    if not self._is_valid_message(last_message):
                        continue

                    if direction != "inbound":
                        prev_sent_by_us = prev_state.get("sent_by_us", False)
                        self._conversation_states[state_key] = {
                            "content": last_message,
                            "time": current_time,
                            "is_outbound": True,
                            "sent_by_us": prev_sent_by_us,
                            "message_time": last_message_time,
                            "direction": direction,
                            "has_unread": has_unread,
                            "last_signature": signature,
                            "last_inbound_signature": prev_state.get("last_inbound_signature", ""),
                            "last_inbound_emitted_at": prev_state.get("last_inbound_emitted_at", 0),
                            **self._merge_identity_fields(
                                customer_name,
                                prev_state,
                                customer_id=customer_id,
                                conversation_id=conv.get("conversation_id", ""),
                            ),
                        }
                        continue

                    if not self._should_emit_inbound_message(
                        prev_state=prev_state,
                        inbound_signature=inbound_signature,
                        current_signature=signature,
                        has_unread=has_unread,
                        current_time=current_time,
                        current_message_time=last_message_time,
                        current_content=last_message,
                    ):
                        self._conversation_states[state_key] = {
                            "content": last_message,
                            "time": current_time,
                            "is_outbound": False,
                            "sent_by_us": False,
                            "message_time": last_message_time,
                            "direction": direction,
                            "has_unread": has_unread,
                            "last_signature": signature,
                            "last_inbound_signature": prev_state.get("last_inbound_signature", inbound_signature),
                            "last_inbound_emitted_at": prev_state.get("last_inbound_emitted_at", 0),
                            **self._merge_identity_fields(
                                customer_name,
                                prev_state,
                                customer_id=customer_id,
                                conversation_id=conv.get("conversation_id", ""),
                            ),
                        }
                        continue

                    dom_new_messages.append({
                        "customer_name": customer_name,
                        "last_message_content": last_message,
                        "direction": direction,
                        "conversation_id": conversation_id,
                        "timestamp": current_time,
                        "msg_id": self._build_dom_message_id(
                            customer_name=customer_name,
                            conversation_id=conversation_id,
                            content=last_message,
                        ),
                        "is_new": True,
                        "signal_source": "conversation_preview",
                    })
                    self._conversation_states[state_key] = {
                        "content": last_message,
                        "time": current_time,
                        "is_outbound": False,
                        "sent_by_us": False,
                        "message_time": last_message_time,
                        "direction": direction,
                        "has_unread": has_unread,
                        "last_signature": signature,
                        "last_inbound_signature": inbound_signature,
                        "last_inbound_emitted_at": current_time,
                        **self._merge_identity_fields(
                            customer_name,
                            prev_state,
                            customer_id=customer_id,
                            conversation_id=conv.get("conversation_id", ""),
                        ),
                    }

            merged_messages = api_validated_messages + dom_new_messages
            if merged_messages:
                logger.info(
                    f"检测到 {len(merged_messages)} 条新消息 "
                    f"(API={len(api_validated_messages)}, DOM={len(dom_new_messages)})"
                )

            return merged_messages

        except Exception as e:
            logger.error(f"获取新消息失败: {e}")
            return []

    def _cleanup_expired_states(self):
        """清理过期的会话状态，防止内存无限增长"""
        current_time = time.time()
        expired_keys = [
            key for key, state in self._conversation_states.items()
            if current_time - state.get("time", 0) > self.STATE_EXPIRE_SECONDS
        ]
        for key in expired_keys:
            del self._conversation_states[key]
        if expired_keys:
            logger.debug(f"清理了 {len(expired_keys)} 个过期会话状态")
        if len(self._conversation_states) > self.MAX_CONVERSATION_STATES:
            sorted_states = sorted(
                self._conversation_states.items(),
                key=lambda x: x[1].get("time", 0)
            )
            remove_count = len(self._conversation_states) - self.MAX_CONVERSATION_STATES
            for key, _ in sorted_states[:remove_count]:
                del self._conversation_states[key]
            logger.debug(f"清理了 {remove_count} 个最旧会话状态，当前数量: {len(self._conversation_states)}")

    def _is_valid_message(self, content: str) -> bool:
        """验证消息是否有效"""
        if not content or len(content.strip()) < 2:
            return False
        stripped = content.strip()
        if any(pattern in content for pattern in self.INVALID_PATTERNS):
            return False
        if any(pattern in stripped for pattern in self.SYSTEM_DYNAMIC_PATTERNS):
            return False
        if re.match(r'^[^\s]{1,16}\s+\d+/\d+$', stripped):
            return False
        # 手机号属于有效留资消息，不能被纯数字时间规则误杀。
        if re.fullmatch(r'1[3-9]\d{9}', stripped):
            return True
        if stripped.startswith('我:') and len(stripped) <= 5:
            return False
        for pattern in self.TIME_PATTERNS:
            if re.match(pattern, stripped):
                return False
        # 过滤机器人回复
        for pattern in self.BOT_REPLY_PATTERNS:
            if pattern in content:
                return False
        return True

    def mark_message_sent(self, customer_name: str, content: str):
        """标记消息已发送。

        仅更新当前会话的最新状态，避免监听层继续维护独立的发送去重规则。
        线程安全：使用_states_lock保护_conversation_states的并发访问。
        """
        try:
            with self._states_lock:
                current_time = time.time()
                state_key = self._resolve_conversation_state_key(customer_name=customer_name)
                prev_state = self._conversation_states.get(state_key, {})
                self._conversation_states[state_key] = {
                    "content": content,
                    "time": current_time,
                    "is_outbound": True,
                    "sent_by_us": True,
                    "message_time": "",
                    "direction": "outbound",
                    "has_unread": False,
                    "last_signature": self._build_message_signature(
                        customer_name=customer_name,
                        conversation_id=prev_state.get("conversation_id", ""),
                        content=content,
                        direction="outbound",
                    ),
                    "last_inbound_signature": prev_state.get("last_inbound_signature", ""),
                    "last_inbound_emitted_at": prev_state.get("last_inbound_emitted_at", 0),
                    **self._merge_identity_fields(customer_name, prev_state),
                }
            logger.debug(f"标记消息已发送: {customer_name} -> {content[:30]}...")
        except Exception as e:
            logger.debug(f"标记消息发送失败: {e}")

    def restore_persisted_state(self, state: dict):
        """从持久化存储恢复消息监控器状态

        在系统重启后恢复会话状态，防止重启后将已有消息误判为新消息。

        Args:
            state: 持久化状态字典，包含 conversation_states
        """
        try:
            with self._states_lock:
                if 'conversation_states' in state:
                    restored_count = 0
                    for name, conv_state in state['conversation_states'].items():
                        if isinstance(conv_state, dict) and name not in self._conversation_states:
                            self._conversation_states[name] = conv_state
                            restored_count += 1
                    if restored_count > 0:
                        logger.info(f"恢复会话状态: {restored_count}个会话")

            logger.info(f"消息监控器状态恢复完成: {len(self._conversation_states)}个会话")
        except Exception as e:
            logger.error(f"恢复消息监控器状态失败: {e}")

    # [REFACTOR-INST:convergence] 整条旧发送链路（send_message_to_active_conversation /
    # _send_by_mode / _verify_send_success / _collect_send_verification_snapshot /
    # _read_input_box_content / _normalize_send_verification_text / _get_api_capture_seq /
    # _collect_api_outbound_fingerprints / _platform_outbound_has_advanced /
    # _snapshot_matches_expected_target / _message_tail_has_advanced /
    # validate_send_target_consistency / _collect_active_conversation_identity_snapshot /
    # _normalize_identity_name 等）共约 500 行已删除。
    #
    # 删除原因：
    # 1. 链路内部引用 self.smart_finder / self.mode_selector，但这两个属性在 MessageMonitor 中
    #    已不再定义（line 96-98 注释明确"smart_finder/mode_selector 已废弃"），运行时会
    #    AttributeError，整条链路是死代码。
    # 2. OutboundSendGateway 是新的统一发送入口（src/web/outbound_send_gateway.py），
    #    内部 _send_via_rpa / _send_via_rpa 优先使用 rpa_engine.DouYinRPAEngine。
    # 3. 兜底路径 bot._send_via_monitor -> send_message_task -> send_message_to_active_conversation
    #    永远会 AttributeError，已无任何恢复可能。
    #
    # _is_target_conversation_active 的真正实现在文件下方（被 click_conversation 链路使用）。

    def ensure_chat_page(self) -> bool:
        """确保当前在聊天页面（公共接口，供外部模块调用）"""
        return self._ensure_chat_page()

    def _ensure_chat_page(self) -> bool:
        """确保当前在聊天页面 - 优先委托给 coordinator，保留旧路径作为回退"""
        try:
            coordinator = getattr(self, "_chat_session_coordinator", None)
            if coordinator and coordinator.ensure_chat_page(monitor=self):
                return True

            if self._chat_page_ensured:
                self._ensure_verify_counter += 1
                try:
                    cached_page = None
                    if self._chat_page and not self._chat_page.is_closed():
                        cached_page = self._chat_page
                    elif self.page and not self.page.is_closed():
                        cached_page = self.page

                    if cached_page is not None:
                        current_url = cached_page.url or ""
                        if "/chat" in current_url and "douyin.com" in current_url:
                            return True
                        logger.warning(f"页面偏离聊天页面: {current_url[:50]}，重置标志")
                    self._chat_page_ensured = False
                except Exception:
                    self._chat_page_ensured = False
            
            logger.debug(f"[DEBUG] _ensure_chat_page: _chat_page_ensured=False, _chat_page={'exists' if self._chat_page else 'None'}, page={'exists' if self.page else 'None'}, page_closed={self.page.is_closed() if self.page else 'N/A'}")

            if self._ensure_chat_page_attempts >= self._max_ensure_attempts:
                # 修复：每5分钟重置尝试次数，允许页面恢复后重新尝试
                current_time = time.time()
                last_reset_time = getattr(self, '_ensure_last_reset_time', 0)
                if current_time - last_reset_time > 300:
                    logger.info("重置_ensure_chat_page尝试次数（5分钟冷却期已过）")
                    self._ensure_chat_page_attempts = 0
                    self._ensure_failed_logged = False
                    self._ensure_last_reset_time = current_time
                else:
                    if not self._ensure_failed_logged:
                        logger.warning(f"已达到最大尝试次数({self._max_ensure_attempts})，跳过创建新页面")
                        self._ensure_failed_logged = True
                    return False

            try:
                if self._chat_page and not self._chat_page.is_closed():
                    current_url = self._chat_page.url or ""
                    if "/chat" in current_url and "douyin.com" in current_url:
                        self.page = self._chat_page
                        self._chat_page_ensured = True
                        self._ensure_chat_page_attempts = 0
                        logger.info("已在聊天页面")
                        return True
            except Exception as url_error:
                logger.debug(f"获取页面URL失败: {url_error}")

            target_page = None
            if self._chat_page and not self._chat_page.is_closed():
                target_page = self._chat_page
            elif self.page and not self.page.is_closed():
                current_url = self.page.url or ""
                # 修复：无论当前页面是什么，都使用它来导航
                target_page = self.page
            
            if target_page:
                try:
                    current_url = target_page.url or ""
                    if "/chat" not in current_url or "douyin.com" not in current_url:
                        logger.info(f"页面不在聊天页面({current_url[:50]})，尝试导航恢复...")
                        from src.config.settings import DOUYIN_CHAT_URL
                        target_page.goto(DOUYIN_CHAT_URL, timeout=30000)
                        target_page.wait_for_load_state("domcontentloaded", timeout=15000)
                        try:
                            # 等待聊天页面元素加载
                            target_page.wait_for_selector('[data-e2e="conversation-item"], [class*="conversationItem"]', timeout=5000)
                        except Exception:
                            target_page.wait_for_timeout(2000)
                        new_url = target_page.url or ""
                        if "/chat" in new_url and "douyin.com" in new_url:
                            self._chat_page = target_page
                            self.page = target_page
                            self._chat_page_ensured = True
                            self._ensure_chat_page_attempts = 0
                            self._ensure_failed_logged = False
                            logger.info("导航恢复成功，已回到聊天页面")
                            return True
                        else:
                            logger.warning(f"导航后页面URL不匹配: {new_url[:50]}")
                except Exception as nav_e:
                    logger.warning(f"导航到聊天页面失败: {nav_e}")

            self._ensure_chat_page_attempts += 1
            logger.info(f"检查聊天页面 (尝试 {self._ensure_chat_page_attempts}/{self._max_ensure_attempts})")

            return False

        except Exception as e:
            logger.error(f"确保聊天页面失败: {e}")
            return False

    def _is_target_conversation_active(self, customer_name: str) -> bool:
        """校验当前右侧激活会话是否已切到目标客户，而不是仅判断输入框存在。"""
        try:
            target_name = str(customer_name or "").strip()
            if not target_name or not self.page:
                return False
            activation = self.page.evaluate(
                """
                (customerName) => {
                    const normalize = (text) => String(text || '').trim().replace(/\\s+/g, '').replace(/\\u200b/g, '');
                    const target = normalize(customerName);
                    const inputBox = document.querySelector('div[contenteditable="true"], textarea, [contenteditable="true"]');
                    if (!inputBox || !target) {
                        return { ok: false, reason: 'input_or_target_missing', activeNames: [] };
                    }

                    const activeNames = [];
                    const pushName = (value) => {
                        const normalized = normalize(value);
                        if (normalized && !activeNames.includes(normalized) && activeNames.length < 8) {
                            activeNames.push(normalized);
                        }
                    };

                    const activeItems = document.querySelectorAll('[data-e2e="conversation-item"][aria-selected="true"], [data-e2e="conversation-item"].active, [data-e2e="conversation-item"][class*="active"]');
                    for (const item of activeItems) {
                        const nameEl = item.querySelector('.conversationConversationItemtitle') || item.querySelector('.nickname') || item.querySelector('.name') || item.querySelector('[class*="title"]') || item.querySelector('[class*="name"]');
                        if (nameEl) pushName(nameEl.textContent || '');
                    }

                    const headerSelectors = [
                        '[data-e2e="chat-user-name"]',
                        '[class*="chatHeader"] [class*="title"]',
                        '[class*="chat-header"] [class*="title"]',
                        '[class*="messageHeader"] [class*="title"]',
                        '[class*="im-chat-header"] [class*="title"]',
                    ];
                    for (const selector of headerSelectors) {
                        const nodes = document.querySelectorAll(selector);
                        for (const node of nodes) {
                            pushName(node.textContent || '');
                        }
                    }

                    const isMatch = activeNames.some((name) => {
                        if (name === target) return true;
                        const minLen = Math.min(name.length, target.length);
                        const maxLen = Math.max(name.length, target.length);
                        return (name.includes(target) || target.includes(name)) && minLen >= maxLen * 0.85;
                    });
                    return { ok: isMatch, reason: isMatch ? 'matched' : 'active_mismatch', activeNames };
                }
                """,
                target_name,
            ) or {}
            if activation.get("ok"):
                return True
            logger.warning(
                f"目标会话未激活: target={target_name}, reason={activation.get('reason', 'unknown')}, "
                f"active_names={activation.get('activeNames', [])}"
            )
        except Exception as exc:
            logger.debug(f"校验目标会话激活失败: {exc}")
        return False

    def _build_click_target_candidates(
        self,
        customer_name: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        candidates: list[str] = []
        seen = set()

        def _add(value: str):
            text = str(value or "").strip()
            normalized = self._normalize_state_key_part(text)
            if not text or not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(text)

        _add(customer_name)
        for candidate in target_candidates or []:
            _add(candidate)
        if customer_id and not str(customer_id).startswith("temp_"):
            _add(customer_id)

        normalized_conversation_id = self._normalize_state_key_part(conversation_id)
        normalized_customer_id = self._normalize_state_key_part(customer_id)
        conversation_states = getattr(self, "_conversation_states", {}) or {}
        for raw_name, state in conversation_states.items():
            if not isinstance(state, dict):
                continue
            state_conversation_id = self._normalize_state_key_part(state.get("conversation_id", ""))
            state_customer_id = self._normalize_state_key_part(state.get("customer_id", ""))
            if normalized_conversation_id and state_conversation_id == normalized_conversation_id:
                _add(raw_name)
                for alias in state.get("aliases", []) or []:
                    _add(alias)
                _add(state.get("customer_id", ""))
            elif normalized_customer_id and state_customer_id == normalized_customer_id:
                _add(raw_name)
                for alias in state.get("aliases", []) or []:
                    _add(alias)
                _add(state.get("customer_id", ""))
        return candidates

    def click_conversation(
        self,
        customer_name: str,
        *,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """点击指定客户名的会话
        
        修复：仅使用聊天专用标签页，避免替换到爬取标签页(crawler_page)，
        防止消息监听与爬取任务并发时页面引用被错误替换。
        """
        try:
            if self._chat_page and not self._chat_page.is_closed():
                try:
                    chat_url = self._chat_page.url or ""
                    if "/chat" in chat_url and "douyin.com" in chat_url:
                        if self.page != self._chat_page:
                            self.page = self._chat_page
                            logger.debug("click_conversation: 使用缓存的聊天页面")
                except Exception as e:
                    logger.debug(f"检查缓存聊天页面失败: {e}")

            page_url = self.page.url or ""
            if not self.page or "/chat" not in page_url or "douyin.com" not in page_url:
                if not self._ensure_chat_page():
                    return False

            click_candidates = self._build_click_target_candidates(
                customer_name,
                conversation_id=conversation_id,
                customer_id=customer_id,
                target_candidates=target_candidates,
            )
            preferred_query = click_candidates[0] if click_candidates else customer_name

            def _apply_search_filter() -> bool:
                try:
                    changed = bool(self.page.evaluate(set_search_js, preferred_query))
                    if changed:
                        logger.info(
                            f"直接会话列表未命中，已通过搜索框过滤用户: {preferred_query}, "
                            f"candidates={click_candidates[:4]}"
                        )
                        self.page.wait_for_timeout(800)
                    return changed
                except Exception as search_e:
                    logger.debug(f"搜索框过滤用户失败: {search_e}")
                    return False

            set_search_js = """
            (targetName) => {
                const selectors = [
                    'input[placeholder*="搜索"]',
                    'input[placeholder*="search"]',
                    'input[type="search"]',
                    '[role="searchbox"] input',
                    '[class*="search"] input'
                ];
                let changed = false;
                for (const selector of selectors) {
                    const inputs = document.querySelectorAll(selector);
                    for (const input of inputs) {
                        try {
                            const currentValue = (input.value || '').trim();
                            if (currentValue !== targetName) {
                                input.focus();
                                input.value = targetName;
                                input.dispatchEvent(new Event('input', { bubbles: true }));
                                input.dispatchEvent(new Event('change', { bubbles: true }));
                                changed = true;
                            }
                        } catch (e) {}
                    }
                }
                return changed;
            }
            """
            self.page.wait_for_timeout(400)

            try:
                locator = self.page.locator('[data-e2e="conversation-item"]')
                items = locator.all()
                clicked = False
                visible_name_samples = []
                for item in items:
                    try:
                        name_el = item.locator('.conversationConversationItemtitle, [class*="title"], [class*="name"], .nickname').first
                        if name_el.count() > 0:
                            name_text = name_el.text_content(timeout=1000).strip()
                            if name_text and len(visible_name_samples) < 8:
                                visible_name_samples.append(name_text)
                            for candidate in click_candidates or [customer_name]:
                                if self._fuzzy_match_name(name_text, candidate):
                                    item.click()
                                    clicked = True
                                    break
                            if clicked:
                                break
                    except Exception:
                        continue
                if clicked:
                    self.page.wait_for_timeout(2000)
                    logger.info(
                        f"成功点击会话: {customer_name}, visible_count={len(items)}, "
                        f"candidates={click_candidates[:4]}, "
                        f"sample_names={visible_name_samples}"
                    )
                    return self._is_target_conversation_active(customer_name)
                if allow_search_filter:
                    _apply_search_filter()
            except Exception as e:
                logger.debug(f"点击会话失败: {e}")

            click_js = """
            (payload) => {
                const candidates = Array.isArray(payload?.candidates) ? payload.candidates : [];
                const targetName = String(payload?.customerName || '').trim();
                if (targetName) candidates.unshift(targetName);
                const isMaskedNumericMatch = (candidate, target) => {
                    const candidateDigits = (candidate || '').replace(/\\D/g, '');
                    const targetDigits = (target || '').replace(/\\D/g, '');
                    if (!candidateDigits || !targetDigits) return false;
                    if (candidateDigits === targetDigits) return true;
                    if (Math.min(candidateDigits.length, targetDigits.length) < 7) return false;
                    const prefixLen = Math.min(4, candidateDigits.length, targetDigits.length);
                    const suffixLen = Math.min(4, candidateDigits.length, targetDigits.length);
                    return candidateDigits.slice(0, prefixLen) === targetDigits.slice(0, prefixLen) &&
                           candidateDigits.slice(-suffixLen) === targetDigits.slice(-suffixLen);
                };
                const items = document.querySelectorAll('[data-e2e="conversation-item"]');
                const normalizedCandidates = candidates
                    .map((item) => String(item || '').trim().replace(/\\s+/g, '').replace(/\\u200b/g, ''))
                    .filter(Boolean);
                let exactMatch = null;
                let fuzzyMatch = null;
                const sampleNames = [];
                for (const item of items) {
                    const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                                   item.querySelector('.nickname') || item.querySelector('.name');
                    if (nameEl) {
                        const nameText = nameEl.textContent.trim().replace(/\\s+/g, '').replace(/\\u200b/g, '');
                        if (nameText && sampleNames.length < 8) {
                            sampleNames.push(nameText);
                        }
                        for (const normalizedTarget of normalizedCandidates) {
                            if (nameText === normalizedTarget) {
                                exactMatch = item;
                                break;
                            }
                            if (
                                !fuzzyMatch &&
                                nameText &&
                                normalizedTarget &&
                                isMaskedNumericMatch(nameText, normalizedTarget)
                            ) {
                                fuzzyMatch = item;
                            }
                        }
                        if (exactMatch) break;
                    }
                }
                const match = exactMatch || fuzzyMatch;
                if (match) {
                    match.scrollIntoView(true);
                    match.dispatchEvent(new MouseEvent('click', { bubbles: true, button: 0 }));
                    return { success: true, count: items.length, sampleNames };
                }
                return { success: false, count: items.length, sampleNames };
            }
            """

            scroll_list_js = """
            () => {
                const knownSelectors = [
                    '[class*="conversationList"]',
                    '[class*="conversation-list"]',
                    '[class*="chatList"]',
                    '[class*="chat-list"]',
                    '[class*="messageList"]',
                    '[class*="sidebar"]'
                ];
                let container = null;
                for (const selector of knownSelectors) {
                    const candidate = document.querySelector(selector);
                    if (candidate && candidate.scrollHeight > candidate.clientHeight + 20) {
                        container = candidate;
                        break;
                    }
                }
                if (!container) {
                    const firstItem = document.querySelector('[data-e2e="conversation-item"], [class*="conversationItem"], [class*="chat-item"]');
                    let parent = firstItem ? firstItem.parentElement : null;
                    while (parent) {
                        const style = window.getComputedStyle(parent);
                        const overflowY = `${style.overflowY || ''}${style.overflow || ''}`;
                        if ((overflowY.includes('auto') || overflowY.includes('scroll')) &&
                            parent.scrollHeight > parent.clientHeight + 20) {
                            container = parent;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                }
                if (!container) {
                    return { changed: false, reason: 'container_not_found' };
                }
                const before = container.scrollTop;
                const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
                const step = Math.max(container.clientHeight * 0.8, 320);
                container.scrollTop = Math.min(maxScrollTop, before + step);
                return {
                    changed: container.scrollTop > before,
                    scrollTop: container.scrollTop,
                    maxScrollTop
                };
            }
            """

            last_result = {}
            scroll_attempts = 0
            for _ in range(8):
                result = self.page.evaluate(
                    click_js,
                    {
                        "customerName": customer_name,
                        "candidates": click_candidates,
                        "conversationId": conversation_id,
                        "customerId": customer_id,
                    },
                ) or {}
                last_result = result
                if result.get('success'):
                    self.page.wait_for_timeout(2000)
                    logger.info(
                        f"成功点击会话(JS): {customer_name}, visible_count={result.get('count', 0)}, "
                        f"sample_names={result.get('sampleNames', [])}, scroll_attempts={scroll_attempts}, "
                        f"candidates={click_candidates[:4]}"
                    )

                    # 保留搜索过滤结果，避免清空后 React 将会话重置回默认首项（如 chen）
                    return self._is_target_conversation_active(customer_name)

                scroll_result = self.page.evaluate(scroll_list_js) or {}
                if not scroll_result.get('changed'):
                    logger.debug(f"Scroll changed is false: {scroll_result}")
                    break
                scroll_attempts += 1
                self.page.wait_for_timeout(400)

            logger.warning(
                f"点击会话未命中: {customer_name}, visible_count={last_result.get('count', 0)}, "
                f"sample_names={last_result.get('sampleNames', [])}, scroll_attempts={scroll_attempts}, "
                f"candidates={click_candidates[:4]}, conversation_id={conversation_id or '-'}, customer_id={customer_id or '-'}"
            )
            
            return False

        except Exception as e:
            logger.error(f"点击会话失败: {e}")
            return False

