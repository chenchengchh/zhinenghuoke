"""聊天页与会话切换协调层。

阶段1收口目标：
- BotService 不再直接关心"怎么切会话、怎么找聊天页"，只调 coordinator。
- 所有页面获取、聊天页确保、会话切换、监听页恢复统一经过本层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from src.douyin_bot.message_monitor import MessageMonitor
    from src.web.bot_service import BotService


class ChatSessionCoordinator:
    """统一封装聊天页确保、目标会话激活与监听页恢复。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service

    # ==================== 统一入口 ====================

    def ensure_chat_page(
        self,
        monitor: "MessageMonitor | None" = None,
        *,
        force_new: bool = False,
    ) -> bool:
        """统一聊天页确保入口。

        等价于旧链路中 MessageMonitor._ensure_chat_page()，
        但所有页面获取和导航恢复逻辑收口到 BrowserManager，
        本层只做编排和状态同步。
        """
        browser_manager = getattr(self.bot, "browser_manager", None)
        if not browser_manager:
            logger.warning("ensure_chat_page: browser_manager 不存在")
            return False

        page = self._acquire_monitor_page(browser_manager, force_new=force_new)
        if not page:
            return False

        if not self._verify_page_on_chat_url(page):
            page = self._navigate_page_to_chat(browser_manager, page)
            if not page:
                return False

        if monitor is not None:
            self._sync_monitor_page_state(monitor, page)
        return True

    # ==================== 页面获取 ====================

    def ensure_monitor_chat_page(
        self,
        monitor: "MessageMonitor | None" = None,
        *,
        force_new: bool = False,
    ) -> bool:
        """确保监听链当前绑定到真实可用的聊天页。"""
        browser_manager = getattr(self.bot, "browser_manager", None)
        if not browser_manager:
            logger.warning("聊天页协调失败：browser_manager 不存在")
            return False

        page = self._acquire_monitor_page(browser_manager, force_new=force_new)
        if not page:
            logger.warning("聊天页协调失败：未获取到 monitor page")
            return False

        try:
            if page.is_closed():
                logger.warning("聊天页协调失败：monitor page 已关闭")
                return False
        except Exception:
            logger.warning("聊天页协调失败：monitor page 状态不可用")
            return False

        if monitor is not None:
            self._sync_monitor_page_state(monitor, page)
        return True

    def get_monitor_page(self) -> Optional["Page"]:
        """获取当前 monitor page 引用，不触发创建。"""
        browser_manager = getattr(self.bot, "browser_manager", None)
        if not browser_manager:
            return None
        page = getattr(browser_manager, "monitor_page", None)
        if page and not page.is_closed():
            return page
        return None

    # ==================== 会话切换 ====================

    def activate_conversation(
        self,
        identity: str,
        *,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """确保聊天页就绪后激活目标会话。

        统一入口：BotService 和其他模块只需调用此方法，
        不需要知道底层是 monitor 还是 RPA 在做会话切换。
        """
        monitor = getattr(self.bot, "message_monitor", None)
        if not monitor:
            logger.warning("切换目标会话失败：message_monitor 不存在")
            return False

        if not self.ensure_chat_page(monitor=monitor):
            return False

        click_conversation = getattr(monitor, "click_conversation", None)
        if not callable(click_conversation):
            logger.warning("切换目标会话失败：message_monitor.click_conversation 不可用")
            return False

        return bool(
            click_conversation(
                identity,
                allow_search_filter=allow_search_filter,
                conversation_id=conversation_id,
                customer_id=customer_id,
                target_candidates=target_candidates,
            )
        )

    def activate_monitor_conversation(
        self,
        customer_name: str,
        *,
        allow_search_filter: bool = True,
        conversation_id: str = "",
        customer_id: str = "",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """兼容旧调用路径，委托到 activate_conversation。"""
        return self.activate_conversation(
            customer_name,
            allow_search_filter=allow_search_filter,
            conversation_id=conversation_id,
            customer_id=customer_id,
            target_candidates=target_candidates,
        )

    def verify_active_conversation(self, identity: str) -> bool:
        """校验当前聊天窗口已切到目标会话。"""
        monitor = getattr(self.bot, "message_monitor", None)
        if not monitor:
            return False
        verifier = getattr(monitor, "_is_target_conversation_active", None)
        if not callable(verifier):
            return False
        return bool(verifier(identity))

    def verify_monitor_conversation(self, customer_name: str) -> bool:
        """兼容旧调用路径，委托到 verify_active_conversation。"""
        return self.verify_active_conversation(customer_name)

    # ==================== 监听页恢复 ====================

    def recover_monitor_page(self, closed_page: Any):
        """监听页关闭后恢复新的 monitor page。"""
        browser_manager = getattr(self.bot, "browser_manager", None)
        if not browser_manager:
            return None

        try:
            if getattr(browser_manager, "monitor_page", None) is closed_page:
                browser_manager.monitor_page = None
        except Exception:
            return None

        page = self._acquire_monitor_page(browser_manager, force_new=False)
        if not page:
            return None

        try:
            if page.is_closed():
                return None
        except Exception:
            return None

        collapse_pages = getattr(self.bot, "_collapse_monitoring_to_single_page", None)
        if callable(collapse_pages):
            collapse_pages(page)
        return page

    # ==================== 底层能力（从 MessageMonitor 抽取） ====================

    def _acquire_monitor_page(
        self,
        browser_manager: Any,
        *,
        force_new: bool = False,
    ) -> Optional["Page"]:
        """从 BrowserManager 获取 monitor page，优先使用 ensure_monitor_chat_page。"""
        ensure_page = getattr(browser_manager, "ensure_monitor_chat_page", None)
        if callable(ensure_page):
            try:
                return ensure_page(force_new=force_new)
            except Exception as exc:
                logger.warning(f"ensure_monitor_chat_page 失败: {exc}")

        legacy_getter = getattr(browser_manager, "get_monitor_page", None)
        if callable(legacy_getter):
            try:
                return legacy_getter(force_new=force_new)
            except Exception as exc:
                logger.warning(f"get_monitor_page 失败: {exc}")

        return None

    @staticmethod
    def _verify_page_on_chat_url(page: "Page") -> bool:
        """检查页面是否已在抖音聊天页 URL 上。"""
        try:
            url = str(page.url or "").strip()
            return "douyin.com" in url and "/chat" in url
        except Exception:
            return False

    @staticmethod
    def _navigate_page_to_chat(
        browser_manager: Any,
        page: "Page",
    ) -> Optional["Page"]:
        """将页面导航到聊天页，成功后通过 ensure_monitor_chat_page 刷新引用。"""
        from src.config.settings import DOUYIN_CHAT_URL

        try:
            logger.info("协调层：页面不在聊天页，尝试导航恢复...")
            page.goto(
                DOUYIN_CHAT_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                page.wait_for_selector(
                    '[data-e2e="conversation-item"], [class*="conversationItem"]',
                    timeout=5000,
                )
            except Exception:
                page.wait_for_timeout(1500)

            ensure_page = getattr(browser_manager, "ensure_monitor_chat_page", None)
            if callable(ensure_page):
                refreshed = ensure_page(force_new=False)
                if refreshed:
                    return refreshed

            return page
        except Exception as exc:
            logger.warning(f"协调层：导航到聊天页失败: {exc}")
            return None

    @staticmethod
    def _sync_monitor_page_state(monitor: "MessageMonitor", page: Any) -> None:
        """让 MessageMonitor 与当前 monitor page 状态保持一致。"""
        monitor.page = page
        monitor._chat_page = page
        monitor._chat_page_ensured = True
        monitor._ensure_chat_page_attempts = 0
        monitor._ensure_failed_logged = False
