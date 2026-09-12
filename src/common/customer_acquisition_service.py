"""
获客业务逻辑优化模块 - 企业级
实现客户获取、培育、转化的全流程管理
"""
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging
import json

from src.common.purchase_intent_analyzer import (
    PurchaseIntentAnalyzer,
    PurchaseIntentAnalysisResult,
    CustomerLifecycleStage,
    BuyingRole,
    PurchaseSignal
)

logger = logging.getLogger(__name__)


class LeadScore(Enum):
    """线索评分等级"""
    HOT = "hot"  # 热线索 - 立即跟进
    WARM = "warm"  # 温线索 - 优先跟进
    COOL = "cool"  # 冷线索 - 培育跟进
    COLD = "cold"  # 冷冻线索 - 长期培育


class FollowUpPriority(Enum):
    """跟进优先级"""
    URGENT = "urgent"  # 紧急 - 1小时内
    HIGH = "high"  # 高 - 4小时内
    MEDIUM = "medium"  # 中 - 24小时内
    LOW = "low"  # 低 - 72小时内
    NURTURE = "nurture"  # 培育 - 1周内


@dataclass
class LeadScoringResult:
    """线索评分结果"""
    customer_id: str
    lead_score: LeadScore
    follow_up_priority: FollowUpPriority
    
    # 评分详情
    intent_score: float
    engagement_score: float
    fit_score: float  # 客户匹配度
    timing_score: float  # 时机评分
    
    # 推荐行动
    recommended_actions: List[Dict] = field(default_factory=list)
    
    # 跟进建议
    suggested_follow_up_time: Optional[datetime] = None
    suggested_channel: str = "douyin"
    suggested_content: str = ""
    
    # 风险提示
    risk_alerts: List[str] = field(default_factory=list)
    
    # 转化预测
    conversion_probability: float = 0.0
    estimated_value: float = 0.0


@dataclass
class CustomerAcquisitionConfig:
    """获客配置"""
    # 线索评分阈值
    hot_lead_threshold: float = 70.0
    warm_lead_threshold: float = 50.0
    cool_lead_threshold: float = 30.0
    
    # 跟进时间配置（小时）
    urgent_follow_up_hours: int = 1
    high_follow_up_hours: int = 4
    medium_follow_up_hours: int = 24
    low_follow_up_hours: int = 72
    nurture_follow_up_hours: int = 168  # 1周
    
    # 自动化配置
    auto_follow_up_enabled: bool = True
    auto_nurture_enabled: bool = True
    auto_alert_enabled: bool = True
    
    # 培育配置
    nurture_message_interval_hours: int = 48
    max_nurture_messages: int = 5


class CustomerAcquisitionService:
    """
    获客业务服务 - 企业级
    
    功能：
    1. 线索评分与分级
    2. 智能跟进建议
    3. 客户培育自动化
    4. 转化预测与优化
    """
    
    # 默认配置
    DEFAULT_CONFIG = CustomerAcquisitionConfig()
    
    # 跟进话术模板
    FOLLOW_UP_TEMPLATES = {
        LeadScore.HOT: {
            "initial": "您好！感谢您的咨询。看到您对我们的产品很感兴趣，您可以直接说想了解哪一部分。",
            "price_inquiry": "关于价格，我们目前有专属优惠活动。请问您方便电话沟通吗？我可以为您详细介绍。",
            "demo_request": "好的！我可以为您安排专属演示。请问您什么时间方便？我们可以根据您的需求定制演示内容。",
            "contact_request": "好的！您可以留下方便联系的方式或直接留言，我会尽快回复您。"
        },
        LeadScore.WARM: {
            "initial": "您好！感谢关注我们的产品。请问您主要想了解哪方面的内容呢？",
            "price_inquiry": "关于价格，我们有多种方案可以满足不同需求。请问您的主要使用场景是什么？",
            "demo_request": "我们可以为您提供免费试用。请问您想试用哪个版本呢？",
            "feature_inquiry": "我们的产品功能丰富，请问您最关注哪些功能？我可以为您详细介绍。"
        },
        LeadScore.COOL: {
            "initial": "您好！欢迎了解我们的产品。如果您有任何问题，随时可以咨询我~",
            "general": "感谢您的关注！我们提供优质的产品和服务，如有需要随时联系我。",
            "follow_up": "您好！之前您咨询过我们的产品，不知道您是否还有疑问？我可以为您解答。"
        },
        LeadScore.COLD: {
            "initial": "您好！感谢您的关注。我们是XXX，专注于XXX领域，欢迎了解更多~",
            "nurture": "分享一个好消息：我们最近推出了新功能/新活动，感兴趣的话可以了解一下~"
        }
    }
    
    # 培育内容模板
    NURTURE_CONTENT = {
        "day1": {
            "content": "感谢您的关注！为您介绍一下我们的核心功能...",
            "type": "product_intro"
        },
        "day3": {
            "content": "分享一个客户成功案例，看看他们是如何使用我们的产品提升效率的...",
            "type": "case_study"
        },
        "day5": {
            "content": "限时优惠活动：现在咨询可享受专属折扣，名额有限...",
            "type": "promotion"
        },
        "day7": {
            "content": "您是否还有疑问？我可以为您安排专属顾问一对一解答...",
            "type": "follow_up"
        },
        "day14": {
            "content": "新功能上线通知：我们刚刚发布了XXX功能，可以帮助您...",
            "type": "feature_update"
        }
    }
    
    def __init__(
        self,
        config: CustomerAcquisitionConfig = None,
        database=None,
        message_sender: Callable = None
    ):
        """
        初始化获客服务
        
        Args:
            config: 获客配置
            database: 数据库实例
            message_sender: 消息发送函数
        """
        self.config = config or self.DEFAULT_CONFIG
        self.database = database
        self.message_sender = message_sender
        self.intent_analyzer = PurchaseIntentAnalyzer()
        
        # 线索缓存
        self._lead_cache: Dict[str, LeadScoringResult] = {}
        
        # 培育状态跟踪
        self._nurture_state: Dict[str, Dict] = {}
    
    def score_lead(
        self,
        customer_data: Dict,
        message_history: List[Dict] = None
    ) -> LeadScoringResult:
        """
        对线索进行评分
        
        Args:
            customer_data: 客户数据
            message_history: 消息历史
            
        Returns:
            LeadScoringResult: 线索评分结果
        """
        customer_id = customer_data.get("customer_id", customer_data.get("sec_uid", ""))
        
        # 1. 使用购买意向分析器进行分析
        intent_result = self.intent_analyzer.analyze(customer_data, message_history)
        
        # 2. 计算线索评分
        intent_score = intent_result.score.total_score
        engagement_score = intent_result.score.engagement_score
        fit_score = self._calculate_fit_score(customer_data, intent_result)
        timing_score = self._calculate_timing_score(intent_result)
        
        # 3. 确定线索等级
        lead_score = self._determine_lead_score(intent_score, engagement_score, fit_score)
        
        # 4. 确定跟进优先级
        follow_up_priority = self._determine_follow_up_priority(
            lead_score, 
            timing_score, 
            intent_result
        )
        
        # 5. 生成推荐行动
        recommended_actions = self._generate_recommended_actions(
            lead_score,
            follow_up_priority,
            intent_result
        )
        
        # 6. 计算建议跟进时间
        suggested_follow_up_time = self._calculate_follow_up_time(
            follow_up_priority,
            intent_result
        )
        
        # 7. 生成建议内容
        suggested_content = self._generate_suggested_content(
            lead_score,
            intent_result,
            message_history
        )
        
        # 8. 风险预警
        risk_alerts = self._generate_risk_alerts(intent_result)
        
        # 9. 转化预测
        conversion_probability = intent_result.score.purchase_probability
        estimated_value = intent_result.score.estimated_deal_size
        
        result = LeadScoringResult(
            customer_id=customer_id,
            lead_score=lead_score,
            follow_up_priority=follow_up_priority,
            intent_score=intent_score,
            engagement_score=engagement_score,
            fit_score=fit_score,
            timing_score=timing_score,
            recommended_actions=recommended_actions,
            suggested_follow_up_time=suggested_follow_up_time,
            suggested_channel="douyin",
            suggested_content=suggested_content,
            risk_alerts=risk_alerts,
            conversion_probability=conversion_probability,
            estimated_value=estimated_value
        )
        
        # 缓存结果
        self._lead_cache[customer_id] = result
        
        logger.info(f"线索评分: {customer_id} - {lead_score.value} (意向:{intent_score:.1f}, 参与:{engagement_score:.1f}, 匹配:{fit_score:.1f})")
        
        return result
    
    def _calculate_fit_score(self, customer_data: Dict, intent_result: PurchaseIntentAnalysisResult) -> float:
        """计算客户匹配度评分"""
        score = 50  # 默认中等
        
        # 根据购买角色调整
        if intent_result.buying_role == BuyingRole.DECISION_MAKER:
            score += 25
        elif intent_result.buying_role == BuyingRole.INFLUENCER:
            score += 15
        elif intent_result.buying_role == BuyingRole.CHAMPION:
            score += 20
        
        # 根据生命周期阶段调整
        if intent_result.lifecycle_stage == CustomerLifecycleStage.SQL:
            score += 20
        elif intent_result.lifecycle_stage == CustomerLifecycleStage.OPPORTUNITY:
            score += 30
        elif intent_result.lifecycle_stage == CustomerLifecycleStage.MQL:
            score += 10
        
        return min(100, max(0, score))
    
    def _calculate_timing_score(self, intent_result: PurchaseIntentAnalysisResult) -> float:
        """计算时机评分"""
        score = 50
        
        # 紧迫度
        score += intent_result.score.urgency_score * 0.3
        
        # 时间线匹配度
        score += intent_result.score.timeline_score * 0.2
        
        return min(100, max(0, score))
    
    def _determine_lead_score(
        self,
        intent_score: float,
        engagement_score: float,
        fit_score: float
    ) -> LeadScore:
        """确定线索等级"""
        # 综合评分
        combined_score = intent_score * 0.5 + engagement_score * 0.3 + fit_score * 0.2
        
        if combined_score >= self.config.hot_lead_threshold:
            return LeadScore.HOT
        elif combined_score >= self.config.warm_lead_threshold:
            return LeadScore.WARM
        elif combined_score >= self.config.cool_lead_threshold:
            return LeadScore.COOL
        else:
            return LeadScore.COLD
    
    def _determine_follow_up_priority(
        self,
        lead_score: LeadScore,
        timing_score: float,
        intent_result: PurchaseIntentAnalysisResult
    ) -> FollowUpPriority:
        """确定跟进优先级"""
        # 高意向信号
        high_intent_signals = [
            PurchaseSignal.EXPLICIT_INTENT,
            PurchaseSignal.CONTACT_REQUEST,
            PurchaseSignal.DEMO_REQUEST
        ]
        
        has_high_intent_signal = any(
            signal in intent_result.score.signals_detected
            for signal in high_intent_signals
        )
        
        # 根据线索等级和信号确定优先级
        if lead_score == LeadScore.HOT or has_high_intent_signal:
            if timing_score >= 70:
                return FollowUpPriority.URGENT
            else:
                return FollowUpPriority.HIGH
        elif lead_score == LeadScore.WARM:
            if timing_score >= 60:
                return FollowUpPriority.HIGH
            else:
                return FollowUpPriority.MEDIUM
        elif lead_score == LeadScore.COOL:
            return FollowUpPriority.LOW
        else:
            return FollowUpPriority.NURTURE
    
    def _generate_recommended_actions(
        self,
        lead_score: LeadScore,
        priority: FollowUpPriority,
        intent_result: PurchaseIntentAnalysisResult
    ) -> List[Dict]:
        """生成推荐行动"""
        actions = []
        
        # 基于线索等级的行动
        if lead_score == LeadScore.HOT:
            actions.append({
                "action": "immediate_contact",
                "description": "热线索，立即联系客户",
                "priority": "critical",
                "channel": "phone" if intent_result.buying_role == BuyingRole.DECISION_MAKER else "douyin"
            })
            actions.append({
                "action": "send_proposal",
                "description": "发送定制方案和报价",
                "priority": "high"
            })
        
        elif lead_score == LeadScore.WARM:
            actions.append({
                "action": "schedule_demo",
                "description": "安排产品演示",
                "priority": "high"
            })
            actions.append({
                "action": "send_case_study",
                "description": "发送相关案例和资料",
                "priority": "medium"
            })
        
        elif lead_score == LeadScore.COOL:
            actions.append({
                "action": "nurture_lead",
                "description": "纳入培育流程",
                "priority": "medium"
            })
            actions.append({
                "action": "send_intro",
                "description": "发送产品介绍",
                "priority": "low"
            })
        
        else:
            actions.append({
                "action": "long_term_nurture",
                "description": "长期培育",
                "priority": "low"
            })
        
        # 基于信号的额外行动
        if PurchaseSignal.OBJECTION in intent_result.score.signals_detected:
            actions.append({
                "action": "handle_objection",
                "description": "处理客户异议",
                "priority": "high",
                "objection_type": "price"  # 可以根据具体内容判断
            })
        
        if PurchaseSignal.COMPETITOR_MENTION in intent_result.score.signals_detected:
            actions.append({
                "action": "competitive_response",
                "description": "竞品对比响应",
                "priority": "high"
            })
        
        return actions
    
    def _calculate_follow_up_time(
        self,
        priority: FollowUpPriority,
        intent_result: PurchaseIntentAnalysisResult
    ) -> datetime:
        """计算建议跟进时间"""
        now = datetime.now()
        
        # 根据优先级确定延迟时间
        delay_hours = {
            FollowUpPriority.URGENT: self.config.urgent_follow_up_hours,
            FollowUpPriority.HIGH: self.config.high_follow_up_hours,
            FollowUpPriority.MEDIUM: self.config.medium_follow_up_hours,
            FollowUpPriority.LOW: self.config.low_follow_up_hours,
            FollowUpPriority.NURTURE: self.config.nurture_follow_up_hours
        }
        
        hours = delay_hours.get(priority, 24)
        
        # 考虑最佳联系时间
        best_time = intent_result.best_contact_time
        if best_time:
            try:
                # 解析最佳时间范围
                start_hour = int(best_time.split("-")[0].split(":")[0])
                suggested = now + timedelta(hours=hours)
                suggested = suggested.replace(hour=start_hour, minute=0, second=0)
                if suggested > now:
                    return suggested
            except Exception:
                pass
        
        return now + timedelta(hours=hours)
    
    def _generate_suggested_content(
        self,
        lead_score: LeadScore,
        intent_result: PurchaseIntentAnalysisResult,
        message_history: List[Dict] = None
    ) -> str:
        """生成建议内容"""
        templates = self.FOLLOW_UP_TEMPLATES.get(lead_score, self.FOLLOW_UP_TEMPLATES[LeadScore.COOL])
        
        # 根据信号选择模板
        if PurchaseSignal.PRICE_INQUIRY in intent_result.score.signals_detected:
            return templates.get("price_inquiry", templates.get("initial", ""))
        elif PurchaseSignal.DEMO_REQUEST in intent_result.score.signals_detected:
            return templates.get("demo_request", templates.get("initial", ""))
        elif PurchaseSignal.CONTACT_REQUEST in intent_result.score.signals_detected:
            return templates.get("contact_request", templates.get("initial", ""))
        else:
            return templates.get("initial", "")
    
    def _generate_risk_alerts(self, intent_result: PurchaseIntentAnalysisResult) -> List[str]:
        """生成风险预警"""
        alerts = []
        
        # 流失风险
        if intent_result.score.churn_risk > 0.5:
            alerts.append(f"高流失风险 ({intent_result.score.churn_risk:.0%})")
        
        # 异议风险
        if PurchaseSignal.OBJECTION in intent_result.score.signals_detected:
            alerts.append("存在购买异议")
        
        # 竞品风险
        if PurchaseSignal.COMPETITOR_MENTION in intent_result.score.signals_detected:
            alerts.append("正在考虑竞品")
        
        # 时间线风险
        if intent_result.score.timeline_score < 30:
            alerts.append("购买时间线不明确")
        
        return alerts
    
    def get_lead_score(self, customer_id: str) -> Optional[LeadScoringResult]:
        """获取缓存的线索评分"""
        return self._lead_cache.get(customer_id)
    
    def get_hot_leads(self) -> List[LeadScoringResult]:
        """获取所有热线索"""
        return [
            result for result in self._lead_cache.values()
            if result.lead_score == LeadScore.HOT
        ]
    
    def get_pending_follow_ups(self) -> List[LeadScoringResult]:
        """获取待跟进线索"""
        now = datetime.now()
        return [
            result for result in self._lead_cache.values()
            if result.suggested_follow_up_time and result.suggested_follow_up_time <= now
        ]
    
    def start_nurture_campaign(self, customer_id: str) -> Dict:
        """
        启动培育活动
        
        Args:
            customer_id: 客户ID
            
        Returns:
            Dict: 培育活动配置
        """
        if customer_id in self._nurture_state:
            return self._nurture_state[customer_id]
        
        nurture_config = {
            "customer_id": customer_id,
            "start_date": datetime.now().isoformat(),
            "current_day": 1,
            "messages_sent": 0,
            "status": "active",
            "next_message_day": 1,
            "completed": False
        }
        
        self._nurture_state[customer_id] = nurture_config
        logger.info(f"启动培育活动: {customer_id}")
        
        return nurture_config
    
    def get_nurture_message(self, customer_id: str) -> Optional[str]:
        """
        获取培育消息
        
        Args:
            customer_id: 客户ID
            
        Returns:
            Optional[str]: 培育消息内容
        """
        if customer_id not in self._nurture_state:
            return None
        
        state = self._nurture_state[customer_id]
        
        if state["completed"] or state["messages_sent"] >= self.config.max_nurture_messages:
            return None
        
        day = state["next_message_day"]
        
        # 获取对应日期的内容
        content_config = self.NURTURE_CONTENT.get(f"day{day}")
        if content_config:
            # 更新状态
            state["messages_sent"] += 1
            state["next_message_day"] = self._get_next_nurture_day(day)
            
            if state["messages_sent"] >= self.config.max_nurture_messages:
                state["completed"] = True
            
            return content_config["content"]
        
        return None
    
    def _get_next_nurture_day(self, current_day: int) -> int:
        """获取下一个培育日"""
        day_sequence = [1, 3, 5, 7, 14]
        try:
            idx = day_sequence.index(current_day)
            if idx < len(day_sequence) - 1:
                return day_sequence[idx + 1]
        except ValueError:
            pass
        return current_day + 7
    
    def analyze_conversion_funnel(self) -> Dict:
        """
        分析转化漏斗
        
        Returns:
            Dict: 漏斗分析结果
        """
        total_leads = len(self._lead_cache)
        if total_leads == 0:
            return {"message": "暂无数据"}
        
        hot_count = sum(1 for r in self._lead_cache.values() if r.lead_score == LeadScore.HOT)
        warm_count = sum(1 for r in self._lead_cache.values() if r.lead_score == LeadScore.WARM)
        cool_count = sum(1 for r in self._lead_cache.values() if r.lead_score == LeadScore.COOL)
        cold_count = sum(1 for r in self._lead_cache.values() if r.lead_score == LeadScore.COLD)
        
        avg_intent = sum(r.intent_score for r in self._lead_cache.values()) / total_leads
        avg_conversion = sum(r.conversion_probability for r in self._lead_cache.values()) / total_leads
        total_estimated_value = sum(r.estimated_value for r in self._lead_cache.values())
        
        return {
            "total_leads": total_leads,
            "funnel": {
                "hot": {"count": hot_count, "percentage": hot_count / total_leads * 100},
                "warm": {"count": warm_count, "percentage": warm_count / total_leads * 100},
                "cool": {"count": cool_count, "percentage": cool_count / total_leads * 100},
                "cold": {"count": cold_count, "percentage": cold_count / total_leads * 100}
            },
            "metrics": {
                "average_intent_score": avg_intent,
                "average_conversion_probability": avg_conversion,
                "total_estimated_value": total_estimated_value
            }
        }


def get_customer_acquisition_service(
    config: CustomerAcquisitionConfig = None,
    database=None,
    message_sender: Optional[Callable] = None
) -> CustomerAcquisitionService:
    """
    获取获客服务实例
    
    Args:
        config: 获客配置
        database: 数据库实例
        message_sender: 消息发送函数
        
    Returns:
        CustomerAcquisitionService: 服务实例
    """
    return CustomerAcquisitionService(
        config=config,
        database=database,
        message_sender=message_sender
    )
