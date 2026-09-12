"""
风险评估引擎

负责评估客户对话中的各种风险等级
"""

from typing import Dict, Any, List, Optional
from loguru import logger


class RiskAssessmentEngine:
    """
    风险评估引擎

    负责评估客户交互过程中的综合风险等级，
    包括投诉风险、流失风险、负面传播风险等
    """

    def __init__(self):
        self.risk_thresholds = {
            "high": 0.7,
            "medium": 0.4,
            "low": 0.2
        }

    def assess_comprehensive_risk(
        self,
        customer_data: Dict[str, Any],
        conversation_history: List[Dict[str, Any]],
        context: List[str]
    ) -> Dict[str, Any]:
        """
        评估综合风险

        Args:
            customer_data: 客户数据
            conversation_history: 对话历史
            context: 上下文文本

        Returns:
            风险评估结果
        """
        result = {
            "overall_risk": "low",
            "risk_score": 0.0,
            "risk_factors": [],
            "recommendations": []
        }

        try:
            complaint_keywords = ["投诉", "举报", "差评", "曝光", "退款", "退货"]
            churn_keywords = ["不用了", "不需要", "取消", "退订", "换一家"]

            all_text = " ".join(context)

            complaint_risk = sum(1 for kw in complaint_keywords if kw in all_text) / len(complaint_keywords)
            churn_risk = sum(1 for kw in churn_keywords if kw in all_text) / len(churn_keywords)

            risk_score = (complaint_risk * 0.6 + churn_risk * 0.4)

            result["risk_score"] = risk_score

            if risk_score >= self.risk_thresholds["high"]:
                result["overall_risk"] = "high"
                result["recommendations"].append("需要立即关注，可能发生投诉或流失")
            elif risk_score >= self.risk_thresholds["medium"]:
                result["overall_risk"] = "medium"
                result["recommendations"].append("建议重点关注，及时处理客户问题")
            else:
                result["overall_risk"] = "low"
                result["recommendations"].append("正常服务，维持现有策略")

            if complaint_risk > churn_risk:
                result["risk_factors"].append("投诉风险较高")
            else:
                result["risk_factors"].append("流失风险较高")

        except Exception as e:
            logger.warning(f"风险评估异常: {e}")

        return result

    def assess_complaint_risk(self, message: str) -> float:
        """评估投诉风险"""
        complaint_keywords = ["投诉", "举报", "差评", "曝光", "退款", "退货", "不满", "失望"]
        count = sum(1 for kw in complaint_keywords if kw in message)
        return min(count / 3.0, 1.0)

    def assess_churn_risk(self, message: str) -> float:
        """评估流失风险"""
        churn_keywords = ["不用了", "不需要", "取消", "退订", "换一家", "其他家", "别家"]
        count = sum(1 for kw in churn_keywords if kw in message)
        return min(count / 3.0, 1.0)


risk_assessment_engine = RiskAssessmentEngine()
