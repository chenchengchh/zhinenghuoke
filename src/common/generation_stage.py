"""
生成阶段封装

将回复主链中的生成策略与 RAG+LLM 生成抽离为独立 stage，
便于后续继续拆分 generation / guardrail。
"""
from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.common.utils import call_llm_safe
from src.config.settings import (
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_TEMPERATURE,
    OLLAMA_TOP_K,
    OLLAMA_TOP_P,
    REPLY_LLM_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def _is_math_related_content(content: str) -> bool:
    if not content:
        return False
    math_indicators = [
        "无数学问题",
        "无计算结果",
        "无法给出数学计算结果",
        "没有包含任何具体的数学问题",
        "不满足.*指代历史上的计算题",
        "从未出现任何数学问题",
        "不存在任何数学问题",
        "可计算的数值表达式",
        "数学表达式或运算请求",
        "分数.*3/20",
        "化为小数",
        "计算结果：",
        "LaTeX",
        "\\frac{",
        "$$",
        "对话历史中.*数学",
        "数学语境中",
    ]
    for pat in math_indicators:
        if re.search(pat, content):
            return True
    if content.startswith("✅") and "数学" in content:
        return True
    if content.startswith("**") and ("数学" in content or "计算" in content):
        return True
    return False


def _filter_math_history(conversation_history: List[Dict], max_messages: int = 3) -> str:
    history_text = ""
    count = 0
    assistant_count = 0
    max_assistant_messages = 1  # 限制助手历史最多1条，减少 LLM 对自身历史回复的依赖
    if conversation_history:
        for msg in reversed(conversation_history):
            content = msg.get("content", "")
            if _is_math_related_content(content):
                continue
            role = "用户" if msg.get("direction") == "inbound" else "助手"
            if role == "助手":
                assistant_count += 1
                if assistant_count > max_assistant_messages:
                    continue
            history_text = f"{role}：{content}\n" + history_text
            count += 1
            if count >= max_messages:
                break
    return history_text


class GenerationStage:
    """封装回复主链中的生成阶段。"""

    @staticmethod
    def _call_should_prefer_grounded_plan_direct(
        service: Any,
        message: str,
        answer_plan: Dict[str, Any],
        bundle_route_facts: Dict[str, Any],
    ) -> bool:
        try:
            return service._should_prefer_grounded_plan_direct(
                message,
                answer_plan,
                route_facts=bundle_route_facts,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc or ""):
                raise
            return service._should_prefer_grounded_plan_direct(message, answer_plan)

    @staticmethod
    def _call_should_prefer_single_route_grounded_direct(
        service: Any,
        message: str,
        answer_plan: Dict[str, Any],
        bundle_route_facts: Dict[str, Any],
        bundle_route_evidence: Dict[str, Any],
        matched_knowledge: Any,
    ) -> bool:
        try:
            return service._should_prefer_single_route_grounded_direct(
                message,
                answer_plan,
                bundle_route_facts,
                bundle_route_evidence,
                matched_knowledge,
            )
        except TypeError as exc:
            if (
                "unexpected keyword argument" not in str(exc or "")
                and "positional argument" not in str(exc or "")
            ):
                raise
            return service._should_prefer_single_route_grounded_direct(message, answer_plan)

    @staticmethod
    def _append_trace_span(
        service: Any,
        reply_analysis: Dict[str, Any],
        *,
        name: str,
        duration_ms: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if hasattr(service, "_append_trace_span"):
            service._append_trace_span(
                reply_analysis,
                name=name,
                duration_ms=duration_ms,
                metadata=metadata,
            )

    @staticmethod
    def _call_should_prefer_grounded_direct_answer(
        service: Any,
        message: str,
        matched_knowledge: Any,
        retrieval_query: Optional[str],
    ) -> bool:
        try:
            return service._should_prefer_grounded_direct_answer(
                message,
                matched_knowledge,
                retrieval_query=retrieval_query,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc or ""):
                raise
            return service._should_prefer_grounded_direct_answer(
                message,
                matched_knowledge,
            )

    def generate(
        self,
        *,
        service: Any,
        mode: str,
        message: str,
        retrieval_query: Optional[str],
        enterprise_id: str = "",
        session_id: str = "",
        conversation_history: List[Dict],
        matched_knowledge: Any,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict],
        reply_objective: Optional[Any] = None,
    ) -> Optional[str]:
        started_at = time.perf_counter()
        smart_reply = None
        retrieval_state = reply_analysis.get("retrieval", {})
        answer_plan: Dict[str, Any] = {}
        bundle_route_facts: Dict[str, Any] = {}
        bundle_route_evidence: Dict[str, Any] = {}
        should_try_llm_only_fallback = False
        if retrieved_contexts:
            answer_plan, bundle_route_facts, bundle_route_evidence = service._extract_bundle_grounded_context(
                retrieved_contexts
            )
            if "candidate_routes" not in answer_plan and bundle_route_facts:
                answer_plan["candidate_routes"] = list(bundle_route_facts.keys())
            if self._call_should_prefer_grounded_plan_direct(
                service,
                message,
                answer_plan,
                bundle_route_facts,
            ):
                reply_analysis["fallback"] = {
                    "source": "knowledge_base_grounded_plan_direct",
                    "reason": "structured_bundle_answer_plan",
                }
                reply_analysis.setdefault("generation_policy", {})["direct_reply_disabled"] = True
            if self._call_should_prefer_single_route_grounded_direct(
                service,
                message,
                answer_plan,
                bundle_route_facts,
                bundle_route_evidence,
                matched_knowledge,
            ):
                reply_analysis["fallback"] = {
                    "source": "knowledge_base_single_route_grounded_direct",
                    "reason": "structured_bundle_single_route_plan",
                }
                reply_analysis.setdefault("generation_policy", {})["direct_reply_disabled"] = True

        existing_fallback_source = str((reply_analysis.get("fallback") or {}).get("source") or "")
        if retrieval_state.get("exact_match") and matched_knowledge and getattr(matched_knowledge, "answer", ""):
            if existing_fallback_source not in {
                "knowledge_base_grounded_plan_direct",
                "knowledge_base_single_route_grounded_direct",
            }:
                reply_analysis["fallback"] = {
                    "source": "knowledge_base_exact_direct",
                    "reason": "exact_question_or_alias",
                }
                reply_analysis.setdefault("generation_policy", {})["direct_reply_disabled"] = True

        if retrieval_state.get("final_action") == "no_answer":
            # 修复：保留弱证据（relevance_score >= CRAG_MIN_RELEVANCE）供 LLM 参考，
            # 不再无条件清空 retrieved_contexts，避免弱证据"死亡区间"丢失
            min_relevance = float(getattr(service, "CRAG_MIN_RELEVANCE", 0.22) or 0.22)
            weak_evidence = [
                ctx for ctx in retrieved_contexts
                if isinstance(ctx, dict)
                and float(ctx.get("relevance_score", 0.0) or 0.0) >= min_relevance
            ]
            if weak_evidence:
                retrieved_contexts = weak_evidence
                reply_analysis.setdefault("generation_policy", {})["weak_evidence_preserved"] = True
                reply_analysis.setdefault("generation_policy", {})["weak_evidence_warning"] = (
                    f"以下证据相关性较低（最低 {min_relevance:.2f}），请谨慎参考"
                )
                logger.info(
                    f"CRAG no_answer 保留弱证据: query={message[:30]}, "
                    f"保留 contexts 数={len(weak_evidence)}"
                )
            else:
                retrieved_contexts = []
            matched_knowledge = None
            should_try_llm_only_fallback = bool(
                service._should_allow_llm_only_fallback_for_generation(
                    mode=mode,
                    message=message,
                    reply_analysis=reply_analysis,
                    enterprise_id=enterprise_id,
                )
            )
            reply_analysis.setdefault("generation_policy", {})["terminated_reason"] = retrieval_state.get(
                "final_reason",
                "insufficient_evidence",
            )
            logger.info(f"CRAG no_answer，停止生成硬兜底回复: query={message[:30]}")

        if retrieval_state.get("final_action") == "retry":
            # P1-4: Web 搜索兜底证据保留，不清除 contexts
            if retrieval_state.get("final_reason") == "web_search_fallback" and retrieved_contexts:
                reply_analysis["fallback"] = {
                    "source": "web_search_fallback",
                    "reason": "web_search_evidence_preserved",
                }
                reply_analysis.setdefault("generation_policy", {})["web_search_context"] = True
                logger.info(
                    f"Web 搜索兜底证据保留: query={message[:30]}, "
                    f"contexts={len(retrieved_contexts)}"
                )
            else:
                allow_retry_direct = service._should_allow_retry_direct_answer(
                    message,
                    retrieved_contexts,
                    conversation_history=conversation_history,
                    session_id=session_id,
                    enterprise_id=enterprise_id,
                )
                if (
                    retrieval_state.get("final_reason") == "rewrite_failed_preserve_initial"
                    and matched_knowledge
                    and getattr(matched_knowledge, "answer", "")
                    and retrieved_contexts
                    and allow_retry_direct
                ):
                    reply_analysis["fallback"] = {
                        "source": "knowledge_base_retry_direct",
                        "reason": retrieval_state.get("final_reason", "weak_evidence_preserved"),
                    }
                    reply_analysis.setdefault("generation_policy", {})["direct_reply_disabled"] = True
                elif service._should_route_retry_to_llm_only(
                    message=message,
                    retrieved_contexts=retrieved_contexts,
                    conversation_history=conversation_history,
                    session_id=session_id,
                    enterprise_id=enterprise_id,
                ):
                    # 修复：保留弱证据供 LLM 参考，不再无条件清空
                    min_relevance = float(getattr(service, "CRAG_MIN_RELEVANCE", 0.22) or 0.22)
                    weak_evidence = [
                        ctx for ctx in retrieved_contexts
                        if isinstance(ctx, dict)
                        and float(ctx.get("relevance_score", 0.0) or 0.0) >= min_relevance
                    ]
                    if weak_evidence:
                        retrieved_contexts = weak_evidence
                        reply_analysis.setdefault("generation_policy", {})["weak_evidence_preserved"] = True
                        reply_analysis.setdefault("generation_policy", {})["weak_evidence_warning"] = (
                            f"以下证据相关性较低（最低 {min_relevance:.2f}），请谨慎参考"
                        )
                        logger.info(
                            f"CRAG retry→llm_only 保留弱证据: query={message[:30]}, "
                            f"保留 contexts 数={len(weak_evidence)}"
                        )
                    else:
                        retrieved_contexts = []
                    matched_knowledge = None
                    should_try_llm_only_fallback = bool(
                        service._should_allow_llm_only_fallback_for_generation(
                            mode=mode,
                            message=message,
                            reply_analysis=reply_analysis,
                            enterprise_id=enterprise_id,
                        )
                    )
                    reply_analysis.setdefault("generation_policy", {})["terminated_reason"] = retrieval_state.get(
                        "final_reason",
                        "weak_evidence_preserved",
                    )
                else:
                    # 修复：保留弱证据供 LLM 参考，不再无条件清空
                    min_relevance = float(getattr(service, "CRAG_MIN_RELEVANCE", 0.22) or 0.22)
                    weak_evidence = [
                        ctx for ctx in retrieved_contexts
                        if isinstance(ctx, dict)
                        and float(ctx.get("relevance_score", 0.0) or 0.0) >= min_relevance
                    ]
                    if weak_evidence:
                        retrieved_contexts = weak_evidence
                        reply_analysis.setdefault("generation_policy", {})["weak_evidence_preserved"] = True
                        reply_analysis.setdefault("generation_policy", {})["weak_evidence_warning"] = (
                            f"以下证据相关性较低（最低 {min_relevance:.2f}），请谨慎参考"
                        )
                        logger.info(
                            f"CRAG retry 兜底保留弱证据: query={message[:30]}, "
                            f"保留 contexts 数={len(weak_evidence)}"
                        )
                    else:
                        retrieved_contexts = []
                    matched_knowledge = None
                    should_try_llm_only_fallback = bool(
                        service._should_allow_llm_only_fallback_for_generation(
                            mode=mode,
                            message=message,
                            reply_analysis=reply_analysis,
                            enterprise_id=enterprise_id,
                        )
                    )
                    reply_analysis.setdefault("generation_policy", {})["terminated_reason"] = retrieval_state.get(
                        "final_reason",
                        "weak_evidence_preserved",
                    )

        direct_reply = self._build_direct_reply(
            service=service,
            mode=mode,
            message=message,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            answer_plan=answer_plan,
            bundle_route_facts=bundle_route_facts,
            bundle_route_evidence=bundle_route_evidence,
        )
        if direct_reply:
            smart_reply = direct_reply
            self._append_trace_span(
                service,
                reply_analysis,
                name="reply_generation_direct",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={
                    "source": str((reply_analysis.get("fallback") or {}).get("source") or "direct"),
                    "reply_present": True,
                },
            )

        if retrieved_contexts and not smart_reply:
            existing_fallback_source = str((reply_analysis.get("fallback") or {}).get("source") or "")
            if (
                self._call_should_prefer_grounded_direct_answer(
                    service,
                    message,
                    matched_knowledge,
                    retrieval_query,
                )
                and existing_fallback_source not in {
                    "knowledge_base_grounded_plan_direct",
                    "knowledge_base_single_route_grounded_direct",
                    "knowledge_base_exact_direct",
                    "knowledge_base_retry_direct",
                }
            ):
                reply_analysis["fallback"] = {
                    "source": "knowledge_base_grounded_direct",
                    "reason": "exact_message_or_rewrite_match",
                }
                reply_analysis.setdefault("generation_policy", {})["direct_reply_disabled"] = True
            try:
                self._generate_with_context(
                    service=service,
                    mode=mode,
                    message=message,
                    enterprise_id=enterprise_id,
                    conversation_history=conversation_history,
                    reply_analysis=reply_analysis,
                    retrieved_contexts=retrieved_contexts,
                )
                rag_llm = reply_analysis.get("rag_llm") or {}
                if not smart_reply and rag_llm.get("reply"):
                    smart_reply = rag_llm["reply"]
            except Exception as e:
                logger.warning(f"LLM重组答案失败: {e}")
                reply_analysis.setdefault("generation_policy", {})["terminated_reason"] = f"rag_llm_exception:{str(e)[:80]}"

        if (
            not smart_reply
            and not retrieved_contexts
            and not matched_knowledge
            and service._should_allow_llm_only_fallback_for_generation(
                mode=mode,
                message=message,
                reply_analysis=reply_analysis,
                enterprise_id=enterprise_id,
            )
        ):
            should_try_llm_only_fallback = True

        if not smart_reply and should_try_llm_only_fallback:
            fallback_reason = str(
                (reply_analysis.get("fallback") or {}).get("reason")
                or retrieval_state.get("final_reason")
                or "knowledge_not_found"
            )
            reply_analysis["fallback"] = {
                "source": "llm_only_pending",
                "reason": fallback_reason,
            }
            smart_reply = service._generate_llm_only_fallback_reply(
                mode=mode,
                message=message,
                conversation_history=conversation_history,
                reply_analysis=reply_analysis,
                enterprise_id=enterprise_id,
            )
        elif not smart_reply and not should_try_llm_only_fallback:
            reply_analysis.setdefault("generation_policy", {})["llm_only_blocked"] = True
            fallback_reason = str(
                (reply_analysis.get("fallback") or {}).get("reason")
                or retrieval_state.get("final_reason")
                or "knowledge_not_found"
            )
            reply_analysis["fallback"] = {
                "source": "llm_only_blocked",
                "reason": fallback_reason,
            }

        if not smart_reply:
            reply_analysis.setdefault("generation_policy", {}).setdefault(
                "terminated_reason",
                "no_direct_reply_or_rag_llm_answer",
            )
            self._append_trace_span(
                service,
                reply_analysis,
                name="reply_generation_chain",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                metadata={
                    "source": str((reply_analysis.get("fallback") or {}).get("source") or "unresolved"),
                    "reply_present": False,
                    "terminated_reason": str(
                        (reply_analysis.get("generation_policy") or {}).get("terminated_reason") or ""
                    ),
                },
            )
            return None
        cta_text = service._generate_stage_cta(
            reply_objective=reply_objective,
            message=message,
            conversation_history=conversation_history,
            enterprise_id=enterprise_id,
        )
        if service._should_append_stage_cta(
            message=message,
            answer_text=smart_reply or "",
            reply_objective=reply_objective,
            enterprise_id=enterprise_id,
        ):
            final_reply = service._merge_answer_and_cta(smart_reply or "", cta_text)
        else:
            final_reply = smart_reply
        reply_analysis["generation_cta"] = {
            "used": bool(final_reply != smart_reply),
            "cta_text": cta_text[:120],
            "cta_mode": str(getattr(reply_objective, "cta_mode", "") or ""),
            "next_best_action": str(getattr(getattr(reply_objective, "next_best_action", None), "value", getattr(reply_objective, "next_best_action", "")) or ""),
            "contains_contact_phrase": any(
                marker in str(final_reply or "")
                for marker in ("留个接收资料的联系方式", "更完整的资料", "继续为您处理")
            ),
        }
        self._append_trace_span(
            service,
            reply_analysis,
            name="reply_generation_chain",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            metadata={
                "source": str(
                    (reply_analysis.get("rag_llm") or {}).get("source")
                    or (reply_analysis.get("fallback") or {}).get("source")
                    or "unknown"
                ),
                "reply_present": bool(final_reply),
                "used_cta": bool(final_reply != smart_reply),
            },
        )
        return final_reply

    def _build_direct_reply(
        self,
        *,
        service: Any,
        mode: str,
        message: str,
        matched_knowledge: Any,
        reply_analysis: Dict[str, Any],
        answer_plan: Dict[str, Any],
        bundle_route_facts: Dict[str, Any],
        bundle_route_evidence: Dict[str, Any],
    ) -> Optional[str]:
        fallback_source = str((reply_analysis.get("fallback") or {}).get("source") or "")
        if not fallback_source:
            return None
        if fallback_source == "knowledge_base_grounded_plan_direct" and answer_plan:
            return service._render_grounded_plan_direct_reply(
                mode=mode,
                message=message,
                answer_plan=answer_plan,
                route_facts=bundle_route_facts,
            )
        if fallback_source == "knowledge_base_single_route_grounded_direct" and answer_plan:
            return service._render_single_route_grounded_reply(
                mode=mode,
                message=message,
                answer_plan=answer_plan,
                route_facts=bundle_route_facts,
                route_evidence=bundle_route_evidence,
                matched_knowledge=matched_knowledge,
            )
        if fallback_source in {
            "knowledge_base_exact_direct",
            "knowledge_base_grounded_direct",
            "knowledge_base_retry_direct",
        }:
            return service._build_knowledge_only_fallback_reply(
                message=message,
                matched_knowledge=matched_knowledge,
                retrieved_contexts=[],
            )
        return None

    def _generate_with_context(
        self,
        *,
        service: Any,
        mode: str,
        message: str,
        enterprise_id: str = "",
        conversation_history: List[Dict],
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict],
    ) -> None:
        perf_metrics: Dict[str, float] = {}
        stage_started_at = time.perf_counter()
        from .llm_service import get_default_llm_provider

        llm_provider = get_default_llm_provider()
        service._record_perf_metric(perf_metrics, "get_llm_provider_with_context", stage_started_at)

        if not llm_provider:
            return

        model_name = f"ollama/{getattr(llm_provider, 'model', type(llm_provider).__name__)}"
        structured_bundle_text, context_text = service._build_retrieval_prompt_sections(retrieved_contexts[:5])
        history_text = _filter_math_history(conversation_history, 3)
        prompt = service._build_contextual_reply_prompt(
            mode=mode,
            context_text=context_text,
            structured_bundle_text=structured_bundle_text,
            history_text=history_text,
            message=message,
            enterprise_id=enterprise_id,
        )

        stage_started_at = time.perf_counter()
        smart_reply = call_llm_safe(
            llm_provider,
            prompt,
            timeout=REPLY_LLM_TIMEOUT_SECONDS,
            num_ctx=OLLAMA_NUM_CTX,
            num_predict=OLLAMA_NUM_PREDICT,
            temperature=OLLAMA_TEMPERATURE,
            top_p=OLLAMA_TOP_P,
            top_k=OLLAMA_TOP_K,
        )
        service._record_perf_metric(perf_metrics, "rag_llm_chat", stage_started_at)
        if not smart_reply:
            logger.warning("RAG+LLM生成回复为空，继续走无证据 LLM 生成")
            reply_analysis["fallback"] = {"source": "rag_llm_empty_retry"}
        reply_analysis["rag_llm"] = {
            "source": "knowledge_base+llm",
            "model": model_name,
            "contexts_count": len(retrieved_contexts),
            "gate_action": (reply_analysis.get("retrieval") or {}).get("final_action", "pass"),
            "reply": smart_reply,
        }
        reply_analysis["generation_perf"] = copy.deepcopy(perf_metrics)
        self._append_trace_span(
            service,
            reply_analysis,
            name="reply_generation_with_context",
            duration_ms=sum(perf_metrics.values()),
            metadata={
                "source": "knowledge_base+llm",
                "contexts_count": len(retrieved_contexts),
            },
        )
        service._log_perf_metrics(
            "generate_reply_with_context",
            perf_metrics,
            mode=mode,
            contexts_count=len(retrieved_contexts),
        )
        logger.info(f"RAG+LLM生成回复: 检索到{len(retrieved_contexts)}条知识")
