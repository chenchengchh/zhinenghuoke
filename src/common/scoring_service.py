"""
客户评分统一入口

封装规则评分（AdvancedCustomerScorer）和ML评分（MLScoringModel），
提供自动选择评分路径的统一接口。

评分策略：
- ML模型可用且已训练时，优先使用ML评分
- 否则降级到规则评分
"""

from typing import Dict, List, Optional, Any

from .advanced_scoring import AdvancedCustomerScorer
from .ml_scoring import MLScoringModel


class ScoringService:
    """
    客户评分统一服务

    封装 AdvancedCustomerScorer（规则评分）和 MLScoringModel（ML评分），
    根据 ML 模型是否可用自动选择评分路径。
    """

    def __init__(self, ml_model_type: str = 'lightgbm'):
        """
        初始化评分服务

        Args:
            ml_model_type: ML模型类型 (xgboost/lightgbm/random_forest)
        """
        self._rule_scorer = AdvancedCustomerScorer()
        self._ml_scorer = MLScoringModel(model_type=ml_model_type)
        self._ml_available = self._ml_scorer.load_model()

    @property
    def ml_available(self) -> bool:
        """ML模型是否可用"""
        return self._ml_available and self._ml_scorer.is_trained

    def score_customer(
        self,
        customer_id: str,
        customer_data: Dict,
        messages: List[Dict] = None,
        transactions: List[Dict] = None,
        usage_data: Dict = None,
        behavior_data: Dict = None,
    ) -> Dict:
        """
        统一客户评分接口

        ML模型可用时优先使用ML评分，否则降级到规则评分。

        Args:
            customer_id: 客户ID
            customer_data: 客户数据
            messages: 消息历史
            transactions: 交易记录
            usage_data: 使用数据
            behavior_data: 行为数据（ML评分使用）

        Returns:
            Dict: 评分结果，包含以下字段：
                - customer_id: 客户ID
                - scoring_method: 评分方法 ('ml' 或 'rule')
                - rule_score: 规则评分结果
                - ml_score: ML评分结果（仅ML可用时）
                - overall_score: 综合评分
                - grade: 评分等级
        """
        # 始终执行规则评分作为基线
        rule_result = self._rule_scorer.score_customer(
            customer_id=customer_id,
            customer_data=customer_data,
            messages=messages,
            transactions=transactions,
            usage_data=usage_data,
        )

        result = {
            'customer_id': customer_id,
            'scoring_method': 'rule',
            'rule_score': rule_result,
            'ml_score': None,
            'overall_score': rule_result.get('overall_score', 0),
            'grade': rule_result.get('grade', 'D'),
        }

        # ML模型可用时优先使用ML评分
        if self.ml_available:
            ml_result = self._ml_scorer.predict(
                customer_data=customer_data,
                messages=messages,
                behavior_data=behavior_data,
            )
            result['scoring_method'] = 'ml'
            result['ml_score'] = {
                'probability': ml_result.probability,
                'score': ml_result.score,
                'grade': ml_result.grade,
                'confidence': ml_result.confidence,
                'feature_importance': ml_result.feature_importance,
                'top_factors': ml_result.top_factors,
            }
            result['overall_score'] = ml_result.score
            result['grade'] = ml_result.grade

        return result

    def train_ml_model(
        self,
        X: Any,
        y: Any,
        params: Dict = None,
    ) -> Dict:
        """
        训练ML评分模型

        Args:
            X: 特征矩阵
            y: 标签
            params: 模型参数

        Returns:
            Dict: 训练结果
        """
        result = self._ml_scorer.train(X, y, params)
        self._ml_available = self._ml_scorer.is_trained
        return result


_scoring_service_instance: Optional[ScoringService] = None


def get_scoring_service(ml_model_type: str = 'lightgbm') -> ScoringService:
    """
    获取评分服务单例

    Args:
        ml_model_type: ML模型类型（仅首次调用时生效）

    Returns:
        ScoringService: 评分服务实例
    """
    global _scoring_service_instance
    if _scoring_service_instance is None:
        _scoring_service_instance = ScoringService(ml_model_type=ml_model_type)
    return _scoring_service_instance
