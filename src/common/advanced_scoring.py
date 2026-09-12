"""
高级客户评分模型

基于GitHub最佳实践实现的多维度客户评分系统：
- RFM分析模型
- 客户健康度评分
- 购买概率预测
- 客户生命周期价值预测
- BANT增强评分
"""
import os
import json
import math
import hashlib
import joblib
import numpy as np
from loguru import logger
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict


class RFMSegment(Enum):
    """RFM客户分群"""
    CHAMPIONS = "champions"           # 最近购买、频率高、金额高
    LOYAL_CUSTOMERS = "loyal"         # 频率高、金额高
    POTENTIAL_LOYALIST = "potential"  # 最近购买、频率中等
    NEW_CUSTOMERS = "new"             # 最近购买、频率低
    PROMISING = "promising"           # 最近购买、金额中等
    NEED_ATTENTION = "attention"      # 频率和金额中等
    ABOUT_TO_SLEEP = "sleeping"       # 较长时间未购买
    AT_RISK = "at_risk"              # 长时间未购买、频率高
    CANT_LOSE = "cant_lose"          # 长时间未购买、金额高
    HIBERNATING = "hibernating"       # 长时间未购买、频率低、金额低
    LOST = "lost"                     # 很长时间未购买


class CustomerHealthStatus(Enum):
    """客户健康状态"""
    HEALTHY = "healthy"           # 健康
    ATTENTION = "attention"       # 需关注
    AT_RISK = "at_risk"          # 有风险
    CRITICAL = "critical"         # 危急
    CHURNED = "churned"           # 已流失


@dataclass
class RFMScore:
    """RFM评分结果"""
    recency_score: int = 0        # 最近一次购买时间评分 (1-5)
    frequency_score: int = 0      # 购买频率评分 (1-5)
    monetary_score: int = 0       # 购买金额评分 (1-5)
    rfm_segment: RFMSegment = RFMSegment.HIBERNATING
    rfm_score: int = 0            # 综合RFM评分 (3-15)
    recency_days: int = 0         # 最近购买天数
    frequency_count: int = 0      # 购买次数
    monetary_total: float = 0.0   # 总消费金额


@dataclass
class HealthScore:
    """客户健康度评分"""
    overall_score: float = 0.0           # 综合健康度 (0-100)
    status: CustomerHealthStatus = CustomerHealthStatus.ATTENTION
    engagement_score: float = 0.0        # 参与度评分
    adoption_score: float = 0.0          # 采用度评分
    satisfaction_score: float = 0.0      # 满意度评分
    retention_risk: float = 0.0          # 流失风险
    churn_probability: float = 0.0       # 流失概率
    key_indicators: Dict = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class PurchaseProbability:
    """购买概率预测"""
    probability: float = 0.0             # 购买概率 (0-1)
    confidence: float = 0.0              # 置信度
    expected_value: float = 0.0          # 预期价值
    time_to_purchase: int = 0            # 预计购买天数
    purchase_signals: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)


@dataclass
class CustomerLifetimeValue:
    """客户生命周期价值"""
    clv: float = 0.0                     # 客户生命周期价值
    predicted_clv: float = 0.0           # 预测CLV
    average_order_value: float = 0.0     # 平均订单价值
    purchase_frequency: float = 0.0      # 购买频率
    customer_lifespan: float = 0.0       # 客户生命周期（月）
    retention_rate: float = 0.0          # 留存率
    profit_margin: float = 0.0           # 利润率
    clv_segment: str = ""                # CLV分群


class RFMAnalyzer:
    """
    RFM分析模型
    
    Recency - 最近一次购买时间
    Frequency - 购买频率
    Monetary - 购买金额
    """
    
    def __init__(self):
        self.recency_bins = None
        self.frequency_bins = None
        self.monetary_bins = None
    
    def analyze(
        self,
        customer_id: str,
        transactions: List[Dict],
        reference_date: datetime = None
    ) -> RFMScore:
        """
        分析客户RFM值
        
        Args:
            customer_id: 客户ID
            transactions: 交易记录列表
            reference_date: 参考日期（默认今天）
        
        Returns:
            RFMScore: RFM评分结果
        """
        if reference_date is None:
            reference_date = datetime.now()
        
        if not transactions:
            return RFMScore()
        
        # 计算R、F、M值
        recency_days = self._calculate_recency(transactions, reference_date)
        frequency_count = len(transactions)
        monetary_total = sum(t.get('amount', 0) for t in transactions)
        
        # 计算评分 (1-5分)
        recency_score = self._score_recency(recency_days)
        frequency_score = self._score_frequency(frequency_count)
        monetary_score = self._score_monetary(monetary_total)
        
        # 确定客户分群
        rfm_segment = self._determine_segment(
            recency_score, frequency_score, monetary_score
        )
        
        return RFMScore(
            recency_score=recency_score,
            frequency_score=frequency_score,
            monetary_score=monetary_score,
            rfm_segment=rfm_segment,
            rfm_score=recency_score + frequency_score + monetary_score,
            recency_days=recency_days,
            frequency_count=frequency_count,
            monetary_total=monetary_total
        )
    
    def _calculate_recency(self, transactions: List[Dict], reference_date: datetime) -> int:
        """计算最近购买天数"""
        dates = []
        for t in transactions:
            date_str = t.get('date') or t.get('created_at') or t.get('timestamp')
            if date_str:
                try:
                    if isinstance(date_str, str):
                        date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    else:
                        date = date_str
                    dates.append(date)
                except Exception as e:
                    logger.debug(f"日期解析失败: {e}")
        
        if not dates:
            return 999
        
        latest_date = max(dates)
        return (reference_date - latest_date.replace(tzinfo=None)).days
    
    def _score_recency(self, days: int) -> int:
        """评分最近购买时间"""
        if days <= 7:
            return 5
        elif days <= 14:
            return 4
        elif days <= 30:
            return 3
        elif days <= 60:
            return 2
        else:
            return 1
    
    def _score_frequency(self, count: int) -> int:
        """评分购买频率"""
        if count >= 10:
            return 5
        elif count >= 5:
            return 4
        elif count >= 3:
            return 3
        elif count >= 2:
            return 2
        else:
            return 1
    
    def _score_monetary(self, amount: float) -> int:
        """评分购买金额"""
        if amount >= 10000:
            return 5
        elif amount >= 5000:
            return 4
        elif amount >= 1000:
            return 3
        elif amount >= 500:
            return 2
        else:
            return 1
    
    def _determine_segment(
        self,
        r: int,
        f: int,
        m: int
    ) -> RFMSegment:
        """确定客户分群"""
        # 基于RFM评分确定分群
        if r >= 4 and f >= 4 and m >= 4:
            return RFMSegment.CHAMPIONS
        elif f >= 4 and m >= 4:
            return RFMSegment.LOYAL_CUSTOMERS
        elif r >= 4 and f >= 3:
            return RFMSegment.POTENTIAL_LOYALIST
        elif r >= 4 and f <= 2:
            return RFMSegment.NEW_CUSTOMERS
        elif r >= 3 and m >= 3:
            return RFMSegment.PROMISING
        elif f >= 3 and m >= 3:
            return RFMSegment.NEED_ATTENTION
        elif r <= 2 and f >= 3:
            return RFMSegment.ABOUT_TO_SLEEP
        elif r <= 2 and f >= 4:
            return RFMSegment.AT_RISK
        elif r <= 2 and m >= 4:
            return RFMSegment.CANT_LOSE
        elif r <= 2 and f <= 2 and m <= 2:
            return RFMSegment.HIBERNATING
        else:
            return RFMSegment.LOST


class CustomerHealthScorer:
    """
    客户健康度评分器
    
    多维度评估客户健康状态
    """
    
    # 健康度指标权重
    WEIGHTS = {
        'engagement': 0.25,
        'adoption': 0.25,
        'satisfaction': 0.20,
        'retention': 0.15,
        'growth': 0.15
    }
    
    def __init__(self):
        self.engagement_thresholds = {
            'high': 0.7,
            'medium': 0.4,
            'low': 0.2
        }
    
    def score(
        self,
        customer_data: Dict,
        messages: List[Dict] = None,
        usage_data: Dict = None
    ) -> HealthScore:
        """
        计算客户健康度
        
        Args:
            customer_data: 客户数据
            messages: 消息历史
            usage_data: 使用数据
        
        Returns:
            HealthScore: 健康度评分
        """
        health = HealthScore()
        
        # 计算各维度评分
        health.engagement_score = self._calculate_engagement(messages)
        health.adoption_score = self._calculate_adoption(usage_data, customer_data)
        health.satisfaction_score = self._calculate_satisfaction(messages, customer_data)
        
        # 计算流失风险
        health.retention_risk = self._calculate_retention_risk(
            health.engagement_score,
            health.adoption_score,
            messages
        )
        health.churn_probability = min(health.retention_risk * 1.2, 1.0)
        
        # 计算综合健康度
        health.overall_score = (
            health.engagement_score * self.WEIGHTS['engagement'] +
            health.adoption_score * self.WEIGHTS['adoption'] +
            health.satisfaction_score * self.WEIGHTS['satisfaction'] +
            (1 - health.retention_risk) * self.WEIGHTS['retention'] +
            self._calculate_growth_score(customer_data) * self.WEIGHTS['growth']
        ) * 100
        
        # 确定健康状态
        health.status = self._determine_status(health.overall_score, health.churn_probability)
        
        # 关键指标
        health.key_indicators = self._extract_key_indicators(
            customer_data, messages, health
        )
        
        # 推荐行动
        health.recommendations = self._generate_recommendations(health)
        
        return health
    
    def _calculate_engagement(self, messages: List[Dict]) -> float:
        """计算参与度评分"""
        if not messages:
            return 0.0
        
        inbound = [m for m in messages if m.get('direction') == 'inbound']
        outbound = [m for m in messages if m.get('direction') == 'outbound']
        
        # 响应率
        response_rate = len(inbound) / len(outbound) if outbound else 0
        
        # 最近活跃度
        recent_count = 0
        for m in messages:
            ts = m.get('created_at') or m.get('timestamp')
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        dt = ts
                    if (datetime.now() - dt.replace(tzinfo=None)).days <= 7:
                        recent_count += 1
                except Exception:
                    pass
        
        recency_score = min(recent_count / 10, 1.0)
        
        # 消息质量（平均长度）
        avg_length = sum(len(m.get('content', '')) for m in inbound) / len(inbound) if inbound else 0
        quality_score = min(avg_length / 100, 1.0)
        
        return min((response_rate * 0.4 + recency_score * 0.4 + quality_score * 0.2), 1.0)
    
    def _calculate_adoption(self, usage_data: Dict, customer_data: Dict) -> float:
        """计算采用度评分"""
        if not usage_data:
            # 基于客户数据估算
            intent_level = customer_data.get('intent_level', 'C')
            return {'A': 0.8, 'B': 0.6, 'C': 0.4, 'D': 0.2, 'E': 0.1}.get(intent_level, 0.3)
        
        # 功能使用率
        features_used = usage_data.get('features_used', 0)
        total_features = usage_data.get('total_features', 10)
        feature_adoption = features_used / total_features if total_features else 0
        
        # 登录频率
        login_frequency = usage_data.get('login_frequency', 0)
        login_score = min(login_frequency / 20, 1.0)
        
        # 使用时长
        usage_hours = usage_data.get('usage_hours', 0)
        usage_score = min(usage_hours / 100, 1.0)
        
        return min((feature_adoption * 0.5 + login_score * 0.3 + usage_score * 0.2), 1.0)
    
    def _calculate_satisfaction(self, messages: List[Dict], customer_data: Dict) -> float:
        """计算满意度评分"""
        score = 0.5
        
        if messages:
            content = ' '.join([m.get('content', '') for m in messages if m.get('direction') == 'inbound'])
            
            # 正面信号
            positive = ['谢谢', '感谢', '满意', '好', '棒', '优秀', '推荐', '喜欢']
            for kw in positive:
                if kw in content:
                    score += 0.05
            
            # 负面信号
            negative = ['投诉', '不满', '差', '问题', '不好', '失望', '退货']
            for kw in negative:
                if kw in content:
                    score -= 0.08
        
        # NPS评分
        nps = customer_data.get('nps_score')
        if nps:
            score += (nps - 5) / 10
        
        return min(max(score, 0), 1.0)
    
    def _calculate_retention_risk(
        self,
        engagement: float,
        adoption: float,
        messages: List[Dict]
    ) -> float:
        """计算留存风险"""
        risk = 0.0
        
        # 低参与度增加风险
        if engagement < 0.3:
            risk += 0.3
        elif engagement < 0.5:
            risk += 0.15
        
        # 低采用度增加风险
        if adoption < 0.3:
            risk += 0.25
        elif adoption < 0.5:
            risk += 0.1
        
        # 长时间未互动
        if messages:
            last_inbound = None
            for m in reversed(messages):
                if m.get('direction') == 'inbound':
                    last_inbound = m
                    break
            
            if last_inbound:
                ts = last_inbound.get('created_at') or last_inbound.get('timestamp')
                if ts:
                    try:
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        else:
                            dt = ts
                        days = (datetime.now() - dt.replace(tzinfo=None)).days
                        if days > 30:
                            risk += 0.2
                        elif days > 14:
                            risk += 0.1
                    except Exception:
                        pass
        
        return min(risk, 1.0)
    
    def _calculate_growth_score(self, customer_data: Dict) -> float:
        """计算增长评分"""
        score = 0.5
        
        # 意向等级
        intent_level = customer_data.get('intent_level', 'C')
        intent_scores = {'A': 1.0, 'B': 0.8, 'C': 0.5, 'D': 0.3, 'E': 0.1}
        score = intent_scores.get(intent_level, 0.5)
        
        return score
    
    def _determine_status(self, score: float, churn_prob: float) -> CustomerHealthStatus:
        """确定健康状态"""
        if score >= 80 and churn_prob < 0.2:
            return CustomerHealthStatus.HEALTHY
        elif score >= 60 and churn_prob < 0.4:
            return CustomerHealthStatus.ATTENTION
        elif score >= 40 or churn_prob < 0.6:
            return CustomerHealthStatus.AT_RISK
        elif score >= 20:
            return CustomerHealthStatus.CRITICAL
        else:
            return CustomerHealthStatus.CHURNED
    
    def _extract_key_indicators(
        self,
        customer_data: Dict,
        messages: List[Dict],
        health: HealthScore
    ) -> Dict:
        """提取关键指标"""
        indicators = {
            'last_activity': None,
            'total_interactions': len(messages) if messages else 0,
            'intent_level': customer_data.get('intent_level', 'C'),
            'engagement_trend': 'stable'
        }
        
        if messages:
            last_msg = messages[-1]
            ts = last_msg.get('created_at') or last_msg.get('timestamp')
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        dt = ts
                    indicators['last_activity'] = dt.isoformat()
                except Exception:
                    pass
        
        return indicators
    
    def _generate_recommendations(self, health: HealthScore) -> List[str]:
        """生成推荐行动"""
        recommendations = []
        
        if health.engagement_score < 0.4:
            recommendations.append("增加客户互动频率，发送个性化内容")
        
        if health.adoption_score < 0.4:
            recommendations.append("提供产品培训，引导深度使用")
        
        if health.satisfaction_score < 0.5:
            recommendations.append("主动收集反馈，解决客户痛点")
        
        if health.churn_probability > 0.5:
            recommendations.append("启动客户挽留计划，提供专属优惠")
        
        if health.status == CustomerHealthStatus.HEALTHY:
            recommendations.append("维护良好关系，探索增购机会")
        
        return recommendations


class PurchaseProbabilityPredictor:
    """
    购买概率预测器
    
    基于多维度特征预测客户购买概率
    """
    
    # 购买信号权重
    SIGNAL_WEIGHTS = {
        'price_inquiry': 0.15,
        'demo_request': 0.20,
        'budget_confirmed': 0.25,
        'decision_maker': 0.15,
        'urgent_timeline': 0.10,
        'competitor_comparison': 0.05,
        'objection_raised': -0.10
    }
    
    def predict(
        self,
        customer_data: Dict,
        messages: List[Dict] = None,
        intent_result: Dict = None
    ) -> PurchaseProbability:
        """
        预测购买概率
        
        Args:
            customer_data: 客户数据
            messages: 消息历史
            intent_result: 意向分析结果
        
        Returns:
            PurchaseProbability: 购买概率预测
        """
        result = PurchaseProbability()
        
        # 收集购买信号
        signals = self._detect_signals(messages, customer_data)
        result.purchase_signals = signals['positive']
        result.risk_factors = signals['negative']
        has_strong_buying_signal = any(
            signal in {'demo_request', 'budget_confirmed', 'decision_maker'}
            for signal in signals['positive']
        )
        
        # 计算基础概率
        base_prob = self._calculate_base_probability(customer_data, intent_result)
        
        # 信号调整
        signal_adjustment = sum(
            self.SIGNAL_WEIGHTS.get(s, 0) for s in signals['positive']
        ) - sum(
            abs(self.SIGNAL_WEIGHTS.get(s, 0)) for s in signals['negative']
        )
        
        result.probability = min(max(base_prob + signal_adjustment, 0.01), 0.99)
        # 没有真实成交数据时，进入保守模式，避免仅凭弱文本信号把概率抬得过高。
        if not has_strong_buying_signal:
            result.probability = min(result.probability, 0.35)
        
        # 计算置信度
        result.confidence = self._calculate_confidence(messages, signals)
        
        # 预期价值
        result.expected_value = self._estimate_value(customer_data, result.probability)
        
        # 预计购买时间
        result.time_to_purchase = self._estimate_time_to_purchase(
            messages, result.probability
        )
        
        # 推荐行动
        result.recommended_actions = self._generate_actions(result)
        
        return result
    
    def _detect_signals(self, messages: List[Dict], customer_data: Dict) -> Dict:
        """检测购买信号"""
        signals = {'positive': [], 'negative': []}
        
        if not messages:
            return signals
        
        content = ' '.join([m.get('content', '') for m in messages if m.get('direction') == 'inbound'])
        
        # 正面信号
        positive_patterns = {
            'price_inquiry': ['价格', '多少钱', '费用', '收费', '套餐'],
            'demo_request': ['演示', '试用', '体验', '测试'],
            'budget_confirmed': ['有预算', '预算充足', '资金到位'],
            'decision_maker': ['我做主', '我决定', '负责人', '老板'],
            'urgent_timeline': ['马上', '立即', '今天', '明天', '急需'],
            'competitor_comparison': ['对比', '比较', '其他家', '竞品']
        }
        
        for signal, keywords in positive_patterns.items():
            for kw in keywords:
                if kw in content:
                    signals['positive'].append(signal)
                    break
        
        # 负面信号
        negative_patterns = {
            'objection_raised': ['太贵', '预算不够', '不需要', '考虑一下'],
            'competitor_preference': ['已经买了', '用了其他', '选了别家'],
            'no_budget': ['没预算', '资金紧张', '经费不足']
        }
        
        for signal, keywords in negative_patterns.items():
            for kw in keywords:
                if kw in content:
                    signals['negative'].append(signal)
                    break
        
        return signals
    
    def _calculate_base_probability(
        self,
        customer_data: Dict,
        intent_result: Dict
    ) -> float:
        """计算基础概率"""
        base = 0.1
        
        # 意向等级调整
        intent_level = customer_data.get('intent_level', 'C')
        intent_adjustments = {'A': 0.2, 'B': 0.1, 'C': 0.0, 'D': -0.05, 'E': -0.1}
        base += intent_adjustments.get(intent_level, 0)
        
        # 意向分析结果
        if intent_result:
            score = intent_result.get('total_score', 0)
            base += score / 400
        
        return min(max(base, 0.02), 0.6)
    
    def _calculate_confidence(self, messages: List[Dict], signals: Dict) -> float:
        """计算置信度"""
        confidence = 0.3
        
        # 消息数量增加置信度
        if messages:
            inbound_count = sum(1 for m in messages if m.get('direction') == 'inbound')
            confidence += min(inbound_count * 0.05, 0.3)
        
        # 信号数量增加置信度
        total_signals = len(signals['positive']) + len(signals['negative'])
        confidence += min(total_signals * 0.05, 0.2)
        
        # 正面信号占比
        if total_signals > 0:
            positive_ratio = len(signals['positive']) / total_signals
            confidence += positive_ratio * 0.2
        
        return min(confidence, 0.95)
    
    def _estimate_value(self, customer_data: Dict, probability: float) -> float:
        """估算预期价值"""
        base_value = 1000
        
        # 根据概率调整
        expected = base_value * probability
        
        # 根据意向等级调整
        intent_level = customer_data.get('intent_level', 'C')
        multipliers = {'A': 3.0, 'B': 2.0, 'C': 1.0, 'D': 0.5, 'E': 0.3}
        expected *= multipliers.get(intent_level, 1.0)
        
        return expected
    
    def _estimate_time_to_purchase(self, messages: List[Dict], probability: float) -> int:
        """估算购买时间"""
        base_days = 30
        
        if probability >= 0.8:
            base_days = 7
        elif probability >= 0.6:
            base_days = 14
        elif probability >= 0.4:
            base_days = 21
        
        # 检查紧急程度
        if messages:
            content = ' '.join([m.get('content', '') for m in messages if m.get('direction') == 'inbound'])
            if any(kw in content for kw in ['马上', '立即', '今天', '明天']):
                base_days = max(1, base_days - 5)
            elif any(kw in content for kw in ['以后', '明年', '再说']):
                base_days += 30
        
        return base_days
    
    def _generate_actions(self, result: PurchaseProbability) -> List[str]:
        """生成推荐行动"""
        actions = []
        
        if result.probability >= 0.7:
            actions.append("立即安排销售跟进，提供专属优惠")
            actions.append("准备合同和报价单")
        elif result.probability >= 0.5:
            actions.append("发送产品案例和客户评价")
            actions.append("安排产品演示")
        elif result.probability >= 0.3:
            actions.append("持续培育，发送行业资讯")
            actions.append("邀请参加线上活动")
        else:
            actions.append("纳入长期培育流程")
            actions.append("定期发送有价值内容")
        
        if 'objection_raised' in result.risk_factors:
            actions.append("针对性解决客户异议")
        
        return actions


class CustomerLifetimeValuePredictor:
    """
    客户生命周期价值预测器
    """
    
    def predict(
        self,
        customer_data: Dict,
        transactions: List[Dict] = None,
        messages: List[Dict] = None
    ) -> CustomerLifetimeValue:
        """
        预测客户生命周期价值
        
        Args:
            customer_data: 客户数据
            transactions: 交易记录
            messages: 消息历史
        
        Returns:
            CustomerLifetimeValue: CLV预测结果
        """
        result = CustomerLifetimeValue()
        
        # 计算基础指标
        if transactions:
            result.average_order_value = self._calculate_aov(transactions)
            result.purchase_frequency = self._calculate_frequency(transactions)
        else:
            # 没有真实交易记录时，不再估算非零 CLV，避免把互动行为误读为已验证商业价值。
            result.average_order_value = 0.0
            result.purchase_frequency = 0.0
        
        # 估算留存率和生命周期
        if transactions:
            result.retention_rate = self._estimate_retention(customer_data, messages)
            result.customer_lifespan = 1 / (1 - result.retention_rate) if result.retention_rate < 1 else 36
        else:
            result.retention_rate = 0.0
            result.customer_lifespan = 0.0
        
        # 利润率
        result.profit_margin = 0.3  # 默认30%利润率
        
        # 计算CLV
        result.clv = self._calculate_clv(
            result.average_order_value,
            result.purchase_frequency,
            result.customer_lifespan,
            result.profit_margin
        )
        
        # 预测CLV（考虑增长潜力）
        growth_factor = self._estimate_growth_factor(customer_data, messages)
        result.predicted_clv = result.clv * growth_factor
        
        # CLV分群
        result.clv_segment = self._determine_segment(result.clv)
        
        return result
    
    def _calculate_aov(self, transactions: List[Dict]) -> float:
        """计算平均订单价值"""
        if not transactions or len(transactions) == 0:
            return 0.0
        valid_amounts = [t.get('amount', 0) for t in transactions if t.get('amount', 0) > 0]
        if not valid_amounts:
            return 0.0
        return sum(valid_amounts) / len(valid_amounts)
    
    def _calculate_frequency(self, transactions: List[Dict]) -> float:
        """计算购买频率（月均）"""
        if not transactions or len(transactions) == 0:
            return 0.0
        
        dates = []
        for t in transactions:
            ts = t.get('date') or t.get('created_at')
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        dt = ts
                    dates.append(dt)
                except Exception:
                    pass
        
        if len(dates) < 2:
            return len(transactions)
        
        dates.sort()
        months = (dates[-1] - dates[0]).days / 30
        return len(transactions) / max(months, 1)
    
    def _estimate_aov(self, customer_data: Dict, messages: List[Dict]) -> float:
        """估算平均订单价值"""
        base = 500
        
        intent_level = customer_data.get('intent_level', 'C')
        multipliers = {'A': 3.0, 'B': 2.0, 'C': 1.0, 'D': 0.6, 'E': 0.3}
        return base * multipliers.get(intent_level, 1.0)
    
    def _estimate_frequency(self, messages: List[Dict]) -> float:
        """估算购买频率"""
        if not messages:
            return 0.5
        
        inbound = [m for m in messages if m.get('direction') == 'inbound']
        return min(len(inbound) / 10, 4.0)
    
    def _estimate_retention(self, customer_data: Dict, messages: List[Dict]) -> float:
        """估算留存率"""
        base = 0.5
        
        intent_level = customer_data.get('intent_level', 'C')
        adjustments = {'A': 0.3, 'B': 0.15, 'C': 0.0, 'D': -0.1, 'E': -0.2}
        base += adjustments.get(intent_level, 0)
        
        return min(max(base, 0.1), 0.95)
    
    def _calculate_clv(
        self,
        aov: float,
        frequency: float,
        lifespan: float,
        margin: float
    ) -> float:
        """计算CLV"""
        return aov * frequency * lifespan * margin
    
    def _estimate_growth_factor(self, customer_data: Dict, messages: List[Dict]) -> float:
        """估算增长因子"""
        factor = 1.0
        
        intent_level = customer_data.get('intent_level', 'C')
        if intent_level in ['A', 'B']:
            factor += 0.3
        
        return factor
    
    def _determine_segment(self, clv: float) -> str:
        """确定CLV分群"""
        if clv <= 0:
            return "暂无真实交易"
        elif clv >= 10000:
            return "高价值客户"
        elif clv >= 5000:
            return "中高价值客户"
        elif clv >= 1000:
            return "中等价值客户"
        elif clv >= 500:
            return "潜力客户"
        else:
            return "低价值客户"


class AdvancedCustomerScorer:
    """
    高级客户评分器
    
    整合所有评分模型
    """
    
    def __init__(self):
        self.rfm_analyzer = RFMAnalyzer()
        self.health_scorer = CustomerHealthScorer()
        self.purchase_predictor = PurchaseProbabilityPredictor()
        self.clv_predictor = CustomerLifetimeValuePredictor()
    
    def score_customer(
        self,
        customer_id: str,
        customer_data: Dict,
        messages: List[Dict] = None,
        transactions: List[Dict] = None,
        usage_data: Dict = None
    ) -> Dict:
        """
        综合评分客户
        
        Args:
            customer_id: 客户ID
            customer_data: 客户数据
            messages: 消息历史
            transactions: 交易记录
            usage_data: 使用数据
        
        Returns:
            Dict: 综合评分结果
        """
        # RFM分析
        rfm_score = self.rfm_analyzer.analyze(customer_id, transactions or [])
        
        # 健康度评分
        health_score = self.health_scorer.score(customer_data, messages, usage_data)
        
        # 购买概率预测
        purchase_prob = self.purchase_predictor.predict(customer_data, messages)
        
        # CLV预测
        clv_result = self.clv_predictor.predict(customer_data, transactions, messages)
        
        # 综合评分
        overall_score = self._calculate_overall_score(
            rfm_score, health_score, purchase_prob, clv_result
        )
        
        return {
            'customer_id': customer_id,
            'overall_score': overall_score,
            'rfm': {
                'recency_score': rfm_score.recency_score,
                'frequency_score': rfm_score.frequency_score,
                'monetary_score': rfm_score.monetary_score,
                'rfm_score': rfm_score.rfm_score,
                'segment': rfm_score.rfm_segment.value,
                'recency_days': rfm_score.recency_days,
                'frequency_count': rfm_score.frequency_count,
                'monetary_total': rfm_score.monetary_total
            },
            'health': {
                'score': health_score.overall_score,
                'status': health_score.status.value,
                'engagement': health_score.engagement_score,
                'adoption': health_score.adoption_score,
                'satisfaction': health_score.satisfaction_score,
                'churn_probability': health_score.churn_probability,
                'recommendations': health_score.recommendations
            },
            'purchase_probability': {
                'probability': purchase_prob.probability,
                'confidence': purchase_prob.confidence,
                'expected_value': purchase_prob.expected_value,
                'time_to_purchase': purchase_prob.time_to_purchase,
                'signals': purchase_prob.purchase_signals,
                'risk_factors': purchase_prob.risk_factors,
                'actions': purchase_prob.recommended_actions
            },
            'clv': {
                'value': clv_result.clv,
                'predicted': clv_result.predicted_clv,
                'aov': clv_result.average_order_value,
                'frequency': clv_result.purchase_frequency,
                'lifespan': clv_result.customer_lifespan,
                'segment': clv_result.clv_segment
            },
            'grade': self._get_grade(overall_score),
            'priority': self._get_priority(overall_score, health_score.churn_probability)
        }
    
    def _calculate_overall_score(
        self,
        rfm: RFMScore,
        health: HealthScore,
        purchase: PurchaseProbability,
        clv: CustomerLifetimeValue
    ) -> float:
        """计算综合评分"""
        # RFM评分归一化
        rfm_normalized = rfm.rfm_score / 15 * 100
        
        # 综合计算
        score = (
            rfm_normalized * 0.20 +
            health.overall_score * 0.30 +
            purchase.probability * 100 * 0.30 +
            min(clv.clv / 100, 100) * 0.20
        )
        
        return min(score, 100)
    
    def _get_grade(self, score: float) -> str:
        """获取等级"""
        if score >= 80:
            return 'A'
        elif score >= 60:
            return 'B'
        elif score >= 40:
            return 'C'
        elif score >= 20:
            return 'D'
        else:
            return 'E'
    
    def _get_priority(self, score: float, churn_prob: float) -> str:
        """获取优先级"""
        if score >= 70 or churn_prob > 0.6:
            return 'high'
        elif score >= 50:
            return 'medium'
        else:
            return 'low'


def get_advanced_scorer() -> AdvancedCustomerScorer:
    """
    获取高级客户评分器实例
    
    Returns:
        AdvancedCustomerScorer: 评分器实例
    """
    return AdvancedCustomerScorer()
