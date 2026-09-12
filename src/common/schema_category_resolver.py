from __future__ import annotations

from typing import Any, Dict, List

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat


_DEFAULT_CATEGORY_PROFILES: Dict[str, Dict[str, Any]] = {
    "product": {"label": "产品介绍", "keywords": ["产品", "方案", "功能", "特性", "版本", "product"]},
    "service": {"label": "服务支持", "keywords": ["服务", "客服", "售后", "支持", "咨询", "service", "support"]},
    "price": {"label": "价格费用", "keywords": ["价格", "费用", "多少钱", "报价", "预算", "price", "cost"]},
    "process": {"label": "流程说明", "keywords": ["流程", "步骤", "开通", "办理", "部署", "使用", "process"]},
    "faq": {"label": "常见问题", "keywords": ["如何", "怎么", "为什么", "可以吗", "是否", "faq"]},
    "policy": {"label": "规则政策", "keywords": ["规则", "政策", "退款", "协议", "条款", "policy"]},
    "company": {"label": "公司介绍", "keywords": ["公司", "品牌", "团队", "资质", "案例", "company"]},
    "other": {"label": "其他", "keywords": []},
}


def _normalize_keywords(items: Any) -> List[str]:
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item or "").strip().lower()
        if text:
            normalized.append(text)
    return normalized


def get_active_schema_category_profiles(
    *,
    enterprise_id: str = "",
    preferred_schema_id: str = "",
) -> Dict[str, Dict[str, Any]]:
    profiles = {
        key: {
            "label": value.get("label", key),
            "keywords": list(value.get("keywords", []) or []),
        }
        for key, value in _DEFAULT_CATEGORY_PROFILES.items()
    }
    try:
        normalized_enterprise_id = str(enterprise_id or "").strip()
        normalized_preferred_schema_id = str(preferred_schema_id or "").strip()
        service = get_industry_schema_service()
        if not normalized_preferred_schema_id and not normalized_enterprise_id:
            normalized_preferred_schema_id = str(
                (service.get_settings() or {}).get("active_schema_id") or ""
            ).strip()
        active_schema = get_active_schema_with_compat(
            service,
            enterprise_id=normalized_enterprise_id,
            preferred_schema_id=normalized_preferred_schema_id,
        )
        metadata = active_schema.get("metadata") or {}
        configured_profiles = metadata.get("category_profiles") or {}
        if isinstance(configured_profiles, dict):
            for category, profile in configured_profiles.items():
                category_key = str(category or "").strip()
                if not category_key:
                    continue
                current = profiles.get(category_key, {"label": category_key, "keywords": []})
                if isinstance(profile, dict):
                    label = str(profile.get("label") or current.get("label") or category_key).strip()
                    keywords = _normalize_keywords(profile.get("keywords"))
                    if keywords or category_key not in profiles:
                        profiles[category_key] = {"label": label or category_key, "keywords": keywords}
                    else:
                        profiles[category_key] = {"label": label or category_key, "keywords": list(current.get("keywords", []))}
    except Exception:
        return profiles
    return profiles


def infer_category_from_text(
    text: str,
    default_category: str = "other",
    *,
    enterprise_id: str = "",
    preferred_schema_id: str = "",
) -> str:
    normalized = str(text or "").lower()
    profiles = get_active_schema_category_profiles(
        enterprise_id=enterprise_id,
        preferred_schema_id=preferred_schema_id,
    )
    best_category = str(default_category or "other").strip() or "other"
    best_score = 0

    for category, profile in profiles.items():
        keywords = _normalize_keywords(profile.get("keywords"))
        if not keywords:
            continue
        score = sum(normalized.count(keyword) for keyword in keywords if keyword in normalized)
        if score > best_score:
            best_score = score
            best_category = category

    return best_category if best_score > 0 else (best_category or "other")
