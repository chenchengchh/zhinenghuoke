from __future__ import annotations

from .base import BaseIndustryStrategy, GenericIndustryStrategy
from .schema_driven_strategy import SchemaDrivenStrategy
from .followup_rule_engine import FollowupRuleEngine
from ..industry_schema_service import get_industry_schema_service, get_active_schema_with_compat

_GENERIC_STRATEGY = GenericIndustryStrategy()


def get_active_industry_strategy(enterprise_id: str = "") -> BaseIndustryStrategy:
    service = get_industry_schema_service()
    active_schema = get_active_schema_with_compat(service, enterprise_id=enterprise_id)
    is_domain_specific = bool(active_schema.get("is_domain_specific", False))
    if is_domain_specific:
        return SchemaDrivenStrategy(enterprise_id=enterprise_id)
    return _GENERIC_STRATEGY


__all__ = [
    "BaseIndustryStrategy",
    "GenericIndustryStrategy",
    "SchemaDrivenStrategy",
    "FollowupRuleEngine",
    "get_active_industry_strategy",
]
