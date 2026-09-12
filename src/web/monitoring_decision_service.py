"""消息监听放行判定服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from src.web.bot_service import BotService


@dataclass(frozen=True)
class MonitoringDecision:
    accepted: bool
    reason: str
    customer_name: str = ""
    content: str = ""
    normalized_direction: str = "unknown"


class MonitoringDecisionService:
    """集中处理实时监听消息的放行与跳过判定。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service

    def evaluate_live_inbound_message(
        self,
        *,
        source: str,
        customer_name: str,
        content: str,
        direction: Any = "inbound",
        is_new: bool = True,
        msg_id: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        apply_self_reply_guard: bool = False,
        direction_confidence: str = "high",
    ) -> MonitoringDecision:
        bot = self.bot
        normalized_direction = bot._normalize_direction(direction, default="unknown")
        mark_skip_kwargs = {
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "logical_message_id": logical_message_id,
            "source_message_id": source_message_id or msg_id,
        }
        if normalized_direction != "inbound":
            if customer_name and content:
                bot._mark_message_skipped(customer_name, content, "outbound", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "non_inbound", normalized_direction=normalized_direction)

        if not is_new:
            if customer_name and content:
                bot._mark_message_skipped(customer_name, content, "not_new", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "not_new", normalized_direction=normalized_direction)

        customer_name = (customer_name or "").strip()
        content = (content or "").strip()
        if not customer_name:
            return MonitoringDecision(False, "empty_customer", normalized_direction=normalized_direction)
        if not content:
            return MonitoringDecision(False, "too_short", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if bot._is_invalid_message(content):
            bot._mark_message_skipped(customer_name, content, "invalid", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "invalid", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if content.startswith("我:") and len(content) <= 3:
            bot._mark_message_skipped(customer_name, content, "short_prefix", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "short_prefix", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if bot._is_message_processed(
            customer_name,
            content,
            msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id or msg_id,
            direction=normalized_direction,
        ):
            trace_id = str(logical_message_id or source_message_id or msg_id or "")[:12]
            logger.debug(
                f"{source}消息已处理过，跳过: trace={trace_id or '-'} customer={customer_name} "
                f"conversation_id={conversation_id or '-'} customer_id={customer_id or '-'} "
                f"source_message_id={(source_message_id or msg_id) or '-'} "
                f"logical_message_id={logical_message_id or '-'}"
            )
            return MonitoringDecision(False, "duplicate", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if bot._looks_like_bot_message(content):
            bot._mark_message_skipped(customer_name, content, "bot_pattern", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "bot_pattern", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if bot._is_echo_message(
            customer_name,
            content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform="douyin",
        ):
            bot._mark_message_skipped(customer_name, content, "echo", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "echo", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if bot._is_recently_sent_by_us(customer_name, content):
            bot._mark_message_skipped(customer_name, content, "self_sent", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "self_sent", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        if apply_self_reply_guard and bot._is_self_reply_content(customer_name, content):
            bot._mark_message_skipped(customer_name, content, "self_reply_pattern", msg_id, **mark_skip_kwargs)
            return MonitoringDecision(False, "self_reply_pattern", customer_name=customer_name, content=content, normalized_direction=normalized_direction)

        return MonitoringDecision(
            True,
            "accepted",
            customer_name=customer_name,
            content=content,
            normalized_direction=normalized_direction,
        )
