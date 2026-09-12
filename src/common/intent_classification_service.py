"""
知识画像辅助意图分类服务

在不破坏现有规则识别主链的前提下，为通用行业场景提供基于 DomainProfile 的意图加权能力。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from src.common.domain_profile_service import DomainProfileService, get_domain_profile_service
from src.common.types.intent import IntentType
from src.common.types.knowledge_profile import DomainProfile


class IntentClassificationService:
    POLICY_INTENT_MAP = {
        "price": IntentType.PRICE_INQUIRY,
        "refund": IntentType.REFUND_REQUEST,
        "service": IntentType.SERVICE_INQUIRY,
        "contact": IntentType.CONTACT_INQUIRY,
        "process": IntentType.PURCHASE_INTENT,
    }

    GENERIC_HINTS = {
        IntentType.PRODUCT_INQUIRY: ["什么", "介绍", "功能", "内容", "适合", "有哪些", "做什么"],
        IntentType.PRICE_INQUIRY: ["价格", "费用", "报价", "学费", "多少钱", "收费"],
        IntentType.CONTACT_INQUIRY: ["微信", "电话", "联系方式", "联系", "怎么找"],
        IntentType.PURCHASE_INTENT: ["报名", "预约", "下单", "购买", "开通", "签约", "试听"],
        IntentType.SERVICE_INQUIRY: ["怎么", "流程", "使用", "支持", "售后", "教程", "安排"],
        IntentType.COMPARISON: ["哪个好", "区别", "对比", "怎么选", "选哪个"],
    }

    def __init__(self, domain_profile_service: Optional[DomainProfileService] = None):
        self.domain_profile_service = domain_profile_service or get_domain_profile_service()

    def build_profile_intent_scores(
        self,
        *,
        message: str,
        enterprise_id: Optional[str] = None,
    ) -> Dict[IntentType, float]:
        if not enterprise_id:
            return {}
        profile = self.domain_profile_service.get_profile(enterprise_id)
        if not profile or (
            not profile.products and not profile.policies and not profile.faq_signals and not profile.summary
        ):
            return {}

        normalized_message = str(message or "").strip().lower()
        if not normalized_message:
            return {}

        scores: Dict[IntentType, float] = {}
        self._apply_product_signals(profile, normalized_message, scores)
        self._apply_policy_signals(profile, normalized_message, scores)
        self._apply_faq_signals(profile, normalized_message, scores)
        self._apply_sales_actions(profile, normalized_message, scores)
        return scores

    def build_prompt_context(self, enterprise_id: Optional[str] = None) -> str:
        if not enterprise_id:
            return ""
        profile = self.domain_profile_service.get_profile(enterprise_id)
        if not profile or (
            not profile.products and not profile.policies and not profile.faq_signals and not profile.summary
        ):
            return ""

        product_names = [product.name for product in profile.products[:5] if product.name]
        policy_types = [policy.rule_type for policy in profile.policies[:5] if policy.rule_type]
        faq_hints = [signal.intent_hint for signal in profile.faq_signals[:5] if signal.intent_hint]

        parts = []
        if profile.industry and profile.industry != "general":
            parts.append(f"知识画像行业: {profile.industry}")
        if product_names:
            parts.append(f"知识画像产品/服务: {product_names}")
        if policy_types:
            parts.append(f"知识画像规则类型: {policy_types}")
        if faq_hints:
            parts.append(f"高频问题意图: {faq_hints}")
        if profile.summary:
            parts.append(f"知识画像摘要: {profile.summary[:200]}")
        return "\n".join(parts)

    def _apply_product_signals(
        self,
        profile: DomainProfile,
        message: str,
        scores: Dict[IntentType, float],
    ) -> None:
        for product in profile.products:
            terms = [product.name] + list(product.aliases or [])
            matched_terms = [term for term in terms if term and term.lower() in message]
            if not matched_terms:
                continue
            scores[IntentType.PRODUCT_INQUIRY] = scores.get(IntentType.PRODUCT_INQUIRY, 0.0) + 1.2

            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.PRICE_INQUIRY]):
                scores[IntentType.PRICE_INQUIRY] = scores.get(IntentType.PRICE_INQUIRY, 0.0) + 1.0
            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.CONTACT_INQUIRY]):
                scores[IntentType.CONTACT_INQUIRY] = scores.get(IntentType.CONTACT_INQUIRY, 0.0) + 0.9
            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.PURCHASE_INTENT]):
                scores[IntentType.PURCHASE_INTENT] = scores.get(IntentType.PURCHASE_INTENT, 0.0) + 1.1
            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.COMPARISON]):
                scores[IntentType.COMPARISON] = scores.get(IntentType.COMPARISON, 0.0) + 1.0

    def _apply_policy_signals(
        self,
        profile: DomainProfile,
        message: str,
        scores: Dict[IntentType, float],
    ) -> None:
        for policy in profile.policies:
            intent = self.POLICY_INTENT_MAP.get(str(policy.rule_type or "").strip().lower())
            if not intent:
                continue
            terms = [policy.title, policy.content] + list(policy.conditions or [])
            haystack = " ".join(str(term or "") for term in terms)
            if not haystack.strip():
                continue
            matched_policy_word = any(
                hint in message for hint in self.GENERIC_HINTS.get(intent, [])
            ) or self._contains_overlap_token(message, haystack)
            if matched_policy_word:
                scores[intent] = scores.get(intent, 0.0) + 0.9

    def _apply_faq_signals(
        self,
        profile: DomainProfile,
        message: str,
        scores: Dict[IntentType, float],
    ) -> None:
        for signal in profile.faq_signals:
            intent = self._intent_from_hint(signal.intent_hint)
            if not intent:
                continue
            terms = [signal.question, signal.answer_summary] + list(signal.keywords or [])
            haystack = " ".join(str(term or "") for term in terms).lower()
            if self._contains_overlap_token(message, haystack):
                scores[intent] = scores.get(intent, 0.0) + 1.15

    def _apply_sales_actions(
        self,
        profile: DomainProfile,
        message: str,
        scores: Dict[IntentType, float],
    ) -> None:
        sales_text = " ".join(profile.sales_actions or [])
        if sales_text:
            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.PURCHASE_INTENT]):
                scores[IntentType.PURCHASE_INTENT] = scores.get(IntentType.PURCHASE_INTENT, 0.0) + 0.6
            if any(hint in message for hint in self.GENERIC_HINTS[IntentType.CONTACT_INQUIRY]):
                scores[IntentType.CONTACT_INQUIRY] = scores.get(IntentType.CONTACT_INQUIRY, 0.0) + 0.5

    @staticmethod
    def _contains_overlap_token(message: str, haystack: str) -> bool:
        tokens = [
            token.strip().lower()
            for token in re.split(r"[\s,，。；;、:/\-\(\)]+", str(haystack or ""))
            if len(token.strip()) >= 2
        ]
        unique_tokens = list(dict.fromkeys(tokens))
        return any(token in message for token in unique_tokens[:20])

    @staticmethod
    def _intent_from_hint(intent_hint: str) -> Optional[IntentType]:
        normalized = str(intent_hint or "").strip().lower()
        for intent in IntentType:
            if intent.value == normalized:
                return intent
        return None
