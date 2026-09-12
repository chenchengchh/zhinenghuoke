"""结果验收器 - 验证子任务结果，失败时建议修复策略"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .task_types import TaskType, SubTask
from .video_discovery_types import DiscoveryResult
from .comment_crawl_agent import CommentCrawlResult
from .private_message_agent import PMResult
from .comment_reply_agent import ReplyResult

logger = logging.getLogger(__name__)


class ResultValidator:
    """结果验收器 - 验证子任务结果，失败时建议修复策略"""

    @staticmethod
    def validate(task: SubTask, result: Any) -> Dict:
        """验证子任务执行结果"""
        task_type = task.task_type

        if task_type == TaskType.VIDEO_DISCOVERY:
            if isinstance(result, DiscoveryResult):
                if result.success and result.videos_discovered > 0:
                    return {"valid": True, "summary": f"发现{result.videos_discovered}个视频"}
                elif result.videos_discovered == 0:
                    return {
                        "valid": False,
                        "reason": "no_videos_discovered",
                        "summary": "未发现任何视频",
                        "partial_success": False,
                        "suggested_fix": "check_popup_or_layout",
                    }
            return {"valid": bool(getattr(result, 'success', False)), "summary": "视频发现结果未知"}

        elif task_type == TaskType.COMMENT_CRAWL:
            if isinstance(result, CommentCrawlResult):
                if result.success:
                    return {"valid": True, "summary": f"爬取{result.total_comments}条评论"}
                elif result.risk_control_detected:
                    return {"valid": False, "reason": "risk_control", "summary": "风控拦截"}
                return {"valid": False, "reason": "no_comments", "summary": "未爬取到评论"}

        elif task_type in (TaskType.POPUP_FIX, TaskType.LAYOUT_FIX):
            if isinstance(result, dict):
                status = result.get("status", "")
                if status in ("success", "no_action_needed"):
                    return {"valid": True, "summary": result.get("detail", "")}
            return {"valid": True, "summary": "修复任务完成"}

        return {
            "valid": bool(getattr(result, 'success', False)),
            "summary": f"{task_type.value} 完成",
        }

    @staticmethod
    def suggest_auto_fix(task: SubTask, validation: Dict, crawler: Any) -> Dict:
        """根据验证结果建议自动修复策略"""
        suggested_fix = validation.get("suggested_fix", "")

        if task.task_type == TaskType.VIDEO_DISCOVERY and suggested_fix == "check_popup_or_layout":
            fixes_applied = []

            # 1. 强力弹窗清除
            if hasattr(crawler, '_dismiss_login_popup_if_present'):
                try:
                    if crawler._dismiss_login_popup_if_present():
                        fixes_applied.append("force_login_popup_clear")
                except Exception:
                    pass

            if hasattr(crawler, '_dismiss_recommended_video_if_present'):
                try:
                    if crawler._dismiss_recommended_video_if_present(max_rounds=2):
                        fixes_applied.append("force_rec_popup_clear")
                except Exception:
                    pass

            # 2. 强制布局切换
            if hasattr(crawler, '_ensure_multi_column_search_layout'):
                try:
                    crawler._ensure_multi_column_search_layout()
                    fixes_applied.append("force_multicolumn")
                except Exception:
                    pass

            # 3. 重新消费数据
            reconsumed = 0
            if hasattr(crawler, '_consume_search_api_batches'):
                try:
                    pending = getattr(crawler, 'pending_entries', None) or []
                    entries = crawler._consume_search_api_batches(pending, 30)
                    reconsumed = len([e for e in entries if e])
                except Exception:
                    pass

            if fixes_applied or reconsumed > 0:
                return {"fixed": True, "fix_action": ",".join(fixes_applied) + (f",reconsumed={reconsumed}" if reconsumed else "")}

        return {"fixed": False, "fix_action": "no_auto_fix_available"}
