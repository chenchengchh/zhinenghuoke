"""
增强版否定句和条件句识别模块

功能：
1. 否定句检测：识别"不"、"没"、"别"等否定词
2. 条件句检测：识别"如果"、"假如"、"要是"等条件词
3. 否定范围识别：确定否定词的作用范围
4. 条件逻辑识别：提取条件内容和结果
"""

import re
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class NegationInfo:
    """否定信息"""
    has_negation: bool
    negation_words: List[str] = field(default_factory=list)
    negation_scope: List[str] = field(default_factory=list)
    negation_type: str = ""  # full: 全句否定，partial: 部分否定，double: 双重否定


@dataclass
class ConditionalInfo:
    """条件信息"""
    has_condition: bool
    conditional_words: List[str] = field(default_factory=list)
    condition_content: str = ""
    result_content: str = ""
    condition_type: str = ""  # sufficient: 充分条件，necessary: 必要条件


class NegationDetector:
    """
    否定句检测器
    
    识别否定词、否定范围和否定类型
    """
    
    # 否定词列表
    NEGATION_WORDS = [
        '不', '没', '没有', '别', '勿', '莫', '非', '无',
        '不要', '不能', '不可', '无法', '没法', '不许',
        '不是', '没有', '未曾', '尚未', '还未'
    ]
    
    # 否定强度词
    NEGATION_INTENSITY = {
        '不': 0.8, '没': 0.9, '没有': 1.0,
        '别': 0.7, '勿': 0.6, '莫': 0.6,
        '不要': 0.8, '不能': 0.9, '不可': 0.9
    }
    
    # 双重否定模式
    DOUBLE_NEGATION_PATTERNS = [
        r'不.*不.*',
        r'没.*没.*',
        r'非.*不可',
        r'不得不',
        r'不能不',
        r'不会不',
    ]
    
    def __init__(self):
        self._compile_patterns()
    
    def _compile_patterns(self):
        """编译正则模式"""
        self.negation_pattern = re.compile(
            r'(' + '|'.join(re.escape(w) for w in self.NEGATION_WORDS) + r')'
        )
        
        self.double_neg_patterns = [
            re.compile(p) for p in self.DOUBLE_NEGATION_PATTERNS
        ]
    
    def detect(self, text: str) -> NegationInfo:
        """
        检测否定句
        
        Args:
            text: 输入文本
            
        Returns:
            NegationInfo 对象
        """
        # 查找否定词
        matches = self.negation_pattern.findall(text)
        has_negation = len(matches) > 0
        
        if not has_negation:
            return NegationInfo(has_negation=False)
        
        # 判断否定类型
        negation_type = self._judge_negation_type(text, matches)
        
        # 提取否定范围
        negation_scope = self._extract_negation_scope(text, matches)
        
        return NegationInfo(
            has_negation=True,
            negation_words=list(set(matches)),
            negation_scope=negation_scope,
            negation_type=negation_type
        )
    
    def _judge_negation_type(self, text: str, negations: List[str]) -> str:
        """
        判断否定类型
        
        Args:
            text: 输入文本
            negations: 否定词列表
            
        Returns:
            否定类型
        """
        # 检查双重否定
        for pattern in self.double_neg_patterns:
            if pattern.search(text):
                return 'double'
        
        # 检查是否全句否定
        if len(negations) >= 2:
            return 'full'
        
        # 部分否定
        return 'partial'
    
    def _extract_negation_scope(self, text: str, negations: List[str]) -> List[str]:
        """
        提取否定范围
        
        策略：否定词后的 5-10 个字或到下一个标点
        
        Args:
            text: 输入文本
            negations: 否定词列表
            
        Returns:
            否定范围列表
        """
        scopes = []
        
        for neg in negations:
            neg_pos = text.find(neg)
            if neg_pos >= 0:
                # 提取否定词后的内容
                scope_end = min(neg_pos + 15, len(text))
                
                # 查找下一个标点
                for i in range(neg_pos + 1, min(neg_pos + 15, len(text))):
                    if text[i] in '，。！？；：':
                        scope_end = i
                        break
                
                scope = text[neg_pos:scope_end].strip()
                if scope and len(scope) > 1:
                    scopes.append(scope)
        
        return scopes
    
    def get_negation_intensity(self, negation_word: str) -> float:
        """获取否定强度"""
        return self.NEGATION_INTENSITY.get(negation_word, 0.7)


class ConditionalClauseDetector:
    """
    条件句检测器
    
    识别条件词、提取条件内容和结果
    """
    
    # 条件词列表
    CONDITIONAL_WORDS = [
        '如果', '假如', '要是', '倘若', '若', '如若',
        '只要', '只有', '除非', '无论', '不论', '不管',
        '一旦', '假使', '假若', '如使', '若使'
    ]
    
    # 条件类型
    SUFFICIENT_CONDITIONS = ['如果', '假如', '要是', '倘若', '若', '一旦', '假使']
    NECESSARY_CONDITIONS = ['只要', '只有', '除非']
    UNIVERSAL_CONDITIONS = ['无论', '不论', '不管']
    
    # 结果标记词
    RESULT_MARKERS = ['就', '那么', '则', '便', '才', '都', '也']
    
    def __init__(self):
        self._compile_patterns()
    
    def _compile_patterns(self):
        """编译正则模式"""
        self.conditional_pattern = re.compile(
            r'(' + '|'.join(re.escape(w) for w in self.CONDITIONAL_WORDS) + r')'
        )
        
        # 条件 - 结果模式
        self.cond_result_pattern = re.compile(
            r'(?:' + '|'.join(re.escape(w) for w in self.CONDITIONAL_WORDS) + r')(.+?)(?:[，,]|$)'
        )
        
        # 结果模式
        self.result_pattern = re.compile(
            r'(?:[，,]|$)(?:.*?)(?:' + '|'.join(re.escape(w) for w in self.RESULT_MARKERS) + r')(.+?)(?:[。！？]|$)'
        )
    
    def detect(self, text: str) -> ConditionalInfo:
        """
        检测条件句
        
        Args:
            text: 输入文本
            
        Returns:
            ConditionalInfo 对象
        """
        # 查找条件词
        matches = self.conditional_pattern.findall(text)
        has_condition = len(matches) > 0
        
        if not has_condition:
            return ConditionalInfo(has_condition=False)
        
        # 判断条件类型
        condition_type = self._judge_condition_type(matches)
        
        # 提取条件内容和结果
        condition_content, result_content = self._extract_condition_result(text)
        
        return ConditionalInfo(
            has_condition=True,
            conditional_words=list(set(matches)),
            condition_content=condition_content,
            result_content=result_content,
            condition_type=condition_type
        )
    
    def _judge_condition_type(self, condition_words: List[str]) -> str:
        """
        判断条件类型
        
        Args:
            condition_words: 条件词列表
            
        Returns:
            条件类型
        """
        for word in condition_words:
            if word in self.SUFFICIENT_CONDITIONS:
                return 'sufficient'
            elif word in self.NECESSARY_CONDITIONS:
                return 'necessary'
            elif word in self.UNIVERSAL_CONDITIONS:
                return 'universal'
        
        return 'sufficient'  # 默认
    
    def _extract_condition_result(self, text: str) -> Tuple[str, str]:
        """
        提取条件内容和结果
        
        Args:
            text: 输入文本
            
        Returns:
            (条件内容，结果内容)
        """
        # 尝试匹配条件 - 结果模式
        cond_match = self.cond_result_pattern.search(text)
        result_match = self.result_pattern.search(text)
        
        condition_content = ""
        result_content = ""
        
        if cond_match:
            condition_content = cond_match.group(1).strip()
        
        if result_match:
            result_content = result_match.group(1).strip()
        
        # 如果没有明确的结果标记，尝试简单分割
        if not result_content and condition_content:
            for word in self.CONDITIONAL_WORDS:
                if word in text:
                    parts = text.split(word, 1)
                    if len(parts) == 2:
                        condition_content = parts[1].split('，')[0].split(',')[0]
                        result_content = parts[1].split('，')[-1] if '，' in parts[1] else ""
                        break
        
        return condition_content, result_content


class EnhancedLinguisticAnalyzer:
    """
    增强语言分析器
    
    整合否定句和条件句检测，提供综合语言分析
    """
    
    def __init__(self):
        self.negation_detector = NegationDetector()
        self.conditional_detector = ConditionalClauseDetector()
        logger.info("增强语言分析器初始化完成")
    
    def analyze(self, text: str) -> Dict:
        """
        综合分析文本
        
        Args:
            text: 输入文本
            
        Returns:
            分析结果字典
        """
        negation_info = self.negation_detector.detect(text)
        conditional_info = self.conditional_detector.detect(text)
        
        analysis = {
            'text': text,
            'has_negation': negation_info.has_negation,
            'negation_details': {
                'words': negation_info.negation_words,
                'type': negation_info.negation_type,
                'scope': negation_info.negation_scope,
                'intensity': max([
                    self.negation_detector.get_negation_intensity(w)
                    for w in negation_info.negation_words
                ], default=0.0)
            } if negation_info.has_negation else None,
            'has_condition': conditional_info.has_condition,
            'condition_details': {
                'words': conditional_info.conditional_words,
                'type': conditional_info.condition_type,
                'condition': conditional_info.condition_content,
                'result': conditional_info.result_content
            } if conditional_info.has_condition else None,
            'complexity_score': self._calculate_complexity(
                negation_info, conditional_info
            )
        }
        
        return analysis
    
    def _calculate_complexity(
        self,
        negation: NegationInfo,
        condition: ConditionalInfo
    ) -> float:
        """
        计算句子复杂度
        
        Args:
            negation: 否定信息
            condition: 条件信息
            
        Returns:
            复杂度分数 (0-1)
        """
        score = 0.0
        
        # 否定增加复杂度
        if negation.has_negation:
            score += 0.2
            if negation.negation_type == 'double':
                score += 0.2
            elif negation.negation_type == 'full':
                score += 0.1
        
        # 条件增加复杂度
        if condition.has_condition:
            score += 0.2
            if condition.condition_type == 'necessary':
                score += 0.1
        
        return min(score, 1.0)
    
    def should_enhance_query(self, analysis: Dict) -> bool:
        """
        判断是否需要增强查询
        
        Args:
            analysis: 分析结果
            
        Returns:
            是否需要增强
        """
        # 如果有否定或条件，需要增强
        if analysis.get('has_negation') or analysis.get('has_condition'):
            return True
        
        # 如果复杂度高，需要增强
        if analysis.get('complexity_score', 0) > 0.3:
            return True
        
        return False
    
    def enhance_query_for_retrieval(
        self,
        query: str,
        analysis: Dict
    ) -> Tuple[str, List[str]]:
        """
        为检索增强查询
        
        Args:
            query: 原始查询
            analysis: 分析结果
            
        Returns:
            (增强后的查询，关键词列表)
        """
        enhanced_parts = [query]
        keywords = []
        
        # 处理否定
        if analysis.get('has_negation'):
            neg_details = analysis['negation_details']
            # 移除否定词，保留核心内容
            clean_query = query
            for neg_word in neg_details['words']:
                clean_query = clean_query.replace(neg_word, '')
            enhanced_parts.append(f"不包含{clean_query.strip()}")
            keywords.extend(neg_details['words'])
        
        # 处理条件
        if analysis.get('has_condition'):
            cond_details = analysis['condition_details']
            if cond_details.get('condition'):
                enhanced_parts.append(f"条件：{cond_details['condition']}")
            if cond_details.get('result'):
                enhanced_parts.append(f"结果：{cond_details['result']}")
            keywords.extend(cond_details['words'])
        
        enhanced_query = ' '.join(enhanced_parts)
        return enhanced_query, list(set(keywords))


# 工厂函数
def create_linguistic_analyzer() -> EnhancedLinguisticAnalyzer:
    """创建语言分析器"""
    return EnhancedLinguisticAnalyzer()
