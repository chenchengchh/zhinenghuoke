"""
数据分析增强服务
支持客户画像、转化漏斗、行为分析
"""
import warnings
warnings.warn(
    "analytics_service 模块已废弃，请使用 enhanced_analytics_service.EnhancedAnalyticsService 替代。",
    DeprecationWarning,
    stacklevel=2,
)
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict
import json


@dataclass
class CustomerProfile:
    """客户画像"""
    customerId: str
    name: str
    platform: str
    avatar: str = ""
    
    intentLevel: str = "C"
    intentScore: float = 0.0
    
    totalMessages: int = 0
    inboundMessages: int = 0
    outboundMessages: int = 0
    
    firstContactTime: datetime = None
    lastContactTime: datetime = None
    avgResponseTime: float = 0.0
    
    interests: List[str] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    
    tags: List[str] = field(default_factory=list)
    source: str = "unknown"
    status: str = "active"
    
    conversionProbability: float = 0.0
    lifetimeValue: float = 0.0
    
    metadata: Dict = field(default_factory=dict)


@dataclass
class FunnelStage:
    """漏斗阶段"""
    name: str
    count: int
    percentage: float
    dropoffRate: float
    avgTimeInStage: float


@dataclass
class ConversionFunnel:
    """转化漏斗"""
    stages: List[FunnelStage]
    totalEntries: int
    overallConversionRate: float
    avgTimeToConvert: float


class CustomerProfiler:
    """
    客户画像生成器
    分析客户行为，生成画像
    """
    
    INTENT_LEVEL_SCORES = {
        "A": 100,
        "B": 80,
        "C": 60,
        "D": 40,
        "E": 20
    }
    
    INTEREST_KEYWORDS = {
        "产品": ["产品", "功能", "系统", "软件", "工具"],
        "价格": ["价格", "费用", "多少钱", "收费", "套餐"],
        "服务": ["服务", "售后", "支持", "培训", "教程"],
        "合作": ["合作", "代理", "加盟", "OEM"],
        "购买": ["购买", "下单", "订购", "付款", "开通"]
    }
    
    def __init__(self, database=None):
        self.database = database
        self._profiles: Dict[str, CustomerProfile] = {}
    
    def generateProfile(
        self,
        customerId: str,
        messages: List[Dict],
        conversations: List[Dict] = None
    ) -> CustomerProfile:
        """
        生成客户画像
        
        Args:
            customerId: 客户ID
            messages: 消息列表
            conversations: 会话列表
        
        Returns:
            客户画像
        """
        if not messages:
            return CustomerProfile(
                customerId=customerId,
                name="未知用户",
                platform="unknown"
            )
        
        firstMessage = messages[0]
        profile = CustomerProfile(
            customerId=customerId,
            name=firstMessage.get("senderName", "未知用户"),
            platform=firstMessage.get("platform", "unknown"),
            avatar=firstMessage.get("senderAvatar", "")
        )
        
        profile.totalMessages = len(messages)
        profile.inboundMessages = sum(1 for m in messages if m.get("direction") == "inbound")
        profile.outboundMessages = sum(1 for m in messages if m.get("direction") == "outbound")
        
        timestamps = [m.get("timestamp") for m in messages if m.get("timestamp")]
        if timestamps:
            timestamps = sorted(timestamps)
            profile.firstContactTime = timestamps[0]
            profile.lastContactTime = timestamps[-1]
        
        allContent = " ".join(m.get("content", "") for m in messages)
        profile.interests = self._extractInterests(allContent)
        profile.keywords = self._extractKeywords(allContent)
        
        intentLevels = [m.get("intentLevel", "C") for m in messages if m.get("intentLevel")]
        if intentLevels:
            avgIntentScore = sum(
                self.INTENT_LEVEL_SCORES.get(level, 60)
                for level in intentLevels
            ) / len(intentLevels)
            
            if avgIntentScore >= 90:
                profile.intentLevel = "A"
            elif avgIntentScore >= 70:
                profile.intentLevel = "B"
            elif avgIntentScore >= 50:
                profile.intentLevel = "C"
            elif avgIntentScore >= 30:
                profile.intentLevel = "D"
            else:
                profile.intentLevel = "E"
            
            profile.intentScore = avgIntentScore
        
        profile.tags = self._generateTags(profile)
        profile.conversionProbability = self._calculateConversionProbability(profile)
        profile.lifetimeValue = self._estimateLifetimeValue(profile)
        
        self._profiles[customerId] = profile
        return profile
    
    def _extractInterests(self, content: str) -> List[str]:
        """提取兴趣点"""
        interests = []
        for interest, keywords in self.INTEREST_KEYWORDS.items():
            if any(kw in content for kw in keywords):
                interests.append(interest)
        return interests
    
    def _extractKeywords(self, content: str) -> List[str]:
        """提取关键词"""
        keywords = []
        importantWords = [
            "产品", "价格", "服务", "合作", "购买",
            "功能", "费用", "售后", "代理", "下单",
            "系统", "多少钱", "培训", "加盟", "付款"
        ]
        for word in importantWords:
            if word in content and word not in keywords:
                keywords.append(word)
        return keywords[:10]
    
    def _generateTags(self, profile: CustomerProfile) -> List[str]:
        """生成标签"""
        tags = []
        
        if profile.intentLevel == "A":
            tags.append("高意向客户")
        elif profile.intentLevel == "B":
            tags.append("中高意向客户")
        
        if "价格" in profile.interests:
            tags.append("价格敏感")
        if "合作" in profile.interests:
            tags.append("潜在合作伙伴")
        if "购买" in profile.interests:
            tags.append("购买意向")
        
        if profile.totalMessages > 20:
            tags.append("活跃用户")
        elif profile.totalMessages > 10:
            tags.append("中等活跃")
        
        return tags
    
    def _calculateConversionProbability(self, profile: CustomerProfile) -> float:
        """计算转化概率"""
        score = 0.0
        
        score += self.INTENT_LEVEL_SCORES.get(profile.intentLevel, 60) * 0.4
        
        if "购买" in profile.interests:
            score += 20
        if "价格" in profile.interests:
            score += 10
        if "合作" in profile.interests:
            score += 15
        
        if profile.totalMessages > 10:
            score += 10
        elif profile.totalMessages > 5:
            score += 5
        
        return min(score / 100, 1.0)
    
    def _estimateLifetimeValue(self, profile: CustomerProfile) -> float:
        """预估生命周期价值"""
        baseValue = 1000
        
        if profile.intentLevel == "A":
            baseValue *= 3
        elif profile.intentLevel == "B":
            baseValue *= 2
        elif profile.intentLevel == "C":
            baseValue *= 1.5
        
        if "合作" in profile.interests:
            baseValue *= 2
        
        return baseValue
    
    def getProfile(self, customerId: str) -> Optional[CustomerProfile]:
        """获取客户画像"""
        return self._profiles.get(customerId)
    
    def getAllProfiles(self) -> List[CustomerProfile]:
        """获取所有画像"""
        return list(self._profiles.values())


class FunnelAnalyzer:
    """
    转化漏斗分析器
    分析客户转化路径
    """
    
    DEFAULT_STAGES = [
        {"name": "首次接触", "key": "first_contact"},
        {"name": "意向表达", "key": "interest_shown"},
        {"name": "深入咨询", "key": "deep_inquiry"},
        {"name": "购买意向", "key": "purchase_intent"},
        {"name": "成交转化", "key": "converted"}
    ]
    
    def __init__(self, database=None):
        self.database = database
        self._stageDefinitions = list(self.DEFAULT_STAGES)
    
    def analyze(
        self,
        customers: List[CustomerProfile],
        startDate: datetime = None,
        endDate: datetime = None
    ) -> ConversionFunnel:
        """
        分析转化漏斗
        
        Args:
            customers: 客户列表
            startDate: 开始日期
            endDate: 结束日期
        
        Returns:
            转化漏斗
        """
        if startDate and endDate:
            customers = [
                c for c in customers
                if c.firstContactTime
                and startDate <= c.firstContactTime <= endDate
            ]
        
        stageCounts = self._calculateStageCounts(customers)
        
        stages = []
        prevCount = len(customers)
        
        for i, stageDef in enumerate(self._stageDefinitions):
            count = stageCounts.get(stageDef["key"], 0)
            
            if prevCount > 0:
                percentage = (count / len(customers)) * 100
                dropoffRate = ((prevCount - count) / prevCount) * 100 if prevCount > 0 else 0
            else:
                percentage = 0
                dropoffRate = 0
            
            stages.append(FunnelStage(
                name=stageDef["name"],
                count=count,
                percentage=percentage,
                dropoffRate=dropoffRate,
                avgTimeInStage=0
            ))
            
            prevCount = count
        
        overallRate = 0
        if len(customers) > 0 and stages:
            overallRate = (stages[-1].count / len(customers)) * 100
        
        return ConversionFunnel(
            stages=stages,
            totalEntries=len(customers),
            overallConversionRate=overallRate,
            avgTimeToConvert=0
        )
    
    def _calculateStageCounts(self, customers: List[CustomerProfile]) -> Dict[str, int]:
        """计算各阶段数量"""
        counts = defaultdict(int)
        
        for customer in customers:
            counts["first_contact"] += 1
            
            if customer.interests:
                counts["interest_shown"] += 1
            
            if customer.totalMessages > 3 or customer.intentLevel in ["A", "B"]:
                counts["deep_inquiry"] += 1
            
            if "购买" in customer.interests or customer.intentLevel == "A":
                counts["purchase_intent"] += 1
            
            if customer.status == "converted" or customer.conversionProbability > 0.8:
                counts["converted"] += 1
        
        return dict(counts)


class AnalyticsService:
    """
    数据分析服务
    整合客户画像、漏斗分析、行为分析
    """
    
    def __init__(self, database=None):
        self.database = database
        self.profiler = CustomerProfiler(database)
        self.funnelAnalyzer = FunnelAnalyzer(database)
    
    def getDashboardStats(self) -> Dict:
        """获取仪表盘统计"""
        profiles = self.profiler.getAllProfiles()
        
        intentDistribution = defaultdict(int)
        for profile in profiles:
            intentDistribution[profile.intentLevel] += 1
        
        return {
            "totalCustomers": len(profiles),
            "intentDistribution": dict(intentDistribution),
            "highIntentCount": sum(1 for p in profiles if p.intentLevel in ["A", "B"]),
            "avgConversionProbability": sum(p.conversionProbability for p in profiles) / len(profiles) if profiles else 0,
            "avgLifetimeValue": sum(p.lifetimeValue for p in profiles) / len(profiles) if profiles else 0
        }
    
    def getCustomerProfile(self, customerId: str) -> Optional[Dict]:
        """获取客户画像"""
        profile = self.profiler.getProfile(customerId)
        if profile:
            return {
                "customerId": profile.customerId,
                "name": profile.name,
                "platform": profile.platform,
                "intentLevel": profile.intentLevel,
                "intentScore": profile.intentScore,
                "interests": profile.interests,
                "keywords": profile.keywords,
                "tags": profile.tags,
                "conversionProbability": profile.conversionProbability,
                "lifetimeValue": profile.lifetimeValue,
                "totalMessages": profile.totalMessages,
                "firstContactTime": profile.firstContactTime.isoformat() if profile.firstContactTime else None,
                "lastContactTime": profile.lastContactTime.isoformat() if profile.lastContactTime else None
            }
        return None
    
    def getConversionFunnel(
        self,
        startDate: datetime = None,
        endDate: datetime = None
    ) -> Dict:
        """获取转化漏斗"""
        profiles = self.profiler.getAllProfiles()
        funnel = self.funnelAnalyzer.analyze(profiles, startDate, endDate)
        
        return {
            "stages": [
                {
                    "name": stage.name,
                    "count": stage.count,
                    "percentage": stage.percentage,
                    "dropoffRate": stage.dropoffRate
                }
                for stage in funnel.stages
            ],
            "totalEntries": funnel.totalEntries,
            "overallConversionRate": funnel.overallConversionRate
        }
    
    def getInterestDistribution(self) -> Dict:
        """获取兴趣分布"""
        profiles = self.profiler.getAllProfiles()
        
        interestCounts = defaultdict(int)
        for profile in profiles:
            for interest in profile.interests:
                interestCounts[interest] += 1
        
        return dict(interestCounts)
    
    def getTopCustomers(self, limit: int = 10) -> List[Dict]:
        """获取高价值客户"""
        profiles = self.profiler.getAllProfiles()
        
        sortedProfiles = sorted(
            profiles,
            key=lambda x: (x.intentScore, x.conversionProbability),
            reverse=True
        )
        
        return [
            {
                "customerId": p.customerId,
                "name": p.name,
                "intentLevel": p.intentLevel,
                "conversionProbability": p.conversionProbability,
                "lifetimeValue": p.lifetimeValue,
                "tags": p.tags
            }
            for p in sortedProfiles[:limit]
        ]
    
    def getTrendAnalysis(
        self,
        days: int = 7
    ) -> Dict:
        """获取趋势分析"""
        profiles = self.profiler.getAllProfiles()
        
        dailyStats = defaultdict(lambda: {
            "newCustomers": 0,
            "activeCustomers": 0,
            "convertedCustomers": 0
        })
        
        today = datetime.now()
        
        for profile in profiles:
            if profile.firstContactTime:
                daysAgo = (today - profile.firstContactTime).days
                if daysAgo < days:
                    dateKey = profile.firstContactTime.strftime("%Y-%m-%d")
                    dailyStats[dateKey]["newCustomers"] += 1
            
            if profile.lastContactTime:
                daysAgo = (today - profile.lastContactTime).days
                if daysAgo < days:
                    dateKey = profile.lastContactTime.strftime("%Y-%m-%d")
                    dailyStats[dateKey]["activeCustomers"] += 1
            
            if profile.conversionProbability > 0.8:
                if profile.lastContactTime:
                    daysAgo = (today - profile.lastContactTime).days
                    if daysAgo < days:
                        dateKey = profile.lastContactTime.strftime("%Y-%m-%d")
                        dailyStats[dateKey]["convertedCustomers"] += 1
        
        return {
            "period": f"{days}天",
            "dailyStats": dict(dailyStats),
            "totalNewCustomers": sum(s["newCustomers"] for s in dailyStats.values()),
            "totalActiveCustomers": sum(s["activeCustomers"] for s in dailyStats.values()),
            "totalConvertedCustomers": sum(s["convertedCustomers"] for s in dailyStats.values())
        }
