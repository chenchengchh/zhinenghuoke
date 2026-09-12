"""
数据分析增强服务 - 企业级
整合购买意向分析、客户画像、行为分析、趋势预测
"""
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict
import json
import math
import logging
import re
from src.common.chat_store import ChatStoreFacade
from src.common.conversation_id import build_conversation_id
from src.common.follow_up_reminder_service import get_follow_up_reminder_service
from src.common.industry_schema_service import get_active_schema_with_compat

logger = logging.getLogger(__name__)


@dataclass
class CustomerInsight:
    """客户洞察"""
    customer_id: str
    customer_name: str
    
    # 基础画像
    platform: str = "douyin"
    avatar: str = ""
    first_contact_time: Optional[datetime] = None
    last_contact_time: Optional[datetime] = None
    
    # 消息统计
    total_messages: int = 0
    inbound_messages: int = 0
    outbound_messages: int = 0
    avg_message_length: float = 0.0
    
    # 意向分析
    intent_score: float = 0.0
    lead_score: str = "cold"
    purchase_probability: float = 0.0
    lifecycle_stage: str = "prospect"
    buying_role: str = "unknown"
    
    # 价值评估
    estimated_value: float = 0.0
    lifetime_value: float = 0.0
    
    # 行为特征
    interests: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    
    # 风险与机会
    risk_factors: List[str] = field(default_factory=list)
    opportunity_factors: List[str] = field(default_factory=list)
    
    # 活跃度
    engagement_level: str = "low"
    response_rate: float = 0.0
    avg_response_time: float = 0.0
    
    # 预测
    predicted_next_action: str = ""
    recommended_follow_up: str = ""


@dataclass
class TrendData:
    """趋势数据"""
    date: str
    metric_name: str
    value: float
    change_rate: float = 0.0
    trend: str = "stable"  # up, down, stable


@dataclass
class AnalyticsDashboard:
    """分析仪表板"""
    # 概览数据
    total_customers: int = 0
    total_messages: int = 0
    high_intent_count: int = 0
    hot_leads_count: int = 0
    
    # 分布数据
    lead_score_distribution: Dict[str, int] = field(default_factory=dict)
    lifecycle_stage_distribution: Dict[str, int] = field(default_factory=dict)
    interest_distribution: Dict[str, int] = field(default_factory=dict)
    
    # 转化数据
    conversion_funnel: List[Dict] = field(default_factory=list)
    overall_conversion_rate: float = 0.0
    
    # 价值数据
    total_estimated_value: float = 0.0
    avg_deal_size: float = 0.0
    
    # 趋势数据
    daily_trends: List[TrendData] = field(default_factory=list)
    weekly_trends: List[TrendData] = field(default_factory=list)
    
    # 待办事项
    action_items: List[Dict] = field(default_factory=list)
    pending_follow_ups: int = 0
    
    # 预警
    alerts: List[Dict] = field(default_factory=list)


class EnhancedAnalyticsService:
    """
    增强数据分析服务
    
    功能：
    1. 客户洞察分析
    2. 转化漏斗分析
    3. 趋势预测
    4. 行为模式分析
    5. 智能推荐
    """
    
    # 活跃度阈值
    ENGAGEMENT_THRESHOLDS = {
        "high": {"min_messages": 15, "min_response_rate": 0.7},
        "medium": {"min_messages": 5, "min_response_rate": 0.4},
        "low": {"min_messages": 0, "min_response_rate": 0}
    }
    
    # 生命周期阶段权重
    LIFECYCLE_WEIGHTS = {
        "prospect": 0.1,
        "lead": 0.2,
        "mql": 0.4,
        "sql": 0.6,
        "opportunity": 0.8,
        "customer": 1.0
    }
    
    # 购买角色权重
    ROLE_WEIGHTS = {
        "decision_maker": 1.0,
        "champion": 0.9,
        "influencer": 0.7,
        "user": 0.5,
        "gatekeeper": 0.4,
        "unknown": 0.3
    }

    DEFAULT_BUSINESS_FUNNEL_STAGES = [
        {"key": "consultation", "name": "初步咨询"},
        {"key": "needs_confirmed", "name": "需求明确"},
        {"key": "quote_requested", "name": "方案报价"},
        {"key": "proposal_ready", "name": "锁位留资"},
        {"key": "converted", "name": "报名成交"},
    ]

    DEFAULT_LEGACY_STAGE_ALIASES = {
        "prospect": "consultation",
        "lead": "needs_confirmed",
        "awareness": "needs_confirmed",
        "mql": "quote_requested",
        "sql": "quote_requested",
        "consideration": "quote_requested",
        "opportunity": "proposal_ready",
        "customer": "converted",
        "advocate": "converted",
    }

    DEFAULT_FUNNEL_KEYWORDS = {
        "converted": [
            "已成交", "已签约", "已付款", "已开通", "开始合作", "合同已签", "确认采购", "已下单",
            "采购完成", "开票信息", "订单", "锁位", "锁个位置", "先付定金", "付定金", "报名信息", "报名表"
        ],
        "proposal_ready": [
            "方案发我", "报价发我", "合同", "采购流程", "怎么付款", "怎么合作", "排期",
            "上线时间", "实施时间", "对接人", "联系方式", "微信", "手机号", "邮箱", "发资料"
        ],
        "quote_requested": [
            "价格", "多少钱", "报价", "费用", "怎么收费", "预算", "优惠", "折扣",
            "方案", "区别", "差别", "哪个合适", "推荐哪个", "包含", "适合吗", "能解决什么"
        ],
        "needs_confirmed": [
            "什么需求", "什么场景", "怎么用", "多少人", "什么时间", "预算范围",
            "使用对象", "部署方式", "行业场景", "交付要求", "上线时间"
        ],
    }

    _domain_funnel_keywords = []

    _TIME_ONLY_RE = re.compile(
        r"^(昨天|今天|刚刚|\d{1,2}:\d{2}|\d{4}/\d{1,2}/\d{1,2}|"
        r"(昨天|今天)\s+\d{1,2}:\d{2})$"
    )

    def _get_active_schema(self, enterprise_id: str = ""):
        try:
            from .industry_schema_service import IndustrySchemaService
            service = IndustrySchemaService()
            resolved_enterprise = str(enterprise_id or getattr(self, "_enterprise_id", "") or "").strip()
            return get_active_schema_with_compat(
                service,
                enterprise_id=resolved_enterprise,
            ) or {}
        except Exception:
            return {}

    def _get_domain_funnel_keywords(self, enterprise_id: str = ""):
        try:
            schema = self._get_active_schema(enterprise_id=enterprise_id)
            return (schema.get("metadata") or {}).get("domain_funnel_keywords") or []
        except Exception:
            return []

    def __init__(self, database=None, purchase_intent_analyzer=None, enterprise_id: str = ""):
        """
        初始化分析服务
        
        Args:
            database: 数据库实例
            purchase_intent_analyzer: 购买意向分析器
        """
        self.database = database
        self.chat_store = ChatStoreFacade(database) if database else None
        self.purchase_intent_analyzer = purchase_intent_analyzer
        self._enterprise_id = str(enterprise_id or "").strip()
        self._customer_insights: Dict[str, CustomerInsight] = {}
        self._business_funnel_stages = self._build_business_funnel_stages()
        self._legacy_stage_aliases = self._build_legacy_stage_aliases()
        self._funnel_keywords = self._build_funnel_keywords()

    def _get_analytics_config(self) -> Dict:
        schema = self._get_active_schema(enterprise_id=self._enterprise_id)
        return (schema.get("metadata") or {}).get("analytics") or {}

    def _build_business_funnel_stages(self) -> List[Dict]:
        analytics_cfg = self._get_analytics_config()
        stages = analytics_cfg.get("business_funnel_stages") or []
        if isinstance(stages, list) and stages:
            normalized = []
            for stage in stages:
                if not isinstance(stage, dict):
                    continue
                key = str(stage.get("key") or "").strip()
                name = str(stage.get("name") or key).strip()
                if key and name:
                    normalized.append({"key": key, "name": name})
            if normalized:
                return normalized
        return [dict(stage) for stage in self.DEFAULT_BUSINESS_FUNNEL_STAGES]

    def _build_legacy_stage_aliases(self) -> Dict[str, str]:
        analytics_cfg = self._get_analytics_config()
        aliases = analytics_cfg.get("legacy_stage_aliases") or {}
        if isinstance(aliases, dict) and aliases:
            normalized = {
                str(key).strip(): str(value).strip()
                for key, value in aliases.items()
                if str(key).strip() and str(value).strip()
            }
            if normalized:
                return normalized
        return dict(self.DEFAULT_LEGACY_STAGE_ALIASES)

    def _build_funnel_keywords(self) -> Dict[str, List[str]]:
        keywords = {
            stage: list(values)
            for stage, values in self.DEFAULT_FUNNEL_KEYWORDS.items()
        }
        analytics_cfg = self._get_analytics_config()
        configured = analytics_cfg.get("funnel_keywords") or {}
        if isinstance(configured, dict):
            for stage, values in configured.items():
                normalized_values = [str(value).strip() for value in (values or []) if str(value).strip()]
                if normalized_values:
                    keywords[str(stage).strip()] = normalized_values

        domain_keywords = self._get_domain_funnel_keywords(enterprise_id=self._enterprise_id)
        if domain_keywords:
            current = keywords.setdefault("needs_confirmed", [])
            for keyword in domain_keywords:
                normalized = str(keyword).strip()
                if normalized and normalized not in current:
                    current.append(normalized)
        return keywords
    
    def get_dashboard(self) -> AnalyticsDashboard:
        """
        获取分析仪表板
        
        Returns:
            AnalyticsDashboard: 仪表板数据
        """
        dashboard = AnalyticsDashboard()
        
        try:
            # 获取所有会话
            conversations = self._get_all_conversations()
            messages = self._get_all_messages()
            
            # 计算概览数据
            dashboard.total_customers = len(conversations)
            dashboard.total_messages = len(messages)
            
            # 计算分布
            dashboard.lead_score_distribution = self._calculate_distribution(
                conversations, "lead_score", ["hot", "warm", "cool", "cold"]
            )
            dashboard.lifecycle_stage_distribution = self._calculate_distribution(
                conversations, "lifecycle_stage", ["prospect", "lead", "mql", "sql", "opportunity", "customer"]
            )
            
            # 高意向客户
            dashboard.high_intent_count = sum(
                1 for c in conversations 
                if c.get("purchase_intent_score", 0) >= 50
            )
            dashboard.hot_leads_count = dashboard.lead_score_distribution.get("hot", 0)
            
            # 转化漏斗
            dashboard.conversion_funnel = self._build_conversion_funnel(conversations, messages)
            dashboard.overall_conversion_rate = self._calculate_conversion_rate(conversations, messages)
            
            # 价值数据
            dashboard.total_estimated_value = sum(
                c.get("estimated_deal_size", 0) for c in conversations
            )
            dashboard.avg_deal_size = (
                dashboard.total_estimated_value / len(conversations) 
                if conversations else 0
            )
            
            # 趋势数据
            dashboard.daily_trends = self._calculate_daily_trends(messages, days=7)
            dashboard.weekly_trends = self._calculate_weekly_trends(messages, weeks=4)
            
            # 待办事项
            messages_by_conversation = self._group_messages_by_conversation(messages)
            dashboard.action_items = self._generate_action_items(
                conversations,
                messages_by_conversation,
                # 仪表板读取是纯展示链路，不能在页面刷新/轮询时触发钉钉发送。
                notify=False,
            )
            dashboard.pending_follow_ups = len(dashboard.action_items)
            
            # 预警
            dashboard.alerts = self._generate_alerts(conversations)
            
            # 兴趣分布
            dashboard.interest_distribution = self._calculate_interest_distribution(conversations)
            
        except Exception as e:
            logger.error(f"获取仪表板数据失败: {e}")
        
        return dashboard

    def get_conversion_funnel_details(self) -> Dict:
        """返回当前活动 schema 对应的业务漏斗及各阶段客户详情。"""
        conversations = list(self._get_all_conversations() or [])
        messages_by_conversation = self._group_messages_by_conversation(self._get_all_messages())
        funnel = self._build_conversion_funnel(conversations, self._get_all_messages())

        stages: Dict[str, List[Dict]] = {
            stage["key"]: [] for stage in self._business_funnel_stages
        }
        stage_name_map = {
            stage["key"]: stage["name"] for stage in self._business_funnel_stages
        }

        for conv in conversations:
            conversation_id = conv.get("conversation_id")
            stage_key = self._infer_business_funnel_stage(
                conv,
                messages_by_conversation.get(conversation_id, []),
            )
            stages.setdefault(stage_key, []).append({
                "conversation_id": conversation_id,
                "customer_name": conv.get("customer_name") or conversation_id or "",
                "stage_key": stage_key,
                "stage_name": stage_name_map.get(stage_key, stage_key),
                "lead_score": conv.get("lead_score", "cold"),
                "purchase_probability": round(float(conv.get("purchase_probability", 0) or 0), 4),
                "purchase_intent_score": round(float(conv.get("purchase_intent_score", 0) or 0), 2),
                "estimated_deal_size": conv.get("estimated_deal_size", 0),
                "lifecycle_stage": conv.get("lifecycle_stage", "prospect"),
                "last_message_time": conv.get("last_message_time"),
                "signals_detected": conv.get("signals_detected", []),
                "opportunity_factors": conv.get("opportunity_factors", []),
            })

        for items in stages.values():
            items.sort(
                key=lambda item: (
                    float(item.get("purchase_probability") or 0),
                    float(item.get("purchase_intent_score") or 0),
                    float(item.get("estimated_deal_size") or 0),
                ),
                reverse=True,
            )

        stage_counts = {key: len(items) for key, items in stages.items()}
        if "proposal_ready" in stage_counts:
            stage_counts["reservation_ready"] = stage_counts.pop("proposal_ready")

        return {
            "funnel": funnel,
            "stages": stages,
            "stage_counts": stage_counts,
            "overall_rate": self._calculate_conversion_rate(conversations, self._get_all_messages()),
        }
    
    def get_customer_insight(self, customer_name: str) -> Optional[CustomerInsight]:
        """
        获取客户洞察
        
        Args:
            customer_name: 客户名称
            
        Returns:
            CustomerInsight: 客户洞察
        """
        try:
            conversation_id = build_conversation_id(customer_name, "douyin")
            
            # 获取消息历史
            messages = self._get_conversation_messages(conversation_id)
            
            # 获取会话信息
            conversation = self._get_conversation(conversation_id)
            
            if not messages and not conversation:
                return None
            
            insight = CustomerInsight(
                customer_id=customer_name,
                customer_name=customer_name
            )
            
            # 填充基础数据
            if conversation:
                insight.platform = conversation.get("platform", "douyin")
                insight.intent_score = conversation.get("purchase_intent_score", 0)
                insight.lead_score = conversation.get("lead_score", "cold")
                insight.purchase_probability = conversation.get("purchase_probability", 0)
                insight.lifecycle_stage = conversation.get("lifecycle_stage", "prospect")
                insight.buying_role = conversation.get("buying_role", "unknown")
                insight.estimated_value = conversation.get("estimated_deal_size", 0)
                insight.signals = conversation.get("signals_detected", [])
                insight.risk_factors = conversation.get("risk_factors", [])
                insight.opportunity_factors = conversation.get("opportunity_factors", [])
            
            # 计算消息统计
            if messages:
                insight.total_messages = len(messages)
                insight.inbound_messages = sum(1 for m in messages if m.get("direction") == "inbound")
                insight.outbound_messages = sum(1 for m in messages if m.get("direction") == "outbound")
                insight.avg_message_length = sum(len(m.get("content", "")) for m in messages) / len(messages)
                
                # 计算响应率
                if insight.outbound_messages > 0:
                    insight.response_rate = min(insight.inbound_messages / insight.outbound_messages, 1.0)
                
                # 获取时间信息
                timestamps = []
                for m in messages:
                    ts = m.get("created_at", m.get("timestamp"))
                    if ts:
                        try:
                            if isinstance(ts, str):
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            else:
                                dt = ts
                            timestamps.append(dt)
                        except (ValueError, TypeError) as e:
                            logger.debug(f"时间戳解析失败: {ts}, 错误: {e}")
                
                if timestamps:
                    insight.first_contact_time = min(timestamps)
                    insight.last_contact_time = max(timestamps)
                
                # 提取关键词和兴趣
                all_content = " ".join(m.get("content", "") for m in messages)
                insight.keywords = self._extract_keywords(all_content)
                insight.interests = self._extract_interests(all_content)
            
            # 计算活跃度
            insight.engagement_level = self._calculate_engagement_level(insight)
            
            # 生成标签
            insight.tags = self._generate_tags(insight)
            
            # 计算生命周期价值
            insight.lifetime_value = self._calculate_lifetime_value(insight)
            
            # 生成推荐
            insight.predicted_next_action = self._predict_next_action(insight)
            insight.recommended_follow_up = self._generate_follow_up_recommendation(insight)
            
            return insight
            
        except Exception as e:
            logger.error(f"获取客户洞察失败: {e}")
            return None
    
    def get_trend_analysis(self, days: int = 30) -> Dict:
        """
        获取趋势分析
        
        Args:
            days: 分析天数
            
        Returns:
            Dict: 趋势分析结果
        """
        try:
            messages = self._get_all_messages()
            conversations = self._get_all_conversations()
            
            # 按日期分组
            daily_stats = defaultdict(lambda: {
                "new_customers": 0,
                "messages": 0,
                "high_intent": 0,
                "conversions": 0
            })
            
            today = datetime.now()
            
            # 统计消息
            for msg in messages:
                ts = msg.get("created_at", msg.get("timestamp"))
                if ts:
                    try:
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        else:
                            dt = ts
                        
                        days_ago = (today - dt).days
                        if days_ago < days:
                            date_key = dt.strftime("%Y-%m-%d")
                            daily_stats[date_key]["messages"] += 1
                    except (ValueError, TypeError) as e:
                        logger.debug(f"消息时间解析失败: {e}")
            
            # 统计客户
            for conv in conversations:
                created = conv.get("created_at")
                if created:
                    try:
                        if isinstance(created, str):
                            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        else:
                            dt = created
                        
                        days_ago = (today - dt).days
                        if days_ago < days:
                            date_key = dt.strftime("%Y-%m-%d")
                            daily_stats[date_key]["new_customers"] += 1
                            
                            if conv.get("purchase_intent_score", 0) >= 70:
                                daily_stats[date_key]["high_intent"] += 1
                    except (ValueError, TypeError) as e:
                        logger.debug(f"会话时间解析失败: {e}")
            
            # 构建趋势数据
            trends = []
            sorted_dates = sorted(daily_stats.keys())
            
            for i, date in enumerate(sorted_dates):
                stats = daily_stats[date]
                
                # 计算变化率
                change_rate = 0
                if i > 0:
                    prev_stats = daily_stats[sorted_dates[i-1]]
                    if prev_stats["messages"] > 0:
                        change_rate = (stats["messages"] - prev_stats["messages"]) / prev_stats["messages"] * 100
                
                # 判断趋势
                if change_rate > 10:
                    trend = "up"
                elif change_rate < -10:
                    trend = "down"
                else:
                    trend = "stable"
                
                trends.append({
                    "date": date,
                    "messages": stats["messages"],
                    "new_customers": stats["new_customers"],
                    "high_intent": stats["high_intent"],
                    "change_rate": round(change_rate, 2),
                    "trend": trend
                })
            
            # 计算汇总统计
            total_messages = sum(d["messages"] for d in trends)
            total_new_customers = sum(d["new_customers"] for d in trends)
            total_high_intent = sum(d["high_intent"] for d in trends)
            
            avg_daily_messages = total_messages / len(trends) if trends else 0
            
            return {
                "period_days": days,
                "trends": trends,
                "summary": {
                    "total_messages": total_messages,
                    "total_new_customers": total_new_customers,
                    "total_high_intent": total_high_intent,
                    "avg_daily_messages": round(avg_daily_messages, 2),
                    "peak_day": max(trends, key=lambda x: x["messages"])["date"] if trends else None
                },
                "insights": self._generate_trend_insights(trends)
            }
            
        except Exception as e:
            logger.error(f"获取趋势分析失败: {e}")
            return {"error": str(e)}
    
    def get_behavior_analysis(self, customer_name: str = None) -> Dict:
        """
        获取行为分析
        
        Args:
            customer_name: 客户名称（可选，不提供则分析全部）
            
        Returns:
            Dict: 行为分析结果
        """
        try:
            if customer_name:
                return self._analyze_single_customer_behavior(customer_name)
            else:
                return self._analyze_all_customers_behavior()
                
        except Exception as e:
            logger.error(f"获取行为分析失败: {e}")
            return {"error": str(e)}
    
    def _analyze_single_customer_behavior(self, customer_name: str) -> Dict:
        """分析单个客户行为"""
        conversation_id = build_conversation_id(customer_name, "douyin")
        messages = self._get_conversation_messages(conversation_id)
        
        if not messages:
            return {"error": "没有找到消息记录"}
        
        # 按时间排序
        sorted_messages = []
        for msg in messages:
            ts = msg.get("created_at", msg.get("timestamp"))
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    sorted_messages.append((dt, msg))
                except (ValueError, TypeError) as e:
                    logger.debug(f"消息时间解析失败: {ts}, 错误: {e}")
        
        sorted_messages.sort(key=lambda x: x[0])
        
        # 分析行为模式
        behavior_patterns = {
            "active_hours": self._analyze_active_hours(sorted_messages),
            "response_pattern": self._analyze_response_pattern(sorted_messages),
            "engagement_trend": self._analyze_engagement_trend(sorted_messages),
            "topic_evolution": self._analyze_topic_evolution(sorted_messages),
            "interaction_frequency": self._analyze_interaction_frequency(sorted_messages)
        }
        
        return {
            "customer_name": customer_name,
            "behavior_patterns": behavior_patterns,
            "insights": self._generate_behavior_insights(behavior_patterns)
        }
    
    def _analyze_all_customers_behavior(self) -> Dict:
        """分析所有客户行为"""
        conversations = self._get_all_conversations()
        
        # 汇总行为统计
        behavior_stats = {
            "total_interactions": 0,
            "avg_interactions_per_customer": 0,
            "most_active_hours": {},
            "common_topics": {},
            "engagement_distribution": {"high": 0, "medium": 0, "low": 0}
        }
        
        for conv in conversations:
            customer_name = conv.get("customer_name")
            if customer_name:
                insight = self.get_customer_insight(customer_name)
                if insight:
                    behavior_stats["total_interactions"] += insight.total_messages
                    
                    if insight.engagement_level in behavior_stats["engagement_distribution"]:
                        behavior_stats["engagement_distribution"][insight.engagement_level] += 1
        
        if conversations:
            behavior_stats["avg_interactions_per_customer"] = (
                behavior_stats["total_interactions"] / len(conversations)
            )
        
        return behavior_stats
    
    def get_conversion_prediction(self, customer_name: str) -> Dict:
        """
        获取转化预测
        
        Args:
            customer_name: 客户名称
            
        Returns:
            Dict: 转化预测结果
        """
        try:
            insight = self.get_customer_insight(customer_name)
            
            if not insight:
                return {"error": "客户不存在"}
            
            # 计算转化概率
            base_probability = insight.purchase_probability
            
            # 根据行为调整
            engagement_factor = {
                "high": 1.2,
                "medium": 1.0,
                "low": 0.8
            }.get(insight.engagement_level, 1.0)
            
            # 根据生命周期阶段调整
            lifecycle_factor = self.LIFECYCLE_WEIGHTS.get(insight.lifecycle_stage, 0.3)
            
            # 根据购买角色调整
            role_factor = self.ROLE_WEIGHTS.get(insight.buying_role, 0.3)
            
            # 计算最终概率
            final_probability = min(
                base_probability * engagement_factor * (1 + lifecycle_factor * 0.5) * (1 + role_factor * 0.3),
                0.99
            )
            
            # 预测成交时间
            estimated_days = self._estimate_conversion_time(insight)
            
            # 预测成交金额
            estimated_value = insight.estimated_value * final_probability
            
            # 关键影响因素
            key_factors = self._identify_key_factors(insight)
            
            return {
                "customer_name": customer_name,
                "conversion_probability": round(final_probability, 4),
                "estimated_days_to_close": estimated_days,
                "estimated_value": round(estimated_value, 2),
                "confidence_level": self._calculate_confidence(insight),
                "key_factors": key_factors,
                "recommendations": self._generate_conversion_recommendations(insight)
            }
            
        except Exception as e:
            logger.error(f"获取转化预测失败: {e}")
            return {"error": str(e)}
    
    def get_segment_analysis(self) -> Dict:
        """
        获取客户分群分析
        
        Returns:
            Dict: 分群分析结果
        """
        try:
            conversations = self._get_all_conversations()
            
            # 定义分群
            segments = {
                "high_value_prospects": [],
                "hot_leads": [],
                "nurture_candidates": [],
                "at_risk": [],
                "champions": []
            }
            
            for conv in conversations:
                customer_name = conv.get("customer_name")
                if not customer_name:
                    continue
                
                insight = self.get_customer_insight(customer_name)
                if not insight:
                    continue
                
                # 分群逻辑
                score = insight.intent_score
                engagement = insight.engagement_level
                probability = insight.purchase_probability
                
                customer_data = {
                    "customer_name": customer_name,
                    "score": score,
                    "probability": probability,
                    "engagement": engagement
                }
                
                # 高价值潜在客户: 高意向 或 高购买概率 或 处于方案报价阶段
                if score >= 70 or probability >= 0.7 or insight.lifecycle_stage in ["quote_requested", "proposal_ready"]:
                    segments["high_value_prospects"].append(customer_data)
                
                # 热线索: 明确留资或已锁位
                if insight.lead_score == "hot" or insight.lifecycle_stage == "proposal_ready":
                    segments["hot_leads"].append(customer_data)
                
                # 培育候选: 初步咨询或需求明确，且有一定互动
                if 30 <= score < 70 and engagement in ["medium", "high"] and insight.lifecycle_stage in ["initial_inquiry", "needs_clarified"]:
                    segments["nurture_candidates"].append(customer_data)
                
                # 风险客户: 存在明确风险因素，或高意向但突然失联/低互动
                if insight.risk_factors or (score >= 60 and engagement == "low"):
                    segments["at_risk"].append(customer_data)
                
                # 支持者: 已经成交的客户 或 明确表达好评/支持
                if insight.lifecycle_stage == "closed_won" or insight.buying_role == "champion" or "支持者" in insight.tags:
                    segments["champions"].append(customer_data)
            
            # 计算各群体统计
            segment_stats = {}
            for segment_name, customers in segments.items():
                if customers:
                    avg_score = sum(c["score"] for c in customers) / len(customers)
                    avg_probability = sum(c["probability"] for c in customers) / len(customers)
                else:
                    avg_score = 0
                    avg_probability = 0
                
                segment_stats[segment_name] = {
                    "count": len(customers),
                    "avg_score": round(avg_score, 2),
                    "avg_probability": round(avg_probability, 4),
                    "customers": customers[:10]  # 只返回前10个
                }
            
            return {
                "segments": segment_stats,
                "total_customers": len(conversations),
                "segmentation_insights": self._generate_segmentation_insights(segments)
            }
            
        except Exception as e:
            logger.error(f"获取分群分析失败: {e}")
            return {"error": str(e)}
    
    # ==================== 辅助方法 ====================
    
    def _get_all_messages(self) -> List[Dict]:
        """获取所有消息"""
        if self.chat_store:
            try:
                return self.chat_store.get_all_messages_dicts()
            except Exception:
                pass

        if not self.database:
            return []
        
        try:
            get_all_messages = getattr(self.database, "get_all_messages", None)
            if callable(get_all_messages):
                return list(get_all_messages() or [])
            return []
        except Exception:
            return []

    def _get_all_conversations(self) -> List[Dict]:
        if self.chat_store:
            try:
                return self.chat_store.get_all_conversations_dicts()
            except Exception:
                pass
        if not self.database:
            return []
        try:
            get_all_conversations = getattr(self.database, "get_all_conversations", None)
            return list(get_all_conversations() or []) if callable(get_all_conversations) else []
        except Exception:
            return []

    def _get_conversation_messages(self, conversation_id: str) -> List[Dict]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return []
        if self.chat_store:
            try:
                messages = self.chat_store.get_recent_messages_dicts(conversation_id, limit=9999)
                if messages:
                    return messages
            except Exception:
                pass
        messages = self._get_all_messages()
        return [msg for msg in messages if str(msg.get("conversation_id", "") or "") == conversation_id]
    
    def _get_conversation(self, conversation_id: str) -> Optional[Dict]:
        """获取会话"""
        if not self.database:
            return None
        
        try:
            conversations = self._get_all_conversations()
            for conv in conversations:
                if conv.get("conversation_id") == conversation_id:
                    return conv
        except Exception as e:
            logger.warning(f"获取会话失败: {e}")
        return None
    
    def _calculate_distribution(self, items: List[Dict], key: str, default_values: List[str]) -> Dict[str, int]:
        """计算分布"""
        distribution = {v: 0 for v in default_values}
        for item in items:
            value = item.get(key, default_values[-1] if default_values else "unknown")
            if value in distribution:
                distribution[value] += 1
            else:
                distribution[value] = 1
        return distribution
    
    def _build_conversion_funnel(self, conversations: List[Dict], messages: Optional[List[Dict]] = None) -> List[Dict]:
        """基于当前活动 schema 的业务阶段构建转化漏斗。"""
        stage_idx_map = {
            stage["key"]: i for i, stage in enumerate(self._business_funnel_stages)
        }
        cumulative_counts = [0] * len(self._business_funnel_stages)
        messages_by_conversation = self._group_messages_by_conversation(messages or self._get_all_messages())

        for conv in conversations:
            stage_key = self._infer_business_funnel_stage(
                conv,
                messages_by_conversation.get(conv.get("conversation_id"), []),
            )
            idx = stage_idx_map.get(stage_key, 0)
            for i in range(idx + 1):
                cumulative_counts[i] += 1

        funnel = []
        prev_count = cumulative_counts[0] if cumulative_counts else 0

        for i, stage in enumerate(self._business_funnel_stages):
            count = cumulative_counts[i]
            percentage = (count / len(conversations) * 100) if conversations else 0
            dropoff = ((prev_count - count) / prev_count * 100) if prev_count > 0 else 0
            funnel.append({
                "key": stage["key"],
                "name": stage["name"],
                "count": count,
                "percentage": round(percentage, 2),
                "dropoff_rate": round(dropoff, 2),
            })
            prev_count = count

        return funnel

    def _calculate_conversion_rate(self, conversations: List[Dict], messages: Optional[List[Dict]] = None) -> float:
        """计算最终成交占全部咨询的比例。"""
        if not conversations:
            return 0

        messages_by_conversation = self._group_messages_by_conversation(messages or self._get_all_messages())
        converted = 0
        for conv in conversations:
            stage_key = self._infer_business_funnel_stage(
                conv,
                messages_by_conversation.get(conv.get("conversation_id"), []),
            )
            if stage_key == "converted":
                converted += 1
        return round(converted / len(conversations) * 100, 2)

    def _group_messages_by_conversation(self, messages: List[Dict]) -> Dict[str, List[Dict]]:
        grouped: Dict[str, List[Dict]] = defaultdict(list)
        for msg in messages or []:
            conversation_id = str(msg.get("conversation_id") or "").strip()
            if conversation_id:
                grouped[conversation_id].append(msg)
        return grouped

    def _infer_business_funnel_stage(self, conversation: Dict, messages: List[Dict]) -> str:
        lifecycle_stage = str(conversation.get("lifecycle_stage") or "").strip().lower()
        lead_score = str(conversation.get("lead_score") or "").strip().lower()
        purchase_probability = float(conversation.get("purchase_probability") or 0)
        inbound_text = " ".join(self._extract_meaningful_inbound_texts(messages))
        signal_text = self._flatten_text_fragments(conversation.get("signals_detected"))
        opportunity_text = self._flatten_text_fragments(conversation.get("opportunity_factors"))
        combined_text = " ".join(
            part for part in [inbound_text, signal_text, opportunity_text] if part
        )

        if self._matches_funnel_keywords(combined_text, "converted"):
            return "converted"
        if lifecycle_stage in {"customer", "advocate"}:
            return "converted"

        if self._matches_funnel_keywords(combined_text, "proposal_ready"):
            return "proposal_ready"
        if lifecycle_stage == "opportunity" or lead_score == "hot" or purchase_probability >= 0.55:
            return "proposal_ready"

        if self._matches_funnel_keywords(combined_text, "quote_requested"):
            return "quote_requested"
        if lifecycle_stage in {"mql", "sql", "consideration"} or lead_score == "warm" or purchase_probability >= 0.2:
            return "quote_requested"

        if self._matches_funnel_keywords(combined_text, "needs_confirmed"):
            return "needs_confirmed"
        if lifecycle_stage in {"lead", "awareness"}:
            return "needs_confirmed"

        meaningful_inbound_count = len(self._extract_meaningful_inbound_texts(messages))
        if meaningful_inbound_count >= 2:
            return "needs_confirmed"

        return self._legacy_stage_aliases.get(lifecycle_stage, "consultation")

    def _extract_meaningful_inbound_texts(self, messages: List[Dict]) -> List[str]:
        texts: List[str] = []
        for msg in messages or []:
            if str(msg.get("direction") or "").lower() != "inbound":
                continue
            cleaned = self._sanitize_message_text(msg.get("content", ""))
            if cleaned:
                texts.append(cleaned)
        return texts

    def _sanitize_message_text(self, text: str) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return ""
        if "撤回了一条消息" in value:
            return ""
        if "[分享视频]" in value and len(value) <= 20:
            return ""
        if self._TIME_ONLY_RE.fullmatch(value):
            return ""
        return value

    def _flatten_text_fragments(self, raw_value) -> str:
        if isinstance(raw_value, list):
            fragments = [str(item).strip() for item in raw_value if str(item).strip()]
            return " ".join(fragments)
        return str(raw_value or "").strip()

    def _matches_funnel_keywords(self, text: str, stage_key: str) -> bool:
        haystack = str(text or "")
        if not haystack:
            return False
        return any(keyword in haystack for keyword in self._funnel_keywords.get(stage_key, []))
    
    def _calculate_daily_trends(self, messages: List[Dict], days: int) -> List[Dict]:
        """计算每日趋势"""
        daily_counts = defaultdict(int)
        today = datetime.now()
        
        for msg in messages:
            ts = msg.get("created_at", msg.get("timestamp"))
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    
                    if (today - dt).days < days:
                        date_key = dt.strftime("%Y-%m-%d")
                        daily_counts[date_key] += 1
                except (ValueError, TypeError) as e:
                    logger.debug(f"日期解析失败: {e}")
        
        return [
            {"date": date, "count": count}
            for date, count in sorted(daily_counts.items())
        ]
    
    def _calculate_weekly_trends(self, messages: List[Dict], weeks: int) -> List[Dict]:
        """计算每周趋势"""
        weekly_counts = defaultdict(int)
        today = datetime.now()
        
        for msg in messages:
            ts = msg.get("created_at", msg.get("timestamp"))
            if ts:
                try:
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    
                    week_num = (today - dt).days // 7
                    if week_num < weeks:
                        week_key = f"Week {weeks - week_num}"
                        weekly_counts[week_key] += 1
                except (ValueError, TypeError) as e:
                    logger.debug(f"周时间解析失败: {e}")
        
        return [
            {"week": week, "count": count}
            for week, count in sorted(weekly_counts.items())
        ]
    
    def _generate_action_items(
        self,
        conversations: List[Dict],
        messages_by_conversation: Optional[Dict[str, List[Dict]]] = None,
        *,
        notify: bool = False,
    ) -> List[Dict]:
        """生成待办事项，仅保留已留资或高意向客户。"""
        return get_follow_up_reminder_service().build_action_items(
            conversations,
            messages_by_conversation,
            notify=notify,
            limit=20,
        )
    
    def _get_pending_follow_ups(self, conversations: List[Dict]) -> List[Dict]:
        """获取待跟进客户"""
        messages_by_conversation = self._group_messages_by_conversation(self._get_all_messages())
        return self._generate_action_items(
            conversations,
            messages_by_conversation,
            notify=False,
        )
    
    def _generate_alerts(self, conversations: List[Dict]) -> List[Dict]:
        """生成预警"""
        alerts = []
        
        # 高意向客户数量预警
        hot_count = sum(1 for c in conversations if c.get("lead_score") == "hot")
        if hot_count > 5:
            alerts.append({
                "type": "hot_leads_overflow",
                "level": "warning",
                "message": f"有 {hot_count} 个热线索待跟进"
            })
        
        # 流失风险预警
        at_risk = sum(1 for c in conversations if c.get("risk_factors"))
        if at_risk > 3:
            alerts.append({
                "type": "churn_risk",
                "level": "warning",
                "message": f"有 {at_risk} 个客户存在流失风险"
            })
        
        return alerts
    
    def _calculate_interest_distribution(self, conversations: List[Dict]) -> Dict[str, int]:
        """计算兴趣分布"""
        interest_counts = defaultdict(int)
        
        for conv in conversations:
            signals = conv.get("signals_detected", [])
            for signal in signals:
                interest_counts[signal] += 1
        
        return dict(interest_counts)
    
    def _extract_keywords(self, content: str) -> List[str]:
        """提取关键词"""
        keywords = []
        important_words = [
            "产品", "价格", "服务", "合作", "购买", "功能", "费用",
            "售后", "代理", "下单", "系统", "多少钱", "培训", "加盟"
        ]
        for word in important_words:
            if word in content and word not in keywords:
                keywords.append(word)
        return keywords[:10]
    
    def _extract_interests(self, content: str) -> List[str]:
        """提取兴趣"""
        interests = []
        interest_keywords = {
            "产品咨询": ["产品", "功能", "系统"],
            "价格咨询": ["价格", "费用", "多少钱"],
            "合作意向": ["合作", "代理", "加盟"],
            "购买意向": ["购买", "下单", "付款"]
        }
        
        for interest, keywords in interest_keywords.items():
            if any(kw in content for kw in keywords):
                interests.append(interest)
        
        return interests
    
    def _calculate_engagement_level(self, insight: CustomerInsight) -> str:
        """计算活跃度等级"""
        if (insight.total_messages >= self.ENGAGEMENT_THRESHOLDS["high"]["min_messages"] and
            insight.response_rate >= self.ENGAGEMENT_THRESHOLDS["high"]["min_response_rate"]):
            return "high"
        elif (insight.total_messages >= self.ENGAGEMENT_THRESHOLDS["medium"]["min_messages"] and
              insight.response_rate >= self.ENGAGEMENT_THRESHOLDS["medium"]["min_response_rate"]):
            return "medium"
        else:
            return "low"
    
    def _generate_tags(self, insight: CustomerInsight) -> List[str]:
        """生成标签"""
        tags = []
        
        # 意向标签
        if insight.lead_score == "hot":
            tags.append("热线索")
        elif insight.lead_score == "warm":
            tags.append("温线索")
        
        # 活跃度标签
        if insight.engagement_level == "high":
            tags.append("高活跃")
        
        # 角色标签
        if insight.buying_role == "decision_maker":
            tags.append("决策者")
        elif insight.buying_role == "champion":
            tags.append("支持者")
        
        # 兴趣标签
        for interest in insight.interests[:3]:
            tags.append(interest)
        
        return tags
    
    def _calculate_lifetime_value(self, insight: CustomerInsight) -> float:
        """计算生命周期价值"""
        base_value = insight.estimated_value
        
        # 根据活跃度调整
        engagement_multiplier = {"high": 1.5, "medium": 1.0, "low": 0.7}
        base_value *= engagement_multiplier.get(insight.engagement_level, 1.0)
        
        # 根据意向调整
        base_value *= (1 + insight.purchase_probability)
        
        return base_value
    
    def _predict_next_action(self, insight: CustomerInsight) -> str:
        """预测下一步行动"""
        if insight.lead_score == "hot":
            return "安排产品演示或发送报价"
        elif insight.lead_score == "warm":
            return "发送案例资料或安排咨询"
        elif insight.engagement_level == "low":
            return "发送培育内容激活客户"
        else:
            return "继续跟进了解需求"
    
    def _generate_follow_up_recommendation(self, insight: CustomerInsight) -> str:
        """生成跟进建议"""
        if insight.lead_score == "hot":
            return "建议24小时内电话跟进，发送定制方案"
        elif insight.lead_score == "warm":
            return "建议48小时内发送产品资料，安排演示"
        elif insight.risk_factors:
            return "建议尽快处理客户顾虑，提供解决方案"
        else:
            return "建议定期发送有价值的内容，保持联系"
    
    def _generate_trend_insights(self, trends: List[Dict]) -> List[str]:
        """生成趋势洞察"""
        insights = []
        
        if not trends:
            return ["暂无足够数据进行分析"]
        
        # 分析趋势方向
        up_days = sum(1 for t in trends if t["trend"] == "up")
        down_days = sum(1 for t in trends if t["trend"] == "down")
        
        if up_days > down_days:
            insights.append("整体趋势向上，客户活跃度在增加")
        elif down_days > up_days:
            insights.append("整体趋势向下，需要加强客户运营")
        
        # 分析峰值
        if trends:
            peak = max(trends, key=lambda x: x["messages"])
            insights.append(f"峰值出现在 {peak['date']}，当日消息数 {peak['messages']}")
        
        return insights
    
    def _analyze_active_hours(self, messages: List[Tuple]) -> Dict:
        """分析活跃时段"""
        hour_counts = defaultdict(int)
        for dt, msg in messages:
            hour_counts[dt.hour] += 1
        
        if hour_counts:
            peak_hour = max(hour_counts.items(), key=lambda x: x[1])
            return {
                "peak_hour": peak_hour[0],
                "hour_distribution": dict(sorted(hour_counts.items()))
            }
        return {}
    
    def _analyze_response_pattern(self, messages: List[Tuple]) -> Dict:
        """分析响应模式"""
        response_times = []
        
        for i in range(1, len(messages)):
            if messages[i][1].get("direction") != messages[i-1][1].get("direction"):
                time_diff = (messages[i][0] - messages[i-1][0]).total_seconds() / 60
                response_times.append(time_diff)
        
        if response_times:
            return {
                "avg_response_time_minutes": sum(response_times) / len(response_times),
                "min_response_time_minutes": min(response_times),
                "max_response_time_minutes": max(response_times)
            }
        return {}
    
    def _analyze_engagement_trend(self, messages: List[Tuple]) -> Dict:
        """分析参与度趋势"""
        if len(messages) < 3:
            return {"trend": "insufficient_data"}
        
        # 将消息分成前半和后半
        mid = len(messages) // 2
        first_half = messages[:mid]
        second_half = messages[mid:]
        
        first_avg_len = sum(len(m[1].get("content", "")) for m in first_half) / len(first_half)
        second_avg_len = sum(len(m[1].get("content", "")) for m in second_half) / len(second_half)
        
        if second_avg_len > first_avg_len * 1.2:
            trend = "increasing"
        elif second_avg_len < first_avg_len * 0.8:
            trend = "decreasing"
        else:
            trend = "stable"
        
        return {
            "trend": trend,
            "first_half_avg_length": round(first_avg_len, 2),
            "second_half_avg_length": round(second_avg_len, 2)
        }
    
    def _analyze_topic_evolution(self, messages: List[Tuple]) -> Dict:
        """分析话题演变"""
        topics = []
        topic_keywords = {
            "产品咨询": ["产品", "功能", "系统"],
            "价格讨论": ["价格", "费用", "多少钱"],
            "合作洽谈": ["合作", "代理", "加盟"],
            "购买决策": ["购买", "下单", "付款"]
        }
        
        for dt, msg in messages:
            content = msg.get("content", "")
            for topic, keywords in topic_keywords.items():
                if any(kw in content for kw in keywords):
                    topics.append({"date": dt.strftime("%Y-%m-%d"), "topic": topic})
                    break
        
        return {"topics": topics[-10:]}  # 返回最近10个话题
    
    def _analyze_interaction_frequency(self, messages: List[Tuple]) -> Dict:
        """分析交互频率"""
        if len(messages) < 2:
            return {"frequency": "insufficient_data"}
        
        # 计算消息间隔
        intervals = []
        for i in range(1, len(messages)):
            interval = (messages[i][0] - messages[i-1][0]).total_seconds() / 3600  # 小时
            intervals.append(interval)
        
        avg_interval = sum(intervals) / len(intervals)
        
        if avg_interval < 1:
            frequency = "very_high"
        elif avg_interval < 24:
            frequency = "high"
        elif avg_interval < 72:
            frequency = "medium"
        else:
            frequency = "low"
        
        return {
            "frequency": frequency,
            "avg_interval_hours": round(avg_interval, 2)
        }
    
    def _generate_behavior_insights(self, patterns: Dict) -> List[str]:
        """生成行为洞察"""
        insights = []
        
        # 活跃时段洞察
        if patterns.get("active_hours", {}).get("peak_hour"):
            peak = patterns["active_hours"]["peak_hour"]
            insights.append(f"客户最活跃时段为 {peak}:00 左右，建议此时段联系")
        
        # 响应模式洞察
        if patterns.get("response_pattern", {}).get("avg_response_time_minutes"):
            avg_time = patterns["response_pattern"]["avg_response_time_minutes"]
            if avg_time < 30:
                insights.append("客户响应迅速，购买意向可能较高")
            elif avg_time > 1440:  # 超过24小时
                insights.append("客户响应较慢，需要更有吸引力的内容")
        
        # 参与度趋势洞察
        if patterns.get("engagement_trend", {}).get("trend") == "increasing":
            insights.append("客户参与度在提升，是积极信号")
        elif patterns.get("engagement_trend", {}).get("trend") == "decreasing":
            insights.append("客户参与度在下降，需要关注")
        
        return insights
    
    def _estimate_conversion_time(self, insight: CustomerInsight) -> int:
        """预估成交时间"""
        base_days = 30
        
        # 根据意向等级调整
        lead_adjustments = {
            "hot": -20,
            "warm": -10,
            "cool": 0,
            "cold": 15
        }
        base_days += lead_adjustments.get(insight.lead_score, 0)
        
        # 根据生命周期阶段调整
        stage_adjustments = {
            "opportunity": -15,
            "sql": -10,
            "mql": -5,
            "lead": 0,
            "prospect": 10
        }
        base_days += stage_adjustments.get(insight.lifecycle_stage, 0)
        
        return max(1, base_days)
    
    def _identify_key_factors(self, insight: CustomerInsight) -> List[Dict]:
        """识别关键影响因素"""
        factors = []
        
        # 正向因素
        if insight.lead_score in ["hot", "warm"]:
            factors.append({"factor": "high_intent", "impact": "positive", "weight": 0.3})
        
        if insight.buying_role == "decision_maker":
            factors.append({"factor": "decision_maker", "impact": "positive", "weight": 0.25})
        
        if insight.engagement_level == "high":
            factors.append({"factor": "high_engagement", "impact": "positive", "weight": 0.2})
        
        # 负向因素
        if insight.risk_factors:
            factors.append({"factor": "risk_factors", "impact": "negative", "weight": -0.2})
        
        if insight.engagement_level == "low":
            factors.append({"factor": "low_engagement", "impact": "negative", "weight": -0.15})
        
        return factors
    
    def _calculate_confidence(self, insight: CustomerInsight) -> str:
        """计算置信度"""
        confidence_score = 0
        
        # 消息数量
        if insight.total_messages >= 10:
            confidence_score += 0.3
        elif insight.total_messages >= 5:
            confidence_score += 0.2
        else:
            confidence_score += 0.1
        
        # 信号数量
        if len(insight.signals) >= 3:
            confidence_score += 0.3
        elif len(insight.signals) >= 1:
            confidence_score += 0.2
        
        # 活跃度
        if insight.engagement_level == "high":
            confidence_score += 0.4
        elif insight.engagement_level == "medium":
            confidence_score += 0.3
        else:
            confidence_score += 0.1
        
        if confidence_score >= 0.7:
            return "high"
        elif confidence_score >= 0.4:
            return "medium"
        else:
            return "low"
    
    def _generate_conversion_recommendations(self, insight: CustomerInsight) -> List[str]:
        """生成转化建议"""
        recommendations = []
        
        if insight.lead_score == "hot":
            recommendations.append("立即安排产品演示或试用")
            recommendations.append("发送定制报价方案")
        elif insight.lead_score == "warm":
            recommendations.append("发送成功案例和客户评价")
            recommendations.append("安排销售顾问一对一沟通")
        
        if insight.buying_role == "decision_maker":
            recommendations.append("提供高管级别的沟通和服务")
        
        if insight.risk_factors:
            recommendations.append("主动解决客户顾虑：" + "、".join(insight.risk_factors[:2]))
        
        if insight.engagement_level == "low":
            recommendations.append("发送有价值的培育内容激活客户")
        
        return recommendations
    
    def _generate_segmentation_insights(self, segments: Dict) -> List[str]:
        """生成分群洞察"""
        insights = []

        if segments["hot_leads"]:
            insights.append(f"有 {len(segments['hot_leads'])} 个热线索需要优先跟进")

        if segments["at_risk"]:
            insights.append(f"有 {len(segments['at_risk'])} 个客户存在流失风险，需要关注")

        if segments["champions"]:
            insights.append(f"有 {len(segments['champions'])} 个支持者可以协助推广")

        if segments["nurture_candidates"]:
            insights.append(f"有 {len(segments['nurture_candidates'])} 个客户适合培育转化")

        return insights

    # ==================== 旧版兼容方法 ====================

    def getDashboardStats(self) -> Dict:
        """兼容旧版 AnalyticsService.getDashboardStats()"""
        dashboard = self.get_dashboard()
        conversations = self._get_all_conversations()

        intent_distribution = defaultdict(int)
        for conv in conversations:
            level = str(conv.get("intent_level") or "C").strip().upper()
            intent_distribution[level] += 1

        profiles_count = dashboard.total_customers
        high_intent_count = sum(
            1 for conv in conversations
            if str(conv.get("lead_score") or "").strip().lower() in ("hot", "warm")
        )

        avg_conversion_probability = 0.0
        avg_lifetime_value = 0.0
        if conversations:
            probabilities = [
                float(conv.get("purchase_probability") or 0)
                for conv in conversations
            ]
            avg_conversion_probability = sum(probabilities) / len(probabilities)
            avg_lifetime_value = dashboard.avg_deal_size

        return {
            "totalCustomers": profiles_count,
            "intentDistribution": dict(intent_distribution),
            "highIntentCount": high_intent_count,
            "avgConversionProbability": avg_conversion_probability,
            "avgLifetimeValue": avg_lifetime_value,
        }

    def getCustomerProfile(self, customerId: str) -> Optional[Dict]:
        """兼容旧版 AnalyticsService.getCustomerProfile()"""
        insight = self.get_customer_insight(customerId)
        if not insight:
            return None

        return {
            "customerId": insight.customer_id,
            "name": insight.customer_name,
            "platform": insight.platform,
            "intentLevel": insight.lead_score,
            "intentScore": insight.intent_score,
            "interests": insight.interests,
            "keywords": insight.keywords,
            "tags": insight.tags,
            "conversionProbability": insight.purchase_probability,
            "lifetimeValue": insight.lifetime_value,
            "totalMessages": insight.total_messages,
            "firstContactTime": insight.first_contact_time.isoformat() if insight.first_contact_time else None,
            "lastContactTime": insight.last_contact_time.isoformat() if insight.last_contact_time else None,
        }

    def getConversionFunnel(
        self,
        startDate: datetime = None,
        endDate: datetime = None
    ) -> Dict:
        """兼容旧版 AnalyticsService.getConversionFunnel()"""
        conversations = self._get_all_conversations()
        if startDate and endDate:
            filtered = []
            for conv in conversations:
                created = conv.get("created_at") or conv.get("first_contact_time")
                if created:
                    try:
                        if isinstance(created, str):
                            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        else:
                            dt = created
                        if startDate <= dt <= endDate:
                            filtered.append(conv)
                    except (ValueError, TypeError):
                        pass
            conversations = filtered

        funnel = self._build_conversion_funnel(conversations, self._get_all_messages())
        overall_rate = self._calculate_conversion_rate(conversations, self._get_all_messages())

        return {
            "stages": [
                {
                    "name": stage.get("name", ""),
                    "count": stage.get("count", 0),
                    "percentage": stage.get("percentage", 0),
                    "dropoffRate": stage.get("dropoff_rate", 0),
                }
                for stage in funnel
            ],
            "totalEntries": len(conversations),
            "overallConversionRate": overall_rate,
        }

    def getInterestDistribution(self) -> Dict:
        """兼容旧版 AnalyticsService.getInterestDistribution()"""
        conversations = self._get_all_conversations()
        return self._calculate_interest_distribution(conversations)

    def getTopCustomers(self, limit: int = 10) -> List[Dict]:
        """兼容旧版 AnalyticsService.getTopCustomers()"""
        conversations = self._get_all_conversations()

        sorted_conversations = sorted(
            conversations,
            key=lambda c: (
                float(c.get("purchase_intent_score") or 0),
                float(c.get("purchase_probability") or 0),
            ),
            reverse=True,
        )

        return [
            {
                "customerId": conv.get("customer_id") or conv.get("conversation_id") or conv.get("customer_name", ""),
                "name": conv.get("customer_name", "未知用户"),
                "intentLevel": conv.get("intent_level", "C"),
                "conversionProbability": float(conv.get("purchase_probability") or 0),
                "lifetimeValue": float(conv.get("estimated_deal_size") or 0),
                "tags": conv.get("tags", []) if isinstance(conv.get("tags"), list) else [],
            }
            for conv in sorted_conversations[:limit]
        ]

    def getTrendAnalysis(self, days: int = 7) -> Dict:
        """兼容旧版 AnalyticsService.getTrendAnalysis()"""
        result = self.get_trend_analysis(days)

        if "error" in result:
            return result

        daily_stats = {}
        for trend in result.get("trends", []):
            date_key = trend.get("date", "")
            daily_stats[date_key] = {
                "newCustomers": trend.get("new_customers", 0),
                "activeCustomers": trend.get("messages", 0),
                "convertedCustomers": trend.get("high_intent", 0),
            }

        summary = result.get("summary", {})
        return {
            "period": f"{days}天",
            "dailyStats": daily_stats,
            "totalNewCustomers": summary.get("total_new_customers", 0),
            "totalActiveCustomers": summary.get("total_messages", 0),
            "totalConvertedCustomers": summary.get("total_high_intent", 0),
        }


def get_enhanced_analytics_service(database=None, purchase_intent_analyzer=None, enterprise_id: str = "") -> EnhancedAnalyticsService:
    """
    获取增强分析服务实例
    
    Args:
        database: 数据库实例
        purchase_intent_analyzer: 购买意向分析器
        
    Returns:
        EnhancedAnalyticsService: 服务实例
    """
    return EnhancedAnalyticsService(
        database=database,
        purchase_intent_analyzer=purchase_intent_analyzer,
        enterprise_id=enterprise_id,
    )
