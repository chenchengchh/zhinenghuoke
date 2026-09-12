"""
优先级决策引擎

负责根据多种因素决定客户服务优先级
"""

from typing import Dict, Any, Optional
from loguru import logger


class PriorityDecisionEngine:
    """
    优先级决策引擎

    根据客户价值、问题紧急度、风险等级等因素
    计算并决定客户服务处理的优先级
    """

    def __init__(self):
        self.base_priority_weights = {
            "customer_value": 0.3,
            "urgency": 0.3,
            "risk": 0.2,
            "intent": 0.2
        }

    def decide_priority(
        self,
        customer_data: Dict[str, Any],
        message: str,
        intent: str,
        risk_level: str = "low"
    ) -> Dict[str, Any]:
        """
        决定处理优先级

        Args:
            customer_data: 客户数据
            message: 客户消息
            intent: 客户意图
            risk_level: 风险等级

        Returns:
            优先级决策结果
        """
        result = {
            "priority_score": 0.0,
            "priority_level": "normal",
            "priority_factors": {},
            "recommended_action": ""
        }

        try:
            customer_value = self._evaluate_customer_value(customer_data)
            urgency = self._evaluate_urgency(message, intent)
            risk = self._risk_level_to_score(risk_level)
            intent_score = self._evaluate_intent(intent)

            priority_score = (
                customer_value * self.base_priority_weights["customer_value"] +
                urgency * self.base_priority_weights["urgency"] +
                risk * self.base_priority_weights["risk"] +
                intent_score * self.base_priority_weights["intent"]
            )

            result["priority_score"] = priority_score
            result["priority_factors"] = {
                "customer_value": customer_value,
                "urgency": urgency,
                "risk": risk,
                "intent": intent_score
            }

            if priority_score >= 0.8:
                result["priority_level"] = "urgent"
                result["recommended_action"] = "立即处理"
            elif priority_score >= 0.6:
                result["priority_level"] = "high"
                result["recommended_action"] = "优先处理"
            elif priority_score >= 0.4:
                result["priority_level"] = "normal"
                result["recommended_action"] = "正常排队"
            else:
                result["priority_level"] = "low"
                result["recommended_action"] = "稍后处理"

        except Exception as e:
            logger.warning(f"优先级决策异常: {e}")

        return result

    def _evaluate_customer_value(self, customer_data: Dict[str, Any]) -> float:
        """评估客户价值"""
        if not customer_data:
            return 0.5

        purchase_history = customer_data.get("purchase_history", [])
        if isinstance(purchase_history, list):
            total_spent = sum(item.get("amount", 0) for item in purchase_history)
            if total_spent > 10000:
                return 1.0
            elif total_spent > 5000:
                return 0.8
            elif total_spent > 1000:
                return 0.6
            else:
                return 0.4
        return 0.5

    def _evaluate_urgency(self, message: str, intent: str) -> float:
        """评估问题紧急度"""
        urgent_keywords = ["紧急", "马上", "立刻", "急", "尽快", "过期", "无法使用"]
        count = sum(1 for kw in urgent_keywords if kw in message)
        return min(count / 3.0, 1.0)

    def _risk_level_to_score(self, risk_level: str) -> float:
        """将风险等级转换为分数"""
        level_map = {"high": 1.0, "medium": 0.6, "low": 0.2}
        return level_map.get(risk_level, 0.5)

    def _evaluate_intent(self, intent: str) -> float:
        """评估意图得分"""
        high_priority_intents = ["purchase", "complaint", "refund", "technical_support"]
        if intent in high_priority_intents:
            return 1.0
        return 0.5


priority_decision_engine = PriorityDecisionEngine()
