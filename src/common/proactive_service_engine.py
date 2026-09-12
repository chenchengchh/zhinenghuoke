"""
主动服务引擎 (ProactiveServiceEngine)
支持意图预测、服务机会检测、动作调度

功能:
1. IntentPredictor - 购买信号预测、流失预警
2. OpportunityDetector - 场景触发检测、最佳时机判断
3. ActionScheduler - 消息推送、渠道选择
"""

import logging
from typing import List, Dict, Optional, Any, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class ProactiveActionType(Enum):
    """主动服务动作类型"""
    CART_REMINDER = "cart_reminder"
    COUPON_PUSH = "coupon_push"
    PRODUCT_SUGGESTION = "product_suggestion"
    SERVICE_CHECKIN = "service_checkin"
    URGENCY_REMINDER = "urgency_reminder"
    SATISFACTION_SURVEY = "satisfaction_survey"
    RE_ENGAGEMENT = "re_engagement"
    CROSS_SELL = "cross_sell"


class TriggerScenario(Enum):
    """触发场景"""
    ABANDONED_CART = "abandoned_cart"
    BROWSING_TIMEOUT = "browsing_timeout"
    NEGATIVE_SENTIMENT = "negative_sentiment"
    INACTIVE_USER = "inactive_user"
    PRICE_INQUIRY = "price_inquiry"
    PRODUCT_VIEW = "product_view"
    PURCHASE_COMPLETE = "purchase_complete"
    SERVICE_ISSUE = "service_issue"


@dataclass
class ProactiveSignal:
    """主动服务信号"""
    signal_type: str
    customer_id: str
    session_id: str
    intensity: float
    evidence: Dict[str, Any]
    timestamp: datetime
    context: Dict[str, Any]


@dataclass
class ServiceOpportunity:
    """服务机会"""
    opportunity_type: TriggerScenario
    customer_id: str
    score: float
    action_type: ProactiveActionType
    message_template: str
    channels: List[str]
    priority: int
    metadata: Dict[str, Any]
    valid_until: datetime


@dataclass
class ProactiveAction:
    """主动服务动作"""
    action_id: str
    action_type: ProactiveActionType
    customer_id: str
    message: str
    channel: str
    triggered_by: str
    created_at: datetime
    status: str = "pending"
    feedback: Optional[Dict[str, Any]] = None


class IntentPredictor:
    """
    意图预测器

    基于用户历史行为和当前上下文预测购买意向
    """

    PURCHASE_SIGNALS = {
        "browsing_product": 0.3,
        "browsing_price": 0.4,
        "adding_to_cart": 0.6,
        "viewing_comparison": 0.5,
        "repeated_view": 0.4,
        "asking_about_discount": 0.5,
        "inquiring_delivery": 0.4,
        "inquiring_payment": 0.5
    }

    CHURN_SIGNALS = {
        "browsing_competitor": -0.3,
        "negative_sentiment": -0.4,
        "complaint_raised": -0.5,
        "price_comparison": -0.3,
        "delayed_response": -0.2,
        "multiple_visits_no_purchase": -0.3
    }

    def __init__(self):
        self._behavior_cache: Dict[str, List[Dict]] = {}

    def predict_purchase_signals(
        self,
        customer_id: str,
        conversation_context: Dict[str, Any],
        recent_behaviors: List[Dict] = None
    ) -> ProactiveSignal:
        """
        预测购买信号

        Args:
            customer_id: 客户ID
            conversation_context: 对话上下文
            recent_behaviors: 最近行为列表

        Returns:
            购买信号
        """
        total_score = 0.0
        evidence = {}
        signal_type = "neutral"

        behaviors = recent_behaviors or self._behavior_cache.get(customer_id, [])

        for behavior in behaviors:
            behavior_type = behavior.get("type", "")
            if behavior_type in self.PURCHASE_SIGNALS:
                weight = self.PURCHASE_SIGNALS[behavior_type]
                total_score += weight
                evidence[behavior_type] = weight

        last_message = conversation_context.get("last_message", "")
        sentiment = conversation_context.get("sentiment", "neutral")

        if sentiment == "positive":
            total_score += 0.2
            evidence["positive_sentiment"] = 0.2

        if "优惠" in last_message or "便宜" in last_message:
            total_score += 0.3
            evidence["price_sensitivity"] = 0.3

        normalized_score = min(1.0, max(0.0, (total_score + 1) / 2))

        if normalized_score > 0.7:
            signal_type = "high_purchase_intent"
        elif normalized_score > 0.4:
            signal_type = "medium_purchase_intent"
        else:
            signal_type = "low_purchase_intent"

        return ProactiveSignal(
            signal_type=signal_type,
            customer_id=customer_id,
            session_id=conversation_context.get("session_id", ""),
            intensity=normalized_score,
            evidence=evidence,
            timestamp=datetime.now(),
            context=conversation_context
        )

    def predict_churn_risk(
        self,
        customer_id: str,
        conversation_context: Dict[str, Any]
    ) -> ProactiveSignal:
        """预测流失风险"""
        total_risk = 0.0
        evidence = {}

        sentiment = conversation_context.get("sentiment", "neutral")
        if sentiment == "negative":
            total_risk += 0.5
            evidence["negative_sentiment"] = 0.5

        last_intent = conversation_context.get("last_intent", "")
        if last_intent == "complaint":
            total_risk += 0.6
            evidence["complaint_intent"] = 0.6

        recent_queries = conversation_context.get("recent_queries", [])
        competitor_keywords = ["别家", "其他", "对比", "竞品"]
        if any(any(kw in q for kw in competitor_keywords) for q in recent_queries):
            total_risk += 0.4
            evidence["competitor_interest"] = 0.4

        normalized_risk = min(1.0, max(0.0, total_risk))

        return ProactiveSignal(
            signal_type="churn_risk" if normalized_risk > 0.5 else "normal",
            customer_id=customer_id,
            session_id=conversation_context.get("session_id", ""),
            intensity=normalized_risk,
            evidence=evidence,
            timestamp=datetime.now(),
            context=conversation_context
        )


class OpportunityDetector:
    """
    服务机会检测器

    基于场景触发规则检测服务机会
    """

    SCENARIO_RULES = {
        TriggerScenario.ABANDONED_CART: {
            "condition": lambda ctx: ctx.get("cart_items") and not ctx.get("purchase_completed"),
            "time_threshold": timedelta(minutes=30),
            "action_type": ProactiveActionType.CART_REMINDER,
            "priority": 2
        },
        TriggerScenario.BROWSING_TIMEOUT: {
            "condition": lambda ctx: ctx.get("browsing_duration", 0) > 120,
            "action_type": ProactiveActionType.SERVICE_CHECKIN,
            "priority": 3
        },
        TriggerScenario.NEGATIVE_SENTIMENT: {
            "condition": lambda ctx: ctx.get("sentiment") == "negative",
            "action_type": ProactiveActionType.SERVICE_CHECKIN,
            "priority": 1
        },
        TriggerScenario.INACTIVE_USER: {
            "condition": lambda ctx: ctx.get("days_since_last_visit", 0) > 7,
            "action_type": ProactiveActionType.RE_ENGAGEMENT,
            "priority": 4
        },
        TriggerScenario.PRICE_INQUIRY: {
            "condition": lambda ctx: "价格" in ctx.get("last_intent", "") or "优惠" in ctx.get("last_intent", ""),
            "action_type": ProactiveActionType.COUPON_PUSH,
            "priority": 2
        }
    }

    MESSAGE_TEMPLATES = {
        ProactiveActionType.CART_REMINDER: [
            "您好，您购物车中的商品还没下单哦~ 现在下单可享受专属优惠，快来看看吧！",
            "看到您对{product}很感兴趣，错过优惠可惜了，点击即可下单~"
        ],
        ProactiveActionType.COUPON_PUSH: [
            "为您准备了一张专属优惠券，满{amount}减{discount}，有效期至{date}，先到先得！",
            "您浏览的{product}正在促销中，使用优惠券可直接抵扣，快去看看吧~"
        ],
        ProactiveActionType.SERVICE_CHECKIN: [
            "您好，看到您在使用过程中有任何问题吗？我们可以为您提供帮助~",
            "感谢您的信任，如果在产品使用上有任何疑问，随时联系我们哦！"
        ],
        ProactiveActionType.URGENCY_REMINDER: [
            "您关注{product}的优惠活动即将结束，仅剩{time}，抓紧时间下单吧！",
            "促销活动倒计时中，错过再等一年，点击立即抢购~"
        ],
        ProactiveActionType.CROSS_SELL: [
            "购买{original_product}的客户还选择了{new_product}，搭配购买更优惠哦~",
            "发现您对{new_product}也很感兴趣，现在一起购买可以享受组合优惠！"
        ]
    }

    def detect(self, customer_context: Dict[str, Any]) -> List[ServiceOpportunity]:
        """
        检测服务机会

        Args:
            customer_context: 客户上下文

        Returns:
            检测到的服务机会列表
        """
        opportunities = []

        for scenario, rule in self.SCENARIO_RULES.items():
            try:
                if rule["condition"](customer_context):
                    opportunity = ServiceOpportunity(
                        opportunity_type=scenario,
                        customer_id=customer_context.get("customer_id", ""),
                        score=self._calculate_opportunity_score(scenario, customer_context),
                        action_type=rule["action_type"],
                        message_template=self._select_template(rule["action_type"]),
                        channels=self._select_channels(scenario),
                        priority=rule["priority"],
                        metadata={"scenario": scenario.value},
                        valid_until=datetime.now() + timedelta(hours=24)
                    )
                    opportunities.append(opportunity)
            except Exception as e:
                logger.warning(f"场景检测异常 {scenario}: {e}")

        opportunities.sort(key=lambda x: x.score, reverse=True)
        return opportunities

    def _calculate_opportunity_score(
        self,
        scenario: TriggerScenario,
        context: Dict[str, Any]
    ) -> float:
        """计算机会分数"""
        base_scores = {
            TriggerScenario.ABANDONED_CART: 0.7,
            TriggerScenario.NEGATIVE_SENTIMENT: 0.8,
            TriggerScenario.PRICE_INQUIRY: 0.6,
            TriggerScenario.BROWSING_TIMEOUT: 0.5,
            TriggerScenario.INACTIVE_USER: 0.4
        }

        base = base_scores.get(scenario, 0.5)

        customer_value = context.get("customer_value", "medium")
        value_multiplier = {"high": 1.2, "medium": 1.0, "low": 0.8}
        multiplier = value_multiplier.get(customer_value, 1.0)

        return min(1.0, base * multiplier)

    def _select_template(self, action_type: ProactiveActionType) -> str:
        """选择消息模板"""
        templates = self.MESSAGE_TEMPLATES.get(action_type, [])
        if templates:
            return templates[0]
        return "您好，我们有新优惠活动，快来看看吧~"

    def _select_channels(self, scenario: TriggerScenario) -> List[str]:
        """选择触达渠道"""
        channel_map = {
            TriggerScenario.ABANDONED_CART: ["push", "popup"],
            TriggerScenario.NEGATIVE_SENTIMENT: ["push", "sms"],
            TriggerScenario.PRICE_INQUIRY: ["push", "popup"],
            TriggerScenario.BROWSING_TIMEOUT: ["push"],
            TriggerScenario.INACTIVE_USER: ["sms", "email"]
        }
        return channel_map.get(scenario, ["push"])


class ActionScheduler:
    """
    动作调度器

    负责主动消息的发送调度
    """

    def __init__(self):
        self._scheduled_actions: List[ProactiveAction] = []
        self._action_handlers: Dict[str, Callable] = {}

    def schedule_action(
        self,
        opportunity: ServiceOpportunity,
        send_time: datetime = None
    ) -> ProactiveAction:
        """调度主动动作"""
        action = ProactiveAction(
            action_id=f"action_{datetime.now().timestamp()}",
            action_type=opportunity.action_type,
            customer_id=opportunity.customer_id,
            message=self._fill_template(opportunity),
            channel=opportunity.channels[0] if opportunity.channels else "push",
            triggered_by=opportunity.opportunity_type.value,
            created_at=datetime.now()
        )

        self._scheduled_actions.append(action)
        return action

    def _fill_template(self, opportunity: ServiceOpportunity) -> str:
        """填充消息模板"""
        template = opportunity.message_template
        metadata = opportunity.metadata

        fill_map = {
            "{product}": metadata.get("product_name", "商品"),
            "{amount}": str(metadata.get("min_amount", 100)),
            "{discount}": str(metadata.get("discount_amount", 10)),
            "{date}": metadata.get("expire_date", "本周内"),
            "{time}": metadata.get("remaining_time", "24小时"),
            "{original_product}": metadata.get("original_product", "当前商品"),
            "{new_product}": metadata.get("cross_sell_product", "热门商品")
        }

        message = template
        for placeholder, value in fill_map.items():
            message = message.replace(placeholder, value)

        return message

    def get_pending_actions(self, customer_id: str = None) -> List[ProactiveAction]:
        """获取待发送动作"""
        if customer_id:
            return [a for a in self._scheduled_actions if a.customer_id == customer_id and a.status == "pending"]
        return [a for a in self._scheduled_actions if a.status == "pending"]

    def update_action_status(
        self,
        action_id: str,
        status: str,
        feedback: Dict = None
    ) -> bool:
        """更新动作状态"""
        for action in self._scheduled_actions:
            if action.action_id == action_id:
                action.status = status
                if feedback:
                    action.feedback = feedback
                return True
        return False


class ProactiveServiceEngine:
    """
    主动服务引擎

    整合意图预测、机会检测、动作调度
    """

    ACTION_THRESHOLD = 0.65
    MAX_DAILY_ACTIONS_PER_CUSTOMER = 3

    def __init__(self):
        self.intent_predictor = IntentPredictor()
        self.opportunity_detector = OpportunityDetector()
        self.action_scheduler = ActionScheduler()
        self._action_history: Dict[str, List[datetime]] = {}

    def evaluate(
        self,
        customer_id: str,
        conversation_context: Dict[str, Any],
        recent_behaviors: List[Dict] = None
    ) -> List[ProactiveAction]:
        """
        评估并执行主动服务

        Args:
            customer_id: 客户ID
            conversation_context: 对话上下文
            recent_behaviors: 最近行为

        Returns:
            执行的主动动作列表
        """
        triggered_actions = []

        purchase_signal = self.intent_predictor.predict_purchase_signals(
            customer_id, conversation_context, recent_behaviors
        )

        churn_signal = self.intent_predictor.predict_churn_risk(
            customer_id, conversation_context
        )

        context_for_detect = {
            **conversation_context,
            "customer_id": customer_id,
            "purchase_signal": purchase_signal.__dict__,
            "churn_signal": churn_signal.__dict__
        }
        opportunities = self.opportunity_detector.detect(context_for_detect)

        for opportunity in opportunities:
            combined_score = self._calculate_combined_score(
                purchase_signal,
                churn_signal,
                opportunity
            )

            if combined_score >= self.ACTION_THRESHOLD:
                if self._check_frequency_limit(customer_id):
                    action = self.action_scheduler.schedule_action(opportunity)
                    triggered_actions.append(action)
                    self._record_action(customer_id)

        return triggered_actions

    def _calculate_combined_score(
        self,
        purchase_signal: ProactiveSignal,
        churn_signal: ProactiveSignal,
        opportunity: ServiceOpportunity
    ) -> float:
        """计算综合分数"""
        w1, w2, w3 = 0.3, 0.4, 0.3

        purchase_score = purchase_signal.intensity
        churn_score = churn_signal.intensity
        opportunity_score = opportunity.score

        return w1 * purchase_score + w2 * opportunity_score + w3 * churn_score

    def _check_frequency_limit(self, customer_id: str) -> bool:
        """检查发送频率限制"""
        today = datetime.now().date()
        history = self._action_history.get(customer_id, [])

        today_actions = [d for d in history if d.date() == today]
        return len(today_actions) < self.MAX_DAILY_ACTIONS_PER_CUSTOMER

    def _record_action(self, customer_id: str):
        """记录动作发送"""
        if customer_id not in self._action_history:
            self._action_history[customer_id] = []
        self._action_history[customer_id].append(datetime.now())

    def get_customer_actions(
        self,
        customer_id: str,
        status: str = None
    ) -> List[ProactiveAction]:
        """获取客户的所有动作"""
        actions = self._action_scheduler._scheduled_actions
        if customer_id:
            actions = [a for a in actions if a.customer_id == customer_id]
        if status:
            actions = [a for a in actions if a.status == status]
        return actions
