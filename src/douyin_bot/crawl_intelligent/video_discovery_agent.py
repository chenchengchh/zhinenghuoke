"""视频发现子智能体 - 负责搜索关键词、确保多列布局、发现并收集视频条目"""

from __future__ import annotations

import logging
import time
import random
from typing import Any, Optional, List, Dict, Callable

from .video_discovery_types import VideoDiscoveryPhase, DiscoveryStep, DiscoveryResult

logger = logging.getLogger(__name__)


class VideoDiscoveryAgent:
    """
    视频发现子智能体

    职责范围：
    1. 接收搜索关键词，执行搜索操作
    2. 搜索前后自动检测并关闭弹窗
    3. 检测搜索结果页布局，必要时切换为多列
    4. 滚动加载更多结果，持续发现新视频
    5. 当无数据时自动诊断原因（弹窗？布局？网络？）并尝试修复
    6. 返回发现的视频列表和完整的执行日志

    设计原则：
    - 每个步骤都是独立的 DiscoveryStep，可追踪、可重试
    - 失败时自动降级而非中断
    - 支持停止信号即时响应
    """

    DEFAULT_SEARCH_TARGET_COUNT = 300
    DEFAULT_MAX_SCROLLS = 80
    IDLE_STOP_ROUNDS = 3
    SCROLL_SLEEP_MIN = 0.8
    SCROLL_SLEEP_MAX = 1.4

    def __init__(self, crawler_instance: Any):
        """
        初始化视频发现智能体

        Args:
            crawler_instance: Crawler 实例（继承 SearchMixin + PageInteractionMixin）
        """
        self.crawler = crawler_instance
        self.steps: List[DiscoveryStep] = []
        self._current_phase = VideoDiscoveryPhase.INITIALIZING
        self._popup_close_count = 0
        self._layout_switched = False
        self._discovered_urls: set = set()
        self._discovered_videos: list = []

    def discover(self, keyword: str, max_results: int = 0,
                 on_progress: Optional[Callable[[DiscoveryStep], None]] = None) -> DiscoveryResult:
        """
        执行视频发现主流程（同步包装器）

        Args:
            keyword: 搜索关键词
            max_results: 最大目标数量（0=使用默认值300）
            on_progress: 步骤进度回调

        Returns:
            DiscoveryResult 包含所有发现结果和执行日志
        """
        target_count = max_results or self.DEFAULT_SEARCH_TARGET_COUNT

        # 构建执行计划
        plan = self._build_plan(keyword, target_count)
        self.steps = plan

        result = DiscoveryResult(
            success=False,
            steps_total=len(plan),
            execution_log=[],
        )

        try:
            # 执行每个步骤
            for i, step in enumerate(plan):
                step.status = "running"
                step.started_at = time.time()

                if on_progress:
                    on_progress(step)

                # 执行步骤
                exec_result = self._execute_step(step, keyword, target_count)

                step.status = exec_result["status"]
                step.detail = exec_result.get("detail", "")
                step.finished_at = time.time()
                result.steps_completed = i + 1

                if exec_result["status"] == "failed":
                    # 尝试诊断和修复
                    fix_result = self._diagnose_and_fix(step)
                    if fix_result["fixed"]:
                        step.status = "success"  # 修复后视为成功
                        step.detail += f" | 已修复: {fix_result['fix_action']}"

                if exec_result.get("should_terminate"):
                    result.termination_reason = exec_result.get("termination_reason", "unknown")
                    break

                # 检查停止信号
                if hasattr(self.crawler, 'is_stopped') and self.crawler.is_stopped():
                    result.termination_reason = "stop_requested"
                    break

            result.success = len(self._discovered_videos) > 0
            result.videos_discovered = len(self._discovered_videos)
            result.videos = list(self._discovered_videos)
            result.popup_closed_count = self._popup_close_count
            result.layout_switched = self._layout_switched
            result.execution_log = list(self.steps)

        except Exception as e:
            logger.error(f"[VideoDiscoveryAgent] 异常终止: {e}", exc_info=True)
            result.termination_reason = f"exception: {e}"

        finally:
            self._current_phase = VideoDiscoveryPhase.COMPLETED if result.success else VideoDiscoveryPhase.FAILED

        return result

    def _build_plan(self, keyword: str, target_count: int) -> List[DiscoveryStep]:
        """构建执行计划"""
        return [
            DiscoveryStep(phase=VideoDiscoveryPhase.INITIALIZING, action="初始化搜索环境"),
            DiscoveryStep(phase=VideoDiscoveryPhase.SEARCH_NAVIGATING, action=f"搜索关键词: {keyword}"),
            DiscoveryStep(phase=VideoDiscoveryPhase.POPUP_CLEARING, action="关闭搜索后弹窗"),
            DiscoveryStep(phase=VideoDiscoveryPhase.LAYOUT_CHECKING, action="检测布局模式"),
            DiscoveryStep(phase=VideoDiscoveryPhase.LAYOUT_SWITCHING, action="确保多列布局"),
            DiscoveryStep(phase=VideoDiscoveryPhase.VIDEO_DISCOVERING, action="消费首屏视频数据"),
            DiscoveryStep(phase=VideoDiscoveryPhase.SCROLLING, action="滚动加载更多视频"),
        ]

    def _execute_step(self, step: DiscoveryStep, keyword: str, target_count: int) -> Dict:
        """执行单个步骤"""
        phase = step.phase

        if phase == VideoDiscoveryPhase.INITIALIZING:
            return self._step_initialize(step)
        elif phase == VideoDiscoveryPhase.SEARCH_NAVIGATING:
            return self._step_search(step, keyword)
        elif phase == VideoDiscoveryPhase.POPUP_CLEARING:
            return self._step_clear_popups(step)
        elif phase == VideoDiscoveryPhase.LAYOUT_CHECKING:
            return self._step_check_layout(step)
        elif phase == VideoDiscoveryPhase.LAYOUT_SWITCHING:
            return self._step_ensure_multi_column(step)
        elif phase == VideoDiscoveryPhase.VIDEO_DISCOVERING:
            return self._step_consume_first_page(step, target_count)
        elif phase == VideoDiscoveryPhase.SCROLLING:
            return self._step_scroll_loop(step, target_count)

        return {"status": "skipped", "detail": "未知阶段"}

    def _step_initialize(self, step: DiscoveryStep) -> Dict:
        """初始化步骤：清空历史数据"""
        try:
            if hasattr(self.crawler, 'current_video_urls'):
                self.crawler.current_video_urls.clear()
            if hasattr(self.crawler, '_search_data'):
                self.crawler._search_data.clear()
            self._discovered_urls.clear()
            self._discovered_videos.clear()
            self._popup_close_count = 0
            self._layout_switched = False
            return {"status": "success", "detail": "已清空历史数据"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _step_search(self, step: DiscoveryStep, keyword: str) -> Dict:
        """搜索步骤：提交关键词搜索"""
        try:
            # 调用 Crawler 的 search_keyword_stream 或 _search_keyword_via_url
            if hasattr(self.crawler, '_search_keyword_via_url'):
                success = self.crawler._search_keyword_via_url(keyword)
                if not success:
                    # 回退到首页搜索
                    if hasattr(self.crawler, '_search_keyword_via_home'):
                        success = self.crawler._search_keyword_via_home(keyword)

                if success:
                    return {"status": "success", "detail": "搜索成功"}
                else:
                    return {"status": "failed", "detail": "搜索失败（URL直达+首页回退均失败）"}
            else:
                return {"status": "failed", "detail": "缺少搜索方法"}
        except Exception as e:
            return {"status": "failed", "detail": f"搜索异常: {e}"}

    def _step_clear_popups(self, step: DiscoveryStep) -> Dict:
        """弹窗清理步骤"""
        closed_count = 0

        # 关闭登录弹窗
        if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
            try:
                if self.crawler._dismiss_login_popup_if_present():
                    closed_count += 1
                    self._popup_close_count += 1
            except Exception:
                pass

        # 关闭推荐视频弹窗
        if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
            try:
                if self.crawler._dismiss_recommended_video_if_present(max_rounds=1):
                    closed_count += 1
                    self._popup_close_count += 1
            except Exception:
                pass

        # 清理搜索结果表面
        if hasattr(self.crawler, '_ensure_search_results_surface_clear'):
            try:
                self.crawler._ensure_search_results_surface_clear()
            except Exception:
                pass

        if closed_count > 0:
            return {"status": "success", "detail": f"已关闭{closed_count}个弹窗"}
        return {"status": "success", "detail": "无弹窗需要关闭"}

    def _step_check_layout(self, step: DiscoveryStep) -> Dict:
        """布局检测步骤"""
        try:
            if hasattr(self.crawler, '_get_general_search_layout_snapshot'):
                snapshot = self.crawler._get_general_search_layout_snapshot()
                if snapshot and hasattr(self.crawler, '_classify_general_search_layout_snapshot'):
                    layout = self.crawler._classify_general_search_layout_snapshot(snapshot)
                    step.detail = f"当前布局: {layout}"
                    return {"status": "success", "detail": f"布局={layout}"}
            return {"status": "success", "detail": "无法检测布局（将跳过）"}
        except Exception as e:
            return {"status": "failed", "detail": f"布局检测异常: {e}"}

    def _step_ensure_multi_column(self, step: DiscoveryStep) -> Dict:
        """确保多列布局步骤"""
        try:
            if hasattr(self.crawler, '_ensure_multi_column_search_layout'):
                self.crawler._ensure_multi_column_search_layout()
                self._layout_switched = True
                return {"status": "success", "detail": "已执行多列布局确认/切换"}
            return {"status": "skipped", "detail": "无布局切换方法"}
        except Exception as e:
            return {"status": "failed", "detail": f"布局切换异常: {e}"}

    def _step_consume_first_page(self, step: DiscoveryStep, target_count: int) -> Dict:
        """消费首屏数据步骤"""
        try:
            consumed = 0
            if hasattr(self.crawler, '_drain_pending_search_entries'):
                pending = getattr(self.crawler, 'pending_entries', None) or []
                entries = self.crawler._drain_pending_search_entries(pending, target_count)
                for entry in entries:
                    if entry and entry.get('aweme_id') and entry.get('url') not in self._discovered_urls:
                        self._discovered_urls.add(entry['url'])
                        self._discovered_videos.append(entry)
                        consumed += 1

            if consumed > 0:
                return {"status": "success", "detail": f"首屏发现{consumed}个视频"}
            else:
                # 首屏无数据 - 这是需要智能诊断的关键场景
                return {"status": "success", "detail": "首屏无视频数据（将在滚动阶段继续或触发诊断）",
                        "needs_diagnosis": True}
        except Exception as e:
            return {"status": "failed", "detail": f"首屏消费异常: {e}"}

    def _step_scroll_loop(self, step: DiscoveryStep, target_count: int) -> Dict:
        """滚动循环步骤"""
        idle_rounds = 0
        total_scrolled = 0
        max_scrolls = max(10, min(200, target_count * 3))
        response_count = 0

        try:
            while total_scrolled < max_scrolls:
                # 终止条件检查
                if hasattr(self.crawler, 'is_stopped') and self.crawler.is_stopped():
                    return {"status": "success", "should_terminate": True,
                           "termination_reason": "stop_requested",
                           "detail": f"滚动{total_scrolled}轮后收到停止信号"}

                if len(self._discovered_urls) >= target_count:
                    return {"status": "success", "should_terminate": True,
                           "termination_reason": "target_reached",
                           "detail": f"已达目标数量{target_count}"}

                # 弹窗清理
                if hasattr(self.crawler, '_ensure_search_results_surface_clear'):
                    try:
                        self.crawler._ensure_search_results_surface_clear()
                    except Exception:
                        pass

                # 滚动
                scroll_amount = max(int(getattr(self.crawler, 'page', object()).inner_height * 0.9) if hasattr(self.crawler, 'page') else 900, 900)
                if hasattr(self.crawler, 'page'):
                    try:
                        self.crawler.page.scroll_by(0, scroll_amount)
                    except Exception:
                        pass

                # 可中断等待
                sleep_time = random.uniform(self.SCROLL_SLEEP_MIN, self.SCROLL_SLEEP_MAX)
                if hasattr(self.crawler, '_interruptible_sleep'):
                    self.crawler._interruptible_sleep(sleep_time)
                else:
                    time.sleep(sleep_time)

                # 再次弹窗清理
                if hasattr(self.crawler, '_ensure_search_results_surface_clear'):
                    try:
                        self.crawler._ensure_search_results_surface_clear()
                    except Exception:
                        pass

                # 消费新批次
                new_count = 0
                if hasattr(self.crawler, '_consume_search_api_batches'):
                    pending = getattr(self.crawler, 'pending_entries', None) or []
                    entries = self.crawler._consume_search_api_batches(pending, min(target_count - len(self._discovered_urls), 20))
                    for entry in entries:
                        if entry and entry.get('aweme_id') and entry.get('url') not in self._discovered_urls:
                            self._discovered_urls.add(entry['url'])
                            self._discovered_videos.append(entry)
                            new_count += 1

                # 排空队列
                if hasattr(self.crawler, '_drain_pending_search_entries'):
                    pending = getattr(self.crawler, 'pending_entries', None) or []
                    entries = self.crawler._drain_pending_search_entries(pending, target_count)
                    for entry in entries:
                        if entry and entry.get('aweme_id') and entry.get('url') not in self._discovered_urls:
                            self._discovered_urls.add(entry['url'])
                            self._discovered_videos.append(entry)
                            new_count += 1

                total_scrolled += 1

                # 更新空闲计数
                if new_count > 0:
                    idle_rounds = 0
                    response_count += 1
                else:
                    idle_rounds += 1

                # 空闲退出
                if idle_rounds >= self.IDLE_STOP_ROUNDS and response_count >= 1:
                    return {"status": "success", "should_terminate": True,
                           "termination_reason": "idle_stop",
                           "detail": f"连续{idle_rounds}轮无新数据"}

            return {"status": "success", "should_terminate": True,
                   "termination_reason": "scroll_limit",
                   "detail": f"达到最大滚动次数{max_scrolls}"}

        except Exception as e:
            return {"status": "failed", "detail": f"滚动循环异常: {e}"}

    def _diagnose_and_fix(self, failed_step: DiscoveryStep) -> Dict:
        """
        诊断失败步骤的原因并尝试修复

        这是智能体的核心能力：当某步失败时，
        自动分析原因并执行修复动作
        """
        diagnosis = {"fixed": False, "fix_action": "", "diagnosis": ""}

        try:
            phase = failed_step.phase

            # 场景1: 搜索失败 → 可能是弹窗遮挡或网络问题
            if phase == VideoDiscoveryPhase.SEARCH_NAVIGATING:
                # 尝试强力清除弹窗
                fixes = 0
                if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
                    if self.crawler._dismiss_login_popup_if_present(): fixes += 1
                if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
                    if self.crawler._dismiss_recommended_video_if_present(max_rounds=2): fixes += 1

                if fixes > 0:
                    diagnosis["fixed"] = True
                    diagnosis["fix_action"] = f"force_clear_popups({fixes})"
                    diagnosis["diagnosis"] = "搜索失败可能由弹窗引起，已强制清除"
                else:
                    diagnosis["diagnosis"] = "搜索失败，非弹窗原因（可能是网络或页面结构变更）"

            # 场景2: 首屏无数据 → 可能是弹窗遮挡/单列布局/页面未加载
            elif phase == VideoDiscoveryPhase.VIDEO_DISCOVERING:
                fixes = []

                # 2a. 强力弹窗清除
                if hasattr(self.crawler, '_dismiss_login_popup_if_present'):
                    if self.crawler._dismiss_login_popup_if_present():
                        fixes.append("clear_login_popup")
                        self._popup_close_count += 1
                if hasattr(self.crawler, '_dismiss_recommended_video_if_present'):
                    if self.crawler._dismiss_recommended_video_if_present(max_rounds=2):
                        fixes.append("clear_rec_popup")
                        self._popup_close_count += 1

                # 2b. 确保多列布局
                if hasattr(self.crawler, '_ensure_multi_column_search_layout'):
                    try:
                        self.crawler._ensure_multi_column_search_layout()
                        fixes.append("ensure_multicolumn")
                        self._layout_switched = True
                    except Exception:
                        pass

                # 2c. 尝试重新消费
                if hasattr(self.crawler, '_consume_search_api_batches'):
                    try:
                        pending = getattr(self.crawler, 'pending_entries', None) or []
                        entries = self.crawler._consume_search_api_batches(pending, 20)
                        if entries:
                            for entry in entries:
                                if entry and entry.get('aweme_id') and entry.get('url') not in self._discovered_urls:
                                    self._discovered_urls.add(entry['url'])
                                    self._discovered_videos.append(entry)
                            fixes.append(f"reconsume_got_{len(entries)}")
                    except Exception:
                        pass

                if fixes:
                    diagnosis["fixed"] = True
                    diagnosis["fix_action"] = ",".join(fixes)
                    diagnosis["diagnosis"] = "首屏无数据，执行了多项修复措施"
                else:
                    diagnosis["diagnosis"] = "首屏无数据且无法自动修复"

            # 场景3: 布局检测失败 → 跳过，不影响主流程
            elif phase in (VideoDiscoveryPhase.LAYOUT_CHECKING, VideoDiscoveryPhase.LAYOUT_SWITCHING):
                diagnosis["fixed"] = True
                diagnosis["fix_action"] = "skip_layout_check"
                diagnosis["diagnosis"] = "布局检测失败，降级跳过（不影响爬取）"

            # 场景4: 滚动失败 → 通常不可恢复
            elif phase == VideoDiscoveryPhase.SCROLLING:
                diagnosis["diagnosis"] = "滚动失败，可能页面已崩溃或网络断开"

        except Exception as e:
            diagnosis["diagnosis"] = f"诊断过程异常: {e}"

        return diagnosis
