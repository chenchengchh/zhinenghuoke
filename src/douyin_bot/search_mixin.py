"""
SearchMixin — 从 Crawler 中提取的搜索功能域方法集合。

所有方法通过 self 访问 Crawler 实例的属性（page, db, _stop_event,
search_interceptor 等），保持 100% 向后兼容。
"""
import contextlib
import json
import math
import random
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.common.utils import random_sleep
from src.douyin_bot.crawler_selectors import (
    SEARCH_BUTTON_SELECTORS,
    SEARCH_GENERAL_TAB_SELECTORS,
    SEARCH_INPUT_SELECTORS,
    SEARCH_LAYOUT_CONTROL_SELECTORS,
    SEARCH_LAYOUT_MULTI_COLUMN_TEXTS,
    SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS,
    SEARCH_RESULT_WAIT_SELECTORS,
)


class SearchMixin:
    """搜索关键词、综合页布局管理、搜索结果解析与消费等方法。"""

    # 搜索配置常量（与 Crawler 类定义保持一致）
    DOUYIN_HOME_URL = "https://www.douyin.com/"
    DEFAULT_SEARCH_TARGET_COUNT = 300
    DEFAULT_SEARCH_MAX_SCROLLS = 80
    SEARCH_IDLE_STOP_ROUNDS = 3
    SEARCH_IDLE_STOP_MIN_RESPONSES = 1

    # ---- 搜索入口 ----

    def search_keyword(self, keyword: str, max_results: int = 0) -> list:
        """
        通过关键词搜索视频

        使用API拦截方式获取搜索结果，比DOM解析更可靠

        Args:
            keyword: 搜索关键词
            max_results: 需要发现的视频上限，0 表示按默认搜索策略尽量多找

        Returns:
            list: 视频URL列表
        """
        for _ in self.search_keyword_stream(keyword, max_results=max_results):
            pass

        return self.current_video_urls

    def search_keyword_stream(self, keyword: str, max_results: int = 0):
        """
        流式搜索关键词。

        每发现一个新的相关视频就立即 yield，调用方可边发现边执行评论抓取，
        无需先等待综合页把所有候选结果都扫完。
        """
        logger.info(f"开始流式搜索关键词: {keyword}, max_results={max_results or 'auto'}")
        self.current_video_urls = []
        self._search_data = []
        self._search_response_count = 0
        self._last_search_interceptor_stats = {
            "search_response_batches": 0,
            "pending_search_batches": 0,
            "peak_pending_search_batches": 0,
            "dropped_search_batches_count": 0,
            "latest_search_batch_id": 0,
            "latest_search_batch_url": "",
            "discovered_video_count": 0,
        }
        target_result_count = max(0, int(max_results or 0))
        pending_entries: List[Dict[str, Any]] = []

        search_interceptor_enabled = False
        try:
            if self.search_interceptor:
                self.search_interceptor.clear()
                self.search_interceptor.enable()
                search_interceptor_enabled = True

            try:
                self._search_keyword_via_url(keyword)
            except Exception as direct_search_error:
                logger.warning(f"直达搜索结果页失败，回退首页搜索流程: {direct_search_error}")
                search_started = self._search_keyword_via_home(keyword)
                if not search_started:
                    logger.warning("首页搜索流程未稳定命中，再次回退到搜索URL兜底")
                    self._search_keyword_via_url(keyword)
            if self.is_stopped():
                logger.info("搜索阶段收到停止信号，终止继续发现视频")
                return

            # 检查并关闭可能自动打开的推荐视频
            self._dismiss_recommended_video_if_present()
            self._wait_for_search_bootstrap(timeout_seconds=1.0)
            self._consume_search_api_batches(pending_entries, target_result_count)

            # ---- 智能诊断：首屏无数据时自动检测弹窗/布局问题并修复 ----
            if not pending_entries and not self.current_video_urls:
                logger.warning("[智能体] 首屏无视频数据，启动智能诊断修复...")
                fix_applied = self._intelligent_diagnose_and_fix("first_page_empty")
                if fix_applied:
                    # 修复后重新消费
                    self._consume_search_api_batches(pending_entries, target_result_count)
                    for entry in self._drain_pending_search_entries(pending_entries, search_target_count):
                        if self.is_stopped():
                            break
                        yielded_count += 1
                        yield entry

            yielded_count = 0
            idle_rounds = 0
            search_target_count, search_max_scrolls = self._resolve_search_discovery_limits(target_result_count)

            # 先消费首屏/首批接口结果，再决定是否继续下滚。
            for entry in self._drain_pending_search_entries(pending_entries, search_target_count):
                if self.is_stopped():
                    logger.info("搜索首屏发现阶段收到停止信号，终止继续输出候选视频")
                    break
                yielded_count += 1
                yield entry
            previous_count = len(self.current_video_urls)
            previous_response_count = self._search_response_count

            while not self._stop_event.is_set():
                if search_target_count and len(self.current_video_urls) >= search_target_count:
                    logger.info(f"综合页搜索已达到目标视频数 {search_target_count}，停止继续发现")
                    break
                if search_max_scrolls <= 0:
                    break

                # 每次滚动前检查一下是否弹出了推荐视频，如果有则关闭，防止遮挡或阻碍滚动
                self._ensure_search_results_surface_clear()

                self.page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 900))")
                if not self._interruptible_sleep(random.uniform(0.8, 1.4), chunk_seconds=0.12):
                    logger.info("搜索滚动等待阶段收到停止信号，终止继续发现视频")
                    break
                self._ensure_search_results_surface_clear()
                search_max_scrolls -= 1
                self._consume_search_api_batches(pending_entries, target_result_count)

                drained = list(self._drain_pending_search_entries(pending_entries, search_target_count))
                current_count = len(self.current_video_urls)
                current_response_count = self._search_response_count
                if drained:
                    idle_rounds = 0
                    logger.info(f"综合页继续发现新视频: {len(self.current_video_urls)} 个")
                    for entry in drained:
                        if self.is_stopped():
                            logger.info("搜索结果输出阶段收到停止信号，终止继续分发候选视频")
                            break
                        yielded_count += 1
                        yield entry
                    if self.is_stopped():
                        break
                else:
                    idle_rounds = self._update_search_idle_rounds(
                        previous_video_count=previous_count,
                        current_video_count=current_count,
                        previous_response_count=previous_response_count,
                        current_response_count=current_response_count,
                        drained_count=0,
                        current_idle_rounds=idle_rounds,
                    )
                    if current_response_count > previous_response_count:
                        logger.info(
                            "综合页本轮虽然没有新增视频，但搜索接口仍在返回数据，继续等待更多结果..."
                        )
                    elif current_count > previous_count:
                        logger.info("综合页本轮视频数有增长，继续搜索")
                    else:
                        logger.info(f"综合页本轮无新增视频，空转轮次 {idle_rounds}/4")
                        # ---- 智能修复：连续空转时自动检测并修复弹窗/布局问题 ----
                        if idle_rounds >= 2 and current_count == previous_count:
                            logger.warning("[智能体] 连续空转，启动滚动阶段智能诊断修复...")
                            fix_applied = self._intelligent_diagnose_and_fix("scroll_idle")
                            if fix_applied:
                                idle_rounds = 0  # 修复后重置空转计数
                    if not self._should_continue_search_after_idle(
                        idle_rounds,
                        self._search_response_count,
                    ):
                        logger.info("综合页连续多轮无新增结果，停止继续下滚")
                        break

                previous_count = len(self.current_video_urls)
                previous_response_count = self._search_response_count
                if yielded_count and yielded_count % 6 == 0:
                    pause = random.uniform(0.4, 0.9)
                    logger.info(f"搜索滚动短暂停顿 {pause:.1f} 秒")
                    if not self._interruptible_sleep(pause, chunk_seconds=0.12):
                        logger.info("搜索滚动停顿阶段收到停止信号，终止继续发现视频")
                        break

            logger.info(f"API拦截共找到 {len(self.current_video_urls)} 个视频")
            self._sync_search_interceptor_stats()
            for i, video in enumerate(self._search_data[:5]):
                logger.info(f"  [{i+1}] {video['author']}: {video['title'][:50]}... (点赞:{video['like_count']}, 评论:{video['comment_count']})")
        except Exception as e:
            logger.error(f"搜索过程出错: {e}")
        finally:
            if search_interceptor_enabled and self.search_interceptor:
                self._sync_search_interceptor_stats()
                self.search_interceptor.disable()
                self.search_interceptor.clear()

    # ---- 搜索入口辅助 ----

    def _search_keyword_via_home(self, keyword: str) -> bool:
        """优先使用首页搜索框执行搜索，再切到综合页。"""
        logger.info("使用首页搜索框执行抖音搜索")
        try:
            self.page.goto(self.DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            if not self._interruptible_sleep(random.uniform(2.0, 4.0), chunk_seconds=0.15):
                return False
            self._dismiss_login_popup_if_present()
            if self.is_stopped():
                return False

            search_input = self._find_visible_locator(SEARCH_INPUT_SELECTORS, timeout=12000)
            if not search_input:
                logger.warning("首页未找到搜索输入框")
                return False

            self._human_type_locator(search_input, keyword)
            try:
                typed_value = search_input.input_value(timeout=2000)
                logger.info(f"首页搜索框当前值: {typed_value}")
            except Exception as input_value_e:
                logger.debug(f"读取首页搜索框当前值失败: {input_value_e}")
            if not self._interruptible_sleep(random.uniform(0.6, 1.2), chunk_seconds=0.12):
                return False

            current_url = self.page.url
            submitted = self._submit_search_from_input()
            if not submitted:
                return False
            if not self._wait_for_search_context_change(current_url):
                if submitted:
                    logger.info("首次提交后未明显进入搜索结果，尝试二次回车兜底")
                    self.page.keyboard.press("Enter")
                    if not self._interruptible_sleep(random.uniform(0.8, 1.5), chunk_seconds=0.12):
                        return False
                if not self._wait_for_search_context_change(current_url, timeout_seconds=4.0):
                    logger.warning("首页搜索未跳转到结果页")
                    return False

            self._ensure_search_results_surface_clear()
            self._dismiss_login_popup_if_present()
            self._prepare_general_search_results_page()
            if not self._is_general_search_url(self.page.url):
                logger.warning(f"搜索后当前URL不是综合结果页: {self.page.url}")
                return False
            return True
        except Exception as e:
            logger.warning(f"首页搜索流程失败: {e}")
            return False

    def _search_keyword_via_url(self, keyword: str):
        """优先直达综合搜索结果页，减少首页搜索框交互带来的额外等待。"""
        search_url = f"https://www.douyin.com/search/{urllib.parse.quote(keyword)}?type=general"
        logger.info(f"直达访问搜索页面: {search_url}")
        self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        if not self._interruptible_sleep(random.uniform(0.8, 1.6), chunk_seconds=0.12):
            return
        self._dismiss_login_popup_if_present()
        self._prepare_general_search_results_page()

    def _find_visible_locator(self, selectors: List[str], timeout: int = 3000):
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=timeout):
                    return locator
            except Exception:
                continue
        return None

    def _human_type_locator(self, locator, text: str):
        helper = self._human_helper()
        expected = self._normalize_editor_text(text)

        def _read_current_text() -> str:
            return self._normalize_editor_text(self._read_editor_text(locator))

        def _write_direct(value: str) -> None:
            locator.evaluate(
                """(el, newValue) => {
                    if (!el) return;
                    const target =
                        (el.matches && el.matches('input, textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]') ? el : null)
                        || el.querySelector?.('input, textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]')
                        || el;
                    try { target.focus(); } catch (e) {}
                    if (target && target.isContentEditable) {
                        if (typeof target.replaceChildren === 'function') {
                            target.replaceChildren(document.createTextNode(newValue));
                        } else {
                            target.textContent = newValue;
                        }
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
                                (el.matches && el.matches('input, textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]') ? el : null)
                                || el.querySelector?.('input, textarea, [contenteditable="true"], [contenteditable], [role="textbox"], [role="combobox"]')
                                || el;
                            return !!(target && target.isContentEditable);
                        }"""
                    )
                )
            except Exception:
                return False

        def _focus_and_clear() -> None:
            locator.click()
            random_sleep(0.2, 0.5)
            try:
                locator.press("Control+A")
            except Exception:
                self.page.keyboard.press("Control+A")
            random_sleep(0.1, 0.2)
            with contextlib.suppress(Exception):
                locator.press("Backspace")
            with contextlib.suppress(Exception):
                self.page.keyboard.press("Backspace")
            random_sleep(0.2, 0.4)

        if helper.clear_and_type(locator, text) and _read_current_text() == expected:
            return

        logger.warning(
            f"搜索框拟人输入后内容不匹配，准备回退写入: expected={expected!r}, actual={_read_current_text()!r}"
        )
        _focus_and_clear()

        if _is_contenteditable_target():
            try:
                self.page.keyboard.insert_text(text)
                random_sleep(0.1, 0.2)
                if _read_current_text() == expected:
                    logger.info("搜索框已通过 keyboard.insert_text 成功写入关键词")
                    return
            except Exception as e:
                logger.debug(f"搜索框 keyboard.insert_text 写入失败: {e}")
                _focus_and_clear()

        try:
            locator.fill(text, timeout=2500)
            random_sleep(0.1, 0.2)
            if _read_current_text() == expected:
                logger.info("搜索框已通过 fill 成功写入关键词")
                return
        except Exception as e:
            logger.debug(f"搜索框 fill 写入失败: {e}")

        _write_direct(text)
        random_sleep(0.1, 0.2)
        if _read_current_text() == expected:
            logger.info("搜索框已通过 DOM 直写成功写入关键词")
            return

        helper.press_sequentially(text, base_delay=(80, 140), typo_chance=0.06)

    # ---- 搜索上下文切换 ----

    def _wait_for_search_context_change(self, previous_url: str, timeout_seconds: float = 8.0) -> bool:
        deadline = time.time() + max(timeout_seconds, 1.0)
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            current_url = self.page.url
            if current_url != previous_url and "/search" in current_url:
                return True
            if self._has_visible_search_results():
                return True
            if not self._interruptible_sleep(0.4, chunk_seconds=0.1):
                return False
        return False

    def _submit_search_from_input(self) -> bool:
        """优先点击搜索按钮提交，避免仅回车时仍停留首页。"""
        search_button = self._find_visible_locator(SEARCH_BUTTON_SELECTORS, timeout=2500)
        if search_button:
            try:
                if not self._human_helper().click_target(search_button):
                    search_button.click()
                return self._interruptible_sleep(random.uniform(0.8, 1.5), chunk_seconds=0.12)
            except Exception as e:
                logger.debug(f"点击搜索按钮失败，回退键盘提交: {e}")

        try:
            self.page.keyboard.press("Enter")
            return self._interruptible_sleep(random.uniform(0.8, 1.5), chunk_seconds=0.12)
        except Exception as e:
            logger.warning(f"搜索提交失败: {e}")
            return False

    # ---- URL / 内容类型判断 ----

    @staticmethod
    def _is_general_search_url(url: str) -> bool:
        normalized = (url or "").lower()
        return "/search/" in normalized and "type=general" in normalized

    @staticmethod
    def _is_video_detail_popup_url(url: str) -> bool:
        normalized = (url or "").lower()
        if not normalized:
            return False
        if "/video/" in normalized or "/note/" in normalized:
            return True
        return "modal_id=" in normalized

    @staticmethod
    def _is_note_aweme(aweme_info: dict) -> bool:
        """根据 aweme 数据判断是否为图文内容。"""
        if not isinstance(aweme_info, dict):
            return False

        if aweme_info.get("images") or aweme_info.get("image_infos") or aweme_info.get("image_post_info"):
            return True

        aweme_type = str(aweme_info.get("aweme_type", "") or aweme_info.get("awemeType", "")).strip()
        if aweme_type in {"68", "150"}:
            return True

        return False

    @classmethod
    def _build_aweme_detail_url(cls, aweme_info: dict) -> str:
        aweme_id = str((aweme_info or {}).get("aweme_id", "") or "").strip()
        if not aweme_id:
            return ""
        detail_kind = "note" if cls._is_note_aweme(aweme_info) else "video"
        return f"https://www.douyin.com/{detail_kind}/{aweme_id}"

    @staticmethod
    def _is_supported_aweme_id(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        import re as _re
        if not _re.fullmatch(r"[0-9A-Za-z_-]{8,64}", text):
            return False
        return bool(_re.search(r"\d", text))

    @staticmethod
    def _extract_aweme_id_from_candidate_value(value: Any) -> str:
        import re as _re
        text = str(value or "").strip()
        if not text:
            return ""
        if text.isdigit():
            return text

        for pattern in (
            r"/video/([0-9A-Za-z_-]+)",
            r"/note/([0-9A-Za-z_-]+)",
            r"[?&]aweme_id=([0-9A-Za-z_-]+)",
            r"[?&]modal_id=([0-9A-Za-z_-]+)",
        ):
            match = _re.search(pattern, text)
            if match:
                return str(match.group(1) or "").strip()
        return ""

    # ---- 搜索结果 aweme_id 解析 ----

    @classmethod
    def _iter_search_result_aweme_id_candidates(cls, item: dict, aweme_info: dict):
        aweme_info = aweme_info if isinstance(aweme_info, dict) else {}
        item = item if isinstance(item, dict) else {}
        video_info = item.get("video") if isinstance(item.get("video"), dict) else {}
        card_item = item.get("card_item") if isinstance(item.get("card_item"), dict) else {}
        card_aweme_info = card_item.get("aweme_info") if isinstance(card_item.get("aweme_info"), dict) else {}
        candidate_specs = [
            ("aweme_info.aweme_id", aweme_info.get("aweme_id")),
            ("item.aweme_id", item.get("aweme_id")),
            ("card_item.aweme_info.aweme_id", card_aweme_info.get("aweme_id")),
            ("video.aweme_id", video_info.get("aweme_id")),
            ("aweme_info.share_url", aweme_info.get("share_url")),
            (
                "aweme_info.share_info.share_url",
                aweme_info.get("share_info", {}).get("share_url") if isinstance(aweme_info.get("share_info"), dict) else "",
            ),
            ("aweme_info.schema", aweme_info.get("schema")),
            ("aweme_info.url", aweme_info.get("url")),
            ("item.share_url", item.get("share_url")),
            ("item.detail_url", item.get("detail_url")),
            ("item.url", item.get("url")),
            ("item.web_url", item.get("web_url")),
            ("item.jump_url", item.get("jump_url")),
            ("video.share_url", video_info.get("share_url")),
            ("video.url", video_info.get("url")),
        ]
        for source, candidate in candidate_specs:
            yield source, candidate

    @staticmethod
    def _summarize_search_candidate_value(value: Any, *, limit: int = 120) -> str:
        import re as _re
        text = _re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    @classmethod
    def _build_search_aweme_resolution_diagnostics(cls, item: dict, aweme_info: dict) -> List[Dict[str, str]]:
        diagnostics: List[Dict[str, str]] = []
        for source, candidate in cls._iter_search_result_aweme_id_candidates(item, aweme_info):
            candidate_preview = cls._summarize_search_candidate_value(candidate)
            resolved = cls._extract_aweme_id_from_candidate_value(candidate)
            if not candidate_preview and not resolved:
                continue
            diagnostics.append(
                {
                    "source": source,
                    "candidate": candidate_preview,
                    "resolved_aweme_id": resolved,
                }
            )
        return diagnostics

    @classmethod
    def _build_search_result_item_snapshot(cls, item: dict, aweme_info: dict) -> Dict[str, Any]:
        item = item if isinstance(item, dict) else {}
        aweme_info = aweme_info if isinstance(aweme_info, dict) else {}
        card_item = item.get("card_item") if isinstance(item.get("card_item"), dict) else {}
        card_aweme_info = card_item.get("aweme_info") if isinstance(card_item.get("aweme_info"), dict) else {}
        video_info = item.get("video") if isinstance(item.get("video"), dict) else {}
        author = aweme_info.get("author") if isinstance(aweme_info.get("author"), dict) else {}
        stats = aweme_info.get("statistics") if isinstance(aweme_info.get("statistics"), dict) else {}

        return {
            "item_type": item.get("type"),
            "item_keys": sorted(str(key) for key in item.keys())[:20],
            "aweme_info_keys": sorted(str(key) for key in aweme_info.keys())[:20],
            "author_nickname": cls._summarize_search_candidate_value(author.get("nickname", ""), limit=40),
            "desc": cls._summarize_search_candidate_value(aweme_info.get("desc", ""), limit=80),
            "aweme_info_aweme_id": cls._summarize_search_candidate_value(aweme_info.get("aweme_id", ""), limit=40),
            "item_aweme_id": cls._summarize_search_candidate_value(item.get("aweme_id", ""), limit=40),
            "card_aweme_id": cls._summarize_search_candidate_value(card_aweme_info.get("aweme_id", ""), limit=40),
            "share_url": cls._summarize_search_candidate_value(aweme_info.get("share_url", ""), limit=120),
            "schema": cls._summarize_search_candidate_value(aweme_info.get("schema", ""), limit=120),
            "url": cls._summarize_search_candidate_value(aweme_info.get("url", ""), limit=120),
            "item_share_url": cls._summarize_search_candidate_value(item.get("share_url", ""), limit=120),
            "item_detail_url": cls._summarize_search_candidate_value(item.get("detail_url", ""), limit=120),
            "item_url": cls._summarize_search_candidate_value(item.get("url", ""), limit=120),
            "item_jump_url": cls._summarize_search_candidate_value(item.get("jump_url", ""), limit=120),
            "video_share_url": cls._summarize_search_candidate_value(video_info.get("share_url", ""), limit=120),
            "video_url": cls._summarize_search_candidate_value(video_info.get("url", ""), limit=120),
            "has_video": bool(aweme_info.get("video") or video_info),
            "has_images": bool(
                aweme_info.get("images")
                or aweme_info.get("image_infos")
                or aweme_info.get("image_post_info")
            ),
            "statistics": {
                "digg_count": int(stats.get("digg_count", 0) or 0),
                "comment_count": int(stats.get("comment_count", 0) or 0),
                "play_count": int(stats.get("play_count", 0) or 0),
            },
        }

    @classmethod
    def _is_suspicious_search_result_item(cls, item: dict, aweme_info: dict, entry: Dict[str, Any]) -> bool:
        item = item if isinstance(item, dict) else {}
        aweme_info = aweme_info if isinstance(aweme_info, dict) else {}
        entry = entry if isinstance(entry, dict) else {}
        if str(entry.get("content_type", "") or "").strip().lower() != "video":
            return False

        desc = str(aweme_info.get("desc", "") or "").strip()
        stats = aweme_info.get("statistics") if isinstance(aweme_info.get("statistics"), dict) else {}
        has_counts = bool(stats.get("digg_count") or stats.get("comment_count") or stats.get("play_count"))
        has_media = bool(
            aweme_info.get("video")
            or aweme_info.get("images")
            or aweme_info.get("image_infos")
            or aweme_info.get("image_post_info")
        )
        has_author = bool(
            str((aweme_info.get("author") or {}).get("nickname", "") or "").strip()
        )
        resolved_from_url_only = not str(item.get("aweme_id", "") or "").strip() and bool(
            aweme_info.get("share_url")
            or aweme_info.get("schema")
            or aweme_info.get("url")
            or item.get("share_url")
            or item.get("detail_url")
            or item.get("url")
            or item.get("jump_url")
        )
        return bool(has_author and not desc and not has_counts and not has_media and resolved_from_url_only)

    @classmethod
    def _resolve_search_result_aweme_id(cls, item: dict, aweme_info: dict) -> str:
        for _, candidate in cls._iter_search_result_aweme_id_candidates(item, aweme_info):
            resolved = cls._extract_aweme_id_from_candidate_value(candidate)
            if resolved:
                return resolved
        return ""

    # ---- URL 工具 ----

    @staticmethod
    def _sanitize_general_search_url(url: str) -> str:
        parsed = urllib.parse.urlparse(url or "")
        if "/search/" not in (parsed.path or "").lower():
            return url
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params["type"] = ["general"]
        for key in ("modal_id", "vid", "aweme_id", "previous_page", "from"):
            params.pop(key, None)
        rebuilt_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=rebuilt_query))

    def _remember_general_search_url(self):
        current_url = self.page.url
        if self._is_general_search_url(current_url):
            self._last_general_search_url = self._sanitize_general_search_url(current_url)

    # ---- 搜索结果可见性 ----

    def _has_visible_search_results(self) -> bool:
        for selector in SEARCH_RESULT_WAIT_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    def _wait_for_search_results_ready(self):
        if self._search_response_count > 0:
            logger.info(f"搜索API已返回 {self._search_response_count} 个响应批次")
            return
        for selector in SEARCH_RESULT_WAIT_SELECTORS:
            try:
                self.page.wait_for_selector(selector, state="visible", timeout=6000)
                logger.info(f"搜索结果已加载，命中选择器: {selector}")
                return
            except Exception:
                continue
        logger.warning("未显式检测到综合结果元素，继续使用API拦截结果")

    # ---- 综合搜索页准备 ----

    def _prepare_general_search_results_page(self):
        """统一收敛综合搜索页准备动作，确保布局稳定后再开始爬取。"""
        self._wait_for_search_results_ready()
        self._ensure_general_search_tab()
        self._remember_general_search_url()
        self._dismiss_login_popup_if_present()
        self._ensure_search_results_surface_clear()
        self._ensure_multi_column_search_layout()
        self._remember_general_search_url()

    def _ensure_general_search_tab(self):
        """确保搜索结果停留在综合/全部标签页。"""
        self._dismiss_login_popup_if_present()
        if self._is_general_search_url(self.page.url):
            logger.info("当前搜索结果URL已明确为综合页(type=general)")
            return

        for selector in SEARCH_GENERAL_TAB_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if not locator.is_visible(timeout=1500):
                    continue
                aria_selected = ""
                try:
                    aria_selected = (locator.get_attribute("aria-selected") or "").lower()
                except Exception:
                    aria_selected = ""
                class_name = ""
                try:
                    class_name = (locator.get_attribute("class") or "").lower()
                except Exception:
                    class_name = ""

                if aria_selected == "true" or "active" in class_name or "selected" in class_name:
                    logger.info("搜索结果已位于综合/全部标签页")
                    return

                locator.click()
                logger.info("已切换到综合/全部标签页")
                random_sleep(1.5, 2.8)
                self._wait_for_search_results_ready()
                if self._is_general_search_url(self.page.url):
                    return
                return
            except Exception as e:
                logger.debug(f"切换综合标签失败({selector}): {e}")

        current_url = self.page.url
        if "/search/" in current_url:
            try:
                parsed = urllib.parse.urlparse(current_url)
                params = urllib.parse.parse_qs(parsed.query)
                params["type"] = ["general"]
                rebuilt_query = urllib.parse.urlencode(params, doseq=True)
                general_url = urllib.parse.urlunparse(parsed._replace(query=rebuilt_query))
                logger.info(f"未找到综合标签，直接切换到综合搜索URL: {general_url}")
                self.page.goto(general_url, wait_until="domcontentloaded", timeout=30000)
                random_sleep(1.5, 2.8)
                self._dismiss_login_popup_if_present()
                self._wait_for_search_results_ready()
                return
            except Exception as e:
                logger.warning(f"切换综合搜索URL失败: {e}")

        logger.warning("未显式定位到综合/全部标签，继续使用当前搜索结果页")

    # ---- 布局检测与切换 ----

    @staticmethod
    def _is_layout_control_selected(control: Dict[str, Any]) -> bool:
        if not isinstance(control, dict):
            return False
        aria_pressed = str(control.get("aria_pressed", "") or "").lower()
        aria_selected = str(control.get("aria_selected", "") or "").lower()
        class_name = str(control.get("class_name", "") or "").lower()
        return (
            aria_pressed == "true"
            or aria_selected == "true"
            or "active" in class_name
            or "selected" in class_name
            or "current" in class_name
            or "checked" in class_name
        )

    @classmethod
    def _classify_general_search_layout_snapshot(cls, snapshot: Optional[Dict[str, Any]]) -> str:
        """
        根据综合页控件状态和卡片排布判断当前是多列还是单列。
        """
        if not isinstance(snapshot, dict):
            return "unknown"

        controls = snapshot.get("controls")
        if isinstance(controls, list):
            for control in controls:
                text = str((control or {}).get("text", "") or "")
                if not cls._is_layout_control_selected(control):
                    continue
                if any(label in text for label in SEARCH_LAYOUT_MULTI_COLUMN_TEXTS):
                    return "multi_column"
                if any(label in text for label in SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS):
                    return "single_column"

        # ---- 新增：当控件无法判断选中状态时，通过文本内容推断 ----
        # 抖音页面的"多列"/"单列"按钮通常没有aria-pressed属性
        # 如果控件文本中包含"多列"或"单列"，且没有其他控件标记为选中，
        # 则尝试通过DOM结构中的class推断
        if isinstance(controls, list):
            for control in controls:
                text = str((control or {}).get("text", "") or "")
                class_name = str((control or {}).get("class_name", "") or "").lower()
                # 抖音多列按钮的class通常包含特定标记
                if any(label in text for label in SEARCH_LAYOUT_MULTI_COLUMN_TEXTS):
                    # 检查class中是否有选中标记（抖音可能用不同方式标记）
                    if any(marker in class_name for marker in ("active", "selected", "current", "checked", "on", "highlight")):
                        return "multi_column"

        rows = snapshot.get("rows")
        if not isinstance(rows, list) or len(rows) == 0:
            # ---- 新增：当rows为空时，通过card_count和控件位置推断 ----
            # 如果有搜索结果卡片但rows为空，说明选择器可能不匹配
            # 此时通过控件文本中"多列"是否出现在"单列"之前来推断
            if isinstance(controls, list):
                multi_found = False
                single_found = False
                for control in controls:
                    text = str((control or {}).get("text", "") or "")
                    if any(label in text for label in SEARCH_LAYOUT_MULTI_COLUMN_TEXTS):
                        multi_found = True
                    if any(label in text for label in SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS):
                        single_found = True
                # 如果页面同时有"多列"和"单列"按钮，说明是搜索结果页
                # 默认假设抖音搜索结果页默认是多列布局
                if multi_found and single_found:
                    return "multi_column"
            return "unknown"

        row_counts: List[int] = []
        for row in rows[:4]:
            if not isinstance(row, dict):
                continue
            try:
                count = int(row.get("count", 0) or 0)
            except (TypeError, ValueError):
                continue
            if count > 0:
                row_counts.append(count)

        if any(count >= 2 for count in row_counts):
            return "multi_column"

        card_count = 0
        try:
            card_count = int(snapshot.get("card_count", 0) or 0)
        except (TypeError, ValueError):
            card_count = 0

        if len(row_counts) >= 2 and max(row_counts) == 1 and card_count >= 2:
            return "single_column"

        return "unknown"

    def _get_general_search_layout_snapshot(self) -> Dict[str, Any]:
        return self.page.evaluate(
            """
            () => {
                const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                const visible = (node) => {
                    if (!node) return false;
                    const rect = node.getBoundingClientRect();
                    if (rect.width < 16 || rect.height < 16) return false;
                    const style = window.getComputedStyle(node);
                    return style && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const toClassName = (value) => {
                    if (!value) return '';
                    if (typeof value === 'string') return value;
                    return String(value.baseVal || value);
                };
                const controls = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], a, div, span'))
                    .map((node) => {
                        const text = textOf(node);
                        if (!text || !/多列|双列|单列|筛选/.test(text) || !visible(node)) {
                            return null;
                        }
                        const rect = node.getBoundingClientRect();
                        return {
                            text,
                            aria_pressed: node.getAttribute('aria-pressed') || '',
                            aria_selected: node.getAttribute('aria-selected') || '',
                            class_name: toClassName(node.className).slice(0, 200),
                            left: Math.round(rect.left),
                            top: Math.round(rect.top),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height)
                        };
                    })
                    .filter(Boolean)
                    .slice(0, 20);

                const rawCards = Array.from(document.querySelectorAll(
                    'div[data-e2e="search-common-video"], div[data-e2e="search-item"], li[data-e2e*="search"], [class*="search-result"] [href*="/video/"], a[href*="/video/"], [class*="search-result-card"], div[class*="videoCard"], div[class*="SearchVideo"], ul[class*="search-result"] > li, div[class*="feed-card"]'
                ));
                const cards = [];
                const seen = new Set();
                for (const node of rawCards) {
                    const container = node.closest('li, [data-e2e="search-common-video"], [data-e2e="search-item"], article, div') || node;
                    if (!container || seen.has(container) || !visible(container)) continue;
                    seen.add(container);
                    const rect = container.getBoundingClientRect();
                    if (rect.width < 120 || rect.height < 120) continue;
                    cards.push({
                        x: Math.round(rect.left),
                        y: Math.round(rect.top),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
                    });
                }

                cards.sort((a, b) => a.y - b.y || a.x - b.x);
                const rows = [];
                for (const card of cards.slice(0, 24)) {
                    let row = rows.find((item) => Math.abs(item.y - card.y) <= 36);
                    if (!row) {
                        row = { y: card.y, count: 0, xs: [] };
                        rows.push(row);
                    }
                    row.count += 1;
                    row.xs.push(card.x);
                }

                return {
                    controls,
                    card_count: cards.length,
                    rows: rows.slice(0, 6)
                };
            }
            """
        )

    def _try_click_multi_column_layout_control(self) -> bool:
        for selector in SEARCH_LAYOUT_CONTROL_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if not locator.is_visible(timeout=1200):
                    continue
                locator.click()
                logger.info(f"已点击综合页多列布局控件: {selector}")
                return True
            except Exception as e:
                logger.debug(f"点击多列布局控件失败({selector}): {e}")

        try:
            clicked = self.page.evaluate(
                """
                () => {
                    const textOf = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], a, div, span'))
                        .filter((node) => {
                            const text = textOf(node);
                            if (!text || !/多列|双列/.test(text)) return false;
                            const rect = node.getBoundingClientRect();
                            if (rect.width < 16 || rect.height < 16) return false;
                            const style = window.getComputedStyle(node);
                            return style.display !== 'none' && style.visibility !== 'hidden';
                        })
                        .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                    const target = candidates[0];
                    if (!target) return '';
                    target.click();
                    return textOf(target);
                }
                """
            )
            if clicked:
                logger.info(f"已通过DOM脚本点击综合页多列布局控件: {clicked}")
                return True
        except Exception as e:
            logger.debug(f"通过DOM脚本点击多列布局控件失败: {e}")

        return False

    def _ensure_multi_column_search_layout(self):
        """根据综合页真实结构确认布局；若当前为单列则先切到多列。"""
        try:
            snapshot = self._get_general_search_layout_snapshot()
        except Exception as e:
            logger.warning(f"获取综合页布局快照失败，跳过多列确认: {e}")
            return

        layout = self._classify_general_search_layout_snapshot(snapshot)
        logger.info(
            "综合页布局检测结果: "
            f"{layout}, controls={snapshot.get('controls', [])[:4]}, rows={snapshot.get('rows', [])[:3]}"
        )
        if layout == "multi_column":
            return

        clicked = self._try_click_multi_column_layout_control()
        if not clicked:
            logger.warning("未定位到综合页多列布局控件，继续使用当前布局执行爬取")
            return

        random_sleep(1.0, 2.0)
        self._dismiss_login_popup_if_present()
        self._wait_for_search_results_ready()

        try:
            verified_snapshot = self._get_general_search_layout_snapshot()
        except Exception as e:
            logger.warning(f"复核综合页多列布局失败: {e}")
            return

        verified_layout = self._classify_general_search_layout_snapshot(verified_snapshot)
        logger.info(
            "综合页布局切换后复核: "
            f"{verified_layout}, rows={verified_snapshot.get('rows', [])[:3]}"
        )
        if verified_layout != "multi_column":
            logger.warning("综合页未明确切到多列布局，将继续使用当前结果页执行爬取")

    # ---- 搜索滚动 ----

    def _scroll_search_results(
        self,
        max_scrolls: int = 30,
        target_count: int = 200,
        idle_rounds_limit: int = 4,
    ):
        idle_rounds = 0
        previous_count = len(self.current_video_urls)
        for i in range(max_scrolls):
            if self._stop_event.is_set():
                break
            viewport_step = self.page.evaluate("() => Math.max(window.innerHeight * 0.9, 900)")
            self._human_helper().scroll_page(total_distance=float(viewport_step or 900))
            random_sleep(1.2, 2.4)

            current_count = len(self.current_video_urls)
            if current_count >= target_count:
                logger.info(f"综合页已累计发现 {current_count} 个视频，达到目标后停止滚动")
                break

            if current_count > previous_count:
                idle_rounds = 0
                logger.info(f"综合页继续发现新视频: {current_count} 个")
            else:
                idle_rounds += 1
                logger.info(f"综合页本轮无新增视频，空转轮次 {idle_rounds}/{idle_rounds_limit}")

            previous_count = current_count
            if idle_rounds >= idle_rounds_limit and self._search_response_count > 0:
                logger.info("综合页连续多轮无新增结果，停止继续下滚")
                break

            if (i + 1) % 3 == 0:
                pause = random.uniform(2.5, 4.5)
                logger.info(f"搜索滚动降速，暂停 {pause:.1f} 秒")
                time.sleep(pause)

    # ---- 搜索结果条目 ----

    @staticmethod
    def _build_search_video_entry(aweme_info: dict) -> Dict[str, Any]:
        aweme_id = str(aweme_info.get("aweme_id", "") or "").strip()
        if not aweme_id:
            return {}
        author = aweme_info.get("author") or {}
        is_note = SearchMixin._is_note_aweme(aweme_info)
        canonical_url = SearchMixin._build_aweme_detail_url(aweme_info)
        entry = {
            "url": canonical_url,
            "aweme_id": aweme_id,
            "content_type": "note" if is_note else "video",
            "title": aweme_info.get("desc", "")[:100],
            "author": author.get("nickname", ""),
            "author_id": author.get("sec_uid", ""),
            "author_unique_id": author.get("unique_id", ""),
            "like_count": aweme_info.get("statistics", {}).get("digg_count", 0),
            "comment_count": aweme_info.get("statistics", {}).get("comment_count", 0),
            "share_count": aweme_info.get("statistics", {}).get("share_count", 0),
            "play_count": aweme_info.get("statistics", {}).get("play_count", 0),
            "publish_timestamp": aweme_info.get("create_time", 0),
        }
        logger.info(
            "搜索结果 canonical 诊断: "
            f"aweme_id={aweme_id}, "
            f"content_type={entry.get('content_type', '')}, "
            f"canonical_url={canonical_url}, "
            f"source_url={SearchMixin._summarize_search_candidate_value(aweme_info.get('url', ''))}, "
            f"share_url={SearchMixin._summarize_search_candidate_value(aweme_info.get('share_url', ''))}, "
            f"schema={SearchMixin._summarize_search_candidate_value(aweme_info.get('schema', ''))}"
        )
        return entry

    def _record_search_video_entry(self, entry: Dict[str, Any]) -> bool:
        video_url = str(entry.get("url", "") or "").strip()
        if not video_url or video_url in self.current_video_urls:
            return False
        self.current_video_urls.append(video_url)
        self._search_data.append(entry)
        logger.info(
            "搜索结果入队诊断: "
            f"aweme_id={entry.get('aweme_id', '')}, "
            f"content_type={entry.get('content_type', '')}, "
            f"canonical_url={video_url}, "
            f"title={self._summarize_search_candidate_value(entry.get('title', ''), limit=60)}"
        )
        return True

    @staticmethod
    def _drain_pending_search_entries(
        pending_entries: List[Dict[str, Any]],
        target_result_count: int = 0,
    ):
        while pending_entries:
            if target_result_count and len(pending_entries) >= 0:
                pass
            yield pending_entries.pop(0)

    # ---- 搜索拦截器同步 ----

    def _sync_search_interceptor_stats(self) -> Dict[str, Any]:
        stats = {
            "search_response_batches": int(getattr(self, "_search_response_count", 0) or 0),
            "pending_search_batches": 0,
            "peak_pending_search_batches": 0,
            "dropped_search_batches_count": 0,
            "latest_search_batch_id": 0,
            "latest_search_batch_url": "",
            "discovered_video_count": len(getattr(self, "current_video_urls", []) or []),
        }
        interceptor = getattr(self, "search_interceptor", None)
        if interceptor:
            queue_stats = interceptor.get_queue_stats()
            stats.update({
                "pending_search_batches": int(queue_stats.get("pending_total", 0) or 0),
                "peak_pending_search_batches": int(queue_stats.get("peak_pending_batches", 0) or 0),
                "dropped_search_batches_count": int(queue_stats.get("dropped_batches_count", 0) or 0),
                "latest_search_batch_id": int(queue_stats.get("latest_batch_id", 0) or 0),
                "latest_search_batch_url": str(queue_stats.get("latest_url", "") or ""),
            })
        self._last_search_interceptor_stats = dict(stats)
        return dict(stats)

    def get_last_search_interceptor_stats(self) -> Dict[str, Any]:
        cached = getattr(self, "_last_search_interceptor_stats", None)
        if not isinstance(cached, dict):
            return self._sync_search_interceptor_stats()
        return dict(cached)

    # ---- 搜索 API 批次消费 ----

    def _consume_search_api_batches(
        self,
        pending_entries: List[Dict[str, Any]],
        target_result_count: int = 0,
    ) -> int:
        if not self.search_interceptor:
            return 0
        consumed_batches = 0
        batches = self.search_interceptor.drain_batches()
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            consumed_batches += 1
            self._search_response_count += 1
            data = batch.get("data")
            if not isinstance(data, dict):
                continue
            items = self._extract_search_items(data)
            for item in items:
                try:
                    aweme_info = self._extract_aweme_info(item)
                    if not self._is_relevant_general_video_item(item, aweme_info):
                        continue
                    entry = self._build_search_video_entry(aweme_info)
                    if not entry:
                        continue
                    if self._is_suspicious_search_result_item(item, aweme_info, entry):
                        logger.warning(
                            "搜索结果疑似脏视频卡片: "
                            f"batch_id={int(batch.get('batch_id', 0) or 0)}, "
                            f"batch_url={self._summarize_search_candidate_value(batch.get('url', ''), limit=160)}, "
                            f"aweme_id={entry.get('aweme_id', '')}, "
                            f"canonical_url={entry.get('url', '')}, "
                            f"snapshot={self._build_search_result_item_snapshot(item, aweme_info)}, "
                            f"candidates={self._build_search_aweme_resolution_diagnostics(item, aweme_info)}"
                        )
                    if target_result_count and len(self.current_video_urls) >= target_result_count:
                        break
                    if self._record_search_video_entry(entry):
                        pending_entries.append(entry)
                except Exception as e:
                    logger.debug(f"消费搜索批次中的单个搜索项失败: {e}")
        self._sync_search_interceptor_stats()
        return consumed_batches

    # ---- 搜索等待与策略 ----

    def _wait_for_search_bootstrap(self, timeout_seconds: float = 2.0) -> bool:
        if not self.search_interceptor:
            return False
        deadline = time.time() + max(float(timeout_seconds or 0.0), 0.5)
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            queue_stats = self.search_interceptor.get_queue_stats()
            if (
                int(queue_stats.get("pending_total", 0) or 0) > 0
                or float(queue_stats.get("latest_timestamp", 0.0) or 0.0) > 0.0
            ):
                self._sync_search_interceptor_stats()
                return True
            time.sleep(0.12)
        self._sync_search_interceptor_stats()
        return False

    @classmethod
    def _resolve_search_discovery_limits(cls, target_result_count: int) -> Tuple[int, int]:
        target = max(0, int(target_result_count or 0))
        if target > 0:
            return target, max(10, min(200, max(target * 3, target + 12)))
        return cls.DEFAULT_SEARCH_TARGET_COUNT, cls.DEFAULT_SEARCH_MAX_SCROLLS

    @classmethod
    def _should_continue_search_after_idle(
        cls,
        idle_rounds: int,
        response_count: int,
    ) -> bool:
        stop_rounds = cls.SEARCH_IDLE_STOP_ROUNDS
        stop_min_responses = cls.SEARCH_IDLE_STOP_MIN_RESPONSES
        return not (
            idle_rounds >= stop_rounds
            and int(response_count or 0) >= stop_min_responses
        )

    def _intelligent_diagnose_and_fix(self, context: str = "unknown") -> bool:
        """
        智能诊断并修复搜索过程中的问题。

        当搜索无数据时，自动检测以下问题并尝试修复：
        1. 登录弹窗遮挡 → 强力关闭
        2. 推荐视频弹窗遮挡 → 强力关闭
        3. 单列布局（非多列）→ 切换为多列
        4. 页面不在搜索结果页 → 导航回搜索页
        5. 风控/验证码页面 → 记录并通知

        Args:
            context: 诊断上下文，如 "first_page_empty" 或 "scroll_idle"

        Returns:
            bool: 是否执行了有效的修复动作
        """
        fixes_applied = []

        try:
            # 1. 强力关闭登录弹窗
            if hasattr(self, '_dismiss_login_popup_if_present'):
                try:
                    if self._dismiss_login_popup_if_present():
                        fixes_applied.append("login_popup_closed")
                        logger.info("[智能体] 修复: 已关闭登录弹窗")
                except Exception as e:
                    logger.debug(f"[智能体] 登录弹窗检测异常: {e}")

            # 2. 强力关闭推荐视频弹窗
            if hasattr(self, '_dismiss_recommended_video_if_present'):
                try:
                    if self._dismiss_recommended_video_if_present(max_rounds=2):
                        fixes_applied.append("rec_video_closed")
                        logger.info("[智能体] 修复: 已关闭推荐视频弹窗")
                except Exception as e:
                    logger.debug(f"[智能体] 推荐视频弹窗检测异常: {e}")

            # 3. 清理搜索结果表面
            if hasattr(self, '_ensure_search_results_surface_clear'):
                try:
                    self._ensure_search_results_surface_clear()
                    fixes_applied.append("surface_cleared")
                except Exception as e:
                    logger.debug(f"[智能体] 表面清理异常: {e}")

            # 4. 检测并切换多列布局
            if hasattr(self, '_get_general_search_layout_snapshot') and hasattr(self, '_classify_general_search_layout_snapshot'):
                try:
                    snapshot = self._get_general_search_layout_snapshot()
                    if snapshot:
                        layout = self._classify_general_search_layout_snapshot(snapshot)
                        if layout != "multi_column":
                            logger.warning(f"[智能体] 诊断: 当前布局为 {layout}，尝试切换多列...")
                            if hasattr(self, '_ensure_multi_column_search_layout'):
                                self._ensure_multi_column_search_layout()
                                fixes_applied.append(f"layout_switched_from_{layout}")
                                logger.info("[智能体] 修复: 已执行多列布局切换")
                        else:
                            logger.info("[智能体] 诊断: 布局已是多列，无需切换")
                except Exception as e:
                    logger.debug(f"[智能体] 布局检测异常: {e}")

            # 5. 检测风控页面
            if hasattr(self, 'page'):
                try:
                    risk_keywords = ["验证码", "请完成验证", "访问受限", "操作过于频繁", "稍后再试", "captcha"]
                    body_text = self.page.evaluate("() => (document.body?.innerText || '').substring(0, 2000)")
                    if body_text:
                        for kw in risk_keywords:
                            if kw.lower() in body_text.lower():
                                logger.error(f"[智能体] 诊断: 检测到风控关键词 '{kw}'，无法自动修复")
                                fixes_applied.append(f"risk_control_detected:{kw}")
                                break
                except Exception:
                    pass

            # 6. 检查是否还在搜索结果页URL
            if hasattr(self, 'page'):
                try:
                    current_url = self.page.url
                    if "/search/" not in current_url and "/searchresult/" not in current_url:
                        logger.warning(f"[智能体] 诊断: 当前URL不在搜索结果页 ({current_url[:80]})")
                        # 尝试导航回搜索页
                        if hasattr(self, '_last_general_search_url') and self._last_general_search_url:
                            logger.info("[智能体] 修复: 导航回搜索结果页")
                            self.page.goto(self._last_general_search_url, timeout=15000)
                            self._interruptible_sleep(random.uniform(1.5, 2.5))
                            fixes_applied.append("navigated_back_to_search")
                except Exception as e:
                    logger.debug(f"[智能体] URL检测异常: {e}")

        except Exception as e:
            logger.error(f"[智能体] 诊断修复过程异常: {e}")

        if fixes_applied:
            logger.info(f"[智能体] 诊断修复完成({context}): {', '.join(fixes_applied)}")
        else:
            logger.info(f"[智能体] 诊断完成({context}): 未发现可修复的问题")

        return len(fixes_applied) > 0

    @staticmethod
    def _update_search_idle_rounds(
        *,
        previous_video_count: int,
        current_video_count: int,
        previous_response_count: int,
        current_response_count: int,
        drained_count: int,
        current_idle_rounds: int,
    ) -> int:
        if drained_count > 0:
            return 0
        if current_video_count > previous_video_count:
            return 0
        if current_response_count > previous_response_count:
            return 0
        return current_idle_rounds + 1

    # ---- 搜索结果解析 ----

    def _extract_aweme_info(self, item: dict) -> dict:
        """
        从搜索结果项中提取视频信息

        Args:
            item: 搜索结果项

        Returns:
            dict: 视频信息
        """
        aweme_info = None

        if item.get("aweme_info"):
            aweme_info = dict(item["aweme_info"])
        elif item.get("aweme_id"):
            aweme_info = dict(item)
        elif item.get("card_item", {}).get("aweme_info"):
            aweme_info = dict(item["card_item"]["aweme_info"])
        elif item.get("video"):
            aweme_info = dict(item["video"])

        if not isinstance(aweme_info, dict):
            return {}

        resolved_aweme_id = self._resolve_search_result_aweme_id(item, aweme_info)
        resolution_diagnostics = self._build_search_aweme_resolution_diagnostics(item, aweme_info)
        if resolved_aweme_id:
            aweme_info["aweme_id"] = resolved_aweme_id
        else:
            aweme_info.pop("aweme_id", None)

        logger.info(
            "搜索结果 aweme_id 解析诊断: "
            f"resolved_aweme_id={resolved_aweme_id or ''}, "
            f"desc={self._summarize_search_candidate_value(aweme_info.get('desc', ''), limit=60)}, "
            f"snapshot={self._build_search_result_item_snapshot(item, aweme_info)}, "
            f"candidates={resolution_diagnostics}"
        )

        return aweme_info

    @staticmethod
    def _parse_search_response_text(raw_text: str) -> Optional[dict]:
        text = str(raw_text or "").strip()
        if not text:
            return None

        decoder = json.JSONDecoder()
        candidate_starts = []
        for index, char in enumerate(text):
            if char in "{[":
                candidate_starts.append(index)
                if len(candidate_starts) >= 6:
                    break

        for start in candidate_starts:
            try:
                parsed, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _parse_search_response_payload(self, response) -> Optional[dict]:
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception as json_err:
            try:
                raw_text = response.text()
            except Exception as text_err:
                logger.debug(f"搜索API响应JSON解析失败: {json_err}; 文本读取失败: {text_err}")
                return None

            parsed = self._parse_search_response_text(raw_text)
            if parsed is not None:
                return parsed

            logger.debug(
                "搜索API响应JSON解析失败: "
                f"{json_err}; 响应头片段={raw_text[:120]!r}"
            )
            return None

        return None

    @staticmethod
    def _extract_search_items(data: dict) -> list:
        """兼容综合搜索接口的多种返回形态。"""
        if not isinstance(data, dict):
            return []

        if isinstance(data.get("data"), list):
            return data.get("data", [])

        if isinstance(data.get("data"), dict):
            nested = data.get("data", {})
            for key in ("list", "data", "item_list"):
                if isinstance(nested.get(key), list):
                    return nested.get(key, [])

        if isinstance(data.get("item_list"), list):
            return data.get("item_list", [])

        return []

    @staticmethod
    def _is_relevant_general_video_item(item: dict, aweme_info: dict) -> bool:
        """
        综合页会混入直播、广告、用户、话题等卡片，这里只保留可稳定落到 /video/{aweme_id} 的视频结果。
        """
        if not isinstance(item, dict) or not isinstance(aweme_info, dict):
            return False

        aweme_id = str(aweme_info.get("aweme_id", "") or "").strip()
        if not SearchMixin._is_supported_aweme_id(aweme_id):
            return False

        item_type = item.get("type")
        try:
            normalized_type = int(item_type)
        except (TypeError, ValueError):
            normalized_type = -1

        if normalized_type in {16, 17, 18}:
            return False

        if item.get("live_info") or item.get("lives") or item.get("room_info") or item.get("user_list"):
            return False

        desc = str(aweme_info.get("desc", "") or "").strip()
        author = aweme_info.get("author") or {}
        stats = aweme_info.get("statistics") or {}
        has_author = bool(str(author.get("nickname", "") or "").strip())
        has_counts = bool(stats.get("digg_count") or stats.get("comment_count") or stats.get("play_count"))
        has_media = bool(aweme_info.get("video") or aweme_info.get("images") or aweme_info.get("image_infos"))

        return bool(desc or has_author or has_counts or has_media)

    # ---- 待处理评论批次消费（搜索域） ----

    def _consume_pending_comment_batches_if_any(
        self,
        session: dict,
        aweme_id: str,
        video_url: str,
        target_keywords: list,
        skip_crawled: bool,
        platform: str,
        video_title: str = "",
        author_name: str = "",
        reason: str = "",
    ) -> int:
        """在风险检查或异常退出前，优先消费拦截器中已到达的评论批次。"""
        if not self.comment_interceptor:
            return 0
        try:
            queue_stats = self.comment_interceptor.get_queue_stats()
        except Exception:
            queue_stats = {}

        pending_total = int(queue_stats.get("pending_total", 0) or 0)
        latest_timestamp = float(queue_stats.get("latest_timestamp", 0.0) or 0.0)
        if pending_total <= 0 and latest_timestamp <= 0.0:
            return 0

        processed_batches = self._consume_comment_api_batches(
            session,
            aweme_id,
            video_url,
            target_keywords,
            skip_crawled,
            platform,
            video_title,
            author_name,
        )
        if processed_batches:
            logger.info(
                "已优先消费待处理评论批次: "
                f"reason={reason or 'unspecified'}, processed_batches={processed_batches}"
            )
        return processed_batches
