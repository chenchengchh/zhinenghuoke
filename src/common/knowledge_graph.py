"""
知识图谱服务 (KnowledgeGraphService)

基于Phase 3设计文档实现
提供实体关系图 + 知识推理能力

功能：
1. 实体识别与抽取
2. 关系抽取与存储
3. 图谱查询与推理
4. RAG + 知识图谱融合
"""

import json
import logging
import re
from typing import List, Dict, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict

logger = logging.getLogger(__name__)


class EntityType(Enum):
    """实体类型"""
    PRODUCT = "product"
    SERVICE = "service"
    PROCESS = "process"
    PRICE = "price"
    FEATURE = "feature"
    ACTIVITY = "activity"
    LOCATION = "location"
    COMPANY = "company"
    FAQ = "faq"
    UNKNOWN = "unknown"


class RelationType(Enum):
    """关系类型"""
    IS_A = "is_a"
    HAS_FEATURE = "has_feature"
    HAS_BENEFIT = "has_benefit"
    PRICED_AT = "priced_at"
    SUPPORTS = "supports"
    COMPETES_WITH = "competes_with"
    PARTNERS_WITH = "partners_with"
    USED_FOR = "used_for"
    ALTERNATIVES = "alternatives"
    INCLUDES = "includes"
    RELATED_TO = "related_to"


@dataclass
class Entity:
    """实体"""
    id: str
    name: str
    entity_type: EntityType
    description: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # P2-9: 主题标签（LightRAG high-level 检索使用）
    topics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type.value,
            "description": self.description,
            "properties": self.properties,
            "aliases": self.aliases,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "topics": self.topics,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Entity":
        """从字典反序列化。"""
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            entity_type=EntityType(str(data.get("entity_type") or "product")),
            description=str(data.get("description") or ""),
            properties=data.get("properties") or {},
            aliases=list(data.get("aliases") or []),
            confidence=float(data.get("confidence") or 1.0),
            created_at=str(data.get("created_at") or datetime.now().isoformat()),
            topics=list(data.get("topics") or []),
        )


@dataclass
class Relation:
    """关系"""
    id: str
    source_id: str
    target_id: str
    relation_type: RelationType
    weight: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "id": self.id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation_type": self.relation_type.value,
            "weight": self.weight,
            "properties": self.properties,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Relation":
        """从字典反序列化。"""
        return cls(
            id=str(data.get("id") or ""),
            source_id=str(data.get("source_id") or ""),
            target_id=str(data.get("target_id") or ""),
            relation_type=RelationType(str(data.get("relation_type") or "related_to")),
            weight=float(data.get("weight") or 1.0),
            properties=data.get("properties") or {},
            description=str(data.get("description") or ""),
        )


@dataclass
class GraphPath:
    """图谱路径"""
    entities: List[Entity]
    relations: List[Relation]
    total_weight: float


class EntityExtractor:
    """实体抽取器"""

    DEFAULT_ENTITY_PATTERNS = {
        EntityType.PRODUCT: [
            r"产品", r"方案", r"套餐", r"版本", r"模块", r"服务包", r"系统"
        ],
        EntityType.SERVICE: [
            r"服务", r"支持", r"售后", r"咨询", r"实施", r"交付", r"培训"
        ],
        EntityType.PROCESS: [
            r"流程", r"步骤", r"开通", r"部署", r"对接", r"上线", r"安排"
        ],
        EntityType.PRICE: [
            r"\d+元", r"\d+万", r"报价", r"价格", r"费用", r"收费", r"预算", r"套餐"
        ],
        EntityType.FEATURE: [
            r"功能", r"特点", r"配置", r"规格", r"能力", r"优势", r"包含", r"说明"
        ],
        EntityType.ACTIVITY: [
            r"活动", r"合作"
        ],
        EntityType.LOCATION: [
            r"区域", r"地点", r"城市", r"站点"
        ],
        EntityType.COMPANY: [
            r"公司", r"品牌", r"厂商", r"团队", r"机构", r"供应商"
        ],
        EntityType.FAQ: [
            r"如何", r"怎么", r"为什么", r"是否", r"常见问题"
        ],
    }

    # 通用实体类型别名（跨行业通用），行业专用别名（如 route/attraction/food/hotel/transport）
    # 由 schema metadata.intent_recognition.entity_type_aliases 提供，不在此硬编码
    ENTITY_TYPE_ALIASES = {
        "product": EntityType.PRODUCT,
        "service": EntityType.SERVICE,
        "process": EntityType.PROCESS,
        "price": EntityType.PRICE,
        "feature": EntityType.FEATURE,
        "activity": EntityType.ACTIVITY,
        "location": EntityType.LOCATION,
        "company": EntityType.COMPANY,
        "faq": EntityType.FAQ,
        "policy": EntityType.FAQ,
        "district": EntityType.LOCATION,
    }

    def _get_active_schema(self, enterprise_id: str = ""):
        try:
            from .industry_schema_service import IndustrySchemaService
            service = IndustrySchemaService()
            return service.get_active_schema(enterprise_id=str(enterprise_id or "").strip()) or {}
        except Exception:
            return {}

    def _get_schema_entity_type_aliases(self, enterprise_id: str = "") -> Dict[str, EntityType]:
        """从 schema metadata.intent_recognition.entity_type_aliases 读取行业专用实体类型别名。"""
        schema = self._get_active_schema(enterprise_id=enterprise_id)
        metadata = schema.get("metadata") or {}
        intent_config = metadata.get("intent_recognition") or {}
        raw_aliases = intent_config.get("entity_type_aliases") or {}
        if not isinstance(raw_aliases, dict):
            return {}
        aliases: Dict[str, EntityType] = {}
        for raw_key, raw_value in raw_aliases.items():
            normalized = str(raw_key or "").strip().lower()
            if not normalized:
                continue
            try:
                aliases[normalized] = EntityType(str(raw_value or "").strip().upper())
            except ValueError:
                continue
        return aliases

    def _resolve_entity_type(self, raw_key: Any, enterprise_id: str = "") -> Optional[EntityType]:
        normalized = str(raw_key or "").strip().lower()
        if not normalized:
            return None
        # 优先查 schema 行业专用别名
        schema_aliases = self._get_schema_entity_type_aliases(enterprise_id=enterprise_id)
        if normalized in schema_aliases:
            return schema_aliases[normalized]
        # 回退到通用别名
        if normalized in self.ENTITY_TYPE_ALIASES:
            return self.ENTITY_TYPE_ALIASES[normalized]
        try:
            return EntityType(normalized)
        except ValueError:
            return None

    def _get_domain_entity_patterns(self, enterprise_id: str = "") -> Dict[EntityType, List[str]]:
        schema = self._get_active_schema(enterprise_id=enterprise_id)
        metadata = schema.get("metadata") or {}
        intent_config = metadata.get("intent_recognition") or {}
        domain_patterns = intent_config.get("domain_entity_patterns") or {}
        normalized_patterns: Dict[EntityType, List[str]] = {
            entity_type: list(patterns)
            for entity_type, patterns in self.DEFAULT_ENTITY_PATTERNS.items()
        }
        for raw_key, patterns in domain_patterns.items():
            entity_type = self._resolve_entity_type(raw_key, enterprise_id=enterprise_id)
            if not entity_type:
                continue
            merged = normalized_patterns.get(entity_type, []) + [
                str(pattern or "").strip()
                for pattern in list(patterns or [])
                if str(pattern or "").strip()
            ]
            normalized_patterns[entity_type] = list(dict.fromkeys(merged))
        return normalized_patterns

    def __init__(self, enterprise_id: str = ""):
        self.enterprise_id = str(enterprise_id or "").strip()
        self.entity_cache: Dict[str, Entity] = {}
        self.entity_patterns = self._get_domain_entity_patterns(enterprise_id=self.enterprise_id)

    def extract(self, text: str) -> List[Entity]:
        """从文本中抽取实体"""
        entities = []
        text_lower = text.lower()

        for entity_type, patterns in self.entity_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    entity_name = self._get_match_text(text, pattern)
                    if entity_name and entity_name not in self.entity_cache:
                        entity = Entity(
                            id=f"entity_{len(self.entity_cache)}",
                            name=entity_name,
                            entity_type=entity_type,
                            confidence=0.8
                        )
                        entities.append(entity)
                        self.entity_cache[entity_name] = entity

        return entities

    def _get_match_text(self, text: str, pattern: str) -> Optional[str]:
        """获取匹配文本"""
        match = re.search(pattern, text.lower())
        if match:
            return match.group()
        return None

    # ------------------------------------------------------------------
    # P1-5: LLM 实体抽取（双路径：LLM 优先 + 正则兜底）
    # ------------------------------------------------------------------

    def extract_with_llm(
        self,
        text: str,
        llm_service: Any = None,
        enterprise_id: str = "",
    ) -> Tuple[List[Entity], List[Relation]]:
        """使用 LLM 抽取实体和关系，失败时回退到正则模式。

        Args:
            text: 待抽取文本
            llm_service: LLM 服务实例（None 时尝试获取默认 provider）
            enterprise_id: 企业 ID（用于 schema 上下文）

        Returns:
            Tuple[List[Entity], List[Relation]]: 实体列表和关系列表
        """
        if not text or len(text.strip()) < 10:
            return [], []

        try:
            import asyncio
            from src.rag.graph_extraction_adapter import KnowledgeExtractor

            extractor = KnowledgeExtractor(llmService=llm_service)
            # 在同步上下文中运行异步抽取
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 已在事件循环中，使用 asyncio.ensure_future + 同步等待
                    # 但 build_graph_from_knowledge 在后台线程中，通常没有运行中的循环
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        entities, relations = pool.submit(
                            asyncio.run,
                            extractor.extractFromText(
                                text,
                                enterpriseId=str(enterprise_id or self.enterprise_id or "default"),
                            ),
                        ).result()
                else:
                    entities, relations = loop.run_until_complete(
                        extractor.extractFromText(
                            text,
                            enterpriseId=str(enterprise_id or self.enterprise_id or "default"),
                        )
                    )
            except RuntimeError:
                # 没有事件循环，创建新的
                entities, relations = asyncio.run(
                    extractor.extractFromText(
                        text,
                        enterpriseId=str(enterprise_id or self.enterprise_id or "default"),
                    )
                )

            # 校验 LLM 抽取结果
            validated_entities = [e for e in entities if e and e.name and e.id]
            validated_relations = [
                r for r in relations
                if r and r.source_id and r.target_id and r.source_id != r.target_id
            ]

            if validated_entities:
                logger.info(
                    f"LLM 实体抽取成功: {len(validated_entities)} 实体, "
                    f"{len(validated_relations)} 关系"
                )
                return validated_entities, validated_relations

            # LLM 无结果，回退到正则
            logger.debug("LLM 抽取无结果，回退到正则模式")
            return self.extract(text), []
        except Exception as exc:
            logger.warning(f"LLM 实体抽取失败，回退到正则模式: {exc}")
            return self.extract(text), []


class RelationExtractor:
    """关系抽取器"""

    RELATION_PATTERNS = {
        RelationType.IS_A: [
            (r"(\w+)是一种?(\w+)", r"\2"),
            (r"(\w+)属于(\w+)", r"\1"),
        ],
        RelationType.HAS_FEATURE: [
            (r"(\w+)具有?(\w+)功能", r"has_feature"),
            (r"(\w+)支持(\w+)", r"supports"),
        ],
        RelationType.PRICED_AT: [
            (r"(\d+元/?月)", r"price"),
        ],
        RelationType.SUPPORTS: [
            (r"支持(\w+)", r"supports"),
            (r"可以(\w+)", r"can_do"),
        ],
        RelationType.RELATED_TO: [
            (r"关于(\w+)", r"about"),
            (r"和(\w+)相关", r"related"),
        ],
    }

    def __init__(self, enterprise_id: str = ""):
        """初始化，从 schema 读取行业专用关系模式并合并。"""
        self.enterprise_id = enterprise_id
        self.relation_patterns = self._get_merged_relation_patterns(enterprise_id)

    def _get_merged_relation_patterns(self, enterprise_id: str = "") -> Dict[RelationType, List[tuple]]:
        """合并默认关系模式与 schema 行业专用关系模式。"""
        merged = {rt: list(patterns) for rt, patterns in self.RELATION_PATTERNS.items()}
        try:
            from src.common.industry_schema_service import get_industry_schema_service
            schema_service = get_industry_schema_service()
            settings = schema_service.get_settings() or {}
            active_id = str(settings.get("active_schema_id") or "").strip()
            if not active_id:
                return merged
            schema = schema_service.get_schema(active_id) or {}
            metadata = schema.get("metadata") or {}
            intent_config = metadata.get("intent_recognition") or {}
            domain_patterns = intent_config.get("domain_relation_patterns") or {}
            if not isinstance(domain_patterns, dict):
                return merged
            for raw_key, patterns in domain_patterns.items():
                rt = self._resolve_relation_type(raw_key)
                if not rt:
                    continue
                merged.setdefault(rt, [])
                for p in list(patterns or []):
                    if isinstance(p, list) and len(p) >= 2:
                        merged[rt].append((str(p[0]), str(p[1])))
                    elif isinstance(p, str):
                        merged[rt].append((p, r"\1"))
        except Exception:
            pass
        return merged

    @staticmethod
    def _resolve_relation_type(raw_key: str) -> Optional[RelationType]:
        """将字符串映射到 RelationType 枚举。"""
        key = str(raw_key or "").strip().lower().replace("-", "_")
        mapping = {
            "is_a": RelationType.IS_A,
            "has_feature": RelationType.HAS_FEATURE,
            "priced_at": RelationType.PRICED_AT,
            "supports": RelationType.SUPPORTS,
            "related_to": RelationType.RELATED_TO,
        }
        return mapping.get(key)

    def extract(self, text: str, entities: List[Entity]) -> List[Relation]:
        """从文本和实体中抽取关系"""
        relations = []

        for entity in entities:
            for relation_type, patterns in self.relation_patterns.items():
                for pattern, _ in patterns:
                    if re.search(pattern, text.lower()):
                        relation = Relation(
                            id=f"rel_{len(relations)}",
                            source_id=entity.id,
                            target_id="",
                            relation_type=relation_type,
                            weight=0.7
                        )
                        relations.append(relation)

        return relations


class GraphStore:
    """图谱存储"""

    def __init__(self):
        self.entities: Dict[str, Entity] = {}
        self.relations: Dict[str, List[Relation]] = defaultdict(list)
        self.entity_index: Dict[str, Set[str]] = defaultdict(set)
        self.type_index: Dict[EntityType, Set[str]] = defaultdict(set)
        # P2-9: 主题索引（主题 → 实体 ID 集合），用于 LightRAG high-level 检索
        self.topic_index: Dict[str, Set[str]] = defaultdict(set)

    def add_entity(self, entity: Entity) -> bool:
        """添加实体"""
        try:
            self.entities[entity.id] = entity
            self.type_index[entity.entity_type].add(entity.id)

            for alias in entity.aliases:
                self.entity_index[alias.lower()].add(entity.id)

            self.entity_index[entity.name.lower()].add(entity.id)

            # P2-9: 更新主题索引
            for topic in entity.topics:
                topic_key = str(topic or "").strip().lower()
                if topic_key:
                    self.topic_index[topic_key].add(entity.id)
            return True
        except Exception as e:
            logger.error(f"添加实体失败: {e}")
            return False

    def add_relation(self, relation: Relation) -> bool:
        """添加关系"""
        try:
            self.relations[relation.source_id].append(relation)
            return True
        except Exception as e:
            logger.error(f"添加关系失败: {e}")
            return False

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """获取实体"""
        return self.entities.get(entity_id)

    def get_entities_by_type(self, entity_type: EntityType) -> List[Entity]:
        """按类型获取实体"""
        entity_ids = self.type_index.get(entity_type, set())
        return [self.entities[eid] for eid in entity_ids if eid in self.entities]

    def get_entities_by_topic(self, topic: str) -> List[Entity]:
        """P2-9: 按主题获取实体集合（LightRAG high-level 检索）。

        Args:
            topic: 主题关键词（不区分大小写）

        Returns:
            List[Entity]: 该主题下的所有实体
        """
        topic_key = str(topic or "").strip().lower()
        if not topic_key:
            return []
        entity_ids = self.topic_index.get(topic_key, set())
        return [self.entities[eid] for eid in entity_ids if eid in self.entities]

    def get_all_topics(self) -> List[str]:
        """P2-9: 返回所有主题标签（小写）。"""
        return [t for t in self.topic_index.keys() if t]

    def get_relations(self, entity_id: str) -> List[Relation]:
        """获取实体的关系"""
        return self.relations.get(entity_id, [])

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 3
    ) -> List[GraphPath]:
        """查找两个实体间的路径"""
        paths = []
        visited = set()

        def dfs(current_id: str, target_id: str, depth: int,
                path_entities: List[Entity], path_relations: List[Relation], weight: float):
            if depth > max_depth:
                return

            if current_id == target_id:
                paths.append(GraphPath(
                    entities=list(path_entities),
                    relations=list(path_relations),
                    total_weight=weight
                ))
                return

            visited.add(current_id)

            for relation in self.get_relations(current_id):
                next_id = relation.target_id

                if next_id not in visited:
                    next_entity = self.get_entity(next_id)
                    if next_entity:
                        path_entities.append(next_entity)
                        path_relations.append(relation)
                        dfs(next_id, target_id, depth + 1,
                            path_entities, path_relations, weight * relation.weight)
                        path_entities.pop()
                        path_relations.pop()

            visited.remove(current_id)

        source_entity = self.get_entity(source_id)
        if source_entity:
            dfs(source_id, target_id, 0, [source_entity], [], 1.0)

        return paths

    def query_by_keyword(self, keyword: str) -> List[Entity]:
        """按关键词查询实体"""
        keyword_lower = keyword.lower()
        entity_ids = self.entity_index.get(keyword_lower, set())

        results = []
        for entity_id in entity_ids:
            entity = self.get_entity(entity_id)
            if entity:
                results.append(entity)

        return results

    # ------------------------------------------------------------------
    # P0-1: 持久化支持
    # ------------------------------------------------------------------

    _GRAPH_FILE_VERSION = 1

    def save_to_file(self, file_path: str, schema_id: str = "") -> bool:
        """将图谱序列化到 JSON 文件。

        Args:
            file_path: 目标文件绝对路径
            schema_id: 关联的 schema_id（用于缓存失效与多租户隔离）

        Returns:
            bool: 是否保存成功
        """
        try:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            payload = {
                "version": self._GRAPH_FILE_VERSION,
                "schema_id": str(schema_id or ""),
                "entities": {eid: e.to_dict() for eid, e in self.entities.items()},
                "relations": {
                    src: [r.to_dict() for r in rels]
                    for src, rels in self.relations.items()
                },
                "entity_index": {
                    key: sorted(list(eids)) for key, eids in self.entity_index.items()
                },
                "type_index": {
                    etype.value: sorted(list(eids))
                    for etype, eids in self.type_index.items()
                },
                # P2-9: 主题索引持久化
                "topic_index": {
                    key: sorted(list(eids)) for key, eids in self.topic_index.items()
                },
                "built_at": datetime.now().isoformat(),
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(
                f"知识图谱已保存: {file_path} (entities={len(self.entities)}, "
                f"relations={sum(len(r) for r in self.relations.values())})"
            )
            return True
        except Exception as exc:
            logger.error(f"保存知识图谱失败: {exc}")
            return False

    def load_from_file(self, file_path: str) -> bool:
        """从 JSON 文件加载图谱（覆盖当前内存状态）。

        Args:
            file_path: 源文件绝对路径

        Returns:
            bool: 是否加载成功
        """
        try:
            import os
            if not os.path.exists(file_path):
                return False
            with open(file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return False
            if payload.get("version") != self._GRAPH_FILE_VERSION:
                logger.warning(
                    f"知识图谱版本不匹配 (file={payload.get('version')}, "
                    f"expected={self._GRAPH_FILE_VERSION})，跳过加载"
                )
                return False

            # 清空当前状态
            self.entities.clear()
            self.relations.clear()
            self.entity_index.clear()
            self.type_index.clear()
            self.topic_index.clear()

            # 还原 entities
            for eid, edata in (payload.get("entities") or {}).items():
                try:
                    entity = Entity.from_dict(edata)
                    self.entities[entity.id] = entity
                except Exception as exc:
                    logger.warning(f"反序列化实体失败 id={eid}: {exc}")

            # 还原 relations
            for src, rel_list in (payload.get("relations") or {}).items():
                for rdata in rel_list or []:
                    try:
                        relation = Relation.from_dict(rdata)
                        self.relations[src].append(relation)
                    except Exception as exc:
                        logger.warning(f"反序列化关系失败 src={src}: {exc}")

            # 还原 entity_index（字符串键）
            for key, id_list in (payload.get("entity_index") or {}).items():
                self.entity_index[key] = set(id_list or [])

            # 还原 type_index（字符串键 -> EntityType）
            for type_key, id_list in (payload.get("type_index") or {}).items():
                try:
                    etype = EntityType(str(type_key))
                    self.type_index[etype] = set(id_list or [])
                except Exception:
                    continue

            # P2-9: 还原 topic_index（向前兼容：旧文件无 topic_index 时从 entities 重建）
            topic_index_payload = payload.get("topic_index")
            if isinstance(topic_index_payload, dict) and topic_index_payload:
                for topic_key, id_list in topic_index_payload.items():
                    self.topic_index[str(topic_key)] = set(id_list or [])
            else:
                for entity in self.entities.values():
                    for topic in entity.topics:
                        topic_key = str(topic or "").strip().lower()
                        if topic_key:
                            self.topic_index[topic_key].add(entity.id)

            logger.info(
                f"知识图谱已加载: {file_path} (entities={len(self.entities)}, "
                f"relations={sum(len(r) for r in self.relations.values())})"
            )
            return True
        except Exception as exc:
            logger.error(f"加载知识图谱失败: {exc}")
            return False


class ReasoningEngine:
    """推理引擎"""

    def __init__(self, graph_store: GraphStore):
        self.graph_store = graph_store

    def infer(self, paths: List[GraphPath]) -> List[GraphPath]:
        """推理增强路径"""
        enhanced_paths = []

        for path in paths:
            new_weight = path.total_weight

            for relation in path.relations:
                if relation.relation_type == RelationType.HAS_FEATURE:
                    new_weight *= 1.2
                elif relation.relation_type == RelationType.IS_A:
                    new_weight *= 1.1

            enhanced_path = GraphPath(
                entities=path.entities,
                relations=path.relations,
                total_weight=new_weight
            )
            enhanced_paths.append(enhanced_path)

        enhanced_paths.sort(key=lambda p: p.total_weight, reverse=True)
        return enhanced_paths

    def deduce_related(
        self,
        entity_id: str,
        relation_type: RelationType = None
    ) -> List[Entity]:
        """推导相关实体"""
        relations = self.graph_store.get_relations(entity_id)
        related = []

        for relation in relations:
            if relation_type is None or relation.relation_type == relation_type:
                target = self.graph_store.get_entity(relation.target_id)
                if target:
                    related.append(target)

        return related


class CommunitySummarizer:
    """P2-7: 图社区摘要（参考微软 GraphRAG）

    使用社区发现算法（Louvain）划分实体社区，并用 LLM 生成层次化摘要。
    全局性问题检索社区摘要而非单个实体。
    """

    # 全局性问题关键词（跨行业通用）
    GLOBAL_QUERY_KEYWORDS = [
        "所有", "全部", "共同", "整体", "总体", "哪些", "每种", "每类",
        "一共", "总共", "分别", "各个", "各款", "各项",
    ]

    def __init__(self, graph_store: GraphStore, llm_service: Any = None):
        self.graph_store = graph_store
        self.llm_service = llm_service
        self.communities: List[Dict[str, Any]] = []

    def is_global_query(self, query: str) -> bool:
        """检测是否为全局性问题。"""
        text = str(query or "").strip()
        if not text:
            return False
        return any(kw in text for kw in self.GLOBAL_QUERY_KEYWORDS)

    def detect_communities(self) -> List[Dict[str, Any]]:
        """使用 networkx Louvain 算法检测社区。"""
        try:
            import networkx as nx
        except ImportError:
            logger.warning("networkx 未安装，社区发现降级为连通分量")
            return self._detect_communities_fallback()

        # 构建网络图
        G = nx.Graph()
        for eid, entity in self.graph_store.entities.items():
            G.add_node(eid, name=entity.name, entity_type=entity.entity_type.value)

        for src_id, rels in self.graph_store.relations.items():
            for rel in rels:
                if rel.target_id and rel.target_id in self.graph_store.entities:
                    G.add_edge(src_id, rel.target_id, weight=rel.weight)

        if G.number_of_nodes() == 0:
            return []

        # Louvain 社区发现
        try:
            communities_list = nx.community.louvain_communities(G, weight="weight", seed=42)
        except Exception:
            # 降级到连通分量
            communities_list = list(nx.connected_components(G))

        result = []
        for idx, community_nodes in enumerate(communities_list):
            if len(community_nodes) < 2:
                continue
            entities = [
                self.graph_store.get_entity(nid)
                for nid in community_nodes
                if self.graph_store.get_entity(nid)
            ]
            if not entities:
                continue
            result.append({
                "community_id": f"community_{idx}",
                "entity_ids": sorted(list(community_nodes)),
                "entity_count": len(entities),
                "entity_names": [e.name for e in entities[:20]],
                "entity_types": list(set(e.entity_type.value for e in entities)),
                "summary": "",
            })

        self.communities = result
        logger.info(f"社区发现完成: {len(result)} 个社区")
        return result

    def _detect_communities_fallback(self) -> List[Dict[str, Any]]:
        """无 networkx 时降级为连通分量（BFS）。"""
        from collections import deque

        visited = set()
        communities = []
        for start_id in self.graph_store.entities:
            if start_id in visited:
                continue
            # BFS 遍历连通分量
            queue = deque([start_id])
            component = set()
            while queue:
                nid = queue.popleft()
                if nid in visited:
                    continue
                visited.add(nid)
                component.add(nid)
                for rel in self.graph_store.get_relations(nid):
                    if rel.target_id and rel.target_id not in visited:
                        queue.append(rel.target_id)

            if len(component) < 2:
                continue
            entities = [
                self.graph_store.get_entity(nid)
                for nid in component
                if self.graph_store.get_entity(nid)
            ]
            communities.append({
                "community_id": f"community_{len(communities)}",
                "entity_ids": sorted(list(component)),
                "entity_count": len(entities),
                "entity_names": [e.name for e in entities[:20]],
                "entity_types": list(set(e.entity_type.value for e in entities)),
                "summary": "",
            })

        self.communities = communities
        return communities

    def summarize_communities(self, llm_service: Any = None) -> List[Dict[str, Any]]:
        """用 LLM 为每个社区生成摘要。"""
        effective_llm = llm_service or self.llm_service
        if not effective_llm or not self.communities:
            return self.communities

        try:
            for community in self.communities:
                entity_names = community.get("entity_names") or []
                entity_types = community.get("entity_types") or []
                if not entity_names:
                    continue
                prompt = (
                    f"请为以下知识图谱社区生成一段简洁摘要（50-100字），"
                    f"概括这些实体的共同主题和关键特征：\n"
                    f"实体: {', '.join(entity_names[:15])}\n"
                    f"类型: {', '.join(entity_types)}"
                )
                try:
                    summary = self._call_llm(effective_llm, prompt)
                    if summary:
                        community["summary"] = str(summary).strip()[:300]
                except Exception as exc:
                    logger.debug(f"社区 {community.get('community_id')} 摘要生成失败: {exc}")
        except Exception as exc:
            logger.warning(f"社区摘要生成失败: {exc}")

        return self.communities

    @staticmethod
    def _call_llm(llm_service: Any, prompt: str) -> str:
        """调用 LLM 服务生成文本（兼容同步/异步接口）。"""
        # 尝试常见的 LLM 接口模式
        if hasattr(llm_service, "generate"):
            result = llm_service.generate(prompt)
            return str(result or "")
        if hasattr(llm_service, "chat"):
            result = llm_service.chat([{"role": "user", "content": prompt}])
            if isinstance(result, dict):
                return str(result.get("content") or result.get("response") or "")
            return str(result or "")
        if hasattr(llm_service, "complete"):
            result = llm_service.complete(prompt)
            return str(result or "")
        return ""

    def save_to_file(self, file_path: str) -> bool:
        """持久化社区摘要到 JSON 文件。"""
        try:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            payload = {
                "version": 1,
                "communities": self.communities,
                "built_at": datetime.now().isoformat(),
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info(f"社区摘要已保存: {file_path} ({len(self.communities)} 个社区)")
            return True
        except Exception as exc:
            logger.error(f"保存社区摘要失败: {exc}")
            return False

    def load_from_file(self, file_path: str) -> bool:
        """从 JSON 文件加载社区摘要。"""
        try:
            import os
            if not os.path.exists(file_path):
                return False
            with open(file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return False
            self.communities = payload.get("communities") or []
            logger.info(f"社区摘要已加载: {file_path} ({len(self.communities)} 个社区)")
            return True
        except Exception as exc:
            logger.error(f"加载社区摘要失败: {exc}")
            return False

    def query_communities(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """检索与查询相关的社区摘要。"""
        if not self.communities:
            return []
        query_lower = str(query or "").lower()
        scored = []
        for community in self.communities:
            score = 0.0
            entity_names = community.get("entity_names") or []
            for name in entity_names:
                if name.lower() in query_lower or query_lower in name.lower():
                    score += 1.0
            summary = community.get("summary") or ""
            if summary:
                for word in query_lower:
                    if len(word) > 1 and word in summary.lower():
                        score += 0.3
            if score > 0:
                scored.append((score, community))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]


class TopicIndexer:
    """P2-9: 主题索引器（LightRAG high-level 检索）。

    为图谱中的实体生成主题标签，构建 topic_index（主题 → 实体集合），
    支持主题层面的检索，与实体层面检索通过 RRF 融合。

    双路径：
    - LLM 路径：基于实体名称、描述、属性、上下文生成主题标签
    - 关键词兜底：实体类型 + 名称分词 + 描述关键词
    """

    # 实体类型 → 中文主题映射（兜底使用）
    _ENTITY_TYPE_TOPICS: Dict[EntityType, str] = {
        EntityType.PRODUCT: "产品",
        EntityType.SERVICE: "服务",
        EntityType.PROCESS: "流程",
        EntityType.PRICE: "价格",
        EntityType.FEATURE: "功能",
        EntityType.ACTIVITY: "活动",
        EntityType.LOCATION: "地点",
        EntityType.COMPANY: "公司",
        EntityType.FAQ: "常见问题",
        EntityType.UNKNOWN: "其他",
    }

    def __init__(self, graph_store: GraphStore, llm_service: Any = None):
        self.graph_store = graph_store
        self.llm_service = llm_service

    def assign_topics_for_entity(
        self,
        entity: Entity,
        context_text: str = "",
    ) -> List[str]:
        """为单个实体生成主题标签。

        Args:
            entity: 待标注的实体
            context_text: 实体来源的上下文文本（如知识条目的 question+answer）

        Returns:
            List[str]: 主题标签列表（小写，去重）
        """
        topics: List[str] = []

        # LLM 路径
        if self.llm_service is not None:
            llm_topics = self._extract_topics_with_llm(entity, context_text)
            if llm_topics:
                topics.extend(llm_topics)

        # 关键词兜底（LLM 失败或未启用时）
        if not topics:
            topics.extend(self._extract_topics_with_keywords(entity, context_text))

        # 去重 + 小写 + 限制数量
        seen: Set[str] = set()
        normalized: List[str] = []
        for t in topics:
            key = str(t or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                normalized.append(key)
            if len(normalized) >= 5:
                break

        entity.topics = normalized
        return normalized

    def assign_topics_for_all(self, context_map: Optional[Dict[str, str]] = None) -> int:
        """为图谱中所有实体生成主题标签。

        Args:
            context_map: 实体 ID → 上下文文本（可选，用于提升 LLM 标注质量）

        Returns:
            int: 处理的实体数量
        """
        context_map = context_map or {}
        count = 0
        for entity in self.graph_store.entities.values():
            ctx = context_map.get(entity.id, "")
            self.assign_topics_for_entity(entity, ctx)
            count += 1
        logger.info(f"主题索引构建完成: {count} 个实体已标注主题")
        return count

    def _extract_topics_with_llm(
        self,
        entity: Entity,
        context_text: str = "",
    ) -> List[str]:
        """使用 LLM 生成主题标签。"""
        try:
            prompt = (
                "请为以下实体生成 2-5 个主题标签（用于知识图谱的主题层面检索）。\n"
                "要求：\n"
                "1. 每个标签 2-6 个字\n"
                "2. 涵盖实体所属的业务主题（如产品类、价格类、售后类等）\n"
                "3. 只输出标签，用逗号分隔，不要其他内容\n\n"
                f"实体名称: {entity.name}\n"
                f"实体类型: {entity.entity_type.value}\n"
                f"描述: {entity.description[:200] if entity.description else '无'}\n"
                f"上下文: {context_text[:200] if context_text else '无'}\n"
            )
            result = self._call_llm(prompt)
            if not result:
                return []
            # 解析 LLM 输出（逗号/顿号/换行分隔）
            raw_topics = re.split(r"[,，、\n;；]+", str(result))
            topics: List[str] = []
            for t in raw_topics:
                t = t.strip().strip("\"'`").strip()
                if t and 1 < len(t) <= 12:
                    topics.append(t)
            return topics
        except Exception as exc:
            logger.debug(f"LLM 主题标注失败 (entity={entity.name}): {exc}")
            return []

    def _extract_topics_with_keywords(
        self,
        entity: Entity,
        context_text: str = "",
    ) -> List[str]:
        """关键词兜底：实体类型 + 名称分词 + 描述关键词。"""
        topics: List[str] = []

        # 1. 实体类型映射
        type_topic = self._ENTITY_TYPE_TOPICS.get(entity.entity_type, "")
        if type_topic:
            topics.append(type_topic)

        # 2. 实体名称分词（取长度≥2的词）
        name_words = self._tokenize(entity.name)
        topics.extend(name_words[:2])

        # 3. 描述关键词（取前 2 个长度≥2的词）
        if entity.description:
            desc_words = self._tokenize(entity.description)
            topics.extend(desc_words[:2])

        # 4. 属性中的关键词
        for key, value in (entity.properties or {}).items():
            if isinstance(value, str) and len(value) <= 8:
                topics.append(value)

        return topics

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """中文分词（jieba 优先，失败时按字符切分）。"""
        text = str(text or "").strip()
        if not text:
            return []
        try:
            import jieba
            words = [w.strip() for w in jieba.cut(text) if len(w.strip()) >= 2]
            return words
        except Exception:
            # 简单兜底：按标点和空格切分
            parts = re.split(r"[\s,，。、；;]+", text)
            return [p.strip() for p in parts if len(p.strip()) >= 2]

    @staticmethod
    def _call_llm(llm_service: Any, prompt: str) -> str:
        """调用 LLM 服务生成文本（兼容同步/异步接口）。"""
        if hasattr(llm_service, "generate"):
            result = llm_service.generate(prompt)
            return str(result or "")
        if hasattr(llm_service, "chat"):
            result = llm_service.chat([{"role": "user", "content": prompt}])
            if isinstance(result, dict):
                return str(result.get("content") or result.get("response") or "")
            return str(result or "")
        if hasattr(llm_service, "complete"):
            result = llm_service.complete(prompt)
            return str(result or "")
        return ""


class KnowledgeGraphService:
    """
    知识图谱服务

    功能：
    1. 从知识库构建图谱
    2. 实体识别与关系抽取
    3. 图谱查询与推理
    4. RAG + 知识图谱融合查询
    """

    def __init__(self, enterprise_id: str = "", schema_id: str = "", llm_service: Any = None):
        self.enterprise_id = str(enterprise_id or "").strip()
        self.schema_id = str(schema_id or "").strip()
        self.llm_service = llm_service
        self.entity_extractor = EntityExtractor(enterprise_id=self.enterprise_id)
        self.relation_extractor = RelationExtractor(enterprise_id=self.enterprise_id)
        self.graph_store = GraphStore()
        self.reasoning_engine = ReasoningEngine(self.graph_store)
        self.community_summarizer = CommunitySummarizer(self.graph_store, llm_service=llm_service)
        # P2-9: 主题索引器（LightRAG high-level 检索）
        self.topic_indexer = TopicIndexer(self.graph_store, llm_service=llm_service)
        self._initialized = False

    # ------------------------------------------------------------------
    # P0-1: 持久化协调
    # ------------------------------------------------------------------

    @staticmethod
    def _get_graph_cache_path(schema_id: str) -> str:
        """返回 schema 对应的图谱缓存文件路径。

        优先使用 schema_id，为空时回退到 'default'，避免文件名冲突。
        """
        import os
        safe_key = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(schema_id or "").strip()) or "default"
        return os.path.join("data", "knowledge_graphs", f"{safe_key}.json")

    def _resolve_cache_schema_id(self, schema_id: str = "") -> str:
        """解析用于缓存键的 schema_id（参数优先，其次实例属性）。"""
        sid = str(schema_id or "").strip() or self.schema_id
        if not sid:
            try:
                from src.common.industry_schema_service import get_industry_schema_service
                sid = str(
                    (get_industry_schema_service().get_settings() or {}).get("active_schema_id") or ""
                ).strip()
            except Exception:
                pass
        return sid

    def load_or_build(
        self,
        knowledge_items: List[Any],
        schema_id: str = "",
        force_rebuild: bool = False,
    ) -> int:
        """优先从缓存加载图谱，缓存缺失或失效时构建并保存。

        Args:
            knowledge_items: 知识条目列表（缓存命中时可省略，但建议传入以便失效时重建）
            schema_id: 关联的 schema_id（覆盖实例属性）
            force_rebuild: True 时强制重建并覆盖缓存

        Returns:
            int: 实体数量（缓存命中时返回已加载实体数）
        """
        sid = self._resolve_cache_schema_id(schema_id)
        cache_path = self._get_graph_cache_path(sid)

        if not force_rebuild:
            if self.graph_store.load_from_file(cache_path):
                self._initialized = True
                # P2-7: 同时加载社区摘要缓存
                self._load_communities_cache(sid)
                return len(self.graph_store.entities)

        # 缓存缺失或强制重建：构建并保存
        entity_count = self.build_graph_from_knowledge(
            knowledge_items, schema_id=sid
        )
        if entity_count > 0:
            self.graph_store.save_to_file(cache_path, schema_id=sid)
        return entity_count

    def invalidate_cache(self, schema_id: str = "") -> bool:
        """删除指定 schema 的图谱缓存文件。

        Args:
            schema_id: 关联的 schema_id（覆盖实例属性）

        Returns:
            bool: 是否删除成功（文件不存在也返回 True）
        """
        import os
        sid = self._resolve_cache_schema_id(schema_id)
        cache_path = self._get_graph_cache_path(sid)
        community_path = cache_path.replace(".json", "_communities.json")
        try:
            for path in (cache_path, community_path):
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"缓存已失效: {path}")
            return True
        except Exception as exc:
            logger.warning(f"删除知识图谱缓存失败: {exc}")
            return False

    def build_graph_from_knowledge(
        self,
        knowledge_items: List[Any],
        schema_id: str = "",
        llm_service: Any = None,
    ) -> int:
        """
        从知识库构建图谱

        Args:
            knowledge_items: 知识条目列表
            schema_id: 关联的 schema_id（用于持久化，覆盖实例属性）
            llm_service: LLM 服务实例（P1-5: 可用时使用 LLM 抽取，否则正则兜底）

        Returns:
            int: 构建的实体数量
        """
        if schema_id:
            self.schema_id = str(schema_id).strip()
        effective_llm = llm_service if llm_service is not None else self.llm_service
        use_llm = effective_llm is not None
        # P2-9: 同步主题索引器的 LLM 服务
        self.topic_indexer.llm_service = effective_llm
        entity_count = 0
        relation_count = 0

        for item in knowledge_items:
            if hasattr(item, 'question') and hasattr(item, 'answer'):
                text = f"{item.question} {item.answer}"

                if use_llm:
                    # P1-5: LLM 双路径抽取（LLM 优先 + 正则兜底）
                    entities, llm_relations = self.entity_extractor.extract_with_llm(
                        text,
                        llm_service=effective_llm,
                        enterprise_id=self.enterprise_id,
                    )
                else:
                    # 正则抽取
                    entities = self.entity_extractor.extract(text)
                    llm_relations = []

                for entity in entities:
                    entity.aliases = self._generate_aliases(item)
                    # P2-9: 添加实体前先标注主题（add_entity 会自动更新 topic_index）
                    self.topic_indexer.assign_topics_for_entity(entity, text)
                    if self.graph_store.add_entity(entity):
                        entity_count += 1

                if use_llm and llm_relations:
                    # LLM 抽取的关系已有正确 source_id/target_id，直接添加
                    for relation in llm_relations:
                        # 确保关系两端实体已存在
                        if (self.graph_store.get_entity(relation.source_id)
                                and self.graph_store.get_entity(relation.target_id)):
                            self.graph_store.add_relation(relation)
                            relation_count += 1
                else:
                    # 正则抽取的关系，target 恒指向最后一个实体
                    relations = self.relation_extractor.extract(text, entities)
                    for relation in relations:
                        if len(entities) >= 2:
                            relation.target_id = entities[-1].id
                            self.graph_store.add_relation(relation)
                            relation_count += 1

        self._initialized = True
        logger.info(
            f"图谱构建完成: {entity_count} 个实体, {relation_count} 个关系 "
            f"(mode={'llm' if use_llm else 'regex'})"
        )
        return entity_count

    def _generate_aliases(self, item: Any) -> List[str]:
        """生成实体别名"""
        aliases = []

        if hasattr(item, 'aliases'):
            aliases.extend(item.aliases)

        if hasattr(item, 'keywords'):
            aliases.extend(item.keywords)

        if hasattr(item, 'tags'):
            aliases.extend(item.tags)

        return list(set(aliases))

    def query_with_reasoning(
        self,
        query: str,
        max_paths: int = 5
    ) -> Dict[str, Any]:
        """
        图谱推理查询

        Args:
            query: 查询文本
            max_paths: 最大路径数

        Returns:
            Dict: 包含实体、关系和推理结果
        """
        results = {
            "query": query,
            "extracted_entities": [],
            "paths": [],
            "related_entities": [],
            "reasoning_result": None,
            "community_summaries": [],
            # P2-9: LightRAG 双层检索
            "topic_entities": [],      # high-level: 主题层面检索到的实体
            "fused_entities": [],      # RRF 融合后的实体名称列表（low + high）
        }

        # P2-7: 全局性问题检测，检索社区摘要
        if self.community_summarizer.is_global_query(query):
            communities = self.community_summarizer.query_communities(query, top_k=3)
            if communities:
                results["community_summaries"] = [
                    {
                        "community_id": c.get("community_id"),
                        "entity_count": c.get("entity_count"),
                        "summary": c.get("summary"),
                        "entity_names": c.get("entity_names", [])[:10],
                    }
                    for c in communities
                ]
                logger.info(f"全局查询命中社区摘要: {len(communities)} 个社区")

        # P2-9: Low-level 检索（实体/关系层面）
        entities = self.entity_extractor.extract(query)
        results["extracted_entities"] = [e.name for e in entities]

        # low-level 检索结果按出现顺序建立 rank
        low_level_ranked: List[str] = []  # 按相关度排序的实体名称
        for entity in entities:
            related = self.reasoning_engine.deduce_related(entity.id)
            for r in related:
                results["related_entities"].append(r.name)
                if r.name not in low_level_ranked:
                    low_level_ranked.append(r.name)

            relations = self.graph_store.get_relations(entity.id)
            for rel in relations:
                target = self.graph_store.get_entity(rel.target_id)
                if target:
                    results["paths"].append({
                        "source": entity.name,
                        "target": target.name,
                        "relation": rel.relation_type.value,
                        "weight": rel.weight
                    })
                    if target.name not in low_level_ranked:
                        low_level_ranked.append(target.name)

        # 主实体本身也加入 low-level 排序列表（rank 最低，因为已通过 extracted_entities 暴露）
        for entity in entities:
            if entity.name not in low_level_ranked:
                low_level_ranked.append(entity.name)

        if entities and len(entities) >= 2:
            paths = self.graph_store.find_paths(
                entities[0].id,
                entities[1].id,
                max_depth=3
            )
            reasoned_paths = self.reasoning_engine.infer(paths)
            results["reasoning_result"] = {
                "path_count": len(reasoned_paths),
                "best_path": reasoned_paths[0].total_weight if reasoned_paths else 0
            }

        # P2-9: High-level 检索（主题层面）
        high_level_ranked: List[str] = []
        hit_topics = self._detect_query_topics(query)
        if hit_topics:
            topic_entity_ids: Set[str] = set()
            for topic in hit_topics[:5]:
                topic_entities = self.graph_store.get_entities_by_topic(topic)
                for te in topic_entities:
                    if te.id not in topic_entity_ids:
                        topic_entity_ids.add(te.id)
                        if te.name not in high_level_ranked:
                            high_level_ranked.append(te.name)
            if high_level_ranked:
                results["topic_entities"] = high_level_ranked[:20]
                logger.info(
                    f"主题层面检索命中: topics={hit_topics[:5]}, "
                    f"entities={len(high_level_ranked)}"
                )

        # P2-9: RRF 融合（low-level + high-level）
        results["fused_entities"] = self._rrf_fuse_entities(low_level_ranked, high_level_ranked)

        return results

    # ------------------------------------------------------------------
    # P2-9: LightRAG 双层检索辅助方法
    # ------------------------------------------------------------------

    def _detect_query_topics(self, query: str) -> List[str]:
        """检测查询中命中的主题标签。

        匹配策略：
        1. 查询分词后，检查每个词是否命中 topic_index 键
        2. topic_index 键是否是查询的子串（覆盖多字主题）

        Args:
            query: 查询文本

        Returns:
            List[str]: 命中的主题标签列表（小写，去重）
        """
        query_lower = str(query or "").strip().lower()
        if not query_lower:
            return []

        all_topics = self.graph_store.get_all_topics()
        if not all_topics:
            return []

        hit: List[str] = []
        seen: Set[str] = set()

        # 策略1: 主题键是查询的子串（覆盖"价格"、"产品"等多字主题）
        for topic in all_topics:
            if topic in query_lower and topic not in seen:
                hit.append(topic)
                seen.add(topic)

        # 策略2: 查询分词后命中主题键（覆盖单字主题或精确匹配）
        try:
            import jieba
            query_words = [w.strip().lower() for w in jieba.cut(query_lower) if w.strip()]
        except Exception:
            query_words = re.split(r"[\s,，。、；;]+", query_lower)

        for word in query_words:
            if word in all_topics and word not in seen:
                hit.append(word)
                seen.add(word)

        return hit

    @staticmethod
    def _rrf_fuse_entities(
        low_level: List[str],
        high_level: List[str],
        k: int = 60,
    ) -> List[str]:
        """RRF（Reciprocal Rank Fusion）融合 low-level 和 high-level 检索结果。

        Args:
            low_level: low-level 检索的实体名称列表（按相关度排序）
            high_level: high-level 检索的实体名称列表（按相关度排序）
            k: RRF 平滑常数（默认 60）

        Returns:
            List[str]: 融合后按 RRF 分数排序的实体名称列表
        """
        if not low_level and not high_level:
            return []
        if not low_level:
            return list(high_level)
        if not high_level:
            return list(low_level)

        rrf_scores: Dict[str, float] = defaultdict(float)

        for rank, name in enumerate(low_level):
            rrf_scores[name] += 1.0 / (k + rank + 1)

        for rank, name in enumerate(high_level):
            rrf_scores[name] += 1.0 / (k + rank + 1)

        fused = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        return [name for name, _ in fused]

    # ------------------------------------------------------------------
    # P2-7: 图社区摘要
    # ------------------------------------------------------------------

    def build_communities(self, llm_service: Any = None) -> int:
        """构建社区摘要（社区发现 + LLM 摘要）。

        Args:
            llm_service: LLM 服务实例（覆盖实例属性）

        Returns:
            int: 社区数量
        """
        effective_llm = llm_service if llm_service is not None else self.llm_service
        self.community_summarizer.llm_service = effective_llm
        communities = self.community_summarizer.detect_communities()
        if communities and effective_llm:
            self.community_summarizer.summarize_communities(llm_service=effective_llm)
        # 持久化社区摘要
        sid = self._resolve_cache_schema_id("")
        community_path = self._get_graph_cache_path(sid).replace(".json", "_communities.json")
        self.community_summarizer.save_to_file(community_path)
        return len(communities)

    def _load_communities_cache(self, schema_id: str = "") -> bool:
        """加载社区摘要缓存。"""
        sid = self._resolve_cache_schema_id(schema_id)
        community_path = self._get_graph_cache_path(sid).replace(".json", "_communities.json")
        return self.community_summarizer.load_from_file(community_path)

    def query_related(
        self,
        entity_name: str,
        relation_type: RelationType = None
    ) -> List[Dict[str, Any]]:
        """查询相关实体"""
        entities = self.graph_store.query_by_keyword(entity_name)

        if not entities:
            return []

        entity = entities[0]
        related = self.reasoning_engine.deduce_related(entity.id, relation_type)

        return [
            {
                "name": e.name,
                "type": e.entity_type.value,
                "description": e.description
            }
            for e in related
        ]

    def get_graph_stats(self) -> Dict[str, Any]:
        """获取图谱统计信息"""
        return {
            "total_entities": len(self.graph_store.entities),
            "total_relations": sum(
                len(rels) for rels in self.graph_store.relations.values()
            ),
            "entity_types": {
                etype.value: len(eids)
                for etype, eids in self.graph_store.type_index.items()
            },
            "initialized": self._initialized
        }

    def add_entity(self, entity: Entity) -> bool:
        """手动添加实体"""
        return self.graph_store.add_entity(entity)

    def add_relation(self, relation: Relation) -> bool:
        """手动添加关系"""
        return self.graph_store.add_relation(relation)


_knowledge_graph_instances: Dict[str, KnowledgeGraphService] = {}


def get_knowledge_graph_service(enterprise_id: str = "") -> KnowledgeGraphService:
    """
    获取知识图谱服务实例

    .. deprecated::
        此工厂函数已被 ``get_knowledge_service().graph`` 取代。
        新代码请使用 ``from src.common.knowledge_service_adapter import get_knowledge_service`` 。
        本函数保留仅为向后兼容，将在未来版本移除。
    """
    import warnings as _w
    _w.warn(
        "get_knowledge_graph_service() 已废弃，请使用 get_knowledge_service().graph 替代。"
        " 迁移指南: from src.common.knowledge_service_adapter import get_knowledge_service",
        DeprecationWarning,
        stacklevel=2,
    )
    normalized_enterprise = str(enterprise_id or "").strip()
    instance = _knowledge_graph_instances.get(normalized_enterprise)
    if instance is None:
        instance = KnowledgeGraphService(enterprise_id=normalized_enterprise)
        _knowledge_graph_instances[normalized_enterprise] = instance
    return instance
