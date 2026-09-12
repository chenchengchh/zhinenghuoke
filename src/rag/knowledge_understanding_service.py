"""
知识理解服务

上传文档后生成企业级知识画像，作为后续意图识别、检索路由和回复约束的基础产物。
"""
import inspect
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

from src.common.domain_profile_service import DomainProfileService, get_domain_profile_service
from src.common.types.knowledge_profile import DomainProfile, FAQSignal, PolicyRule, ProductProfile


class KnowledgeUnderstandingService:
    PROFILE_PROMPT = """你是企业知识理解助手。请根据下面的知识内容，为企业生成结构化知识画像。

企业ID: {enterprise_id}
文件名: {document_name}
知识内容:
{content}

请严格输出 JSON，字段如下：
{{
  "industry": "行业",
  "sub_industry": "细分行业",
  "tone": "建议回复语气",
  "summary": "知识库摘要",
  "products": [
    {{
      "name": "产品或服务名",
      "aliases": ["别名"],
      "summary": "一句话介绍",
      "features": ["特点"],
      "price_notes": ["价格信息"],
      "audience": ["适用人群"],
      "scenarios": ["适用场景"]
    }}
  ],
  "policies": [
    {{
      "rule_type": "price/refund/service/contact/process/other",
      "title": "规则标题",
      "content": "规则内容",
      "conditions": ["触发条件"]
    }}
  ],
  "faq_signals": [
    {{
      "question": "典型问题",
      "intent_hint": "product_inquiry/price_inquiry/service_inquiry/contact_inquiry/purchase_intent/comparison/consultation",
      "keywords": ["关键词"],
      "answer_summary": "答案摘要"
    }}
  ],
  "sales_actions": ["可推进动作"],
  "forbidden_claims": ["不应乱承诺的说法"]
}}
"""

    INDUSTRY_HINTS = {
        "tourism": ["旅游", "线路", "景点", "跟团", "一日游"],
        "education": ["课程", "学费", "试听", "培训", "上课", "老师", "班型"],
        "software": ["系统", "功能", "接口", "账号", "开通", "部署", "SaaS", "软件"],
        "retail": ["商品", "发货", "快递", "库存", "退货", "售后", "下单"],
        "service_sales": ["方案", "服务", "报价", "签约", "合作", "对接", "咨询"],
    }
    POLICY_HINTS = {
        "price": ["价格", "费用", "报价", "学费", "收费", "套餐", "预算", "多少钱"],
        "refund": ["退款", "退费", "退票", "取消", "退订"],
        "service": ["售后", "支持", "保修", "服务", "培训", "教程"],
        "contact": ["微信", "电话", "联系方式", "联系", "对接人"],
        "process": ["流程", "步骤", "怎么开通", "怎么报名", "怎么预约", "怎么下单", "如何办理"],
    }
    INTENT_HINTS = {
        "price_inquiry": ["价格", "费用", "报价", "多少钱", "学费"],
        "contact_inquiry": ["微信", "电话", "联系方式", "联系"],
        "purchase_intent": ["报名", "预约", "下单", "购买", "开通", "签约"],
        "comparison": ["哪个好", "怎么选", "区别", "对比"],
        "service_inquiry": ["售后", "支持", "怎么用", "教程", "流程"],
        "product_inquiry": ["是什么", "介绍", "功能", "内容", "路线", "产品"],
    }
    PRODUCT_PATTERNS = (
        r"([A-Za-z0-9\u4e00-\u9fa5]{2,20}(?:产品|服务|系统|方案|套餐|课程|线路))",
    )

    def __init__(self, llm_service=None, domain_profile_service: Optional[DomainProfileService] = None):
        self.llm_service = llm_service
        self.domain_profile_service = domain_profile_service or get_domain_profile_service()

    def _get_active_schema(
        self,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ):
        try:
            from src.common.industry_schema_service import IndustrySchemaService
            return IndustrySchemaService().get_active_schema(
                enterprise_id=str(enterprise_id or "").strip(),
                preferred_schema_id=str(schema_id or "").strip(),
            ) or {}
        except Exception:
            return {}

    async def build_and_store_profile(
        self,
        *,
        enterprise_id: str,
        document_name: str,
        content: str,
        extracted_items: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DomainProfile:
        rule_profile = self._build_rule_profile(
            enterprise_id=enterprise_id,
            document_name=document_name,
            content=content,
            extracted_items=extracted_items or [],
            metadata=metadata or {},
        )
        llm_profile = await self._build_llm_profile(
            enterprise_id=enterprise_id,
            document_name=document_name,
            content=content,
        )
        if llm_profile:
            rule_profile = self._merge_profiles(rule_profile, llm_profile)
        return self.domain_profile_service.merge_profile(enterprise_id, rule_profile)

    async def _build_llm_profile(self, *, enterprise_id: str, document_name: str, content: str) -> Optional[DomainProfile]:
        llm = self.llm_service or self._get_llm_service()
        if not llm or not str(content or "").strip():
            return None
        prompt = self.PROFILE_PROMPT.format(
            enterprise_id=enterprise_id,
            document_name=document_name,
            content=str(content)[:5000],
        )
        try:
            response = llm.generate(prompt)
            if inspect.isawaitable(response):
                response = await response
            parsed = self._parse_llm_profile_response(str(response or ""), enterprise_id)
            return parsed
        except Exception as exc:
            logger.warning(f"LLM 生成知识画像失败，使用规则画像: {exc}")
            return None

    def _get_llm_service(self):
        if self.llm_service is not None:
            return self.llm_service
        try:
            from src.common.llm_service import get_default_llm_provider
            self.llm_service = get_default_llm_provider()
            return self.llm_service
        except Exception as exc:
            logger.warning(f"获取默认 LLM 服务失败: {exc}")
            return None

    def _parse_llm_profile_response(self, response: str, enterprise_id: str) -> Optional[DomainProfile]:
        try:
            match = re.search(r"\{[\s\S]*\}", response)
            if not match:
                return None
            data = json.loads(match.group(0))
            return DomainProfile(
                enterprise_id=enterprise_id,
                industry=str(data.get("industry", "general") or "general"),
                sub_industry=str(data.get("sub_industry", "") or ""),
                tone=str(data.get("tone", "专业顾问") or "专业顾问"),
                summary=str(data.get("summary", "") or ""),
                products=[ProductProfile.from_dict(item) for item in list(data.get("products", []) or [])],
                policies=[PolicyRule.from_dict(item) for item in list(data.get("policies", []) or [])],
                faq_signals=[FAQSignal.from_dict(item) for item in list(data.get("faq_signals", []) or [])],
                sales_actions=list(data.get("sales_actions", []) or []),
                forbidden_claims=list(data.get("forbidden_claims", []) or []),
                metadata={"source": "llm_profile"},
            )
        except Exception as exc:
            logger.warning(f"解析 LLM 知识画像失败: {exc}")
            return None

    def _build_rule_profile(
        self,
        *,
        enterprise_id: str,
        document_name: str,
        content: str,
        extracted_items: List[Dict[str, Any]],
        metadata: Dict[str, Any],
    ) -> DomainProfile:
        text = str(content or "").strip()
        items = list(extracted_items or [])
        industry = self._infer_industry(text, metadata)
        products = self._extract_products(
            text,
            items,
            document_name,
            enterprise_id=enterprise_id,
            schema_id=str(metadata.get("schema_id") or ""),
        )
        policies = self._extract_policies(text, items)
        faq_signals = self._extract_faq_signals(items)
        sales_actions = self._extract_sales_actions(text, items)
        forbidden_claims = self._extract_forbidden_claims(text)
        summary = self._build_summary(industry, document_name, products, policies, faq_signals)

        return DomainProfile(
            enterprise_id=enterprise_id,
            industry=industry,
            sub_industry=str(metadata.get("category", "") or ""),
            tone="专业顾问",
            summary=summary,
            products=products,
            policies=policies,
            faq_signals=faq_signals,
            sales_actions=sales_actions,
            forbidden_claims=forbidden_claims,
            metadata={
                "source": "rule_profile",
                "document_name": document_name,
                "original_category": metadata.get("category", ""),
            },
        )

    def _infer_industry(self, text: str, metadata: Dict[str, Any]) -> str:
        category = str(metadata.get("category", "") or "").lower()
        if category in {"course", "education"}:
            return "education"
        if category in {"software", "process", "service"} and "系统" in text:
            return "software"
        lowered = text.lower()
        best_industry = "general"
        best_score = 0
        for industry, keywords in self.INDUSTRY_HINTS.items():
            score = sum(1 for keyword in keywords if keyword.lower() in lowered)
            if score > best_score:
                best_industry = industry
                best_score = score
        return best_industry

    def _extract_products(
        self,
        text: str,
        extracted_items: List[Dict[str, Any]],
        document_name: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> List[ProductProfile]:
        candidates: List[str] = []
        all_patterns = list(self.PRODUCT_PATTERNS)
        schema = self._get_active_schema(enterprise_id=enterprise_id, schema_id=schema_id)
        try:
            anchor_terms = schema.get("metadata", {}).get("followup_strategy", {}).get("anchor_terms") or []
            if anchor_terms:
                escaped = [re.escape(term) for term in anchor_terms]
                all_patterns.append(r"(" + "|".join(escaped) + ")")
        except Exception:
            pass
        for pattern in all_patterns:
            candidates.extend(match.strip() for match in re.findall(pattern, text))
        for item in extracted_items:
            question = str(item.get("question", "") or "")
            candidates.extend(match.strip() for pattern in self.PRODUCT_PATTERNS for match in re.findall(pattern, question))

        normalized: List[str] = []
        for item in candidates:
            if item and item not in normalized:
                normalized.append(item)
        if not normalized:
            stem = os.path.splitext(str(document_name or "").strip())[0]
            if stem:
                normalized.append(stem)

        products: List[ProductProfile] = []
        for name in normalized[:5]:
            related_answers = [
                str(item.get("answer", "") or "")
                for item in extracted_items
                if name in str(item.get("question", "") or "") or name in str(item.get("answer", "") or "")
            ]
            products.append(
                ProductProfile(
                    name=name,
                    summary=related_answers[0][:120] if related_answers else "",
                    features=self._extract_feature_points(related_answers),
                    price_notes=self._extract_price_points(related_answers),
                )
            )
        return products

    def _extract_policies(self, text: str, extracted_items: List[Dict[str, Any]]) -> List[PolicyRule]:
        policies: List[PolicyRule] = []
        seen = set()
        for item in extracted_items:
            question = str(item.get("question", "") or "")
            answer = str(item.get("answer", "") or "")
            combined = f"{question} {answer}"
            rule_type = self._infer_policy_type(combined)
            if not rule_type:
                continue
            key = f"{rule_type}:{question}".lower()
            if key in seen:
                continue
            seen.add(key)
            policies.append(
                PolicyRule(
                    rule_type=rule_type,
                    title=question[:60],
                    content=answer[:200],
                    conditions=self._extract_conditions(answer),
                )
            )
        if not policies:
            inferred_type = self._infer_policy_type(text)
            if inferred_type:
                policies.append(PolicyRule(rule_type=inferred_type, title=f"{inferred_type}_policy", content=text[:200]))
        return policies[:8]

    def _extract_faq_signals(self, extracted_items: List[Dict[str, Any]]) -> List[FAQSignal]:
        signals: List[FAQSignal] = []
        for item in extracted_items[:12]:
            question = str(item.get("question", "") or "").strip()
            answer = str(item.get("answer", "") or "").strip()
            if len(question) < 3 or len(answer) < 5:
                continue
            signals.append(
                FAQSignal(
                    question=question,
                    intent_hint=self._infer_intent_hint(question, answer),
                    keywords=list(item.get("keywords", []) or [])[:5],
                    answer_summary=answer[:120],
                )
            )
        return signals

    def _extract_sales_actions(self, text: str, extracted_items: List[Dict[str, Any]]) -> List[str]:
        actions = []
        combined = f"{text}\n" + "\n".join(
            f"{item.get('question', '')} {item.get('answer', '')}" for item in extracted_items[:20]
        )
        if any(keyword in combined for keyword in ["联系方式", "微信", "电话", "对接"]):
            actions.append("高意向用户可引导留资或建立联系")
        if any(keyword in combined for keyword in ["报名", "预约", "下单", "开通", "签约"]):
            actions.append("客户确认需求后可推进报名、预约、下单或签约")
        if any(keyword in combined for keyword in ["时间", "日期", "人数", "预算"]):
            actions.append("信息不足时优先补齐时间、人数、预算等关键槽位")
        return actions

    def _extract_forbidden_claims(self, text: str) -> List[str]:
        forbidden = []
        if "保证" in text:
            forbidden.append("避免未经证据支持的绝对保证类承诺")
        if "最低价" in text or "最便宜" in text:
            forbidden.append("避免承诺绝对最低价")
        if "包过" in text or "百分百" in text:
            forbidden.append("避免承诺百分百结果")
        return forbidden

    def _build_summary(
        self,
        industry: str,
        document_name: str,
        products: List[ProductProfile],
        policies: List[PolicyRule],
        faq_signals: List[FAQSignal],
    ) -> str:
        parts = [f"行业画像：{industry}"]
        if document_name:
            parts.append(f"来源文档：{document_name}")
        if products:
            parts.append(f"主要产品/服务：{', '.join(item.name for item in products[:3])}")
        if policies:
            parts.append(f"已抽取规则：{', '.join(item.rule_type for item in policies[:4])}")
        if faq_signals:
            parts.append(f"典型问题数：{len(faq_signals)}")
        return "；".join(parts)

    def _extract_feature_points(self, answers: Iterable[str]) -> List[str]:
        features = []
        for answer in answers:
            sentences = [segment.strip() for segment in re.split(r"[。；;\n]+", answer) if segment.strip()]
            for sentence in sentences[:3]:
                if sentence not in features:
                    features.append(sentence[:60])
        return features[:5]

    def _extract_price_points(self, answers: Iterable[str]) -> List[str]:
        prices = []
        for answer in answers:
            matches = re.findall(r"([零一二两三四五六七八九十百千\d]+(?:元|块|万|/人|/位|/课时))", answer)
            for match in matches:
                if match not in prices:
                    prices.append(match)
        return prices[:5]

    def _infer_policy_type(self, text: str) -> str:
        lowered = str(text or "").lower()
        best_type = ""
        best_score = 0
        for rule_type, keywords in self.POLICY_HINTS.items():
            score = sum(1 for keyword in keywords if keyword.lower() in lowered)
            if score > best_score:
                best_type = rule_type
                best_score = score
        return best_type

    def _infer_intent_hint(self, question: str, answer: str) -> str:
        text = f"{question} {answer}"
        for intent_hint, keywords in self.INTENT_HINTS.items():
            if any(keyword in text for keyword in keywords):
                return intent_hint
        return "consultation"

    @staticmethod
    def _extract_conditions(answer: str) -> List[str]:
        fragments = [frag.strip() for frag in re.split(r"[，,；;。]", str(answer or "")) if frag.strip()]
        return fragments[:3]

    def _merge_profiles(self, current: DomainProfile, incoming: DomainProfile) -> DomainProfile:
        return DomainProfile(
            enterprise_id=current.enterprise_id,
            industry=incoming.industry if incoming.industry and incoming.industry != "general" else current.industry,
            sub_industry=incoming.sub_industry or current.sub_industry,
            tone=incoming.tone or current.tone,
            summary=self.domain_profile_service._merge_text(current.summary, incoming.summary),
            products=self.domain_profile_service._merge_products(current.products, incoming.products),
            policies=self.domain_profile_service._merge_policies(current.policies, incoming.policies),
            faq_signals=self.domain_profile_service._merge_faq_signals(current.faq_signals, incoming.faq_signals),
            sales_actions=self.domain_profile_service._merge_strings(current.sales_actions, incoming.sales_actions),
            forbidden_claims=self.domain_profile_service._merge_strings(current.forbidden_claims, incoming.forbidden_claims),
            metadata={**current.metadata, **incoming.metadata},
            created_at=current.created_at,
        )


_knowledge_understanding_service: Optional[KnowledgeUnderstandingService] = None


def get_knowledge_understanding_service(
    llm_service=None,
    domain_profile_service: Optional[DomainProfileService] = None,
) -> KnowledgeUnderstandingService:
    """
    获取知识理解服务实例

    .. deprecated::
        此工厂函数已被 ``get_knowledge_service().understanding`` 取代。
        新代码请使用 ``from src.common.knowledge_service_adapter import get_knowledge_service`` 。
        本函数保留仅为向后兼容，将在未来版本移除。
    """
    import warnings as _w
    _w.warn(
        "get_knowledge_understanding_service() 已废弃，请使用 get_knowledge_service().understanding 替代。"
        " 迁移指南: from src.common.knowledge_service_adapter import get_knowledge_service",
        DeprecationWarning,
        stacklevel=2,
    )
    global _knowledge_understanding_service
    if llm_service is not None or domain_profile_service is not None:
        return KnowledgeUnderstandingService(
            llm_service=llm_service,
            domain_profile_service=domain_profile_service,
        )
    if _knowledge_understanding_service is None:
        _knowledge_understanding_service = KnowledgeUnderstandingService()
    return _knowledge_understanding_service
