"""上传链使用的轻量图谱抽取适配层。"""

from __future__ import annotations

import hashlib
from typing import List, Tuple

from loguru import logger

from src.common.knowledge_graph import Entity, EntityType, Relation, RelationType
from src.common.llm_knowledge_extractor import get_llm_extractor


class KnowledgeExtractor:
    """把统一 LLM 知识抽取结果适配成统一图谱服务使用的图结构。"""

    def __init__(self, llmService=None):
        self._has_explicit_llm = llmService is not None
        self.extractor = get_llm_extractor(llmService)

    async def extractFromText(
        self,
        text: str,
        enterpriseId: str = "default",
        sourceDocument: str = "",
    ) -> Tuple[List[Entity], List[Relation]]:
        if not text or len(text.strip()) < 10:
            return [], []

        knowledges = []
        if self._has_explicit_llm:
            try:
                knowledges = await self.extractor.extract_from_text(
                    text,
                    context={
                        "enterprise_id": enterpriseId,
                        "source_document": sourceDocument,
                    },
                )
            except Exception as exc:
                logger.warning(f"统一知识抽取失败: {exc}")

        if not knowledges:
            knowledges = [self._build_fallback_knowledge(text, enterprise_id=enterpriseId)]

        entities: List[Entity] = []
        relations: List[Relation] = []
        seen_entities = set()

        for knowledge in knowledges[:5]:
            question = (knowledge.question or "").strip()
            answer = (knowledge.answer or "").strip()
            if not question or not answer:
                continue

            category = (knowledge.category or "concept").lower()
            entity_type = self._map_entity_type(category, enterprise_id=enterpriseId)
            entity_id = self._make_id("entity", enterpriseId, sourceDocument, question)
            if entity_id not in seen_entities:
                entities.append(
                    Entity(
                        id=entity_id,
                        name=question[:80],
                        entity_type=entity_type,
                        description=answer[:300],
                        properties={
                            "category": knowledge.category,
                            "confidence": knowledge.confidence,
                            "keywords": knowledge.keywords,
                            "tags": knowledge.tags,
                            "reasoning": knowledge.reasoning,
                            "source_document": sourceDocument,
                            "enterprise_id": enterpriseId,
                        },
                        aliases=list(dict.fromkeys((knowledge.keywords or [])[:5])),
                        confidence=max(0.3, float(knowledge.confidence or 0.3)),
                    )
                )
                seen_entities.add(entity_id)

            for keyword in (knowledge.keywords or [])[:5]:
                keyword = (keyword or "").strip()
                if not keyword:
                    continue
                keyword_id = self._make_id("keyword", enterpriseId, sourceDocument, keyword)
                if keyword_id not in seen_entities:
                    entities.append(
                        Entity(
                            id=keyword_id,
                            name=keyword,
                            entity_type=EntityType.UNKNOWN,
                            description=f"从 {question[:40]} 提取的关键词",
                            properties={
                                "source_question": question[:80],
                                "source_document": sourceDocument,
                                "enterprise_id": enterpriseId,
                            },
                            confidence=0.5,
                        )
                    )
                    seen_entities.add(keyword_id)

                relations.append(
                    Relation(
                        id=self._make_id("rel", entity_id, keyword_id, "answers"),
                        source_id=entity_id,
                        target_id=keyword_id,
                        relation_type=RelationType.RELATED_TO,
                        weight=max(0.5, float(knowledge.confidence or 0.5)),
                        properties={
                            "evidence": answer[:120],
                            "source_document": sourceDocument,
                            "enterprise_id": enterpriseId,
                        },
                        description="知识点与关键词关联",
                    )
                )

        return entities, relations

    def _build_fallback_knowledge(self, text: str, *, enterprise_id: str = ""):
        from src.common.types.knowledge import ExtractedKnowledge

        normalized = " ".join(text.split())
        title = normalized[:40]
        # 从 schema 读取行业 domain_keywords 作为兜底关键词，不再硬编码旅游术语
        fallback_tokens = self._get_schema_domain_keywords(enterprise_id=enterprise_id)
        keywords = []
        for token in fallback_tokens:
            if token in normalized:
                keywords.append(token)
        if not keywords:
            keywords = [normalized[:8]]

        return ExtractedKnowledge(
            question=title,
            answer=normalized[:300],
            source="graph_upload_fallback",
            confidence=0.4,
            keywords=keywords[:5],
            category="其他",
            tags=["fallback", "graph_upload"],
            reasoning="LLM 不可用，使用规则回退生成基础图谱节点",
        )

    def _get_schema_domain_keywords(self, *, enterprise_id: str = "") -> List[str]:
        """从 schema metadata.query_understanding.domain_keywords 读取行业关键词。"""
        try:
            from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat
            schema = get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=str(enterprise_id or "").strip(),
            )
            metadata = schema.get("metadata") or {}
            query_understanding = metadata.get("query_understanding") or {}
            keywords = query_understanding.get("domain_keywords") or []
            if isinstance(keywords, list):
                return [str(k or "").strip() for k in keywords if str(k or "").strip()]
        except Exception:
            pass
        return []

    @staticmethod
    def _make_id(prefix: str, *parts: str) -> str:
        raw = "|".join(part or "" for part in parts)
        return f"{prefix}_{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"

    # 通用实体类型映射（跨行业通用），行业专用映射由 schema entity_type_aliases 提供
    _GENERIC_ENTITY_TYPE_MAP = {
        "产品": EntityType.PRODUCT,
        "product": EntityType.PRODUCT,
        "价格": EntityType.PRICE,
        "price": EntityType.PRICE,
        "服务": EntityType.SERVICE,
        "service": EntityType.SERVICE,
        "合作": EntityType.ACTIVITY,
        "cooperation": EntityType.ACTIVITY,
        "faq": EntityType.FAQ,
        "促销": EntityType.ACTIVITY,
        "promotion": EntityType.ACTIVITY,
    }

    def _map_entity_type(self, category: str, *, enterprise_id: str = "") -> EntityType:
        # 优先查 schema 行业专用别名
        try:
            from src.common.knowledge_graph import EntityExtractor
            extractor = EntityExtractor(enterprise_id=enterprise_id)
            schema_aliases = extractor._get_schema_entity_type_aliases(enterprise_id=enterprise_id)
            normalized = str(category or "").strip().lower()
            if normalized in schema_aliases:
                return schema_aliases[normalized]
        except Exception:
            pass
        # 回退到通用映射
        return self._GENERIC_ENTITY_TYPE_MAP.get(category, EntityType.UNKNOWN)
