"""
LLM增强知识抽取器
使用大语言模型从对话和文本中提取结构化知识
"""
import re
import json
import hashlib
import inspect
from typing import List, Dict, Tuple, Any, Optional
from datetime import datetime
from loguru import logger

from .types.knowledge import ExtractedKnowledge


class LLMKnowledgeExtractor:
    """
    LLM增强知识抽取器
    
    使用大语言模型从对话和文本中提取结构化知识
    """
    
    # 知识抽取提示模板
    EXTRACTION_PROMPT = """你是一个专业的知识抽取助手。请从以下内容中提取结构化知识。

内容:
{content}

请按以下格式输出JSON:
{{
    "question": "标准化的用户问题（简洁明确）",
    "answer": "完整准确的答案",
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "tags": ["标签1", "标签2"],
    "category": "分类（价格/产品/服务/合作/其他）",
    "confidence": 0.0-1.0之间的置信度,
    "reasoning": "抽取理由简述"
}}

要求:
1. 问题要简洁明确，便于用户搜索
2. 答案要完整准确，包含所有必要信息
3. 关键词提取3-5个核心词
4. 标签用于分类管理
5. 置信度反映知识的可靠性
"""

    CONVERSATION_EXTRACTION_PROMPT = """你是一个专业的对话分析助手。请从以下对话中提取有价值的知识。

对话记录:
{conversation}

请分析对话并提取:
1. 用户的核心问题
2. 最佳答案
3. 相关关键词
4. 问题分类

输出JSON格式:
{{
    "extractions": [
        {{
            "question": "标准化问题",
            "answer": "完整答案",
            "keywords": ["关键词"],
            "tags": ["标签"],
            "category": "分类",
            "confidence": 置信度,
            "context": "对话上下文简述"
        }}
    ],
    "dialogue_quality": "good/medium/poor",
    "learning_value": "high/medium/low"
}}
"""

    def __init__(self, llm_service=None):
        """
        初始化LLM知识抽取器
        
        Args:
            llm_service: LLM服务实例
        """
        self.llm_service = llm_service
        self._extraction_cache: Dict[str, ExtractedKnowledge] = {}
        
        # 分类映射
        self.category_mapping = {
            "价格": ["价格", "费用", "多少钱", "收费", "套餐", "优惠"],
            "产品": ["功能", "产品", "系统", "特性", "能力"],
            "服务": ["售后", "客服", "服务", "支持", "帮助"],
            "合作": ["合作", "代理", "加盟", "经销商"],
            "操作": ["怎么", "如何", "操作", "使用", "教程"],
            "其他": []
        }
    
    def _get_llm_service(self):
        """获取LLM服务"""
        if self.llm_service:
            return self.llm_service
        
        try:
            from src.common.llm_service import get_default_llm_provider
            self.llm_service = get_default_llm_provider()
            return self.llm_service
        except Exception as e:
            logger.warning(f"获取LLM服务失败: {e}")
            return None
    
    async def extract_from_conversation(
        self,
        conversation: List[Dict],
        context: Dict = None
    ) -> List[ExtractedKnowledge]:
        """
        从对话中提取知识
        
        Args:
            conversation: 对话记录列表
            context: 额外上下文
            
        Returns:
            提取的知识列表
        """
        if not conversation:
            return []
        
        # 格式化对话内容
        conversation_text = self._format_conversation(conversation)
        
        # 尝试使用LLM提取
        llm = self._get_llm_service()
        if llm:
            try:
                return await self._extract_with_llm(
                    conversation_text,
                    self.CONVERSATION_EXTRACTION_PROMPT
                )
            except Exception as e:
                logger.warning(f"LLM提取失败，使用规则提取: {e}")
        
        # 回退到规则提取
        return self._extract_with_rules(conversation)
    
    async def extract_from_text(
        self,
        text: str,
        context: Dict = None
    ) -> List[ExtractedKnowledge]:
        """
        从文本中提取知识
        
        Args:
            text: 文本内容
            context: 额外上下文
            
        Returns:
            提取的知识列表
        """
        if not text or len(text.strip()) < 10:
            return []
        
        # 尝试使用LLM提取
        llm = self._get_llm_service()
        if llm:
            try:
                return await self._extract_with_llm(
                    text,
                    self.EXTRACTION_PROMPT
                )
            except Exception as e:
                logger.warning(f"LLM提取失败，使用规则提取: {e}")
        
        # 回退到规则提取
        return self._extract_text_with_rules(text)
    
    async def _extract_with_llm(
        self,
        content: str,
        prompt_template: str
    ) -> List[ExtractedKnowledge]:
        """
        使用LLM提取知识
        
        Args:
            content: 内容
            prompt_template: 提示模板
            
        Returns:
            提取的知识列表
        """
        llm = self._get_llm_service()
        if not llm:
            return []
        
        prompt = prompt_template.format(content=content)
        
        try:
            # 调用LLM
            response = llm.generate(prompt)
            if inspect.isawaitable(response):
                response = await response
            
            # 解析响应
            return self._parse_llm_response(response)
        except Exception as e:
            logger.error(f"LLM调用失败: {e}")
            return []
    
    def _parse_llm_response(self, response: str) -> List[ExtractedKnowledge]:
        """
        解析LLM响应
        
        Args:
            response: LLM响应文本
            
        Returns:
            知识列表
        """
        results = []
        
        try:
            # 尝试提取JSON
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                data = json.loads(json_match.group())
                
                # 处理单个提取结果
                if 'question' in data:
                    knowledge = self._create_knowledge_from_dict(data)
                    if knowledge:
                        results.append(knowledge)
                
                # 处理多个提取结果
                elif 'extractions' in data:
                    for item in data['extractions']:
                        knowledge = self._create_knowledge_from_dict(item)
                        if knowledge:
                            results.append(knowledge)
                            
        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败: {e}")
            # 尝试使用正则提取
            results = self._extract_with_regex(response)
        except Exception as e:
            logger.error(f"解析LLM响应失败: {e}")
        
        return results
    
    def _create_knowledge_from_dict(self, data: Dict) -> Optional[ExtractedKnowledge]:
        """
        从字典创建知识对象
        
        Args:
            data: 知识数据字典
            
        Returns:
            知识对象或None
        """
        question = data.get('question', '').strip()
        answer = data.get('answer', '').strip()
        
        if len(question) < 5 or len(answer) < 10:
            return None
        
        return ExtractedKnowledge(
            question=self._normalize_question(question),
            answer=self._normalize_answer(answer),
            keywords=data.get('keywords', [])[:5],
            tags=data.get('tags', [])[:3],
            category=data.get('category', '其他'),
            confidence=float(data.get('confidence', 0.8)),
            quality_score=self._calculate_quality_score(question, answer)
        )
    
    def _calculate_quality_score(self, question: str, answer: str) -> float:
        """
        计算知识质量分数
        
        Args:
            question: 问题
            answer: 答案
            
        Returns:
            质量分数 (0-1)
        """
        score = 0.0
        
        # 问题完整性 (0.3)
        if len(question) >= 5:
            score += 0.1
        if len(question) >= 10:
            score += 0.1
        if any(word in question for word in ['怎么', '如何', '什么', '为什么']):
            score += 0.1
        
        # 答案完整性 (0.4)
        if len(answer) >= 10:
            score += 0.1
        if len(answer) >= 30:
            score += 0.1
        if len(answer) >= 50:
            score += 0.1
        if '。' in answer or '！' in answer:
            score += 0.1
        
        # 内容相关性 (0.3)
        if any(word in question + answer for word in ['功能', '价格', '服务', '产品', '系统']):
            score += 0.15
        if any(word in question + answer for word in ['营销', '推广', '客户', '用户']):
            score += 0.15
        
        return min(score, 1.0)
    
    def _extract_with_rules(self, conversation: List[Dict]) -> List[ExtractedKnowledge]:
        """
        使用规则从对话中提取知识
        
        Args:
            conversation: 对话记录
            
        Returns:
            知识列表
        """
        results = []
        
        # 提取问答对
        qa_pairs = self._extract_qa_pairs(conversation)
        
        for question, answer in qa_pairs:
            if len(question) >= 5 and len(answer) >= 10:
                knowledge = ExtractedKnowledge(
                    question=self._normalize_question(question),
                    answer=self._normalize_answer(answer),
                    keywords=self._extract_keywords(question + " " + answer),
                    tags=self._extract_tags(question + " " + answer),
                    category=self._classify_question(question),
                    confidence=0.6,
                    quality_score=0.5
                )
                results.append(knowledge)
        
        return results
    
    def _extract_text_with_rules(self, text: str) -> List[ExtractedKnowledge]:
        """
        使用规则从文本中提取知识
        
        Args:
            text: 文本内容
            
        Returns:
            知识列表
        """
        results = []
        
        # 尝试识别问答格式
        qa_pattern = r'问[题]?[:：]\s*(.+?)\s*答[案]?[:：]\s*(.+?)(?=问|$)'
        matches = re.findall(qa_pattern, text, re.DOTALL)
        
        for question, answer in matches:
            question = question.strip()
            answer = answer.strip()
            
            if len(question) >= 5 and len(answer) >= 10:
                knowledge = ExtractedKnowledge(
                    question=self._normalize_question(question),
                    answer=self._normalize_answer(answer),
                    keywords=self._extract_keywords(question + " " + answer),
                    tags=self._extract_tags(question + " " + answer),
                    category=self._classify_question(question),
                    confidence=0.7,
                    quality_score=0.6
                )
                results.append(knowledge)
        
        return results
    
    def _extract_with_regex(self, text: str) -> List[ExtractedKnowledge]:
        """
        使用正则从文本中提取
        
        Args:
            text: 文本内容
            
        Returns:
            知识列表
        """
        results = []
        
        # 提取问题和答案
        question_pattern = r'问[题]?[:：]\s*(.+?)(?=\n|$)'
        answer_pattern = r'答[案]?[:：]\s*(.+?)(?=\n|$)'
        
        questions = re.findall(question_pattern, text)
        answers = re.findall(answer_pattern, text)
        
        for i, question in enumerate(questions):
            if i < len(answers):
                answer = answers[i]
                if len(question) >= 5 and len(answer) >= 10:
                    knowledge = ExtractedKnowledge(
                        question=question.strip(),
                        answer=answer.strip(),
                        keywords=self._extract_keywords(question + " " + answer),
                        confidence=0.5
                    )
                    results.append(knowledge)
        
        return results
    
    def _format_conversation(self, conversation: List[Dict]) -> str:
        """格式化对话记录"""
        lines = []
        for msg in conversation:
            role = msg.get('role', msg.get('direction', 'unknown'))
            content = msg.get('content', msg.get('message', ''))
            
            if role in ['user', 'inbound']:
                lines.append(f"用户: {content}")
            elif role in ['assistant', 'outbound', 'bot']:
                lines.append(f"客服: {content}")
            else:
                lines.append(f"{role}: {content}")
        
        return "\n".join(lines)
    
    def _extract_qa_pairs(self, conversation: List[Dict]) -> List[Tuple[str, str]]:
        """提取问答对"""
        pairs = []
        
        user_msg = None
        for msg in conversation:
            role = msg.get('role', msg.get('direction', ''))
            content = msg.get('content', msg.get('message', ''))
            
            if role in ['user', 'inbound']:
                user_msg = content
            elif role in ['assistant', 'outbound', 'bot'] and user_msg:
                pairs.append((user_msg, content))
                user_msg = None
        
        return pairs
    
    def _normalize_question(self, question: str) -> str:
        """标准化问题"""
        # 移除多余空格
        question = ' '.join(question.split())
        
        # 确保以问号结尾
        if not question.endswith('？') and not question.endswith('?'):
            if any(word in question for word in ['怎么', '如何', '什么', '为什么', '哪', '吗']):
                question += '？'
        
        return question.strip()
    
    def _normalize_answer(self, answer: str) -> str:
        """标准化答案"""
        # 移除多余空格
        answer = ' '.join(answer.split())
        
        # 确保以句号结尾
        if not answer.endswith('。') and not answer.endswith('！') and not answer.endswith('.'):
            answer += '。'
        
        return answer.strip()
    
    def _extract_keywords(self, text: str) -> List[str]:
        """提取关键词"""
        keywords = []
        
        # 使用jieba分词
        try:
            import jieba.analyse
            keywords = jieba.analyse.extract_tags(text, topK=5)
        except ImportError:
            logger.debug("jieba未安装，使用简单关键词提取")
            important_words = [
                '价格', '费用', '功能', '产品', '系统', '服务', '售后',
                '营销', '推广', '客户', '用户', '合作', '代理', '直播',
                '抖音', '获客', '转化', '活动', '策划'
            ]
            for word in important_words:
                if word in text:
                    keywords.append(word)
        except Exception as e:
            logger.warning(f"关键词提取失败: {e}")
        
        return list(set(keywords))[:5]
    
    def _extract_tags(self, text: str) -> List[str]:
        """提取标签"""
        tags = []
        
        tag_keywords = {
            '价格': ['价格', '费用', '多少钱', '收费', '套餐'],
            '产品': ['功能', '产品', '系统', '特性'],
            '服务': ['售后', '客服', '服务', '支持'],
            '合作': ['合作', '代理', '加盟'],
            '营销': ['营销', '推广', '活动', '直播'],
        }
        
        for tag, keywords in tag_keywords.items():
            if any(kw in text for kw in keywords):
                tags.append(tag)
        
        return tags[:3]
    
    def _classify_question(self, question: str) -> str:
        """分类问题"""
        for category, keywords in self.category_mapping.items():
            if any(kw in question for kw in keywords):
                return category
        return "其他"
    
    def _get_cache_key(self, content: str) -> str:
        """生成缓存键"""
        return hashlib.md5(content.encode()).hexdigest()
    
    def get_cached_extraction(self, content: str) -> Optional[ExtractedKnowledge]:
        """获取缓存的提取结果"""
        cache_key = self._get_cache_key(content)
        return self._extraction_cache.get(cache_key)
    
    def cache_extraction(self, content: str, knowledge: ExtractedKnowledge):
        """缓存提取结果"""
        cache_key = self._get_cache_key(content)
        self._extraction_cache[cache_key] = knowledge
    
    def get_extraction_stats(self) -> Dict[str, Any]:
        """获取提取统计"""
        return {
            'cache_size': len(self._extraction_cache),
            'total_extractions': len(self._extraction_cache)
        }


# 全局实例
_llm_extractor = None

def get_llm_extractor(llm_service=None) -> LLMKnowledgeExtractor:
    """获取LLM知识抽取器单例"""
    global _llm_extractor
    if _llm_extractor is None:
        _llm_extractor = LLMKnowledgeExtractor(llm_service)
    return _llm_extractor
