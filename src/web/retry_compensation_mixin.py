"""
RetryCompensationMixin - 重试补偿机制

从 BotService 中提取的出站补偿重试调度、延迟重试执行、工作流投递等方法。
Mixin 中的方法通过 self 访问 BotService 的属性和其他方法，保持100%向后兼容。
"""
import time
import hashlib
import contextlib
import threading

from datetime import datetime, timedelta
from typing import Optional, Any
from loguru import logger
from src.common.logical_message import build_logical_message_id


class RetryCompensationMixin:
    """重试补偿机制 Mixin"""

    def _resume_persisted_retry_workflows(self, limit: int = 20) -> int:
        """恢复持久化的 waiting_retry/retry_pending 任务。"""
        monitoring_status = self._get_monitoring_status()
        if not bool(monitoring_status.get("effective_monitoring")):
            return 0

        try:
            waiting_runs = list(self.db.list_workflow_runs(statuses=["waiting_retry"], limit=max(int(limit or 0), 1) * 4) or [])
        except Exception as e:
            logger.debug(f"读取 waiting_retry 工作流失败: {e}")
            return 0

        restored = 0
        now = datetime.now()
        for run in waiting_runs:
            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            outbox_id = str(run.get("outbox_id", "") or "").strip()
            if not workflow_run_id or not outbox_id:
                continue

            outbox_event = self.db.get_outbox_event(outbox_id) or {}
            if str(outbox_event.get("status", "") or "").strip().lower() != "retry_pending":
                continue
            if not str(outbox_event.get("reply_content", "") or "").strip():
                continue

            if not self._allow_background_delivery_without_new_inbound("outbox_retry"):
                self._freeze_auto_delivery_work_item(
                    outbox_id=outbox_id,
                    workflow_run_id=workflow_run_id,
                    reason="await_new_inbound:outbox_retry",
                    suggested_reply=str(outbox_event.get("reply_content", "") or ""),
                )
                continue

            retry_key = self._build_retry_timer_key(
                outbox_id=outbox_id,
                workflow_run_id=workflow_run_id,
                logical_message_id=str(run.get("logical_message_id", "") or ""),
            )
            if self._has_pending_retry_timer(retry_key):
                continue

            next_retry_at = self._parse_iso_datetime(str(outbox_event.get("next_retry_at", "") or ""))
            delay_seconds = 0.0
            if next_retry_at is not None:
                delay_seconds = max((next_retry_at - now).total_seconds(), 0.0)

            try:
                if delay_seconds > 0.5:
                    timer = threading.Timer(
                        delay_seconds,
                        self._execute_persisted_workflow_retry_if_allowed,
                        kwargs={"workflow_run_id": workflow_run_id, "outbox_id": outbox_id},
                    )
                    timer.daemon = True
                    self._register_pending_retry_timer(retry_key, timer)
                    timer.start()
                else:
                    self._register_pending_retry_timer(retry_key, object())
                    self._reply_executor.submit(
                        self._execute_persisted_workflow_retry_if_allowed,
                        workflow_run_id=workflow_run_id,
                        outbox_id=outbox_id,
                    )
                restored += 1
            except Exception as e:
                self._pop_pending_retry_timer(retry_key)
                logger.warning(
                    f"恢复持久化补偿重试失败: workflow_run_id={workflow_run_id}, "
                    f"outbox_id={outbox_id}, error={e}"
                )
                continue

            if restored >= max(int(limit or 0), 1):
                break

        return restored

    def _finalize_workflow_delivery_success(
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
        """统一收口补偿发送成功路径，避免 done/success/complete 多处手写。"""
        self._get_outbound_workflow_delivery_coordinator().finalize_success(
            workflow_run_id=workflow_run_id,
            inbound_logical_message_id=inbound_logical_message_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            trigger=trigger,
            deduplicated=deduplicated,
            inbound_reason_override=inbound_reason_override,
            bind_outbox=bind_outbox,
        )

    def _pause_workflow_delivery(
        self,
        *,
        workflow_run_id: str,
        outbox_id: str,
        reply_msg_id: str,
        reply_content: str,
        reason: str,
        bind_outbox: bool = False,
    ) -> None:
        """统一收口补偿发送暂停路径，避免 cancelled/pause_for_human 多处手写。"""
        self._get_outbound_workflow_delivery_coordinator().pause_delivery(
            workflow_run_id=workflow_run_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            reply_content=reply_content,
            reason=reason,
            bind_outbox=bind_outbox,
        )

    def _deliver_workflow_reply(
        self,
        *,
        workflow_run_id: str = "",
        inbound_logical_message_id: str = "",
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
        trigger: str = "workflow_resume",
        retry_count: int = 0,
    ) -> dict:
        """将工作流中的待发送回复真正投递到统一发送主链。"""
        reply_content = (reply_content or "").strip()
        customer_name = (customer_name or "").strip()
        conversation_id = (conversation_id or "").strip()
        platform = (platform or "douyin").strip() or "douyin"
        retry_count = max(int(retry_count or 0), 0)

        if not reply_content:
            return {"success": False, "status": "invalid", "message": "reply_content_empty"}
        if not customer_name:
            return {"success": False, "status": "invalid", "message": "customer_name_empty"}

        if not conversation_id:
            conversation_id = self._resolve_conversation_id(customer_name, platform, customer_id)

        if not reply_msg_id:
            reply_msg_id = (
                f"{trigger}_{int(datetime.now().timestamp() * 1000)}_"
                f"{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:6]}"
            )
        if not logical_message_id:
            logical_message_id = build_logical_message_id(
                platform=platform,
                conversation_id=conversation_id,
                customer_name=customer_name,
                direction="outbound",
                content=reply_content,
                source_message_id=reply_msg_id,
            )
        if not outbox_id:
            outbox_id = f"outbox_{logical_message_id}"

        if workflow_run_id:
            try:
                self._get_inbound_message_state_repository().bind_workflow_outbox(workflow_run_id, outbox_id)
                self.message_workflow_manager.activate_run(
                    workflow_run_id,
                    node_id="send_reply",
                    metadata={"outbox_id": outbox_id, "trigger": trigger, "retry_count": retry_count},
                )
            except Exception as workflow_e:
                logger.debug(f"激活工作流失败: {workflow_e}")

        attempt_result = self._attempt_reply_delivery(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
            smart_result=smart_result or {
                "priority_level": "P1",
                "risk_assessment": {"overall_level": "medium"},
                "sentiment": "neutral",
                "suggested_action": trigger,
            },
            session=session,
            original_content=original_content or reply_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            allow_duplicate_content=(trigger != "outbox_retry"),
            duplicate_as_success=True,
            outbound_source="workflow",
            outbound_trigger=trigger,
        )
        delivery_decision = self._get_outbound_send_verifier().resolve_workflow_delivery_decision(attempt_result)
        if delivery_decision.action == "finalize_success":
            self._finalize_workflow_delivery_success(
                workflow_run_id=workflow_run_id,
                inbound_logical_message_id=inbound_logical_message_id,
                outbox_id=outbox_id,
                reply_msg_id=reply_msg_id,
                trigger=trigger,
                deduplicated=delivery_decision.deduplicated,
            )
            return {
                "success": delivery_decision.success,
                "status": delivery_decision.status,
                "message_id": reply_msg_id,
                "outbox_id": outbox_id,
            }
        self._pause_workflow_delivery(
            workflow_run_id=workflow_run_id,
            outbox_id=outbox_id,
            reply_msg_id=reply_msg_id,
            reply_content=reply_content,
            reason=delivery_decision.pause_reason,
        )
        return {
            "success": delivery_decision.success,
            "status": delivery_decision.status,
            "message": delivery_decision.message,
            "outbox_id": outbox_id,
        }

    def _resume_monitor_inactive_inbound_workflows(self, limit: int = 50) -> int:
        """恢复仅因监听暂时失活而暂停的入站工作流。"""
        if not self._allow_background_delivery_without_new_inbound("monitor_resume"):
            return 0
        resumed = 0
        with contextlib.suppress(Exception):
            monitoring_status = self._get_monitoring_status()
            if not bool(monitoring_status.get("effective_monitoring")):
                return 0

        try:
            paused_runs = list(self.db.list_workflow_runs(statuses=["paused"], limit=max(int(limit or 0), 1) * 4) or [])
        except Exception as e:
            logger.debug(f"读取 monitor_inactive 工作流失败: {e}")
            return 0

        for run in paused_runs:
            paused_reason = str(run.get("paused_reason", "") or "").strip()
            if not paused_reason.startswith("monitor_inactive:"):
                continue

            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            logical_message_id = str(run.get("logical_message_id", "") or "").strip()
            conversation_id = str(run.get("conversation_id", "") or "").strip()
            customer_name = str(run.get("customer_name", "") or "").strip()
            content = str(run.get("content", "") or "").strip()
            if not workflow_run_id or not logical_message_id or not customer_name or not content:
                continue

            inbound_event = {}
            with contextlib.suppress(Exception):
                inbound_event = self.db.get_inbound_event(logical_message_id) or {}
            inbound_status = str(inbound_event.get("status", "") or "").strip()
            if inbound_status in {"done", "skipped", "failed"}:
                continue

            payload = {
                "customer_name": customer_name,
                "content": content,
                "direction": "inbound",
                "conversation_id": conversation_id,
                "platform": str(run.get("platform", "douyin") or "douyin"),
                "customer_id": str(run.get("customer_id", "") or ""),
                "is_new": True,
                "msg_id": str(
                    inbound_event.get("source_message_id", "")
                    or run.get("source_message_id", "")
                    or ""
                ),
                "logical_message_id": logical_message_id,
                "workflow_run_id": workflow_run_id,
                "_message_already_persisted": True,
                "_resumed_from_monitor_inactive": True,
                "enterprise_id": str(
                    inbound_event.get("enterprise_id", "")
                    or run.get("enterprise_id", "")
                    or ""
                ).strip(),
                "preferred_schema_id": str(
                    inbound_event.get("preferred_schema_id", "")
                    or run.get("preferred_schema_id", "")
                    or ""
                ).strip(),
                "schema_id": str(
                    inbound_event.get("schema_id", "")
                    or run.get("schema_id", "")
                    or inbound_event.get("preferred_schema_id", "")
                    or run.get("preferred_schema_id", "")
                    or ""
                ).strip(),
                "tenant_resolution_mode": str(
                    inbound_event.get("tenant_resolution_mode", "")
                    or run.get("tenant_resolution_mode", "")
                    or ""
                ).strip(),
            }

            try:
                self.message_workflow_manager.activate_run(
                    workflow_run_id,
                    node_id="ingest",
                    metadata={"trigger": "monitor_resume", "paused_reason": paused_reason},
                )
                queued = self._submit_inbound_reply_job(payload)
            except Exception as e:
                logger.warning(f"恢复 monitor_inactive 工作流失败: workflow_run_id={workflow_run_id}, error={e}")
                queued = False

            if queued:
                resumed += 1
                logger.info(
                    f"恢复 monitor_inactive 入站工作流: workflow_run_id={workflow_run_id}, "
                    f"logical_message_id={logical_message_id}, customer={customer_name}"
                )
                if resumed >= max(int(limit or 0), 1):
                    break
                continue

            with contextlib.suppress(Exception):
                self.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "paused",
                        "paused_reason": paused_reason,
                        "current_node": "ingest",
                    }
                )
        return resumed
