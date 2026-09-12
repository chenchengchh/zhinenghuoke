from __future__ import annotations

"""旅游线路销售策略兼容导出。

历史代码和测试仍会导入 `RouteSalesStrategy`，当前实现已收敛到
`SchemaDrivenStrategy`。这里保留兼容类，并补回少量旅游线路直出逻辑。
"""

import re

from .schema_driven_strategy import SchemaDrivenStrategy


class RouteSalesStrategy(SchemaDrivenStrategy):
    _DAY_TRIP_VIABILITY_HINTS = (
        "一天能够玩",
        "一天能玩",
        "一天可以玩",
        "一天玩得下来",
        "一天来得及",
    )

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).strip().lower()

    def should_prefer_grounded_plan_direct(
        self,
        service,
        message: str,
        answer_plan: dict,
    ) -> bool:
        normalized_message = self._normalize(message)
        if not normalized_message:
            return False
        if not any(hint in normalized_message for hint in self._DAY_TRIP_VIABILITY_HINTS):
            return False
        recommended_route = str((answer_plan or {}).get("recommended_route") or "").strip()
        if not recommended_route:
            return False
        reason_points = list((answer_plan or {}).get("reason_points") or [])
        return any(str(point or "").strip() for point in reason_points)

    def render_grounded_plan_direct_reply(
        self,
        service,
        *,
        mode: str,
        message: str,
        answer_plan: dict,
        route_facts: dict,
    ) -> str:
        del mode
        if not self.should_prefer_grounded_plan_direct(service, message, answer_plan):
            return ""
        recommended_route = str((answer_plan or {}).get("recommended_route") or "").strip()
        facts = dict((route_facts or {}).get(recommended_route) or {})
        highlights = [str(item or "").strip() for item in list(facts.get("highlights") or []) if str(item or "").strip()]
        highlight_text = "、".join(highlights[:3])
        pace = str(facts.get("pace") or "").strip()
        followup = str((answer_plan or {}).get("followup_action") or "").strip()
        lines = [f"{recommended_route}一天是可以玩下来的。"]
        if pace:
            lines.append(f"整体节奏是{pace}。")
        if highlight_text:
            lines.append(f"已知亮点主要是{highlight_text}。")
        if followup:
            lines.append(followup)
        cleaner = getattr(service, "_clean_rendered_sales_reply", None)
        rendered = "".join(lines).strip()
        return cleaner(rendered) if callable(cleaner) else rendered


__all__ = ["RouteSalesStrategy"]
