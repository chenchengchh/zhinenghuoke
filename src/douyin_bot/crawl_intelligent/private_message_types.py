"""自动私信子智能体 - 数据类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List


class PMPhase(Enum):
    """私信阶段枚举"""
    PRE_CHECK = auto()
    NAVIGATE_PROFILE = auto()
    FIND_ENTRY_BUTTON = auto()
    CLICK_ENTRY = auto()
    WAIT_CHAT_READY = auto()
    VALIDATE_TARGET = auto()
    LOCATE_INPUT = auto()
    TYPE_MESSAGE = auto()
    SEND_ACTION = auto()
    VERIFY_RESULT = auto()
    POST_PROCESS = auto()


@dataclass
class PMStep:
    """私信步骤"""
    phase: PMPhase
    action: str
    status: str = "pending"
    detail: str = ""


@dataclass
class PMResult:
    """私信结果"""
    success: bool
    user_id: str = ""
    reason: str = ""
    execution_log: List[PMStep] = field(default_factory=list)
