"""
PageInteractionMixin — 从 Crawler 中提取的页面交互方法集合。

所有方法通过 self 访问 Crawler 实例的属性（page, db, _stop_event 等），
保持 100% 向后兼容。
"""
import contextlib
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from src.common.utils import random_sleep
from src.douyin_bot.crawler_selectors import (
    LOGIN_MODAL_CLOSE_SELECTORS,
    RECOMMENDED_VIDEO_CLOSE_SELECTORS,
    RECOMMENDED_VIDEO_SURFACE_SELECTORS,
    _get_comment_composer_container_selectors,
)
from src.douyin_bot.human_interaction import HumanInteractionHelper
from src.infrastructure.runtime_paths import get_log_dir


class PageInteractionMixin:
    """页面弹窗处理、视频上下文管理、点赞/互动、停止信号等通用交互方法。"""

    # ---- 弹窗处理 ----

    def _dismiss_login_popup_if_present(self):
        for selector in LOGIN_MODAL_CLOSE_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=600):
                    locator.click()
                    random_sleep(0.5, 1.0)
                    logger.info(f"已关闭登录弹窗: {selector}")
                    return True
            except Exception:
                continue

        try:
            closed = self.page.evaluate(
                """
                () => {
                    const roots = Array.from(document.querySelectorAll('[id^="login-full-panel-"], [class*="login"], [class*="Login"]'));
                    const tryClick = (node) => {
                        if (!node || node.offsetParent === null) return '';
                        const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (!text) return '';
                        if (!/继续看视频|暂不登录|以后再说|稍后再说|关闭/.test(text)) return '';
                        try { node.focus?.(); } catch (e) {}
                        try { node.click?.(); } catch (e) {}
                        return text;
                    };

                    for (const root of roots) {
                        if (!root || root.offsetParent === null) continue;
                        const nodes = [root, ...Array.from(root.querySelectorAll('button, div, span'))];
                        for (const node of nodes) {
                            const clickedText = tryClick(node);
                            if (clickedText) return clickedText;
                        }
                    }
                    return '';
                }
                """
            )
            closed_text = str(closed).strip() if isinstance(closed, str) else ""
            if closed_text:
                logger.info(f"已关闭登录覆盖层: {closed_text}")
                random_sleep(0.5, 1.0)
                return True
        except Exception:
            pass

        try:
            closed = self.page.evaluate(
                """
                () => {
                    const nodes = Array.from(document.querySelectorAll('button, div, span'));
                    for (const node of nodes) {
                        const text = (node.innerText || node.textContent || '').trim();
                        if (!text) continue;
                        if (!/关闭|暂不登录|以后再说|稍后再说/.test(text)) continue;
                        if (node.offsetParent === null) continue;
                        node.click();
                        return text;
                    }
                    return '';
                }
                """
            )
            closed_text = str(closed).strip() if isinstance(closed, str) else ""
            if closed_text:
                logger.info(f"已关闭登录弹窗: {closed_text}")
                random_sleep(0.5, 1.0)
                return True
        except Exception:
            pass
        return False

    def _detect_recommended_video_popup_state(self) -> Dict[str, Any]:
        state = {
            "url": self.page.url,
            "detail_url": self._is_video_detail_popup_url(self.page.url),
            "surface_selector": "",
            "close_selector": "",
            "js_close_text": "",
        }

        try:
            fast_probe = self.page.evaluate(
                """
                ([surfaceSelectors, closeSelectors]) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    let foundSurface = "";
                    for (const sel of surfaceSelectors) {
                        if (isVisible(document.querySelector(sel))) {
                            foundSurface = sel;
                            break;
                        }
                    }

                    let foundClose = "";
                    for (const sel of closeSelectors) {
                        if (isVisible(document.querySelector(sel))) {
                            foundClose = sel;
                            break;
                        }
                    }

                    let jsText = "";
                    if (!foundClose) {
                        const nodes = Array.from(document.querySelectorAll('button, div, span, svg'));
                        for (const node of nodes) {
                            if (!isVisible(node)) continue;
                            const rect = node.getBoundingClientRect();
                            if (rect.width < 12 || rect.height < 12) continue;
                            const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                            const cls = String(node.className?.baseVal || node.className || '').toLowerCase();
                            const aria = String(node.getAttribute?.('aria-label') || '').toLowerCase();
                            if (/关闭|close|返回/.test(text) || /close|关闭|返回/.test(cls) || /close|关闭|返回/.test(aria)) {
                                jsText = text || aria || cls;
                                break;
                            }
                        }
                    }

                    return { surface: foundSurface, close: foundClose, jsText: jsText };
                }
                """,
                [RECOMMENDED_VIDEO_SURFACE_SELECTORS, RECOMMENDED_VIDEO_CLOSE_SELECTORS]
            )
            state["surface_selector"] = fast_probe.get("surface", "")
            state["close_selector"] = fast_probe.get("close", "")
            state["js_close_text"] = fast_probe.get("jsText", "")
        except Exception:
            pass

        state["active"] = bool(
            state["detail_url"]
            or state["surface_selector"]
            or state["close_selector"]
            or state["js_close_text"]
        )
        return state

    def _dismiss_recommended_video_if_present(self, max_rounds: int = 2):
        """关闭搜索后可能自动弹出的推荐视频弹窗/播放页。"""
        dismissed = False
        for _ in range(max(1, max_rounds)):
            popup_state = self._detect_recommended_video_popup_state()
            if not popup_state.get("active"):
                return dismissed

            clicked = False
            for selector in RECOMMENDED_VIDEO_CLOSE_SELECTORS:
                try:
                    locator = self.page.locator(selector).first
                    if locator.is_visible(timeout=600):
                        locator.click()
                        random_sleep(0.4, 0.8)
                        logger.info(f"已关闭推荐视频弹窗: {selector}")
                        clicked = True
                        dismissed = True
                        break
                except Exception:
                    continue

            if not clicked:
                try:
                    closed_text = self.page.evaluate(
                        """
                        () => {
                            const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                            const nodes = Array.from(document.querySelectorAll('button, div, span, svg'));
                            for (const node of nodes) {
                                const rect = node.getBoundingClientRect?.();
                                if (!rect || rect.width < 12 || rect.height < 12) continue;
                                const style = window.getComputedStyle(node);
                                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                                const text = textOf(node);
                                const cls = String(node.className?.baseVal || node.className || '').toLowerCase();
                                const aria = String(node.getAttribute?.('aria-label') || '').toLowerCase();
                                if (/关闭|close|返回/.test(text) || /close|关闭|返回/.test(cls) || /close|关闭|返回/.test(aria)) {
                                    node.click();
                                    return text || aria || cls;
                                }
                            }
                            return '';
                        }
                        """
                    )
                    if closed_text:
                        random_sleep(0.4, 0.8)
                        logger.info(f"已通过DOM脚本关闭推荐视频弹窗: {closed_text}")
                        clicked = True
                        dismissed = True
                except Exception:
                    pass

            if not clicked and popup_state.get("detail_url"):
                try:
                    self.page.keyboard.press("Escape")
                    random_sleep(0.4, 0.8)
                    logger.info("已通过 Escape 尝试关闭推荐视频/详情弹层")
                    clicked = True
                    dismissed = True
                except Exception:
                    pass

            recovered = self._recover_general_search_page_if_needed()
            if not clicked and not recovered:
                return dismissed

        return dismissed

    # ---- 搜索页面恢复 ----

    def _recover_general_search_page_if_needed(self) -> bool:
        current_url = self.page.url
        if not self._is_video_detail_popup_url(current_url):
            return False

        target_url = self._sanitize_general_search_url(self._last_general_search_url or current_url)
        if not self._is_general_search_url(target_url):
            return False

        if current_url == target_url:
            return False

        try:
            logger.info(f"检测到已进入视频详情态，回退到综合搜索页: {target_url}")
            self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            random_sleep(1.0, 2.0)
            self._wait_for_search_results_ready()
            self._remember_general_search_url()
            return True
        except Exception as e:
            logger.warning(f"回退综合搜索页失败: {e}")
            return False

    def _ensure_search_results_surface_clear(self, max_rounds: int = 3) -> bool:
        cleared = False
        for _ in range(max(1, max_rounds)):
            login_closed = self._dismiss_login_popup_if_present()
            popup_closed = self._dismiss_recommended_video_if_present(max_rounds=1)
            recovered = self._recover_general_search_page_if_needed()
            if not login_closed and not popup_closed and not recovered:
                break
            cleared = True
            random_sleep(0.4, 0.9)
        return cleared

    # ---- 视频上下文管理 ----

    def _cleanup_current_video_page_before_skip(
        self,
        reason: str = "",
        *,
        login_popup_closed: bool = False,
        escape_attempts: int = 2,
    ) -> Dict[str, Any]:
        cleanup_state = {
            "reason": str(reason or "").strip(),
            "login_popup_closed": bool(login_popup_closed),
            "escape_pressed": False,
            "current_url": str(getattr(self.page, "url", "") or "").strip(),
        }
        if not cleanup_state["login_popup_closed"]:
            with contextlib.suppress(Exception):
                cleanup_state["login_popup_closed"] = bool(self._dismiss_login_popup_if_present())
        for _ in range(max(int(escape_attempts or 0), 0)):
            try:
                self.page.keyboard.press("Escape")
                cleanup_state["escape_pressed"] = True
                time.sleep(0.05)
            except Exception:
                break
        logger.info(
            "跳过当前视频前已执行页面清理: "
            f"reason={cleanup_state['reason'] or '-'}, "
            f"login_popup_closed={cleanup_state['login_popup_closed']}, "
            f"escape_pressed={cleanup_state['escape_pressed']}, "
            f"url={cleanup_state['current_url'] or '-'}"
        )
        return cleanup_state

    def _validate_target_video_aweme_context(
        self,
        expected_aweme_id: str,
        target_url: str = "",
        stage: str = "",
    ) -> dict:
        expected_aweme_id = str(expected_aweme_id or "").strip()
        current_url = str(getattr(self.page, "url", "") or "").strip()
        current_aweme_id = self._get_current_page_aweme_id()
        state = {
            "stage": str(stage or "").strip(),
            "expected_aweme_id": expected_aweme_id,
            "current_aweme_id": current_aweme_id,
            "current_url": current_url,
            "target_url": str(target_url or "").strip(),
            "matched": bool(expected_aweme_id and current_aweme_id == expected_aweme_id),
        }
        logger.info(
            "目标视频 aweme_id 校验: "
            f"stage={state['stage'] or 'unknown'}, "
            f"expected={expected_aweme_id or '-'}, "
            f"current={current_aweme_id or '-'}, "
            f"matched={state['matched']}, "
            f"current_url={current_url or '-'}, "
            f"target_url={state['target_url'] or '-'}"
        )
        self._append_crawl_trace("video_context_validation", state)
        return state

    def _inspect_detail_page_structure(self) -> dict:
        """识别当前详情页属于普通视频还是图文详情。"""
        current_url = str(getattr(self.page, "url", "") or "")
        if "/note/" in current_url:
            return {
                "detail_kind": "note",
                "url": current_url,
                "visible_video_count": 0,
                "visible_image_count": 0,
                "note_marker_count": 0,
                "comment_candidate_count": 0,
            }
        if "/video/" in current_url:
            return {
                "detail_kind": "video",
                "url": current_url,
                "visible_video_count": 0,
                "visible_image_count": 0,
                "note_marker_count": 0,
                "comment_candidate_count": 0,
            }
        try:
            result = self.page.evaluate(
                """() => {
                    const visible = (node) => {
                        if (!node || !node.getBoundingClientRect) return false;
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) return false;
                        const style = window.getComputedStyle(node);
                        return style && style.display !== 'none' && style.visibility !== 'hidden';
                    };

                    const url = String(location.href || '');
                    const visibleVideos = Array.from(document.querySelectorAll('video')).filter(visible);
                    const visibleImages = Array.from(document.querySelectorAll('img')).filter((node) => {
                        if (!visible(node)) return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width >= 160 && rect.height >= 160;
                    });
                    const noteMarkers = Array.from(document.querySelectorAll('[data-e2e*="note"], article, [class*="note"]'))
                        .filter(visible)
                        .slice(0, 8)
                        .length;
                    const commentCandidates = Array.from(document.querySelectorAll(
                        "[data-e2e='comment-panel'], [data-e2e='comment-count'], [data-e2e='feed-comment-icon'], [data-e2e='comment-icon'], [data-e2e='note-comment-list']"
                    ))
                        .filter(visible)
                        .length;

                    let detailKind = 'unknown';
                    if (url.includes('/note/')) {
                        detailKind = 'note';
                    } else if (url.includes('/video/')) {
                        detailKind = 'video';
                    } else if (visibleVideos.length > 0) {
                        detailKind = 'video';
                    } else if (visibleImages.length >= 2 || noteMarkers > 0) {
                        detailKind = 'note';
                    }

                    return {
                        detail_kind: detailKind,
                        url,
                        visible_video_count: visibleVideos.length,
                        visible_image_count: visibleImages.length,
                        note_marker_count: noteMarkers,
                        comment_candidate_count: commentCandidates,
                    };
                }"""
            )
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.debug(f"识别详情页结构失败: {e}")
            return {"detail_kind": "unknown", "url": current_url}

    # ---- 互动页面管理 ----

    def _open_interaction_page(self):
        original_page = self.page
        interaction_page = None
        try:
            context = getattr(original_page, "context", None)
            if context is not None:
                interaction_page = context.new_page()
                with contextlib.suppress(Exception):
                    interaction_page.bring_to_front()
                self.page = interaction_page
                self._human = HumanInteractionHelper(interaction_page)
                logger.info("已创建一键互动独立标签页")
        except Exception as e:
            logger.warning(f"创建一键互动独立标签页失败，将回退复用当前页: {e}")
            interaction_page = None
            self.page = original_page
            self._human = HumanInteractionHelper(original_page)
        return original_page, interaction_page

    def _close_interaction_page(self, original_page, interaction_page) -> None:
        try:
            if interaction_page and interaction_page != original_page:
                try:
                    if not interaction_page.is_closed():
                        interaction_page.close()
                        logger.info("一键互动独立标签页已关闭")
                except Exception as e:
                    logger.warning(f"关闭一键互动独立标签页失败: {e}")
        finally:
            self.page = original_page
            if original_page is not None:
                self._human = HumanInteractionHelper(original_page)
                with contextlib.suppress(Exception):
                    if not original_page.is_closed():
                        original_page.bring_to_front()
            else:
                self._human = None

    # ---- 点赞 ----

    def _read_like_button_snapshot(self, locator) -> dict:
        try:
            return locator.evaluate(
                """(el) => {
                    const text = (node) => String(node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const attr = (name) => String(el.getAttribute?.(name) || '');
                    return {
                        text: text(el),
                        aria_pressed: attr('aria-pressed'),
                        aria_label: attr('aria-label'),
                        class_name: String(el.className || ''),
                        html: String(el.outerHTML || '').slice(0, 800),
                    };
                }"""
            )
        except Exception as e:
            return {"error": str(e)}

    def _click_video_like_button(self) -> bool:
        helper = self._human_helper()
        like_selectors = [
            "[data-e2e='video-player-digg']",
            "[data-e2e*='digg']",
            "button[aria-label*='赞']",
            "[role='button'][aria-label*='赞']",
            "[class*='digg']",
            "[class*='like']",
        ]
        for selector in like_selectors:
            try:
                locators = self.page.locator(selector)
                count = min(locators.count(), 8)
                for idx in range(count):
                    locator = locators.nth(idx)
                    if not locator.is_visible(timeout=800):
                        continue
                    before = self._read_like_button_snapshot(locator)
                    logger.info(f"尝试点击点赞按钮: selector={selector} before={before}")
                    clicked = helper.click_target(locator, pre_hover=True)
                    if not clicked:
                        try:
                            locator.click(timeout=1500, force=True)
                            clicked = True
                        except Exception as click_error:
                            logger.debug(f"点赞按钮强制点击失败({selector}): {click_error}")
                    if not clicked:
                        continue
                    time.sleep(1)
                    after = self._read_like_button_snapshot(locator)
                    logger.info(f"点赞按钮点击后状态: selector={selector} after={after}")
                    if before != after:
                        return True
                    if str(after.get("aria_pressed", "")).lower() == "true":
                        return True
            except Exception as e:
                logger.debug(f"点赞候选检查失败({selector}): {e}")
                continue
        return False

    # ---- 等待与停止信号 ----

    def _interruptible_sleep(self, total_seconds: float, *, chunk_seconds: float = 0.2) -> bool:
        deadline = time.monotonic() + max(float(total_seconds or 0.0), 0.0)
        step = max(float(chunk_seconds or 0.0), 0.05)
        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining <= 0:
                return not self._stop_event.is_set()
            if self._stop_event.is_set():
                return False
            time.sleep(min(step, remaining))

    def _human_helper(self) -> HumanInteractionHelper:
        helper = getattr(self, "_human", None)
        if helper is None:
            helper = HumanInteractionHelper(self.page)
            self._human = helper
        return helper

    # ---- 一键互动 ----

    def perform_one_click_interact(self, sec_uid: str, comment_text: str) -> bool:
        """执行一键三联互动 (关注 + 第一个视频点赞 + 发送评论)"""
        if not sec_uid:
            return False

        profile_url = f"https://www.douyin.com/user/{sec_uid}"
        logger.info(f"正在开始一键互动: 用户={sec_uid}, URL={profile_url}")

        original_page, interaction_page = self._open_interaction_page()
        try:
            # 1. 进入主页
            self.page.goto(profile_url, wait_until="domcontentloaded")
            time.sleep(2)
            self._dismiss_login_popup_if_present()

            # 2. 执行关注 (如果未关注)
            try:
                follow_locator = self.page.locator("button:has-text('关注')").first
                if follow_locator.is_visible(timeout=2000):
                    text = follow_locator.text_content().strip()
                    if text == "关注":
                        logger.info(f"点击关注用户: {sec_uid}")
                        follow_locator.click()
                        time.sleep(1)
            except Exception as e:
                logger.debug(f"关注动作跳过或执行失败 (可能已关注): {e}")

            # 3. 寻找第一个视频并进入
            try:
                video_locator = self.page.locator("[data-e2e='user-post-list'] a").first
                if not video_locator.is_visible(timeout=3000):
                    video_locator = self.page.locator("a[href*='/video/']").first

                if not video_locator.is_visible(timeout=2000):
                    logger.warning(f"未找到用户的公开视频，互动任务终止: {sec_uid}")
                    return False

                video_url = video_locator.get_attribute("href")
                if video_url and not video_url.startswith("http"):
                    video_url = f"https://www.douyin.com{video_url}"

                logger.info(f"进入第一个视频执行点赞评论: {video_url}")
                self.page.goto(video_url, wait_until="domcontentloaded")
                time.sleep(2)
                self._dismiss_login_popup_if_present()

                interaction_ok = False

                # 4. 执行点赞
                try:
                    if self._click_video_like_button():
                        logger.info("已执行点赞动作")
                    else:
                        logger.warning("未确认点赞成功，可能未命中真实可交互层")
                except Exception as e:
                    logger.warning(f"点赞动作执行失败: {e}")

                # 5. 执行评论
                try:
                    detail_structure = self._inspect_detail_page_structure()
                    self._open_comment_surface(detail_structure=detail_structure)
                    time.sleep(2.0)

                    self._activate_comment_input()
                    time.sleep(1.5)

                    comment_locator = self._find_comment_editor_from_exact_dom()

                    if comment_locator:
                        logger.info(f"找到评论输入框，准备输入: {comment_text}")
                        helper = self._human_helper()

                        try:
                            comment_locator.evaluate(
                                """el => {
                                    try { el.focus(); } catch(e) {}
                                    try { el.scrollIntoView({block: 'center'}); } catch(e) {}
                                }"""
                            )
                        except Exception:
                            pass
                        time.sleep(0.2)

                        input_ok = self._fill_comment_editor(comment_locator, comment_text)
                        if not input_ok:
                            logger.warning("评论输入失败，终止发送流程")
                            return False

                        time.sleep(0.8)

                        send_button = self._find_exact_comment_send_button(comment_locator)

                        if send_button:
                            if not helper.click_target(send_button, pre_hover=True):
                                send_button.click(timeout=1500, force=True)
                            logger.info("已点击评论发送按钮")
                            time.sleep(1.2)
                            if self._verify_comment_submit_success(comment_locator, comment_text):
                                interaction_ok = True
                            else:
                                logger.warning("评论发送按钮已点击，但未通过发送后校验")
                        else:
                            logger.warning("未找到精准评论发送按钮，终止发送流程")
                            return False

                        time.sleep(2)
                        logger.info(f"评论提交流程已执行: {comment_text}")
                        return interaction_ok

                    logger.warning(f"未找到评论输入框，互动任务不完整 (URL: {video_url})")
                except Exception as e:
                    logger.warning(f"评论动作执行失败: {e}")

            except Exception as e:
                logger.error(f"处理视频页面失败: {e}")

            return False
        except Exception as e:
            logger.error(f"一键互动过程出错: {e}")
            return False
        finally:
            self._cleanup_comment_composer_marks()
            self._close_interaction_page(original_page, interaction_page)

    # ---- 视频信息爬取 ----

    def crawl_video_info(self, video_url: str) -> dict:
        """
        爬取视频详细信息

        Args:
            video_url: 视频URL

        Returns:
            dict: 视频信息
        """
        video_info = {}

        def handle_video_response(response):
            try:
                if "aweme/v1/web/aweme/detail" in response.url:
                    if response.status == 200:
                        try:
                            data = response.json()
                        except Exception:
                            return
                        aweme_detail = data.get("aweme_detail", {})

                        video_info.update({
                            "aweme_id": aweme_detail.get("aweme_id", ""),
                            "title": aweme_detail.get("desc", ""),
                            "author": aweme_detail.get("author", {}).get("nickname", ""),
                            "author_id": aweme_detail.get("author", {}).get("sec_uid", ""),
                            "like_count": aweme_detail.get("statistics", {}).get("digg_count", 0),
                            "comment_count": aweme_detail.get("statistics", {}).get("comment_count", 0),
                            "share_count": aweme_detail.get("statistics", {}).get("share_count", 0),
                            "play_count": aweme_detail.get("statistics", {}).get("play_count", 0),
                            "create_time": aweme_detail.get("create_time", 0)
                        })

            except Exception as e:
                logger.error(f"处理视频详情响应失败: {e}")

        self.page.on("response", handle_video_response)

        try:
            self.page.goto(video_url, wait_until='domcontentloaded', timeout=30000)
            random_sleep(3, 5)

        except Exception as e:
            logger.error(f"爬取视频信息失败: {e}")
        finally:
            self.page.remove_listener("response", handle_video_response)

        return video_info

    # ---- 评论详情类型 / URL 推断 ----

    def _resolve_comment_detail_kind(self, detail_structure: Optional[dict] = None) -> str:
        if isinstance(detail_structure, dict):
            detail_kind = str(detail_structure.get("detail_kind", "") or "").strip().lower()
            if detail_kind in {"note", "video"}:
                return detail_kind
        current_url = str(getattr(self.page, "url", "") or "").strip().lower()
        if "/note/" in current_url:
            return "note"
        if "/video/" in current_url:
            return "video"
        inspected = self._inspect_detail_page_structure()
        detail_kind = str((inspected or {}).get("detail_kind", "") or "").strip().lower()
        return detail_kind if detail_kind in {"note", "video"} else "unknown"

    @staticmethod
    def _infer_detail_kind_from_url(url: str) -> str:
        normalized = str(url or "").lower()
        if "/note/" in normalized:
            return "note"
        if "/video/" in normalized:
            return "video"
        return ""

    @classmethod
    def _build_reply_target_detail_url(cls, target: dict) -> str:
        target = target if isinstance(target, dict) else {}
        video_url = str(target.get("video_url", "") or "").strip()
        aweme_id = str(target.get("aweme_id", "") or "").strip()
        target_kind = (
            str(target.get("detail_kind", "") or "").strip().lower()
            or str(target.get("content_type", "") or "").strip().lower()
            or cls._infer_detail_kind_from_url(video_url)
        )
        if target_kind not in {"video", "note"}:
            target_kind = "note" if str(target.get("is_note", "") or "").strip().lower() in {"1", "true", "yes"} else "video"
        if video_url:
            extracted_aweme_id = cls._extract_aweme_id_from_candidate_value(video_url)
            if extracted_aweme_id and (not aweme_id or extracted_aweme_id == aweme_id):
                detail_kind = cls._infer_detail_kind_from_url(video_url) or target_kind
                if detail_kind in {"video", "note"}:
                    return f"https://www.douyin.com/{detail_kind}/{extracted_aweme_id}"
                return video_url
        if not aweme_id:
            return ""
        return f"https://www.douyin.com/{target_kind}/{aweme_id}"

    def _recover_reply_target_context(self, target: dict, expected_aweme_id: str) -> bool:
        target_url = self._build_reply_target_detail_url(target)
        if not target_url or not expected_aweme_id:
            return False
        try:
            logger.info(
                "检测到评论下回复帖子上下文漂移，尝试重新进入目标帖子: "
                f"aweme_id={expected_aweme_id}, target_url={target_url}"
            )
            self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            random_sleep(0.8, 1.6)
            self._dismiss_login_popup_if_present()
            self._dismiss_recommended_video_if_present(max_rounds=1)
            recovered_aweme_id = self._get_current_page_aweme_id()
            return recovered_aweme_id == expected_aweme_id
        except Exception as e:
            logger.warning(
                "恢复评论下回复目标帖子上下文失败: "
                f"aweme_id={expected_aweme_id}, target_url={target_url}, error={e}"
            )
            return False

    def _ensure_reply_target_context(self, target: dict, allow_recover: bool = True) -> dict:
        target = target if isinstance(target, dict) else {}
        expected_aweme_id = str(target.get("aweme_id", "") or "").strip()
        current_aweme_id = self._get_current_page_aweme_id()
        current_url = str(getattr(self.page, "url", "") or "").strip()
        state = {
            "matched": bool(expected_aweme_id and current_aweme_id == expected_aweme_id),
            "expected_aweme_id": expected_aweme_id,
            "current_aweme_id": current_aweme_id,
            "current_url": current_url,
            "target_url": self._build_reply_target_detail_url(target),
            "recovered": False,
        }
        if state["matched"] or not allow_recover or not expected_aweme_id:
            return state
        with contextlib.suppress(Exception):
            self._dismiss_login_popup_if_present()
        with contextlib.suppress(Exception):
            self._dismiss_recommended_video_if_present(max_rounds=1)
        current_aweme_id = self._get_current_page_aweme_id()
        current_url = str(getattr(self.page, "url", "") or "").strip()
        if current_aweme_id == expected_aweme_id:
            state.update(
                {
                    "matched": True,
                    "current_aweme_id": current_aweme_id,
                    "current_url": current_url,
                }
            )
            return state
        recovered = self._recover_reply_target_context(target, expected_aweme_id)
        current_aweme_id = self._get_current_page_aweme_id()
        current_url = str(getattr(self.page, "url", "") or "").strip()
        state.update(
            {
                "matched": bool(current_aweme_id == expected_aweme_id),
                "current_aweme_id": current_aweme_id,
                "current_url": current_url,
                "recovered": bool(recovered),
            }
        )
        return state

    # ---- aweme_id / 不可用视频检测 ----

    def _get_current_page_aweme_id(self) -> str:
        try:
            current_url = str(getattr(self.page, "url", "") or "").strip()
        except Exception:
            current_url = ""
        return self._extract_aweme_id(current_url)

    @staticmethod
    def _match_unavailable_video_text(text: Any) -> str:
        normalized = re.sub(r"\s+", "", str(text or "")).strip()
        if not normalized:
            return ""
        pattern = (
            r"视频不存在|你要观看的视频不存在|该视频已被删除|视频已失效|该视频不可见|"
            r"视频已隐藏|暂时无法查看|作者已设置仅自己可见|作品状态异常|该视频无法查看"
        )
        return str(text or "").strip() if re.search(pattern, normalized) else ""

    # ---- 爬取追踪 ----

    @staticmethod
    def _append_crawl_trace(event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        try:
            log_dir = get_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            trace_path = log_dir / "crawl_video_trace.jsonl"
            entry = {
                "timestamp": datetime.now().isoformat(),
                "event": str(event or "").strip() or "unknown",
                "payload": dict(payload or {}),
            }
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug(f"写入 crawl_video_trace 失败: {exc}")

    # ---- 停止信号 ----

    def set_stop_flag(self, stop: bool = True):
        """设置停止标志（线程安全）"""
        if stop:
            self._stop_event.set()
        else:
            self._stop_event.clear()
        logger.info(f"爬虫停止标志设置为: {stop}")

    def is_stopped(self) -> bool:
        """检查是否已停止（线程安全）"""
        stop_event = getattr(self, "_stop_event", None)
        return bool(stop_event.is_set()) if stop_event is not None else False

    def _sleep_with_stop_check(self, total_seconds: float, step_seconds: float = 0.1) -> bool:
        """短分片等待，便于 stop 信号尽快中断当前页面流程。"""
        deadline = time.monotonic() + max(float(total_seconds or 0.0), 0.0)
        step = min(max(float(step_seconds or 0.1), 0.02), 0.5)
        while time.monotonic() < deadline:
            if self.is_stopped():
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(step, max(remaining, 0.0)))
        return not self.is_stopped()

    # ---- 视频列表 / 搜索数据访问 ----

    def get_video_list(self) -> list:
        """
        获取当前视频列表

        Returns:
            list: 视频URL列表
        """
        return self.current_video_urls

    def get_search_data(self) -> list:
        """
        获取搜索结果详情

        Returns:
            list: 搜索结果详情列表
        """
        return self._search_data
