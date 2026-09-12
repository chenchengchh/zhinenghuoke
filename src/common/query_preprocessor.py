"""
高级查询预处理器
实现Multi-Query、HyDE、查询分解、查询路由等优化策略

基于业界最佳实践:
- Multi-Query: 为同一问题生成多个等价改写
- HyDE: 假设文档嵌入
- Query Decomposition: 复杂问题分解
- Query Routing: 查询分类路由
"""

import re
import logging
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import jieba
import jieba.analyse

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """查询类型枚举"""
    SIMPLE = "simple"           # 简单查询
    COMPLEX = "complex"         # 复杂查询
    MULTI_INTENT = "multi_intent"  # 多意图查询
    COMPARISON = "comparison"   # 比较查询
    PROCEDURAL = "procedural"   # 流程查询


@dataclass
class ProcessedQuery:
    """处理后的查询"""
    original: str
    rewritten: List[str]
    hypothetical_doc: str = ""
    sub_queries: List[str] = field(default_factory=list)
    query_type: QueryType = QueryType.SIMPLE
    keywords: List[str] = field(default_factory=list)
    intent: str = ""


class QueryRewriter:
    """
    查询改写器
    
    功能:
    1. 同义词替换
    2. 口语转书面语
    3. Multi-Query生成
    """
    
    SYNONYM_MAP = {
        "多少钱": ["价格", "咋卖", "多少钱", "价位", "收费"],
        "咋样": ["怎么样", "好不好", "如何", "咋样"],
        "能不": ["是否", "能不能", "可以不", "能不"],
        "有没有": ["是否有", "有没有", "存在吗", "有无"],
        "贵": ["价格高", "贵", "贵不贵"],
        "便宜": ["价格低", "便宜", "优惠"],
        "怎么买": ["如何购买", "怎么买", "购买方式", "怎么订购"],
        "如何购买": ["怎么买", "如何购买", "购买方式", "订购方法"],
        "怎么用": ["如何使用", "怎么用", "使用方法", "操作方法"],
        "获客": ["获取客户", "获客", "引流", "拉新"],
        "转化": ["转化率", "转化", "成交", "变现"],
        "添加": ["增加", "添加", "新建", "创建"],
        "新客户": ["新客户", "新用户", "新客", "新会员"],
    }
    
    COLLOQUIAL_PATTERNS = [
        (r"我想问一下", ""),
        (r"请问一下", ""),
        (r"问一下", ""),
        (r"帮忙问一下", ""),
        (r"麻烦问一下", ""),
        (r"能不能", "是否能够"),
        (r"可以不", "是否可以"),
        (r"有吗", "是否有"),
        (r"那个", ""),
        (r"嗯", ""),
        (r"啊", ""),
    ]
    
    FILLER_WORDS = ["那个", "嗯", "啊", "呢", "嘛", "呀", "哈", "咯", "哎", "吧", "哦"]
    
    def rewrite(self, query: str) -> Tuple[str, List[str]]:
        """
        基础查询改写
        
        Args:
            query: 原始查询
            
        Returns:
            (改写后的查询, 提取的关键词)
        """
        rewritten = query
        
        for pattern, replacement in self.COLLOQUIAL_PATTERNS:
            rewritten = re.sub(pattern, replacement, rewritten)
        
        for word in self.FILLER_WORDS:
            rewritten = rewritten.replace(word, "")
        
        rewritten = re.sub(r'\s+', ' ', rewritten).strip()
        
        keywords = jieba.analyse.extract_tags(query, topK=10)
        
        return rewritten, keywords
    
    def rewrite_multi(self, query: str, num_queries: int = 3) -> List[str]:
        """
        Multi-Query生成
        
        为同一问题生成多个等价改写，提升召回覆盖度
        
        Args:
            query: 原始查询
            num_queries: 生成的查询数量
            
        Returns:
            改写后的查询列表
        """
        queries = [query]
        
        rewritten, keywords = self.rewrite(query)
        if rewritten != query:
            queries.append(rewritten)
        
        for word in keywords:
            if word in self.SYNONYM_MAP:
                synonyms = self.SYNONYM_MAP[word]
                for synonym in synonyms[:2]:
                    new_query = query.replace(word, synonym)
                    if new_query not in queries:
                        queries.append(new_query)
        
        keyword_query = " ".join(keywords)
        if keyword_query not in queries and len(keywords) > 1:
            queries.append(keyword_query)
        
        return queries[:num_queries + 1]


class HyDEGenerator:
    """
    HyDE (Hypothetical Document Embeddings) 生成器
    
    核心思想:
    - 让LLM生成与查询相关的假设性文档
    - 使用假设文档的嵌入向量进行检索
    - 解决查询与文档之间的语义鸿沟
    """
    
    HYDE_PROMPT = """请为以下问题生成一个假设性的答案文档。
这个文档应该包含可能回答该问题的相关信息，不需要完全准确，但要相关。

问题: {query}

假设性答案文档:"""
    
    def __init__(self, llm_client=None):
        """
        初始化HyDE生成器
        
        Args:
            llm_client: LLM客户端（可选，如果没有则使用模板生成）
        """
        self.llm_client = llm_client
    
    def generate(self, query: str) -> str:
        """
        生成假设性文档
        
        Args:
            query: 用户查询
            
        Returns:
            假设性文档内容
        """
        if self.llm_client:
            return self._generate_with_llm(query)
        else:
            return self._generate_with_template(query)
    
    def _generate_with_llm(self, query: str) -> str:
        """使用LLM生成假设性文档"""
        try:
            prompt = self.HYDE_PROMPT.format(query=query)
            response = self.llm_client.chat(prompt)
            return response
        except Exception as e:
            logger.warning(f"LLM生成假设文档失败: {e}")
            return self._generate_with_template(query)
    
    def _generate_with_template(self, query: str) -> str:
        """使用模板生成假设性文档"""
        keywords = jieba.analyse.extract_tags(query, topK=5)
        
        template_parts = [
            f"关于{query}的相关信息：",
            f"这个问题涉及到{', '.join(keywords[:3])}等方面。",
            f"通常情况下，{query}需要考虑多种因素。",
            f"相关的内容可能包括：{', '.join(keywords)}。",
        ]
        
        return " ".join(template_parts)


class QueryDecomposer:
    """
    查询分解器
    
    功能:
    1. 识别复杂查询
    2. 将复杂问题分解为子问题
    3. 分别检索后合并结果
    """
    
    DECOMPOSITION_PATTERNS = [
        (r"(.+?)和(.+?)哪个", ["比较{0}的特点", "比较{1}的特点"]),
        (r"(.+?)还是(.+?)", ["分析{0}的优势", "分析{1}的优势"]),
        (r"首先(.+?)然后(.+?)", ["{0}", "{1}"]),
        (r"(.+?)以及(.+?)", ["{0}", "{1}"]),
    ]
    
    def decompose(self, query: str) -> List[str]:
        """
        分解复杂查询
        
        Args:
            query: 原始查询
            
        Returns:
            子查询列表
        """
        sub_queries = [query]
        
        for pattern, templates in self.DECOMPOSITION_PATTERNS:
            match = re.search(pattern, query)
            if match:
                sub_queries = []
                for i, template in enumerate(templates):
                    try:
                        sub_query = template.format(*match.groups())
                        sub_queries.append(sub_query)
                    except (IndexError, KeyError):
                        continue
                if sub_queries:
                    break
        
        if len(sub_queries) == 1 and self._is_complex_query(query):
            sub_queries = self._split_by_keywords(query)
        
        return sub_queries
    
    def _is_complex_query(self, query: str) -> bool:
        """判断是否为复杂查询"""
        complex_indicators = ["和", "以及", "同时", "并且", "还是", "或者", "首先", "然后"]
        return any(indicator in query for indicator in complex_indicators)
    
    def _split_by_keywords(self, query: str) -> List[str]:
        """按关键词分割查询"""
        keywords = jieba.analyse.extract_tags(query, topK=3)
        if len(keywords) >= 2:
            return [f"关于{kw}的信息" for kw in keywords]
        return [query]


class QueryRouter:
    """
    查询路由器
    
    功能:
    1. 查询类型分类
    2. 意图识别
    3. 路由到合适的检索策略
    """
    
    INTENT_KEYWORDS = {
        "price_inquiry": ["价格", "多少钱", "收费", "费用", "贵", "便宜", "优惠", "折扣"],
        "product_info": ["功能", "特点", "介绍", "是什么", "怎么样", "如何"],
        "purchase": ["购买", "订购", "怎么买", "下单", "开通"],
        "support": ["使用", "操作", "怎么用", "教程", "帮助", "问题"],
        "comparison": ["对比", "比较", "哪个好", "区别", "差异"],
        "complaint": ["投诉", "不满", "问题", "退款", "差评"],
    }
    
    def classify(self, query: str) -> QueryType:
        """
        分类查询类型
        
        Args:
            query: 用户查询
            
        Returns:
            查询类型
        """
        if self._is_multi_intent(query):
            return QueryType.MULTI_INTENT
        
        if self._is_comparison(query):
            return QueryType.COMPARISON
        
        if self._is_procedural(query):
            return QueryType.PROCEDURAL
        
        if self._is_complex(query):
            return QueryType.COMPLEX
        
        return QueryType.SIMPLE
    
    def detect_intent(self, query: str) -> str:
        """
        检测查询意图
        
        Args:
            query: 用户查询
            
        Returns:
            意图类型
        """
        query_lower = query.lower()
        
        for intent, keywords in self.INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    return intent
        
        return "general"
    
    def _is_multi_intent(self, query: str) -> bool:
        """判断是否为多意图查询"""
        intent_count = 0
        for keywords in self.INTENT_KEYWORDS.values():
            if any(kw in query for kw in keywords):
                intent_count += 1
        return intent_count >= 2
    
    def _is_comparison(self, query: str) -> bool:
        """判断是否为比较查询"""
        comparison_words = ["对比", "比较", "哪个好", "区别", "差异", "还是"]
        return any(word in query for word in comparison_words)
    
    def _is_procedural(self, query: str) -> bool:
        """判断是否为流程查询"""
        procedural_words = ["怎么", "如何", "步骤", "流程", "方法", "首先", "然后"]
        return any(word in query for word in procedural_words)
    
    def _is_complex(self, query: str) -> bool:
        """判断是否为复杂查询"""
        return len(jieba.lcut(query)) > 10


class AdvancedQueryPreprocessor:
    """
    高级查询预处理器
    
    整合所有查询优化策略:
    1. 查询改写 (Query Rewriting)
    2. 查询分解 (Query Decomposition)
    3. HyDE假设文档生成
    4. 查询路由 (Query Routing)
    """
    
    def __init__(self, llm_client=None):
        """
        初始化查询预处理器
        
        Args:
            llm_client: LLM客户端（可选）
        """
        self.rewriter = QueryRewriter()
        self.hyde_generator = HyDEGenerator(llm_client)
        self.decomposer = QueryDecomposer()
        self.router = QueryRouter()
    
    def process(self, query: str) -> ProcessedQuery:
        """
        处理用户查询
        
        Args:
            query: 用户查询
            
        Returns:
            处理后的查询对象
        """
        query_type = self.router.classify(query)
        
        intent = self.router.detect_intent(query)
        
        rewritten_queries = self.rewriter.rewrite_multi(query)
        
        hypothetical_doc = self.hyde_generator.generate(query)
        
        sub_queries = []
        if query_type in [QueryType.COMPLEX, QueryType.MULTI_INTENT]:
            sub_queries = self.decomposer.decompose(query)
        
        _, keywords = self.rewriter.rewrite(query)
        
        return ProcessedQuery(
            original=query,
            rewritten=rewritten_queries,
            hypothetical_doc=hypothetical_doc,
            sub_queries=sub_queries,
            query_type=query_type,
            keywords=keywords,
            intent=intent
        )


def create_query_preprocessor(llm_client=None) -> AdvancedQueryPreprocessor:
    """创建查询预处理器实例"""
    return AdvancedQueryPreprocessor(llm_client)
