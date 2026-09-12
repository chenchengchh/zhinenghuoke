from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.common.industry_schema_service import get_industry_schema_service, get_active_schema_with_compat


class FollowupRuleEngine:
    """
    通用追问规则引擎。

    从 Schema JSON 的 followup_strategy.custom_rewrite_rules 读取规则，
    通过锚点匹配 + 条件评估 + 模板渲染三步完成追问重写。

    规则格式：
    {
        "anchor_pattern": "domain_anchor",
        "rules": [
            {
                "condition": "has_elder",
                "rewrite": "domain_anchor带老人能走下来吗"
            }
        ]
    }

    条件评估器内置支持：
    - has_elder: 检测老人/带老人/老年人
    - has_child: 检测小孩/带小孩/儿童
    - has_fatigue_concern: 检测累/体力/走不动
    - has_fee_concern: 检测门票/自费/包含/费用
    - has_departure_concern: 检测几点出发/出发时间/集合时间
    - has_return_concern: 检测几点回来/回来时间/返回时间
    - has_meal_concern: 检测吃饭/午餐/含餐/包吃
    - has_transport_concern: 检测怎么去/交通/接送/集合方式
    - has_budget_concern: 检测预算/便宜/划算/性价比
    - has_value_concern: 检测值得/值不值/好不好
    - has_steady_concern: 检测稳/稳妥/安全/轻松
    - always: 始终匹配
    """

    _CONDITION_EVALUATORS = {
        "has_elder": lambda ctx: any(kw in ctx for kw in ["老人", "带老人", "老年人"]),
        "has_child": lambda ctx: any(kw in ctx for kw in ["小孩", "带小孩", "儿童", "亲子"]),
        "has_fatigue_concern": lambda ctx: any(kw in ctx for kw in ["累", "累不累", "体力", "强度"]),
        "has_fee_concern": lambda ctx: any(kw in ctx for kw in ["费用", "收费", "包含", "额外"]),
        "has_departure_concern": lambda ctx: any(kw in ctx for kw in ["几点出发", "出发时间", "什么时候出发", "开始时间"]),
        "has_return_concern": lambda ctx: any(kw in ctx for kw in ["几点回来", "回来时间", "返回时间", "结束时间"]),
        "has_meal_concern": lambda ctx: any(kw in ctx for kw in ["吃饭", "午餐", "用餐", "餐饮"]),
        "has_transport_concern": lambda ctx: any(kw in ctx for kw in ["怎么去", "交通", "接送", "出行方式"]),
        "has_budget_concern": lambda ctx: any(kw in ctx for kw in ["预算", "便宜", "划算", "性价比", "贵不贵"]),
        "has_value_concern": lambda ctx: any(kw in ctx for kw in ["值得", "值不值", "好不好", "怎么样"]),
        "has_steady_concern": lambda ctx: any(kw in ctx for kw in ["稳", "稳妥", "安全", "轻松"]),
        "always": lambda ctx: True,
    }

    def __init__(self, enterprise_id: str = ""):
        self._enterprise_id = str(enterprise_id or "").strip()
        self._rules_cache = None
        self._rules_cache_key = None

    def _get_rules(self) -> List[Dict[str, Any]]:
        service = get_industry_schema_service()
        active_schema = get_active_schema_with_compat(service, enterprise_id=self._enterprise_id)
        cache_key = f"{self._enterprise_id}::{active_schema.get('schema_id', '')}"
        if cache_key != self._rules_cache_key:
            metadata = active_schema.get("metadata") or {}
            fs_config = metadata.get("followup_strategy") or {}
            self._rules_cache = fs_config.get("custom_rewrite_rules") or []
            self._rules_cache_key = cache_key
        return self._rules_cache

    def evaluate(
        self,
        anchor: str,
        message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        rules = self._get_rules()
        context_text = self._build_context_text(message, conversation_history)

        for rule in rules:
            if rule.get("anchor_pattern") != anchor:
                continue
            for sub_rule in rule.get("rules") or []:
                condition = sub_rule.get("condition", "")
                if self._evaluate_condition(condition, context_text):
                    return sub_rule.get("rewrite", message)

        return None

    def _evaluate_condition(self, condition: str, context_text: str) -> bool:
        evaluator = self._CONDITION_EVALUATORS.get(condition)
        if evaluator:
            try:
                return evaluator(context_text)
            except Exception:
                return False
        return False

    @staticmethod
    def _build_context_text(
        message: str,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        parts = [str(message or "")]
        for msg in conversation_history or []:
            content = str(msg.get("content") or msg.get("message") or "")
            if content:
                parts.append(content)
        return " ".join(parts)

    @classmethod
    def register_condition(cls, name: str, evaluator):
        cls._CONDITION_EVALUATORS[name] = evaluator
