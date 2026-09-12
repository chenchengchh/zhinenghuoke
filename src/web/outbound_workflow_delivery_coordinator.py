from __future__ import annotations

from typing import Any, Callable


class OutboundWorkflowDeliveryCoordinator:
    """统一承接工作流发送成功/暂停的状态收口执行。"""

    def __init__(
        self,
        *,
        cancel_pending_retry_timer: Callable[..., Any],
        state_repository: Any,
        workflow_manager: Any,
    ):
        self._cancel_pending_retry_timer = cancel_pending_retry_timer
        self._state_repository = state_repository
        self._workflow_manager = workflow_manager

    def finalize_success(
        self,
        *,
        workflow_run_id: str,
        inbound_logical_message_id: str,
        outbox_id: str,
        reply_msg_id: str,
        trigger: str,
        deduplicated: bool = False,
        inbound_reason_override: str = "",
        bind_outbox: bool = False,
    ) -> None:
        self._cancel_pending_retry_timer(
            outbox_id=outbox_id,
            workflow_run_id=workflow_run_id,
            logical_message_id=inbound_logical_message_id,
        )
        if inbound_logical_message_id:
            reason = (
                inbound_reason_override
                or ("duplicate_reply_skipped" if deduplicated else f"{trigger}_reply_sent")
            )
            finalize_kwargs = {"status": "done", "reason": reason}
            if not deduplicated:
                finalize_kwargs["reply_message_id"] = reply_msg_id
            self._state_repository.finalize_inbound_event_safe(inbound_logical_message_id, **finalize_kwargs)

        if workflow_run_id:
            if bind_outbox and outbox_id:
                self._state_repository.bind_workflow_outbox(workflow_run_id, outbox_id)
            success_reason = (
                inbound_reason_override
                or ("duplicate_reply_skipped" if deduplicated else "reply_sent")
            )
            metadata = {
                "outbox_id": outbox_id,
                "trigger": trigger,
                "reason": success_reason,
                "reason_code": success_reason,
                "workflow_action": "send",
            }
            if deduplicated:
                metadata["deduplicated"] = True
            else:
                metadata["message_id"] = reply_msg_id
            self._workflow_manager.record_node(
                workflow_run_id,
                "send_reply",
                status="success",
                metadata=metadata,
            )
            self._workflow_manager.complete_run(workflow_run_id, message_id=reply_msg_id)

    def pause_delivery(
        self,
        *,
        workflow_run_id: str,
        outbox_id: str,
        reply_msg_id: str,
        reply_content: str,
        reason: str,
        bind_outbox: bool = False,
    ) -> None:
        self._cancel_pending_retry_timer(outbox_id=outbox_id, workflow_run_id=workflow_run_id)
        self._state_repository.update_outbox_event(
            outbox_id,
            status="cancelled",
            message_id=reply_msg_id,
            reason=reason[:100],
            workflow_run_id=workflow_run_id,
        )
        if workflow_run_id:
            if bind_outbox and outbox_id:
                self._state_repository.bind_workflow_outbox(workflow_run_id, outbox_id)
            self._workflow_manager.pause_for_human(
                workflow_run_id,
                reason=reason[:100],
                suggested_reply=reply_content,
                outbox_id=outbox_id,
                eligibility_action="pause_for_human",
            )
