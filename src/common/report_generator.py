"""
自动化报告生成服务 - 企业级
支持日报、周报、月报自动生成，多维度数据分析报告
"""
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json
import logging

from src.common.chat_store import ChatStoreFacade

logger = logging.getLogger(__name__)


class ReportType(Enum):
    """报告类型"""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CUSTOMER = "customer"
    CAMPAIGN = "campaign"


class ReportFormat(Enum):
    """报告格式"""
    JSON = "json"
    HTML = "html"
    MARKDOWN = "markdown"


@dataclass
class ReportSection:
    """报告章节"""
    title: str
    content: str
    data: Dict = field(default_factory=dict)
    charts: List[Dict] = field(default_factory=list)
    insights: List[str] = field(default_factory=list)


@dataclass
class GeneratedReport:
    """生成的报告"""
    report_id: str
    report_type: ReportType
    title: str
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    
    # 报告内容
    summary: str
    sections: List[ReportSection] = field(default_factory=list)
    
    # 关键指标
    key_metrics: Dict = field(default_factory=dict)
    
    # 建议和行动项
    recommendations: List[str] = field(default_factory=list)
    action_items: List[Dict] = field(default_factory=list)
    
    # 元数据
    metadata: Dict = field(default_factory=dict)


class ReportGenerator:
    """
    报告生成器
    
    功能：
    1. 日报/周报/月报自动生成
    2. 客户分析报告
    3. 活动效果报告
    4. 多格式导出
    """
    
    # 报告模板
    REPORT_TEMPLATES = {
        ReportType.DAILY: {
            "title": "每日客户分析报告",
            "sections": [
                "overview",
                "new_customers",
                "hot_leads",
                "conversions",
                "action_items"
            ]
        },
        ReportType.WEEKLY: {
            "title": "每周客户分析报告",
            "sections": [
                "overview",
                "trends",
                "funnel",
                "top_customers",
                "segments",
                "recommendations"
            ]
        },
        ReportType.MONTHLY: {
            "title": "每月客户分析报告",
            "sections": [
                "overview",
                "trends",
                "funnel",
                "segments",
                "value_analysis",
                "performance",
                "recommendations"
            ]
        },
        ReportType.CUSTOMER: {
            "title": "客户分析报告",
            "sections": [
                "profile",
                "behavior",
                "intent",
                "conversion_prediction",
                "recommendations"
            ]
        }
    }
    
    def __init__(self, database=None, analytics_service=None):
        """
        初始化报告生成器
        
        Args:
            database: 数据库实例
            analytics_service: 分析服务实例
        """
        self.database = database
        self.chat_store = ChatStoreFacade(database) if database else None
        self.analytics_service = analytics_service
        self._report_cache: Dict[str, GeneratedReport] = {}
    
    def generate_daily_report(self, date: datetime = None) -> GeneratedReport:
        """
        生成日报
        
        Args:
            date: 报告日期，默认今天
            
        Returns:
            GeneratedReport: 生成的报告
        """
        if date is None:
            date = datetime.now()
        
        period_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        period_end = period_start + timedelta(days=1)
        
        report = GeneratedReport(
            report_id=f"daily_{date.strftime('%Y%m%d')}",
            report_type=ReportType.DAILY,
            title=f"每日客户分析报告 - {date.strftime('%Y年%m月%d日')}",
            generated_at=datetime.now(),
            period_start=period_start,
            period_end=period_end,
            summary=""
        )
        
        # 生成各章节
        report.sections.append(self._generate_overview_section(period_start, period_end))
        report.sections.append(self._generate_new_customers_section(period_start, period_end))
        report.sections.append(self._generate_hot_leads_section())
        report.sections.append(self._generate_conversions_section(period_start, period_end))
        report.sections.append(self._generate_action_items_section())
        
        # 生成摘要
        report.summary = self._generate_daily_summary(report)
        
        # 关键指标
        report.key_metrics = self._extract_key_metrics(report)
        
        # 建议
        report.recommendations = self._generate_recommendations(report)
        
        # 缓存报告
        self._report_cache[report.report_id] = report
        
        return report
    
    def generate_weekly_report(self, week_start: datetime = None) -> GeneratedReport:
        """
        生成周报
        
        Args:
            week_start: 周开始日期，默认本周一
            
        Returns:
            GeneratedReport: 生成的报告
        """
        if week_start is None:
            week_start = datetime.now() - timedelta(days=datetime.now().weekday())
        
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        
        report = GeneratedReport(
            report_id=f"weekly_{week_start.strftime('%Y%m%d')}",
            report_type=ReportType.WEEKLY,
            title=f"每周客户分析报告 - {week_start.strftime('%Y年%m月%d日')} 至 {(week_end - timedelta(days=1)).strftime('%Y年%m月%d日')}",
            generated_at=datetime.now(),
            period_start=week_start,
            period_end=week_end,
            summary=""
        )
        
        # 生成各章节
        report.sections.append(self._generate_overview_section(week_start, week_end))
        report.sections.append(self._generate_trends_section(week_start, week_end))
        report.sections.append(self._generate_funnel_section())
        report.sections.append(self._generate_top_customers_section())
        report.sections.append(self._generate_segments_section())
        report.sections.append(self._generate_recommendations_section())
        
        # 生成摘要
        report.summary = self._generate_weekly_summary(report)
        
        # 关键指标
        report.key_metrics = self._extract_key_metrics(report)
        
        # 行动项
        report.action_items = self._extract_action_items(report)
        
        # 缓存报告
        self._report_cache[report.report_id] = report
        
        return report
    
    def generate_monthly_report(self, month_start: datetime = None) -> GeneratedReport:
        """
        生成月报
        
        Args:
            month_start: 月开始日期，默认本月1日
            
        Returns:
            GeneratedReport: 生成的报告
        """
        if month_start is None:
            month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # 计算月末
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        
        report = GeneratedReport(
            report_id=f"monthly_{month_start.strftime('%Y%m')}",
            report_type=ReportType.MONTHLY,
            title=f"每月客户分析报告 - {month_start.strftime('%Y年%m月')}",
            generated_at=datetime.now(),
            period_start=month_start,
            period_end=month_end,
            summary=""
        )
        
        # 生成各章节
        report.sections.append(self._generate_overview_section(month_start, month_end))
        report.sections.append(self._generate_trends_section(month_start, month_end))
        report.sections.append(self._generate_funnel_section())
        report.sections.append(self._generate_segments_section())
        report.sections.append(self._generate_value_analysis_section())
        report.sections.append(self._generate_performance_section(month_start, month_end))
        report.sections.append(self._generate_recommendations_section())
        
        # 生成摘要
        report.summary = self._generate_monthly_summary(report)
        
        # 关键指标
        report.key_metrics = self._extract_key_metrics(report)
        
        # 行动项
        report.action_items = self._extract_action_items(report)
        
        # 缓存报告
        self._report_cache[report.report_id] = report
        
        return report
    
    def generate_customer_report(self, customer_name: str) -> GeneratedReport:
        """
        生成客户分析报告
        
        Args:
            customer_name: 客户名称
            
        Returns:
            GeneratedReport: 生成的报告
        """
        now = datetime.now()
        
        report = GeneratedReport(
            report_id=f"customer_{customer_name}_{now.strftime('%Y%m%d%H%M%S')}",
            report_type=ReportType.CUSTOMER,
            title=f"客户分析报告 - {customer_name}",
            generated_at=now,
            period_start=now - timedelta(days=30),
            period_end=now,
            summary=""
        )
        
        # 获取客户洞察
        if self.analytics_service:
            insight = self.analytics_service.get_customer_insight(customer_name)
            behavior = self.analytics_service.get_behavior_analysis(customer_name)
            prediction = self.analytics_service.get_conversion_prediction(customer_name)
        else:
            insight = None
            behavior = None
            prediction = None
        
        # 生成各章节
        report.sections.append(self._generate_customer_profile_section(customer_name, insight))
        report.sections.append(self._generate_customer_behavior_section(customer_name, behavior))
        report.sections.append(self._generate_customer_intent_section(customer_name, insight))
        report.sections.append(self._generate_customer_prediction_section(customer_name, prediction))
        report.sections.append(self._generate_customer_recommendations_section(customer_name, insight))
        
        # 生成摘要
        report.summary = self._generate_customer_summary(customer_name, insight)
        
        # 关键指标
        if insight:
            report.key_metrics = {
                "intent_score": insight.intent_score,
                "lead_score": insight.lead_score,
                "purchase_probability": insight.purchase_probability,
                "engagement_level": insight.engagement_level,
                "estimated_value": insight.estimated_value
            }
        
        # 缓存报告
        self._report_cache[report.report_id] = report
        
        return report
    
    def export_report(self, report: GeneratedReport, format: ReportFormat = ReportFormat.JSON) -> str:
        """
        导出报告
        
        Args:
            report: 报告对象
            format: 导出格式
            
        Returns:
            str: 导出的内容
        """
        if format == ReportFormat.JSON:
            return self._export_json(report)
        elif format == ReportFormat.HTML:
            return self._export_html(report)
        elif format == ReportFormat.MARKDOWN:
            return self._export_markdown(report)
        else:
            return self._export_json(report)
    
    def get_cached_report(self, report_id: str) -> Optional[GeneratedReport]:
        """获取缓存的报告"""
        return self._report_cache.get(report_id)
    
    def list_cached_reports(self) -> List[Dict]:
        """列出缓存的报告"""
        return [
            {
                "report_id": report.report_id,
                "report_type": report.report_type.value,
                "title": report.title,
                "generated_at": report.generated_at.isoformat()
            }
            for report in self._report_cache.values()
        ]
    
    # ==================== 章节生成方法 ====================
    
    def _generate_overview_section(self, start: datetime, end: datetime) -> ReportSection:
        """生成概览章节"""
        conversations = self._get_all_conversations()
        messages = self._get_messages_in_period(start, end)
        
        total_customers = len(conversations)
        total_messages = len(messages)
        high_intent = sum(1 for c in conversations if c.get("purchase_intent_score", 0) >= 70)
        hot_leads = sum(1 for c in conversations if c.get("lead_score") == "hot")
        
        return ReportSection(
            title="数据概览",
            content=f"在报告期间，共有 {total_customers} 个客户，产生了 {total_messages} 条消息交互。其中高意向客户 {high_intent} 个，热线索 {hot_leads} 个。",
            data={
                "total_customers": total_customers,
                "total_messages": total_messages,
                "high_intent_customers": high_intent,
                "hot_leads": hot_leads
            },
            insights=[
                f"客户总数: {total_customers}",
                f"消息总数: {total_messages}",
                f"高意向客户: {high_intent}",
                f"热线索: {hot_leads}"
            ]
        )
    
    def _generate_new_customers_section(self, start: datetime, end: datetime) -> ReportSection:
        """生成新客户章节"""
        conversations = self._get_all_conversations()
        
        new_customers = []
        for conv in conversations:
            created = conv.get("created_at")
            if created:
                try:
                    if isinstance(created, str):
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    else:
                        dt = created
                    
                    if start <= dt < end:
                        new_customers.append(conv)
                except Exception:
                    pass
        
        return ReportSection(
            title="新增客户",
            content=f"今日新增客户 {len(new_customers)} 个。",
            data={
                "count": len(new_customers),
                "customers": [
                    {
                        "name": c.get("customer_name", ""),
                        "intent_score": c.get("purchase_intent_score", 0)
                    }
                    for c in new_customers[:10]
                ]
            },
            insights=[f"新增客户数: {len(new_customers)}"]
        )
    
    def _generate_hot_leads_section(self) -> ReportSection:
        """生成热线索章节"""
        if self.analytics_service:
            hot_leads = self.analytics_service.get_high_intent_conversations(min_score=70)
        else:
            conversations = self._get_all_conversations()
            hot_leads = [c for c in conversations if c.get("lead_score") == "hot"]
        
        return ReportSection(
            title="热线索",
            content=f"当前有 {len(hot_leads)} 个热线索需要优先跟进。",
            data={
                "count": len(hot_leads),
                "leads": [
                    {
                        "name": c.get("customer_name", ""),
                        "score": c.get("purchase_intent_score", 0),
                        "probability": c.get("purchase_probability", 0)
                    }
                    for c in hot_leads[:10]
                ]
            },
            insights=[f"热线索数量: {len(hot_leads)}", "建议24小时内跟进"]
        )
    
    def _generate_conversions_section(self, start: datetime, end: datetime) -> ReportSection:
        """生成转化章节"""
        conversations = self._get_all_conversations()
        
        converted = sum(1 for c in conversations if c.get("lifecycle_stage") == "customer")
        conversion_rate = (converted / len(conversations) * 100) if conversations else 0
        
        return ReportSection(
            title="转化情况",
            content=f"报告期间转化率为 {conversion_rate:.2f}%。",
            data={
                "converted_count": converted,
                "conversion_rate": conversion_rate
            },
            insights=[f"转化客户数: {converted}", f"转化率: {conversion_rate:.2f}%"]
        )
    
    def _generate_action_items_section(self) -> ReportSection:
        """生成待办事项章节"""
        if self.analytics_service:
            dashboard = self.analytics_service.get_dashboard()
            action_items = dashboard.action_items
        else:
            action_items = []
        
        return ReportSection(
            title="待办事项",
            content=f"共有 {len(action_items)} 项待处理事项。",
            data={
                "count": len(action_items),
                "items": action_items[:10]
            },
            insights=[f"待办事项: {len(action_items)} 项"]
        )
    
    def _generate_trends_section(self, start: datetime, end: datetime) -> ReportSection:
        """生成趋势章节"""
        if self.analytics_service:
            days = (end - start).days
            trends = self.analytics_service.get_trend_analysis(days)
        else:
            trends = {"trends": [], "summary": {}}
        
        return ReportSection(
            title="趋势分析",
            content="展示报告期间的数据变化趋势。",
            data=trends,
            insights=trends.get("insights", [])
        )
    
    def _generate_funnel_section(self) -> ReportSection:
        """生成漏斗章节"""
        conversations = self._get_all_conversations()
        
        stages = {
            "潜在客户": 0,
            "线索": 0,
            "营销合格": 0,
            "销售合格": 0,
            "商机": 0,
            "客户": 0
        }
        
        for conv in conversations:
            stage = conv.get("lifecycle_stage", "prospect")
            stage_mapping = {
                "prospect": "潜在客户",
                "lead": "线索",
                "mql": "营销合格",
                "sql": "销售合格",
                "opportunity": "商机",
                "customer": "客户"
            }
            stage_name = stage_mapping.get(stage, "潜在客户")
            stages[stage_name] += 1
        
        return ReportSection(
            title="转化漏斗",
            content="客户转化漏斗分析。",
            data={"stages": stages},
            insights=[f"{stage}: {count}" for stage, count in stages.items()]
        )
    
    def _generate_top_customers_section(self) -> ReportSection:
        """生成高价值客户章节"""
        conversations = self._get_all_conversations()
        
        sorted_customers = sorted(
            conversations,
            key=lambda x: x.get("purchase_intent_score", 0),
            reverse=True
        )[:10]
        
        return ReportSection(
            title="高价值客户",
            content="按意向评分排序的前10名客户。",
            data={
                "customers": [
                    {
                        "name": c.get("customer_name", ""),
                        "score": c.get("purchase_intent_score", 0),
                        "lead_score": c.get("lead_score", "cold")
                    }
                    for c in sorted_customers
                ]
            },
            insights=[f"最高意向: {sorted_customers[0].get('customer_name', '')}" if sorted_customers else "暂无数据"]
        )
    
    def _generate_segments_section(self) -> ReportSection:
        """生成分群章节"""
        if self.analytics_service:
            segments = self.analytics_service.get_segment_analysis()
        else:
            segments = {"segments": {}}
        
        return ReportSection(
            title="客户分群",
            content="按意向和行为特征对客户进行分群。",
            data=segments,
            insights=segments.get("segmentation_insights", [])
        )
    
    def _generate_value_analysis_section(self) -> ReportSection:
        """生成价值分析章节"""
        conversations = self._get_all_conversations()
        
        total_value = sum(c.get("estimated_deal_size", 0) for c in conversations)
        avg_value = total_value / len(conversations) if conversations else 0
        
        return ReportSection(
            title="价值分析",
            content=f"预估总价值 ¥{total_value:,.0f}，平均客户价值 ¥{avg_value:,.0f}。",
            data={
                "total_value": total_value,
                "avg_value": avg_value
            },
            insights=[f"预估总价值: ¥{total_value:,.0f}", f"平均客户价值: ¥{avg_value:,.0f}"]
        )
    
    def _generate_performance_section(self, start: datetime, end: datetime) -> ReportSection:
        """生成绩效章节"""
        return ReportSection(
            title="绩效分析",
            content="报告期间的团队绩效分析。",
            data={
                "period_days": (end - start).days
            },
            insights=["绩效数据需要进一步配置"]
        )
    
    def _generate_recommendations_section(self) -> ReportSection:
        """生成建议章节"""
        return ReportSection(
            title="优化建议",
            content="基于数据分析的优化建议。",
            data={},
            insights=[
                "建议优先跟进热线索",
                "关注高意向客户的转化",
                "对低活跃客户进行培育激活"
            ]
        )
    
    def _generate_customer_profile_section(self, customer_name: str, insight) -> ReportSection:
        """生成客户画像章节"""
        if insight:
            content = f"客户 {customer_name} 的基础画像信息。"
            data = {
                "customer_name": insight.customer_name,
                "platform": insight.platform,
                "total_messages": insight.total_messages,
                "engagement_level": insight.engagement_level,
                "tags": insight.tags
            }
            insights = [
                f"消息总数: {insight.total_messages}",
                f"活跃度: {insight.engagement_level}",
                f"标签: {', '.join(insight.tags[:3])}"
            ]
        else:
            content = "暂无客户画像数据。"
            data = {}
            insights = []
        
        return ReportSection(
            title="客户画像",
            content=content,
            data=data,
            insights=insights
        )
    
    def _generate_customer_behavior_section(self, customer_name: str, behavior) -> ReportSection:
        """生成客户行为章节"""
        if behavior and "behavior_patterns" in behavior:
            patterns = behavior["behavior_patterns"]
            content = f"客户 {customer_name} 的行为模式分析。"
            data = patterns
            insights = behavior.get("insights", [])
        else:
            content = "暂无行为分析数据。"
            data = {}
            insights = []
        
        return ReportSection(
            title="行为分析",
            content=content,
            data=data,
            insights=insights
        )
    
    def _generate_customer_intent_section(self, customer_name: str, insight) -> ReportSection:
        """生成客户意向章节"""
        if insight:
            content = f"客户 {customer_name} 的购买意向分析。"
            data = {
                "intent_score": insight.intent_score,
                "lead_score": insight.lead_score,
                "purchase_probability": insight.purchase_probability,
                "lifecycle_stage": insight.lifecycle_stage,
                "buying_role": insight.buying_role,
                "signals": insight.signals
            }
            insights = [
                f"意向评分: {insight.intent_score:.1f}",
                f"线索等级: {insight.lead_score}",
                f"购买概率: {insight.purchase_probability:.0%}"
            ]
        else:
            content = "暂无意向分析数据。"
            data = {}
            insights = []
        
        return ReportSection(
            title="意向分析",
            content=content,
            data=data,
            insights=insights
        )
    
    def _generate_customer_prediction_section(self, customer_name: str, prediction) -> ReportSection:
        """生成转化预测章节"""
        if prediction and "error" not in prediction:
            content = f"客户 {customer_name} 的转化预测。"
            data = prediction
            insights = [
                f"转化概率: {prediction.get('conversion_probability', 0):.0%}",
                f"预估成交天数: {prediction.get('estimated_days_to_close', 0)} 天",
                f"预估价值: ¥{prediction.get('estimated_value', 0):,.0f}"
            ]
        else:
            content = "暂无转化预测数据。"
            data = {}
            insights = []
        
        return ReportSection(
            title="转化预测",
            content=content,
            data=data,
            insights=insights
        )
    
    def _generate_customer_recommendations_section(self, customer_name: str, insight) -> ReportSection:
        """生成客户建议章节"""
        if insight:
            recommendations = [
                insight.predicted_next_action,
                insight.recommended_follow_up
            ]
        else:
            recommendations = ["建议进一步了解客户需求"]
        
        return ReportSection(
            title="跟进建议",
            content=f"针对客户 {customer_name} 的跟进建议。",
            data={"recommendations": recommendations},
            insights=recommendations
        )
    
    # ==================== 摘要生成方法 ====================
    
    def _generate_daily_summary(self, report: GeneratedReport) -> str:
        """生成日报摘要"""
        overview = next((s for s in report.sections if s.title == "数据概览"), None)
        if overview:
            data = overview.data
            return f"今日共 {data.get('total_customers', 0)} 个客户，{data.get('total_messages', 0)} 条消息，{data.get('hot_leads', 0)} 个热线索待跟进。"
        return "暂无数据摘要。"
    
    def _generate_weekly_summary(self, report: GeneratedReport) -> str:
        """生成周报摘要"""
        overview = next((s for s in report.sections if s.title == "数据概览"), None)
        funnel = next((s for s in report.sections if s.title == "转化漏斗"), None)
        
        summary_parts = []
        if overview:
            data = overview.data
            summary_parts.append(f"本周共 {data.get('total_customers', 0)} 个客户")
        if funnel:
            stages = funnel.data.get("stages", {})
            summary_parts.append(f"转化漏斗: {stages}")
        
        return "。".join(summary_parts) if summary_parts else "暂无数据摘要。"
    
    def _generate_monthly_summary(self, report: GeneratedReport) -> str:
        """生成月报摘要"""
        overview = next((s for s in report.sections if s.title == "数据概览"), None)
        value = next((s for s in report.sections if s.title == "价值分析"), None)
        
        summary_parts = []
        if overview:
            data = overview.data
            summary_parts.append(f"本月共 {data.get('total_customers', 0)} 个客户")
        if value:
            data = value.data
            summary_parts.append(f"预估总价值 ¥{data.get('total_value', 0):,.0f}")
        
        return "。".join(summary_parts) if summary_parts else "暂无数据摘要。"
    
    def _generate_customer_summary(self, customer_name: str, insight) -> str:
        """生成客户摘要"""
        if insight:
            return f"客户 {customer_name}，意向评分 {insight.intent_score:.1f}，线索等级 {insight.lead_score}，购买概率 {insight.purchase_probability:.0%}。"
        return f"客户 {customer_name} 暂无足够数据。"
    
    # ==================== 辅助方法 ====================
    
    def _get_messages_in_period(self, start: datetime, end: datetime) -> List[Dict]:
        """获取时间段内的消息"""
        if self.chat_store:
            try:
                messages = self.chat_store.get_all_messages_dicts()
            except Exception:
                messages = []
        elif not self.database:
            return []
        else:
            try:
                get_all_messages = getattr(self.database, "get_all_messages", None)
                if callable(get_all_messages):
                    messages = list(get_all_messages() or [])
                else:
                    messages = []
            except Exception:
                return []
        try:
            result = []
            for msg in messages:
                ts = msg.get("created_at", msg.get("timestamp"))
                if ts:
                    try:
                        if isinstance(ts, str):
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        else:
                            dt = ts
                        
                        if start <= dt < end:
                            result.append(msg)
                    except Exception:
                        pass
            
            return result
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
    
    def _extract_key_metrics(self, report: GeneratedReport) -> Dict:
        """提取关键指标"""
        metrics = {}
        for section in report.sections:
            if section.data:
                for key, value in section.data.items():
                    if isinstance(value, (int, float, str)):
                        metrics[key] = value
        return metrics
    
    def _extract_action_items(self, report: GeneratedReport) -> List[Dict]:
        """提取行动项"""
        action_section = next((s for s in report.sections if s.title == "待办事项"), None)
        if action_section and action_section.data.get("items"):
            return action_section.data["items"]
        return []
    
    def _generate_recommendations(self, report: GeneratedReport) -> List[str]:
        """生成建议"""
        recommendations = []
        
        # 基于热线索数量
        hot_leads_section = next((s for s in report.sections if s.title == "热线索"), None)
        if hot_leads_section and hot_leads_section.data.get("count", 0) > 5:
            recommendations.append("热线索较多，建议增加跟进人手")
        
        # 基于转化率
        conversions_section = next((s for s in report.sections if s.title == "转化情况"), None)
        if conversions_section:
            rate = conversions_section.data.get("conversion_rate", 0)
            if rate < 10:
                recommendations.append("转化率偏低，建议优化跟进策略")
        
        return recommendations
    
    # ==================== 导出方法 ====================
    
    def _export_json(self, report: GeneratedReport) -> str:
        """导出为JSON格式"""
        return json.dumps({
            "report_id": report.report_id,
            "report_type": report.report_type.value,
            "title": report.title,
            "generated_at": report.generated_at.isoformat(),
            "period_start": report.period_start.isoformat(),
            "period_end": report.period_end.isoformat(),
            "summary": report.summary,
            "sections": [
                {
                    "title": s.title,
                    "content": s.content,
                    "data": s.data,
                    "insights": s.insights
                }
                for s in report.sections
            ],
            "key_metrics": report.key_metrics,
            "recommendations": report.recommendations,
            "action_items": report.action_items
        }, ensure_ascii=False, indent=2)
    
    def _export_html(self, report: GeneratedReport) -> str:
        """导出为HTML格式"""
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{report.title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        h1 {{ color: #333; }}
        h2 {{ color: #666; border-bottom: 1px solid #ddd; padding-bottom: 10px; }}
        .summary {{ background: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0; }}
        .section {{ margin: 20px 0; }}
        .insights {{ background: #e8f4f8; padding: 15px; border-radius: 5px; }}
        .metrics {{ display: flex; flex-wrap: wrap; gap: 20px; }}
        .metric {{ background: #f9f9f9; padding: 15px; border-radius: 5px; min-width: 150px; }}
        .metric-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .metric-label {{ color: #666; }}
    </style>
</head>
<body>
    <h1>{report.title}</h1>
    <p>生成时间: {report.generated_at.strftime('%Y年%m月%d日 %H:%M')}</p>
    <p>报告周期: {report.period_start.strftime('%Y年%m月%d日')} 至 {report.period_end.strftime('%Y年%m月%d日')}</p>
    
    <div class="summary">
        <h2>摘要</h2>
        <p>{report.summary}</p>
    </div>
    
    <div class="metrics">
        <div class="metric">
            <div class="metric-value">{report.key_metrics.get('total_customers', 0)}</div>
            <div class="metric-label">总客户数</div>
        </div>
        <div class="metric">
            <div class="metric-value">{report.key_metrics.get('hot_leads', 0)}</div>
            <div class="metric-label">热线索</div>
        </div>
        <div class="metric">
            <div class="metric-value">{report.key_metrics.get('high_intent_customers', 0)}</div>
            <div class="metric-label">高意向客户</div>
        </div>
    </div>
"""
        
        for section in report.sections:
            html += f"""
    <div class="section">
        <h2>{section.title}</h2>
        <p>{section.content}</p>
        <div class="insights">
            <strong>关键洞察:</strong>
            <ul>
"""
            for insight in section.insights:
                html += f"                <li>{insight}</li>\n"
            
            html += """            </ul>
        </div>
    </div>
"""
        
        if report.recommendations:
            html += """
    <div class="section">
        <h2>优化建议</h2>
        <ul>
"""
            for rec in report.recommendations:
                html += f"            <li>{rec}</li>\n"
            html += """        </ul>
    </div>
"""
        
        html += """
</body>
</html>
"""
        return html
    
    def _export_markdown(self, report: GeneratedReport) -> str:
        """导出为Markdown格式"""
        md = f"""# {report.title}

**生成时间:** {report.generated_at.strftime('%Y年%m月%d日 %H:%M')}

**报告周期:** {report.period_start.strftime('%Y年%m月%d日')} 至 {report.period_end.strftime('%Y年%m月%d日')}

## 摘要

{report.summary}

## 关键指标

| 指标 | 数值 |
|------|------|
| 总客户数 | {report.key_metrics.get('total_customers', 0)} |
| 热线索 | {report.key_metrics.get('hot_leads', 0)} |
| 高意向客户 | {report.key_metrics.get('high_intent_customers', 0)} |

"""
        
        for section in report.sections:
            md += f"""## {section.title}

{section.content}

**关键洞察:**
"""
            for insight in section.insights:
                md += f"- {insight}\n"
            md += "\n"
        
        if report.recommendations:
            md += """## 优化建议

"""
            for rec in report.recommendations:
                md += f"- {rec}\n"
        
        return md


def get_report_generator(database=None, analytics_service=None) -> ReportGenerator:
    """
    获取报告生成器实例
    
    Args:
        database: 数据库实例
        analytics_service: 分析服务实例
        
    Returns:
        ReportGenerator: 报告生成器实例
    """
    return ReportGenerator(database=database, analytics_service=analytics_service)
