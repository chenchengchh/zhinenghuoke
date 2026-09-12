"""入站判新决策引擎。

阶段2收口目标：
- 最终"是不是新消息"只由 InboundDecisionEngine 决定。
- RPA、MessageMonitor、APIInterceptor 只负责给证据。
- 新增 decide_inbound_event(raw_evidence_bundle) 统一入口。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class InboundEvidence:
    """单个证据源产出的原始证据。"""

    source: str = ""
    customer_name: str = ""
    content: str = ""
    direction: str = "inbound"
    conversation_id: str = ""
    customer_id: str = ""
    msg_id: str = ""
    has_unread: bool = False
    unread_count: int = 0
    message_time: str = ""
    active_snapshot: Optional[dict[str, Any]] = None
    is_active_conversation: bool = False
    state: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InboundDecision:
    """InboundDecisionEngine 的统一决策结果。"""

    action: str = "skip"
    customer_name: str = ""
    content: str = ""
    conversation_id: str = ""
    customer_id: str = ""
    msg_id: str = ""
    signal_source: str = ""
    evidence_sources: list[str] = field(default_factory=list)
    direction: str = "inbound"
    has_unread: bool = False
    unread_count: int = 0
    bubble_signature: str = ""


class InboundDecisionEngine:
    """收口 active_snapshot 相关的签名构建与 new/same/missing 判定。"""

    def __init__(
        self,
        *,
        normalize_customer_name: Optional[Callable[[str], str]] = None,
        normalize_preview_message_reference: Optional[Callable[[str], str]] = None,
        is_valid_message: Optional[Callable[[str], bool]] = None,
        resolve_inbound_message_for_conversation: Optional[
            Callable[..., tuple[Optional[str], str, str]]
        ] = None,
        normalize_processed_inbound_bubble_signatures: Optional[Callable[[Any], list[str]]] = None,
        get_inbound_round_anchor: Optional[Callable[..., str]] = None,
        build_inbound_bubble_signature: Optional[Callable[..., str]] = None,
        looks_like_preview_of_last_sent_message: Optional[Callable[[str, str], bool]] = None,
    ) -> None:
        self._normalize_customer_name = normalize_customer_name or (
            lambda value: str(value or "").strip().lower()
        )
        self._normalize_preview_message_reference = normalize_preview_message_reference or (
            lambda value: str(value or "").strip()
        )
        self._is_valid_message = is_valid_message or (
            lambda content: bool(str(content or "").strip())
        )
        self._resolve_inbound_message_for_conversation = (
            resolve_inbound_message_for_conversation
            or (lambda *args, **kwargs: (None, "", ""))
        )
        self._normalize_processed_inbound_bubble_signatures = (
            normalize_processed_inbound_bubble_signatures
            or (
                lambda values: [
                    str(value).strip()
                    for value in list(values or [])
                    if str(value).strip()
                ]
            )
        )
        self._get_inbound_round_anchor = get_inbound_round_anchor or (lambda *args, **kwargs: "")
        self._build_inbound_bubble_signature = build_inbound_bubble_signature or (
            lambda **kwargs: ""
        )
        self._looks_like_preview_of_last_sent_message = (
            looks_like_preview_of_last_sent_message or (lambda content, last_content: False)
        )

    # ==================== 统一决策入口 ====================

    # 证据源优先级（数值越小越先判定）。收敛前：list literal 内联在 decide_inbound_event 中。
    _EVIDENCE_PRIORITY: tuple[str, ...] = (
        "active_snapshot",
        "api_intercept",
        "network_api",
        "network_ws",
        "rpa_observer",
        "rpa_dom",
        "conversation_preview",
    )

    def decide_inbound_event(
        self,
        evidence_bundle: list[InboundEvidence],
        *,
        prev_state: Optional[dict[str, Any]] = None,
    ) -> InboundDecision:
        """统一决策入口：接收多源证据，产出唯一 InboundDecision。

        优先级（从高到低）：
        1. active_snapshot（最可靠）
        2. api_intercept / network_*
        3. rpa_* / conversation_preview
        4. 其余 -> skip
        """
        if not evidence_bundle:
            return InboundDecision(action="skip")

        for evidence in self._iter_evidence_by_priority(evidence_bundle):
            decision = self._decision_from_evidence(evidence, prev_state=prev_state)
            if decision is not None:
                return decision

        return InboundDecision(
            action="skip",
            evidence_sources=[e.source for e in evidence_bundle],
        )

    def _iter_evidence_by_priority(
        self,
        evidence_bundle: list[InboundEvidence],
    ):
        """按优先级顺序产出 inbound 证据。"""
        priority = self._EVIDENCE_PRIORITY
        unknown_priority = len(priority)
        for evidence in sorted(
            evidence_bundle,
            key=lambda e: priority.index(e.source) if e.source in priority else unknown_priority,
        ):
            if evidence.direction == "inbound":
                yield evidence

    def _decision_from_evidence(
        self,
        evidence: InboundEvidence,
        *,
        prev_state: Optional[dict[str, Any]] = None,
    ) -> Optional[InboundDecision]:
        """对单条证据产出决策，None 表示"证据不足以决策，继续下一条"。

        收敛前：emit/skip 两段 InboundDecision(...) 构造内联在 for 循环里。
        收敛后：单方法 + 信号分类后委托。
        """
        classification = self.classify_new_same_missing(
            evidence=evidence,
            prev_state=prev_state,
        )
        if classification == "new":
            return InboundDecision(
                action="emit",
                customer_name=evidence.customer_name,
                content=evidence.content,
                conversation_id=evidence.conversation_id,
                customer_id=evidence.customer_id,
                msg_id=evidence.msg_id,
                signal_source=evidence.source,
                evidence_sources=[evidence.source],
                direction="inbound",
                has_unread=evidence.has_unread,
                unread_count=evidence.unread_count,
                bubble_signature=evidence.extra.get("bubble_signature", ""),
            )
        if classification == "same":
            return InboundDecision(
                action="skip",
                customer_name=evidence.customer_name,
                content=evidence.content,
                signal_source=evidence.source,
                evidence_sources=[evidence.source],
            )
        return None  # "missing" -> 继续下一条证据

    def classify_new_same_missing(
        self,
        evidence: InboundEvidence,
        *,
        prev_state: Optional[dict[str, Any]] = None,
    ) -> str:
        """对单条证据做 new/same/missing 分类。"""
        content = str(evidence.content or "").strip()
        if not content or not self._is_valid_message(content):
            return "missing"

        if evidence.active_snapshot is not None:
            return self.classify_resolved_inbound_content(
                evidence.customer_name,
                last_reference=str((prev_state or {}).get("content", "") or ""),
                is_active=evidence.is_active_conversation,
                active_snapshot=evidence.active_snapshot,
                state=evidence.state or prev_state,
            )[0]

        return self.classify_source_inbound_state(
            prev_state=prev_state,
            inbound_signature=self.build_inbound_identity_signature(
                customer_name=evidence.customer_name,
                conversation_id=evidence.conversation_id,
                content=content,
                msg_id=evidence.msg_id,
            ),
            current_signature=self._build_message_signature(
                customer_name=evidence.customer_name,
                conversation_id=evidence.conversation_id,
                content=content,
                direction=evidence.direction,
                message_time=evidence.message_time,
                msg_id=evidence.msg_id,
            ),
            has_unread=evidence.has_unread,
            current_message_time=evidence.message_time,
            current_content=content,
        )

    def should_emit(self, decision: InboundDecision) -> bool:
        """判断决策结果是否应该发射。"""
        return decision.action == "emit"

    def build_inbound_identity_signature(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        content: str,
        msg_id: str = "",
    ) -> str:
        """为入站去重构建稳定签名。"""
        return self._build_message_signature(
            customer_name=customer_name,
            conversation_id=conversation_id,
            content=content,
            direction="inbound",
            message_time="",
            msg_id=msg_id,
        )

    # ==================== 已有方法 ====================

    def build_active_snapshot_signature(
        self,
        active_snapshot: Optional[dict[str, Any]],
        *,
        customer_name: str = "",
    ) -> str:
        if not isinstance(active_snapshot, dict):
            return ""

        snapshot_name = self._normalize_customer_name(active_snapshot.get("customer_name", ""))
        normalized_customer_name = self._normalize_customer_name(customer_name)
        if normalized_customer_name and snapshot_name != normalized_customer_name:
            return ""

        last_bubble_text = self._normalize_preview_message_reference(
            active_snapshot.get("last_bubble_text", "")
        )
        last_inbound_message = self._normalize_preview_message_reference(
            active_snapshot.get("last_inbound_message", "")
        )
        bubble_count = int(active_snapshot.get("bubble_count", 0) or 0)
        inbound_bubble_count = int(active_snapshot.get("inbound_bubble_count", 0) or 0)
        last_inbound_index = int(active_snapshot.get("last_inbound_index", -1) or -1)
        last_bubble_is_inbound = bool(active_snapshot.get("last_bubble_is_inbound", False))

        if (
            not last_bubble_text
            and not last_inbound_message
            and bubble_count <= 0
            and inbound_bubble_count <= 0
        ):
            return ""

        parts = [
            snapshot_name,
            "1" if last_bubble_is_inbound else "0",
            str(bubble_count),
            str(inbound_bubble_count),
            str(last_inbound_index),
            last_bubble_text,
            last_inbound_message,
        ]
        return "|".join(parts)

    def build_active_snapshot_inbound_signature(
        self,
        *,
        customer_name: str,
        active_snapshot: Optional[dict[str, Any]],
        state: Optional[dict[str, Any]],
        position_hint: str,
        text: str,
    ) -> str:
        round_anchor = self._get_inbound_round_anchor(
            state,
            fallback_anchor=f"snapshot:{self._normalize_customer_name(customer_name)}",
        )
        snapshot_signature = self.build_active_snapshot_signature(
            active_snapshot,
            customer_name=customer_name,
        )
        if snapshot_signature:
            snapshot_digest = hashlib.md5(snapshot_signature.encode("utf-8")).hexdigest()[:12]
            round_anchor = f"{round_anchor}|snap:{snapshot_digest}"
        return self._build_inbound_bubble_signature(
            customer_name=customer_name,
            round_anchor=round_anchor,
            position_hint=position_hint,
            text=text,
        )

    def classify_resolved_inbound_content(
        self,
        customer_name: str,
        *,
        last_reference: str,
        is_active: bool = False,
        active_snapshot: Optional[dict[str, Any]] = None,
        state: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Optional[str], str, str]:
        actual_content, signal_source, bubble_signature = (
            self._resolve_inbound_message_for_conversation(
                customer_name,
                is_active=is_active,
                active_snapshot=active_snapshot,
                state=state,
                last_reference=last_reference,
            )
        )
        # [REFACTOR-INST:convergence] 统一埋点
        from src.common.debug_instrument import debug_event
        debug_event(
            "DBG-CLASSIFY", "decision",
            customer=customer_name,
            signal=signal_source,
            actual=actual_content,
            last_ref=last_reference,
            bubble_sig=bubble_signature,
            is_active=is_active,
        )
        if state is not None:
            debug_event(
                "DBG-CLASSIFY", "state",
                customer=customer_name,
                user_last_content=state.get("user_last_content"),
                last_content=state.get("last_content"),
                sent_by_us=state.get("sent_by_us"),
                last_detected_unread=state.get("last_detected_unread"),
                processed_sigs_count=len(state.get("processed_inbound_bubble_signatures") or []),
            )
        if active_snapshot is not None:
            debug_event(
                "DBG-CLASSIFY", "active_snapshot",
                customer=customer_name,
                last_bubble_text=active_snapshot.get("last_bubble_text"),
                last_inbound_message=active_snapshot.get("last_inbound_message"),
                bubble_count=active_snapshot.get("bubble_count"),
                inbound_bubble_count=active_snapshot.get("inbound_bubble_count"),
                last_inbound_index=active_snapshot.get("last_inbound_index"),
            )
        if actual_content and self._is_valid_message(actual_content):
            normalized_actual = self._normalize_preview_message_reference(actual_content)
            normalized_reference = self._normalize_preview_message_reference(last_reference)
            if normalized_actual != normalized_reference:
                return "new", actual_content, signal_source, bubble_signature
            # [REFACTOR-INST:convergence] 统一埋点
            from src.common.debug_instrument import debug_event
            try:
                _processed = set(
                    self._normalize_processed_inbound_bubble_signatures(
                        (state or {}).get("processed_inbound_bubble_signatures", [])
                    )
                )
                _norm_bubble_sig = str(bubble_signature or "").strip()
                _in_processed = _norm_bubble_sig in _processed if _norm_bubble_sig else None
                # snapshot 签名比较
                _snap_sig_now = self.build_active_snapshot_signature(active_snapshot, customer_name=customer_name)
                _snap_sig_last = str((state or {}).get("active_snapshot_signature", "") or "").strip()
                _round_anchor = self._get_inbound_round_anchor(
                    state,
                    fallback_anchor=f"snapshot:{self._normalize_customer_name(customer_name)}",
                )
                debug_event(
                    "DBG-SAME", "content_equal",
                    customer=customer_name,
                    bubble_sig=_norm_bubble_sig,
                    in_processed=_in_processed,
                    snap_now=_snap_sig_now,
                    snap_last=_snap_sig_last,
                    snap_advanced=(_snap_sig_now != _snap_sig_last),
                    round_anchor=_round_anchor,
                    sent_by_us=(state or {}).get("sent_by_us"),
                )
            except Exception as _same_e:
                debug_event("DBG-SAME", "exception", err=str(_same_e))
            if self._signal_source_supports_signature_dedup(signal_source):
                normalized_bubble_signature = str(bubble_signature or "").strip()
                processed_signatures = set(
                    self._normalize_processed_inbound_bubble_signatures(
                        (state or {}).get("processed_inbound_bubble_signatures", [])
                    )
                )
                if (
                    normalized_bubble_signature
                    and normalized_bubble_signature not in processed_signatures
                ):
                    return "new", actual_content, signal_source, bubble_signature
            return "same", actual_content, signal_source, bubble_signature
        return "missing", None, signal_source, bubble_signature

    @staticmethod
    def _signal_source_supports_signature_dedup(signal_source: str) -> bool:
        normalized = str(signal_source or "").strip().lower()
        return (
            normalized.startswith("active_snapshot")
            or normalized.startswith("api_intercept")
            or normalized.startswith("network_")
        )

    def classify_source_inbound_state(
        self,
        *,
        prev_state: Optional[dict[str, Any]],
        inbound_signature: str,
        current_signature: str,
        has_unread: bool,
        current_message_time: Any = "",
        current_content: str = "",
    ) -> str:
        """对无 active_snapshot 证据的入站做 new/same/missing 分类。

        收敛前：5 个嵌套 if 分支。
        收敛后：单层 if 序列，每条规则一目了然。
        """
        if current_content and not self._is_valid_message(current_content):
            return "missing"

        state = prev_state or {}
        last_inbound_signature = str(state.get("last_inbound_signature", "") or "")
        last_signature = str(state.get("last_signature", "") or "")

        # 规则 1：首次入站（无任何历史签名） -> new
        if not last_inbound_signature and not last_signature:
            return "new"

        # 规则 2：last_inbound_signature 缺失时（无明确入站历史），靠 inbound_signature vs last_signature 判定
        if not last_inbound_signature:
            return "new" if inbound_signature and inbound_signature != last_signature else "same"

        # 规则 3：入站签名变了 -> new
        if inbound_signature != last_inbound_signature:
            return "new"

        # 规则 4：消息签名变了，但仅在有未读时算 new
        if current_signature and last_signature and current_signature != last_signature:
            return "new" if has_unread else "same"

        return "same"

    def evaluate_preview_fallback_decision(
        self,
        *,
        mode: str,
        is_active_conversation: bool,
        content: str,
        unread_count: int,
        last_detected_unread: int,
        preview_signature: str,
        last_preview_signature: str,
        last_content: str,
        last_user_content: str,
        active_snapshot_matches: bool = False,
        active_snapshot_name: str = "",
        active_snapshot_last_bubble_inbound: bool = False,
        active_snapshot_tail_inbound_count: int = 0,
        was_sent_by_us: bool = False,
    ) -> Optional[dict[str, Any]]:
        if not content or not self._is_valid_message(content):
            return None
        if not preview_signature or preview_signature == last_preview_signature:
            return None

        normalized_preview = self._normalize_preview_message_reference(content)
        normalized_last_content = self._normalize_preview_message_reference(last_content)
        normalized_last_user = self._normalize_preview_message_reference(last_user_content)

        if not normalized_preview:
            return None
        if (
            was_sent_by_us
            and normalized_last_content
            and self._looks_like_preview_of_last_sent_message(content, last_content)
        ):
            return None
        if normalized_last_content and normalized_preview == normalized_last_content:
            return None

        if mode == "active":
            if not is_active_conversation:
                return None
            normalized_snapshot_name = self._normalize_customer_name(active_snapshot_name)
            if normalized_snapshot_name and not active_snapshot_matches:
                return None
            if was_sent_by_us and normalized_last_user and normalized_preview == normalized_last_user:
                return None

            allow_capped_unread_preview_change = bool(
                unread_count == last_detected_unread
                and normalized_last_content
                and normalized_preview != normalized_last_content
            )
            if not (unread_count > last_detected_unread or allow_capped_unread_preview_change):
                return None
            if active_snapshot_last_bubble_inbound or active_snapshot_tail_inbound_count > 0:
                return None
            return self._build_fallback_pass_dict(content, unread_count, "active_preview_fallback")

        if mode == "non_active":
            if is_active_conversation:
                return None
            if unread_count <= 0 or unread_count != last_detected_unread:
                return None
            if normalized_last_user and normalized_preview == normalized_last_user:
                return None
            return self._build_fallback_pass_dict(content, unread_count, "non_active_preview_fallback")

        return None

    @staticmethod
    def _build_fallback_pass_dict(
        content: str,
        unread_count: int,
        signal_source: str,
    ) -> dict[str, Any]:
        """统一构造 preview_fallback 的 pass 决策 dict。
        
        收敛前：active/non_active 两个分支重复构造相同结构的 5 字段 dict。
        收敛后：单方法。
        """
        return {
            "action": "pass",
            "content": str(content or ""),
            "unread_count": max(0, int(unread_count or 0)),
            "evidence": "fallback",
            "signal_source": signal_source,
        }

    @staticmethod
    def normalize_text_digest(text: str) -> str:
        normalized = re.sub(r"\s+", "", str(text or "")).strip()
        if not normalized:
            return ""
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def is_relative_message_time_label(message_time: Any) -> bool:
        text = str(message_time or "").strip()
        if not text:
            return False
        return bool(
            text == "刚刚"
            or text == "昨天"
            or re.match(r"^\d+分钟前$", text)
            or re.match(r"^\d+小时前$", text)
            or re.match(r"^\d+天前$", text)
        )

    # ==================== 内部辅助 ====================

    @staticmethod
    def _build_message_signature(
        *,
        customer_name: str,
        conversation_id: str,
        content: str,
        direction: str,
        message_time: str = "",
        msg_id: str = "",
    ) -> str:
        if msg_id:
            return f"mid:{str(msg_id).strip()}"
        normalized_message_time = InboundDecisionEngine._normalize_message_time_for_signature(
            message_time
        )
        normalized = "|".join(
            [
                " ".join(str(conversation_id or "").strip().split()),
                " ".join(str(customer_name or "").strip().split()),
                " ".join(str(direction or "").strip().split()),
                " ".join(str(content or "").strip().split()),
                normalized_message_time,
            ]
        )
        return f"sig:{hashlib.md5(normalized.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _normalize_message_time_for_signature(message_time: Any) -> str:
        text = " ".join(str(message_time or "").strip().split())
        if not text or InboundDecisionEngine.is_relative_message_time_label(text):
            return ""
        try:
            numeric_value = float(text)
        except (TypeError, ValueError):
            numeric_value = None
        if numeric_value is not None:
            if numeric_value > 1e12:
                numeric_value /= 1000.0
            if numeric_value > 0:
                return str(int(numeric_value))
            return ""
        try:
            return str(int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()))
        except ValueError:
            return text
