from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from src.common.database import DatabaseManager
from src.common.handoff_ticket_repository import HandoffTicketRepository
from src.common.monitoring import get_metrics


class MessageWorkflowManager:
    """消息回复工作流的持久化管理器。"""

    WORKFLOW_TYPE = "message_reply"

    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()
        self.handoff_repo = HandoffTicketRepository(self.db)

    @staticmethod
    def _normalize_transition_metadata(
        metadata: Optional[Dict[str, Any]] = None,
        *,
        reason_code: str = "",
        workflow_action: str = "",
        eligibility_action: str = "",
    ) -> Dict[str, Any]:
        normalized = dict(metadata or {})
        resolved_reason_code = str(
            reason_code
            or normalized.get("reason_code")
            or normalized.get("reason")
            or ""
        ).strip()
        resolved_workflow_action = str(
            workflow_action or normalized.get("workflow_action") or ""
        ).strip()
        resolved_eligibility_action = str(
            eligibility_action or normalized.get("eligibility_action") or ""
        ).strip()
        if resolved_reason_code:
            normalized["reason_code"] = resolved_reason_code
            normalized.setdefault("reason", resolved_reason_code)
        if resolved_workflow_action:
            normalized["workflow_action"] = resolved_workflow_action
        if resolved_eligibility_action:
            normalized["eligibility_action"] = resolved_eligibility_action
        return normalized

    def start_run(
        self,
        *,
        logical_message_id: str,
        conversation_id: str,
        customer_name: str,
        content: str,
        customer_id: str = "",
        platform: str = "douyin",
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        schema_id: str = "",
        tenant_resolution_mode: str = "",
        inbound_trigger: str = "",
    ) -> str:
        workflow_run_id = f"wf_{uuid.uuid4().hex[:16]}"
        self.db.upsert_workflow_run(
            {
                "workflow_run_id": workflow_run_id,
                "workflow_type": self.WORKFLOW_TYPE,
                "logical_message_id": logical_message_id,
                "conversation_id": conversation_id,
                "customer_name": customer_name,
                "customer_id": customer_id,
                "platform": platform,
                "content": content,
                "enterprise_id": str(enterprise_id or "").strip(),
                "preferred_schema_id": str(preferred_schema_id or "").strip(),
                "schema_id": str(schema_id or "").strip(),
                "tenant_resolution_mode": str(tenant_resolution_mode or "").strip(),
                "inbound_trigger": str(inbound_trigger or "").strip(),
                "status": "running",
                "current_node": "ingest",
                "nodes": {},
                "history": [],
                "paused_reason": "",
                "outbox_id": "",
            }
        )
        self.record_node(
            workflow_run_id,
            "ingest",
            status="success",
            metadata={
                "logical_message_id": logical_message_id,
                "inbound_trigger": str(inbound_trigger or "").strip(),
            },
        )
        get_metrics().record_workflow_run("running")
        return workflow_run_id

    def record_node(
        self,
        workflow_run_id: str,
        node_id: str,
        *,
        status: str,
        metadata: Optional[Dict[str, Any]] = None,
        error: str = "",
    ):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        normalized_metadata = self._normalize_transition_metadata(metadata)

        nodes = workflow_run.get("nodes", {}) or {}
        nodes[node_id] = {
            "status": status,
            "updated_at": datetime.now().isoformat(),
            "metadata": normalized_metadata,
        }
        if error:
            nodes[node_id]["error"] = error

        history = list(workflow_run.get("history", []) or [])
        history.append(
            {
                "node_id": node_id,
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "metadata": normalized_metadata,
                "error": error,
            }
        )
        workflow_run["nodes"] = nodes
        workflow_run["history"] = history[-50:]
        workflow_run["current_node"] = node_id
        self.db.upsert_workflow_run(workflow_run)

    def pause_for_human(
        self,
        workflow_run_id: str,
        *,
        reason: str,
        suggested_reply: str,
        outbox_id: str = "",
        eligibility_snapshot: Optional[Dict[str, Any]] = None,
        eligibility_action: str = "",
    ):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["status"] = "paused"
        workflow_run["paused_reason"] = reason
        workflow_run["last_reason_code"] = str(reason or "").strip()
        workflow_run["last_workflow_action"] = "pause_for_human"
        workflow_run["last_eligibility_action"] = str(eligibility_action or "").strip()
        if eligibility_snapshot:
            workflow_run["eligibility_snapshot"] = dict(eligibility_snapshot)
        workflow_run["suggested_reply"] = suggested_reply
        if outbox_id:
            workflow_run["outbox_id"] = outbox_id
        ticket_id = str(workflow_run.get("handoff_ticket_id", "") or "").strip()
        if not ticket_id:
            ticket_id = self.handoff_repo.create_ticket(
                {
                    "workflow_run_id": workflow_run_id,
                    "logical_message_id": workflow_run.get("logical_message_id", ""),
                    "conversation_id": workflow_run.get("conversation_id", ""),
                    "customer_id": workflow_run.get("customer_id", ""),
                    "customer_name": workflow_run.get("customer_name", ""),
                    "platform": workflow_run.get("platform", "douyin"),
                    "reason_code": reason,
                    "handoff_level": str((eligibility_snapshot or {}).get("handoff_level", "normal") or "normal"),
                    "suggested_reply": suggested_reply,
                    "raw_customer_message": workflow_run.get("content", ""),
                    "eligibility_snapshot": eligibility_snapshot or {},
                    "reply_analysis_snapshot": {},
                    "schema_id": workflow_run.get("schema_id", ""),
                    "enterprise_id": workflow_run.get("enterprise_id", ""),
                }
            )
        workflow_run["handoff_ticket_id"] = ticket_id
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            "human_gate",
            status="paused",
            metadata=self._normalize_transition_metadata(
                {
                    "suggested_reply": suggested_reply[:200],
                    "ticket_id": ticket_id,
                    "handoff_level": str((eligibility_snapshot or {}).get("handoff_level", "normal") or "normal"),
                },
                reason_code=reason,
                workflow_action="pause_for_human",
                eligibility_action=eligibility_action,
            ),
        )
        get_metrics().record_workflow_run("paused")

    def mark_skipped(
        self,
        workflow_run_id: str,
        *,
        reason: str,
        node_id: str = "send_reply",
        metadata: Optional[Dict[str, Any]] = None,
        eligibility_action: str = "",
    ) -> None:
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["status"] = "skipped"
        workflow_run["paused_reason"] = str(reason or "").strip()
        workflow_run["last_reason_code"] = str(reason or "").strip()
        workflow_run["last_workflow_action"] = "skip"
        workflow_run["last_eligibility_action"] = str(eligibility_action or "").strip()
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            node_id,
            status="skipped",
            metadata=self._normalize_transition_metadata(
                metadata,
                reason_code=reason,
                workflow_action="skip",
                eligibility_action=eligibility_action,
            ),
        )
        get_metrics().record_workflow_run("skipped")

    def _build_handoff_ticket_payload(
        self,
        workflow_run: dict,
        *,
        workflow_run_id: str,
        reason: str = "",
        suggested_reply: str = "",
        eligibility_snapshot: Optional[Dict[str, Any]] = None,
        ticket_id: str = "",
    ) -> Dict[str, Any]:
        normalized_snapshot = dict(eligibility_snapshot or {})
        return {
            "ticket_id": str(ticket_id or "").strip(),
            "workflow_run_id": workflow_run_id,
            "logical_message_id": workflow_run.get("logical_message_id", ""),
            "conversation_id": workflow_run.get("conversation_id", ""),
            "customer_id": workflow_run.get("customer_id", ""),
            "customer_name": workflow_run.get("customer_name", ""),
            "platform": workflow_run.get("platform", "douyin"),
            "reason_code": str(reason or workflow_run.get("paused_reason", "") or "").strip(),
            "handoff_level": str(
                (normalized_snapshot or {}).get("handoff_level", "normal") or "normal"
            ),
            "suggested_reply": str(
                suggested_reply
                or workflow_run.get("suggested_reply", "")
                or workflow_run.get("approved_reply", "")
                or ""
            ).strip(),
            "raw_customer_message": workflow_run.get("content", ""),
            "eligibility_snapshot": normalized_snapshot,
            "reply_analysis_snapshot": {},
            "schema_id": workflow_run.get("schema_id", ""),
            "enterprise_id": workflow_run.get("enterprise_id", ""),
        }

    def ensure_handoff_ticket(
        self,
        workflow_run_id: str,
        *,
        reason: str = "",
        suggested_reply: str = "",
        eligibility_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return ""
        if str(workflow_run.get("status", "") or "").strip() != "paused":
            return ""

        ticket_id = str(workflow_run.get("handoff_ticket_id", "") or "").strip()
        if ticket_id and self.handoff_repo.get_ticket(ticket_id):
            return ticket_id

        payload = self._build_handoff_ticket_payload(
            workflow_run,
            workflow_run_id=workflow_run_id,
            reason=reason,
            suggested_reply=suggested_reply,
            eligibility_snapshot=eligibility_snapshot,
            ticket_id=ticket_id,
        )
        created_ticket_id = self.handoff_repo.create_ticket(payload)
        workflow_run["handoff_ticket_id"] = created_ticket_id
        self.db.upsert_workflow_run(workflow_run)
        return created_ticket_id

    def reconcile_paused_handoff_tickets(self, *, limit: int = 200) -> int:
        updated = 0
        paused_runs = list(self.db.list_workflow_runs(statuses=["paused"], limit=max(int(limit or 0), 1)) or [])
        for workflow_run in paused_runs:
            workflow_run_id = str(workflow_run.get("workflow_run_id", "") or "").strip()
            if not workflow_run_id:
                continue
            ticket_id = str(workflow_run.get("handoff_ticket_id", "") or "").strip()
            if ticket_id and self.handoff_repo.get_ticket(ticket_id):
                continue
            created_ticket_id = self.ensure_handoff_ticket(
                workflow_run_id,
                reason=str(workflow_run.get("paused_reason", "") or "").strip(),
                suggested_reply=str(workflow_run.get("suggested_reply", "") or "").strip(),
                eligibility_snapshot=dict(workflow_run.get("eligibility_snapshot", {}) or {}),
            )
            if created_ticket_id:
                updated += 1
        return updated

    def mark_waiting_retry(
        self,
        workflow_run_id: str,
        *,
        outbox_id: str,
        reason: str,
        retry_count: int,
        eligibility_action: str = "",
    ):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["status"] = "waiting_retry"
        workflow_run["outbox_id"] = outbox_id
        workflow_run["paused_reason"] = reason
        workflow_run["last_reason_code"] = str(reason or "").strip()
        workflow_run["last_workflow_action"] = "retry_later"
        workflow_run["last_eligibility_action"] = str(eligibility_action or "").strip()
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            "send_reply",
            status="waiting_retry",
            metadata=self._normalize_transition_metadata(
                {"outbox_id": outbox_id, "retry_count": retry_count},
                reason_code=reason,
                workflow_action="retry_later",
                eligibility_action=eligibility_action,
            ),
        )

    def activate_run(
        self,
        workflow_run_id: str,
        *,
        node_id: str = "send_reply",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[dict]:
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return None
        workflow_run["status"] = "running"
        workflow_run["paused_reason"] = ""
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            node_id,
            status="running",
            metadata=metadata or {},
        )
        return workflow_run

    def complete_run(self, workflow_run_id: str, *, message_id: str = ""):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["status"] = "completed"
        workflow_run["completed_at"] = datetime.now().isoformat()
        workflow_run["last_workflow_action"] = "complete"
        workflow_run["last_reason_code"] = "reply_sent"
        if message_id:
            workflow_run["message_id"] = message_id
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            "complete",
            status="success",
            metadata=self._normalize_transition_metadata(
                {"message_id": message_id},
                reason_code="reply_sent",
                workflow_action="complete",
            ),
        )
        get_metrics().record_workflow_run("completed")

    def fail_run(self, workflow_run_id: str, *, reason: str):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["status"] = "failed"
        workflow_run["error"] = reason
        workflow_run["last_reason_code"] = str(reason or "").strip()
        workflow_run["last_workflow_action"] = "fail"
        self.db.upsert_workflow_run(workflow_run)
        self.record_node(
            workflow_run_id,
            "failed",
            status="failed",
            metadata=self._normalize_transition_metadata(
                {},
                reason_code=reason,
                workflow_action="fail",
            ),
            error=reason,
        )
        get_metrics().record_workflow_run("failed")

    def get_run(self, workflow_run_id: str) -> Optional[dict]:
        return self.db.get_workflow_run(workflow_run_id)

    def list_runs(self, statuses: Optional[List[str]] = None, limit: int = 100) -> List[dict]:
        return self.db.list_workflow_runs(statuses=statuses, limit=limit)

    def resume_human_run(self, workflow_run_id: str, *, approved_reply: str, ticket_id: str = "") -> Optional[dict]:
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run or workflow_run.get("status") != "paused":
            return None
        workflow_run["status"] = "resumed"
        workflow_run["approved_reply"] = approved_reply
        workflow_run["paused_reason"] = ""
        workflow_run["last_workflow_action"] = "resume"
        self.db.upsert_workflow_run(workflow_run)
        ticket_id = ticket_id or str(workflow_run.get("handoff_ticket_id", "") or "").strip()
        if ticket_id:
            self.handoff_repo.resolve_ticket(ticket_id, approved_reply)
        self.record_node(
            workflow_run_id,
            "human_gate",
            status="resumed",
            metadata=self._normalize_transition_metadata(
                {"approved_reply": approved_reply[:200], "ticket_id": ticket_id},
                reason_code="human_review_resolved",
                workflow_action="resume",
            ),
        )
        return workflow_run

    def bind_outbox(self, workflow_run_id: str, outbox_id: str):
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return
        workflow_run["outbox_id"] = outbox_id
        self.db.upsert_workflow_run(workflow_run)

    def schedule_outbox_retry(
        self,
        *,
        workflow_run_id: str,
        outbox_id: str,
        retry_count: int,
        reason: str,
        delay_seconds: int = 30,
        eligibility_action: str = "",
    ) -> str:
        next_retry_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
        self.db.update_outbox_event(
            outbox_id,
            status="retry_pending",
            retry_count=retry_count,
            next_retry_at=next_retry_at,
            reason=reason,
            workflow_run_id=workflow_run_id,
        )
        self.mark_waiting_retry(
            workflow_run_id,
            outbox_id=outbox_id,
            reason=reason,
            retry_count=retry_count,
            eligibility_action=eligibility_action,
        )
        return next_retry_at


_message_workflow_manager: Optional[MessageWorkflowManager] = None


def get_message_workflow_manager(db: Optional[DatabaseManager] = None) -> MessageWorkflowManager:
    global _message_workflow_manager
    if _message_workflow_manager is None or (db is not None and _message_workflow_manager.db is not db):
        _message_workflow_manager = MessageWorkflowManager(db=db)
    return _message_workflow_manager
