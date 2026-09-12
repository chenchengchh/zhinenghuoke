"""任务类型定义 - 枚举、数据类"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional


class TaskType(Enum):
    """子任务类型"""
    VIDEO_DISCOVERY = "video_discovery"       # 视频发现
    COMMENT_CRAWL = "comment_crawl"           # 评论区爬取
    PRIVATE_MESSAGE = "private_message"       # 自动私信
    COMMENT_REPLY = "comment_reply"           # 评论下回复
    POPUP_FIX = "popup_fix"                  # 弹窗修复
    LAYOUT_FIX = "layout_fix"               # 布局修复
    RISK_HANDLE = "risk_handle"              # 风控处理


class TaskPriority(Enum):
    HIGH = 1
    MEDIUM = 2
    LOW = 3


@dataclass
class SubTask:
    """子任务定义"""
    task_id: str
    task_type: TaskType
    priority: TaskPriority = TaskPriority.MEDIUM
    params: Dict = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)  # 依赖的任务ID
    status: str = "pending"  # pending / running / success / failed / skipped
    result: Optional[Any] = None
    retry_count: int = 0
    max_retries: int = 2
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""


@dataclass
class CrawlPlan:
    """爬取执行计划"""
    plan_id: str
    keyword: str = ""
    tasks: List[SubTask] = field(default_factory=list)
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0


@dataclass
class CrawlExecutionReport:
    """执行报告"""
    plan_id: str
    success: bool
    total_duration_seconds: float = 0.0
    videos_discovered: int = 0
    comments_crawled: int = 0
    messages_sent: int = 0
    replies_posted: int = 0
    popup_fixed_count: int = 0
    layout_switched: bool = False
    task_results: Dict[str, Any] = field(default_factory=dict)
    execution_log: List[str] = field(default_factory=list)
    termination_reason: str = ""
