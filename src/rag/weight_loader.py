"""RAG 融合权重加载器（P0-3）

按优先级加载 RAG 四路融合权重：
    schema metadata.retrieval_policy.rrf_weights > rag_weights.yaml enterprises > industries > default

消除 PipelineConfig 硬编码与 rag_weights.yaml 不一致问题。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional
from pathlib import Path

from loguru import logger

# 默认权重（与 PipelineConfig dataclass 默认值一致，作为最终兜底）
_DEFAULT_WEIGHTS: Dict[str, Any] = {
    "keyword_weight": 0.35,
    "vector_weight": 0.40,
    "bm25_weight": 0.20,
    "kg_weight": 0.20,
    "rrf_k": 60,
    "fusion": "rrf",
}

_yaml_cache: Optional[dict] = None
_yaml_cache_mtime: float = 0.0


def _load_yaml_weights() -> dict:
    """加载 rag_weights.yaml，带文件修改时间缓存。"""
    global _yaml_cache, _yaml_cache_mtime
    yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "rag_weights.yaml"
    if not yaml_path.exists():
        return {}
    try:
        current_mtime = yaml_path.stat().st_mtime
        if _yaml_cache is not None and current_mtime == _yaml_cache_mtime:
            return _yaml_cache
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _yaml_cache = data
        _yaml_cache_mtime = current_mtime
        return data
    except Exception as exc:
        logger.warning(f"加载 rag_weights.yaml 失败: {exc}")
        return {}


def _resolve_industry_from_schema(schema: Dict[str, Any]) -> str:
    """从 schema 推断行业标识（用于匹配 rag_weights.yaml industries 配置）。"""
    industry_code = str(schema.get("industry_code") or "").strip().lower()
    if industry_code:
        return industry_code
    entity_type = str(schema.get("entity_type") or "").strip().lower()
    # 旅游相关 entity_type 映射到 tourism
    if entity_type in {"route", "travel", "tour", "itinerary"}:
        return "tourism"
    if entity_type in {"course", "education"}:
        return "education"
    if entity_type in {"product", "ecommerce"}:
        return "ecommerce"
    return ""


def load_rag_weights(
    schema_id: str = "",
    enterprise_id: str = "",
    industry: str = "",
    schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """加载 RAG 融合权重。

    优先级（从高到低）：
        1. schema metadata.retrieval_policy.rrf_weights
        2. rag_weights.yaml enterprises[enterprise_id]
        3. rag_weights.yaml industries[industry]
        4. rag_weights.yaml default
        5. _DEFAULT_WEIGHTS（代码兜底）

    返回 dict 含: keyword_weight, vector_weight, bm25_weight, kg_weight, rrf_k, fusion
    """
    result = dict(_DEFAULT_WEIGHTS)

    # 4. yaml default
    yaml_data = _load_yaml_weights()
    yaml_default = yaml_data.get("default") or {}
    if isinstance(yaml_default, dict):
        for key in ("keyword_weight", "vector_weight", "bm25_weight", "kg_weight", "rrf_k", "fusion"):
            if key in yaml_default:
                result[key] = yaml_default[key]

    # 3. yaml industries[industry]
    effective_industry = industry or ""
    if not effective_industry and schema:
        effective_industry = _resolve_industry_from_schema(schema)
    if effective_industry:
        industries = yaml_data.get("industries") or {}
        industry_config = industries.get(effective_industry) or {}
        if isinstance(industry_config, dict):
            for key in ("keyword_weight", "vector_weight", "bm25_weight", "kg_weight", "rrf_k", "fusion"):
                if key in industry_config:
                    result[key] = industry_config[key]

    # 2. yaml enterprises[enterprise_id]
    if enterprise_id:
        enterprises = yaml_data.get("enterprises") or {}
        enterprise_config = enterprises.get(enterprise_id) or {}
        if isinstance(enterprise_config, dict):
            for key in ("keyword_weight", "vector_weight", "bm25_weight", "kg_weight", "rrf_k", "fusion"):
                if key in enterprise_config:
                    result[key] = enterprise_config[key]

    # 1. schema metadata.retrieval_policy.rrf_weights（最高优先级）
    if schema:
        metadata = schema.get("metadata") or {}
        retrieval_policy = metadata.get("retrieval_policy") or {}
        rrf_weights = retrieval_policy.get("rrf_weights") or {}
        if isinstance(rrf_weights, dict):
            for key in ("keyword_weight", "vector_weight", "bm25_weight", "kg_weight", "rrf_k", "fusion"):
                if key in rrf_weights:
                    result[key] = rrf_weights[key]

    return result
