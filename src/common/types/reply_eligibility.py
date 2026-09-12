from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class EligibilityAction(str, Enum):
    SEND = "send"
    ACK_ONLY = "ack_only"
    DRAFT_FOR_REVIEW = "draft_for_review"
    PAUSE_FOR_HUMAN = "pause_for_human"
    RETRY_LATER = "retry_later"
    SKIP = "skip"


class EvidenceLevel(str, Enum):
    GROUNDED = "grounded"
    WEAK_GROUNDED = "weak_grounded"
    UNGROUNDED = "ungrounded"


class HandoffLevel(str, Enum):
    NONE = "none"
    NORMAL = "normal"
    URGENT = "urgent"
    COMPLAINT = "complaint"


@dataclass(frozen=True)
class ReplyEligibilityPolicy:
    # 调低发送门槛，避免正常问候/致谢被误判为低置信
    min_send_confidence: float = 0.6
    min_grounded_confidence: float = 0.5
    allow_no_answer_fail_soft: bool = False
    enable_ack_only: bool = True
    high_risk_action: str = EligibilityAction.PAUSE_FOR_HUMAN.value
    complaint_action: str = EligibilityAction.PAUSE_FOR_HUMAN.value
    quota_block_action: str = EligibilityAction.RETRY_LATER.value
    unknown_direction_action: str = EligibilityAction.SKIP.value
    # 未找到证据时按策略处置（默认 ACK_ONLY，依赖 LLM 兜底时可配置为 SEND）
    ungrounded_action: str = EligibilityAction.ACK_ONLY.value
    weak_identity_action: str = EligibilityAction.ACK_ONLY.value
    unstable_window_action: str = EligibilityAction.ACK_ONLY.value
    recent_reply_cooldown_action: str = EligibilityAction.SKIP.value
    min_stable_observation_count: int = 2
    recent_reply_cooldown_seconds: int = 120


@dataclass(frozen=True)
class ReplyEligibilityInput:
    customer_name: str
    content: str
    conversation_id: str
    logical_message_id: str
    workflow_run_id: str
    platform: str
    smart_result: Dict[str, Any]
    customer_id: str = ""
    cached_reply_content: str = ""
    history_message_count: int = 0
    schema_policy: Dict[str, Any] = field(default_factory=dict)
    runtime_flags: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyEligibilityDecision:
    action: str
    reason_code: str
    should_send: bool
    should_pause_workflow: bool
    should_create_handoff: bool
    handoff_level: str
    evidence_level: str
    risk_level: str
    confidence: float
    final_reply: str
    normalized_metadata: Dict[str, Any] = field(default_factory=dict)
