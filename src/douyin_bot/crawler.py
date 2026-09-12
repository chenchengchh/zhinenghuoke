"""
抖音爬虫模块
负责搜索视频和爬取评论

优化要点：
1. 使用API拦截获取搜索结果和评论数据
2. 支持评论关键词过滤
3. 支持分页加载更多评论
4. 增强错误处理和日志记录
5. 添加间隔时间避免触发反爬机制
"""
from playwright.sync_api import Page
from loguru import logger
import contextlib
import json
import math
import time
import re
import random
import threading
import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from src.common.utils import random_sleep
from src.config.settings import SELECTORS
from src.common.database import DatabaseManager
from src.infrastructure.runtime_paths import get_log_dir
from src.douyin_bot.comment_batch_processor import evaluate_comment_batch_item
from src.douyin_bot.comment_batch_persistence import (
    persist_crawled_comment_records,
    persist_matched_comment_batch,
)
from src.douyin_bot.comment_batch_status import update_comment_batch_progress
from src.douyin_bot.comment_api_interceptor import CommentAPIInterceptor
from src.douyin_bot.comment_reply_executor import execute_original_comment_reply_rounds
from src.douyin_bot.comment_reply_targets import (
    append_linked_reply_targets,
    build_matched_comment_target,
    normalize_reply_target,
)
from src.douyin_bot.search_api_interceptor import SearchAPIInterceptor
from src.douyin_bot.human_interaction import HumanInteractionHelper
from src.douyin_bot.input_locator_service import InputLocatorContext, get_input_locator_service


from .search_mixin import SearchMixin
from .page_interaction_mixin import PageInteractionMixin
from .comment_mixin import CommentMixin
from .comment_reply_mixin import CommentReplyMixin


class Crawler(SearchMixin, PageInteractionMixin, CommentMixin, CommentReplyMixin):
    """
    抖音爬虫类
    
    负责搜索视频和爬取评论数据
    """
    
    # 爬取间隔配置（秒）
    DEFAULT_VIDEO_INTERVAL = (5, 10)  # 视频之间的间隔时间范围
    DEFAULT_SCROLL_INTERVAL = (2, 4)  # 滚动之间的间隔时间范围
    DOUYIN_HOME_URL = "https://www.douyin.com/"
    DEFAULT_SEARCH_TARGET_COUNT = 300
    DEFAULT_SEARCH_MAX_SCROLLS = 80

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
    SEARCH_IDLE_STOP_ROUNDS = 3
    SEARCH_IDLE_STOP_MIN_RESPONSES = 1
    SEARCH_INPUT_SELECTORS = [
        'input[placeholder*="搜索"]',
        'input[type="search"]',
        'input[data-e2e*="search"]',
        'input[class*="search"]',
        '[role="searchbox"] input',
        '[role="textbox"][placeholder*="搜索"]',
        '[contenteditable="true"][data-e2e*="search"]',
        '[contenteditable="true"][placeholder*="搜索"]',
    ]
    SEARCH_BUTTON_SELECTORS = [
        'button:has-text("搜索")',
        '[data-e2e="search-button"]',
        'button[class*="search"]',
        '[role="button"][aria-label*="搜索"]',
    ]
    SEARCH_RESULT_WAIT_SELECTORS = [
        '[data-e2e="search-common-video"]',
        '[data-e2e="search-item"]',
        '[class*="search-result"]',
        '[class*="searchResult"]',
        '[class*="video-item"]',
    ]
    SEARCH_GENERAL_TAB_SELECTORS = [
        '[role="tab"]:has-text("综合")',
        '[role="tab"]:has-text("全部")',
        'a:has-text("综合")',
        'a:has-text("全部")',
        '[class*="tab"]:has-text("综合")',
        '[class*="tab"]:has-text("全部")',
    ]
    SEARCH_LAYOUT_MULTI_COLUMN_TEXTS = ("多列", "双列")
    SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS = ("单列",)
    SEARCH_LAYOUT_CONTROL_SELECTORS = [
        'button:has-text("多列")',
        '[role="button"]:has-text("多列")',
        '[role="tab"]:has-text("多列")',
        'div:has-text("多列")',
        'span:has-text("多列")',
    ]
    LOGIN_MODAL_CLOSE_SELECTORS = [
        '[data-e2e="login-close"]',
        '[data-e2e="modal-close-inner-button"]',
        '[class*="close"]:has(svg)',
        'button[aria-label*="关闭"]',
        'button:has-text("关闭")',
    ]
    RECOMMENDED_VIDEO_CLOSE_SELECTORS = [
        '[data-e2e="feed-close"]',
        '[data-e2e="detail-close"]',
        '[data-e2e="video-detail-close"]',
        '[data-e2e="close-detail"]',
        '.xgplayer-close',
        '.video-detail-close',
        'div[class*="close"]',  # 模糊匹配常见的带有 close 样式的 div 按钮
        'svg[class*="close"]',  # 模糊匹配 SVG 图标关闭按钮
    ]
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
    COMMENT_SURFACE_SELECTORS = [
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[data-e2e="comment-panel"]',
        '[data-e2e="note-comment-list"]',
        '[class*="comment-list"]',
        '[class*="CommentList"]',
        '[class*="comment-panel"]',
        '[class*="commentContent"]',
        '[class*="comment-drawer"]',
        '[class*="CommentDrawer"]',
        '[class*="commentContainer"]',
        '[class*="CommentContainer"]',
        '[class*="commentWrap"]',
        '[class*="CommentWrap"]',
        '[class*="commentScroll"]',
        '[class*="CommentScroll"]',
        '.comment-main',
    ]
    COMMENT_ITEM_SELECTORS = [
        '[data-e2e="comment-item"]',
        '[class*="comment-item"]',
        '[class*="CommentItem"]',
    ]
    COMMENT_OPEN_TRIGGER_SELECTORS = [
        "[data-e2e='comment-icon']",
        "[data-e2e='feed-comment-icon']",
        "[data-e2e='browse-comment']",
        "[data-e2e='video-comment-icon']",
        "[data-e2e='comment-count']",
        "button[aria-label*='评论']",
        "[role='button'][aria-label*='评论']",
        "[class*='comment-icon']",
        "[class*='CommentIcon']",
        "[class*='commentIcon']",
        "[class*='comment-count']",
        "[class*='commentCount']",
    ]
    def __init__(self, page: Page, db: DatabaseManager):
        """
        初始化爬虫
        
        Args:
            page: Playwright页面对象
            db: 数据库管理器
        """
        self.page = page
        self.db = db
        self.current_video_urls = []
        self._search_data = []
        self._stop_event = threading.Event()
        self.comment_interceptor = CommentAPIInterceptor(page)
        self.search_interceptor = SearchAPIInterceptor(page)
        self._last_search_interceptor_stats = {
            "search_response_batches": 0,
            "pending_search_batches": 0,
            "peak_pending_search_batches": 0,
            "dropped_search_batches_count": 0,
            "latest_search_batch_id": 0,
            "latest_search_batch_url": "",
            "discovered_video_count": 0,
        }
        self._last_general_search_url = ""
        self._last_original_reply_result = {
            "ok": False,
            "reason": "",
            "stage": "",
            "message": "",
        }

    def _set_last_original_reply_result(
        self,
        *,
        ok: bool,
        reason: str = "",
        stage: str = "",
        message: str = "",
        diagnostics: Optional[dict] = None,
    ) -> dict:
        result = {
            "ok": bool(ok),
            "reason": str(reason or "").strip(),
            "stage": str(stage or "").strip(),
            "message": str(message or "").strip(),
            "diagnostics": dict(diagnostics or {}),
        }
        self._last_original_reply_result = result
        return dict(result)

    def get_last_original_reply_result(self) -> dict:
        return dict(getattr(self, "_last_original_reply_result", {}) or {})

    @staticmethod
    def _build_reply_diagnostics(
        *,
        target: Optional[dict] = None,
        context_state: Optional[dict] = None,
        click_result: Optional[dict] = None,
        activation: Optional[dict] = None,
        surface_selector: str = "",
        recovered_stage: str = "",
        recovered: bool = False,
    ) -> dict:
        target = target if isinstance(target, dict) else {}
        context_state = context_state if isinstance(context_state, dict) else {}
        click_result = click_result if isinstance(click_result, dict) else {}
        activation = activation if isinstance(activation, dict) else {}
        return {
            "target_aweme_id": str(target.get("aweme_id", "") or "").strip(),
            "target_comment_id": str(target.get("comment_id", "") or "").strip(),
            "target_parent_comment_id": str(target.get("parent_comment_id", "") or "").strip(),
            "target_root_comment_id": str(target.get("root_comment_id", "") or "").strip(),
            "target_comment_level": int(target.get("comment_level", 1) or 1),
            "target_sec_uid": str(target.get("sec_uid", "") or "").strip(),
            "target_unique_id": str(target.get("unique_id", "") or "").strip(),
            "current_aweme_id": str(context_state.get("current_aweme_id", "") or "").strip(),
            "current_url": str(context_state.get("current_url", "") or "").strip(),
            "target_url": str(context_state.get("target_url", "") or "").strip(),
            "context_recovered": bool(context_state.get("recovered")) or bool(recovered),
            "recovered_stage": str(recovered_stage or "").strip(),
            "surface_selector": str(click_result.get("surfaceSelector", "") or surface_selector or "").strip(),
            "surface_item_count": int(click_result.get("surfaceItemCount", 0) or 0),
            "candidate_count": int(click_result.get("inspected", 0) or 0),
            "parent_anchor_matched": bool(click_result.get("parentAnchorMatched")),
            "document_fallback_used": bool(click_result.get("usedDocumentFallback")),
            "trigger_source": str(click_result.get("triggerSource", "") or "").strip(),
            "matched_text": str(click_result.get("matchedText", "") or "").strip()[:120],
            "live_anchor_matched": bool(target.get("_live_anchor_matched")),
            "live_parent_anchor_matched": bool(target.get("_live_parent_anchor_matched")),
            "live_anchor_index_size": int(target.get("_live_anchor_index_size", 0) or 0),
            "activation_reason": str(activation.get("reason", "") or "").strip(),
            "activation_active": bool(activation.get("active")),
        }

    @staticmethod
    def _dedupe_selector_list(selectors: list[str]) -> list[str]:
        result = []
        seen = set()
        for selector in selectors or []:
            normalized = str(selector or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

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

    def _get_comment_surface_selectors_for_layout(self, detail_kind: str) -> list[str]:
        note_priority = [
            '[data-e2e="note-comment-list"]',
            '[class*="commentContainer"]',
            '[class*="CommentContainer"]',
            '[class*="commentWrap"]',
            '[class*="CommentWrap"]',
            '[class*="commentScroll"]',
            '[class*="CommentScroll"]',
        ]
        video_priority = [
            '[data-e2e="comment-panel"]',
            '[data-e2e="comment-list"]',
            '[data-e2e="comment-list-container"]',
            '[class*="comment-drawer"]',
            '[class*="CommentDrawer"]',
            '[class*="comment-panel"]',
            '[class*="commentContent"]',
            '.comment-main',
        ]
        if detail_kind == "note":
            return self._dedupe_selector_list(note_priority + list(self.COMMENT_SURFACE_SELECTORS))
        if detail_kind == "video":
            return self._dedupe_selector_list(video_priority + list(self.COMMENT_SURFACE_SELECTORS))
        return list(self.COMMENT_SURFACE_SELECTORS)

    def _get_comment_item_selectors_for_layout(self, detail_kind: str) -> list[str]:
        note_priority = [
            '[data-e2e="note-comment-list"] [data-e2e="comment-item"]',
            '[data-e2e="note-comment-list"] li[class*="comment"]',
            '[data-e2e="note-comment-list"] article[class*="comment"]',
            '[class*="commentContainer"] [data-e2e="comment-item"]',
            '[class*="CommentContainer"] [data-e2e="comment-item"]',
            '[data-e2e="comment-item"]',
            '[class*="comment-item"]',
            '[class*="CommentItem"]',
            'li[class*="comment"]',
            'article[class*="comment"]',
        ]
        video_priority = [
            '[data-e2e="comment-item"]',
            '[class*="comment-item"]',
            '[class*="CommentItem"]',
            'li[class*="comment"]',
        ]
        if detail_kind == "note":
            return self._dedupe_selector_list(note_priority + list(self.COMMENT_ITEM_SELECTORS))
        if detail_kind == "video":
            return self._dedupe_selector_list(video_priority + list(self.COMMENT_ITEM_SELECTORS))
        return list(self.COMMENT_ITEM_SELECTORS)

    def _get_comment_composer_container_selectors(self, detail_kind: str) -> list[str]:
        note_priority = [
            '[data-e2e="comment-input"]',
            '.comment-input-inner-container',
            '.comment-input-inner',
            '.comment-input-area',
            '[class*="comment-input"]',
            '[class*="commentInput"]',
            '[class*="CommentInput"]',
        ]
        video_priority = [
            '#comment-input-container',
            '.kBeyEVwN#comment-input-container',
            '[data-e2e="comment-input"]',
            '.comment-input-inner-container',
            '.comment-input-inner',
            '.comment-input-area',
            '[class*="comment-input"]',
            '[class*="commentInput"]',
            '[class*="CommentInput"]',
        ]
        if detail_kind == "note":
            return self._dedupe_selector_list(note_priority + video_priority)
        if detail_kind == "video":
            return self._dedupe_selector_list(video_priority + note_priority)
        return self._dedupe_selector_list(video_priority + note_priority)

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

    def _human_helper(self) -> HumanInteractionHelper:
        helper = getattr(self, "_human", None)
        if helper is None:
            helper = HumanInteractionHelper(self.page)
            self._human = helper
        return helper
        
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

            search_input = self._find_visible_locator(self.SEARCH_INPUT_SELECTORS, timeout=12000)
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
        search_button = self._find_visible_locator(self.SEARCH_BUTTON_SELECTORS, timeout=2500)
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
        if not re.fullmatch(r"[0-9A-Za-z_-]{8,64}", text):
            return False
        return bool(re.search(r"\d", text))

    @staticmethod
    def _extract_aweme_id_from_candidate_value(value: Any) -> str:
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
            match = re.search(pattern, text)
            if match:
                return str(match.group(1) or "").strip()
        return ""

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
        text = re.sub(r"\s+", " ", str(value or "").strip())
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

    def _has_visible_search_results(self) -> bool:
        for selector in self.SEARCH_RESULT_WAIT_SELECTORS:
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
        for selector in self.SEARCH_RESULT_WAIT_SELECTORS:
            try:
                self.page.wait_for_selector(selector, state="visible", timeout=6000)
                logger.info(f"搜索结果已加载，命中选择器: {selector}")
                return
            except Exception:
                continue
        logger.warning("未显式检测到综合结果元素，继续使用API拦截结果")

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

        for selector in self.SEARCH_GENERAL_TAB_SELECTORS:
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
                if any(label in text for label in cls.SEARCH_LAYOUT_MULTI_COLUMN_TEXTS):
                    return "multi_column"
                if any(label in text for label in cls.SEARCH_LAYOUT_SINGLE_COLUMN_TEXTS):
                    return "single_column"

        rows = snapshot.get("rows")
        if not isinstance(rows, list):
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
                    'div[data-e2e="search-common-video"], div[data-e2e="search-item"], li[data-e2e*="search"], [class*="search-result"] [href*="/video/"], a[href*="/video/"]'
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
        for selector in self.SEARCH_LAYOUT_CONTROL_SELECTORS:
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

    @staticmethod
    def _build_search_video_entry(aweme_info: dict) -> Dict[str, Any]:
        aweme_id = str(aweme_info.get("aweme_id", "") or "").strip()
        if not aweme_id:
            return {}
        author = aweme_info.get("author") or {}
        is_note = Crawler._is_note_aweme(aweme_info)
        canonical_url = Crawler._build_aweme_detail_url(aweme_info)
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
            f"source_url={Crawler._summarize_search_candidate_value(aweme_info.get('url', ''))}, "
            f"share_url={Crawler._summarize_search_candidate_value(aweme_info.get('share_url', ''))}, "
            f"schema={Crawler._summarize_search_candidate_value(aweme_info.get('schema', ''))}"
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
                # 实际限制由上层 current_video_urls 控制，这里只负责按发现顺序吐出。
                pass
            yield pending_entries.pop(0)

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

    def _dismiss_login_popup_if_present(self):
        for selector in self.LOGIN_MODAL_CLOSE_SELECTORS:
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
            # 极速探测：将所有探测逻辑打包在单个 JS 中执行，避免逐个 selector 等待超时
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
                [self.RECOMMENDED_VIDEO_SURFACE_SELECTORS, self.RECOMMENDED_VIDEO_CLOSE_SELECTORS]
            )
            state["surface_selector"] = fast_probe.get("surface", "")
            state["close_selector"] = fast_probe.get("close", "")
            state["js_close_text"] = fast_probe.get("jsText", "")
        except Exception as e:
            pass

        state["active"] = bool(
            state["detail_url"]
            or state["surface_selector"]
            or state["close_selector"]
            or state["js_close_text"]
        )
        return state

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

    def _dismiss_recommended_video_if_present(self, max_rounds: int = 2):
        """关闭搜索后可能自动弹出的推荐视频弹窗/播放页。"""
        dismissed = False
        for _ in range(max(1, max_rounds)):
            popup_state = self._detect_recommended_video_popup_state()
            if not popup_state.get("active"):
                return dismissed

            clicked = False
            for selector in self.RECOMMENDED_VIDEO_CLOSE_SELECTORS:
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

    def _extract_aweme_info(self, item: dict) -> dict:
        """
        从搜索结果项中提取视频信息
        
        Args:
            item: 搜索结果项
            
        Returns:
            dict: 视频信息
        """
        # 尝试多种数据结构
        aweme_info = None
        
        # 结构1: 直接是aweme_info
        if item.get("aweme_info"):
            aweme_info = dict(item["aweme_info"])
        # 结构2: item本身就是视频信息
        elif item.get("aweme_id"):
            aweme_info = dict(item)
        # 结构3: 嵌套在card_item中
        elif item.get("card_item", {}).get("aweme_info"):
            aweme_info = dict(item["card_item"]["aweme_info"])
        # 结构4: 在video字段中
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
        if not Crawler._is_supported_aweme_id(aweme_id):
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
                # 抖音关注按钮通常包含 "关注" 字样，已关注显示 "已关注" 或私信图标
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
                # 视频列表选择器
                video_locator = self.page.locator("[data-e2e='user-post-list'] a").first
                if not video_locator.is_visible(timeout=3000):
                    # 兜底选择器
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

                    # 先尝试点击评论输入区域的占位符来激活编辑器
                    self._activate_comment_input()
                    time.sleep(1.5)

                    # 先走已确认的精确 DOM，再回退到泛化选择器
                    comment_locator = self._find_comment_editor_from_exact_dom()

                    if comment_locator:
                        logger.info(f"找到评论输入框，准备输入: {comment_text}")
                        helper = self._human_helper()

                        # 聚焦评论输入框
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
