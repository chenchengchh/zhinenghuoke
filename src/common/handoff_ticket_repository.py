from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from src.common.database import DatabaseManager


class HandoffTicketRepository:
    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db or DatabaseManager()

    def create_ticket(self, payload: dict) -> str:
        ticket_id = str((payload or {}).get("ticket_id", "") or "").strip() or f"ht_{uuid.uuid4().hex[:16]}"
        current = datetime.now()
        escalate_at = str((payload or {}).get("escalate_at", "") or "").strip()
        if not escalate_at:
            escalate_at = (current + timedelta(minutes=30)).isoformat()
        document = {
            "ticket_id": ticket_id,
            "workflow_run_id": str((payload or {}).get("workflow_run_id", "") or "").strip(),
            "logical_message_id": str((payload or {}).get("logical_message_id", "") or "").strip(),
            "conversation_id": str((payload or {}).get("conversation_id", "") or "").strip(),
            "customer_id": str((payload or {}).get("customer_id", "") or "").strip(),
            "customer_name": str((payload or {}).get("customer_name", "") or "").strip(),
            "platform": str((payload or {}).get("platform", "douyin") or "").strip() or "douyin",
            "reason_code": str((payload or {}).get("reason_code", "") or "").strip(),
            "handoff_level": str((payload or {}).get("handoff_level", "normal") or "").strip() or "normal",
            "status": str((payload or {}).get("status", "pending") or "").strip() or "pending",
            "owner": str((payload or {}).get("owner", "") or "").strip(),
            "source": str((payload or {}).get("source", "reply_eligibility") or "").strip(),
            "suggested_reply": str((payload or {}).get("suggested_reply", "") or "").strip(),
            "raw_customer_message": str((payload or {}).get("raw_customer_message", "") or "").strip(),
            "eligibility_snapshot": dict((payload or {}).get("eligibility_snapshot") or {}),
            "reply_analysis_snapshot": dict((payload or {}).get("reply_analysis_snapshot") or {}),
            "schema_id": str((payload or {}).get("schema_id", "") or "").strip(),
            "enterprise_id": str((payload or {}).get("enterprise_id", "") or "").strip(),
            "claimed_at": str((payload or {}).get("claimed_at", "") or "").strip(),
            "resolved_at": str((payload or {}).get("resolved_at", "") or "").strip(),
            "escalate_at": escalate_at,
            "resolution_note": str((payload or {}).get("resolution_note", "") or "").strip(),
            "approved_reply": str((payload or {}).get("approved_reply", "") or "").strip(),
            "created_at": str((payload or {}).get("created_at", "") or "").strip() or current.isoformat(),
            "updated_at": current.isoformat(),
        }
        self.db.upsert_handoff_ticket(document)
        return ticket_id

    def get_ticket(self, ticket_id: str) -> Optional[dict]:
        return self.db.get_handoff_ticket(ticket_id)

    def list_tickets(
        self,
        statuses: List[str] | None = None,
        owner: str = "",
        limit: int = 100,
    ) -> List[dict]:
        return self.db.list_handoff_tickets(statuses=statuses, owner=owner, limit=limit)

    def claim_ticket(self, ticket_id: str, owner: str) -> bool:
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            return False
        ticket["status"] = "claimed"
        ticket["owner"] = str(owner or "").strip()
        ticket["claimed_at"] = datetime.now().isoformat()
        self.db.upsert_handoff_ticket(ticket)
        return True

    def resolve_ticket(self, ticket_id: str, approved_reply: str, resolution_note: str = "") -> bool:
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            return False
        ticket["status"] = "resolved"
        ticket["approved_reply"] = str(approved_reply or "").strip()
        ticket["resolution_note"] = str(resolution_note or "").strip()
        ticket["resolved_at"] = datetime.now().isoformat()
        self.db.upsert_handoff_ticket(ticket)
        return True

    def escalate_timeout_tickets(self, now_iso: str) -> int:
        current = str(now_iso or "").strip()
        updated = 0
        for ticket in self.list_tickets(statuses=["pending", "claimed"], limit=500):
            escalate_at = str(ticket.get("escalate_at", "") or "").strip()
            if escalate_at and escalate_at <= current:
                ticket["status"] = "timeout_escalated"
                self.db.upsert_handoff_ticket(ticket)
                updated += 1
        return updated


_handoff_ticket_repository: HandoffTicketRepository | None = None


def get_handoff_ticket_repository() -> HandoffTicketRepository:
    global _handoff_ticket_repository
    if _handoff_ticket_repository is None:
        _handoff_ticket_repository = HandoffTicketRepository()
    return _handoff_ticket_repository
