"""任务拆分器 - 将高层目标拆分为有序的子任务链"""

from __future__ import annotations

import time
from typing import List, Optional

from .task_types import TaskType, TaskPriority, SubTask, CrawlPlan


class TaskDecomposer:
    """任务拆分器 - 将高层目标拆分为有序的子任务链"""

    @staticmethod
    def decompose_search_and_crawl(
        keyword: str,
        lead_quota: int = 1000,
        max_videos: int = 0,
        auto_reply: bool = False,
        reply_templates: Optional[List[str]] = None,
        comment_keywords: Optional[List[str]] = None,
        platform: str = "douyin",
    ) -> CrawlPlan:
        """将搜索+爬取高层目标拆分为子任务链"""
        plan_id = f"crawl_{int(time.time())}_{hash(keyword) % 10000:04d}"
        tasks = []

        # 任务1: 弹窗修复（总是第一个执行）
        tasks.append(SubTask(
            task_id="t1_popup_fix",
            task_type=TaskType.POPUP_FIX,
            priority=TaskPriority.HIGH,
            params={"max_rounds": 2},
        ))

        # 任务2: 视频发现
        tasks.append(SubTask(
            task_id="t2_video_discovery",
            task_type=TaskType.VIDEO_DISCOVERY,
            priority=TaskPriority.HIGH,
            params={
                "keyword": keyword,
                "lead_quota": lead_quota,
                "max_videos": max_videos,
                "platform": platform,
            },
            dependencies=["t1_popup_fix"],
        ))

        # 任务3: 评论区爬取
        tasks.append(SubTask(
            task_id="t3_comment_crawl",
            task_type=TaskType.COMMENT_CRAWL,
            priority=TaskPriority.HIGH,
            params={
                "comment_keywords": comment_keywords or [],
                "max_requests": 50,
            },
            dependencies=["t2_video_discovery"],
        ))

        # 任务4: 评论回复（可选）
        if auto_reply and reply_templates:
            tasks.append(SubTask(
                task_id="t4_comment_reply",
                task_type=TaskType.COMMENT_REPLY,
                priority=TaskPriority.MEDIUM,
                params={
                    "reply_templates": reply_templates,
                },
                dependencies=["t3_comment_crawl"],
            ))

        return CrawlPlan(
            plan_id=plan_id,
            keyword=keyword,
            tasks=tasks,
            total_tasks=len(tasks),
        )
