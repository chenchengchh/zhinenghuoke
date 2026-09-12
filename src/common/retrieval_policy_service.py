"""
按意图路由的检索策略服务

根据当前意图、企业知识画像和业务阶段，生成统一检索计划。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.common.domain_profile_service import DomainProfileService, get_domain_profile_service
from src.common.industry_schema_service import IndustrySchemaService, get_industry_schema_service
from src.common.types.knowledge_profile import DomainProfile


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


DEFAULT_RETRIEVAL_POLICY: Dict[str, Any] = {
    "intent_category_map": {
        "product_inquiry": ["product", "faq", "service"],
        "price_inquiry": ["price", "promotion", "faq", "policy"],
        "contact_inquiry": ["service", "faq", "cooperation"],
        "purchase_intent": ["service", "product", "faq", "policy"],
        "service_inquiry": ["service", "faq", "policy"],
        "comparison": ["product", "price", "faq"],
        "refund_request": ["after_sale", "policy", "service"],
        "cooperation_intent": ["cooperation", "service", "faq"],
    },
    "intent_term_hints": {
        "price_inquiry": ["价格", "费用", "报价", "收费"],
        "contact_inquiry": ["联系方式", "微信", "电话", "联系"],
        "purchase_intent": ["预约", "下单", "开通", "购买"],
        "service_inquiry": ["流程", "使用", "支持", "售后"],
        "comparison": ["对比", "区别", "怎么选", "哪个好"],
        "refund_request": ["退款", "退费", "售后", "规则"],
        "cooperation_intent": ["合作", "代理", "加盟", "对接"],
    },
    "enabled_sources": {
        "default": ["knowledge_base", "bm25", "vector"],
        "keyword_only": ["knowledge_base", "bm25"],
        "lexical": ["knowledge_base", "bm25"],
        "fast": ["knowledge_base", "bm25"],
        "vector_only": ["vector"],
        "semantic": ["vector"],
    },
    "per_source_top_k": {
        "default": 10,
        "adaptive": 8,
        "fast": 4,
    },
    "query_type_terms": {
        "comparison": ["区别", "对比", "哪个好", "怎么选"],
        "selection": ["哪种", "哪款", "哪一个", "选哪个"],
        "price": ["价格", "多少钱", "费用", "报价"],
        "itinerary": ["流程", "步骤", "怎么安排", "怎么使用"],
        "inclusion": ["包含", "包括", "含什么", "不含"],
        "contact": ["联系方式", "怎么联系", "联系你", "发我资料"],
        "domain_specific": [],
    },
    "scoring": {
        "priority_factor": 0.5,
        "enterprise_match_boost": 6.0,
        "non_target_category_penalty": -3.0,
        "source_weights": {
            "conversation": -12.0,
            "massive_expansion": -22.0,
            "optimized": 0.0,
            "document_upload": 0.0,
        },
        "source_priority_multipliers": {
            "massive_expansion": -1.5,
        },
        "query_type_source_weights": {
            "comparison": {"massive_expansion": -18.0, "optimized": 6.0, "document_upload": 4.0},
            "selection": {"massive_expansion": -18.0, "optimized": 6.0, "document_upload": 4.0},
            "price": {"conversation": -8.0, "massive_expansion": -18.0},
            "itinerary": {"conversation": -8.0, "massive_expansion": -18.0},
            "domain_specific": {"conversation": -8.0},
        },
        "contact_pollution_weights": {
            "default": -42.0,
            "explicit_contact": -8.0,
            "fact_query_extra": -16.0,
        },
        "query_category_weights": {
            "comparison": {"service": -10.0, "product": 8.0, "price": 8.0, "itinerary": 8.0, "tips": 8.0},
            "price": {"price": 12.0, "itinerary": -5.0},
            "itinerary": {"itinerary": 8.0, "price": -5.0},
        },
        "domain_route_weights": {
            "route_match": 18.0,
            "route_mismatch": -48.0,
            "route_mismatch_core_category": -18.0,
            "route_match_price_category": 12.0,
            "route_match_itinerary_category": 9.0,
            "sales_script_penalty": -20.0,
            "domain_specific_item": 12.0,
            "domain_specific_core_category": 5.0,
            "service_penalty": -5.0,
            "comparison_sales_script_penalty": -15.0,
            "comparison_service_penalty": -10.0,
            "comparison_core_category": 8.0,
            "query_route_extra_match": 30.0,
            "query_route_extra_mismatch": -25.0,
            "price_category": 10.0,
            "price_itinerary_penalty": -5.0,
            "itinerary_category": 10.0,
            "itinerary_price_penalty": -5.0,
        },
        "exact_match": {
            "direct_score": 500.0,
            "comparison_alias_exact": 68.0,
            "comparison_alias_fuzzy": 28.0,
            "fuzzy_enabled_query_types": ["comparison", "selection"],
            "fuzzy_priority_categories": ["tips", "product", "price"],
            "fuzzy_priority_category_bonus": 2,
            "fuzzy_alias_base_score": 6,
            "fuzzy_question_base_score": 4,
            "fuzzy_overlap_base_score": 8,
            "fuzzy_overlap_step_score": 3,
            "fuzzy_min_overlap": 2,
            "fuzzy_extra_terms": [],
            "fuzzy_excluded_terms": ["有什么", "区别", "对比", "哪个好", "怎么选", "选哪条", "线路", "一日游", "哪个"],
        },
    },
}


@dataclass
class RetrievalPlan:
    intent: str = ""
    enterprise_id: str = ""
    schema_id: str = ""
    target_categories: List[str] = field(default_factory=list)
    category_boosts: Dict[str, float] = field(default_factory=dict)
    preferred_terms: List[str] = field(default_factory=list)
    routing_query: str = ""
    requested_mode: str = "standard"
    enabled_sources: List[str] = field(default_factory=lambda: ["knowledge_base", "vector", "bm25"])
    per_source_top_k: int = 0
    enable_query_rewrite: bool = False
    enable_query_expansion: bool = True
    enable_semantic_dedup: bool = True
    enable_relevance_filter: bool = True
    notes: List[str] = field(default_factory=list)
    policy: Dict[str, Any] = field(default_factory=dict)


class RetrievalPolicyService:
    def __init__(
        self,
        domain_profile_service: Optional[DomainProfileService] = None,
        schema_service: Optional[IndustrySchemaService] = None,
    ):
        self.domain_profile_service = domain_profile_service or get_domain_profile_service()
        self.schema_service = schema_service or get_industry_schema_service()

    def get_active_policy(self, enterprise_id: str = "", preferred_schema_id: str = "") -> Dict[str, Any]:
        policy = deepcopy(DEFAULT_RETRIEVAL_POLICY)
        try:
            active_schema = self.schema_service.get_active_schema(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
            ) or {}
            metadata = active_schema.get("metadata") or {}
            schema_policy = metadata.get("retrieval_policy") or {}
            if isinstance(schema_policy, dict):
                policy = _deep_merge_dict(policy, schema_policy)
        except Exception:
            pass
        return policy

    def build_plan(
        self,
        *,
        query: str,
        intent: str = "",
        enterprise_id: str = "",
        business_stage: str = "",
        retrieval_options: Optional[Dict[str, Any]] = None,
    ) -> RetrievalPlan:
        normalized_intent = str(intent or "").strip().lower()
        normalized_enterprise = str(enterprise_id or "").strip()
        profile = self.domain_profile_service.get_profile(normalized_enterprise) if normalized_enterprise else DomainProfile()
        options = retrieval_options or {}
        requested_mode = str(options.get("mode") or "standard").strip().lower() or "standard"
        requested_schema_id = str(
            options.get("schema_id") or options.get("preferred_schema_id") or ""
        ).strip()
        schema_id = requested_schema_id or self.schema_service.get_effective_active_schema_id(
            enterprise_id=normalized_enterprise
        )
        policy = self.get_active_policy(enterprise_id=normalized_enterprise, preferred_schema_id=schema_id)
        intent_category_map = policy.get("intent_category_map") or {}
        intent_term_hints = policy.get("intent_term_hints") or {}

        target_categories = list(intent_category_map.get(normalized_intent, []))
        category_boosts = {
            category: max(6.0, 12.0 - index * 2.0)
            for index, category in enumerate(target_categories)
        }
        preferred_terms = self._build_preferred_terms(profile, normalized_intent, intent_term_hints)
        enable_query_rewrite = bool(options.get("enable_self_query", False))
        routing_query = (
            self._build_routing_query(query, normalized_intent, preferred_terms, business_stage, intent_term_hints)
            if enable_query_rewrite
            else str(query or "").strip()
        )
        notes: List[str] = []
        if target_categories:
            notes.append(f"按意图优先检索类别: {target_categories}")
        if preferred_terms:
            notes.append(f"画像增强词: {preferred_terms[:6]}")

        enabled_source_map = policy.get("enabled_sources") or {}
        enabled_sources = list(enabled_source_map.get("default") or ["knowledge_base", "bm25", "vector"])
        if bool(options.get("allow_vector_init", True)):
            if "vector" not in enabled_sources:
                enabled_sources.append("vector")
        else:
            enabled_sources = [source for source in enabled_sources if source != "vector"]
        if requested_mode in {"vector_only", "semantic"}:
            enabled_sources = list(enabled_source_map.get(requested_mode) or (["vector"] if "vector" in enabled_sources else ["bm25"]))
        elif requested_mode in {"keyword_only", "lexical", "fast"}:
            enabled_sources = list(enabled_source_map.get(requested_mode) or ["knowledge_base", "bm25"])

        enable_query_expansion = bool(options.get("enable_multi_hop", False))
        enable_semantic_dedup = bool(options.get("enable_compression", True))
        enable_relevance_filter = bool(options.get("enable_compression", True))
        per_source_top_k_config = policy.get("per_source_top_k") or {}
        per_source_top_k = int(
            per_source_top_k_config.get(requested_mode)
            or per_source_top_k_config.get("default")
            or (4 if requested_mode == "fast" else 8 if requested_mode == "adaptive" else 10)
        )
        notes.append(f"检索模式: {requested_mode}")
        notes.append(f"启用路由: {enabled_sources}")

        return RetrievalPlan(
            intent=normalized_intent,
            enterprise_id=normalized_enterprise,
            schema_id=schema_id,
            target_categories=target_categories,
            category_boosts=category_boosts,
            preferred_terms=preferred_terms,
            routing_query=routing_query,
            requested_mode=requested_mode,
            enabled_sources=enabled_sources,
            per_source_top_k=per_source_top_k,
            enable_query_rewrite=enable_query_rewrite,
            enable_query_expansion=enable_query_expansion,
            enable_semantic_dedup=enable_semantic_dedup,
            enable_relevance_filter=enable_relevance_filter,
            notes=notes,
            policy=policy,
        )

    def _build_preferred_terms(
        self,
        profile: DomainProfile,
        intent: str,
        intent_term_hints: Dict[str, List[str]],
    ) -> List[str]:
        terms: List[str] = []
        for product in list(profile.products or [])[:5]:
            if product.name:
                terms.append(product.name)
            terms.extend(list(product.aliases or [])[:3])
        for signal in list(profile.faq_signals or [])[:5]:
            if signal.intent_hint == intent or not intent:
                terms.extend(list(signal.keywords or [])[:4])
        for policy in list(profile.policies or [])[:5]:
            if policy.rule_type:
                terms.append(policy.rule_type)
            if policy.title:
                terms.append(policy.title)
        for hint in intent_term_hints.get(intent, []):
            terms.append(hint)

        deduped: List[str] = []
        seen = set()
        for term in terms:
            normalized = str(term or "").strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped[:10]

    def _build_routing_query(
        self,
        query: str,
        intent: str,
        preferred_terms: List[str],
        business_stage: str,
        intent_term_hints: Dict[str, List[str]],
    ) -> str:
        base_query = str(query or "").strip()
        if not base_query:
            return ""
        hints = []
        for hint in intent_term_hints.get(intent, []):
            if hint not in base_query:
                hints.append(hint)
        for term in preferred_terms:
            if term not in base_query:
                hints.append(term)
            if len(hints) >= 4:
                break
        if business_stage and business_stage not in base_query:
            hints.append(business_stage)
        if not hints:
            return base_query
        return f"{base_query} {' '.join(hints[:4])}".strip()
