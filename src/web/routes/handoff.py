from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.web.bot_service import get_bot_service


router = APIRouter(prefix="/api/handoff", tags=["人工接管"])


class HandoffClaimRequest(BaseModel):
    owner: str


class HandoffResolveRequest(BaseModel):
    approved_reply: str
    resolution_note: str = ""


@router.get("/tickets")
def list_handoff_tickets(
    status: Optional[str] = Query(default=None),
    owner: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
):
    statuses = [item.strip() for item in str(status or "").split(",") if item.strip()] or None
    tickets = get_bot_service().get_handoff_tickets(statuses=statuses, owner=owner, limit=limit)
    return {"success": True, "tickets": tickets}


@router.post("/tickets/{ticket_id}/claim")
def claim_handoff_ticket(ticket_id: str, payload: HandoffClaimRequest):
    if not get_bot_service().claim_handoff_ticket(ticket_id, payload.owner):
        raise HTTPException(status_code=404, detail="ticket_not_found")
    return {"success": True, "ticket_id": ticket_id}


@router.post("/tickets/{ticket_id}/resolve")
def resolve_handoff_ticket(ticket_id: str, payload: HandoffResolveRequest):
    if not get_bot_service().resolve_handoff_ticket(
        ticket_id,
        payload.approved_reply,
        resolution_note=payload.resolution_note,
    ):
        raise HTTPException(status_code=404, detail="ticket_not_found")
    return {"success": True, "ticket_id": ticket_id}
