"""
评论回复 Mixin
从 crawler.py 拆分的评论回复相关方法
"""
import contextlib
import random
import re
import time
from typing import Callable, List, Optional

from loguru import logger

from src.douyin_bot.comment_api_interceptor import CommentAPIInterceptor
from src.douyin_bot.comment_reply_executor import execute_original_comment_reply_rounds
from src.douyin_bot.input_locator_service import InputLocatorContext, get_input_locator_service
from .crawler_selectors import COMMENT_SURFACE_SELECTORS


class CommentReplyMixin:

    # ------------------------------------------------------------------
    # 1. _classify_comment_submit_api_url
    # ------------------------------------------------------------------
    @staticmethod
    def _classify_comment_submit_api_url(url: str) -> str:
        normalized = (url or "").lower()
        if "aweme/v1/web/comment/list" in normalized:
            return ""
        for marker in (
            "aweme/v1/web/comment/publish",
            "aweme/v1/web/comment/create",
            "aweme/v1/web/comment/post",
            "/comment/publish/",
            "/comment/create/",
            "/comment/post/",
        ):
            if marker in normalized:
                return "submit"
        return ""

    # ------------------------------------------------------------------
    # 2. _wait_for_reply_progress
    # ------------------------------------------------------------------
    def _wait_for_reply_progress(
        self,
        previous_reply_api_request_count: int,
        previous_reply_comment_count: int,
        previous_pending_reply_batch_count: int = 0,
        previous_latest_reply_batch_timestamp: float = 0.0,
        timeout_seconds: float = 5.0,
    ) -> bool:
        deadline = time.time() + max(timeout_seconds, 1.0)
        while time.time() < deadline:
            time.sleep(0.1)
            if self._stop_event.is_set():
                return False
            if self._detect_risk_control_page():
                return True
            if self.comment_interceptor:
                queue_stats = self.comment_interceptor.get_queue_stats()
                if (
                    int(queue_stats.get("pending_reply", 0) or 0) > previous_pending_reply_batch_count
                    or float(queue_stats.get("latest_reply_timestamp", 0.0) or 0.0) > previous_latest_reply_batch_timestamp
                ):
                    return True
            current_reply_api = getattr(self, "_comment_reply_api_request_count", previous_reply_api_request_count)
            current_reply_total = getattr(self, "_comment_reply_total_count", previous_reply_comment_count)
            if current_reply_api > previous_reply_api_request_count or current_reply_total > previous_reply_comment_count:
                return True
        return False

    # ------------------------------------------------------------------
    # 3. _resolve_reply_expand_click_pause
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_reply_expand_click_pause(session: Optional[dict] = None) -> float:
        current_reply_buttons = max(int((session or {}).get("current_reply_button_count", 0) or 0), 0)
        no_progress_streak = max(int((session or {}).get("reply_expand_no_progress_streak", 0) or 0), 0)
        false_positive_count = max(int((session or {}).get("reply_expand_false_positive_count", 0) or 0), 0)

        low, high = 0.16, 0.34
        if current_reply_buttons >= 12:
            low += 0.06
            high += 0.18
        if no_progress_streak >= 2 or false_positive_count >= 1:
            low += 0.08
            high += 0.22
        return random.uniform(low, high)

    # ------------------------------------------------------------------
    # 4. _resolve_reply_collection_enabled
    # ------------------------------------------------------------------
    def _resolve_reply_collection_enabled(self, collect_reply_comments: Optional[bool] = None) -> bool:
        return False

    # ------------------------------------------------------------------
    # 5. _resolve_pending_reply_retry_wait
    # ------------------------------------------------------------------
    def _resolve_pending_reply_retry_wait(self, session: dict) -> float:
        if not self._collect_reply_comments_enabled():
            return 0.0
        if not isinstance(session, dict):
            return 0.0
        if not self._has_pending_reply_completion_risk(session):
            session["reply_cooldown_wait_rounds"] = 0
            return 0.0

        next_allowed = float(session.get("next_reply_expand_after", 0.0) or 0.0)
        if next_allowed <= 0:
            session["reply_cooldown_wait_rounds"] = 0
            return 0.0

        remaining = next_allowed - time.monotonic()
        if remaining <= 0:
            session["reply_cooldown_wait_rounds"] = 0
            return 0.0

        wait_rounds = max(int(session.get("reply_cooldown_wait_rounds", 0) or 0), 0)
        current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)
        false_positive_count = max(int(session.get("reply_expand_false_positive_count", 0) or 0), 0)

        if current_reply_buttons >= 40:
            max_wait_rounds = 5
        elif current_reply_buttons >= 12 or false_positive_count >= 2:
            max_wait_rounds = 4
        else:
            max_wait_rounds = 3
        if wait_rounds >= max_wait_rounds:
            return 0.0

        session["reply_cooldown_wait_rounds"] = wait_rounds + 1
        extra_buffer = 2.0 if current_reply_buttons >= 20 or false_positive_count >= 2 else 1.2
        max_single_wait = 9.0 if current_reply_buttons >= 20 else 6.0
        return min(max(remaining + extra_buffer, 1.5), max_single_wait)

    # ------------------------------------------------------------------
    # 6. _update_reply_surface_stats
    # ------------------------------------------------------------------
    @staticmethod
    def _update_reply_surface_stats(session: dict, surface_info: dict) -> None:
        if not isinstance(session, dict):
            return
        reply_button_count = 0
        if isinstance(surface_info, dict):
            try:
                reply_button_count = int(surface_info.get("reply_button_count", 0) or 0)
            except Exception:
                reply_button_count = 0
        session["current_reply_button_count"] = max(reply_button_count, 0)
        session["visible_reply_button_count"] = max(
            int(session.get("visible_reply_button_count", 0) or 0),
            max(reply_button_count, 0),
        )

    # ------------------------------------------------------------------
    # 7. _resolve_reply_expand_plan
    # ------------------------------------------------------------------
    def _resolve_reply_expand_plan(self, session: dict, *, scroll_count: int, idle_rounds: int) -> dict:
        if not self._collect_reply_comments_enabled():
            return {"should_expand": False}
        if not isinstance(session, dict):
            return {"should_expand": False}
        aggressive_mode = self._comment_aggressive_mode_enabled(session)

        now = time.monotonic()
        next_allowed = float(session.get("next_reply_expand_after", 0.0) or 0.0)
        if next_allowed and now < next_allowed:
            return {"should_expand": False}

        has_more = bool(session.get("has_more"))
        current_reply_buttons = int(session.get("current_reply_button_count", 0) or 0)
        seen_reply_buttons = int(session.get("visible_reply_button_count", 0) or 0)
        no_progress_streak = int(session.get("reply_expand_no_progress_streak", 0) or 0)
        reply_expand_clicks = int(session.get("reply_expand_clicks", 0) or 0)

        if not has_more and seen_reply_buttons <= 0 and reply_expand_clicks <= 0:
            return {"should_expand": False}
        if has_more and current_reply_buttons <= 0 and idle_rounds < 2 and (scroll_count % (12 if aggressive_mode else 8) != 0):
            return {"should_expand": False}

        should_expand = (
            (not has_more and (current_reply_buttons > 0 or seen_reply_buttons > 0))
            or idle_rounds >= (3 if aggressive_mode else 2)
            or scroll_count % (12 if aggressive_mode else 8) == 0
        )
        if not should_expand:
            return {"should_expand": False}
        if aggressive_mode and has_more and current_reply_buttons <= 0 and idle_rounds < 3:
            return {"should_expand": False}
        if not has_more and no_progress_streak >= 2 and idle_rounds < (5 if aggressive_mode else 4):
            return {"should_expand": False}

        max_clicks = 1
        if not aggressive_mode and not has_more:
            max_clicks = 2 if current_reply_buttons >= 2 else 1
        if no_progress_streak >= 2:
            max_clicks = 1

        feedback_timeout = 2.2 if aggressive_mode and not has_more else (2.8 if not has_more else 1.8)
        if no_progress_streak >= 2:
            feedback_timeout = 2.6 if aggressive_mode and not has_more else (3.2 if not has_more else 2.2)

        return {
            "should_expand": True,
            "max_clicks": max_clicks,
            "feedback_timeout": feedback_timeout,
            "tail_phase": not has_more,
        }

    # ------------------------------------------------------------------
    # 8. _mark_reply_expand_result
    # ------------------------------------------------------------------
    def _mark_reply_expand_result(
        self,
        session: dict,
        *,
        had_progress: bool,
        clicks: int,
        tail_phase: bool,
        signal: str = "",
        button_signature: str = "",
    ) -> None:
        if not isinstance(session, dict):
            return
        if clicks <= 0:
            return

        failed_signature_counts = session.get("reply_failed_signature_counts")
        if not isinstance(failed_signature_counts, dict):
            failed_signature_counts = {}
            session["reply_failed_signature_counts"] = failed_signature_counts

        if had_progress:
            session["reply_expand_no_progress_streak"] = 0
            session["reply_expand_success_count"] = int(session.get("reply_expand_success_count", 0) or 0) + 1
            if button_signature:
                failed_signature_counts.pop(button_signature, None)
            cooldown = random.uniform(0.9, 1.6) if tail_phase else random.uniform(2.0, 3.4)
        else:
            session["reply_expand_no_progress_streak"] = int(session.get("reply_expand_no_progress_streak", 0) or 0) + 1
            if signal == "button_disappeared":
                session["reply_expand_false_positive_count"] = int(
                    session.get("reply_expand_false_positive_count", 0) or 0
                ) + 1
            if button_signature:
                failed_signature_counts[button_signature] = int(failed_signature_counts.get(button_signature, 0) or 0) + 1
            cooldown = random.uniform(3.5, 5.2) if tail_phase else random.uniform(6.0, 9.5)

        session["reply_cooldown_wait_rounds"] = 0
        session["next_reply_expand_after"] = time.monotonic() + cooldown

    # ------------------------------------------------------------------
    # 9. _resolve_reply_finish_idle_limit
    # ------------------------------------------------------------------
    def _resolve_reply_finish_idle_limit(self, session: dict) -> int:
        if not self._collect_reply_comments_enabled():
            return 2
        seen_reply_buttons = int(session.get("visible_reply_button_count", 0) or 0)
        current_reply_buttons = int(session.get("current_reply_button_count", 0) or 0)
        no_progress_streak = int(session.get("reply_expand_no_progress_streak", 0) or 0)
        success_count = int(session.get("reply_expand_success_count", 0) or 0)
        false_positive_count = int(session.get("reply_expand_false_positive_count", 0) or 0)

        if seen_reply_buttons >= 120:
            base_limit = 10
        elif seen_reply_buttons >= 40:
            base_limit = 8
        elif current_reply_buttons > 0:
            base_limit = 6
        elif success_count > 0:
            base_limit = 5
        elif seen_reply_buttons > 0:
            base_limit = 4
        else:
            base_limit = 2

        if no_progress_streak >= 2 and seen_reply_buttons > 0:
            base_limit += 2
        if false_positive_count >= 2 and seen_reply_buttons > 0:
            base_limit += 2
        return min(base_limit, 12)

    # ------------------------------------------------------------------
    # 10. _click_one_reply_expand_button
    # ------------------------------------------------------------------
    def _click_one_reply_expand_button(self, blocked_signatures: Optional[List[str]] = None) -> dict:
        try:
            result = self.page.evaluate(
                """
                (payload) => {
                    const visible = (node) => !!(node && node.offsetParent !== null);
                    const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const findScrollableAncestor = (node) => {
                        let parent = node?.parentElement || null;
                        while (parent && parent !== document.body) {
                            const style = window.getComputedStyle(parent);
                            const overflowY = style?.overflowY || '';
                            if (
                                (overflowY.includes('auto') || overflowY.includes('scroll')) &&
                                parent.scrollHeight > parent.clientHeight + 20
                            ) {
                                return parent;
                            }
                            parent = parent.parentElement;
                        }
                        return null;
                    };
                    const surfaceSelectors = payload.surfaceSelectors || [];
                    const blockedSignatures = new Set(Array.isArray(payload.blockedSignatures) ? payload.blockedSignatures : []);
                    const pattern = /^(?:共\\s*\\d+\\s*条回复|(?:展开|查看|全部|更多)\\s*.{0,12}?回复)$/i;

                    let surface = null;
                    for (const selector of surfaceSelectors) {
                        const node = document.querySelector(selector);
                        if (visible(node)) {
                            surface = node;
                            break;
                        }
                    }

                    const root = surface || document.body;
                    if (!root) return '';

                    const nodes = Array.from(root.querySelectorAll('button, a, [role="button"]'));
                    const candidates = [];
                    for (const node of nodes) {
                        if (!visible(node)) continue;
                        const text = normalizeText(node.innerText || node.textContent || '');
                        if (!text || !pattern.test(text)) continue;
                        if (text.length > 24) continue;
                        if ((node.innerText || '').split('\\n').length > 2) continue;
                        const attemptCount = Number((node.dataset && node.dataset.replyExpandAttempts) || '0') || 0;
                        if (attemptCount >= 2) continue;

                        const rect = node.getBoundingClientRect();
                        const area = Math.max(rect.width, 0) * Math.max(rect.height, 0);
                        if (rect.width <= 8 || rect.height <= 8) continue;
                        if (rect.height > 40 || area > 32000) continue;

                        const scrollableAncestor = findScrollableAncestor(node);
                        const hostRect = scrollableAncestor
                            ? scrollableAncestor.getBoundingClientRect()
                            : (surface ? surface.getBoundingClientRect() : { top: 0, bottom: window.innerHeight, height: window.innerHeight });
                        const visibleMargin = Math.max(Math.min((hostRect.height || window.innerHeight) * 0.35, 320), 140);
                        const visibleTop = Number(hostRect.top || 0) - visibleMargin;
                        const visibleBottom = Number(hostRect.bottom || window.innerHeight) + visibleMargin;
                        if (rect.bottom < visibleTop || rect.top > visibleBottom) continue;

                        const className = String(node.className || '');
                        const signature = `${text.slice(0, 40)}|${className.slice(0, 40)}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                        if (blockedSignatures.has(signature)) continue;
                        const centerY = rect.top + rect.height / 2;
                        const preferredCenterY = Number(hostRect.top || 0) + Math.max((hostRect.height || window.innerHeight) * 0.58, 120);
                        const centerDistance = Math.abs(centerY - preferredCenterY);
                        const score =
                            (className.includes('comment-reply-expand-btn') ? 100 : 0) +
                            (/reply|expand/i.test(className) ? 30 : 0) +
                            (/^展开\\d+条回复$/.test(text) ? 20 : 0) +
                            (/^展开/.test(text) ? 10 : 0) -
                            Math.min(text.length, 24);

                        candidates.push({
                            text,
                            clickable: node,
                            area,
                            width: Math.round(rect.width || 0),
                            score,
                            className,
                            attemptCount,
                            signature,
                            centerDistance,
                            scrollableAncestor,
                            hostRect,
                        });
                    }

                    candidates.sort((a, b) => {
                        if (a.score !== b.score) return b.score - a.score;
                        if (a.centerDistance !== b.centerDistance) return a.centerDistance - b.centerDistance;
                        if (a.width !== b.width) return a.width - b.width;
                        return a.area - b.area;
                    });

                    for (const item of candidates) {
                        const clickable = item.clickable;
                        const rectBefore = clickable.getBoundingClientRect();
                        const currentHostRect = item.hostRect || { top: 0, bottom: window.innerHeight, height: window.innerHeight };
                        const shouldCenterScroll =
                            rectBefore.top < Number(currentHostRect.top || 0) + 24 ||
                            rectBefore.bottom > Number(currentHostRect.bottom || window.innerHeight) - 24;
                        if (shouldCenterScroll && clickable.scrollIntoView) {
                            clickable.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
                        }

                        const scrollableAncestor = item.scrollableAncestor || findScrollableAncestor(clickable);

                        if (scrollableAncestor) {
                            const hostRect = scrollableAncestor.getBoundingClientRect();
                            const buttonRect = clickable.getBoundingClientRect();
                            const delta = (buttonRect.top - hostRect.top) - Math.max(hostRect.height * 0.35, 80);
                            scrollableAncestor.scrollTop += delta;
                        }

                        const triggerMouseSequence = (el) => {
                            const rect = el.getBoundingClientRect();
                            const clientX = rect.left + Math.max(rect.width / 2, 2);
                            const clientY = rect.top + Math.max(rect.height / 2, 2);
                            const eventTypes = [
                                'pointerover',
                                'mouseover',
                                'mouseenter',
                                'pointerdown',
                                'mousedown',
                                'pointerup',
                                'mouseup',
                                'click',
                            ];
                            for (const type of eventTypes) {
                                try {
                                    const EventCtor = type.startsWith('pointer') ? PointerEvent : MouseEvent;
                                    el.dispatchEvent(new EventCtor(type, {
                                        bubbles: true,
                                        cancelable: true,
                                        composed: true,
                                        view: window,
                                        clientX,
                                        clientY,
                                    }));
                                } catch (e) {}
                            }
                        };

                        if (clickable.dataset) {
                            clickable.dataset.replyExpandAttempts = String((item.attemptCount || 0) + 1);
                        }

                        try { clickable.focus?.(); } catch (e) {}
                        try { triggerMouseSequence(clickable); } catch (e) {}
                        try { clickable.click?.(); } catch (e) {}

                        const rectAfter = clickable.getBoundingClientRect();
                        return {
                            label: item.text.slice(0, 60),
                            class_name: item.className.slice(0, 80),
                            attempts: (item.attemptCount || 0) + 1,
                            signature: item.signature,
                            position_before: `${Math.round(rectBefore.x)},${Math.round(rectBefore.y)}`,
                            position_after: `${Math.round(rectAfter.x)},${Math.round(rectAfter.y)}`,
                        };
                    }
                    return null;
                }
                """,
                {
                    "surfaceSelectors": COMMENT_SURFACE_SELECTORS,
                    "blockedSignatures": blocked_signatures or [],
                }
            )
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.debug(f"点击展开回复按钮失败: {e}")
            return {}

    # ------------------------------------------------------------------
    # 11. _reply_button_signature_exists
    # ------------------------------------------------------------------
    def _reply_button_signature_exists(self, button_signature: str) -> bool:
        if not button_signature:
            return False
        try:
            return bool(
                self.page.evaluate(
                    """
                    (signature) => {
                        const normalizeText = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                        for (const node of nodes) {
                            if (!node || node.offsetParent === null) continue;
                            const rect = node.getBoundingClientRect();
                            const currentSignature = `${normalizeText(node.innerText || node.textContent || '').slice(0, 40)}|${String(node.className || '').slice(0, 40)}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                            if (currentSignature === signature) {
                                return true;
                            }
                        }
                        return false;
                    }
                    """,
                    button_signature,
                )
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 12. _wait_for_reply_feedback
    # ------------------------------------------------------------------
    def _wait_for_reply_feedback(
        self,
        previous_reply_api_request_count: int,
        previous_reply_comment_count: int,
        button_signature: str = "",
        previous_pending_reply_batch_count: int = 0,
        previous_latest_reply_batch_timestamp: float = 0.0,
        timeout_seconds: float = 5.0,
        progress_callback: Optional[Callable[[dict], None]] = None,
        session: Optional[dict] = None,
        aweme_id: str = "",
        extra_detail: str = "",
    ) -> dict:
        deadline = time.time() + max(timeout_seconds, 1.0)
        while time.time() < deadline:
            self._report_comment_crawl_progress(
                progress_callback,
                session=session or {},
                aweme_id=aweme_id,
                extra_detail=extra_detail or "等待回复展开后的接口反馈",
            )
            time.sleep(0.1)
            if self._stop_event.is_set():
                return {"progressed": False, "signal": "stopped"}
            if self._detect_risk_control_page():
                return {"progressed": True, "signal": "risk_control"}
            if self.comment_interceptor:
                queue_stats = self.comment_interceptor.get_queue_stats()
                if (
                    int(queue_stats.get("pending_reply", 0) or 0) > previous_pending_reply_batch_count
                    or float(queue_stats.get("latest_reply_timestamp", 0.0) or 0.0) > previous_latest_reply_batch_timestamp
                ):
                    return {"progressed": True, "signal": "reply_queue"}
            current_reply_api = getattr(self, "_comment_reply_api_request_count", previous_reply_api_request_count)
            current_reply_total = getattr(self, "_comment_reply_total_count", previous_reply_comment_count)
            if current_reply_api > previous_reply_api_request_count or current_reply_total > previous_reply_comment_count:
                return {"progressed": True, "signal": "reply_api"}
            if button_signature and not self._reply_button_signature_exists(button_signature):
                return {"progressed": False, "signal": "button_disappeared"}
        return {"progressed": False, "signal": "timeout"}

    # ------------------------------------------------------------------
    # 13. _expand_reply_threads
    # ------------------------------------------------------------------
    def _expand_reply_threads(self, session: Optional[dict] = None, max_clicks: int = 2) -> int:
        clicked = 0
        self._last_reply_click_signature = ""
        blocked_signatures: List[str] = []
        if isinstance(session, dict):
            blocked_map = session.get("reply_failed_signature_counts") or {}
            if isinstance(blocked_map, dict):
                blocked_signatures = [
                    str(signature)
                    for signature, count in blocked_map.items()
                    if str(signature or "") and int(count or 0) >= 1
                ]
        for _ in range(max(0, max_clicks)):
            if self.is_stopped():
                break
            click_info = self._click_one_reply_expand_button(blocked_signatures=blocked_signatures)
            if not click_info:
                break
            clicked += 1
            self._last_reply_click_signature = str(click_info.get("signature", "") or "")
            logger.info(
                f"展开回复线程: {click_info.get('label', '')} | "
                f"class={click_info.get('class_name', '')} | "
                f"attempts={click_info.get('attempts', 0)} | "
                f"pos={click_info.get('position_before', '')}->{click_info.get('position_after', '')}"
            )
            if not self._interruptible_sleep(self._resolve_reply_expand_click_pause(session), chunk_seconds=0.12):
                break
        return clicked

    # ------------------------------------------------------------------
    # 14. _expand_reply_threads_for_target
    # ------------------------------------------------------------------
    def _expand_reply_threads_for_target(
        self,
        target: dict,
        *,
        parent_marker_uid: str = "",
        max_clicks: int = 2,
    ) -> int:
        target = self._normalize_reply_target(target)
        parent_comment_id = str((target or {}).get("parent_comment_id", "") or "").strip()
        root_comment_id = str((target or {}).get("root_comment_id", "") or "").strip()
        parent_anchor = (target or {}).get("parent_anchor") if isinstance((target or {}).get("parent_anchor"), dict) else {}
        parent_text = str((parent_anchor or {}).get("comment_text", "") or "").strip()
        parent_nickname = str((parent_anchor or {}).get("nickname", "") or "").strip()

        if not (parent_marker_uid or parent_comment_id or root_comment_id or parent_text or parent_nickname):
            return 0

        clicked = 0
        self._last_reply_click_signature = ""
        for _ in range(max(0, max_clicks)):
            try:
                click_info = self.page.evaluate(
                    """(payload) => {
                        const visible = (node) => !!(node && node.offsetParent !== null);
                        const normalize = (value) => String(value || '')
                            .replace(/[\\u200b\\ufeff]/g, '')
                            .replace(/\\s+/g, ' ')
                            .trim()
                            .toLowerCase();
                        const textOf = (node) => String(node?.innerText || node?.textContent || '');
                        const pattern = /^(?:共\\s*\\d+\\s*条回复|(?:展开|查看|全部|更多)\\s*.{0,12}?回复)$/i;
                        const parentMarker = payload.parentMarkerUid
                            ? document.querySelector(`[data-trae-reply-parent="${payload.parentMarkerUid}"]`)
                            : null;
                        const targetParentCommentId = normalize(payload.parentCommentId);
                        const targetRootCommentId = normalize(payload.rootCommentId);
                        const parentText = normalize(payload.parentText);
                        const parentNickname = normalize(payload.parentNickname);
                        const findParentNode = () => {
                            if (parentMarker && visible(parentMarker)) return parentMarker;
                            const selectors = [
                                "[data-trae-reply-item]",
                                "[class*='comment-item']",
                                "[class*='CommentItem']",
                                "[data-e2e='comment-item']",
                                "li",
                            ];
                            const seen = new Set();
                            const candidates = [];
                            for (const selector of selectors) {
                                for (const node of Array.from(document.querySelectorAll(selector))) {
                                    if (!node || seen.has(node) || !visible(node)) continue;
                                    seen.add(node);
                                    const text = normalize(textOf(node));
                                    const attrBag = normalize([
                                        node.id || '',
                                        node.className || '',
                                        node.getAttribute?.('data-e2e') || '',
                                        node.getAttribute?.('data-comment-id') || '',
                                        node.getAttribute?.('data-cid') || '',
                                        node.getAttribute?.('data-id') || '',
                                    ].join(' '));
                                    let score = 0;
                                    if (targetParentCommentId && attrBag.includes(targetParentCommentId)) score += 20;
                                    if (targetRootCommentId && attrBag.includes(targetRootCommentId)) score += 14;
                                    if (parentNickname && text.includes(parentNickname)) score += 6;
                                    if (parentText && text.includes(parentText.slice(0, Math.min(parentText.length, 18)))) score += 8;
                                    if (score > 0) {
                                        candidates.push({ node, score });
                                    }
                                }
                            }
                            candidates.sort((a, b) => b.score - a.score);
                            return candidates[0]?.node || null;
                        };
                        const parentNode = findParentNode();
                        if (!parentNode) {
                            return null;
                        }
                        const candidateButtons = Array.from(
                            parentNode.querySelectorAll('button, a, [role="button"], span, div')
                        ).filter((node) => {
                            if (!visible(node)) return false;
                            const text = normalize(textOf(node));
                            if (!text || !pattern.test(text)) return false;
                            const rect = node.getBoundingClientRect();
                            return rect.width > 8 && rect.height > 8;
                        });
                        const button = candidateButtons[0] || null;
                        if (!button) {
                            return null;
                        }
                        const rect = button.getBoundingClientRect();
                        const signature = `${normalize(textOf(button)).slice(0, 40)}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
                        try {
                            parentNode.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
                        } catch (e) {}
                        try { button.focus?.(); } catch (e) {}
                        try { button.click?.(); } catch (e) {}
                        return {
                            label: textOf(button).slice(0, 60),
                            signature,
                            parentText: textOf(parentNode).slice(0, 80),
                        };
                    }""",
                    {
                        "parentMarkerUid": str(parent_marker_uid or ""),
                        "parentCommentId": parent_comment_id,
                        "rootCommentId": root_comment_id,
                        "parentText": parent_text,
                        "parentNickname": parent_nickname,
                    },
                )
            except Exception as e:
                logger.debug(f"按目标父评论展开回复失败: {e}")
                click_info = None
            if not click_info:
                break
            clicked += 1
            self._last_reply_click_signature = str(click_info.get("signature", "") or "")
            logger.info(
                "按目标父评论展开回复线程: "
                f"label={click_info.get('label', '')} | "
                f"parent={click_info.get('parentText', '')}"
            )
            time.sleep(random.uniform(0.8, 1.3))
        return clicked

    # ------------------------------------------------------------------
    # 15. _mark_comment_composer_targets
    # ------------------------------------------------------------------
    def _mark_comment_composer_targets(self, editor_locator) -> dict:
        try:
            return editor_locator.evaluate(
                """(el) => {
                    const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 10) return false;
                        const style = window.getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden' && style.pointerEvents !== 'none';
                    };

                    const cleanAttrs = () => {
                        document.querySelectorAll('[data-trae-comment-container], [data-trae-comment-send]').forEach((node) => {
                            node.removeAttribute('data-trae-comment-container');
                            node.removeAttribute('data-trae-comment-send');
                        });
                    };

                    cleanAttrs();

                    const container =
                        el.closest('#comment-input-container, [data-e2e="comment-input"], .comment-input-inner-container, .comment-input-inner, .comment-input-area, [class*="comment-input"], [class*="commentInput"], [class*="CommentInput"]')
                        || el.parentElement;
                    if (!container) return { ok: false, reason: 'container_not_found' };

                    const uid = `trae-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                    container.setAttribute('data-trae-comment-container', uid);

                    const textOf = (node) => String(node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const editorRect = el.getBoundingClientRect();
                    const editorCenterY = editorRect.top + (editorRect.height / 2);
                    const candidates = Array.from(container.querySelectorAll('button, [role="button"], div, span, i, svg, path'));
                    const clickableAncestor = (node) => (
                        node?.closest?.('button, [role="button"], a, [tabindex="0"]')
                        || node?.parentElement
                        || null
                    );
                    const colorScore = (node) => {
                        const style = window.getComputedStyle(node);
                        const colorBag = `${style.color || ''} ${style.fill || ''} ${style.backgroundColor || ''}`.toLowerCase();
                        if (/fe2c55|ff0000|rgb\\(254, 44, 85\\)|rgb\\(255, 0, 0\\)/.test(colorBag)) return 6;
                        if (/rgb\\(255,/.test(colorBag)) return 2;
                        return 0;
                    };
                    const isWrapperLike = (node) => {
                        const cls = String(node?.className || '').toLowerCase();
                        if (!cls) return false;
                        return (
                            /commentinput-right-ct/.test(cls)
                            || (/container|wrapper|wrap/.test(cls) && !/send|submit|arrow|icon/.test(cls))
                        );
                    };
                    const hasDescendantSendSignal = (node) => {
                        if (!node || !node.querySelectorAll) return false;
                        return Array.from(node.querySelectorAll('button, [role="button"], svg, path, span, i')).some((child) => {
                            if (!visible(child)) return false;
                            const text = textOf(child);
                            const aria = String(child.getAttribute?.('aria-label') || '');
                            const cls = String(child.className || '').toLowerCase();
                            return (
                                /发送|发布/.test(text)
                                || /发送|发布/.test(aria)
                                || /send|submit|arrow|icon/.test(cls)
                                || colorScore(child) >= 6
                            );
                        });
                    };
                    const scoreSendCandidate = (node) => {
                        if (!node || !visible(node) || node === el || node.contains?.(el)) return -999;
                        const rect = node.getBoundingClientRect();
                        const centerY = rect.top + (rect.height / 2);
                        const rightAligned = rect.left >= editorRect.right - 18;
                        const nearEditor = Math.abs(centerY - editorCenterY) <= Math.max(96, editorRect.height * 2.8);
                        const compact = rect.width <= 120 && rect.height <= 72;
                        if (!rightAligned && !nearEditor) return -999;
                        const text = textOf(node);
                        const aria = String(node.getAttribute?.('aria-label') || '');
                        const cls = String(node.className || '');
                        const redSignal = colorScore(node);
                        const semanticSignal =
                            /发送|发布/.test(text)
                            || /发送|发布/.test(aria)
                            || /send|submit|arrow|icon/.test(cls.toLowerCase());
                        if (isWrapperLike(node) && hasDescendantSendSignal(node)) return -999;
                        if (!semanticSignal && redSignal < 6 && rect.width > 64) return -999;
                        let score = 0;
                        if (/发送|发布/.test(text)) score += 12;
                        if (/发送|发布/.test(aria)) score += 10;
                        if (/send|submit|arrow|icon/.test(cls.toLowerCase())) score += 4;
                        if (rightAligned) score += 4;
                        if (nearEditor) score += 3;
                        if (compact) score += 2;
                        if (rect.width <= 52) score += 2;
                        score += redSignal;
                        return score;
                    };

                    let sendButton = container.querySelector('.PAK9ytjW .kMBnEFed, .commentInput-right-ct .kMBnEFed');
                    const scored = [];
                    for (const node of candidates) {
                        const target = clickableAncestor(node) || node;
                        const score = scoreSendCandidate(target);
                        if (score > 0) {
                            scored.push({ node: target, score });
                        }
                    }
                    scored.sort((a, b) => b.score - a.score);
                    if (!sendButton && scored.length) {
                        sendButton = scored[0].node;
                    }

                    if (sendButton) {
                        sendButton.setAttribute('data-trae-comment-send', uid);
                    }

                    return {
                        ok: true,
                        uid,
                        has_send_button: !!sendButton,
                        send_score: scored[0]?.score || 0,
                        send_text: textOf(sendButton),
                        send_class: String(sendButton?.className || ''),
                        send_tag: String(sendButton?.tagName || '').toLowerCase(),
                    };
                }"""
            )
        except Exception as e:
            logger.debug(f"标记评论输入框/发送按钮失败: {e}")
            return {"ok": False, "reason": str(e)}

    # ------------------------------------------------------------------
    # 16. _find_comment_editor_from_exact_dom
    # ------------------------------------------------------------------
    def _find_comment_editor_from_exact_dom(
        self,
        detail_structure: Optional[dict] = None,
        activation: Optional[dict] = None,
    ):
        """按已确认的抖音评论框 DOM 精准查找输入框。"""
        locator_result = get_input_locator_service().locate(
            self.page,
            InputLocatorContext(
                scene="comment_reply",
                activation=activation or {},
                allow_document_fallback=False,
            ),
        )
        if locator_result.success and locator_result.input_candidate is not None:
            return locator_result.input_candidate.locator
        detail_kind = self._resolve_comment_detail_kind(detail_structure)
        activation = activation if isinstance(activation, dict) else {}
        activation_container_uid = str((activation or {}).get("containerUid", "") or "").strip()
        activation_editor_uid = str((activation or {}).get("editorUid", "") or "").strip()
        container_selectors = []
        if activation_container_uid:
            container_selectors.append(f'[data-trae-reply-active-container="{activation_container_uid}"]')
        container_selectors.extend(self._get_comment_composer_container_selectors(detail_kind))
        container_selectors = self._dedupe_selector_list(container_selectors)
        placeholder_selectors = [
            ".public-DraftEditorPlaceholder-inner",
            ".public-DraftEditorPlaceholder-root .public-DraftEditorPlaceholder-inner",
            "textarea[placeholder*='评论']",
            "textarea[placeholder*='写']",
        ]
        editor_selectors = []
        if activation_editor_uid:
            editor_selectors.append(f'[data-trae-reply-active-editor="{activation_editor_uid}"]')
        editor_selectors.extend([
            ".DraftEditor-editorContainer .public-DraftEditor-content[contenteditable='true']",
            ".notranslate.public-DraftEditor-content[contenteditable='true']",
            ".public-DraftEditor-content[contenteditable='true']",
            "[contenteditable='true'][role='combobox']",
            "[contenteditable='true']",
            "[contenteditable='plaintext-only']",
            "textarea",
            "[role='textbox']",
        ])
        editor_selectors = self._dedupe_selector_list(editor_selectors)
        for container_selector in container_selectors:
            try:
                container = self.page.locator(container_selector).first
                if not container.is_visible(timeout=1500):
                    continue
                container_meta = container.evaluate(
                    """el => ({
                        text: String(el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
                        html: String(el.outerHTML || '').slice(0, 1000),
                    })"""
                )
                logger.info(f"命中评论容器: selector={container_selector} meta={container_meta}")

                # 先尝试点击 placeholder/容器，激活 DraftEditor
                for placeholder_selector in placeholder_selectors:
                    with contextlib.suppress(Exception):
                        placeholder = container.locator(placeholder_selector).first
                        if placeholder.count() > 0:
                            helper = self._human_helper()
                            if helper.click_target(placeholder, pre_hover=True):
                                logger.info(f"通过 placeholder 激活评论框: selector={placeholder_selector}")
                                time.sleep(0.4)
                                break
                            placeholder.click(timeout=1500, force=True)
                            logger.info(f"通过强制点击 placeholder 激活评论框: selector={placeholder_selector}")
                            time.sleep(0.4)
                            break

                for editor_selector in editor_selectors:
                    try:
                        locator = container.locator(editor_selector).first
                        if locator.count() <= 0:
                            continue
                        with contextlib.suppress(Exception):
                            locator.evaluate(
                                """el => {
                                    try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                                    try { el.focus(); } catch (e) {}
                                }"""
                            )
                        visible = False
                        with contextlib.suppress(Exception):
                            visible = locator.is_visible(timeout=800)
                        placeholder_html = locator.evaluate(
                            """el => {
                                const container = el.closest('#comment-input-container') || el.parentElement;
                                return String(container?.innerHTML || '').slice(0, 1200);
                            }"""
                        )
                        if "发一条弹幕吧" in placeholder_html:
                            continue
                        logger.info(
                            f"通过精准 DOM 命中评论输入框: container={container_selector} "
                            f"editor={editor_selector} visible={visible}"
                        )
                        return locator
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"精准评论容器检查失败({container_selector}): {e}")
                continue
        return None

    # ------------------------------------------------------------------
    # 17. _find_exact_comment_send_button
    # ------------------------------------------------------------------
    def _find_exact_comment_send_button(self, editor_locator=None, activation: Optional[dict] = None):
        def _try_marked_button():
            marked_uid = ""
            if editor_locator is not None:
                marker_result = self._mark_comment_composer_targets(editor_locator)
                marked_uid = str((marker_result or {}).get("uid", "") or "").strip()
                if marked_uid:
                    try:
                        marked = self.page.locator(f'[data-trae-comment-send="{marked_uid}"]').first
                        if marked.is_visible(timeout=1200):
                            logger.info(
                                "通过标记容器命中评论发送按钮: "
                                f"meta={marker_result}"
                            )
                            return marked
                    except Exception as e:
                        logger.debug(f"通过标记容器定位评论发送按钮失败: {e}")
            return None

        # Prefer the button re-marked from the actual active editor. That path
        # normalizes to a clickable ancestor and is more reliable than an early
        # activation probe that may have tagged an inner svg/path icon.
        marked = _try_marked_button()
        if marked:
            return marked

        activation = activation if isinstance(activation, dict) else {}
        activation_send_uid = str((activation or {}).get("sendUid", "") or "").strip()
        activation_container_uid = str((activation or {}).get("containerUid", "") or "").strip()
        if activation_send_uid:
            try:
                active_send = self.page.locator(f'[data-trae-reply-active-send="{activation_send_uid}"]').first
                if active_send.is_visible(timeout=1200):
                    logger.info("通过回复态探测器命中评论发送按钮")
                    return active_send
            except Exception as e:
                logger.debug(f"通过回复态探测器定位评论发送按钮失败: {e}")

        if activation_container_uid:
            try:
                active_container = self.page.locator(
                    f'[data-trae-reply-active-container="{activation_container_uid}"]'
                ).first
                if active_container.is_visible(timeout=1200):
                    active_scoped = active_container.locator(
                        '[data-trae-reply-active-send], [data-trae-comment-send], '
                        'button:has-text("发送"), [role="button"]:has-text("发送"), '
                        'button:has-text("发布"), [role="button"]:has-text("发布")'
                    ).first
                    if active_scoped.is_visible(timeout=900):
                        logger.info("通过激活回复容器命中评论发送按钮")
                        return active_scoped
            except Exception as e:
                logger.debug(f"通过激活回复容器定位评论发送按钮失败: {e}")

        strict_video_selectors = [
            '#comment-input-container .commentInput-right-ct button',
            '#comment-input-container .commentInput-right-ct [role="button"]',
            '#comment-input-container .PAK9ytjW .kMBnEFed button',
            '#comment-input-container .PAK9ytjW .kMBnEFed [role="button"]',
            '#comment-input-container .PAK9ytjW .kMBnEFed',
            '#comment-input-container .commentInput-right-ct .kMBnEFed',
        ]

        for selector in strict_video_selectors:
            try:
                strict_candidate = self.page.locator(selector).first
                if strict_candidate.is_visible(timeout=1000):
                    logger.info(f"通过视频结构精确 selector 命中评论发送按钮: selector={selector}")
                    return strict_candidate
            except Exception as e:
                logger.debug(f"视频结构评论发送按钮精确 selector 命中失败({selector}): {e}")

        layered_selectors = [
            '[data-trae-reply-active-send]',
            '[data-trae-comment-send]',
            '#comment-input-container [role="button"]:has-text("发送")',
            '#comment-input-container button:has-text("发送")',
            '#comment-input-container [role="button"]:has-text("发布")',
            '#comment-input-container button:has-text("发布")',
            '[data-e2e="comment-input"] [role="button"]:has-text("发送")',
            '[data-e2e="comment-input"] button:has-text("发送")',
            '[data-e2e="comment-input"] [role="button"]:has-text("发布")',
            '[data-e2e="comment-input"] button:has-text("发布")',
        ]
        for selector in layered_selectors:
            try:
                candidate = self.page.locator(selector).first
                if candidate.is_visible(timeout=1200):
                    logger.info(f"通过分层 selector 命中评论发送按钮: selector={selector}")
                    return candidate
            except Exception as e:
                logger.debug(f"评论发送按钮 selector 命中失败({selector}): {e}")

        try:
            fallback = self.page.locator(
                '#comment-input-container button, '
                '#comment-input-container [role="button"], '
                '[data-e2e="comment-input"] button, '
                '[data-e2e="comment-input"] [role="button"]'
            ).filter(has_text=re.compile(r"发送|发布|回复")).first
            if fallback.is_visible(timeout=1200):
                logger.info("通过按钮文案兜底命中评论发送按钮")
                return fallback
        except Exception as e:
            logger.debug(f"评论发送按钮文案兜底失败: {e}")

        if editor_locator is not None:
            for _ in range(2):
                time.sleep(0.2)
                marked = _try_marked_button()
                if marked:
                    return marked

        locator_result = get_input_locator_service().locate(
            self.page,
            InputLocatorContext(
                scene="comment_reply",
                activation=activation or {},
                allow_document_fallback=False,
            ),
        )
        if locator_result.success and locator_result.send_candidate is not None:
            return locator_result.send_candidate.locator

        logger.warning("未通过分层 DOM 命中评论发送按钮")
        return None

    # ------------------------------------------------------------------
    # 18. _attempt_reply_stage_recovery
    # ------------------------------------------------------------------
    def _attempt_reply_stage_recovery(
        self,
        target: dict,
        detail_structure: Optional[dict] = None,
        surface_selector: str = "",
        stage: str = "",
        activation: Optional[dict] = None,
    ) -> dict:
        target = self._normalize_reply_target(target)
        detail_structure = detail_structure if isinstance(detail_structure, dict) else {}
        activation = activation if isinstance(activation, dict) else {}
        result = {
            "recovered": False,
            "detail_structure": detail_structure,
            "surface_selector": str(surface_selector or ""),
            "activation": activation,
        }
        with contextlib.suppress(Exception):
            self._dismiss_login_popup_if_present()
        with contextlib.suppress(Exception):
            self._dismiss_recommended_video_if_present(max_rounds=1)
        if stage in {"reply_button_not_effective", "reply_editor_missing", "reply_context_missing"}:
            refreshed_detail = self._inspect_detail_page_structure()
            refreshed_surface = self._ensure_comment_surface_ready(detail_structure=refreshed_detail)
            refreshed_selector = str((refreshed_surface or {}).get("surface_selector", "") or surface_selector)
            with contextlib.suppress(Exception):
                self._expand_reply_threads({}, max_clicks=2, feedback_timeout=2.0)
                time.sleep(0.4)
            result.update(
                {
                    "recovered": True,
                    "detail_structure": refreshed_detail,
                    "surface_selector": refreshed_selector,
                    "activation": {},
                }
            )
            return result
        if stage in {"send_button_missing", "submit_verify_failed"}:
            if activation:
                result["activation"] = activation
            result["recovered"] = True
            return result
        return result

    # ------------------------------------------------------------------
    # 19. _enrich_reply_target_with_live_comment_anchor
    # ------------------------------------------------------------------
    def _enrich_reply_target_with_live_comment_anchor(
        self,
        target: dict,
        *,
        aweme_id: str = "",
        bootstrap_timeout_seconds: float = 0.0,
    ) -> dict:
        normalized_target = self._normalize_reply_target(target)
        if bootstrap_timeout_seconds > 0:
            with contextlib.suppress(Exception):
                self._wait_for_comment_bootstrap(timeout_seconds=bootstrap_timeout_seconds)
        live_index = self._build_live_comment_anchor_index(
            aweme_id=aweme_id or str(normalized_target.get("aweme_id", "") or "").strip()
        )
        if not live_index:
            normalized_target["_live_anchor_index_size"] = 0
            normalized_target["_live_anchor_matched"] = False
            normalized_target["_live_parent_anchor_matched"] = False
            return normalized_target

        comment_id = str(normalized_target.get("comment_id", "") or "").strip()
        parent_comment_id = str(normalized_target.get("parent_comment_id", "") or "").strip()
        live_anchor = live_index.get(comment_id, {})
        live_parent_anchor = live_index.get(parent_comment_id, {}) if parent_comment_id else {}

        merged_target = self._merge_live_anchor_fields(normalized_target, live_anchor)
        if live_parent_anchor:
            existing_parent_anchor = (
                merged_target.get("parent_anchor") if isinstance(merged_target.get("parent_anchor"), dict) else {}
            )
            merged_target["parent_anchor"] = self._merge_live_anchor_fields(existing_parent_anchor, live_parent_anchor)
        merged_target["_live_anchor_index_size"] = len(live_index)
        merged_target["_live_anchor_matched"] = bool(live_anchor)
        merged_target["_live_parent_anchor_matched"] = bool(live_parent_anchor)
        return self._normalize_reply_target(merged_target)

    # ------------------------------------------------------------------
    # 20. _cleanup_comment_composer_marks
    # ------------------------------------------------------------------
    def _cleanup_comment_composer_marks(self):
        try:
            self.page.evaluate(
                """() => {
                    document.querySelectorAll('[data-trae-comment-container], [data-trae-comment-send]').forEach((node) => {
                        node.removeAttribute('data-trae-comment-container');
                        node.removeAttribute('data-trae-comment-send');
                    });
                }"""
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 21. _normalize_editor_text
    # ------------------------------------------------------------------
    def _normalize_editor_text(self, text: str) -> str:
        normalized = str(text or "")
        normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        return normalized.strip()

    # ------------------------------------------------------------------
    # 22. _read_editor_text
    # ------------------------------------------------------------------
    def _read_editor_text(self, locator) -> str:
        try:
            raw = locator.evaluate(
                """el => {
                    if (!el) return '';
                    const target =
                        (el.matches && el.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]') ? el : null)
                        || el.querySelector?.('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')
                        || el;
                    if (target && target.isContentEditable) {
                        return String(target.innerText || target.textContent || '');
                    }
                    if (target && typeof target.value === 'string') {
                        return target.value;
                    }
                    return String(target?.innerText || target?.textContent || '');
                }"""
            )
        except Exception:
            try:
                raw = locator.inner_text() or ""
            except Exception:
                raw = ""
        return self._normalize_editor_text(raw)

    # ------------------------------------------------------------------
    # 23. _inspect_comment_submit_state
    # ------------------------------------------------------------------
    def _inspect_comment_submit_state(self, editor_locator, expected_text: str) -> dict:
        expected = self._normalize_editor_text(expected_text)
        state = {
            "current_text": "",
            "text_changed": False,
            "editor_empty": False,
            "placeholder_restored": False,
            "editor_detached": False,
            "send_hidden_or_missing": False,
            "send_disabled": False,
            "container_uid": "",
            "success": False,
            "reason": "submit_state_unconfirmed",
            "message": "点击发送后未捕获到明确成功信号",
        }
        try:
            editor_connected = True
            with contextlib.suppress(Exception):
                editor_connected = bool(editor_locator.evaluate("el => !!(el && el.isConnected)"))
            if not editor_connected:
                state.update(
                    {
                        "editor_detached": True,
                        "success": True,
                        "reason": "editor_detached",
                        "message": "发送后回复输入框已从 DOM 脱离",
                    }
                )
                return state

            current_text = self._read_editor_text(editor_locator)
            state["current_text"] = current_text
            state["text_changed"] = bool(current_text and current_text != expected)
            state["editor_empty"] = not bool(current_text)
            if state["text_changed"]:
                state.update(
                    {
                        "success": True,
                        "reason": "editor_text_changed",
                        "message": f"发送后输入框内容已变化: current={current_text[:80]!r}",
                    }
                )
                return state

            marker_result = self._mark_comment_composer_targets(editor_locator)
            marked_uid = str((marker_result or {}).get("uid", "") or "").strip()
            state["container_uid"] = marked_uid
            placeholder_selectors = [
                ".public-DraftEditorPlaceholder-inner",
                "textarea[placeholder*='评论']",
                "textarea[placeholder*='写']",
                "[placeholder*='评论']",
            ]
            if marked_uid:
                for selector in placeholder_selectors:
                    placeholder_visible = self.page.locator(
                        f'[data-trae-comment-container="{marked_uid}"] {selector}'
                    ).first
                    with contextlib.suppress(Exception):
                        if placeholder_visible.is_visible(timeout=1200):
                            state["placeholder_restored"] = True
                            break

                send_locator = self.page.locator(f'[data-trae-comment-send="{marked_uid}"]').first
                try:
                    send_visible = send_locator.is_visible(timeout=500)
                except Exception:
                    send_visible = False
                state["send_hidden_or_missing"] = not send_visible
                if send_visible:
                    with contextlib.suppress(Exception):
                        state["send_disabled"] = bool(
                            send_locator.evaluate(
                                """el => {
                                    if (!el) return false;
                                    const ariaDisabled = String(el.getAttribute?.('aria-disabled') || '').toLowerCase() === 'true';
                                    const disabledProp = !!el.disabled;
                                    const cls = String(el.className || '').toLowerCase();
                                    const style = window.getComputedStyle(el);
                                    return (
                                        ariaDisabled
                                        || disabledProp
                                        || cls.includes('disabled')
                                        || style.pointerEvents === 'none'
                                        || style.opacity === '0'
                                    );
                                }"""
                            )
                        )

            if state["editor_empty"] and state["placeholder_restored"]:
                state.update(
                    {
                        "success": True,
                        "reason": "placeholder_restored",
                        "message": "发送后占位提示已恢复",
                    }
                )
                return state

            if state["editor_empty"] and (state["send_hidden_or_missing"] or state["send_disabled"]):
                state.update(
                    {
                        "success": True,
                        "reason": "editor_cleared_and_send_inactive",
                        "message": "发送后输入框已清空且发送按钮已隐藏或失活",
                    }
                )
                return state
        except Exception as e:
            logger.debug(f"检查评论发送状态失败: {e}")
            state["message"] = f"检查评论发送状态失败: {e}"
        return state

    # ------------------------------------------------------------------
    # 24. _verify_comment_submit_success
    # ------------------------------------------------------------------
    def _verify_comment_submit_success(self, editor_locator, expected_text: str) -> bool:
        """发送后校验评论框是否已清空或恢复占位态。"""
        state = self._inspect_comment_submit_state(editor_locator, expected_text)
        if state.get("success"):
            logger.info(f"评论发送后状态确认成功: {state}")
            return True
        logger.info(f"评论发送后状态尚未确认成功: {state}")
        return False

    # ------------------------------------------------------------------
    # 25. _parse_comment_submit_response
    # ------------------------------------------------------------------
    def _parse_comment_submit_response(self, response) -> dict:
        url = str(getattr(response, "url", "") or "").strip()
        if self._classify_comment_submit_api_url(url) != "submit":
            return {
                "matched": False,
                "ok": False,
                "reason": "submit_response_unmatched",
                "message": "",
                "status": int(getattr(response, "status", 0) or 0),
                "status_code": None,
                "url": url,
            }

        status = int(getattr(response, "status", 0) or 0)
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("content-type", "") or "")
        data = None
        raw_text = ""
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass

        if data is None:
            try:
                raw_text = str(response.text() or "")
            except Exception:
                raw_text = ""
            parsed = CommentAPIInterceptor._parse_response_text(raw_text) if raw_text else None
            if isinstance(parsed, dict):
                data = parsed

        payload = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        raw_status_code = None
        if isinstance(data, dict):
            raw_status_code = data.get("status_code", data.get("statusCode"))
            if raw_status_code is None and isinstance(payload, dict):
                raw_status_code = payload.get("status_code", payload.get("statusCode"))
        normalized_status_code = "" if raw_status_code in (None, "") else str(raw_status_code).strip()
        message = ""
        if isinstance(data, dict):
            for key in ("status_msg", "message", "msg", "description", "prompt"):
                value = str(data.get(key, "") or "").strip()
                if value:
                    message = value
                    break
        if not message and isinstance(payload, dict):
            for key in ("status_msg", "message", "msg", "description", "prompt"):
                value = str(payload.get(key, "") or "").strip()
                if value:
                    message = value
                    break

        comment_info = payload.get("comment") if isinstance(payload, dict) else None
        if not comment_info and isinstance(data, dict):
            comment_info = data.get("comment")
        comment_id = ""
        for candidate in (
            payload.get("comment_id") if isinstance(payload, dict) else "",
            payload.get("commentId") if isinstance(payload, dict) else "",
            payload.get("cid") if isinstance(payload, dict) else "",
            data.get("comment_id") if isinstance(data, dict) else "",
            data.get("commentId") if isinstance(data, dict) else "",
            data.get("cid") if isinstance(data, dict) else "",
        ):
            value = str(candidate or "").strip()
            if value:
                comment_id = value
                break

        explicit_success = status == 200 and normalized_status_code == "0"
        inferred_success = status == 200 and normalized_status_code == "" and bool(comment_info or comment_id)
        ok = bool(explicit_success or inferred_success)
        if ok:
            success_message = message or ("评论发布响应已确认" if explicit_success else "评论发布已回包，按评论标识推断成功")
            return {
                "matched": True,
                "ok": True,
                "reason": "submit_response_confirmed",
                "message": success_message,
                "status": status,
                "status_code": normalized_status_code,
                "url": url,
                "content_type": content_type,
            }

        failure_message = message or (raw_text[:120] if raw_text else "")
        if not failure_message:
            failure_message = "评论发布接口未返回可确认的成功结果"
        return {
            "matched": True,
            "ok": False,
            "reason": "submit_request_rejected",
            "message": failure_message,
            "status": status,
            "status_code": normalized_status_code,
            "url": url,
            "content_type": content_type,
        }

    # ------------------------------------------------------------------
    # 26. _submit_comment_and_confirm_result
    # ------------------------------------------------------------------
    def _submit_comment_and_confirm_result(self, send_button, editor_locator, expected_text: str) -> dict:
        submitted = False
        click_channel = ""

        def _perform_enter_submit() -> bool:
            nonlocal submitted, click_channel
            if submitted or editor_locator is None:
                return False
            try:
                with contextlib.suppress(Exception):
                    editor_locator.click(timeout=1200)
                with contextlib.suppress(Exception):
                    editor_locator.press("Enter", timeout=1200)
                    click_channel = "editor_enter"
                    submitted = True
                    return True
            except Exception as e:
                logger.debug(f"评论输入框 Enter 提交失败: {e}")
            try:
                keyboard = getattr(self.page, "keyboard", None)
                if keyboard and hasattr(keyboard, "press"):
                    keyboard.press("Enter")
                    click_channel = "editor_enter"
                    submitted = True
                    return True
            except Exception as e:
                logger.debug(f"页面键盘 Enter 提交失败: {e}")
            return False

        def _perform_submit() -> None:
            nonlocal submitted, click_channel
            if submitted:
                return
            helper = self._human_helper()
            clicked = helper.click_target(send_button, pre_hover=True) if helper else False
            if clicked:
                click_channel = "human_like"
                submitted = True
                return
            try:
                send_button.click(timeout=1500, force=True)
                click_channel = "force_click"
                submitted = True
                return
            except Exception as e:
                logger.debug(f"发送按钮 force click 失败: {e}")
            try:
                send_button.evaluate("(el) => el.click()")
                click_channel = "dom_click"
                submitted = True
                return
            except Exception as e:
                logger.debug(f"发送按钮 DOM click 失败: {e}")
            click_channel = "submit_click_failed"

        def _is_submit_response(response) -> bool:
            return (
                self._classify_comment_submit_api_url(
                    str(getattr(response, "url", "") or "")
                ) == "submit"
                and str(
                    getattr(
                        getattr(response, "request", None),
                        "method",
                        "",
                    )
                    or ""
                ).upper() == "POST"
            )

        submit_response = None
        listener_response = {"value": None}
        response_listener = None
        listener_registered = False
        enter_state = None
        if hasattr(self.page, "on") and hasattr(self.page, "remove_listener"):
            def _handle_response(response):
                if listener_response["value"] is None and _is_submit_response(response):
                    listener_response["value"] = response

            response_listener = _handle_response
            try:
                self.page.on("response", response_listener)
                listener_registered = True
                enter_attempted = _perform_enter_submit()
                enter_deadline = time.monotonic() + (1.2 if enter_attempted else 0.0)
                while enter_attempted and time.monotonic() < enter_deadline:
                    if listener_response["value"] is not None:
                        submit_response = listener_response["value"]
                        break
                    if not self._sleep_with_stop_check(0.1):
                        break
                if submit_response is None and enter_attempted and not self.is_stopped():
                    enter_state = self._inspect_comment_submit_state(editor_locator, expected_text)
                    if not enter_state.get("success"):
                        submitted = False
                        click_channel = ""

                if submit_response is None and not submitted and not self.is_stopped():
                    _perform_submit()
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if listener_response["value"] is not None:
                        submit_response = listener_response["value"]
                        break
                    if not self._sleep_with_stop_check(0.1):
                        break
            except Exception as e:
                logger.info(f"监听评论发布响应失败，回退到输入框状态校验: {e}")
            finally:
                if listener_registered and response_listener is not None:
                    with contextlib.suppress(Exception):
                        self.page.remove_listener("response", response_listener)
        else:
            expect_response = getattr(self.page, "expect_response", None)
            if callable(expect_response):
                try:
                    with expect_response(_is_submit_response, timeout=5000) as response_info:
                        _perform_submit()
                    submit_response = getattr(response_info, "value", None)
                except Exception as e:
                    logger.info(f"等待评论发布响应未命中，回退到输入框状态校验: {e}")

        if not submitted:
            _perform_submit()
        if not submitted:
            return {
                "matched": False,
                "ok": False,
                "reason": "submit_click_failed",
                "message": "发送按钮点击失败，未触发提交",
                "status": 0,
                "status_code": "",
                "url": "",
            }

        if self.is_stopped():
            return {
                "matched": False,
                "ok": False,
                "reason": "submit_cancelled",
                "message": "发送过程中收到停止信号，已取消等待提交结果",
                "status": 0,
                "status_code": "",
                "url": "",
                "click_channel": click_channel,
            }

        if not self._sleep_with_stop_check(1.2):
            return {
                "matched": False,
                "ok": False,
                "reason": "submit_cancelled",
                "message": "发送后等待确认时收到停止信号，已取消当前步骤",
                "status": 0,
                "status_code": "",
                "url": "",
                "click_channel": click_channel,
            }
        if submit_response is not None:
            parsed = self._parse_comment_submit_response(submit_response)
            if parsed.get("matched"):
                parsed["click_channel"] = click_channel
                return parsed

        state = enter_state if isinstance(enter_state, dict) and enter_state.get("success") else None
        if state is None:
            state = self._inspect_comment_submit_state(editor_locator, expected_text)
        if state.get("success"):
            return {
                "matched": False,
                "ok": True,
                "reason": "success_dom_fallback",
                "message": str(state.get("message", "") or "未捕获发布响应，但发送后状态已变化"),
                "status": 0,
                "status_code": "",
                "url": "",
                "click_channel": click_channel,
                "submit_state": state,
            }

        return {
            "matched": False,
            "ok": False,
            "reason": "submit_verify_failed",
            "message": str(state.get("message", "") or "点击发送后未通过输入框状态校验"),
            "status": 0,
            "status_code": "",
            "url": "",
            "click_channel": click_channel,
            "submit_state": state,
        }

    # ------------------------------------------------------------------
    # 27. _fill_comment_editor
    # ------------------------------------------------------------------
    def _fill_comment_editor(self, locator, text: str) -> bool:
        expected = self._normalize_editor_text(text)

        def _clear_with_dom() -> None:
            locator.evaluate(
                """el => {
                    if (!el) return;
                    const target =
                        (el.matches && el.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]') ? el : null)
                        || el.querySelector?.('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')
                        || el;
                    try { target.focus(); } catch (e) {}
                    if (target && target.isContentEditable) {
                        if (typeof target.replaceChildren === 'function') {
                            target.replaceChildren(document.createElement('br'));
                        } else {
                            target.innerHTML = '<br>';
                        }
                        try {
                            target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
                        } catch (e) {
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        return;
                    }
                    if (target && typeof target.value === 'string') {
                        target.value = '';
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }"""
            )

        def _direct_write(value: str) -> None:
            locator.evaluate(
                """(el, newValue) => {
                    if (!el) return;
                    const target =
                        (el.matches && el.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]') ? el : null)
                        || el.querySelector?.('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]')
                        || el;
                    try { target.focus(); } catch (e) {}
                    if (target && target.isContentEditable) {
                        const selection = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(target);
                        range.collapse(false);
                        selection.removeAllRanges();
                        selection.addRange(range);
                        let wrote = false;
                        try {
                            wrote = document.execCommand('insertText', false, newValue);
                        } catch (e) {}
                        if (!wrote) {
                            if (typeof target.replaceChildren === 'function') {
                                target.replaceChildren(document.createTextNode(newValue));
                            } else {
                                target.textContent = newValue;
                            }
                        }
                        try {
                            target.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, inputType: 'insertText', data: newValue }));
                        } catch (e) {}
                        try {
                            target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: newValue }));
                        } catch (e) {
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        return;
                    }
                    if (target && typeof target.value === 'string') {
                        target.value = newValue;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                value,
            )

        def _is_contenteditable_target() -> bool:
            try:
                return bool(
                    locator.evaluate(
                        """el => {
                            const target =
                                (el.matches && el.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]') ? el : null)
                                || el.querySelector?.('textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]')
                                || el;
                            return !!(target && target.isContentEditable);
                        }"""
                    )
                )
            except Exception:
                return False

        def _clear_with_keyboard() -> None:
            _focus_editor()
            with contextlib.suppress(Exception):
                locator.press("Control+A", timeout=1200)
            with contextlib.suppress(Exception):
                locator.press("Meta+A", timeout=1200)
            with contextlib.suppress(Exception):
                locator.press("Backspace", timeout=1200)
            with contextlib.suppress(Exception):
                locator.press("Delete", timeout=1200)

        def _focus_editor(allow_click: bool = False) -> None:
            if allow_click:
                with contextlib.suppress(Exception):
                    locator.click(timeout=1200, force=True)
            with contextlib.suppress(Exception):
                locator.evaluate(
                    """el => {
                        const target =
                            (el.matches && el.matches('textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]') ? el : null)
                            || el.querySelector?.('textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]')
                            || el;
                        try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                        try { target.focus(); } catch (e) {}
                    }"""
                )

        is_contenteditable = _is_contenteditable_target()
        _focus_editor(allow_click=True)

        if is_contenteditable:
            helper = self._human_helper()
            try:
                _clear_with_keyboard()
                self.page.wait_for_timeout(80)
                _focus_editor()
                if helper:
                    helper.press_sequentially(text, base_delay=(55, 105), allow_typos=False)
                else:
                    locator.type(text, delay=80, timeout=4000)
                self.page.wait_for_timeout(180)
                actual = self._read_editor_text(locator)
                if actual == expected:
                    logger.info("评论框已通过 contenteditable 真实键盘输入成功写入目标文本")
                    return True
                logger.warning(
                    f"contenteditable 真实键盘输入后评论框文本不匹配: expected={expected[:40]!r}, actual={actual[:40]!r}"
                )
            except Exception as e:
                logger.debug(f"contenteditable 真实键盘输入评论框失败: {e}")
            try:
                _clear_with_dom()
                self.page.wait_for_timeout(80)
                _focus_editor()
                self.page.keyboard.insert_text(text)
                self.page.wait_for_timeout(180)
                actual = self._read_editor_text(locator)
                if actual == expected:
                    logger.info("评论框已通过 keyboard.insert_text 成功写入目标文本")
                    return True
                logger.warning(
                    f"keyboard.insert_text 后评论框文本不匹配: expected={expected[:40]!r}, actual={actual[:40]!r}"
                )
            except Exception as e:
                logger.debug(f"keyboard.insert_text 写入评论框失败: {e}")
            try:
                _clear_with_dom()
                self.page.wait_for_timeout(80)
                _focus_editor()
                _direct_write(text)
                self.page.wait_for_timeout(180)
                actual = self._read_editor_text(locator)
                if actual == expected:
                    logger.info("评论框已通过 DOM 直写成功写入目标文本")
                    return True
                logger.warning(
                    f"DOM 直写后评论框文本不匹配: expected={expected[:40]!r}, actual={actual[:40]!r}"
                )
            except Exception as e:
                logger.debug(f"DOM 直写评论框失败: {e}")
            return False

        # 主路径：先 focus，再用 fill，让 Playwright 自己触发 contenteditable 的 input 链。
        try:
            _clear_with_dom()
            self.page.wait_for_timeout(80)
            _focus_editor()
            locator.fill(text, timeout=2500)
            self.page.wait_for_timeout(180)
            actual = self._read_editor_text(locator)
            if actual == expected:
                logger.info("评论框已通过 focus + fill 成功写入目标文本")
                return True
            logger.warning(f"focus + fill 后评论框文本不匹配: expected={expected[:40]!r}, actual={actual[:40]!r}")
        except Exception as e:
            logger.debug(f"focus + fill 写入评论框失败: {e}")

        # 回退 1：逐字 type，触发更完整的键盘事件。
        try:
            _clear_with_dom()
            self.page.wait_for_timeout(80)
            _focus_editor()
            locator.type(text, delay=80, timeout=4000)
            self.page.wait_for_timeout(180)
            actual = self._read_editor_text(locator)
            if actual == expected:
                logger.info("评论框已通过 focus + type 成功写入目标文本")
                return True
            logger.warning(f"focus + type 后评论框文本不匹配: expected={expected[:40]!r}, actual={actual[:40]!r}")
        except Exception as e:
            logger.debug(f"focus + type 写入评论框失败: {e}")

        return False

    # ------------------------------------------------------------------
    # 28. _activate_comment_input
    # ------------------------------------------------------------------
    def _activate_comment_input(self, detail_structure: Optional[dict] = None):
        """点击评论输入区域的占位符/容器来激活编辑器，使 contenteditable 元素出现。"""
        # 1. 尝试通过占位文字定位并点击
        placeholder_texts = [
            "写评论…", "写评论...", "留下你的精彩评论吧",
            "善语结善缘，评论会有惊喜哦", "发一条友善的评论吧",
            "说点什么...", "请输入评论",
        ]
        for placeholder in placeholder_texts:
            try:
                loc = self.page.locator(f"text={placeholder}").first
                if loc.is_visible(timeout=1500):
                    loc.click(timeout=2000)
                    logger.info(f"已点击评论占位文字激活编辑器: {placeholder}")
                    time.sleep(1.0)
                    return
            except Exception:
                continue

        # 2. 尝试通过容器选择器点击
        activate_selectors = self._get_comment_composer_container_selectors(
            self._resolve_comment_detail_kind(detail_structure)
        )
        for selector in activate_selectors:
            try:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=1500):
                    loc.click(timeout=2000)
                    logger.info(f"已点击评论输入区域激活编辑器: selector={selector}")
                    time.sleep(1.0)
                    return
            except Exception:
                continue

        # 3. 兜底：通过 JS 查找包含评论占位文字的元素并点击
        try:
            clicked = self.page.evaluate(
                """() => {
                    const targets = Array.from(document.querySelectorAll(
                        'div, span, p, [contenteditable], textarea'
                    )).filter(el => {
                        if (!el.offsetParent) return false;
                        const text = (el.innerText || el.placeholder || '').trim();
                        return text === '写评论…' || text === '写评论...'
                            || text === '留下你的精彩评论吧'
                            || text === '善语结善缘，评论会有惊喜哦'
                            || text === '发一条友善的评论吧'
                            || text === '说点什么...'
                            || text === '请输入评论';
                    });
                    if (targets.length) {
                        targets[0].click();
                        return true;
                    }
                    return false;
                }"""
            )
            if clicked:
                logger.info("通过 JS 兜底点击评论占位文字激活编辑器")
            time.sleep(1.0)
        except Exception as e:
            logger.debug(f"JS兜底激活评论输入框失败: {e}")

    # ------------------------------------------------------------------
    # 29. _find_comment_editor_by_js
    # ------------------------------------------------------------------
    def _find_comment_editor_by_js(
        self,
        detail_structure: Optional[dict] = None,
        activation: Optional[dict] = None,
    ):
        """通过 JavaScript 动态查找评论区内的 contenteditable 元素作为兜底方案。"""
        try:
            detail_kind = self._resolve_comment_detail_kind(detail_structure)
            handle = self.page.evaluate_handle(
                """(payload) => {
                    const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 10) return false;
                        const style = window.getComputedStyle(node);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const normalize = (value) => String(value || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const textOf = (node) => String(
                        node?.innerText
                        || node?.textContent
                        || node?.value
                        || node?.getAttribute?.('placeholder')
                        || node?.getAttribute?.('aria-label')
                        || ''
                    );
                    const isDanmuEditor = (node) => {
                        const text = normalize(textOf(node));
                        return text === '发一条弹幕吧' || text.includes('弹幕');
                    };
                    const activationContainer = payload.activationContainerUid
                        ? document.querySelector(`[data-trae-reply-active-container="${payload.activationContainerUid}"]`)
                        : null;
                    const activationEditor = payload.activationEditorUid
                        ? document.querySelector(`[data-trae-reply-active-editor="${payload.activationEditorUid}"]`)
                        : null;
                    const activationSend = payload.activationSendUid
                        ? document.querySelector(`[data-trae-reply-active-send="${payload.activationSendUid}"]`)
                        : null;

                    if (activationEditor && visible(activationEditor) && !isDanmuEditor(activationEditor)) {
                        return activationEditor;
                    }

                    const containerSeen = new Set();
                    const orderedContainers = [];
                    const pushContainer = (node) => {
                        if (!node || !visible(node) || containerSeen.has(node)) return;
                        containerSeen.add(node);
                        orderedContainers.push(node);
                    };

                    pushContainer(activationContainer);

                    // 1. 在评论输入容器内查找
                    const containers = Array.from(document.querySelectorAll(payload.containerSelectors.join(', '))).filter(visible);
                    for (const container of containers) {
                        pushContainer(container);
                    }

                    const scoreEditor = (editor, container) => {
                        if (!editor || !visible(editor) || isDanmuEditor(editor)) return -999;
                        const editorRect = editor.getBoundingClientRect();
                        const containerRect = container?.getBoundingClientRect?.() || editorRect;
                        const containerText = normalize(textOf(container));
                        const containerHtml = normalize(String(container?.innerHTML || ''));
                        const placeholder = normalize(
                            editor.getAttribute?.('placeholder')
                            || editor.getAttribute?.('aria-label')
                            || ''
                        );
                        const attrBag = normalize([
                            container?.id || '',
                            container?.className || '',
                            container?.getAttribute?.('data-e2e') || '',
                            container?.getAttribute?.('data-trae-reply-active-container') || '',
                            editor?.getAttribute?.('data-trae-reply-active-editor') || '',
                        ].join(' '));
                        let score = 0;
                        if (container && activationContainer && container === activationContainer) score += 30;
                        if (editor && activationEditor && editor === activationEditor) score += 40;
                        if (activationSend && container?.contains?.(activationSend)) score += 18;
                        if (attrBag.includes('trae-reply-active')) score += 14;
                        if (containerText.includes('回复') || containerHtml.includes('回复')) score += 10;
                        if (placeholder.includes('回复') || placeholder.includes('评论') || placeholder.includes('写')) score += 6;
                        if (containerRect.width >= 220) score += 4;
                        if (editorRect.width >= 120) score += 2;
                        return score;
                    };

                    for (const container of orderedContainers) {
                        const editors = Array.from(container.querySelectorAll(
                            '[data-trae-reply-active-editor], [contenteditable="true"], [role="textbox"], [contenteditable="plaintext-only"], textarea'
                        )).filter(visible);
                        const scored = [];
                        for (const editor of editors) {
                            const score = scoreEditor(editor, container);
                            if (score > 0) {
                                scored.push({ editor, score });
                            }
                        }
                        scored.sort((a, b) => b.score - a.score);
                        if (scored.length) {
                            return scored[0].editor;
                        }
                    }

                    // 2. 查找 textarea（新版抖音可能使用 textarea）
                    const textareas = Array.from(document.querySelectorAll(
                        'textarea[placeholder*="评论"], textarea[placeholder*="评论"], textarea[placeholder*="写"]'
                    )).filter((node) => visible(node) && !isDanmuEditor(node));
                    if (textareas.length) {
                        if (activationContainer) {
                            const innerTextarea = textareas.find((node) => activationContainer.contains(node));
                            if (innerTextarea) return innerTextarea;
                        }
                        return textareas[0];
                    }

                    // 3. 全局查找所有 contenteditable 元素，排除弹幕
                    const allEditors = Array.from(document.querySelectorAll(
                        '[contenteditable="true"], [role="textbox"], textarea, [contenteditable="plaintext-only"]'
                    )).filter((node) => visible(node) && !isDanmuEditor(node));

                    if (activationContainer) {
                        const anchorRect = activationContainer.getBoundingClientRect();
                        const scored = [];
                        for (const editor of allEditors) {
                            const rect = editor.getBoundingClientRect();
                            const distance = Math.abs(rect.top - anchorRect.top) + Math.abs(rect.left - anchorRect.left);
                            let score = 1000 - distance;
                            if (activationContainer.contains(editor)) score += 500;
                            scored.push({ editor, score });
                        }
                        scored.sort((a, b) => b.score - a.score);
                        if (scored.length) return scored[0].editor;
                    }

                    // 优先选择评论区附近的（页面下半部分或右侧面板）
                    const pageHeight = window.innerHeight;
                    const pageWidth = window.innerWidth;
                    for (const editor of allEditors) {
                        const rect = editor.getBoundingClientRect();
                        // 评论区输入框通常在页面下方或右侧面板
                        if (rect.top > pageHeight * 0.2 || rect.left > pageWidth * 0.4) {
                            return editor;
                        }
                    }

                    // 4. 最后兜底：返回第一个非弹幕的 contenteditable
                    for (const editor of allEditors) {
                        const text = (editor.innerText || '').trim();
                        if (text === '发一条弹幕吧') continue;
                        return editor;
                    }

                    return null;
                }""",
                {
                    "containerSelectors": self._get_comment_composer_container_selectors(detail_kind),
                    "activationContainerUid": str((activation or {}).get("containerUid", "") or "").strip(),
                    "activationEditorUid": str((activation or {}).get("editorUid", "") or "").strip(),
                    "activationSendUid": str((activation or {}).get("sendUid", "") or "").strip(),
                }
            )
            if handle:
                locator = handle.as_element()
                if locator and locator.is_visible(timeout=1000):
                    logger.info("通过 JS 动态查找命中评论输入框")
                    return locator
        except Exception as e:
            logger.debug(f"JS动态查找评论输入框失败: {e}")
        return None

    # ------------------------------------------------------------------
    # 30. _normalize_comment_match_text
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_comment_match_text(text: str) -> str:
        normalized = str(text or "").replace("\u200b", "").replace("\ufeff", "")
        normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.strip().lower()

    # ------------------------------------------------------------------
    # 31. _click_reply_button_for_comment_target
    # ------------------------------------------------------------------
    def _click_reply_button_for_comment_target(
        self,
        target: dict,
        detail_structure: Optional[dict] = None,
        preferred_surface_selector: str = "",
        allow_document_fallback: bool = True,
    ) -> dict:
        target = self._normalize_reply_target(target)
        detail_kind = self._resolve_comment_detail_kind(detail_structure)
        payload = {
            "commentId": str((target or {}).get("comment_id", "") or "").strip(),
            "parentCommentId": str((target or {}).get("parent_comment_id", "") or "").strip(),
            "rootCommentId": str((target or {}).get("root_comment_id", "") or "").strip(),
            "nickname": str((target or {}).get("nickname", "") or "").strip(),
            "commentText": str((target or {}).get("comment_text", "") or "").strip(),
            "commentTextPrefix": str((target or {}).get("comment_text_prefix", "") or "").strip(),
            "commentTextShortPrefix": str((target or {}).get("comment_text_short_prefix", "") or "").strip(),
            "secUid": str((target or {}).get("sec_uid", "") or "").strip(),
            "uniqueId": str((target or {}).get("unique_id", "") or "").strip(),
            "createTime": int((target or {}).get("create_time", 0) or 0),
            "parentNickname": str((((target or {}).get("parent_anchor") or {}).get("nickname", "") or "")).strip(),
            "parentText": str((((target or {}).get("parent_anchor") or {}).get("comment_text", "") or "")).strip(),
            "parentTextPrefix": str((((target or {}).get("parent_anchor") or {}).get("comment_text_prefix", "") or "")).strip(),
            "parentTextShortPrefix": str((((target or {}).get("parent_anchor") or {}).get("comment_text_short_prefix", "") or "")).strip(),
            "parentSecUid": str((((target or {}).get("parent_anchor") or {}).get("sec_uid", "") or "")).strip(),
            "parentUniqueId": str((((target or {}).get("parent_anchor") or {}).get("unique_id", "") or "")).strip(),
            "commentLevel": max(int((target or {}).get("comment_level", 1) or 1), 1),
            "itemSelectors": self._get_comment_item_selectors_for_layout(detail_kind),
            "surfaceSelectors": self._get_comment_surface_selectors_for_layout(detail_kind),
            "detailKind": detail_kind,
            "preferredSurfaceSelector": str(preferred_surface_selector or ""),
            "allowDocumentFallback": bool(allow_document_fallback),
        }
        try:
            locate_result = self.page.evaluate(
                """(payload) => {
                    document.querySelectorAll('[data-trae-reply-trigger], [data-trae-reply-item], [data-trae-reply-parent], [data-trae-reply-surface]').forEach((node) => {
                        node.removeAttribute('data-trae-reply-trigger');
                        node.removeAttribute('data-trae-reply-item');
                        node.removeAttribute('data-trae-reply-parent');
                        node.removeAttribute('data-trae-reply-surface');
                    });
                    const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 24 || rect.height < 12) return false;
                        const style = window.getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const textOf = (node) => String(node?.innerText || node?.textContent || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const normalize = (value) => String(value || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const isReplyText = (value) => {
                        const text = normalize(value);
                        if (!text) return false;
                        if (/查看|展开|全部|更多/.test(text)) return false;
                        return text === '回复' || /^回复\\s*\\d*$/.test(text);
                    };
                    const isCompactControl = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width >= 16 && rect.width <= 180 && rect.height >= 12 && rect.height <= 56;
                    };
                    const targetCommentId = normalize(payload.commentId);
                    const targetParentCommentId = normalize(payload.parentCommentId);
                    const targetRootCommentId = normalize(payload.rootCommentId);
                    const targetNickname = normalize(payload.nickname);
                    const targetText = normalize(payload.commentText);
                    const textPrefix = normalize(payload.commentTextPrefix || targetText.slice(0, 24));
                    const fallbackPrefix = normalize(payload.commentTextShortPrefix || targetText.slice(0, 12));
                    const targetSecUid = normalize(payload.secUid);
                    const targetUniqueId = normalize(payload.uniqueId);
                    const targetParentNickname = normalize(payload.parentNickname);
                    const targetParentText = normalize(payload.parentText);
                    const targetParentTextPrefix = normalize(payload.parentTextPrefix || targetParentText.slice(0, 24));
                    const targetParentShortPrefix = normalize(payload.parentTextShortPrefix || targetParentText.slice(0, 12));
                    const targetParentSecUid = normalize(payload.parentSecUid);
                    const targetParentUniqueId = normalize(payload.parentUniqueId);
                    const itemSelectors = payload.itemSelectors || [];
                    const surfaceSelectors = payload.surfaceSelectors || [];
                    const detailKind = normalize(payload.detailKind || '');
                    const preferredSurfaceSelector = normalize(payload.preferredSurfaceSelector || '');
                    const allowDocumentFallback = Boolean(payload.allowDocumentFallback);
                    const collectScopedItems = (scopeRoot) => {
                        if (!scopeRoot || !scopeRoot.querySelectorAll) return [];
                        const scopedItems = [];
                        const scopedSeen = new Set();
                        for (const selector of itemSelectors) {
                            for (const node of Array.from(scopeRoot.querySelectorAll(selector))) {
                                if (!visible(node) || scopedSeen.has(node)) continue;
                                scopedSeen.add(node);
                                scopedItems.push(node);
                            }
                        }
                        if (!scopedItems.length) {
                            const fallbackItems = Array.from(scopeRoot.querySelectorAll('li, article, div'))
                                .filter((node) => node !== scopeRoot)
                                .filter(visible)
                                .filter((node) => {
                                    const text = normalize(node.innerText || node.textContent || '');
                                    return text.length >= 8 && /回复/.test(text);
                                });
                            for (const node of fallbackItems) {
                                if (scopedSeen.has(node)) continue;
                                scopedSeen.add(node);
                                scopedItems.push(node);
                            }
                        }
                        return scopedItems;
                    };
                    const resolveSurfaceRoot = () => {
                        if (preferredSurfaceSelector && !preferredSurfaceSelector.includes('::')) {
                            try {
                                const preferredNode = document.querySelector(preferredSurfaceSelector);
                                if (visible(preferredNode)) {
                                    const scopedItems = collectScopedItems(preferredNode);
                                    return {
                                        node: preferredNode,
                                        selector: preferredSurfaceSelector,
                                        score: 999,
                                        itemCount: scopedItems.length,
                                        textLength: normalize(preferredNode.innerText || preferredNode.textContent || '').length,
                                    };
                                }
                            } catch (e) {}
                        }
                        const scored = [];
                        const classBag = (node) => normalize([
                            node?.className || '',
                            node?.id || '',
                            node?.getAttribute?.('data-e2e') || '',
                        ].join(' '));
                        for (const selector of surfaceSelectors) {
                            for (const matched of Array.from(document.querySelectorAll(selector)).filter(visible)) {
                                const scopedItems = collectScopedItems(matched);
                                const textLength = normalize(matched.innerText || matched.textContent || '').length;
                                const rect = matched.getBoundingClientRect();
                                const classes = classBag(matched);
                                let score = 0;
                                if (selector === '[data-e2e="note-comment-list"]') score += 18;
                                else if (selector === '[data-e2e="comment-panel"]') score += 16;
                                if (detailKind === 'note' && /note-comment|commentcontainer|commentwrap|commentscroll/.test(classes)) {
                                    score += 10;
                                }
                                if (detailKind === 'video' && /comment-panel|commentdrawer|commentcontent|comment-main/.test(classes)) {
                                    score += 8;
                                }
                                score += Math.min(scopedItems.length, 12) * 5;
                                if (textLength >= 80) score += 4;
                                if ((rect.width || 0) >= 220) score += 2;
                                if ((rect.height || 0) >= 160) score += 2;
                                scored.push({
                                    node: matched,
                                    selector,
                                    score,
                                    itemCount: scopedItems.length,
                                    textLength,
                                });
                            }
                        }
                        scored.sort((a, b) => {
                            const scoreDiff = (b.score || 0) - (a.score || 0);
                            if (scoreDiff !== 0) return scoreDiff;
                            const itemDiff = (b.itemCount || 0) - (a.itemCount || 0);
                            if (itemDiff !== 0) return itemDiff;
                            return (b.textLength || 0) - (a.textLength || 0);
                        });
                        return scored[0] || null;
                    };
                    let surfaceRoot = null;
                    let resolvedSurface = resolveSurfaceRoot();
                    if (resolvedSurface && resolvedSurface.node) {
                        surfaceRoot = resolvedSurface.node;
                    }
                    let surfaceMarkerUid = '';
                    if (surfaceRoot) {
                        surfaceMarkerUid = `trae-reply-surface-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                        try {
                            surfaceRoot.setAttribute('data-trae-reply-surface', surfaceMarkerUid);
                        } catch (e) {}
                    }
                    if (!surfaceRoot && !allowDocumentFallback) {
                        return {
                            clicked: false,
                            reason: 'comment_surface_missing',
                            inspected: 0,
                            surfaceSelector: preferredSurfaceSelector || '',
                            surfaceItemCount: 0,
                            usedDocumentFallback: false,
                            surfaceMarkerUid,
                        };
                    }
                    const searchRoot = surfaceRoot || document;
                    const usedDocumentFallback = !surfaceRoot;
                    const items = [];
                    const seen = new Set();
                    for (const selector of itemSelectors) {
                        for (const node of Array.from(searchRoot.querySelectorAll(selector))) {
                            if (!visible(node) || seen.has(node)) continue;
                            seen.add(node);
                            items.push(node);
                        }
                    }
                    if (!items.length && surfaceRoot) {
                        for (const node of collectScopedItems(surfaceRoot)) {
                            if (seen.has(node)) continue;
                            seen.add(node);
                            items.push(node);
                        }
                    }
                    const getAttrBag = (node) => {
                        const attrs = [];
                        if (node?.id) attrs.push(node.id);
                        if (node?.className) attrs.push(String(node.className));
                        if (node?.dataset) {
                            for (const key of Object.keys(node.dataset)) {
                                attrs.push(String(node.dataset[key] || ''));
                            }
                        }
                        for (const name of ['data-cid', 'data-id', 'data-comment-id', 'data-e2e', 'href']) {
                            const value = node?.getAttribute?.(name);
                            if (value) attrs.push(String(value));
                        }
                        return normalize(attrs.join(' '));
                    };
                    const getAuthorBag = (item) => {
                        const values = [];
                        for (const node of Array.from(item?.querySelectorAll?.('a[href], [data-e2e], [href]') || [])) {
                            const href = node?.getAttribute?.('href');
                            const dataE2e = node?.getAttribute?.('data-e2e');
                            const aria = node?.getAttribute?.('aria-label');
                            const text = node?.innerText || node?.textContent || '';
                            if (href) values.push(String(href));
                            if (dataE2e) values.push(String(dataE2e));
                            if (aria) values.push(String(aria));
                            if (text) values.push(String(text));
                        }
                        return normalize(values.join(' '));
                    };
                    const buildReplyCandidate = (item, extraScore = 0) => {
                        const itemText = normalize(item.innerText || item.textContent || '');
                        const attrBag = getAttrBag(item);
                        const authorBag = getAuthorBag(item);
                        const idMatched = Boolean(targetCommentId && attrBag.includes(targetCommentId));
                        const nicknameMatched = Boolean(targetNickname && itemText.includes(targetNickname));
                        const secUidMatched = Boolean(targetSecUid && (attrBag.includes(targetSecUid) || authorBag.includes(targetSecUid)));
                        const uniqueIdMatched = Boolean(targetUniqueId && (attrBag.includes(targetUniqueId) || authorBag.includes(targetUniqueId)));
                        const exactTextMatched = Boolean(targetText && itemText.includes(targetText));
                        const strongPrefixMatched = Boolean(textPrefix.length >= 8 && itemText.includes(textPrefix));
                        const fallbackPrefixMatched = Boolean(fallbackPrefix.length >= 12 && itemText.includes(fallbackPrefix));
                        const textMatched = exactTextMatched || strongPrefixMatched || fallbackPrefixMatched;
                        let score = extraScore;
                        if (idMatched) score += 12;
                        if (secUidMatched) score += 5;
                        if (uniqueIdMatched) score += 5;
                        if (nicknameMatched) score += 4;
                        if (exactTextMatched) score += 10;
                        else if (strongPrefixMatched) score += 6;
                        else if (fallbackPrefixMatched) score += 3;
                        if (payload.commentLevel > 1 && /回复/.test(itemText)) score += 1;
                        const authorMatched = secUidMatched || uniqueIdMatched || nicknameMatched;
                        const strongEnough = idMatched || (authorMatched && textMatched);
                        if (!strongEnough || score <= 0) return null;
                        return {
                            item,
                            score,
                            itemText,
                            attrBag,
                            authorBag,
                            idMatched,
                            secUidMatched,
                            uniqueIdMatched,
                            nicknameMatched,
                            exactTextMatched,
                            strongPrefixMatched,
                            fallbackPrefixMatched,
                            textMatched,
                        };
                    };
                    const buildParentCandidate = (item) => {
                        const itemText = normalize(item.innerText || item.textContent || '');
                        const attrBag = getAttrBag(item);
                        const authorBag = getAuthorBag(item);
                        const parentIdMatched = Boolean(
                            (targetParentCommentId && attrBag.includes(targetParentCommentId))
                            || (targetRootCommentId && attrBag.includes(targetRootCommentId))
                        );
                        const parentNicknameMatched = Boolean(targetParentNickname && itemText.includes(targetParentNickname));
                        const parentSecUidMatched = Boolean(targetParentSecUid && (attrBag.includes(targetParentSecUid) || authorBag.includes(targetParentSecUid)));
                        const parentUniqueIdMatched = Boolean(targetParentUniqueId && (attrBag.includes(targetParentUniqueId) || authorBag.includes(targetParentUniqueId)));
                        const exactTextMatched = Boolean(targetParentText && itemText.includes(targetParentText));
                        const strongPrefixMatched = Boolean(targetParentTextPrefix.length >= 8 && itemText.includes(targetParentTextPrefix));
                        const fallbackPrefixMatched = Boolean(targetParentShortPrefix.length >= 8 && itemText.includes(targetParentShortPrefix));
                        const parentTextMatched = exactTextMatched || strongPrefixMatched || fallbackPrefixMatched;
                        let score = 0;
                        if (parentIdMatched) score += 14;
                        if (parentSecUidMatched) score += 5;
                        if (parentUniqueIdMatched) score += 5;
                        if (parentNicknameMatched) score += 4;
                        if (exactTextMatched) score += 8;
                        else if (strongPrefixMatched) score += 5;
                        else if (fallbackPrefixMatched) score += 2;
                        const authorMatched = parentSecUidMatched || parentUniqueIdMatched || parentNicknameMatched;
                        const strongEnough = parentIdMatched || (authorMatched && parentTextMatched);
                        if (!strongEnough || score <= 0) return null;
                        return {
                            item,
                            score,
                            itemText,
                            attrBag,
                            parentIdMatched,
                            parentNicknameMatched,
                            parentSecUidMatched,
                            parentUniqueIdMatched,
                            parentTextMatched,
                        };
                    };
                    const collectReplyItemsNearParent = (parentNode) => {
                        if (!parentNode) return [];
                        const buckets = [];
                        const direct = collectScopedItems(parentNode).filter((node) => node !== parentNode);
                        if (direct.length) buckets.push(direct);
                        const parentElement = parentNode.parentElement;
                        if (parentElement) {
                            const siblingScoped = collectScopedItems(parentElement).filter((node) => node !== parentNode);
                            if (siblingScoped.length) buckets.push(siblingScoped);
                        }
                        const expanded = parentNode.closest?.('[data-e2e], li, article, section, div');
                        if (expanded && expanded !== parentNode && expanded !== parentElement) {
                            const expandedScoped = collectScopedItems(expanded).filter((node) => node !== parentNode);
                            if (expandedScoped.length) buckets.push(expandedScoped);
                        }
                        for (const bucket of buckets) {
                            if (bucket.length) return bucket;
                        }
                        return [];
                    };
                    let parentAnchor = null;
                    let parentMarkerUid = '';
                    let candidateItems = items;
                    if (payload.commentLevel > 1 && (targetParentCommentId || targetRootCommentId)) {
                        const parentCandidates = [];
                        for (const item of items) {
                            const parentCandidate = buildParentCandidate(item);
                            if (!parentCandidate) continue;
                            parentCandidates.push(parentCandidate);
                        }
                        parentCandidates.sort((a, b) => b.score - a.score);
                        parentAnchor = parentCandidates[0] || null;
                        if (parentAnchor) {
                            parentMarkerUid = `trae-reply-parent-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                            try {
                                parentAnchor.item.setAttribute('data-trae-reply-parent', parentMarkerUid);
                            } catch (e) {}
                            const scopedItems = collectReplyItemsNearParent(parentAnchor.item);
                            if (scopedItems.length) {
                                candidateItems = scopedItems;
                            }
                        }
                    }
                    const candidates = [];
                    for (const item of candidateItems) {
                        const candidate = buildReplyCandidate(
                            item,
                            parentAnchor && parentAnchor.item && parentAnchor.item.contains(item) ? 4 : 0,
                        );
                        if (!candidate) continue;
                        candidates.push(candidate);
                    }
                    candidates.sort((a, b) => b.score - a.score);
                    const best = candidates[0];
                    if (!best || best.score < 6) {
                        return {
                            clicked: false,
                            reason: 'target_not_found',
                            score: best ? best.score : 0,
                            inspected: candidates.length,
                            parentAnchorMatched: Boolean(parentAnchor),
                            parentAnchorText: parentAnchor ? parentAnchor.itemText.slice(0, 120) : '',
                            surfaceSelector: resolvedSurface ? resolvedSurface.selector : '',
                            surfaceItemCount: resolvedSurface ? resolvedSurface.itemCount : 0,
                            usedDocumentFallback,
                            parentMarkerUid,
                            surfaceMarkerUid,
                        };
                    }
                    const second = candidates[1];
                    const ambiguousWithoutId = !best.idMatched && !!second && second.score >= best.score - 1;
                    if (ambiguousWithoutId) {
                        return {
                            clicked: false,
                            reason: 'target_ambiguous',
                            score: best.score,
                            inspected: candidates.length,
                            ambiguous: true,
                            matchedText: best.itemText.slice(0, 120),
                            parentAnchorMatched: Boolean(parentAnchor),
                            surfaceSelector: resolvedSurface ? resolvedSurface.selector : '',
                            surfaceItemCount: resolvedSurface ? resolvedSurface.itemCount : 0,
                            usedDocumentFallback,
                            parentMarkerUid,
                            surfaceMarkerUid,
                        };
                    }

                    const directInteractiveCandidates = Array.from(
                        best.item.querySelectorAll('button, a, [role="button"], [tabindex="0"]')
                    )
                        .filter(visible)
                        .filter(isCompactControl)
                        .filter((node) => isReplyText(textOf(node)));

                    let replyButton = directInteractiveCandidates[0] || null;
                    let triggerSource = replyButton ? 'direct_interactive' : '';

                    if (!replyButton) {
                        const textCandidates = Array.from(
                            best.item.querySelectorAll('span, div, p')
                        )
                            .filter(visible)
                            .filter(isCompactControl)
                            .filter((node) => isReplyText(textOf(node)));

                        for (const node of textCandidates) {
                            const ancestor = node.closest?.('button, a, [role="button"], [tabindex="0"]');
                            if (
                                ancestor &&
                                best.item.contains(ancestor) &&
                                visible(ancestor) &&
                                isCompactControl(ancestor) &&
                                (isReplyText(textOf(ancestor)) || textOf(ancestor) === textOf(node))
                            ) {
                                replyButton = ancestor;
                                triggerSource = 'interactive_ancestor';
                                break;
                            }
                            if (!replyButton) {
                                replyButton = node;
                                triggerSource = 'direct_text';
                            }
                        }
                    }

                    if (!replyButton) {
                        return {
                            clicked: false,
                            reason: 'reply_button_missing',
                            score: best.score,
                            inspected: candidates.length,
                            matchedText: best.itemText.slice(0, 120),
                            usedDocumentFallback,
                            parentMarkerUid,
                            surfaceMarkerUid,
                        };
                    }
                    const uid = `trae-reply-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                    best.item.setAttribute('data-trae-reply-item', uid);
                    replyButton.setAttribute('data-trae-reply-trigger', uid);
                    return {
                        clicked: false,
                        ready: true,
                        reason: 'reply_button_ready',
                        score: best.score,
                        inspected: candidates.length,
                        markerUid: uid,
                        parentAnchorMatched: Boolean(parentAnchor),
                        surfaceSelector: resolvedSurface ? resolvedSurface.selector : '',
                        surfaceItemCount: resolvedSurface ? resolvedSurface.itemCount : 0,
                        usedDocumentFallback,
                        parentMarkerUid,
                        surfaceMarkerUid,
                        triggerSource,
                        replyButtonTag: String(replyButton.tagName || '').toLowerCase(),
                        replyButtonText: textOf(replyButton).slice(0, 32),
                        matchedText: best.itemText.slice(0, 120),
                    };
                }""",
                payload,
            )
            result = locate_result if isinstance(locate_result, dict) else {"clicked": False}
            if not result.get("ready"):
                return result

            marker_uid = str(result.get("markerUid", "") or "").strip()
            if not marker_uid:
                return {"clicked": False, "reason": "reply_button_missing"}

            selector = f'[data-trae-reply-trigger="{marker_uid}"]'
            try:
                reply_button = self.page.locator(selector).first
                if not reply_button.is_visible(timeout=1200):
                    return {
                        "clicked": False,
                        "reason": "reply_button_missing",
                        "score": result.get("score", 0),
                        "inspected": result.get("inspected", 0),
                        "matchedText": result.get("matchedText", ""),
                        "parentMarkerUid": result.get("parentMarkerUid", ""),
                        "surfaceMarkerUid": result.get("surfaceMarkerUid", ""),
                    }
                helper = self._human_helper()
                clicked = helper.click_target(reply_button, pre_hover=True) if helper else False
                time.sleep(0.25)
                activation = self._inspect_reply_activation_state(target, detail_structure=detail_structure)
                activation_active = bool((activation or {}).get("active"))
                click_channel = "human_like"
                if not activation_active:
                    if not clicked:
                        reply_button.click(timeout=1500, force=True)
                    time.sleep(0.25)
                    activation = self._inspect_reply_activation_state(target, detail_structure=detail_structure)
                    activation_active = bool((activation or {}).get("active"))
                    click_channel = "force_click"
                if not activation_active:
                    with contextlib.suppress(Exception):
                        reply_button.evaluate("(el) => el.click()")
                    time.sleep(0.25)
                    activation = self._inspect_reply_activation_state(target, detail_structure=detail_structure)
                    activation_active = bool((activation or {}).get("active"))
                    click_channel = "dom_click"
                if not activation_active:
                    return {
                        "clicked": False,
                        "reason": "reply_button_not_effective",
                        "score": result.get("score", 0),
                        "inspected": result.get("inspected", 0),
                        "replyButtonTag": result.get("replyButtonTag", ""),
                        "replyButtonText": result.get("replyButtonText", ""),
                        "triggerSource": result.get("triggerSource", ""),
                        "clickChannel": click_channel,
                        "activation": activation,
                        "matchedText": result.get("matchedText", ""),
                        "parentMarkerUid": result.get("parentMarkerUid", ""),
                        "surfaceMarkerUid": result.get("surfaceMarkerUid", ""),
                    }
                return {
                    "clicked": True,
                    "reason": "reply_button_clicked",
                    "score": result.get("score", 0),
                    "inspected": result.get("inspected", 0),
                    "replyButtonTag": result.get("replyButtonTag", ""),
                    "replyButtonText": result.get("replyButtonText", ""),
                    "triggerSource": result.get("triggerSource", ""),
                    "clickChannel": click_channel,
                    "activation": activation,
                    "matchedText": result.get("matchedText", ""),
                    "markerUid": marker_uid,
                    "parentMarkerUid": result.get("parentMarkerUid", ""),
                    "surfaceMarkerUid": result.get("surfaceMarkerUid", ""),
                }
            except Exception as click_error:
                logger.debug(f"点击评论下回复按钮失败: {click_error}")
                return {
                    "clicked": False,
                    "reason": "reply_button_click_failed",
                    "score": result.get("score", 0),
                    "inspected": result.get("inspected", 0),
                    "replyButtonTag": result.get("replyButtonTag", ""),
                    "replyButtonText": result.get("replyButtonText", ""),
                    "triggerSource": result.get("triggerSource", ""),
                    "matchedText": result.get("matchedText", ""),
                    "parentMarkerUid": result.get("parentMarkerUid", ""),
                    "surfaceMarkerUid": result.get("surfaceMarkerUid", ""),
                }
        except Exception as e:
            logger.debug(f"定位评论下回复按钮失败: {e}")
            return {"clicked": False, "reason": "reply_button_lookup_failed"}

    # ------------------------------------------------------------------
    # 32. _inspect_reply_activation_state
    # ------------------------------------------------------------------
    def _inspect_reply_activation_state(
        self,
        target: dict,
        detail_structure: Optional[dict] = None,
    ) -> dict:
        detail_kind = self._resolve_comment_detail_kind(detail_structure)
        payload = {
            "detailKind": detail_kind,
            "commentId": str((target or {}).get("comment_id", "") or "").strip(),
            "nickname": str((target or {}).get("nickname", "") or "").strip(),
            "commentText": str((target or {}).get("comment_text", "") or "").strip(),
            "containerSelectors": self._get_comment_composer_container_selectors(detail_kind),
        }
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 12 || rect.height < 12) return false;
                        const style = window.getComputedStyle(node);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const normalize = (value) => String(value || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const textOf = (node) => String(node?.innerText || node?.textContent || '');
                    const targetCommentId = normalize(payload.commentId);
                    const targetNickname = normalize(payload.nickname);
                    const targetText = normalize(payload.commentText);
                    const textPrefix = targetText.slice(0, 12);
                    document.querySelectorAll('[data-trae-reply-active-container], [data-trae-reply-active-editor], [data-trae-reply-active-send]').forEach((node) => {
                        node.removeAttribute('data-trae-reply-active-container');
                        node.removeAttribute('data-trae-reply-active-editor');
                        node.removeAttribute('data-trae-reply-active-send');
                    });
                    const selectors = payload.containerSelectors || [];
                    const matches = [];

                    for (const selector of selectors) {
                        for (const node of Array.from(document.querySelectorAll(selector))) {
                            if (!visible(node)) continue;
                            const text = normalize(textOf(node));
                            const html = normalize(String(node.innerHTML || ''));
                            const placeholder = normalize(
                                node.getAttribute?.('placeholder')
                                || node.getAttribute?.('aria-label')
                                || node.querySelector?.('[placeholder], [aria-label]')?.getAttribute?.('placeholder')
                                || node.querySelector?.('[placeholder], [aria-label]')?.getAttribute?.('aria-label')
                                || ''
                            );
                            const attrBag = normalize([
                                node.id || '',
                                node.className || '',
                                node.getAttribute?.('data-e2e') || '',
                                node.getAttribute?.('data-comment-id') || '',
                            ].join(' '));
                            const editor = node.querySelector?.(
                                "[contenteditable='true'], textarea, [role='textbox'], [contenteditable='plaintext-only']"
                            );
                            const sendCandidates = Array.from(
                                node.querySelectorAll?.('button, [role=\"button\"], svg, path, i, span, div') || []
                            ).filter((child) => {
                                if (!visible(child)) return false;
                                const childText = normalize(textOf(child));
                                const childClass = normalize(String(child.className || ''));
                                const childAria = normalize(child.getAttribute?.('aria-label') || '');
                                const style = window.getComputedStyle(child);
                                const color = normalize(`${style.color || ''} ${style.fill || ''} ${style.backgroundColor || ''}`);
                                return (
                                    /发送|发布|回复/.test(childText)
                                    || /send|submit|arrow/.test(childClass)
                                    || /发送|发布/.test(childAria)
                                    || /rgb\\(255, 0, 0\\)|rgb\\(254, 44, 85\\)|#fe2c55|#ff0000/.test(color)
                                );
                            });
                            const commentIdMatched = Boolean(targetCommentId && (attrBag.includes(targetCommentId) || html.includes(targetCommentId)));
                            const nicknameMatched = Boolean(targetNickname && (text.includes(targetNickname) || html.includes(targetNickname) || placeholder.includes(targetNickname)));
                            const textMatched = Boolean(targetText && (text.includes(targetText) || (textPrefix && text.includes(textPrefix))));
                            const hasReplyWord = /回复/.test(`${text} ${html} ${placeholder}`);
                            const rect = node.getBoundingClientRect();
                            matches.push({
                                node,
                                editor,
                                sendNode: sendCandidates[0] || null,
                                selector,
                                text: text.slice(0, 120),
                                placeholder: placeholder.slice(0, 80),
                                commentIdMatched,
                                nicknameMatched,
                                textMatched,
                                hasReplyWord,
                                editorFound: !!editor,
                                sendFound: sendCandidates.length > 0,
                                width: Math.round(rect.width || 0),
                                height: Math.round(rect.height || 0),
                                left: Math.round(rect.left || 0),
                                top: Math.round(rect.top || 0),
                            });
                        }
                    }

                    const best = matches.find((item) => item.commentIdMatched)
                        || matches.find((item) => item.hasReplyWord && (item.nicknameMatched || item.textMatched))
                        || matches.find((item) => item.editorFound && item.hasReplyWord)
                        || matches[0]
                        || null;
                    if (!best) {
                        return {
                            active: false,
                            detailKind: payload.detailKind || 'unknown',
                            reason: 'container_missing',
                            containerCount: 0,
                        };
                    }

                    const noteLike = payload.detailKind === 'note' || best.width >= 260;
                    const videoLike = payload.detailKind === 'video' || (best.width > 0 && best.width < 260);
                    const active = Boolean(
                        best.commentIdMatched
                        || (
                            best.hasReplyWord
                            && (
                                best.nicknameMatched
                                || best.textMatched
                                || best.editorFound
                            )
                        )
                    );
                    const uid = `trae-reply-active-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                    let editorUid = '';
                    let sendUid = '';
                    try {
                        best.node?.setAttribute?.('data-trae-reply-active-container', uid);
                    } catch (e) {}
                    try {
                        if (best.editor) {
                            best.editor.setAttribute('data-trae-reply-active-editor', uid);
                            editorUid = uid;
                        }
                    } catch (e) {}
                    try {
                        if (best.sendNode) {
                            best.sendNode.setAttribute('data-trae-reply-active-send', uid);
                            sendUid = uid;
                        }
                    } catch (e) {}
                    return {
                        active,
                        detailKind: payload.detailKind || 'unknown',
                        layoutHint: noteLike ? 'note_like' : (videoLike ? 'video_like' : 'unknown'),
                        reason: active ? 'reply_context_detected' : 'reply_context_missing',
                        containerCount: matches.length,
                        containerUid: uid,
                        editorUid,
                        sendUid,
                        best: {
                            selector: best.selector,
                            text: best.text,
                            placeholder: best.placeholder,
                            commentIdMatched: best.commentIdMatched,
                            nicknameMatched: best.nicknameMatched,
                            textMatched: best.textMatched,
                            hasReplyWord: best.hasReplyWord,
                            editorFound: best.editorFound,
                            sendFound: best.sendFound,
                            width: best.width,
                            height: best.height,
                            left: best.left,
                            top: best.top,
                        },
                    };
                }""",
                payload,
            )
            return result if isinstance(result, dict) else {"active": False, "reason": "probe_failed"}
        except Exception as e:
            logger.debug(f"检查回复态失败: {e}")
            return {"active": False, "reason": "probe_failed", "message": str(e)}

    # ------------------------------------------------------------------
    # 33. _is_reply_context_active
    # ------------------------------------------------------------------
    def _is_reply_context_active(self, target: dict, editor_locator=None, activation: Optional[dict] = None) -> bool:
        payload = {
            "commentId": str((target or {}).get("comment_id", "") or "").strip(),
            "nickname": str((target or {}).get("nickname", "") or "").strip(),
            "commentText": str((target or {}).get("comment_text", "") or "").strip(),
            "containerUid": "",
            "activationContainerUid": str((activation or {}).get("containerUid", "") or "").strip(),
            "activationActive": bool((activation or {}).get("active")),
            "activationHasReplyWord": bool(((activation or {}).get("best", {}) or {}).get("hasReplyWord")),
            "activationNicknameMatched": bool(((activation or {}).get("best", {}) or {}).get("nicknameMatched")),
            "activationTextMatched": bool(((activation or {}).get("best", {}) or {}).get("textMatched")),
            "activationCommentIdMatched": bool(((activation or {}).get("best", {}) or {}).get("commentIdMatched")),
            "activationEditorFound": bool(((activation or {}).get("best", {}) or {}).get("editorFound")),
        }
        if editor_locator is not None:
            marker_result = self._mark_comment_composer_targets(editor_locator)
            payload["containerUid"] = str((marker_result or {}).get("uid", "") or "").strip()
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const normalize = (value) => String(value || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const activationConfirmed = Boolean(
                        payload.activationActive
                        && (
                            payload.activationCommentIdMatched
                            || (
                                payload.activationHasReplyWord
                                && (
                                    payload.activationNicknameMatched
                                    || payload.activationTextMatched
                                    || payload.activationEditorFound
                                )
                            )
                        )
                    );
                    const container =
                        (payload.activationContainerUid
                            ? document.querySelector(`[data-trae-reply-active-container="${payload.activationContainerUid}"]`)
                            : null)
                        || (payload.containerUid
                            ? document.querySelector(`[data-trae-comment-container="${payload.containerUid}"]`)
                            : null)
                        || document.querySelector('#comment-input-container, [data-e2e="comment-input"], .comment-input-inner-container, .comment-input-inner, .comment-input-area, [class*="comment-input"], [class*="commentInput"], [class*="CommentInput"]');
                    if (!container) {
                        return { active: activationConfirmed, reason: activationConfirmed ? 'activation_confirmed' : 'container_missing' };
                    }
                    const text = normalize(container.innerText || container.textContent || '');
                    const html = normalize(container.innerHTML || '');
                    const attrBag = normalize([
                        container.id || '',
                        container.className || '',
                        container.getAttribute?.('data-e2e') || '',
                        container.getAttribute?.('data-comment-id') || '',
                        container.getAttribute?.('data-trae-reply-active-container') || '',
                    ].join(' '));
                    const targetCommentId = normalize(payload.commentId);
                    const targetNickname = normalize(payload.nickname);
                    const targetText = normalize(payload.commentText);
                    const hasReplyWord = text.includes('回复') || html.includes('回复');
                    const commentIdMatched = Boolean(targetCommentId && (attrBag.includes(targetCommentId) || html.includes(targetCommentId)));
                    const nicknameMatched = Boolean(targetNickname && (text.includes(targetNickname) || html.includes(targetNickname)));
                    const textMatched = Boolean(targetText && targetText.length >= 8 && text.includes(targetText.slice(0, 8)));
                    return {
                        active: Boolean(
                            activationConfirmed
                            || commentIdMatched
                            || (hasReplyWord && (nicknameMatched || textMatched))
                        ),
                        hasReplyWord,
                        nicknameMatched,
                        textMatched,
                        commentIdMatched,
                        activationConfirmed,
                    };
                }""",
                payload,
            )
            return bool(isinstance(result, dict) and result.get("active"))
        except Exception as e:
            logger.debug(f"校验评论下回复上下文失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 34. _execute_original_comment_reply_rounds
    # ------------------------------------------------------------------
    def _execute_original_comment_reply_rounds(
        self,
        *,
        target: dict,
        reply_text: str,
        aweme_id: str,
        comment_id: str,
        detail_structure,
        surface_selector: str,
        reply_diagnostic_mode: bool,
    ) -> bool:
        return execute_original_comment_reply_rounds(
            target=target,
            reply_text=reply_text,
            aweme_id=aweme_id,
            comment_id=comment_id,
            detail_structure=detail_structure,
            surface_selector=surface_selector,
            reply_diagnostic_mode=reply_diagnostic_mode,
            enrich_reply_target_with_live_comment_anchor=self._enrich_reply_target_with_live_comment_anchor,
            ensure_reply_target_context=self._ensure_reply_target_context,
            inspect_detail_page_structure=self._inspect_detail_page_structure,
            ensure_comment_surface_ready=self._ensure_comment_surface_ready,
            click_reply_button_for_comment_target=self._click_reply_button_for_comment_target,
            find_comment_editor_from_exact_dom=self._find_comment_editor_from_exact_dom,
            find_comment_editor_by_js=self._find_comment_editor_by_js,
            is_reply_context_active=self._is_reply_context_active,
            attempt_reply_stage_recovery=self._attempt_reply_stage_recovery,
            fill_comment_editor=self._fill_comment_editor,
            find_exact_comment_send_button=self._find_exact_comment_send_button,
            submit_comment_and_confirm_result=self._submit_comment_and_confirm_result,
            scroll_comment_list=self._scroll_comment_list,
            set_last_original_reply_result=self._set_last_original_reply_result,
            build_reply_diagnostics=self._build_reply_diagnostics,
            logger=logger,
            sleep_fn=time.sleep,
            random_uniform_fn=random.uniform,
        )

    # ------------------------------------------------------------------
    # 35. reply_to_original_comment
    # ------------------------------------------------------------------
    def reply_to_original_comment(self, target: dict, reply_text: str) -> bool:
        target = self._normalize_reply_target(target)
        video_url = str((target or {}).get("video_url", "") or "").strip()
        aweme_id = str((target or {}).get("aweme_id", "") or "").strip()
        comment_id = str((target or {}).get("comment_id", "") or "").strip()
        if not aweme_id or not comment_id or not str(reply_text or "").strip():
            self._set_last_original_reply_result(
                ok=False,
                reason="invalid_target",
                stage="validate",
                message="缺少评论或回复内容",
                diagnostics=self._build_reply_diagnostics(target=target),
            )
            return False

        self._set_last_original_reply_result(
            ok=False,
            reason="started",
            stage="start",
                message="开始执行评论下回复",
            diagnostics=self._build_reply_diagnostics(target=target),
        )
        logger.info(
            f"开始评论下回复执行器: aweme_id={aweme_id}, comment_id={comment_id}, "
            f"level={int((target or {}).get('comment_level', 1) or 1)}"
        )

        reply_comment_interceptor_enabled = False
        try:
            if self.comment_interceptor:
                with contextlib.suppress(Exception):
                    self.comment_interceptor.clear()
                    self.comment_interceptor.enable()
                    reply_comment_interceptor_enabled = True
            context_state = self._ensure_reply_target_context(target, allow_recover=True)
            current_aweme_id = str(context_state.get("current_aweme_id", "") or "")
            if not context_state.get("matched"):
                self._set_last_original_reply_result(
                    ok=False,
                    reason="current_video_context_mismatch",
                    stage="validate_context",
                    message=(
                        f"当前页面视频上下文不匹配，要求 aweme_id={aweme_id}，"
                        f"实际 aweme_id={current_aweme_id or 'unknown'}"
                    ),
                    diagnostics=self._build_reply_diagnostics(target=target, context_state=context_state),
                )
                logger.warning(
                    "评论下回复取消执行：当前页不是目标视频上下文，"
                    f"target_aweme_id={aweme_id}, current_aweme_id={current_aweme_id}, "
                    f"current_url={str(context_state.get('current_url', '') or '')}"
                )
                return False

            self._dismiss_login_popup_if_present()
            self._dismiss_recommended_video_if_present(max_rounds=1)
            detail_structure = self._inspect_detail_page_structure()
            surface_info = self._ensure_comment_surface_ready(detail_structure=detail_structure)
            surface_selector = str(surface_info.get("surface_selector", "") or "")
            target = self._enrich_reply_target_with_live_comment_anchor(
                target,
                aweme_id=aweme_id,
                bootstrap_timeout_seconds=1.2,
            )
            context_state = self._ensure_reply_target_context(target, allow_recover=True)
            if not context_state.get("matched"):
                self._set_last_original_reply_result(
                    ok=False,
                    reason="current_video_context_mismatch",
                    stage="validate_context",
                    message=(
                        f"评论面板准备后页面视频上下文不匹配，要求 aweme_id={aweme_id}，"
                        f"实际 aweme_id={str(context_state.get('current_aweme_id', '') or 'unknown')}"
                    ),
                    diagnostics=self._build_reply_diagnostics(target=target, context_state=context_state, surface_selector=surface_selector),
                )
                logger.warning(
                    "评论下回复取消执行：评论面板准备后帖子上下文仍不匹配，"
                    f"target_aweme_id={aweme_id}, current_aweme_id={str(context_state.get('current_aweme_id', '') or '')}"
                )
                return False
            if context_state.get("recovered"):
                detail_structure = self._inspect_detail_page_structure()
                surface_info = self._ensure_comment_surface_ready(detail_structure=detail_structure)
                surface_selector = str(surface_info.get("surface_selector", "") or surface_selector)
            return self._execute_original_comment_reply_rounds(
                target=target,
                reply_text=reply_text,
                aweme_id=aweme_id,
                comment_id=comment_id,
                detail_structure=detail_structure,
                surface_selector=surface_selector,
                reply_diagnostic_mode=bool((target or {}).get("reply_diagnostic_mode", False)),
            )
        except Exception as e:
            self._set_last_original_reply_result(
                ok=False,
                reason="execution_exception",
                stage="execute",
                message=str(e),
                diagnostics=self._build_reply_diagnostics(target=target),
            )
            logger.error(f"评论下回复执行失败: aweme_id={aweme_id}, comment_id={comment_id}, error={e}")
            return False
        finally:
            if reply_comment_interceptor_enabled and self.comment_interceptor:
                with contextlib.suppress(Exception):
                    self.comment_interceptor.disable()
                    self.comment_interceptor.clear()
