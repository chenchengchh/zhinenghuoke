from __future__ import annotations

import logging
import warnings
from dataclasses import asdict
from typing import Any, Dict

from src.common.types.reply_eligibility import (
    EligibilityAction,
    EvidenceLevel,
    HandoffLevel,
    ReplyEligibilityDecision,
    ReplyEligibilityInput,
    ReplyEligibilityPolicy,
)

logger = logging.getLogger(__name__)


class ReplyEligibilityService:
    """统一回复资格判定中心。

    .. deprecated::
        请使用 :class:`src.common.reply_management_service.ReplyManagementService` 替代。
    """

    def __init__(self):
        warnings.warn(
            "ReplyEligibilityService 已弃用，请使用 ReplyManagementService",
            DeprecationWarning,
            stacklevel=2,
        )

    def merge_policy(
        self,
        schema_policy: Dict[str, Any] | None = None,
        runtime_flags: Dict[str, Any] | None = None,
    ) -> ReplyEligibilityPolicy:
        payload: Dict[str, Any] = {}
        if isinstance(schema_policy, dict):
            payload.update(schema_policy)
        if isinstance(runtime_flags, dict):
            payload.update({k: v for k, v in runtime_flags.items() if v is not None})
        base = asdict(ReplyEligibilityPolicy())
        base.update({k: v for k, v in payload.items() if k in base})
        return ReplyEligibilityPolicy(**base)

    def classify_evidence_level(self, smart_result: Dict[str, Any]) -> str:
        matched_knowledge = str((smart_result or {}).get("matched_knowledge", "") or "").strip()
        source = str((smart_result or {}).get("source", "") or "").strip().lower()
        confidence = self._to_float((smart_result or {}).get("intent_score"), default=0.0)
        if matched_knowledge and matched_knowledge != "LLM生成":
            return EvidenceLevel.GROUNDED.value
        if source in {"knowledge_base", "knowledge_fallback", "grounded", "enhanced_base"}:
            return EvidenceLevel.GROUNDED.value
        if source in {"llm生成", "llm", "llm_only", "llm_only_fallback", "llm_only_fail_soft"}:
            return EvidenceLevel.UNGROUNDED.value
        if confidence >= 0.5:
            return EvidenceLevel.WEAK_GROUNDED.value
        return EvidenceLevel.UNGROUNDED.value

    def normalize_reason_code(self, smart_result: Dict[str, Any], fallback: str = "") -> str:
        metadata = (smart_result or {}).get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        return str(
            (smart_result or {}).get("reason_code")
            or metadata.get("reason_code")
            or fallback
            or ""
        ).strip()

    def evaluate(self, payload: ReplyEligibilityInput) -> ReplyEligibilityDecision:
        smart_result = payload.smart_result or {}
        metadata = smart_result.get("metadata") if isinstance(smart_result.get("metadata"), dict) else {}
        policy = self.merge_policy(payload.schema_policy, payload.runtime_flags)
        reply_content = payload.cached_reply_content or smart_result.get("reply") or ""
        if not isinstance(reply_content, str):
            reply_content = str(reply_content or "")
        reply_content = reply_content.strip()
        reason_code = self.normalize_reason_code(smart_result)
        reply_disposition = str(
            smart_result.get("reply_disposition")
            or metadata.get("reply_disposition")
            or ""
        ).strip().lower()
        retrieval_final_action = str(
            smart_result.get("retrieval_final_action")
            or metadata.get("retrieval_final_action")
            or ((smart_result.get("reply_analysis") or {}).get("retrieval") or {}).get("final_action", "")
            or ""
        ).strip().lower()
        fallback_generation_empty = bool(
            smart_result.get("fallback_generation_empty")
            or metadata.get("fallback_generation_empty")
            or metadata.get("empty_answer")
        )
        fallback_source = str(
            ((smart_result.get("reply_analysis") or {}).get("fallback") or {}).get("source", "")
            or ""
        ).strip().lower()
        need_human = bool(smart_result.get("need_human", False))
        confidence = self._to_float(
            smart_result.get("intent_score"),
            default=self._to_float((smart_result.get("reply_analysis") or {}).get("confidence"), default=0.0),
        )
        risk_level = str(
            smart_result.get("risk_level")
            or ((smart_result.get("reply_analysis") or {}).get("risk") or {}).get("level")
            or ""
        ).strip().lower()
        evidence_level = self.classify_evidence_level(smart_result)
        runtime_flags = payload.runtime_flags if isinstance(payload.runtime_flags, dict) else {}
        identity_level = self._resolve_identity_level(payload, runtime_flags)
        stable_observation_count = self._resolve_stable_observation_count(payload, runtime_flags)
        recent_reply_cooldown_hit = self._to_bool(
            runtime_flags.get("recent_reply_cooldown_hit") or runtime_flags.get("recent_duplicate_hit")
        )
        preserve_existing_reply = self._to_bool(runtime_flags.get("preserve_existing_reply"))
        allow_no_answer_fail_soft = (
            policy.allow_no_answer_fail_soft
            and not fallback_generation_empty
            and not need_human
            and bool(reply_content)
            and fallback_source in {"llm_only_fallback", "llm_only_fail_soft"}
        )

        if not str(payload.content or "").strip():
            return self._decision(
                action=EligibilityAction.SKIP,
                reason_code="empty_request",
                reply_content="",
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        if reply_disposition == "pause_for_human" or need_human:
            return self._decision(
                action=EligibilityAction.PAUSE_FOR_HUMAN,
                reason_code=reason_code or "need_human",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level or "high",
                confidence=confidence,
                handoff_level=self._handoff_level_for(risk_level, smart_result),
            )
        if reply_disposition == "defer":
            return self._decision(
                action=EligibilityAction.RETRY_LATER,
                reason_code=reason_code or "reply_deferred",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        if reply_disposition == "skip":
            return self._decision(
                action=EligibilityAction.SKIP,
                reason_code=reason_code or "reply_skipped",
                reply_content="",
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        if fallback_generation_empty and not reply_content:
            return self._decision(
                action=EligibilityAction.SKIP,
                reason_code=reason_code or "fallback_generation_empty",
                reply_content="",
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        if retrieval_final_action == "no_answer" and not allow_no_answer_fail_soft and not reply_content:
            # 合并：no_answer + 空内容才拦截，避免已有可发内容时再被无意义卡掉
            # 修复：空回复时返回 SKIP 而非 ACK_ONLY，ACK_ONLY 仅用于有回复内容但需要降级的场景
            return self._decision(
                action=EligibilityAction.SKIP,
                reason_code=reason_code or "no_grounded_answer",
                reply_content="",
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        if not reply_content:
            return self._decision(
                action=EligibilityAction.SKIP,
                reason_code=reason_code or "empty_reply",
                reply_content="",
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
            )
        # 关键快速通道：已有可用 reply_content + 风险可控 + 不在冷却中 → 直接放行
        # 把"有内容"的消息不再被连环拦截（Phase 3 核心改动）
        if risk_level in {"critical", "high"}:
            return self._decision(
                action=EligibilityAction.PAUSE_FOR_HUMAN,
                reason_code=reason_code or "high_risk_blocked",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
                handoff_level=self._handoff_level_for(risk_level, smart_result),
            )
        # 修复：知识库已命中（retrieval_final_action=pass + GROUNDED + 有回复内容）时，
        # 不应被 recent_reply_cooldown 跳过或 ACK_ONLY 覆盖，否则用户连续提问时后续问题无法回复
        # 场景1：用户消息"可以自己外出就餐吗" intent=E score=0.00，但 knowledge=yes retrieval_final_action=pass
        # 场景2：用户连续提问"错过开船时间怎么办"，前一条已回复，此条 knowledge=yes 但被冷却跳过
        knowledge_grounded_pass = (
            retrieval_final_action == "pass"
            and evidence_level == EvidenceLevel.GROUNDED.value
            and bool(reply_content)
        )
        if recent_reply_cooldown_hit and not preserve_existing_reply:
            logger.info(
                f"[reply_eligibility] cooldown检查: "
                f"knowledge_grounded_pass={knowledge_grounded_pass}, "
                f"retrieval_final_action={retrieval_final_action}, "
                f"evidence_level={evidence_level}, "
                f"confidence={confidence:.3f}, "
                f"reply_len={len(reply_content)}"
            )
            if knowledge_grounded_pass:
                logger.info(
                    f"知识库命中回复跳过 recent_reply_cooldown: "
                    f"retrieval_final_action={retrieval_final_action}, "
                    f"evidence_level={evidence_level}, reply_len={len(reply_content)}"
                )
            else:
                action = self._resolve_action(policy.recent_reply_cooldown_action, default=EligibilityAction.SKIP)
                return self._decision(
                    action=action,
                    reason_code=reason_code or "recent_reply_cooldown",
                    reply_content="" if action in {EligibilityAction.SKIP, EligibilityAction.ACK_ONLY} else reply_content,
                    evidence_level=evidence_level,
                    risk_level=risk_level,
                    confidence=confidence,
                    extra_metadata={
                        "identity_level": identity_level,
                        "stable_observation_count": stable_observation_count,
                        "recent_reply_cooldown_hit": True,
                    },
                )
        # 合并：未取信 + 低置信度 → 走统一 ungrounded 处置
        # 修复 P3：读取 policy.ungrounded_action，ACK_ONLY 时清空 reply_content
        if (
            (evidence_level == EvidenceLevel.UNGROUNDED.value
             or confidence < policy.min_grounded_confidence)
            and not allow_no_answer_fail_soft
            and not preserve_existing_reply
            and not knowledge_grounded_pass
        ):
            action = self._resolve_action(policy.ungrounded_action, default=EligibilityAction.ACK_ONLY)
            logger.info(
                f"[reply_eligibility] ACK_ONLY触发: "
                f"action={action}, evidence_level={evidence_level}, "
                f"confidence={confidence:.3f} < {policy.min_grounded_confidence}, "
                f"knowledge_grounded_pass={knowledge_grounded_pass}, "
                f"retrieval_final_action={retrieval_final_action}, "
                f"allow_no_answer_fail_soft={allow_no_answer_fail_soft}"
            )
            return self._decision(
                action=action,
                reason_code=reason_code or "ungrounded_reply_blocked",
                reply_content="" if action in {EligibilityAction.SKIP, EligibilityAction.ACK_ONLY} else reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
                extra_metadata={
                    "identity_level": identity_level,
                    "stable_observation_count": stable_observation_count,
                    "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                },
            )
        # 已在上方合并：ungrounded + 弱置信度统一走 weak_grounding_send
        # 修复 P3：弱身份处置读取 policy.weak_identity_action 而非硬编码 SEND
        if not self._has_strong_identity(identity_level) and not preserve_existing_reply:
            weak_identity_action = str(
                getattr(policy, "weak_identity_action", "") or EligibilityAction.ACK_ONLY.value
            )
            if weak_identity_action == EligibilityAction.ACK_ONLY.value:
                return self._decision(
                    action=EligibilityAction.ACK_ONLY,
                    reason_code=reason_code or "weak_target_identity",
                    reply_content="",  # ACK_ONLY 时清空，由 _apply_reply_eligibility_decision_content 替换为兜底语
                    evidence_level=evidence_level,
                    risk_level=risk_level,
                    confidence=confidence,
                    extra_metadata={
                        "identity_level": identity_level,
                        "stable_observation_count": stable_observation_count,
                        "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                    },
                )
            return self._decision(
                action=EligibilityAction.SEND,
                reason_code=reason_code or "weak_identity_send",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
                extra_metadata={
                    "identity_level": identity_level,
                    "stable_observation_count": stable_observation_count,
                    "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                },
            )
        # 观察窗口不稳定：按策略处置（修复 P3：读取 policy.unstable_window_action 而非硬编码 SEND）
        if (
            stable_observation_count < max(int(policy.min_stable_observation_count or 0), 1)
            and not preserve_existing_reply
        ):
            unstable_action = str(
                getattr(policy, "unstable_window_action", "") or EligibilityAction.ACK_ONLY.value
            )
            if unstable_action == EligibilityAction.ACK_ONLY.value:
                return self._decision(
                    action=EligibilityAction.ACK_ONLY,
                    reason_code=reason_code or "unstable_inbound_window",
                    reply_content="",  # ACK_ONLY 时清空，由 _apply_reply_eligibility_decision_content 替换为兜底语
                    evidence_level=evidence_level,
                    risk_level=risk_level,
                    confidence=confidence,
                    extra_metadata={
                        "identity_level": identity_level,
                        "stable_observation_count": stable_observation_count,
                        "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                    },
                )
            return self._decision(
                action=EligibilityAction.SEND,
                reason_code=reason_code or "unstable_window_send",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
                extra_metadata={
                    "identity_level": identity_level,
                    "stable_observation_count": stable_observation_count,
                    "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                },
            )
        if confidence < policy.min_send_confidence and not allow_no_answer_fail_soft:
            # Phase 3：DRAFT_FOR_REVIEW 改回 SEND 倾向（已用 reply_content）
            # 当 confidence 仍低于 min_send 但已有可发内容时降级发送，不再卡住
            return self._decision(
                action=EligibilityAction.SEND,
                reason_code=reason_code or "low_confidence_with_content_send",
                reply_content=reply_content,
                evidence_level=evidence_level,
                risk_level=risk_level,
                confidence=confidence,
                extra_metadata={
                    "identity_level": identity_level,
                    "stable_observation_count": stable_observation_count,
                    "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
                },
            )
        return self._decision(
            action=EligibilityAction.SEND,
            reason_code=reason_code or "eligible_send",
            reply_content=reply_content,
            evidence_level=evidence_level,
            risk_level=risk_level,
            confidence=confidence,
            extra_metadata={
                "identity_level": identity_level,
                "stable_observation_count": stable_observation_count,
                "recent_reply_cooldown_hit": recent_reply_cooldown_hit,
            },
        )

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value or "").strip().lower()
        return normalized in {"1", "true", "yes", "on"}

    def _resolve_identity_level(self, payload: ReplyEligibilityInput, runtime_flags: Dict[str, Any]) -> str:
        identity_level = str(runtime_flags.get("identity_level", "") or "").strip().lower()
        if identity_level:
            return identity_level
        if str(payload.customer_id or "").strip():
            return "strict_customer_id"
        if str(payload.conversation_id or "").strip():
            return "strict_conversation_id"
        if str(payload.customer_name or "").strip():
            return "exact_name"
        return "name_fuzzy"

    def _resolve_stable_observation_count(
        self,
        payload: ReplyEligibilityInput,
        runtime_flags: Dict[str, Any],
    ) -> int:
        runtime_value = runtime_flags.get("stable_observation_count")
        if runtime_value is not None:
            return max(self._to_int(runtime_value, default=0), 0)
        history_count = max(self._to_int(payload.history_message_count, default=0), 0)
        return 2 if history_count > 0 else 1

    @staticmethod
    def _has_strong_identity(identity_level: str) -> bool:
        return str(identity_level or "").strip().lower() in {
            "strict_conversation_id",
            "strict_customer_id",
        }

    @staticmethod
    def _resolve_action(action_name: str, default: EligibilityAction) -> EligibilityAction:
        normalized = str(action_name or "").strip().lower()
        for action in EligibilityAction:
            if action.value == normalized:
                return action
        return default

    def _handoff_level_for(self, risk_level: str, smart_result: Dict[str, Any]) -> str:
        risk_level = str(risk_level or "").strip().lower()
        intents = {
            str((smart_result or {}).get("intent", "") or "").strip().lower(),
            str((smart_result or {}).get("intent_label", "") or "").strip().lower(),
            str((smart_result or {}).get("intent_type", "") or "").strip().lower(),
        }
        if "complaint" in intents:
            return HandoffLevel.COMPLAINT.value
        if risk_level in {"critical", "high"}:
            return HandoffLevel.URGENT.value
        return HandoffLevel.NORMAL.value

    def _decision(
        self,
        *,
        action: EligibilityAction,
        reason_code: str,
        reply_content: str,
        evidence_level: str,
        risk_level: str,
        confidence: float,
        handoff_level: str = HandoffLevel.NONE.value,
        extra_metadata: Dict[str, Any] | None = None,
    ) -> ReplyEligibilityDecision:
        metadata = {
            "action": action.value,
            "evidence_level": evidence_level,
            "risk_level": str(risk_level or "").strip(),
            "confidence": confidence,
            "handoff_level": handoff_level,
        }
        if isinstance(extra_metadata, dict):
            metadata.update(extra_metadata)
        return ReplyEligibilityDecision(
            action=action.value,
            reason_code=str(reason_code or "").strip(),
            should_send=action == EligibilityAction.SEND,
            should_pause_workflow=action == EligibilityAction.PAUSE_FOR_HUMAN,
            should_create_handoff=action == EligibilityAction.PAUSE_FOR_HUMAN,
            handoff_level=handoff_level,
            evidence_level=evidence_level,
            risk_level=str(risk_level or "").strip(),
            confidence=confidence,
            final_reply=str(reply_content or ""),
            normalized_metadata=metadata,
        )


_reply_eligibility_service: ReplyEligibilityService | None = None


def get_reply_eligibility_service() -> ReplyEligibilityService:
    global _reply_eligibility_service
    if _reply_eligibility_service is None:
        _reply_eligibility_service = ReplyEligibilityService()
    return _reply_eligibility_service
