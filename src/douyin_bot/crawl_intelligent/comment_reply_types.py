"""评论回复子智能体 - 数据类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List


class ReplyPhase(Enum):
    """评论回复阶段"""
    PREPARE = auto()
    ENSURE_CONTEXT = auto()
    OPEN_COMMENT_PANEL = auto()
    ENRICH_ANCHOR = auto()
    CLICK_REPLY_BUTTON = auto()
    FIND_EDITOR = auto()
    CHECK_CONTEXT_ACTIVE = auto()
    FILL_CONTENT = auto()
    FIND_SEND_BTN = auto()
    SUBMIT_AND_VERIFY = auto()
    DONE = auto()


@dataclass
class ReplyStep:
    """回复步骤"""
    phase: ReplyPhase
    action: str
    status: str = "pending"
    detail: str = ""


@dataclass
class ReplyResult:
    """评论回复结果"""
    success: bool
    target_comment_id: str = ""
    rounds_attempted: int = 0
    reason: str = ""
    execution_log: List[ReplyStep] = field(default_factory=list)
