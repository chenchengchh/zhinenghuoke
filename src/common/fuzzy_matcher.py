"""
增强版模糊匹配器
支持同义词扩展、语义理解、部分匹配、意图推断
"""
import re
import jieba
import jieba.analyse
from typing import List, Dict, Tuple, Set, Optional, Any
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass
class MatchResult:
    """匹配结果"""
    score: float
    matchType: str  # exact, synonym, semantic, partial, fuzzy
    matchedKeywords: List[str]
    confidence: float
    reasoning: str


class SynonymDictionary:
    """
    同义词词典
    包含领域相关的同义词映射
    """
    
    # 营销相关同义词
    MARKETING_SYNONYMS = {
        "营销": ["推广", "宣传", "运营", "市场", "营销推广"],
        "活动": ["策划", "方案", "计划", "项目"],
        "策划": ["规划", "设计", "制定", "构思"],
        "推广": ["营销", "宣传", "引流", "获客"],
        "客户": ["用户", "顾客", "消费者", "买家"],
        "获客": ["拉新", "引流", "获客", "获取客户"],
        "转化": ["成交", "变现", "转化率"],
        "直播": ["直播带货", "直播间", "直播卖货"],
        "抖音": ["短视频", "抖音平台", "抖音号"],
        "价格": ["费用", "收费", "多少钱", "价位"],
        "功能": ["特性", "能力", "特点", "有什么用"],
        "使用": ["操作", "用法", "怎么用", "如何使用"],
        "注册": ["开户", "申请账号", "创建账号"],
        "登录": ["登入", "进入系统", "登录账号"],
        "售后": ["服务", "客服", "技术支持", "售后服务"],
        "合作": ["加盟", "代理", "合作模式"],
        "效果": ["结果", "成效", "作用"],
        "教程": ["指南", "文档", "说明", "操作手册"],
    }
    
    # 问题类型同义词
    QUESTION_SYNONYMS = {
        "怎么": ["如何", "怎样", "怎么才能", "怎么操作"],
        "什么": ["哪些", "有什么", "包含什么"],
        "为什么": ["为啥", "原因", "为什么"],
        "能不能": ["可以吗", "是否支持", "是否可以"],
        "多少钱": ["价格", "费用", "收费", "价位"],
        "怎么样": ["如何", "好不好", "效果如何"],
    }
    
    # 意图关键词映射
    INTENT_KEYWORDS = {
        "purchase": ["买", "购买", "下单", "付款", "订购", "定购", "要买", "想买"],
        "inquiry": ["是什么", "有什么", "功能", "特点", "介绍", "详情"],
        "price": ["价格", "费用", "多少钱", "收费", "价位", "成本"],
        "tutorial": ["怎么", "如何", "教程", "操作", "使用", "指南"],
        "service": ["售后", "客服", "服务", "支持", "帮助"],
        "cooperation": ["合作", "代理", "加盟", "经销商", "渠道"],
        "complaint": ["投诉", "不满", "问题", "差评", "退款"],
    }
    
    @classmethod
    def get_synonyms(cls, word: str) -> List[str]:
        """获取词语的同义词"""
        word = word.strip()
        synonyms = [word]
        
        # 检查营销同义词
        if word in cls.MARKETING_SYNONYMS:
            synonyms.extend(cls.MARKETING_SYNONYMS[word])
        
        # 检查问题同义词
        if word in cls.QUESTION_SYNONYMS:
            synonyms.extend(cls.QUESTION_SYNONYMS[word])
        
        # 反向查找
        for key, values in cls.MARKETING_SYNONYMS.items():
            if word in values:
                synonyms.append(key)
        
        for key, values in cls.QUESTION_SYNONYMS.items():
            if word in values:
                synonyms.append(key)
        
        return list(set(synonyms))
    
    @classmethod
    def get_intent(cls, query: str) -> Tuple[str, float]:
        """推断查询意图"""
        query_lower = query.lower()
        
        intent_scores = {}
        for intent, keywords in cls.INTENT_KEYWORDS.items():
            score = 0.0
            for keyword in keywords:
                if keyword in query_lower:
                    score += 1.0
            intent_scores[intent] = score
        
        if intent_scores:
            best_intent = max(intent_scores.keys(), key=lambda x: intent_scores[x])
            confidence = intent_scores[best_intent] / max(sum(intent_scores.values()), 1)
            return best_intent, confidence
        
        return "unknown", 0.0


class FuzzyMatcher:
    """
    模糊匹配器
    支持多种匹配策略
    """
    
    def __init__(self):
        """初始化模糊匹配器"""
        self.synonym_dict = SynonymDictionary()
        
        # 预编译常见问题模式
        self.question_patterns = [
            (r'怎么.{0,5}(活动|营销|策划)', 'activity_planning'),
            (r'如何.{0,5}(活动|营销|策划)', 'activity_planning'),
            (r'(活动|营销).{0,5}策划', 'activity_planning'),
            (r'(价格|费用|多少钱)', 'price_inquiry'),
            (r'(功能|有什么|能做)', 'feature_inquiry'),
            (r'(怎么|如何).{0,5}(用|操作|使用)', 'tutorial_inquiry'),
            (r'(售后|客服|服务)', 'service_inquiry'),
            (r'(合作|代理|加盟)', 'cooperation_inquiry'),
        ]
    
    def extract_keywords(self, text: str) -> Set[str]:
        """
        提取关键词
        
        Args:
            text: 输入文本
            
        Returns:
            关键词集合
        """
        # 使用jieba分词
        words = jieba.lcut(text)
        
        # 过滤停用词和短词
        keywords = set()
        for word in words:
            if len(word) >= 2 and word not in ['怎么', '什么', '如何', '可以', '能够', '是否', '有没有']:
                keywords.add(word)
        
        # 使用TF-IDF提取关键词
        try:
            tfidf_keywords = jieba.analyse.extract_tags(text, topK=5)
            keywords.update(tfidf_keywords)
        except ImportError as e:
            logger.debug(f"jieba未安装: {e}")
        except Exception as e:
            logger.warning(f"TF-IDF关键词提取失败: {e}")
        
        return keywords
    
    def expand_with_synonyms(self, keywords: Set[str]) -> Set[str]:
        """
        使用同义词扩展关键词
        
        Args:
            keywords: 原始关键词
            
        Returns:
            扩展后的关键词
        """
        expanded = set(keywords)
        
        for keyword in keywords:
            synonyms = self.synonym_dict.get_synonyms(keyword)
            expanded.update(synonyms)
        
        return expanded
    
    def calculate_match_score(
        self,
        query: str,
        target: str,
        query_keywords: Set[str] = None,
        target_keywords: Set[str] = None
    ) -> MatchResult:
        """
        计算匹配分数
        
        Args:
            query: 查询文本
            target: 目标文本
            query_keywords: 查询关键词（可选）
            target_keywords: 目标关键词（可选）
            
        Returns:
            MatchResult: 匹配结果
        """
        query_lower = query.lower()
        target_lower = target.lower()
        
        # 提取关键词
        if query_keywords is None:
            query_keywords = self.extract_keywords(query)
        if target_keywords is None:
            target_keywords = self.extract_keywords(target)
        
        # 扩展同义词
        query_expanded = self.expand_with_synonyms(query_keywords)
        target_expanded = self.expand_with_synonyms(target_keywords)
        
        score = 0.0
        match_type = "fuzzy"
        matched_keywords = []
        reasoning_parts = []
        
        # 1. 精确匹配
        if query_lower == target_lower:
            return MatchResult(
                score=100.0,
                matchType="exact",
                matchedKeywords=list(query_keywords),
                confidence=1.0,
                reasoning="完全匹配"
            )
        
        # 2. 包含匹配
        if query_lower in target_lower:
            score += 50.0
            match_type = "partial"
            reasoning_parts.append("查询包含在目标中")
        
        if target_lower in query_lower:
            score += 40.0
            match_type = "partial"
            reasoning_parts.append("目标包含在查询中")
        
        # 3. 关键词匹配
        common_keywords = query_keywords & target_keywords
        if common_keywords:
            keyword_score = len(common_keywords) * 15.0
            score += keyword_score
            matched_keywords.extend(common_keywords)
            if match_type == "fuzzy":
                match_type = "keyword"
            reasoning_parts.append(f"关键词匹配: {common_keywords}")
        
        # 4. 同义词匹配
        synonym_matches = query_expanded & target_expanded
        if synonym_matches:
            synonym_score = len(synonym_matches) * 8.0
            score += synonym_score
            new_matches = synonym_matches - common_keywords
            if new_matches:
                matched_keywords.extend(new_matches)
            if match_type == "fuzzy":
                match_type = "synonym"
            reasoning_parts.append(f"同义词匹配: {synonym_matches}")
        
        # 5. 语义相似度（使用SequenceMatcher）
        try:
            seq_matcher = SequenceMatcher(None, query_lower, target_lower)
            seq_ratio = seq_matcher.ratio()
            if seq_ratio > 0.3:
                semantic_score = seq_ratio * 30.0
                score += semantic_score
                if match_type == "fuzzy":
                    match_type = "semantic"
                reasoning_parts.append(f"语义相似度: {seq_ratio:.2f}")
        except Exception as e:
            logger.debug(f"语义相似度计算失败: {e}")
        
        # 6. 意图匹配
        query_intent, query_conf = self.synonym_dict.get_intent(query)
        target_intent, target_conf = self.synonym_dict.get_intent(target)
        
        if query_intent == target_intent and query_intent != "unknown":
            score += 20.0
            reasoning_parts.append(f"意图匹配: {query_intent}")
        
        # 计算置信度
        confidence = min(score / 100.0, 1.0)
        
        return MatchResult(
            score=score,
            matchType=match_type,
            matchedKeywords=list(set(matched_keywords)),
            confidence=confidence,
            reasoning="; ".join(reasoning_parts) if reasoning_parts else "模糊匹配"
        )
    
    def match_question(
        self,
        query: str,
        questions: List[str],
        top_k: int = 5
    ) -> List[Tuple[int, MatchResult]]:
        """
        匹配问题列表
        
        Args:
            query: 查询文本
            questions: 问题列表
            top_k: 返回数量
            
        Returns:
            List[Tuple[int, MatchResult]]: (索引, 匹配结果) 列表
        """
        results = []
        
        # 预处理查询关键词
        query_keywords = self.extract_keywords(query)
        
        for i, question in enumerate(questions):
            result = self.calculate_match_score(
                query, question,
                query_keywords=query_keywords
            )
            results.append((i, result))
        
        # 按分数排序
        results.sort(key=lambda x: x[1].score, reverse=True)
        
        return results[:top_k]
    
    def detect_intent_pattern(self, query: str) -> Tuple[str, float]:
        """
        检测问题模式
        
        Args:
            query: 查询文本
            
        Returns:
            Tuple[str, float]: (模式类型, 置信度)
        """
        for pattern, intent_type in self.question_patterns:
            if re.search(pattern, query):
                return intent_type, 0.8
        
        return "general", 0.5


class EnhancedKnowledgeSearcher:
    """
    增强版知识搜索器
    结合模糊匹配和知识库搜索
    """
    
    def __init__(self, knowledge_base):
        """
        初始化搜索器
        
        Args:
            knowledge_base: 知识库实例
        """
        self.knowledge_base = knowledge_base
        self.fuzzy_matcher = FuzzyMatcher()

    def _list_knowledge_items(self) -> List[Any]:
        if not self.knowledge_base:
            return []
        if hasattr(self.knowledge_base, "list_knowledge_items"):
            return list(self.knowledge_base.list_knowledge_items() or [])
        if hasattr(self.knowledge_base, "get_knowledge_list"):
            return list(self.knowledge_base.get_knowledge_list() or [])
        if hasattr(self.knowledge_base, "get_all_items"):
            return list(self.knowledge_base.get_all_items() or [])
        return list(getattr(self.knowledge_base, "knowledge_items", []) or [])
    
    def search(
        self,
        query: str,
        top_k: int = 5,
        use_fuzzy: bool = True,
        min_score: float = 10.0
    ) -> List[Tuple[Any, float, MatchResult]]:
        """
        增强版搜索
        
        Args:
            query: 查询文本
            top_k: 返回数量
            use_fuzzy: 是否使用模糊匹配
            min_score: 最低分数阈值
            
        Returns:
            List[Tuple[KnowledgeItem, float, MatchResult]]: 搜索结果
        """
        results = []
        
        # 1. 传统关键词搜索
        kb_results = self.knowledge_base.search(query, top_k=top_k*2)
        
        # 2. 模糊匹配增强
        if use_fuzzy:
            query_keywords = self.fuzzy_matcher.extract_keywords(query)
            
            for item, base_score in kb_results:
                # 计算模糊匹配分数
                match_result = self.fuzzy_matcher.calculate_match_score(
                    query, item.question,
                    query_keywords=query_keywords
                )
                
                # 综合分数
                combined_score = base_score + match_result.score
                
                if combined_score >= min_score:
                    results.append((item, combined_score, match_result))
        
        # 3. 检查别名匹配
        for item in self._list_knowledge_items():
            if not item.enabled:
                continue
            
            for alias in item.aliases:
                match_result = self.fuzzy_matcher.calculate_match_score(query, alias)
                if match_result.score >= min_score:
                    # 检查是否已存在
                    existing_ids = [r[0].id for r in results]
                    if item.id not in existing_ids:
                        combined_score = match_result.score
                        results.append((item, combined_score, match_result))
        
        # 4. 排序并返回
        results.sort(key=lambda x: x[1], reverse=True)
        
        return results[:top_k]


def get_fuzzy_matcher() -> FuzzyMatcher:
    """获取模糊匹配器单例"""
    global _fuzzy_matcher
    if '_fuzzy_matcher' not in globals():
        _fuzzy_matcher = FuzzyMatcher()
    return _fuzzy_matcher
