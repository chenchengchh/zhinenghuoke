"""主控智能体 - 任务拆分、状态评估、调度执行、结果验收"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, List, Dict, Callable

from .task_types import TaskType, TaskPriority, SubTask, CrawlPlan, CrawlExecutionReport
from .task_decomposer import TaskDecomposer
from .result_validator import ResultValidator
from .page_state import (
    PageState, PageStateAssessment, PopupDetectionResult, LayoutState,
    PageStateEvaluator, PopupType,
)
from .video_discovery_agent import VideoDiscoveryAgent, VideoDiscoveryPhase, DiscoveryResult
from .comment_crawl_agent import CommentCrawlAgent, CommentCrawlResult
from .private_message_agent import PrivateMessageAgent, PMResult
from .comment_reply_agent import CommentReplyAgent, ReplyResult

logger = logging.getLogger(__name__)


# ============================================================
# 主控智能体
# ============================================================

class CrawlIntelligentAgent:
    """
    抖音爬取主控智能体

    核心能力：
    ┌─────────────────────────────────────────────┐
    │  1. 任务拆分器 (Decomposer)                 │
    │     将高层目标拆分为有序的子任务链           │
    ├─────────────────────────────────────────────┤
    │  2. 状态评估器 (Evaluator)                   │
    │     执行前评估页面状态，决定是否需要预修复     │
    ├─────────────────────────────────────────────┤
    │  3. 执行调度器 (Scheduler)                   │
    │     按依赖关系和优先级调度子智能体执行         │
    ├─────────────────────────────────────────────┤
    │  4. 结果验收器 (Validator)                   │
    │     验证子任务结果，失败时触发自动修复/重试    │
    └─────────────────────────────────────────────┘

    典型工作流：
    用户请求 "搜索产品介绍 → 爬取评论 → 回复匹配评论"
        ↓
    拆分为:
      [1] POPUP_FIX (弹窗修复) - 无依赖
      [2] VIDEO_DISCOVERY (视频发现) - 依赖 [1]
      [3] COMMENT_CRAWL (评论爬取) - 依赖 [2]
      [4] COMMENT_REPLY (评论回复) - 依赖 [3]
        ↓
    对每个任务:
      assess_page() → execute_subtask() → validate() → fix_or_retry()
    """

    # 页面状态→自动修复动作映射
    STATE_FIX_ACTIONS = {
        PageState.LOGIN_POPUP: ["dismiss_login_popup"],
        PageState.RECOMMENDED_VIDEO_POPUP: ["dismiss_recommended_video"],
        PageState.SINGLE_COLUMN_LAYOUT: ["switch_to_multi_column"],
        PageState.RISK_CONTROL: ["wait_and_retry", "navigate_back"],
        PageState.VIDEO_DETAIL_PAGE: ["go_back_to_search"],
    }

    def __init__(self, crawler_instance: Any):
        """
        初始化主控智能体

        Args:
            crawler_instance: Crawler 实例（必须继承 SearchMixin + PageInteractionMixin 等）
        """
        self.crawler = crawler_instance
        self.page_evaluator: Optional[PageStateEvaluator] = None

        # 子智能体实例（延迟初始化）
        self._video_agent: Optional[VideoDiscoveryAgent] = None
        self._comment_agent: Optional[CommentCrawlAgent] = None
        self._pm_agent: Optional[PrivateMessageAgent] = None
        self._reply_agent: Optional[CommentReplyAgent] = None

        # 执行日志
        self._log: List[str] = []
        self._start_time: float = 0

    # ---- 公开接口 ----

    def execute_search_and_crawl(
        self,
        keyword: str,
        lead_quota: int = 1000,
        max_videos: int = 0,
        auto_reply: bool = False,
        reply_templates: Optional[List[str]] = None,
        comment_keywords: Optional[List[str]] = None,
        platform: str = "douyin",
        on_progress: Optional[Callable[[str, Dict], None]] = None,
    ) -> CrawlExecutionReport:
        """
        执行完整的搜索+爬取流程（主要入口）

        这是最高层接口，自动完成：
        1. 页面状态评估和预修复
        2. 视频发现（搜索+多列布局+收集）
        3. 对每个发现的视频执行评论爬取
        4. （可选）对匹配评论执行回复
        5. （可选）对客户执行私信

        Args:
            keyword: 搜索关键词
            lead_quota: 目标获客数
            max_videos: 最大视频数（0=不限）
            auto_reply: 是否自动回复评论
            reply_templates: 回复模板列表
            comment_keywords: 评论匹配关键词
            platform: 平台
            on_progress: 进度回调 (phase, data) -> None

        Returns:
            CrawlExecutionReport 完整执行报告
        """
        self._start_time = time.time()
        self._log = []

        plan = TaskDecomposer.decompose_search_and_crawl(
            keyword, lead_quota, max_videos, auto_reply,
            reply_templates, comment_keywords, platform
        )

        report = CrawlExecutionReport(
            plan_id=plan.plan_id,
            success=False,
        )

        self._log.append(f"[计划] 创建执行计划: {plan.plan_id}, 共{plan.total_tasks}个子任务")

        try:
            # 执行计划
            for task in plan.tasks:
                # 前置条件：检查停止信号
                if hasattr(self.crawler, 'is_stopped') and self.crawler.is_stopped():
                    report.termination_reason = "stop_requested"
                    self._log.append("[终止] 收到停止信号")
                    break

                # 依赖检查
                deps_met = self._check_dependencies(task, plan)
                if not deps_met:
                    task.status = "skipped"
                    task.error = "依赖任务未完成"
                    self._log.append(f"[跳过] {task.task_id}: 依赖未满足")
                    continue

                # 执行前页面状态评估
                should_execute, fix_actions = self._pre_execution_assessment(task)

                # 执行预修复动作
                if fix_actions:
                    for action in fix_actions:
                        self._execute_fix_action(action)

                # 执行子任务
                task.status = "running"
                task.started_at = time.time()

                if on_progress:
                    on_progress(task.task_type.value, {"task_id": task.task_id, "status": "running"})

                result = self._dispatch_task(task)

                task.finished_at = time.time()
                task.result = result

                # 结果验收
                validation = ResultValidator.validate(task, result)

                if validation["valid"]:
                    task.status = "success"
                    plan.completed_tasks += 1
                    self._log.append(f"[完成] {task.task_id}: {validation['summary']}")
                else:
                    task.status = "failed"
                    task.error = validation.get("reason", "unknown")
                    plan.failed_tasks += 1

                    # 尝试修复/重试
                    if task.retry_count < task.max_retries:
                        fix_result = ResultValidator.suggest_auto_fix(task, validation, self.crawler)
                        if fix_result["fixed"]:
                            task.retry_count += 1
                            # 重新插入到任务队列（简单处理：下一个循环再试）
                            task.status = "pending"
                            self._log.append(f"[重试] {task.task_id}: {fix_result['fix_action']}")
                            continue
                    else:
                        self._log.append(f"[失败] {task.task_id}: {task.error} (已达最大重试)")

                        # 判断是否应该终止整个流程
                        if task.task_type in (TaskType.VIDEO_DISCOVERY,) and not validation.get("partial_success"):
                            # 视频发现完全失败，后续任务无法执行
                            report.termination_reason = f"critical_failure:{task.task_id}"
                            break

                if on_progress:
                    on_progress(task.task_type.value, {
                        "task_id": task.task_id,
                        "status": task.status,
                        "result_summary": validation.get("summary", ""),
                    })

            # 汇总报告
            report.success = plan.failed_tasks == 0 or plan.completed_tasks > 0
            report.total_duration_seconds = time.time() - self._start_time
            report.task_results = {t.task_id: {"status": t.status, "error": t.error} for t in plan.tasks}
            report.execution_log = list(self._log)

            # 从各子任务结果提取统计
            self._extract_statistics(report, plan)

        except Exception as e:
            logger.error(f"[CrawlIntelligentAgent] 执行异常: {e}", exc_info=True)
            report.termination_reason = f"exception: {e}"
            report.execution_log = list(self._log)

        finally:
            self._log.append(f"[结束] 总耗时: {report.total_duration_seconds:.1f}s, "
                           f"成功: {plan.completed_tasks}/{plan.total_tasks}")

        return report

    # ---- 页面状态评估 ----

    def _pre_execution_assessment(self, task: SubTask) -> tuple[bool, List[str]]:
        """执行前评估页面状态"""
        try:
            evaluator = self._get_page_evaluator()
            needs_fix, actions = evaluator.needs_fix_before_crawl()
            return needs_fix, actions
        except Exception as e:
            logger.warning(f"[CrawlIA] 状态评估异常: {e}")
            return False, []

    def _get_page_evaluator(self) -> PageStateEvaluator:
        """获取或创建页面状态评估器"""
        if self.page_evaluator is None:
            if hasattr(self.crawler, 'page'):
                self.page_evaluator = PageStateEvaluator(self.crawler.page)
            else:
                # 创建一个空壳评估器
                self.page_evaluator = PageStateEvaluator(None)
        return self.page_evaluator

    # ---- 任务调度 ----

    def _dispatch_task(self, task: SubTask) -> Any:
        """分发任务到对应的子智能体"""
        task_type = task.task_type

        if task_type == TaskType.POPUP_FIX:
            return self._execute_popup_fix(task)
        elif task_type == TaskType.VIDEO_DISCOVERY:
            return self._execute_video_discovery(task)
        elif task_type == TaskType.COMMENT_CRAWL:
            return self._execute_comment_crawl(task)
        elif task_type == TaskType.COMMENT_REPLY:
            return self._execute_comment_reply(task)
        elif task_type == TaskType.PRIVATE_MESSAGE:
            return self._execute_private_message(task)
        elif task_type == TaskType.LAYOUT_FIX:
            return self._execute_layout_fix(task)
        else:
            return {"status": "error", "detail": f"未知任务类型: {task_type}"}

    def _execute_popup_fix(self, task: SubTask) -> Dict:
        """执行弹窗修复"""
        fixed_count = 0
        max_rounds = task.params.get("max_rounds", 2)

        for round_num in range(max_rounds):
            round_fixed = 0

            # 登录弹窗
            if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
                try:
                    if self.crawler._dismiss_login_popup_if_present():
                        fixed_count += 1
                        round_fixed += 1
                except Exception:
                    pass

            # 推荐视频弹窗
            if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
                try:
                    if self.crawler._dismiss_recommended_video_if_present(max_rounds=1):
                        fixed_count += 1
                        round_fixed += 1
                except Exception:
                    pass

            # 搜索结果表面清理
            if hasattr(self.crawler, '_ensure_search_results_surface_clear'):
                try:
                    self.crawler._ensure_search_results_surface_clear()
                except Exception:
                    pass

            if round_fixed == 0:
                break  # 本轮没有修复任何东西，不再重试

        return {
            "status": "success" if fixed_count > 0 else "no_action_needed",
            "fixed_count": fixed_count,
            "detail": f"修复了{fixed_count}个弹窗问题",
        }

    def _execute_video_discovery(self, task: SubTask) -> DiscoveryResult:
        """执行视频发现"""
        agent = self._get_video_agent()
        params = task.params
        return agent.discover(
            keyword=params.get("keyword", ""),
            max_results=params.get("max_videos", 0),
        )

    def _execute_comment_crawl(self, task: SubTask) -> CommentCrawlResult:
        """执行评论爬取"""
        agent = self._get_comment_agent()

        # 获取上一个视频发现任务的发现的视频列表
        videos = []
        prev_result = None
        for t in getattr(self, '_current_plan', type('', (), {'tasks': []})()).tasks:
            if t.task_type == TaskType.VIDEO_DISCOVERY and t.result:
                prev_result = t.result
                break

        if prev_result and hasattr(prev_result, 'videos'):
            videos = prev_result.videos

        if not videos:
            return CommentCrawlResult(
                success=False,
                termination_reason="no_videos_to_crawl",
            )

        # 取第一个视频进行评论爬取（简化版；完整版应遍历所有视频）
        first_video = videos[0] if videos else {}

        return agent.crawl(
            video_url=first_video.get("url", ""),
            aweme_id=first_video.get("aweme_id", ""),
            comment_keywords=task.params.get("comment_keywords", []),
            max_requests=task.params.get("max_requests", 50),
        )

    def _execute_comment_reply(self, task: SubTask) -> ReplyResult:
        """执行评论回复"""
        agent = self._get_reply_agent()
        # 这里需要从评论爬取结果中找到匹配的评论
        # 简化实现：返回待执行状态
        return ReplyResult(
            success=False,
            reason="delegated_to_crawler_engine",
        )

    def _execute_private_message(self, task: SubTask) -> PMResult:
        """执行私信"""
        agent = self._get_pm_agent()
        return agent.send(
            user_id=task.params.get("user_id", ""),
            message=task.params.get("message", ""),
        )

    def _execute_layout_fix(self, task: SubTask) -> Dict:
        """执行布局修复"""
        switched = False
        if hasattr(self.crawler, '_ensure_multi_column_search_layout'):
            try:
                self.crawler._ensure_multi_column_search_layout()
                switched = True
            except Exception as e:
                return {"status": "failed", "detail": str(e)}
        return {"status": "success", "switched": switched, "detail": "布局已切换" if switched else "无需切换"}

    def _execute_fix_action(self, action: str) -> bool:
        """执行单个修复动作"""
        try:
            if action == "dismiss_login_popup":
                if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
                    return self.crawler._dismiss_login_popup_if_present()
            elif action == "dismiss_recommended_video":
                if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
                    return self.crawler._dismiss_recommended_video_if_present(max_rounds=2)
            elif action == "switch_to_multi_column":
                if hasattr(self.crawler, '_ensure_multi_column_search_layout'):
                    self.crawler._ensure_multi_column_search_layout()
                    return True
            elif action == "wait_and_retry":
                import time; time.sleep(2)
                return True
        except Exception as e:
            logger.warning(f"[CrawlIA] 修复动作[{action}]失败: {e}")
        return False

    # ---- 辅助方法 ----

    def _check_dependencies(self, task: SubTask, plan: CrawlPlan) -> bool:
        """检查任务依赖是否满足"""
        for dep_id in task.dependencies:
            dep_task = next((t for t in plan.tasks if t.task_id == dep_id), None)
            if dep_task and dep_task.status != "success":
                return False
        return True

    def _extract_statistics(self, report: CrawlExecutionReport, plan: CrawlPlan):
        """从各子任务结果中提取统计数据"""
        for task in plan.tasks:
            result = task.result
            if result is None:
                continue

            if isinstance(result, DiscoveryResult):
                report.videos_discovered = result.videos_discovered
                report.popup_fixed_count = result.popup_closed_count
                report.layout_switched = result.layout_switched
            elif isinstance(result, CommentCrawlResult):
                report.comments_crawled = result.total_comments
            elif isinstance(result, PMResult):
                if result.success:
                    report.messages_sent += 1
            elif isinstance(result, ReplyResult):
                if result.success:
                    report.replies_posted += 1

    # ---- 子智能体工厂 ----

    def _get_video_agent(self) -> VideoDiscoveryAgent:
        if self._video_agent is None:
            self._video_agent = VideoDiscoveryAgent(self.crawler)
        return self._video_agent

    def _get_comment_agent(self) -> CommentCrawlAgent:
        if self._comment_agent is None:
            self._comment_agent = CommentCrawlAgent(self.crawler)
        return self._comment_agent

    def _get_pm_agent(self) -> PrivateMessageAgent:
        if self._pm_agent is None:
            sender = getattr(self.crawler, 'message_sender', None) or self.crawler
            self._pm_agent = PrivateMessageAgent(sender)
        return self._pm_agent

    def _get_reply_agent(self) -> CommentReplyAgent:
        if self._reply_agent is None:
            self._reply_agent = CommentReplyAgent(self.crawler)
        return self._reply_agent

    def get_status_report(self) -> Dict:
        """获取当前状态快照"""
        return {
            "agent_type": "CrawlIntelligentAgent",
            "sub_agents_initialized": {
                "video": self._video_agent is not None,
                "comment": self._comment_agent is not None,
                "pm": self._pm_agent is not None,
                "reply": self._reply_agent is not None,
            },
            "evaluator_initialized": self.page_evaluator is not None,
            "log_entries": len(self._log),
            "recent_logs": self._log[-10:] if self._log else [],
        }
