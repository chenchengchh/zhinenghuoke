"""
rpa_engine.core - RPA 引擎核心实现

阶段D·D-1 拆分产物：
原 rpa_engine.py 中"数据/常量"已抽到 models.py，本模块只保留
DouYinRPAEngine 主类及其方法实现。

业务逻辑：
1. DOM监听（首选）：用MutationObserver监听消息列表容器的子节点新增
2. 轮询（备选）：定时查询未读元素与消息列表长度
3. 去重：用msg_id/create_time标记已处理消息
4. 内容提取：定位消息文本、发送者、时间
"""

import time
import hashlib
import threading
import random
import contextlib
import json
import re
from typing import List, Dict, Optional, Callable, Any, Tuple
from datetime import datetime
from pathlib import Path
from loguru import logger
from src.common.conversation_id import build_conversation_id
from src.common.debug_instrument import debug_event
from src.common.utils import normalize_direction
from src.douyin_bot.api_interceptor import acquire_shared_api_interceptor, release_shared_api_interceptor
from src.douyin_bot.boundary_guard import BoundaryGuard
from src.douyin_bot.human_interaction import HumanInteractionHelper
from src.douyin_bot.smart_element_finder import SmartElementFinder
from src.douyin_bot.input_locator_service import InputLocatorContext, get_input_locator_service
from src.douyin_bot.network_message_detector import (
    NetworkMessageDetector,
    RPAMessageAdapter,
)
from src.douyin_bot.send_verification_policy import (
    collection_has_advanced_match,
    decide_send_confirmation,
    message_tail_has_advanced,
)
from src.config.settings import DOUYIN_CHAT_URL
from src.infrastructure.runtime_paths import get_log_dir
from src.web.inbound_decision_engine import InboundDecisionEngine

# 阶段D·D-1：从 models 抽出数据类/枚举/常量，保持向后兼容
from .models import (
    MessageDirection,
    OperationMode,
    PageState,
    RPAMessage,
    OperationResult,
    InboundDecisionResult,
    SELECTOR_POOL,
    _STATE_UNSET,
)

# 在模块顶层提供 SELECTOR_POOL 别名，便于外部 `from rpa_engine.core import SELECTOR_POOL`


class DouYinRPAEngine:
    """
    抖音RPA引擎
    
    核心职责：
    1. 消息获取（DOM监听 + 轮询备选）
    2. 消息发送
    3. 状态管理
    """

    POLL_INTERVAL = 1.0
    SESSION_CACHE_TTL = 1800
    SENT_MESSAGE_TTL = 60
    DIRTY_UNREAD_RECHECK_INTERVAL = 30
    DIRTY_UNREAD_MAX_RECHECK_INTERVAL = 300
    MAX_CONCURRENT_OPERATIONS = 1
    OPERATION_TIMEOUT = 30
    RATE_LIMIT_WINDOW = 60
    MAX_SEND_PER_WINDOW = 20
    MAX_RETRIES = 3
    BASE_RETRY_DELAY = 1.0
    MAX_RETRY_DELAY = 10.0
    MIN_CHAT_INPUT_SCORE = 60
    SEND_BUTTON_SELECTORS = [
        "[data-e2e='send-message-btn']",
        "button[data-e2e='send-message-btn']",
        "button:has-text('发送')",
        "button:has-text('Send')",
        "[class*='send-message']",
        "[class*='send-btn']",
        "[class*='sendBtn']",
        "[class*='sendButton']",
    ]
    LOGIN_CHECK_INTERVAL = 30
    MIN_MESSAGE_LENGTH = 2
    MAX_MESSAGE_LENGTH = 2000
    MAX_SENT_CACHE_SIZE = 500
    OBSERVER_DOM_REFRESH_EVERY_POLLS = 6
    TARGET_CONVERSATION_READY_WAIT_MS = 1800
    TARGET_INPUT_READY_TIMEOUT_MS = 1200
    SEND_PRE_DISPATCH_WAIT_MS = 80
    SEND_POST_BUTTON_WAIT_MS = 200
    SEND_POST_ENTER_WAIT_MS = 150
    SEND_VERIFY_INITIAL_WAIT_MS = 80
    SEND_VERIFY_WAIT_ROUNDS_MS = (120, 220)

    INVALID_PATTERNS = [
        '已撤回', '正在输入', '正在加载',
        '滑动的', '上拉', '下拉', '系统消息', '系统通知',
        '[图片]', '[语音]', '[视频]', '[表情]', '[文件]', '[链接]',
        '[红包]', '[位置]', '[名片]', '[小程序]', '[商品]',
        '对方回复或关注你之前，只能发送一条文字消息',
        '请礼貌发言，自觉遵守',
        '抖音自律公约',
        '你已关注对方，现在可以发送消息了',
        '你们已成为好友，可以开始聊天了',
        '关注后才能发送消息',
        '发送消息需要先关注',
    ]

    STATUS_INDICATOR_PATTERNS = [
        '小时前在线', '分钟前在线', '刚刚在线', '天前在线',
        '小时前离线', '分钟前离线', '天前离线',
    ]

    SYSTEM_DYNAMIC_PATTERNS = [
        '加入了群聊',
        '查看历史消息',
        '赞了对方分享的',
        '赞了你分享的',
        '通过明天',
        '个人主页',
        '新成员可查看历史消息',
        '分享的 视频',
        '分享的视频',
    ]

    BOT_REPLY_PATTERNS = [
        'Thinking Process', 'Analyze the Request',
        'Professional Sales Consultant', 'General Service Sales',
        '序号：', '序号:',
        '对公转账需要：公司名称',
        '方便的话也可以留个接收资料的联系方式',
        '更完整的资料、详细说明',
        '继续为您处理',
    ]

    BOT_REPLY_REGEX_PATTERNS = [
        r'^序号[：:]\s*\d',
        r'(?is)^thinking process[:：]?\s',
    ]

    def __del__(self):
        interceptor = getattr(self, "api_interceptor", None)
        page = getattr(self, "page", None)
        if interceptor is not None and page is not None:
            try:
                release_shared_api_interceptor(page, interceptor)
            except Exception:
                pass

    def __init__(self, page, browser_manager=None):
        """初始化RPA引擎"""
        self.page = page
        self.browser_manager = browser_manager
        self.api_interceptor = None
        self._owner_thread = threading.current_thread()
        self.browser_context = None
        if page:
            try:
                self.browser_context = page.context
            except Exception as e:
                logger.debug(f"获取页面上下文失败: {e}")
        if not self.browser_context and browser_manager:
            try:
                self.browser_context = browser_manager.context
            except Exception as e:
                logger.debug(f"获取浏览器管理器上下文失败: {e}")

        self._states: Dict[str, Dict] = {}
        self._sent_cache: Dict[str, float] = {}
        self._processed_msg_ids: Dict[str, float] = {}
        self._page_state = PageState.NORMAL
        self._login_valid = False

        self._state_lock = threading.RLock()
        self._operation_lock = threading.Semaphore(self.MAX_CONCURRENT_OPERATIONS)

        self._last_login_check = 0
        self._last_poll_time = 0
        self._poll_count = 0
        self._last_active_snapshot_log_signature = ""
        self._last_active_snapshot_log_time = 0.0
        self._last_sparse_conversation_retry_time = 0.0

        self._message_callback: Optional[Callable] = None
        self._state_callback: Optional[Callable] = None

        self._monitoring = False
        self._first_fetch_done = False
        self._observer_active = False
        self._chat_page_ensured = False
        self._ensure_verify_counter = 0
        self._recovery_attempt_count = 0
        self._last_recovery_attempt_time = 0
        self._recovery_notified = False

        self._boundary_guard = BoundaryGuard()
        if page:
            try:
                self.api_interceptor = acquire_shared_api_interceptor(page)
            except Exception as e:
                logger.debug(f"初始化RPA API拦截器失败: {e}")

        import queue
        self._send_queue: queue.Queue = queue.Queue()
        self._send_results: Dict[str, OperationResult] = {}
        self._send_results_lock = threading.RLock()
        self._prepare_queue: queue.Queue = queue.Queue()
        self._prepare_results: Dict[str, bool] = {}
        self._prepare_results_lock = threading.RLock()
        self._queue_processing_lock = threading.RLock()
        self._typing_lock = threading.RLock()  # 防止焦点丢失的原子锁
        self._smart_finder = SmartElementFinder(page) if page else None
        self._dom_diag_dir = get_log_dir() / "dom_diagnostics"
        self._dom_diag_dir.mkdir(parents=True, exist_ok=True)
        self._my_user_id: Optional[str] = None
        self._my_user_id_resolved_at: float = 0
        self._network_detector: Optional[NetworkMessageDetector] = None
        self._cancelled_send_tasks: Dict[str, float] = {}

    def resolve_my_user_id(self) -> Optional[str]:
        if not hasattr(self, '_my_user_id'):
            self._my_user_id: Optional[str] = None
            self._my_user_id_resolved_at: float = 0
        if self._my_user_id and (time.time() - self._my_user_id_resolved_at) < 3600:
            return self._my_user_id
        uid = self._resolve_my_user_id_from_api_interceptor()
        if not uid and hasattr(self, 'page'):
            uid = self._resolve_my_user_id_from_cookies()
        if not uid and hasattr(self, 'page'):
            uid = self._resolve_my_user_id_from_dom()
        if uid:
            self._my_user_id = uid
            self._my_user_id_resolved_at = time.time()
            logger.info(f"已解析当前登录用户ID: {uid}")
        return self._my_user_id

    def _resolve_my_user_id_from_api_interceptor(self) -> Optional[str]:
        interceptor = getattr(self, 'api_interceptor', None)
        if not interceptor:
            return None
        try:
            requests = interceptor.get_intercepted_requests()
            for req in requests[-50:]:
                data = req.get('data', {})
                if not isinstance(data, dict):
                    continue
                inner = data.get('data', {})
                if isinstance(inner, dict):
                    uid = inner.get('user_id') or inner.get('uid') or inner.get('sec_uid')
                    if uid and str(uid).strip():
                        return str(uid).strip()
        except Exception:
            pass
        return None

    def _resolve_my_user_id_from_cookies(self) -> Optional[str]:
        if not self.page or self.page.is_closed():
            return None
        try:
            cookies = self.page.context.cookies()
            for c in cookies:
                name = c.get('name', '')
                if name == 'uid_tt' or name == 'uid_tt_ss':
                    val = str(c.get('value', '')).strip()
                    if val and len(val) > 5:
                        return val
                elif name == 'passport_csrf_id':
                    val = str(c.get('value', '')).strip()
                    if val and len(val) > 5:
                        return f"csrf:{val}"
        except Exception:
            pass
        return None

    def _resolve_my_user_id_from_dom(self) -> Optional[str]:
        if not self.page or self.page.is_closed():
            return None
        try:
            result = self._safe_evaluate("""
            () => {
                const avatar = document.querySelector('[data-e2e="navigation-avatar"]') ||
                              document.querySelector('[data-e2e="user-avatar"]');
                if (avatar) {
                    const uid = avatar.getAttribute('data-user-id') ||
                               avatar.getAttribute('data-sec-uid') ||
                               avatar.dataset?.userId ||
                               avatar.dataset?.secUid;
                    if (uid) return uid;
                }
                const userLink = document.querySelector('a[href*="/user/"]');
                if (userLink) {
                    const match = userLink.href.match(/\\/user\\/([A-Za-z0-9_-]+)/);
                    if (match) return match[1];
                }
                return null;
            }
            """, timeout_ms=5000)
            if result and str(result).strip():
                return str(result).strip()
        except Exception:
            pass
        return None

    def _human_helper(self) -> HumanInteractionHelper:
        helper = getattr(self, "_human", None)
        if helper is None:
            helper = HumanInteractionHelper(self.page)
            self._human = helper
        return helper

    def _init_network_detector(self) -> None:
        try:
            if not hasattr(self, '_network_detector'):
                self._network_detector = None

            use_network = True
            try:
                from src.config.settings import USE_NETWORK_DETECTION
                use_network = USE_NETWORK_DETECTION
            except ImportError:
                pass

            if not use_network:
                logger.info("网络消息检测已通过配置关闭，使用 DOM 方案")
                return

            if not self.page or self.page.is_closed():
                logger.warning("页面不可用，跳过网络消息检测器初始化")
                return

            dedup_window = 600
            try:
                from src.config.settings import NETWORK_DETECTION_DEDUP_WINDOW_SECONDS
                dedup_window = NETWORK_DETECTION_DEDUP_WINDOW_SECONDS
            except ImportError:
                pass

            self._network_detector = NetworkMessageDetector(
                page=self.page,
                my_user_id_resolver=self.resolve_my_user_id,
                dedup_window_seconds=dedup_window,
            )
            enabled = self._network_detector.enable()
            if enabled:
                logger.info("网络消息检测器初始化成功，将作为主链路")
            else:
                logger.warning("网络消息检测器启用失败，回退到 DOM 方案")
                self._network_detector = None
        except Exception as e:
            logger.warning(f"网络消息检测器初始化异常: {e}，回退到 DOM 方案")
            self._network_detector = None

    def _poll_network_messages(self) -> List[RPAMessage]:
        """通过网络拦截获取新入站消息"""
        network_detector = getattr(self, '_network_detector', None)
        if not network_detector or not network_detector.is_enabled:
            return []

        try:
            network_messages = network_detector.get_new_inbound_messages()
            if not network_messages:
                return []

            rpa_messages: List[RPAMessage] = []
            for nm in network_messages:
                msg_dict = RPAMessageAdapter.to_rpa_message(nm, "douyin")

                if not msg_dict.get("customer_name"):
                    continue

                content = str(msg_dict.get("content", "") or "").strip()
                if not content or len(content) < self.MIN_MESSAGE_LENGTH:
                    continue

                if any(pattern in content for pattern in self.INVALID_PATTERNS):
                    continue
                if any(pattern in content for pattern in self.SYSTEM_DYNAMIC_PATTERNS):
                    continue
                if any(pattern in content for pattern in self.BOT_REPLY_PATTERNS):
                    continue

                direction_str = msg_dict.get("direction", "inbound")
                if direction_str == "inbound" or direction_str == "unknown":
                    direction = MessageDirection.INBOUND
                else:
                    direction = MessageDirection.OUTBOUND
                conversation_id = msg_dict.get("conversation_id", "")
                customer_id = msg_dict.get("customer_id", "")
                msg_id = msg_dict.get("msg_id", "")
                timestamp = msg_dict.get("timestamp", 0.0) or 0.0

                rpa_msg = RPAMessage(
                    customer_name=msg_dict["customer_name"],
                    content=content,
                    direction=direction,
                    timestamp=timestamp if timestamp else time.time(),
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    sender_id=msg_dict.get("sender_id", ""),
                    is_new=True,
                    msg_id=msg_id,
                    signal_source=msg_dict.get("signal_source", "network_api"),
                    direction_confidence=msg_dict.get("direction_confidence", "high"),
                )

                if self._is_new_message(rpa_msg):
                    rpa_messages.append(rpa_msg)
                    logger.info(
                        f"[Network] 新入站消息: {rpa_msg.customer_name} -> "
                        f"{content[:40]}... direction_source={msg_dict.get('direction_source', '')}"
                    )
                else:
                    if msg_id:
                        network_detector.mark_message_seen(msg_id)

            return rpa_messages

        except Exception as e:
            logger.error(f"网络消息检测失败: {e}")
            return []

    def _ensure_queue_runtime_state(self):
        """兼容旧实例或热恢复场景，补齐发送/预热队列所需的运行时字段。"""
        import queue

        repaired_fields = []

        if not hasattr(self, "_owner_thread") or self._owner_thread is None:
            self._owner_thread = threading.current_thread()
            repaired_fields.append("_owner_thread")
        if not hasattr(self, "_send_queue") or self._send_queue is None:
            self._send_queue = queue.Queue()
            repaired_fields.append("_send_queue")
        if not hasattr(self, "_send_results") or self._send_results is None:
            self._send_results = {}
            repaired_fields.append("_send_results")
        if not hasattr(self, "_send_results_lock") or self._send_results_lock is None:
            self._send_results_lock = threading.RLock()
            repaired_fields.append("_send_results_lock")
        if not hasattr(self, "_prepare_queue") or self._prepare_queue is None:
            self._prepare_queue = queue.Queue()
            repaired_fields.append("_prepare_queue")
        if not hasattr(self, "_prepare_results") or self._prepare_results is None:
            self._prepare_results = {}
            repaired_fields.append("_prepare_results")
        if not hasattr(self, "_prepare_results_lock") or self._prepare_results_lock is None:
            self._prepare_results_lock = threading.RLock()
            repaired_fields.append("_prepare_results_lock")
        if not hasattr(self, "_queue_processing_lock") or self._queue_processing_lock is None:
            self._queue_processing_lock = threading.RLock()
            repaired_fields.append("_queue_processing_lock")
        if not hasattr(self, "_typing_lock") or self._typing_lock is None:
            self._typing_lock = threading.RLock()
            repaired_fields.append("_typing_lock")
        if not hasattr(self, "_smart_finder") or (
            self.page and getattr(self._smart_finder, "page", None) is not self.page
        ):
            self._smart_finder = SmartElementFinder(self.page) if self.page else None
            repaired_fields.append("_smart_finder")
        if (not hasattr(self, "browser_context") or self.browser_context is None) and self.page:
            try:
                self.browser_context = self.page.context
                repaired_fields.append("browser_context")
            except Exception:
                pass

        if repaired_fields:
            logger.warning(
                "RPA引擎运行时字段缺失，已自动补齐: %s",
                ",".join(repaired_fields),
            )

    def _safe_evaluate(self, js_expression: str, timeout_ms: int = 8000):
        """安全执行页面JS脚本，带超时保护
        
        通过try-except捕获异常实现安全执行。
        Playwright同步API的page.evaluate()不支持timeout参数，
        使用默认超时行为（30秒），通过异常捕获防止线程阻塞。
        
        Args:
            js_expression: JavaScript表达式字符串
            timeout_ms: 超时时间（毫秒），仅用于日志标识
            
        Returns:
            JS执行结果，超时或异常时返回None
        """
        if not self.page or self.page.is_closed():
            return None
        try:
            return self.page.evaluate(js_expression)
        except Exception as e:
            err_str = str(e)
            if "Timeout" in err_str or "timeout" in err_str:
                logger.warning(f"页面JS执行超时({timeout_ms}ms): {e}")
            else:
                logger.debug(f"页面JS执行失败: {e}")
            return None

    def _safe_evaluate_with_args(self, js_expression: str, arg, timeout_ms: int = 8000):
        """安全执行页面JS脚本（带参数），带超时保护
        
        Args:
            js_expression: JavaScript函数字符串
            arg: 传递给JS函数的参数
            timeout_ms: 超时时间（毫秒），仅用于日志标识
            
        Returns:
            JS执行结果，超时或异常时返回None
        """
        if not self.page or self.page.is_closed():
            # [REFACTOR-INST:convergence] 统一埋点：自动 try/except / 字段化
            debug_event(
                "DBG-SAFE-EVAL", "page_closed",
                timeout_ms=timeout_ms,
                js_len=len(js_expression),
                js_head=js_expression[:80],
            )
            return None
        try:
            return self.page.evaluate(js_expression, arg)
        except Exception as e:
            err_str = str(e)
            # [REFACTOR-INST:convergence] 统一埋点
            import time as _t
            debug_event(
                "DBG-SAFE-EVAL", "EXCEPTION",
                exc_type=type(e).__name__,
                timeout_ms=timeout_ms,
                js_len=len(js_expression),
                ts=_t.time(),
                err=err_str[:300],
            )
            if "Timeout" in err_str or "timeout" in err_str:
                logger.warning(f"页面JS执行超时({timeout_ms}ms): {e}")
            else:
                logger.debug(f"页面JS执行失败(带参数): {e}")
            return None

    def _clear_conversation_search_filter(self) -> bool:
        """清理左侧会话搜索框残留过滤，避免监听只看到被筛选后的列表。"""
        if not self.page or self.page.is_closed():
            return False
        clear_js = """
        (selectors) => {
            const candidateSelectors = selectors || [
                'input[placeholder*="搜索"]',
                'input[placeholder*="search"]',
                'input[type="search"]',
                '[role="searchbox"] input',
                '[class*="search"] input'
            ];
            let changed = false;
            for (const selector of candidateSelectors) {
                const inputs = document.querySelectorAll(selector);
                for (const input of inputs) {
                    try {
                        const currentValue = String(input.value || '').trim();
                        if (!currentValue) continue;
                        input.focus();
                        input.value = '';
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Escape' }));
                        input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Escape' }));
                        changed = true;
                    } catch (e) {}
                }
            }
            return changed;
        }
        """
        try:
            changed = bool(self.page.evaluate(clear_js, SELECTOR_POOL["search_input"]))
            if changed:
                logger.info("已清理左侧会话搜索框残留过滤")
                self.page.wait_for_timeout(250)
            return changed
        except Exception as e:
            logger.debug(f"清理左侧会话搜索框失败: {e}")
            return False

    def _get_dom_diag_dir(self) -> Path:
        diag_dir = getattr(self, "_dom_diag_dir", None)
        if isinstance(diag_dir, Path):
            diag_dir.mkdir(parents=True, exist_ok=True)
            return diag_dir
        diag_dir = get_log_dir() / "dom_diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        self._dom_diag_dir = diag_dir
        return diag_dir

    @staticmethod
    def _sanitize_dom_diag_token(value: str, fallback: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value or "").strip())
        cleaned = cleaned.strip("._-")
        return cleaned[:60] or fallback

    def _collect_chat_dom_snapshot(self, *, customer_name: str = "", content: str = "", stage: str = "", attempt: int = 0) -> Dict[str, Any]:
        # [REFACTOR-INST:convergence] 统一埋点
        import time as _t
        debug_event(
            "DBG-SNAP", "enter",
            customer=customer_name,
            stage=stage,
            attempt=attempt,
            content_len=len(content or ""),
            ts=_t.time(),
        )
        if not self.page or self.page.is_closed():
            debug_event(
                "DBG-SNAP", "page_unavailable",
                stage=stage,
                customer=customer_name,
            )
            return {
                "success": False,
                "reason": "page_unavailable",
                "stage": stage,
                "customer_name": customer_name,
                "attempt": attempt,
            }

        payload = {
            "customerName": customer_name,
            "contentPreview": (content or "")[:120],
            "stage": stage,
            "attempt": attempt,
        }
        snapshot_js = """
        (payload) => {
            const limit = (arr, maxCount = 20) => Array.isArray(arr) ? arr.slice(0, maxCount) : [];
            const rectInfo = (el) => {
                if (!el || !el.getBoundingClientRect) return {};
                const rect = el.getBoundingClientRect();
                return {
                    top: Math.round(rect.top || 0),
                    left: Math.round(rect.left || 0),
                    width: Math.round(rect.width || 0),
                    height: Math.round(rect.height || 0),
                    bottom: Math.round(rect.bottom || 0),
                    right: Math.round(rect.right || 0),
                };
            };
            const normalizeText = (text) => (text || '').replace(/\\s+/g, ' ').trim();
            const styleInfo = (el) => {
                if (!el || !window.getComputedStyle) return {};
                const style = window.getComputedStyle(el);
                return {
                    display: style.display || '',
                    position: style.position || '',
                    alignItems: style.alignItems || '',
                    justifyContent: style.justifyContent || '',
                    paddingTop: style.paddingTop || '',
                    paddingBottom: style.paddingBottom || '',
                    marginTop: style.marginTop || '',
                    marginBottom: style.marginBottom || '',
                    minHeight: style.minHeight || '',
                    height: style.height || '',
                    lineHeight: style.lineHeight || '',
                    whiteSpace: style.whiteSpace || '',
                };
            };
            const childSummary = (el) => {
                if (!el || !el.childNodes) return [];
                return Array.from(el.childNodes).slice(0, 5).map((node) => {
                    if (!node) return {};
                    const text = normalizeText(node.textContent || '');
                    const element = node.nodeType === Node.ELEMENT_NODE ? node : null;
                    const childStyle = element ? styleInfo(element) : {};
                    return {
                        nodeType: Number(node.nodeType || 0),
                        tag: element ? (element.tagName || '').toLowerCase() : '#text',
                        className: element && typeof element.className === 'string' ? element.className : '',
                        text: text.slice(0, 120),
                        rect: element ? rectInfo(element) : {},
                        style: childStyle,
                    };
                });
            };
            const editorDebug = (el) => {
                if (!el) return null;
                return {
                    innerHTML: String(el.innerHTML || '').slice(0, 1500),
                    textContent: normalizeText(el.textContent || '').slice(0, 300),
                    style: styleInfo(el),
                    children: childSummary(el),
                };
            };
            const messageDebug = (el) => {
                if (!el) return null;
                const contentEl =
                    el.querySelector('[class*="messageMessageBoxcontentBox"]') ||
                    el.querySelector('[data-e2e="msg-item-content"]') ||
                    el;
                const classText = [
                    el.className || '',
                    contentEl && typeof contentEl.className === 'string' ? contentEl.className : '',
                    contentEl && contentEl.closest && contentEl.closest('[class*="isFromMe"]') ? 'isFromMe' : '',
                ].join(' ');
                return {
                    box: nodeInfo(el),
                    content: nodeInfo(contentEl),
                    innerHTML: String((contentEl && contentEl.innerHTML) || '').slice(0, 1500),
                    textContent: normalizeText((contentEl && contentEl.textContent) || '').slice(0, 300),
                    style: styleInfo(contentEl),
                    children: childSummary(contentEl),
                    isFromMe: classText.toLowerCase().includes('isfromme'),
                };
            };
            const nodeInfo = (el) => {
                if (!el) return null;
                const text = normalizeText(el.innerText || el.textContent || '');
                const parent = el.parentElement;
                const grandParent = parent && parent.parentElement;
                return {
                    tag: (el.tagName || '').toLowerCase(),
                    dataE2e: el.getAttribute && el.getAttribute('data-e2e') || '',
                    role: el.getAttribute && el.getAttribute('role') || '',
                    placeholder: el.getAttribute && (el.getAttribute('placeholder') || el.getAttribute('data-placeholder')) || '',
                    className: typeof el.className === 'string' ? el.className : '',
                    text: text.slice(0, 160),
                    editable: !!el.isContentEditable,
                    disabled: !!el.disabled,
                    ariaDisabled: el.getAttribute && (el.getAttribute('aria-disabled') || '') || '',
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                    rect: rectInfo(el),
                    parentTag: parent ? (parent.tagName || '').toLowerCase() : '',
                    parentClass: parent && typeof parent.className === 'string' ? parent.className : '',
                    grandParentTag: grandParent ? (grandParent.tagName || '').toLowerCase() : '',
                    grandParentClass: grandParent && typeof grandParent.className === 'string' ? grandParent.className : '',
                };
            };
            const uniquePush = (items, el, maxCount = 20) => {
                if (!el || items.length >= maxCount) return;
                const info = nodeInfo(el);
                if (!info) return;
                const key = [info.tag, info.dataE2e, info.placeholder, info.className, info.text].join('|');
                if (!items.some(item => [item.tag, item.dataE2e, item.placeholder, item.className, item.text].join('|') === key)) {
                    items.push(info);
                }
            };
            const uniqueTextPush = (items, text, maxCount = 20) => {
                const normalized = normalizeText(text);
                if (!normalized || items.length >= maxCount) return;
                if (!items.includes(normalized)) {
                    items.push(normalized);
                }
            };
            const conversationItemSelectors = [
                '[data-e2e="conversation-item"]',
                '.chat-list-item',
                '[class*="conversationItem"]',
                '[class*="chat-item"]'
            ];
            const collectConversationItems = (root = document) => {
                const seen = new Set();
                const items = [];
                conversationItemSelectors.forEach((selector) => {
                    root.querySelectorAll(selector).forEach((el) => {
                        if (!seen.has(el)) {
                            seen.add(el);
                            items.push(el);
                        }
                    });
                });
                return items;
            };
            const extractConversationName = (el) => normalizeText(
                (el && (
                    (el.querySelector('.conversationConversationItemtitle') || null) ||
                    (el.querySelector('[class*="title"]') || null) ||
                    (el.querySelector('[class*="nickname"]') || null) ||
                    (el.querySelector('[class*="name"]') || null)
                )?.textContent) || (el && el.textContent) || ''
            );
            const isConversationItemActive = (el) => {
                if (!el || !el.getAttribute) return false;
                const ariaSelected = String(el.getAttribute('aria-selected') || '').toLowerCase() === 'true';
                const dataSelected = String(el.getAttribute('data-selected') || '').toLowerCase() === 'true';
                const cls = String(el.className || '').toLowerCase();
                return ariaSelected || dataSelected || cls.includes('curconversation') || cls.includes('active') || cls.includes('selected');
            };
            const findConversationListRoot = () => {
                const knownSelectors = [
                    '[class*="conversationList"]',
                    '[class*="conversation-list"]',
                    '[class*="chatList"]',
                    '[class*="chat-list"]',
                    '[class*="messageList"]',
                    '[class*="sidebar"]'
                ];
                for (const selector of knownSelectors) {
                    const candidate = document.querySelector(selector);
                    if (candidate && collectConversationItems(candidate).length) {
                        return candidate;
                    }
                }
                const firstItem = collectConversationItems(document)[0] || null;
                let parent = firstItem ? firstItem.parentElement : null;
                while (parent) {
                    if (collectConversationItems(parent).length >= 1) {
                        return parent;
                    }
                    parent = parent.parentElement;
                }
                return document;
            };
            const rightPanelRoot =
                document.querySelector('[class*="componentsRightPanelwrapper"]') ||
                document.querySelector('[class*="RightPanel"]') ||
                document.querySelector('[class*="rightPanel"]') ||
                document.body;
            const conversationListRoot = findConversationListRoot();
            const conversationItems = collectConversationItems(conversationListRoot);
            const messageListRoot =
                rightPanelRoot.querySelector('[class*="messageMessageListwrapper"]') ||
                rightPanelRoot.querySelector('[class*="messageMessageListlist"]') ||
                rightPanelRoot.querySelector('[class*="messageList"]') ||
                rightPanelRoot.querySelector('[class*="msgList"]') ||
                rightPanelRoot.querySelector('[class*="chatContent"]') ||
                rightPanelRoot;
            [messageListRoot, messageListRoot && messageListRoot.parentElement, rightPanelRoot].filter(Boolean).forEach((el) => {
                try {
                    if (el.scrollHeight > el.clientHeight + 20) {
                        el.scrollTop = el.scrollHeight;
                    }
                } catch (e) {}
            });
            const dataE2eNodes = [];
            document.querySelectorAll('[data-e2e]').forEach((el) => uniquePush(dataE2eNodes, el, 80));
            const messageBubbleNodes = [];
            const messageBoxNodes = Array.from(
                (messageListRoot || rightPanelRoot || document).querySelectorAll('[class*="messageMessageBoxmessageBox"]')
            );
            if (messageBoxNodes.length) {
                messageBoxNodes.forEach((el) => uniquePush(messageBubbleNodes, el, 80));
            } else {
                (messageListRoot || rightPanelRoot || document)
                    .querySelectorAll('[data-e2e="msg-item-content"]')
                    .forEach((el) => uniquePush(messageBubbleNodes, el, 80));
            }
            const latestMessageBox = messageBoxNodes.length ? messageBoxNodes[messageBoxNodes.length - 1] : null;
            const latestOutboundBox = [...messageBoxNodes].reverse().find((el) => {
                const contentEl = el.querySelector('[class*="messageMessageBoxcontentBox"]') || el;
                const classText = [
                    el.className || '',
                    contentEl && typeof contentEl.className === 'string' ? contentEl.className : '',
                    contentEl && contentEl.closest && contentEl.closest('[class*="isFromMe"]') ? 'isFromMe' : '',
                ].join(' ');
                return classText.toLowerCase().includes('isfromme');
            }) || null;
            const latestInboundBox = [...messageBoxNodes].reverse().find((el) => {
                const contentEl = el.querySelector('[class*="messageMessageBoxcontentBox"]') || el;
                const classText = [
                    el.className || '',
                    contentEl && typeof contentEl.className === 'string' ? contentEl.className : '',
                    contentEl && contentEl.closest && contentEl.closest('[class*="isFromMe"]') ? 'isFromMe' : '',
                ].join(' ');
                return !classText.toLowerCase().includes('isfromme');
            }) || null;

            const inputCandidates = [];
            [
                '[data-e2e*="input"]',
                '[data-e2e*="editor"]',
                '[data-e2e*="chat-input"]',
                'textarea',
                '[contenteditable="true"]',
                '[role="textbox"]'
            ].forEach((selector) => {
                document.querySelectorAll(selector).forEach((el) => uniquePush(inputCandidates, el, 30));
            });

            const sendButtons = [];
            [
                '[data-e2e="send-message-btn"]',
                'button[data-e2e="send-message-btn"]',
                'button',
                '[role="button"]'
            ].forEach((selector) => {
                document.querySelectorAll(selector).forEach((el) => {
                    const text = normalizeText(el.innerText || el.textContent || '');
                    const e2e = el.getAttribute && (el.getAttribute('data-e2e') || '') || '';
                    const cls = typeof el.className === 'string' ? el.className : '';
                    if (e2e.includes('send') || text.includes('发送') || cls.includes('send')) {
                        uniquePush(sendButtons, el, 20);
                    }
                });
            });

            const messageTail = [];
            [
                '[data-e2e*="message"]',
                '[class*="message"]',
                '[class*="msg"]'
            ].forEach((selector) => {
                document.querySelectorAll(selector).forEach((el) => {
                    const text = normalizeText(el.innerText || el.textContent || '');
                    if (text) uniquePush(messageTail, el, 30);
                });
            });

            const leftConversationNames = [];
            conversationItems.forEach((el) => {
                uniqueTextPush(leftConversationNames, extractConversationName(el), 20);
            });
            const rightPanelTitles = [];
            [
                headerRoot,
                headerRoot && headerRoot.querySelector('[class*="nickname"]'),
                headerRoot && headerRoot.querySelector('[class*="title"]'),
                headerRoot && headerRoot.querySelector('[class*="name"]'),
                headerRoot && headerRoot.querySelector('h1'),
                headerRoot && headerRoot.querySelector('h2')
            ].filter(Boolean).forEach((el) => {
                uniqueTextPush(rightPanelTitles, el.textContent || '', 10);
            });
            const activeConversation = [];
            const activeConversationSources = [];
            conversationItems.forEach((el) => {
                if (!isConversationItemActive(el)) {
                    return;
                }
                const info = nodeInfo(el);
                if (!info) {
                    return;
                }
                const name = extractConversationName(el);
                if (name) {
                    info.text = name.slice(0, 160);
                }
                activeConversation.push(info);
                if (activeConversation.length <= 12) {
                    activeConversationSources.push({
                        source: 'left_conversation_item',
                        text: name || info.text || '',
                        className: info.className || '',
                        dataE2e: info.dataE2e || '',
                    });
                }
            });

            const activeElementNode = document.activeElement || null;
            const activeElement = activeElementNode ? nodeInfo(activeElementNode) : null;
            const primaryInputNode =
                document.querySelector('[data-e2e="msg-input"] [contenteditable="true"]') ||
                document.querySelector('[data-e2e="msg-input"] [role="textbox"]') ||
                document.querySelector('[data-e2e="msg-input"] textarea') ||
                null;
            return {
                success: true,
                stage: payload.stage || '',
                customerName: payload.customerName || '',
                contentPreview: payload.contentPreview || '',
                attempt: payload.attempt || 0,
                capturedAt: new Date().toISOString(),
                location: {
                    href: location.href,
                    title: document.title,
                    readyState: document.readyState,
                },
                activeElement,
                activeElementDebug: editorDebug(activeElementNode),
                primaryInputDebug: editorDebug(primaryInputNode),
                latestMessageBubbleDebug: messageDebug(latestMessageBox),
                latestOutboundBubbleDebug: messageDebug(latestOutboundBox),
                latestInboundBubbleDebug: messageDebug(latestInboundBox),
                dataE2eNodes: limit(dataE2eNodes, 80),
                messageBubbleNodes: limit(messageBubbleNodes, 80),
                inputCandidates: limit(inputCandidates, 30),
                sendButtons: limit(sendButtons, 20),
                conversationListRoot: nodeInfo(conversationListRoot),
                leftConversationNames: limit(leftConversationNames, 20),
                rightPanelTitles: limit(rightPanelTitles, 10),
                activeConversation: limit(activeConversation, 12),
                activeConversationSources: limit(activeConversationSources, 12),
                messageTail: limit(messageTail, 20),
                bodyTextPreview: normalizeText((document.body && document.body.innerText) || '').slice(0, 500),
            };
        }
        """
        snapshot = self._safe_evaluate_with_args(snapshot_js, payload, timeout_ms=5000)
        # [REFACTOR-INST:convergence] 统一埋点
        import time as _t
        if isinstance(snapshot, dict):
            debug_event(
                "DBG-SNAP", "exit",
                value_type="dict",
                success=snapshot.get("success", True),
                reason=snapshot.get("reason", ""),
                keys_count=len(snapshot),
                ts=_t.time(),
            )
        else:
            debug_event(
                "DBG-SNAP", "exit",
                value_type=type(snapshot).__name__,
                value=str(snapshot)[:200],
                ts=_t.time(),
            )
        if isinstance(snapshot, dict):
            return snapshot
        return {
            "success": False,
            "reason": "snapshot_failed",
            "stage": stage,
            "customer_name": customer_name,
            "attempt": attempt,
        }

    def _dump_chat_dom_snapshot(
        self,
        *,
        stage: str,
        customer_name: str = "",
        content: str = "",
        attempt: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        try:
            snapshot = self._collect_chat_dom_snapshot(
                customer_name=customer_name,
                content=content,
                stage=stage,
                attempt=attempt,
            )
            if extra:
                snapshot["extra"] = extra
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            customer_token = self._sanitize_dom_diag_token(customer_name, "unknown")
            stage_token = self._sanitize_dom_diag_token(stage, "snapshot")
            file_path = self._get_dom_diag_dir() / f"douyin_chat_{stamp}_{customer_token}_{stage_token}.json"
            file_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            # [REFACTOR-INST:convergence] 文件名后缀：基于 snapshot 真实 success 自动追加 _SNAP_FAIL
            # 解决"send_success.json 但内容是 snapshot_failed"的命名误导
            try:
                snap_ok = bool(snapshot.get("success", False))
                if not snap_ok and "_SNAP_FAIL" not in file_path.name:
                    new_path = file_path.with_name(file_path.stem + "_SNAP_FAIL" + file_path.suffix)
                    new_path.write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    file_path.unlink(missing_ok=True)
                    file_path = new_path
            except Exception as rename_exc:
                logger.debug(f"追加 _SNAP_FAIL 后缀失败: {rename_exc}")
            logger.info(f"聊天页DOM诊断已导出: {file_path}")
            return file_path
        except Exception as e:
            logger.warning(f"导出聊天页DOM诊断失败(stage={stage}, customer={customer_name}): {e}")
            return None

    def _snapshot_text_contains(self, items: Any, needle: str) -> bool:
        matched, _ = self._match_target_conversation_candidates(items, needle)
        return matched

    def _format_conversation_debug_names(self, items: Any, *, limit: int = 8) -> List[str]:
        names: List[str] = []
        for item in items or []:
            for candidate in self._extract_conversation_name_candidates((item or {}).get("text") or ""):
                if candidate and candidate not in names:
                    names.append(candidate)
                if len(names) >= limit:
                    return names
        return names[:limit]

    def _extract_conversation_name_candidates(self, raw_text: Any) -> List[str]:
        text = re.sub(r"[\u200b-\u200f\ufeff]+", "", str(raw_text or "")).strip()
        if not text:
            return []

        candidates: List[str] = []
        marker_trimmed = False

        def _push(value: Any) -> None:
            normalized = self._normalize_customer_name(str(value or ""))
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        marker_pattern = re.compile(
            r"^(.*?)(?:\s+|)(刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2})(?:\s+.*)?$"
        )
        marker_match = marker_pattern.match(text)
        if marker_match:
            _push(marker_match.group(1))
            marker_trimmed = True

        parts = re.split(
            r"\s+(?:刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2})\s+",
            text,
            maxsplit=1,
        )
        if parts:
            _push(parts[0])
            if len(parts) > 1:
                marker_trimmed = True

        for line in re.split(r"[\r\n]+", text):
            if marker_trimmed and str(line).strip() == text:
                continue
            _push(line)
        if not marker_trimmed:
            _push(text)

        return candidates

    def _match_target_conversation_candidates(self, items: Any, needle: str) -> Tuple[bool, List[str]]:
        target = self._normalize_customer_name(needle).lower()
        if not target:
            return False, []

        candidates: List[str] = []
        for item in items or []:
            for candidate in self._extract_conversation_name_candidates((item or {}).get("text") or ""):
                lowered = candidate.lower()
                if lowered and lowered not in candidates:
                    candidates.append(lowered)

        for candidate in candidates:
            if candidate == target:
                return True, candidates
            min_len = min(len(candidate), len(target))
            max_len = max(len(candidate), len(target))
            if min_len >= 2 and (candidate in target or target in candidate) and min_len >= max_len * 0.92:
                return True, candidates
        return False, candidates

    def _check_for_captcha(self) -> bool:
        """检查是否存在风控验证码弹窗"""
        try:
            check_js = """
            () => {
                const textContent = document.body ? document.body.innerText || '' : '';
                // 抖音常见验证码/滑块特征词
                const captchaKeywords = [
                    "完成验证后继续",
                    "人机验证",
                    "请拖动滑块",
                    "验证码错误",
                    "点选依次",
                    "安全验证",
                    "访问异常"
                ];
                
                // 检查是否有蒙层或弹窗
                const hasOverlay = !!document.querySelector('[class*="mask"], [class*="overlay"], [class*="dialog"], [class*="captcha"]');
                
                for (const keyword of captchaKeywords) {
                    if (textContent.includes(keyword) && hasOverlay) {
                        return true;
                    }
                }
                return false;
            }
            """
            return bool(self._safe_evaluate(check_js, timeout_ms=3000))
        except Exception:
            return False

    def _classify_send_failure_from_dom(
        self,
        *,
        snapshot: Optional[Dict[str, Any]],
        error: str = "",
        customer_name: str = "",
    ) -> Dict[str, Any]:
        snapshot = snapshot or {}
        location = snapshot.get("location") or {}
        href = str(location.get("href") or "")
        input_candidates = snapshot.get("inputCandidates") or []
        send_buttons = snapshot.get("sendButtons") or []
        active_conversation = snapshot.get("activeConversation") or []
        left_conversation_names = snapshot.get("leftConversationNames") or []
        right_panel_titles = snapshot.get("rightPanelTitles") or []
        active_sources = snapshot.get("activeConversationSources") or []
        active_element = snapshot.get("activeElement") or {}
        error_text = str(error or "")
        active_matched, active_candidates = self._match_target_conversation_candidates(
            active_conversation,
            customer_name,
        )

        failure_class = "unknown"
        repair_actions: List[str] = []

        if "/chat" not in href:
            failure_class = "page_not_chat"
            repair_actions = ["ensure_chat_page", "recover_page"]
        elif customer_name and not active_matched:
            failure_class = "wrong_conversation"
            repair_actions = ["ensure_target_conversation"]
        elif not input_candidates or "未找到输入框" in error_text:
            failure_class = "input_missing"
            repair_actions = ["ensure_chat_page", "ensure_target_conversation", "recover_page"]
        elif input_candidates and not any(bool((item or {}).get("editable")) for item in input_candidates):
            failure_class = "editor_missing"
            repair_actions = ["focus_input", "wait_dom_settle"]
        elif active_element and not bool(active_element.get("editable")) and "输入框内容校验失败" in error_text:
            failure_class = "editor_not_focused"
            repair_actions = ["focus_input", "wait_dom_settle"]
        elif "发送验证失败" in error_text and send_buttons:
            failure_class = "message_not_committed"
            repair_actions = ["click_send_button", "wait_dom_settle"]
        elif "发送验证失败" in error_text:
            failure_class = "send_button_not_ready"
            repair_actions = ["focus_input", "wait_dom_settle"]
        elif send_buttons:
            failure_class = "send_button_not_ready"
            repair_actions = ["click_send_button", "wait_dom_settle"]

        return {
            "failure_class": failure_class,
            "repair_actions": repair_actions,
            "href": href,
            "target_customer": self._normalize_customer_name(customer_name),
            "active_candidates": active_candidates,
            "left_conversation_names": [
                self._normalize_customer_name(name)
                for name in left_conversation_names
                if self._normalize_customer_name(name)
            ],
            "right_panel_titles": self._format_conversation_debug_names(
                [{"text": title} for title in right_panel_titles]
            ),
            "active_sources": [
                {
                    "source": str(item.get("source") or "").strip(),
                    "text": self._normalize_customer_name(str(item.get("text") or "").strip()),
                }
                for item in active_sources
            ],
            "active_element_text": self._normalize_preview_message_reference(active_element.get("text", "")),
        }

    def _apply_send_self_heal(
        self,
        *,
        diagnosis: Dict[str, Any],
        customer_name: str = "",
        content: str = "",
    ) -> bool:
        actions = list(diagnosis.get("repair_actions") or [])
        if not actions:
            return False

        repaired = False
        for action in actions:
            try:
                if action == "ensure_chat_page":
                    repaired = self._ensure_chat_page() or repaired
                elif action == "recover_page":
                    self._recover_page()
                    repaired = self._ensure_chat_page() or repaired
                elif action == "ensure_target_conversation" and customer_name:
                    repaired = self._prepare_send_target_now(customer_name) or repaired
                elif action == "focus_input":
                    input_box = self._find_input_box()
                    if input_box:
                        repaired = self._focus_input_box(input_box) or repaired
                elif action == "click_send_button":
                    repaired = self._click_send_button() or repaired
                elif action == "wait_dom_settle":
                    self.page.wait_for_timeout(250)
                    repaired = True or repaired
            except Exception as heal_error:
                logger.debug(f"自愈动作执行失败({action}): {heal_error}")

        if repaired:
            self._dump_chat_dom_snapshot(
                stage="self_heal_applied",
                customer_name=customer_name,
                content=content,
                extra={"diagnosis": diagnosis},
            )
        return repaired

    @property
    def is_monitoring(self):
        """获取消息回复状态"""
        with self._state_lock:
            return self._monitoring

    def register_message_callback(self, callback: Optional[Callable[[List[RPAMessage]], None]] = None):
        """注册消息回调（线程安全）"""
        with self._state_lock:
            self._message_callback = callback

    def register_state_callback(self, callback: Callable[[PageState, str], None]):
        """注册状态回调（线程安全）"""
        with self._state_lock:
            self._state_callback = callback

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
    def restore_persisted_state(self, state: Dict[str, Any]):
        """从持久化存储恢复RPA引擎状态

        Args:
            state: 持久化状态字典，包含 states, sent_cache, processed_msg_ids
        """
        try:
            if 'states' in state:
                with self._state_lock:
                    self._states.update(state['states'])
                logger.info(f"恢复会话状态机: {len(state['states'])}个会话")

            if 'sent_cache' in state:
                with self._state_lock:
                    self._sent_cache.update(state['sent_cache'])

            if 'processed_msg_ids' in state:
                with self._state_lock:
                    self._processed_msg_ids.update(state['processed_msg_ids'])
                logger.info(f"恢复已处理消息ID: {len(state['processed_msg_ids'])}条")

            logger.info("RPA引擎持久化状态恢复完成")
        except Exception as e:
            logger.error(f"恢复RPA引擎持久化状态失败: {e}")

    def start_monitoring(self):
        """启动消息回复（带启动中状态保护，防止并发start或start/stop交错）"""
        with self._state_lock:
            if self._monitoring:
                logger.warning("RPA引擎消息回复已在运行中")
                return
            if getattr(self, '_starting', False):
                logger.warning("RPA引擎正在启动中，请勿重复调用")
                return
            self._starting = True

        try:
            chat_ready = self._prepare_chat_page()
            if not chat_ready:
                raise RuntimeError("无法进入聊天页面")
            self._setup_mutation_observer()
            self.resolve_my_user_id()
            self._init_network_detector()
            
            with self._state_lock:
                restored_state_exists = bool(self._states)
                self._monitoring = True
                self._starting = False
                self._recovery_attempt_count = 0
                self._recovery_notified = False
                if not restored_state_exists:
                    self._first_fetch_done = False
                else:
                    # 已恢复历史会话状态时，应把其视作启动基线，避免重启/手动停启后
                    # 又把当前活动会话按“首次加载新消息”直接放行进回复主链。
                    self._first_fetch_done = True
                if restored_state_exists:
                    logger.info(
                        f"启动消息回复时保留已恢复会话状态: states={len(self._states)}, "
                        f"processed_msg_ids={len(self._processed_msg_ids)}, "
                        f"first_fetch_done={self._first_fetch_done}"
                    )
            if self._boundary_guard:
                self._boundary_guard.reset()
            
            logger.info("RPA引擎消息回复已启动")
        except Exception as e:
            with self._state_lock:
                self._monitoring = False
                self._starting = False
                self._chat_page_ensured = False
            logger.error(f"RPA引擎启动失败: {e}")
            raise

    def stop_monitoring(self):
        """停止消息回复"""
        self._ensure_queue_runtime_state()
        with self._state_lock:
            self._monitoring = False
            self._first_fetch_done = False
            self._observer_active = False

        network_detector = getattr(self, '_network_detector', None)
        if network_detector:
            try:
                network_detector.disable()
            except Exception:
                pass
            
        # 清空发送队列，防止停止后还有残留消息被发送
        with self._queue_processing_lock:
            while not self._send_queue.empty():
                try:
                    task_id, content, customer_name, assume_target_ready, identity_level, result_event = self._send_queue.get_nowait()
                    with self._send_results_lock:
                        self._send_results[task_id] = OperationResult(
                            success=False, mode=OperationMode.SMART, error="RPA引擎已停止"
                        )
                    if result_event:
                        result_event.set()
                except Exception:
                    break
            while not self._prepare_queue.empty():
                try:
                    (
                        task_id,
                        customer_name,
                        conversation_id,
                        customer_id,
                        identity_level,
                        target_candidates,
                        result_event,
                    ) = self._prepare_queue.get_nowait()
                    with self._prepare_results_lock:
                        self._prepare_results[task_id] = False
                    if result_event:
                        result_event.set()
                except Exception:
                    break
                    
        logger.info("RPA引擎消息回复已停止，发送/预处理队列已清空")

    def _prepare_chat_page(self) -> bool:
        """准备聊天页面"""
        try:
            if not self.page or self.page.is_closed():
                self._chat_page_ensured = False
                logger.error("准备聊天页面失败: 页面对象不可用")
                return False

            current_url = self.page.url or ""
            if "/chat" in current_url and "douyin.com" in current_url:
                self._chat_page_ensured = True
                logger.info(f"当前页面已是聊天页面: {current_url}")
                return True

            logger.info(f"导航到聊天页面，当前URL: {current_url}...")
            self.page.goto(DOUYIN_CHAT_URL, timeout=30000)
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            try:
                self.page.wait_for_selector('[data-e2e="conversation-item"], [class*="conversationItem"]', timeout=5000)
            except Exception:
                self.page.wait_for_timeout(2000)

            current_url = self.page.url or ""
            if "/chat" not in current_url or "douyin.com" not in current_url:
                self._chat_page_ensured = False
                logger.error(f"导航后仍未进入聊天页面，当前URL: {current_url}")
                return False

            self._chat_page_ensured = True
            logger.info("已导航到聊天页面")
            return True
        except Exception as e:
            self._chat_page_ensured = False
            logger.error(f"准备聊天页面失败: {e}")
            return False

    def _setup_mutation_observer(self):
        """设置MutationObserver监听DOM变化（首选方案）"""
        try:
            observer_js = """
            () => {
                if (window.__douyinObserver) {
                    window.__douyinObserver.disconnect();
                }
                window.__douyinNewMessages = [];
                
                function simpleHash(str) {
                    var hash = 0;
                    for (var i = 0; i < str.length; i++) {
                        var char = str.charCodeAt(i);
                        hash = ((hash << 5) - hash) + char;
                        hash = hash & hash;
                    }
                    return Math.abs(hash).toString(36);
                }
                
                const selectors = [
                    '[data-e2e="conversation-item"]',
                    '.chat-list-item',
                    '[class*="conversationItem"]',
                    '[class*="chat-item"]',
                    '[class*="conversation"]',
                    '[class*="session"]'
                ];
                
                let container = null;
                for (const selector of selectors) {
                    const found = document.querySelector(selector);
                    if (found && found.parentElement) {
                        container = found.parentElement;
                        break;
                    }
                }
                
                if (!container) {
                    container = document.querySelector('[class*="chat-list"]') ||
                               document.querySelector('[class*="conversation-list"]') ||
                               document.body;
                }
                
                const observer = new MutationObserver((mutations) => {
                    for (const mutation of mutations) {
                        if (mutation.type === 'childList') {
                            for (const node of mutation.addedNodes) {
                                if (node.nodeType === 1) {
                                    const nameEl = node.querySelector('.conversationConversationItemtitle') ||
                                                   node.querySelector('[class*="title"]') ||
                                                   node.querySelector('[class*="name"]');
                                    const customerName = nameEl ? nameEl.textContent.trim() : '';
                                    
                                    const unreadEl = node.querySelector('[class*="commonStreak"]') ||
                                                     node.querySelector('[class*="unread"]') ||
                                                     node.querySelector('[class*="badge"]') ||
                                                     node.querySelector('[class*="dot"]') ||
                                                     node.querySelector('[class*="msgCount"]');
                                    const unreadText = unreadEl ? unreadEl.textContent.trim() : '0';
                                    const unreadCount = parseInt(unreadText) || (unreadEl && unreadEl.offsetWidth > 0 ? 1 : 0);
                                    
                                    if (customerName) {
                                        const fullText = node.innerText || '';
                                        const lines = fullText.split('\\n').filter(l => l.trim());
                                        let lastMessage = '';
                                        
                                        for (const line of lines) {
                                            const trimmed = line.trim();
                                            if (trimmed === customerName) continue;
                                            if (/^\\d{1,2}:\\d{2}$/.test(trimmed)) continue;
                                            if (/^\\d{4}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                                            if (/^\\d{1,2}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                                            if (/^(昨天|刚刚|\\d+分钟前|\\d+小时前|前天|今天|星期[一二三四五六日天])$/.test(trimmed)) continue;
                                            if (/^\\d+$/.test(trimmed) && trimmed.length < 3) continue;
                                            if (trimmed.length > 0) {
                                                lastMessage = trimmed;
                                                break;
                                            }
                                        }
                                        
                                        if (lastMessage) {
                                            const selfMsgEl = node.querySelector('[class*="self-message"], [class*="msg-self"], [class*="outbound"], [class*="message-out"], [class*="chat-message-self"]');
                                            const isSelfByClass = !!selfMsgEl;
                                            const isSelfByPrefix = lastMessage.startsWith('我:') || lastMessage.startsWith('我：') || lastMessage.startsWith('[自动回复]');
                                            const hasUnread = unreadCount > 0;
                                            let direction = 'unknown';
                                            let directionConfidence = 'low';
                                            if (hasUnread) {
                                                direction = 'inbound';
                                                directionConfidence = 'medium';
                                            } else if (isSelfByClass || isSelfByPrefix) {
                                                direction = 'outbound';
                                                directionConfidence = 'high';
                                            }
                                            const msgId = 'src_' + customerName + '_' + simpleHash(lastMessage);
                                            const existingChild = window.__douyinNewMessages.find(m => m.customer_name === customerName);
                                            if (!existingChild || existingChild.content !== lastMessage) {
                                                window.__douyinNewMessages.push({
                                                    msg_id: msgId,
                                                    customer_name: customerName,
                                                    content: lastMessage,
                                                    direction: direction,
                                                    direction_confidence: directionConfidence,
                                                    unread_count: unreadCount,
                                                    create_time: new Date().toISOString()
                                                });
                                            }
                                        }
                                    }
                                }
                            }
                        } else if (mutation.type === 'characterData' || mutation.type === 'attributes') {
                            let target = mutation.target;
                            if (target.nodeType === 3) target = target.parentElement;
                            if (!target || target.nodeType !== 1) continue;
                            
                            const item = target.closest('[data-e2e="conversation-item"]') ||
                                         target.closest('.chat-list-item') ||
                                         target.closest('[class*="conversationItem"]') ||
                                         target.closest('[class*="chat-item"]');
                            if (!item) continue;
                            
                            const unreadEl = item.querySelector('[class*="commonStreak"]') ||
                                             item.querySelector('[class*="unread"]') ||
                                             item.querySelector('[class*="badge"]') ||
                                             item.querySelector('[class*="dot"]') ||
                                             item.querySelector('[class*="msgCount"]');
                            if (!unreadEl) continue;
                            
                            const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                                           item.querySelector('[class*="title"]') ||
                                           item.querySelector('[class*="name"]');
                            const customerName = nameEl ? nameEl.textContent.trim() : '';
                            if (!customerName) continue;
                            
                            const fullText = item.innerText || '';
                            const lines = fullText.split('\\n').filter(l => l.trim());
                            let lastMessage = '';
                            for (const line of lines) {
                                const trimmed = line.trim();
                                if (trimmed === customerName) continue;
                                if (/^\\d{1,2}:\\d{2}$/.test(trimmed)) continue;
                                if (/^\\d{4}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                                if (/^\\d{1,2}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                                if (/^(昨天|刚刚|\\d+分钟前|\\d+小时前|前天|今天|星期[一二三四五六日天])$/.test(trimmed)) continue;
                                if (/^\\d+$/.test(trimmed) && trimmed.length < 3) continue;
                                if (trimmed.length > 0) { lastMessage = trimmed; break; }
                            }
                            
                            if (lastMessage) {
                                const selfMsgEl = item.querySelector('[class*="self-message"], [class*="msg-self"], [class*="outbound"], [class*="message-out"], [class*="chat-message-self"]');
                                const isSelfByClass = !!selfMsgEl;
                                const isSelfByPrefix = lastMessage.startsWith('我:') || lastMessage.startsWith('我：') || lastMessage.startsWith('[自动回复]');
                                const hasUnreadAttr = unreadEl && (parseInt(unreadEl.textContent.trim()) > 0 || unreadEl.offsetWidth > 0);
                                let direction = 'unknown';
                                let directionConfidence = 'low';
                                if (hasUnreadAttr) {
                                    direction = 'inbound';
                                    directionConfidence = 'medium';
                                } else if (isSelfByClass || isSelfByPrefix) {
                                    direction = 'outbound';
                                    directionConfidence = 'high';
                                }
                                const msgId = 'src_attr_' + customerName + '_' + simpleHash(lastMessage);
                                const existing = window.__douyinNewMessages.find(m => m.customer_name === customerName);
                                if (existing) {
                                    if (existing.content !== lastMessage) {
                                        existing.content = lastMessage;
                                        existing.direction = direction;
                                        existing.direction_confidence = directionConfidence;
                                        existing.unread_count = unreadCount;
                                        existing.msg_id = msgId;
                                    }
                                } else {
                                    window.__douyinNewMessages.push({
                                        msg_id: msgId,
                                        customer_name: customerName,
                                        content: lastMessage,
                                        direction: direction,
                                        direction_confidence: directionConfidence,
                                        unread_count: unreadCount,
                                        create_time: new Date().toISOString()
                                    });
                                }
                            }
                        }
                    }
                });
                
                observer.observe(container, { childList: true, subtree: true, characterData: true, attributes: true });
                window.__douyinObserver = observer;
                return { success: true };
            }
            """
            
            result = self._safe_evaluate(observer_js, timeout_ms=10000)
            self._observer_active = result.get('success', False) if result else False
            logger.info(f"MutationObserver 设置完成: {result}")
            
        except Exception as e:
            logger.warning(f"MutationObserver 设置失败: {e}")
            self._observer_active = False

    def _check_login(self):
        """检查登录状态"""
        current_time = time.time()
        if current_time - self._last_login_check < self.LOGIN_CHECK_INTERVAL:
            return

        self._last_login_check = current_time

        try:
            if self.page and not self.page.is_closed():
                current_url = self.page.url or ""
                if "douyin.com" not in current_url:
                    if self._login_valid:
                        logger.warning("页面不在抖音域名内")
                        self._login_valid = False
                        self._update_page_state(PageState.LOGGED_OUT)
                    return

                is_chat_url = "/chat" in current_url
                login_modal_visible = False
                login_button_visible = False
                chat_entry_visible = False

                try:
                    login_btn = self.page.query_selector("header button:has-text('登录')")
                    login_button_visible = bool(login_btn and login_btn.is_visible())
                except Exception:
                    login_button_visible = False

                try:
                    modal = self.page.query_selector(".dy-account-close, text='登录后查看'")
                    login_modal_visible = bool(modal and modal.is_visible())
                except Exception:
                    login_modal_visible = False

                if is_chat_url:
                    chat_selectors = list(SELECTOR_POOL.get("conversation_item", [])) + [
                        '[class*="message-list"]',
                        '[class*="chat-messages"]',
                        '[aria-selected="true"][data-e2e="conversation-item"]',
                        '[class*="curConversation"]',
                    ]
                    for selector in chat_selectors:
                        try:
                            el = self.page.query_selector(selector)
                            if el and el.is_visible():
                                chat_entry_visible = True
                                break
                        except Exception:
                            continue

                    # 兜底：部分聊天页左侧列表会延迟渲染，但右侧输入框/发送区已可用。
                    if not chat_entry_visible:
                        for selector in list(SELECTOR_POOL.get("input_box", [])) + list(self.SEND_BUTTON_SELECTORS):
                            try:
                                el = self.page.query_selector(selector)
                                if el and el.is_visible():
                                    chat_entry_visible = True
                                    break
                            except Exception:
                                continue

                login_valid = is_chat_url and chat_entry_visible and not login_button_visible and not login_modal_visible
                if login_valid != self._login_valid:
                    self._login_valid = login_valid
                    if login_valid:
                        logger.info("聊天页与会话列表可见，登录状态有效")
                    else:
                        logger.warning(
                            "登录状态失效: "
                            f"url={current_url[:80]}, is_chat_url={is_chat_url}, "
                            f"chat_entry_visible={chat_entry_visible}, login_button_visible={login_button_visible}, "
                            f"login_modal_visible={login_modal_visible}"
                        )
                        self._update_page_state(PageState.LOGGED_OUT)
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")

    def fetch_messages(self) -> List[RPAMessage]:
        """
        获取新消息

        方案（优先级）：
        1. 网络拦截（首选）：通过 HTTP API / WebSocket 拦截获取真实消息
        2. MutationObserver DOM监听（备选）
        3. DOM轮询（兜底）
        """
        try:
            self._ensure_queue_runtime_state()
            if self._poll_count % 60 == 0:
                if hasattr(self, '_browser') and self._browser:
                    try:
                        if not self._browser.is_connected():
                            logger.warning("浏览器会话已断开，通知上层浏览器异常")
                            if self._state_callback:
                                self._state_callback(PageState.EXCEPTION)
                            return []
                    except Exception:
                        pass

            self._process_prepare_queue()

            chat_ok = self._ensure_chat_page()
            if not chat_ok:
                logger.warning("fetch_messages: _ensure_chat_page返回False，跳过本次轮询")
                return []

            current_time = time.time()
            if current_time - self._last_poll_time < self.POLL_INTERVAL:
                self._process_prepare_queue()
                self._process_send_queue()
                return []
            self._last_poll_time = current_time
            self._poll_count += 1

            self._process_prepare_queue()

            if self._poll_count % 30 == 0:
                logger.info(f"RPA轮询 #{self._poll_count}: observer_active={self._observer_active}, chat_page_ensured={self._chat_page_ensured}, first_fetch_done={self._first_fetch_done}, states_count={len(self._states)}, network_detector={getattr(self, '_network_detector', None) is not None}")

            # 阶段4：灰度流量控制 - 根据配置比例决定是否使用网络拦截
            use_network = True
            try:
                from src.config.settings import NETWORK_DETECTION_TRAFFIC_RATIO
                import random
                use_network = random.random() < NETWORK_DETECTION_TRAFFIC_RATIO
            except Exception:
                pass
            
            network_messages: List[RPAMessage] = []
            if use_network:
                network_messages = self._poll_network_messages()
            
            if network_messages:
                unique_messages = self._deduplicate_messages(network_messages)
                for msg in unique_messages:
                    logger.debug(f"[Network] 收到新消息: {msg.customer_name} -> {msg.content[:30]}...")

                if unique_messages and self._message_callback:
                    try:
                        logger.debug(f"触发消息回调，消息数量: {len(unique_messages)}")
                        self._message_callback(unique_messages)
                        for msg in unique_messages:
                            self._mark_message_processed(msg)
                    except Exception as cb_e:
                        logger.error(f"消息回调执行失败: {cb_e}")
                        with self._state_lock:
                            for msg in unique_messages:
                                if msg.msg_id and msg.msg_id in self._processed_msg_ids:
                                    del self._processed_msg_ids[msg.msg_id]
                                elif not msg.msg_id:
                                    content_hash = hashlib.md5(msg.content.encode('utf-8')).hexdigest()[:16]
                                    minute_bucket = int(time.time()) // 120
                                    content_key = f"{msg.customer_name}:{minute_bucket}:{content_hash}"
                                    self._processed_msg_ids.pop(content_key, None)
                        network_detector = getattr(self, '_network_detector', None)
                        if network_detector:
                            for msg in unique_messages:
                                try:
                                    network_detector.rollback_message_seen(msg.customer_name, msg.content, msg.msg_id)
                                except Exception:
                                    pass
                elif unique_messages:
                    for msg in unique_messages:
                        self._mark_message_processed(msg)

                return unique_messages

            all_messages: List[RPAMessage] = []
            observer_msgs: List[Dict] = []

            if self._observer_active:
                observer_msgs = self._get_observer_messages()
                if observer_msgs:
                    logger.info(f"fetch_messages: Observer获取到 {len(observer_msgs)} 条原始消息")
                elif self._poll_count % 30 == 0:
                    logger.info("fetch_messages: Observer无新消息")
                for msg_data in observer_msgs:
                    msg = self._create_message(msg_data, current_time)
                    if msg and self._should_emit_observer_message(msg, current_time) and self._is_new_message(msg):
                        all_messages.append(msg)
                        logger.info(f"[Observer] 新消息: {msg.customer_name} -> {msg.content[:30]}...")
            else:
                if self._poll_count % 30 == 0:
                    logger.info("fetch_messages: Observer未激活，仅依赖DOM轮询")
                try:
                    logger.info("fetch_messages: 尝试重新注入MutationObserver...")
                    self._setup_mutation_observer()
                    if self._observer_active:
                        logger.info("fetch_messages: MutationObserver重新注入成功")
                except Exception as obs_e:
                    logger.warning(f"fetch_messages: MutationObserver重新注入失败: {obs_e}")

            should_run_dom_scan = (
                not self._first_fetch_done
                or not self._observer_active
                or self._poll_count % self.OBSERVER_DOM_REFRESH_EVERY_POLLS == 0
            )
            active_snapshot = None
            conversations: List[Dict[str, Any]] = []
            if should_run_dom_scan:
                active_snapshot = self._get_active_conversation_snapshot()
                conversations = self._fetch_conversations_from_dom()
                if len(conversations) <= 3 and self._clear_conversation_search_filter():
                    refreshed_conversations = self._fetch_conversations_from_dom()
                    if len(refreshed_conversations) > len(conversations):
                        conversations = refreshed_conversations
                        logger.info(
                            f"fetch_messages: 清理搜索过滤后恢复到 {len(conversations)} 个会话"
                        )
                if (
                    len(conversations) <= 3
                    and current_time - self._last_sparse_conversation_retry_time >= 5
                ):
                    self._last_sparse_conversation_retry_time = current_time
                    retry_conversations = self._fetch_conversations_from_dom()
                    if len(retry_conversations) > len(conversations):
                        conversations = retry_conversations
                        logger.info(
                            f"fetch_messages: 会话采集增强重试后提升到 {len(conversations)} 个会话"
                        )
            elif observer_msgs and self._poll_count % 30 == 0:
                logger.info("fetch_messages: Observer已命中新消息，当前轮跳过重型DOM会话扫描")
            if self._poll_count % 30 == 0:
                logger.info(f"fetch_messages: DOM获取到 {len(conversations) if conversations else 0} 个会话")
                if conversations:
                    try:
                        conv_summary = [
                            f"{str(conv.get('customer_name', '') or '')[:20]}(unread={int(conv.get('unread_count', 0) or 0)})"
                            for conv in conversations[:10]
                        ]
                        logger.info(f"fetch_messages: DOM会话样本={conv_summary}")
                    except Exception as conv_log_e:
                        logger.debug(f"记录DOM会话样本失败: {conv_log_e}")
                if active_snapshot:
                    snapshot_log = (
                        "fetch_messages: 活动会话快照 "
                        f"name={str(active_snapshot.get('customer_name', '') or '')[:20]} "
                        f"last_inbound={str(active_snapshot.get('last_inbound_message', '') or '')[:30]} "
                        f"last_bubble_inbound={bool(active_snapshot.get('last_bubble_is_inbound', False))} "
                        f"last_bubble={str(active_snapshot.get('last_bubble_text', '') or '')[:30]}"
                    )
                    snapshot_signature = "|".join([
                        str(active_snapshot.get('customer_name', '') or ''),
                        str(active_snapshot.get('last_inbound_message', '') or ''),
                        str(bool(active_snapshot.get('last_bubble_is_inbound', False))),
                        str(active_snapshot.get('last_bubble_text', '') or ''),
                    ])
                    if (
                        snapshot_signature != self._last_active_snapshot_log_signature
                        or current_time - self._last_active_snapshot_log_time >= 60
                    ):
                        logger.info(snapshot_log)
                        self._last_active_snapshot_log_signature = snapshot_signature
                        self._last_active_snapshot_log_time = current_time
                    else:
                        logger.debug(snapshot_log)
            if conversations:
                dom_messages = self._parse_conversations(
                    conversations,
                    current_time,
                    active_snapshot=active_snapshot,
                )
                if dom_messages:
                    logger.info(f"fetch_messages: _parse_conversations返回 {len(dom_messages)} 条新消息")
                for msg in dom_messages:
                    if self._is_new_message(msg):
                        all_messages.append(msg)

            unique_messages = self._deduplicate_messages(all_messages)

            for msg in unique_messages:
                logger.debug(f"收到新消息: {msg.customer_name} -> {msg.content[:30]}...")

            if unique_messages and self._message_callback:
                try:
                    logger.debug(f"触发消息回调，消息数量: {len(unique_messages)}")
                    self._message_callback(unique_messages)
                    for msg in unique_messages:
                        self._mark_message_processed(msg)
                except Exception as cb_e:
                    logger.error(f"消息回调执行失败: {cb_e}")
                    with self._state_lock:
                        for msg in unique_messages:
                            if msg.msg_id and msg.msg_id in self._processed_msg_ids:
                                del self._processed_msg_ids[msg.msg_id]
                            elif not msg.msg_id:
                                content_hash = hashlib.md5(msg.content.encode('utf-8')).hexdigest()[:16]
                                minute_bucket = int(time.time()) // 120
                                content_key = f"{msg.customer_name}:{minute_bucket}:{content_hash}"
                                self._processed_msg_ids.pop(content_key, None)
            elif unique_messages:
                for msg in unique_messages:
                    self._mark_message_processed(msg)

            return unique_messages

        except Exception as e:
            logger.error(f"获取消息失败: {e}")
            return []
        finally:
            self._process_send_queue()

    def _get_observer_messages(self) -> List[Dict]:
        try:
            swap_js = """
            () => {
                const messages = window.__douyinNewMessages || [];
                window.__douyinNewMessages = [];
                const prev = window.__douyinSwapBuffer || [];
                window.__douyinSwapBuffer = messages;
                return prev;
            }
            """
            messages = self._safe_evaluate(swap_js, timeout_ms=5000)
            if not messages:
                return []
            
            return messages
        except Exception as e:
            logger.debug(f"获取Observer消息失败: {e}")
            return []

    def _create_message(self, msg_data: Dict, current_time: float) -> Optional[RPAMessage]:
        """创建消息对象"""
        try:
            customer_name = msg_data.get('customer_name', '')
            content = msg_data.get('content', '')
            
            if not customer_name or not content:
                return None
            
            if not self._is_valid_message(content):
                return None
            
            direction_str = msg_data.get('direction', 'inbound')
            direction = MessageDirection.INBOUND if direction_str == 'inbound' else MessageDirection.OUTBOUND
            
            return RPAMessage(
                customer_name=customer_name,
                content=content,
                direction=direction,
                timestamp=current_time,
                conversation_id=(
                    str(msg_data.get('conversation_id', '') or '').strip()
                    or build_conversation_id(customer_name, "douyin")
                ),
                customer_id=str(msg_data.get('customer_id', '') or '').strip(),
                is_new=True,
                msg_id=msg_data.get('msg_id', ''),
                create_time=msg_data.get('create_time', ''),
                unread_count=max(int(msg_data.get('unread_count', 0) or 0), 0),
                signal_source=str(msg_data.get('signal_source', '') or ''),
                direction_confidence=str(msg_data.get('direction_confidence', 'high') or 'high'),
            )
        except Exception as e:
            logger.debug(f"创建消息对象失败: {e}")
            return None

    def _should_emit_observer_message(self, msg: RPAMessage, current_time: float) -> bool:
        """决定 Observer 捕获的原始消息是否允许进入自动回复主链。

        Observer 负责尽快感知页面变化，但其事件可能只是页面初开时的列表渲染、
        预览刷新或属性抖动。这里做保守过滤，避免把无未读的伪入站直接当成新消息。

        direction_confidence 策略:
        - high: 有未读或有明确方向信号，直接放行
        - low: 无未读且无自身发送特征时的默认inbound，需额外检查:
          * 已知会话且内容未变化 → 跳过（预览刷新/重复）
          * 已知会话且内容变化但sent_by_us=True → 跳过（我们刚发送的回复预览更新）
          * 已知会话且内容变化且sent_by_us=False → 放行（可能是客户新消息但未读已清零）
          * 新会话 → 跳过（首轮基线采集风险高）
        """
        if msg.direction != MessageDirection.INBOUND:
            return False

        unread_count = max(int(getattr(msg, "unread_count", 0) or 0), 0)
        if unread_count > 0:
            return True

        direction_confidence = getattr(msg, 'direction_confidence', 'high')

        with self._state_lock:
            state = self._states.get(msg.customer_name)
            first_fetch_done = self._first_fetch_done

        if not first_fetch_done:
            logger.info(
                f"[Observer] 首轮基线采集阶段跳过无未读入站: {msg.customer_name} -> "
                f"{msg.content[:30]}... (confidence={direction_confidence})"
            )
            return False

        if state:
            last_content = state.get('last_content', '')
            sent_by_us = state.get('sent_by_us', False)
            sent_by_us_at = float(state.get('sent_by_us_at', 0) or 0)
            # 修复：延长 sent_by_us TTL 从 300 秒到 1800 秒（30分钟）
            # 原 300 秒过期后，DOM 预览刷新即可触发重复回复；30 分钟覆盖一个完整的轮询周期
            if sent_by_us and (current_time - sent_by_us_at) > 1800:
                sent_by_us = False
            if msg.content == last_content:
                logger.debug(
                    f"[Observer] 跳过无未读重复入站: {msg.customer_name} -> "
                    f"{msg.content[:30]}... (confidence={direction_confidence})"
                )
                return False
            if direction_confidence == 'low' and not sent_by_us:
                min_change_ratio = 0.3
                max_compare = max(len(msg.content), len(last_content), 1)
                change_chars = sum(1 for a, b in zip(msg.content, last_content) if a != b)
                change_chars += abs(len(msg.content) - len(last_content))
                change_ratio = change_chars / max_compare
                if change_ratio >= min_change_ratio:
                    logger.info(
                        f"[Observer] 低置信度入站内容显著变化，放行: "
                        f"{msg.customer_name} -> {msg.content[:30]}... "
                        f"(change_ratio={change_ratio:.2f}, confidence={direction_confidence})"
                    )
                    return True
                logger.info(
                    f"[Observer] 低置信度入站内容微变，跳过: "
                    f"{msg.customer_name} -> {msg.content[:30]}... "
                    f"(change_ratio={change_ratio:.2f}, confidence={direction_confidence})"
                )
                return False
            if direction_confidence == 'high' and sent_by_us:
                logger.info(
                    f"[Observer] 高置信度入站内容变化(我们刚回复后)，放行: {msg.customer_name} -> "
                    f"{msg.content[:30]}... (sent_by_us={sent_by_us}, confidence={direction_confidence})"
                )
                return True
            logger.info(
                f"[Observer] 跳过无未读入站预览变化: {msg.customer_name} -> "
                f"{msg.content[:30]}... (sent_by_us={sent_by_us}, confidence={direction_confidence})"
            )
            return False

        logger.info(
            f"[Observer] 跳过无未读的新会话入站: {msg.customer_name} -> "
            f"{msg.content[:30]}... (confidence={direction_confidence})"
        )
        return False

    def _is_new_message(self, msg: RPAMessage) -> bool:
        """检查是否为新消息。

        RPA 采集层只做传输级 msg_id 去重，业务级幂等统一交给 BotService。
        """
        with self._state_lock:
            now = time.time()
            if msg.msg_id and msg.msg_id in self._processed_msg_ids:
                # 修复：检查时间戳是否过期，过期的条目视为未处理并清理
                ts = self._processed_msg_ids.get(msg.msg_id, 0.0)
                if now - ts > self.SESSION_CACHE_TTL:
                    del self._processed_msg_ids[msg.msg_id]
                    logger.info(
                        f"_is_new_message: msg_id={msg.msg_id} 已过期({now - ts:.0f}s>"
                        f"{self.SESSION_CACHE_TTL}s)，视为新消息"
                    )
                else:
                    return False
            # 修复 B3：msg_id 维度未命中时，用内容 hash 兜底去重，
            # 覆盖网络源（API msg_id）与 DOM 源（dom_ 指纹）生成方式不同
            # 导致的跨来源重复触发。仅检查当前时间桶，避免误杀跨桶的合法重复消息。
            content_hash = hashlib.md5(msg.content.encode('utf-8')).hexdigest()[:16]
            minute_bucket = int(time.time()) // 120
            content_key = f"{msg.customer_name}:{minute_bucket}:{content_hash}"
            if content_key in self._processed_msg_ids:
                # 内容 hash 桶键按 120 秒分桶，无需额外过期检查
                return False
            return True

    def _mark_message_processed(self, msg: RPAMessage):
        with self._state_lock:
            if msg.msg_id:
                self._processed_msg_ids[msg.msg_id] = time.time()
            # 修复 B3：同时记录内容 hash 键，使另一来源的同内容消息可被兜底去重
            content_hash = hashlib.md5(msg.content.encode('utf-8')).hexdigest()[:16]
            minute_bucket = int(time.time()) // 120
            content_key = f"{msg.customer_name}:{minute_bucket}:{content_hash}"
            self._processed_msg_ids[content_key] = time.time()
            # 修复：移除 len > 200 门槛，每次都清理过期条目，
            # 避免条目数 ≤ 200 时过期 msg_id 永久驻留导致消息无法重新处理
            now = time.time()
            expired = [k for k, v in self._processed_msg_ids.items()
                      if now - v > self.SESSION_CACHE_TTL]
            for k in expired:
                del self._processed_msg_ids[k]

    def _deduplicate_messages(self, messages: List[RPAMessage]) -> List[RPAMessage]:
        """消息去重。

        仅在单次抓取批次内做轻量去重，避免 Observer 与 DOM 同批次重复上报。
        跨轮询、跨来源的业务幂等由 BotService 和数据库负责。
        """
        import re
        seen_msg_ids = set()
        seen_batch_keys = set()
        unique = []
        for msg in messages:
            normalized = re.sub(r'\s+', '', msg.content).strip()
            content_hash = hashlib.md5(normalized.encode('utf-8')).hexdigest()[:16]
            minute_bucket = int(time.time()) // 120
            batch_key = (
                msg.customer_name,
                minute_bucket,
                content_hash,
            )
            if batch_key in seen_batch_keys:
                continue
            if msg.msg_id and msg.msg_id in seen_msg_ids:
                continue
            if msg.msg_id:
                seen_msg_ids.add(msg.msg_id)
            seen_batch_keys.add(batch_key)

            unique.append(msg)
        return unique

    def _is_dirty_unread_cooling_down(
        self,
        customer_name: str,
        preview_content: str,
        unread_count: int,
        current_time: float,
    ) -> bool:
        """判断会话是否处于脏未读冷却期。

        只有当未读数未增加且预览内容未变化时，才继续沿用冷却；否则立即重新检查。
        """
        with self._state_lock:
            state = self._states.get(customer_name)
            if not state:
                return False
            cooldown_until = float(state.get("dirty_unread_until", 0) or 0)
            if cooldown_until <= current_time:
                return False
            if int(state.get("dirty_unread_count", 0) or 0) < int(unread_count or 0):
                return False
            cached_preview = str(state.get("dirty_unread_preview", "") or "").strip()
            return cached_preview == str(preview_content or "").strip()

    def _mark_dirty_unread(
        self,
        customer_name: str,
        preview_content: str,
        unread_count: int,
        current_time: float,
    ) -> None:
        """记录脏未读状态，避免会话在短时间内被每轮重复重检。"""
        with self._state_lock:
            state = self._states.get(customer_name, {})
            previous_count = int(state.get("dirty_unread_count", 0) or 0)
            previous_preview = str(state.get("dirty_unread_preview", "") or "").strip()
            previous_hits = int(state.get("dirty_unread_hits", 0) or 0)

            if previous_count == max(int(unread_count or 0), 0) and previous_preview == str(preview_content or "").strip():
                hits = previous_hits + 1
            else:
                hits = 1

            interval = min(
                self.DIRTY_UNREAD_RECHECK_INTERVAL * max(hits, 1),
                self.DIRTY_UNREAD_MAX_RECHECK_INTERVAL,
            )
            state["dirty_unread_until"] = current_time + interval
            state["dirty_unread_count"] = max(int(unread_count or 0), 0)
            state["dirty_unread_preview"] = str(preview_content or "").strip()
            state["dirty_unread_hits"] = hits
            if "last_time" not in state:
                state["last_time"] = current_time
            self._states[customer_name] = state
        if hits >= 3:
            logger.warning(
                f"[状态机] {customer_name}: 脏未读重复命中 {hits} 次，"
                f"进入升级冷却 {interval}s (unread={unread_count})"
            )
        else:
            logger.info(
                f"[状态机] {customer_name}: 记录脏未读，第 {hits} 次，"
                f"冷却 {interval}s (unread={unread_count})"
            )

    def _clear_dirty_unread(self, state: Optional[Dict[str, Any]]) -> None:
        """清理脏未读标记。"""
        if not isinstance(state, dict):
            return
        state.pop("dirty_unread_until", None)
        state.pop("dirty_unread_count", None)
        state.pop("dirty_unread_preview", None)
        state.pop("dirty_unread_hits", None)

    def _reset_unread_tracking(self, state: Optional[Dict[str, Any]]) -> None:
        """在会话未读归零时同步清理旧未读锚点，避免后续继续按旧未读判重。"""
        if not isinstance(state, dict):
            return
        self._clear_dirty_unread(state)
        state["unread_count"] = 0
        state["last_detected_unread"] = 0
        state["dom_unread_at_send"] = 0

    def _build_stable_dom_msg_id(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        content: str,
        direction: MessageDirection,
        unread_count: int,
        bubble_signature: str = "",
    ) -> str:
        """为 DOM 轮询分支生成稳定消息指纹。

        不再使用当前时间拼接，避免同一条旧消息在后续轮询中被包装成新的 msg_id。
        """
        normalized_customer = (customer_name or "").strip().lower()
        normalized_conversation = (conversation_id or "").strip().lower()
        normalized_content = re.sub(r"\s+", "", str(content or "")).strip()
        direction_str = direction.value if hasattr(direction, "value") else str(direction)
        fingerprint = "|".join(
            [
                normalized_customer,
                normalized_conversation,
                direction_str,
                str(max(int(unread_count or 0), 0)),
                normalized_content,
                str(bubble_signature or "").strip(),
            ]
        )
        return f"dom_{hashlib.md5(fingerprint.encode('utf-8')).hexdigest()[:24]}"

    def _normalize_customer_name(self, customer_name: str) -> str:
        """清洗会话名，去掉头部/列表上混入的未读数和时间尾巴。"""
        text = re.sub(r"[\u200b-\u200f\ufeff]+", "", str(customer_name or "")).strip()
        if not text:
            return ""

        tokens = [token for token in re.split(r"\s+", text) if token]
        if len(tokens) <= 1:
            return text

        trailing_pattern = re.compile(
            r"^(刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d+)$"
        )
        while len(tokens) > 1 and trailing_pattern.match(tokens[-1]):
            tokens.pop()

        normalized = " ".join(tokens).strip()
        return normalized or text

    @staticmethod
    def _normalize_preview_message_reference(content: Any) -> str:
        text = re.sub(r"[\u200b-\u200f\ufeff]+", "", str(content or "")).strip()
        if not text:
            return ""
        # 移除更丰富的抖音时间前缀模式
        # 11:12, 昨天 11:12, 2025/07/03 11:12, 3分钟前, 刚刚, 星期三 等
        text = re.sub(
            r"^((刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天])\s*)?(\d{1,2}:\d{2})?\s*",
            "",
            text,
        ).strip()
        # 移除可能存在的日期前缀
        text = re.sub(r"^\d{2,4}[/\-]\d{1,2}([/\-]\d{1,2})?\s*", "", text).strip()
        return re.sub(r"\s+", " ", text)

    def _extract_message_from_preview_block(self, customer_name: str, raw_text: Any) -> str:
        """从异常放大的会话预览块中提取最可能的消息正文。"""
        text = re.sub(r"[\u200b-\u200f\ufeff]+", "", str(raw_text or "")).strip()
        if not text:
            return ""

        lines = [
            segment.strip()
            for segment in re.split(r"[\r\n|]+", text)
            if str(segment or "").strip()
        ]
        if len(lines) <= 1:
            return self._normalize_preview_message_reference(text)

        normalized_customer_name = self._normalize_customer_name(customer_name)
        skip_patterns = (
            r"^(昨天|今天|前天|刚刚|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天])$",
            r"^\d{1,2}:\d{2}$",
            r"^\d{4}[/\-]\d{1,2}([/\-]\d{1,2})?$",
            r"^\d{1,2}[/\-]\d{1,2}([/\-]\d{1,2})?$",
        )
        skip_prefixes = (
            "对方回复或关注你之前",
            "请礼貌发言",
            "纸牌屋科技有限公司顾问",
        )

        for line in lines:
            normalized_line = self._normalize_customer_name(line)
            if normalized_customer_name and normalized_line == normalized_customer_name:
                continue
            if re.match(r"^\d+$", line) and len(line) < 3:
                continue
            if any(re.match(pattern, line) for pattern in skip_patterns):
                continue
            if any(line.startswith(prefix) for prefix in skip_prefixes):
                continue
            if "抖音自律公约" in line:
                continue
            normalized_line = self._normalize_preview_message_reference(line)
            if normalized_line:
                return normalized_line

        return self._normalize_preview_message_reference(lines[-1])

    def _sanitize_dom_preview_payload(
        self,
        *,
        customer_name: str,
        content: str,
        preview_signature: str,
    ) -> tuple[str, str]:
        """兜底清洗会话预览，避免整块列表文本污染状态机。"""
        raw_content = str(content or "").strip()
        raw_signature = str(preview_signature or "").strip()
        if not raw_content and not raw_signature:
            return "", ""

        content_has_block_shape = ("\n" in raw_content) or (raw_content.count("|") >= 3)
        signature_has_block_shape = ("\n" in raw_signature) or (raw_signature.count("|") >= 3)

        sanitized_content = raw_content
        if content_has_block_shape:
            sanitized_content = self._extract_message_from_preview_block(customer_name, raw_content)

        sanitized_signature = raw_signature
        if signature_has_block_shape:
            signature_message = self._extract_message_from_preview_block(customer_name, raw_signature)
            signature_hash = hashlib.md5(raw_signature.encode("utf-8", errors="ignore")).hexdigest()[:8]
            sanitized_signature = " | ".join(
                part for part in [customer_name.strip(), signature_message, f"sig:{signature_hash}"] if part
            ).strip()

        if sanitized_content and sanitized_content != raw_content:
            logger.warning(
                f"[状态机] {customer_name}: 检测到被污染的会话预览块，"
                f"已净化为消息文本: {sanitized_content[:30]}..."
            )
        return sanitized_content or raw_content, sanitized_signature or raw_signature

    def _normalize_preview_equivalence_text(self, content: Any) -> str:
        """归一化左侧会话预览，容忍抖音对长回复的截断、去括号标题和空白污染。"""
        text = self._normalize_preview_message_reference(content)
        if not text:
            return ""
        text = re.sub(r"【[^】]{0,60}】", "", text)
        text = re.sub(r"\[[^\]]{0,60}\]", "", text)
        text = re.sub(r"[（）()「」『』《》〈〉<>]", "", text)
        text = re.sub(r"[，。！？!?,、:：；;·\-\s]+", "", text)
        return text.strip()

    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """计算两个文本的字符级 2-gram Jaccard 相似度。"""
        left = str(text1 or "").strip().lower()
        right = str(text2 or "").strip().lower()
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        if len(left) < 2 or len(right) < 2:
            return 0.0
        grams1 = {left[i:i + 2] for i in range(len(left) - 1)}
        grams2 = {right[i:i + 2] for i in range(len(right) - 1)}
        if not grams1 or not grams2:
            return 0.0
        union = grams1 | grams2
        if not union:
            return 0.0
        return len(grams1 & grams2) / len(union)

    def _looks_like_preview_of_last_sent_message(self, preview_content: Any, last_sent_content: Any) -> bool:
        """判断左侧预览是否只是我方上一条回复的预览形态。"""
        preview_text = self._normalize_preview_equivalence_text(preview_content)
        sent_text = self._normalize_preview_equivalence_text(last_sent_content)
        if not preview_text or not sent_text:
            return False
        if preview_text == sent_text:
            return True
        if len(preview_text) >= 12 and len(sent_text) >= 12 and (
            preview_text in sent_text or sent_text in preview_text
        ):
            return True
        return self._calculate_text_similarity(preview_text, sent_text) >= 0.78

    def _get_active_conversation_snapshot(self) -> Dict[str, Any]:
        """抓取当前右侧活动会话的真实末条快照。

        这条链只依赖当前聊天区，不点击左侧列表，减少对预览文本和未读数的依赖。
        """
        try:
            snapshot_js = r"""
            () => {
                const normalizeText = (value) => String(value || '')
                    .replace(/[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/g, '')
                    .replace(/\\s+/g, ' ')
                    .trim();
                const normalizeName = (value) => {
                    const text = String(value || '').replace(/[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/g, '').trim();
                    if (!text) return '';
                    const tokens = text.split(/\s+/).filter(Boolean);
                    if (tokens.length <= 1) return text;
                    const trailingPattern = /^(刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d+)$/;
                    while (tokens.length > 1 && trailingPattern.test(tokens[tokens.length - 1])) {
                        tokens.pop();
                    }
                    return tokens.join(' ').trim() || text;
                };
                const pickText = (root, selectors) => {
                    for (const selector of selectors) {
                        const el = root.querySelector(selector);
                        const text = el && el.textContent ? el.textContent.trim() : '';
                        if (text) {
                            return text;
                        }
                    }
                    return '';
                };

                const firstMessage = document.querySelector('[data-e2e="msg-item-content"]');
                const msgInput = document.querySelector('[data-e2e="msg-input"]');
                const rightPanelRoot =
                    (msgInput && (
                        msgInput.closest('[class*="RightPanel"]') ||
                        msgInput.closest('[class*="rightPanel"]') ||
                        msgInput.closest('[class*="chatMain"]') ||
                        msgInput.closest('[class*="imChat"]') ||
                        msgInput.parentElement
                    )) ||
                    (firstMessage && (
                        firstMessage.closest('[class*="RightPanel"]') ||
                        firstMessage.closest('[class*="rightPanel"]') ||
                        firstMessage.closest('[class*="chatMain"]') ||
                        firstMessage.closest('[class*="imChat"]') ||
                        firstMessage.parentElement
                    )) ||
                    document.body;

                const headerRoot =
                    rightPanelRoot.querySelector('[class*="chatHeader"]') ||
                    rightPanelRoot.querySelector('[data-e2e="chat-header"]') ||
                    rightPanelRoot.querySelector('[class*="header"]') ||
                    rightPanelRoot;

                let customerName = normalizeName(pickText(headerRoot, [
                    '[class*="nickname"]',
                    '[class*="title"]',
                    '[class*="name"]',
                    'h1',
                    'h2'
                ]));

                if (!customerName) {
                    const activeConversationItem =
                        document.querySelector('[data-e2e="conversation-item"][class*="curConversation"]') ||
                        document.querySelector('[aria-selected="true"][data-e2e="conversation-item"]') ||
                        document.querySelector('[class*="curConversation"]');
                    if (activeConversationItem) {
                        customerName = normalizeName(pickText(activeConversationItem, [
                            '.conversationConversationItemtitle',
                            '[class*="title"]',
                            '[class*="nickname"]',
                            '[class*="name"]'
                        ]) || activeConversationItem.textContent || '');
                    }
                }

                const messageListRoot =
                    rightPanelRoot.querySelector('[class*="messageMessageListwrapper"]') ||
                    rightPanelRoot.querySelector('[class*="messageMessageListlist"]') ||
                    rightPanelRoot.querySelector('[class*="messageList"]') ||
                    rightPanelRoot.querySelector('[class*="msgList"]') ||
                    rightPanelRoot.querySelector('[class*="chatContent"]') ||
                    rightPanelRoot;
                const messageContentNodes = Array.from(
                    (messageListRoot || rightPanelRoot || document).querySelectorAll('[data-e2e="msg-item-content"]')
                );
                if (!messageContentNodes.length) {
                    return {
                        customer_name: customerName,
                        last_inbound_message: '',
                        last_bubble_text: '',
                        last_bubble_is_inbound: false,
                        inbound_bubbles: [],
                        tail_inbound_bubbles: [],
                        bubble_count: 0,
                        inbound_bubble_count: 0,
                        last_inbound_index: -1,
                        last_outbound_index: -1,
                    };
                }

                const cleanBubbleText = (text) => normalizeText(text).replace(/(^|\s)已读(\s|$)/g, ' ').trim();
                const messageItems = [];
                const viewportWidth = Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0, 1);
                const messageBoxNodes = Array.from(
                    (messageListRoot || rightPanelRoot || document).querySelectorAll('[class*="messageMessageBoxmessageBox"]')
                );
                if (messageBoxNodes.length) {
                    for (const boxEl of messageBoxNodes) {
                        const contentEl =
                            boxEl.querySelector('[class*="messageMessageBoxcontentBox"]') ||
                            boxEl.querySelector('[data-e2e="msg-item-content"]') ||
                            boxEl;
                        const rawText = cleanBubbleText(
                            (contentEl && (contentEl.innerText || contentEl.textContent)) ||
                            boxEl.innerText ||
                            boxEl.textContent ||
                            ''
                        );
                        if (!rawText) continue;
                        const classText = [
                            boxEl.className || '',
                            contentEl && typeof contentEl.className === 'string' ? contentEl.className : '',
                            contentEl && contentEl.closest && contentEl.closest('[class*="isFromMe"]')
                                ? 'isFromMe'
                                : '',
                        ].join(' ').toLowerCase();
                        const rect = typeof boxEl.getBoundingClientRect === 'function'
                            ? boxEl.getBoundingClientRect()
                            : { top: 0, bottom: 0, left: 0, right: 0 };
                        const bubbleCenterX = (
                            (Number.isFinite(rect.left) ? rect.left : 0) +
                            (Number.isFinite(rect.right) ? rect.right : 0)
                        ) / 2;
                        const isRightAlignedBubble =
                            viewportWidth > 0 &&
                            bubbleCenterX >= viewportWidth * 0.62;
                        const isFromMe =
                            classText.includes('isfromme') ||
                            classText.includes('outbound') ||
                            classText.includes('self') ||
                            isRightAlignedBubble;

                        messageItems.push({
                            text: rawText,
                            isInbound: !isFromMe,
                            rectTop: Number.isFinite(rect.top) ? rect.top : 0,
                            rectBottom: Number.isFinite(rect.bottom) ? rect.bottom : 0,
                        });
                    }
                } else {
                    for (const textEl of messageContentNodes) {
                        const text = cleanBubbleText(textEl && textEl.textContent ? textEl.textContent : '');
                        if (!text) continue;

                        const bubbleRoot =
                            textEl.closest('[class*="messageBox"]') ||
                            textEl.closest('[class*="columnBox"]') ||
                            textEl.closest('[class*="rowBox"]') ||
                            textEl.parentElement ||
                            textEl;
                        const bubbleParent = bubbleRoot && bubbleRoot.parentElement ? bubbleRoot.parentElement : null;
                        const bubbleGrandParent = bubbleParent && bubbleParent.parentElement ? bubbleParent.parentElement : null;
                        const nearbyClassParts = [
                            textEl.className || '',
                            textEl.parentElement ? (textEl.parentElement.className || '') : '',
                            bubbleRoot ? (bubbleRoot.className || '') : '',
                            bubbleParent ? (bubbleParent.className || '') : '',
                            bubbleGrandParent ? (bubbleGrandParent.className || '') : '',
                        ].join(' ').toLowerCase();
                        const senderEl = bubbleRoot ? bubbleRoot.querySelector('[class*="sender"]') : null;
                        const senderText = senderEl && senderEl.textContent ? senderEl.textContent.trim() : '';
                        const rect = typeof textEl.getBoundingClientRect === 'function'
                            ? textEl.getBoundingClientRect()
                            : { top: 0, bottom: 0, left: 0, right: 0 };
                        const hasFromMeAncestor =
                            !!textEl.closest('[class*="isFromMe"]') ||
                            !!textEl.closest('[class*="isfromme"]');
                        const hasExplicitOutboundMarker =
                            hasFromMeAncestor ||
                            nearbyClassParts.includes('isfromme') ||
                            nearbyClassParts.includes('outbound') ||
                            nearbyClassParts.includes('self') ||
                            senderText === '我' ||
                            senderText === '我发送的' ||
                            text.startsWith('我:') ||
                            text.startsWith('我：') ||
                            text.startsWith('[自动回复]');
                        const bubbleCenterX = (
                            (Number.isFinite(rect.left) ? rect.left : 0) +
                            (Number.isFinite(rect.right) ? rect.right : 0)
                        ) / 2;
                        const isRightAlignedBubble =
                            viewportWidth > 0 &&
                            bubbleCenterX >= viewportWidth * 0.62;
                        const isFromMe =
                            hasExplicitOutboundMarker ||
                            (!hasFromMeAncestor && isRightAlignedBubble);

                        messageItems.push({
                            text,
                            isInbound: !isFromMe,
                            rectTop: Number.isFinite(rect.top) ? rect.top : 0,
                            rectBottom: Number.isFinite(rect.bottom) ? rect.bottom : 0,
                        });
                    }
                }

                if (!messageItems.length) {
                    return {
                        customer_name: customerName,
                        last_inbound_message: '',
                        last_bubble_text: '',
                        last_bubble_is_inbound: false,
                        inbound_bubbles: [],
                        tail_inbound_bubbles: [],
                        bubble_count: 0,
                        inbound_bubble_count: 0,
                        last_inbound_index: -1,
                        last_outbound_index: -1,
                    };
                }

                messageItems.sort((a, b) => {
                    if (a.rectTop !== b.rectTop) return a.rectTop - b.rectTop;
                    return a.rectBottom - b.rectBottom;
                });

                const lastBubble = messageItems[messageItems.length - 1] || null;
                const inboundItems = messageItems.filter((item) => item.isInbound);
                const lastInbound = inboundItems.length ? inboundItems[inboundItems.length - 1].text : '';
                const inboundBubbles = [];
                const tailInboundBubbles = [];
                let lastInboundIndex = -1;
                let lastOutboundIndex = -1;
                for (let i = messageItems.length - 1; i >= 0; i -= 1) {
                    if (messageItems[i] && messageItems[i].isInbound) {
                        lastInboundIndex = i;
                        break;
                    }
                }
                for (let i = messageItems.length - 1; i >= 0; i -= 1) {
                    if (messageItems[i] && !messageItems[i].isInbound) {
                        lastOutboundIndex = i;
                        break;
                    }
                }
                for (let i = 0; i < messageItems.length; i += 1) {
                    const item = messageItems[i];
                    if (!item || !item.isInbound || !item.text) continue;
                    inboundBubbles.push({
                        index: i,
                        text: item.text,
                    });
                }
                let tailOrder = 0;
                for (let i = Math.max(lastOutboundIndex + 1, 0); i < messageItems.length; i += 1) {
                    const item = messageItems[i];
                    if (!item || !item.isInbound || !item.text) continue;
                    tailInboundBubbles.push({
                        tail_order: tailOrder,
                        text: item.text,
                    });
                    tailOrder += 1;
                }

                return {
                    customer_name: customerName,
                    last_inbound_message: lastInbound,
                    last_bubble_text: lastBubble ? lastBubble.text : '',
                    last_bubble_is_inbound: Boolean(lastBubble && lastBubble.isInbound),
                    inbound_bubbles: inboundBubbles.slice(-20),
                    tail_inbound_bubbles: tailInboundBubbles.slice(-10),
                    bubble_count: messageItems.length,
                    inbound_bubble_count: inboundItems.length,
                    last_inbound_index: lastInboundIndex,
                    last_outbound_index: lastOutboundIndex,
                };
            }
            """
            result = self._safe_evaluate(snapshot_js, timeout_ms=5000)
            if not isinstance(result, dict):
                return {}
            result["customer_name"] = self._normalize_customer_name(result.get("customer_name", ""))
            return result
        except Exception as e:
            logger.debug(f"获取活动会话快照失败: {e}")
            return {}

    def _build_active_snapshot_signature(
        self,
        active_snapshot: Optional[Dict[str, Any]],
        *,
        customer_name: str = "",
    ) -> str:
        """构建活动会话右侧气泡签名，用于区分同文案的不同轮次消息。"""
        return self._get_inbound_decision_engine().build_active_snapshot_signature(
            active_snapshot,
            customer_name=customer_name,
        )

    def _normalize_processed_inbound_bubble_signatures(self, values: Any) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        for value in list(values or []):
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        if len(normalized) > 20:
            normalized = normalized[-20:]
        return normalized

    def _build_inbound_bubble_signature(
        self,
        *,
        customer_name: str,
        round_anchor: str,
        position_hint: str,
        text: str,
    ) -> str:
        normalized_name = self._normalize_customer_name(customer_name)
        normalized_text = self._normalize_preview_message_reference(text)
        normalized_text = re.sub(r"\s+", "", normalized_text or "").strip()
        if not normalized_text:
            normalized_text = re.sub(r"\s+", "", str(text or "")).strip()
        if not normalized_text:
            return ""
        digest = hashlib.md5(normalized_text.encode("utf-8")).hexdigest()[:16]
        return f"{normalized_name}|{str(round_anchor or '').strip()}|{str(position_hint or '').strip()}|{digest}"

    def _build_active_snapshot_inbound_signature(
        self,
        *,
        customer_name: str,
        active_snapshot: Optional[Dict[str, Any]],
        state: Optional[Dict[str, Any]],
        position_hint: str,
        text: str,
    ) -> str:
        """为 active_snapshot 直读到的末条入站绑定稳定签名。

        不能只依赖整体 snapshot 是否变化；否则同一条旧消息在右侧 DOM 结构变化后，
        仍可能被重新当成新入站。这里把“我方最近一轮回复锚点 + 当前右侧快照签名”
        绑定进同一个 bubble signature，用于业务级判重。
        """
        return self._get_inbound_decision_engine().build_active_snapshot_inbound_signature(
            customer_name=customer_name,
            active_snapshot=active_snapshot,
            state=state,
            position_hint=position_hint,
            text=text,
        )

    def _get_inbound_round_anchor(
        self,
        state: Optional[Dict[str, Any]],
        *,
        fallback_anchor: str = "",
    ) -> str:
        if isinstance(state, dict) and bool(state.get("sent_by_us", False)):
            content = str(state.get("last_content", "") or "").strip()
            if content:
                # 使用内容摘要作为锚点，比使用毫秒级时间戳稳定得多，
                # 只有当我们真正发送了不同内容的回复时，才会刷新旧气泡的签名。
                content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
                return f"reply_content:{content_hash}"
            
            try:
                last_time = float(state.get("last_time", 0) or 0)
            except Exception:
                last_time = 0.0
            if last_time > 0:
                # 降级：若内容为空，使用按分钟对齐的时间戳，减少秒级抖动导致的签名刷新
                return f"reply_time:{int(last_time / 60)}"
        fallback = str(fallback_anchor or "").strip()
        if fallback:
            return fallback
        return "no_reply_anchor"

    def _collect_tail_inbound_bubble_signatures(
        self,
        active_snapshot: Optional[Dict[str, Any]],
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """收集 active_snapshot.tail_inbound_bubbles 中所有气泡的签名。

        用于首次加载建立基线时全量入库历史气泡签名，防止后续轮询逐条浮现历史气泡。
        """
        if not isinstance(active_snapshot, dict):
            return []
        tail_candidates = list(active_snapshot.get("tail_inbound_bubbles") or [])
        if not tail_candidates:
            return []
        snapshot_name = self._normalize_customer_name(active_snapshot.get("customer_name", ""))
        normalized_customer_name = self._normalize_customer_name(customer_name)
        round_anchor = self._get_inbound_round_anchor(
            state,
            fallback_anchor=f"snapshot:{snapshot_name or normalized_customer_name}",
        )
        signatures: List[str] = []
        for item in tail_candidates:
            text = str((item or {}).get("text") or "").strip()
            if not text or not self._is_valid_message(text):
                continue
            try:
                tail_order = int((item or {}).get("tail_order", -1) or -1)
            except Exception:
                tail_order = -1
            signature = self._build_inbound_bubble_signature(
                customer_name=snapshot_name or normalized_customer_name,
                round_anchor=round_anchor,
                position_hint=f"tail:{tail_order}",
                text=text,
            )
            if signature:
                signatures.append(signature)
        return signatures

    def _merge_processed_inbound_bubble_signatures(
        self,
        state: Optional[Dict[str, Any]],
        *,
        new_signature: str = "",
    ) -> List[str]:
        merged = self._normalize_processed_inbound_bubble_signatures(
            (state or {}).get("processed_inbound_bubble_signatures", [])
        )
        normalized_signature = str(new_signature or "").strip()
        if normalized_signature:
            merged.append(normalized_signature)
        return self._normalize_processed_inbound_bubble_signatures(merged)

    def _get_active_inbound_bubble_candidates(
        self,
        active_snapshot: Optional[Dict[str, Any]],
        *,
        customer_name: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(active_snapshot, dict):
            return []

        snapshot_name = self._normalize_customer_name(active_snapshot.get("customer_name", ""))
        normalized_customer_name = self._normalize_customer_name(customer_name)
        if normalized_customer_name and snapshot_name != normalized_customer_name:
            return []

        candidates: List[Dict[str, Any]] = []
        tail_candidates = list(active_snapshot.get("tail_inbound_bubbles") or [])
        if not tail_candidates:
            return []
        round_anchor = self._get_inbound_round_anchor(
            state,
            fallback_anchor=f"snapshot:{snapshot_name or normalized_customer_name}",
        )

        for item in tail_candidates:
            text = str((item or {}).get("text") or "").strip()
            if not text or not self._is_valid_message(text):
                continue
            try:
                tail_order = int((item or {}).get("tail_order", -1) or -1)
            except Exception:
                tail_order = -1
            signature = self._build_inbound_bubble_signature(
                customer_name=snapshot_name or normalized_customer_name,
                round_anchor=round_anchor,
                position_hint=f"tail:{tail_order}",
                text=text,
            )
            candidates.append(
                {
                    "tail_order": tail_order,
                    "text": text,
                    "signature": signature,
                }
            )

        candidates.sort(key=lambda item: item.get("tail_order", -1))
        return candidates

    def _resolve_next_active_inbound_candidate(
        self,
        active_snapshot: Optional[Dict[str, Any]],
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]] = None,
        last_reference: str = "",
    ) -> tuple[Optional[str], str, str]:
        candidates = self._get_active_inbound_bubble_candidates(
            active_snapshot,
            customer_name=customer_name,
            state=state,
        )
        if (
            not candidates
            and isinstance(active_snapshot, dict)
            and bool(active_snapshot.get("last_bubble_is_inbound", False)) is False
        ):
            return None, "", ""
        if not candidates:
            return None, "", ""

        processed_signatures = set(
            self._normalize_processed_inbound_bubble_signatures(
                (state or {}).get("processed_inbound_bubble_signatures", [])
            )
        )
        
        normalized_last_reference = self._normalize_preview_message_reference(last_reference)
        
        for candidate in candidates:
            signature = str(candidate.get("signature", "") or "").strip()
            if signature and signature in processed_signatures:
                continue
            
            # 二次防御：即便签名没见过（比如anchor变了），如果内容跟上次回复的用户消息完全一样，也跳过。
            # 这能彻底解决“我方回复后，旧用户消息还在tail里被误判成新消息”的顽疾。
            text = str(candidate.get("text", "") or "").strip()
            if text and self._normalize_preview_message_reference(text) == normalized_last_reference:
                continue
                
            return text, "active_snapshot_queue", signature

        # 兜底：如果所有候选气泡都被跳过（可能是因为签名逻辑或二次防御过于严格），
        # 但最后一条气泡的内容确实发生了变化（不同于上次参考值），则依然放行，确保不漏掉消息。
        # 修复：增加签名检查，如果最后一条气泡的签名已在 processed 集合中，说明是已处理过的历史气泡，
        # 不应通过兜底逻辑放行。根因：首次加载基线全量入库签名后，所有历史气泡都被跳过，
        # 但兜底逻辑仍会放行最后一条气泡（只要内容不同于 last_reference），导致历史消息被误判为新消息。
        if candidates:
            latest_candidate = candidates[-1]
            latest_text = str(latest_candidate.get("text", "") or "").strip()
            latest_signature = str(latest_candidate.get("signature", "") or "").strip()
            if (
                latest_text
                and self._normalize_preview_message_reference(latest_text) != normalized_last_reference
                and latest_signature not in processed_signatures
            ):
                return latest_text, "active_snapshot_fallback", latest_signature

        return None, "", ""

    def _build_dom_snapshot_signature(
        self,
        snapshot: Optional[Dict[str, Any]],
        *,
        customer_name: str = "",
    ) -> str:
        """从通用 DOM 快照生成右侧气泡签名，兜底活动快照缺字段的场景。"""
        if not isinstance(snapshot, dict):
            return ""

        nodes = list(snapshot.get("messageBubbleNodes") or snapshot.get("dataE2eNodes") or [])
        if not nodes:
            return ""

        message_nodes = []
        left_positions = []
        for node in nodes:
            if str((node or {}).get("dataE2e") or "").strip() != "msg-item-content":
                continue
            text = str((node or {}).get("text") or "").strip()
            if not text or not self._is_valid_message(text):
                continue
            rect = (node or {}).get("rect") or {}
            try:
                left = float(rect.get("left", 0) or 0)
                top = float(rect.get("top", 0) or 0)
                bottom = float(rect.get("bottom", 0) or 0)
            except Exception:
                left = 0.0
                top = 0.0
                bottom = 0.0
            message_nodes.append(
                {
                    "text": text,
                    "left": left,
                    "top": top,
                    "bottom": bottom,
                    "class_text": " ".join(
                        [
                            str((node or {}).get("className") or ""),
                            str((node or {}).get("parentClass") or ""),
                            str((node or {}).get("grandParentClass") or ""),
                        ]
                    ).lower(),
                }
            )
            left_positions.append(left)

        if not message_nodes:
            return ""

        sorted_left = sorted(left_positions)
        split_index = len(sorted_left) // 2
        inbound_threshold = sorted_left[split_index] if sorted_left else 0.0
        if len(sorted_left) >= 2:
            inbound_threshold = (sorted_left[0] + sorted_left[-1]) / 2.0

        for node in message_nodes:
            class_text = node["class_text"]
            node["is_inbound"] = not (
                "isfromme" in class_text
                or "outbound" in class_text
                or "self" in class_text
                or node["left"] > inbound_threshold
            )

        message_nodes.sort(key=lambda item: (item["top"], item["bottom"]))
        inbound_nodes = [item for item in message_nodes if item.get("is_inbound")]
        last_node = message_nodes[-1]
        last_inbound_index = -1
        for index in range(len(message_nodes) - 1, -1, -1):
            if message_nodes[index].get("is_inbound"):
                last_inbound_index = index
                break

        parts = [
            self._normalize_customer_name(customer_name),
            str(len(message_nodes)),
            str(len(inbound_nodes)),
            str(last_inbound_index),
            "1" if bool(last_node.get("is_inbound")) else "0",
            str(last_node.get("text", "")),
            str(inbound_nodes[-1].get("text", "")) if inbound_nodes else "",
        ]
        return "|".join(parts)

    def _extract_inbound_message_from_dom_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> Optional[str]:
        """从通用 DOM 诊断快照中兜底提取最后一条用户消息。"""
        if not isinstance(snapshot, dict):
            return None

        nodes = list(snapshot.get("messageBubbleNodes") or snapshot.get("dataE2eNodes") or [])
        if not nodes:
            return None

        message_nodes = []
        left_positions = []
        for node in nodes:
            if str((node or {}).get("dataE2e") or "").strip() != "msg-item-content":
                continue
            text = str((node or {}).get("text") or "").strip()
            if not text or not self._is_valid_message(text):
                continue
            rect = (node or {}).get("rect") or {}
            left = rect.get("left", 0)
            top = rect.get("top", 0)
            bottom = rect.get("bottom", 0)
            try:
                left = float(left)
                top = float(top)
                bottom = float(bottom)
            except Exception:
                left = 0.0
                top = 0.0
                bottom = 0.0
            message_nodes.append(
                {
                    "text": text,
                    "left": left,
                    "top": top,
                    "bottom": bottom,
                    "class_text": " ".join(
                        [
                            str((node or {}).get("className") or ""),
                            str((node or {}).get("parentClass") or ""),
                            str((node or {}).get("grandParentClass") or ""),
                        ]
                    ).lower(),
                }
            )
            left_positions.append(left)

        if not message_nodes:
            return None

        min_left = min(left_positions)
        max_left = max(left_positions)
        alignment_threshold = (min_left + max_left) / 2.0
        alignment_span = max_left - min_left

        message_nodes.sort(key=lambda item: (item["top"], item["bottom"]))
        last_inbound = None
        for item in message_nodes:
            class_text = item["class_text"]
            explicit_outbound = (
                "isfromme" in class_text
                or "outbound" in class_text
                or "self" in class_text
                or item["text"].startswith("我:")
                or item["text"].startswith("我：")
                or item["text"].startswith("[自动回复]")
            )
            right_aligned = alignment_span >= 80 and item["left"] > alignment_threshold
            if explicit_outbound or right_aligned:
                continue
            last_inbound = item["text"]

        return last_inbound

    def _resolve_inbound_message_for_conversation(
        self,
        customer_name: str,
        *,
        is_active: bool = False,
        active_snapshot: Optional[Dict[str, Any]] = None,
        state: Optional[Dict[str, Any]] = None,
        last_reference: str = "",
    ) -> tuple[Optional[str], str, str]:
        """统一确认某个会话的真实用户消息。

        活动会话优先使用右侧聊天区快照；只有拿不到时才回退到点击会话核实。
        """
        normalized_name = str(customer_name or "").strip()
        normalized_name = self._normalize_customer_name(normalized_name)
        if not normalized_name:
            return None, "", ""

        snapshot = active_snapshot or {}
        if is_active:
            snapshot_name = self._normalize_customer_name(snapshot.get("customer_name", ""))
            queued_message, queued_source, queued_signature = self._resolve_next_active_inbound_candidate(
                snapshot,
                customer_name=normalized_name,
                state=state,
                last_reference=last_reference,
            )
            if queued_message:
                return queued_message, queued_source, queued_signature
            snapshot_message = str(snapshot.get("last_inbound_message", "") or "").strip()
            if (
                snapshot_name == normalized_name
                and bool(snapshot.get("last_bubble_is_inbound", False))
                and snapshot_message
                and self._is_valid_message(snapshot_message)
            ):
                fallback_signature = self._build_active_snapshot_inbound_signature(
                    customer_name=snapshot_name or normalized_name,
                    active_snapshot=active_snapshot,
                    state=state,
                    position_hint="latest_inbound",
                    text=snapshot_message,
                )
                return snapshot_message, "active_snapshot", fallback_signature
            if snapshot_name == normalized_name:
                dom_snapshot = self._collect_chat_dom_snapshot(
                    customer_name=normalized_name,
                    stage="resolve_active_inbound_fallback",
                )
                fallback_message = self._extract_inbound_message_from_dom_snapshot(dom_snapshot)
                if fallback_message:
                    logger.info(
                        f"[状态机] {normalized_name}: 通过DOM快照兜底确认真实用户消息: "
                        f"{fallback_message[:30]}..."
                    )
                    return fallback_message, "dom_snapshot", ""

        api_message, api_source, api_signature = self._resolve_api_inbound_message_for_conversation(
            normalized_name,
            state=state,
            last_reference=last_reference,
        )
        if api_message:
            return api_message, api_source, api_signature

        fetched_message, fetched_source, fetched_signature = self._fetch_actual_user_message(normalized_name)
        return fetched_message, fetched_source, fetched_signature

    def _build_api_intercept_inbound_signature(
        self,
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]],
        message: Dict[str, Any],
        content: str,
    ) -> str:
        msg_id = str((message or {}).get("msg_id") or "").strip()
        if msg_id:
            return f"api:{self._normalize_customer_name(customer_name)}|mid:{msg_id}"

        timestamp = str(
            (message or {}).get("timestamp")
            or (message or {}).get("created_at")
            or (message or {}).get("last_message_time")
            or ""
        ).strip()
        return self._build_inbound_bubble_signature(
            customer_name=customer_name,
            round_anchor=self._get_inbound_round_anchor(
                state,
                fallback_anchor=f"api:{self._normalize_customer_name(customer_name)}",
            ),
            position_hint=f"api:{timestamp or 'latest'}",
            text=content,
        )

    def _resolve_api_inbound_message_for_conversation(
        self,
        customer_name: str,
        *,
        state: Optional[Dict[str, Any]] = None,
        last_reference: str = "",
    ) -> tuple[Optional[str], str, str]:
        interceptor = getattr(self, "api_interceptor", None)
        if interceptor is None:
            return None, "", ""

        conversation_id = str((state or {}).get("conversation_id") or "").strip()
        customer_id = str((state or {}).get("customer_id") or "").strip()
        try:
            messages = interceptor.get_messages_for_conversation(
                customer_name=customer_name,
                conversation_id=conversation_id,
                customer_id=customer_id,
            )
        except Exception as exc:
            logger.debug(f"获取API入站证据失败: {exc}")
            return None, "", ""

        if not messages:
            return None, "", ""

        sorted_messages = sorted(
            list(messages),
            key=lambda item: (
                float(item.get("timestamp") or 0),
                int(item.get("_capture_seq") or 0),
            ),
        )
        normalized_reference = self._normalize_preview_message_reference(last_reference)
        for message in reversed(sorted_messages):
            direction = normalize_direction((message or {}).get("direction"), default="unknown")
            if direction != "inbound":
                continue
            content = str(
                (message or {}).get("content")
                or (message or {}).get("last_message_content")
                or ""
            ).strip()
            if not content or not self._is_valid_message(content):
                continue
            signature = self._build_api_intercept_inbound_signature(
                customer_name=customer_name,
                state=state,
                message=message,
                content=content,
            )
            normalized_content = self._normalize_preview_message_reference(content)
            if normalized_content != normalized_reference or signature:
                return content, "api_intercept", signature
        return None, "", ""

    def _classify_resolved_inbound_content(
        self,
        customer_name: str,
        *,
        last_reference: str,
        is_active: bool = False,
        active_snapshot: Optional[Dict[str, Any]] = None,
        state: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, Optional[str], str, str]:
        """统一判断真实用户消息是新消息、旧消息还是暂时取不到。"""
        return self._get_inbound_decision_engine().classify_resolved_inbound_content(
            customer_name,
            last_reference=last_reference,
            is_active=is_active,
            active_snapshot=active_snapshot,
            state=state,
        )

    def _get_inbound_decision_engine(self) -> InboundDecisionEngine:
        engine = getattr(self, "_inbound_decision_engine", None)
        if engine is None:
            engine = InboundDecisionEngine(
                normalize_customer_name=self._normalize_customer_name,
                normalize_preview_message_reference=self._normalize_preview_message_reference,
                is_valid_message=self._is_valid_message,
                resolve_inbound_message_for_conversation=self._resolve_inbound_message_for_conversation,
                normalize_processed_inbound_bubble_signatures=(
                    self._normalize_processed_inbound_bubble_signatures
                ),
                get_inbound_round_anchor=self._get_inbound_round_anchor,
                build_inbound_bubble_signature=self._build_inbound_bubble_signature,
                looks_like_preview_of_last_sent_message=self._looks_like_preview_of_last_sent_message,
            )
            self._inbound_decision_engine = engine
        return engine

    def _emit_resolved_inbound_content(
        self,
        *,
        conv: Dict[str, Any],
        actual_content: str,
        signal_source: str,
        unread_count: int,
    ) -> tuple[str, str, int]:
        """统一把已确认的真实用户消息写回当前候选并放行。"""
        conv["inbound_signal_source"] = signal_source or "resolved_inbound"
        return "emit", actual_content, max(0, int(unread_count or 0))

    def _make_inbound_decision_result(
        self,
        *,
        action: str,
        content: str,
        unread_count: int,
        evidence: str = "reject",
        signal_source: str = "",
    ) -> InboundDecisionResult:
        """统一构造入站决策结果，便于逐步把裸字符串动作收口成显式语义。"""
        return InboundDecisionResult(
            action=str(action or "skip"),
            content=str(content or ""),
            unread_count=max(0, int(unread_count or 0)),
            evidence=str(evidence or "reject"),
            signal_source=str(signal_source or ""),
        )

    def _decision_tuple(self, decision: InboundDecisionResult) -> tuple[str, str, int]:
        """在外层仍依赖旧 tuple 返回值时，把统一结果模型降级回兼容格式。"""
        return decision.action, decision.content, decision.unread_count

    def _resolve_classified_inbound_action(
        self,
        *,
        conv: Dict[str, Any],
        customer_name: str,
        unread_count: int,
        last_reference: str,
        is_active: bool,
        active_snapshot: Optional[Dict[str, Any]],
        success_log: str,
        same_log: str,
        missing_log: str,
        same_level: str = "info",
        state: Optional[Dict[str, Any]] = None,
        current_time: Optional[float] = None,
        skip_content: str = "",
    ) -> tuple[str, str, int]:
        """统一消费真实用户消息分类结果，按 new/same/missing 产出动作。"""
        resolved_status, actual_content, signal_source, bubble_signature = self._classify_resolved_inbound_content(
            customer_name,
            last_reference=str(last_reference or "").strip(),
            is_active=is_active,
            active_snapshot=active_snapshot,
            state=state,
        )
        if resolved_status == "new" and actual_content:
            if bubble_signature:
                conv["inbound_bubble_signature"] = bubble_signature
            logger.info(success_log)
            return self._emit_resolved_inbound_content(
                conv=conv,
                actual_content=actual_content,
                signal_source=signal_source,
                unread_count=unread_count,
            )

        # same -> same_log; missing -> missing_log。level 统一收口到 _log_skip_with_level
        logger_func = logger.debug if same_level == "debug" else logger.info
        logger_func(same_log if resolved_status == "same" else missing_log)

        if state is not None and current_time is not None:
            self._touch_state(
                state,
                content=skip_content,
                current_time=current_time,
                unread_count=unread_count,
            )
        return "skip", skip_content, unread_count

    def _skip_failed_inbound_resolution(
        self,
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]],
        content: str,
        unread_count: int,
        current_time: float,
        log_message: str,
        level: str = "info",
        warning_message: str = "",
        mark_dirty: bool = True,
    ) -> tuple[str, str, int]:
        """统一处理真实用户消息确认失败后的日志、脏未读与 skip。"""
        if warning_message:
            logger.warning(warning_message)
        if mark_dirty:
            self._mark_dirty_unread(customer_name, content, unread_count, current_time)
        return self._skip_inbound_with_state(
            customer_name=customer_name,
            state=state,
            content=content,
            unread_count=unread_count,
            log_message=log_message,
            level=level,
            evidence="dirty" if mark_dirty else "reject",
        )

    def _resolve_bot_preview_actual_content(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
    ) -> tuple[str, str, int]:
        """处理 BOT 预览下的真实用户消息确认与放行。"""
        user_last = state.get('user_last_content') if state else None
        if user_last and not any(pattern in user_last for pattern in self.BOT_REPLY_PATTERNS):
            logger.info(f"[状态机] {customer_name}: 使用状态机中保存的用户消息: {str(user_last)[:30]}...")
            return self._emit_resolved_inbound_content(
                conv=conv,
                actual_content=str(user_last),
                signal_source=str(conv.get("inbound_signal_source", "") or "state_user_last"),
                unread_count=unread_count,
            )

        logger.info(f"[状态机] {customer_name}: 状态机中无用户消息，尝试统一确认真实用户消息")
        result, resolved_content, resolved_unread = self._resolve_classified_inbound_action(
            conv=conv,
            customer_name=customer_name,
            unread_count=unread_count,
            last_reference=str(user_last or "").strip(),
            is_active=bool(conv.get('is_active', False)),
            active_snapshot=active_snapshot,
            success_log=(
                f"[状态机] {customer_name}: 获取到用户实际消息，"
                "继续按真实用户消息放行"
            ),
            same_log=(
                f"[状态机] {customer_name}: BOT模式下确认到真实用户消息与参考值相同，"
                "继续按已确认结果处理"
            ),
            missing_log=f"[状态机] {customer_name}: BOT模式无法确认真实用户消息，跳过",
            same_level="info",
            state=state,
            current_time=current_time,
            skip_content=content,
        )
        if result == "skip":
            return self._skip_failed_inbound_resolution(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=resolved_unread,
                current_time=current_time,
                log_message=f"[状态机] {customer_name}: BOT模式无法确认真实用户消息，跳过",
                level="debug",
                warning_message=f"[状态机] {customer_name}: 无法获取用户实际消息，跳过本次处理(避免用BOT内容作为用户消息)",
            )
        return result, resolved_content, resolved_unread

    def _build_conversation_state(
        self,
        customer_name: str,
        *,
        content: str,
        current_time: float,
        sent_by_us: bool,
        unread_count: int,
        existing_state: Optional[Dict[str, Any]] = None,
        customer_id: str = "",
        conversation_id: str = "",
        user_last_content: Any = _STATE_UNSET,
        dom_unread_at_send: Any = _STATE_UNSET,
        last_detected_unread: Any = _STATE_UNSET,
        inbound_signal_source: Any = _STATE_UNSET,
        preview_signature: Any = _STATE_UNSET,
        active_snapshot_signature: Any = _STATE_UNSET,
        processed_inbound_bubble_signatures: Any = _STATE_UNSET,
    ) -> Dict[str, Any]:
        """统一构建会话状态，收口重复字典写入。"""
        base_state = dict(existing_state or {})
        merged_state = {
            "last_content": content,
            "last_time": current_time,
            "sent_by_us": sent_by_us,
            "sent_by_us_at": current_time if sent_by_us else base_state.get("sent_by_us_at", 0),
            "unread_count": unread_count,
            "user_last_content": base_state.get("user_last_content"),
            "dom_unread_at_send": base_state.get("dom_unread_at_send", 0),
            "last_detected_unread": base_state.get("last_detected_unread", 0),
            "inbound_signal_source": base_state.get("inbound_signal_source", ""),
            "preview_signature": base_state.get("preview_signature", ""),
            "active_snapshot_signature": base_state.get("active_snapshot_signature", ""),
            "processed_inbound_bubble_signatures": self._normalize_processed_inbound_bubble_signatures(
                base_state.get("processed_inbound_bubble_signatures", [])
            ),
            **self._merge_identity_fields(
                customer_name,
                base_state,
                customer_id=customer_id,
                conversation_id=conversation_id,
            ),
        }
        if user_last_content is not _STATE_UNSET:
            merged_state["user_last_content"] = user_last_content
        if dom_unread_at_send is not _STATE_UNSET:
            merged_state["dom_unread_at_send"] = dom_unread_at_send
        if last_detected_unread is not _STATE_UNSET:
            merged_state["last_detected_unread"] = last_detected_unread
        if inbound_signal_source is not _STATE_UNSET:
            merged_state["inbound_signal_source"] = inbound_signal_source
        if preview_signature is not _STATE_UNSET:
            merged_state["preview_signature"] = preview_signature
        if active_snapshot_signature is not _STATE_UNSET:
            merged_state["active_snapshot_signature"] = active_snapshot_signature
        if processed_inbound_bubble_signatures is not _STATE_UNSET:
            merged_state["processed_inbound_bubble_signatures"] = self._normalize_processed_inbound_bubble_signatures(
                processed_inbound_bubble_signatures
            )
        return merged_state

    def _touch_state(
        self,
        state: Dict[str, Any],
        *,
        content: Any = _STATE_UNSET,
        current_time: Any = _STATE_UNSET,
        unread_count: Any = _STATE_UNSET,
        sent_by_us: Any = _STATE_UNSET,
        user_last_content: Any = _STATE_UNSET,
        dom_unread_at_send: Any = _STATE_UNSET,
        last_detected_unread: Any = _STATE_UNSET,
        inbound_signal_source: Any = _STATE_UNSET,
        preview_signature: Any = _STATE_UNSET,
        active_snapshot_signature: Any = _STATE_UNSET,
        processed_inbound_bubble_signatures: Any = _STATE_UNSET,
    ) -> None:
        """局部更新状态字段，减少 scattered 赋值。"""
        if content is not _STATE_UNSET:
            state["last_content"] = content
        if current_time is not _STATE_UNSET:
            state["last_time"] = current_time
        if unread_count is not _STATE_UNSET:
            state["unread_count"] = unread_count
        if sent_by_us is not _STATE_UNSET:
            state["sent_by_us"] = sent_by_us
        if user_last_content is not _STATE_UNSET:
            state["user_last_content"] = user_last_content
        if dom_unread_at_send is not _STATE_UNSET:
            state["dom_unread_at_send"] = dom_unread_at_send
        if last_detected_unread is not _STATE_UNSET:
            state["last_detected_unread"] = last_detected_unread
        if inbound_signal_source is not _STATE_UNSET:
            state["inbound_signal_source"] = inbound_signal_source
        if preview_signature is not _STATE_UNSET:
            state["preview_signature"] = preview_signature
        if active_snapshot_signature is not _STATE_UNSET:
            state["active_snapshot_signature"] = active_snapshot_signature
        if processed_inbound_bubble_signatures is not _STATE_UNSET:
            state["processed_inbound_bubble_signatures"] = self._normalize_processed_inbound_bubble_signatures(
                processed_inbound_bubble_signatures
            )

    @staticmethod
    def _resolve_last_reference(state: Optional[Dict[str, Any]]) -> str:
        """统一取"上次参考消息"：我方刚发->取用户上轮；用户发->取会话上轮。
        
        收敛前：3 处重复
            was_sent_by_us -> user_last_content
            else           -> last_content
        收敛后：单点定义。
        """
        if not isinstance(state, dict):
            return ""
        if bool(state.get("sent_by_us", False)):
            return str(state.get("user_last_content") or "").strip()
        return str(state.get("last_content") or "").strip()

    @staticmethod
    def _is_snapshot_advanced(conv: Dict[str, Any], state: Dict[str, Any]) -> bool:
        """判断 DOM 快照是否真的"前进了一格"（用户新消息已落到 DOM 中）。
        
        三要素同时满足：
        1. 当前快照签名非空
        2. 快照签名发生变化
        3. 末条气泡是入站，或 tail_inbound_bubbles 计数 > 0
        
        修复：要求 tail_inbound_count >= 2 或 last_bubble_inbound 为 True
        过滤 UI 抖动（如时间显示更新"刚刚→1分钟前"）导致的签名变化误判
        """
        signature = str((conv or {}).get("active_snapshot_signature", "") or "").strip()
        last_signature = str((state or {}).get("active_snapshot_signature", "") or "").strip()
        if not signature or signature == last_signature:
            return False
        last_bubble_inbound = bool((conv or {}).get("active_snapshot_last_bubble_inbound", False))
        tail_inbound_count = int((conv or {}).get("active_snapshot_tail_inbound_count", 0) or 0)
        # 修复：仅当末条气泡是入站（高置信度新消息）时才认为快照推进
        # tail_inbound_count >= 2 要求至少有 2 条入站气泡，过滤单条气泡的 UI 抖动
        return last_bubble_inbound or tail_inbound_count >= 2

    def _normalize_dom_conversation_candidate(
        self,
        conv: Dict[str, Any],
        *,
        active_customer_name: str,
        active_snapshot: Optional[Dict[str, Any]],
        current_time: float,
    ) -> Optional[Dict[str, Any]]:
        """归一化 DOM 会话候选，只保留可进入状态机的会话。"""
        customer_name = self._normalize_customer_name(conv.get("customer_name", ""))
        content = str(conv.get("last_message_content", "") or "").strip()
        unread_count = int(conv.get("unread_count", 0) or 0)
        preview_signature = str(conv.get("preview_signature", "") or "").strip()
        content, preview_signature = self._sanitize_dom_preview_payload(
            customer_name=customer_name,
            content=content,
            preview_signature=preview_signature,
        )
        is_active_conversation = bool(conv.get("is_active", False)) or (
            bool(active_customer_name) and customer_name == active_customer_name
        )
        if not customer_name:
            return None

        # 净化后二次检查：如果内容与状态机中 sent_by_us=True 的 last_content 匹配，
        # 强制将 direction 修正为 outbound，防止自回复回显被误判为用户新消息
        with self._state_lock:
            existing_state_for_sent_check = self._states.get(customer_name, {})
        if (
            existing_state_for_sent_check.get("sent_by_us", False)
            and content
            and self._looks_like_preview_of_last_sent_message(
                content, str(existing_state_for_sent_check.get("last_content", "") or "")
            )
        ):
            conv = dict(conv)
            conv["direction"] = "outbound"
            conv["direction_confidence"] = "high"
            logger.info(
                f"[状态机] {customer_name}: 净化后内容与我方已发送回复匹配，"
                f"修正direction=outbound, content={content[:30]}..."
            )

        normalized_conv = dict(conv)
        normalized_conv["is_active"] = is_active_conversation
        normalized_conv["last_message_content"] = content
        normalized_conv["preview_signature"] = preview_signature

        snapshot_name = self._normalize_customer_name((active_snapshot or {}).get("customer_name", ""))
        snapshot_message = self._normalize_preview_message_reference(
            (active_snapshot or {}).get("last_inbound_message", "")
        )
        active_snapshot_matches = bool(is_active_conversation and snapshot_name == customer_name)
        active_snapshot_signature = (
            self._build_active_snapshot_signature(active_snapshot, customer_name=customer_name)
            if active_snapshot_matches
            else ""
        )
        if (
            active_snapshot_matches
            and not active_snapshot_signature
            and unread_count > 0
        ):
            dom_signature_snapshot = self._collect_chat_dom_snapshot(
                customer_name=customer_name,
                content=content,
                stage="active_signature_fallback",
            )
            active_snapshot_signature = self._build_dom_snapshot_signature(
                dom_signature_snapshot,
                customer_name=customer_name,
            )
        if active_snapshot_signature:
            normalized_conv["active_snapshot_signature"] = active_snapshot_signature
        normalized_conv["active_snapshot_last_bubble_inbound"] = bool(
            (active_snapshot or {}).get("last_bubble_is_inbound", False)
        )
        normalized_conv["active_snapshot_tail_inbound_count"] = len(
            list((active_snapshot or {}).get("tail_inbound_bubbles") or [])
        )
        if (
            active_snapshot_matches
            and snapshot_message
            and self._is_valid_message(snapshot_message)
        ):
            # 修复 C2：优先采用 active_snapshot_queue 的最新气泡（右侧气泡队列，置信度最高），
            # 避免 active_snapshot.last_inbound_message（可能是旧 DOM 预览）先被发射。
            # 场景：unread 增长时，active_snapshot.last_inbound_message 可能是旧消息，
            # 而 active_snapshot_queue 的最新气泡才是真实最新消息。
            with self._state_lock:
                existing_state_for_queue = self._states.get(customer_name, {})
            queue_message, queue_signal_source, queue_signature = self._resolve_next_active_inbound_candidate(
                active_snapshot,
                customer_name=customer_name,
                state=existing_state_for_queue,
                last_reference=str(existing_state_for_queue.get("user_last_content", "") or ""),
            )
            if queue_message and self._is_valid_message(queue_message):
                normalized_conv["last_message_content"] = queue_message
                normalized_conv["direction"] = "inbound"
                normalized_conv["inbound_signal_source"] = queue_signal_source or "active_snapshot_queue"
                if queue_signature:
                    normalized_conv["inbound_bubble_signature"] = queue_signature
                content = queue_message
            else:
                # 当前打开会话以右侧真实末条为准，避免左侧旧未读预览把更早消息再次放进状态机。
                normalized_conv["last_message_content"] = snapshot_message
                normalized_conv["direction"] = "inbound"
                normalized_conv["inbound_signal_source"] = "active_snapshot"
                content = snapshot_message

        if unread_count > 0:
            with self._state_lock:
                existing_state = dict(self._states.get(customer_name, {}) or {})

            last_content = str(existing_state.get("last_content", "") or "").strip()
            last_user_content = str(existing_state.get("user_last_content", "") or "").strip()
            last_preview_signature = str(existing_state.get("preview_signature", "") or "").strip()
            last_active_snapshot_signature = str(existing_state.get("active_snapshot_signature", "") or "").strip()
            last_detected_unread = int(existing_state.get("last_detected_unread", 0) or 0)
            was_sent_by_us = bool(existing_state.get("sent_by_us", False))
            dirty_hits = int(existing_state.get("dirty_unread_hits", 0) or 0)
            if not self._is_valid_message(content):
                actual_content, signal_source, bubble_signature = self._resolve_inbound_message_for_conversation(
                    customer_name,
                    is_active=is_active_conversation,
                    active_snapshot=active_snapshot,
                    state=existing_state,
                    last_reference=last_user_content or last_content,
                )
                if actual_content and self._is_valid_message(actual_content):
                    normalized_conv["last_message_content"] = actual_content.strip()
                    normalized_conv["direction"] = "inbound"
                    normalized_conv["inbound_signal_source"] = signal_source or "resolved_inbound"
                    if bubble_signature:
                        normalized_conv["inbound_bubble_signature"] = bubble_signature
                    logger.info(
                        f"[状态机] {customer_name}: 左侧未读预览无效，"
                        f"已回退读取真实用户消息: {actual_content[:30]}..."
                    )
                    return normalized_conv
                logger.info(
                    f"[状态机] {customer_name}: 左侧未读预览无效，且未确认到真实用户消息，跳过本轮"
                )
                return None

            active_same_unread_preview_shift = bool(
                is_active_conversation
                and unread_count > 0
                and unread_count <= last_detected_unread
                and content
                and self._is_valid_message(content)
                and content != last_content
            )
            suspicious_unread = bool(
                existing_state
                and (
                    was_sent_by_us
                    or dirty_hits > 0
                    or (last_content and content == last_content)
                    or active_same_unread_preview_shift
                )
            )

            if (not is_active_conversation) and suspicious_unread and self._is_dirty_unread_cooling_down(
                customer_name,
                content,
                unread_count,
                current_time,
            ):
                logger.debug(
                    f"[状态机] {customer_name}: 命中脏未读冷却，暂不重检(unread={unread_count})"
                )
                return None

            if suspicious_unread:
                if (
                    (not is_active_conversation)
                    and was_sent_by_us
                    and unread_count <= last_detected_unread
                ):
                    logger.info(
                        f"[状态机] {customer_name}: 非活动会话未读未增长({unread_count}<={last_detected_unread})，"
                        "跳过切换核实，等待真正的新消息再触发会话跳转"
                    )
                    self._mark_dirty_unread(customer_name, content, unread_count, current_time)
                    return None
                actual_content, signal_source, bubble_signature = self._resolve_inbound_message_for_conversation(
                    customer_name,
                    is_active=is_active_conversation,
                    active_snapshot=active_snapshot,
                    state=existing_state,
                    last_reference=last_user_content or last_content,
                )
                if actual_content and self._is_valid_message(actual_content):
                    normalized_conv["last_message_content"] = actual_content.strip()
                    normalized_conv["direction"] = "inbound"
                    normalized_conv["inbound_signal_source"] = signal_source or "resolved_inbound"
                    if bubble_signature:
                        normalized_conv["inbound_bubble_signature"] = bubble_signature
                    confirmed_log = (
                        f"[状态机] {customer_name}: 已确认最新真实用户消息"
                        f"(source={signal_source or 'unknown'}): {actual_content[:30]}..."
                    )
                    repeated_same_message = bool(
                        actual_content == last_user_content and unread_count <= last_detected_unread
                    )
                    if repeated_same_message:
                        logger.debug(confirmed_log)
                    else:
                        logger.info(confirmed_log)
                    return normalized_conv

                active_preview_decision = self._evaluate_preview_fallback_decision(
                    mode="active",
                    is_active_conversation=is_active_conversation,
                    active_snapshot_matches=active_snapshot_matches,
                    active_snapshot_name=snapshot_name,
                    content=content,
                    unread_count=unread_count,
                    last_detected_unread=last_detected_unread,
                    preview_signature=preview_signature,
                    last_preview_signature=last_preview_signature,
                    last_content=last_content,
                    active_snapshot_last_bubble_inbound=bool(
                        (active_snapshot or {}).get("last_bubble_is_inbound", False)
                    ),
                    active_snapshot_tail_inbound_count=len(
                        list((active_snapshot or {}).get("tail_inbound_bubbles") or [])
                    ),
                    was_sent_by_us=was_sent_by_us,
                    last_user_content=last_user_content,
                )
                if active_preview_decision is not None:
                    normalized_conv["last_message_content"] = active_preview_decision.content.strip()
                    normalized_conv["direction"] = "inbound"
                    normalized_conv["inbound_signal_source"] = (
                        active_preview_decision.signal_source or "active_preview_fallback"
                    )
                    logger.info(
                        f"[状态机] {customer_name}: 右侧末条暂未提取成功，"
                        f"当前活动会话改用最新预览兜底放行: {content[:30]}..."
                    )
                    return normalized_conv

                non_active_preview_decision = self._evaluate_preview_fallback_decision(
                    mode="non_active",
                    is_active_conversation=is_active_conversation,
                    content=content,
                    unread_count=unread_count,
                    last_detected_unread=last_detected_unread,
                    preview_signature=preview_signature,
                    last_preview_signature=last_preview_signature,
                    last_content=last_content,
                    last_user_content=last_user_content,
                )
                if non_active_preview_decision is not None:
                    normalized_conv["last_message_content"] = non_active_preview_decision.content.strip()
                    normalized_conv["direction"] = "inbound"
                    normalized_conv["inbound_signal_source"] = (
                        non_active_preview_decision.signal_source or "non_active_preview_fallback"
                    )
                    logger.info(
                        f"[状态机] {customer_name}: 未读徽标未增长但预览已切到新内容，"
                        f"按非活动会话预览兜底放行: {content[:30]}..."
                    )
                    return normalized_conv

                self._skip_failed_inbound_resolution(
                    customer_name=customer_name,
                    state=existing_state,
                    content=content,
                    unread_count=unread_count,
                    current_time=current_time,
                    log_message=(
                        f"[状态机] {customer_name}: 未读会话命中可疑分支，但会话内未确认到新的用户末条消息，"
                        f"跳过本次处理(unread={unread_count})"
                    ),
                    level="info",
                )
                return None

            normalized_conv.setdefault("inbound_signal_source", "conversation_preview")
            normalized_conv["direction"] = "inbound"
            return normalized_conv

        if content and self._is_valid_message(content):
            normalized_conv.setdefault("inbound_signal_source", "conversation_preview")
            return normalized_conv

        return None

    def _emit_dom_message(
        self,
        *,
        customer_name: str,
        content: str,
        direction: MessageDirection,
        current_time: float,
        unread_count: int,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        messages: List[RPAMessage],
    ) -> None:
        """写入最新状态并生成 DOM 新消息对象。"""
        inbound_signal_source = str(
            conv.get("inbound_signal_source")
            or (state or {}).get("inbound_signal_source")
            or "conversation_preview"
        ).strip()
        logger.info(
            f"检测到新消息: {customer_name} -> {content[:30]}... "
            f"(unread={unread_count}, source={inbound_signal_source})"
        )
        merged_state = self._build_conversation_state(
            customer_name,
            content=content,
            current_time=current_time,
            sent_by_us=False,
            unread_count=unread_count,
            existing_state=state,
            customer_id=conv.get('customer_id', ''),
            conversation_id=conv.get('conversation_id', ''),
            user_last_content=content,
            dom_unread_at_send=0,
            last_detected_unread=unread_count,
            inbound_signal_source=inbound_signal_source,
            preview_signature=conv.get('preview_signature', ''),
            active_snapshot_signature=conv.get('active_snapshot_signature', ''),
            processed_inbound_bubble_signatures=self._merge_processed_inbound_bubble_signatures(
                state,
                new_signature=str(conv.get("inbound_bubble_signature", "") or "").strip(),
            ),
        )
        self._states[customer_name] = merged_state

        stable_conversation_id = (
            str(merged_state.get('conversation_id', '') or '').strip()
            or str(conv.get('conversation_id', '') or '').strip()
            or build_conversation_id(customer_name, "douyin")
        )

        msg = RPAMessage(
            customer_name=customer_name,
            content=content,
            direction=direction,
            timestamp=current_time,
            conversation_id=stable_conversation_id,
            customer_id=(
                str(merged_state.get('customer_id', '') or '').strip()
                or str(conv.get('customer_id', '') or '').strip()
            ),
            is_new=True,
            msg_id=self._build_stable_dom_msg_id(
                customer_name=customer_name,
                conversation_id=stable_conversation_id,
                content=content,
                direction=direction,
                unread_count=unread_count,
                bubble_signature=str(conv.get("inbound_bubble_signature", "") or "").strip(),
            ),
            signal_source=inbound_signal_source,
            direction_confidence=str(conv.get('direction_confidence', 'high') or 'high'),
        )
        messages.append(msg)
        logger.info(
            f"[DOM] 新消息: {customer_name} -> {content[:30]}... "
            f"(未读:{unread_count}, source={inbound_signal_source})"
        )

    def _finalize_first_fetch_and_trim(self, conversations: List[Dict], current_time: float) -> None:
        """统一处理首次加载收尾与缓存裁剪。"""
        if not self._first_fetch_done and conversations:
            self._first_fetch_done = True
            total_unread = sum(conv.get('unread_count', 0) for conv in conversations if conv.get('unread_count', 0) > 0)
            unread_convs = [(conv.get('customer_name', ''), conv.get('unread_count', 0)) for conv in conversations if conv.get('unread_count', 0) > 0]
            logger.info(f"首次加载完成，已记录 {len(self._states)} 个会话状态, 总未读数={total_unread}, 有未读的会话={unread_convs[:5]}")

        # 修复 B4：裁剪逻辑与 _is_new_message/_mark_message_processed 操作同一批
        # 共享结构（_states / _processed_msg_ids），必须持有 _state_lock，
        # 否则并发轮询时迭代中删除会抛 RuntimeError 并导致已处理消息丢失去重。
        with self._state_lock:
            expired_keys = [k for k, v in self._states.items() if current_time - v['last_time'] > self.SESSION_CACHE_TTL]
            for key in expired_keys:
                del self._states[key]

            # 修复：移除 len > 200 门槛，每次都清理过期的 _processed_msg_ids 条目，
            # 避免条目数 ≤ 200 时过期 msg_id 永久驻留导致消息无法重新处理
            now_for_msg_ids = current_time
            expired_msg_ids = [k for k, v in self._processed_msg_ids.items()
                              if now_for_msg_ids - v > self.SESSION_CACHE_TTL]
            for key in expired_msg_ids:
                del self._processed_msg_ids[key]
            if expired_msg_ids:
                logger.info(f"已处理消息ID缓存清理：清理了{len(expired_msg_ids)}项过期条目，剩余{len(self._processed_msg_ids)}项")

            # 保留 len > 200 时的批量裁剪作为额外保护
            if len(self._processed_msg_ids) > 200:
                sorted_ids = sorted(self._processed_msg_ids.items(), key=lambda x: x[1])
                remove_count = len(self._processed_msg_ids) - 100
                for key, _ in sorted_ids[:remove_count]:
                    del self._processed_msg_ids[key]
                logger.info(f"已处理消息ID缓存裁剪：清理了{remove_count}项，剩余{len(self._processed_msg_ids)}项")

            if len(self._states) > 500:
                sorted_states = sorted(self._states.items(), key=lambda x: x[1]['last_time'])
                remove_count = len(self._states) - 300
                for key, _ in sorted_states[:remove_count]:
                    del self._states[key]
                logger.info(f"状态缓存裁剪完成：清理了{remove_count}项，剩余{len(self._states)}项")

    def _skip_inbound_with_state(
        self,
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]],
        content: str,
        unread_count: int,
        log_message: str,
        level: str = "debug",
        touch_state: bool = False,
        current_time: Optional[float] = None,
        evidence: str = "reject",
    ) -> tuple[str, str, int]:
        """统一处理入站过滤的日志与状态更新。"""
        if level == "info":
            logger.info(log_message)
        else:
            logger.debug(log_message)

        if not state:
            return self._decision_tuple(
                self._make_inbound_decision_result(
                    action="skip",
                    content=content,
                    unread_count=unread_count,
                    evidence=evidence,
                )
            )

        normalized_unread_count = int(unread_count or 0)
        if normalized_unread_count <= 0:
            self._reset_unread_tracking(state)
            normalized_unread_count = 0

        if touch_state:
            touch_kwargs = {
                "content": content,
                "current_time": current_time or time.time(),
                "unread_count": normalized_unread_count,
            }
            if normalized_unread_count <= 0:
                touch_kwargs["last_detected_unread"] = 0
            self._touch_state(
                state,
                **touch_kwargs,
            )
        else:
            state['unread_count'] = normalized_unread_count
            if normalized_unread_count <= 0:
                state['last_detected_unread'] = 0

        return self._decision_tuple(
            self._make_inbound_decision_result(
                action="skip",
                content=content,
                unread_count=normalized_unread_count,
                evidence=evidence,
            )
        )

    def _build_inbound_baseline_state(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        last_detected_unread: Optional[int] = None,
        active_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """统一构建仅用于建立基线的入站会话状态。"""
        # 修复：首次加载建立基线时，全量入库 active_snapshot.tail_inbound_bubbles 的所有签名，
        # 防止第二轮轮询时 _resolve_next_active_inbound_candidate 逐条浮现历史气泡导致误判为新消息。
        # 根因：原实现只合并单条 inbound_bubble_signature，未遍历 tail_inbound_bubbles 全量入库，
        # 导致历史气泡在后续轮询中依次被当作"新候选"返回并触发自动回复。
        baseline_signatures = self._merge_processed_inbound_bubble_signatures(
            state,
            new_signature=str(conv.get("inbound_bubble_signature", "") or "").strip(),
        )
        if isinstance(active_snapshot, dict):
            tail_signatures = self._collect_tail_inbound_bubble_signatures(
                active_snapshot,
                customer_name=customer_name,
                state=state,
            )
            if tail_signatures:
                existing = set(self._normalize_processed_inbound_bubble_signatures(baseline_signatures))
                for sig in tail_signatures:
                    if sig and sig not in existing:
                        baseline_signatures.append(sig)
                        existing.add(sig)
        return self._build_conversation_state(
            customer_name,
            content=content,
            current_time=current_time,
            sent_by_us=False,
            unread_count=unread_count,
            existing_state=state,
            customer_id=conv.get('customer_id', ''),
            conversation_id=conv.get('conversation_id', ''),
            user_last_content=content,
            dom_unread_at_send=0,
            last_detected_unread=unread_count if last_detected_unread is None else last_detected_unread,
            inbound_signal_source=conv.get("inbound_signal_source", "conversation_preview"),
            preview_signature=conv.get("preview_signature", ""),
            active_snapshot_signature=conv.get("active_snapshot_signature", ""),
            processed_inbound_bubble_signatures=baseline_signatures,
        )

    def _should_use_active_preview_fallback(
        self,
        *,
        is_active_conversation: bool,
        active_snapshot_matches: bool,
        active_snapshot_name: str = "",
        content: str,
        unread_count: int,
        last_detected_unread: int,
        preview_signature: str,
        last_preview_signature: str,
        last_content: str = "",
        active_snapshot_last_bubble_inbound: bool,
        active_snapshot_tail_inbound_count: int,
        was_sent_by_us: bool = False,
        last_user_content: str = "",
    ) -> bool:
        """统一判断活动会话是否还能安全使用 preview fallback。"""
        return self._evaluate_preview_fallback_decision(
            mode="active",
            is_active_conversation=is_active_conversation,
            active_snapshot_matches=active_snapshot_matches,
            active_snapshot_name=active_snapshot_name,
            content=content,
            unread_count=unread_count,
            last_detected_unread=last_detected_unread,
            preview_signature=preview_signature,
            last_preview_signature=last_preview_signature,
            last_content=last_content,
            last_user_content=last_user_content,
            active_snapshot_last_bubble_inbound=active_snapshot_last_bubble_inbound,
            active_snapshot_tail_inbound_count=active_snapshot_tail_inbound_count,
            was_sent_by_us=was_sent_by_us,
        ) is not None

    def _evaluate_preview_fallback_decision(
        self,
        *,
        mode: str,
        is_active_conversation: bool,
        content: str,
        unread_count: int,
        last_detected_unread: int,
        preview_signature: str,
        last_preview_signature: str,
        last_content: str,
        last_user_content: str,
        active_snapshot_matches: bool = False,
        active_snapshot_name: str = "",
        active_snapshot_last_bubble_inbound: bool = False,
        active_snapshot_tail_inbound_count: int = 0,
        was_sent_by_us: bool = False,
    ) -> Optional[InboundDecisionResult]:
        """统一评估 preview fallback，按活动/非活动策略返回显式决策结果。"""
        decision = self._get_inbound_decision_engine().evaluate_preview_fallback_decision(
            mode=mode,
            is_active_conversation=is_active_conversation,
            content=content,
            unread_count=unread_count,
            last_detected_unread=last_detected_unread,
            preview_signature=preview_signature,
            last_preview_signature=last_preview_signature,
            last_content=last_content,
            last_user_content=last_user_content,
            active_snapshot_matches=active_snapshot_matches,
            active_snapshot_name=active_snapshot_name,
            active_snapshot_last_bubble_inbound=active_snapshot_last_bubble_inbound,
            active_snapshot_tail_inbound_count=active_snapshot_tail_inbound_count,
            was_sent_by_us=was_sent_by_us,
        )
        if decision is None:
            return None
        return self._make_inbound_decision_result(**decision)

    def _handle_existing_repeat_inbound(
        self,
        *,
        conv: Dict[str, Any],
        state: Dict[str, Any],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        last_content: str,
        was_sent_by_us: bool,
        last_detected_unread: int,
        user_last_content: str,
    ) -> Optional[tuple[str, str, int]]:
        """统一处理已有状态下的入站判定。

        简化原则：内容已见过 → 跳过；内容真正不同 → 放行。
        不再依赖 snapshot_advanced（不可靠：我方回复后 DOM 刷新也会导致签名变化，
        误判为"新消息"），改为纯内容+未读数判定。

        ┌──────────┬─────────────────────────────────────────────────────┐
        │ 路径     │ 触发条件                                            │
        ├──────────┼─────────────────────────────────────────────────────┤
        │ 1 skip   │ 我方已回复 + 内容与用户上次或我方上次相同 → 跳过    │
        │ 2 skip   │ 未读未增长 + 内容已见过 → 跳过                      │
        │ 3 emit   │ 内容与所有已知消息不同 → 真新消息，放行              │
        └──────────┴─────────────────────────────────────────────────────┘
        """
        same_as_self = bool(last_content) and content == last_content
        same_as_user = bool(user_last_content) and content == user_last_content
        content_already_seen = same_as_self or same_as_user

        # 修复：增加模糊匹配，防止 DOM 预览偏移（显示更早的消息）导致 content_already_seen=False
        # 当内容与已知消息高度相似（>0.8）时，也视为"已见过"
        if not content_already_seen and (last_content or user_last_content):
            try:
                from difflib import SequenceMatcher
                if last_content and len(content) >= 2 and len(last_content) >= 2:
                    ratio = SequenceMatcher(None, content, last_content).ratio()
                    if ratio >= 0.8:
                        same_as_self = True
                        content_already_seen = True
                if not content_already_seen and user_last_content and len(content) >= 2 and len(user_last_content) >= 2:
                    ratio = SequenceMatcher(None, content, user_last_content).ratio()
                    if ratio >= 0.8:
                        same_as_user = True
                        content_already_seen = True
            except Exception:
                pass

        # 路径 1: 我方已回复 + 内容与已处理消息相同 → 跳过
        # 无论 snapshot_advanced 与否：我方已回复说明上一轮已处理完毕，
        # 内容相同说明没有新消息，只是 DOM 刷新的回显
        # 修复：当 unread_count == 0 时，不立即 skip，交给 _handle_existing_no_unread_state
        # 通过深度核实确认是否为真实新消息（场景：用户重发同内容消息但未读徽标未刷新）
        if was_sent_by_us and content_already_seen:
            if unread_count == 0:
                return None  # 交给第二道 _handle_existing_no_unread_state 做深度核实
            reason = "与我方回复相同" if same_as_self else "与用户上次消息相同"
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=unread_count,
                log_message=(
                    f"[状态机] {customer_name}: 我方已回复+内容{reason}，"
                    f"跳过(回复后回显，unread={unread_count})"
                ),
            )

        # 路径 2: 未读未增长 + 内容已见过
        # 修复 C1：当 unread_count == 0 时，不立即 skip，交给 _handle_existing_no_unread_state
        # 通过 has_new_evidence 检查决定是否触发深度核实，避免漏掉 active_snapshot_queue 中的真实新消息
        # （场景：用户已发新消息但未读徽标未刷新，DOM 预览仍显示旧内容导致 content_already_seen=True）
        if content_already_seen and unread_count <= last_detected_unread:
            if unread_count == 0:
                return None  # 交给第二道 _handle_existing_no_unread_state 做 has_new_evidence 检查
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=unread_count,
                log_message=(
                    f"[状态机] {customer_name}: 内容已见过+未读未增长"
                    f"({unread_count}<={last_detected_unread})，跳过(已检测过)"
                ),
            )

        # 路径 3: 内容与所有已知消息不同 → 真新消息
        # 修复 P1：当 unread_count == 0 时，不直接 emit，交给 _handle_existing_no_unread_state
        # 通过深度核实确认真实消息后更新 inbound_signal_source，避免终局守卫误拦
        # （场景：DOM 预览偏移显示更早消息导致 content_already_seen=False，但实际无新消息）
        if not content_already_seen:
            if unread_count == 0:
                return None  # 交给第二道 _handle_existing_no_unread_state 做深度核实
            logger.info(
                f"[状态机] {customer_name}: 检测到新内容(未读={unread_count})，判定为用户新消息"
            )
            return "emit", content, unread_count

        # 兜底: 内容已见过但未读增长了 → 可能是用户重发同内容消息
        # 注：was_sent_by_us 场景已被路径1（was_sent_by_us and content_already_seen）覆盖，此处 was_sent_by_us 必为 False
        logger.info(
            f"[状态机] {customer_name}: 内容已见过但未读增长({unread_count}>{last_detected_unread})，"
            f"判定为用户重发同内容消息"
        )
        return "emit", content, unread_count

    def _handle_existing_inbound_state(
        self,
        *,
        conv: Dict[str, Any],
        state: Dict[str, Any],
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
    ) -> tuple[str, str, int]:
        """处理已有状态的入站会话。

        收敛：原 _handle_existing_changed_content（包装层）已 inline。
        判定顺序：repeat_inbound -> no_unread -> active_verify -> emit。
        """
        customer_name = conv.get('customer_name', '')
        last_content = state.get('last_content', '')
        was_sent_by_us = state.get('sent_by_us', False)
        last_detected_unread = state.get('last_detected_unread', 0)
        user_last_content = state.get('user_last_content', '')
        time_since_last = current_time - state.get('last_time', 0)

        # 第一道：4 路径核心判断（最常用）
        repeat_result = self._handle_existing_repeat_inbound(
            conv=conv,
            state=state,
            customer_name=customer_name,
            content=content,
            unread_count=unread_count,
            current_time=current_time,
            last_content=last_content,
            was_sent_by_us=was_sent_by_us,
            last_detected_unread=last_detected_unread,
            user_last_content=user_last_content,
        )
        if repeat_result is not None:
            return repeat_result

        # 第二道：无未读场景
        if unread_count == 0:
            return self._handle_existing_no_unread_state(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                active_snapshot=active_snapshot,
                last_content=last_content,
                user_last_content=user_last_content,
                was_sent_by_us=was_sent_by_us,
                time_since_last=time_since_last,
            )

        # 第三道：活动会话 + 有未读 -> 深度核实（防 DOM 抖动重复触发）
        if bool(conv.get("is_active", False)):
            decision, res_content, res_unread = self._handle_active_conversation_verify(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                active_snapshot=active_snapshot,
                last_content=last_content,
                user_last_content=user_last_content,
                was_sent_by_us=was_sent_by_us,
            )
            if decision != "pass":
                return decision, res_content, res_unread

        # 兜底：emit
        return "emit", content, unread_count

    def _handle_active_conversation_verify(
        self,
        *,
        conv: Dict[str, Any],
        state: Dict[str, Any],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
        last_content: str,
        user_last_content: str,
        was_sent_by_us: bool,
    ) -> tuple[str, str, int]:
        """对活动会话进行深度核实（无论是否有未读）。"""
        # 收敛：单点定义 last_ref（was_sent_by_us -> user_last_content else last_content）
        last_ref = self._resolve_last_reference(state)
        last_detected_unread = int(state.get("last_detected_unread", 0) or 0)

        resolved_status, actual_content, signal_source, bubble_signature = self._classify_resolved_inbound_content(
            customer_name,
            last_reference=last_ref,
            is_active=True,
            active_snapshot=active_snapshot,
            state=state,
        )
        active_snapshot_signature = self._build_active_snapshot_signature(
            active_snapshot,
            customer_name=customer_name,
        )
        processed_bubble_signatures = set(
            self._normalize_processed_inbound_bubble_signatures(
                (state or {}).get("processed_inbound_bubble_signatures", [])
            )
        )
        bubble_advanced = bool(
            bubble_signature and str(bubble_signature).strip() not in processed_bubble_signatures
        )
        snapshot_advanced = self._is_snapshot_advanced(conv, state)

        if resolved_status == "new" and actual_content:
            normalized_actual = self._normalize_preview_message_reference(actual_content)
            normalized_last_ref = self._normalize_preview_message_reference(last_ref)
            if (
                was_sent_by_us
                and normalized_actual
                and normalized_actual == normalized_last_ref
                and unread_count <= last_detected_unread
            ):
                logger.info(
                    f"[状态机] {customer_name}: 活动会话深度核实命中旧用户末条，"
                    f"未读未增长({unread_count}<={last_detected_unread})，跳过重复放行"
                )
                if state is not None and current_time is not None:
                    self._touch_state(
                        state,
                        content=content,
                        current_time=current_time,
                        unread_count=unread_count,
                        active_snapshot_signature=active_snapshot_signature or None,
                        processed_inbound_bubble_signatures=(
                            self._merge_processed_inbound_bubble_signatures(
                                state,
                                new_signature=bubble_signature,
                            )
                            if bubble_signature
                            else None
                        ),
                    )
                return self._decision_tuple(
                    self._make_inbound_decision_result(
                        action="skip",
                        content=content,
                        unread_count=unread_count,
                        evidence="reject",
                    )
                )
            if bubble_signature:
                conv["inbound_bubble_signature"] = bubble_signature
            logger.info(f"[状态机] {customer_name}: 活动会话(unread={unread_count})深度核实确认到真实用户新消息，放行")
            return self._emit_resolved_inbound_content(
                conv=conv,
                actual_content=actual_content,
                signal_source=signal_source,
                unread_count=unread_count,
            )

        if resolved_status == "same":
            # 收敛：单一"是否有新证据"判断，三类证据取并集：
            #   unread_advanced（unread 增长）/ bubble_advanced（气泡签名新）/ snapshot_advanced（DOM 推进）
            has_new_evidence = bool(
                unread_count > last_detected_unread
                or bubble_advanced
                or snapshot_advanced
            )
            if has_new_evidence:
                logger.info(
                    f"[状态机] {customer_name}: 活动会话(unread={unread_count})深度核实末条内容相同，"
                    "但已发现新的未读/气泡/快照证据，按用户待回复消息放行"
                )
                if bubble_signature:
                    conv["inbound_bubble_signature"] = bubble_signature
                if active_snapshot_signature:
                    conv["active_snapshot_signature"] = active_snapshot_signature
                return self._emit_resolved_inbound_content(
                    conv=conv,
                    actual_content=actual_content or content,
                    signal_source=signal_source,
                    unread_count=unread_count,
                )
            logger.info(f"[状态机] {customer_name}: 活动会话(unread={unread_count})深度核实确认末条无变化，跳过")
            if state is not None and current_time is not None:
                self._touch_state(
                    state,
                    current_time=current_time,
                    unread_count=unread_count,
                    active_snapshot_signature=active_snapshot_signature or None,
                )
            return self._decision_tuple(
                self._make_inbound_decision_result(
                    action="skip",
                    content=content,
                    unread_count=unread_count,
                    evidence="reject",
                )
            )

        # missing 场景：没确认到气泡，只能在满足既有 preview fallback 安全条件时兜底。
        if unread_count > 0:
            snapshot_name = self._normalize_customer_name((active_snapshot or {}).get("customer_name", ""))
            normalized_customer_name = self._normalize_customer_name(customer_name)
            preview_fallback_decision = self._evaluate_preview_fallback_decision(
                mode="active",
                is_active_conversation=True,
                active_snapshot_matches=bool(
                    snapshot_name
                    and normalized_customer_name
                    and snapshot_name == normalized_customer_name
                ),
                active_snapshot_name=snapshot_name,
                content=content,
                unread_count=unread_count,
                last_detected_unread=int(state.get("last_detected_unread", 0) or 0),
                preview_signature=str(conv.get("preview_signature", "") or ""),
                last_preview_signature=str(state.get("preview_signature", "") or ""),
                last_content=str(last_content or ""),
                active_snapshot_last_bubble_inbound=bool(
                    (active_snapshot or {}).get("last_bubble_is_inbound", False)
                ),
                active_snapshot_tail_inbound_count=len(
                    list((active_snapshot or {}).get("tail_inbound_bubbles") or [])
                ),
                was_sent_by_us=was_sent_by_us,
                last_user_content=str(user_last_content or "").strip(),
            )
            if preview_fallback_decision is not None:
                logger.info(
                    f"[状态机] {customer_name}: 活动会话深度核实 missing 但满足 preview fallback 条件，"
                    f"继续使用预览内容兜底(unread={unread_count})"
                )
                return self._decision_tuple(preview_fallback_decision)

        logger.info(f"[状态机] {customer_name}: 活动会话深度核实未确认到新消息")
        if state is not None and current_time is not None:
            self._touch_state(state, content=content, current_time=current_time, unread_count=unread_count)
        return self._decision_tuple(
            self._make_inbound_decision_result(
                action="skip",
                content=content,
                unread_count=unread_count,
                evidence="reject",
            )
        )

    def _handle_existing_no_unread_state(
        self,
        *,
        conv: Dict[str, Any],
        state: Dict[str, Any],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
        last_content: str,
        user_last_content: str,
        was_sent_by_us: bool,
        time_since_last: float,
    ) -> tuple[str, str, int]:
        """统一处理已有状态下 `unread_count == 0` 的场景。

        收敛：无未读时统一计算新证据标志（预览变化/内容变化/快照推进），
        无新证据则直接跳过深度核实，避免点击会话/查找对话框等昂贵 DOM 操作。
        """
        is_active_conversation = bool(conv.get('is_active', False))
        preview_signature = str(conv.get("preview_signature", "") or "").strip()
        last_preview_signature = str(state.get("preview_signature", "") or "").strip()
        normalized_content = self._normalize_preview_message_reference(content)
        normalized_last_content = self._normalize_preview_message_reference(last_content)
        normalized_last_user = self._normalize_preview_message_reference(user_last_content)

        # 统一计算新证据标志，三路共用，避免无新消息时触发深度核实
        has_preview_change = bool(
            preview_signature and preview_signature != last_preview_signature
        )
        has_content_change = bool(
            normalized_content
            and normalized_content != normalized_last_content
            and normalized_content != normalized_last_user
        )
        snapshot_advanced = self._is_snapshot_advanced(conv, state)
        # 检查 API 拦截证据：非活动会话无未读时，API 拦截可能是唯一新消息信号源
        has_api_evidence = False
        try:
            api_message, _, _ = self._resolve_api_inbound_message_for_conversation(
                customer_name,
                state=state,
                last_reference=self._resolve_last_reference(state),
            )
            has_api_evidence = bool(api_message)
        except Exception:
            pass
        has_new_evidence = has_preview_change or has_content_change or snapshot_advanced or has_api_evidence

        if is_active_conversation:
            if not has_new_evidence:
                return self._skip_inbound_with_state(
                    customer_name=customer_name,
                    state=state,
                    content=content,
                    unread_count=unread_count,
                    log_message=(
                        f"[状态机] {customer_name}: 活动会话无未读且无新证据"
                        "(内容/预览/快照均无变化)，跳过深度核实"
                    ),
                    level="info",
                    touch_state=True,
                    current_time=current_time,
                )
            return self._handle_active_conversation_verify(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                active_snapshot=active_snapshot,
                last_content=last_content,
                user_last_content=user_last_content,
                was_sent_by_us=was_sent_by_us,
            )

        if was_sent_by_us:
            # 当内容与用户上次消息相同时，用户可能重发了同内容消息，
            # has_new_evidence 无法检测此场景（内容/预览均无变化），必须深度核实确认
            is_repeated_user_content = bool(
                normalized_content and normalized_content == normalized_last_user
            )
            if not has_new_evidence and not is_repeated_user_content:
                return self._skip_inbound_with_state(
                    customer_name=customer_name,
                    state=state,
                    content=content,
                    unread_count=unread_count,
                    log_message=(
                        f"[状态机] {customer_name}: 我方刚回复后无未读且无新证据"
                        "(内容/预览/快照均无变化)，跳过深度核实"
                    ),
                    level="info",
                    touch_state=True,
                    current_time=current_time,
                )
            return self._resolve_classified_inbound_action(
                conv=conv,
                customer_name=customer_name,
                unread_count=unread_count,
                # 我方刚回复后，无未读场景下应拿"用户上一条真实消息"做参考，
                # 不能把我方刚发的回复当成新入站对比基准，否则旧用户消息会被误判成新消息再次回复。
                last_reference=self._resolve_last_reference(state),
                is_active=False,
                active_snapshot=active_snapshot,
                success_log=(
                    f"[状态机] {customer_name}: 我方刚回复后虽无未读徽标，"
                    "但会话内已确认到新的真实用户消息，继续放行"
                ),
                same_log=(
                    f"[状态机] {customer_name}: 我方刚回复后无未读，"
                    "深度核实确认末条仍是旧用户消息，跳过"
                ),
                missing_log=(
                    f"[状态机] {customer_name}: 我方刚回复后无未读，"
                    "未在会话内确认到新的真实用户消息，跳过"
                ),
                state=state,
                current_time=current_time,
                skip_content=content,
            )

        # 非活动 + 无未读：无新证据直接跳过
        if not has_new_evidence:
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=unread_count,
                log_message=(
                    f"[状态机] {customer_name}: 非活动会话且无未读+距上次{time_since_last:.0f}s，"
                    "跳过自动回复(仅更新状态，等待真实用户消息信号)"
                ),
                level="info",
                touch_state=True,
                current_time=current_time,
            )

        return self._resolve_classified_inbound_action(
            conv=conv,
            customer_name=customer_name,
            unread_count=unread_count,
            last_reference=self._resolve_last_reference(state),
            is_active=False,
            active_snapshot=active_snapshot,
            success_log=(
                f"[状态机] {customer_name}: 非活动会话虽无未读徽标，"
                "但已确认到新的真实用户消息，继续放行"
            ),
            same_log=(
                f"[状态机] {customer_name}: 非活动会话无未读，"
                "深度核实确认末条仍是旧用户消息，跳过"
            ),
            missing_log=(
                f"[状态机] {customer_name}: 非活动会话无未读，"
                "未在会话内确认到新的真实用户消息，继续等待真实信号"
            ),
            state=state,
            current_time=current_time,
            skip_content=content,
        )

    def _handle_outbound_or_debounce(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        direction: MessageDirection,
        current_time: float,
    ) -> str:
        """处理出站预览与短间隔防抖。返回 `skip` 或 `pass`。"""
        if direction == MessageDirection.OUTBOUND:
            existing_state = self._states.get(customer_name, {})
            self._clear_dirty_unread(existing_state)
            self._states[customer_name] = self._build_conversation_state(
                customer_name,
                content=content,
                current_time=current_time,
                sent_by_us=True,
                unread_count=0,
                existing_state=existing_state,
                customer_id=conv.get('customer_id', ''),
                conversation_id=conv.get('conversation_id', ''),
            )
            return "skip"

        if state and (current_time - state['last_time']) < 1:
            logger.debug(f"[状态机] {customer_name}: 防抖跳过(间隔<1s)")
            return "skip"

        return "pass"

    def mark_outbound_sent(self, customer_name: str, content: str) -> None:
        """外部调用：出站发送成功后主动标记 sent_by_us=True。

        DOM 轮询无法可靠识别出站方向（抖音会话列表预览无方向标记），
        因此需要发送侧在成功后主动通知状态机，避免自回复回显被误判为新消息。
        """
        normalized = self._normalize_customer_name(customer_name)
        if not normalized:
            return
        current_time = time.time()
        with self._state_lock:
            existing_state = self._states.get(normalized, {})
            self._clear_dirty_unread(existing_state)
            self._states[normalized] = self._build_conversation_state(
                normalized,
                content=content,
                current_time=current_time,
                sent_by_us=True,
                unread_count=0,
                existing_state=existing_state,
                customer_id=existing_state.get("customer_id", ""),
                conversation_id=existing_state.get("conversation_id", ""),
            )
        logger.info(
            f"[状态机] {normalized}: 外部标记sent_by_us=True, content={content[:30]}..."
        )

    def _normalize_recent_self_reply_unread(
        self,
        *,
        customer_name: str,
        state: Optional[Dict[str, Any]],
        content: str,
        unread_count: int,
        current_time: float,
    ) -> int:
        """归一化我方刚回复后的 DOM 未读数，避免自回复回显被误判成用户新消息。

        收敛说明：仅保留精确匹配（content == last_sent_content）。
        前缀匹配启发式已删除——_handle_existing_repeat_inbound 路径1 已覆盖
        精确匹配场景，前缀匹配可能导致误判（用户消息前缀与我方回复相同但内容不同）。
        保留此方法是为了在状态机入口前提前归零 unread，避免后续不必要的深度核实。
        """
        if not state or not state.get('sent_by_us') or unread_count <= 0:
            return unread_count

        time_since_sent = current_time - state.get('last_time', 0)
        last_sent_content = state.get('last_content', '')
        if content == last_sent_content and time_since_sent < 60:
            logger.info(f"[状态机] {customer_name}: 我方已回复({time_since_sent:.0f}s前)且DOM内容与我方回复相同，强制unread_count=0(自回复回显)")
            return 0

        return unread_count

    def _handle_bot_preview(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
    ) -> tuple[str, str, int]:
        """处理 DOM 预览命中 BOT 模式的场景。"""
        if not self._is_bot_reply_content(content):
            return "pass", content, unread_count

        if unread_count == 0:
            self._ensure_bot_mode_state(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
            )
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=unread_count,
                log_message=f"[状态机] {customer_name}: BOT模式+无未读，跳过",
            )

        last_detected = state.get('last_detected_unread', 0) if state else 0
        if unread_count <= last_detected:
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=unread_count,
                log_message=f"[状态机] {customer_name}: BOT模式匹配+有未读({unread_count})但未增加(上次={last_detected})，跳过(已检测过)",
            )

        logger.info(f"[状态机] {customer_name}: BOT模式匹配但有未读({unread_count})且增加(上次={last_detected})，DOM显示的是系统回复，需要获取用户实际消息")
        return self._resolve_bot_preview_actual_content(
            conv=conv,
            state=state,
            customer_name=customer_name,
            content=content,
            unread_count=unread_count,
            current_time=current_time,
            active_snapshot=active_snapshot,
        )

    def _is_bot_reply_content(self, content: str) -> bool:
        """统一判断 content 是否命中 BOT 模式（字符串或正则）。"""
        if any(pattern in content for pattern in self.BOT_REPLY_PATTERNS):
            return True
        return any(re.search(pat, content) for pat in self.BOT_REPLY_REGEX_PATTERNS)

    def _ensure_bot_mode_state(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
    ) -> None:
        """BOT 模式命中后，确保 state 已更新/已建立。

        收敛前：state 存在/不存在两条分支内联在 _handle_bot_preview 中，重复调用。
        收敛后：单方法。
        """
        if state:
            self._clear_dirty_unread(state)
            self._touch_state(
                state,
                content=content,
                current_time=current_time,
                sent_by_us=False,
                unread_count=unread_count,
            )
            return
        self._states[customer_name] = self._build_conversation_state(
            customer_name,
            content=content,
            current_time=current_time,
            sent_by_us=False,
            unread_count=unread_count,
            customer_id=conv.get('customer_id', ''),
            conversation_id=conv.get('conversation_id', ''),
            user_last_content=None,
            dom_unread_at_send=0,
            last_detected_unread=0,
        )


    def _handle_initial_or_stateless_conversation(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """处理首次加载和无状态会话。返回 `skip` 或 `pass`。"""
        if not self._first_fetch_done:
            is_active_conversation = bool(conv.get("is_active", False))
            if unread_count > 0 and state and is_active_conversation:
                last_reference = str(
                    state.get("user_last_content")
                    or state.get("last_user_message")
                    or state.get("last_content")
                    or ""
                ).strip()
                resolved_status, actual_content, signal_source, bubble_signature = self._classify_resolved_inbound_content(
                    customer_name,
                    last_reference=last_reference,
                    is_active=True,
                    active_snapshot=active_snapshot,
                )
                if resolved_status == "new" and actual_content:
                    conv["inbound_signal_source"] = signal_source or "resolved_inbound"
                    if bubble_signature:
                        conv["inbound_bubble_signature"] = bubble_signature
                    logger.info(
                        f"[状态机] {customer_name}: 首次加载命中活动会话新消息，"
                        "跳过未读基线，直接按真实末条放行"
                    )
                    conv["last_message_content"] = actual_content
                    return "pass"

            self._states[customer_name] = self._build_inbound_baseline_state(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                active_snapshot=active_snapshot,
            )
            if unread_count > 0:
                logger.info(
                    f"[状态机] {customer_name}: 首次加载但有未读消息({unread_count})，"
                    "仅建立未读基线，本轮不立即自动回复"
                )
                return "skip"

            logger.debug(f"[状态机] {customer_name}: 首次加载，只记录状态不触发消息(unread={unread_count})")
            return "skip"

        if unread_count == 0 and not state:
            logger.debug(f"[状态机] {customer_name}: 新会话但无未读消息，跳过")
            self._states[customer_name] = self._build_inbound_baseline_state(
                conv=conv,
                state=state,
                customer_name=customer_name,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                last_detected_unread=0,
                active_snapshot=active_snapshot,
            )
            return "skip"

        return "pass"

    def _resolve_conversation_emit_action(
        self,
        *,
        conv: Dict[str, Any],
        state: Optional[Dict[str, Any]],
        customer_name: str,
        content: str,
        direction: MessageDirection,
        unread_count: int,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
    ) -> tuple[str, str, int]:
        """统一编排单个会话在发消息前的过滤链。"""
        unread_count = self._normalize_recent_self_reply_unread(
            customer_name=customer_name,
            state=state,
            content=content,
            unread_count=unread_count,
            current_time=current_time,
        )

        precheck_action = self._handle_outbound_or_debounce(
            conv=conv,
            state=state,
            customer_name=customer_name,
            content=content,
            direction=direction,
            current_time=current_time,
        )
        if precheck_action == "skip":
            return "skip", content, unread_count

        bot_action, content, unread_count = self._handle_bot_preview(
            conv=conv,
            state=state,
            customer_name=customer_name,
            content=content,
            unread_count=unread_count,
            current_time=current_time,
            active_snapshot=active_snapshot,
        )
        if bot_action == "skip":
            return "skip", content, unread_count

        if state:
            self._clear_dirty_unread(state)
            decision, content, unread_count = self._handle_existing_inbound_state(
                conv=conv,
                state=state,
                content=content,
                unread_count=unread_count,
                current_time=current_time,
                active_snapshot=active_snapshot,
            )
            if decision == "skip":
                return "skip", content, unread_count

        initial_action = self._handle_initial_or_stateless_conversation(
            conv=conv,
            state=state,
            customer_name=customer_name,
            content=content,
            unread_count=unread_count,
            current_time=current_time,
            active_snapshot=active_snapshot,
        )
        if initial_action == "skip":
            return self._decision_tuple(
                self._make_inbound_decision_result(
                    action="skip",
                    content=content,
                    unread_count=unread_count,
                    evidence="reject",
                )
            )

        inbound_signal_source = str(conv.get("inbound_signal_source", "") or "conversation_preview").strip()
        if unread_count <= 0 and inbound_signal_source == "conversation_preview":
            return self._skip_inbound_with_state(
                customer_name=customer_name,
                state=state,
                content=content,
                unread_count=0,
                log_message=(
                    f"[状态机] {customer_name}: 无未读且仅有左侧会话预览信号，"
                    "不直接放行自动回复，继续等待真实会话证据"
                ),
                level="info",
                touch_state=bool(state),
                current_time=current_time,
            )

        return self._decision_tuple(
            self._make_inbound_decision_result(
                action="emit",
                content=content,
                unread_count=unread_count,
                evidence=inbound_signal_source or "confirmed",
            )
        )

    def _ensure_chat_page(self) -> bool:
        """确保在聊天页面，支持自动导航恢复、页面关闭恢复和周期性URL验证"""
        if self._chat_page_ensured:
            with self._state_lock:
                self._ensure_verify_counter += 1
            try:
                if self.page and not self.page.is_closed():
                    current_url = self.page.url or ""
                    if "/chat" in current_url and "douyin.com" in current_url:
                        return True
                    logger.warning(f"页面偏离聊天页面: {current_url[:50]}，重置标志")
                    self._chat_page_ensured = False
                else:
                    self._chat_page_ensured = False
                    self._try_recover_closed_page()
            except Exception:
                self._chat_page_ensured = False
        try:
            if self.page and not self.page.is_closed():
                current_url = self.page.url or ""
                if "/chat" in current_url and "douyin.com" in current_url:
                    self._chat_page_ensured = True
                    return True

                if self._monitoring:
                    logger.info(f"页面不在聊天页面({current_url[:50]})，尝试导航恢复...")
                    try:
                        self.page.goto(DOUYIN_CHAT_URL, timeout=15000)
                        self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                        self.page.wait_for_timeout(1000)
                        new_url = self.page.url or ""
                        if "/chat" in new_url and "douyin.com" in new_url:
                            self._chat_page_ensured = True
                            self._setup_mutation_observer()
                            logger.info("页面导航恢复成功，已回到聊天页面")
                            return True
                        else:
                            logger.warning(f"导航后页面URL不匹配: {new_url[:50]}")
                    except Exception as nav_e:
                        logger.warning(f"导航到聊天页面失败: {nav_e}")
                        self._chat_page_ensured = False
            else:
                self._try_recover_closed_page()
        except Exception as e:
            logger.debug(f"确保聊天页面失败: {e}")
        return False

    def _try_recover_closed_page(self):
        """尝试从浏览器上下文中恢复已关闭的页面（带指数退避和最大重试限制）

        退避策略：2s -> 4s -> 8s -> 16s -> 30s(上限)
        超过10次连续失败后停止重试并通知上层浏览器已断开
        """
        current_time = time.time()
        if not hasattr(self, '_recovery_attempt_count'):
            self._recovery_attempt_count = 0
            self._last_recovery_attempt_time = 0

        max_recovery_attempts = 10
        if self._recovery_attempt_count >= max_recovery_attempts:
            if not hasattr(self, '_recovery_notified') or not self._recovery_notified:
                logger.error(f"浏览器页面恢复连续失败{max_recovery_attempts}次，停止重试")
                self._recovery_notified = True
                if self._state_callback:
                    try:
                        self._state_callback(PageState.EXCEPTION)
                    except Exception:
                        pass
            return

        min_interval = min(2 * (2 ** self._recovery_attempt_count), 30)
        if current_time - self._last_recovery_attempt_time < min_interval:
            return

        self._last_recovery_attempt_time = current_time
        self._recovery_attempt_count += 1

        try:
            if not self.browser_context:
                return
            
            pages = self.browser_context.pages
            for page in pages:
                if not page.is_closed():
                    try:
                        url = page.url or ""
                        if "douyin.com" in url and "/chat" in url:
                            with self._state_lock:
                                self.page = page
                                self._chat_page_ensured = True
                                self._observer_active = False
                            logger.info(f"从浏览器上下文中恢复聊天页面: {url[:50]}")
                            self._recovery_attempt_count = 0
                            self._recovery_notified = False
                            try:
                                self._setup_mutation_observer()
                                logger.info("恢复页面后已重新注入MutationObserver")
                            except Exception as obs_e:
                                logger.warning(f"恢复页面后重新注入Observer失败: {obs_e}")
                            return
                    except Exception:
                        continue
            
            logger.warning("浏览器上下文中无聊天页面，尝试创建新页面")
            try:
                new_page = self.browser_context.new_page()
                new_page.goto(DOUYIN_CHAT_URL, timeout=15000)
                with self._state_lock:
                    self.page = new_page
                    self._chat_page_ensured = True
                    self._observer_active = False
                logger.info("已创建新页面并导航到聊天页面")
                self._recovery_attempt_count = 0
                self._recovery_notified = False
                try:
                    self._setup_mutation_observer()
                    logger.info("新页面已注入MutationObserver")
                except Exception as obs_e:
                    logger.warning(f"新页面注入Observer失败: {obs_e}")
            except Exception as create_e:
                logger.error(f"创建新页面失败: {create_e}")
        except Exception as e:
            logger.error(f"恢复关闭页面失败: {e}")

    def _fetch_conversations_from_dom(
        self,
    ) -> List[Dict]:
        """从DOM获取会话列表（备选方案）"""
        try:
            fetch_js = r"""
            async () => {
                const conversations = [];
                const selectors = [
                    '[data-e2e="conversation-item"]',
                    '.chat-list-item',
                    '[class*="conversationItem"]',
                    '[class*="chat-item"]',
                    '[class*="conversation"]',
                    '[class*="session"]'
                ];

                const normalizeCustomerName = (value) => {
                    const text = String(value || '').replace(/[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/g, '').trim();
                    if (!text) return '';
                    const tokens = text.split(/\s+/).filter(Boolean);
                    if (tokens.length <= 1) return text;
                    const trailingPattern = /^(刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d+)$/;
                    while (tokens.length > 1 && trailingPattern.test(tokens[tokens.length - 1])) {
                        tokens.pop();
                    }
                    return tokens.join(' ').trim() || text;
                };

                const findRenderedItems = () => {
                    const merged = [];
                    const seen = new Set();
                    for (const selector of selectors) {
                        const found = Array.from(document.querySelectorAll(selector));
                        for (const item of found) {
                            if (!item || seen.has(item)) continue;
                            seen.add(item);
                            merged.push(item);
                        }
                    }
                    return merged;
                };

                const parseItem = (item) => {
                    try {
                        const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                                       item.querySelector('[class*="title"]') ||
                                       item.querySelector('[class*="name"]');
                        let customerName = normalizeCustomerName(nameEl ? nameEl.textContent.trim() : '');

                        const fullText = item.innerText || '';
                        const lines = fullText.split('\\n').map(line => line.trim()).filter(Boolean);
                        const previewSignature = lines.slice(0, 4).join(' | ');
                        if (!customerName && lines.length > 0) {
                            customerName = normalizeCustomerName(lines[0]);
                        }

                        if (!customerName || customerName.length > 50) return null;

                        let lastMessage = '';
                        for (const line of lines) {
                            const trimmed = line.trim();
                            if (trimmed === customerName) continue;
                            if (/^\\d{1,2}:\\d{2}$/.test(trimmed)) continue;
                            if (/^\\d{4}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                            if (/^\\d{1,2}[/\\-]\\d{1,2}([/\\-]\\d{1,2})?$/.test(trimmed)) continue;
                            if (/^(昨天|刚刚|\\d+分钟前|\\d+小时前|\\d+天前|前天|今天|星期[一二三四五六日天])$/.test(trimmed)) continue;
                            if (/^\\d+$/.test(trimmed) && trimmed.length < 3) continue;
                            if (trimmed.length > 0) {
                                lastMessage = trimmed;
                                break;
                            }
                        }

                        let direction = 'inbound';
                        let directionConfidence = 'high';
                        const itemClassName = String(item.className || '').toLowerCase();
                        const selfMsgEl = item.querySelector('[class*="self-message"], [class*="msg-self"], [class*="outbound"], [class*="message-out"], [class*="chat-message-self"]');
                        if (selfMsgEl) {
                            direction = 'outbound';
                        }
                        const senderEl = item.querySelector('[class*="sender"]');
                        if (senderEl && (senderEl.textContent.trim() === '我' || senderEl.textContent.trim() === '我发送的')) {
                            direction = 'outbound';
                        }
                        if (lastMessage.startsWith('我:') || lastMessage.startsWith('我：')) {
                            direction = 'outbound';
                        }
                        if (direction === 'inbound' && lastMessage.startsWith('[自动回复]')) {
                            direction = 'outbound';
                        }
                        if (direction === 'inbound') {
                            directionConfidence = 'low';
                        }

                        let unreadCount = 0;
                        const unreadSelectors = [
                            '[class*="commonStreak"]',
                            '[class*="unread"]',
                            '[class*="badge"]',
                            '[class*="dot"]',
                            '[class*="msgCount"]',
                            '[class*="count"]'
                        ];
                        for (const sel of unreadSelectors) {
                            const unreadEl = item.querySelector(sel);
                            if (!unreadEl) continue;
                            const text = unreadEl.textContent.trim();
                            const num = parseInt(text);
                            if (!isNaN(num) && num > 0) {
                                unreadCount = num;
                                break;
                            }
                            if (unreadEl.offsetWidth > 0 && text.length > 0) {
                                unreadCount = 1;
                                break;
                            }
                        }

                        const isActive =
                            item.getAttribute('aria-selected') === 'true' ||
                            itemClassName.includes('curconversation') ||
                            itemClassName.includes('selected') ||
                            itemClassName.includes('active');

                        return {
                            customer_name: customerName,
                            customer_id:
                                item.getAttribute('data-user-id') ||
                                item.getAttribute('data-customer-id') ||
                                item.getAttribute('data-sec-uid') ||
                                item.dataset?.userId ||
                                item.dataset?.customerId ||
                                item.dataset?.secUid ||
                                '',
                            conversation_id:
                                item.getAttribute('data-conversation-id') ||
                                item.getAttribute('data-conv-id') ||
                                item.getAttribute('data-id') ||
                                item.dataset?.conversationId ||
                                item.dataset?.convId ||
                                '',
                            last_message_content: lastMessage,
                            preview_signature: previewSignature,
                            direction: direction === 'outbound' ? 'outbound' : (unreadCount > 0 ? 'inbound' : direction),
                            direction_confidence: direction === 'outbound' ? directionConfidence : (unreadCount > 0 ? 'medium' : directionConfidence),
                            unread_count: unreadCount,
                            is_active: isActive
                        };
                    } catch (e) {
                        return null;
                    }
                };

                const byKey = new Map();
                const scoreConversation = (parsed) => {
                    let score = 0;
                    if (parsed.is_active) score += 1000;
                    score += Math.max(parseInt(parsed.unread_count || 0, 10) || 0, 0) * 100;
                    if (parsed.conversation_id) score += 20;
                    if (parsed.customer_id) score += 10;
                    score += Math.min(String(parsed.last_message_content || '').length, 50);
                    return score;
                };
                const collectRendered = () => {
                    const items = findRenderedItems();
                    for (const item of items) {
                        const parsed = parseItem(item);
                        if (!parsed) continue;
                        const key = parsed.conversation_id || parsed.customer_id || parsed.customer_name;
                        const existing = byKey.get(key);
                        if (!existing || scoreConversation(parsed) >= scoreConversation(existing)) {
                            byKey.set(key, parsed);
                        }
                    }
                    return items;
                };

                collectRendered();

                conversations.push(...Array.from(byKey.values()));
                return conversations;
            }
            """
            result = self._safe_evaluate(fetch_js, timeout_ms=10000)
            return result if result else []
        except Exception as e:
            logger.debug(f"DOM获取会话失败: {e}")
            return []

    def _build_active_snapshot_fallback_conversation(
        self,
        *,
        active_customer_name: str,
        active_snapshot: Optional[Dict[str, Any]],
        raw_conversations: List[Dict[str, Any]],
        valid_conversations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """当左侧列表漏掉当前活动会话时，用右侧真实末条补入状态机。"""
        active_snapshot_name = self._normalize_customer_name((active_snapshot or {}).get("customer_name", ""))
        active_snapshot_message = str((active_snapshot or {}).get("last_inbound_message", "") or "").strip()
        if not (
            active_customer_name
            and active_snapshot_name == active_customer_name
            and active_snapshot_message
            and self._is_valid_message(active_snapshot_message)
        ):
            return None

        if any(
            self._normalize_customer_name(conv.get("customer_name", "")) == active_customer_name
            for conv in raw_conversations
        ):
            return None

        if any(
            self._normalize_customer_name(conv.get("customer_name", "")) == active_customer_name
            for conv in valid_conversations
        ):
            return None

        return {
            "customer_name": active_customer_name,
            "last_message_content": active_snapshot_message,
            "direction": "inbound",
            "inbound_signal_source": "active_snapshot",
            "unread_count": 0,
            "is_active": True,
            "customer_id": "",
            "conversation_id": build_conversation_id(active_customer_name, "douyin"),
        }

    def _collect_valid_conversations(
        self,
        conversations: List[Dict],
        *,
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """统一归一化左侧会话，并按需补入当前活动会话。"""
        active_customer_name = self._normalize_customer_name((active_snapshot or {}).get("customer_name", ""))
        valid_conversations: List[Dict[str, Any]] = []
        for conv in conversations:
            normalized_conv = self._normalize_dom_conversation_candidate(
                conv,
                active_customer_name=active_customer_name,
                active_snapshot=active_snapshot,
                current_time=current_time,
            )
            if normalized_conv:
                valid_conversations.append(normalized_conv)

        active_fallback_conv = self._build_active_snapshot_fallback_conversation(
            active_customer_name=active_customer_name,
            active_snapshot=active_snapshot,
            raw_conversations=conversations,
            valid_conversations=valid_conversations,
        )
        if active_fallback_conv:
            valid_conversations.append(active_fallback_conv)
            logger.info(
                f"fetch_messages: 活动会话兜底补入状态机 customer={active_customer_name} "
                f"content={str(active_fallback_conv.get('last_message_content', '') or '')[:30]}"
            )
        return valid_conversations

    def _parse_conversations(
        self,
        conversations: List[Dict],
        current_time: float,
        active_snapshot: Optional[Dict[str, Any]] = None,
    ) -> List[RPAMessage]:
        """解析会话列表，检测新消息
        
        简化版状态机 - 核心判定原则：
        抖音消息页面中，左边白色气泡=用户消息，右边蓝色气泡=系统回复。
        当用户有新消息（白色气泡）后面没有系统回复（蓝色气泡）时，触发自动回复。
        
        判定逻辑（优先级从高到低）：
        1. 出站消息(direction=outbound) → 只更新状态，不触发回复
        2. 入站消息 + 有未读(unread>0) → 用户新消息，触发回复
        3. 入站消息 + 内容变化 + 无未读 → 可能是页面刷新，跳过
        4. 入站消息 + 内容相同 → 跳过（已处理过）
        5. 防抖：1秒内重复跳过
        
        自回复防护：依赖当前会话状态(sent_by_us/unread_count)识别回显，
        不再维护额外发送缓存做业务判定。
        """
        messages: List[RPAMessage] = []

        valid_conversations = self._collect_valid_conversations(
            conversations,
            current_time=current_time,
            active_snapshot=active_snapshot,
        )

        with self._state_lock:
            for conv in valid_conversations:
                customer_name = self._normalize_customer_name(conv.get('customer_name', ''))
                content = conv.get('last_message_content', '')
                direction_str = conv.get('direction', 'inbound')
                direction = MessageDirection.INBOUND if direction_str == 'inbound' else MessageDirection.OUTBOUND
                unread_count = conv.get('unread_count', 0)

                state = self._states.get(customer_name)
                logger.debug(f"[状态机] customer={customer_name} dir={direction_str} unread={unread_count} content={content[:25]}... state={'exists' if state else 'NEW'}")

                action, content, unread_count = self._resolve_conversation_emit_action(
                    conv=conv,
                    state=state,
                    customer_name=customer_name,
                    content=content,
                    direction=direction,
                    unread_count=unread_count,
                    current_time=current_time,
                    active_snapshot=active_snapshot,
                )
                if action == "skip":
                    continue

                self._emit_dom_message(
                    customer_name=customer_name,
                    content=content,
                    direction=direction,
                    current_time=current_time,
                    unread_count=unread_count,
                    conv=conv,
                    state=state,
                    messages=messages,
                )

            self._finalize_first_fetch_and_trim(conversations, current_time)

        return messages

    def _extract_snapshot_message_for_customer(
        self,
        customer_name: str,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
        require_last_bubble_inbound: bool = False,
    ) -> Optional[str]:
        """从当前活动会话快照中提取指定客户的真实用户末条。"""
        active_snapshot = snapshot if isinstance(snapshot, dict) else self._get_active_conversation_snapshot()
        snapshot_name = self._normalize_customer_name(active_snapshot.get("customer_name", ""))
        normalized_customer_name = self._normalize_customer_name(customer_name)
        if snapshot_name != normalized_customer_name:
            return None

        if require_last_bubble_inbound and not bool(active_snapshot.get("last_bubble_is_inbound", False)):
            return None

        snapshot_message = str(active_snapshot.get("last_inbound_message", "") or "").strip()
        if snapshot_message and self._is_valid_message(snapshot_message):
            return snapshot_message
        return None

    def _snapshot_has_bubble_evidence(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        """判断右侧活动会话是否已拿到任何消息气泡证据。"""
        if not isinstance(snapshot, dict):
            return False
        return bool(
            int(snapshot.get("bubble_count", 0) or 0) > 0
            or str(snapshot.get("last_bubble_text", "") or "").strip()
            or str(snapshot.get("last_inbound_message", "") or "").strip()
            or list(snapshot.get("inbound_bubbles") or [])
            or list(snapshot.get("tail_inbound_bubbles") or [])
        )

    def _activate_conversation_for_fetch(self, customer_name: str) -> bool:
        """激活目标会话，优先复用当前活动会话，避免多余点击。

        fetch 场景禁用搜索框过滤（allow_search_filter=False），
        避免深度核实时写入搜索框导致搜索框一直处于打开状态。
        """
        if self._extract_snapshot_message_for_customer(
            customer_name,
            require_last_bubble_inbound=False,
        ) is not None:
            return True

        try:
            if self._is_target_conversation_active(customer_name):
                snapshot = self._get_active_conversation_snapshot()
                if self._snapshot_has_bubble_evidence(snapshot):
                    return True
                logger.info(f"目标会话已高亮但右侧气泡为空，尝试重新点击刷新: {customer_name}")
                return self._click_conversation(customer_name, allow_search_filter=False)
            return self._click_conversation(customer_name, allow_search_filter=False)
        except Exception as e:
            logger.debug(f"激活目标会话失败: {e}")
            return False

    def _wait_for_conversation_snapshot_message(
        self,
        customer_name: str,
        *,
        max_wait_ms: int = 1200,
        step_ms: int = 120,
    ) -> tuple[Optional[str], str]:
        """短轮询等待目标会话快照稳定，尽快返回真实用户末条。"""
        elapsed = 0
        retried_click = False
        while elapsed <= max_wait_ms:
            snapshot = self._get_active_conversation_snapshot()
            message = self._extract_snapshot_message_for_customer(
                customer_name,
                snapshot=snapshot,
                require_last_bubble_inbound=False,
            )
            if message:
                return message, "active_snapshot"

            snapshot_name = self._normalize_customer_name(snapshot.get("customer_name", ""))
            normalized_customer_name = self._normalize_customer_name(customer_name)
            if (
                snapshot_name == normalized_customer_name
                and not self._snapshot_has_bubble_evidence(snapshot)
                and not retried_click
                and max_wait_ms // 2 <= elapsed < max_wait_ms
            ):
                retried_click = True
                logger.info(f"目标会话已命中但右侧仍为空，等待期内再次点击刷新: {customer_name}")
                # 修复 R13：深度核实路径禁用搜索框过滤，避免搜索框残留
                self._click_conversation(customer_name, allow_search_filter=False)

            if elapsed >= max_wait_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms

        dom_snapshot = self._collect_chat_dom_snapshot(
            customer_name=customer_name,
            stage="wait_snapshot_message_fallback",
        )
        fallback_message = self._extract_inbound_message_from_dom_snapshot(dom_snapshot)
        if fallback_message:
            return fallback_message, "dom_snapshot"
        return None, ""

    def _fetch_actual_user_message(self, customer_name: str) -> tuple[Optional[str], str, str]:
        """确认最新一条气泡是否真的是用户消息，并补齐 bubble_signature。

        新链路：
        1. 先直接读取当前活动会话快照；
        2. 只有目标不是当前会话时才激活左侧会话；
        3. 激活后短轮询等待快照稳定，避免固定 sleep 阻塞。

        收敛变更：返回签名三元组 (message, source, bubble_signature)，
        避免下游 classify_resolved_inbound_content 拿到空 signature 后 dedup 失效。
        """
        try:
            direct_message = self._extract_snapshot_message_for_customer(customer_name)
            if direct_message:
                signature = self._build_active_snapshot_inbound_signature_for_text(
                    customer_name=customer_name,
                    text=direct_message,
                )
                return direct_message, "active_snapshot", signature

            activated = self._activate_conversation_for_fetch(customer_name)
            if not activated:
                return None, "", ""

            message, source = self._wait_for_conversation_snapshot_message(customer_name)
            if not message:
                return None, "", ""
            signature = self._build_active_snapshot_inbound_signature_for_text(
                customer_name=customer_name,
                text=message,
            )
            return message, source or "active_snapshot", signature
        except Exception as e:
            logger.debug(f"获取用户实际消息失败: {e}")
            return None, "", ""

    def _build_active_snapshot_inbound_signature_for_text(
        self,
        *,
        customer_name: str,
        text: str,
    ) -> str:
        """为 _fetch_actual_user_message 补齐 bubble_signature。"""
        normalized_text = self._normalize_preview_message_reference(text)
        if not normalized_text:
            return ""
        active_snapshot = self._get_active_conversation_snapshot()
        snapshot_name = self._normalize_customer_name(
            (active_snapshot or {}).get("customer_name", "")
        )
        target_name = self._normalize_customer_name(customer_name)
        if snapshot_name and target_name and snapshot_name != target_name:
            return ""
        state = self._states.get(target_name) or self._states.get(customer_name)
        return self._build_active_snapshot_inbound_signature(
            customer_name=target_name or customer_name,
            active_snapshot=active_snapshot,
            state=state,
            position_hint="latest_inbound",
            text=normalized_text,
        )

    def _is_valid_message(self, content: str) -> bool:
        """验证消息有效性"""
        if not content:
            return False

        content_stripped = content.strip()

        if len(content_stripped) < self.MIN_MESSAGE_LENGTH:
            return False
        if len(content_stripped) > self.MAX_MESSAGE_LENGTH:
            return False

        for pattern in self.INVALID_PATTERNS:
            if pattern in content:
                return False

        for pattern in self.STATUS_INDICATOR_PATTERNS:
            if pattern in content:
                return False

        if content_stripped.startswith('我:') and len(content_stripped) <= 5:
            return False
        if content_stripped.startswith('[自动回复]'):
            return False
        if content_stripped.startswith('我发送了'):
            return False

        import re
        if any(pattern in content_stripped for pattern in self.SYSTEM_DYNAMIC_PATTERNS):
            return False
        if re.match(r'^[^\s]{1,16}\s+\d+/\d+$', content_stripped):
            return False

        date_patterns = [
            r'^\d{4}[\/\-]\d{1,2}([\/\-]\d{1,2})?$',
            r'^\d{1,2}[\/\-]\d{1,2}([\/\-]\d{1,2})?$',
            r'^\d{1,2}:\d{2}$',
            r'^(昨天|刚刚|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天])$',
        ]
        for pat in date_patterns:
            if re.match(pat, content_stripped):
                return False

        return True

    def _process_send_queue(self):
        """处理发送队列（在主线程轮询中调用，确保Playwright操作线程安全）
        
        修复竞态条件：增加队列处理锁，防止多个线程同时处理队列
        """
        self._ensure_queue_runtime_state()
        with self._queue_processing_lock:
            try:
                processed_count = 0
                max_process_per_cycle = 10  # 每次最多处理10条消息，避免阻塞
                
                while not self._send_queue.empty() and processed_count < max_process_per_cycle:
                    try:
                        task_id, content, customer_name, assume_target_ready, identity_level, result_event = self._send_queue.get_nowait()
                        processed_count += 1
                        
                        with self._state_lock:
                            if task_id in self._cancelled_send_tasks:
                                del self._cancelled_send_tasks[task_id]
                                logger.info(f"跳过已取消的发送任务: {task_id[:8]}")
                                if result_event:
                                    result_event.set()
                                continue
                            now_ts = time.time()
                            expired = [k for k, v in self._cancelled_send_tasks.items() if now_ts - v > 300]
                            for k in expired:
                                del self._cancelled_send_tasks[k]
                        
                        try:
                            result = self._execute_send_with_lock(
                                content,
                                customer_name,
                                assume_target_ready=assume_target_ready,
                                identity_level=identity_level,
                            )
                            with self._send_results_lock:
                                with self._state_lock:
                                    if task_id in self._cancelled_send_tasks:
                                        del self._cancelled_send_tasks[task_id]
                                        logger.info(f"发送完成但任务已取消，清理结果: {task_id[:8]}")
                                    else:
                                        self._send_results[task_id] = result
                        except Exception as e:
                            with self._send_results_lock:
                                self._send_results[task_id] = OperationResult(
                                    success=False, mode=OperationMode.SMART, error=str(e)
                                )
                        finally:
                            if result_event:
                                result_event.set()
                    except Exception as e:
                        logger.error(f"处理发送队列失败: {e}")
                        break  # 发生异常时退出循环，避免无限循环
                
                if processed_count > 0:
                    logger.debug(f"发送队列处理完成，处理了 {processed_count} 条消息")
                    
                with self._send_results_lock:
                    if len(self._send_results) > 50:
                        self._send_results.clear()
                    
            except Exception as e:
                logger.error(f"处理发送队列异常: {e}")

    def _process_prepare_queue(self):
        """处理发送前目标会话准备队列，统一复用 owner 线程 DOM 能力。"""
        self._ensure_queue_runtime_state()
        with self._queue_processing_lock:
            try:
                processed_count = 0
                max_process_per_cycle = 10

                while not self._prepare_queue.empty() and processed_count < max_process_per_cycle:
                    try:
                        (
                            task_id,
                            customer_name,
                            conversation_id,
                            customer_id,
                            identity_level,
                            target_candidates,
                            result_event,
                        ) = self._prepare_queue.get_nowait()
                        processed_count += 1
                        try:
                            result = self._prepare_send_target_now(
                                customer_name,
                                conversation_id=conversation_id,
                                customer_id=customer_id,
                                identity_level=identity_level,
                                target_candidates=target_candidates,
                            )
                        except Exception as e:
                            logger.debug(f"处理目标会话准备任务失败: {e}")
                            result = False
                        finally:
                            with self._prepare_results_lock:
                                self._prepare_results[task_id] = result
                            if result_event:
                                result_event.set()
                    except Exception as e:
                        logger.error(f"处理目标会话准备队列失败: {e}")
                        break

                if processed_count > 0:
                    logger.debug(f"目标会话准备队列处理完成，处理了 {processed_count} 条任务")
            except Exception as e:
                logger.error(f"处理目标会话准备队列异常: {e}")

    def _get_send_result_wait_timeout(self) -> float:
        """估算单次发送任务的最长期待完成时间，避免上层先超时、底层晚成功。"""
        retry_wait_budget = sum(self._calculate_retry_delay(i) for i in range(self.MAX_RETRIES))
        attempt_budget = (self.MAX_RETRIES + 1) * (self.OPERATION_TIMEOUT + 5)
        return attempt_budget + retry_wait_budget + 10

    def send_message(
        self,
        content: str,
        customer_name: Optional[str] = None,
        *,
        assume_target_ready: bool = False,
        identity_level: str = "exact_name",
    ) -> OperationResult:
        """发送消息（内部方法，外部应通过 OutboundSendGateway.send() 发送；线程安全：通过队列将Playwright操作调度到页面所属线程执行）"""
        import uuid

        self._ensure_queue_runtime_state()
        if threading.current_thread() is self._owner_thread:
            return self._execute_send_with_lock(
                content,
                customer_name,
                assume_target_ready=assume_target_ready,
                identity_level=identity_level,
            )

        task_id = str(uuid.uuid4())
        result_event = threading.Event()
        self._send_queue.put((task_id, content, customer_name, assume_target_ready, identity_level, result_event))
        wait_timeout = self._get_send_result_wait_timeout()
        if result_event.wait(timeout=wait_timeout):
            with self._send_results_lock:
                result = self._send_results.pop(task_id, None)
            if result:
                return result
            return OperationResult(success=False, mode=OperationMode.SMART, error="发送结果丢失")
        else:
            with self._send_results_lock:
                self._send_results.pop(task_id, None)
            with self._state_lock:
                self._cancelled_send_tasks[task_id] = time.time()
            logger.error(f"发送超时：页面所属线程未在规定时间内处理发送任务 (timeout={wait_timeout:.1f}s)")
            return OperationResult(success=False, mode=OperationMode.SMART, error="发送超时：页面线程未处理")

    def prepare_send_target(
        self,
        customer_name: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        identity_level: str = "exact_name",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """线程安全地预热目标会话，为后续发送提前完成会话切换确认。"""
        import uuid

        self._ensure_queue_runtime_state()
        if not customer_name:
            return False

        if threading.current_thread() is self._owner_thread:
            return self._prepare_send_target_now(
                customer_name,
                conversation_id=conversation_id,
                customer_id=customer_id,
                identity_level=identity_level,
                target_candidates=target_candidates,
            )

        task_id = str(uuid.uuid4())
        result_event = threading.Event()
        self._prepare_queue.put(
            (
                task_id,
                customer_name,
                conversation_id,
                customer_id,
                identity_level,
                list(target_candidates or []),
                result_event,
            )
        )
        wait_timeout = max(
            self.OPERATION_TIMEOUT + 5,
            self.TARGET_CONVERSATION_READY_WAIT_MS / 1000.0 + 10,
        )
        if result_event.wait(timeout=wait_timeout):
            with self._prepare_results_lock:
                result = self._prepare_results.pop(task_id, None)
            return bool(result)

        with self._prepare_results_lock:
            self._prepare_results.pop(task_id, None)
        logger.error(f"目标会话准备超时：页面所属线程未在规定时间内处理任务 (timeout={wait_timeout:.1f}s)")
        return False

    def _execute_send_with_lock(
        self,
        content: str,
        customer_name: Optional[str] = None,
        *,
        assume_target_ready: bool = False,
        identity_level: str = "exact_name",
    ) -> OperationResult:
        """带锁的发送操作"""
        acquired = self._operation_lock.acquire(timeout=self.OPERATION_TIMEOUT)
        if not acquired:
            return OperationResult(success=False, mode=OperationMode.SMART, error="并发控制超时")

        try:
            bg_acquired = self._boundary_guard.acquire('send')
            if not bg_acquired:
                return OperationResult(success=False, mode=OperationMode.SMART, error="边界保护拒绝发送")

            try:
                result = self._execute_send_directly(
                    content,
                    customer_name,
                    assume_target_ready=assume_target_ready,
                    identity_level=identity_level,
                )
                self._boundary_guard.record_operation('send', customer_name or 'unknown', result.success, result.duration, result.error)
                return result
            except Exception as e:
                self._boundary_guard.record_operation('send', customer_name or 'unknown', False, 0, str(e))
                raise
            finally:
                self._boundary_guard.release()
        finally:
            self._operation_lock.release()

    def _execute_send_directly(
        self,
        content: str,
        customer_name: Optional[str] = None,
        *,
        assume_target_ready: bool = False,
        identity_level: str = "exact_name",
    ) -> OperationResult:
        """执行发送操作"""
        start_time = time.time()
        self_heal_used = False

        content_hash = hashlib.md5(f"{customer_name}:{content}".encode()).hexdigest()
        if self._is_duplicate_send(content_hash):
            logger.warning(f"发送被拒绝(重复): {customer_name}")
            return OperationResult(success=False, mode=OperationMode.SMART, error="消息已发送，请勿重复")

        with self._typing_lock:
            if customer_name and not assume_target_ready:
                click_result = self._prepare_send_target_now(
                    customer_name,
                    allow_search_filter=False,
                    identity_level=identity_level,
                )
                if not click_result:
                    failure_code = self._get_last_target_prepare_error(
                        f"conversation_not_ready:{customer_name}"
                    )
                    logger.error(f"无法进入目标会话并准备发送: {customer_name}")
                    return OperationResult(success=False, mode=OperationMode.SMART, error=failure_code)

            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    capture_attempt_snapshot = attempt > 0
                    if attempt > 0 and customer_name:
                        # 重试前检查消息是否已发送成功，避免因验证误判导致重复发送
                        if self._is_duplicate_send(content_hash):
                            logger.info(f"重试前检测到消息已发送，跳过重复发送: {customer_name}")
                            duration = time.time() - start_time
                            return OperationResult(success=True, mode=OperationMode.SMART, retry_count=attempt, duration=duration, message="消息已在之前的尝试中发送")

                        # 重试时重新准备目标会话（可能因前次失败导致会话切换）
                        if not assume_target_ready:
                            if not self._prepare_send_target_now(
                                customer_name,
                                allow_search_filter=False,
                                identity_level=identity_level,
                            ):
                                logger.warning(f"重试时无法进入目标会话，跳过本次尝试: {customer_name}")
                                if attempt < self.MAX_RETRIES:
                                    continue
                                failure_code = self._get_last_target_prepare_error(
                                    f"conversation_not_ready:{customer_name}"
                                )
                                return OperationResult(success=False, mode=OperationMode.SMART, error=failure_code)

                    if not self._ensure_chat_page():
                        raise Exception("无法进入聊天页面")

                    input_box = self._find_input_box()
                    if not input_box:
                        self._dump_chat_dom_snapshot(
                            stage="input_not_found",
                            customer_name=customer_name or "",
                            content=content,
                            attempt=attempt + 1,
                        )
                        raise Exception("未找到输入框")
                    with contextlib.suppress(Exception):
                        capture_attempt_snapshot = capture_attempt_snapshot or bool(
                            input_box.evaluate("el => !!(el && el.isContentEditable)")
                        )

                    if capture_attempt_snapshot:
                        self._dump_chat_dom_snapshot(
                            stage="before_input",
                            customer_name=customer_name or "",
                            content=content,
                            attempt=attempt + 1,
                        )
                    logger.info(f"开始输入消息(尝试{attempt + 1}): {customer_name}, 内容长度={len(content)}")
                    
                    # 侦测风控验证码
                    if self._check_for_captcha():
                        logger.error(f"侦测到风控验证码/滑块弹窗，挂起发送队列: {customer_name}")
                        # 发送钉钉告警 (如果后续有统一告警中心可以接入)
                        return OperationResult(success=False, mode=OperationMode.SMART, error="触发风控验证码，队列已挂起")
                        
                    if not self._human_like_input(input_box, content):
                        self._dump_chat_dom_snapshot(
                            stage="input_failed",
                            customer_name=customer_name or "",
                            content=content,
                            attempt=attempt + 1,
                        )
                        raise Exception("输入框内容校验失败")
                    if capture_attempt_snapshot:
                        self._dump_chat_dom_snapshot(
                            stage="after_input",
                            customer_name=customer_name or "",
                            content=content,
                            attempt=attempt + 1,
                        )
                    before_dispatch_snapshot = self._collect_chat_dom_snapshot(
                        customer_name=customer_name or "",
                        content=content,
                        stage="before_dispatch",
                        attempt=attempt + 1,
                    )
                    # [FIX:empty-line-v10] 移除 v7/v8 的 sanitize + Home+Backspace 修复
                    # 收敛前：发送前做 sanitize + Home+Backspace 修复前导空行，
                    # 但这些 DOM 操作本身就会导致 slate.js 状态不同步，产生前导空行。
                    # 收敛后：_clear_input_box 用键盘事件清空，_human_like_input 不 sanitize，
                    # slate.js 内部状态始终同步，不需要发送前修复。
                    if self._dispatch_send_action(
                        input_box,
                        content,
                        before_snapshot=before_dispatch_snapshot,
                        expected_customer_name=customer_name or "",
                    ):
                        if capture_attempt_snapshot:
                            self._dump_chat_dom_snapshot(
                                stage="send_success",
                                customer_name=customer_name or "",
                                content=content,
                                attempt=attempt + 1,
                            )
                        # [FIX-INST:empty-line-diagnosis] 发送成功后立即 dump input_box 真实 DOM，
                        # 供"空一行"问题诊断。这里要拿到"刚发送出去的真实内容"，而不是下一次 IME 重置后的状态。
                        try:
                            actual_input_html = self.page.evaluate("""() => {
                                const candidates = [
                                    '[data-e2e="msg-input"] [contenteditable="true"]',
                                    'div[class*="public-DraftEditor-content"]',
                                    'div[contenteditable="true"]',
                                ];
                                for (const sel of candidates) {
                                    const el = document.querySelector(sel);
                                    if (el) {
                                        return {
                                            selector: sel,
                                            outerHTML: (el.outerHTML || '').slice(0, 800),
                                            innerHTML: (el.innerHTML || '').slice(0, 600),
                                            textContent: (el.textContent || '').slice(0, 100),
                                        };
                                    }
                                }
                                return null;
                            }""")
                            if actual_input_html:
                                logger.info(
                                    f"[DIAG-INST] 发送后 input_box 真实 DOM: "
                                    f"selector={actual_input_html.get('selector')}, "
                                    f"textContent={actual_input_html.get('textContent')!r}, "
                                    f"innerHTML={actual_input_html.get('innerHTML')!r}"
                                )
                            else:
                                logger.warning("[DIAG-INST] 发送后未找到 input_box 元素")
                        except Exception as diag_exc:
                            logger.debug(f"[DIAG-INST] 发送后 input_box DOM dump 失败: {diag_exc}")
                        duration = time.time() - start_time
                        self._mark_sent(customer_name or "unknown", content, content_hash)
                        logger.info(f"消息发送成功: {customer_name}, 耗时={duration:.1f}s")
                        return OperationResult(success=True, mode=OperationMode.SMART, retry_count=attempt, duration=duration)
                    else:
                        self._dump_chat_dom_snapshot(
                            stage="send_verify_failed",
                            customer_name=customer_name or "",
                            content=content,
                            attempt=attempt + 1,
                        )
                        logger.warning(f"发送验证失败，不标记为已发送，允许重试: {customer_name}")
                        raise Exception("发送验证失败")

                except Exception as e:
                    duration = time.time() - start_time
                    failure_snapshot = self._collect_chat_dom_snapshot(
                        customer_name=customer_name or "",
                        content=content,
                        stage="send_failure_classify",
                        attempt=attempt + 1,
                    )
                    diagnosis = self._classify_send_failure_from_dom(
                        snapshot=failure_snapshot,
                        error=str(e),
                        customer_name=customer_name or "",
                    )
                    self._dump_chat_dom_snapshot(
                        stage="send_exception",
                        customer_name=customer_name or "",
                        content=content,
                        attempt=attempt + 1,
                        extra={
                            "error": str(e),
                            "duration": round(duration, 3),
                            "diagnosis": diagnosis,
                        },
                    )
                    logger.warning(
                        f"发送失败 (尝试 {attempt + 1}/{self.MAX_RETRIES + 1}): {e}, "
                        f"耗时={duration:.1f}s, failure_class={diagnosis.get('failure_class')}, "
                        f"target={diagnosis.get('target_customer') or '-'}, "
                        f"active_candidates={diagnosis.get('active_candidates') or []}"
                    )

                    if attempt < self.MAX_RETRIES:
                        if not self_heal_used:
                            self_heal_used = True
                            healed = self._apply_send_self_heal(
                                diagnosis=diagnosis,
                                customer_name=customer_name or "",
                                content=content,
                            )
                            if healed:
                                logger.info(f"发送失败后已执行一次DOM自愈: {diagnosis.get('failure_class')}")
                                continue
                        delay = self._calculate_retry_delay(attempt)
                        try:
                            self.page.wait_for_timeout(int(delay * 1000))
                        except Exception:
                            time.sleep(delay)
                        self._recover_page()
                    else:
                        return OperationResult(success=False, mode=OperationMode.SMART, error=str(e), retry_count=attempt, duration=duration)

            return OperationResult(success=False, mode=OperationMode.SMART, error="重试次数耗尽")

    def _read_input_box_content(self, input_box) -> str:
        """读取输入框内容，统一兼容 contenteditable 与 textarea。

        兜底链：input_box.evaluate(js) → input_box.inner_text() →
        input_box.text_content() → input_box.input_value()。
        最后两项是 Playwright 同步 API，page.evaluate 不可用时仍可工作。
        """
        # 主路径：JS evaluate 同时尝试 textarea 与 contenteditable
        try:
            raw = input_box.evaluate(
                """el => {
                    if (!el) return '';
                    const resolveTarget = (root) => {
                        const candidates = [];
                        const pushCandidate = (node) => {
                            if (!node || candidates.includes(node)) return;
                            candidates.push(node);
                        };
                        if (root.matches && root.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')) {
                            pushCandidate(root);
                        }
                        for (const node of root.querySelectorAll('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')) {
                            pushCandidate(node);
                        }
                        let best = root;
                        let bestScore = -1;
                        for (const node of candidates) {
                            let score = 0;
                            if (node.getAttribute && node.getAttribute('role') === 'textbox') score += 60;
                            if (node.isContentEditable || node.tagName === 'TEXTAREA') score += 40;
                            if (!node.querySelector('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')) score += 50;
                            if (node !== root) score += 10;
                            if (score > bestScore) {
                                best = node;
                                bestScore = score;
                            }
                        }
                        return best || root;
                    };
                    const target = resolveTarget(el);
                    if (target && target.isContentEditable) {
                        return (target.innerText || target.textContent || '');
                    }
                    if (target && typeof target.value === 'string') {
                        return target.value;
                    }
                    return (target?.innerText || target?.textContent || el.innerText || el.textContent || '');
                }"""
            )
            if raw is not None and str(raw).strip():
                return self._remove_zero_width_chars(str(raw))
        except Exception:
            pass
        # 兜底 1：Playwright inner_text()
        try:
            raw = input_box.inner_text() or ""
            if raw and raw.strip():
                return self._remove_zero_width_chars(raw)
        except Exception:
            pass
        # 兜底 2：Playwright text_content()
        try:
            raw = input_box.text_content() or ""
            if raw and raw.strip():
                return self._remove_zero_width_chars(raw)
        except Exception:
            pass
        # 兜底 3：Playwright input_value()（仅对 <input> / <textarea> 有效）
        try:
            raw = input_box.input_value() or ""
            if raw and raw.strip():
                return self._remove_zero_width_chars(raw)
        except Exception:
            pass
        return ""

    def _normalize_editor_text(self, text: str) -> str:
        """统一编辑器内容对比，避免换行和 nbsp 差异导致误判。

        [FIX-INST:empty-line-v6] 同步去除全角空格 \u3000 和其他 Unicode 空白字符
        收敛前：仅 strip() 去除 ASCII 空白，全角空格 \u3000 仍残留，写入 input_box
        后变成"前导空 div"显示为"前面空一行"
        收敛后：先 re.sub 去除所有 Unicode 空白前缀/后缀，再 strip()

        [FIX-INST:input-mismatch] 去除 slate.js 光标占位产生的尾部换行
        收敛前：slate.js 在 contenteditable 中插入 <div data-enter="true"> 作为光标占位，
        innerText 读取时尾部多出 \n，导致 actual_clean != expected_clean 误判，
        触发不必要的兜底输入（清空→重输→清空→重输...），用户看到内容被重复输入多次。
        收敛后：normalize 时去除尾部所有换行符，只保留中间的换行（用户真实换行）。
        """
        if not text:
            return ""
        # [FIX-INST:empty-line-v6] 先去除零宽字符，再去所有 Unicode 空白（含全角空格 U+3000），
        # 最后做 ASCII strip 兜底
        normalized = self._remove_zero_width_chars(text or "")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        # [FIX-INST:input-mismatch] 去除尾部换行（slate.js 光标占位产生的）
        # 保留中间的换行（用户真实换行），只去除尾部
        normalized = normalized.rstrip("\n")
        # 去前导/后导 Unicode 空白字符（含全角空格 \u3000 / 各类 Unicode 空格）
        unicode_whitespace_pattern = (
            r"^[\s\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|[\s\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$"
        )
        normalized = re.sub(unicode_whitespace_pattern, "", normalized)
        return normalized.strip()

    def _get_send_pacing_profile(self, content: str) -> Dict[str, int]:
        """根据消息长度返回更保守或更快的发送节奏。"""
        length = len(str(content or "").strip())
        if length <= 80:
            return {
                "chunk_min": max(length, 1),
                "chunk_max": max(length, 1),
                "type_delay_min": 8,
                "type_delay_max": 18,
                "inter_chunk_wait_min": 0,
                "inter_chunk_wait_max": 0,
                "dispatch_wait_min": 40,
                "dispatch_wait_max": 120,
                "post_button_wait_ms": 100,
                "post_enter_wait_ms": 80,
            }
        if length <= 160:
            return {
                "chunk_min": 60,
                "chunk_max": 90,
                "type_delay_min": 9,
                "type_delay_max": 20,
                "inter_chunk_wait_min": 80,
                "inter_chunk_wait_max": 180,
                "dispatch_wait_min": 60,
                "dispatch_wait_max": 160,
                "post_button_wait_ms": 120,
                "post_enter_wait_ms": 100,
            }
        return {
            "chunk_min": 40,
            "chunk_max": 60,
            "type_delay_min": 10,
            "type_delay_max": 30,
            "inter_chunk_wait_min": 200,
            "inter_chunk_wait_max": 500,
            "dispatch_wait_min": 100,
            "dispatch_wait_max": 300,
            "post_button_wait_ms": self.SEND_POST_BUTTON_WAIT_MS,
            "post_enter_wait_ms": self.SEND_POST_ENTER_WAIT_MS,
        }

    def _focus_input_box(self, input_box) -> bool:
        """快速聚焦输入框，避免 Playwright 默认长超时卡死。

        [简化] 收敛前：3 层 try（click_target → click → JS evaluate），
        收敛后：2 层（click → JS evaluate），click_target 内部已含 click 兜底。
        """
        # 主路径：直接 click（click_target 是 human_helper 的薄包装，内部已有兜底）
        try:
            input_box.click(timeout=1500, force=True)
            return True
        except Exception:
            pass

        # 兜底：JS focus
        try:
            focused = input_box.evaluate(
                """el => {
                    if (!el) return false;
                    try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                    try { el.focus(); } catch (e) {}
                    const active = document.activeElement;
                    return active === el || (!!active && el.contains && el.contains(active));
                }"""
            )
            if focused:
                self.page.wait_for_timeout(50)
                return True
        except Exception as e:
            logger.debug(f"聚焦输入框失败: {e}")

        return False

    def _click_send_button(self, input_box=None) -> bool:
        """点击发送按钮，优先在 msg-input 容器内定位。"""
        try:
            selector_candidates = list(SELECTOR_POOL["send_button"]) + list(self.SEND_BUTTON_SELECTORS)
            scoped_selectors: List[str] = [
                f'[data-e2e="msg-input"] {selector}' for selector in selector_candidates
            ]
            selectors = scoped_selectors + selector_candidates

            for selector in selectors:
                try:
                    locator = self.page.locator(selector)
                    count = min(locator.count(), 3)
                    for index in range(count):
                        button = locator.nth(index)
                        if not button.is_visible():
                            continue
                        try:
                            if not self._human_helper().click_target(button):
                                button.click(timeout=1500, force=True)
                            logger.info(f"已点击发送按钮: {selector}")
                            return True
                        except Exception as click_error:
                            logger.debug(f"点击发送按钮失败({selector}): {click_error}")
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"查找发送按钮失败: {e}")
        return False

    def _wait_and_click_send_button(self, input_box=None, max_wait_ms: int = 900, step_ms: int = 150) -> bool:
        """短轮询等待发送按钮出现/激活，优先在输入后窗口内点击。"""
        elapsed = 0
        while elapsed <= max_wait_ms:
            if self._click_send_button(input_box=input_box):
                return True
            if elapsed >= max_wait_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        return False

    def _dispatch_send_action(
        self,
        input_box,
        content: str,
        before_snapshot: Optional[Dict[str, Any]] = None,
        *,
        expected_customer_name: str = "",
    ) -> bool:
        """执行发送动作，优先点击输入后出现/激活的发送按钮，Enter 仅作兜底。"""
        is_contenteditable = False
        with contextlib.suppress(Exception):
            is_contenteditable = bool(input_box.evaluate("el => !!(el && el.isContentEditable)"))

        # [FIX:empty-line-v10] 移除 v9 的 Ctrl+A+Delete+insert_text 修复
        # 收敛前：检测到前导空行后用 Ctrl+A+Delete+insert_text 重输，
        # 这会清空已输入内容并重新输入，用户看到"文本被多次输入"。
        # 收敛后：_clear_input_box 用键盘事件清空，slate.js 状态同步，
        # 不会产生前导空行，不需要发送前检测和重输。

        pacing = self._get_send_pacing_profile(content)
        self.page.wait_for_timeout(random.randint(pacing["dispatch_wait_min"], pacing["dispatch_wait_max"]))
        self.page.wait_for_timeout(self.SEND_PRE_DISPATCH_WAIT_MS)
        if self._wait_and_click_send_button(input_box=input_box):
            self.page.wait_for_timeout(pacing["post_button_wait_ms"])
            if self._verify_send_success(
                content,
                before_snapshot=before_snapshot,
                expected_customer_name=expected_customer_name,
                input_box=input_box,
            ):
                return True

        # [FIX:empty-line-v10] 移除 Enter 兜底前的 sanitize（已是 no-op，且不需要）

        try:
            input_box.press("Enter", timeout=1500)
            logger.info("已通过输入框 Enter 触发发送")
        except Exception as e:
            logger.debug(f"输入框 Enter 发送失败，改用键盘 Enter: {e}")
            with contextlib.suppress(Exception):
                self.page.keyboard.press("Enter")
                logger.info("已通过键盘 Enter 触发发送")

        self.page.wait_for_timeout(pacing["post_enter_wait_ms"])
        if self._verify_send_success(
            content,
            before_snapshot=before_snapshot,
            expected_customer_name=expected_customer_name,
            input_box=input_box,
        ):
            return True

        return False

    def _clear_input_box(self, input_box) -> bool:
        """清空输入框，仅使用键盘事件让 slate.js 正确同步内部状态。

        [FIX:empty-line-v10] 收敛前：用 fill("") + JS innerHTML='' 清空，
        slate.js 内部状态与 DOM 不同步，下次输入时插入前导空 div 作为占位行，
        导致消息首行空行。收敛后：只用 Ctrl+A + Delete 键盘事件，
        slate.js 能正确处理 deleteBackward，内部状态同步，不产生前导空行。
        """
        self._focus_input_box(input_box)
        # 只用键盘事件清空，slate.js 能正确同步内部状态
        with contextlib.suppress(Exception):
            input_box.press("Control+a", timeout=1200)
            input_box.press("Delete", timeout=1200)
        self.page.wait_for_timeout(50)

        # 验证清空结果
        cleared_content = self._read_input_box_content(input_box)
        if cleared_content.strip():
            # 兜底：再试一次
            with contextlib.suppress(Exception):
                input_box.press("Control+a", timeout=800)
                input_box.press("Delete", timeout=800)
            self.page.wait_for_timeout(50)
            cleared_content = self._read_input_box_content(input_box)
            if cleared_content.strip():
                logger.warning(f"输入框清空失败，残留内容: {cleared_content[:50]}")
                return False
        return True

    def _sanitize_contenteditable_input(self, input_box) -> None:
        """[FIX:empty-line-v10] 不再对 slate.js 编辑器做 DOM 清理。

        收敛前：用 JS removeChild / innerHTML='' / textContent='' 清理占位节点，
        但 slate.js 维护自己的内部状态（虚拟 DOM），DOM 操作导致状态不同步，
        下次输入时 slate.js 插入前导空 div 作为占位行，导致消息首行空行。
        收敛后：完全不动 DOM，让 slate.js 自己管理占位符。
        读取内容时用 _normalize_editor_text 统一处理零宽字符和尾部换行。
        """
        return

    def _human_like_input(self, input_box, content: str) -> bool:
        """模拟人工输入并校验。

        [FIX:empty-line-v10] 收敛策略：清空 → 输入 → 校验，单次完成。
        ┌──────────┬──────────────────────────────────────────────────────┐
        │ 级别     │ 方式                                                 │
        ├──────────┼──────────────────────────────────────────────────────┤
        │ 主路径   │ press_sequentially 拟人分段输入 → 校验               │
        │ 宽容通过 │ 核心内容包含匹配即通过（slate.js 占位差异可容忍）     │
        └──────────┴──────────────────────────────────────────────────────┘

        收敛前：5级回退 + Home+Backspace + insert_text 兜底，每次清空重输，
        用户看到内容被反复输入/清空。根因是 DOM 操作导致 slate.js 状态不同步。
        收敛后：只用键盘事件清空（Ctrl+A+Delete），不 sanitize DOM，
        不做 Home+Backspace 修复，不做 insert_text 兜底重输。
        如果主路径 + 宽容校验都不通过，返回 False 让上层重试。
        """
        if not self._focus_input_box(input_box):
            logger.warning("无法聚焦输入框")
            return False
        if not self._clear_input_box(input_box):
            return False

        expected_clean = self._normalize_editor_text(content)
        content = expected_clean
        pacing = self._get_send_pacing_profile(expected_clean)
        helper = self._human_helper()
        is_contenteditable = False
        with contextlib.suppress(Exception):
            is_contenteditable = bool(input_box.evaluate("el => !!(el && el.isContentEditable)"))

        # ── 主路径：拟人分段输入 ──
        success = self._input_via_press_sequentially(
            input_box, content, pacing, helper, is_contenteditable
        )

        if success:
            actual_clean = self._normalize_editor_text(self._read_input_box_content(input_box))
            if actual_clean == expected_clean:
                return True
            # 宽容校验：核心内容包含匹配即通过（但禁止前导/尾部空行）
            if self._is_content_acceptable(actual_clean, expected_clean):
                logger.info("拟人输入后内容存在可容忍差异（slate占位等），宽容通过")
                return True
            logger.warning(
                f"输入校验未通过: 期望({len(expected_clean)})={expected_clean[:80]}..., "
                f"实际({len(actual_clean)})={actual_clean[:80]}..."
            )
        else:
            logger.warning(f"拟人分段输入失败: 期望={expected_clean[:80]}...")

        # 不再兜底重输，避免内容被多次输入。返回 False 让上层重试整个流程。
        return False

    def _input_via_press_sequentially(
        self, input_box, content: str, pacing: dict, helper, is_contenteditable: bool
    ) -> bool:
        """拟人分段输入，返回是否成功写入（不校验内容匹配）。"""
        chunk_size = random.randint(pacing["chunk_min"], pacing["chunk_max"])
        chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
        # [FIX:text-overlap] slate.js 编辑器中 Backspace 不可靠，错字模拟会导致文本叠加
        # 收敛前：allow_typos=True，错字 Backspace 在 slate.js 中可能不删除，正确字符追加到错字后面
        # 收敛后：contenteditable 编辑器禁用错字模拟
        typo_chance = 0.0 if is_contenteditable else (0.05 if len(content) <= 80 else 0.03)
        any_chunk_failed = False

        for i, chunk in enumerate(chunks):
            try:
                helper.press_sequentially(
                    chunk,
                    base_delay=(pacing["type_delay_min"], pacing["type_delay_max"]),
                    allow_typos=not is_contenteditable,
                    typo_chance=typo_chance,
                )
            except Exception as type_err:
                logger.debug(f"拟人分段输入失败({i}): {type_err}")
                try:
                    if is_contenteditable:
                        self.page.keyboard.insert_text(chunk)
                    else:
                        input_box.type(chunk, delay=random.randint(pacing["type_delay_min"], pacing["type_delay_max"]))
                except Exception as fallback_err:
                    logger.debug(f"分段兜底输入失败({i}): {fallback_err}")
                    any_chunk_failed = True

            # 段与段之间的停顿模拟思考
            if i < len(chunks) - 1:
                wait_min = pacing["inter_chunk_wait_min"]
                wait_max = pacing["inter_chunk_wait_max"]
                if wait_max > 0:
                    self.page.wait_for_timeout(random.randint(wait_min, wait_max))

        # [FIX:empty-line-v10] 不再 sanitize DOM，让 slate.js 自己管理占位符
        self.page.wait_for_timeout(80)
        return not any_chunk_failed

    def _is_content_acceptable(self, actual: str, expected: str) -> bool:
        """宽容校验：如果核心内容一致，允许 slate.js 占位等微小差异。

        判定规则：
        1. 去除所有空白后完全一致 → 通过（但禁止前导/尾部空行差异）
        2. expected 是 actual 的子串（或反之）且长度差 < 5 → 通过
        3. 文本相似度 >= 0.9 → 通过
        """
        if not actual or not expected:
            return False
        # [FIX:empty-line] 禁止前导/尾部空行通过
        # slate.js 提交时会把前导 \n 作为消息一部分发送，导致聊天界面首行空行
        actual_stripped = actual.strip("\n\r\u200b\u200c\u200d\ufeff")
        expected_stripped = expected.strip("\n\r\u200b\u200c\u200d\ufeff")
        if actual_stripped != actual:
            # actual 有前导/尾部空行，不允许宽容通过
            logger.debug(f"输入内容有前导/尾部空行，不允许宽容通过: actual={actual[:50]!r}")
            return False
        # 规则1：去除所有空白后比较
        actual_compact = re.sub(r"\s+", "", actual)
        expected_compact = re.sub(r"\s+", "", expected)
        if actual_compact == expected_compact:
            return True
        # 规则2：子串包含且长度差小
        if expected_compact in actual_compact or actual_compact in expected_compact:
            if abs(len(actual_compact) - len(expected_compact)) < 5:
                return True
        # 规则3：文本相似度
        if self._calculate_text_similarity(actual, expected) >= 0.9:
            return True
        return False

    def _click_conversation(
        self,
        customer_name: str,
        *,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """点击指定客户的会话；发送场景可禁用搜索框过滤，避免 UI 先跳到其他会话。"""
        try:
            delegate_monitor = getattr(self, "message_monitor", None)
            delegate_click = getattr(delegate_monitor, "click_conversation", None)
            if callable(delegate_click):
                try:
                    delegated = bool(
                        delegate_click(
                            customer_name,
                            allow_search_filter=allow_search_filter,
                            conversation_id=conversation_id,
                            customer_id=customer_id,
                            target_candidates=target_candidates,
                        )
                    )
                    delegate_page = getattr(delegate_monitor, "page", None)
                    if delegate_page and not delegate_page.is_closed():
                        self.page = delegate_page
                    if delegated:
                        return True
                    logger.warning(f"monitor 委托切换会话失败，回退 RPA 旧点击链: {customer_name}")
                except Exception as delegate_exc:
                    logger.debug(f"monitor 委托切换会话异常，回退 RPA 旧点击链: {delegate_exc}")

            if not self._ensure_chat_page():
                return False

            def _apply_search_filter() -> bool:
                try:
                    changed = bool(
                        self.page.evaluate(set_search_js, [customer_name, SELECTOR_POOL["search_input"]])
                    )
                    if changed:
                        logger.info(f"直接会话列表未命中，已通过搜索框过滤用户: {customer_name}")
                        self.page.wait_for_timeout(800)
                    return changed
                except Exception as search_e:
                    logger.debug(f"搜索框过滤用户失败: {search_e}")
                    return False

            def _cleanup_search_filter_after_activation() -> None:
                """会话激活后清理搜索框残留过滤，防止搜索框一直处于打开状态。

                延迟清理策略：先确认目标会话右侧聊天区已加载（有气泡证据），
                再清空搜索框并派发 Escape，避免 React 状态重置导致跳回默认会话。
                """
                try:
                    self.page.wait_for_timeout(300)
                    self._clear_conversation_search_filter()
                except Exception:
                    pass

            set_search_js = """
            ([targetName, selectors]) => {
                const candidateSelectors = selectors || [
                    'input[placeholder*="搜索"]',
                    'input[placeholder*="search"]',
                    'input[type="search"]',
                    '[role="searchbox"] input',
                    '[class*="search"] input'
                ];
                let changed = false;
                for (const selector of candidateSelectors) {
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
            time.sleep(0.1)

            click_js = r"""
            ([targetNameRaw, selectors]) => {
                const normalizeName = (value) => {
                    const text = String(value || '').replace(/[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/g, '').trim().replace(/^@/, '');
                    if (!text) return '';
                    const tokens = text.split(/\s+/).filter(Boolean);
                    if (tokens.length <= 1) return text;
                    const trailingPattern = /^(刚刚|昨天|今天|前天|\d+分钟前|\d+小时前|\d+天前|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d+)$/;
                    while (tokens.length > 1 && trailingPattern.test(tokens[tokens.length - 1])) {
                        tokens.pop();
                    }
                    return tokens.join(' ').trim() || text;
                };
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
                const targetName = normalizeName(targetNameRaw);
                const normalizedTarget = targetName.replace(/\\s+/g, '').replace(/\\u200b/g, '');
                
                const itemSelectors = selectors || [
                    '[data-e2e="conversation-item"]',
                    '.chat-list-item',
                    '[class*="conversationItem"]',
                    '[class*="chat-item"]'
                ];
                
                const seen = new Set();
                let items = [];
                for (const selector of itemSelectors) {
                    const found = document.querySelectorAll(selector);
                    for (const item of found) {
                        if (seen.has(item)) continue;
                        seen.add(item);
                        items.push(item);
                    }
                }
                
                let exactMatch = null;
                let fuzzyMatch = null;
                const sampleNames = [];
                for (const item of items) {
                    const nameEl = item.querySelector('.conversationConversationItemtitle') ||
                                   item.querySelector('[class*="title"]') ||
                                   item.querySelector('[class*="name"]');
                    if (!nameEl) {
                        continue;
                    }
                    const rawName = nameEl.textContent || '';
                    const elName = normalizeName(rawName);
                    const normalizedElName = elName.replace(/\\s+/g, '').replace(/\\u200b/g, '');
                    if (elName && sampleNames.length < 8) {
                        sampleNames.push(elName);
                    }
                    if (normalizedElName === normalizedTarget) {
                        exactMatch = { item, elName };
                        break;
                    }
                    if (
                        !fuzzyMatch &&
                        normalizedElName &&
                        normalizedTarget &&
                        isMaskedNumericMatch(normalizedElName, normalizedTarget)
                    ) {
                        fuzzyMatch = { item, elName };
                    }
                }
                
                const match = exactMatch || fuzzyMatch;
                if (match) {
                    match.item.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    
                    const rect = match.item.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    
                    ['mousedown', 'mouseup', 'click'].forEach(eventType => {
                        const event = new MouseEvent(eventType, {
                            bubbles: true, cancelable: true, view: window,
                            clientX: centerX, clientY: centerY
                        });
                        match.item.dispatchEvent(event);
                    });
                    
                    return {
                        success: true,
                        matchedName: match.elName,
                        matchType: exactMatch ? 'exact' : 'fuzzy',
                        count: items.length,
                        sampleNames
                    };
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
            search_filter_applied = False  # 标记搜索框是否被写入
            for _ in range(8):
                result = self.page.evaluate(click_js, [customer_name, SELECTOR_POOL["conversation_item"]])
                if not result:
                    logger.warning(f"点击会话JS执行失败: {customer_name}")
                    return False
                last_result = result

                if result.get('success'):
                    match_type = result.get('matchType', 'unknown')
                    logger.info(
                        f"成功点击会话: {customer_name} (匹配方式: {match_type}, "
                        f"visible_count={result.get('count', 0)}, sample_names={result.get('sampleNames', [])}, "
                        f"scroll_attempts={scroll_attempts})"
                    )
                    
                    if self._wait_for_target_conversation_active(customer_name, max_wait_ms=1200):
                        # 只有搜索框被写入后才需要清理，避免不必要的搜索框操作
                        if search_filter_applied:
                            _cleanup_search_filter_after_activation()
                        return True
                        
                    logger.warning(f"点击后目标会话仍未激活，准备重试: {customer_name}")
                    self.page.wait_for_timeout(250)
                    continue

                if scroll_attempts == 0 and allow_search_filter:
                    if _apply_search_filter():
                        search_filter_applied = True

                scroll_result = self._safe_evaluate(scroll_list_js, timeout_ms=5000) or {}
                if not scroll_result.get('changed'):
                    logger.debug(f"Scroll changed is false: {scroll_result}")
                    break
                scroll_attempts += 1
                self.page.wait_for_timeout(400)

            logger.warning(
                f"点击会话失败: {customer_name}, visible_count={last_result.get('count', 0)}, "
                f"sample_names={last_result.get('sampleNames', [])}, scroll_attempts={scroll_attempts}, "
                f"allow_search_filter={allow_search_filter}"
            )
            
            return False
        except Exception as e:
            logger.error(f"点击会话失败: {e}")
            return False

    def _is_target_conversation_active(self, customer_name: str) -> bool:
        """确认当前聊天框是否已经切到目标会话。"""
        normalized_customer = self._normalize_customer_name(customer_name)
        if not normalized_customer:
            return False

        snapshot = self._get_active_conversation_snapshot()
        snapshot_name = self._normalize_customer_name((snapshot or {}).get("customer_name", ""))
        snapshot_name_matched, _ = self._match_target_conversation_candidates(
            [{"text": snapshot_name}] if snapshot_name else [],
            normalized_customer,
        )
        if snapshot_name_matched:
            return True

        dom_snapshot = self._collect_chat_dom_snapshot(
            customer_name=customer_name,
            stage="verify_target_conversation",
        )
        active_conversation = dom_snapshot.get("activeConversation") or []
        matched, candidates = self._match_target_conversation_candidates(active_conversation, normalized_customer)
        if not matched:
            left_names = [
                self._normalize_customer_name(name)
                for name in list(dom_snapshot.get("leftConversationNames") or [])[:8]
                if self._normalize_customer_name(name)
            ]
            right_titles = self._format_conversation_debug_names(
                [{"text": title} for title in list(dom_snapshot.get("rightPanelTitles") or [])[:8]]
            )
            active_sources = [
                {
                    "source": str(item.get("source") or "").strip(),
                    "text": self._normalize_customer_name(str(item.get("text") or "").strip()),
                }
                for item in list(dom_snapshot.get("activeConversationSources") or [])[:8]
            ]
            logger.warning(
                f"RPA目标会话校验未通过: target={normalized_customer}, "
                f"snapshot_name={snapshot_name or '-'}, "
                f"left_names={left_names}, right_titles={right_titles}, "
                f"active_sources={active_sources}, active_candidates={candidates}"
            )
        return matched

    def _wait_for_target_conversation_active(self, customer_name: str, *, max_wait_ms: int = 1800, step_ms: int = 150) -> bool:
        """短轮询等待目标会话真正切到右侧聊天区。"""
        elapsed = 0
        while elapsed <= max_wait_ms:
            if self._is_target_conversation_active(customer_name):
                return True
            if elapsed >= max_wait_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        return False

    def _prepare_send_target_now(
        self,
        customer_name: str,
        *,
        max_wait_ms: int = TARGET_CONVERSATION_READY_WAIT_MS,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        identity_level: str = "exact_name",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """统一封装当前线程内的目标会话准备（薄包装，直接转发到 _ensure_target_conversation_ready）。"""
        return bool(
            self._ensure_target_conversation_ready(
                customer_name,
                max_wait_ms=max_wait_ms,
                allow_search_filter=allow_search_filter,
                conversation_id=conversation_id,
                customer_id=customer_id,
                identity_level=identity_level,
                target_candidates=target_candidates,
            )
        )

    def _set_last_target_prepare_error(self, error_code: str) -> str:
        self._last_target_prepare_error = str(error_code or "").strip()[:160]
        return self._last_target_prepare_error

    def _clear_last_target_prepare_error(self):
        self._last_target_prepare_error = ""

    def _get_last_target_prepare_error(self, default: str = "") -> str:
        error_code = str(getattr(self, "_last_target_prepare_error", "") or "").strip()
        return error_code or str(default or "").strip()

    def _validate_ready_target_consistency(
        self,
        customer_name: str,
        *,
        identity_level: str = "exact_name",
    ) -> tuple[bool, str]:
        target_name = self._normalize_customer_name(customer_name)
        if not target_name:
            return False, "invalid_send_target"

        snapshot = self._get_active_conversation_snapshot()
        snapshot_name = self._normalize_customer_name(snapshot.get("customer_name", ""))
        if snapshot_name == target_name:
            return True, ""

        allow_fuzzy = identity_level in {"strict_conversation_id", "strict_customer_id"}
        if allow_fuzzy and self._wait_for_target_conversation_active(customer_name, max_wait_ms=350):
            return True, ""

        failure_code = (
            "identity_conflict_blocked"
            if identity_level in {"alias_match", "name_fuzzy"}
            else "conversation_active_mismatch"
        )
        logger.warning(
            f"RPA发送前目标一致性校验失败: expected={customer_name}, "
            f"identity_level={identity_level}, active_snapshot_name={snapshot_name or '-'}"
        )
        return False, failure_code

    def _wait_for_input_box_ready(self, *, timeout_ms: int, step_ms: int = 250):
        """短轮询等待真实聊天输入框出现，减少会话切换后的瞬时误判。"""
        elapsed = 0
        while elapsed <= timeout_ms:
            # _find_input_box 内部已有自己的超时控制，这里用短超时快速探测
            input_box = self._find_input_box(timeout=step_ms)
            if input_box:
                return input_box
            if elapsed >= timeout_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        return None

    def _ensure_target_conversation_ready(
        self,
        customer_name: str,
        *,
        max_wait_ms: int = TARGET_CONVERSATION_READY_WAIT_MS,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        identity_level: str = "exact_name",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """发送前强制进入目标会话，再去定位输入框。

        [简化] 收敛前：3 处重复的"等待输入框→验证一致性→返回"逻辑，
        收敛后：提取为内部函数 _try_use_active_input_box，统一调用。
        """
        self._clear_last_target_prepare_error()
        if not customer_name:
            self._set_last_target_prepare_error("invalid_send_target")
            return False
        if not self._ensure_chat_page():
            self._set_last_target_prepare_error("chat_page_not_ready")
            return False

        def _try_use_active_input_box(log_tag: str) -> bool:
            """等待输入框就绪并验证一致性，成功返回 True。"""
            input_box = self._wait_for_input_box_ready(timeout_ms=self.TARGET_INPUT_READY_TIMEOUT_MS)
            if not input_box:
                return False
            consistent, failure_code = self._validate_ready_target_consistency(
                customer_name,
                identity_level=identity_level,
            )
            if not consistent:
                self._set_last_target_prepare_error(f"{failure_code}:{customer_name}")
                return False
            logger.info(f"{log_tag}: {customer_name}")
            return True

        # 若当前右侧聊天区已经是目标会话，直接复用，避免重复点击导致状态抖动。
        if self._wait_for_target_conversation_active(customer_name, max_wait_ms=min(600, max_wait_ms)):
            if _try_use_active_input_box("目标会话已在当前聊天区激活，直接复用输入框"):
                return True
            logger.debug(f"目标会话已激活，但输入框暂未就绪，准备重新点击会话: {customer_name}")

        total_attempts = 3
        for attempt in range(1, total_attempts + 1):
            click_ok = self._click_conversation(
                customer_name,
                allow_search_filter=allow_search_filter,
                conversation_id=conversation_id,
                customer_id=customer_id,
                target_candidates=target_candidates,
            )

            # 无论 click_ok 是否为 True，都尝试等待会话激活并复用输入框
            if self._wait_for_target_conversation_active(customer_name, max_wait_ms=max_wait_ms if click_ok else min(600, max_wait_ms)):
                if _try_use_active_input_box(
                    "点击会话返回失败，但目标会话实际已激活并可发送" if not click_ok else "目标会话已就绪，开始准备输入"
                ):
                    return True

            # 点击或等待失败，判断是否重试
            if attempt < total_attempts:
                logger.warning(f"目标会话准备未就绪，准备重试({attempt}/{total_attempts}): {customer_name}")
                self.page.wait_for_timeout(250 if not click_ok else 300)
                continue

        self._dump_chat_dom_snapshot(
            stage="target_conversation_not_ready",
            customer_name=customer_name,
            attempt=total_attempts,
        )
        self._set_last_target_prepare_error(f"target_conversation_not_ready:{customer_name}")
        logger.warning(f"目标会话准备失败，取消本次发送: {customer_name}")
        return False

    def _find_input_box(self, timeout: int = 5000):
        """查找输入框（优先选择真正的聊天编辑器，避免误选旧草稿区域）"""
        try:
            result = get_input_locator_service().locate(
                self.page,
                InputLocatorContext(scene="private_message"),
            )
            if result.success and result.input_candidate is not None:
                logger.info(
                    f"找到输入框: {result.input_candidate.selector} "
                    f"(score={result.input_candidate.score}, stage={result.fallback_stage})"
                )
                return result.input_candidate.locator
            logger.warning("未找到输入框")
            return None
        except Exception as e:
            logger.error(f"查找输入框失败: {e}")
            return None

    def _message_tail_has_advanced(
        self,
        before_snapshot: Optional[Dict[str, Any]],
        after_snapshot: Optional[Dict[str, Any]],
        original_content: str,
    ) -> bool:
        return message_tail_has_advanced(
            before_items=(before_snapshot or {}).get("messageTail") or [],
            after_items=(after_snapshot or {}).get("messageTail") or [],
            original_content=original_content,
            normalize_text=self._normalize_editor_text,
            extract_text=lambda item: (item or {}).get("text", ""),
        )

    def _platform_outbound_has_advanced(
        self,
        before_snapshot: Optional[Dict[str, Any]],
        after_snapshot: Optional[Dict[str, Any]],
        original_content: str,
    ) -> bool:
        return collection_has_advanced_match(
            before_items=[
                ((before_snapshot or {}).get("latestOutboundBubbleDebug") or {}).get("textContent", ""),
                ((before_snapshot or {}).get("latestMessageBubbleDebug") or {}).get("textContent", ""),
            ],
            after_items=[
                ((after_snapshot or {}).get("latestOutboundBubbleDebug") or {}).get("textContent", ""),
                ((after_snapshot or {}).get("latestMessageBubbleDebug") or {}).get("textContent", ""),
            ],
            original_content=original_content,
            normalize_text=self._normalize_editor_text,
            limit=0,
        )

    def _snapshot_matches_expected_target(
        self,
        snapshot: Optional[Dict[str, Any]],
        expected_customer_name: str = "",
    ) -> bool:
        expected = str(expected_customer_name or "").strip()
        if not expected:
            return True

        matched, candidates = self._match_target_conversation_candidates(
            (snapshot or {}).get("activeConversation"),
            expected,
        )
        if matched:
            return True

        if self._is_target_conversation_active(expected):
            return True

        logger.warning(
            f"发送验证：当前激活会话与目标不一致，expected={expected}, "
            f"active_candidates={candidates}"
        )
        return False

    def _verify_send_success(
        self,
        original_content: str,
        before_snapshot: Optional[Dict[str, Any]] = None,
        expected_customer_name: str = "",
        input_box=None,
    ) -> bool:
        """验证发送成功（检查输入框清空和消息出现在聊天区域）

        核心原则：宁可把“输入框已清空但回显尚未刷新”视为成功，也不要
        因验证误判而再次点击发送按钮或补发 Enter，避免真实双发。

        兜底策略：page.evaluate 不可用（snapshot/JS 调用全失败）时，
        一旦 input_box 已清空，立即判定为发送成功，避免误判漏发。
        """
        try:
            # [REFACTOR-INST:convergence] 统一埋点
            import time as _t
            debug_event(
                "DBG-VERIFY", "enter",
                content=original_content[:20],
                has_before=before_snapshot is not None,
                has_input_box=input_box is not None,
                expected=expected_customer_name,
                ts=_t.time(),
            )
            # [DEBUG-INST:greeting-no-reply] 兜底 fast path：input_box 仍能定位且已清空 → 立即成功
            # 适用场景：page.evaluate 不可用（snapshot/verify JS 全部超时）时，避免误判漏发
            try:
                if not self.page or self.page.is_closed():
                    return False
                _box = input_box
                if _box is None:
                    _box = self._find_input_box(timeout=1500)
                if _box is not None:
                    _current = self._read_input_box_content(_box)
                    if _current.strip() == "" and (original_content or "").strip():
                        # [REFACTOR-INST:convergence] 统一埋点
                        import time as _t2
                        debug_event(
                            "DBG-VERIFY", "FAST_PATH_INPUT_CLEARED",
                            content=original_content[:20],
                            ts=_t2.time(),
                        )
                        return True
            except Exception as _fast_e:
                # [REFACTOR-INST:convergence] 统一埋点
                debug_event("DBG-VERIFY", "fast_path_exception", err=str(_fast_e))
            input_box = self._find_input_box()
            if not input_box:
                logger.warning("验证发送：未重新定位到输入框，转而检查聊天区与消息尾部变化")
                self.page.wait_for_timeout(self.SEND_VERIFY_INITIAL_WAIT_MS)
                after_snapshot = self._collect_chat_dom_snapshot(
                    customer_name=expected_customer_name,
                    content=original_content,
                    stage="verify_send_no_input_box",
                    attempt=1,
                )
                if not self._snapshot_matches_expected_target(
                    after_snapshot,
                    expected_customer_name=expected_customer_name,
                ):
                    return False
                if before_snapshot is not None and self._platform_outbound_has_advanced(
                    before_snapshot,
                    after_snapshot,
                    original_content,
                ):
                    logger.info("发送验证：检测到平台侧新增 outbound 气泡，判定消息已发送")
                    return True
                if self._chat_contains_recent_outbound(original_content):
                    logger.info("输入框缺失但聊天区已出现发送内容，判定消息已发送")
                    return True
                if before_snapshot is not None:
                    if self._message_tail_has_advanced(before_snapshot, after_snapshot, original_content):
                        logger.info("输入框缺失但聊天消息尾部已推进，判定消息已发送")
                        return True
                return False

            self.page.wait_for_timeout(self.SEND_VERIFY_INITIAL_WAIT_MS)
            initial_snapshot = self._collect_chat_dom_snapshot(
                customer_name=expected_customer_name,
                content=original_content,
                stage="verify_send_initial",
                attempt=1,
            )
            if not self._snapshot_matches_expected_target(
                initial_snapshot,
                expected_customer_name=expected_customer_name,
            ):
                return False
            if before_snapshot is not None and self._platform_outbound_has_advanced(
                before_snapshot,
                initial_snapshot,
                original_content,
            ):
                logger.info("发送验证：检测到平台侧新增 outbound 气泡，判定消息已发送")
                return True
            current_content = self._read_input_box_content(input_box)
            initial_tail_advanced = bool(
                before_snapshot is not None
                and self._message_tail_has_advanced(before_snapshot, initial_snapshot, original_content)
            )
            initial_platform_outbound_advanced = bool(
                before_snapshot is not None
                and self._platform_outbound_has_advanced(before_snapshot, initial_snapshot, original_content)
            )
            initial_decision = decide_send_confirmation(
                input_cleared=current_content.strip() == "",
                tail_advanced=initial_tail_advanced,
                platform_outbound_seen=initial_platform_outbound_advanced,
            )
            if initial_decision.success:
                if initial_decision.reason_code in {"tail_advanced", "input_cleared_and_tail_advanced"}:
                    logger.info("发送验证：聊天尾部已推进，判定消息已发送")
                elif initial_decision.reason_code in {"platform_outbound_confirmed", "platform_outbound_seen"}:
                    logger.info("发送验证：平台侧 outbound 气泡已推进，判定消息已发送")
                else:
                    logger.info("发送验证：聊天尾部已出现目标消息，判定消息已发送")
                return True
            if self._chat_contains_recent_outbound(original_content):
                logger.info("聊天区已快速出现发送内容，判定消息已发送")
                return True

            current_content = ""
            for check_round, wait_ms in enumerate(self.SEND_VERIFY_WAIT_ROUNDS_MS, start=1):
                self.page.wait_for_timeout(wait_ms)
                round_snapshot = self._collect_chat_dom_snapshot(
                    customer_name=expected_customer_name,
                    content=original_content,
                    stage="verify_send_round",
                    attempt=check_round + 1,
                )
                if not self._snapshot_matches_expected_target(
                    round_snapshot,
                    expected_customer_name=expected_customer_name,
                ):
                    return False
                if before_snapshot is not None and self._platform_outbound_has_advanced(
                    before_snapshot,
                    round_snapshot,
                    original_content,
                ):
                    logger.info("发送验证：检测到平台侧新增 outbound 气泡，判定消息已发送")
                    return True
                current_content = self._read_input_box_content(input_box)
                round_tail_advanced = bool(
                    before_snapshot is not None
                    and self._message_tail_has_advanced(before_snapshot, round_snapshot, original_content)
                )
                round_platform_outbound_advanced = bool(
                    before_snapshot is not None
                    and self._platform_outbound_has_advanced(before_snapshot, round_snapshot, original_content)
                )
                round_decision = decide_send_confirmation(
                    input_cleared=current_content.strip() == "",
                    tail_advanced=round_tail_advanced,
                    platform_outbound_seen=round_platform_outbound_advanced,
                )
                if round_decision.success:
                    if round_decision.reason_code in {"tail_advanced", "input_cleared_and_tail_advanced"}:
                        logger.info("发送验证：聊天尾部已推进，判定消息已发送")
                    elif round_decision.reason_code in {"platform_outbound_confirmed", "platform_outbound_seen"}:
                        logger.info("发送验证：平台侧 outbound 气泡已推进，判定消息已发送")
                    else:
                        logger.info("发送验证：聊天尾部已出现目标消息，判定消息已发送")
                    return True
                if self._chat_contains_recent_outbound(original_content):
                    if current_content.strip():
                        logger.info("聊天区已出现发送内容，按发送成功处理，即使输入框仍有残留")
                    return True

            if before_snapshot is not None:
                after_snapshot = self._collect_chat_dom_snapshot(
                    customer_name=expected_customer_name,
                    content=original_content,
                    stage="verify_send_final",
                    attempt=len(self.SEND_VERIFY_WAIT_ROUNDS_MS) + 1,
                )
                if not self._snapshot_matches_expected_target(
                    after_snapshot,
                    expected_customer_name=expected_customer_name,
                ):
                    return False
                if self._platform_outbound_has_advanced(
                    before_snapshot,
                    after_snapshot,
                    original_content,
                ):
                    logger.info("发送验证：最终检测到平台侧新增 outbound 气泡，判定消息已发送")
                    return True
                if self._message_tail_has_advanced(before_snapshot, after_snapshot, original_content):
                    logger.info("聊天消息尾部发生提交变化，判定消息已发送")
                    return True

            # 输入框仍有内容且聊天区未出现发送结果，发送失败
            logger.debug(f"输入框仍有内容，发送失败: {current_content[:30]}...")
            return False
        except Exception as e:
            logger.warning(f"验证发送异常: {e}")
            return False

    def _chat_contains_recent_outbound(self, original_content: str) -> bool:
        """检查聊天区域是否已出现刚发送的内容。

        主路径：page.evaluate 扫描气泡节点。
        兜底路径：当 page.evaluate 不可用时，使用 Playwright 同步 API
        (locator.query_selector_all + element.text_content) 扫描气泡节点。
        """
        needle = (original_content or "").strip()[:30]
        if not needle:
            return False
        if not self.page or self.page.is_closed():
            return False
        verify_js = """
        (originalContent) => {
            const needle = (originalContent || '').trim().slice(0, 30);
            if (!needle) return false;
            const selectors = [
                '[data-e2e="msg-item-content"]',
                '[class*="messageItem"]', '[class*="msg-item"]',
                '[class*="chat-message"]', '[class*="message-text"]',
                '[class*="msgContent"]', '[class*="textMsg"]',
                '[data-e2e="chat-message-text"]',
                '[class*="messageMessageBoxcontentBox"]'
            ];
            for (const sel of selectors) {
                const elements = document.querySelectorAll(sel);
                for (const el of elements) {
                    const text = (el.textContent || '').trim();
                    if (text && text.includes(needle)) {
                        return true;
                    }
                }
            }
            const allText = (document.body && document.body.innerText) || '';
            return allText.includes(needle);
        }
        """
        # 主路径：JS evaluate
        try:
            result = self._safe_evaluate_with_args(verify_js, original_content, timeout_ms=3000)
            if result:
                return True
        except Exception as e:
            # [REFACTOR-INST:convergence] 统一埋点
            debug_event("DBG-CHAT-CHECK", "evaluate_failed", err=str(e))
        # 兜底：Playwright 同步 API 扫描气泡节点
        try:
            selectors = [
                '[data-e2e="msg-item-content"]',
                '[class*="messageItem"]', '[class*="msg-item"]',
                '[class*="chat-message"]', '[class*="message-text"]',
                '[class*="msgContent"]', '[class*="textMsg"]',
                '[data-e2e="chat-message-text"]',
                '[class*="messageMessageBoxcontentBox"]',
            ]
            for sel in selectors:
                try:
                    locator = self.page.locator(sel)
                    count = min(locator.count(), 80)
                    for idx in range(count):
                        try:
                            text = (locator.nth(idx).text_content() or "").strip()
                            if text and needle in text:
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
            # 兜底：抓整页文本
            try:
                body_text = (self.page.locator("body").text_content() or "")
                if needle in body_text:
                    return True
            except Exception:
                pass
        except Exception as e:
            # [REFACTOR-INST:convergence] 统一埋点
            debug_event("DBG-CHAT-CHECK", "fallback_failed", err=str(e))
        return False

    def _remove_zero_width_chars(self, text: str) -> str:
        """移除零宽字符和不可见字符"""
        if not text:
            return ""
        zero_width_chars = ['\u200b', '\u200c', '\u200d', '\u200e', '\u200f', 
                          '\ufeff', '\u2060', '\u2061', '\u2062', '\u2063',
                          '\u00ad', '\u034f', '\u061c', '\u17b4', '\u17b5']
        result = text
        for char in zero_width_chars:
            result = result.replace(char, '')
        return result.strip()

    def _recover_page(self):
        """恢复页面状态"""
        logger.info("尝试恢复页面状态...")
        self._chat_page_ensured = False
        self._update_page_state(PageState.NORMAL)

    def _calculate_retry_delay(self, attempt: int) -> float:
        """计算重试延迟"""
        base_delay = min(self.BASE_RETRY_DELAY * (2 ** attempt), self.MAX_RETRY_DELAY)
        jitter = base_delay * 0.1 * random.random()
        return base_delay + jitter

    def _is_duplicate_send(self, content_hash: str) -> bool:
        """检查是否重复发送"""
        with self._state_lock:
            if content_hash in self._sent_cache:
                if time.time() - self._sent_cache[content_hash] < self.SENT_MESSAGE_TTL:
                    return True
                del self._sent_cache[content_hash]
            
            expired_keys = [k for k, v in self._sent_cache.items() 
                          if time.time() - v > self.SENT_MESSAGE_TTL]
            for k in expired_keys:
                del self._sent_cache[k]
            
            if len(self._sent_cache) > self.MAX_SENT_CACHE_SIZE:
                sorted_items = sorted(self._sent_cache.items(), key=lambda x: x[1])
                remove_count = len(self._sent_cache) - self.MAX_SENT_CACHE_SIZE // 2
                for k, _ in sorted_items[:remove_count]:
                    del self._sent_cache[k]
            
            return False

    def _mark_sent(self, customer_name: str, content: str, content_hash: str):
        """标记消息已发送（统一入口，保留已有状态字段）
        
        修复：发送消息后将unread_count重置为0，防止未读计数一直保持导致重复触发回复。
        
        网络拦截增强：同时在 NetworkMessageDetector 中标记消息为已见，
        防止发送的消息被误判为入站消息（即使方向判定失败）。
        """
        with self._state_lock:
            current_time = time.time()
            existing = self._states.get(customer_name, {})
            dom_unread_before = existing.get('unread_count', 0)
            existing.update({
                'last_content': content,
                'last_time': current_time,
                'sent_by_us': True,
                'sent_by_us_at': current_time,
                'unread_count': 0,
                'last_detected_unread': 0,
                'dom_unread_at_send': dom_unread_before,
            })
            existing.update(self._merge_identity_fields(customer_name, existing))
            self._states[customer_name] = existing
            self._sent_cache[content_hash] = current_time
        
        # 网络拦截增强：在 NetworkMessageDetector 中标记消息为已见
        # 使用基于 customer_name+content 的鲁棒签名，防止发送的消息被误判为入站消息
        network_detector = getattr(self, '_network_detector', None)
        if network_detector:
            try:
                network_detector.mark_sent_message_seen(customer_name, content)
            except Exception as e:
                logger.debug(f"标记网络检测器已见签名失败: {e}")

    def _update_page_state(self, new_state: PageState):
        """更新页面状态（含回调通知，线程安全）"""
        old_state = None
        callback = None
        with self._state_lock:
            if self._page_state != new_state:
                old_state = self._page_state
                self._page_state = new_state
                callback = self._state_callback
        if old_state is not None:
            logger.info(f"页面状态变更: {old_state.value} -> {new_state.value}")
            if callback:
                try:
                    callback(new_state, f"页面状态从{old_state.value}变为{new_state.value}")
                except Exception as e:
                    logger.error(f"状态回调执行失败: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'page_state': self._page_state.value,
            'is_logged_in': self._login_valid,
            'states_count': len(self._states),
            'observer_active': self._observer_active,
            'monitoring': self._monitoring
        }
