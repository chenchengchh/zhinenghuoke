from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from .industry_schema_service import get_industry_schema_service


DEFAULT_KNOWLEDGE_NORMALIZATION_CONFIG: Dict[str, Any] = {
    "contact_guidance_markers": [
        "留个接收资料的联系方式",
        "接收资料的联系方式",
        "方便接收资料",
        "我继续为您处理",
        "我接着帮您整理",
    ],
    "contact_soft_markers": [
        "留个联系方式",
        "留个电话",
        "方便留个联系方式",
    ],
    "label_suffix_patterns": [
        "(多少钱|价格多少钱|价格|费用|收费|票价)[？?]?$",
        "(流程怎么安排|流程安排|流程|步骤|怎么使用|怎么开通|如何开始)[？?]?$",
        "(有哪些注意事项|注意事项|包含什么|包含哪些项目|值不值得|适合.*吗)[？?]?$",
    ],
    "generic_terms": [
        "产品",
        "方案",
        "服务",
        "流程",
        "步骤",
        "价格",
    ],
    "question_rewrites": {
        "你好": ["客户打招呼时怎么回复？", ["你好", "您好"]],
        "您好": ["客户打招呼时怎么回复？", ["你好", "您好"]],
        "在吗": ["客户问在吗时怎么回复？", ["在吗", "有人吗"]],
        "联系方式": ["客户想接收资料时怎么引导留联系方式？", ["联系方式", "怎么联系你", "怎么联系您"]],
    },
}


def _merge_dict(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged.get(key) or {})
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = deepcopy(value)
    return merged


class KnowledgeNormalizationService:
    def get_config(
        self,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, Any]:
        try:
            schema = get_industry_schema_service().get_active_schema(
                enterprise_id=str(enterprise_id or "").strip(),
                preferred_schema_id=str(schema_id or "").strip(),
            ) or {}
        except Exception:
            schema = {}

        metadata = schema.get("metadata") if isinstance(schema, dict) else {}
        incoming = metadata.get("knowledge_normalization") if isinstance(metadata, dict) else {}
        if not isinstance(incoming, dict):
            incoming = {}
        return _merge_dict(DEFAULT_KNOWLEDGE_NORMALIZATION_CONFIG, incoming)


knowledge_normalization_service = KnowledgeNormalizationService()
