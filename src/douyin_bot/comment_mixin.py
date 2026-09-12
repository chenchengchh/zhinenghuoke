"""评论爬取相关方法Mixin。"""
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
from src.douyin_bot.human_interaction import HumanInteractionHelper
from .crawler_selectors import (
    COMMENT_SURFACE_SELECTORS,
    COMMENT_ITEM_SELECTORS,
    COMMENT_OPEN_TRIGGER_SELECTORS,
)


class CommentMixin:
    """评论爬取相关方法。"""
    COLLECT_REPLY_COMMENTS = False
    INCREMENTAL_COMMENT_EXPECTED_THRESHOLD = 80
    LARGE_COMMENT_EXPECTED_THRESHOLD = 300
    DEFAULT_COMMENT_INTERVAL = (3, 6)

    @staticmethod
    def _parse_filter_datetime(value: str) -> int:
        """解析前端传入的时间范围为秒级时间戳。"""
        if not value:
            return 0

        normalized = str(value).strip()
        if not normalized:
            return 0

        normalized = normalized.replace("Z", "+00:00")
        for candidate in (normalized, normalized.replace(" ", "T")):
            try:
                return int(datetime.fromisoformat(candidate).timestamp())
            except ValueError:
                continue

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(normalized, fmt).timestamp())
            except ValueError:
                continue
        return 0


    @staticmethod
    def _format_filter_datetime(timestamp: int) -> str:
        if not timestamp:
            return ""
        try:
            return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return ""


    @staticmethod
    def _classify_comment_api_url(url: str) -> str:
        normalized = (url or "").lower()
        if "aweme/v1/web/comment/list/reply" in normalized:
            return "reply"
        if "aweme/v1/web/comment/list" in normalized:
            return "comment"
        return ""


    @staticmethod
    def _response_indicates_risk_control(data: dict) -> bool:
        if not isinstance(data, dict):
            return False
        text_parts = [
            str(data.get("status_msg", "") or ""),
            str(data.get("message", "") or ""),
            str(data.get("prompts", "") or ""),
        ]
        combined = " ".join(text_parts)
        return bool(re.search(r"验证|验证码|频繁|异常|限制|稍后再试|risk|captcha", combined, re.IGNORECASE))


    @staticmethod
    def _extract_comment_page_payload(data: dict) -> Tuple[list, str, bool]:
        if not isinstance(data, dict):
            return [], "0", False
        comments = (
            data.get("comments")
            or data.get("reply_comments")
            or data.get("comment_list")
            or data.get("list")
            or []
        )
        if not isinstance(comments, list):
            comments = []
        cursor = str(
            data.get("cursor")
            or data.get("offset")
            or data.get("next_cursor")
            or data.get("reply_cursor")
            or "0"
        )
        has_more = bool(
            data.get("has_more")
            or data.get("has_more_reply")
            or data.get("more")
        )
        return comments, cursor, has_more


    def _iter_comment_items(self, comments: list, include_replies: bool = False) -> List[Tuple[dict, str, dict, int]]:
        flattened: List[Tuple[dict, str, dict, int]] = []
        for comment in comments or []:
            if not isinstance(comment, dict):
                continue
            flattened.append((comment, "", {}, 1))
        return flattened


    def _get_expected_comment_count(self, aweme_id: str) -> int:
        for item in self._search_data:
            if item.get("aweme_id") == aweme_id:
                try:
                    return int(item.get("comment_count", 0) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0


    def _detect_risk_control_page(self) -> str:
        try:
            page_text = self.page.evaluate(
                """() => (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 2000)"""
            )
        except Exception:
            return ""

        if not page_text:
            return ""
        match = re.search(r"(验证码|请完成验证|访问受限|操作过于频繁|稍后再试|账号异常|风险提示|验证后继续)", page_text)
        return match.group(1) if match else ""


    def _wait_for_comment_progress(
        self,
        previous_api_request_count: int,
        previous_total_comment_count: int,
        previous_pending_batch_count: int = 0,
        previous_latest_batch_timestamp: float = 0.0,
        timeout_seconds: float = 8.0,
        progress_callback: Optional[Callable[[dict], None]] = None,
        session: Optional[dict] = None,
        aweme_id: str = "",
        extra_detail: str = "",
    ) -> bool:
        deadline = time.time() + max(timeout_seconds, 1.0)
        while time.time() < deadline:
            self._report_comment_crawl_progress(
                progress_callback,
                session=session or {},
                aweme_id=aweme_id,
                extra_detail=extra_detail or "等待评论接口分页返回",
            )
            time.sleep(0.1)
            if self._stop_event.is_set():
                return False
            if self._detect_risk_control_page():
                return True
            if self.comment_interceptor:
                queue_stats = self.comment_interceptor.get_queue_stats()
                if (
                    int(queue_stats.get("pending_total", 0) or 0) > previous_pending_batch_count
                    or float(queue_stats.get("latest_timestamp", 0.0) or 0.0) > previous_latest_batch_timestamp
                ):
                    return True
            current_api = getattr(self, "_comment_api_request_count", previous_api_request_count)
            current_total = getattr(self, "_comment_total_count", previous_total_comment_count)
            if current_api > previous_api_request_count or current_total > previous_total_comment_count:
                return True
        return False


    def _build_comment_crawl_progress_detail(self, session: dict, extra_detail: str = "") -> str:
        session = session if isinstance(session, dict) else {}
        total_comments = max(int(session.get("total_comment_count", 0) or 0), 0)
        matched_comments = max(int(session.get("matched_comment_count", 0) or 0), 0)
        saved_customers = max(int(session.get("saved_customer_count", 0) or 0), 0)
        cursor = str(session.get("last_seen_cursor") or session.get("cursor") or "").strip()

        summary = (
            f"已采评论 {total_comments} 条，匹配 {matched_comments} 条，"
            f"入库 {saved_customers} 条"
        )
        if cursor:
            summary = f"{summary}，cursor={cursor}"

        prefix = str(extra_detail or "").strip()
        return f"{prefix}；{summary}" if prefix else summary


    def _report_comment_crawl_progress(
        self,
        progress_callback: Optional[Callable[[dict], None]],
        *,
        session: dict,
        aweme_id: str,
        extra_detail: str = "",
    ) -> None:
        if not callable(progress_callback):
            return

        payload = {
            "aweme_id": str(aweme_id or "").strip(),
            "detail": self._build_comment_crawl_progress_detail(session, extra_detail),
            "total_comments": max(int((session or {}).get("total_comment_count", 0) or 0), 0),
            "matched_comments": max(int((session or {}).get("matched_comment_count", 0) or 0), 0),
            "saved_customers": max(int((session or {}).get("saved_customer_count", 0) or 0), 0),
            "cursor": str((session or {}).get("last_seen_cursor") or (session or {}).get("cursor") or "").strip(),
            "has_more": bool((session or {}).get("has_more", False)),
            "api_request_count": max(int((session or {}).get("api_request_count", 0) or 0), 0),
            "reply_comment_count": max(int((session or {}).get("reply_comment_count", 0) or 0), 0),
            "top_level_comment_count": max(int((session or {}).get("top_level_comment_count", 0) or 0), 0),
        }
        with contextlib.suppress(Exception):
            progress_callback(payload)


    def _sleep_with_comment_crawl_progress(
        self,
        total_seconds: float,
        progress_callback: Optional[Callable[[dict], None]],
        *,
        session: dict,
        aweme_id: str,
        extra_detail: str = "",
        chunk_seconds: float = 2.0,
    ) -> bool:
        deadline = time.monotonic() + max(float(total_seconds or 0.0), 0.0)
        step = max(float(chunk_seconds or 0.0), 0.2)

        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            if remaining <= 0:
                self._report_comment_crawl_progress(
                    progress_callback,
                    session=session,
                    aweme_id=aweme_id,
                    extra_detail=extra_detail,
                )
                return not self._stop_event.is_set()

            if self._stop_event.is_set():
                return False

            self._report_comment_crawl_progress(
                progress_callback,
                session=session,
                aweme_id=aweme_id,
                extra_detail=extra_detail,
            )
            if not self._interruptible_sleep(min(step, remaining), chunk_seconds=min(step, 0.2)):
                return False


    def _inspect_comment_surface(self, detail_structure: Optional[dict] = None) -> dict:
        """识别当前页面是否已经存在评论面板，而不是默认假设必须先打开抽屉。"""
        try:
            detail_kind = self._resolve_comment_detail_kind(detail_structure)
            result = self.page.evaluate(
                """(payload) => {
                    const visible = (node) => !!(node && node.offsetParent !== null);
                    const surfaceSelectors = payload.surfaceSelectors || [];
                    const itemSelectors = payload.itemSelectors || [];
                    const replyPattern = /(展开|查看|全部|更多).{0,12}回复|共\\s*\\d+\\s*条回复/i;

                    let surface = null;
                    let usedSurfaceSelector = '';
                    for (const selector of surfaceSelectors) {
                        const node = document.querySelector(selector);
                        if (visible(node)) {
                            surface = node;
                            usedSurfaceSelector = selector;
                            break;
                        }
                    }

                    const countVisible = (selectorList, root = document) => {
                        for (const selector of selectorList) {
                            const nodes = Array.from(root.querySelectorAll(selector)).filter(visible);
                            if (nodes.length) {
                                return { count: nodes.length, selector };
                            }
                        }
                        return { count: 0, selector: '' };
                    };

                    let itemCount = 0;
                    let matchedItemSelector = '';
                    for (const selector of itemSelectors) {
                        const nodes = Array.from(document.querySelectorAll(selector)).filter(visible);
                        if (nodes.length) {
                            itemCount = nodes.length;
                            matchedItemSelector = selector;
                            break;
                        }
                    }

                    if (!surface && itemCount > 0) {
                        for (const selector of itemSelectors) {
                            const firstItem = Array.from(document.querySelectorAll(selector)).find(visible);
                            if (!firstItem) continue;
                            const ancestor = firstItem.closest(
                                '[data-e2e="comment-panel"], [data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                                '[class*="comment-list"], [class*="CommentList"], [class*="comment-panel"], [class*="commentContent"], ' +
                                '[class*="comment-drawer"], [class*="CommentDrawer"], [class*="commentContainer"], [class*="CommentContainer"], ' +
                                '[class*="commentWrap"], [class*="CommentWrap"], [class*="commentScroll"], .comment-main'
                            );
                            if (visible(ancestor)) {
                                surface = ancestor;
                                usedSurfaceSelector = selector + '::closest';
                                break;
                            }
                        }
                    }

                    const textPool = surface ? (surface.innerText || '') : '';
                    const replyRoot = surface || document.body;
                    const replyButtonCount = Array.from((replyRoot || document).querySelectorAll('button, a, [role="button"]'))
                        .filter((node) => {
                            if (!visible(node)) return false;
                            const text = (node.innerText || node.textContent || '').trim();
                            if (!replyPattern.test(text)) return false;
                            const rect = node.getBoundingClientRect();
                            if ((rect.height || 0) > 40) return false;
                            return true;
                        })
                        .length;

                    return {
                        has_surface: !!surface,
                        surface_selector: usedSurfaceSelector,
                        item_count: itemCount,
                        item_selector: matchedItemSelector,
                        reply_button_count: replyButtonCount,
                        text_length: textPool.trim().length,
                        has_comments: (itemCount > 0) || (!!surface && (replyButtonCount > 0 || textPool.trim().length > 80)),
                    };
                }""",
                {
                    "surfaceSelectors": self._get_comment_surface_selectors_for_layout(detail_kind),
                    "itemSelectors": self._get_comment_item_selectors_for_layout(detail_kind),
                },
            )
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logger.debug(f"识别评论面板失败: {e}")
            return {}


    def _ensure_comment_surface_ready(self, detail_structure: Optional[dict] = None) -> dict:
        """优先识别已渲染评论面板，仅在确实没有评论面板时才尝试点击评论入口。"""
        surface_info = self._inspect_comment_surface(detail_structure=detail_structure)
        if surface_info.get("has_comments"):
            logger.info(
                f"检测到评论面板已存在: selector={surface_info.get('surface_selector') or 'document'}, "
                f"items={surface_info.get('item_count', 0)}, replies={surface_info.get('reply_button_count', 0)}"
            )
            return surface_info

        logger.info("当前页面未发现稳定评论面板，尝试点击评论入口")
        self._open_comment_surface(detail_structure=detail_structure)
        self._interruptible_sleep(random.uniform(0.35, 0.6), chunk_seconds=0.12)
        return self._inspect_comment_surface(detail_structure=detail_structure)


    @staticmethod
    def _new_comment_session(requested_start_ts: int, requested_end_ts: int) -> dict:
        return {
            "session_id": "",
            "started_at": "",
            "matched_comment_count": 0,
            "saved_customer_count": 0,
            "total_comment_count": 0,
            "top_level_comment_count": 0,
            "reply_comment_count": 0,
            "duplicate_comment_count": 0,
            "time_filtered_count": 0,
            "keyword_filtered_count": 0,
            "history_recorded_count": 0,
            "seen_comment_ids": set(),
            "cursor": "0",
            "has_more": True,
            "request_count": 0,
            "reply_has_more": False,
            "reply_cursor_map": {},
            "api_request_count": 0,
            "reply_api_request_count": 0,
            "risk_control_detected": False,
            "risk_control_reason": "",
            "blocked_response_count": 0,
            "completeness_warning": "",
            "termination_reason": "",
            "reply_expand_clicks": 0,
            "expected_comment_count": 0,
            "visible_reply_button_count": 0,
            "requested_start_ts": requested_start_ts,
            "requested_end_ts": requested_end_ts,
            "reached_comment_end": False,
            "reply_collection_enabled": False,
            "history_cutoff_ts": 0,
            "history_cutoff_reached": False,
            "last_seen_cursor": "0",
            "request_limit_extensions": 0,
            "current_reply_button_count": 0,
            "reply_expand_no_progress_streak": 0,
            "reply_expand_success_count": 0,
            "reply_expand_false_positive_count": 0,
            "reply_failed_signature_counts": {},
            "next_reply_expand_after": 0.0,
            "reply_cooldown_wait_rounds": 0,
            "last_scroll_found": False,
            "last_scroll_near_bottom": False,
            "last_scroll_ratio": 0.0,
            "last_scroll_remaining_distance": 0.0,
            "last_scroll_near_bottom_threshold": 0.0,
            "page_tail_last_remaining_distance": 0.0,
            "page_tail_end_block_stall_rounds": 0,
            "initial_end_probe_attempts": 0,
            "deferred_initial_end_reason": "",
            "crawl_mode": "incremental",
            "completion_status": "",
            "fact_recorded_count": 0,
            "customer_linked_count": 0,
            "linked_sec_uids": [],
            "linked_reply_targets": [],
            "comment_anchor_map": {},
            "remaining_customer_quota": 0,
            "quota_reached": False,
            "dropped_batches_count": 0,
            "peak_pending_batches": 0,
            "latest_batch_id": 0,
            "duplicate_comment_batch_streak": 0,
            "no_new_comment_batch_streak": 0,
        }


    @classmethod
    def _is_incremental_comment_scan(cls, session: dict) -> bool:
        if not isinstance(session, dict):
            return False
        history_cutoff_ts = int(session.get("history_cutoff_ts", 0) or 0)
        expected_comment_count = int(session.get("expected_comment_count", 0) or 0)
        return bool(
            history_cutoff_ts
            or (0 < expected_comment_count <= cls.INCREMENTAL_COMMENT_EXPECTED_THRESHOLD)
        )


    def _capture_comment_queue_stats(self, session: dict) -> dict:
        if not self.comment_interceptor or not isinstance(session, dict):
            return {}
        try:
            stats = self.comment_interceptor.get_queue_stats()
        except Exception:
            return {}

        session["dropped_batches_count"] = max(
            int(session.get("dropped_batches_count", 0) or 0),
            int(stats.get("dropped_batches_count", 0) or 0),
        )
        session["peak_pending_batches"] = max(
            int(session.get("peak_pending_batches", 0) or 0),
            int(stats.get("peak_pending_batches", 0) or 0),
        )
        session["latest_batch_id"] = max(
            int(session.get("latest_batch_id", 0) or 0),
            int(stats.get("latest_batch_id", 0) or 0),
        )
        return stats


    @staticmethod
    def _resolve_comment_completion_status(session: dict) -> str:
        if not isinstance(session, dict):
            return "partial_due_to_limits"

        if str(session.get("termination_reason", "") or "") == "stop_requested":
            return "stopped_by_user"

        if str(session.get("termination_reason", "") or "") == "lead_quota_reached":
            return "completed_quota_limited"

        if bool(session.get("risk_control_detected")):
            return "partial_due_to_risk"

        reached_end = bool(session.get("reached_comment_end"))
        has_warning = bool(str(session.get("completeness_warning", "") or "").strip())
        dropped_batches = int(session.get("dropped_batches_count", 0) or 0)
        crawl_mode = str(session.get("crawl_mode", "incremental") or "incremental")
        reply_gap = CommentMixin._has_pending_reply_completion_risk(session)

        if reached_end and not has_warning and not reply_gap and dropped_batches <= 0:
            return "completed_incremental" if crawl_mode == "incremental" else "completed_full"

        return "partial_due_to_limits"


    @staticmethod
    def _resolve_remaining_customer_quota(session: dict) -> int:
        if not isinstance(session, dict):
            return 0
        quota_limit = max(int(session.get("remaining_customer_quota", 0) or 0), 0)
        if quota_limit <= 0:
            return 0
        saved_count = max(int(session.get("saved_customer_count", 0) or 0), 0)
        return max(quota_limit - saved_count, 0)


    def _mark_customer_quota_reached(self, session: dict) -> None:
        if not isinstance(session, dict):
            return
        session["quota_reached"] = True
        if not str(session.get("termination_reason", "") or "").strip():
            session["termination_reason"] = "lead_quota_reached"
        logger.info(
            f"当前视频已达到剩余用户配额，停止继续采集更多客户: "
            f"saved={int(session.get('saved_customer_count', 0) or 0)}, "
            f"quota={int(session.get('remaining_customer_quota', 0) or 0)}"
        )


    def _persist_matched_customers_with_quota(
        self,
        session: dict,
        matched_customers: list,
        customer_comment_links: list,
    ) -> list:
        if not matched_customers or not self.db:
            return []

        linked_sec_uid_list = session.setdefault("linked_sec_uids", [])

        def _log_saved_customer_trace(customer_data: dict, link_data: Optional[dict] = None) -> None:
            customer_data = customer_data if isinstance(customer_data, dict) else {}
            link_data = link_data if isinstance(link_data, dict) else {}
            sec_uid = str(customer_data.get("sec_uid", "") or "").strip()
            persisted_customer = None
            get_customer = getattr(self.db, "get_customer", None)
            if sec_uid and callable(get_customer):
                with contextlib.suppress(Exception):
                    persisted_customer = get_customer(sec_uid)
            final_saved_url = str(
                customer_data.get("first_source_video_url")
                or customer_data.get("source_video_url")
                or ""
            ).strip()
            first_saved_url = str(customer_data.get("first_source_video_url", "") or "").strip()
            if isinstance(persisted_customer, dict):
                final_saved_url = str(
                    persisted_customer.get("first_source_video_url")
                    or persisted_customer.get("source_video_url")
                    or ""
                ).strip()
                first_saved_url = str(persisted_customer.get("first_source_video_url", "") or "").strip()
            target_aweme_id = str((link_data or {}).get("aweme_id", "") or "").strip()
            payload = {
                "session_id": str(session.get("session_id", "") or "").strip(),
                "search_task_id": str(session.get("search_task_id", "") or "").strip(),
                "sec_uid": sec_uid,
                "nickname": str(customer_data.get("nickname", "") or "").strip(),
                "target_aweme_id": target_aweme_id,
                "comment_id": str((link_data or {}).get("comment_id", "") or "").strip(),
                "input_source_video_url": str(customer_data.get("source_video_url", "") or "").strip(),
                "input_first_source_video_url": str(customer_data.get("first_source_video_url", "") or "").strip(),
                "final_saved_url": final_saved_url,
                "first_saved_url": first_saved_url or final_saved_url,
                "comment_time": str(customer_data.get("comment_time", "") or "").strip(),
            }
            logger.info(
                "客户来源视频落库: "
                f"sec_uid={payload['sec_uid'] or '-'}, "
                f"target_aweme_id={payload['target_aweme_id'] or '-'}, "
                f"comment_id={payload['comment_id'] or '-'}, "
                f"input_url={payload['input_source_video_url'] or '-'}, "
                f"final_saved_url={payload['final_saved_url'] or '-'}"
            )
            self._append_crawl_trace("customer_source_video_saved", payload)

        def _append_linked_sec_uids(records: list) -> None:
            for link_data in list(records or []):
                sec_uid = str((link_data or {}).get("sec_uid", "") or "").strip()
                if sec_uid and sec_uid not in linked_sec_uid_list:
                    linked_sec_uid_list.append(sec_uid)

        remaining_quota = self._resolve_remaining_customer_quota(session)
        if remaining_quota <= 0 and int(session.get("remaining_customer_quota", 0) or 0) > 0:
            self._mark_customer_quota_reached(session)
            return []

        if int(session.get("remaining_customer_quota", 0) or 0) <= 0:
            session["saved_customer_count"] += int(self.db.add_customers_batch(matched_customers) or 0)
            linked_records = list(customer_comment_links or [])
            _append_linked_sec_uids(linked_records)
            for customer_data, link_data in zip(matched_customers, linked_records):
                _log_saved_customer_trace(customer_data, link_data)
            return linked_records

        linked_records = []
        for customer_data, link_data in zip(matched_customers, customer_comment_links):
            if remaining_quota <= 0:
                self._mark_customer_quota_reached(session)
                break
            inserted = int(self.db.add_customers_batch([customer_data]) or 0)
            linked_records.append(link_data)
            _append_linked_sec_uids([link_data])
            _log_saved_customer_trace(customer_data, link_data)
            if inserted > 0:
                session["saved_customer_count"] += inserted
                remaining_quota = max(remaining_quota - inserted, 0)
                if remaining_quota <= 0:
                    self._mark_customer_quota_reached(session)
                    break
        return linked_records


    @staticmethod
    def _append_linked_reply_targets(
        session: dict,
        linked_customer_comment_links: list,
        matched_comment_targets: list,
    ) -> None:
        append_linked_reply_targets(
            session,
            linked_customer_comment_links,
            matched_comment_targets,
            normalize_target=CommentMixin._normalize_reply_target,
        )


    @classmethod
    def _normalize_reply_target(cls, target: dict) -> dict:
        return normalize_reply_target(
            target,
            infer_detail_kind_from_url=cls._infer_detail_kind_from_url,
        )


    @classmethod
    def _build_matched_comment_target(
        cls,
        *,
        platform: str,
        aweme_id: str,
        video_url: str,
        comment_data: dict,
    ) -> dict:
        return build_matched_comment_target(
            platform=platform,
            aweme_id=aweme_id,
            video_url=video_url,
            comment_data=comment_data,
            infer_detail_kind_from_url=cls._infer_detail_kind_from_url,
            normalize_target=cls._normalize_reply_target,
        )


    def _persist_comment_batch_records(
        self,
        *,
        session: dict,
        platform: str,
        aweme_id: str,
        video_url: str,
        crawled_comment_records: list,
    ) -> None:
        persisted = persist_crawled_comment_records(
            db=self.db,
            session=session,
            platform=platform,
            aweme_id=aweme_id,
            video_url=video_url,
            crawled_comment_records=crawled_comment_records,
        )
        session["history_recorded_count"] += int(persisted.get("history_recorded_count", 0) or 0)
        session["fact_recorded_count"] += int(persisted.get("fact_recorded_count", 0) or 0)


    def _persist_matched_comment_batch(
        self,
        *,
        session: dict,
        matched_customers: list,
        customer_comment_links: list,
        matched_comment_targets: list,
    ) -> None:
        persisted = persist_matched_comment_batch(
            db=self.db,
            session=session,
            matched_customers=matched_customers,
            customer_comment_links=customer_comment_links,
            matched_comment_targets=matched_comment_targets,
            persist_matched_customers_with_quota=self._persist_matched_customers_with_quota,
            append_linked_reply_targets=self._append_linked_reply_targets,
        )
        linked_customer_comment_links = list(
            persisted.get("linked_customer_comment_links") or []
        )
        session["customer_linked_count"] += int(persisted.get("customer_linked_count", 0) or 0)
        if not linked_customer_comment_links:
            return
        linked_sec_uid_list = session.setdefault("linked_sec_uids", [])
        for link_data in linked_customer_comment_links:
            sec_uid = str((link_data or {}).get("sec_uid", "") or "").strip()
            if sec_uid and sec_uid not in linked_sec_uid_list:
                linked_sec_uid_list.append(sec_uid)


    def _update_comment_batch_progress(
        self,
        *,
        session: dict,
        response_kind: str,
        batch_new_comment_count: int,
        batch_existing_comment_count: int,
    ) -> None:
        progress = update_comment_batch_progress(
            session=session,
            response_kind=response_kind,
            batch_new_comment_count=batch_new_comment_count,
            batch_existing_comment_count=batch_existing_comment_count,
        )
        if progress.get("should_log_no_new_batch"):
            logger.info(
                "当前评论批次未发现新评论事实: "
                f"existing_comments={batch_existing_comment_count}, "
                f"duplicate_batch_streak={int(progress.get('duplicate_comment_batch_streak', 0) or 0)}, "
                f"no_new_batch_streak={int(progress.get('no_new_comment_batch_streak', 0) or 0)}, "
                f"has_more={session['has_more']}, cursor={session['cursor'] or ''}"
            )


    def _upsert_comment_crawl_session_snapshot(
        self,
        session: dict,
        *,
        aweme_id: str,
        video_url: str,
        finished: bool = False,
    ) -> None:
        if not self.db or not isinstance(session, dict):
            return

        payload = {
            "session_id": session.get("session_id", ""),
            "platform": "douyin",
            "aweme_id": aweme_id,
            "video_url": video_url,
            "search_task_id": session.get("search_task_id", ""),
            "crawl_mode": session.get("crawl_mode", "incremental"),
            "started_at": session.get("started_at", ""),
            "finished_at": datetime.now().isoformat() if finished else "",
            "comment_time_start": self._format_filter_datetime(session.get("requested_start_ts", 0)),
            "comment_time_end": self._format_filter_datetime(session.get("requested_end_ts", 0)),
            "reply_collection_enabled": False,
            "completion_status": session.get("completion_status", ""),
            "termination_reason": session.get("termination_reason", ""),
            "completeness_warning": session.get("completeness_warning", ""),
            "risk_control_detected": bool(session.get("risk_control_detected")),
            "risk_control_reason": session.get("risk_control_reason", ""),
            "history_cutoff_timestamp": session.get("history_cutoff_ts", 0),
            "expected_comment_count": session.get("expected_comment_count", 0),
            "top_level_comment_count": session.get("top_level_comment_count", 0),
            "reply_comment_count": session.get("reply_comment_count", 0),
            "total_comment_count": session.get("total_comment_count", 0),
            "matched_comment_count": session.get("matched_comment_count", 0),
            "saved_customer_count": session.get("saved_customer_count", 0),
            "duplicate_comment_count": session.get("duplicate_comment_count", 0),
            "time_filtered_count": session.get("time_filtered_count", 0),
            "keyword_filtered_count": session.get("keyword_filtered_count", 0),
            "history_recorded_count": session.get("history_recorded_count", 0),
            "fact_recorded_count": session.get("fact_recorded_count", 0),
            "customer_linked_count": session.get("customer_linked_count", 0),
            "api_request_count": session.get("api_request_count", 0),
            "reply_api_request_count": session.get("reply_api_request_count", 0),
            "reply_expand_clicks": session.get("reply_expand_clicks", 0),
            "dropped_batches_count": session.get("dropped_batches_count", 0),
            "peak_pending_batches": session.get("peak_pending_batches", 0),
            "latest_batch_id": session.get("latest_batch_id", 0),
        }
        self.db.upsert_comment_crawl_session(payload)


    @classmethod
    def _resolve_comment_crawl_limits(cls, expected_comment_count: int, incremental_scan: bool) -> tuple[int, int]:
        if incremental_scan:
            return 72, 90

        expected = max(int(expected_comment_count or 0), 0)
        if expected <= 0:
            return 120, 120

        dynamic_requests = max(120, min(260, int(expected / 8) + 24))
        dynamic_scrolls = max(120, min(280, int(dynamic_requests * 1.2)))
        return dynamic_requests, dynamic_scrolls


    @staticmethod
    def _should_extend_comment_crawl_limits(
        session: dict,
        *,
        incremental_scan: bool,
        max_requests: int,
    ) -> bool:
        if incremental_scan or not isinstance(session, dict):
            return False
        if not bool(session.get("has_more")):
            return False
        if bool(session.get("risk_control_detected")):
            return False
        if int(session.get("blocked_response_count", 0) or 0) > 0:
            return False
        if int(session.get("request_limit_extensions", 0) or 0) >= 2:
            return False

        expected = int(session.get("expected_comment_count", 0) or 0)
        collected_key = "total_comment_count" if bool(session.get("reply_collection_enabled")) else "top_level_comment_count"
        collected = int(session.get(collected_key, 0) or 0)
        api_requests = int(session.get("api_request_count", 0) or 0)

        if expected > 0 and collected >= expected:
            return False

        # 允许扩容的条件：未知总量仍在翻页 / 大评论量未过半 / 已用完请求额度但接口仍返回更多
        return bool(
            expected <= 0
            or (expected >= CommentMixin.LARGE_COMMENT_EXPECTED_THRESHOLD and collected < int(expected * 0.6))
            or api_requests >= max_requests
            or (collected > 0 and collected < max(int(expected * 0.8), int(collected * 1.5)))
        )


    @staticmethod
    def _extend_comment_crawl_limits(max_requests: int, max_scrolls: int) -> tuple[int, int]:
        return min(max_requests + 80, 320), min(max_scrolls + 100, 360)


    @classmethod
    def _resolve_comment_idle_limit(
        cls,
        *,
        incremental_scan: bool,
        has_more: bool,
        near_bottom: bool,
        scroll_ratio: float,
        expected_comment_count: int,
        collected_comment_count: int,
    ) -> int:
        idle_limit = 8 if incremental_scan else 12
        if has_more and not near_bottom:
            if scroll_ratio < 0.35:
                idle_limit = 14 if incremental_scan else 22
            elif scroll_ratio < 0.7:
                idle_limit = 10 if incremental_scan else 16

        expected = max(int(expected_comment_count or 0), 0)
        collected = max(int(collected_comment_count or 0), 0)
        if not incremental_scan and expected >= cls.LARGE_COMMENT_EXPECTED_THRESHOLD:
            progress_ratio = (collected / expected) if expected > 0 else 1.0
            if progress_ratio < 0.2:
                idle_limit = max(idle_limit, 28)
            elif progress_ratio < 0.5:
                idle_limit = max(idle_limit, 22)
            else:
                idle_limit = max(idle_limit, 16)

        return idle_limit


    def _resolve_effective_comment_idle_limit(
        self,
        session: dict,
        *,
        incremental_scan: bool,
        near_bottom: bool,
        scroll_ratio: float,
    ) -> int:
        idle_limit = self._resolve_comment_idle_limit(
            incremental_scan=incremental_scan,
            has_more=bool(session.get("has_more")),
            near_bottom=near_bottom,
            scroll_ratio=scroll_ratio,
            expected_comment_count=int(session.get("expected_comment_count", 0) or 0),
            collected_comment_count=self._resolve_completion_collected_comment_count(session),
        )
        if self._collect_reply_comments_enabled() and not bool(session.get("has_more")):
            idle_limit = max(idle_limit, self._resolve_reply_finish_idle_limit(session))
        return idle_limit


    @classmethod
    def _resolve_comment_bottom_retry_limit(
        cls,
        *,
        incremental_scan: bool,
        has_more: bool,
        expected_comment_count: int,
        collected_comment_count: int,
    ) -> int:
        if not has_more:
            return 0

        retry_limit = 1 if incremental_scan else 2
        expected = max(int(expected_comment_count or 0), 0)
        collected = max(int(collected_comment_count or 0), 0)
        if expected >= cls.LARGE_COMMENT_EXPECTED_THRESHOLD:
            progress_ratio = (collected / expected) if expected > 0 else 1.0
            if progress_ratio < 0.5:
                retry_limit = max(retry_limit, 3)
            else:
                retry_limit = max(retry_limit, 2)
        return retry_limit


    @staticmethod
    def _resolve_comment_surface_open_pause(*, already_loaded: bool = False) -> float:
        if already_loaded:
            return random.uniform(0.45, 0.95)
        return random.uniform(1.05, 2.35)


    @staticmethod
    def _build_humanized_comment_scroll_segments(total_distance: float) -> List[int]:
        distance = max(int(total_distance or 0), 0)
        if distance <= 0:
            return []

        if distance >= 1800:
            steps = random.randint(8, 11)
        elif distance >= 1100:
            steps = random.randint(7, 10)
        else:
            steps = random.randint(6, 9)

        base_segments = HumanInteractionHelper.build_scroll_segments(distance, steps=steps)
        if not base_segments:
            return []

        jittered = []
        for index, segment in enumerate(base_segments):
            progress = index / max(len(base_segments) - 1, 1)
            if progress < 0.25:
                low, high = 0.72, 0.96
            elif progress > 0.75:
                low, high = 0.78, 1.02
            else:
                low, high = 0.88, 1.18
            jittered.append(max(1, int(round(segment * random.uniform(low, high)))))

        total = sum(jittered) or 1
        scale = distance / total
        normalized = [max(1, int(round(segment * scale))) for segment in jittered]
        delta = distance - sum(normalized)
        normalized[-1] = max(1, normalized[-1] + delta)
        return normalized


    @staticmethod
    def _should_attempt_comment_bottom_retry(
        *,
        progressed: bool,
        has_more: bool,
        near_bottom: bool,
        scroll_found: bool,
        retry_rounds: int,
        retry_limit: int,
    ) -> bool:
        return bool(
            (not progressed)
            and has_more
            and near_bottom
            and scroll_found
            and retry_limit > 0
            and retry_rounds < retry_limit
        )


    @classmethod
    def _resolve_comment_volume_tier(cls, session: dict) -> str:
        if not isinstance(session, dict):
            return "normal"
        expected = max(int(session.get("expected_comment_count", 0) or 0), 0)
        total_comments = max(int(session.get("total_comment_count", 0) or 0), 0)
        api_requests = max(int(session.get("api_request_count", 0) or 0), 0)
        current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)

        if expected >= 1000 or total_comments >= 350 or api_requests >= 36 or current_reply_buttons >= 30:
            return "ultra"
        if (
            expected >= cls.LARGE_COMMENT_EXPECTED_THRESHOLD
            or total_comments >= 160
            or api_requests >= 18
            or current_reply_buttons >= 12
        ):
            return "large"
        return "normal"


    @classmethod
    def _resolve_comment_pacing_delay(
        cls,
        *,
        phase: str,
        incremental_scan: bool,
        session: dict,
        reply_collection_enabled: bool = False,
        idle_rounds: int = 0,
        same_cursor_streak: int = 0,
    ) -> float:
        volume_tier = cls._resolve_comment_volume_tier(session)
        blocked_responses = max(int((session or {}).get("blocked_response_count", 0) or 0), 0)
        risk_boost = blocked_responses > 0 or idle_rounds >= 2 or same_cursor_streak >= 3

        if phase == "post_scroll":
            low, high = (0.03, 0.08) if incremental_scan else (0.05, 0.12)
            if volume_tier == "large":
                low += 0.04
                high += 0.12
            elif volume_tier == "ultra":
                low += 0.08
                high += 0.22
            if risk_boost:
                high += 0.12
        elif phase == "bottom_retry":
            low, high = ((0.25, 0.55) if incremental_scan else (0.5, 1.0))
            if volume_tier == "large":
                low += 0.2
                high += 0.5
            elif volume_tier == "ultra":
                low += 0.5
                high += 0.9
            if risk_boost:
                high += 0.5
        elif phase == "slow_path":
            low, high = ((0.2, 0.45) if incremental_scan else (0.45, 0.95))
            if volume_tier == "large":
                low += 0.15
                high += 0.35
            elif volume_tier == "ultra":
                low += 0.35
                high += 0.75
            if risk_boost:
                high += 0.45
        elif phase == "volume_cooldown":
            low, high = (0.0, 0.0) if incremental_scan else (0.8, 1.6)
            if volume_tier == "large":
                low += 0.25
                high += 0.45
            elif volume_tier == "ultra":
                low += 0.55
                high += 0.9
            if risk_boost:
                high += 0.4
        else:
            return 0.0

        if high <= 0:
            return 0.0
        return random.uniform(max(low, 0.0), max(high, max(low, 0.0)))


    @classmethod
    def _should_apply_comment_volume_cooldown(
        cls,
        session: dict,
        *,
        incremental_scan: bool,
        scroll_count: int,
    ) -> bool:
        if incremental_scan or not isinstance(session, dict):
            return False
        if not bool(session.get("has_more")):
            return False
        volume_tier = cls._resolve_comment_volume_tier(session)
        if volume_tier == "ultra":
            return scroll_count > 0 and scroll_count % 6 == 0
        if volume_tier == "large":
            return scroll_count > 0 and scroll_count % 10 == 0
        return False


    def _collect_reply_comments_enabled(self) -> bool:
        return False


    def _comment_aggressive_mode_enabled(self, session: Optional[dict] = None) -> bool:
        if isinstance(session, dict) and "aggressive_mode" in session:
            return bool(session.get("aggressive_mode"))
        return bool(getattr(self, "_comment_aggressive_mode", False))


    def _resolve_completion_collected_comment_count(self, session: dict) -> int:
        if not isinstance(session, dict):
            return 0
        return max(int(session.get("top_level_comment_count", 0) or 0), 0)


    @staticmethod
    def _resolve_comment_completion_tolerance(expected_comment_count: int) -> int:
        expected = max(int(expected_comment_count or 0), 0)
        if expected <= 0:
            return 0
        if expected <= 20:
            return 2
        if expected <= 80:
            return 4
        if expected <= 200:
            return 8
        return max(12, int(expected * 0.08))


    @staticmethod
    def _has_pending_reply_completion_risk(session: dict) -> bool:
        if not isinstance(session, dict):
            return False
        if not bool(session.get("reply_collection_enabled")):
            return False
        if bool(session.get("has_more")):
            return False

        seen_reply_buttons = max(int(session.get("visible_reply_button_count", 0) or 0), 0)
        current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)
        reply_comment_count = max(int(session.get("reply_comment_count", 0) or 0), 0)
        false_positive_count = max(int(session.get("reply_expand_false_positive_count", 0) or 0), 0)
        no_progress_streak = max(int(session.get("reply_expand_no_progress_streak", 0) or 0), 0)

        if seen_reply_buttons <= 0:
            return False
        if current_reply_buttons >= 12:
            return True
        if current_reply_buttons >= 5 and (false_positive_count > 0 or no_progress_streak >= 3):
            return True
        if current_reply_buttons > 0 and reply_comment_count <= 0:
            return True
        return False


    @staticmethod
    def _update_comment_scroll_state(session: dict, scroll_result: dict) -> None:
        if not isinstance(session, dict):
            return
        if not isinstance(scroll_result, dict):
            return
        session["last_scroll_found"] = bool(scroll_result.get("found"))
        session["last_scroll_near_bottom"] = bool(scroll_result.get("near_bottom"))
        session["last_scroll_ratio"] = float(scroll_result.get("scroll_ratio", 0.0) or 0.0)
        session["last_scroll_remaining_distance"] = float(scroll_result.get("remaining_distance", 0.0) or 0.0)
        session["last_scroll_near_bottom_threshold"] = float(
            scroll_result.get("near_bottom_threshold", 0.0) or 0.0
        )


    @staticmethod
    def _has_pending_page_tail_scroll_risk(session: dict) -> bool:
        if not isinstance(session, dict):
            return False
        if not bool(session.get("reply_collection_enabled")):
            return False
        if bool(session.get("has_more")):
            return False
        if not bool(session.get("last_scroll_found")):
            return False

        remaining_distance = max(float(session.get("last_scroll_remaining_distance", 0.0) or 0.0), 0.0)
        near_bottom_threshold = max(float(session.get("last_scroll_near_bottom_threshold", 0.0) or 0.0), 0.0)
        scroll_ratio = max(float(session.get("last_scroll_ratio", 0.0) or 0.0), 0.0)
        current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)

        if remaining_distance <= 0:
            return False
        if remaining_distance >= max(near_bottom_threshold * 2.5, 1800.0):
            return True
        if remaining_distance >= 900.0 and scroll_ratio < 0.88:
            return True
        if current_reply_buttons >= 12 and remaining_distance >= max(near_bottom_threshold * 1.5, 800.0):
            return True
        return False


    def _resolve_tail_completion_probe_limit(self, session: dict) -> int:
        if not self._collect_reply_comments_enabled():
            return 0
        limit = 3
        if self._has_pending_reply_completion_risk(session):
            limit = max(limit, 5)
        if self._has_pending_page_tail_scroll_risk(session):
            remaining_distance = max(float(session.get("last_scroll_remaining_distance", 0.0) or 0.0), 0.0)
            current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)
            distance_bonus = max(min(int(math.ceil(remaining_distance / 3200.0)), 8), 1)
            reply_bonus = 2 if current_reply_buttons >= 80 else (1 if current_reply_buttons >= 24 else 0)
            limit = max(limit, min(12, 5 + distance_bonus + reply_bonus))
        if self._comment_aggressive_mode_enabled(session):
            limit = min(limit, 2 if self._has_pending_reply_completion_risk(session) else 1)
        return limit


    @staticmethod
    def _consume_page_tail_end_block(session: dict) -> dict:
        if not isinstance(session, dict):
            return {"block_end": False, "remaining_distance": 0.0, "stall_rounds": 0, "progressed": False}
        if not CommentMixin._has_pending_page_tail_scroll_risk(session):
            session["page_tail_last_remaining_distance"] = 0.0
            session["page_tail_end_block_stall_rounds"] = 0
            return {"block_end": False, "remaining_distance": 0.0, "stall_rounds": 0, "progressed": False}

        remaining_distance = max(float(session.get("last_scroll_remaining_distance", 0.0) or 0.0), 0.0)
        last_remaining_distance = max(float(session.get("page_tail_last_remaining_distance", 0.0) or 0.0), 0.0)
        progress_threshold = max(min(last_remaining_distance * 0.03, 1200.0), 480.0)
        progressed = last_remaining_distance <= 0 or remaining_distance < max(last_remaining_distance - progress_threshold, 0.0)
        stall_rounds = 0 if progressed else max(int(session.get("page_tail_end_block_stall_rounds", 0) or 0), 0) + 1
        session["page_tail_last_remaining_distance"] = remaining_distance
        session["page_tail_end_block_stall_rounds"] = stall_rounds
        return {
            "block_end": progressed or stall_rounds <= 4,
            "remaining_distance": remaining_distance,
            "stall_rounds": stall_rounds,
            "progressed": progressed,
        }


    def _should_probe_tail_comment_completion(self, session: dict) -> bool:
        if not isinstance(session, dict):
            return False
        if not self._collect_reply_comments_enabled():
            return False
        if bool(session.get("has_more")):
            return False
        if bool(session.get("risk_control_detected")):
            return False
        aggressive_mode = self._comment_aggressive_mode_enabled(session)
        if self._has_pending_page_tail_scroll_risk(session):
            if aggressive_mode:
                remaining_distance = max(float(session.get("last_scroll_remaining_distance", 0.0) or 0.0), 0.0)
                current_reply_buttons = max(int(session.get("current_reply_button_count", 0) or 0), 0)
                return bool(remaining_distance >= 1800.0 or current_reply_buttons >= 18)
            return True
        if self._has_pending_reply_completion_risk(session):
            return True

        expected = max(int(session.get("expected_comment_count", 0) or 0), 0)
        if expected <= 0:
            return False

        collected = self._resolve_completion_collected_comment_count(session)
        tolerance = self._resolve_comment_completion_tolerance(expected)
        if aggressive_mode:
            tolerance = max(tolerance, int(expected * 0.18))
        return bool(collected + tolerance < expected)


    @staticmethod
    def _should_defer_initial_comment_end(session: dict, *, performed_action: bool) -> bool:
        if not isinstance(session, dict):
            return False
        if not bool(session.get("reply_collection_enabled")):
            return False
        if bool(performed_action):
            return False
        if bool(session.get("aggressive_mode")):
            return int(session.get("initial_end_probe_attempts", 0) or 0) < 1
        return int(session.get("initial_end_probe_attempts", 0) or 0) < 3


    def _recover_comment_surface_for_pagination(
        self,
        *,
        detail_structure: dict,
        session: dict,
        current_surface_selector: str = "",
        progress_callback: Optional[Callable[[dict], None]] = None,
        aweme_id: str = "",
    ) -> dict:
        logger.info(
            "评论分页长时间未推进，尝试重新确认评论面板并重触发分页容器"
        )
        surface_info = self._inspect_comment_surface()
        if not surface_info.get("has_comments"):
            surface_info = self._ensure_comment_surface_ready()
        if not surface_info.get("has_comments"):
            self._open_comment_surface(detail_structure=detail_structure)
            self._sleep_with_comment_crawl_progress(
                random.uniform(0.8, 1.4),
                progress_callback,
                session=session,
                aweme_id=aweme_id,
                extra_detail="重新打开评论面板后等待评论接口恢复",
                chunk_seconds=0.6,
            )
            settled_surface = self._wait_for_comment_bootstrap(
                timeout_seconds=1.8,
                progress_callback=progress_callback,
                session=session,
                aweme_id=aweme_id,
                extra_detail="等待恢复后的评论面板重新稳定",
            )
            if settled_surface.get("has_comments"):
                surface_info = settled_surface

        self._update_reply_surface_stats(session, surface_info)
        if surface_info.get("has_comments"):
            scroll_hint = surface_info.get("surface_selector") or current_surface_selector or ""
            try:
                self._scroll_comment_list(scroll_hint)
            except Exception:
                pass
        return surface_info if isinstance(surface_info, dict) else {}


    def _consume_comment_api_batches(
        self,
        session: dict,
        aweme_id: str,
        video_url: str,
        target_keywords: list,
        skip_crawled: bool,
        platform: str,
        video_title: str = "",
        author_name: str = "",
    ) -> int:
        """消费评论 API 拦截器中的批次数据，统一完成分页推进、过滤与入库。"""
        processed_batches = 0
        self._capture_comment_queue_stats(session)
        batches = self.comment_interceptor.drain_batches()
        if not batches:
            return processed_batches

        for batch in batches:
            processed_batches += 1
            session["latest_batch_id"] = max(
                int(session.get("latest_batch_id", 0) or 0),
                int(batch.get("batch_id", 0) or 0),
            )
            response_kind = batch.get("kind", "")
            url = str(batch.get("url", "") or "")
            status = int(batch.get("status", 0) or 0)
            data = batch.get("data")
            query = batch.get("query") or {}

            # 校验接口返回的 aweme_id 是否与目标视频一致
            request_aweme_id = str(query.get("aweme_id", [""])[0])
            if request_aweme_id and request_aweme_id != aweme_id:
                logger.warning(f"发现非目标视频的评论API请求 (目标: {aweme_id}, 实际: {request_aweme_id})，可能视频已自动连播/跳转，该批次将被丢弃。")
                session["wrong_video_detected"] = True
                continue

            session["api_request_count"] += 1
            self._comment_api_request_count = session["api_request_count"]
            if response_kind == "reply":
                session["reply_api_request_count"] += 1
                self._comment_reply_api_request_count = session["reply_api_request_count"]

            if status in (401, 403, 429):
                session["risk_control_detected"] = True
                session["risk_control_reason"] = f"评论接口返回状态 {status}"
                session["blocked_response_count"] += 1
                logger.warning(f"{session['risk_control_reason']}, URL: {url[:120]}")
                continue

            if status != 200:
                logger.warning(f"评论API响应状态非200: {status}, URL: {url[:100]}")
                continue

            if not isinstance(data, dict):
                logger.debug(f"评论API响应数据为空或非字典: {url[:100]}")
                continue

            if self._response_indicates_risk_control(data):
                session["risk_control_detected"] = True
                session["risk_control_reason"] = str(data.get("status_msg") or data.get("message") or "评论接口触发风控")
                session["blocked_response_count"] += 1
                logger.warning(f"评论接口疑似触发风控: {session['risk_control_reason']}")
                continue

            if response_kind == "reply" and not self._collect_reply_comments_enabled():
                continue

            comments, next_cursor, response_has_more = self._extract_comment_page_payload(data)
            if not comments:
                logger.debug(f"{response_kind}评论列表为空")
                if response_kind == "comment":
                    session["has_more"] = False
                else:
                    session["reply_has_more"] = False
                continue

            if response_kind == "comment":
                session["cursor"] = next_cursor
                session["has_more"] = response_has_more
            else:
                session["reply_has_more"] = response_has_more
                parent_comment_id = str(query.get("comment_id", [""])[0] or query.get("cid", [""])[0])
                if parent_comment_id:
                    session["reply_cursor_map"][parent_comment_id] = next_cursor

            logger.info(
                f"{'回复' if response_kind == 'reply' else '一级'}评论接口返回 {len(comments)} 条, "
                f"has_more={response_has_more}, cursor={next_cursor}"
            )

            matched_customers = []
            customer_comment_links = []
            matched_comment_targets = []
            crawled_comment_records = []
            batch_new_comment_count = 0
            batch_existing_comment_count = 0
            for comment, parent_cid, parent_comment, comment_level in self._iter_comment_items(
                comments,
                include_replies=False,
            ):
                try:
                    comment_data = self._parse_comment(
                        comment,
                        video_url,
                        target_keywords,
                        parent_cid=parent_cid,
                        parent_comment=parent_comment,
                        comment_level=comment_level,
                        video_title=video_title,
                        author_name=author_name,
                    )
                    if not comment_data:
                        continue

                    comment_id = comment_data.get("cid", "")
                    if comment_id and comment_id in session["seen_comment_ids"]:
                        continue
                    if comment_id:
                        session["seen_comment_ids"].add(comment_id)
                        session.setdefault("comment_anchor_map", {})[comment_id] = self._build_comment_anchor(comment_data)

                    session["total_comment_count"] += 1
                    self._comment_total_count = session["total_comment_count"]
                    
                    from src.common.crawl_limit_service import get_crawl_limit_service
                    limit_service = get_crawl_limit_service()
                    limit_service.increment_today_count(1)
                    
                    session["top_level_comment_count"] += 1

                    decision = evaluate_comment_batch_item(
                        comment_data=comment_data,
                        platform=platform,
                        aweme_id=aweme_id,
                        video_url=video_url,
                        search_task_id=str(session.get("search_task_id", "") or ""),
                        requested_start_ts=int(session.get("requested_start_ts", 0) or 0),
                        requested_end_ts=int(session.get("requested_end_ts", 0) or 0),
                        skip_crawled=skip_crawled,
                        has_crawled_comment=lambda p, a, c: bool(
                            self.db and self.db.has_crawled_comment(p, a, c)
                        ),
                        build_customer_data=self._build_customer_data,
                        build_matched_comment_target=self._build_matched_comment_target,
                    )

                    if decision.is_time_filtered:
                        session["time_filtered_count"] += 1

                    if decision.should_skip_historical:
                        session["duplicate_comment_count"] += 1
                        batch_existing_comment_count += 1
                        continue

                    batch_new_comment_count += 1
                    crawled_comment_records.append(decision.comment_data)
                    if decision.is_time_filtered:
                        continue
                    if decision.is_target:
                        session["matched_comment_count"] += 1
                        matched_customers.append(decision.customer_data)
                        customer_comment_links.append(decision.customer_comment_link)
                        matched_comment_targets.append(decision.matched_comment_target)
                        logger.info(f"找到目标评论: {comment_data['nickname']} - {comment_data['text'][:30]}...")
                    else:
                        session["keyword_filtered_count"] += 1
                except Exception as e:
                    logger.debug(f"解析单条评论失败: {e}")

            if crawled_comment_records:
                self._persist_comment_batch_records(
                    session=session,
                    platform=platform,
                    aweme_id=aweme_id,
                    video_url=video_url,
                    crawled_comment_records=crawled_comment_records,
                )
            if matched_customers:
                self._persist_matched_comment_batch(
                    session=session,
                    matched_customers=matched_customers,
                    customer_comment_links=customer_comment_links,
                    matched_comment_targets=matched_comment_targets,
                )
            if session.get("quota_reached"):
                break
            self._update_comment_batch_progress(
                session=session,
                response_kind=response_kind,
                batch_new_comment_count=batch_new_comment_count,
                batch_existing_comment_count=batch_existing_comment_count,
            )

        self._capture_comment_queue_stats(session)
        return processed_batches


    def _wait_for_comment_bootstrap(
        self,
        timeout_seconds: float = 4.0,
        progress_callback: Optional[Callable[[dict], None]] = None,
        session: Optional[dict] = None,
        aweme_id: str = "",
        extra_detail: str = "",
    ) -> dict:
        """
        在视频页初始阶段短轮询评论面板或首批评论接口，避免使用固定长时间 sleep。
        """
        deadline = time.time() + max(timeout_seconds, 0.5)
        latest_surface_info = {}
        while time.time() < deadline:
            self._report_comment_crawl_progress(
                progress_callback,
                session=session or {},
                aweme_id=aweme_id,
                extra_detail=extra_detail or "等待评论面板或首批评论接口就绪",
            )
            if self._stop_event.is_set():
                return latest_surface_info
            if self._detect_risk_control_page():
                return latest_surface_info
            if self.comment_interceptor:
                queue_stats = self.comment_interceptor.get_queue_stats()
                if (
                    int(queue_stats.get("pending_total", 0) or 0) > 0
                    or int(queue_stats.get("pending_reply", 0) or 0) > 0
                    or float(queue_stats.get("latest_timestamp", 0.0) or 0.0) > 0.0
                ):
                    return latest_surface_info
            latest_surface_info = self._inspect_comment_surface()
            if latest_surface_info.get("has_comments"):
                return latest_surface_info
            if (
                getattr(self, "_comment_api_request_count", 0) > 0
                or getattr(self, "_comment_total_count", 0) > 0
                or getattr(self, "_comment_reply_api_request_count", 0) > 0
                or getattr(self, "_comment_reply_total_count", 0) > 0
            ):
                return latest_surface_info
            time.sleep(0.15)
        return latest_surface_info


    def _open_comment_surface(self, detail_structure: Optional[dict] = None):
        """兼容新的评论面板命名，统一复用现有打开逻辑。"""
        self._open_comment_drawer(detail_structure=detail_structure)


    def _extract_comment_trigger_meta(self, element) -> Dict[str, Any]:
        try:
            meta = element.evaluate(
                """(node) => {
                    const rect = node.getBoundingClientRect();
                    const closestLink = node.closest('a[href]');
                    return {
                        text: (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim(),
                        class_name: String(node.className || ''),
                        aria_label: String(node.getAttribute?.('aria-label') || ''),
                        data_e2e: String(node.getAttribute?.('data-e2e') || ''),
                        href: String(node.getAttribute?.('href') || ''),
                        role: String(node.getAttribute?.('role') || ''),
                        tag_name: String(node.tagName || '').toLowerCase(),
                        has_link_ancestor: !!(closestLink && closestLink !== node),
                        width: Math.round(rect.width || 0),
                        height: Math.round(rect.height || 0),
                    };
                }"""
            )
            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.debug(f"读取评论入口候选元数据失败: {e}")
            return {}


    @staticmethod
    def _has_strong_comment_trigger_signal(meta: Dict[str, Any]) -> bool:
        text = str(meta.get("text", "") or "")
        class_name = str(meta.get("class_name", "") or "").lower()
        aria_label = str(meta.get("aria_label", "") or "").lower()
        data_e2e = str(meta.get("data_e2e", "") or "").lower()
        href = str(meta.get("href", "") or "").lower()
        return any(
            (
                "评论" in text,
                "comment" in class_name,
                "comment" in aria_label,
                "comment" in data_e2e,
                "comment" in href,
            )
        )


    def _click_precise_comment_trigger(self, selectors: List[str]) -> bool:
        """只点击明确的评论入口选择器，不做泛化猜测。"""
        for selector in selectors:
            try:
                elements = self.page.query_selector_all(selector)
            except Exception as e:
                logger.debug(f"查询评论入口失败({selector}): {e}")
                continue

            for element in elements:
                try:
                    if not (element and element.is_visible()):
                        continue
                    meta = self._extract_comment_trigger_meta(element)
                    strong_comment_signal = self._has_strong_comment_trigger_signal(meta)
                    href = str(meta.get("href", "") or "").lower()
                    tag_name = str(meta.get("tag_name", "") or "").lower()
                    role = str(meta.get("role", "") or "").lower()
                    if (meta.get("has_link_ancestor") or tag_name == "a" or role == "link") and not strong_comment_signal:
                        continue
                    if href and any(part in href for part in ("/user/", "/music/", "/hashtag/")):
                        if not strong_comment_signal:
                            continue
                    if href and any(part in href for part in ("/note/", "/video/")) and not strong_comment_signal:
                        continue
                    try:
                        if not self._human_helper().click_target(element):
                            element.click()
                    except Exception as click_error:
                        logger.debug(f"评论入口常规点击失败，尝试DOM点击兜底: {click_error}")
                        try:
                            element.evaluate(
                                """(node) => {
                                    const fire = (type) => node.dispatchEvent(
                                        new MouseEvent(type, {
                                            bubbles: true,
                                            cancelable: true,
                                            composed: true,
                                            view: window,
                                        })
                                    );
                                    try { node.focus?.(); } catch (e) {}
                                    try { fire('pointerdown'); } catch (e) {}
                                    try { fire('mousedown'); } catch (e) {}
                                    try { fire('pointerup'); } catch (e) {}
                                    try { fire('mouseup'); } catch (e) {}
                                    try { fire('click'); } catch (e) {}
                                    return true;
                                }"""
                            )
                        except Exception as fallback_error:
                            logger.debug(f"评论入口DOM点击兜底失败({selector}): {fallback_error}")
                            if self._dismiss_login_popup_if_present():
                                try:
                                    element.click()
                                except Exception:
                                    continue
                            else:
                                continue
                    logger.info(
                        "通过精确评论入口打开评论区: "
                        f"selector={selector} text={meta.get('text', '')} class={meta.get('class_name', '')}"
                    )
                    time.sleep(self._resolve_comment_surface_open_pause())
                    if self._check_comment_drawer_open() or self._comment_api_request_count > 0:
                        return True
                except Exception as e:
                    logger.debug(f"点击评论入口失败({selector}): {e}")
                    continue
        return False


    @staticmethod
    def _build_comment_anchor(comment_data: dict) -> dict:
        comment_data = comment_data if isinstance(comment_data, dict) else {}
        user = comment_data.get("user") if isinstance(comment_data.get("user"), dict) else {}
        text = str(comment_data.get("text", "") or comment_data.get("comment_text", "") or "").strip()
        return {
            "comment_id": str(comment_data.get("cid", "") or comment_data.get("comment_id", "") or "").strip(),
            "nickname": str(comment_data.get("nickname", "") or user.get("nickname", "") or "").strip(),
            "sec_uid": str(comment_data.get("sec_uid", "") or user.get("sec_uid", "") or "").strip(),
            "unique_id": str(comment_data.get("unique_id", "") or user.get("unique_id", "") or "").strip(),
            "comment_text": text,
            "comment_text_prefix": text[:24],
            "comment_text_short_prefix": text[:12],
            "create_time": int(comment_data.get("create_time", 0) or 0),
            "comment_level": max(int(comment_data.get("comment_level", 1) or 1), 1),
        }


    def _build_comment_anchor_from_api_item(
        self,
        comment: dict,
        *,
        parent_comment: Optional[dict] = None,
        comment_level: int = 1,
    ) -> dict:
        comment = comment if isinstance(comment, dict) else {}
        parent_comment = parent_comment if isinstance(parent_comment, dict) else {}
        parent_comment_id = str(
            comment.get("reply_id", "")
            or comment.get("reply_comment_id", "")
            or comment.get("reply_to_comment_id", "")
            or comment.get("parent_cid", "")
            or parent_comment.get("cid", "")
            or ""
        ).strip()
        root_comment_id = str(
            comment.get("root_cid", "")
            or comment.get("root_comment_id", "")
            or parent_comment_id
            or comment.get("cid", "")
            or ""
        ).strip()
        anchor = self._build_comment_anchor({
            **comment,
            "comment_level": max(int(comment_level or 1), 1),
        })
        anchor["parent_comment_id"] = parent_comment_id
        anchor["root_comment_id"] = root_comment_id
        anchor["reply_parent_probe_id"] = parent_comment_id or root_comment_id
        if parent_comment:
            parent_anchor = self._build_comment_anchor({
                **parent_comment,
                "comment_level": 1,
            })
            anchor["parent_anchor"] = parent_anchor
        return self._normalize_reply_target(anchor)


    @staticmethod
    def _merge_live_anchor_fields(base_anchor: Optional[dict], incoming_anchor: Optional[dict]) -> dict:
        base_anchor = dict(base_anchor or {})
        incoming_anchor = dict(incoming_anchor or {})
        merged = dict(base_anchor)
        prioritized_fields = [
            "comment_id",
            "parent_comment_id",
            "root_comment_id",
            "nickname",
            "sec_uid",
            "unique_id",
            "comment_text",
            "comment_text_prefix",
            "comment_text_short_prefix",
            "create_time",
            "comment_level",
            "reply_parent_probe_id",
        ]
        for field in prioritized_fields:
            current_value = merged.get(field)
            incoming_value = incoming_anchor.get(field)
            if field in {"create_time", "comment_level"}:
                if (not current_value or int(current_value or 0) <= 0) and int(incoming_value or 0) > 0:
                    merged[field] = int(incoming_value or 0)
                continue
            if (not str(current_value or "").strip()) and str(incoming_value or "").strip():
                merged[field] = incoming_value
        base_parent_anchor = base_anchor.get("parent_anchor") if isinstance(base_anchor.get("parent_anchor"), dict) else {}
        incoming_parent_anchor = incoming_anchor.get("parent_anchor") if isinstance(incoming_anchor.get("parent_anchor"), dict) else {}
        if incoming_parent_anchor:
            merged["parent_anchor"] = CommentMixin._merge_live_anchor_fields(base_parent_anchor, incoming_parent_anchor)
        elif base_parent_anchor:
            merged["parent_anchor"] = dict(base_parent_anchor)
        return merged


    def _build_live_comment_anchor_index(self, aweme_id: str = "") -> dict:
        if not self.comment_interceptor:
            return {}
        batches = []
        with contextlib.suppress(Exception):
            batches = self.comment_interceptor.peek_batches()
        if not batches:
            return {}

        comment_index = {}
        expected_aweme_id = str(aweme_id or "").strip()
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            data = batch.get("data")
            if not isinstance(data, dict):
                continue
            query = batch.get("query") if isinstance(batch.get("query"), dict) else {}
            batch_aweme_id = str(
                (query.get("aweme_id", [""])[0] if isinstance(query.get("aweme_id"), list) else "")
                or (query.get("item_id", [""])[0] if isinstance(query.get("item_id"), list) else "")
                or data.get("aweme_id", "")
                or ""
            ).strip()
            if expected_aweme_id and batch_aweme_id and batch_aweme_id != expected_aweme_id:
                continue
            comments, _next_cursor, _has_more = self._extract_comment_page_payload(data)
            if not comments:
                continue
            for comment, _parent_cid, parent_comment, comment_level in self._iter_comment_items(
                comments,
                include_replies=False,
            ):
                anchor = self._build_comment_anchor_from_api_item(
                    comment,
                    parent_comment=parent_comment,
                    comment_level=comment_level,
                )
                comment_id = str(anchor.get("comment_id", "") or "").strip()
                if not comment_id:
                    continue
                existing_anchor = comment_index.get(comment_id, {})
                comment_index[comment_id] = self._merge_live_anchor_fields(existing_anchor, anchor)
        return comment_index


    def crawl_comments(
        self,
        video_url: str,
        target_keywords: list = None,
        comment_time_start: str = "",
        comment_time_end: str = "",
        skip_crawled: bool = True,
        video_title: str = "",
        author_name: str = "",
        search_keyword: str = "",
        search_task_id: str = "",
        remaining_customer_quota: int = 0,
        aggressive_mode: bool = False,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """
        爬取视频评论
        
        使用API拦截方式获取评论，支持分页加载
        
        Args:
            video_url: 视频URL
            target_keywords: 评论关键词过滤列表，None表示不过滤
            comment_time_start: 评论时间下限
            comment_time_end: 评论时间上限
            skip_crawled: 是否跳过历史已爬取评论
            
        Returns:
            dict: 当前视频的爬取统计
        """
        logger.info(f"正在抓取评论: {video_url}")
        
        # 重置停止标志
        self._stop_event.clear()
        platform = "douyin"
        requested_start_ts = self._parse_filter_datetime(comment_time_start)
        requested_end_ts = self._parse_filter_datetime(comment_time_end)
        if requested_start_ts and requested_end_ts and requested_start_ts > requested_end_ts:
            requested_start_ts, requested_end_ts = requested_end_ts, requested_start_ts
        session = self._new_comment_session(requested_start_ts, requested_end_ts)
        session["started_at"] = datetime.now().isoformat()
        session["search_task_id"] = str(search_task_id or "").strip()
        session["search_keyword"] = str(search_keyword or "").strip()
        session["remaining_customer_quota"] = max(int(remaining_customer_quota or 0), 0)
        
        # 从URL提取视频ID
        aweme_id = self._extract_aweme_id(video_url)
        if not aweme_id:
            logger.warning(f"无法从URL提取视频ID: {video_url}")
            return {
                "video_url": video_url,
                "aweme_id": "",
                "total_comments": 0,
                "matched_comments": 0,
                "saved_customers": 0,
                "duplicate_comments": 0,
                "time_filtered_comments": 0,
                "keyword_filtered_comments": 0,
                "history_recorded_comments": 0,
                "top_level_comments": 0,
                "reply_comments": 0,
                "expected_comment_count": 0,
                "reached_comment_end": False,
                "risk_control_detected": False,
                "risk_control_reason": "",
                "history_cutoff_time": "",
                "requested_time_start": self._format_filter_datetime(requested_start_ts),
                "requested_time_end": self._format_filter_datetime(requested_end_ts),
            }
        
        if session["search_keyword"]:
            logger.info(
                "当前评论抓取使用纯直链方案: "
                f"aweme_id={aweme_id}, search_keyword={session['search_keyword']}, video_url={video_url}"
            )

        logger.info(f"视频ID: {aweme_id}")
        self._report_comment_crawl_progress(
            progress_callback,
            session=session,
            aweme_id=aweme_id,
            extra_detail="已进入视频详情页，准备初始化评论抓取",
        )
        session["expected_comment_count"] = self._get_expected_comment_count(aweme_id)
        if skip_crawled:
            session["history_cutoff_ts"] = self.db.get_latest_crawled_comment_timestamp(platform, aweme_id)
            if session["history_cutoff_ts"]:
                logger.info(
                    f"视频 {aweme_id} 启用基于 comment_id 的增量去重，历史时间参考点为 "
                    f"{self._format_filter_datetime(session['history_cutoff_ts'])}"
                )
        self._comment_api_request_count = 0
        self._comment_reply_api_request_count = 0
        self._comment_total_count = 0
        self._comment_reply_total_count = 0
        self._comment_aggressive_mode = bool(aggressive_mode)
        self._reply_collection_enabled = False
        session["aggressive_mode"] = bool(self._comment_aggressive_mode)
        session["reply_collection_enabled"] = False
        incremental_scan = self._is_incremental_comment_scan(session)
        session["crawl_mode"] = "incremental" if incremental_scan else "full"
        session["session_id"] = f"comment_session_{aweme_id}_{int(time.time() * 1000)}"
        self._append_crawl_trace(
            "video_crawl_started",
            {
                "session_id": session["session_id"],
                "search_task_id": str(session.get("search_task_id", "") or "").strip(),
                "aweme_id": str(aweme_id or "").strip(),
                "video_url": str(video_url or "").strip(),
                "platform": str(platform or "").strip(),
                "skip_crawled": bool(skip_crawled),
                "aggressive_mode": bool(aggressive_mode),
            },
        )
        logger.info(
            "开始抓取视频评论: "
            f"session_id={session['session_id']}, "
            f"aweme_id={aweme_id or '-'}, "
            f"video_url={video_url or '-'}"
        )
        self._upsert_comment_crawl_session_snapshot(session, aweme_id=aweme_id, video_url=video_url, finished=False)
        max_requests, max_scrolls = self._resolve_comment_crawl_limits(
            session["expected_comment_count"],
            incremental_scan,
        )
        
        try:
            self.comment_interceptor.clear()
            self.comment_interceptor.enable()

            # 访问详情页面（普通视频 / 图文）
            logger.info("正在访问详情页面...")
            self.page.goto(video_url, wait_until='domcontentloaded', timeout=30000)
            if self.is_stopped():
                session["termination_reason"] = "stop_requested"
                return self._build_comment_crawl_result(session, video_url=video_url, aweme_id=aweme_id)
            
            # 等待一小段时间让重定向或错误提示能够加载出来
            if not self._interruptible_sleep(random.uniform(1.0, 2.0), chunk_seconds=0.15):
                session["termination_reason"] = "stop_requested"
                return self._build_comment_crawl_result(session, video_url=video_url, aweme_id=aweme_id)
            page_popup_closed = False
            with contextlib.suppress(Exception):
                page_popup_closed = bool(self._dismiss_login_popup_if_present())
            if page_popup_closed:
                session["termination_reason"] = "page_popup_interrupted"
                self._cleanup_current_video_page_before_skip(
                    "page_popup_interrupted",
                    login_popup_closed=True,
                )
                raise RuntimeError("详情页出现页面弹窗，已关闭后跳过当前视频")
            page_context = self._validate_target_video_aweme_context(
                aweme_id,
                target_url=video_url,
                stage="after_goto",
            )

            # 检测是否被重定向到了其他视频（例如原视频被删除或不可见，跳转到了推荐Feed流）
            if aweme_id and not page_context.get("matched"):
                logger.warning(
                    "页面跳转后 aweme_id 与目标不一致，视频可能已失效或被重定向。"
                    f" expected={aweme_id}, current={page_context.get('current_aweme_id', '') or '-'},"
                    f" current_url={page_context.get('current_url', '') or '-'}"
                )
                session["termination_reason"] = "redirected_to_other_video"
                self._cleanup_current_video_page_before_skip("redirected_to_other_video")
                raise RuntimeError(
                    f"目标视频无法访问，已被重定向到: {page_context.get('current_url', '') or self.page.url}"
                )

            # 检查是否有视频失效/删除/隐藏提示 (处理未重定向但显示错误状态的页面)
            try:
                error_text = self.page.evaluate("""() => {
                    const errorNodes = Array.from(document.querySelectorAll('div, span, p, h1, h2')).filter(n => {
                        const text = (n.innerText || '').replace(/\\s+/g, '').trim();
                        return /视频不存在|你要观看的视频不存在|该视频已被删除|视频已失效|该视频不可见|视频已隐藏|暂时无法查看|作者已设置仅自己可见|作品状态异常|该视频无法查看/.test(text);
                    });
                    if (errorNodes.length > 0) {
                        return errorNodes[0].innerText.trim();
                    }
                    return '';
                }""")
                if not isinstance(error_text, str):
                    error_text = ""
                error_text = self._match_unavailable_video_text(error_text)
                if error_text:
                    logger.warning(f"检测到视频不可用提示: {error_text}。放弃抓取，跳至下一个视频。")
                    session["termination_reason"] = "video_unavailable"
                    self._cleanup_current_video_page_before_skip("video_unavailable")
                    raise RuntimeError(f"目标视频不可用: {error_text}")
            except Exception as e:
                if isinstance(e, RuntimeError):
                    raise

            # 阻止视频自动连播/滚动到下一个推荐视频，防止在爬取评论时页面自动跳转
            try:
                self.page.evaluate("""() => {
                    // 持续检测并暂停视频、开启循环播放
                    setInterval(() => {
                        const videos = document.querySelectorAll('video');
                        videos.forEach(v => {
                            if (!v.loop) v.loop = true;
                            if (!v.paused) v.pause();
                        });
                    }, 800);
                }""")
            except Exception:
                pass

            detail_structure = self._inspect_detail_page_structure()
            logger.info(
                "详情页结构识别: "
                f"kind={detail_structure.get('detail_kind', 'unknown')} "
                f"url={detail_structure.get('url', self.page.url)} "
                f"videos={detail_structure.get('visible_video_count', 0)} "
                f"images={detail_structure.get('visible_image_count', 0)} "
                f"note_markers={detail_structure.get('note_marker_count', 0)} "
                f"comment_candidates={detail_structure.get('comment_candidate_count', 0)}"
            )

            bootstrap_timeout = 1.0 if incremental_scan else 1.4
            bootstrap_info = self._wait_for_comment_bootstrap(
                timeout_seconds=bootstrap_timeout,
                progress_callback=progress_callback,
                session=session,
                aweme_id=aweme_id,
                extra_detail="等待评论面板首批数据预热",
            )
            if bootstrap_info.get("has_comments") or self._comment_api_request_count > 0:
                logger.info("详情页已提前出现评论面板或评论接口，直接进入评论采集")
            page_context = self._validate_target_video_aweme_context(
                aweme_id,
                target_url=video_url,
                stage="after_bootstrap",
            )
            if aweme_id and not page_context.get("matched"):
                logger.warning(
                    "评论预热后 aweme_id 与目标不一致，疑似自动串流到其他视频。"
                    f" expected={aweme_id}, current={page_context.get('current_aweme_id', '') or '-'},"
                    f" current_url={page_context.get('current_url', '') or '-'}"
                )
                session["termination_reason"] = "redirected_to_other_video"
                raise RuntimeError(
                    f"评论预热后页面已不在目标视频上下文: {page_context.get('current_url', '') or self.page.url}"
                )

            self._consume_pending_comment_batches_if_any(
                session,
                aweme_id,
                video_url,
                target_keywords,
                skip_crawled,
                platform,
                video_title,
                author_name,
                reason="before_initial_risk_check",
            )
            if session.get("wrong_video_detected"):
                logger.warning("预热阶段检测到串流(评论接口aweme_id不符)，强制中断当前视频抓取。")
                session["termination_reason"] = "redirected_to_other_video"
                raise RuntimeError("页面发生了串流或自动连播，已跳过推荐视频。")

            risk_marker = self._detect_risk_control_page()
            if risk_marker:
                session["risk_control_detected"] = True
                session["risk_control_reason"] = risk_marker
                session["termination_reason"] = "risk_control_page"
                raise RuntimeError(f"页面触发风控提示: {risk_marker}")
            
            resolved_video_title = str(video_title or "").strip()
            resolved_author_name = str(author_name or "").strip()
            if not resolved_video_title or not resolved_author_name:
                page_title = str(self.page.title() or "").strip()
                if page_title:
                    logger.info(f"页面标题: {page_title}")
                if not resolved_video_title:
                    resolved_video_title = page_title
                if not resolved_video_title or not resolved_author_name:
                    with contextlib.suppress(Exception):
                        video_metadata = self.page.evaluate(
                            """() => {
                                const title = document.title || "";
                                let video_name = "";
                                let author_name = "";
                                const parts = title.split(' - ');
                                if (parts.length >= 2) {
                                    video_name = parts[0].trim();
                                    author_name = parts[1].replace(/的视频$/, '').trim();
                                }
                                if (!video_name) {
                                    const h1 = document.querySelector('h1');
                                    if (h1) video_name = (h1.innerText || '').trim();
                                }
                                return { video_name, author_name };
                            }"""
                        ) or {}
                        if not resolved_video_title:
                            resolved_video_title = str(video_metadata.get("video_name", "") or "").strip()
                        if not resolved_author_name:
                            resolved_author_name = str(video_metadata.get("author_name", "") or "").strip()
            session["video_name"] = resolved_video_title or str(self.page.title() or "").strip() or aweme_id
            session["author_name"] = resolved_author_name or "未知作者"

            surface_info = bootstrap_info if bootstrap_info.get("has_comments") else self._ensure_comment_surface_ready(
                detail_structure=detail_structure
            )
            if not surface_info.get("has_comments"):
                settled_surface_after_click = self._wait_for_comment_bootstrap(
                    timeout_seconds=0.8,
                    progress_callback=progress_callback,
                    session=session,
                    aweme_id=aweme_id,
                    extra_detail="评论面板已打开，等待首批评论返回",
                )
                if settled_surface_after_click.get("has_comments"):
                    surface_info = settled_surface_after_click
            self._update_reply_surface_stats(session, surface_info)
            self._consume_comment_api_batches(
                session,
                aweme_id,
                video_url,
                target_keywords,
                skip_crawled,
                platform,
                resolved_video_title,
                resolved_author_name,
            )
            if session.get("wrong_video_detected"):
                logger.warning("在面板预热阶段检测到串流(评论接口aweme_id不符)，强制中断当前视频抓取。")
                session["termination_reason"] = "redirected_to_other_video"
                raise RuntimeError("页面发生了串流或自动连播，已跳过推荐视频。")
            
            self._report_comment_crawl_progress(
                progress_callback,
                session=session,
                aweme_id=aweme_id,
                extra_detail="评论面板预热完成，开始采集评论",
            )
            if session.get("quota_reached"):
                logger.info("评论面板预热阶段已达到剩余用户配额，直接结束当前视频采集")
            elif surface_info.get("has_comments"):
                logger.info("评论面板已就绪，开始采集评论")
            else:
                logger.warning("未检测到稳定评论面板，将继续尝试通过页面动作触发评论接口")
            
            scroll_count = 0
            idle_rounds = 0
            same_cursor_streak = 0
            surface_recovery_attempts = 0
            bottom_retry_rounds = 0
            tail_completion_probe_attempts = 0
            forced_tail_probe_rounds = 0
            session["last_seen_cursor"] = session["cursor"]
            
            while True:
                self._report_comment_crawl_progress(
                    progress_callback,
                    session=session,
                    aweme_id=aweme_id,
                    extra_detail="单视频评论抓取进行中",
                )
                if self._stop_event.is_set():
                    logger.info("收到停止信号，终止评论爬取")
                    session["termination_reason"] = "stop_requested"
                    break
                if session.get("quota_reached"):
                    break

                self._consume_pending_comment_batches_if_any(
                    session,
                    aweme_id,
                    video_url,
                    target_keywords,
                    skip_crawled,
                    platform,
                    video_title,
                    author_name,
                    reason="before_loop_risk_check",
                )
                if session.get("wrong_video_detected"):
                    logger.warning("在抓取循环前检测到串流(评论接口aweme_id不符)，强制中断当前视频抓取。")
                    session["termination_reason"] = "redirected_to_other_video"
                    raise RuntimeError("页面发生了串流或自动连播，已跳过推荐视频。")
                if session.get("quota_reached"):
                    break
                risk_marker = self._detect_risk_control_page()
                if risk_marker:
                    session["risk_control_detected"] = True
                    session["risk_control_reason"] = risk_marker
                    session["termination_reason"] = "risk_control_page"
                    logger.warning(f"检测到页面风控提示: {risk_marker}")
                    break

                self._consume_comment_api_batches(session, aweme_id, video_url, target_keywords, skip_crawled, platform, video_title, author_name)
                if session.get("wrong_video_detected"):
                    logger.warning("在抓取循环中检测到串流(评论接口aweme_id不符)，强制中断当前视频抓取。")
                    session["termination_reason"] = "redirected_to_other_video"
                    raise RuntimeError("页面发生了串流或自动连播，已跳过推荐视频。")
                if session.get("quota_reached"):
                    break
                before_api_request_count = session["api_request_count"]
                before_total_comments = session["total_comment_count"]
                before_cursor = session["cursor"]
                queue_stats_before_actions = (
                    self.comment_interceptor.get_queue_stats() if self.comment_interceptor else {}
                )

                performed_action = False
                scroll_result = {"found": False, "scrolled": False}
                should_try_comment_pagination = bool(
                    session["has_more"]
                    or forced_tail_probe_rounds > 0
                    or self._has_pending_page_tail_scroll_risk(session)
                )
                if should_try_comment_pagination:
                    scroll_result = self._scroll_comment_list(surface_info.get("surface_selector", ""))
                    self._update_comment_scroll_state(session, scroll_result)
                    performed_action = True
                    if forced_tail_probe_rounds > 0:
                        forced_tail_probe_rounds = max(forced_tail_probe_rounds - 1, 0)
                    scroll_pause = self._resolve_comment_pacing_delay(
                        phase="post_scroll",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    if scroll_pause > 0:
                        self._sleep_with_comment_crawl_progress(
                            scroll_pause,
                            progress_callback,
                            session=session,
                            aweme_id=aweme_id,
                            extra_detail="滚动评论列表后等待接口返回",
                        )

                before_reply_api_request_count = session["reply_api_request_count"]
                before_reply_total_count = session["reply_comment_count"]
                reply_plan = self._resolve_reply_expand_plan(
                    session,
                    scroll_count=scroll_count,
                    idle_rounds=idle_rounds,
                )
                should_expand_replies = bool(reply_plan.get("should_expand"))
                new_reply_clicks = 0
                if should_expand_replies:
                    reply_max_clicks = int(reply_plan.get("max_clicks", 1) or 1)
                    new_reply_clicks = self._expand_reply_threads(session=session, max_clicks=reply_max_clicks)
                session["reply_expand_clicks"] += new_reply_clicks
                reply_progressed = False
                if new_reply_clicks:
                    performed_action = True
                    reply_feedback = self._wait_for_reply_feedback(
                        before_reply_api_request_count,
                        before_reply_total_count,
                        getattr(self, "_last_reply_click_signature", ""),
                        int(queue_stats_before_actions.get("pending_reply", 0) or 0),
                        float(queue_stats_before_actions.get("latest_reply_timestamp", 0.0) or 0.0),
                        timeout_seconds=float(reply_plan.get("feedback_timeout", 2.0) or 2.0),
                        progress_callback=progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="等待回复展开后的评论树反馈",
                    )
                    reply_progressed = bool(reply_feedback.get("progressed"))
                    self._mark_reply_expand_result(
                        session,
                        had_progress=reply_progressed,
                        clicks=new_reply_clicks,
                        tail_phase=bool(reply_plan.get("tail_phase")),
                        signal=str(reply_feedback.get("signal", "") or ""),
                        button_signature=getattr(self, "_last_reply_click_signature", ""),
                    )
                    if not reply_progressed:
                        logger.info(
                            "回复展开未确认到真实进展: "
                            f"signal={reply_feedback.get('signal', '')}, "
                            f"clicks={new_reply_clicks}, "
                            f"signature={getattr(self, '_last_reply_click_signature', '')[:80]}"
                        )
                    if reply_progressed:
                        idle_rounds = 0

                if self._collect_reply_comments_enabled():
                    comment_progress_timeout = 2.8 if session["has_more"] else 2.0
                else:
                    comment_progress_timeout = 0.9 if session["has_more"] else 0.6
                if new_reply_clicks and not session["has_more"]:
                    comment_progress_timeout = 2.4
                progressed = self._wait_for_comment_progress(
                    before_api_request_count,
                    before_total_comments,
                    int(queue_stats_before_actions.get("pending_total", 0) or 0),
                    float(queue_stats_before_actions.get("latest_timestamp", 0.0) or 0.0),
                    timeout_seconds=comment_progress_timeout if performed_action else (1.0 if self._collect_reply_comments_enabled() else 0.5),
                    progress_callback=progress_callback,
                    session=session,
                    aweme_id=aweme_id,
                    extra_detail="等待评论分页接口返回",
                )
                self._consume_comment_api_batches(session, aweme_id, video_url, target_keywords, skip_crawled, platform, video_title, author_name)
                if session.get("quota_reached"):
                    break
                surface_snapshot = self._inspect_comment_surface()
                if isinstance(surface_snapshot, dict) and surface_snapshot.get("has_surface"):
                    self._update_reply_surface_stats(session, surface_snapshot)
                reply_progress_observed = bool(
                    session["reply_api_request_count"] > before_reply_api_request_count
                    or session["reply_comment_count"] > before_reply_total_count
                )
                observed_progress = bool(
                    session["api_request_count"] > before_api_request_count
                    or session["total_comment_count"] > before_total_comments
                    or reply_progress_observed
                    or (
                        bool(session["cursor"])
                        and bool(before_cursor)
                        and session["cursor"] != before_cursor
                    )
                )
                progressed = bool(progressed or observed_progress)
                if progressed or session["has_more"]:
                    session["deferred_initial_end_reason"] = ""

                scroll_ratio = float(scroll_result.get("scroll_ratio", 0.0) or 0.0)
                near_bottom = bool(scroll_result.get("near_bottom"))
                bottom_retry_limit = self._resolve_comment_bottom_retry_limit(
                    incremental_scan=incremental_scan,
                    has_more=bool(session["has_more"]),
                    expected_comment_count=session["expected_comment_count"],
                    collected_comment_count=self._resolve_completion_collected_comment_count(session),
                )
                if self._should_attempt_comment_bottom_retry(
                    progressed=progressed,
                    has_more=bool(session["has_more"]),
                    near_bottom=near_bottom,
                    scroll_found=bool(scroll_result.get("found")),
                    retry_rounds=bottom_retry_rounds,
                    retry_limit=bottom_retry_limit,
                ):
                    bottom_retry_rounds += 1
                    retry_wait = self._resolve_comment_pacing_delay(
                        phase="bottom_retry",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    logger.info(
                        "评论分页已接近底部且接口仍声明 has_more=True，"
                        f"执行第 {bottom_retry_rounds} 次底部补触发并额外等待 {retry_wait:.1f} 秒"
                    )
                    retry_scroll_result = self._scroll_comment_list(
                        surface_info.get("surface_selector", "") or scroll_result.get("selector", "")
                    )
                    self._sleep_with_comment_crawl_progress(
                        retry_wait,
                        progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="评论分页底部补触发后等待接口返回",
                    )
                    self._consume_comment_api_batches(
                        session,
                        aweme_id,
                        video_url,
                        target_keywords,
                        skip_crawled,
                        platform,
                        video_title,
                        author_name,
                    )
                    if session.get("quota_reached"):
                        break
                    surface_snapshot = self._inspect_comment_surface()
                    if isinstance(surface_snapshot, dict) and surface_snapshot.get("has_surface"):
                        self._update_reply_surface_stats(session, surface_snapshot)
                    retry_progressed = bool(
                        session["api_request_count"] > before_api_request_count
                        or session["total_comment_count"] > before_total_comments
                        or session["reply_api_request_count"] > before_reply_api_request_count
                        or session["reply_comment_count"] > before_reply_total_count
                        or (
                            bool(session["cursor"])
                            and bool(before_cursor)
                            and session["cursor"] != before_cursor
                        )
                    )
                    if retry_progressed:
                        progressed = True
                        scroll_result = (
                            retry_scroll_result
                            if isinstance(retry_scroll_result, dict) and retry_scroll_result.get("scrolled")
                            else scroll_result
                        )
                        self._update_comment_scroll_state(session, scroll_result)
                    else:
                        logger.info(
                            "底部补触发后暂未观察到新增评论分页: "
                            f"cursor={session['cursor'] or ''}, "
                            f"retry_rounds={bottom_retry_rounds}/{bottom_retry_limit}"
                        )
                else:
                    if progressed or not session["has_more"] or not near_bottom:
                        bottom_retry_rounds = 0

                scroll_count += 1
                if session["has_more"]:
                    session["request_count"] += 1

                if progressed:
                    idle_rounds = 0
                else:
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        logger.info(
                            "评论分页空转: "
                            f"idle_rounds={idle_rounds}, has_more={session['has_more']}, "
                            f"cursor={session['cursor'] or ''}, last_seen_cursor={session['last_seen_cursor'] or ''}, "
                            f"api_requests={session['api_request_count']}, total_comments={session['total_comment_count']}, "
                            f"scroll_mode={scroll_result.get('mode', '')}, "
                            f"scroll_selector={scroll_result.get('selector', '')}, "
                            f"scroll_position={scroll_result.get('scroll_position', '')}"
                        )

                if session["cursor"] and session["cursor"] == before_cursor:
                    same_cursor_streak += 1
                else:
                    same_cursor_streak = 0
                session["last_seen_cursor"] = session["cursor"] or session["last_seen_cursor"]

                if session["risk_control_detected"] and session["blocked_response_count"] >= 2:
                    session["termination_reason"] = "risk_control_blocked_response"
                    logger.warning("疑似连续触发抖音评论接口风控，终止本次采集避免进一步封控")
                    break

                if not self._collect_reply_comments_enabled() and not session["has_more"]:
                    if self._should_defer_initial_comment_end(session, performed_action=performed_action):
                        session["initial_end_probe_attempts"] = int(session.get("initial_end_probe_attempts", 0) or 0) + 1
                        session["deferred_initial_end_reason"] = "top_level_comment_end_reached"
                        forced_tail_probe_rounds = max(forced_tail_probe_rounds, 1)
                        logger.info(
                            "首批评论接口已显示末页，先执行 1 次补探测或滚动确认，再决定是否结束"
                        )
                        surface_info = self._recover_comment_surface_for_pagination(
                            detail_structure=detail_structure,
                            session=session,
                            current_surface_selector=surface_info.get("surface_selector", ""),
                            progress_callback=progress_callback,
                            aweme_id=aweme_id,
                        )
                        idle_rounds = max(idle_rounds - 1, 0)
                        same_cursor_streak = 0
                        continue
                    session["reached_comment_end"] = True
                    session["termination_reason"] = "top_level_comment_end_reached"
                    logger.info("一级评论接口已到末页，当前模式不采集回复，结束采集")
                    break

                reply_finish_idle_limit = self._resolve_reply_finish_idle_limit(session)
                if not session["has_more"] and idle_rounds >= reply_finish_idle_limit and not reply_progress_observed:
                    reply_gap_pending = self._has_pending_reply_completion_risk(session)
                    page_tail_pending = self._has_pending_page_tail_scroll_risk(session)
                    max_tail_completion_probes = self._resolve_tail_completion_probe_limit(session)
                    if self._should_probe_tail_comment_completion(session) and tail_completion_probe_attempts < max_tail_completion_probes:
                        tail_completion_probe_attempts += 1
                        forced_tail_probe_rounds = max(forced_tail_probe_rounds, 1)
                        collected = self._resolve_completion_collected_comment_count(session)
                        logger.info(
                            "评论接口暂时显示末页，但尾页仍存在未完成风险；"
                            f"执行第 {tail_completion_probe_attempts} 次尾页补探测: "
                            f"collected={collected}, expected={session['expected_comment_count']}, "
                            f"reply_gap_pending={reply_gap_pending}, "
                            f"page_tail_pending={page_tail_pending}, "
                            f"current_reply_buttons={session['current_reply_button_count']}, "
                            f"false_positive={int(session.get('reply_expand_false_positive_count', 0) or 0)}"
                        )
                        surface_info = self._recover_comment_surface_for_pagination(
                            detail_structure=detail_structure,
                            session=session,
                            current_surface_selector=surface_info.get("surface_selector", ""),
                            progress_callback=progress_callback,
                            aweme_id=aweme_id,
                        )
                        idle_rounds = max(idle_rounds - 1, 0)
                        same_cursor_streak = 0
                        continue
                    page_tail_end_guard = self._consume_page_tail_end_block(session)
                    if page_tail_end_guard.get("block_end"):
                        logger.info(
                            "页面评论面板仍未接近末页，阻断 comment_end_reached 收尾并继续推进: "
                            f"remaining_distance={page_tail_end_guard.get('remaining_distance', 0.0):.0f}, "
                            f"stall_rounds={int(page_tail_end_guard.get('stall_rounds', 0) or 0)}, "
                            f"progressed={bool(page_tail_end_guard.get('progressed'))}, "
                            f"current_reply_buttons={session['current_reply_button_count']}"
                        )
                        forced_tail_probe_rounds = max(forced_tail_probe_rounds, 2)
                        surface_info = self._recover_comment_surface_for_pagination(
                            detail_structure=detail_structure,
                            session=session,
                            current_surface_selector=surface_info.get("surface_selector", ""),
                            progress_callback=progress_callback,
                            aweme_id=aweme_id,
                        )
                        idle_rounds = max(idle_rounds - 2, 0)
                        same_cursor_streak = 0
                        continue

                    pending_reply_retry_wait = self._resolve_pending_reply_retry_wait(session)
                    if pending_reply_retry_wait > 0:
                        logger.info(
                            "回复区仍有未完成风险，且上一轮展开仍处于冷却窗口；"
                            f"额外等待 {pending_reply_retry_wait:.1f} 秒后再重试，"
                            f"current_reply_buttons={session['current_reply_button_count']}, "
                            f"cooldown_wait_rounds={int(session.get('reply_cooldown_wait_rounds', 0) or 0)}"
                        )
                        self._sleep_with_comment_crawl_progress(
                            pending_reply_retry_wait,
                            progress_callback,
                            session=session,
                            aweme_id=aweme_id,
                            extra_detail="回复区补探测冷却中，等待再次确认是否还有新增回复",
                        )
                        idle_rounds = max(idle_rounds - 2, 0)
                        same_cursor_streak = 0
                        continue
                    if page_tail_pending:
                        logger.warning(
                            "页面评论面板仍未接近末页，但剩余距离连续多轮未明显收敛；"
                            f"允许结束当前采集: remaining_distance={page_tail_end_guard.get('remaining_distance', 0.0):.0f}, "
                            f"stall_rounds={int(page_tail_end_guard.get('stall_rounds', 0) or 0)}, "
                            f"current_reply_buttons={session['current_reply_button_count']}"
                        )
                    session["reached_comment_end"] = True
                    session["termination_reason"] = "comment_end_reached"
                    logger.info(
                        "评论接口已到末页，且回复区在多轮尝试后无新增进展，结束采集: "
                        f"idle_rounds={idle_rounds}, reply_finish_idle_limit={reply_finish_idle_limit}, "
                        f"current_reply_buttons={session['current_reply_button_count']}, "
                        f"seen_reply_buttons={session['visible_reply_button_count']}"
                    )
                    break

                idle_limit = self._resolve_effective_comment_idle_limit(
                    session,
                    incremental_scan=incremental_scan,
                    near_bottom=near_bottom,
                    scroll_ratio=scroll_ratio,
                )

                if idle_rounds >= idle_limit:
                    surface_recovery_attempts += 1
                    logger.info(
                        "连续多轮无新增评论响应，但已移除自动结束门禁；"
                        "继续恢复评论面板并重试: "
                        f"recovery_attempts={surface_recovery_attempts}, "
                        f"idle_rounds={idle_rounds}, idle_limit={idle_limit}, "
                        f"has_more={session['has_more']}, scroll_ratio={scroll_ratio:.3f}, "
                        f"near_bottom={near_bottom}"
                    )
                    surface_info = self._recover_comment_surface_for_pagination(
                        detail_structure=detail_structure,
                        session=session,
                        current_surface_selector=surface_info.get("surface_selector", ""),
                        progress_callback=progress_callback,
                        aweme_id=aweme_id,
                    )
                    forced_tail_probe_rounds = max(forced_tail_probe_rounds, 1 if session["has_more"] else 2)
                    cooloff_pause = self._resolve_comment_pacing_delay(
                        phase="slow_path",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    self._sleep_with_comment_crawl_progress(
                        min(max(cooloff_pause, 1.2), 6.0),
                        progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="评论面板恢复后冷却等待，准备继续分页",
                    )
                    idle_rounds = max(idle_rounds - 3, 0)
                    same_cursor_streak = 0
                    continue

                if session["has_more"] and same_cursor_streak >= 4:
                    backoff_pause = self._resolve_comment_pacing_delay(
                        phase="slow_path",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    logger.info(
                        "评论分页游标长时间未推进，执行退避等待以降低风控风险: "
                        f"pause={backoff_pause:.1f}s, volume_tier={self._resolve_comment_volume_tier(session)}"
                    )
                    self._sleep_with_comment_crawl_progress(
                        backoff_pause,
                        progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="评论分页游标长时间未推进，执行退避等待",
                    )
                    same_cursor_streak = 0

                if session["has_more"] and session["request_count"] >= max_requests:
                    session["request_limit_extensions"] += 1
                    max_requests, max_scrolls = self._extend_comment_crawl_limits(max_requests, max_scrolls)
                    logger.info(
                        "已移除最大请求次数自动结束门禁；"
                        f"第 {session['request_limit_extensions']} 次扩容抓取上限到 "
                        f"max_requests={max_requests}, max_scrolls={max_scrolls}"
                    )
                    continue

                if session["has_more"] and (same_cursor_streak >= 2 or idle_rounds >= 1):
                    pause = self._resolve_comment_pacing_delay(
                        phase="slow_path",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    logger.info(
                        f"检测到分页推进变慢，主动降速 {pause:.1f} 秒以降低风控概率"
                        f"，volume_tier={self._resolve_comment_volume_tier(session)}"
                    )
                    self._sleep_with_comment_crawl_progress(
                        pause,
                        progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="检测到分页推进变慢，主动降速等待",
                    )

                if self._should_apply_comment_volume_cooldown(
                    session,
                    incremental_scan=incremental_scan,
                    scroll_count=scroll_count,
                ):
                    cooldown_pause = self._resolve_comment_pacing_delay(
                        phase="volume_cooldown",
                        incremental_scan=incremental_scan,
                        session=session,
                        reply_collection_enabled=self._collect_reply_comments_enabled(),
                        idle_rounds=idle_rounds,
                        same_cursor_streak=same_cursor_streak,
                    )
                    if cooldown_pause > 0:
                        logger.info(
                            "大评论区持续分页中，插入随机冷却等待以降低风控概率: "
                            f"pause={cooldown_pause:.1f}s, total_comments={session['total_comment_count']}, "
                            f"api_requests={session['api_request_count']}, "
                            f"volume_tier={self._resolve_comment_volume_tier(session)}"
                        )
                        self._sleep_with_comment_crawl_progress(
                            cooldown_pause,
                            progress_callback,
                            session=session,
                            aweme_id=aweme_id,
                            extra_detail="大评论区分页冷却等待中，保持心跳续报",
                        )

                if scroll_count % 5 == 0:
                    logger.info(
                        f"爬取进度: 轮次{scroll_count}, API请求{session['api_request_count']}次, "
                        f"一级评论{session['top_level_comment_count']}条, 回复评论{session['reply_comment_count']}条, "
                        f"总计{session['total_comment_count']}条, 匹配{session['matched_comment_count']}条, "
                        f"跳过历史{session['duplicate_comment_count']}条, 时间过滤{session['time_filtered_count']}条, "
                        f"入库{session['saved_customer_count']}条, 当前cursor={session['last_seen_cursor']}"
                    )
                    self._report_comment_crawl_progress(
                        progress_callback,
                        session=session,
                        aweme_id=aweme_id,
                        extra_detail="单视频评论抓取进度已刷新",
                    )

            self._consume_comment_api_batches(session, aweme_id, video_url, target_keywords, skip_crawled, platform, video_title, author_name)

            if not session["termination_reason"] and not session["reached_comment_end"]:
                session["termination_reason"] = "loop_finished_without_end"

            if (
                session["expected_comment_count"]
                and self._resolve_completion_collected_comment_count(session)
                + self._resolve_comment_completion_tolerance(session["expected_comment_count"])
                < session["expected_comment_count"]
            ):
                collected = self._resolve_completion_collected_comment_count(session)
                session["completeness_warning"] = (
                    f"已采评论 {collected} / 预估 {session['expected_comment_count']}，"
                    "页面可能未完全放出全部评论或触发了限制"
                )
            if (
                self._collect_reply_comments_enabled()
                and
                session["visible_reply_button_count"] > 0
                and not session["completeness_warning"]
                and self._has_pending_reply_completion_risk(session)
            ):
                session["completeness_warning"] = (
                    f"页面仍残留 {session['current_reply_button_count']} / {session['visible_reply_button_count']} 个回复入口，"
                    f"当前仅采到 {session['reply_comment_count']} 条回复，"
                    f"且存在 {int(session.get('reply_expand_false_positive_count', 0) or 0)} 次疑似假展开，"
                    "回复评论可能尚未采集完整"
                )
            if session["risk_control_detected"] and not session["completeness_warning"]:
                session["completeness_warning"] = f"疑似触发抖音风控: {session['risk_control_reason'] or '评论接口限制'}"
            if not session["reached_comment_end"] and not session["completeness_warning"]:
                session["completeness_warning"] = (
                    "本轮采集在到达评论末页前结束，"
                    f"termination_reason={session['termination_reason'] or 'unknown'}，"
                    "大评论量视频可能仍有遗漏"
                )
            self._capture_comment_queue_stats(session)
            session["completion_status"] = self._resolve_comment_completion_status(session)
            
            logger.info(
                f"评论爬取完成: 一级评论 {session['top_level_comment_count']} 条, 回复评论 {session['reply_comment_count']} 条, "
                f"总计 {session['total_comment_count']} 条评论, 其中 {session['matched_comment_count']} 条匹配关键词, "
                f"跳过历史 {session['duplicate_comment_count']} 条, 时间过滤 {session['time_filtered_count']} 条, "
                f"实际入库 {session['saved_customer_count']} 条, API请求 {session['api_request_count']} 次, "
                f"reached_end={session['reached_comment_end']}, termination_reason={session['termination_reason']}, "
                f"completion_status={session['completion_status']}, 风控={session['risk_control_detected']}, "
                f"dropped_batches={session['dropped_batches_count']}"
            )
            
        except Exception as e:
            self._consume_pending_comment_batches_if_any(
                session,
                aweme_id,
                video_url,
                target_keywords,
                skip_crawled,
                platform,
                video_title,
                author_name,
                reason="exception_before_return",
            )
            logger.error(f"爬取评论出错: {e}")
        finally:
            self._capture_comment_queue_stats(session)
            if not session.get("completion_status"):
                session["completion_status"] = self._resolve_comment_completion_status(session)
            self._upsert_comment_crawl_session_snapshot(session, aweme_id=aweme_id, video_url=video_url, finished=True)
            self.comment_interceptor.disable()
            self._comment_aggressive_mode = False
            self._reply_collection_enabled = self.COLLECT_REPLY_COMMENTS
            
        return {
            "video_url": video_url,
            "aweme_id": aweme_id,
            "total_comments": session["total_comment_count"],
            "matched_comments": session["matched_comment_count"],
            "saved_customers": session["saved_customer_count"],
            "linked_sec_uids": list(session.get("linked_sec_uids") or []),
            "linked_reply_targets": list(session.get("linked_reply_targets") or []),
            "duplicate_comments": session["duplicate_comment_count"],
            "time_filtered_comments": session["time_filtered_count"],
            "keyword_filtered_comments": session["keyword_filtered_count"],
            "history_recorded_comments": session["history_recorded_count"],
            "top_level_comments": session["top_level_comment_count"],
            "reply_comments": session["reply_comment_count"],
            "expected_comment_count": session["expected_comment_count"],
            "reply_expand_clicks": session["reply_expand_clicks"],
            "api_request_count": session["api_request_count"],
            "termination_reason": session["termination_reason"],
            "completion_status": session["completion_status"],
            "crawl_mode": session["crawl_mode"],
            "crawl_session_id": session["session_id"],
            "reached_comment_end": bool(session["reached_comment_end"] or (not session["has_more"] and not session["risk_control_detected"])),
            "risk_control_detected": session["risk_control_detected"],
            "risk_control_reason": session["risk_control_reason"],
            "completeness_warning": session["completeness_warning"],
            "history_cutoff_timestamp": session["history_cutoff_ts"],
            "history_cutoff_time": self._format_filter_datetime(session["history_cutoff_ts"]),
            "requested_time_start": self._format_filter_datetime(requested_start_ts),
            "requested_time_end": self._format_filter_datetime(requested_end_ts),
            "fact_recorded_comments": session["fact_recorded_count"],
            "customer_linked_comments": session["customer_linked_count"],
            "dropped_batches_count": session["dropped_batches_count"],
            "peak_pending_batches": session["peak_pending_batches"],
            "latest_batch_id": session["latest_batch_id"],
        }


    def _extract_aweme_id(self, video_url: str) -> str:
        """
        从视频URL提取视频ID
        
        Args:
            video_url: 视频URL
            
        Returns:
            str: 视频ID
        """
        return CommentMixin._extract_aweme_id_from_candidate_value(video_url)


    def _open_comment_drawer(self, detail_structure: Optional[dict] = None):
        """
        打开评论区抽屉
        
        尝试多种方式打开评论区，并验证是否成功打开
        """
        try:
            logger.info("尝试打开评论区...")
            self._dismiss_login_popup_if_present()
            
            # 首先检查评论区是否已经打开
            if self._check_comment_drawer_open(detail_structure=detail_structure):
                logger.info("评论区已经打开，无需手动操作")
                self._interruptible_sleep(
                    self._resolve_comment_surface_open_pause(already_loaded=True),
                    chunk_seconds=0.12,
                )
                return
            if self._click_precise_comment_trigger(self.COMMENT_OPEN_TRIGGER_SELECTORS):
                logger.info("评论区已成功打开")
                return
            
            # 最后检查一次
            if self._check_comment_drawer_open(detail_structure=detail_structure):
                logger.info("评论区已打开（可能是自动打开的）")
            else:
                logger.warning("无法打开评论区，可能评论区不存在或页面结构已变化")
            
        except Exception as e:
            logger.error(f"打开评论区失败: {e}")
    

    def _check_comment_drawer_open(self, detail_structure: Optional[dict] = None) -> bool:
        """
        检查评论区是否已打开
        
        Returns:
            bool: 评论区是否已打开
        """
        try:
            surface_info = self._inspect_comment_surface(detail_structure=detail_structure)
            if surface_info.get("has_comments"):
                logger.debug(
                    "通过评论面板识别确认评论区已打开: "
                    f"selector={surface_info.get('surface_selector', '')} "
                    f"items={surface_info.get('item_count', 0)} "
                    f"replies={surface_info.get('reply_button_count', 0)}"
                )
                return True
            if surface_info.get("has_surface") and (
                int(surface_info.get("item_count", 0) or 0) > 0
                or int(surface_info.get("reply_button_count", 0) or 0) > 0
                or int(surface_info.get("text_length", 0) or 0) >= 20
            ):
                logger.debug(
                    "通过评论面板容器确认评论区已打开: "
                    f"selector={surface_info.get('surface_selector', '')} "
                    f"text_length={surface_info.get('text_length', 0)}"
                )
                return True

            # 检查评论区容器的选择器
            comment_drawer_selectors = [
                "[data-e2e='comment-list']",
                "[class*='comment-list']",
                "[class*='CommentList']",
                "[class*='comment-drawer']",
                "[class*='CommentDrawer']",
                "[class*='comment-panel']",
                "[class*='commentContent']",
                ".comment-main",
            ]
            
            for selector in comment_drawer_selectors:
                try:
                    element = self.page.query_selector(selector)
                    if element and element.is_visible():
                        # 额外验证：检查是否有评论内容
                        has_content = self.page.evaluate(f"""
                            () => {{
                                const container = document.querySelector("{selector}");
                                if (!container) return false;
                                // 检查是否有评论项
                                const commentItems = container.querySelectorAll("[class*='comment-item'], [class*='CommentItem'], [data-e2e='comment-item']");
                                return commentItems.length > 0 || container.innerText.length > 20;
                            }}
                        """)
                        if has_content:
                            logger.debug(f"检测到评论区已打开: {selector}")
                            return True
                except Exception:
                    continue
            
            return False
            
        except Exception as e:
            logger.debug(f"检查评论区状态失败: {e}")
            return False


    def _scroll_comment_list(
        self,
        preferred_selector: str = "",
        direction: str = "down",
        target: Optional[dict] = None,
        target_marker_uid: str = "",
        parent_marker_uid: str = "",
    ) -> dict:
        """
        滚动评论列表加载更多评论
        
        Returns:
            dict: 滚动结果 {found: bool, scrolled: bool, comment_count: int}
        """
        try:
            normalized_target = self._normalize_reply_target(target or {}) if isinstance(target, dict) else {}
            target_detail_kind = str((normalized_target or {}).get("detail_kind", "") or "").strip().lower()
            if target_detail_kind not in {"video", "note"}:
                target_detail_kind = self._resolve_comment_detail_kind()
            anchor_scroll_result = self.page.evaluate(
                """(payload) => {
                    const isVisible = (el) => !!(el && el.offsetParent !== null);
                    const normalize = (value) => String(value || '')
                        .replace(/[\\u200b\\ufeff]/g, '')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const textOf = (node) => String(node?.innerText || node?.textContent || '');
                    const isScrollable = (el) => {
                        if (!el || !isVisible(el)) return false;
                        const style = window.getComputedStyle(el);
                        const overflowY = String(style?.overflowY || '');
                        return (
                            (overflowY.includes('auto') || overflowY.includes('scroll') || overflowY.includes('overlay')) &&
                            el.scrollHeight > el.clientHeight + 40
                        );
                    };
                    const findScrollableAncestor = (node) => {
                        let current = node;
                        let depth = 0;
                        while (current && current !== document.body && depth < 8) {
                            if (isScrollable(current)) return current;
                            current = current.parentElement;
                            depth += 1;
                        }
                        return null;
                    };
                    const findTargetNode = () => {
                        const directSelectors = [];
                        if (payload.targetMarkerUid) {
                            directSelectors.push(`[data-trae-reply-item="${payload.targetMarkerUid}"]`);
                        }
                        if (payload.parentMarkerUid) {
                            directSelectors.push(`[data-trae-reply-parent="${payload.parentMarkerUid}"]`);
                        }
                        for (const selector of directSelectors) {
                            try {
                                const node = document.querySelector(selector);
                                if (node) return {
                                    node,
                                    anchorType: selector.includes('reply-item') ? 'target_marker' : 'parent_marker',
                                };
                            } catch (e) {}
                        }
                        const itemSelectors = Array.isArray(payload.itemSelectors) ? payload.itemSelectors : [];
                        const candidates = [];
                        const seen = new Set();
                        const targetCommentId = normalize(payload.commentId);
                        const targetParentCommentId = normalize(payload.parentCommentId);
                        const targetRootCommentId = normalize(payload.rootCommentId);
                        const targetNickname = normalize(payload.nickname);
                        const targetText = normalize(payload.commentText);
                        const textPrefix = normalize(payload.commentTextPrefix || targetText.slice(0, 24));
                        const targetParentText = normalize(payload.parentText);
                        const targetParentNickname = normalize(payload.parentNickname);
                        for (const selector of itemSelectors) {
                            for (const node of Array.from(document.querySelectorAll(selector))) {
                                if (!node || seen.has(node)) continue;
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
                                let anchorType = 'target_guess';
                                if (targetCommentId && attrBag.includes(targetCommentId)) score += 20;
                                if (targetParentCommentId && attrBag.includes(targetParentCommentId)) {
                                    score += 16;
                                    anchorType = 'parent_guess';
                                }
                                if (targetRootCommentId && attrBag.includes(targetRootCommentId)) {
                                    score += 12;
                                    anchorType = 'parent_guess';
                                }
                                if (targetNickname && text.includes(targetNickname)) score += 5;
                                if (targetText && text.includes(targetText)) score += 8;
                                else if (textPrefix && textPrefix.length >= 8 && text.includes(textPrefix)) score += 5;
                                if (targetParentText && text.includes(targetParentText)) {
                                    score += 6;
                                    anchorType = 'parent_guess';
                                }
                                if (targetParentNickname && text.includes(targetParentNickname)) {
                                    score += 4;
                                    anchorType = 'parent_guess';
                                }
                                if (score > 0) {
                                    candidates.push({ node, score, anchorType });
                                }
                            }
                        }
                        candidates.sort((a, b) => b.score - a.score);
                        return candidates[0] || null;
                    };

                    const targetMatch = findTargetNode();
                    if (!targetMatch || !targetMatch.node) {
                        return { matched: false };
                    }
                    const container = findScrollableAncestor(targetMatch.node);
                    if (!container) {
                        try {
                            targetMatch.node.scrollIntoView({ block: 'center', inline: 'nearest' });
                        } catch (e) {}
                        return {
                            matched: true,
                            found: false,
                            scrolled: true,
                            mode: 'target-scroll-into-view',
                            anchor_type: targetMatch.anchorType,
                            comment_count: document.querySelectorAll("[class*='comment-item'], [class*='CommentItem'], [data-e2e='comment-item']").length,
                        };
                    }

                    const currentScrollTop = container.scrollTop || 0;
                    const maxScrollTop = Math.max(container.scrollHeight - container.clientHeight, 0);
                    const containerRect = container.getBoundingClientRect();
                    const targetRect = targetMatch.node.getBoundingClientRect();
                    const scrollingUp = String(payload.direction || 'down') === 'up';
                    const baseOffset = scrollingUp
                        ? container.clientHeight * 0.35
                        : container.clientHeight * (targetMatch.anchorType === 'parent_marker' || targetMatch.anchorType === 'parent_guess' ? 0.2 : 0.32);
                    let desiredScrollTop = currentScrollTop + (targetRect.top - containerRect.top) - baseOffset;
                    if (!scrollingUp && (targetMatch.anchorType === 'parent_marker' || targetMatch.anchorType === 'parent_guess')) {
                        desiredScrollTop += Math.min(container.clientHeight * 0.35, 420);
                    }
                    desiredScrollTop = Math.max(0, Math.min(maxScrollTop, desiredScrollTop));
                    container.scrollTop = desiredScrollTop;
                    const delta = desiredScrollTop - currentScrollTop;
                    if (Math.abs(delta) > 4) {
                        const wheelEvent = new WheelEvent('wheel', {
                            deltaY: delta,
                            bubbles: true,
                        });
                        container.dispatchEvent(wheelEvent);
                    }
                    return {
                        matched: true,
                        found: true,
                        scrolled: Math.abs(delta) > 1,
                        mode: 'target-anchored',
                        selector: payload.preferredSelector || '',
                        direction: scrollingUp ? 'up' : 'down',
                        anchor_type: targetMatch.anchorType,
                        comment_count: document.querySelectorAll("[class*='comment-item'], [class*='CommentItem'], [data-e2e='comment-item']").length,
                        scroll_position: `${currentScrollTop} -> ${desiredScrollTop}/${maxScrollTop}`,
                    };
                }""",
                {
                    "preferredSelector": str(preferred_selector or ""),
                    "direction": str(direction or "down"),
                    "targetMarkerUid": str(target_marker_uid or ""),
                    "parentMarkerUid": str(parent_marker_uid or ""),
                    "commentId": str((normalized_target or {}).get("comment_id", "") or "").strip(),
                    "parentCommentId": str((normalized_target or {}).get("parent_comment_id", "") or "").strip(),
                    "rootCommentId": str((normalized_target or {}).get("root_comment_id", "") or "").strip(),
                    "nickname": str((normalized_target or {}).get("nickname", "") or "").strip(),
                    "commentText": str((normalized_target or {}).get("comment_text", "") or "").strip(),
                    "commentTextPrefix": str((normalized_target or {}).get("comment_text_prefix", "") or "").strip(),
                    "parentNickname": str((((normalized_target or {}).get("parent_anchor") or {}).get("nickname", "") or "")).strip(),
                    "parentText": str((((normalized_target or {}).get("parent_anchor") or {}).get("comment_text", "") or "")).strip(),
                    "itemSelectors": self._get_comment_item_selectors_for_layout(target_detail_kind),
                },
            )
            if isinstance(anchor_scroll_result, dict) and anchor_scroll_result.get("matched"):
                logger.debug(
                    "按目标锚点滚动评论区: "
                    f"anchor={anchor_scroll_result.get('anchor_type', '')} "
                    f"mode={anchor_scroll_result.get('mode', '')} "
                    f"scroll={anchor_scroll_result.get('scroll_position', '')}"
                )
                return anchor_scroll_result

            scroll_js = """
            ([preferredSelector, stepSegments, direction]) => {
                const isVisible = (el) => !!(el && el.offsetParent !== null);
                const isScrollable = (el) => {
                    if (!el || !isVisible(el)) return false;
                    const style = window.getComputedStyle(el);
                    const overflowY = String(style?.overflowY || '');
                    return (
                        (overflowY.includes('auto') || overflowY.includes('scroll') || overflowY.includes('overlay')) &&
                        el.scrollHeight > el.clientHeight + 40
                    );
                };
                const closestCommentAncestorSelector =
                    '[data-e2e="comment-panel"], [data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                    '[class*="comment-list"], [class*="CommentList"], [class*="comment-panel"], [class*="commentContent"], ' +
                    '[class*="comment-drawer"], [class*="CommentDrawer"], [class*="commentContainer"], [class*="CommentContainer"], ' +
                    '[class*="commentWrap"], [class*="CommentWrap"], [class*="commentScroll"], .comment-main';
                const findBestScrollableContainer = (root, selectorHint = '') => {
                    if (!root || !isVisible(root)) return null;
                    if (isScrollable(root)) {
                        return { node: root, selector: selectorHint, mode: 'root-scrollable' };
                    }

                    const scrollableDescendants = Array.from(root.querySelectorAll('*')).filter((el) => isScrollable(el));
                    if (scrollableDescendants.length) {
                        scrollableDescendants.sort((a, b) => {
                            const aScore = Math.abs((a.clientHeight || 0) - (root.clientHeight || 0));
                            const bScore = Math.abs((b.clientHeight || 0) - (root.clientHeight || 0));
                            return aScore - bScore;
                        });
                        return { node: scrollableDescendants[0], selector: selectorHint, mode: 'descendant-scrollable' };
                    }

                    let current = root.parentElement;
                    let depth = 0;
                    while (current && current !== document.body && depth < 8) {
                        if (isScrollable(current)) {
                            return { node: current, selector: selectorHint || '[ancestor]', mode: 'ancestor-scrollable' };
                        }
                        current = current.parentElement;
                        depth += 1;
                    }
                    return null;
                };
                const resolvePreferredRoot = (selector) => {
                    if (!selector) return null;
                    if (selector.includes('::closest')) {
                        const baseSelector = selector.split('::closest')[0];
                        if (!baseSelector) return null;
                        try {
                            const firstItem = Array.from(document.querySelectorAll(baseSelector)).find((el) => isVisible(el));
                            if (!firstItem) return null;
                            const ancestor = firstItem.closest(closestCommentAncestorSelector);
                            return ancestor && isVisible(ancestor) ? ancestor : null;
                        } catch (e) {
                            return null;
                        }
                    }
                    try {
                        return document.querySelector(selector);
                    } catch (e) {
                        return null;
                    }
                };

                // 查找评论列表容器
                const containerSelectors = [];
                if (preferredSelector) {
                    containerSelectors.push(preferredSelector);
                }
                containerSelectors.push(
                    "div[data-e2e='comment-list']",
                    "[data-e2e='comment-list-container']",
                    "[data-e2e='comment-panel']",
                    "[data-e2e='note-comment-list']",
                    ".comment-main",
                    "[class*='comment-list-container']",
                    "[class*='CommentList']",
                    "[class*='commentContent']",
                    "[class*='comment-drawer']",
                    "[class*='CommentDrawer']",
                    "[class*='commentContainer']",
                    "[class*='CommentContainer']",
                    "[class*='commentWrap']",
                    "[class*='CommentWrap']",
                    "[class*='commentScroll']",
                    "[class*='CommentScroll']",
                    "[class*='scroll-container']"
                );
                
                let container = null;
                let usedSelector = "";
                let usedMode = "direct";
                
                for (const selector of containerSelectors) {
                    const root = resolvePreferredRoot(selector);
                    if (!root) {
                        continue;
                    }

                    const resolved = findBestScrollableContainer(root, selector);
                    if (resolved && resolved.node) {
                        container = resolved.node;
                        usedSelector = resolved.selector || selector;
                        usedMode = resolved.mode || "resolved-scrollable";
                        break;
                    }
                }

                if (!container) {
                    const commentItems = Array.from(
                        document.querySelectorAll("[class*='comment-item'], [class*='CommentItem'], [data-e2e='comment-item']")
                    ).filter((el) => isVisible(el));
                    const ancestorCandidates = new Map();
                    for (const item of commentItems.slice(0, 8)) {
                        let current = item;
                        let depth = 0;
                        while (current && current !== document.body && depth < 8) {
                            current = current.parentElement;
                            depth += 1;
                            if (!current || !isVisible(current)) continue;
                            if (!isScrollable(current)) continue;
                            const key = current;
                            ancestorCandidates.set(key, (ancestorCandidates.get(key) || 0) + 1);
                        }
                    }
                    if (ancestorCandidates.size) {
                        const ranked = Array.from(ancestorCandidates.entries()).sort((a, b) => {
                            const voteDiff = (b[1] || 0) - (a[1] || 0);
                            if (voteDiff !== 0) return voteDiff;
                            return Math.abs((b[0].clientHeight || 0) - window.innerHeight * 0.55)
                                - Math.abs((a[0].clientHeight || 0) - window.innerHeight * 0.55);
                        });
                        container = ranked[0][0];
                        usedSelector = "[comment-item]::scrollable-ancestor";
                        usedMode = "comment-item-ancestor";
                    }
                }
                
                // 获取当前评论数量
                const commentItems = document.querySelectorAll("[class*='comment-item'], [class*='CommentItem'], [data-e2e='comment-item']");
                const currentCount = commentItems.length;
                
                if (container) {
                    // 获取当前滚动位置
                    const currentScrollTop = container.scrollTop;
                    const scrollHeight = container.scrollHeight;
                    const maxScrollTop = Math.max(scrollHeight - container.clientHeight, 0);
                    const currentRatio = maxScrollTop > 0 ? (currentScrollTop / maxScrollTop) : 1;
                    const scrollingUp = String(direction || 'down') === 'up';
                    
                    // 大评论量视频在前半段需要更大的推进步长，否则会在 has_more=true 时被空转阈值提前截断。
                    let step = Math.max(container.clientHeight * 1.15, 720);
                    if (currentRatio < 0.15) {
                        step = Math.max(container.clientHeight * 3.0, 2200);
                    } else if (currentRatio < 0.35) {
                        step = Math.max(container.clientHeight * 2.2, 1600);
                    } else if (currentRatio < 0.7) {
                        step = Math.max(container.clientHeight * 1.6, 1100);
                    }
                    if (maxScrollTop > 0) {
                        const boundedStep = Math.max(Math.min(maxScrollTop * 0.55, 1600), Math.min(maxScrollTop, 260));
                        step = Math.min(step, boundedStep);
                    }

                    // 分步滚动到底部，尽量触发懒加载和IntersectionObserver
                    const rawSegments = Array.isArray(stepSegments) && stepSegments.length ? stepSegments : [step];
                    const usableSegments = [];
                    let remaining = scrollingUp
                        ? Math.max(0, currentScrollTop)
                        : Math.max(0, maxScrollTop - currentScrollTop);
                    for (const segment of rawSegments) {
                        if (remaining <= 0) break;
                        const applied = Math.max(1, Math.min(remaining, Number(segment) || 0));
                        usableSegments.push(applied);
                        remaining -= applied;
                    }
                    const totalDelta = usableSegments.reduce((sum, value) => sum + value, 0);
                    const nextScrollTop = scrollingUp
                        ? Math.max(0, currentScrollTop - totalDelta)
                        : Math.min(maxScrollTop, currentScrollTop + totalDelta);
                    let animatedScrollTop = currentScrollTop;
                    for (const segment of usableSegments) {
                        animatedScrollTop = scrollingUp
                            ? Math.max(0, animatedScrollTop - segment)
                            : Math.min(maxScrollTop, animatedScrollTop + segment);
                        container.scrollTop = animatedScrollTop;
                    }
                    
                    // 也尝试滚动内部的滚动容器
                    const innerScrollContainers = container.querySelectorAll('[class*="scroll"], [class*="list"]');
                    innerScrollContainers.forEach(el => {
                        const innerMaxScrollTop = Math.max(el.scrollHeight - el.clientHeight, 0);
                        const innerCurrentRatio = innerMaxScrollTop > 0 ? (el.scrollTop / innerMaxScrollTop) : 1;
                        let innerStep = Math.max(el.clientHeight * 1.15, 720);
                        if (innerCurrentRatio < 0.15) {
                            innerStep = Math.max(el.clientHeight * 3.0, 2200);
                        } else if (innerCurrentRatio < 0.35) {
                            innerStep = Math.max(el.clientHeight * 2.2, 1600);
                        } else if (innerCurrentRatio < 0.7) {
                            innerStep = Math.max(el.clientHeight * 1.6, 1100);
                        }
                        const innerSegments = Array.isArray(stepSegments) && stepSegments.length
                            ? stepSegments
                            : [innerStep];
                        for (const segment of innerSegments) {
                            const applied = Math.max(1, Number(segment) || 0);
                            el.scrollTop = scrollingUp
                                ? Math.max(0, el.scrollTop - applied)
                                : Math.min(innerMaxScrollTop, el.scrollTop + applied);
                        }
                    });

                    // 触发滚轮事件，兼容只响应wheel的实现
                    for (const segment of usableSegments) {
                        const wheelEvent = new WheelEvent('wheel', {
                            deltaY: (scrollingUp ? -1 : 1) * Math.max(segment, container.clientHeight * 0.2, 120),
                            bubbles: true,
                        });
                        container.dispatchEvent(wheelEvent);
                    }
                    
                    const remainingDistance = Math.max(0, maxScrollTop - nextScrollTop);
                    const remainingToTop = Math.max(0, nextScrollTop);
                    const nearBottomThreshold = maxScrollTop > 0
                        ? Math.max(160, Math.min(Math.max(container.clientHeight * 0.35, 220), maxScrollTop * 0.2))
                        : 0;
                    const nearTopThreshold = Math.max(120, Math.min(Math.max(container.clientHeight * 0.25, 160), Math.max(maxScrollTop * 0.12, 160)));
                    return { 
                        found: true, 
                        scrolled: usableSegments.length > 0 && nextScrollTop !== currentScrollTop, 
                        comment_count: currentCount,
                        selector: usedSelector,
                        mode: usedMode,
                        direction: scrollingUp ? 'up' : 'down',
                        container_tag: container.tagName,
                        container_class: String(container.className || '').slice(0, 160),
                        scroll_position: `${currentScrollTop} -> ${nextScrollTop}/${maxScrollTop}`,
                        scroll_ratio: maxScrollTop > 0 ? (nextScrollTop / maxScrollTop) : 1,
                        near_bottom: remainingDistance <= nearBottomThreshold,
                        near_top: remainingToTop <= nearTopThreshold,
                        remaining_distance: remainingDistance,
                        remaining_to_top: remainingToTop,
                        near_bottom_threshold: nearBottomThreshold,
                        near_top_threshold: nearTopThreshold,
                        scroll_step: step
                    };
                } else {
                    // 如果找不到容器，滚动整个页面
                    window.scrollBy(0, (String(direction || 'down') === 'up' ? -1 : 1) * Math.max(window.innerHeight * 0.8, 720));
                    return { 
                        found: false, 
                        scrolled: true, 
                        comment_count: currentCount,
                        selector: 'window',
                        mode: 'window',
                        direction: String(direction || 'down')
                    };
                }
            }
            """
            
            scroll_probe = self.page.evaluate(
                """(preferredSelector) => {
                    const isVisible = (el) => !!(el && el.offsetParent !== null);
                    const isScrollable = (el) => {
                        if (!el || !isVisible(el)) return false;
                        const style = window.getComputedStyle(el);
                        const overflowY = String(style?.overflowY || '');
                        return (
                            (overflowY.includes('auto') || overflowY.includes('scroll') || overflowY.includes('overlay')) &&
                            el.scrollHeight > el.clientHeight + 40
                        );
                    };
                    const selectors = [];
                    if (preferredSelector) selectors.push(preferredSelector);
                    selectors.push(
                        "div[data-e2e='comment-list']",
                        "[data-e2e='comment-list-container']",
                        "[data-e2e='comment-panel']",
                        "[data-e2e='note-comment-list']",
                        ".comment-main",
                        "[class*='comment-list-container']",
                        "[class*='CommentList']",
                        "[class*='commentContent']",
                        "[class*='comment-drawer']",
                        "[class*='CommentDrawer']",
                        "[class*='commentContainer']",
                        "[class*='CommentContainer']",
                        "[class*='commentWrap']",
                        "[class*='CommentWrap']",
                        "[class*='commentScroll']",
                        "[class*='CommentScroll']",
                        "[class*='scroll-container']"
                    );
                    for (const selector of selectors) {
                        try {
                            const node = document.querySelector(selector);
                            if (!node) continue;
                            if (isScrollable(node)) {
                                const maxScrollTop = Math.max(node.scrollHeight - node.clientHeight, 0);
                                const currentScrollTop = node.scrollTop || 0;
                                const ratio = maxScrollTop > 0 ? (currentScrollTop / maxScrollTop) : 1;
                                let step = Math.max(node.clientHeight * 1.15, 720);
                                if (ratio < 0.15) step = Math.max(node.clientHeight * 3.0, 2200);
                                else if (ratio < 0.35) step = Math.max(node.clientHeight * 2.2, 1600);
                                else if (ratio < 0.7) step = Math.max(node.clientHeight * 1.6, 1100);
                                if (maxScrollTop > 0) {
                                    const bounded = Math.max(Math.min(maxScrollTop * 0.55, 1600), Math.min(maxScrollTop, 260));
                                    step = Math.min(step, bounded);
                                }
                                return step;
                            }
                        } catch (e) {}
                    }
                    return Math.max(window.innerHeight * 0.8, 720);
                }""",
                preferred_selector or "",
            )
            step_segments = self._build_humanized_comment_scroll_segments(float(scroll_probe or 720))
            result = self.page.evaluate(scroll_js, [preferred_selector or "", step_segments, direction or "down"])
            if not isinstance(result, dict):
                return {"found": False, "scrolled": False, "comment_count": 0}
            
            if result.get("found"):
                logger.debug(
                    f"滚动评论列表成功: direction={result.get('direction', direction)}, "
                    f"选择器={result.get('selector')}, 评论数={result.get('comment_count')}"
                )
            else:
                logger.debug(
                    f"未找到评论容器，使用页面滚动: direction={result.get('direction', direction)}, "
                    f"评论数={result.get('comment_count')}"
                )
            
            return result
            
        except Exception as e:
            logger.debug(f"滚动评论列表失败: {e}")
            return {"found": False, "scrolled": False, "comment_count": 0}


    def _parse_comment(
        self,
        comment: dict,
        video_url: str,
        target_keywords: list = None,
        parent_cid: str = "",
        parent_comment: dict = None,
        comment_level: int = 1,
        video_title: str = "",
        author_name: str = "",
    ) -> dict:
        """
        解析评论数据
        
        Args:
            comment: 评论原始数据
            video_url: 视频URL
            target_keywords: 目标关键词列表
            
        Returns:
            dict: 解析后的评论数据
        """
        try:
            # 提取评论基本信息
            text = comment.get("text", "")
            cid = comment.get("cid", "")
            create_time = comment.get("create_time", 0)
            digg_count = comment.get("digg_count", 0)
            ip_location = (
                comment.get("ip_label")
                or comment.get("ip_location")
                or comment.get("region")
                or comment.get("location")
                or ""
            )
            
            # 提取用户信息
            user = comment.get("user", {})
            nickname = user.get("nickname", "")
            sec_uid = user.get("sec_uid", "")
            unique_id = user.get("unique_id", "")
            signature = user.get("signature", "")
            avatar_url = ""
            
            # 提取头像URL
            avatar_thumb = user.get("avatar_thumb", {})
            if avatar_thumb:
                url_list = avatar_thumb.get("url_list", [])
                if url_list:
                    avatar_url = url_list[0]
            
            # 检查是否匹配关键词
            is_target = True
            matched_keyword = ""
            
            if target_keywords:
                is_target = False
                text_lower = text.lower()
                for kw in target_keywords:
                    if kw.lower() in text_lower:
                        is_target = True
                        matched_keyword = kw
                        break
            parent_comment = parent_comment if isinstance(parent_comment, dict) else {}
            parent_anchor = self._build_comment_anchor(parent_comment) if parent_comment else {}
            
            return {
                "cid": cid,
                "text": text,
                "create_time": create_time,
                "digg_count": digg_count,
                "ip_location": ip_location,
                "nickname": nickname,
                "sec_uid": sec_uid,
                "unique_id": unique_id,
                "signature": signature,
                "avatar_url": avatar_url,
                "video_url": video_url,
                "parent_cid": parent_cid,
                "parent_anchor": parent_anchor,
                "comment_level": max(int(comment_level or 1), 1),
                "is_target": is_target,
                "matched_keyword": matched_keyword,
                "video_title": video_title,
                "author_name": author_name
            }
            
        except Exception as e:
            logger.debug(f"解析评论失败: {e}")
            return None


    def _build_customer_data(self, comment_data: dict) -> dict:
        """将评论记录转换为客户存储结构。"""
        return {
            "sec_uid": comment_data.get("sec_uid", ""),
            "nickname": comment_data.get("nickname", ""),
            "profile_url": f"https://www.douyin.com/user/{comment_data.get('sec_uid', '')}",
            "source_video_url": comment_data.get("video_url", ""),
            "comment_content": comment_data.get("text", ""),
            "comment_time": str(comment_data.get("create_time", "")),
            "unique_id": comment_data.get("unique_id", ""),
            "ip_location": comment_data.get("ip_location", ""),
            "signature": comment_data.get("signature", ""),
            "avatar_url": comment_data.get("avatar_url", ""),
            "video_title": comment_data.get("video_title", ""),
            "author_name": comment_data.get("author_name", "")
        }


    def _save_customer(self, comment_data: dict):
        """
        保存客户信息到数据库
        
        Args:
            comment_data: 评论数据
        """
        try:
            self.db.add_customer(self._build_customer_data(comment_data))
            
        except Exception as e:
            logger.error(f"保存客户信息失败: {e}")


