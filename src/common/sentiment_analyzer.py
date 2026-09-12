"""
情感分析 ML 模型模块

使用 BERT 微调模型进行情感分类
相比关键词匹配，ML 模型能更好地理解上下文和复杂情感

核心优势:
1. 深层语义理解，不仅限于关键词
2. 支持上下文依赖
3. 识别混合情感
4. 抗干扰能力强

模型架构:
- Base: bert-base-chinese
- Classification Head: Linear(768, 4)
- Classes: POSITIVE, NEGATIVE, NEUTRAL, MIXED
"""

import os
import json
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger
import numpy as np

from src.infrastructure.model_loading import build_transformers_local_kwargs


class SentimentLabel(Enum):
    """情感标签"""
    POSITIVE = "positive"  # 积极
    NEGATIVE = "negative"  # 消极
    NEUTRAL = "neutral"    # 中性
    MIXED = "mixed"        # 混合


@dataclass
class SentimentResult:
    """情感分析结果"""
    sentiment: SentimentLabel
    confidence: float
    scores: Dict[str, float] = field(default_factory=dict)  # 所有类别的分数
    reasoning: str = ""  # 分析理由 (可选)
    keywords: List[str] = field(default_factory=list)  # 触发关键词


@dataclass
class SentimentConfig:
    """情感分析配置"""
    model_name: str = "bert-base-chinese"
    local_model_path: str = "models/sentiment/bert-sentiment-zh"
    max_length: int = 128
    batch_size: int = 32
    device: str = "cpu"
    use_cuda: bool = False
    threshold: float = 0.6  # 置信度阈值
    enable_explanation: bool = False  # 是否启用解释


class BERTSentimentAnalyzer:
    """
    BERT 情感分析器
    
    使用微调的 BERT 模型进行情感分类
    支持本地模型和备用关键词方案
    """
    
    def __init__(self, config: SentimentConfig = None):
        """
        初始化情感分析器

        Args:
            config: 情感分析配置
        """
        self.config = config or SentimentConfig()
        self.model = None
        self.tokenizer = None
        self._initialized = False
        self._label_map = {
            0: SentimentLabel.POSITIVE,
            1: SentimentLabel.NEGATIVE,
            2: SentimentLabel.NEUTRAL,
            3: SentimentLabel.MIXED
        }
        
        # 关键词备用方案
        self._keyword_patterns = {
            SentimentLabel.POSITIVE: [
                "好", "棒", "满意", "感谢", "太好了", "不错", "喜欢", "优秀",
                "推荐", "值得", "开心", "高兴", "赞", "完美", "精彩"
            ],
            SentimentLabel.NEGATIVE: [
                "差", "烂", "垃圾", "骗", "投诉", "退款", "失望", "糟糕",
                "不好", "讨厌", "生气", "愤怒", "太差", "坑人", "虚假"
            ],
            SentimentLabel.NEUTRAL: [
                "一般", "还行", "凑合", "普通", "正常", "没什么", "就那样"
            ]
        }
        
        try:
            self._load_model()
        except Exception as e:
            logger.warning(f"BERT 情感分析模型加载失败：{e}，将使用关键词备用方案")
            self.model = None
    
    def _load_model(self):
        """加载模型"""
        try:
            from transformers import BertForSequenceClassification, BertTokenizer
            import torch
            
            logger.info(f"正在加载 BERT 情感分析模型：{self.config.model_name}")
            start_time = time.time()
            
            # 检查本地模型
            model_path = self.config.local_model_path if os.path.exists(self.config.local_model_path) else self.config.model_name
            load_kwargs = build_transformers_local_kwargs()
            
            # 加载 tokenizer
            self.tokenizer = BertTokenizer.from_pretrained(model_path, **load_kwargs)
            
            # 加载模型
            self.model = BertForSequenceClassification.from_pretrained(
                model_path,
                num_labels=4,
                **load_kwargs,
            )
            
            if self.config.use_cuda and torch.cuda.is_available():
                self.model = self.model.to(self.config.device)
                self.model = self.model.eval()
            
            load_time = time.time() - start_time
            logger.info(f"BERT 情感分析模型加载成功，耗时：{load_time:.2f}s")
            
            self._initialized = True
            
        except ImportError as e:
            logger.error(f"缺少依赖：{e}，请安装：pip install transformers torch")
            raise
        except Exception as e:
            logger.error(f"模型加载失败：{e}")
            raise
    
    def analyze(self, text: str) -> SentimentResult:
        """
        情感分析
        
        Args:
            text: 待分析文本
            
        Returns:
            情感分析结果
        """
        if not text or not text.strip():
            return SentimentResult(
                sentiment=SentimentLabel.NEUTRAL,
                confidence=1.0,
                scores={
                    "positive": 0.0,
                    "negative": 0.0,
                    "neutral": 1.0,
                    "mixed": 0.0
                }
            )
        
        # 优先使用 BERT 模型
        if self._initialized and self.model is not None:
            try:
                return self._analyze_with_bert(text)
            except Exception as e:
                logger.warning(f"BERT 情感分析失败：{e}，回退到关键词方案")
        
        # 回退到关键词方案
        return self._analyze_with_keywords(text)
    
    def _analyze_with_bert(self, text: str) -> SentimentResult:
        """使用 BERT 模型进行情感分析"""
        try:
            import torch
            from torch.nn import functional as F
            
            # Tokenize
            inputs = self.tokenizer(
                text,
                return_tensors="pt",
                max_length=self.config.max_length,
                truncation=True,
                padding="max_length"
            )
            
            # 移动到设备
            if self.config.use_cuda and torch.cuda.is_available():
                inputs = {k: v.to(self.config.device) for k, v in inputs.items()}
            
            # 推理
            with torch.no_grad():
                outputs = self.model(**inputs)
                logits = outputs.logits
                probabilities = F.softmax(logits, dim=-1)
            
            # 获取结果
            probs = probabilities[0].cpu().numpy()
            predicted_class = np.argmax(probs)
            confidence = float(probs[predicted_class])
            
            # 构建结果
            sentiment = self._label_map[predicted_class]
            scores = {
                "positive": float(probs[0]),
                "negative": float(probs[1]),
                "neutral": float(probs[2]),
                "mixed": float(probs[3])
            }
            
            # 提取关键词
            keywords = self._extract_keywords(text, sentiment)
            
            result = SentimentResult(
                sentiment=sentiment,
                confidence=confidence,
                scores=scores,
                keywords=keywords
            )
            
            # 添加解释 (可选)
            if self.config.enable_explanation:
                result.reasoning = self._generate_explanation(text, sentiment, confidence, keywords)
            
            logger.debug(f"BERT 情感分析：{sentiment.value} (置信度：{confidence:.3f})")
            
            return result
            
        except Exception as e:
            logger.error(f"BERT 情感分析失败：{e}")
            raise
    
    def _analyze_with_keywords(self, text: str) -> SentimentResult:
        """使用关键词匹配进行情感分析 (备用方案)"""
        text_lower = text.lower()
        
        scores = {
            "positive": 0.0,
            "negative": 0.0,
            "neutral": 0.0,
            "mixed": 0.0
        }
        
        matched_keywords = {
            "positive": [],
            "negative": [],
            "neutral": []
        }
        
        # 统计各情感关键词匹配数
        for sentiment, keywords in self._keyword_patterns.items():
            count = 0
            for keyword in keywords:
                if keyword in text_lower:
                    count += 1
                    matched_keywords[sentiment.value].append(keyword)
            
            # 计算分数
            if sentiment == SentimentLabel.POSITIVE:
                scores["positive"] = min(count / 5.0, 1.0)
            elif sentiment == SentimentLabel.NEGATIVE:
                scores["negative"] = min(count / 5.0, 1.0)
            elif sentiment == SentimentLabel.NEUTRAL:
                scores["neutral"] = min(count / 3.0, 1.0)
        
        # 检测混合情感
        high_scores = [k for k, v in scores.items() if v > 0.3]
        if len(high_scores) >= 2:
            scores["mixed"] = 0.5
            sentiment = SentimentLabel.MIXED
            confidence = 0.5
        else:
            # 选择最高分数的情感
            sentiment_key = max(scores, key=scores.get)
            confidence = scores[sentiment_key]
            
            if sentiment_key == "positive":
                sentiment = SentimentLabel.POSITIVE
            elif sentiment_key == "negative":
                sentiment = SentimentLabel.NEGATIVE
            else:
                sentiment = SentimentLabel.NEUTRAL
        
        # 归一化分数
        total = sum(scores.values())
        if total > 0:
            scores = {k: v / total for k, v in scores.items()}
        
        # 合并关键词
        all_keywords = []
        for kw_list in matched_keywords.values():
            all_keywords.extend(kw_list)
        
        return SentimentResult(
            sentiment=sentiment,
            confidence=confidence,
            scores=scores,
            keywords=all_keywords[:10]  # 限制关键词数量
        )
    
    def _extract_keywords(self, text: str, sentiment: SentimentLabel) -> List[str]:
        """提取触发关键词"""
        keywords = []
        text_lower = text.lower()
        
        for keyword in self._keyword_patterns.get(sentiment, []):
            if keyword in text_lower:
                keywords.append(keyword)
        
        return keywords[:10]
    
    def _generate_explanation(
        self,
        text: str,
        sentiment: SentimentLabel,
        confidence: float,
        keywords: List[str]
    ) -> str:
        """生成分析解释"""
        sentiment_names = {
            SentimentLabel.POSITIVE: "积极",
            SentimentLabel.NEGATIVE: "消极",
            SentimentLabel.NEUTRAL: "中性",
            SentimentLabel.MIXED: "混合"
        }
        
        sentiment_name = sentiment_names.get(sentiment, "未知")
        confidence_pct = f"{confidence * 100:.1f}%"
        
        explanation = f"文本情感为{sentiment_name}，置信度{confidence_pct}"
        
        if keywords:
            explanation += f"，触发关键词：{', '.join(keywords[:5])}"
        
        return explanation
    
    def analyze_batch(self, texts: List[str]) -> List[SentimentResult]:
        """
        批量情感分析
        
        Args:
            texts: 待分析文本列表
            
        Returns:
            情感分析结果列表
        """
        results = []
        
        for i in range(0, len(texts), self.config.batch_size):
            batch_texts = texts[i:i + self.config.batch_size]
            batch_results = [self.analyze(text) for text in batch_texts]
            results.extend(batch_results)
        
        return results


def get_sentiment_analyzer(config: SentimentConfig = None) -> BERTSentimentAnalyzer:
    """获取情感分析器单例"""
    return BERTSentimentAnalyzer(config)
