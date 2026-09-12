"""
检索阶段封装

将增强回复链中的统一知识检索、CRAG 门控、改写重试与 tracing 记录
抽离为独立 stage，便于后续继续拆分为 retrieval / evidence / guardrail 子阶段。
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .types.intent import IntentType

logger = logging.getLogger(__name__)


class RetrievalStage:
    """封装回复主链中的检索与证据门控阶段。"""

    @staticmethod
    def _is_unexpected_keyword_type_error(exc: TypeError) -> bool:
        message = str(exc or "")
        return "unexpected keyword argument" in message or "positional argument" in message

    @staticmethod
    def _call_search_knowledge(
        service: Any,
        *,
        query: str,
        top_k: int,
        business_stage: str,
        intent: str,
        enterprise_id: str,
        schema_id: str,
    ):
        try:
            return service._search_knowledge(
                query,
                top_k=top_k,
                business_stage=business_stage,
                intent=intent,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except TypeError as exc:
            if not RetrievalStage._is_unexpected_keyword_type_error(exc):
                raise
            if enterprise_id or schema_id:
                raise
            if business_stage:
                try:
                    return service._search_knowledge(query, top_k=top_k, business_stage=business_stage)
                except TypeError:
                    pass
            return service._search_knowledge(query, top_k=top_k)

    @staticmethod
    def _call_search_knowledge_bundle(
        service: Any,
        *,
        query: str,
        business_stage: str,
        intent: str,
        enterprise_id: str,
        schema_id: str,
    ):
        try:
            return service._search_knowledge_bundle(
                query,
                business_stage=business_stage,
                intent=intent,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except TypeError as exc:
            if not RetrievalStage._is_unexpected_keyword_type_error(exc):
                raise
            if enterprise_id or schema_id:
                raise
            if business_stage:
                try:
                    return service._search_knowledge_bundle(query, business_stage=business_stage)
                except TypeError:
                    pass
            return service._search_knowledge_bundle(query)

    def collect(
        self,
        *,
        service: Any,
        message: str,
        search_query: str,
        conversation_history: List[Dict],
        customer_data: Dict,
        intent_result: Any,
        rag_need_retrieval: bool,
        rag_complexity: float,
        record_learning_match: bool,
        reply_objective: Optional[Any] = None,
    ) -> Tuple[Any, Dict[str, Any], List[Dict]]:
        matched_knowledge = None
        reply_analysis: Dict[str, Any] = {}
        retrieved_contexts: List[Dict] = []
        enterprise_id = service._resolve_enterprise_id(customer_data, conversation_history)
        schema_id = str((customer_data or {}).get("schema_id") or "").strip()
        # 修复：兼容 IntentResult.primaryIntent（camelCase）和 UnifiedIntentResult.primary_intent（snake_case）
        intent_primary = (
            getattr(intent_result, "primaryIntent", None)
            or getattr(intent_result, "primary_intent", None)
        )
        intent_value = str(getattr(intent_primary, "value", "") or "")
        reply_analysis["retrieval"] = {
            "engine": "unified_knowledge_service",
            "query": search_query,
            "requested": rag_need_retrieval,
            "complexity": rag_complexity,
            "schema_id": schema_id,
            "branch": "retrieval" if rag_need_retrieval else str((customer_data or {}).get("_reply_branch") or "pure_llm"),
        }
        if reply_objective:
            reply_analysis["retrieval"]["conversion_stage"] = reply_objective.conversion_stage.value
            reply_analysis["retrieval"]["next_best_action"] = reply_objective.next_best_action.value

        if not rag_need_retrieval:
            reply_analysis["retrieval"]["skipped"] = True
            reply_analysis["retrieval"]["skip_reason"] = str(
                (customer_data or {}).get("_skip_mainline_retrieval_reason") or "general_qa"
            ).strip() or "general_qa"
            reply_analysis["retrieval"]["final_action"] = "skipped"
            return matched_knowledge, reply_analysis, retrieved_contexts

        perf_metrics: Dict[str, float] = {}
        business_stage = reply_objective.conversion_stage.value if reply_objective else ""
        stage_started_at = time.perf_counter()
        knowledge_results = self._call_search_knowledge(
            service,
            query=search_query,
            top_k=10,
            business_stage=business_stage,
            intent=intent_value,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        service._record_perf_metric(perf_metrics, "search_knowledge", stage_started_at)
        stage_started_at = time.perf_counter()
        evidence_bundle = self._call_search_knowledge_bundle(
            service,
            query=search_query,
            business_stage=business_stage,
            intent=intent_value,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        service._record_perf_metric(perf_metrics, "search_knowledge_bundle", stage_started_at)
        exact_match = service._find_exact_knowledge_match(
            message,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        ) or service._find_exact_knowledge_match(
            search_query,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        if exact_match:
            reordered_results = [
                (item, score)
                for item, score in knowledge_results
                if getattr(item, "id", None) != getattr(exact_match, "id", None)
            ]
            exact_score = max([score for _, score in knowledge_results], default=120.0) + 1000.0
            knowledge_results = [(exact_match, exact_score)] + reordered_results

        if knowledge_results:
            best_match, score = knowledge_results[0]
            matched_knowledge = best_match

            for item, item_score in knowledge_results[:3]:
                retrieved_contexts.append(
                    {
                        "question": item.question,
                        "answer": item.answer,
                        "category": item.category,
                        "schema_id": str(getattr(item, "schema_id", "") or ""),
                        "enterprise_id": enterprise_id,
                        "score": item_score,
                        "source": "knowledge_base",
                        "retrieval_method": "unified_knowledge_service.search",
                        "knowledge_type": service._infer_knowledge_type(item),
                    }
                )

            logger.info(f"知识库检索: 匹配='{best_match.question}', 分数={score:.1f}, 结果数={len(knowledge_results)}")

            reply_analysis["retrieval"].update(
                {
                    "matched": True,
                    "result_count": len(knowledge_results),
                    "top_score": score,
                    "exact_match": bool(
                        exact_match
                        and getattr(best_match, "id", None) == getattr(exact_match, "id", None)
                    ),
                }
            )
            if evidence_bundle:
                reply_analysis["retrieval"]["structured_bundle"] = True
                if hasattr(evidence_bundle, "to_metadata"):
                    reply_analysis["retrieval"]["bundle_metadata"] = evidence_bundle.to_metadata()
                answer_plan = service._build_bundle_answer_plan(message, evidence_bundle)
                if answer_plan:
                    reply_analysis["retrieval"]["answer_plan"] = answer_plan
            if record_learning_match:
                service._learning_engine.on_knowledge_matched_async(
                    message,
                    knowledge_results[:3],
                    intent=intent_value,
                )
        else:
            reply_analysis["retrieval"].update(
                {
                    "matched": False,
                    "result_count": 0,
                }
            )
            if evidence_bundle and hasattr(evidence_bundle, "to_metadata"):
                reply_analysis["retrieval"]["structured_bundle"] = True
                reply_analysis["retrieval"]["bundle_metadata"] = evidence_bundle.to_metadata()
                answer_plan = service._build_bundle_answer_plan(message, evidence_bundle)
                if answer_plan:
                    reply_analysis["retrieval"]["answer_plan"] = answer_plan

        # P2-8: 多意图并行检索（次要意图补充检索）
        # 兼容 IntentResult.secondaryIntents（List[IntentType]）
        # 与 UnifiedIntentResult.metadata["secondary_intents"]（List[str] 值）
        secondary_intents: List[IntentType] = []
        if intent_result is not None:
            raw_secondary = getattr(intent_result, "secondaryIntents", None)
            if raw_secondary:
                secondary_intents = list(raw_secondary)
            else:
                meta = getattr(intent_result, "metadata", None)
                if isinstance(meta, dict):
                    raw_str_list = meta.get("secondary_intents") or []
                    for sval in raw_str_list:
                        try:
                            secondary_intents.append(IntentType(sval))
                        except ValueError:
                            logger.debug(f"次要意图值无法解析为 IntentType: {sval}")
        if secondary_intents:
            existing_questions = {
                str(ctx.get("question") or "").strip().lower()
                for ctx in retrieved_contexts
                if isinstance(ctx, dict)
            }
            secondary_contexts_added = 0
            for sec_intent in secondary_intents[:3]:
                sec_intent_value = str(getattr(sec_intent, "value", "") or "")
                if not sec_intent_value or sec_intent_value == intent_value:
                    continue
                try:
                    sec_results = self._call_search_knowledge(
                        service,
                        query=search_query,
                        top_k=5,
                        business_stage=business_stage,
                        intent=sec_intent_value,
                        enterprise_id=enterprise_id,
                        schema_id=schema_id,
                    )
                    for item, item_score in sec_results[:2]:
                        q_key = str(getattr(item, "question", "") or "").strip().lower()
                        if q_key and q_key not in existing_questions:
                            existing_questions.add(q_key)
                            retrieved_contexts.append(
                                {
                                    "question": item.question,
                                    "answer": item.answer,
                                    "category": item.category,
                                    "schema_id": str(getattr(item, "schema_id", "") or ""),
                                    "enterprise_id": enterprise_id,
                                    "score": item_score * 0.8,  # 次要意图降权
                                    "source": "knowledge_base_secondary_intent",
                                    "retrieval_method": "multi_intent_search",
                                    "knowledge_type": service._infer_knowledge_type(item),
                                    "secondary_intent": sec_intent_value,
                                }
                            )
                            secondary_contexts_added += 1
                except Exception as sec_exc:
                    logger.debug(f"次要意图检索失败 ({sec_intent_value}): {sec_exc}")

            if secondary_contexts_added > 0:
                reply_analysis["retrieval"]["multi_intent"] = True
                reply_analysis["retrieval"]["secondary_intents"] = [
                    str(getattr(si, "value", "") or "") for si in secondary_intents
                ]
                reply_analysis["retrieval"]["secondary_contexts_added"] = secondary_contexts_added
                logger.info(
                    f"多意图检索: 主意图={intent_value}, "
                    f"次要意图={[str(getattr(si, 'value', '') or '') for si in secondary_intents]}, "
                    f"补充 {secondary_contexts_added} 条上下文"
                )

        retrieved_contexts = service._append_bundle_contexts(retrieved_contexts, evidence_bundle, message=message)
        observability_contexts = copy.deepcopy(retrieved_contexts[:3])
        pre_filter_count = len(retrieved_contexts)
        stage_started_at = time.perf_counter()
        retrieved_contexts = service._crag_filter_retrieval(message, retrieved_contexts)
        service._record_perf_metric(perf_metrics, "crag_filter_initial", stage_started_at)
        initial_gate = service._decide_crag_action(message, retrieved_contexts)
        if reply_analysis["retrieval"].get("exact_match") and retrieved_contexts:
            initial_gate = {
                **initial_gate,
                "action": "pass",
                "reason": "exact_question_or_alias",
                "top_relevance": max(initial_gate.get("top_relevance", 0.0), 1.0),
            }
        reply_analysis["retrieval"]["pre_crag_count"] = pre_filter_count
        reply_analysis["retrieval"]["after_crag_count"] = len(retrieved_contexts)
        reply_analysis["retrieval"]["gate"] = {
            "initial_action": initial_gate["action"],
            "initial_reason": initial_gate["reason"],
            "top_relevance": initial_gate["top_relevance"],
            "avg_relevance": initial_gate["avg_relevance"],
            "pass_count": initial_gate["pass_count"],
        }

        should_try_rewrite = service._should_try_crag_rewrite(message, conversation_history)
        reply_analysis["retrieval"]["rewrite_attempted"] = False

        if initial_gate["action"] == "retry" and should_try_rewrite:
            logger.info("CRAG门控: 当前证据较弱，尝试查询改写")
            reply_analysis["retrieval"]["rewrite_attempted"] = True
            stage_started_at = time.perf_counter()
            rewritten_contexts = service._crag_rewrite_and_retrieve(
                message,
                conversation_history,
                intent=intent_value,
                enterprise_id=enterprise_id,
                business_stage=business_stage,
                schema_id=schema_id,
            )
            service._record_perf_metric(perf_metrics, "crag_rewrite_retrieve", stage_started_at)
            stage_started_at = time.perf_counter()
            rewritten_contexts = service._crag_filter_retrieval(message, rewritten_contexts)
            service._record_perf_metric(perf_metrics, "crag_filter_rewrite", stage_started_at)
            rewritten_gate = service._decide_crag_action(message, rewritten_contexts)
            if rewritten_contexts:
                observability_contexts = copy.deepcopy(rewritten_contexts[:3])
            reply_analysis["retrieval"]["rewrite_retry"] = True
            reply_analysis["retrieval"]["gate"]["rewrite_action"] = rewritten_gate["action"]
            reply_analysis["retrieval"]["gate"]["rewrite_reason"] = rewritten_gate["reason"]
            reply_analysis["retrieval"]["gate"]["rewrite_top_relevance"] = rewritten_gate["top_relevance"]

            if rewritten_gate["action"] == "pass":
                retrieved_contexts = rewritten_contexts
                final_gate = rewritten_gate
            else:
                if initial_gate.get("action") == "no_answer":
                    final_gate = initial_gate
                elif retrieved_contexts:
                    final_gate = {
                        **initial_gate,
                        "action": "retry",
                        "reason": "rewrite_failed_preserve_initial",
                    }
                else:
                    final_gate = {
                        **initial_gate,
                        "action": "no_answer",
                        "reason": "rewrite_failed_evidence_insufficient",
                    }
        elif initial_gate["action"] == "retry":
            reply_analysis["retrieval"]["rewrite_retry"] = False
            reply_analysis["retrieval"]["gate"]["rewrite_action"] = "skipped"
            reply_analysis["retrieval"]["gate"]["rewrite_reason"] = "rewrite_not_needed"
            if retrieved_contexts:
                final_gate = initial_gate
            else:
                final_gate = {
                    **initial_gate,
                    "action": "no_answer",
                    "reason": "empty_contexts",
                }
        elif initial_gate["action"] == "no_answer":
            logger.info("CRAG门控: 证据不足，直接进入拒答策略")
            reply_analysis["retrieval"]["rewrite_retry"] = False
            retrieved_contexts = []
            final_gate = initial_gate
        else:
            reply_analysis["retrieval"]["rewrite_retry"] = False
            final_gate = initial_gate

        # P1-4: no_answer 时尝试 Web 搜索兜底
        if final_gate["action"] == "no_answer":
            web_contexts = service._try_web_search_fallback(
                query=message,
                top_relevance=float(final_gate.get("top_relevance") or 0.0),
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
            if web_contexts:
                retrieved_contexts = web_contexts
                final_gate = {
                    **final_gate,
                    "action": "retry",
                    "reason": "web_search_fallback",
                }
                reply_analysis["retrieval"]["web_search_fallback"] = True
                reply_analysis["retrieval"]["web_search_count"] = len(web_contexts)
                logger.info(
                    f"CRAG no_answer → Web 搜索兜底: query='{message[:30]}', "
                    f"补充 {len(web_contexts)} 条网络证据"
                )

        reply_analysis["retrieval"]["final_action"] = final_gate["action"]
        reply_analysis["retrieval"]["final_reason"] = final_gate["reason"]
        reply_analysis["retrieval"]["final_top_relevance"] = final_gate["top_relevance"]
        reply_analysis["retrieval"]["enterprise_id"] = enterprise_id
        if observability_contexts:
            reply_analysis["retrieval"]["contexts"] = observability_contexts
        reply_analysis["retrieval"]["perf"] = copy.deepcopy(perf_metrics)
        if perf_metrics.get("search_knowledge") is not None:
            service._append_trace_span(
                reply_analysis,
                name="retrieval_search",
                duration_ms=perf_metrics["search_knowledge"],
                metadata={"engine": "unified_knowledge_service.search"},
            )
        if perf_metrics.get("crag_filter_initial") is not None:
            service._append_trace_span(
                reply_analysis,
                name="crag_filter_initial",
                duration_ms=perf_metrics["crag_filter_initial"],
                metadata={"action": initial_gate["action"]},
            )
        if perf_metrics.get("crag_rewrite_retrieve") is not None:
            service._append_trace_span(
                reply_analysis,
                name="crag_rewrite_retrieve",
                duration_ms=perf_metrics["crag_rewrite_retrieve"],
                metadata={"rewrite_attempted": reply_analysis["retrieval"].get("rewrite_attempted", False)},
            )
        if perf_metrics.get("crag_filter_rewrite") is not None:
            service._append_trace_span(
                reply_analysis,
                name="crag_filter_rewrite",
                duration_ms=perf_metrics["crag_filter_rewrite"],
                metadata={"rewrite_retry": reply_analysis["retrieval"].get("rewrite_retry", False)},
            )
        service._append_trace_span(
            reply_analysis,
            name="retrieval_chain",
            duration_ms=sum(perf_metrics.values()),
            metadata={
                "final_action": final_gate["action"],
                "result_count": reply_analysis["retrieval"].get("result_count", 0),
            },
        )
        service._log_perf_metrics(
            "retrieval_chain",
            perf_metrics,
            query=search_query,
            final_action=final_gate["action"],
            result_count=reply_analysis["retrieval"].get("result_count"),
        )

        return matched_knowledge, reply_analysis, retrieved_contexts
