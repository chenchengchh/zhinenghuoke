from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from loguru import logger

from src.web.outbound_send_verifier import OutboundDeliveryAttemptResult

if TYPE_CHECKING:
    from src.web.bot_service import BotService


class OutboundDeliveryOrchestrator:
    """统一承接出站投递尝试阶段的主流程编排。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service

    def attempt_reply_delivery(
        self,
        *,
        conversation_id: str,
        customer_name: str,
        reply_content: str,
        customer_id: str = "",
        platform: str = "douyin",
        intent_level: str = "E",
        intent_score: float = 0.0,
        smart_result: Optional[dict] = None,
        session=None,
        original_content: str = "",
        outbox_id: str = "",
        logical_message_id: str = "",
        reply_msg_id: str = "",
        allow_duplicate_content: bool = False,
        duplicate_as_success: bool = True,
        cancel_duplicate_reservation: bool = False,
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> OutboundDeliveryAttemptResult:
        bot = self.bot
        state_repository = bot._get_inbound_message_state_repository()

        current_outbox = {}
        if outbox_id:
            with contextlib.suppress(Exception):
                current_outbox = bot.db.get_outbox_event(outbox_id) or {}
        current_outbox_status = str(current_outbox.get("status", "") or "").strip().lower()
        if current_outbox_status in {"sent", "delivered", "skipped_duplicate", "cancelled"}:
            logger.info(
                f"[回复主链] outbox_already_finalized customer={customer_name} "
                f"conversation_id={conversation_id or '-'} outbox_id={outbox_id or '-'} "
                f"status={current_outbox_status or '-'}"
            )
            return OutboundDeliveryAttemptResult(
                success=True,
                status="deduplicated",
                message_id=reply_msg_id,
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                message=f"outbox_already_{current_outbox_status or 'finalized'}",
            )

        bot._ensure_outbox_event(
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            platform=platform,
            source_message_id=reply_msg_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

        duplicate_status = "deduplicated" if duplicate_as_success else "duplicate"

        if (not allow_duplicate_content) and bot._is_duplicate_reply_content(conversation_id, reply_content):
            if cancel_duplicate_reservation:
                try:
                    bot.db.cancel_reply_reservation(conversation_id, reply_content)
                except Exception as cancel_e:
                    logger.debug(f"取消重复回复预留失败: {cancel_e}")
            if outbox_id:
                state_repository.update_outbox_event(
                    outbox_id,
                    status="skipped_duplicate",
                    message_id=reply_msg_id,
                    reason="duplicate_reply",
                )
            return OutboundDeliveryAttemptResult(
                success=duplicate_as_success,
                status=duplicate_status,
                message_id=reply_msg_id,
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                message="duplicate_reply",
            )

        can_send, reason = bot.db.can_send_reply(
            conversation_id,
            min_interval_seconds=1,
            reply_content=reply_content,
            logical_message_id=logical_message_id,
            source_message_id=reply_msg_id,
            allow_duplicate_content=allow_duplicate_content,
        )
        if not can_send:
            duplicate_reason = any(
                keyword in reason
                for keyword in (
                    "完全相同",
                    "高度相似",
                    "逻辑消息ID与最近发送记录相同",
                    "源消息ID与最近发送记录相同",
                )
            )
            if duplicate_reason:
                if cancel_duplicate_reservation:
                    try:
                        bot.db.cancel_reply_reservation(conversation_id, reply_content)
                    except Exception as cancel_e:
                        logger.debug(f"取消重复回复预留失败: {cancel_e}")
                if outbox_id:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="skipped_duplicate",
                        message_id=reply_msg_id,
                        reason=reason[:100],
                    )
                return OutboundDeliveryAttemptResult(
                    success=duplicate_as_success,
                    status=duplicate_status,
                    message_id=reply_msg_id,
                    outbox_id=outbox_id,
                    logical_message_id=logical_message_id,
                    message=reason[:100],
                )

            return OutboundDeliveryAttemptResult(
                success=False,
                status="blocked",
                message_id=reply_msg_id,
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                message=reason[:100],
                failure_reason=reason[:100],
                block_category=bot._get_outbound_send_verifier().classify_block_category(reason),
            )

        reply_msg = {
            "message_id": reply_msg_id,
            "logical_message_id": logical_message_id,
            "source_message_id": reply_msg_id,
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "platform": platform,
            "direction": "outbound",
            "message_type": "text",
            "content": reply_content,
            "sender_id": "self",
            "sender_name": "我",
            "is_read": True,
            "is_processed": True,
            "created_at": datetime.now().isoformat(),
        }
        payload = smart_result or {
            "priority_level": "P1",
            "risk_assessment": {"overall_level": "medium"},
            "sentiment": "neutral",
            "suggested_action": "",
        }
        send_result = bot._unified_send_reply_result(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            reply_msg_id=reply_msg_id,
            reply_msg=reply_msg,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=float(intent_score or 0.0),
            smart_result=payload,
            session=session,
            original_content=original_content or reply_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )
        if send_result.success:
            return OutboundDeliveryAttemptResult(
                success=True,
                status="sent",
                message_id=reply_msg_id,
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                reply_msg=reply_msg,
            )

        actual_target_name = bot._get_last_send_target_name(customer_name)
        failure_reason = str(send_result.reason or bot._get_last_send_error("reply_send_failed"))
        non_retryable_reason = bot._classify_non_retryable_send_failure(actual_target_name, failure_reason)
        if non_retryable_reason:
            return OutboundDeliveryAttemptResult(
                success=False,
                status="failed_non_retryable",
                message_id=reply_msg_id,
                outbox_id=outbox_id,
                logical_message_id=logical_message_id,
                message=non_retryable_reason,
                failure_reason=failure_reason[:100],
            )

        return OutboundDeliveryAttemptResult(
            success=False,
            status="failed_retryable",
            message_id=reply_msg_id,
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            message=f"send_failed:{failure_reason[:80]}",
            failure_reason=failure_reason[:100],
        )
