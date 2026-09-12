"""
购买意向分析增强模块 - 企业级
实现多维度购买意向评分、客户旅程分析、预测性分析
"""
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import re
import math
import logging

logger = logging.getLogger(__name__)


class PurchaseSignal(Enum):
    """购买信号类型"""
    EXPLICIT_INTENT = "explicit_intent"  # 明确购买意向
    PRICE_INQUIRY = "price_inquiry"  # 价格咨询
    FEATURE_COMPARISON = "feature_comparison"  # 功能对比
    TIMELINE_INQUIRY = "timeline_inquiry"  # 时间线咨询
    CONTACT_REQUEST = "contact_request"  # 联系方式请求
    DEMO_REQUEST = "demo_request"  # 演示请求
    OBJECTION = "objection"  # 异议/顾虑
    COMPETITOR_MENTION = "competitor_mention"  # 竞品提及
    BUDGET_DISCUSSION = "budget_discussion"  # 预算讨论
    DECISION_MAKER = "decision_maker"  # 决策者标识


class CustomerLifecycleStage(Enum):
    """客户生命周期阶段"""
    PROSPECT = "prospect"  # 潜在客户
    LEAD = "lead"  # 线索
    MQL = "mql"  # 营销合格线索
    SQL = "sql"  # 销售合格线索
    OPPORTUNITY = "opportunity"  # 商机
    CUSTOMER = "customer"  # 客户
    ADVOCATE = "advocate"  # 推广者
    CHURNED = "churned"  # 流失


class BuyingRole(Enum):
    """购买角色"""
    DECISION_MAKER = "decision_maker"  # 决策者
    INFLUENCER = "influencer"  # 影响者
    USER = "user"  # 使用者
    GATEKEEPER = "gatekeeper"  # 守门人
    CHAMPION = "champion"  # 内部支持者
    UNKNOWN = "unknown"  # 未知


@dataclass
class PurchaseIntentScore:
    """购买意向评分"""
    total_score: float = 0.0
    confidence: float = 0.0
    
    # 维度评分
    engagement_score: float = 0.0  # 参与度
    interest_score: float = 0.0  # 兴趣度
    urgency_score: float = 0.0  # 紧迫度
    authority_score: float = 0.0  # 决策权
    need_score: float = 0.0  # 需求度
    budget_score: float = 0.0  # 预算匹配度
    timeline_score: float = 0.0  # 时间线匹配度
    
    # 信号检测
    signals_detected: List[PurchaseSignal] = field(default_factory=list)
    signal_strength: Dict[str, float] = field(default_factory=dict)
    
    # 预测
    purchase_probability: float = 0.0
    estimated_deal_size: float = 0.0
    estimated_close_days: int = 0
    churn_risk: float = 0.0


@dataclass
class CustomerJourney:
    """客户旅程"""
    customer_id: str
    current_stage: CustomerLifecycleStage
    stage_entered_at: datetime
    
    # 触点记录
    touchpoints: List[Dict] = field(default_factory=list)
    
    # 转化路径
    conversion_path: List[str] = field(default_factory=list)
    
    # 关键事件
    key_events: List[Dict] = field(default_factory=list)
    
    # 阶段时长
    stage_durations: Dict[str, float] = field(default_factory=dict)
    
    # 预测下一阶段
    predicted_next_stage: Optional[CustomerLifecycleStage] = None
    predicted_transition_days: int = 0


@dataclass
class PurchaseIntentAnalysisResult:
    """购买意向分析结果"""
    customer_id: str
    platform: str
    
    # 评分
    score: PurchaseIntentScore
    
    # 客户旅程
    journey: Optional[CustomerJourney] = None
    
    # 客户画像
    buying_role: BuyingRole = BuyingRole.UNKNOWN
    lifecycle_stage: CustomerLifecycleStage = CustomerLifecycleStage.PROSPECT
    
    # 推荐行动
    recommended_actions: List[Dict] = field(default_factory=list)
    best_contact_time: Optional[str] = None
    preferred_channel: str = "douyin"
    
    # 风险提示
    risk_factors: List[str] = field(default_factory=list)
    opportunity_factors: List[str] = field(default_factory=list)
    
    # 分析详情
    analysis_details: Dict = field(default_factory=dict)


class PurchaseIntentAnalyzer:
    """
    购买意向分析器 - 企业级
    
    实现多维度购买意向分析，包括：
    1. BANT评分模型（预算、权限、需求、时间线）
    2. 购买信号检测
    3. 客户旅程分析
    4. 预测性分析
    """
    
    # BANT权重配置
    BANT_WEIGHTS = {
        "budget": 0.25,
        "authority": 0.20,
        "need": 0.30,
        "timeline": 0.25
    }
    
    # 购买信号关键词
    PURCHASE_SIGNALS = {
        PurchaseSignal.EXPLICIT_INTENT: {
            "keywords": [
                "想买", "要买", "准备买", "打算买", "决定买", "确定要",
                "下单", "订购", "购买", "成交", "签约", "付款",
                "现在就要", "马上买", "今天定", "立即购买",
                "怎么付款", "付款方式", "多少钱", "给个价"
            ],
            "weight": 1.0,
            "stage_impact": 0.3
        },
        PurchaseSignal.PRICE_INQUIRY: {
            "keywords": [
                "价格", "多少钱", "报价", "费用", "收费", "怎么卖",
                "价位", "定价", "售价", "单价", "总价", "预算",
                "便宜点", "优惠", "折扣", "活动价", "团购价"
            ],
            "weight": 0.7,
            "stage_impact": 0.15
        },
        PurchaseSignal.FEATURE_COMPARISON: {
            "keywords": [
                "对比", "比较", "区别", "哪个好", "优缺点", "差异",
                "和...比", "相比", "竞品", "同类产品", "替代方案"
            ],
            "weight": 0.6,
            "stage_impact": 0.1
        },
        PurchaseSignal.TIMELINE_INQUIRY: {
            "keywords": [
                "什么时候", "多久", "几天", "什么时候能", "需要多长时间",
                "交期", "交付时间", "到货时间", "发货时间", "上线时间"
            ],
            "weight": 0.5,
            "stage_impact": 0.1
        },
        PurchaseSignal.CONTACT_REQUEST: {
            "keywords": [
                "联系", "电话", "微信", "加微信", "留电话", "联系方式",
                "手机号", "微信号", "加好友", "私聊", "面谈"
            ],
            "weight": 0.8,
            "stage_impact": 0.2
        },
        PurchaseSignal.DEMO_REQUEST: {
            "keywords": [
                "试用", "体验", "演示", "demo", "免费试用", "先试试",
                "看效果", "测试一下", "样品", "样机"
            ],
            "weight": 0.75,
            "stage_impact": 0.2
        },
        PurchaseSignal.OBJECTION: {
            "keywords": [
                "太贵", "价格高", "买不起", "超出预算", "考虑一下",
                "再看看", "以后再说", "暂时不需要", "不太确定"
            ],
            "weight": -0.3,
            "stage_impact": -0.1
        },
        PurchaseSignal.COMPETITOR_MENTION: {
            "keywords": [
                "已经买了", "选了别家", "用了其他", "有合作方了",
                "和别人合作", "选了竞品"
            ],
            "weight": -0.5,
            "stage_impact": -0.2
        },
        PurchaseSignal.BUDGET_DISCUSSION: {
            "keywords": [
                "预算", "经费", "资金", "费用预算", "年度预算",
                "项目预算", "采购预算"
            ],
            "weight": 0.65,
            "stage_impact": 0.15
        },
        PurchaseSignal.DECISION_MAKER: {
            "keywords": [
                "我做主", "我说了算", "我决定", "我来定",
                "老板让我", "领导让我", "公司要", "我们公司"
            ],
            "weight": 0.85,
            "stage_impact": 0.25
        }
    }
    
    # 客户生命周期阶段关键词
    LIFECYCLE_KEYWORDS = {
        CustomerLifecycleStage.PROSPECT: {
            "keywords": ["你好", "在吗", "有人吗", "咨询"],
            "min_messages": 0,
            "max_messages": 2
        },
        CustomerLifecycleStage.LEAD: {
            "keywords": ["了解", "介绍", "是什么", "做什么"],
            "min_messages": 1,
            "max_messages": 5
        },
        CustomerLifecycleStage.MQL: {
            "keywords": ["功能", "特点", "价格", "多少钱"],
            "min_messages": 3,
            "max_messages": 10
        },
        CustomerLifecycleStage.SQL: {
            "keywords": ["购买", "下单", "试用", "演示", "合作"],
            "min_messages": 5,
            "max_messages": 20
        },
        CustomerLifecycleStage.OPPORTUNITY: {
            "keywords": ["付款", "签约", "合同", "定下来", "成交"],
            "min_messages": 8,
            "max_messages": 50
        },
        CustomerLifecycleStage.CUSTOMER: {
            "keywords": ["已购买", "订单", "发票", "售后"],
            "min_messages": 10,
            "max_messages": 999
        }
    }
    
    # 购买角色关键词
    BUYING_ROLE_KEYWORDS = {
        BuyingRole.DECISION_MAKER: [
            "我做主", "我说了算", "我决定", "我来定", "老板",
            "负责人", "主管", "经理", "总监"
        ],
        BuyingRole.INFLUENCER: [
            "推荐", "建议", "我觉得", "我认为", "我们团队",
            "我们需要", "我们在找"
        ],
        BuyingRole.USER: [
            "我用", "我操作", "我需要", "我的工作", "日常使用"
        ],
        BuyingRole.GATEKEEPER: [
            "帮我问", "转告", "汇报", "请示", "领导说"
        ],
        BuyingRole.CHAMPION: [
            "很感兴趣", "很想合作", "一定要", "非你们不可",
            "推荐你们", "帮你们推广"
        ]
    }
    
    # 行业特定关键词（可扩展）
    INDUSTRY_KEYWORDS = {
        "software": {
            "high_intent": ["部署", "集成", "API", "定制开发", "二次开发"],
            "medium_intent": ["功能", "系统", "平台", "解决方案"],
            "low_intent": ["了解", "咨询", "看看"]
        },
        "ecommerce": {
            "high_intent": ["下单", "付款", "发货", "库存"],
            "medium_intent": ["价格", "款式", "规格", "批发"],
            "low_intent": ["看看", "咨询", "了解"]
        },
        "service": {
            "high_intent": ["签约", "合作", "服务期", "合同"],
            "medium_intent": ["方案", "报价", "服务内容"],
            "low_intent": ["咨询", "了解", "介绍"]
        }
    }
    
    def __init__(self, industry: str = "software"):
        """
        初始化购买意向分析器
        
        Args:
            industry: 行业类型，用于加载行业特定关键词
        """
        self.industry = industry
        self.industry_keywords = self.INDUSTRY_KEYWORDS.get(industry, {})
        
    def analyze(
        self,
        customer_data: Dict,
        message_history: List[Dict] = None,
        conversation_context: Dict = None
    ) -> PurchaseIntentAnalysisResult:
        """
        分析客户购买意向
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            conversation_context: 对话上下文
            
        Returns:
            PurchaseIntentAnalysisResult: 分析结果
        """
        customer_id = customer_data.get("customer_id", customer_data.get("sec_uid", ""))
        platform = customer_data.get("platform", "douyin")
        
        # 1. 计算购买意向评分
        score = self._calculate_purchase_intent_score(
            customer_data, 
            message_history, 
            conversation_context
        )
        
        # 2. 分析客户旅程
        journey = self._analyze_customer_journey(
            customer_id,
            customer_data,
            message_history
        )
        
        # 3. 识别购买角色
        buying_role = self._identify_buying_role(customer_data, message_history)
        
        # 4. 确定生命周期阶段
        lifecycle_stage = journey.current_stage if journey else CustomerLifecycleStage.PROSPECT
        
        # 5. 生成推荐行动
        recommended_actions = self._generate_recommended_actions(
            score, 
            lifecycle_stage, 
            buying_role,
            message_history
        )
        
        # 6. 分析风险和机会
        risk_factors, opportunity_factors = self._analyze_risks_and_opportunities(
            score,
            message_history
        )
        
        # 7. 确定最佳联系时间
        best_contact_time = self._determine_best_contact_time(message_history)
        
        # 8. 构建分析详情
        analysis_details = {
            "signal_summary": self._summarize_signals(score.signals_detected),
            "engagement_metrics": self._calculate_engagement_metrics(message_history),
            "content_analysis": self._analyze_content_patterns(message_history),
            "temporal_patterns": self._analyze_temporal_patterns(message_history)
        }
        
        return PurchaseIntentAnalysisResult(
            customer_id=customer_id,
            platform=platform,
            score=score,
            journey=journey,
            buying_role=buying_role,
            lifecycle_stage=lifecycle_stage,
            recommended_actions=recommended_actions,
            best_contact_time=best_contact_time,
            risk_factors=risk_factors,
            opportunity_factors=opportunity_factors,
            analysis_details=analysis_details
        )
    
    def _calculate_purchase_intent_score(
        self,
        customer_data: Dict,
        message_history: List[Dict] = None,
        conversation_context: Dict = None
    ) -> PurchaseIntentScore:
        """
        计算购买意向评分
        
        使用BANT模型 + 购买信号检测
        """
        score = PurchaseIntentScore()
        
        if not message_history:
            score.confidence = 0.1
            return score
        
        # 1. 检测购买信号
        all_content = " ".join([
            msg.get("content", "") for msg in message_history
            if msg.get("direction") == "inbound"
        ])
        
        for signal_type, signal_config in self.PURCHASE_SIGNALS.items():
            for keyword in signal_config["keywords"]:
                if keyword in all_content:
                    if signal_type not in score.signals_detected:
                        score.signals_detected.append(signal_type)
                    score.signal_strength[signal_type.value] = score.signal_strength.get(
                        signal_type.value, 0
                    ) + signal_config["weight"]
        
        # 2. 计算各维度评分
        score.budget_score = self._calculate_budget_score(all_content, message_history)
        score.authority_score = self._calculate_authority_score(all_content, customer_data)
        score.need_score = self._calculate_need_score(all_content, message_history)
        score.timeline_score = self._calculate_timeline_score(all_content, message_history)
        
        # 3. 计算参与度评分
        score.engagement_score = self._calculate_engagement_score(message_history)
        
        # 4. 计算兴趣度评分
        score.interest_score = self._calculate_interest_score(all_content, message_history)
        
        # 5. 计算紧迫度评分
        score.urgency_score = self._calculate_urgency_score(all_content, message_history)
        
        # 6. 计算总分
        bant_score = (
            score.budget_score * self.BANT_WEIGHTS["budget"] +
            score.authority_score * self.BANT_WEIGHTS["authority"] +
            score.need_score * self.BANT_WEIGHTS["need"] +
            score.timeline_score * self.BANT_WEIGHTS["timeline"]
        )
        
        # 信号加成
        signal_bonus = sum(score.signal_strength.values()) * 5
        
        # 计算总分
        score.total_score = min(100, max(0, 
            bant_score * 0.5 +
            score.engagement_score * 0.15 +
            score.interest_score * 0.15 +
            score.urgency_score * 0.1 +
            signal_bonus * 0.1
        ))
        
        # 7. 计算置信度
        score.confidence = self._calculate_confidence(message_history, score.signals_detected)
        
        # 8. 预测购买概率
        score.purchase_probability = self._predict_purchase_probability(score)
        
        # 9. 预估成交金额
        score.estimated_deal_size = self._estimate_deal_size(customer_data, score)
        
        # 10. 预估成交时间
        score.estimated_close_days = self._estimate_close_days(score, message_history)
        
        # 11. 计算流失风险
        score.churn_risk = self._calculate_churn_risk(score, message_history)
        
        return score
    
    def _calculate_budget_score(self, content: str, messages: List[Dict]) -> float:
        """计算预算匹配度评分"""
        score = 50  # 默认中等
        
        budget_keywords = {
            "positive": ["预算充足", "预算够", "有预算", "经费到位", "资金到位"],
            "negative": ["没预算", "预算不够", "超出预算", "资金紧张", "经费不足"],
            "inquiry": ["预算", "多少钱", "价格", "费用", "成本"]
        }
        
        for kw in budget_keywords["positive"]:
            if kw in content:
                score += 20
        
        for kw in budget_keywords["negative"]:
            if kw in content:
                score -= 25
        
        for kw in budget_keywords["inquiry"]:
            if kw in content:
                score += 10
        
        return min(100, max(0, score))
    
    def _calculate_authority_score(self, content: str, customer_data: Dict) -> float:
        """计算决策权评分"""
        score = 50  # 默认中等
        
        authority_keywords = {
            "high": ["我做主", "我说了算", "我决定", "老板", "负责人", "经理", "总监"],
            "medium": ["推荐", "建议", "我们团队", "我们需要"],
            "low": ["帮我问", "转告", "汇报", "请示", "问下领导"]
        }
        
        for kw in authority_keywords["high"]:
            if kw in content:
                score += 30
        
        for kw in authority_keywords["medium"]:
            if kw in content:
                score += 15
        
        for kw in authority_keywords["low"]:
            if kw in content:
                score -= 20
        
        return min(100, max(0, score))
    
    def _calculate_need_score(self, content: str, messages: List[Dict]) -> float:
        """计算需求度评分"""
        score = 50
        
        need_keywords = {
            "strong": ["急需", "必须", "一定要", "非...不可", "刚需"],
            "medium": ["需要", "想要", "打算", "考虑", "准备"],
            "weak": ["可能需要", "也许", "看看", "了解一下", "随便看看"]
        }
        
        for kw in need_keywords["strong"]:
            if kw in content:
                score += 25
        
        for kw in need_keywords["medium"]:
            if kw in content:
                score += 15
        
        for kw in need_keywords["weak"]:
            if kw in content:
                score -= 10
        
        # 根据消息数量调整
        if messages:
            inbound_count = sum(1 for m in messages if m.get("direction") == "inbound")
            score += min(inbound_count * 2, 20)
        
        return min(100, max(0, score))
    
    def _calculate_timeline_score(self, content: str, messages: List[Dict]) -> float:
        """计算时间线匹配度评分"""
        score = 50
        
        timeline_keywords = {
            "urgent": ["马上", "立即", "今天", "明天", "这周", "急需", "急用"],
            "short": ["下周", "半个月", "一个月内", "近期"],
            "medium": ["下个月", "季度", "几个月"],
            "long": ["明年", "以后", "再说", "暂时", "先看看"]
        }
        
        for kw in timeline_keywords["urgent"]:
            if kw in content:
                score += 30
        
        for kw in timeline_keywords["short"]:
            if kw in content:
                score += 20
        
        for kw in timeline_keywords["medium"]:
            if kw in content:
                score += 10
        
        for kw in timeline_keywords["long"]:
            if kw in content:
                score -= 15
        
        return min(100, max(0, score))
    
    def _calculate_engagement_score(self, messages: List[Dict]) -> float:
        """计算参与度评分"""
        if not messages:
            return 0
        
        score = 0
        
        # 消息数量
        inbound_count = sum(1 for m in messages if m.get("direction") == "inbound")
        score += min(inbound_count * 5, 30)
        
        # 回复率
        outbound_count = sum(1 for m in messages if m.get("direction") == "outbound")
        if outbound_count > 0:
            reply_rate = inbound_count / outbound_count
            score += min(reply_rate * 20, 20)
        
        # 消息长度
        avg_length = sum(len(m.get("content", "")) for m in messages) / len(messages)
        score += min(avg_length / 5, 20)
        
        # 互动频率（最近7天）
        recent_messages = [
            m for m in messages 
            if self._is_recent_message(m, days=7)
        ]
        score += min(len(recent_messages) * 3, 30)
        
        return min(100, score)
    
    def _calculate_interest_score(self, content: str, messages: List[Dict]) -> float:
        """计算兴趣度评分"""
        score = 0
        
        # 行业特定关键词
        if self.industry_keywords:
            for kw in self.industry_keywords.get("high_intent", []):
                if kw in content:
                    score += 15
            for kw in self.industry_keywords.get("medium_intent", []):
                if kw in content:
                    score += 8
            for kw in self.industry_keywords.get("low_intent", []):
                if kw in content:
                    score += 3
        
        # 通用兴趣关键词
        interest_keywords = [
            "功能", "特点", "优势", "怎么用", "教程", "案例",
            "演示", "试用", "体验", "效果", "性能"
        ]
        for kw in interest_keywords:
            if kw in content:
                score += 5
        
        return min(100, score)
    
    def _calculate_urgency_score(self, content: str, messages: List[Dict]) -> float:
        """计算紧迫度评分"""
        score = 0
        
        urgency_keywords = {
            "high": ["马上", "立即", "今天", "明天", "急需", "紧急", "急用"],
            "medium": ["这周", "下周", "尽快", "早点", "尽快"],
            "low": ["不急", "慢慢", "以后", "再说", "考虑"]
        }
        
        for kw in urgency_keywords["high"]:
            if kw in content:
                score += 25
        
        for kw in urgency_keywords["medium"]:
            if kw in content:
                score += 15
        
        for kw in urgency_keywords["low"]:
            if kw in content:
                score -= 10
        
        return min(100, max(0, score))
    
    def _calculate_confidence(self, messages: List[Dict], signals: List[PurchaseSignal]) -> float:
        """计算置信度"""
        if not messages:
            return 0.1
        
        confidence = 0.3
        
        # 消息数量增加置信度
        confidence += min(len(messages) * 0.02, 0.3)
        
        # 信号数量增加置信度
        confidence += min(len(signals) * 0.05, 0.3)
        
        # 最近活跃度
        recent_count = sum(1 for m in messages if self._is_recent_message(m, days=3))
        confidence += min(recent_count * 0.02, 0.1)
        
        return min(1.0, confidence)
    
    def _predict_purchase_probability(self, score: PurchaseIntentScore) -> float:
        """预测购买概率"""
        # 基于评分的基准概率
        base_prob = score.total_score / 100
        
        # 信号加成
        signal_bonus = len(score.signals_detected) * 0.05
        
        # 置信度调整
        confidence_adjusted = base_prob * score.confidence
        
        return min(0.99, max(0.01, confidence_adjusted + signal_bonus))
    
    def _estimate_deal_size(self, customer_data: Dict, score: PurchaseIntentScore) -> float:
        """预估成交金额"""
        base_amount = 1000
        
        # 根据意向等级调整
        if score.total_score >= 80:
            base_amount *= 3
        elif score.total_score >= 60:
            base_amount *= 2
        elif score.total_score >= 40:
            base_amount *= 1.5
        
        # 合作意向加成
        if PurchaseSignal.BUDGET_DISCUSSION in score.signals_detected:
            base_amount *= 1.5
        
        return base_amount
    
    def _estimate_close_days(self, score: PurchaseIntentScore, messages: List[Dict]) -> int:
        """预估成交天数"""
        base_days = 30
        
        if score.urgency_score >= 70:
            base_days = 7
        elif score.urgency_score >= 50:
            base_days = 14
        elif score.urgency_score >= 30:
            base_days = 21
        
        # 高意向缩短时间
        if score.total_score >= 70:
            base_days = int(base_days * 0.7)
        
        return max(1, base_days)
    
    def _calculate_churn_risk(self, score: PurchaseIntentScore, messages: List[Dict]) -> float:
        """计算流失风险"""
        risk = 0.0
        
        # 低意向增加风险
        if score.total_score < 30:
            risk += 0.4
        elif score.total_score < 50:
            risk += 0.2
        
        # 异议信号增加风险
        if PurchaseSignal.OBJECTION in score.signals_detected:
            risk += 0.2
        
        # 竞品信号增加风险
        if PurchaseSignal.COMPETITOR_MENTION in score.signals_detected:
            risk += 0.3
        
        # 长时间未互动增加风险
        if messages:
            last_inbound = None
            for m in reversed(messages):
                if m.get("direction") == "inbound":
                    last_inbound = m
                    break
            
            if last_inbound and not self._is_recent_message(last_inbound, days=7):
                risk += 0.2
        
        return min(1.0, risk)
    
    def _analyze_customer_journey(
        self,
        customer_id: str,
        customer_data: Dict,
        messages: List[Dict]
    ) -> Optional[CustomerJourney]:
        """分析客户旅程"""
        if not messages:
            return None
        
        journey = CustomerJourney(
            customer_id=customer_id,
            current_stage=CustomerLifecycleStage.PROSPECT,
            stage_entered_at=datetime.now()
        )
        
        # 确定当前阶段
        all_content = " ".join([m.get("content", "") for m in messages])
        
        stage_scores = {}
        for stage, config in self.LIFECYCLE_KEYWORDS.items():
            stage_scores[stage] = 0
            for kw in config["keywords"]:
                if kw in all_content:
                    stage_scores[stage] += 1
            
            # 消息数量匹配
            msg_count = len(messages)
            if config["min_messages"] <= msg_count <= config["max_messages"]:
                stage_scores[stage] += 2
        
        # 选择得分最高的阶段
        if stage_scores:
            best_stage = max(stage_scores.items(), key=lambda x: x[1])
            if best_stage[1] > 0:
                journey.current_stage = best_stage[0]
        
        # 记录触点
        for msg in messages:
            journey.touchpoints.append({
                "timestamp": msg.get("created_at", msg.get("timestamp", "")),
                "type": "message",
                "direction": msg.get("direction", ""),
                "content_preview": msg.get("content", "")[:50]
            })
        
        # 预测下一阶段
        journey.predicted_next_stage = self._predict_next_stage(journey.current_stage)
        journey.predicted_transition_days = self._predict_transition_days(
            journey.current_stage, 
            messages
        )
        
        return journey
    
    def _predict_next_stage(self, current_stage: CustomerLifecycleStage) -> Optional[CustomerLifecycleStage]:
        """预测下一阶段"""
        progression = {
            CustomerLifecycleStage.PROSPECT: CustomerLifecycleStage.LEAD,
            CustomerLifecycleStage.LEAD: CustomerLifecycleStage.MQL,
            CustomerLifecycleStage.MQL: CustomerLifecycleStage.SQL,
            CustomerLifecycleStage.SQL: CustomerLifecycleStage.OPPORTUNITY,
            CustomerLifecycleStage.OPPORTUNITY: CustomerLifecycleStage.CUSTOMER,
            CustomerLifecycleStage.CUSTOMER: CustomerLifecycleStage.ADVOCATE,
        }
        return progression.get(current_stage)
    
    def _predict_transition_days(self, current_stage: CustomerLifecycleStage, messages: List[Dict]) -> int:
        """预测阶段转换天数"""
        base_days = {
            CustomerLifecycleStage.PROSPECT: 7,
            CustomerLifecycleStage.LEAD: 14,
            CustomerLifecycleStage.MQL: 21,
            CustomerLifecycleStage.SQL: 14,
            CustomerLifecycleStage.OPPORTUNITY: 7,
        }
        return base_days.get(current_stage, 14)
    
    def _identify_buying_role(self, customer_data: Dict, messages: List[Dict]) -> BuyingRole:
        """识别购买角色"""
        if not messages:
            return BuyingRole.UNKNOWN
        
        all_content = " ".join([
            m.get("content", "") for m in messages
            if m.get("direction") == "inbound"
        ])
        
        role_scores = {role: 0 for role in BuyingRole}
        
        for role, keywords in self.BUYING_ROLE_KEYWORDS.items():
            for kw in keywords:
                if kw in all_content:
                    role_scores[role] += 1
        
        best_role = max(role_scores.items(), key=lambda x: x[1])
        if best_role[1] > 0:
            return best_role[0]
        
        return BuyingRole.UNKNOWN
    
    def _generate_recommended_actions(
        self,
        score: PurchaseIntentScore,
        lifecycle_stage: CustomerLifecycleStage,
        buying_role: BuyingRole,
        messages: List[Dict]
    ) -> List[Dict]:
        """生成推荐行动"""
        actions = []
        
        # 基于评分的行动
        if score.total_score >= 70:
            actions.append({
                "priority": "high",
                "action": "immediate_follow_up",
                "description": "高意向客户，建议立即跟进",
                "timing": "now"
            })
            actions.append({
                "priority": "high",
                "action": "schedule_demo",
                "description": "安排产品演示或试用",
                "timing": "within_24h"
            })
        elif score.total_score >= 50:
            actions.append({
                "priority": "medium",
                "action": "nurture_lead",
                "description": "中等意向，持续培育",
                "timing": "within_48h"
            })
            actions.append({
                "priority": "medium",
                "action": "send_case_study",
                "description": "发送成功案例和资料",
                "timing": "within_24h"
            })
        else:
            actions.append({
                "priority": "low",
                "action": "drip_campaign",
                "description": "低意向，纳入培育流程",
                "timing": "within_week"
            })
        
        # 基于生命周期的行动
        if lifecycle_stage == CustomerLifecycleStage.SQL:
            actions.append({
                "priority": "high",
                "action": "sales_handoff",
                "description": "销售合格线索，转交销售团队",
                "timing": "now"
            })
        
        # 基于购买角色的行动
        if buying_role == BuyingRole.DECISION_MAKER:
            actions.append({
                "priority": "high",
                "action": "executive_engagement",
                "description": "决策者，提供高管级别沟通",
                "timing": "now"
            })
        elif buying_role == BuyingRole.GATEKEEPER:
            actions.append({
                "priority": "medium",
                "action": "build_relationship",
                "description": "守门人，建立关系获取引荐",
                "timing": "ongoing"
            })
        
        # 基于信号的行动
        if PurchaseSignal.OBJECTION in score.signals_detected:
            actions.append({
                "priority": "high",
                "action": "handle_objection",
                "description": "检测到异议，需要针对性解决",
                "timing": "now"
            })
        
        if PurchaseSignal.COMPETITOR_MENTION in score.signals_detected:
            actions.append({
                "priority": "high",
                "action": "competitive_positioning",
                "description": "提及竞品，强调差异化优势",
                "timing": "now"
            })
        
        return actions
    
    def _analyze_risks_and_opportunities(
        self,
        score: PurchaseIntentScore,
        messages: List[Dict]
    ) -> Tuple[List[str], List[str]]:
        """分析风险和机会"""
        risks = []
        opportunities = []
        
        # 风险分析
        if score.churn_risk > 0.5:
            risks.append("高流失风险，需要紧急干预")
        
        if PurchaseSignal.OBJECTION in score.signals_detected:
            risks.append("存在价格/产品异议")
        
        if PurchaseSignal.COMPETITOR_MENTION in score.signals_detected:
            risks.append("正在考虑竞品")
        
        if score.timeline_score < 30:
            risks.append("购买时间线不明确或过长")
        
        # 机会分析
        if score.total_score >= 70:
            opportunities.append("高购买意向，接近成交")
        
        if PurchaseSignal.CONTACT_REQUEST in score.signals_detected:
            opportunities.append("主动请求联系方式，购买意愿强")
        
        if PurchaseSignal.DEMO_REQUEST in score.signals_detected:
            opportunities.append("请求演示，进入决策阶段")
        
        if score.urgency_score >= 70:
            opportunities.append("紧迫度高，可加速成交")
        
        if score.authority_score >= 70:
            opportunities.append("可能是决策者，直接沟通")
        
        return risks, opportunities
    
    def _determine_best_contact_time(self, messages: List[Dict]) -> Optional[str]:
        """确定最佳联系时间"""
        if not messages:
            return None
        
        # 分析消息时间分布
        hour_counts = {}
        for msg in messages:
            timestamp = msg.get("created_at", msg.get("timestamp", ""))
            if timestamp:
                try:
                    if isinstance(timestamp, str):
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    else:
                        dt = timestamp
                    hour = dt.hour
                    hour_counts[hour] = hour_counts.get(hour, 0) + 1
                except Exception:
                    pass
        
        if hour_counts:
            best_hour = max(hour_counts.items(), key=lambda x: x[1])[0]
            return f"{best_hour}:00-{best_hour+2}:00"
        
        return "9:00-11:00"  # 默认上午
    
    def _is_recent_message(self, message: Dict, days: int = 7) -> bool:
        """检查消息是否在最近N天内"""
        timestamp = message.get("created_at", message.get("timestamp", ""))
        if not timestamp:
            return False
        
        try:
            if isinstance(timestamp, str):
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                dt = timestamp
            
            return (datetime.now() - dt).days <= days
        except Exception:
            return False
    
    def _summarize_signals(self, signals: List[PurchaseSignal]) -> Dict:
        """总结信号"""
        return {
            "total_signals": len(signals),
            "positive_signals": [s.value for s in signals if self.PURCHASE_SIGNALS[s]["weight"] > 0],
            "negative_signals": [s.value for s in signals if self.PURCHASE_SIGNALS[s]["weight"] < 0],
            "signal_strength": sum(self.PURCHASE_SIGNALS[s]["weight"] for s in signals)
        }
    
    def _calculate_engagement_metrics(self, messages: List[Dict]) -> Dict:
        """计算参与度指标"""
        if not messages:
            return {}
        
        inbound = [m for m in messages if m.get("direction") == "inbound"]
        outbound = [m for m in messages if m.get("direction") == "outbound"]
        
        return {
            "total_messages": len(messages),
            "inbound_count": len(inbound),
            "outbound_count": len(outbound),
            "avg_message_length": sum(len(m.get("content", "")) for m in messages) / len(messages),
            "response_rate": len(outbound) / len(inbound) if inbound else 0
        }
    
    def _analyze_content_patterns(self, messages: List[Dict]) -> Dict:
        """分析内容模式"""
        if not messages:
            return {}
        
        all_content = " ".join([m.get("content", "") for m in messages])
        
        return {
            "question_count": all_content.count("？") + all_content.count("?"),
            "exclamation_count": all_content.count("！") + all_content.count("!"),
            "has_contact_info": bool(re.search(r'1[3-9]\d{9}', all_content)),
            "has_email": bool(re.search(r'[\w.-]+@[\w.-]+', all_content))
        }
    
    def _analyze_temporal_patterns(self, messages: List[Dict]) -> Dict:
        """分析时间模式"""
        if not messages:
            return {}
        
        timestamps = []
        for msg in messages:
            ts = msg.get("created_at", msg.get("timestamp", ""))
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    timestamps.append(dt)
                except Exception:
                    pass
        
        if not timestamps:
            return {}
        
        return {
            "first_message": min(timestamps).isoformat(),
            "last_message": max(timestamps).isoformat(),
            "message_span_days": (max(timestamps) - min(timestamps)).days,
            "avg_messages_per_day": len(messages) / max((max(timestamps) - min(timestamps)).days, 1)
        }


def get_purchase_intent_analyzer(industry: str = "software") -> PurchaseIntentAnalyzer:
    """
    获取购买意向分析器实例
    
    Args:
        industry: 行业类型
        
    Returns:
        PurchaseIntentAnalyzer: 分析器实例
    """
    return PurchaseIntentAnalyzer(industry=industry)


class EnhancedCustomerProfiler:
    """
    增强型客户画像分析器
    
    提供更精准的客户定位和多维度分析
    """
    
    # 企业规模特征关键词
    COMPANY_SIZE_KEYWORDS = {
        "enterprise": {
            "keywords": ["集团", "上市公司", "世界500强", "中国500强", "大型企业", "跨国", "总部", "分公司", "子公司"],
            "weight": 1.0
        },
        "mid_market": {
            "keywords": ["中型企业", "成长型", "B轮", "C轮", "pre-IPO", "独角兽"],
            "weight": 0.8
        },
        "smb": {
            "keywords": ["中小企业", "创业公司", "初创", "工作室", "小店", "个体"],
            "weight": 0.6
        },
        "startup": {
            "keywords": ["创业", "天使轮", "A轮", "种子轮", "孵化器"],
            "weight": 0.5
        }
    }
    
    # 行业特征关键词
    INDUSTRY_KEYWORDS = {
        "ecommerce": ["电商", "淘宝", "天猫", "京东", "拼多多", "直播带货", "网店", "店铺"],
        "education": ["教育", "培训", "学校", "学院", "课程", "学员", "招生"],
        "finance": ["金融", "银行", "保险", "证券", "投资", "理财", "基金"],
        "healthcare": ["医疗", "医院", "诊所", "健康", "医药", "器械"],
        "manufacturing": ["制造", "工厂", "生产", "加工", "供应链"],
        "retail": ["零售", "门店", "连锁", "超市", "百货"],
        "technology": ["科技", "软件", "互联网", "IT", "SaaS", "开发"],
        "realestate": ["房地产", "物业", "楼盘", "房产", "置业"],
        "services": ["服务", "咨询", "代理", "中介", "广告"]
    }
    
    # 决策风格特征
    DECISION_STYLE_KEYWORDS = {
        "analytical": {
            "keywords": ["对比", "分析", "数据", "报表", "详细", "参数", "规格", "评测", "测试"],
            "description": "分析型决策者 - 需要详细数据和对比分析"
        },
        "directive": {
            "keywords": ["直接", "马上", "立即", "现在", "快速", "效率", "结果"],
            "description": "指令型决策者 - 注重效率和快速结果"
        },
        "conceptual": {
            "keywords": ["创新", "未来", "趋势", "愿景", "战略", "长期", "合作"],
            "description": "概念型决策者 - 关注长期价值和战略合作"
        },
        "behavioral": {
            "keywords": ["信任", "关系", "推荐", "口碑", "案例", "服务", "支持"],
            "description": "行为型决策者 - 重视信任关系和服务支持"
        }
    }
    
    # 沟通偏好特征
    COMMUNICATION_PREFERENCES = {
        "formal": {
            "keywords": ["您好", "请问", "贵公司", "感谢", "麻烦", "劳驾"],
            "description": "正式沟通风格"
        },
        "casual": {
            "keywords": ["哈", "呢", "呀", "吧", "哦", "嗯", "好的"],
            "description": "轻松沟通风格"
        },
        "technical": {
            "keywords": ["API", "接口", "集成", "部署", "配置", "技术", "开发"],
            "description": "技术导向沟通"
        },
        "business": {
            "keywords": ["ROI", "成本", "收益", "效率", "转化", "业绩", "增长"],
            "description": "业务导向沟通"
        }
    }
    
    # 购买阶段信号
    BUYING_STAGE_SIGNALS = {
        "awareness": {
            "keywords": ["了解", "介绍", "是什么", "做什么", "功能"],
            "weight": 0.2
        },
        "interest": {
            "keywords": ["感兴趣", "想了解", "看看", "考虑", "对比"],
            "weight": 0.4
        },
        "evaluation": {
            "keywords": ["试用", "演示", "案例", "效果", "价格", "方案"],
            "weight": 0.6
        },
        "decision": {
            "keywords": ["签约", "购买", "下单", "开通", "合作", "付款"],
            "weight": 0.8
        },
        "purchase": {
            "keywords": ["合同", "发票", "账号", "实施", "培训"],
            "weight": 1.0
        }
    }
    
    def __init__(self):
        """初始化增强型客户画像分析器"""
        self.purchase_analyzer = PurchaseIntentAnalyzer()
    
    def analyze_customer_profile(
        self,
        customer_data: Dict,
        message_history: List[Dict] = None
    ) -> Dict:
        """
        分析客户画像
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            Dict: 客户画像分析结果
        """
        all_content = ""
        if message_history:
            all_content = " ".join([
                msg.get("content", "") for msg in message_history
                if msg.get("direction") == "inbound"
            ])
        
        profile = {
            "company_profile": self._analyze_company_profile(all_content, customer_data),
            "industry_profile": self._analyze_industry(all_content),
            "decision_style": self._analyze_decision_style(all_content),
            "communication_preference": self._analyze_communication_preference(all_content),
            "buying_stage": self._analyze_buying_stage(all_content),
            "engagement_pattern": self._analyze_engagement_pattern(message_history),
            "interest_topics": self._extract_interest_topics(all_content),
            "pain_points": self._extract_pain_points(all_content),
            "budget_indicators": self._analyze_budget_indicators(all_content),
            "timeline_indicators": self._analyze_timeline_indicators(all_content)
        }
        
        profile["overall_score"] = self._calculate_overall_profile_score(profile)
        profile["recommendation"] = self._generate_recommendation(profile)
        
        return profile
    
    def _analyze_company_profile(self, content: str, customer_data: Dict) -> Dict:
        """分析企业规模"""
        result = {
            "estimated_size": "unknown",
            "confidence": 0.0,
            "indicators": []
        }
        
        for size, config in self.COMPANY_SIZE_KEYWORDS.items():
            matched_keywords = [kw for kw in config["keywords"] if kw in content]
            if matched_keywords:
                result["estimated_size"] = size
                result["confidence"] = config["weight"]
                result["indicators"] = matched_keywords
                break
        
        if customer_data:
            if customer_data.get("company"):
                result["company_name"] = customer_data["company"]
            if customer_data.get("employee_count"):
                result["employee_count"] = customer_data["employee_count"]
        
        return result
    
    def _analyze_industry(self, content: str) -> Dict:
        """分析行业特征"""
        result = {
            "primary_industry": "unknown",
            "secondary_industries": [],
            "confidence": 0.0,
            "matched_keywords": []
        }
        
        industry_scores = {}
        for industry, keywords in self.INDUSTRY_KEYWORDS.items():
            matched = [kw for kw in keywords if kw in content]
            if matched:
                industry_scores[industry] = len(matched)
                result["matched_keywords"].extend(matched)
        
        if industry_scores:
            sorted_industries = sorted(industry_scores.items(), key=lambda x: x[1], reverse=True)
            result["primary_industry"] = sorted_industries[0][0]
            result["secondary_industries"] = [i[0] for i in sorted_industries[1:3]]
            result["confidence"] = min(sorted_industries[0][1] / 5, 1.0)
        
        return result
    
    def _analyze_decision_style(self, content: str) -> Dict:
        """分析决策风格"""
        result = {
            "primary_style": "unknown",
            "style_scores": {},
            "description": ""
        }
        
        for style, config in self.DECISION_STYLE_KEYWORDS.items():
            matched_count = sum(1 for kw in config["keywords"] if kw in content)
            if matched_count > 0:
                result["style_scores"][style] = matched_count
        
        if result["style_scores"]:
            result["primary_style"] = max(result["style_scores"].items(), key=lambda x: x[1])[0]
            result["description"] = self.DECISION_STYLE_KEYWORDS[result["primary_style"]]["description"]
        
        return result
    
    def _analyze_communication_preference(self, content: str) -> Dict:
        """分析沟通偏好"""
        result = {
            "primary_preference": "unknown",
            "preference_scores": {},
            "description": ""
        }
        
        for pref, config in self.COMMUNICATION_PREFERENCES.items():
            matched_count = sum(1 for kw in config["keywords"] if kw in content)
            if matched_count > 0:
                result["preference_scores"][pref] = matched_count
        
        if result["preference_scores"]:
            result["primary_preference"] = max(result["preference_scores"].items(), key=lambda x: x[1])[0]
            result["description"] = self.COMMUNICATION_PREFERENCES[result["primary_preference"]]["description"]
        
        return result
    
    def _analyze_buying_stage(self, content: str) -> Dict:
        """分析购买阶段"""
        result = {
            "current_stage": "awareness",
            "stage_scores": {},
            "progress_percentage": 0
        }
        
        for stage, config in self.BUYING_STAGE_SIGNALS.items():
            matched_count = sum(1 for kw in config["keywords"] if kw in content)
            if matched_count > 0:
                result["stage_scores"][stage] = matched_count * config["weight"]
        
        if result["stage_scores"]:
            result["current_stage"] = max(result["stage_scores"].items(), key=lambda x: x[1])[0]
            stage_order = list(self.BUYING_STAGE_SIGNALS.keys())
            stage_index = stage_order.index(result["current_stage"])
            result["progress_percentage"] = int((stage_index + 1) / len(stage_order) * 100)
        
        return result
    
    def _analyze_engagement_pattern(self, messages: List[Dict]) -> Dict:
        """分析互动模式"""
        if not messages:
            return {"pattern": "none", "metrics": {}}
        
        inbound = [m for m in messages if m.get("direction") == "inbound"]
        outbound = [m for m in messages if m.get("direction") == "outbound"]
        
        avg_response_time = self._calculate_avg_response_time(messages)
        message_intervals = self._calculate_message_intervals(messages)
        
        pattern = "passive"
        if len(inbound) > len(outbound) * 0.8:
            pattern = "active"
        elif len(inbound) > len(outbound) * 0.5:
            pattern = "moderate"
        
        return {
            "pattern": pattern,
            "metrics": {
                "total_messages": len(messages),
                "inbound_count": len(inbound),
                "outbound_count": len(outbound),
                "avg_response_time_minutes": avg_response_time,
                "message_intervals_avg_hours": message_intervals,
                "engagement_rate": len(inbound) / len(outbound) if outbound else 0
            }
        }
    
    def _calculate_avg_response_time(self, messages: List[Dict]) -> float:
        """计算平均响应时间（分钟）"""
        response_times = []
        
        for i in range(1, len(messages)):
            if messages[i].get("direction") == "inbound" and messages[i-1].get("direction") == "outbound":
                try:
                    t1 = self._parse_timestamp(messages[i-1].get("created_at", ""))
                    t2 = self._parse_timestamp(messages[i].get("created_at", ""))
                    if t1 and t2:
                        diff = (t2 - t1).total_seconds() / 60
                        if diff >= 0:
                            response_times.append(diff)
                except Exception:
                    pass
        
        return sum(response_times) / len(response_times) if response_times else 0
    
    def _calculate_message_intervals(self, messages: List[Dict]) -> float:
        """计算消息间隔时间（小时）"""
        timestamps = []
        for msg in messages:
            ts = self._parse_timestamp(msg.get("created_at", ""))
            if ts:
                timestamps.append(ts)
        
        if len(timestamps) < 2:
            return 0
        
        timestamps.sort()
        intervals = []
        for i in range(1, len(timestamps)):
            diff = (timestamps[i] - timestamps[i-1]).total_seconds() / 3600
            intervals.append(diff)
        
        return sum(intervals) / len(intervals) if intervals else 0
    
    def _parse_timestamp(self, ts) -> Optional[datetime]:
        """解析时间戳"""
        if not ts:
            return None
        try:
            if isinstance(ts, str):
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return ts
        except Exception:
            return None
    
    def _extract_interest_topics(self, content: str) -> List[Dict]:
        """提取兴趣主题"""
        topics = []
        
        topic_keywords = {
            "产品功能": ["功能", "特点", "能力", "支持", "可以"],
            "价格方案": ["价格", "费用", "收费", "套餐", "优惠", "折扣"],
            "技术实现": ["技术", "接口", "集成", "部署", "开发"],
            "服务支持": ["服务", "支持", "培训", "售后", "客服"],
            "案例效果": ["案例", "效果", "成功", "客户", "体验"],
            "合作模式": ["合作", "代理", "分销", "加盟", "渠道"]
        }
        
        for topic, keywords in topic_keywords.items():
            matched = [kw for kw in keywords if kw in content]
            if matched:
                topics.append({
                    "topic": topic,
                    "matched_keywords": matched,
                    "interest_level": len(matched) / len(keywords)
                })
        
        return sorted(topics, key=lambda x: x["interest_level"], reverse=True)
    
    def _extract_pain_points(self, content: str) -> List[Dict]:
        """提取痛点"""
        pain_points = []
        
        pain_patterns = {
            "效率问题": ["效率低", "太慢", "浪费时间", "人工", "手动", "繁琐"],
            "成本问题": ["成本高", "太贵", "预算有限", "资金紧张", "投入大"],
            "管理问题": ["管理难", "混乱", "不统一", "分散", "难追踪"],
            "获客问题": ["获客难", "流量少", "转化低", "客户少", "没客户"],
            "服务问题": ["服务差", "响应慢", "体验差", "投诉", "不满意"]
        }
        
        for pain, keywords in pain_patterns.items():
            matched = [kw for kw in keywords if kw in content]
            if matched:
                pain_points.append({
                    "pain_point": pain,
                    "matched_keywords": matched,
                    "severity": len(matched) / len(keywords)
                })
        
        return sorted(pain_points, key=lambda x: x["severity"], reverse=True)
    
    def _analyze_budget_indicators(self, content: str) -> Dict:
        """分析预算指标"""
        indicators = {
            "has_budget": False,
            "budget_range": "unknown",
            "budget_confidence": 0.0,
            "signals": []
        }
        
        positive_signals = ["有预算", "预算充足", "资金到位", "经费到位", "可以投入"]
        negative_signals = ["没预算", "预算不够", "超出预算", "资金紧张", "经费不足"]
        range_signals = {
            "high": ["几十万", "百万", "预算充足"],
            "medium": ["几万", "预算够", "可以接受"],
            "low": ["几千", "预算有限", "便宜"]
        }
        
        for signal in positive_signals:
            if signal in content:
                indicators["has_budget"] = True
                indicators["signals"].append({"type": "positive", "keyword": signal})
        
        for signal in negative_signals:
            if signal in content:
                indicators["signals"].append({"type": "negative", "keyword": signal})
        
        for range_name, keywords in range_signals.items():
            for kw in keywords:
                if kw in content:
                    indicators["budget_range"] = range_name
                    break
        
        if indicators["signals"]:
            positive_count = sum(1 for s in indicators["signals"] if s["type"] == "positive")
            negative_count = sum(1 for s in indicators["signals"] if s["type"] == "negative")
            indicators["budget_confidence"] = max(0, (positive_count - negative_count) / max(len(indicators["signals"]), 1))
        
        return indicators
    
    def _analyze_timeline_indicators(self, content: str) -> Dict:
        """分析时间线指标"""
        indicators = {
            "urgency": "unknown",
            "estimated_timeline": "unknown",
            "signals": []
        }
        
        urgency_signals = {
            "immediate": ["马上", "立即", "今天", "明天", "急需", "紧急"],
            "short": ["这周", "下周", "半个月", "一个月内"],
            "medium": ["下个月", "季度", "几个月"],
            "long": ["明年", "以后", "再说", "暂时", "先看看"]
        }
        
        for urgency, keywords in urgency_signals.items():
            for kw in keywords:
                if kw in content:
                    indicators["urgency"] = urgency
                    indicators["signals"].append(kw)
                    break
        
        timeline_mapping = {
            "immediate": "1-3天",
            "short": "1-2周",
            "medium": "1-3个月",
            "long": "3个月以上",
            "unknown": "未知"
        }
        indicators["estimated_timeline"] = timeline_mapping[indicators["urgency"]]
        
        return indicators
    
    def _calculate_overall_profile_score(self, profile: Dict) -> float:
        """计算综合画像评分"""
        score = 0.0
        
        # 企业规模权重
        size_weights = {"enterprise": 1.0, "mid_market": 0.8, "smb": 0.6, "startup": 0.5, "unknown": 0.3}
        score += size_weights.get(profile["company_profile"]["estimated_size"], 0.3) * 15
        
        # 购买阶段权重
        stage_weights = {"purchase": 1.0, "decision": 0.8, "evaluation": 0.6, "interest": 0.4, "awareness": 0.2}
        score += stage_weights.get(profile["buying_stage"]["current_stage"], 0.2) * 25
        
        # 预算指标权重
        score += profile["budget_indicators"]["budget_confidence"] * 20
        
        # 互动模式权重
        pattern_weights = {"active": 1.0, "moderate": 0.7, "passive": 0.4, "none": 0.1}
        score += pattern_weights.get(profile["engagement_pattern"]["pattern"], 0.1) * 15
        
        # 紧迫度权重
        urgency_weights = {"immediate": 1.0, "short": 0.8, "medium": 0.5, "long": 0.2, "unknown": 0.3}
        score += urgency_weights.get(profile["timeline_indicators"]["urgency"], 0.3) * 15
        
        # 痛点权重
        if profile["pain_points"]:
            score += min(len(profile["pain_points"]) * 3, 10)
        
        return min(100, score)
    
    def _generate_recommendation(self, profile: Dict) -> Dict:
        """生成推荐策略"""
        recommendations = {
            "priority": "medium",
            "approach": [],
            "timing": "normal",
            "key_points": [],
            "risk_factors": []
        }
        
        # 基于评分确定优先级
        overall_score = profile["overall_score"]
        if overall_score >= 70:
            recommendations["priority"] = "high"
            recommendations["timing"] = "immediate"
        elif overall_score >= 50:
            recommendations["priority"] = "medium"
            recommendations["timing"] = "within_week"
        else:
            recommendations["priority"] = "low"
            recommendations["timing"] = "nurture"
        
        # 基于决策风格推荐沟通方式
        style = profile["decision_style"]["primary_style"]
        if style == "analytical":
            recommendations["approach"].append("提供详细数据和对比分析")
            recommendations["key_points"].append("准备产品规格和案例数据")
        elif style == "directive":
            recommendations["approach"].append("直接高效沟通，快速给结果")
            recommendations["key_points"].append("突出效率和快速交付")
        elif style == "conceptual":
            recommendations["approach"].append("强调长期价值和战略合作")
            recommendations["key_points"].append("展示行业趋势和愿景")
        elif style == "behavioral":
            recommendations["approach"].append("建立信任关系，提供优质服务")
            recommendations["key_points"].append("分享成功案例和客户评价")
        
        # 基于购买阶段推荐行动
        stage = profile["buying_stage"]["current_stage"]
        if stage == "awareness":
            recommendations["approach"].append("教育引导，建立认知")
        elif stage == "interest":
            recommendations["approach"].append("激发兴趣，展示价值")
        elif stage == "evaluation":
            recommendations["approach"].append("提供试用，对比优势")
        elif stage == "decision":
            recommendations["approach"].append("促成决策，消除顾虑")
        elif stage == "purchase":
            recommendations["approach"].append("快速成交，完善服务")
        
        # 风险因素
        if profile["budget_indicators"]["signals"]:
            negative_budget = [s for s in profile["budget_indicators"]["signals"] if s["type"] == "negative"]
            if negative_budget:
                recommendations["risk_factors"].append("预算可能存在问题")
        
        if profile["timeline_indicators"]["urgency"] == "long":
            recommendations["risk_factors"].append("购买时间线较长")
        
        if profile["engagement_pattern"]["pattern"] == "passive":
            recommendations["risk_factors"].append("互动积极性较低")
        
        return recommendations


def get_enhanced_customer_profiler() -> EnhancedCustomerProfiler:
    """
    获取增强型客户画像分析器实例
    
    Returns:
        EnhancedCustomerProfiler: 分析器实例
    """
    return EnhancedCustomerProfiler()
