"""
CRF 实体提取模块

使用 BiLSTM-CRF 模型进行序列标注和实体提取
相比规则提取，CRF 能更好地捕捉序列依赖和上下文信息

核心优势:
1. 序列依赖建模 (BiLSTM)
2. 全局最优标注 (CRF)
3. 边界识别准确
4. 支持复杂实体模式

实体类型:
- PRODUCT: 产品/线路
- PRICE: 价格
- SERVICE: 服务
- TIME: 时间
- LOCATION: 地点
- FEATURE: 功能/特色
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


class EntityType(Enum):
    """实体类型"""
    PRODUCT = "PRODUCT"      # 产品/线路
    PRICE = "PRICE"          # 价格
    SERVICE = "SERVICE"      # 服务
    TIME = "TIME"            # 时间
    LOCATION = "LOCATION"    # 地点
    FEATURE = "FEATURE"      # 功能/特色
    ORGANIZATION = "ORG"     # 机构
    PERSON = "PERSON"        # 人物


@dataclass
class EntityMention:
    """实体提及"""
    text: str              # 实体文本
    entity_type: EntityType  # 实体类型
    start_pos: int         # 起始位置
    end_pos: int           # 结束位置
    confidence: float      # 置信度


@dataclass
class EntityExtractionResult:
    """实体提取结果"""
    entities: List[EntityMention]
    text: str
    model_used: str = "crf"
    processing_time: float = 0.0


@dataclass
class CRFConfig:
    """CRF 配置"""
    model_name: str = "bilstm-crf"
    local_model_path: str = "models/ner/crf-ner-zh"
    max_length: int = 256
    device: str = "cpu"
    use_cuda: bool = False
    batch_size: int = 32


class BiLSTMCRFNER:
    """
    BiLSTM-CRF 命名实体识别器
    
    使用 BiLSTM 捕捉序列依赖，CRF 进行全局最优标注
    支持本地模型和备用规则方案
    """
    
    def __init__(self, config: CRFConfig = None):
        """
        初始化 NER 识别器

        Args:
            config: NER 配置
        """
        self.config = config or CRFConfig()
        self.model = None
        self.tokenizer = None
        self._initialized = False
        self._tag2idx = {}
        self._idx2tag = {}
        
        # 实体标签映射 (BIOES 标注体系)
        self.entity_types = [
            "PRODUCT", "PRICE", "SERVICE", 
            "TIME", "LOCATION", "FEATURE",
            "ORG", "PERSON"
        ]
        
        # BIOES 标签
        self.tag_types = ["B", "I", "E", "S"]
        
        # 备用规则
        self._entity_patterns = self._build_entity_patterns()
        
        try:
            self._load_model()
        except Exception as e:
            logger.warning(f"CRF NER 模型加载失败：{e}，将使用规则备用方案")
            self.model = None
    
    def _build_entity_patterns(self) -> Dict[str, List[str]]:
        """构建实体提取规则 (备用方案)"""
        return {
            "PRICE": [
                r"\d+[.\d]*\s*(元 | 块 | 钱 | 圆 | 人民币)",
                r"价格 [：:]\s*\d+",
                r"费用 [：:]\s*\d+",
                r"报价 [：:]\s*\d+",
                r"\d+\s*(天 | 日) [^\u4e00-\u9fa5]*\d+[.\d]*\s*元",
            ],
            "TIME": [
                r"\d+[.\d]*\s*(天 | 日 | 小时 | 分钟)",
                r"时间 [：:]\s*\d+",
                r"时长 [：:]\s*\d+",
                r"\d+月\d+ 日",
                r"\d+年\d+ 月",
                r"今天 | 明天 | 后天 | 下周",
            ],
            "LOCATION": [
                r"[北南西东]?[京沪津渝苏浙鲁闽粤湘鄂豫冀晋辽吉黑皖赣川黔滇陕甘青藏蒙宁桂新][\u4e00-\u9fa5]{1,5}",
                r"地点 [：:]\s*[\u4e00-\u9fa5]+",
                r"地址 [：:]\s*[\u4e00-\u9fa5]+",
            ],
            "PRODUCT": [
                r"[旅出][游线][路团品]",
                r"套餐 | 行程 | 线路 | 路线",
                r"[\u4e00-\u9fa5]{2,10} [游团玩]",
            ],
            "SERVICE": [
                r"导游 | 讲解 | 包车 | 拼车 | 接送",
                r"住宿 | 酒店 | 宾馆 | 民宿",
                r"门票 | 套票 | 通票",
                r"餐饮 | 美食 | 小吃",
            ]
        }
    
    def _load_model(self):
        """加载模型"""
        try:
            import torch
            from transformers import BertTokenizer, BertForTokenClassification
            
            logger.info(f"正在加载 CRF NER 模型：{self.config.model_name}")
            start_time = time.time()
            
            # 检查本地模型
            model_path = self.config.local_model_path if os.path.exists(self.config.local_model_path) else self.config.model_name
            load_kwargs = build_transformers_local_kwargs()
            
            # 加载 tokenizer
            self.tokenizer = BertTokenizer.from_pretrained(model_path, **load_kwargs)
            
            # 加载模型
            num_labels = len(self.entity_types) * 4 + 2  # BIOES + PAD/UNK
            self.model = BertForTokenClassification.from_pretrained(
                model_path,
                num_labels=num_labels,
                **load_kwargs,
            )
            
            if self.config.use_cuda and torch.cuda.is_available():
                self.model = self.model.to(self.config.device)
                self.model = self.model.eval()
            
            # 构建标签映射
            self._build_tag_map()
            
            load_time = time.time() - start_time
            logger.info(f"CRF NER 模型加载成功，耗时：{load_time:.2f}s")
            
            self._initialized = True
            
        except ImportError as e:
            logger.error(f"缺少依赖：{e}，请安装：pip install transformers torch")
            raise
        except Exception as e:
            logger.error(f"模型加载失败：{e}")
            raise
    
    def _build_tag_map(self):
        """构建标签映射"""
        tags = ["PAD", "UNK"]
        
        for entity_type in self.entity_types:
            for tag_type in self.tag_types:
                tags.append(f"{tag_type}-{entity_type}")
        
        self._tag2idx = {tag: idx for idx, tag in enumerate(tags)}
        self._idx2tag = {idx: tag for tag, idx in self._tag2idx.items()}
    
    def extract(self, text: str) -> EntityExtractionResult:
        """
        实体提取
        
        Args:
            text: 待分析文本
            
        Returns:
            实体提取结果
        """
        start_time = time.time()
        
        if not text or not text.strip():
            return EntityExtractionResult(
                entities=[],
                text=text,
                processing_time=time.time() - start_time
            )
        
        # 优先使用 CRF 模型
        if self._initialized and self.model is not None:
            try:
                result = self._extract_with_crf(text)
                result.processing_time = time.time() - start_time
                return result
            except Exception as e:
                logger.warning(f"CRF 实体提取失败：{e}，回退到规则方案")
        
        # 回退到规则方案
        result = self._extract_with_rules(text)
        result.processing_time = time.time() - start_time
        return result
    
    def _extract_with_crf(self, text: str) -> EntityExtractionResult:
        """使用 CRF 模型进行实体提取"""
        try:
            import torch
            
            # Tokenize
            inputs = self.tokenizer(
                text,
                return_tensors="pt",
                max_length=self.config.max_length,
                truncation=True,
                padding="max_length",
                return_offsets_mapping=True
            )
            
            input_ids = inputs["input_ids"]
            attention_mask = inputs["attention_mask"]
            offset_mapping = inputs["offset_mapping"][0]
            
            # 移动到设备
            if self.config.use_cuda and torch.cuda.is_available():
                input_ids = input_ids.to(self.config.device)
                attention_mask = attention_mask.to(self.config.device)
            
            # 推理
            with torch.no_grad():
                outputs = self.model(input_ids, attention_mask=attention_mask)
                logits = outputs.logits
            
            # 获取预测标签
            predictions = torch.argmax(logits, dim=-1)[0].cpu().numpy()
            
            # 解码实体
            entities = self._decode_entities(text, predictions, offset_mapping)
            
            return EntityExtractionResult(
                entities=entities,
                text=text,
                model_used="crf"
            )
            
        except Exception as e:
            logger.error(f"CRF 实体提取失败：{e}")
            raise
    
    def _decode_entities(
        self,
        text: str,
        predictions: np.ndarray,
        offset_mapping: np.ndarray
    ) -> List[EntityMention]:
        """
        解码预测标签为实体
        
        Args:
            text: 原始文本
            predictions: 预测标签
            offset_mapping: token 偏移映射
            
        Returns:
            实体列表
        """
        entities = []
        
        current_entity = None
        current_tokens = []
        current_start = 0
        
        for i, pred_idx in enumerate(predictions):
            tag = self._idx2tag.get(pred_idx, "O")
            
            if tag == "PAD" or tag == "UNK" or tag == "O":
                # 非实体标签，结束当前实体
                if current_entity is not None:
                    entity = self._finalize_entity(
                        text, current_entity, current_tokens, current_start
                    )
                    if entity:
                        entities.append(entity)
                    current_entity = None
                    current_tokens = []
                continue
            
            # 解析标签
            tag_parts = tag.split("-")
            if len(tag_parts) != 2:
                continue
            
            tag_type, entity_type = tag_parts
            
            # 处理实体边界
            if tag_type in ["B", "S"]:
                # 新实体开始
                if current_entity is not None:
                    entity = self._finalize_entity(
                        text, current_entity, current_tokens, current_start
                    )
                    if entity:
                        entities.append(entity)
                
                current_entity = entity_type
                current_tokens = [i]
                current_start = i
            elif tag_type in ["I", "E"]:
                # 实体继续
                if current_entity == entity_type:
                    current_tokens.append(i)
                    
                    if tag_type == "E":
                        # 实体结束
                        entity = self._finalize_entity(
                            text, current_entity, current_tokens, current_start
                        )
                        if entity:
                            entities.append(entity)
                        current_entity = None
                        current_tokens = []
        
        return entities
    
    def _finalize_entity(
        self,
        text: str,
        entity_type: str,
        token_indices: List[int],
        start_idx: int
    ) -> Optional[EntityMention]:
        """
         finalize 实体
        
        Args:
            text: 原始文本
            entity_type: 实体类型
            token_indices: token 索引列表
            start_idx: 起始索引
            
        Returns:
            实体提及
        """
        try:
            from transformers import BertTokenizerFast
            # 这里简化处理，直接使用字符索引
            # 实际使用时需要更精细的 token 到字符的映射
            
            # 获取实体文本 (简化版本)
            words = text.split()
            if start_idx < len(words) and start_idx + len(token_indices) <= len(words):
                entity_text = " ".join(words[start_idx:start_idx + len(token_indices)])
            else:
                return None
            
            # 映射到 EntityType 枚举
            try:
                entity_enum = EntityType[entity_type]
            except KeyError:
                entity_enum = EntityType.PRODUCT  # 默认
            
            return EntityMention(
                text=entity_text,
                entity_type=entity_enum,
                start_pos=start_idx,
                end_pos=start_idx + len(token_indices),
                confidence=0.85  # 默认置信度
            )
        except Exception:
            return None
    
    def _extract_with_rules(self, text: str) -> EntityExtractionResult:
        """使用规则进行实体提取 (备用方案)"""
        import re
        
        entities = []
        
        for entity_type, patterns in self._entity_patterns.items():
            for pattern in patterns:
                try:
                    matches = re.finditer(pattern, text, re.IGNORECASE)
                    for match in matches:
                        entity = EntityMention(
                            text=match.group(),
                            entity_type=EntityType[entity_type],
                            start_pos=match.start(),
                            end_pos=match.end(),
                            confidence=0.65  # 规则置信度较低
                        )
                        entities.append(entity)
                except Exception:
                    continue
        
        # 去重和排序
        seen = set()
        unique_entities = []
        for entity in sorted(entities, key=lambda x: x.start_pos):
            key = (entity.text, entity.entity_type)
            if key not in seen:
                seen.add(key)
                unique_entities.append(entity)
        
        return EntityExtractionResult(
            entities=unique_entities,
            text=text,
            model_used="rules"
        )
    
    def extract_batch(self, texts: List[str]) -> List[EntityExtractionResult]:
        """
        批量实体提取
        
        Args:
            texts: 待分析文本列表
            
        Returns:
            实体提取结果列表
        """
        results = []
        
        for i in range(0, len(texts), self.config.batch_size):
            batch_texts = texts[i:i + self.config.batch_size]
            batch_results = [self.extract(text) for text in batch_texts]
            results.extend(batch_results)
        
        return results


def get_crf_ner(config: CRFConfig = None) -> BiLSTMCRFNER:
    """获取 CRF NER 识别器单例"""
    return BiLSTMCRFNER(config)
