"""页面状态评估器模块。

本模块提供 PageStateEvaluator 类，用于智能体在执行爬取任务前评估当前页面状态，
判断是否存在弹窗遮挡、布局模式是否正确、是否触发风控等问题，并给出推荐的修复动作。

主要功能：
- 弹窗检测：登录弹窗、推荐视频弹窗等
- 布局评估：单列/多列布局识别与切换建议
- 风控检测：验证码页面识别
- 综合评估：输出完整的页面状态报告与修复动作列表
"""

from __future__ import annotations

import time
import logging
from typing import Optional, Any, Dict, List

from playwright.async_api import Page as AsyncPage

from .page_state_types import (
    PageState, PopupType, PopupDetectionResult, LayoutState, PageStateAssessment
)

logger = logging.getLogger(__name__)


class PageStateEvaluator:
    """
    页面状态评估器
    
    职责：
    1. 检测当前页面是否有弹窗遮挡
    2. 检测搜索结果页面的布局模式（单列/多列）
    3. 检测是否有风控/验证码页面
    4. 综合评估页面状态，给出推荐动作
    """

    # 登录弹窗选择器
    LOGIN_POPUP_SELECTORS = [
        '[data-e2e="login-close"]',
        '[data-e2e="modal-close-inner-button"]',
        '[class*="close"]:has(svg)',
        'button[aria-label*="关闭"]',
        'button:has-text("关闭")',
    ]

    # 推荐视频弹窗关闭选择器
    RECOMMENDED_VIDEO_CLOSE_SELECTORS = [
        '[data-e2e="feed-close"]',
        '[data-e2e="detail-close"]',
        '[data-e2e="video-detail-close"]',
        '[data-e2e="close-detail"]',
        '.xgplayer-close',
        '.video-detail-close',
        'div[class*="close"]',
        'svg[class*="close"]',
    ]

    # 推荐视频弹窗表面检测选择器
    RECOMMENDED_VIDEO_SURFACE_SELECTORS = [
        '[data-e2e="feed-active-video"]',
        '[data-e2e="detail-player"]',
        '[data-e2e="video-player"]',
        '[data-e2e="recommend-video"]',
        '[data-e2e="video-detail"]',
        '[class*="video-detail"]',
        '[class*="detail-player"]',
        '[class*="recommend-video"]',
        '[class*="player-container"]',
        '[class*="xgplayer"]',
    ]

    # 风控关键词
    RISK_CONTROL_KEYWORDS = [
        "验证码", "请完成验证", "访问受限", "操作过于频繁",
        "稍后再试", "账号异常", "风险提示", "验证后继续",
        "captcha", "verification"
    ]

    # 布局控制按钮文本
    LAYOUT_MULTI_TEXTS = ("多列", "双列")
    LAYOUT_SINGLE_TEXTS = ("单列",)

    def __init__(self, page: Any):  # page 可以是 Playwright sync/async Page
        self.page = page

    async def assess_full(self) -> PageStateAssessment:
        """
        完整页面状态评估（主入口）
        
        返回包含所有维度评估结果的 PageStateAssessment
        """
        assessment = PageStateAssessment(
            timestamp=time.time(),
            url=self._get_url(),
        )

        # 1. 弹窗检测
        popup_result = await self.detect_popup()
        assessment.popup_result = popup_result

        # 2. 布局检测
        layout_state = await self.evaluate_layout()
        assessment.layout_state = layout_state

        # 3. 风控检测
        risk_detected = await self.detect_risk_control()

        # 4. 综合判断
        issues = []
        actions = []

        if popup_result.has_popup:
            issues.append(f"弹窗遮挡: {popup_result.popup_type.value if popup_result.popup_type else 'unknown'}")
            if popup_result.close_selectors:
                actions.append(f"close_popup:{popup_result.popup_type.value}")
            if popup_result.severity == "high":
                assessment.page_state = PageState.LOGIN_POPUP if popup_result.popup_type == PopupType.LOGIN_MODAL else PageState.RECOMMENDED_VIDEO_POPUP

        if layout_state and layout_state.layout_type == "single_column":
            issues.append(f"布局为单列模式(需切换为多列)")
            actions.append("switch_to_multi_column")
            if assessment.page_state == PageState.NORMAL:
                assessment.page_state = PageState.SINGLE_COLUMN_LAYOUT

        if risk_detected:
            issues.append("检测到风控/验证码页面")
            actions.append("handle_risk_control")
            assessment.page_state = PageState.RISK_CONTROL

        assessment.issues = issues
        assessment.recommended_actions = actions
        assessment.is_ready_for_crawl = len(issues) == 0 or (
            len(issues) == 1 and layout_state and layout_state.layout_type == "single_column"
        )

        logger.info(f"[PageState] 评估完成: state={assessment.page_state.value}, "
                   f"issues={len(issues)}, actions={actions}")

        return assessment

    async def detect_popup(self) -> PopupDetectionResult:
        """
        检测页面上是否存在弹窗
        
        返回: PopupDetectionResult
        """
        try:
            # 先检测登录弹窗（优先级高）
            for selector in self.LOGIN_POPUP_SELECTORS:
                visible = await self._is_selector_visible(selector)
                if visible:
                    return PopupDetectionResult(
                        has_popup=True,
                        popup_type=PopupType.LOGIN_MODAL,
                        close_selectors=[selector],
                        severity="high",
                        description="检测到登录弹窗，需要关闭后才能继续操作"
                    )

            # JS深度检测登录弹窗
            login_detected = await self._detect_login_popup_js()
            if login_detected:
                return PopupDetectionResult(
                    has_popup=True,
                    popup_type=PopupType.LOGIN_MODAL,
                    severity="high",
                    description="JS探测到登录覆盖层"
                )

            # 检测推荐视频弹窗
            for selector in self.RECOMMENDED_VIDEO_SURFACE_SELECTORS:
                surface_visible = await self._is_selector_visible(selector)
                if surface_visible:
                    return PopupDetectionResult(
                        has_popup=True,
                        popup_type=PopupType.RECOMMENDED_VIDEO,
                        close_selectors=self.RECOMMENDED_VIDEO_CLOSE_SELECTORS,
                        severity="medium",
                        description="检测到推荐视频弹窗/播放页"
                    )

            return PopupDetectionResult(has_popup=False)

        except Exception as e:
            logger.warning(f"[PageState] 弹窗检测异常: {e}")
            return PopupDetectionResult(has_popup=False)

    async def evaluate_layout(self) -> Optional[LayoutState]:
        """
        评估搜索结果页面的布局模式
        
        返回: LayoutState 或 None（非搜索结果页时返回None）
        """
        try:
            snapshot = await self._get_layout_snapshot()
            if not snapshot:
                return None

            controls = snapshot.get("controls", [])
            card_count = snapshot.get("card_count", 0)
            rows = snapshot.get("rows", [])

            # 判断布局类型
            layout_type = self._classify_layout(snapshot)
            control_found = any(
                self._is_control_selected(c) for c in controls
            )

            row_dist = [r.get("count", 0) for r in rows]

            return LayoutState(
                layout_type=layout_type,
                control_found=control_found,
                card_count=card_count,
                row_distribution=row_dist,
                can_switch=layout_type != "multi_column"
            )

        except Exception as e:
            logger.warning(f"[PageState] 布局评估异常: {e}")
            return None

    async def detect_risk_control(self) -> bool:
        """检测是否出现风控/验证码页面"""
        try:
            body_text = await self._get_body_text(prefix=2000)
            if not body_text:
                return False

            for keyword in self.RISK_CONTROL_KEYWORDS:
                if keyword.lower() in body_text.lower():
                    logger.warning(f"[PageState] 检测到风控关键词: {keyword}")
                    return True
            return False
        except Exception:
            return False

    async def needs_fix_before_crawl(self) -> tuple[bool, List[str]]:
        """
        快速判断：爬取前是否需要先修复某些问题
        
        返回: (needs_fix, action_list)
        """
        assessment = await self.assess_full()
        return (not assessment.is_ready_for_crawl, assessment.recommended_actions)

    # ---- 内部方法 ----

    def _get_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return ""

    async def _is_selector_visible(self, selector: str, timeout: float = 0.6) -> bool:
        """检查选择器对应的元素是否可见"""
        try:
            locator = self.page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout * 1000)
            return True
        except Exception:
            return False

    async def _detect_login_popup_js(self) -> bool:
        """JS方式深度检测登录弹窗"""
        js_code = """
        () => {
            const roots = document.querySelectorAll(
                '[id^="login-full-panel-"], [class*="login"], [class*="Login"]'
            );
            for (const root of roots) {
                if (root.offsetParent === null) continue;
                const buttons = root.querySelectorAll('button, div, span');
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (/继续看视频|暂不登录|以后再说|稍后再说|关闭/.test(text)) {
                        return true;
                    }
                }
            }
            // 也检查全局可见的关闭类元素
            const closeBtns = document.querySelectorAll('[class*="login"] [class*="close"]');
            for (const btn of closeBtns) {
                if (btn.offsetParent !== null && (btn.offsetWidth > 10 || btn.offsetHeight > 10)) {
                    return true;
                }
            }
            return false;
        }
        """
        try:
            result = await self.page.evaluate(js_code)
            return bool(result)
        except Exception:
            return False

    async def _get_layout_snapshot(self) -> Optional[Dict]:
        """通过JS注入获取布局快照"""
        js_code = """
        () => {
            // 采集布局控制按钮
            const controls = [];
            const candidates = document.querySelectorAll(
                'button, [role="button"], [role="tab"], a, div, span'
            );
            const layoutTexts = /多列|双列|单列|筛选/;
            for (const el of candidates) {
                const text = (el.textContent || '').trim();
                if (!text || !layoutTexts.test(text)) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 16 || rect.height < 16) continue;
                if (rect.width > 600 || rect.height > 100) continue;
                if (el.offsetParent === null) continue;
                controls.push({
                    text: text.substring(0, 30),
                    aria_pressed: el.getAttribute('aria-pressed') || '',
                    aria_selected: el.getAttribute('aria-selected') || '',
                    class_name: (el.className || '').toString().substring(0, 200),
                    left: Math.round(rect.left),
                    top: Math.round(rect.top),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                });
                if (controls.length >= 20) break;
            }

            // 采集卡片分布
            const cards = [];
            const cardSelectors = [
                '[data-e2e="search-common-video"]', '[data-e2e="search-item"]',
                'li[data-e2e*="search"]', '[class*="search-result"] [href*="/video/"]',
                'a[href*="/video/"]'
            ];
            const seenContainers = new Set();
            for (const sel of cardSelectors) {
                document.querySelectorAll(sel).forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 120 || rect.height < 80) return;
                    let container = el.closest('[class*="card"], [class*="item"], li');
                    if (!container) container = el;
                    const key = container;
                    if (seenContainers.has(key)) return;
                    seenContainers.add(key);
                    cards.push({ x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) });
                });
                if (cards.length >= 24) break;
            }

            // 行分组
            const tolerance = 36;
            const rows = [];
            const sorted = [...cards].sort((a, b) => a.y - b.y || a.x - b.x);
            let currentRow = null;
            for (const c of sorted) {
                if (!currentRow || Math.abs(c.y - currentRow.y) > tolerance) {
                    currentRow = { y: c.y, count: 1, xs: [c.x] };
                    rows.push(currentRow);
                } else {
                    currentRow.count++;
                    currentRow.xs.push(c.x);
                }
            }

            return { controls, card_count: cards.length, rows: rows.slice(0, 6) };
        }
        """
        try:
            return await self.page.evaluate(js_code)
        except Exception as e:
            logger.debug(f"[PageState] 布局快照获取失败: {e}")
            return None

    def _classify_layout(self, snapshot: Dict) -> str:
        """根据快照判断布局类型"""
        controls = snapshot.get("controls", [])
        rows = snapshot.get("rows", [])
        card_count = snapshot.get("card_count", 0)

        # 策略1: 通过控件状态判断
        for ctrl in controls:
            if self._is_control_selected(ctrl):
                text = ctrl.get("text", "")
                if any(t in text for t in self.LAYOUT_MULTI_TEXTS):
                    return "multi_column"
                if any(t in text for t in self.LAYOUT_SINGLE_TEXTS):
                    return "single_column"

        # 策略2: 通过行分布判断
        for row in rows:
            if row.get("count", 0) >= 2:
                return "multi_column"

        # 策略3: 单列推断
        row_counts = [r.get("count", 0) for r in rows]
        if len(row_counts) >= 2 and max(row_counts) == 1 and card_count >= 2:
            return "single_column"

        return "unknown"

    @staticmethod
    def _is_control_selected(control: Dict) -> bool:
        """判断布局控件是否处于选中状态"""
        aria_pressed = control.get("aria_pressed", "")
        aria_selected = control.get("aria_selected", "")
        class_name = control.get("class_name", "")
        active_markers = ("active", "selected", "current", "checked")

        if aria_pressed == "true": return True
        if aria_selected == "true": return True
        if any(m in class_name for m in active_markers): return True
        return False

    async def _get_body_text(self, prefix: int = 2000) -> str:
        """获取页面body文本前N个字符"""
        js = f"() => {{ return (document.body?.innerText || '').substring(0, {prefix}); }}"
        try:
            return await self.page.evaluate(js)
        except Exception:
            return ""
