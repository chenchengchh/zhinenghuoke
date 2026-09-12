"""发送目标解析与会话切换公共服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.web.bot_service import BotService


@dataclass(frozen=True)
class ResolvedSendTarget:
    requested_name: str
    resolved_name: str
    source: str
    identity_level: str = "name_fuzzy"
    conversation_id: str = ""
    customer_id: str = ""
    platform: str = "douyin"


class ConversationSwitchService:
    """统一封装发送前目标会话解析。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service

    def resolve_send_target(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> ResolvedSendTarget:
        bot = self.bot
        normalized_requested_name = bot._normalize_live_target_name(customer_name)
        resolved_name, target_source = bot._resolve_live_send_target_name(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        resolved_name = resolved_name or customer_name
        normalized_resolved_name = bot._normalize_live_target_name(resolved_name)
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_customer_id = str(customer_id or "").strip()
        identity_level = "name_fuzzy"
        if normalized_conversation_id:
            identity_level = "strict_conversation_id"
        elif normalized_customer_id and not normalized_customer_id.startswith("temp_"):
            identity_level = "strict_customer_id"
        else:
            live_names = bot._get_live_conversation_name_samples(limit=20)
            normalized_live_names = {
                bot._normalize_live_target_name(live_name)
                for live_name in live_names
                if str(live_name or "").strip()
            }
            if normalized_resolved_name and normalized_resolved_name in normalized_live_names:
                if target_source == "alias" and normalized_resolved_name != normalized_requested_name:
                    identity_level = "alias_match"
                else:
                    identity_level = "exact_name"
            elif target_source == "alias":
                identity_level = "alias_match"
        if resolved_name != customer_name:
            logger.info(
                f"发送目标已纠正: original={customer_name}, resolved={resolved_name}, "
                f"source={target_source}, identity_level={identity_level}, "
                f"conversation_id={conversation_id}"
            )
        return ResolvedSendTarget(
            requested_name=customer_name,
            resolved_name=resolved_name,
            source=target_source,
            identity_level=identity_level,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

    def ensure_monitor_send_target(self, customer_name: str) -> bool:
        """为传统 monitor 发送提前准备目标会话。"""
        return self.ensure_monitor_send_target_with_identity(customer_name=customer_name)

    def ensure_monitor_send_target_with_identity(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """为传统 monitor 发送提前准备目标会话，优先消费稳定身份信息。"""
        bot = self.bot
        monitor = getattr(bot, "message_monitor", None)
        if not monitor:
            return False

        resolved = self.resolve_send_target(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        resolved_name = resolved.resolved_name or customer_name
        candidate_builder = getattr(bot, "_collect_send_target_candidates", None)
        target_candidates = (
            candidate_builder(
                customer_name=customer_name,
                conversation_id=resolved.conversation_id or conversation_id,
                customer_id=resolved.customer_id or customer_id,
                platform=resolved.platform or platform,
            )
            if callable(candidate_builder)
            else [resolved_name or customer_name]
        )
        if resolved_name and resolved_name not in target_candidates:
            target_candidates = [resolved_name, *target_candidates]

        coordinator_getter = getattr(bot, "_get_chat_session_coordinator", None)
        coordinator = coordinator_getter() if callable(coordinator_getter) else None
        if coordinator is not None:
            if coordinator.ensure_chat_page(monitor=monitor):
                if not resolved_name:
                    return True
                if coordinator.activate_conversation(
                    resolved_name,
                    conversation_id=resolved.conversation_id or conversation_id,
                    customer_id=resolved.customer_id or customer_id,
                    target_candidates=target_candidates,
                ):
                    return True
                logger.warning("协调层切换目标会话失败，回退到 monitor 旧路径")
            else:
                logger.warning("协调层未能确保聊天页面，回退到 monitor 旧路径")

        chat_page_ready = False
        if hasattr(monitor, "ensure_chat_page"):
            chat_page_ready = monitor.ensure_chat_page()
        elif hasattr(monitor, "_ensure_chat_page"):
            chat_page_ready = monitor._ensure_chat_page()

        if not chat_page_ready:
            logger.error("无法确保聊天页面就绪")
            return False

        if not resolved_name:
            return True

        click_conversation = getattr(monitor, "click_conversation", None)
        if not callable(click_conversation):
            return True

        click_kwargs = {
            "conversation_id": resolved.conversation_id or conversation_id,
            "customer_id": resolved.customer_id or customer_id,
            "target_candidates": target_candidates,
        }
        clicked = click_conversation(resolved_name, **click_kwargs)
        if clicked:
            return True

        page = getattr(monitor, "page", None)
        if page:
            try:
                page.evaluate(
                    "() => { const list = document.querySelector('[class*=\"conversationList\"]'); "
                    "if(list) list.scrollTop = list.scrollHeight; }"
                )
                page.wait_for_timeout(1000)
            except Exception:
                pass

        return bool(click_conversation(resolved_name, **click_kwargs))

    def ensure_rpa_send_target(
        self,
        customer_name: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """为 RPA 发送提前准备目标会话。"""
        if not customer_name:
            return False

        launcher = getattr(self.bot, "rpa_launcher", None)
        if not launcher:
            return False

        prepare_target = getattr(launcher, "ensure_send_target", None)
        if not callable(prepare_target):
            return True

        resolved = self.resolve_send_target(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        resolved_name = resolved.resolved_name or customer_name
        candidate_builder = getattr(self.bot, "_collect_send_target_candidates", None)
        target_candidates = (
            candidate_builder(
                customer_name=customer_name,
                conversation_id=resolved.conversation_id or conversation_id,
                customer_id=resolved.customer_id or customer_id,
                platform=resolved.platform or platform,
            )
            if callable(candidate_builder)
            else [resolved_name or customer_name]
        )
        if resolved_name and resolved_name not in target_candidates:
            target_candidates = [resolved_name, *target_candidates]

        return bool(
            prepare_target(
                resolved_name,
                conversation_id=resolved.conversation_id or conversation_id,
                customer_id=resolved.customer_id or customer_id,
                identity_level=resolved.identity_level,
                target_candidates=target_candidates,
            )
        )
