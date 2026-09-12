"""
Self-Query 查询自反思模块

功能：
1. 从用户查询中自动提取元数据过滤条件
2. 智能识别查询意图和分类
3. 提取时间、价格、数量等结构化信息
4. 生成优化后的查询和过滤条件

基于业界最佳实践：
- Query Analysis: 查询分析和理解
- Metadata Extraction: 元数据提取
- Intent Classification: 意图分类
"""

import re
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import jieba
import jieba.analyse

logger = logging.getLogger(__name__)


class QueryIntent(Enum):
    """查询意图类型"""
    INFORMATION = "information"
    COMPARISON = "comparison"
    PROCEDURE = "procedure"
    PRICE = "price"
    TROUBLESHOOTING = "troubleshooting"
    RECOMMENDATION = "recommendation"
    UNKNOWN = "unknown"


@dataclass
class SelfQueryResult:
    """Self-Query结果"""
    original_query: str
    refined_query: str
    filters: Dict[str, Any] = field(default_factory=dict)
    intent: QueryIntent = QueryIntent.UNKNOWN
    confidence: float = 0.0
    extracted_entities: Dict[str, Any] = field(default_factory=dict)
    suggested_categories: List[str] = field(default_factory=list)


class SelfQueryReflector:
    """
    Self-Query查询自反思器
    
    从用户查询中提取结构化信息，生成过滤条件
    """
    
    CATEGORY_KEYWORDS = {
        "product": ["产品", "功能", "特性", "介绍", "特点", "规格", "型号", "版本"],
        "price": ["价格", "多少钱", "费用", "收费", "成本", "价位", "贵", "便宜", "优惠", "折扣"],
        "service": ["服务", "支持", "客服", "帮助", "咨询", "售后"],
        "policy": ["政策", "规定", "规则", "条款", "协议", "制度"],
        "faq": ["怎么", "如何", "为什么", "什么", "能不能", "是否", "常见问题"],
        "promotion": ["活动", "促销", "优惠", "折扣", "福利", "赠品"],
        "after_sale": ["售后", "退换", "维修", "保修", "投诉", "维权"],
        "company": ["公司", "企业", "关于我们", "介绍", "团队", "文化"],
        "cooperation": ["合作", "加盟", "代理", "伙伴", "商务"],
        "tech": ["技术", "开发", "接口", "API", "集成", "文档"]
    }
    
    INTENT_PATTERNS = {
        QueryIntent.INFORMATION: [
            r"是什么", r"介绍", r"说明", r"解释", r"定义"
        ],
        QueryIntent.COMPARISON: [
            r"对比", r"比较", r"区别", r"哪个好", r"还是", r"vs", r"VS"
        ],
        QueryIntent.PROCEDURE: [
            r"怎么", r"如何", r"步骤", r"流程", r"方法", r"操作"
        ],
        QueryIntent.PRICE: [
            r"多少钱", r"价格", r"费用", r"收费", r"贵不贵"
        ],
        QueryIntent.TROUBLESHOOTING: [
            r"问题", r"错误", r"失败", r"不行", r"无法", r"异常", r"故障"
        ],
        QueryIntent.RECOMMENDATION: [
            r"推荐", r"建议", r"哪个好", r"选择", r"适合"
        ]
    }
    
    ENTITY_PATTERNS = {
        "price_range": r"(\d+)[-~至到](\d+)[元块]",
        "quantity": r"(\d+)[个件条项]",
        "time": r"(\d+)[天日月年]",
        "phone": r"1[3-9]\d{9}",
        "email": r"[\w.-]+@[\w.-]+\.\w+"
    }
    
    FILLER_WORDS = [
        "那个", "嗯", "啊", "呢", "嘛", "呀", "哈", "咯", "哎", "吧",
        "请问", "麻烦", "帮忙", "我想问", "问一下", "帮我"
    ]
    
    def __init__(self):
        self._init_jieba()
    
    def _init_jieba(self):
        """初始化jieba分词"""
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                jieba.add_word(keyword)
    
    def reflect(self, query: str) -> SelfQueryResult:
        """
        执行查询自反思
        
        Args:
            query: 原始查询
            
        Returns:
            SelfQueryResult: 反思结果
        """
        result = SelfQueryResult(original_query=query)
        
        result.intent = self._classify_intent(query)
        result.confidence = self._calculate_confidence(query, result.intent)
        
        result.filters = self._extract_filters(query)
        
        result.refined_query = self._refine_query(query)
        
        result.extracted_entities = self._extract_entities(query)
        
        result.suggested_categories = self._suggest_categories(query)
        
        return result
    
    def _classify_intent(self, query: str) -> QueryIntent:
        """分类查询意图"""
        scores = {}
        
        for intent, patterns in self.INTENT_PATTERNS.items():
            score = 0
            for pattern in patterns:
                if re.search(pattern, query):
                    score += 1
            scores[intent] = score
        
        if not scores or max(scores.values()) == 0:
            return QueryIntent.UNKNOWN
        
        return max(scores, key=scores.get)
    
    def _calculate_confidence(self, query: str, intent: QueryIntent) -> float:
        """计算意图分类置信度"""
        if intent == QueryIntent.UNKNOWN:
            return 0.0
        
        patterns = self.INTENT_PATTERNS.get(intent, [])
        matches = sum(1 for p in patterns if re.search(p, query))
        
        return min(matches / len(patterns), 1.0) if patterns else 0.0
    
    def _extract_filters(self, query: str) -> Dict[str, Any]:
        """提取过滤条件"""
        filters = {}
        
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query:
                    filters["category"] = category
                    break
            if "category" in filters:
                break
        
        if re.search(r"启用|开启|有效", query):
            filters["enabled"] = True
        elif re.search(r"禁用|关闭|无效", query):
            filters["enabled"] = False
        
        price_match = re.search(r"(\d+)[-~至到](\d+)[元块]?", query)
        if price_match:
            filters["price_min"] = int(price_match.group(1))
            filters["price_max"] = int(price_match.group(2))
        
        return filters
    
    def _refine_query(self, query: str) -> str:
        """优化查询"""
        refined = query
        
        for filler in self.FILLER_WORDS:
            refined = refined.replace(filler, "")
        
        refined = re.sub(r'\s+', ' ', refined).strip()
        
        refined = re.sub(r'[？?！!。.，,]+$', '', refined)
        
        return refined
    
    def _extract_entities(self, query: str) -> Dict[str, Any]:
        """提取实体"""
        entities = {}
        
        for entity_type, pattern in self.ENTITY_PATTERNS.items():
            matches = re.findall(pattern, query)
            if matches:
                entities[entity_type] = matches
        
        keywords = jieba.analyse.extract_tags(query, topK=5)
        if keywords:
            entities["keywords"] = keywords
        
        return entities
    
    def _suggest_categories(self, query: str) -> List[str]:
        """建议分类"""
        scores = {}
        
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in query)
            if score > 0:
                scores[category] = score
        
        sorted_categories = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        return [cat for cat, _ in sorted_categories[:3]]


def create_self_query_reflector() -> SelfQueryReflector:
    """创建Self-Query反思器实例"""
    return SelfQueryReflector()


def self_query(query: str) -> Tuple[str, Dict[str, Any]]:
    """
    便捷函数：执行Self-Query
    
    Args:
        query: 原始查询
        
    Returns:
        Tuple[str, Dict]: (优化后的查询, 过滤条件)
    """
    reflector = create_self_query_reflector()
    result = reflector.reflect(query)
    return result.refined_query, result.filters
