from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional

from loguru import logger


@dataclass(frozen=True)
class OutboundDeliveryAttemptResult:
    success: bool
    status: str
    message_id: str
    outbox_id: str
    logical_message_id: str
    message: str = ""
    failure_reason: str = ""
    reply_msg: dict[str, Any] | None = None
    block_category: str = ""

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass(frozen=True)
class WorkflowDeliveryDecision:
    action: str
    success: bool
    status: str
    message: str
    pause_reason: str = ""
    deduplicated: bool = False
    block_category: str = ""


@dataclass(frozen=True)
class SendVerification:
    verified: bool
    confidence: str
    notes: str


@dataclass(frozen=True)
class FailureDiagnosis:
    category: str
    is_retryable: bool
    detail: str
    suggested_action: str
    reason_code: str = ""


_NON_RETRYABLE_PATTERNS = (
    "消息已发送，请勿重复",
    "identity_conflict_blocked",
    "failed_target_mismatch",
    "target_not_in_live_conversations",
    "rpa_launcher_unavailable",
    "message_monitor_uninitialized",
    "message_monitor_page_uninitialized",
    "message_monitor_page_closed",
)

_UNCERTAIN_PATTERNS = (
    "发送超时：页面线程未处理",
    "发送结果丢失",
    "rpa_exception",
    "monitor_exception",
)


class OutboundSendVerifier:
    """统一承接发送结果验证、失败诊断与重试判定。"""

    def __init__(
        self,
        *,
        normalize_live_target_name: Callable[[str], str],
        get_live_conversation_name_samples: Callable[..., List[str]],
    ):
        self._normalize_live_target_name = normalize_live_target_name
        self._get_live_conversation_name_samples = get_live_conversation_name_samples

    def verify_send_success(
        self,
        send_result: Any,
        *,
        customer_name: str = "",
    ) -> SendVerification:
        """验证发送结果是否真正成功。

        判定逻辑：
        1. success=True + duplicate_guard_hit → 已发送（高置信）
        2. success=True + channel=rpa/monitor → 成功（高置信）
        3. success=False + 不确定模式 → 不确定（低置信）
        4. success=False + 明确失败 → 失败（高置信）
        """
        success = bool(getattr(send_result, "success", False))
        duplicate_guard = bool(getattr(send_result, "duplicate_guard_hit", False))
        channel = str(getattr(send_result, "channel", "") or "")
        reason = str(getattr(send_result, "reason", "") or "")

        if success and duplicate_guard:
            return SendVerification(
                verified=True,
                confidence="high",
                notes="duplicate_guard_hit:消息已发送，按成功处理",
            )

        if success:
            return SendVerification(
                verified=True,
                confidence="high",
                notes=f"channel={channel}",
            )

        if any(p in reason.lower() for p in _UNCERTAIN_PATTERNS):
            return SendVerification(
                verified=False,
                confidence="low",
                notes=f"uncertain:reason={reason[:120]}",
            )

        return SendVerification(
            verified=False,
            confidence="high",
            notes=f"failed:channel={channel},reason={reason[:120]}",
        )

    def diagnose_failure(
        self,
        send_result: Any,
        *,
        customer_name: str = "",
    ) -> FailureDiagnosis:
        """诊断发送失败原因，返回结构化诊断结果。"""
        reason = str(getattr(send_result, "reason", "") or "")
        channel = str(getattr(send_result, "channel", "") or "")
        normalized_reason = reason.strip().lower()

        if not normalized_reason:
            return FailureDiagnosis(
                category="unknown",
                is_retryable=True,
                detail="无明确失败原因",
                suggested_action="retry",
                reason_code="unknown_failure",
            )

        non_retryable = self.classify_non_retryable_failure(customer_name, reason)
        if non_retryable:
            if "already_sent" in non_retryable:
                return FailureDiagnosis(
                    category="duplicate",
                    is_retryable=False,
                    detail=non_retryable,
                    suggested_action="skip",
                    reason_code="duplicate_guard",
                )
            if "identity_conflict" in non_retryable:
                return FailureDiagnosis(
                    category="identity_blocked",
                    is_retryable=False,
                    detail=non_retryable,
                    suggested_action="skip",
                    reason_code="identity_conflict_blocked",
                )
            if "target_not_in_live" in non_retryable:
                return FailureDiagnosis(
                    category="target_missing",
                    is_retryable=False,
                    detail=non_retryable,
                    suggested_action="skip",
                    reason_code="target_not_in_live_conversations",
                )
            if "send_result_uncertain" in non_retryable:
                return FailureDiagnosis(
                    category="uncertain",
                    is_retryable=True,
                    detail=non_retryable,
                    suggested_action="pause_and_retry",
                    reason_code="send_result_uncertain",
                )
            return FailureDiagnosis(
                category="non_retryable",
                is_retryable=False,
                detail=non_retryable,
                suggested_action="skip",
                reason_code="non_retryable_failure",
            )

        if any(p in normalized_reason for p in _UNCERTAIN_PATTERNS):
            return FailureDiagnosis(
                category="uncertain",
                is_retryable=True,
                detail=f"reason={reason[:120]}",
                suggested_action="retry_with_backoff",
                reason_code="send_result_uncertain",
            )

        if "click_conversation" in normalized_reason or "conversation_not_found" in normalized_reason:
            return FailureDiagnosis(
                category="target_switch_failed",
                is_retryable=True,
                detail=f"channel={channel},reason={reason[:120]}",
                suggested_action="retry_with_target_refresh",
                reason_code="target_switch_failed",
            )

        return FailureDiagnosis(
            category="transient",
            is_retryable=True,
            detail=f"channel={channel},reason={reason[:120]}",
            suggested_action="retry",
            reason_code="transient_send_failure",
        )

    def is_retryable(self, failure_reason: str) -> bool:
        """判断失败原因是否可重试。"""
        normalized = str(failure_reason or "").strip().lower()
        if not normalized:
            return True

        if any(p.lower() in normalized for p in _NON_RETRYABLE_PATTERNS):
            return False

        return True

    def classify_non_retryable_failure(self, customer_name: str, failure_reason: str) -> str:
        normalized_reason = str(failure_reason or "").strip().lower()
        if not normalized_reason:
            return ""
        if "消息已发送，请勿重复" in normalized_reason:
            return "already_sent:duplicate_guard"
        if "发送超时：页面线程未处理" in normalized_reason:
            return "send_result_uncertain:rpa_timeout"
        if "发送结果丢失" in normalized_reason:
            return "send_result_uncertain:result_lost"
        if "identity_conflict_blocked" in normalized_reason:
            return "identity_conflict_blocked:send_target_mismatch"
        click_failed = (
            "monitor_click_conversation_failed" in normalized_reason
            or "conversation_not_found" in normalized_reason
            or "conversation_active_mismatch" in normalized_reason
            or "failed_target_mismatch" in normalized_reason
            or "无法点击会话" in normalized_reason
        )
        if not click_failed:
            return ""

        normalized_target = self._normalize_live_target_name(customer_name)
        if not normalized_target:
            return ""

        live_names = self._get_live_conversation_name_samples(limit=20)
        if not live_names:
            return ""

        normalized_live = [self._normalize_live_target_name(name) for name in live_names]
        if any(
            candidate == normalized_target
            or candidate.startswith(normalized_target)
            or normalized_target.startswith(candidate)
            for candidate in normalized_live
            if candidate
        ):
            return ""

        logger.warning(
            f"检测到非重试型发送失败: target={customer_name}, "
            f"live_names={live_names}, failure_reason={failure_reason}"
        )
        return f"target_not_in_live_conversations:{customer_name}"

    @staticmethod
    def classify_block_category(block_reason: str) -> str:
        normalized_reason = str(block_reason or "").strip().lower()
        if not normalized_reason:
            return ""
        if "发送间隔过短" in normalized_reason or "cooldown" in normalized_reason:
            return "cooldown"
        if "完全相同" in normalized_reason:
            return "duplicate_exact"
        if "高度相似" in normalized_reason:
            return "duplicate_similar"
        if "逻辑消息id" in normalized_reason or "源消息id" in normalized_reason:
            return "reservation_conflict"
        if "重复" in normalized_reason:
            return "duplicate_guard"
        return "policy_blocked"

    def resolve_workflow_delivery_decision(
        self,
        attempt_result: Mapping[str, Any] | OutboundDeliveryAttemptResult,
        *,
        default_failure_reason: str = "send_failed:reply_send_failed",
    ) -> WorkflowDeliveryDecision:
        status = str(attempt_result.get("status", "") or "")
        message = str(attempt_result.get("message", "") or "")
        block_category = str(attempt_result.get("block_category", "") or "")

        if status == "deduplicated":
            return WorkflowDeliveryDecision(
                action="finalize_success",
                success=True,
                status="deduplicated",
                message="",
                deduplicated=True,
            )
        if status == "sent":
            return WorkflowDeliveryDecision(
                action="finalize_success",
                success=True,
                status="sent",
                message="",
                deduplicated=False,
            )
        if status == "blocked":
            pause_reason = f"send_blocked:{message[:80]}"
            return WorkflowDeliveryDecision(
                action="pause",
                success=False,
                status="paused",
                message=pause_reason,
                pause_reason=pause_reason,
                block_category=block_category or self.classify_block_category(message),
            )
        if status == "failed_non_retryable":
            pause_reason = message
            return WorkflowDeliveryDecision(
                action="pause",
                success=False,
                status="paused",
                message=pause_reason,
                pause_reason=pause_reason,
            )

        pause_reason = str(message or default_failure_reason)
        return WorkflowDeliveryDecision(
            action="pause",
            success=False,
            status="paused",
            message=pause_reason,
            pause_reason=pause_reason,
        )
