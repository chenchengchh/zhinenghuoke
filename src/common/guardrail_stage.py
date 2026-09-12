"""
Guardrail / apply 阶段封装

将回复主链中的最终回写、安全拦截、知识直出兜底与 grounded context 记忆
抽离为独立 stage，便于进一步收口 orchestrator。
"""
from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class GuardrailService(Protocol):
    def _looks_like_hidden_reasoning_leak(self, text: str) -> bool: ...
    def _should_append_route_contact_guidance(self, *, message: str, reply: str, conversation_history: Optional[List[Dict]], matched_knowledge: Any) -> bool: ...
    def _append_route_contact_guidance(self, reply: str, *, message: str, conversation_history: Optional[List[Dict]], matched_knowledge: Any, customer_name: str) -> str: ...
    def _sanitize_outbound_text(self, text: str) -> str: ...
    def _remember_grounded_context(self, *, session_id: str, retrieval_query: str, matched_knowledge: Any, reply_analysis: Dict[str, Any], retrieved_contexts: List[Dict[str, Any]]) -> None: ...
    def _repair_route_overview_reply(self, reply: str, *, message: str, conversation_history: Optional[List[Dict]]) -> str: ...
    @property
    def _learning_engine(self) -> Any: ...


class GuardrailStage:
    """封装回复主链中的最终回写与 guardrail 阶段。"""

    _FACT_FRAGMENT_PATTERNS = (
        r"\d+(?:\.\d+)?\s*(?:元|块|天|小时|分钟|次|个|人|折|%|％|周|月|年)",
        r"\d{4}年\d{1,2}月\d{1,2}日",
        r"\d{1,2}月\d{1,2}日",
        r"\d{1,2}[:：]\d{2}",
        r"(?:工作日|周末|周一|周二|周三|周四|周五|周六|周日)",
        r"(?:满|至少|不低于|大于|小于)\s*\d+(?:\.\d+)?",
    )

    @classmethod
    def _extract_fact_fragments(cls, text: str) -> List[str]:
        normalized = str(text or "").strip()
        if not normalized:
            return []
        fragments: List[str] = []
        for pattern in cls._FACT_FRAGMENT_PATTERNS:
            for match in re.findall(pattern, normalized, flags=re.IGNORECASE):
                fragment = str(match or "").strip()
                if fragment and fragment not in fragments:
                    fragments.append(fragment)
        return fragments

    @classmethod
    def _build_evidence_text(cls, matched_knowledge: Any, retrieved_contexts: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for item in retrieved_contexts or []:
            if isinstance(item, dict):
                for key in ("content", "text", "question", "answer", "snippet"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        parts.append(value)
        if matched_knowledge is not None:
            for key in ("question", "answer", "content"):
                value = str(getattr(matched_knowledge, key, "") or "").strip()
                if value:
                    parts.append(value)
        return "\n".join(parts)

    @classmethod
    def _is_fact_sensitive_query(cls, service: Any, message: str) -> bool:
        helper = getattr(service, "_is_high_risk_detail_query", None)
        if callable(helper):
            try:
                return bool(helper(message))
            except Exception:
                pass
        return bool(cls._extract_fact_fragments(message)) or any(
            token in str(message or "")
            for token in ("价格", "费用", "条件", "多久", "时间", "日期", "额度", "名额", "周期")
        )

    @classmethod
    def _find_unsupported_fact_fragments(
        cls,
        *,
        reply: str,
        evidence_text: str,
    ) -> List[str]:
        reply_fragments = cls._extract_fact_fragments(reply)
        normalized_evidence = str(evidence_text or "").strip()
        if not reply_fragments or not normalized_evidence:
            return reply_fragments
        unsupported: List[str] = []
        # 修复 P10：事实片段归一化匹配，避免"元"vs"块"、"3天"vs"三天"等同义表达被误判
        normalized_evidence_text = cls._normalize_fact_text(normalized_evidence)
        for fragment in reply_fragments:
            if cls._normalize_fact_text(fragment) not in normalized_evidence_text:
                unsupported.append(fragment)
        return unsupported

    @staticmethod
    def _normalize_fact_text(text: str) -> str:
        """归一化事实文本，统一中文数字、量词等变体。"""
        if not text:
            return ""
        # 中文数字转阿拉伯数字
        cn_num_map = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
                      "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
        for cn, num in cn_num_map.items():
            text = text.replace(cn, num)
        # 量词统一
        text = text.replace("块钱", "元").replace("块", "元")
        text = text.replace("毛", "角")
        # 去除空格
        text = text.replace(" ", "")
        return text

    @staticmethod
    def _append_trace_span(
        service: Any,
        reply_analysis: Dict[str, Any],
        *,
        duration_ms: float,
        outcome: str,
        source: str,
    ) -> None:
        if hasattr(service, "_append_trace_span"):
            service._append_trace_span(
                reply_analysis,
                name="guardrail_apply",
                duration_ms=duration_ms,
                metadata={
                    "outcome": outcome,
                    "source": source or "unknown",
                },
            )

    def apply(
        self,
        *,
        service: GuardrailService,
        enhanced_result: Dict[str, Any],
        smart_reply: Optional[str],
        matched_knowledge: Any,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict[str, Any]],
        message: str,
        conversation_history: Optional[List[Dict]],
        session_id: Optional[str],
        customer_name: str = "",
    ) -> None:
        started_at = time.perf_counter()
        if not smart_reply:
            self._apply_empty_reply(
                service=service,
                enhanced_result=enhanced_result,
                reply_analysis=reply_analysis,
                retrieved_contexts=retrieved_contexts,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                outcome="empty_reply",
            )
            return

        resolved_smart_reply = str(smart_reply or "").strip()
        if service._looks_like_hidden_reasoning_leak(resolved_smart_reply):
            logger.warning("检测到 LLM 提示词/思维链泄漏，停止发送并返回空回复")
            self._apply_empty_reply(
                service=service,
                enhanced_result=enhanced_result,
                reply_analysis=reply_analysis,
                retrieved_contexts=retrieved_contexts,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                outcome="blocked_hidden_reasoning",
            )
            return

        if hasattr(service, "_repair_route_overview_reply"):
            resolved_smart_reply = service._repair_route_overview_reply(
                resolved_smart_reply,
                message=message,
                conversation_history=conversation_history,
            )

        enhanced_result["reply"] = resolved_smart_reply
        fallback_source = str((reply_analysis.get("fallback") or {}).get("source") or "")
        should_append_contact_guidance = (
            fallback_source != "llm_only_fail_soft"
            and service._should_append_route_contact_guidance(
                message=message,
                reply=enhanced_result.get("reply", ""),
                conversation_history=conversation_history,
                matched_knowledge=matched_knowledge,
            )
        )
        if should_append_contact_guidance:
            enhanced_result["reply"] = service._append_route_contact_guidance(
                enhanced_result.get("reply", ""),
                message=message,
                conversation_history=conversation_history,
                matched_knowledge=matched_knowledge,
                customer_name=customer_name,
            )

        enhanced_result["reply"] = service._sanitize_outbound_text(enhanced_result.get("reply", ""))
        if service._looks_like_hidden_reasoning_leak(enhanced_result.get("reply", "")):
            logger.warning("最终出站文本仍含提示词泄漏痕迹，停止发送并返回空回复")
            self._apply_empty_reply(
                service=service,
                enhanced_result=enhanced_result,
                reply_analysis=reply_analysis,
                retrieved_contexts=retrieved_contexts,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                outcome="blocked_hidden_reasoning_after_sanitize",
            )
            return

        evidence_text = self._build_evidence_text(matched_knowledge, retrieved_contexts)
        unsupported_facts = []
        if self._is_fact_sensitive_query(service, message):
            unsupported_facts = self._find_unsupported_fact_fragments(
                reply=enhanced_result.get("reply", ""),
                evidence_text=evidence_text,
            )
        if unsupported_facts:
            reply_analysis["guardrail"] = {
                "blocked": True,
                "reason": "unsupported_fact_fragments",
                "unsupported_fragments": unsupported_facts[:5],
            }
            logger.warning("检测到回复中的事实片段缺少证据支撑，停止发送并返回空回复")
            self._apply_empty_reply(
                service=service,
                enhanced_result=enhanced_result,
                reply_analysis=reply_analysis,
                retrieved_contexts=retrieved_contexts,
                duration_ms=(time.perf_counter() - started_at) * 1000,
                outcome="blocked_unsupported_facts",
            )
            return

        enhanced_result["reply_analysis"] = reply_analysis
        enhanced_result["retrieval_evidence"] = copy.deepcopy(retrieved_contexts[:3])
        service._remember_grounded_context(
            session_id=str(session_id or ""),
            retrieval_query=str((reply_analysis.get("retrieval") or {}).get("query") or message or ""),
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
        )

        source = "unknown"
        for key in ("rag_llm", "fallback"):
            payload = reply_analysis.get(key)
            if isinstance(payload, dict):
                source = str(payload.get("source") or "unknown")
                break

        if source == "no_answer_policy" or source.startswith("llm_only"):
            enhanced_result["matched_knowledge"] = None
        else:
            enhanced_result["matched_knowledge"] = matched_knowledge.question if matched_knowledge else "LLM生成"

        matched_kid = matched_knowledge.id if matched_knowledge and hasattr(matched_knowledge, "id") else None
        service._learning_engine.on_reply_generated_async(
            message,
            smart_reply,
            source,
            matched_kid,
            session_id,
        )
        self._append_trace_span(
            service,
            reply_analysis,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            outcome="reply_ready",
            source=source,
        )

    def _apply_empty_reply(
        self,
        *,
        service: GuardrailService,
        enhanced_result: Dict[str, Any],
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict[str, Any]],
        duration_ms: float = 0.0,
        outcome: str = "empty_reply",
    ) -> None:
        source = str((reply_analysis.get("fallback") or {}).get("source") or "unknown")
        blocked_for_human = outcome.startswith("blocked_hidden_reasoning") or outcome == "blocked_unsupported_facts"
        enhanced_result["reply"] = ""
        enhanced_result["need_human"] = blocked_for_human
        enhanced_result["reply_disposition"] = "pause_for_human" if blocked_for_human else "skip"
        enhanced_result["reason_code"] = outcome
        enhanced_result["reply_analysis"] = reply_analysis
        enhanced_result["retrieval_evidence"] = copy.deepcopy(retrieved_contexts[:3])
        enhanced_result["matched_knowledge"] = None
        self._append_trace_span(
            service,
            reply_analysis,
            duration_ms=duration_ms,
            outcome=outcome,
            source=source,
        )
