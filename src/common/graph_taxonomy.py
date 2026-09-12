"""统一图谱分类层级常量。"""

from copy import deepcopy


DEFAULT_CATEGORY_HIERARCHY = {
    "product": {
        "label": "产品方案",
        "icon": "📦",
        "topics": {
            "overview": {"label": "产品概览", "tags": ["产品", "方案", "介绍", "概览"]},
            "features": {"label": "功能特点", "tags": ["功能", "能力", "模块", "特点"]},
        },
    },
    "service": {
        "label": "服务支持",
        "icon": "🛠️",
        "topics": {
            "delivery": {"label": "交付实施", "tags": ["交付", "实施", "部署", "上线"]},
            "support": {"label": "支持售后", "tags": ["支持", "售后", "客服", "培训"]},
        },
    },
    "process": {
        "label": "流程说明",
        "icon": "🧭",
        "topics": {
            "onboarding": {"label": "接入开通", "tags": ["开通", "接入", "配置", "对接"]},
            "usage": {"label": "使用流程", "tags": ["使用", "流程", "步骤", "安排"]},
        },
    },
    "price": {
        "label": "价格费用",
        "icon": "💰",
        "topics": {
            "pricing": {"label": "报价说明", "tags": ["价格", "费用", "报价", "预算"]},
            "policy": {"label": "政策条款", "tags": ["合同", "退款", "规则", "条款"]},
        },
    },
    "faq": {
        "label": "常见问题",
        "icon": "❓",
        "topics": {
            "general": {"label": "通用问题", "tags": ["如何", "怎么", "为什么", "是否"]},
            "company": {"label": "公司资质", "tags": ["公司", "团队", "品牌", "案例", "资质"]},
        },
    },
}


DEFAULT_CATEGORY_TO_DOMAIN = {
    "product": "product",
    "promotion": "faq",
    "after_sale": "service",
    "cooperation": "service",
    "faq": "faq",
    "policy": "price",
    "company": "faq",
    "service": "service",
    "process": "process",
    "price": "price",
    "other": "faq",
}


CATEGORY_HIERARCHY = deepcopy(DEFAULT_CATEGORY_HIERARCHY)
CATEGORY_TO_DOMAIN = dict(DEFAULT_CATEGORY_TO_DOMAIN)


def _get_active_schema(enterprise_id: str = ""):
    try:
        from .industry_schema_service import IndustrySchemaService
        service = IndustrySchemaService()
        return service.get_active_schema(enterprise_id=str(enterprise_id or "").strip()) or {}
    except Exception:
        return {}


def _build_domain_taxonomy(enterprise_id: str = ""):
    schema = _get_active_schema(enterprise_id=enterprise_id)
    metadata = schema.get("metadata") or {}
    graph_taxonomy = metadata.get("graph_taxonomy") or {}
    hierarchy = graph_taxonomy.get("hierarchy") or {}
    category_to_domain = graph_taxonomy.get("category_to_domain") or {}

    resolved_hierarchy = deepcopy(DEFAULT_CATEGORY_HIERARCHY)
    if isinstance(hierarchy, dict) and hierarchy:
        resolved_hierarchy = deepcopy(hierarchy)

    resolved_category_to_domain = dict(DEFAULT_CATEGORY_TO_DOMAIN)
    if isinstance(category_to_domain, dict) and category_to_domain:
        resolved_category_to_domain.update({
            str(key): str(value)
            for key, value in category_to_domain.items()
            if str(key).strip() and str(value).strip()
        })
    return resolved_hierarchy, resolved_category_to_domain


def refresh_graph_taxonomy(enterprise_id: str = ""):
    resolved_hierarchy, resolved_category_to_domain = _build_domain_taxonomy(enterprise_id=enterprise_id)

    CATEGORY_HIERARCHY.clear()
    CATEGORY_HIERARCHY.update(deepcopy(resolved_hierarchy))

    CATEGORY_TO_DOMAIN.clear()
    CATEGORY_TO_DOMAIN.update(dict(resolved_category_to_domain))


def get_domain_taxonomy(enterprise_id: str = ""):
    resolved_hierarchy, resolved_category_to_domain = _build_domain_taxonomy(enterprise_id=enterprise_id)
    return deepcopy(resolved_hierarchy), dict(resolved_category_to_domain)


refresh_graph_taxonomy()
