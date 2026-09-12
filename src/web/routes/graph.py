from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from src.common.knowledge_graph import (
    Entity,
    EntityType,
    GraphStore,
    KnowledgeGraphService,
    Relation,
    RelationType,
    get_knowledge_graph_service,
)
from src.common.graph_taxonomy import CATEGORY_HIERARCHY
from src.common.unified_knowledge_service import get_unified_knowledge_service

router = APIRouter(tags=["知识图谱"])


def _get_graph_service() -> KnowledgeGraphService:
    return get_knowledge_graph_service()


def _clear_graph_store(graph_service: KnowledgeGraphService):
    graph_service.graph_store = GraphStore()
    graph_service.reasoning_engine = graph_service.reasoning_engine.__class__(graph_service.graph_store)
    graph_service._initialized = False


def _serialize_entity(entity: Entity) -> dict:
    return {
        "id": entity.id,
        "name": entity.name,
        "entityType": entity.entity_type.value,
        "description": entity.description,
        "properties": entity.properties,
        "aliases": entity.aliases,
    }


def _serialize_relation(relation: Relation) -> dict:
    return {
        "id": relation.id,
        "source_id": relation.source_id,
        "target_id": relation.target_id,
        "relationType": relation.relation_type.value,
        "weight": relation.weight,
    }


def _build_graph_snapshot(graph_service: KnowledgeGraphService) -> dict:
    entities = [_serialize_entity(entity) for entity in graph_service.graph_store.entities.values()]
    relations = []
    for rel_list in graph_service.graph_store.relations.values():
        relations.extend(_serialize_relation(relation) for relation in rel_list)
    return {"entities": entities, "relations": relations}


class GraphEntityRequest(BaseModel):
    name: str
    type: str = "concept"
    description: str = ""
    enterprise_id: str = "default"
    properties: dict = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)


class GraphRelationRequest(BaseModel):
    source_id: str
    target_id: str
    type: str = "related_to"
    enterprise_id: str = "default"
    weight: float = 1.0
    evidence: str = ""


@router.get("/api/graph/stats")
async def get_graph_stats(enterprise_id: Optional[str] = None):
    try:
        graph_service = _get_graph_service()
        stats = graph_service.get_graph_stats()
        return {
            "entity_count": stats.get("total_entities", 0),
            "relation_count": stats.get("total_relations", 0),
        }
    except ImportError:
        return {"entity_count": 0, "relation_count": 0}
    except Exception as exc:
        logger.error(f"获取图谱统计失败: {exc}")
        return {"entity_count": 0, "relation_count": 0}


@router.get("/api/graph/data")
async def get_graph_data(
    enterprise_id: Optional[str] = None,
    query: str = "",
    domain: str = "",
    entity_type: str = "",
):
    try:
        graph_service = _get_graph_service()
        data = _build_graph_snapshot(graph_service)

        if query:
            keyword_matches = {
                entity.id
                for entity in graph_service.graph_store.query_by_keyword(query)
            }
            filtered_entities = []
            for entity in graph_service.graph_store.entities.values():
                haystacks = [entity.name, entity.description, " ".join(entity.aliases)]
                if entity.id in keyword_matches or any(query.lower() in (text or "").lower() for text in haystacks):
                    filtered_entities.append(_serialize_entity(entity))

            filtered_entity_ids = {entity["id"] for entity in filtered_entities}
            filtered_relations = [
                relation
                for relation in data.get("relations", [])
                if relation.get("source_id") in filtered_entity_ids or relation.get("target_id") in filtered_entity_ids
            ]
            data = {"entities": filtered_entities, "relations": filtered_relations}

        if domain or entity_type:
            filtered_entities = []
            filtered_entity_ids = set()
            for entity in data.get("entities", []):
                props = entity.get("properties", {})
                current_type = entity.get("entityType", "")
                if domain:
                    if props.get("domain_id", "") not in ("", domain):
                        continue
                if entity_type and current_type != entity_type:
                    continue
                filtered_entities.append(entity)
                filtered_entity_ids.add(entity.get("id", ""))

            filtered_relations = []
            for relation in data.get("relations", []):
                src_id = relation.get("source_id", "") or relation.get("sourceId", "")
                tgt_id = relation.get("target_id", "") or relation.get("targetId", "")
                if src_id in filtered_entity_ids or tgt_id in filtered_entity_ids:
                    filtered_relations.append(relation)

            return {"entities": filtered_entities, "relations": filtered_relations}

        return data
    except ImportError:
        return {"entities": [], "relations": []}
    except Exception as exc:
        logger.error(f"获取图谱数据失败: {exc}")
        return {"entities": [], "relations": []}


@router.get("/api/graph/hierarchy")
async def get_graph_hierarchy():
    try:
        return {"hierarchy": CATEGORY_HIERARCHY}
    except Exception as exc:
        logger.error(f"获取分类层次失败: {exc}")
        return {"hierarchy": {}}


@router.post("/api/graph/extract")
async def extract_knowledge_graph(enterprise_id: Optional[str] = None):
    try:
        graph_service = _get_graph_service()
        _clear_graph_store(graph_service)
        knowledge_items = get_unified_knowledge_service().get_all_items()
        entity_count = graph_service.build_graph_from_knowledge(knowledge_items)
        stats = graph_service.get_graph_stats()
        return {
            "success": True,
            "entities": entity_count,
            "relations": stats.get("total_relations", 0),
        }
    except ImportError:
        return JSONResponse(status_code=500, content={"success": False, "message": "GraphRAG服务未启用"})
    except Exception as exc:
        logger.error(f"提取知识图谱失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/graph/clear")
async def clear_knowledge_graph(enterprise_id: Optional[str] = None):
    try:
        graph_service = _get_graph_service()
        _clear_graph_store(graph_service)
        return {"success": True}
    except Exception as exc:
        logger.error(f"清空图谱失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/graph/entity")
async def add_graph_entity(request: GraphEntityRequest):
    try:
        graph_service = _get_graph_service()
        try:
            entity_type = EntityType(request.type)
        except Exception:
            entity_type = EntityType.CONCEPT

        entity_id = hashlib.md5(f"{request.enterprise_id}_{request.name}".encode()).hexdigest()[:16]
        entity = Entity(
            id=entity_id,
            name=request.name,
            entity_type=entity_type,
            description=request.description,
            properties=request.properties,
            aliases=request.aliases,
        )
        graph_service.add_entity(entity)
        return {"success": True, "entity_id": entity_id}
    except Exception as exc:
        logger.error(f"添加实体失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.post("/api/graph/relation")
async def add_graph_relation(request: GraphRelationRequest):
    try:
        graph_service = _get_graph_service()
        try:
            relation_type = RelationType(request.type)
        except Exception:
            relation_type = RelationType.RELATED_TO

        relation_id = hashlib.md5(
            f"{request.source_id}_{request.type}_{request.target_id}".encode()
        ).hexdigest()[:16]
        relation = Relation(
            id=relation_id,
            source_id=request.source_id,
            target_id=request.target_id,
            relation_type=relation_type,
            weight=request.weight,
        )
        graph_service.add_relation(relation)
        return {"success": True, "relation_id": relation_id}
    except Exception as exc:
        logger.error(f"添加关系失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/graph/entity/{entity_id}")
async def get_graph_entity(entity_id: str, enterprise_id: Optional[str] = None):
    try:
        graph_service = _get_graph_service()
        entity = graph_service.graph_store.get_entity(entity_id)
        if entity:
            relations = graph_service.graph_store.get_relations(entity_id)
            related_entities = []
            for relation in relations:
                target = graph_service.graph_store.get_entity(relation.target_id)
                if target:
                    related_entities.append(
                        {
                            "relation": relation.relation_type.value,
                            "entity": _serialize_entity(target),
                        }
                    )
            return {"entity": _serialize_entity(entity), "related_entities": related_entities}
        return JSONResponse(status_code=404, content={"message": "实体不存在"})
    except Exception as exc:
        logger.error(f"获取实体详情失败: {exc}")
        return JSONResponse(status_code=500, content={"message": str(exc)})


@router.get("/api/graph/types")
async def get_graph_entity_types():
    try:
        return {
            "entity_types": [entity_type.value for entity_type in EntityType],
            "relation_types": [relation_type.value for relation_type in RelationType],
        }
    except Exception:
        return {
            "entity_types": ["product", "service", "company", "person", "concept", "faq", "event", "policy", "solution", "location"],
            "relation_types": ["has_product", "belongs_to", "related_to", "solves", "requires", "includes", "located_at", "applies_to", "answers", "similar_to", "depends_on"],
        }


@router.get("/api/graph/query")
async def query_knowledge_graph(query: str, max_paths: int = 5):
    try:
        graph_service = _get_graph_service()
        result = graph_service.query_with_reasoning(query, max_paths)
        return {"success": True, "data": result}
    except Exception as exc:
        logger.error(f"查询知识图谱失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})


@router.get("/api/graph/related")
async def get_related_entities(entity_name: str, relation_type: Optional[str] = None):
    try:
        graph_service = _get_graph_service()
        rel_type = RelationType(relation_type) if relation_type else RelationType.RELATED_TO
        result = graph_service.query_related(entity_name, rel_type)
        return {"success": True, "data": result}
    except Exception as exc:
        logger.error(f"获取相关实体失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "error": str(exc)})
