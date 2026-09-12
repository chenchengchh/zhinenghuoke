"""视频发现子智能体 - 数据类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Dict


class VideoDiscoveryPhase(Enum):
    """视频发现阶段枚举"""
    INITIALIZING = auto()
    SEARCH_NAVIGATING = auto()
    POPUP_CLEARING = auto()
    LAYOUT_CHECKING = auto()
    LAYOUT_SWITCHING = auto()
    VIDEO_DISCOVERING = auto()
    SCROLLING = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class DiscoveryStep:
    """单个发现步骤"""
    phase: VideoDiscoveryPhase
    action: str
    status: str = "pending"  # pending / running / success / failed / skipped
    detail: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    retry_count: int = 0


@dataclass
class DiscoveryResult:
    """视频发现结果"""
    success: bool
    videos_discovered: int = 0
    videos: List[Dict] = field(default_factory=list)
    steps_completed: int = 0
    steps_total: int = 0
    termination_reason: str = ""
    execution_log: List[DiscoveryStep] = field(default_factory=list)
    popup_closed_count: int = 0
    layout_switched: bool = False
