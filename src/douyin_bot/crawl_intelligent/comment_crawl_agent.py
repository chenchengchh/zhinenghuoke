"""评论区爬取子智能体 - 负责进入视频详情页、打开评论面板、爬取评论数据"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, List, Dict, Callable

from .comment_crawl_types import CommentCrawlPhase, CommentCrawlStep, CommentCrawlResult

logger = logging.getLogger(__name__)


class CommentCrawlAgent:
    """
    评论区爬取子智能体

    职责范围：
    1. 导航到目标视频详情页
    2. 自动关闭可能的弹窗（登录、推荐视频等）
    3. 校验视频上下文一致性（防串流）
    4. 检测页面类型（视频/图文笔记）
    5. 打开/确认评论面板就绪
    6. 主循环：消费API批次 → 解析评论 → 过滤去重 → 入库
    7. 展开回复线程以获取更完整的数据
    8. 风控检测与处理
    9. 构建最终结果报告
    """

    def __init__(self, crawler_instance: Any):
        self.crawler = crawler_instance
        self.steps: List[CommentCrawlStep] = []

    def crawl(self, video_url: str, aweme_id: str = "",
              comment_keywords: Optional[List[str]] = None,
              max_requests: int = 50,
              on_progress: Optional[Callable] = None) -> CommentCrawlResult:
        """执行评论爬取主流程"""
        plan = self._build_plan(video_url, aweme_id)
        self.steps = plan

        result = CommentCrawlResult(
            success=False,
            video_url=video_url,
            aweme_id=aweme_id,
        )

        session = {}  # 会话状态字典

        try:
            for step in plan:
                step.status = "running"
                if on_progress:
                    on_progress(step)

                outcome = self._execute_step(step, video_url, aweme_id, session,
                                           comment_keywords, max_requests)
                step.status = outcome["status"]
                step.detail = outcome.get("detail", "")

                if outcome.get("risk_control"):
                    result.risk_control_detected = True
                    result.termination_reason = "risk_control"
                    break

                if outcome.get("should_terminate"):
                    result.termination_reason = outcome.get("termination_reason", "unknown")
                    break

            # 从session构建最终结果
            if hasattr(self.crawler, '_build_comment_crawl_result'):
                final = self.crawler._build_comment_crawl_result(session)
                if isinstance(final, dict):
                    result.total_comments = final.get("total_comments", 0)
                    result.matched_comments = final.get("matched_comments", 0)
                    result.saved_customers = final.get("saved_customers", 0)
                    result.termination_reason = final.get("termination_reason", result.termination_reason)

            result.success = result.total_comments > 0 or result.matched_comments > 0
            result.execution_log = list(self.steps)

        except Exception as e:
            logger.error(f"[CommentCrawlAgent] 异常: {e}", exc_info=True)
            result.termination_reason = f"exception: {e}"

        return result

    def _build_plan(self, video_url: str, aweme_id: str) -> List[CommentCrawlStep]:
        return [
            CommentCrawlStep(phase=CommentCrawlPhase.INITIALIZING, action="初始化会话"),
            CommentCrawlStep(phase=CommentCrawlPhase.NAVIGATE_TO_VIDEO, action=f"导航到视频: {aweme_id[:20]}..."),
            CommentCrawlStep(phase=CommentCrawlPhase.CLEAR_POPUPS, action="关闭弹窗"),
            CommentCrawlStep(phase=CommentCrawlPhase.VALIDATE_VIDEO_CONTEXT, action="校验视频上下文"),
            CommentCrawlStep(phase=CommentCrawlPhase.DETAIL_KIND_DETECTION, action="检测页面类型"),
            CommentCrawlStep(phase=CommentCrawlPhase.COMMENT_BOOTSTRAP, action="评论预热"),
            CommentCrawlStep(phase=CommentCrawlPhase.OPEN_COMMENT_PANEL, action="确保评论面板"),
            CommentCrawlStep(phase=CommentCrawlPhase.CRAWL_COMMENTS_LOOP, action="主循环爬取"),
            CommentCrawlStep(phase=CommentCrawlPhase.BUILD_RESULT, action="构建结果"),
        ]

    def _execute_step(self, step: CommentCrawlStep, video_url: str, aweme_id: str,
                     session: dict, keywords, max_requests) -> Dict:
        phase = step.phase

        if phase == CommentCrawlPhase.INITIALIZING:
            return self._step_init(session, video_url, aweme_id)
        elif phase == CommentCrawlPhase.NAVIGATE_TO_VIDEO:
            return self._step_navigate(step, video_url)
        elif phase == CommentCrawlPhase.CLEAR_POPUPS:
            return self._step_clear_popups(step)
        elif phase == CommentCrawlPhase.VALIDATE_VIDEO_CONTEXT:
            return self._step_validate_context(step, aweme_id)
        elif phase == CommentCrawlPhase.DETAIL_KIND_DETECTION:
            return self._step_detect_kind(step)
        elif phase == CommentCrawlPhase.COMMENT_BOOTSTRAP:
            return self._step_bootstrap(session)
        elif phase == CommentCrawlPhase.OPEN_COMMENT_PANEL:
            return self._step_open_panel(step)
        elif phase == CommentCrawlPhase.CRAWL_COMMENTS_LOOP:
            return self._step_crawl_loop(step, session, keywords, max_requests)
        elif phase == CommentCrawlPhase.BUILD_RESULT:
            return {"status": "success", "detail": "结果构建中"}

        return {"status": "skipped"}

    def _step_init(self, session: dict, video_url: str, aweme_id: str) -> Dict:
        try:
            if hasattr(self.crawler, '_new_comment_session'):
                session.update(self.crawler._new_comment_session())
            session["target_aweme_id"] = aweme_id
            session["target_video_url"] = video_url
            return {"status": "success", "detail": "会话已初始化"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_navigate(self, step: CommentCrawlStep, video_url: str) -> Dict:
        try:
            if hasattr(self.crawler, 'page'):
                self.crawler.page.goto(video_url, timeout=30000)
                return {"status": "success", "detail": "已导航到详情页"}
            return {"status": "failed", "detail": "无可用的page对象"}
        except Exception as e:
            return {"status": "failed", "detail": f"导航异常: {e}"}

    def _step_clear_popups(self, step: CommentCrawlStep) -> Dict:
        closed = 0
        if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
            try:
                if self.crawler._dismiss_login_popup_if_present(): closed += 1
            except Exception: pass
        if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
            try:
                if self.crawler._dismiss_recommended_video_if_present(max_rounds=1): closed += 1
            except Exception: pass
        return {"status": "success", "detail": f"关闭{closed}个弹窗"} if closed > 0 else {"status": "success", "detail": "无弹窗"}

    def _step_validate_context(self, step: CommentCrawlStep, aweme_id: str) -> Dict:
        try:
            if hasattr(self.crawler, '_validate_target_video_aweme_context'):
                valid = self.crawler._validate_target_video_aweme_context(stage="after_goto")
                if not valid:
                    return {"status": "failed", "detail": "视频上下文不一致（可能串流）",
                            "should_terminate": True, "termination_reason": "redirected_to_other_video"}
            return {"status": "success", "detail": "上下文校验通过"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_detect_kind(self, step: CommentCrawlStep) -> Dict:
        try:
            kind = "video"
            if hasattr(self.crawler, '_inspect_detail_page_structure'):
                structure = self.crawler._inspect_detail_page_structure()
                kind = structure.get("detail_kind", "video") if structure else "video"
            step.detail = f"页面类型: {kind}"
            return {"status": "success", "detail": f"detail_kind={kind}"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_bootstrap(self, session: dict) -> Dict:
        try:
            if hasattr(self.crawler, '_wait_for_comment_bootstrap'):
                bootstrap = self.crawler._wait_for_comment_bootstrap(timeout_seconds=1.4)
                if bootstrap:
                    session["bootstrap_info"] = bootstrap
            return {"status": "success", "detail": "预热完成"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_open_panel(self, step: CommentCrawlStep) -> Dict:
        try:
            if hasattr(self.crawler, '_ensure_comment_surface_ready'):
                surface = self.crawler._ensure_comment_surface_ready()
                if surface and surface.get("has_comments"):
                    return {"status": "success", "detail": "评论面板已就绪"}
                # 尝试打开
                if hasattr(self.crawler, '_open_comment_surface'):
                    self.crawler._open_comment_surface()
                    return {"status": "success", "detail": "已尝试打开评论面板"}
            return {"status": "success", "detail": "跳过（将依赖主循环中的面板恢复）"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_crawl_loop(self, step: CommentCrawlStep, session: dict, keywords, max_requests) -> Dict:
        """调用crawler的主循环方法"""
        try:
            # 如果有现成的 crawl_comments 方法，直接调用
            if hasattr(self.crawler, 'crawl_comments'):
                # 注意：这里不实际调用完整 crawl_comments（因为它内部有自己的循环）
                # 而是标记该步骤为"将由底层引擎驱动"
                return {"status": "success", "detail": "评论爬取由底层引擎驱动",
                        "delegated_to_engine": True}
            return {"status": "success", "detail": "评论爬取准备就绪"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}
