from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

from src.douyin_bot.smart_element_finder import SmartElementFinder


@dataclass(frozen=True)
class InputLocatorContext:
    scene: str
    expected_identities: List[str] = field(default_factory=list)
    activation: Dict[str, Any] = field(default_factory=dict)
    preferred_container_selector: str = ""
    allow_document_fallback: bool = False


@dataclass(frozen=True)
class LocatorCandidate:
    role: str
    selector: str
    score: float
    visible: bool
    enabled: bool
    locator: Any = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputLocatorResult:
    success: bool
    scene: str
    input_candidate: Optional[LocatorCandidate]
    send_candidate: Optional[LocatorCandidate]
    container_candidate: Optional[LocatorCandidate]
    fallback_stage: str
    rejected_candidates: List[Dict[str, Any]] = field(default_factory=list)


class InputLocatorService:
    MIN_PRIVATE_INPUT_SCORE = 60
    MIN_COMMENT_INPUT_SCORE = 50

    def locate(self, page: Any, context: InputLocatorContext) -> InputLocatorResult:
        if str(context.scene or "").strip().lower() == "comment_reply":
            return self.locate_comment_reply_input(page, context)
        return self.locate_private_message_input(page, context)

    def locate_private_message_input(self, page: Any, context: InputLocatorContext) -> InputLocatorResult:
        # 选择器按精确度排序：精确选择器在前，宽泛选择器在后（已删除最宽泛的3个选择器）
        selectors = [
            '[data-e2e="msg-input"] [contenteditable="true"]',
            '[data-e2e="msg-input"] [role="textbox"]',
            '[data-e2e="msg-input"] textarea',
            '[data-e2e="msg-input"] div[class*="public-DraftEditor-content"]',
            '[class*="chat-input"] [contenteditable="true"]',
            '[class*="chat-input"] [role="textbox"]',
            '[class*="chat-input"] textarea',
            'textarea[data-e2e="chat-input"]',
            "textarea.chat-input",
            ".chat-input-container textarea",
            # 保留有限宽泛的选择器，但通过评分排除搜索框
            '[contenteditable="true"][role="textbox"]',
            'div[class*="public-DraftEditor-content"]',
        ]
        best: Optional[LocatorCandidate] = None
        rejected: List[Dict[str, Any]] = []
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 8)
                for index in range(count):
                    candidate = locator.nth(index)
                    if not candidate.is_visible(timeout=800):
                        continue
                    meta = candidate.evaluate(
                        """el => {
                            const rect = el.getBoundingClientRect();
                            const closest = (selector) => {
                                try {
                                    return el.closest(selector);
                                } catch (e) {
                                    return null;
                                }
                            };
                            const nearestDataE2eNode = closest('[data-e2e]');
                            const nearestRoleNode = closest('[role]');
                            const msgInputContainer = closest('[data-e2e="msg-input"]');
                            const chatInputContainer = closest('[class*="chat-input"], [class*="chatInput"], [class*="composer"]');
                            const headerContainer = closest('header, [class*="header"], [class*="Header"], [class*="toolbar"], [class*="topbar"], [class*="nav"]');
                            const searchContainer = closest('[role="search"], [class*="search"], [class*="Search"], [data-e2e*="search"], [aria-label*="搜索"]');
                            const commentContainer = closest('[data-e2e="comment-input"], [class*="comment"], [class*="reply-input"]');
                            const dialogContainer = closest('[role="dialog"], [class*="modal"], [class*="drawer"], [class*="Dialog"], [class*="Drawer"]');
                            const formContainer = closest('form');
                            const placeholder = (
                                el.getAttribute('data-placeholder') ||
                                el.getAttribute('placeholder') ||
                                el.getAttribute('aria-placeholder') ||
                                ''
                            ).toLowerCase();
                            const className = typeof el.className === 'string' ? el.className.toLowerCase() : '';
                            return {
                                tag: (el.tagName || '').toLowerCase(),
                                role: (el.getAttribute('role') || '').toLowerCase(),
                                dataE2e: (el.getAttribute('data-e2e') || '').toLowerCase(),
                                parentDataE2e: ((el.parentElement && el.parentElement.getAttribute && el.parentElement.getAttribute('data-e2e')) || '').toLowerCase(),
                                grandParentDataE2e: ((el.parentElement && el.parentElement.parentElement && el.parentElement.parentElement.getAttribute && el.parentElement.parentElement.getAttribute('data-e2e')) || '').toLowerCase(),
                                nearestDataE2e: ((nearestDataE2eNode && nearestDataE2eNode.getAttribute && nearestDataE2eNode.getAttribute('data-e2e')) || '').toLowerCase(),
                                nearestRole: ((nearestRoleNode && nearestRoleNode.getAttribute && nearestRoleNode.getAttribute('role')) || '').toLowerCase(),
                                placeholder,
                                className,
                                text: String(el.innerText || el.textContent || '').trim().slice(0, 80),
                                editable: !!el.isContentEditable,
                                hasEditableChild: !!el.querySelector('textarea, [contenteditable="true"], [contenteditable], [role="textbox"]'),
                                insideMsgInputContainer: !!msgInputContainer,
                                insideChatInputContainer: !!chatInputContainer,
                                insideHeaderContainer: !!headerContainer,
                                insideSearchContainer: !!searchContainer,
                                insideCommentContainer: !!commentContainer,
                                insideDialogContainer: !!dialogContainer,
                                insideFormContainer: !!formContainer,
                                top: rect.top || 0,
                                bottom: rect.bottom || 0,
                                left: rect.left || 0,
                                right: rect.right || 0,
                                width: rect.width || 0,
                                height: rect.height || 0,
                                viewportWidth: window.innerWidth || 0,
                                viewportHeight: window.innerHeight || 0,
                                ariaLabel: (el.getAttribute('aria-label') || '').toLowerCase(),
                            };
                        }"""
                    )
                    # 硬性排除搜索框：不参与评分
                    _placeholder = str(meta.get("placeholder", "") or "")
                    _class_name = str(meta.get("className", "") or "")
                    _aria_label = str(meta.get("ariaLabel", "") or "")
                    _inside_search = bool(meta.get("insideSearchContainer"))
                    if _inside_search:
                        rejected.append({"selector": selector, "score": -999, "meta": meta, "reason": "search_container"})
                        continue
                    if any(kw in _placeholder for kw in ("搜索", "search", "查找")):
                        rejected.append({"selector": selector, "score": -999, "meta": meta, "reason": "search_placeholder"})
                        continue
                    if any(kw in _aria_label for kw in ("搜索", "search", "查找")):
                        rejected.append({"selector": selector, "score": -999, "meta": meta, "reason": "search_aria_label"})
                        continue
                    if any(kw in _class_name for kw in ("searchbox", "search-input", "search_input")):
                        rejected.append({"selector": selector, "score": -999, "meta": meta, "reason": "search_class"})
                        continue
                    score = self.score_candidate(meta, context)
                    rejected.append({"selector": selector, "score": score, "meta": meta})
                    if best is None or score > best.score:
                        best = LocatorCandidate(
                            role="input",
                            selector=selector,
                            score=score,
                            visible=True,
                            enabled=True,
                            locator=candidate,
                            meta=meta,
                        )
            except Exception:
                continue

        if best is not None and best.score >= self.MIN_PRIVATE_INPUT_SCORE:
            return InputLocatorResult(
                success=True,
                scene=context.scene,
                input_candidate=best,
                send_candidate=None,
                container_candidate=None,
                fallback_stage="scored_selector",
                rejected_candidates=sorted(rejected, key=lambda item: item.get("score", 0), reverse=True)[:10],
            )

        finder = SmartElementFinder(page)
        fallback = None
        try:
            fallback = finder.find_input_box(timeout=1.5)
        except Exception:
            fallback = None
        if fallback is not None:
            return InputLocatorResult(
                success=True,
                scene=context.scene,
                input_candidate=LocatorCandidate(
                    role="input",
                    selector="smart_element_finder",
                    score=0,
                    visible=True,
                    enabled=True,
                    locator=fallback,
                    meta={},
                ),
                send_candidate=None,
                container_candidate=None,
                fallback_stage="smart_element_finder",
                rejected_candidates=sorted(rejected, key=lambda item: item.get("score", 0), reverse=True)[:10],
            )
        return InputLocatorResult(
            success=False,
            scene=context.scene,
            input_candidate=None,
            send_candidate=None,
            container_candidate=None,
            fallback_stage="not_found",
            rejected_candidates=sorted(rejected, key=lambda item: item.get("score", 0), reverse=True)[:10],
        )

    def locate_comment_reply_input(self, page: Any, context: InputLocatorContext) -> InputLocatorResult:
        activation = context.activation if isinstance(context.activation, dict) else {}
        activation_container_uid = str((activation or {}).get("containerUid", "") or "").strip()
        activation_editor_uid = str((activation or {}).get("editorUid", "") or "").strip()
        activation_send_uid = str((activation or {}).get("sendUid", "") or "").strip()
        container_selectors = []
        if activation_container_uid:
            container_selectors.append(f'[data-trae-reply-active-container="{activation_container_uid}"]')
        if context.preferred_container_selector:
            container_selectors.append(context.preferred_container_selector)
        container_selectors.extend([
            "#comment-input-container",
            '[data-e2e="comment-input"]',
            '[class*="comment-input"]',
        ])
        editor_selectors = []
        if activation_editor_uid:
            editor_selectors.append(f'[data-trae-reply-active-editor="{activation_editor_uid}"]')
        editor_selectors.extend([
            ".DraftEditor-editorContainer .public-DraftEditor-content[contenteditable='true']",
            ".public-DraftEditor-content[contenteditable='true']",
            "[contenteditable='true'][role='combobox']",
            "[contenteditable='true']",
            "textarea",
            "[role='textbox']",
        ])
        send_selectors = []
        if activation_send_uid:
            send_selectors.append(f'[data-trae-reply-active-send="{activation_send_uid}"]')
        send_selectors.extend([
            '[data-trae-comment-send]',
            'button:has-text("发送")',
            '[role="button"]:has-text("发送")',
            'button:has-text("发布")',
            '[role="button"]:has-text("发布")',
        ])
        for container_selector in self._dedupe(container_selectors):
            try:
                container = page.locator(container_selector).first
                if not container.is_visible(timeout=1000):
                    continue
                for editor_selector in self._dedupe(editor_selectors):
                    locator = container.locator(editor_selector).first
                    try:
                        if locator.count() <= 0 or not locator.is_visible(timeout=600):
                            continue
                    except Exception:
                        continue
                    send_candidate = None
                    for send_selector in self._dedupe(send_selectors):
                        try:
                            send_locator = container.locator(send_selector).first
                            if send_locator.count() > 0 and send_locator.is_visible(timeout=400):
                                send_candidate = LocatorCandidate(
                                    role="send_button",
                                    selector=send_selector,
                                    score=80.0,
                                    visible=True,
                                    enabled=True,
                                    locator=send_locator,
                                    meta={},
                                )
                                break
                        except Exception:
                            continue
                    return InputLocatorResult(
                        success=True,
                        scene=context.scene,
                        input_candidate=LocatorCandidate(
                            role="input",
                            selector=editor_selector,
                            score=90.0,
                            visible=True,
                            enabled=True,
                            locator=locator,
                            meta={},
                        ),
                        send_candidate=send_candidate,
                        container_candidate=LocatorCandidate(
                            role="container",
                            selector=container_selector,
                            score=95.0,
                            visible=True,
                            enabled=True,
                            locator=container,
                            meta={},
                        ),
                        fallback_stage="scoped_comment_container",
                        rejected_candidates=[],
                    )
            except Exception:
                continue

        if context.allow_document_fallback:
            logger.warning("评论输入框定位已退回 document fallback，这只建议用于诊断模式")
            try:
                locator = page.locator("[contenteditable='true'], textarea, [role='textbox']").first
                if locator.is_visible(timeout=500):
                    return InputLocatorResult(
                        success=True,
                        scene=context.scene,
                        input_candidate=LocatorCandidate(
                            role="input",
                            selector="document_fallback",
                            score=10.0,
                            visible=True,
                            enabled=True,
                            locator=locator,
                            meta={},
                        ),
                        send_candidate=None,
                        container_candidate=None,
                        fallback_stage="document_fallback",
                        rejected_candidates=[],
                    )
            except Exception:
                pass
        return InputLocatorResult(
            success=False,
            scene=context.scene,
            input_candidate=None,
            send_candidate=None,
            container_candidate=None,
            fallback_stage="not_found",
            rejected_candidates=[],
        )

    def score_candidate(self, candidate_meta: Dict[str, Any], context: InputLocatorContext) -> float:
        score = 0.0
        placeholder = str(candidate_meta.get("placeholder", "") or "")
        class_name = str(candidate_meta.get("className", "") or "")
        data_e2e = str(candidate_meta.get("dataE2e", "") or "")
        parent_data_e2e = str(candidate_meta.get("parentDataE2e", "") or "")
        grand_parent_data_e2e = str(candidate_meta.get("grandParentDataE2e", "") or "")
        nearest_data_e2e = str(candidate_meta.get("nearestDataE2e", "") or "")
        nearest_role = str(candidate_meta.get("nearestRole", "") or "")
        top = float(candidate_meta.get("top", 0) or 0)
        bottom = float(candidate_meta.get("bottom", 0) or 0)
        left = float(candidate_meta.get("left", 0) or 0)
        width = float(candidate_meta.get("width", 0) or 0)
        height = float(candidate_meta.get("height", 0) or 0)
        viewport_height = float(candidate_meta.get("viewportHeight", 0) or 0)
        viewport_width = float(candidate_meta.get("viewportWidth", 0) or 0)
        aria_label = str(candidate_meta.get("ariaLabel", "") or "")
        inside_msg_input_container = bool(candidate_meta.get("insideMsgInputContainer"))
        inside_chat_input_container = bool(candidate_meta.get("insideChatInputContainer"))
        inside_header_container = bool(candidate_meta.get("insideHeaderContainer"))
        inside_search_container = bool(candidate_meta.get("insideSearchContainer"))
        inside_comment_container = bool(candidate_meta.get("insideCommentContainer"))
        inside_dialog_container = bool(candidate_meta.get("insideDialogContainer"))
        inside_form_container = bool(candidate_meta.get("insideFormContainer"))

        if "msg-input" in {
            data_e2e,
            parent_data_e2e,
            grand_parent_data_e2e,
            nearest_data_e2e,
        } or inside_msg_input_container:
            score += 180
        if inside_chat_input_container:
            score += 55
        if any(token in placeholder for token in ("发送", "消息", "回复", "输入")):
            score += 60
        if any(token in placeholder for token in ("搜索", "search")):
            score -= 120
        if any(token in aria_label for token in ("搜索", "search", "查找")):
            score -= 120
        if any(token in placeholder for token in ("评论", "写下你的评论", "reply")):
            score -= 95
        if str(candidate_meta.get("role", "") or "") == "textbox":
            score += 30
        if nearest_role == "dialog" and inside_dialog_container:
            score += 8
        if candidate_meta.get("editable") or str(candidate_meta.get("tag", "") or "") == "textarea":
            score += 20
        if candidate_meta.get("hasEditableChild"):
            score -= 70
        else:
            score += 35
        if any(token in class_name for token in ("editor", "input", "chat", "message", "textbox", "composer")):
            score += 15
        if any(token in class_name for token in ("messageeditor", "editor-kit", "inputarea")):
            score += 40
        if any(token in class_name for token in ("search", "toolbar", "header", "topbar", "nav")):
            score -= 80
        if any(token in class_name for token in ("comment", "reply-input")):
            score -= 85
        if inside_search_container:
            score -= 140
        if inside_header_container:
            score -= 110
        if inside_comment_container:
            score -= 120
        if inside_form_container and not inside_msg_input_container and not inside_chat_input_container:
            score -= 35
        if viewport_height > 0:
            if bottom >= viewport_height * 0.68:
                score += 28
            elif top <= viewport_height * 0.35:
                score -= 70
            elif bottom <= viewport_height * 0.55:
                score -= 25
        # 新增：水平位置评分 - 消息输入框在右侧，搜索框在左侧
        if viewport_width > 0:
            if left >= viewport_width * 0.45:
                score += 35  # 右侧元素加分
            elif left < viewport_width * 0.25:
                score -= 50  # 左侧元素减分（搜索框通常在左侧）
        if width > 250:
            score += 8
        if width < 260:
            score -= 40
        if height >= 32:
            score += 6
        if height < 24:
            score -= 30
        return score

    @staticmethod
    def _dedupe(values: List[str]) -> List[str]:
        ordered: List[str] = []
        seen = set()
        for value in values:
            key = str(value or "").strip()
            if key and key not in seen:
                ordered.append(key)
                seen.add(key)
        return ordered


_input_locator_service: Optional[InputLocatorService] = None


def get_input_locator_service() -> InputLocatorService:
    global _input_locator_service
    if _input_locator_service is None:
        _input_locator_service = InputLocatorService()
    return _input_locator_service
