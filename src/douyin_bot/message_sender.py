import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page
from loguru import logger

from src.common.utils import random_sleep, interruptible_sleep
from src.common.database import DatabaseManager
from src.common.private_message_limit_service import get_private_message_limit_service
from src.config.settings import DEFAULT_TIMEOUT
from src.douyin_bot.human_interaction import HumanInteractionHelper
from src.douyin_bot.input_locator_service import InputLocatorContext, get_input_locator_service
from src.douyin_bot.send_verification_policy import decide_send_confirmation, message_tail_has_advanced
from src.infrastructure.runtime_paths import get_log_dir

class MessageSender:
    PROFILE_PRIVATE_ENTRY_ATTR = "data-huoke-private-entry-candidate"
    PROFILE_MORE_ACTIONS_ATTR = "data-huoke-more-actions-candidate"
    PROFILE_PRIVATE_EXACT_TEXTS = {"私信", "发消息", "发私信"}

    def __init__(self, page: Page, db_manager: DatabaseManager, api_interceptor: Any = None):
        self.page = page
        self.db = db_manager
        self.api_interceptor = api_interceptor
        self._active_target_identity_context: Dict[str, Any] = {}
        self._current_account_scope: Dict[str, Any] = {
            "account_id": "",
            "resolved": False,
            "source": "",
        }
        self._current_account_scope_resolved_at = 0.0

    def _human_helper(self) -> HumanInteractionHelper:
        helper = getattr(self, "_human", None)
        if helper is None:
            helper = HumanInteractionHelper(self.page)
            self._human = helper
        return helper

    @staticmethod
    def _normalize_identity_text(value: Any) -> str:
        return str(value or "").strip().replace(" ", "").replace("\u200b", "").lower()

    @staticmethod
    def _is_safe_identity_match(active_text: str, expected_text: str) -> bool:
        norm_active = str(active_text or "").strip().replace(" ", "").replace("\u200b", "").lower()
        norm_expected = str(expected_text or "").strip().replace(" ", "").replace("\u200b", "").lower()
        if not norm_active or not norm_expected:
            return False
        if norm_active == norm_expected:
            return True

        active_digits = "".join(ch for ch in norm_active if ch.isdigit())
        expected_digits = "".join(ch for ch in norm_expected if ch.isdigit())
        if not active_digits or not expected_digits:
            return False
        if not (
            ("*" in norm_active)
            or ("*" in norm_expected)
            or active_digits == norm_active
            or expected_digits == norm_expected
        ):
            return False
        if active_digits == expected_digits:
            return True
        if len(active_digits) >= 7 and len(expected_digits) >= 7:
            prefix_len = min(4, len(active_digits), len(expected_digits))
            suffix_len = min(4, len(active_digits), len(expected_digits))
            return (
                active_digits[:prefix_len] == expected_digits[:prefix_len]
                and active_digits[-suffix_len:] == expected_digits[-suffix_len:]
            )
        return False

    def _resolve_current_account_id_from_api_interceptor(self) -> Optional[str]:
        interceptor = getattr(self, "api_interceptor", None)
        if not interceptor:
            return None
        try:
            requests = interceptor.get_intercepted_requests()
            for req in requests[-50:]:
                data = req.get("data", {})
                if not isinstance(data, dict):
                    continue
                inner = data.get("data", {})
                if isinstance(inner, dict):
                    uid = inner.get("user_id") or inner.get("uid") or inner.get("sec_uid")
                    if uid and str(uid).strip():
                        return str(uid).strip()
        except Exception:
            pass
        return None

    def _resolve_current_account_id_from_cookies(self) -> str:
        try:
            cookies = self.page.context.cookies()
            for cookie in cookies:
                name = str(cookie.get("name", "") or "").strip()
                value = str(cookie.get("value", "") or "").strip()
                if name in {"uid_tt", "uid_tt_ss"} and len(value) > 5:
                    return value
        except Exception:
            pass
        return ""

    def _resolve_current_account_id_from_dom(self) -> str:
        try:
            result = self.page.evaluate(
                """() => {
                    const avatar = document.querySelector('[data-e2e="navigation-avatar"]')
                        || document.querySelector('[data-e2e="user-avatar"]');
                    if (avatar) {
                        const uid = avatar.getAttribute('data-user-id')
                            || avatar.getAttribute('data-sec-uid')
                            || avatar.dataset?.userId
                            || avatar.dataset?.secUid;
                        if (uid) return String(uid).trim();
                    }
                    const userLink = document.querySelector('a[href*="/user/"]');
                    if (userLink && userLink.href) {
                        const match = userLink.href.match(/\\/user\\/([A-Za-z0-9_-]+)/);
                        if (match && match[1]) return String(match[1]).trim();
                    }
                    return '';
                }"""
            )
            return str(result or "").strip()
        except Exception:
            return ""

    def resolve_current_account_scope(self, force_refresh: bool = False) -> Dict[str, Any]:
        now = time.time()
        if (
            not force_refresh
            and self._current_account_scope.get("resolved")
            and (now - float(self._current_account_scope_resolved_at or 0.0)) < 300
        ):
            return dict(self._current_account_scope)

        account_id = self._resolve_current_account_id_from_api_interceptor() or ""
        source = "api_interceptor" if account_id else ""
        if not account_id:
            account_id = self._resolve_current_account_id_from_cookies()
            source = "cookies" if account_id else ""
        if not account_id:
            account_id = self._resolve_current_account_id_from_dom()
            source = "dom" if account_id else ""

        self._current_account_scope = {
            "account_id": str(account_id or "").strip(),
            "resolved": bool(account_id),
            "source": source,
        }
        self._current_account_scope_resolved_at = now
        return dict(self._current_account_scope)

    def _build_expected_chat_identities(self, user_id: str, user: Dict[str, Any]) -> List[str]:
        candidates = [
            user.get("nickname", ""),
            user.get("unique_id", ""),
            user.get("remark_name", ""),
            user.get("display_name", ""),
            user_id,
        ]
        expected: List[str] = []
        seen = set()
        for candidate in candidates:
            normalized = self._normalize_identity_text(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            expected.append(str(candidate or "").strip())
        return expected

    @staticmethod
    def _resolve_target_conversation_id(user: Dict[str, Any]) -> str:
        for key in ("conversation_id", "conv_id", "conversationId", "chat_id", "dialog_id", "session_id"):
            value = user.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _resolve_target_customer_id(user_id: str, user: Dict[str, Any]) -> str:
        for key in ("customer_id", "sec_uid", "uid", "user_id", "im_user_id", "unique_id"):
            value = user.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return str(user_id or "").strip()

    def _resolve_target_identity_level(
        self,
        *,
        conversation_id: str,
        customer_id: str,
        expected_identities: List[str],
    ) -> str:
        if str(conversation_id or "").strip():
            return "strict_conversation_id"
        normalized_customer_id = str(customer_id or "").strip()
        if normalized_customer_id and not normalized_customer_id.startswith("temp_"):
            return "strict_customer_id"
        if len(expected_identities) >= 2:
            return "exact_name"
        return "name_fuzzy"

    def _collect_api_target_identity_evidence(
        self,
        *,
        expected_identities: List[str],
        conversation_id: str,
        customer_id: str,
    ) -> Dict[str, Any]:
        interceptor = getattr(self, "api_interceptor", None)
        getter = getattr(interceptor, "get_messages_for_conversation", None)
        if not callable(getter):
            return {"matched": False, "identity_names": [], "message_count": 0}
        matched_messages = []
        primary_name = str((expected_identities or [""])[0] or "").strip()
        try:
            matched_messages = getter(
                customer_name=primary_name,
                conversation_id=conversation_id,
                customer_id=customer_id,
            ) or []
        except Exception:
            matched_messages = []
        identity_names: List[str] = []
        seen = set()
        for message in matched_messages:
            for key in ("customer_name", "nickname", "display_name", "remark_name"):
                candidate = str((message or {}).get(key, "") or "").strip()
                normalized = self._normalize_identity_text(candidate)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                identity_names.append(candidate)
        return {
            "matched": bool(matched_messages),
            "identity_names": identity_names,
            "message_count": len(matched_messages),
        }

    def _build_target_identity_context(self, user_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
        expected_identities = self._build_expected_chat_identities(user_id, user)
        conversation_id = self._resolve_target_conversation_id(user)
        customer_id = self._resolve_target_customer_id(user_id, user)
        identity_level = self._resolve_target_identity_level(
            conversation_id=conversation_id,
            customer_id=customer_id,
            expected_identities=expected_identities,
        )
        api_evidence = self._collect_api_target_identity_evidence(
            expected_identities=expected_identities,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
        return {
            "expected_identities": expected_identities,
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "identity_level": identity_level,
            "api_matched": bool(api_evidence.get("matched")),
            "api_identity_names": list(api_evidence.get("identity_names") or []),
            "api_message_count": int(api_evidence.get("message_count", 0) or 0),
        }

    def _allow_profile_dialog_target_fallback(self, snapshot: Dict[str, Any]) -> bool:
        context = dict(getattr(self, "_active_target_identity_context", {}) or {})
        identity_level = str(context.get("identity_level", "") or "").strip()
        if identity_level not in {"strict_conversation_id", "strict_customer_id", "exact_name"}:
            return False
        if not bool(context.get("api_matched")):
            return False
        snapshot = dict(snapshot or {})
        return bool(
            self._snapshot_has_profile_private_dialog(snapshot)
            and not (snapshot.get("activeNames") or [])
            and snapshot.get("hasChatInput")
        )

    @staticmethod
    def _identity_allows_direct_send(context: Dict[str, Any]) -> bool:
        context = dict(context or {})
        identity_level = str(context.get("identity_level", "") or "").strip()
        if identity_level in {"strict_conversation_id", "strict_customer_id"}:
            return True
        if identity_level != "exact_name":
            return False
        return bool(context.get("api_matched")) and int(context.get("api_message_count", 0) or 0) > 0

    def _navigate_to_profile(self, profile_url: str) -> None:
        self.page.goto(profile_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        self.page.wait_for_load_state("domcontentloaded")
        try:
            self.page.wait_for_selector("div[data-e2e='user-info']", timeout=5000)
        except Exception as e:
            logger.debug(f"等待用户信息选择器超时: {e}")
        random_sleep(1.2, 2.2)

    def _collect_chat_target_snapshot(self) -> Dict[str, Any]:
        try:
            snapshot = self.page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const isVisible = (node) => !!(
                        node &&
                        node.getBoundingClientRect &&
                        node.getBoundingClientRect().width > 8 &&
                        node.getBoundingClientRect().height > 8 &&
                        node.getBoundingClientRect().bottom > 0 &&
                        node.getBoundingClientRect().right > 0
                    );
                    const uniquePush = (items, value, maxCount = 12) => {
                        const text = normalize(value);
                        if (!text || items.includes(text) || items.length >= maxCount) return;
                        items.push(text);
                    };
                    const dialogRoots = Array.from(document.querySelectorAll(
                        '[role="dialog"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="drawer"], [class*="Drawer"]'
                    )).filter(isVisible);
                    const activeNames = [];
                    const leftSelectors = [
                        '[data-e2e="conversation-item"][aria-selected="true"]',
                        '[data-e2e="conversation-item"][class*="active"]',
                        '[data-e2e="conversation-item"][class*="selected"]',
                        '[class*="conversationItem"][class*="active"]',
                        '[class*="chat-item"][class*="active"]'
                    ];
                    const titleSelectors = [
                        '.conversationConversationItemtitle',
                        '.nickname',
                        '.name',
                        '[class*="title"]',
                        '[class*="name"]'
                    ];
                    for (const selector of leftSelectors) {
                        document.querySelectorAll(selector).forEach((item) => {
                            for (const titleSelector of titleSelectors) {
                                const node = item.querySelector(titleSelector);
                                if (node) uniquePush(activeNames, node.textContent || '');
                            }
                        });
                    }
                    const headerSelectors = [
                        '[data-e2e="chat-user-name"]',
                        '[class*="chatHeader"] [class*="title"]',
                        '[class*="chat-header"] [class*="title"]',
                        '[class*="messageHeader"] [class*="title"]',
                        '[class*="im-chat-header"] [class*="title"]'
                    ];
                    for (const selector of headerSelectors) {
                        document.querySelectorAll(selector).forEach((node) => {
                            uniquePush(activeNames, node.textContent || '');
                        });
                    }

                    const messageTail = [];
                    const messageSelectors = [
                        '[data-e2e="message-item"]',
                        '[class*="messageItem"]',
                        '[class*="chat-message"]',
                        '[class*="message-content"]'
                    ];
                    for (const selector of messageSelectors) {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        if (!nodes.length) continue;
                        for (const node of nodes.slice(-8)) {
                            uniquePush(messageTail, node.innerText || node.textContent || '', 8);
                        }
                        if (messageTail.length) break;
                    }

                    const scopedHas = (selectors) => {
                        const selectorText = Array.isArray(selectors) ? selectors.join(', ') : String(selectors || '');
                        if (!selectorText) return false;
                        if (document.querySelector(selectorText)) return true;
                        return dialogRoots.some((root) => {
                            try {
                                return !!root.querySelector(selectorText);
                            } catch (e) {
                                return false;
                            }
                        });
                    };
                    const chatInputSelectors = [
                        '[data-e2e="msg-input"] [contenteditable="true"]',
                        '[data-e2e="msg-input"] [role="textbox"]',
                        '[data-e2e="msg-input"] textarea',
                        'textarea[data-e2e="chat-input"]',
                        '[class*="chat-input"] [contenteditable="true"]',
                        '[class*="chat-input"] [role="textbox"]',
                        '[class*="chat-input"] textarea',
                        '[role="dialog"] [contenteditable="true"]',
                        '[role="dialog"] [role="textbox"]',
                        '[role="dialog"] textarea',
                    ];
                    const conversationSelectors = [
                        '[data-e2e="conversation-item"]',
                        '[class*="conversationItem"]',
                        '[role="dialog"] [class*="chat-item"]',
                        '[role="dialog"] [class*="conversation"]',
                    ];
                    const headerSelectorsPresence = [
                        '[data-e2e="chat-user-name"]',
                        '[class*="chatHeader"]',
                        '[class*="chat-header"]',
                        '[class*="messageHeader"]',
                        '[role="dialog"] [class*="header"]',
                    ];
                    const dialogTextHints = [];
                    dialogRoots.forEach((node) => {
                        uniquePush(dialogTextHints, node.innerText || node.textContent || '', 4);
                    });

                    return {
                        url: location.href,
                        title: document.title || '',
                        activeNames,
                        messageTail,
                        hasChatInput: scopedHas(chatInputSelectors),
                        hasConversationList: scopedHas(conversationSelectors),
                        hasChatHeader: scopedHas(headerSelectorsPresence),
                        hasDialogSurface: dialogRoots.length > 0,
                        dialogTextHints,
                        hasSendFailure: !!document.querySelector('.msg-resend-icon, svg[class*="fail"]')
                    };
                }"""
            )
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as e:
            logger.debug(f"采集私信页面快照失败: {e}")
            return {}

    def _snapshot_matches_expected_target(self, snapshot: Dict[str, Any], expected_identities: List[str]) -> bool:
        context = dict(getattr(self, "_active_target_identity_context", {}) or {})
        api_identity_names = [str(item or "").strip() for item in (context.get("api_identity_names") or []) if str(item or "").strip()]
        expected_texts = [
            str(item or "").strip()
            for item in list(expected_identities or []) + api_identity_names
            if str(item or "").strip()
        ]
        if not expected_texts:
            return False

        active_names = [str(item or "").strip() for item in (snapshot or {}).get("activeNames", []) if str(item or "").strip()]
        if not active_names:
            return self._allow_profile_dialog_target_fallback(snapshot)

        for active_name in active_names:
            for expected in expected_texts:
                if self._is_safe_identity_match(active_name, expected):
                    return True
        return False

    def _infer_chat_open_failure_status(self, snapshot: Dict[str, Any], expected_identities: List[str]) -> str:
        if self._snapshot_matches_expected_target(snapshot, expected_identities):
            return "failed_chat_not_ready"
        active_names = (snapshot or {}).get("activeNames") or []
        if not active_names and self._allow_profile_dialog_target_fallback(snapshot):
            return "failed_chat_not_ready"
        if active_names:
            return "failed_target_mismatch"
        return "failed_chat_not_ready"

    def _snapshot_has_profile_private_dialog(self, snapshot: Dict[str, Any]) -> bool:
        snapshot = dict(snapshot or {})
        if snapshot.get("hasSendFailure"):
            return False
        return bool(
            snapshot.get("hasChatInput")
            and (
                snapshot.get("hasDialogSurface")
                or snapshot.get("hasConversationList")
                or snapshot.get("hasChatHeader")
            )
        )

    def _wait_for_private_chat_ready(
        self,
        expected_identities: List[str],
        *,
        timeout_ms: int = 4500,
        step_ms: int = 250,
    ) -> tuple[bool, Dict[str, Any]]:
        elapsed = 0
        last_snapshot: Dict[str, Any] = {}
        while elapsed <= timeout_ms:
            snapshot = self._collect_chat_target_snapshot()
            last_snapshot = snapshot
            url = str(snapshot.get("url") or "")
            has_chat_surface = bool(
                snapshot.get("hasChatInput")
                or snapshot.get("hasChatHeader")
                or snapshot.get("hasConversationList")
                or snapshot.get("hasDialogSurface")
                or "/chat" in url
            )
            if has_chat_surface and self._snapshot_matches_expected_target(snapshot, expected_identities):
                return True, snapshot
            if self._allow_profile_dialog_target_fallback(snapshot):
                return True, snapshot
            if elapsed >= timeout_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        return False, last_snapshot

    def _locator_seems_safe_message_entry(self, locator: Any, selector: str) -> bool:
        selector_lower = str(selector or "").lower()
        if "send-message-btn" in selector_lower:
            return True

        try:
            text = str(locator.inner_text(timeout=800) or "").strip()
        except Exception:
            text = ""

        attr_values: List[str] = []
        for attr_name in ("aria-label", "title", "data-e2e", "class"):
            try:
                attr_values.append(str(locator.get_attribute(attr_name) or ""))
            except Exception:
                continue

        candidate_text = " ".join([text, *attr_values]).lower()
        normalized_text = self._normalize_identity_text(text)
        block_keywords = (
            "关注",
            "已关注",
            "取消关注",
            "举报",
            "分享",
            "更多",
            "收藏",
            "评论",
            "回复",
            "点赞",
            "推荐",
            "群",
        )
        if any(keyword.lower() in candidate_text for keyword in block_keywords):
            return False
        if normalized_text in self.PROFILE_PRIVATE_EXACT_TEXTS:
            return True
        return False

    def _profile_candidate_meta_is_safe(self, meta: Dict[str, Any]) -> bool:
        meta = dict(meta or {})
        text = str(meta.get("text") or "").strip()
        normalized_text = self._normalize_identity_text(text)
        data_e2e = str(meta.get("dataE2e") or "").strip().lower()
        title = str(meta.get("title") or "").strip().lower()
        aria_label = str(meta.get("ariaLabel") or "").strip().lower()
        class_name = str(meta.get("className") or "").strip().lower()
        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
        top = int(meta.get("top") or 0)
        left = int(meta.get("left") or 0)

        signal_text = " ".join([normalized_text, data_e2e, title, aria_label, class_name])
        block_keywords = (
            "关注",
            "已关注",
            "取消关注",
            "举报",
            "分享",
            "更多",
            "收藏",
            "评论",
            "回复",
            "点赞",
            "推荐",
            "群",
            "follow",
            "share",
            "more",
        )
        if any(keyword.lower() in signal_text for keyword in block_keywords):
            return False

        exact_text_match = normalized_text in self.PROFILE_PRIVATE_EXACT_TEXTS
        private_signal = any(
            token in signal_text
            for token in ("私信", "发消息", "发私信", "send-message", "message-btn", "message", "private", "im")
        )
        if not exact_text_match and not private_signal:
            return False

        # Very small toolbar/icon buttons are too risky across viewport sizes.
        if width and width < 52 and not exact_text_match:
            return False
        if height and height < 28 and not exact_text_match:
            return False
        if not text and not any(token in signal_text for token in ("send-message", "message-btn", "private", "私信", "发消息")):
            return False

        # Suppress top-right utility icons that float above the real action row.
        if top and top < 120 and left > 1100 and width < 56:
            return False
        return True

    def _filter_profile_private_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        filtered = [dict(item or {}) for item in (candidates or []) if self._profile_candidate_meta_is_safe(item or {})]
        if not filtered:
            return []

        explicit = [
            item
            for item in filtered
            if self._normalize_identity_text(item.get("text") or "") in self.PROFILE_PRIVATE_EXACT_TEXTS
        ]
        working = explicit or filtered

        row_reference = None
        exact_rows = [int(item.get("top") or 0) for item in explicit if int(item.get("top") or 0) > 0]
        if exact_rows:
            row_reference = min(exact_rows)
        else:
            candidate_rows = [int(item.get("top") or 0) for item in working if int(item.get("top") or 0) > 0]
            if candidate_rows:
                row_reference = min(candidate_rows)

        if row_reference is not None:
            row_filtered = [
                item for item in working
                if abs(int(item.get("top") or 0) - int(row_reference)) <= 48
            ]
            if row_filtered:
                working = row_filtered

        working.sort(
            key=lambda item: (
                0 if self._normalize_identity_text(item.get("text") or "") in self.PROFILE_PRIVATE_EXACT_TEXTS else 1,
                -int(item.get("score") or 0),
                int(item.get("left") or 0),
                int(item.get("top") or 0),
            )
        )
        return working[:2]

    def _clear_profile_button_marks(self) -> None:
        for attr_name in (self.PROFILE_PRIVATE_ENTRY_ATTR, self.PROFILE_MORE_ACTIONS_ATTR):
            try:
                self.page.evaluate(
                    """(attrName) => {
                        document.querySelectorAll(`[${attrName}]`).forEach((node) => {
                            try {
                                node.removeAttribute(attrName);
                            } catch (e) {}
                        });
                    }""",
                    attr_name,
                )
            except Exception:
                continue

    def _read_profile_entry_runtime_meta(self, attr_name: str, rank: int) -> Dict[str, Any]:
        try:
            result = self.page.evaluate(
                """([attrName, rankValue]) => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const selector = `[${attrName}="${String(rankValue)}"]`;
                    const node = document.querySelector(selector);
                    if (!node || !node.getBoundingClientRect) {
                        return {
                            exists: false,
                            visible: false,
                            text: '',
                            rect: {},
                            topmost: false,
                        };
                    }
                    const rect = node.getBoundingClientRect();
                    const centerX = Math.min(Math.max(rect.left + rect.width / 2, 1), Math.max((window.innerWidth || rect.right) - 1, 1));
                    const centerY = Math.min(Math.max(rect.top + rect.height / 2, 1), Math.max((window.innerHeight || rect.bottom) - 1, 1));
                    const hitNode = document.elementFromPoint(centerX, centerY);
                    return {
                        exists: true,
                        visible: rect.width > 8 && rect.height > 8 && rect.bottom > 0 && rect.right > 0,
                        text: normalize(node.innerText || node.textContent || ''),
                        className: typeof node.className === 'string' ? node.className.slice(0, 240) : '',
                        rect: {
                            top: Math.round(rect.top || 0),
                            left: Math.round(rect.left || 0),
                            width: Math.round(rect.width || 0),
                            height: Math.round(rect.height || 0),
                        },
                        topmost: !!(hitNode && (hitNode === node || node.contains(hitNode) || hitNode.contains(node))),
                    };
                }""",
                [attr_name, int(rank or 0)],
            )
            return dict(result or {}) if isinstance(result, dict) else {}
        except Exception as exc:
            logger.debug(f"读取主页按钮运行态失败(attr={attr_name}, rank={rank}): {exc}")
            return {}

    def _wait_for_profile_entry_stable(
        self,
        *,
        attr_name: str,
        rank: int,
        expected_text: str = "",
        timeout_ms: int = 2600,
        step_ms: int = 250,
    ) -> Dict[str, Any]:
        elapsed = 0
        last_signature = ""
        stable_hits = 0
        best_meta: Dict[str, Any] = {}
        expected_norm = self._normalize_identity_text(expected_text)
        while elapsed <= timeout_ms:
            meta = self._read_profile_entry_runtime_meta(attr_name, rank)
            if meta:
                best_meta = dict(meta)
            rect = dict((meta or {}).get("rect") or {})
            text_norm = self._normalize_identity_text((meta or {}).get("text") or "")
            if (
                meta.get("exists")
                and meta.get("visible")
                and meta.get("topmost")
                and (not expected_norm or text_norm == expected_norm)
            ):
                signature = "|".join(
                    [
                        str(text_norm),
                        str(rect.get("top", "")),
                        str(rect.get("left", "")),
                        str(rect.get("width", "")),
                        str(rect.get("height", "")),
                    ]
                )
                if signature and signature == last_signature:
                    stable_hits += 1
                else:
                    stable_hits = 1
                    last_signature = signature
                if stable_hits >= 2:
                    best_meta["stable"] = True
                    best_meta["stable_hits"] = stable_hits
                    best_meta["wait_elapsed_ms"] = elapsed
                    return best_meta
            else:
                stable_hits = 0
                last_signature = ""

            if elapsed >= timeout_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        if best_meta:
            best_meta["stable"] = False
            best_meta["stable_hits"] = stable_hits
            best_meta["wait_elapsed_ms"] = elapsed
        return best_meta

    def _click_profile_entry_with_fallbacks(
        self,
        *,
        attr_name: str,
        rank: int,
        expected_identities: List[str],
        stage_prefix: str,
    ) -> tuple[bool, Dict[str, Any], List[Dict[str, Any]]]:
        attempts: List[Dict[str, Any]] = []
        locator = self.page.locator(f"[{attr_name}='{rank}']").first

        def _record_attempt(method: str, *, click_ok: bool, snapshot: Dict[str, Any]) -> None:
            attempts.append(
                {
                    "method": method,
                    "click_ok": bool(click_ok),
                    "snapshot": dict(snapshot or {}),
                }
            )

        click_strategies = [
            (
                "human_click_prehover",
                lambda: self._human_helper().click_target(locator, pre_hover=True),
            ),
            (
                "locator_click",
                lambda: (locator.click(timeout=1800), True)[1],
            ),
            (
                "locator_force_click",
                lambda: (locator.click(timeout=1800, force=True), True)[1],
            ),
            (
                "dom_dispatch_click",
                lambda: bool(
                    locator.evaluate(
                        """(node) => {
                            if (!node) return false;
                            const fire = (type) => node.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                composed: true,
                                view: window,
                            }));
                            try { node.focus?.(); } catch (e) {}
                            try { fire('pointerdown'); } catch (e) {}
                            try { fire('mousedown'); } catch (e) {}
                            try { fire('pointerup'); } catch (e) {}
                            try { fire('mouseup'); } catch (e) {}
                            try { fire('click'); } catch (e) {}
                            try { node.click?.(); } catch (e) {}
                            return true;
                        }"""
                    )
                ),
            ),
        ]

        last_snapshot: Dict[str, Any] = {}
        for method_name, action in click_strategies:
            click_ok = False
            try:
                click_ok = bool(action())
            except Exception as exc:
                logger.debug(f"{stage_prefix} 点击策略失败({method_name}): {exc}")
            wait_timeout = 2200 if method_name == "human_click_prehover" else 1800
            ready, snapshot = self._wait_for_private_chat_ready(expected_identities, timeout_ms=wait_timeout, step_ms=200)
            last_snapshot = snapshot
            _record_attempt(method_name, click_ok=click_ok, snapshot=snapshot)
            if ready:
                return True, snapshot, attempts
            self.page.wait_for_timeout(220)
        return False, last_snapshot, attempts

    def _get_dom_diag_dir(self) -> Path:
        diag_dir = getattr(self, "_dom_diag_dir", None)
        if isinstance(diag_dir, Path):
            diag_dir.mkdir(parents=True, exist_ok=True)
            return diag_dir
        diag_dir = get_log_dir() / "dom_diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        self._dom_diag_dir = diag_dir
        return diag_dir

    @staticmethod
    def _sanitize_dom_diag_token(value: str, fallback: str) -> str:
        text = str(value or "").strip()
        cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
        cleaned = cleaned.strip("._-")
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned[:60] or fallback

    def _collect_profile_dom_snapshot(self) -> Dict[str, Any]:
        try:
            snapshot = self.page.evaluate(
                """() => {
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const normalizeTight = (value) => normalize(value).replace(/\\s+/g, '').replace(/\\u200b/g, '').toLowerCase();
                    const rectInfo = (node) => {
                        if (!node || !node.getBoundingClientRect) return {};
                        const rect = node.getBoundingClientRect();
                        return {
                            top: Math.round(rect.top || 0),
                            left: Math.round(rect.left || 0),
                            width: Math.round(rect.width || 0),
                            height: Math.round(rect.height || 0),
                            bottom: Math.round(rect.bottom || 0),
                            right: Math.round(rect.right || 0),
                        };
                    };
                    const isVisible = (node) => !!(
                        node &&
                        node.getBoundingClientRect &&
                        node.getBoundingClientRect().width > 8 &&
                        node.getBoundingClientRect().height > 8 &&
                        node.getBoundingClientRect().bottom > 0 &&
                        node.getBoundingClientRect().right > 0 &&
                        node.getBoundingClientRect().top < (window.innerHeight || 0) &&
                        node.getBoundingClientRect().left < (window.innerWidth || 0)
                    );
                    const roots = Array.from(document.querySelectorAll(
                        "[data-e2e='user-info'], [class*='user-info'], [class*='userInfo'], [class*='user-header'], [class*='userHeader']"
                    )).filter(isVisible);
                    const actionAreas = [];
                    const pushArea = (node) => {
                        if (!node || actionAreas.includes(node)) return;
                        actionAreas.push(node);
                    };
                    roots.forEach((root) => {
                        pushArea(root);
                        pushArea(root.parentElement);
                        pushArea(root.closest("main"));
                        pushArea(root.closest("section"));
                    });
                    if (!actionAreas.length) {
                        Array.from(document.querySelectorAll("main, section, body")).slice(0, 3).forEach(pushArea);
                    }
                    const actionNodes = [];
                    const pushActionNode = (node, source) => {
                        if (!node || actionNodes.some((item) => item.node === node)) return;
                        actionNodes.push({ node, source });
                    };
                    const collectActionNodes = (scope, source) => {
                        if (!scope || !scope.querySelectorAll) return;
                        scope.querySelectorAll(
                            [
                                "[data-e2e='send-message-btn']",
                                "[data-e2e='more-actions']",
                                "button",
                                "[role='button']",
                                "a",
                                "div[role='button']",
                                "[title*='私信']",
                                "[title*='发消息']",
                                "[aria-label*='私信']",
                                "[aria-label*='发消息']",
                                "[class*='message']",
                                "[class*='Message']",
                                "[class*='chat']",
                                "[class*='Chat']",
                            ].join(", ")
                        ).forEach((node) => {
                            const clickable = node.closest(
                                "[data-e2e='send-message-btn'], [data-e2e='more-actions'], button, [role='button'], a, div[role='button']"
                            ) || node;
                            pushActionNode(clickable, source);
                        });
                    };

                    actionAreas.forEach((area, index) => collectActionNodes(area, index === 0 ? "profile_root" : "profile_area"));

                    const actionCandidates = actionNodes
                        .filter((item) => isVisible(item.node))
                        .slice(0, 120)
                        .map((item) => {
                            const node = item.node;
                            const rect = rectInfo(node);
                            const centerX = Math.min(Math.max((rect.left || 0) + (rect.width || 0) / 2, 1), Math.max(window.innerWidth - 1, 1));
                            const centerY = Math.min(Math.max((rect.top || 0) + (rect.height || 0) / 2, 1), Math.max(window.innerHeight - 1, 1));
                            const hitNode = document.elementFromPoint(centerX, centerY);
                            return {
                                source: item.source,
                                text: normalize(node.innerText || node.textContent || ''),
                                tightText: normalizeTight(node.innerText || node.textContent || ''),
                                dataE2e: String(node.getAttribute('data-e2e') || ''),
                                title: String(node.getAttribute('title') || ''),
                                ariaLabel: String(node.getAttribute('aria-label') || ''),
                                className: typeof node.className === 'string' ? node.className.slice(0, 240) : '',
                                tagName: String(node.tagName || '').toLowerCase(),
                                markedPrivateRank: String(node.getAttribute('data-huoke-private-entry-candidate') || ''),
                                markedMoreRank: String(node.getAttribute('data-huoke-more-actions-candidate') || ''),
                                topmost: !!(hitNode && (hitNode === node || node.contains(hitNode) || hitNode.contains(node))),
                                rect,
                            };
                        });

                    const menuCandidates = Array.from(document.querySelectorAll("[role='menuitem'], [role='option'], [role='dialog'] button, [role='dialog'] [role='button']"))
                        .filter(isVisible)
                        .slice(0, 30)
                        .map((node) => ({
                            text: normalize(node.innerText || node.textContent || ''),
                            dataE2e: String(node.getAttribute('data-e2e') || ''),
                            title: String(node.getAttribute('title') || ''),
                            ariaLabel: String(node.getAttribute('aria-label') || ''),
                            className: typeof node.className === 'string' ? node.className.slice(0, 240) : '',
                            tagName: String(node.tagName || '').toLowerCase(),
                            rect: rectInfo(node),
                        }));
                    const loginDialogText = normalize(document.body ? (document.body.innerText || document.body.textContent || '') : '');
                    const rawActionCandidates = actionCandidates.slice(0, 80);
                    const markedActionCandidates = rawActionCandidates.filter((item) => item.markedPrivateRank || item.markedMoreRank);

                    return {
                        url: location.href,
                        title: document.title || '',
                        viewport: {
                            width: window.innerWidth || 0,
                            height: window.innerHeight || 0,
                        },
                        userInfoRootCount: roots.length,
                        actionAreaCount: actionAreas.filter(Boolean).length,
                        hasSendMessageBtn: !!document.querySelector("[data-e2e='send-message-btn']"),
                        hasMoreActions: !!document.querySelector("[data-e2e='more-actions'], .more-actions"),
                        hasLoginModal: !!document.querySelector(".dy-account-close, [role='dialog']") || loginDialogText.includes("登录后查看"),
                        hasChatInput: !!document.querySelector(
                            "[data-e2e='msg-input'] [contenteditable='true'], " +
                            "[data-e2e='msg-input'] [role='textbox'], " +
                            "[data-e2e='msg-input'] textarea"
                        ),
                        actionCandidates: markedActionCandidates,
                        rawActionCandidates,
                        menuCandidates,
                    };
                }"""
            )
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as exc:
            logger.debug(f"采集主页私信DOM快照失败: {exc}")
            return {}

    def _export_profile_dom_diagnostic(
        self,
        *,
        stage: str,
        user_id: str,
        profile_url: str = "",
        expected_identities: List[str] | None = None,
        private_candidates: List[Dict[str, Any]] | None = None,
        raw_private_candidates: List[Dict[str, Any]] | None = None,
        more_candidates: List[Dict[str, Any]] | None = None,
        open_snapshot: Dict[str, Any] | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> Path | None:
        try:
            payload = self._collect_profile_dom_snapshot()
            payload["stage"] = stage
            payload["user_id"] = user_id
            payload["profile_url"] = profile_url
            payload["expected_identities"] = list(expected_identities or [])
            payload["private_candidates"] = list(private_candidates or [])
            payload["raw_private_candidates"] = list(raw_private_candidates or [])
            payload["more_candidates"] = list(more_candidates or [])
            payload["chat_snapshot"] = dict(open_snapshot or {})
            if extra:
                payload["extra"] = dict(extra)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            user_token = self._sanitize_dom_diag_token(user_id, "unknown_user")
            stage_token = self._sanitize_dom_diag_token(stage, "profile_snapshot")
            file_path = self._get_dom_diag_dir() / f"profile_private_entry_{stamp}_{user_token}_{stage_token}.json"
            file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"主页私信DOM诊断已导出: {file_path}")
            return file_path
        except Exception as exc:
            logger.warning(f"导出主页私信DOM诊断失败(stage={stage}, user={user_id}): {exc}")
            return None

    def _mark_profile_private_entry_candidates(self) -> List[Dict[str, Any]]:
        try:
            candidates = self.page.evaluate(
                f"""() => {{
                    const ENTRY_ATTR = "{self.PROFILE_PRIVATE_ENTRY_ATTR}";
                    document.querySelectorAll(`[${{ENTRY_ATTR}}]`).forEach((node) => {{
                        try {{
                            node.removeAttribute(ENTRY_ATTR);
                        }} catch (e) {{}}
                    }});

                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const normalizeTight = (value) => normalize(value).replace(/\\s+/g, '').replace(/\\u200b/g, '').toLowerCase();
                    const visible = (node) => !!(
                        node &&
                        node.getBoundingClientRect &&
                        node.getBoundingClientRect().width > 8 &&
                        node.getBoundingClientRect().height > 8 &&
                        node.getBoundingClientRect().bottom > 0 &&
                        node.getBoundingClientRect().right > 0 &&
                        node.getBoundingClientRect().top < (window.innerHeight || 0) &&
                        node.getBoundingClientRect().left < (window.innerWidth || 0)
                    );
                    const isDisabled = (node) => !!(
                        !node ||
                        node.hasAttribute('disabled') ||
                        node.getAttribute('aria-disabled') === 'true'
                    );
                    const roots = Array.from(document.querySelectorAll(
                        "[data-e2e='user-info'], [class*='user-info'], [class*='userInfo'], [class*='user-header'], [class*='userHeader']"
                    )).filter(visible);
                    const actionAreas = [];
                    const pushArea = (node) => {{
                        if (!node || actionAreas.includes(node)) return;
                        actionAreas.push(node);
                    }};
                    roots.forEach((root) => {{
                        pushArea(root);
                        pushArea(root.parentElement);
                        pushArea(root.closest("main"));
                        pushArea(root.closest("section"));
                    }});
                    if (!actionAreas.length) {{
                        Array.from(document.querySelectorAll("main, section, body")).slice(0, 3).forEach(pushArea);
                    }}
                    const blockedKeywords = ['关注', '已关注', '取消关注', '更多', '分享', '举报', '收藏', '评论', '回复', '点赞', '群', 'chat'];
                    const exactAllow = ['私信', '发消息', '发私信'];
                    const uniqueNodes = [];
                    const pushNode = (node) => {{
                        if (!node || uniqueNodes.includes(node)) return;
                        uniqueNodes.push(node);
                    }};

                    const collectNodes = (scope) => {{
                        if (!scope || !scope.querySelectorAll) return;
                        scope.querySelectorAll(
                            [
                                "[data-e2e='send-message-btn']",
                                "button",
                                "[role='button']",
                                "a",
                                "div[role='button']",
                                "[title*='私信']",
                                "[title*='发消息']",
                                "[aria-label*='私信']",
                                "[aria-label*='发消息']",
                                "[class*='message']",
                                "[class*='Message']",
                                "[class*='chat']",
                                "[class*='Chat']",
                            ].join(", ")
                        ).forEach((node) => {{
                            const clickable = node.closest("[data-e2e='send-message-btn'], button, [role='button'], a, div[role='button']") || node;
                            pushNode(clickable);
                        }});
                    }};
                    actionAreas.forEach(collectNodes);

                    const scored = [];
                    for (const node of uniqueNodes) {{
                        if (!visible(node) || isDisabled(node)) continue;
                        const rect = node.getBoundingClientRect();
                        const text = normalize(node.innerText || node.textContent || '');
                        const tightText = normalizeTight(text);
                        const dataE2e = String(node.getAttribute('data-e2e') || '').toLowerCase();
                        const title = String(node.getAttribute('title') || '').toLowerCase();
                        const ariaLabel = String(node.getAttribute('aria-label') || '').toLowerCase();
                        const className = typeof node.className === 'string' ? node.className.toLowerCase() : '';
                        const signature = `${{tightText}}|${{dataE2e}}|${{title}}|${{ariaLabel}}|${{Math.round(rect.top)}}|${{Math.round(rect.left)}}`;
                        const hitNode = document.elementFromPoint(
                            Math.min(Math.max(rect.left + rect.width / 2, 1), (window.innerWidth || rect.right) - 1),
                            Math.min(Math.max(rect.top + rect.height / 2, 1), (window.innerHeight || rect.bottom) - 1)
                        );
                        const topmost = !!(
                            hitNode &&
                            (hitNode === node || node.contains(hitNode) || hitNode.contains(node))
                        );
                        let score = 0;
                        if (dataE2e.includes('send-message-btn')) score += 1000;
                        if (exactAllow.includes(tightText)) score += 420;
                        if (tightText === '私信') score += 180;
                        if (tightText === '发消息' || tightText === '发私信') score += 160;
                        if (dataE2e.includes('message')) score += 150;
                        if (title.includes('私信') || title.includes('发消息')) score += 200;
                        if (ariaLabel.includes('私信') || ariaLabel.includes('发消息')) score += 200;
                        if (className.includes('send-message') || className.includes('message-btn')) score += 120;
                        if (className.includes('message') || className.includes('private') || className.includes('im')) score += 80;
                        if (roots.some((root) => root.contains(node))) score += 120;
                        if (topmost) score += 70;
                        if (rect.top < (window.innerHeight || 0) * 0.62) score += 35;
                        if (rect.width >= 56 && rect.width <= 260) score += 25;
                        if (rect.height >= 28 && rect.height <= 72) score += 25;
                        if (!text && !(dataE2e.includes('message') || title.includes('私信') || ariaLabel.includes('私信') || ariaLabel.includes('发消息'))) score -= 50;
                        if (tightText === '聊天') score -= 500;
                        if (blockedKeywords.some((keyword) => tightText.includes(keyword) || title.includes(keyword) || ariaLabel.includes(keyword))) score -= 240;
                        if (className.includes('follow') || className.includes('share') || className.includes('more')) score -= 180;
                        if (score < 120) continue;
                        scored.push({{
                            signature,
                            text,
                            dataE2e,
                            title,
                            ariaLabel,
                            className: className.slice(0, 200),
                            score,
                            top: Math.round(rect.top),
                            left: Math.round(rect.left),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                            topmost,
                            node,
                        }});
                    }}

                    scored.sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left);
                    const deduped = [];
                    const seen = new Set();
                    for (const item of scored) {{
                        if (seen.has(item.signature)) continue;
                        seen.add(item.signature);
                        deduped.push(item);
                        if (deduped.length >= 4) break;
                    }}

                    deduped.forEach((item, index) => {{
                        try {{
                            item.node.setAttribute(ENTRY_ATTR, String(index + 1));
                        }} catch (e) {{}}
                    }});

                    return deduped.map((item, index) => ({{
                        rank: index + 1,
                        text: item.text,
                        dataE2e: item.dataE2e,
                        title: item.title,
                        ariaLabel: item.ariaLabel,
                        className: item.className,
                        score: item.score,
                        top: item.top,
                        left: item.left,
                        width: item.width,
                        height: item.height,
                        topmost: item.topmost,
                    }}));
                }}"""
            )
            return list(candidates or []) if isinstance(candidates, list) else []
        except Exception as exc:
            logger.debug(f"主页私信按钮候选打分失败: {exc}")
            return []

    def _mark_profile_more_actions_candidates(self) -> List[Dict[str, Any]]:
        try:
            candidates = self.page.evaluate(
                f"""() => {{
                    const ATTR = "{self.PROFILE_MORE_ACTIONS_ATTR}";
                    document.querySelectorAll(`[${{ATTR}}]`).forEach((node) => {{
                        try {{
                            node.removeAttribute(ATTR);
                        }} catch (e) {{}}
                    }});
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const normalizeTight = (value) => normalize(value).replace(/\\s+/g, '').replace(/\\u200b/g, '').toLowerCase();
                    const visible = (node) => !!(
                        node &&
                        node.getBoundingClientRect &&
                        node.getBoundingClientRect().width > 8 &&
                        node.getBoundingClientRect().height > 8 &&
                        node.getBoundingClientRect().bottom > 0 &&
                        node.getBoundingClientRect().right > 0 &&
                        node.getBoundingClientRect().top < (window.innerHeight || 0) &&
                        node.getBoundingClientRect().left < (window.innerWidth || 0)
                    );
                    const roots = Array.from(document.querySelectorAll(
                        "[data-e2e='user-info'], [class*='user-info'], [class*='userInfo'], [class*='user-header'], [class*='userHeader']"
                    )).filter(visible);
                    const actionAreas = [];
                    const pushArea = (node) => {{
                        if (!node || actionAreas.includes(node)) return;
                        actionAreas.push(node);
                    }};
                    roots.forEach((root) => {{
                        pushArea(root);
                        pushArea(root.parentElement);
                        pushArea(root.closest("main"));
                        pushArea(root.closest("section"));
                    }});
                    if (!actionAreas.length) {{
                        Array.from(document.querySelectorAll("main, section, body")).slice(0, 3).forEach(pushArea);
                    }}
                    const uniqueNodes = [];
                    const pushNode = (node) => {{
                        if (!node || uniqueNodes.includes(node)) return;
                        uniqueNodes.push(node);
                    }};
                    for (const root of actionAreas) {{
                        if (!root || !root.querySelectorAll) continue;
                        root.querySelectorAll(
                            "[data-e2e='more-actions'], .more-actions, button, [role='button'], [aria-label*='更多'], [title*='更多'], [class*='more']"
                        ).forEach((node) => {{
                            const clickable = node.closest("[data-e2e='more-actions'], .more-actions, button, [role='button']") || node;
                            pushNode(clickable);
                        }});
                    }}
                    const scored = [];
                    for (const node of uniqueNodes) {{
                        if (!visible(node)) continue;
                        const text = normalize(node.innerText || node.textContent || '');
                        const tightText = normalizeTight(text);
                        const dataE2e = String(node.getAttribute('data-e2e') || '').toLowerCase();
                        const title = String(node.getAttribute('title') || '').toLowerCase();
                        const ariaLabel = String(node.getAttribute('aria-label') || '').toLowerCase();
                        const className = typeof node.className === 'string' ? node.className.toLowerCase() : '';
                        const rect = node.getBoundingClientRect();
                        const hitNode = document.elementFromPoint(
                            Math.min(Math.max(rect.left + rect.width / 2, 1), (window.innerWidth || rect.right) - 1),
                            Math.min(Math.max(rect.top + rect.height / 2, 1), (window.innerHeight || rect.bottom) - 1)
                        );
                        let score = 0;
                        if (dataE2e.includes('more-actions')) score += 220;
                        if (className.includes('more-actions')) score += 150;
                        if (tightText === '更多') score += 120;
                        if (title.includes('更多') || ariaLabel.includes('更多')) score += 160;
                        if (className.includes('more')) score += 100;
                        if (roots.some((root) => root.contains(node))) score += 80;
                        if (hitNode && (hitNode === node || node.contains(hitNode) || hitNode.contains(node))) score += 40;
                        if (score < 120) continue;
                        scored.push({{
                            node,
                            text,
                            dataE2e,
                            title,
                            ariaLabel,
                            className: className.slice(0, 200),
                            score,
                        }});
                    }}
                    scored.sort((a, b) => b.score - a.score);
                    scored.slice(0, 2).forEach((item, index) => {{
                        try {{
                            item.node.setAttribute(ATTR, String(index + 1));
                        }} catch (e) {{}}
                    }});
                    return scored.slice(0, 2).map((item, index) => ({{
                        rank: index + 1,
                        text: item.text,
                        dataE2e: item.dataE2e,
                        title: item.title,
                        ariaLabel: item.ariaLabel,
                        className: item.className,
                        score: item.score,
                    }}));
                }}"""
            )
            return list(candidates or []) if isinstance(candidates, list) else []
        except Exception as exc:
            logger.debug(f"主页更多按钮候选打分失败: {exc}")
            return []

    def _click_send_button(self) -> bool:
        send_selectors = [
            "[data-e2e='msg-input'] [data-e2e='send-message-btn']",
            "[data-e2e='msg-input'] button:has-text('发送')",
            "[data-e2e='msg-input'] [role='button']:has-text('发送')",
            "[data-e2e='send-message-btn']",
            "button[data-e2e='send-message-btn']",
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            "[class*='send-message']",
            "[class*='send-btn']",
            "[class*='sendBtn']",
            "[class*='sendButton']",
        ]
        for selector in send_selectors:
            try:
                locator = self.page.locator(selector)
                count = min(locator.count(), 3)
                for index in range(count):
                    button = locator.nth(index)
                    if not button.is_visible(timeout=800):
                        continue
                    if self._human_helper().click_target(button):
                        logger.info(f"已点击弹窗发送按钮: selector={selector}, index={index}")
                        return True
            except Exception:
                continue
        return False

    def _dispatch_send_action(
        self,
        *,
        input_locator: Any,
        message: str,
        expected_identities: List[str],
        previous_message_tail: List[str] | None = None,
        allow_profile_dialog_fallback: bool = False,
    ) -> bool:
        self.page.wait_for_timeout(120)
        if self._click_send_button():
            self.page.wait_for_timeout(180)
            if self._verify_send_confirmation(
                input_locator=input_locator,
                message=message,
                expected_identities=expected_identities,
                previous_message_tail=previous_message_tail,
                allow_profile_dialog_fallback=allow_profile_dialog_fallback,
            ):
                return True

        try:
            input_locator.press("Enter", timeout=1500)
            logger.info("已通过输入框 Enter 触发发送")
        except Exception as exc:
            logger.debug(f"输入框 Enter 发送失败，改用键盘 Enter: {exc}")
            try:
                self.page.keyboard.press("Enter")
                logger.info("已通过键盘 Enter 触发发送")
            except Exception:
                return False

        return self._verify_send_confirmation(
            input_locator=input_locator,
            message=message,
            expected_identities=expected_identities,
            previous_message_tail=previous_message_tail,
            allow_profile_dialog_fallback=allow_profile_dialog_fallback,
        )

    def _find_message_input_locator(self):
        result = get_input_locator_service().locate(
            self.page,
            InputLocatorContext(scene="private_message"),
        )
        if result.success and result.input_candidate is not None:
            return result.input_candidate.locator
        return None

    def _read_input_locator_content(self, locator: Any) -> str:
        try:
            raw = locator.evaluate(
                """el => {
                    if (!el) return '';
                    if (typeof el.value === 'string') return el.value;
                    return el.innerText || el.textContent || '';
                }"""
            )
            return str(raw or "").strip()
        except Exception:
            return ""

    def _verify_send_confirmation(
        self,
        *,
        input_locator: Any,
        message: str,
        expected_identities: List[str],
        previous_message_tail: List[str] | None = None,
        timeout_ms: int = 3000,
        step_ms: int = 250,
        allow_profile_dialog_fallback: bool = False,
    ) -> bool:
        normalized_message = " ".join(str(message or "").strip().split())
        normalized_message_key = self._normalize_identity_text(normalized_message)
        normalized_previous_tail = [
            self._normalize_identity_text(item)
            for item in (previous_message_tail or [])
            if self._normalize_identity_text(item)
        ]
        context = dict(getattr(self, "_active_target_identity_context", {}) or {})
        elapsed = 0
        while elapsed <= timeout_ms:
            snapshot = self._collect_chat_target_snapshot()
            normalized_current_tail = [
                self._normalize_identity_text(item)
                for item in (snapshot.get("messageTail") or [])
                if self._normalize_identity_text(item)
            ]
            message_seen_in_tail = bool(
                normalized_message_key
                and any(normalized_message_key in tail_item for tail_item in normalized_current_tail)
            )
            tail_advanced = message_tail_has_advanced(
                before_items=normalized_previous_tail,
                after_items=normalized_current_tail,
                original_content=normalized_message,
                normalize_text=self._normalize_identity_text,
            )
            decision = decide_send_confirmation(
                has_explicit_failure=bool(snapshot.get("hasSendFailure")),
                input_cleared=not self._read_input_locator_content(input_locator).strip(),
                tail_advanced=tail_advanced,
                message_seen_in_tail=message_seen_in_tail,
            )
            if snapshot.get("hasSendFailure"):
                logger.warning(
                    "发送确认检测到明确失败标记，判定发送失败: "
                    f"expected={expected_identities}, active_names={snapshot.get('activeNames', [])}"
                )
                return False
            target_matches = self._snapshot_matches_expected_target(snapshot, expected_identities)
            trusted_profile_dialog = bool(
                allow_profile_dialog_fallback
                and self._allow_profile_dialog_target_fallback(snapshot)
            )
            if not target_matches and not trusted_profile_dialog:
                logger.warning(
                    f"发送后目标会话校验失败: expected={expected_identities}, "
                    f"active_names={snapshot.get('activeNames', [])}, "
                    f"identity_level={context.get('identity_level')}, api_matched={context.get('api_matched')}"
                )
                return False

            if decision.success:
                if decision.reason_code == "message_seen_in_tail":
                    logger.info(
                        "发送确认成功：聊天尾部已出现目标消息 "
                        f"target_matches={target_matches} trusted_profile_dialog={trusted_profile_dialog}"
                    )
                elif decision.reason_code == "tail_advanced":
                    logger.info(
                        "发送确认成功：聊天尾部已推进，尽管输入框仍有残留 "
                        f"target_matches={target_matches} trusted_profile_dialog={trusted_profile_dialog}"
                    )
                else:
                    logger.info(
                        "发送确认成功：输入框已清空，按成功处理以避免误判补发 "
                        f"tail_advanced={tail_advanced} target_matches={target_matches} "
                        f"trusted_profile_dialog={trusted_profile_dialog}"
                    )
                return True

            if elapsed >= timeout_ms:
                break
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
        logger.warning(
            "发送确认失败：输入框仍有内容且聊天尾部未推进 "
            f"expected={expected_identities}, normalized_message={normalized_message[:30]}"
        )
        return False

    def send_private_message(self, user_id: str, message: str):
        """
        给指定用户发送私信
        user_id: 对应数据库中的 sec_uid
        """
        user = None
        platform = 'douyin'
        try:
            logger.info(f"开始给用户 {user_id} 发送私信...")

            if self.db.is_sent(user_id):
                logger.info(f"用户 {user_id} 已标记为 sent，跳过重复发送")
                return False
            
            user = self.db.get_user_by_id(user_id)
            if not user:
                logger.error(f"未找到用户 {user_id} 信息")
                return False
                
            profile_url = user.get('profile_url')
            platform = user.get('platform', 'douyin')
            if not profile_url:
                logger.warning(f"用户 {user_id} 没有主页链接")
                self.db.update_customer_status(user_id, platform, 'failed_no_url')
                return False

            target_identity_context = self._build_target_identity_context(user_id, user)
            self._active_target_identity_context = dict(target_identity_context)
            expected_identities = list(target_identity_context.get("expected_identities") or [])
            if not expected_identities:
                logger.warning(f"用户 {user_id} 缺少可校验的会话身份信息")
                self.db.update_customer_status(user_id, platform, 'failed_target_mismatch')
                return False
            if not self._identity_allows_direct_send(target_identity_context):
                logger.warning(
                    f"用户 {user_id} 的目标身份证据不足，拒绝直接发送: "
                    f"identity_level={target_identity_context.get('identity_level')}, "
                    f"api_matched={target_identity_context.get('api_matched')}, "
                    f"api_message_count={target_identity_context.get('api_message_count')}"
                )
                self.db.update_customer_status(user_id, platform, 'failed_target_mismatch')
                return False

            self._navigate_to_profile(profile_url)

            btn_found = False
            open_snapshot: Dict[str, Any] = {}
            raw_profile_candidates = self._mark_profile_private_entry_candidates()
            profile_candidates = self._filter_profile_private_candidates(raw_profile_candidates)
            if profile_candidates:
                logger.info(f"主页私信按钮候选(过滤后): {profile_candidates}")
                if raw_profile_candidates != profile_candidates:
                    logger.info(f"主页私信按钮候选(原始): {raw_profile_candidates}")
            else:
                self._export_profile_dom_diagnostic(
                    stage="profile_private_candidates_empty",
                    user_id=user_id,
                    profile_url=profile_url,
                    expected_identities=expected_identities,
                    private_candidates=profile_candidates,
                    raw_private_candidates=raw_profile_candidates,
                )
            for candidate_meta in profile_candidates:
                try:
                    rank = int(candidate_meta.get("rank") or 0)
                    if rank <= 0:
                        continue
                    candidate = self.page.locator(f"[{self.PROFILE_PRIVATE_ENTRY_ATTR}='{rank}']").first
                    if not candidate.is_visible(timeout=1200):
                        continue
                    if not self._locator_seems_safe_message_entry(candidate, f"ranked:{candidate_meta.get('text', '')}"):
                        continue
                    stable_meta = self._wait_for_profile_entry_stable(
                        attr_name=self.PROFILE_PRIVATE_ENTRY_ATTR,
                        rank=rank,
                        expected_text=str(candidate_meta.get("text") or ""),
                    )
                    logger.info(f"尝试点击主页私信入口: candidate={candidate_meta}, stable_meta={stable_meta}")
                    ready, open_snapshot, click_attempts = self._click_profile_entry_with_fallbacks(
                        attr_name=self.PROFILE_PRIVATE_ENTRY_ATTR,
                        rank=rank,
                        expected_identities=expected_identities,
                        stage_prefix="profile_private_entry",
                    )
                    if ready:
                        btn_found = True
                        break
                    logger.warning(
                        f"点击主页私信候选后未进入正确会话: candidate={candidate_meta}, "
                        f"active_names={open_snapshot.get('activeNames', [])}"
                    )
                    self._export_profile_dom_diagnostic(
                        stage="profile_private_click_not_ready",
                        user_id=user_id,
                        profile_url=profile_url,
                        expected_identities=expected_identities,
                        private_candidates=profile_candidates,
                        raw_private_candidates=raw_profile_candidates,
                        open_snapshot=open_snapshot,
                        extra={
                            "clicked_candidate": candidate_meta,
                            "stable_meta": stable_meta,
                            "click_attempts": click_attempts,
                        },
                    )
                    self._navigate_to_profile(profile_url)
                    raw_profile_candidates = self._mark_profile_private_entry_candidates()
                    profile_candidates = self._filter_profile_private_candidates(raw_profile_candidates)
                except Exception as e:
                    logger.warning(f"处理主页私信候选失败: {candidate_meta}, error={e}")
                    self._export_profile_dom_diagnostic(
                        stage="profile_private_click_exception",
                        user_id=user_id,
                        profile_url=profile_url,
                        expected_identities=expected_identities,
                        private_candidates=profile_candidates,
                        raw_private_candidates=raw_profile_candidates,
                        extra={"clicked_candidate": candidate_meta, "error": str(e)},
                    )
                    self._navigate_to_profile(profile_url)
                    raw_profile_candidates = self._mark_profile_private_entry_candidates()
                    profile_candidates = self._filter_profile_private_candidates(raw_profile_candidates)
            
            if not btn_found:
                more_candidates: List[Dict[str, Any]] = []
                try:
                    page_url = self.page.url
                    logger.warning(
                        f"未找到有效私信入口，用户: {user_id}, 页面URL: {page_url}, "
                        f"expected_identities={expected_identities}"
                    )
                except Exception:
                    pass

                try:
                    more_candidates = self._mark_profile_more_actions_candidates()
                    if more_candidates:
                        logger.info(f"主页更多按钮候选: {more_candidates}")
                    else:
                        self._export_profile_dom_diagnostic(
                            stage="profile_more_candidates_empty",
                            user_id=user_id,
                            profile_url=profile_url,
                            expected_identities=expected_identities,
                            private_candidates=profile_candidates,
                            raw_private_candidates=raw_profile_candidates,
                            more_candidates=more_candidates,
                        )
                    more_btn = self.page.locator(f"[{self.PROFILE_MORE_ACTIONS_ATTR}='1']").first
                    if more_btn.is_visible():
                        more_stable_meta = self._wait_for_profile_entry_stable(
                            attr_name=self.PROFILE_MORE_ACTIONS_ATTR,
                            rank=1,
                            expected_text=str((more_candidates[0] or {}).get("text") or ""),
                            timeout_ms=2200,
                            step_ms=220,
                        )
                        logger.info(f"尝试点击主页更多入口: candidate={more_candidates[0] if more_candidates else {}}, stable_meta={more_stable_meta}")
                        if not self._human_helper().click_target(more_btn, pre_hover=True):
                            try:
                                more_btn.click(timeout=1500)
                            except Exception:
                                more_btn.click(force=True)
                        random_sleep(0.8, 1.4)
                        msg_option = self.page.locator("[role='menuitem']:has-text('私信'), button:has-text('私信'), [role='option']:has-text('私信')").first
                        if msg_option.is_visible() and self._locator_seems_safe_message_entry(msg_option, "menuitem:私信"):
                            logger.info("通过更多菜单点击私信")
                            click_attempts: List[Dict[str, Any]] = []
                            click_ok = self._human_helper().click_target(msg_option, pre_hover=True)
                            btn_found, open_snapshot = self._wait_for_private_chat_ready(expected_identities, timeout_ms=2200, step_ms=200)
                            click_attempts.append({"method": "menu_human_click_prehover", "click_ok": bool(click_ok), "snapshot": dict(open_snapshot or {})})
                            if not btn_found:
                                try:
                                    msg_option.click(timeout=1600)
                                    click_ok = True
                                except Exception:
                                    click_ok = False
                                btn_found, open_snapshot = self._wait_for_private_chat_ready(expected_identities, timeout_ms=1800, step_ms=200)
                                click_attempts.append({"method": "menu_locator_click", "click_ok": bool(click_ok), "snapshot": dict(open_snapshot or {})})
                            if not btn_found:
                                try:
                                    msg_option.click(force=True)
                                    click_ok = True
                                except Exception:
                                    click_ok = False
                                btn_found, open_snapshot = self._wait_for_private_chat_ready(expected_identities, timeout_ms=1800, step_ms=200)
                                click_attempts.append({"method": "menu_force_click", "click_ok": bool(click_ok), "snapshot": dict(open_snapshot or {})})
                            if not btn_found:
                                self._export_profile_dom_diagnostic(
                                    stage="profile_more_click_not_ready",
                                    user_id=user_id,
                                    profile_url=profile_url,
                                    expected_identities=expected_identities,
                                    private_candidates=profile_candidates,
                                    raw_private_candidates=raw_profile_candidates,
                                    more_candidates=more_candidates,
                                    open_snapshot=open_snapshot,
                                    extra={
                                        "more_stable_meta": more_stable_meta,
                                        "click_attempts": click_attempts,
                                    },
                                )
                except Exception as e:
                    logger.debug(f"通过更多菜单点击私信失败: {e}")
                    self._export_profile_dom_diagnostic(
                        stage="profile_more_click_exception",
                        user_id=user_id,
                        profile_url=profile_url,
                        expected_identities=expected_identities,
                        private_candidates=profile_candidates,
                        raw_private_candidates=raw_profile_candidates,
                        more_candidates=more_candidates,
                        extra={"error": str(e)},
                    )

            if not btn_found:
                failure_status = self._infer_chat_open_failure_status(open_snapshot, expected_identities)
                logger.warning(
                    f"私信入口点击后仍未进入目标会话，状态={failure_status}, "
                    f"expected={expected_identities}, active={open_snapshot.get('activeNames', [])}"
                )
                self._export_profile_dom_diagnostic(
                    stage="profile_open_chat_failed",
                    user_id=user_id,
                    profile_url=profile_url,
                    expected_identities=expected_identities,
                    private_candidates=profile_candidates,
                    raw_private_candidates=raw_profile_candidates,
                    open_snapshot=open_snapshot,
                    extra={"failure_status": failure_status},
                )
                self.db.update_customer_status(user_id, platform, failure_status)
                return False

            if self.page.is_visible(".dy-account-close") or self.page.is_visible("text='登录后查看'"):
                logger.warning("检测到登录弹窗，未登录状态无法私信")
                self.db.update_customer_status(user_id, platform, 'failed_login_required')
                return False

            random_sleep(2, 3)

            ready_snapshot = self._collect_chat_target_snapshot()
            trusted_profile_dialog = bool(
                self._allow_profile_dialog_target_fallback(ready_snapshot)
            )
            if not self._snapshot_matches_expected_target(ready_snapshot, expected_identities) and not trusted_profile_dialog:
                logger.warning(
                    f"发送前目标会话校验失败: expected={expected_identities}, "
                    f"active={ready_snapshot.get('activeNames', [])}, "
                    f"identity_level={target_identity_context.get('identity_level')}, "
                    f"api_matched={target_identity_context.get('api_matched')}"
                )
                self.db.update_customer_status(user_id, platform, 'failed_target_mismatch')
                return False
            if trusted_profile_dialog:
                logger.warning(
                    f"发送前未解析到会话标题，但已通过强身份/API证据确认主页私信弹窗目标，继续发送: user={user_id}"
                )

            try:
                pre_send_snapshot = self._collect_chat_target_snapshot()
                input_locator = self._find_message_input_locator()
                if input_locator is None:
                    logger.warning(f"无法定位输入框 (User: {user_id}). Curr URL: {self.page.url}")
                    self._export_profile_dom_diagnostic(
                        stage="profile_input_not_found",
                        user_id=user_id,
                        profile_url=profile_url,
                        expected_identities=expected_identities,
                        open_snapshot=pre_send_snapshot,
                    )
                    self.db.update_customer_status(user_id, platform, 'failed_input_not_found')
                    return False

                if not self._human_helper().clear_and_type(input_locator, message):
                    # clear_and_type 失败（通常是点击目标失败），回退前先清空输入框避免文本叠加
                    try:
                        input_locator.click(timeout=1500)
                        self.page.keyboard.press("Control+A")
                        self.page.keyboard.press("Backspace")
                    except Exception:
                        pass
                    self.page.keyboard.type(message, delay=100)
                random_sleep(0.5, 1.5)

                if not self._dispatch_send_action(
                    input_locator=input_locator,
                    message=message,
                    expected_identities=expected_identities,
                    previous_message_tail=list(pre_send_snapshot.get("messageTail") or []),
                    allow_profile_dialog_fallback=trusted_profile_dialog,
                ):
                    logger.warning(f"发送确认失败，未标记 sent: user={user_id}")
                    self._export_profile_dom_diagnostic(
                        stage="profile_send_confirmation_failed",
                        user_id=user_id,
                        profile_url=profile_url,
                        expected_identities=expected_identities,
                        open_snapshot=self._collect_chat_target_snapshot(),
                        extra={"message_preview": str(message or "")[:120]},
                    )
                    self.db.update_customer_status(user_id, platform, 'failed_send_error')
                    return False

                self.db.update_customer_status(user_id, platform, 'sent')
                
                close_btn = "div[data-e2e='close-chat']"
                if self.page.is_visible(close_btn):
                    close_locator = self.page.locator(close_btn).first
                    if not self._human_helper().click_target(close_locator):
                        self.page.click(close_btn)
                    
                return True
                
            except Exception as e:
                logger.error(f"发送过程输入失败: {e}")
                self.db.update_customer_status(user_id, platform, 'failed_input')
                return False

        except Exception as e:
            logger.error(f"发送私信整体失败: {e}")
            if user:
                platform = user.get('platform', 'douyin')
            self.db.update_customer_status(user_id, platform, 'error')
            return False
        finally:
            self._clear_profile_button_marks()
            self._active_target_identity_context = {}

    def send_wechat_card(self, user_id: str, wechat_id: str):
        """
        发送微信卡片（模拟）
        """
        card_msg = f"你好，我对您的内容很感兴趣，可以加V交流吗？我的V: {wechat_id}"
        return self.send_private_message(user_id, card_msg)

    def process_pending_customers(self, message="Hello", max_count=10, stop_check=None, message_list=None):
        """
        批量处理待发送用户
        stop_check: function that returns True if we should stop
        message_list: list of strings, if provided, will pick one for each customer
        """
        limit_service = get_private_message_limit_service()
        pending_users = self.db.get_pending_customers(platform="douyin", limit=max_count)
        if not pending_users:
            logger.info("没有待发送的用户")
            return {
                "status": "completed",
                "requested_count": max(int(max_count or 0), 0),
                "processed_count": 0,
                "sent_count": 0,
                "remaining_count": 0,
            }

        logger.info(f"发现 {len(pending_users)} 个待发送用户")
        candidate_users = []
        for user in pending_users:
            sec_uid = user.get("sec_uid")
            if not sec_uid:
                continue
            if self.db.is_sent(sec_uid):
                logger.info(f"用户 {sec_uid} 已标记 sent，跳过重复发送")
                continue
            candidate_users.append(user)

        if not candidate_users:
            logger.info("待发送用户均已发送，任务直接完成")
            return {
                "status": "completed",
                "requested_count": max(int(max_count or 0), 0),
                "processed_count": 0,
                "sent_count": 0,
                "remaining_count": 0,
            }

        account_scope = self.resolve_current_account_scope(force_refresh=True)
        account_id = str(account_scope.get("account_id") or "").strip()
        if account_scope.get("resolved"):
            logger.info(f"当前发送账号已解析: account_id={account_id}, source={account_scope.get('source')}")
        else:
            logger.warning("当前发送账号未解析，发送限额将按未解析账号作用域处理")

        sent_count = 0
        processed_count = 0
        for index, user in enumerate(candidate_users):
            if stop_check and stop_check():
                logger.info("批量发送任务已被用户停止")
                return {
                    "status": "stopped",
                    "requested_count": max(int(max_count or 0), 0),
                    "processed_count": processed_count,
                    "sent_count": sent_count,
                    "remaining_count": max(len(candidate_users) - index, 0),
                }

            sec_uid = user.get('sec_uid')
            if not sec_uid:
                continue

            # 智能选择消息内容：如果有列表则随机选一条
            current_message = message
            if message_list and isinstance(message_list, list) and len(message_list) > 0:
                current_message = random.choice(message_list)
                logger.info(f"从多条私信中随机选择了内容: {current_message[:20]}...")

            decision = limit_service.acquire_send_permit(
                count=1,
                message=current_message,
                user_id=sec_uid,
                account_id=account_id,
                source="message_sender_batch",
            )
            if not decision.allowed:
                logger.warning(f"批量私信发送已停止: {decision.message}")
                return {
                    "status": "paused_limit",
                    "limit_reason": decision.status_code,
                    "detail": decision.message,
                    "requested_count": max(int(max_count or 0), 0),
                    "processed_count": processed_count,
                    "sent_count": sent_count,
                    "remaining_count": max(len(candidate_users) - index, 0),
                    "resume_not_before": str(decision.details.get("next_send_available_at") or ""),
                    "retry_after_seconds": int(decision.details.get("retry_after_seconds") or 0),
                    "limit_details": dict(decision.details or {}),
                }
            reservation_token = str(decision.details.get("reservation_token") or "")
            
            success = False
            try:
                success = self.send_private_message(sec_uid, current_message)
            except (Exception,) as send_exc:
                # 阶段A·F-6：发送过程若抛未捕获异常，先确保 reservation 被释放，
                # 再把异常记录到日志中——避免额度永久占用。
                logger.exception(f"私信发送未捕获异常 sec_uid={sec_uid}: {send_exc}")
                try:
                    limit_service.release_reservation(
                        reservation_token=reservation_token,
                        account_id=account_id,
                    )
                except Exception:
                    logger.exception("release_reservation 兜底失败")
                success = False
            else:
                # 成功路径：维持原有 finalize 行为
                try:
                    limit_service.finalize_send_attempt(
                        reservation_token=reservation_token,
                        success=True,
                        count=1,
                        message=current_message,
                        user_id=sec_uid,
                        account_id=account_id,
                        source="message_sender_batch",
                    )
                except Exception as finalize_exc:
                    # finalize 自身失败时也兜底释放，避免 reservation 长期滞留
                    logger.warning(f"finalize_send_attempt 失败: {finalize_exc}")
                    try:
                        limit_service.release_reservation(
                            reservation_token=reservation_token,
                            account_id=account_id,
                        )
                    except Exception:
                        pass
            # 失败分支：调用 finalize（让 reservation 转为释放+记 event，success=False
            # 在当前实现下 reservation 直接被清除不消耗额度）
            if not success and 'finalize_send_attempt' in dir(limit_service):
                try:
                    limit_service.finalize_send_attempt(
                        reservation_token=reservation_token,
                        success=False,
                        count=1,
                        message=current_message,
                        user_id=sec_uid,
                        account_id=account_id,
                        source="message_sender_batch",
                    )
                except Exception as finalize_exc:
                    logger.warning(f"失败分支 finalize_send_attempt 失败: {finalize_exc}")
                    try:
                        limit_service.release_reservation(
                            reservation_token=reservation_token,
                            account_id=account_id,
                        )
                    except Exception:
                        pass
            
            processed_count += 1
            if success:
                sent_count += 1
                logger.info("发送成功，等待 5-10 秒...")
                # 阶段C·C-2：用可中断 sleep 替代固定 sleep，停止信号 1s 内生效
                if interruptible_sleep(
                    total_seconds=random.uniform(5, 10),
                    stop_check=stop_check,
                ):
                    logger.info("等待期间收到停止信号，提前退出循环")
                    return {
                        "status": "stopped",
                        "requested_count": max(int(max_count or 0), 0),
                        "processed_count": processed_count,
                        "sent_count": sent_count,
                        "remaining_count": max(len(candidate_users) - index - 1, 0),
                    }
            else:
                logger.info("发送失败，等待 2-4 秒...")
                if interruptible_sleep(
                    total_seconds=random.uniform(2, 4),
                    stop_check=stop_check,
                ):
                    logger.info("失败等待期间收到停止信号，提前退出循环")
                    return {
                        "status": "stopped",
                        "requested_count": max(int(max_count or 0), 0),
                        "processed_count": processed_count,
                        "sent_count": sent_count,
                        "remaining_count": max(len(candidate_users) - index - 1, 0),
                    }

        return {
            "status": "completed",
            "requested_count": max(int(max_count or 0), 0),
            "processed_count": processed_count,
            "sent_count": sent_count,
            "remaining_count": 0,
        }
