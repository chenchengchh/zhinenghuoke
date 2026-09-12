"""
转化阶段与回复目标类型定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class ConversionStage(Enum):
    DISCOVERY = "discovery"
    QUALIFICATION = "qualification"
    CONSIDERATION = "consideration"
    HIGH_INTENT = "high_intent"
    RESERVATION = "reservation"
    HANDOFF = "handoff"


class NextBestAction(Enum):
    ANSWER_ONLY = "answer_only"
    CLARIFY = "clarify"
    OFFER_MATERIAL = "offer_material"
    OFFER_RESERVATION = "offer_reservation"
    CAPTURE_LEAD = "capture_lead"
    HANDOFF_TO_HUMAN = "handoff_to_human"
    ADVANCE_TO_ORDER = "advance_to_order"


class CtaMode(Enum):
    NONE = "none"
    SOFT_PROBE = "soft_probe"
    MATERIAL_OFFER = "material_offer"
    RESERVATION_OFFER = "reservation_offer"
    LEAD_CAPTURE = "lead_capture"
    HUMAN_HANDOFF = "human_handoff"
    ORDER_OFFER = "order_offer"


@dataclass
class ConversionSignals:
    buying_signal: float = 0.0
    urgency_signal: float = 0.0
    reservation_signal: float = 0.0
    contact_acceptance_signal: float = 0.0
    objection_signal: float = 0.0
    comparison_signal: float = 0.0


@dataclass
class ReplyObjective:
    conversion_stage: ConversionStage
    next_best_action: NextBestAction
    cta_mode: CtaMode
    should_capture_lead: bool = False
    should_offer_reservation: bool = False
    should_handoff: bool = False
    should_advance_to_order: bool = False
    missing_slots: List[str] = field(default_factory=list)
    primary_need: str = ""
    objective_confidence: float = 0.0
    reason: str = ""
