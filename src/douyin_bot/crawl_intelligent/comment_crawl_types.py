"""评论区爬取子智能体 - 数据类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List


class CommentCrawlPhase(Enum):
    """评论区爬取阶段"""
    INITIALIZING = auto()
    NAVIGATE_TO_VIDEO = auto()
    CLEAR_POPUPS = auto()
    VALIDATE_VIDEO_CONTEXT = auto()
    DETAIL_KIND_DETECTION = auto()
    COMMENT_BOOTSTRAP = auto()
    OPEN_COMMENT_PANEL = auto()
    CRAWL_COMMENTS_LOOP = auto()
    EXPAND_REPLIES = auto()
    BUILD_RESULT = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass
class CommentCrawlStep:
    """评论爬取步骤"""
    phase: CommentCrawlPhase
    action: str
    status: str = "pending"
    detail: str = ""
    comment_count: int = 0


@dataclass
class CommentCrawlResult:
    """评论爬取结果"""
    success: bool
    video_url: str = ""
    aweme_id: str = ""
    total_comments: int = 0
    matched_comments: int = 0
    saved_customers: int = 0
    termination_reason: str = ""
    risk_control_detected: bool = False
    execution_log: List[CommentCrawlStep] = field(default_factory=list)
