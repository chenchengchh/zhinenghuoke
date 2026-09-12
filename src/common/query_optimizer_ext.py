"""
查询优化模块
实现查询分解、同义词扩展、查询改写等功能
基于行业最佳实践提升检索准确度
"""

import re
from typing import List, Dict, Tuple, Set
from loguru import logger


class QueryDecomposer:
    """
    查询分解器

    功能：
    1. 复杂查询分解为多个子查询
    2. 同义词扩展
    3. 查询意图推断
    """

    # 意图相关的关键词映射
    INTENT_KEYWORDS = {
        "价格": ["价格", "收费", "费用", "报价", "价位", "定价", "售价", "多少钱", "成本"],
        "产品": ["产品", "功能", "介绍", "特点", "特性", "能做", "有什么用"],
        "服务": ["服务", "售后", "支持", "保障", "维护", "技术"],
        "合作": ["合作", "加盟", "代理", "批发", "采购", "渠道", "OEM"],
        "优惠": ["优惠", "折扣", "便宜", "活动", "促销", "打折", "特价"],
        "售后": ["售后", "维修", "退换", "退货", "退款", "质量问题"],
        "安全": ["安全", "隐私", "加密", "保密", "数据保护"],
        "试用": ["试用", "体验", "注册", "开通", "开始使用", "如何用"]
    }

    # 同义词词典
    SYNONYMS = {
        "产品": ["商品", "服务", "系统", "软件", "工具"],
        "价格": ["收费", "费用", "价钱", "价位", "报价", "成本"],
        "功能": ["特性", "特点", "能力", "作用", "用处"],
        "合作": ["加盟", "代理", "渠道", "伙伴"],
        "优惠": ["折扣", "便宜", "活动", "促销", "特价"],
        "服务": ["售后", "支持", "保障", "帮助"],
        "公司": ["企业", "厂商", "供应商", "我们"],
        "购买": ["买", "采购", "订购", "获取"]
    }

    # 组合查询模式
    COMPLEX_PATTERNS = [
        (r"(.+?)(价格|收费|多少钱)(.+)", ["价格相关"]),
        (r"(.+?)(功能|特点|能做什么)(.+)", ["产品相关"]),
        (r"(.+?)(合作|代理|加盟)(.+)", ["合作相关"]),
        (r"(.+?)(优惠|折扣|活动)(.+)", ["优惠相关"]),
        (r"(.+?)(售后|服务|保障)(.+)", ["服务相关"])
    ]

    def __init__(self):
        """初始化查询分解器"""
        self.stop_words = self._init_stop_words()

    def _init_stop_words(self) -> Set[str]:
        """初始化停用词"""
        return {
            "的", "了", "是", "在", "有", "和", "就", "不", "都", "很",
            "我", "你", "他", "她", "它", "这", "那", "个", "们", "要",
            "会", "能", "可", "以", "到", "说", "对", "也", "而", "及",
            "吗", "呢", "吧", "啊", "哦", "嗯", "啦", "呀", "嘛", "哈",
            "请问", "我想", "我想问", "问一下", "问一下", "想了解",
            "想问一下", "能问一下", "可以问一下"
        }

    def decompose(self, query: str) -> List[str]:
        """
        分解复杂查询为多个子查询

        Args:
            query: 用户查询

        Returns:
            分解后的子查询列表
        """
        if not query or not query.strip():
            return [query]

        queries = []
        original_query = query.strip()

        # 1. 提取核心意图词
        intents = self._extract_intent_topics(original_query)

        # 2. 生成基础查询
        queries.append(original_query)

        # 3. 如果检测到多个意图，生成针对性的子查询
        if len(intents) >= 2:
            for intent in intents[:2]:
                if intent not in original_query:
                    queries.append(f"{original_query} {intent}")

        # 4. 同义词扩展
        expanded_queries = self._expand_with_synonyms(original_query)
        queries.extend(expanded_queries)

        # 5. 去重并返回
        unique_queries = []
        seen = set()
        for q in queries:
            q_normalized = q.lower().strip()
            if q_normalized not in seen and q.strip():
                seen.add(q_normalized)
                unique_queries.append(q.strip())

        return unique_queries[:5]

    def _extract_intent_topics(self, query: str) -> List[str]:
        """提取查询中的意图主题"""
        topics = []
        query_lower = query.lower()

        for topic, keywords in self.INTENT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in query_lower:
                    if topic not in topics:
                        topics.append(topic)
                    break

        return topics

    def _expand_with_synonyms(self, query: str) -> List[str]:
        """使用同义词扩展查询"""
        expanded = []
        query_lower = query.lower()

        for word, synonyms in self.SYNONYMS.items():
            if word in query_lower:
                for syn in synonyms[:2]:
                    if syn not in query_lower:
                        expanded.append(query.replace(word, syn))

        return expanded

    def extract_keywords(self, query: str, top_k: int = 10) -> List[str]:
        """
        提取查询关键词

        Args:
            query: 用户查询
            top_k: 返回数量

        Returns:
            关键词列表
        """
        if not query:
            return []

        # 简单分词
        words = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}', query)

        # 过滤停用词
        keywords = [w for w in words if w not in self.stop_words and len(w) >= 2]

        # 返回前top_k个
        return keywords[:top_k]


class DynamicWeightCalculator:
    """
    动态权重计算器

    根据查询类型和意图动态调整向量检索和关键词检索的权重
    基于行业最佳实践：
    - 价格类查询：关键词权重提高（精确匹配更重要）
    - 产品类查询：向量权重提高（语义相似更重要）
    - 复杂查询：平衡权重
    """

    DEFAULT_VECTOR_WEIGHT = 0.6
    DEFAULT_KEYWORD_WEIGHT = 0.4

    INTENT_WEIGHTS = {
        "价格": {"vector": 0.4, "keyword": 0.6},
        "产品": {"vector": 0.7, "keyword": 0.3},
        "服务": {"vector": 0.5, "keyword": 0.5},
        "合作": {"vector": 0.5, "keyword": 0.5},
        "优惠": {"vector": 0.4, "keyword": 0.6},
        "售后": {"vector": 0.5, "keyword": 0.5},
        "安全": {"vector": 0.6, "keyword": 0.4},
        "试用": {"vector": 0.6, "keyword": 0.4}
    }

    # 关键词模式匹配的权重调整
    PATTERN_WEIGHTS = {
        r"(价格|多少钱|收费|费用)": {"vector": 0.4, "keyword": 0.6},
        r"(优惠|折扣|便宜|活动)": {"vector": 0.4, "keyword": 0.6},
        r"(怎么|如何|步骤)": {"vector": 0.7, "keyword": 0.3},
        r"(是什么|有哪些|特点)": {"vector": 0.7, "keyword": 0.3}
    }

    def calculate(self, query: str, intent: str = None) -> Tuple[float, float]:
        """
        计算动态权重

        Args:
            query: 用户查询
            intent: 意图类型

        Returns:
            (向量权重, 关键词权重)
        """
        vector_weight = self.DEFAULT_VECTOR_WEIGHT
        keyword_weight = self.DEFAULT_KEYWORD_WEIGHT

        # 1. 基于意图类型调整
        if intent and intent in self.INTENT_WEIGHTS:
            weights = self.INTENT_WEIGHTS[intent]
            vector_weight = weights["vector"]
            keyword_weight = weights["keyword"]

        # 2. 基于查询模式调整
        for pattern, weights in self.PATTERN_WEIGHTS.items():
            if re.search(pattern, query):
                # 加权平均
                vector_weight = (vector_weight + weights["vector"]) / 2
                keyword_weight = (keyword_weight + weights["keyword"]) / 2
                break

        # 3. 确保权重和为1
        total = vector_weight + keyword_weight
        return vector_weight / total, keyword_weight / total


class AnswerExtractor:
    """
    增强型答案提取器

    改进点：
    1. 支持更多答案格式
    2. 智能清理无关内容
    3. 答案质量评分
    4. 多段落答案合并
    """

    # 答案格式模式
    ANSWER_PATTERNS = [
        # 标准格式
        (r"答案[:：]\s*(.+?)(?:\n|$)", "标准"),
        (r"答[:：]\s*(.+?)(?:\n|$)", "标准"),
        (r"回复[:：]\s*(.+?)(?:\n|$)", "标准"),
        # 英文格式
        (r"A[:：]\s*(.+?)(?:\n|$)", "英文"),
        (r"Answer[:：]\s*(.+?)(?:\n|$)", "英文"),
        # 列表格式
        (r"\d+[.、](.+?)(?:\n|$)", "列表"),
        # 冒号分隔
        (r"[:：]\s*(.+?)(?:\n|$)", "分隔")
    ]

    # 需要过滤的内容
    FILTER_PATTERNS = [
        r"^\s*问题[：:]\s*.+",
        r"^\s*Q[：:]\s*.+",
        r"^\s*相关问题[：:].*",
        r"^\s*参考[：:].*",
        r"^\s*来源[：:].*",
        r"^\s*更新时间[：:].*",
        r"【优先级】\d+",
        r"【使用次数】\d+",
        r"【状态】\w+",
        r"回复模板.*",
        r"【关键词】.*?【",
        r"【别名】.*?【",
        r"【标签】.*?【",
        r"【答案】.*?【",
        r"【.*?】",
    ]

    def __init__(self):
        """初始化答案提取器"""
        self.max_answer_length = 300
        self.min_answer_length = 10

    def extract(self, content: str) -> Tuple[str, float]:
        """
        从内容中提取答案

        Args:
            content: 知识库内容

        Returns:
            (提取的答案, 质量分数)
        """
        if not content:
            return "", 0.0

        # 1. 清理原始内容
        cleaned = self._clean_content(content)

        # 2. 尝试使用模式匹配提取
        for pattern, pattern_type in self.ANSWER_PATTERNS:
            match = re.search(pattern, cleaned, re.DOTALL)
            if match:
                answer = match.group(1).strip()
                answer = self._post_process(answer)
                if self._is_valid_answer(answer):
                    quality = self._assess_quality(answer, pattern_type)
                    return answer, quality

        # 3. 如果没有匹配到标准格式，尝试智能提取
        answer = self._smart_extract(cleaned)
        if answer:
            quality = self._assess_quality(answer, "智能")
            return answer, quality

        # 4. 最后兜底：返回清理后的内容前200字
        fallback = cleaned[:self.max_answer_length]
        return fallback, 0.3

    def _clean_content(self, content: str) -> str:
        """清理内容中的无关字符"""
        if not content:
            return ""

        # 移除嵌入的元数据
        for pattern in self.FILTER_PATTERNS:
            content = re.sub(pattern, '', content)

        # 移除多余空白
        lines = content.split('\n')
        cleaned_lines = []

        for line in lines:
            line = line.strip()
            # 跳过空行
            if not line:
                continue
            # 跳过只有数字和竖线的行
            if re.match(r'^[\d\s|｜,.]+$', line):
                continue
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def _post_process(self, answer: str) -> str:
        """后处理答案"""
        if not answer:
            return ""

        # 只移除 FILTER_PATTERNS 中的模式
        for pattern in self.FILTER_PATTERNS:
            answer = re.sub(pattern, '', answer)

        # 移除多余空白
        answer = ' '.join(answer.split())

        # 移除末尾的标点符号
        answer = answer.rstrip('.,，、。')

        # 限制长度
        if len(answer) > self.max_answer_length:
            answer = answer[:self.max_answer_length]
            # 尽量在句号处截断
            last_punct = max(
                answer.rfind('。'),
                answer.rfind('！'),
                answer.rfind('？')
            )
            if last_punct > self.min_answer_length:
                answer = answer[:last_punct + 1]

        return answer.strip()

    def _is_valid_answer(self, answer: str) -> bool:
        """判断答案是否有效"""
        if not answer:
            return False

        length = len(answer)

        # 长度检查
        if length < self.min_answer_length or length > self.max_answer_length * 2:
            return False

        # 必须包含有效字符
        if not re.search(r'[\u4e00-\u9fa5a-zA-Z0-9]', answer):
            return False

        # 排除明显的无效内容
        invalid_starts = ["问题", "Q:", "参考", "来源", "更新时间"]
        for start in invalid_starts:
            if answer.startswith(start):
                return False

        return True

    def _smart_extract(self, content: str) -> str:
        """智能提取答案"""
        lines = content.split('\n')
        meaningful_lines = []

        for line in lines:
            line = line.strip()
            # 跳过太短的行
            if len(line) < self.min_answer_length:
                continue
            # 跳过标题类行
            if re.match(r'^#+\s', line):
                continue
            if re.match(r'^\d+\.', line) and len(line) < 30:
                continue
            meaningful_lines.append(line)

        if not meaningful_lines:
            return ""

        # 合并前几条有意义的内容
        result = []
        total_length = 0

        for line in meaningful_lines[:5]:
            if total_length + len(line) <= self.max_answer_length:
                result.append(line)
                total_length += len(line)
            else:
                break

        return ' '.join(result)

    def _assess_quality(self, answer: str, pattern_type: str) -> float:
        """
        评估答案质量

        Returns:
            质量分数 0.0-1.0
        """
        if not answer:
            return 0.0

        score = 0.5

        # 1. 格式匹配加分
        format_scores = {"标准": 0.9, "英文": 0.85, "智能": 0.7, "列表": 0.75, "分隔": 0.6}
        score = format_scores.get(pattern_type, 0.7)

        # 2. 长度适中加分
        length = len(answer)
        if 50 <= length <= 150:
            score += 0.1
        elif 20 <= length < 50 or 150 < length <= 300:
            score += 0.05

        # 3. 包含数字或具体信息加分
        if re.search(r'\d+', answer):
            score += 0.05

        # 4. 包含具体名词加分
        if re.search(r'[\u4e00-\u9fa5]{2,}', answer):
            score += 0.05

        return min(score, 1.0)


def create_query_optimizer() -> QueryDecomposer:
    """创建查询优化器实例"""
    return QueryDecomposer()


def create_weight_calculator() -> DynamicWeightCalculator:
    """创建权重计算器实例"""
    return DynamicWeightCalculator()


def create_answer_extractor() -> AnswerExtractor:
    """创建答案提取器实例"""
    return AnswerExtractor()