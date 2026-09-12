from __future__ import annotations

from typing import Dict, List

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat


DEFAULT_CATEGORY_LABELS: Dict[str, str] = {
    "generic": "📌 通用知识",
    "product": "📦 产品介绍",
    "service": "💬 服务沟通",
    "solution": "🧩 解决方案",
    "policy": "📋 规则政策",
    "process": "🪜 流程说明",
    "faq": "❓ 常见问题",
    "company": "🏢 公司介绍",
    "price": "💰 价格费用",
    "course": "🎓 课程介绍",
    "other": "📁 其他",
}


# 通用类别关键词（跨行业通用），行业专用类别（如 attractions/food/transport 等）
# 由 schema metadata.category_profiles 提供，不在此硬编码
QUERY_CATEGORY_HINTS: Dict[str, List[str]] = {
    "product": ["产品", "方案", "功能", "课程", "班型", "服务包"],
    "service": ["服务", "客服", "售后", "支持", "咨询"],
    "process": ["流程", "步骤", "怎么开通", "怎么使用", "如何操作", "部署"],
    "price": ["价格", "费用", "多少钱", "报价", "预算", "学费"],
    "policy": ["政策", "规则", "退款", "协议", "条款"],
    "faq": ["如何", "怎么", "为什么", "可以吗", "是否", "哪里", "能"],
    "company": ["公司", "团队", "品牌", "案例", "资质"],
}


def _normalize_keywords(items: object) -> List[str]:
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def get_active_category_labels(
    *,
    enterprise_id: str = "",
    preferred_schema_id: str = "",
) -> Dict[str, str]:
    labels = dict(DEFAULT_CATEGORY_LABELS)
    try:
        active_schema = get_active_schema_with_compat(
            get_industry_schema_service(),
            enterprise_id=str(enterprise_id or "").strip(),
            preferred_schema_id=str(preferred_schema_id or "").strip(),
        )
        metadata = active_schema.get("metadata") or {}
        category_profiles = metadata.get("category_profiles") or {}
        if isinstance(category_profiles, dict):
            for category, profile in category_profiles.items():
                if not isinstance(profile, dict):
                    continue
                category_key = str(category or "").strip()
                label = str(profile.get("label") or "").strip()
                if category_key and label:
                    labels[category_key] = label
    except Exception:
        return labels
    return labels


def get_active_query_category_hints(
    *,
    enterprise_id: str = "",
    preferred_schema_id: str = "",
) -> Dict[str, List[str]]:
    hints = {
        category: list(keywords)
        for category, keywords in QUERY_CATEGORY_HINTS.items()
    }
    try:
        active_schema = get_active_schema_with_compat(
            get_industry_schema_service(),
            enterprise_id=str(enterprise_id or "").strip(),
            preferred_schema_id=str(preferred_schema_id or "").strip(),
        )
        metadata = active_schema.get("metadata") or {}
        category_profiles = metadata.get("category_profiles") or {}
        if isinstance(category_profiles, dict):
            for category, profile in category_profiles.items():
                category_key = str(category or "").strip()
                if not category_key or not isinstance(profile, dict):
                    continue
                merged = list(dict.fromkeys(hints.get(category_key, []) + _normalize_keywords(profile.get("keywords"))))
                if merged:
                    hints[category_key] = merged
    except Exception:
        return hints
    return hints
