"""
增强智能客服服务
集成所有10项能力：语义理解、情感分析、意图识别、上下文理解、因果推理、风险评估、优先级判断、解决方案生成
基于现有知识库系统，增强而不破坏原有功能
当前消息回复统一走单一主链：消息理解 -> 知识检索 -> LLM生成/兜底 -> guardrail
"""
import logging
import os
import random
import hashlib
import uuid
import time
import copy
import threading
import re
from typing import Dict, List, Optional, Any, Tuple, Set
from datetime import datetime
from functools import lru_cache

from .context_understanding import context_understanding_module
from .intent_recognizer import EnhancedIntentRecognizer, IntentType
from .types.intent import is_industry_intent, get_intent_value
from .answer_reorganization import get_answer_reorganizer, AnswerReorganizer
from .self_learning_rag import get_self_learning_rag_system
from .multi_turn_dialogue_manager import MultiTurnDialogueManager
# 修复 R4：AdvancedQueryPreprocessor 是死代码，已删除 import
from src.rag.rag_evaluator import RAGASEvaluator
from .proactive_service_engine import ProactiveServiceEngine
from .knowledge_graph import get_knowledge_graph_service
from .unified_knowledge_service import get_unified_knowledge_service
from .bert_intent_recognizer import BertIntentRecognizer, get_bert_intent_recognizer
from .memory_service import MemoryService, get_memory_service
from .domain_profile_service import get_domain_profile_service
from .knowledge_base_adapter import get_learning_knowledge_base_adapter
from .sales_followup_service import get_sales_followup_service
from .industry_schema_service import get_industry_schema_service, get_active_schema_with_compat
from .industry_strategies import (
    GenericIndustryStrategy,
    SchemaDrivenStrategy,
    get_active_industry_strategy,
)
from .reply_router import decide_main_reply_route, decide_reply_execution_route
from .reply_orchestrator import (
    ReplyOrchestrator,
    STANDARD_ANALYSIS_MODE,
)
from .guardrail_stage import GuardrailStage
from .generation_stage import GenerationStage
from .reply_observability import update_mainline_observability_stats
from .retrieval_stage import RetrievalStage
from .agent_orchestrator import get_agent_orchestrator
from .tracing_support import (
    build_trace_snapshot,
    attach_routing_decision,
    attach_execution_routing,
    attach_orchestration_plan,
    append_trace_span,
)
from .category_display_config import get_active_query_category_hints
from src.common.types import (
    RoutingDecision,
    ReplyObjective,
    ConversionStage,
    NextBestAction,
    CtaMode,
)
from src.config.settings import (
    CRAG_MIN_RELEVANCE as SETTINGS_CRAG_MIN_RELEVANCE,
    CRAG_PASS_THRESHOLD as SETTINGS_CRAG_PASS_THRESHOLD,
    CRAG_RETRY_THRESHOLD as SETTINGS_CRAG_RETRY_THRESHOLD,
    CRAG_REWRITE_TIMEOUT_SECONDS as SETTINGS_CRAG_REWRITE_TIMEOUT_SECONDS,
    ENABLE_LLM_ONLY_FALLBACK,
    LLM_ONLY_FALLBACK_TIMEOUT_SECONDS,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_TEMPERATURE,
    OLLAMA_TOP_K,
    OLLAMA_TOP_P,
    REPLY_LLM_TIMEOUT_SECONDS,
    ENABLE_RETRY_DIRECT_ANSWER,
)

logger = logging.getLogger(__name__)

_KB_FAQ_PAIR_RE = re.compile(r"Q[：:]\s*(.+?)\s*A[：:]\s*([\s\S]*?)(?=(?:\n\s*Q[：:])|\Z)")


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_DEFAULT_SCHEMA_REPLY_PROFILE = {
    "sales_persona": "你是一名行业顾问，基于知识库为客户解释方案、差异、适用条件与下一步建议。",
    "qa_persona": "你是一名专业顾问，只回答当前行业相关问题。",
    "general_qa_persona": "你是一名通用问答助手，直接回答用户的通识、百科与常识问题。",
    "sales_focus": "优先回答客户当前问题，再自然补充完成判断所需的关键信息，如时间、数量、预算、适用对象或交付要求。",
    "entity_type_label": "产品",
    "domain_rules": [
        "只使用知识库中提供的信息，不要编造未提供的事实。",
        "先答当前问题，再推进下一步，不要每轮都强行留资。",
        "口吻自然，像真人顾问沟通，不要系统播报腔。",
    ],
    "base_replies": {
        "thanks": "{name}，不客气。后面如果还想了解方案、价格、适用范围或实施方式，直接发我就行。",
        "greeting": "",
        "farewell": "{name}，感谢咨询！后续还有问题随时联系我。",
        "confirmation": "{name}，好的，我继续帮您整理。您把更具体的需求发我，我按已知信息往下说明。",
        "status_inquiry": "{name}，我在的。您直接说产品、预算、数量、用途或时间要求，我这边帮您一起梳理。",
        "ready_check": "{name}，准备好了，这边可以继续说明。您把具体需求发我，我按已知信息帮您匹配。",
        "price_inquiry": "{name}，没问题。不同方案的价格会受配置、范围和服务内容影响，您说下具体需求，我按信息给您拆开说明。",
        "contact_inquiry": "{name}，可以的。您也可以先把需求发我，我先帮您把要点整理清楚。",
        "complaint": "{name}，非常抱歉给您带来不便，我先帮您记录问题并尽快安排跟进处理。",
        "product_inquiry": "{name}，您好，我可以先按您的需求介绍合适方案。您更看重价格、效果，还是适配场景？",
        "purchase_intent": "{name}，好的，那我继续帮您往下整理。您把需求细节和时间安排发我，我按已知信息给您说明下一步。",
        "cooperation_intent": "{name}，欢迎合作咨询。您可以先说下合作方向和需求，我这边帮您整理对接信息。",
        "business_intro": "",
        "default": "",
        "fallback": "",
    },
}

_DEFAULT_SCHEMA_REPLY_POLICY = {
    "intent_reply_fallbacks": {
        "product_inquiry": ["product_inquiry", "service_inquiry", "default", "fallback"],
        "service_inquiry": ["service_inquiry", "product_inquiry", "default", "fallback"],
        "price_inquiry": ["price_inquiry", "product_inquiry", "default", "fallback"],
        "contact_inquiry": ["contact_inquiry", "service_inquiry", "default", "fallback"],
        "purchase_intent": ["purchase_intent", "product_inquiry", "default", "fallback"],
        "cooperation_intent": ["cooperation_intent", "service_inquiry", "default", "fallback"],
        "default": ["product_inquiry", "service_inquiry", "default", "fallback"],
    },
    "stage_objectives": {
        "default": {"next_best_action": "answer_only", "cta_mode": "none"},
        "high_intent": {"next_best_action": "offer_material", "cta_mode": "material_offer"},
        "reservation": {"next_best_action": "offer_reservation", "cta_mode": "reservation_offer"},
        "handoff": {"next_best_action": "capture_lead", "cta_mode": "lead_capture"},
        "missing_slots": {"next_best_action": "clarify", "cta_mode": "soft_probe"},
    },
    "stage_thresholds": {
        "buying_signal_to_order": 0.85,
    },
    "cta_append_policy": {
        "disable_when_next_action": ["clarify"],
        "disable_when_cta_mode": ["none"],
        "require_answer_text": True,
        "fact_query_block_terms": [
            "多少钱", "价格", "费用", "报价", "流程", "步骤", "怎么安排",
            "怎么使用", "怎么开通", "实施", "部署", "交付", "包含", "不含",
        ],
        "explicit_contact_terms": [
            "联系方式", "怎么联系", "联系你", "联系您", "资料发我",
            "发我资料", "报价发我", "方案发我", "留个联系方式", "留联系方式",
        ],
    },
}


def resolve_context_session_id(
    *,
    platform: str,
    customer_id: str = "",
    provided_session_id: Optional[str] = None,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """统一生成上下文与意图识别使用的 session_id。"""
    normalized_platform = str(platform or "").strip() or "unknown"
    provided_session = str(provided_session_id or "").strip()
    if provided_session:
        return provided_session

    history_session_id = EnhancedCustomerService._extract_consistent_history_field(
        conversation_history,
        ("session_id",),
    )
    if history_session_id:
        last_msg = conversation_history[-1] if conversation_history else {}
        last_time_str = last_msg.get("created_at") or last_msg.get("time")
        is_stale = False
        if last_time_str:
            try:
                last_time = datetime.fromisoformat(str(last_time_str).replace("Z", "+00:00"))
                if (datetime.now() - last_time).total_seconds() > 24 * 3600:
                    is_stale = True
            except (ValueError, TypeError):
                is_stale = False
        if not is_stale:
            return history_session_id

    normalized_customer_id = str(customer_id or "").strip()
    if not normalized_customer_id:
        return ""

    today_str = datetime.now().strftime("%Y%m%d")
    return f"{normalized_platform}_{normalized_customer_id}_{today_str}"



# 共享的 safe_math_eval 实现（来自 core_utils.safe_eval）
# 保留 _safe_math_eval 别名以保持向后兼容
from .core_utils.safe_eval import safe_math_eval as _safe_math_eval


def _is_math_related_content(content: str) -> bool:
    """判断消息内容是否是数学相关的讨论（用于过滤对话历史）"""
    if not content:
        return False
    math_indicators = [
        '无数学问题', '无计算结果', '无法给出数学计算结果',
        '没有包含任何具体的数学问题', '不满足.*指代历史上的计算题',
        '从未出现任何数学问题', '不存在任何数学问题',
        '可计算的数值表达式', '数学表达式或运算请求',
        '分数.*3/20', '化为小数', '计算结果：',
        'LaTeX', '\\frac{', '$$',
        '对话历史中.*数学', '数学语境中',
    ]
    import re
    for pat in math_indicators:
        if re.search(pat, content):
            return True
    if content.startswith('✅') and '数学' in content:
        return True
    if content.startswith('**') and ('数学' in content or '计算' in content):
        return True
    return False


def _filter_math_history(conversation_history: list, max_messages: int = 3) -> str:
    """过滤对话历史中的数学相关消息，构建干净的对话文本"""
    history_text = ""
    count = 0
    if conversation_history:
        for msg in reversed(conversation_history):
            content = msg.get('content', '')
            if _is_math_related_content(content):
                continue
            role = "用户" if msg.get('direction') == 'inbound' else "助手"
            history_text = f"{role}：{content}\n" + history_text
            count += 1
            if count >= max_messages:
                break
    return history_text


_DATE_PATTERNS = [
    r'^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$',
    r'^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$',
    r'^\d{1,2}/\d{1,2}$',
    r'^\d{1,2}月\d{1,2}[日号]$',
    r'^\d{4}年\d{1,2}月\d{1,2}[日号]$',
]

_MATH_PATTERNS = [
    r'^[\d\s\+\-\*\/\(\)\.]+$',
    r'^[\d\s\+\-\*\/\(\)\.]+\s*[=不等于]+\s*\?*$',
]

_MATH_HINT_KEYWORDS = [
    '等于多少', '加起来等于', '相加等于', '计算结果', '答案是', '结果是多少',
    'what is', 'what equals', 'result is', 'answer is',
]

_MATH_STRIP_KEYWORDS = ['等于', '是', '多少', '加起来', '相加', '计算']


def _is_probable_math_query(message: str) -> bool:
    """判断是否像数学计算问题，同时排除日期和行业业务语境。"""
    import re

    message_stripped = message.strip()
    for pattern in _DATE_PATTERNS:
        if re.match(pattern, message_stripped):
            return False

    for pattern in EnhancedCustomerService._get_domain_non_math_patterns():
        if re.search(pattern, message_stripped):
            return False

    if any(re.match(pattern, message_stripped) for pattern in _MATH_PATTERNS):
        return True

    msg_lower = message.lower()
    return any(keyword in msg_lower for keyword in _MATH_HINT_KEYWORDS)


def _extract_math_expression(message: str) -> str:
    """从输入中提取可直接计算的表达式。"""
    import re

    math_expr = message.strip()
    op_map = {'加': '+', '减': '-', '乘': '*', '除': '/', '÷': '/', '×': '*'}
    for src, target in op_map.items():
        math_expr = math_expr.replace(src, target)
    for keyword in _MATH_STRIP_KEYWORDS:
        math_expr = math_expr.replace(keyword, '')

    math_expr = re.sub(r'[^\d\+\-\*\/\(\)\.]', '', math_expr)
    return math_expr.strip()


def _resolve_math_query_locally(message: str, conversation_history: List[Dict] = None) -> Tuple[bool, Optional[str]]:
    """优先用规则和安全求值处理数学问题，避免不必要的 LLM 调用。"""
    import re

    if not _is_probable_math_query(message):
        return False, None

    math_expr = _extract_math_expression(message)

    if not math_expr or re.match(r'^\d+$', math_expr):
        if conversation_history:
            last_msg = conversation_history[-1].get('content', '')
            nums = re.findall(r'\d+', last_msg)
            ops = []
            for op in ['+', '-', '*', '/', '÷', '×', '加', '减', '乘', '除']:
                if op in last_msg:
                    ops.append(op)
            if len(nums) >= 2 and ops:
                try:
                    op_map = {'加': '+', '减': '-', '乘': '*', '除': '/', '÷': '/', '×': '*'}
                    actual_op = op_map.get(ops[0], ops[0])
                    expr = f"{nums[0]}{actual_op}{nums[1]}"
                    result = _safe_math_eval(expr)
                    return True, f"{nums[0]}{actual_op}{nums[1]} = {result}"
                except Exception as exc:
                    logger.debug(f"上下文数学表达式计算失败: {exc}")
        return True, None

    if re.match(r'^[\d\s\+\-\*\/\(\)]+$', math_expr):
        try:
            result = _safe_math_eval(math_expr)
            return True, f"{math_expr.replace(' ', '')} = {result}"
        except Exception as exc:
            logger.debug(f"数学表达式求值失败: {exc}")

    return True, None


def _build_math_llm_prompt(message: str, conversation_history: List[Dict] = None) -> str:
    history_text = _filter_math_history(conversation_history, 3)
    return f"""你是一个数学计算助手。请计算用户提出的数学问题并给出答案。

对话历史：
{history_text}

当前问题：{message}

规则：
1. 只计算明确的数学表达式（如"3+5"、"12*4"）
2. 如果问题不是数学计算（如日期、业务费用等），请回复"非数学问题"
3. 直接给出计算结果，格式例如：3+5 = 8"""


class IntentCache:
    """意图识别缓存（线程安全，O(1)淘汰）"""

    def __init__(self, max_size=1000, ttl=300):
        from collections import OrderedDict
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
        self.max_size = max_size
        self.ttl = ttl
        self._lock = threading.Lock()

    def _make_key(self, message: str, session_id: str, context_signature: str = "") -> str:
        return hashlib.md5(f"{session_id}:{message}:{context_signature}".encode()).hexdigest()

    def get(self, message: str, session_id: str, context_signature: str = "") -> Optional[Dict]:
        key = self._make_key(message, session_id, context_signature)
        with self._lock:
            if key in self._cache:
                if time.time() - self._timestamps[key] < self.ttl:
                    self._cache.move_to_end(key)
                    return copy.deepcopy(self._cache[key])
                else:
                    del self._cache[key]
                    del self._timestamps[key]
        return None

    def set(self, message: str, session_id: str, result: Dict, context_signature: str = ""):
        key = self._make_key(message, session_id, context_signature)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_size:
                try:
                    oldest_key, _ = self._cache.popitem(last=False)
                    self._timestamps.pop(oldest_key, None)
                except KeyError:
                    self._cache.clear()
                    self._timestamps.clear()
            self._cache[key] = copy.deepcopy(result)
            self._timestamps[key] = time.time()

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()


class EnhancedCustomerService:
    """
    增强智能客服服务

    性能优化：
    1. 意图识别缓存 - 减少重复计算
    2. 轻量交互元数据 - 用于兼容判断与路由观测
    3. 延迟加载 - 非必要分析延迟执行
    """

    LIGHTWEIGHT_INTERACTION_INTENTS = set(IntentType.get_lightweight_interaction_intents())

    CRAG_MIN_RELEVANCE = SETTINGS_CRAG_MIN_RELEVANCE
    CRAG_PASS_THRESHOLD = SETTINGS_CRAG_PASS_THRESHOLD
    CRAG_RETRY_THRESHOLD = SETTINGS_CRAG_RETRY_THRESHOLD
    CRAG_REWRITE_TIMEOUT_SECONDS = SETTINGS_CRAG_REWRITE_TIMEOUT_SECONDS
    GENERIC_DOMAIN_KEYWORDS = {
        "产品", "服务", "方案", "项目", "功能", "配置", "效果", "适用", "对象",
        "价格", "费用", "报价", "预算", "咨询", "客服", "支持", "流程", "步骤",
        "开通", "使用", "交付", "实施", "售后", "合作", "退款", "协议", "条款",
        "预约", "预订", "下单", "购买", "联系", "资料", "说明",
    }
    OFF_DOMAIN_BUSINESS_TERMS = {
        "做什么", "做啥", "干什么", "干嘛", "主营", "业务", "公司", "产品", "服务",
        "系统", "软件", "卖什么", "哪家公司", "什么公司", "什么业务", "什么系统", "什么软件",
        "智能获客", "获客系统", "营销自动化", "saas", "crm", "抖音产品",
    }
    GENERIC_BUSINESS_INTRO_PATTERNS = (
        r"(你|你们|你家|咱们).*(做啥|干嘛|干什么|做什么|搞什么|哪家公司|什么公司|什么业务|什么系统|什么软件|卖什么)",
        r"(公司|团队|品牌).*(介绍一下|是做什么|主营什么)",
        r"(主营|主要).*(什么业务|做什么|卖什么)",
    )
    PREVIEW_NOISE_LINE_RE = re.compile(
        r"^(?:刚刚|昨天|前天|今天|已读|未读|\d{1,2}:\d{2}|\d+\s*分钟前|\d+\s*小时前)$"
    )

    HIGH_PRIORITY_INTENTS = {
        IntentType.COMPLAINT,
        IntentType.COOPERATION_INTENT,
        IntentType.PURCHASE_INTENT,
        IntentType.CONTACT_INQUIRY
    }

    @staticmethod
    def _get_domain_non_math_patterns(enterprise_id: str = "") -> List[str]:
        try:
            from src.common.industry_schema_service import get_industry_schema_service
            schema = get_industry_schema_service().get_active_schema(enterprise_id=enterprise_id) or {}
            metadata = schema.get("metadata") or {}
            qu_config = metadata.get("query_understanding") or {}
            return qu_config.get("domain_keywords") or []
        except Exception:
            return []

    def __init__(self):
        """初始化增强客服服务"""
        from .llm_service import get_default_llm_provider

        self._learning_knowledge_base = get_learning_knowledge_base_adapter()
        self.knowledge_base = self._learning_knowledge_base
        self._retrieval_knowledge_base = None
        self._guardrail_stage = GuardrailStage()
        self._generation_stage = GenerationStage()
        self._retrieval_stage = RetrievalStage()
        default_llm_provider = get_default_llm_provider()
        self.intent_recognizer = EnhancedIntentRecognizer(llm_service=default_llm_provider)
        self._intent_cache = IntentCache(max_size=500, ttl=180)
        self._learning_system = get_self_learning_rag_system(self._learning_knowledge_base)
        self._reply_orchestrator = ReplyOrchestrator()
        self._dialogue_manager = MultiTurnDialogueManager()
        # 修复 R4：AdvancedQueryPreprocessor 是死代码，已删除；查询改写由 ContextAwareQueryRewriter 处理
        self._rag_evaluator = RAGASEvaluator()
        self._proactive_engine = ProactiveServiceEngine()
        self._context_understanding = context_understanding_module

        # 新增：答案重组模块
        self._answer_reorganizer = None  # 延迟初始化，需要 LLM
        
        # 新增：智能学习引擎
        from .intelligent_learning_engine import get_learning_engine
        self._learning_engine = get_learning_engine(self._learning_knowledge_base)
        
        self._knowledge_graph = get_knowledge_graph_service()
        self._kg_initialized = False
        self._query_rewriter_lock = threading.Lock()
        self._answer_reorganizer_lock = threading.Lock()
        self._kg_lock = threading.Lock()

        # 新增：BERT意图识别器（高置信度时快速命中，低置信度回退到 LLM 语义识别）
        self._bert_recognizer = get_bert_intent_recognizer(
            llm_service=default_llm_provider,
        )

        # 新增：持久化记忆服务
        self._memory_service = get_memory_service()

        # 新增：上下文查询改写器和指代消解器（延迟初始化，需要LLM）
        self._query_rewriter = None
        self._coreference_resolver = None
        self._routing_stats = self._build_empty_routing_stats()
        self._mainline_observability_stats = self._build_empty_mainline_observability_stats()

    @staticmethod
    def _build_empty_routing_stats() -> Dict[str, Any]:
        return {
            "main_routes": {},
            "execution_routes": {},
            "execution_executors": {},
            "handoff_reasons": {},
        }

    @staticmethod
    def _build_empty_mainline_observability_stats() -> Dict[str, Any]:
        return {
            "total_requests": 0,
            "modes": {},
            "final_actions": {},
            "reason_codes": {},
            "reply_sources": {},
            "fallback_sources": {},
            "generation_sources": {},
            "samples": [],
            "by_enterprise": {},
            "by_intent": {},
        }

    @staticmethod
    def _get_industry_strategy(enterprise_id: str = ""):
        return get_active_industry_strategy(enterprise_id=enterprise_id)

    @staticmethod
    def _should_disable_heuristic_domain_followup(enterprise_id: str = "") -> bool:
        """显式 generic schema 且未提供有效 schema 明细时，禁用行业短句跟进启发式改写。"""
        try:
            import src.common.industry_strategies as strategy_module

            active_schema = get_active_schema_with_compat(
                strategy_module.get_industry_schema_service(),
                enterprise_id=enterprise_id,
            )
        except Exception:
            return False

        schema_id = str(active_schema.get("schema_id") or "").strip()
        industry_code = str(active_schema.get("industry_code") or "").strip()
        if schema_id != "generic.service_sales" and industry_code != "generic":
            return False

        return not any(
            active_schema.get(key)
            for key in ("display_name", "entity_type", "fields", "metadata", "intent_keywords", "grounded_config")
        )

    def _ensure_mainline_observability_stats(self) -> Dict[str, Any]:
        stats = getattr(self, "_mainline_observability_stats", None)
        if not isinstance(stats, dict):
            stats = self._build_empty_mainline_observability_stats()
            self._mainline_observability_stats = stats
        return stats

    def _get_active_domain_profile(self, enterprise_id: str = ""):
        normalized_enterprise = str(enterprise_id or "").strip()
        if not normalized_enterprise:
            return None
        try:
            return get_domain_profile_service().get_profile(normalized_enterprise)
        except Exception as exc:
            logger.debug(f"读取企业知识画像失败，忽略画像增强: {exc}")
            return None

    @staticmethod
    def _merge_unique_text_items(*collections: Any) -> List[str]:
        merged: List[str] = []
        seen = set()
        for collection in collections:
            for item in list(collection or []):
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
        return merged

    def _get_query_understanding_profile(self, enterprise_id: str = "") -> Dict[str, Any]:
        profile: Dict[str, Any] = {
            "domain_keywords": [],
            "business_intro_terms": [],
            "business_intro_patterns": [],
            "business_intro_exact_phrases": [],
            "followup_terms": [],
            "contextual_opening_guidance": [],
        }
        sources: List[Dict[str, Any]] = []
        try:
            active_schema = get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterprise_id,
            )
            metadata = active_schema.get("metadata") or {}
            schema_profile = metadata.get("query_understanding") or {}
            if isinstance(schema_profile, dict):
                sources.append(schema_profile)
        except Exception as exc:
            logger.debug(f"读取 Schema query_understanding 配置失败: {exc}")

        domain_profile = self._get_active_domain_profile(enterprise_id)
        if domain_profile:
            profile_metadata = getattr(domain_profile, "metadata", {}) or {}
            profile_section = profile_metadata.get("query_understanding") or {}
            if isinstance(profile_section, dict):
                sources.append(profile_section)

        for source in sources:
            for key in (
                "domain_keywords",
                "business_intro_terms",
                "business_intro_patterns",
                "business_intro_exact_phrases",
                "followup_terms",
            ):
                profile[key] = self._merge_unique_text_items(profile.get(key), source.get(key))
            guidance_items = source.get("contextual_opening_guidance") or []
            if isinstance(guidance_items, list):
                profile["contextual_opening_guidance"].extend(
                    item for item in guidance_items if isinstance(item, dict)
                )
        return profile

    def _get_profile_domain_keywords(self, enterprise_id: str = "") -> set[str]:
        domain_profile = self._get_active_domain_profile(enterprise_id)
        if not domain_profile:
            return set()

        keywords: set[str] = set()

        def add_terms(values: Any) -> None:
            for raw_value in list(values or []):
                value = str(raw_value or "").strip()
                if len(value) >= 2:
                    keywords.add(value)

        add_terms([getattr(domain_profile, "industry", ""), getattr(domain_profile, "sub_industry", "")])
        for product in list(getattr(domain_profile, "products", []) or [])[:10]:
            add_terms([getattr(product, "name", ""), *list(getattr(product, "aliases", []) or [])[:4]])
            add_terms(list(getattr(product, "features", []) or [])[:4])
            add_terms(list(getattr(product, "scenarios", []) or [])[:4])
        for policy in list(getattr(domain_profile, "policies", []) or [])[:10]:
            add_terms([getattr(policy, "rule_type", ""), getattr(policy, "title", "")])
            add_terms(list(getattr(policy, "conditions", []) or [])[:3])
        for signal in list(getattr(domain_profile, "faq_signals", []) or [])[:10]:
            add_terms(list(getattr(signal, "keywords", []) or [])[:4])
        return keywords

    def _get_active_domain_keywords(self, enterprise_id: str = "") -> set[str]:
        strategy = self._get_industry_strategy(enterprise_id=enterprise_id)
        keywords = set(strategy.get_domain_keywords() or set())
        try:
            active_schema = get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterprise_id,
            )
            metadata = active_schema.get("metadata") or {}
            category_profiles = metadata.get("category_profiles") or {}
            if isinstance(category_profiles, dict):
                for profile in category_profiles.values():
                    if not isinstance(profile, dict):
                        continue
                    for keyword in profile.get("keywords") or []:
                        normalized = str(keyword or "").strip()
                        if normalized:
                            keywords.add(normalized)
        except Exception as exc:
            logger.debug(f"读取行业 Schema 关键词失败，回退通用关键词: {exc}")
        query_profile = self._get_query_understanding_profile(enterprise_id)
        keywords.update(self._merge_unique_text_items(query_profile.get("domain_keywords")))
        keywords.update(self._get_profile_domain_keywords(enterprise_id))
        return keywords or set(self.GENERIC_DOMAIN_KEYWORDS)

    def _ensure_routing_stats(self) -> Dict[str, Any]:
        routing_stats = getattr(self, "_routing_stats", None)
        if not isinstance(routing_stats, dict):
            routing_stats = self._build_empty_routing_stats()
            self._routing_stats = routing_stats
        return routing_stats

    def _perf_logging_enabled(self) -> bool:
        """是否启用回复链性能日志。"""
        return os.getenv("SMART_REPLY_PERF_LOG", "1").lower() not in {"0", "false", "off"}

    def _record_perf_metric(self, metrics: Dict[str, float], stage: str, started_at: float) -> float:
        """记录单阶段耗时，单位毫秒。"""
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        metrics[stage] = elapsed_ms
        return elapsed_ms

    def _log_perf_metrics(self, label: str, metrics: Dict[str, float], **extra_fields: Any) -> None:
        """统一输出性能日志，便于定位慢请求瓶颈。"""
        if not self._perf_logging_enabled():
            return
        metric_parts = [f"{key}={value:.1f}ms" for key, value in metrics.items()]
        extra_parts = [f"{key}={value}" for key, value in extra_fields.items() if value is not None]
        logger.info("PERF %s | %s", label, " | ".join(metric_parts + extra_parts))

    def _build_trace_snapshot(self) -> Dict[str, Any]:
        """获取当前请求链路的最小 tracing 快照。"""
        return build_trace_snapshot()

    def _record_routing_decision(
        self,
        reply_analysis: Optional[Dict[str, Any]],
        *,
        route_name: str,
        reason: str,
        confidence: float = 0.0,
        stage: str = "process_message",
        selected_strategy: str = "",
        fallback_route: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将统一路由决策写入 reply_analysis，便于后续扩展 agent routing。"""
        decision = RoutingDecision(
            route_name=route_name,
            reason=reason,
            confidence=confidence,
            stage=stage,
            selected_strategy=selected_strategy,
            fallback_route=fallback_route,
            metadata=metadata or {},
        )
        if reply_analysis is None:
            reply_analysis = {}
        route_bucket = self._ensure_routing_stats().setdefault("main_routes", {})
        route_bucket[route_name] = int(route_bucket.get(route_name, 0) or 0) + 1
        return attach_routing_decision(reply_analysis, decision)

    def _append_trace_span(
        self,
        reply_analysis: Optional[Dict[str, Any]],
        *,
        name: str,
        duration_ms: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """把阶段级 span 追加到 reply_analysis.trace.spans。"""
        if reply_analysis is None:
            reply_analysis = {}
        return append_trace_span(
            reply_analysis,
            name=name,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    def _record_execution_routing(
        self,
        reply_analysis: Optional[Dict[str, Any]],
        *,
        route_name: str,
        reason: str,
        confidence: float = 0.0,
        stage: str = "reply_execution",
        selected_strategy: str = "",
        fallback_route: str = "",
        executor: str = "",
        handoff_reason: str = "",
        route_scores: Optional[Dict[str, float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将执行层 agent routing 写入 reply_analysis.routing.execution。"""
        decision = RoutingDecision(
            route_name=route_name,
            reason=reason,
            confidence=confidence,
            stage=stage,
            selected_strategy=selected_strategy,
            fallback_route=fallback_route,
            executor=executor,
            handoff_reason=handoff_reason,
            route_scores=route_scores or {},
            metadata=metadata or {},
        )
        if reply_analysis is None:
            reply_analysis = {}
        routing_stats = self._ensure_routing_stats()
        execution_bucket = routing_stats.setdefault("execution_routes", {})
        execution_bucket[route_name] = int(execution_bucket.get(route_name, 0) or 0) + 1
        if executor:
            executor_bucket = routing_stats.setdefault("execution_executors", {})
            executor_bucket[executor] = int(executor_bucket.get(executor, 0) or 0) + 1
        if handoff_reason:
            handoff_bucket = routing_stats.setdefault("handoff_reasons", {})
            handoff_bucket[handoff_reason] = int(handoff_bucket.get(handoff_reason, 0) or 0) + 1
        return attach_execution_routing(reply_analysis, decision)

    def get_routing_statistics(self) -> Dict[str, Any]:
        """返回主回复链 routing/execution routing 聚合统计。"""
        routing_stats = self._ensure_routing_stats()
        main_routes = dict(routing_stats.get("main_routes", {}) or {})
        execution_routes = dict(routing_stats.get("execution_routes", {}) or {})
        execution_executors = dict(routing_stats.get("execution_executors", {}) or {})
        handoff_reasons = dict(routing_stats.get("handoff_reasons", {}) or {})
        total_main = sum(int(value or 0) for value in main_routes.values())
        total_execution = sum(int(value or 0) for value in execution_routes.values())
        return {
            "main_routes": main_routes,
            "execution_routes": execution_routes,
            "execution_executors": execution_executors,
            "handoff_reasons": handoff_reasons,
            "total_main_routes": total_main,
            "total_execution_routes": total_execution,
        }

    def _record_mainline_observability_summary(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        stats = self._ensure_mainline_observability_stats()
        return update_mainline_observability_stats(stats, summary or {})

    def get_mainline_observability_statistics(self) -> Dict[str, Any]:
        stats = self._ensure_mainline_observability_stats()
        return copy.deepcopy(stats)

    def export_mainline_observability_samples(self, limit: int = 50) -> List[Dict[str, Any]]:
        stats = self._ensure_mainline_observability_stats()
        samples = list(stats.get("samples", []) or [])
        limit = max(1, int(limit or 50))
        return copy.deepcopy(samples[-limit:])

    def build_minimum_rag_evaluation_dataset(self, limit: int = 50) -> List[Dict[str, Any]]:
        dataset: List[Dict[str, Any]] = []
        for sample in self.export_mainline_observability_samples(limit=limit):
            top_hits = list(sample.get("top_hits", []) or [])
            dataset.append(
                {
                    "query": str(sample.get("retrieval_query") or ""),
                    "enterprise_id": str(sample.get("enterprise_id") or "default"),
                    "intent": str(sample.get("intent") or ""),
                    "reason_code": str(sample.get("reason_code") or ""),
                    "reply_source": str(sample.get("reply_source") or "unknown"),
                    "retrieval_final_action": str(sample.get("retrieval_final_action") or ""),
                    "need_human": bool(sample.get("need_human")),
                    "matched_knowledge": str(sample.get("matched_knowledge") or ""),
                    "contexts": [str(hit.get("question") or "") for hit in top_hits if str(hit.get("question") or "").strip()],
                    "top_hits": copy.deepcopy(top_hits),
                }
            )
        return dataset

    def build_structured_rag_evaluation_dataset(self, limit: int = 50) -> List[Dict[str, Any]]:
        dataset: List[Dict[str, Any]] = []
        samples = self.export_mainline_observability_samples(limit=limit)
        for index, sample in enumerate(samples, start=1):
            top_hits = copy.deepcopy(list(sample.get("top_hits", []) or []))
            contexts = [str(hit.get("question") or "") for hit in top_hits if str(hit.get("question") or "").strip()]
            sample_id = str(sample.get("sample_id") or f"mainline-{index:04d}")
            dataset.append(
                {
                    "sample_id": sample_id,
                    "query": str(sample.get("retrieval_query") or ""),
                    "expected": {
                        "need_human": bool(sample.get("need_human")),
                        "reason_code": str(sample.get("reason_code") or ""),
                        "reply_source": str(sample.get("reply_source") or "unknown"),
                        "retrieval_final_action": str(sample.get("retrieval_final_action") or ""),
                    },
                    "retrieval": {
                        "contexts": contexts,
                        "top_hits": top_hits,
                        "top_hit_count": len(top_hits),
                        "context_count": len(contexts),
                    },
                    "metadata": {
                        "enterprise_id": str(sample.get("enterprise_id") or "default"),
                        "intent": str(sample.get("intent") or ""),
                        "matched_knowledge": str(sample.get("matched_knowledge") or ""),
                    },
                    "labels": {
                        "grounded": None,
                        "helpful": None,
                        "correct_route": None,
                    },
                }
            )
        return dataset

    def build_rag_evaluation_report(self, limit: int = 50) -> Dict[str, Any]:
        dataset = self.build_structured_rag_evaluation_dataset(limit=limit)
        reason_code_distribution: Dict[str, int] = {}
        reply_source_distribution: Dict[str, int] = {}
        intent_distribution: Dict[str, int] = {}
        contexts_total = 0
        top_hits_total = 0
        need_human_count = 0
        no_answer_count = 0

        for item in dataset:
            expected = item.get("expected", {}) or {}
            retrieval = item.get("retrieval", {}) or {}
            metadata = item.get("metadata", {}) or {}
            reason_code = str(expected.get("reason_code") or "unknown")
            reply_source = str(expected.get("reply_source") or "unknown")
            intent = str(metadata.get("intent") or "unknown")
            reason_code_distribution[reason_code] = reason_code_distribution.get(reason_code, 0) + 1
            reply_source_distribution[reply_source] = reply_source_distribution.get(reply_source, 0) + 1
            intent_distribution[intent] = intent_distribution.get(intent, 0) + 1
            contexts_total += int(retrieval.get("context_count") or 0)
            top_hits_total += int(retrieval.get("top_hit_count") or 0)
            need_human_count += 1 if expected.get("need_human") else 0
            no_answer_count += 1 if reason_code.startswith("no_answer") else 0

        total = len(dataset)
        return {
            "total": total,
            "need_human_rate": round(need_human_count / total, 4) if total else 0.0,
            "no_answer_rate": round(no_answer_count / total, 4) if total else 0.0,
            "avg_context_count": round(contexts_total / total, 3) if total else 0.0,
            "avg_top_hit_count": round(top_hits_total / total, 3) if total else 0.0,
            "reason_code_distribution": reason_code_distribution,
            "reply_source_distribution": reply_source_distribution,
            "intent_distribution": intent_distribution,
        }

    def _attach_reply_execution_routing(
        self,
        *,
        reply_analysis: Optional[Dict[str, Any]],
        main_decision: RoutingDecision,
        enhanced_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """为主回复链补充最终执行层 agent routing。"""
        payload = reply_analysis or {}
        retrieval_state = payload.get("retrieval", {}) if isinstance(payload.get("retrieval"), dict) else {}
        rag_state = payload.get("rag_llm", {}) if isinstance(payload.get("rag_llm"), dict) else {}
        fallback_state = payload.get("fallback", {}) if isinstance(payload.get("fallback"), dict) else {}
        matched_knowledge = bool(
            enhanced_result and enhanced_result.get("matched_knowledge") and enhanced_result.get("matched_knowledge") != "LLM生成"
        )
        reply_source = str(rag_state.get("source") or fallback_state.get("source") or "")
        used_generated_reply = bool(rag_state) or reply_source.startswith("rag_llm")
        decision = decide_reply_execution_route(
            main_route=main_decision.route_name,
            retrieval_final_action=str(retrieval_state.get("final_action") or ""),
            retrieved_count=int(retrieval_state.get("result_count") or retrieval_state.get("after_crag_count") or 0),
            reply_source=reply_source,
            matched_knowledge=matched_knowledge,
            used_generated_reply=used_generated_reply,
            need_human=bool(enhanced_result and enhanced_result.get("need_human")),
            metadata={
                "matched_knowledge_name": enhanced_result.get("matched_knowledge", "") if enhanced_result else "",
            },
        )
        self._record_execution_routing(
            payload,
            route_name=decision.route_name,
            reason=decision.reason,
            confidence=decision.confidence,
            stage=decision.stage,
            selected_strategy=decision.selected_strategy,
            fallback_route=decision.fallback_route,
            executor=decision.executor,
            handoff_reason=decision.handoff_reason,
            route_scores=decision.route_scores,
            metadata=decision.metadata,
        )
        orchestration_plan = get_agent_orchestrator().plan_reply_chain(
            main_decision=main_decision,
            execution_decision=decision,
            metadata={
                "reply_source": reply_source,
                "matched_knowledge": matched_knowledge,
            },
        )
        attach_orchestration_plan(payload, orchestration_plan)
        self._append_trace_span(
            payload,
            name="reply_execution_route",
            duration_ms=0.1,
            metadata={
                "route_name": decision.route_name,
                "executor": decision.executor,
                "handoff_reason": decision.handoff_reason,
                "orchestrator": orchestration_plan.get("orchestrator", ""),
            },
        )
        return payload

    def _build_base_result(
        self,
        message: str,
        customer_name: str,
        conversation_history: List[Dict],
        customer_data: Dict,
        intent_result,
    ) -> Dict[str, Any]:
        """构造纯基线结果，不在此阶段执行任何检索或 CRAG 判定。"""
        reply = self._generate_base_reply(message, customer_name, intent_result, conversation_history, customer_data)

        return {
            "reply": reply,
            "intent": intent_result.primaryIntent.value,
            "intent_level": self._map_intent_to_level(intent_result.primaryIntent),
            "intent_score": intent_result.confidence,
            "intent_signals": list(intent_result.keywords or []),
            "intent_barriers": [],
            "suggested_action": "正常跟进",
            "matched_knowledge": None,
            "need_human": intent_result.primaryIntent == IntentType.COMPLAINT,
            "confidence": intent_result.confidence,
            "source": "enhanced_base",
            "sentiment": intent_result.sentiment.value,
            "urgency": intent_result.urgency.value,
        }

    @staticmethod
    def _build_history_signature(conversation_history: Optional[List[Dict]], limit: int = 4) -> str:
        """构建用于缓存隔离的轻量上下文签名。"""
        if not conversation_history:
            return ""

        parts: List[str] = []
        for msg in conversation_history[-limit:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or msg.get("direction") or "")
            content = str(msg.get("content") or "").strip()
            if content:
                parts.append(f"{role}:{content[:80]}")
        return " || ".join(parts)

    @staticmethod
    def _looks_like_misclassified_assistant_message(content: str) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return True

        assistant_markers = [
            "期待为您服务",
            "有需要随时找我",
            "还有什么需要帮助",
            "对公转账需要",
            "转账凭证序号",
            "方便的话留个接收资料的联系方式",
            "方便的话也可以留个接收资料的联系方式",
            "更完整的资料、详细说明",
            "继续为您处理",
            "我把安排说明发您",
            "我把完整说明发您",
        ]
        if any(marker.lower() in text for marker in assistant_markers):
            return True
        if text.startswith(("亲爱的", "您好", "您好！")) and ("服务" in text or "发您" in text):
            return True
        return False

    @staticmethod
    def _extract_consistent_history_field(
        conversation_history: Optional[List[Dict]],
        field_names: Tuple[str, ...],
        conversation_id: str = "",
    ) -> str:
        values = set()
        for msg in conversation_history or []:
            if not isinstance(msg, dict):
                continue
            metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
            message_conversation_id = str(
                msg.get("conversation_id") or metadata.get("conversation_id") or ""
            ).strip()
            if conversation_id and message_conversation_id and message_conversation_id != conversation_id:
                continue
            for field_name in field_names:
                value = str(msg.get(field_name) or metadata.get(field_name) or "").strip()
                if value:
                    values.add(value)
        return next(iter(values)) if len(values) == 1 else ""

    @classmethod
    def _resolve_enterprise_id(
        cls,
        customer_data: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict]] = None,
    ) -> str:
        customer_payload = customer_data or {}
        candidate_fields = ("enterprise_id", "enterpriseId", "tenant_id", "tenantId")
        for field_name in candidate_fields:
            value = str(customer_payload.get(field_name) or "").strip()
            if value:
                return value

        metadata = customer_payload.get("metadata") if isinstance(customer_payload.get("metadata"), dict) else {}
        for field_name in candidate_fields:
            value = str(metadata.get(field_name) or "").strip()
            if value:
                return value

        return cls._extract_consistent_history_field(
            conversation_history,
            candidate_fields,
        )

    def _build_safe_identity(
        self,
        *,
        message: str,
        customer_name: str,
        customer_data: Dict[str, Any],
        conversation_history: Optional[List[Dict]],
        provided_session_id: Optional[str],
    ) -> Tuple[str, str, str]:
        platform = str(customer_data.get("platform") or "").strip() or "unknown"
        conversation_id = str(customer_data.get("conversation_id") or "").strip()
        history_customer_id = self._extract_consistent_history_field(
            conversation_history,
            ("customer_id", "sec_uid", "user_id"),
            conversation_id=conversation_id,
        )
        customer_id = (
            str(customer_data.get("sec_uid") or customer_data.get("customer_id") or "").strip()
            or history_customer_id
        )

        session_id = resolve_context_session_id(
            platform=platform,
            customer_id=customer_id,
            provided_session_id=provided_session_id,
            conversation_history=conversation_history,
        )
        if not session_id:
            # 匿名且无稳定标识时宁可新建隔离会话，也不要按消息内容复用上下文。
            session_id = f"{platform}_anon_{uuid.uuid4().hex[:12]}"

        if not customer_id:
            customer_id = f"anon_{session_id.split('_anon_', 1)[-1]}"
        return customer_id, platform, session_id

    @staticmethod
    def _history_message_matches_context(
        message: Dict[str, Any],
        *,
        session_id: str,
        customer_id: str,
        platform: str,
        conversation_id: str = "",
    ) -> bool:
        if not isinstance(message, dict):
            return False

        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        message_conversation_id = str(
            message.get("conversation_id") or metadata.get("conversation_id") or ""
        ).strip()
        if message_conversation_id and conversation_id and message_conversation_id != conversation_id:
            return False

        message_session_id = str(message.get("session_id") or metadata.get("session_id") or "").strip()
        if message_session_id and session_id and message_session_id != session_id:
            return False

        candidate_customer_ids = [
            str(message.get("customer_id") or "").strip(),
            str(message.get("sec_uid") or "").strip(),
            str(metadata.get("customer_id") or "").strip(),
            str(metadata.get("sec_uid") or "").strip(),
        ]
        candidate_customer_ids = [value for value in candidate_customer_ids if value]
        if candidate_customer_ids and customer_id and all(value != customer_id for value in candidate_customer_ids):
            return False

        message_platform = str(message.get("platform") or metadata.get("platform") or "").strip()
        if message_platform and platform and message_platform != platform:
            return False

        return True

    def _sanitize_conversation_history(
        self,
        conversation_history: Optional[List[Dict]],
        *,
        session_id: str = "",
        customer_id: str = "",
        platform: str = "",
        conversation_id: str = "",
        max_idle_hours: float = 24.0,
    ) -> List[Dict]:
        """清洗上下文，过滤误标助手话术，并拦截跨用户/跨会话消息混入。"""
        if not conversation_history:
            return []

        # 获取当前参考时间（用于判定过往会话是否失效）
        now = datetime.now()
        sanitized: List[Dict] = []
        
        # 倒序查找，一旦发现时间跨度过大的消息，则截断更早的历史
        # 确保对话上下文始终是“新鲜”的，避免数天前的历史污染当前意图识别
        valid_messages: List[Dict] = []
        last_msg_time = None

        for msg in reversed(conversation_history):
            if not isinstance(msg, dict):
                continue
            
            # 1. 基础上下文匹配过滤
            if (session_id or customer_id or platform) and not self._history_message_matches_context(
                msg,
                session_id=session_id,
                customer_id=customer_id,
                platform=platform,
                conversation_id=conversation_id,
            ):
                continue

            # 2. 时间有效性检查 (Session Expiration)
            msg_time_str = msg.get("created_at") or msg.get("time")
            if msg_time_str:
                try:
                    msg_time = datetime.fromisoformat(str(msg_time_str).replace('Z', '+00:00'))
                    # 如果消息早于当前 24 小时，视为上一个旧会话，不再作为多轮上下文
                    if (now - msg_time).total_seconds() > max_idle_hours * 3600:
                        logger.debug(f"消息已过期 ({msg_time_str}), 截断更早历史")
                        break
                except (ValueError, TypeError):
                    pass

            # 3. 助手误标过滤
            content = str(msg.get("content") or "").strip()
            if not content:
                continue

            direction = str(msg.get("direction") or "").strip().lower()
            if direction != "outbound" and self._looks_like_misclassified_assistant_message(content):
                continue

            valid_messages.append(msg)
        
        # 恢复顺序
        return list(reversed(valid_messages))

    def _trim_history_for_business_intro(self, conversation_history: Optional[List[Dict]]) -> List[Dict]:
        """业务范围类开场不再清空历史，只保留最近关键轮次与 grounded 上下文。"""
        history = [msg for msg in (conversation_history or []) if isinstance(msg, dict)]
        if not history:
            return []

        grounded_messages = [
            dict(msg)
            for msg in history
            if str(msg.get("source") or "").strip() == "grounded_context"
        ]
        recent_messages = [
            dict(msg)
            for msg in history[-4:]
            if str(msg.get("content") or "").strip()
        ]

        trimmed: List[Dict] = []
        seen_keys = set()
        for msg in grounded_messages + recent_messages:
            key = (
                str(msg.get("direction") or msg.get("role") or ""),
                str(msg.get("content") or ""),
                str(msg.get("created_at") or msg.get("timestamp") or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            trimmed.append(msg)
        return trimmed


    def _should_enable_lightweight_preanalysis(
        self,
        message: str,
        conversation_history: Optional[List[Dict]],
    ) -> bool:
        """简单单轮消息走轻量预分析，跳过昂贵的改写/消解链。"""
        text = self._normalize_query_text(message)
        if not text:
            return False
        if conversation_history:
            return False
        if len(text) > 24:
            return False
        if any(op in text for op in ("+", "-", "*", "/", "加", "减", "乘", "除", "=")):
            return False
        return True

    @staticmethod
    def _contains_contact_guidance(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        markers = (
            "联系方式",
            "接收资料",
            "留个微信",
            "微信号",
            "方便联系",
            "加微",
            "查余位",
            "预留",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _extract_user_contact_info(text: str) -> Optional[str]:
        """从文本中提取用户提供的联系方式（手机号或微信号格式）"""
        if not text:
            return None
        normalized_text = str(text).strip()

        # 1. 手机号匹配（支持空格/横杠分隔）- 高置信度
        phone_match = re.search(r'1[3-9][\s-]*\d[\d\s-]{8,}', normalized_text)
        if phone_match:
            digits_only = re.sub(r"\D", "", phone_match.group())
            if re.fullmatch(r"1[3-9]\d{9}", digits_only):
                return digits_only

        # 1.1 脱敏手机号（如 5179****9438 / 138****5678）
        masked_phone_match = re.search(r'(?:1\d{2}|\d{4})\*{3,4}\d{4}', normalized_text)
        if masked_phone_match:
            return masked_phone_match.group()

        # 2. 带有显式标识的微信号
        wechat_markers = (r'微信', r'加我', r'联系', r'vx', r'wx', r'v信')
        for marker in wechat_markers:
            # 匹配 marker 后的字母数字串
            match = re.search(f'{marker}[:：\\s]*([a-zA-Z][-_a-zA-Z0-9]{{5,19}})', normalized_text, re.IGNORECASE)
            if match:
                return match.group(1)

        # 3. 邮箱匹配
        email_match = re.search(r'[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}', normalized_text)
        if email_match:
            return email_match.group()
                
        return None

    def _should_append_route_contact_guidance(
        self,
        *,
        message: str,
        reply: str,
        conversation_history: Optional[List[Dict]],
        matched_knowledge,
    ) -> bool:
        matched_question = ""
        if matched_knowledge and getattr(matched_knowledge, "question", ""):
            matched_question = str(getattr(matched_knowledge, "question", "")).strip()
        return get_sales_followup_service().should_append_contact_guidance(
            message=message,
            reply=reply,
            conversation_history=conversation_history,
            matched_question=matched_question,
            looks_like_misclassified_assistant_message=self._looks_like_misclassified_assistant_message,
        )

    def _append_route_contact_guidance(
        self,
        reply: str,
        *,
        message: str,
        conversation_history: Optional[List[Dict]],
        matched_knowledge,
        customer_name: str = "",
    ) -> str:
        matched_question = ""
        if matched_knowledge and getattr(matched_knowledge, "question", ""):
            matched_question = str(getattr(matched_knowledge, "question", "")).strip()
        return get_sales_followup_service().append_contact_guidance(
            reply,
            message=message,
            conversation_history=conversation_history,
            matched_question=matched_question,
            customer_name=customer_name,
            extract_contact_info=self._extract_user_contact_info,
            looks_like_misclassified_assistant_message=self._looks_like_misclassified_assistant_message,
        )

    def _repair_route_overview_reply(
        self,
        reply: str,
        *,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
    ) -> str:
        resolved_reply = str(reply or "").strip()
        if not resolved_reply:
            return ""

        normalized_message = self._normalize_query_text(self._strip_preview_noise_prefix(message or ""))
        is_broad_domain_opening = self._is_broad_domain_opening(normalized_message)
        if not is_broad_domain_opening:
            return resolved_reply

        domain_anchor_terms = self._get_domain_anchor_terms()
        if not domain_anchor_terms:
            return resolved_reply

        primary_anchor = domain_anchor_terms[0] if domain_anchor_terms else ""
        if primary_anchor and primary_anchor in resolved_reply:
            return resolved_reply

        history_text = " ".join(
            str((msg or {}).get("content") or "")
            for msg in (conversation_history or [])
            if isinstance(msg, dict)
        )
        has_anchor_split = (
            primary_anchor and primary_anchor in history_text
            and any(term in history_text for term in domain_anchor_terms[1:])
        )
        if not has_anchor_split:
            return resolved_reply

        return resolved_reply

    def _select_best_faq_subanswer(self, message: str, answer: str) -> str:
        pairs = _KB_FAQ_PAIR_RE.findall(answer or "")
        if not pairs:
            return answer

        message_terms = {
            token for token in re.split(r"[，,、/（）()·\s]+", (message or "").lower())
            if len(token.strip()) >= 2
        }

        best_answer = ""
        best_score = -1
        for sub_question, sub_answer in pairs:
            normalized_question = (sub_question or "").lower()
            score = 0
            for term in message_terms:
                if term and term in normalized_question:
                    score += 1
            if score > best_score:
                best_score = score
                best_answer = sub_answer

        return best_answer or pairs[0][1]

    def _sanitize_knowledge_answer_for_customer(self, message: str, answer: str) -> str:
        text = str(answer or "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\ufeff", "").replace("\u200b", "").replace("\ufffc", "")

        cleaned_lines: List[str] = []
        for raw_line in text.split("\n"):
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            if line in {"已读", "未读"}:
                continue
            if line.startswith("产品：") and _KB_FAQ_PAIR_RE.search(text):
                continue
            cleaned_lines.append(line)

        cleaned = "\n".join(cleaned_lines).strip()
        if _KB_FAQ_PAIR_RE.search(cleaned):
            cleaned = self._select_best_faq_subanswer(message, cleaned)

        cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
        return cleaned or str(answer or "").strip()

    def _append_bundle_contexts(
        self,
        retrieved_contexts: List[Dict[str, Any]],
        evidence_bundle: Any,
        message: str = "",
    ) -> List[Dict[str, Any]]:
        if not evidence_bundle:
            return list(retrieved_contexts or [])

        prompt_text = ""
        metadata: Dict[str, Any] = {}
        if hasattr(evidence_bundle, "to_prompt_text"):
            prompt_text = evidence_bundle.to_prompt_text()
        if hasattr(evidence_bundle, "to_metadata"):
            metadata = evidence_bundle.to_metadata()
        answer_plan = self._build_bundle_answer_plan(message, evidence_bundle)
        if answer_plan:
            plan_text = self._format_bundle_answer_plan(answer_plan)
            if plan_text:
                prompt_text = f"{plan_text}\n\n{prompt_text}" if prompt_text else plan_text
                metadata["answer_plan"] = answer_plan
        if not prompt_text:
            return list(retrieved_contexts or [])

        contexts = list(retrieved_contexts or [])
        # 实体标签由 schema 提供（如旅游为"线路"），默认"项目"，避免硬编码旅游术语
        entity_label = str(self._get_active_schema_profile().get("entity_type_label") or "项目")
        contexts.insert(
            0,
            {
                "question": f"结构化证据包：{'、'.join(metadata.get('routes', [])) or f'未识别{entity_label}'}",
                "answer": prompt_text,
                "category": "bundle",
                "score": 999.0,
                "source": "knowledge_bundle",
                "retrieval_method": "unified_knowledge_service.search_evidence_bundle",
                "bundle_metadata": metadata,
                "bundle_routes": list(getattr(evidence_bundle, "routes", []) or []),
                "bundle_route_facts": dict(getattr(evidence_bundle, "route_facts", {}) or {}),
                "bundle_route_evidence": self._serialize_bundle_route_evidence(evidence_bundle),
            },
        )
        return contexts

    @staticmethod
    def _serialize_bundle_route_evidence(evidence_bundle: Any) -> Dict[str, Any]:
        serialized: Dict[str, Any] = {}
        route_evidence = dict(getattr(evidence_bundle, "route_evidence", {}) or {})
        for route_name, category_map in route_evidence.items():
            route_key = str(route_name or "").strip()
            if not route_key or not isinstance(category_map, dict):
                continue
            route_payload: Dict[str, Any] = {}
            for category, item in category_map.items():
                category_key = str(category or "").strip()
                if not category_key or item is None:
                    continue
                route_payload[category_key] = {
                    "question": str(getattr(item, "question", "") or "").strip(),
                    "answer": str(getattr(item, "answer", "") or "").strip(),
                }
            if route_payload:
                serialized[route_key] = route_payload
        return serialized

    @staticmethod
    def _extract_plan_price_amount(value: Any) -> Optional[int]:
        match = re.search(r"(\d+)", str(value or ""))
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _comparison_focuses_for_plan(message: str) -> List[str]:
        text = str(message or "")
        focuses: List[str] = []
        rules = [
            ("elder", ("老人", "长辈", "腿脚", "小孩", "孩子")),
            ("budget", ("预算", "便宜", "划算", "性价比", "贵", "价格", "多少钱")),
            ("first_time", ("第一次来", "第一次去", "首次")),
            ("shopping", ("购物", "纯玩", "进店", "超市停留")),
            ("light_pace", ("轻松", "不想太累", "省心", "稳", "体力")),
        ]
        for key, terms in rules:
            if any(term in text for term in terms):
                focuses.append(key)
        return focuses

    @staticmethod
    def _map_schema_field_to_plan_key(field_name: str) -> str:
        normalized = str(field_name or "").strip().lower()
        mapping = {
            "name": "canonical_name",
            "route_name": "canonical_name",
            "price": "price",
            "tuition": "price",
            "audience": "suitable_for",
            "features": "highlights",
            "details": "highlights",
            "schedule": "highlights",
            "inclusions": "includes",
            "materials": "includes",
        }
        return mapping.get(normalized, "")

    @staticmethod
    def _format_plan_fact_value(value: Any) -> str:
        if isinstance(value, list):
            items = [str(item or "").strip() for item in value if str(item or "").strip()]
            return "、".join(items[:3])
        return str(value or "").strip()

    @staticmethod
    def _map_schema_field_to_route_fact_key(field_name: str) -> str:
        normalized = str(field_name or "").strip().lower()
        mapping = {
            "name": "canonical_name",
            "route_name": "canonical_name",
            "price": "price",
            "tuition": "price",
            "audience": "suitable_for",
            "inclusions": "includes",
            "materials": "includes",
            "features": "highlights",
            "details": "highlights",
            "shopping": "shopping",
            "pace": "pace",
        }
        return mapping.get(normalized, "")

    def _build_schema_grounded_reason_points(
        self,
        selected_routes: List[str],
        route_facts: Dict[str, Any],
        comparison_fields: List[str],
        field_labels: Dict[str, str],
    ) -> List[str]:
        configured_fields = [str(name or "").strip() for name in list(comparison_fields or []) if str(name or "").strip()]
        if not configured_fields or not selected_routes:
            return []

        reason_points: List[str] = []
        if len(selected_routes) >= 2:
            route_a, route_b = selected_routes[:2]
            facts_a = dict(route_facts.get(route_a) or {})
            facts_b = dict(route_facts.get(route_b) or {})
            for field_name in configured_fields:
                plan_key = self._map_schema_field_to_plan_key(field_name)
                if not plan_key:
                    continue
                value_a = self._format_plan_fact_value(facts_a.get(plan_key))
                value_b = self._format_plan_fact_value(facts_b.get(plan_key))
                if not value_a or not value_b or value_a == value_b:
                    continue
                label = str(field_labels.get(field_name) or field_name).strip()
                reason_points.append(f"{label}上，{route_a}是{value_a}；{route_b}是{value_b}")
        else:
            route_name = selected_routes[0]
            facts = dict(route_facts.get(route_name) or {})
            for field_name in configured_fields:
                plan_key = self._map_schema_field_to_plan_key(field_name)
                if not plan_key:
                    continue
                value = self._format_plan_fact_value(facts.get(plan_key))
                if not value:
                    continue
                label = str(field_labels.get(field_name) or field_name).strip()
                reason_points.append(f"{label}方面，{route_name}当前可确认的是{value}")
        return reason_points[:3]

    @staticmethod
    def _build_schema_followup_action(
        selected_routes: List[str],
        grounded_config: Dict[str, Any],
        field_labels: Dict[str, str],
    ) -> str:
        bundle_type = str((grounded_config or {}).get("bundle_type") or "").strip().lower()
        followup_fields = [
            str(name or "").strip()
            for name in list((grounded_config or {}).get("followup_fields") or [])
            if str(name or "").strip()
        ]
        labels = [
            str(field_labels.get(name) or name).strip()
            for name in followup_fields
            if str(field_labels.get(name) or name).strip()
        ][:3]
        if not labels:
            if len(selected_routes) >= 2:
                return "先按当前推荐回答，再补问时间、人数和是否有同行限制。"
            return "先答清这个方案的价格、说明和适合对象，再按需补问时间和人数。"

        label_text = "、".join(labels)
        if len(selected_routes) >= 2:
            return f"先按当前推荐回答，再补充{label_text}，再按需确认时间、人数和特殊要求。"
        if bundle_type == "service":
            return f"先答清这个流程的{label_text}，再按需确认办理时间和当前进度。"
        if bundle_type == "entity":
            return f"先答清这个对象的{label_text}，再按需补问时间和人数。"
        return f"先答清这条方案的{label_text}，再按需补问时间和人数。"

    def _build_schema_single_route_detail_points(
        self,
        *,
        route_name: str,
        route_fact: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_answer: str,
        answer_plan: Dict[str, Any],
        topic: str,
    ) -> List[str]:
        configured_fields = [
            str(name or "").strip()
            for name in list(answer_plan.get("configured_followup_fields") or [])
            if str(name or "").strip()
        ]
        if not configured_fields:
            return []

        field_labels = dict(answer_plan.get("field_labels") or {})
        covered_fields = {
            "inclusion": {"price", "inclusions", "materials"},
            "weather": set(),
            "signup": {"schedule"},
            "solo": {"audience"},
        }.get(topic, set())

        evidence_text = " ".join(
            str((route_evidence.get(category) or {}).get("answer") or "").strip()
            for category in ("price", "product", "service", "itinerary", "tips")
        )
        evidence_text = " ".join(part for part in (evidence_text, matched_answer) if part).strip()

        detail_points: List[str] = []
        for field_name in configured_fields:
            normalized_field = str(field_name or "").strip().lower()
            if not normalized_field or normalized_field in covered_fields:
                continue
            label = str(field_labels.get(field_name) or field_name).strip()
            fact_key = self._map_schema_field_to_route_fact_key(field_name)
            fact_value = self._format_plan_fact_value(route_fact.get(fact_key)) if fact_key else ""

            if normalized_field in {"price", "tuition"} and fact_value:
                detail_points.append(f"{label}这块，目前常见口径是{fact_value}")
                continue
            if normalized_field in {"audience"} and fact_value:
                detail_points.append(f"{route_name}更适合{fact_value}")
                continue
            if normalized_field in {"inclusions", "materials"} and fact_value:
                detail_points.append(f"{label}一般是{fact_value}")
                continue
            if normalized_field in {"features", "details"} and fact_value:
                detail_points.append(f"{label}重点会是{fact_value}")
                continue
            if normalized_field == "shopping" and fact_value:
                detail_points.append(f"购物属性这块，当前是{fact_value}")
                continue
            if normalized_field == "pace" and fact_value:
                detail_points.append(f"整体节奏会更偏{fact_value}")
                continue
            if normalized_field == "pickup":
                pickup_hits = self._extract_sentences_with_keywords(
                    evidence_text,
                    ("集合", "接", "接驳", "出发", "商圈范围内定点", "上门接"),
                )
                if pickup_hits:
                    detail_points.append(f"{label}这块，{pickup_hits[0]}")
                    continue
            if normalized_field == "schedule":
                schedule_hits = self._extract_sentences_with_keywords(
                    evidence_text,
                    ("行程", "当天", "前一天晚上", "通知集合时间", "集合时间", "发团", "班次"),
                )
                if schedule_hits:
                    detail_points.append(f"{label}这块，{schedule_hits[0]}")
                    continue

        deduped: List[str] = []
        for point in detail_points:
            normalized = str(point or "").strip("。；;，, ")
            if not normalized or normalized in deduped:
                continue
            deduped.append(normalized)
        return deduped[:3]

    def _build_bundle_answer_plan(self, message: str, evidence_bundle: Any) -> Dict[str, Any]:
        route_facts = dict(getattr(evidence_bundle, "route_facts", {}) or {})
        routes = list(getattr(evidence_bundle, "routes", []) or [])
        comparison_summary = str(getattr(evidence_bundle, "comparison_summary", "") or "").strip()
        grounded_config = dict(getattr(evidence_bundle, "grounded_config", {}) or {})
        field_labels = dict(getattr(evidence_bundle, "field_labels", {}) or {})
        if not route_facts and not comparison_summary:
            return {}

        selected_routes = [route for route in routes if route in route_facts] or list(route_facts.keys())
        focuses = self._comparison_focuses_for_plan(message)
        answer_mode = "comparison_recommendation" if len(selected_routes) >= 2 else "single_route_grounded"
        recommended_route = ""
        reason_points: List[str] = []

        if len(selected_routes) >= 2:
            if "budget" in focuses:
                priced = []
                for route in selected_routes:
                    amount = self._extract_plan_price_amount(route_facts.get(route, {}).get("price"))
                    if amount is not None:
                        priced.append((amount, route))
                priced.sort()
                if priced:
                    recommended_route = priced[0][1]
                    reason_points.append(f"预算更敏感时优先看{recommended_route}")

            if not recommended_route and any(focus in focuses for focus in ("elder", "light_pace")):
                for route in selected_routes:
                    facts = route_facts.get(route, {})
                    suitable = " ".join(facts.get("suitable_for") or [])
                    pace = str(facts.get("pace") or "")
                    if any(term in suitable for term in ("老人", "小孩")) or any(term in pace for term in ("轻松", "省心")):
                        recommended_route = route
                        reason_points.append(f"带老人或想轻松一点时更稳的是{route}")
                        break

            if not recommended_route and "shopping" in focuses:
                for route in selected_routes:
                    shopping = str(route_facts.get(route, {}).get("shopping") or "")
                    if "无购物" in shopping or "纯玩" in shopping:
                        recommended_route = route
                        reason_points.append(f"介意购物停留时更适合{route}")
                        break

            if not recommended_route and "first_time" in focuses:
                for route in selected_routes:
                    suitable = " ".join(route_facts.get(route, {}).get("suitable_for") or [])
                    if "第一次来重庆" in suitable:
                        recommended_route = route
                        reason_points.append(f"第一次来时可先从{route}看起")
                        break

        elif selected_routes:
            recommended_route = selected_routes[0]
            facts = route_facts.get(recommended_route, {})
            if facts.get("price"):
                reason_points.append(f"已知价格口径是{facts['price']}")
            highlights = "、".join((facts.get("highlights") or [])[:2])
            if highlights:
                reason_points.append(f"核心亮点是{highlights}")

        for point in self._build_schema_grounded_reason_points(
            selected_routes=selected_routes,
            route_facts=route_facts,
            comparison_fields=list(grounded_config.get("comparison_fields") or []),
            field_labels=field_labels,
        ):
            if point not in reason_points:
                reason_points.append(point)

        if comparison_summary:
            for line in comparison_summary.split("\n"):
                text = str(line or "").strip(" -")
                if text and text not in reason_points:
                    reason_points.append(text)

        if recommended_route:
            facts = route_facts.get(recommended_route, {})
            suitable = "、".join((facts.get("suitable_for") or [])[:2])
            if suitable:
                point = f"{recommended_route}更偏向{suitable}"
                if point not in reason_points:
                    reason_points.append(point)

        deduped_reason_points: List[str] = []
        for point in reason_points:
            normalized = str(point or "").strip()
            if not normalized:
                continue
            if any(
                normalized == existing
                or normalized in existing
                or existing in normalized
                for existing in deduped_reason_points
            ):
                continue
            deduped_reason_points.append(normalized)

        followup_action = self._build_schema_followup_action(
            selected_routes=selected_routes,
            grounded_config=grounded_config,
            field_labels=field_labels,
        )

        return {
            "answer_mode": answer_mode,
            "schema_bundle_type": str(grounded_config.get("bundle_type") or "").strip(),
            "configured_comparison_fields": list(grounded_config.get("comparison_fields") or []),
            "configured_followup_fields": list(grounded_config.get("followup_fields") or []),
            "field_labels": field_labels,
            "recommended_route": recommended_route,
            "candidate_routes": selected_routes,
            "reason_points": deduped_reason_points[:4],
            "followup_action": followup_action,
        }

    @staticmethod
    def _format_bundle_answer_plan(answer_plan: Dict[str, Any]) -> str:
        if not answer_plan:
            return ""
        lines = ["答案规划："]
        answer_mode = str(answer_plan.get("answer_mode") or "").strip()
        recommended_route = str(answer_plan.get("recommended_route") or "").strip()
        if answer_mode:
            lines.append(f"- 回答方式：{answer_mode}")
        if recommended_route:
            lines.append(f"- 优先推荐：{recommended_route}")
        for point in list(answer_plan.get("reason_points") or [])[:4]:
            text = str(point or "").strip()
            if text:
                lines.append(f"- 主要理由：{text}")
        followup_action = str(answer_plan.get("followup_action") or "").strip()
        if followup_action:
            lines.append(f"- 下一步：{followup_action}")
        return "\n".join(lines).strip()

    @staticmethod
    def _extract_bundle_answer_plan_context(retrieved_contexts: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        for ctx in retrieved_contexts or []:
            if ctx.get("source") != "knowledge_bundle":
                continue
            metadata = dict(ctx.get("bundle_metadata") or {})
            answer_plan = dict(metadata.get("answer_plan") or {})
            route_facts = dict(ctx.get("bundle_route_facts") or {})
            if answer_plan:
                return answer_plan, route_facts
        return {}, {}

    @staticmethod
    def _extract_bundle_grounded_context(
        retrieved_contexts: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        for ctx in retrieved_contexts or []:
            if ctx.get("source") != "knowledge_bundle":
                continue
            metadata = dict(ctx.get("bundle_metadata") or {})
            answer_plan = dict(metadata.get("answer_plan") or {})
            route_facts = dict(ctx.get("bundle_route_facts") or {})
            route_evidence = dict(ctx.get("bundle_route_evidence") or {})
            if answer_plan:
                return answer_plan, route_facts, route_evidence
            comparison_summary = str(ctx.get("comparison_summary") or metadata.get("comparison_summary") or "").strip()
            reason_points = [
                str(item or "").strip()
                for item in list(metadata.get("reason_points") or [])
                if str(item or "").strip()
            ]
            followup_action = str(metadata.get("followup_action") or "").strip()
            candidate_routes = [
                str(name or "").strip()
                for name in list(ctx.get("bundle_routes") or route_facts.keys() or route_evidence.keys())
                if str(name or "").strip()
            ]
            should_synthesize_plan = bool(
                comparison_summary
                or reason_points
                or followup_action
                or len(candidate_routes) == 1
            )
            if candidate_routes and should_synthesize_plan:
                recommended_route = candidate_routes[0] if len(candidate_routes) == 1 else ""
                synthesized_plan = {
                    "answer_mode": "comparison_recommendation" if len(candidate_routes) >= 2 else "single_route_grounded",
                    "recommended_route": recommended_route,
                    "candidate_routes": candidate_routes,
                    "reason_points": reason_points,
                    "comparison_summary": comparison_summary,
                    "followup_action": followup_action,
                }
                return synthesized_plan, route_facts, route_evidence
            return {}, route_facts, route_evidence
        return {}, {}, {}

    def _looks_like_domain_grounded_context(
        self,
        *,
        message: str = "",
        answer_plan: Optional[Dict[str, Any]] = None,
        route_facts: Optional[Dict[str, Any]] = None,
        route_evidence: Optional[Dict[str, Any]] = None,
        matched_knowledge: Any = None,
    ) -> bool:
        domain_keywords = self._get_industry_strategy().get_domain_keywords()
        if not domain_keywords:
            return False
        text_parts = [str(message or "")]
        if answer_plan:
            text_parts.extend(
                [
                    str(answer_plan.get("recommended_route") or ""),
                    str(answer_plan.get("comparison_summary") or ""),
                ]
            )
        if route_facts:
            text_parts.extend(str(key or "") for key in route_facts.keys())
        if route_evidence:
            text_parts.extend(str(key or "") for key in route_evidence.keys())
        if matched_knowledge is not None:
            text_parts.append(str(getattr(matched_knowledge, "question", "") or ""))
            text_parts.append(str(getattr(matched_knowledge, "answer", "") or ""))
        combined_text = " ".join(part for part in text_parts if part)
        return any(term in combined_text for term in domain_keywords)

    def _resolve_grounded_route_strategy(
        self,
        *,
        message: str = "",
        answer_plan: Optional[Dict[str, Any]] = None,
        route_facts: Optional[Dict[str, Any]] = None,
        route_evidence: Optional[Dict[str, Any]] = None,
        matched_knowledge: Any = None,
    ):
        strategy = self._get_industry_strategy()
        if isinstance(strategy, GenericIndustryStrategy) and self._looks_like_domain_grounded_context(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        ):
            return SchemaDrivenStrategy()
        return strategy

    def _resolve_followup_strategy(
        self,
        *,
        message: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
        grounded_routes: Optional[List[str]] = None,
        enterprise_id: str = "",
    ):
        strategy = self._get_industry_strategy(enterprise_id=enterprise_id)
        if not isinstance(strategy, GenericIndustryStrategy):
            return strategy
        if self._should_disable_heuristic_domain_followup(enterprise_id=enterprise_id):
            return strategy

        text_parts = [str(message or "")]
        text_parts.extend(
            str((msg or {}).get("content") or "")
            for msg in (conversation_history or [])
            if isinstance(msg, dict)
        )

        resolved_grounded_routes = [
            str(route_name or "").strip()
            for route_name in (grounded_routes or [])
            if str(route_name or "").strip()
        ]
        context_understanding = getattr(self, "_context_understanding", None)
        if session_id and context_understanding:
            grounded = context_understanding.get_grounded_knowledge(session_id) or {}
            text_parts.append(str(grounded.get("recommended_route") or ""))
            text_parts.append(str(grounded.get("matched_knowledge") or ""))
            text_parts.append(str(grounded.get("retrieval_query") or ""))
            if not resolved_grounded_routes:
                resolved_grounded_routes = [
                    str(route_name or "").strip()
                    for route_name in (grounded.get("routes") or [])
                    if str(route_name or "").strip()
                ]
        text_parts.extend(resolved_grounded_routes)

        combined_text = " ".join(part for part in text_parts if part)
        schema_strategy = SchemaDrivenStrategy(enterprise_id=enterprise_id)
        schema_terms = set(schema_strategy.get_domain_keywords())
        try:
            schema_terms.update(
                str(item or "").strip()
                for item in (schema_strategy._get_followup_config().get("anchor_terms") or [])
                if str(item or "").strip()
            )
        except Exception:
            pass
        if self._looks_like_domain_grounded_context(message=combined_text) or any(
            term in combined_text for term in schema_terms
        ):
            return schema_strategy
        return strategy

    def _should_prefer_grounded_plan_direct(
        self,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Optional[Dict[str, Any]] = None,
    ) -> bool:
        strategy = self._resolve_grounded_route_strategy(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
        )
        strategy_decision = strategy.should_prefer_grounded_plan_direct(self, message, answer_plan)
        if strategy_decision:
            return True
        recommended_route = str(answer_plan.get("recommended_route") or "").strip()
        reason_points = [str(item or "").strip() for item in list(answer_plan.get("reason_points") or []) if str(item or "").strip()]
        answer_mode = str(answer_plan.get("answer_mode") or "").strip()
        candidate_routes = [
            str(route_name or "").strip()
            for route_name in list(answer_plan.get("candidate_routes") or [])
            if str(route_name or "").strip()
        ] or [
            str(route_name or "").strip()
            for route_name in list((route_facts or {}).keys())
            if str(route_name or "").strip()
        ]
        explicit_route_mentions = sum(1 for route_name in candidate_routes if route_name and route_name in str(message or ""))
        focuses = set(self._comparison_focuses_for_plan(message))
        grounded_decision_signal = bool({"budget", "elder", "light_pace", "shopping"} & focuses)
        return bool(
            recommended_route
            and reason_points
            and answer_mode in {"comparison_recommendation", ""}
            and any(term in str(message or "") for term in ("怎么选", "哪个好", "区别", "对比", "适合", "预算", "第一次"))
            and (explicit_route_mentions >= 2 or grounded_decision_signal)
        )

    @staticmethod
    def _detect_single_route_grounded_topic(message: str) -> str:
        text = str(message or "")
        if any(term in text for term in ("包含", "费用", "门票", "车费", "餐", "午餐", "自费", "哪些费用")):
            return "inclusion"
        if any(term in text for term in ("下雨", "雨天", "天气")):
            return "weather"
        if any(term in text for term in ("当天能不能报名", "当天报名", "今天能不能报名", "今天能不能订", "今天能不能走", "明天能不能报名", "报名", "预订", "余位")):
            return "signup"
        if any(term in text for term in ("一个人", "单人", "自己去", "独自")):
            return "solo"
        return ""

    def _should_prefer_single_route_grounded_direct(
        self,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge,
    ) -> bool:
        strategy = self._resolve_grounded_route_strategy(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        )
        strategy_decision = strategy.should_prefer_single_route_grounded_direct(
            self,
            message,
            answer_plan,
            route_facts,
            route_evidence,
            matched_knowledge,
        )
        if strategy_decision:
            return True
        return self._legacy_should_prefer_single_route_grounded_direct(
            message,
            answer_plan,
            route_facts,
            route_evidence,
            matched_knowledge,
        )

    def _legacy_should_prefer_single_route_grounded_direct(
        self,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge,
    ) -> bool:
        recommended_route = str(answer_plan.get("recommended_route") or "").strip()
        if not recommended_route:
            return False
        if str(answer_plan.get("answer_mode") or "").strip() not in {"single_route_grounded", ""}:
            return False
        topic = self._detect_single_route_grounded_topic(message)
        if not topic:
            return False
        route_fact = dict((route_facts or {}).get(recommended_route) or {})
        route_ev = dict((route_evidence or {}).get(recommended_route) or {})
        answer_text = str(getattr(matched_knowledge, "answer", "") or "")
        if topic == "inclusion":
            return bool(route_fact.get("price") or route_fact.get("includes") or route_fact.get("excludes") or answer_text)
        if topic == "weather":
            return bool(self._pick_route_evidence_answer(route_ev, "tips", "service", "itinerary") or answer_text)
        if topic == "signup":
            return bool(self._pick_route_evidence_answer(route_ev, "itinerary", "tips", "service") or route_fact.get("price") or answer_text)
        if topic == "solo":
            return bool(route_fact.get("pace") or route_fact.get("suitable_for") or answer_text)
        return False

    @staticmethod
    def _pick_route_evidence_answer(route_evidence: Dict[str, Any], *categories: str) -> str:
        for category in categories:
            answer = str((route_evidence.get(category) or {}).get("answer") or "").strip()
            if answer:
                return answer
        return ""

    @staticmethod
    def _extract_sentences_with_keywords(text: str, keywords: Tuple[str, ...]) -> List[str]:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        parts = re.split(r"[。！？\n]+", normalized)
        hits: List[str] = []
        for part in parts:
            sentence = re.sub(r"\s+", " ", part).strip(" ；;，,")
            if not sentence:
                continue
            if any(keyword in sentence for keyword in keywords):
                hits.append(sentence)
        return hits

    def _render_single_route_grounded_reply(
        self,
        mode: str,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge,
    ) -> str:
        strategy = self._resolve_grounded_route_strategy(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        )
        reply = strategy.render_single_route_grounded_reply(
            self,
            mode=mode,
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        )
        if str(reply or "").strip():
            return reply
        return self._legacy_render_single_route_grounded_reply(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        )
        
    def _legacy_render_single_route_grounded_reply(
        self,
        *,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
        route_evidence: Dict[str, Any],
        matched_knowledge,
    ) -> str:
        recommended_route = str(answer_plan.get("recommended_route") or "").strip()
        if not recommended_route:
            return ""
        route_fact = dict((route_facts or {}).get(recommended_route) or {})
        route_ev = dict((route_evidence or {}).get(recommended_route) or {})
        answer_text = str(getattr(matched_knowledge, "answer", "") or "").strip()
        topic = self._detect_single_route_grounded_topic(message)
        parts: List[str] = []
        schema_detail_points = self._build_schema_single_route_detail_points(
            route_name=recommended_route,
            route_fact=route_fact,
            route_evidence=route_ev,
            matched_answer=answer_text,
            answer_plan=answer_plan,
            topic=topic,
        )

        if topic == "inclusion":
            price = str(route_fact.get("price") or "").strip()
            includes = [str(item or "").strip() for item in list(route_fact.get("includes") or []) if str(item or "").strip()]
            excludes = [str(item or "").strip() for item in list(route_fact.get("excludes") or []) if str(item or "").strip()]
            if price:
                parts.append(f"{recommended_route}这条目前常规价格口径是{price}")
            if includes:
                parts.append(f"一般包含{'、'.join(includes)}")
            evidence_price = self._pick_route_evidence_answer(route_ev, "price", "service", "tips")
            if not includes and evidence_price:
                parts.append(evidence_price)
            if excludes:
                parts.append(f"通常不含{'、'.join(excludes)}")
            parts.extend(point for point in schema_detail_points if point not in parts)
            parts.append("您把出发日期和人数发我，我再按当天班次帮您确认。")
            return self._clean_rendered_sales_reply("。".join(part for part in parts if part) + "。")

        if topic == "weather":
            evidence = self._pick_route_evidence_answer(route_ev, "tips", "service", "itinerary") or ""
            weather_source = " ".join(part for part in [evidence, answer_text] if part)
            weather_hits = self._extract_sentences_with_keywords(weather_source, ("正常发团", "湿滑", "天气", "关闭"))
            if weather_hits:
                parts.append("；".join(weather_hits[:2]))
            elif weather_source:
                parts.append(weather_source)
            parts.extend(point for point in schema_detail_points if point not in parts)
            parts.append("您把出发日期发我，我再按当天预报帮您确认")
            return self._clean_rendered_sales_reply("。".join(part for part in parts if part) + "。")

        if topic == "signup":
            evidence = self._pick_route_evidence_answer(route_ev, "itinerary", "tips", "service") or answer_text
            schedule_hits = self._extract_sentences_with_keywords(evidence, ("前一天晚上", "通知集合时间", "集合时间"))
            if schedule_hits:
                parts.append(schedule_hits[0])
            elif evidence:
                parts.append(evidence)
            parts.extend(point for point in schema_detail_points if point not in parts)
            parts.append("当天能不能报名，主要还是看当天余位和发团安排")
            parts.append("您把出发日期和人数发我，我再帮您确认当天还能不能安排")
            return self._clean_rendered_sales_reply("。".join(part for part in parts if part) + "。")

        if topic == "solo":
            pace = str(route_fact.get("pace") or "").strip()
            suitable_for = [str(item or "").strip() for item in list(route_fact.get("suitable_for") or []) if str(item or "").strip()]
            parts.append(f"{recommended_route}一个人出行通常也合适")
            if pace:
                parts.append(f"整体节奏会更偏{pace}")
            if suitable_for:
                parts.append(f"更适合{'、'.join(suitable_for)}这类需求")
            parts.extend(point for point in schema_detail_points if point not in parts)
            return self._clean_rendered_sales_reply("。".join(part for part in parts if part) + "。")
        return ""

    @staticmethod
    def _pick_grounded_plan_scenario(message: str, focuses: List[str]) -> str:
        text = str(message or "")
        if "budget" in focuses or any(term in text for term in ("预算", "便宜", "划算", "性价比")):
            return "budget"
        if any(focus in focuses for focus in ("elder", "light_pace")) or any(term in text for term in ("老人", "小孩", "腿脚")):
            return "elder"
        if "first_time" in focuses or "第一次来" in text or "第一次去" in text:
            return "first_time"
        if "shopping" in focuses or any(term in text for term in ("购物", "纯玩", "进店", "超市停留")):
            return "shopping"
        return "default"

    @staticmethod
    def _select_grounded_plan_reason_points(
        reason_points: List[str],
        focuses: List[str],
        scenario: str,
        recommended_route: str = "",
        route_names: Optional[List[str]] = None,
    ) -> List[str]:
        def _reason_tag(point: str) -> str:
            text = str(point or "")
            if any(term in text for term in ("预算", "价格", "划算", "性价比", "返20", "返40")):
                return "budget"
            if any(term in text for term in ("购物", "纯玩", "进店")):
                return "shopping"
            if any(term in text for term in ("第一次来", "第一次去", "踩坑", "看起")):
                return "first_time"
            if any(term in text for term in ("老人", "小孩", "亲子")):
                return "elder"
            if any(term in text for term in ("轻松", "省心", "体力", "赶", "费体力")):
                return "pace"
            if any(term in text for term in ("适合", "偏向")):
                return "suitable"
            return "general"

        route_names = [str(name or "").strip() for name in (route_names or []) if str(name or "").strip()]

        def _mentions_other_route(point: str) -> bool:
            text = str(point or "")
            if not recommended_route:
                return False
            return any(
                route_name != recommended_route and route_name in text
                for route_name in route_names
            )

        preferred_rules = {
            "budget": ("预算", "价格", "划算", "性价比"),
            "elder": ("老人", "小孩", "轻松", "稳", "省心", "体力"),
            "first_time": ("第一次来", "第一次去", "踩坑", "稳", "看起"),
            "shopping": ("购物", "纯玩", "进店"),
        }
        selected: List[str] = []
        selected_tags: List[str] = []

        def _push(point: str) -> bool:
            normalized = str(point or "").strip()
            if not normalized or normalized in selected:
                return False
            if _mentions_other_route(normalized):
                return False
            tag = _reason_tag(normalized)
            if tag in selected_tags and tag != "general":
                return False
            selected.append(normalized)
            selected_tags.append(tag)
            return True

        for keyword in preferred_rules.get(scenario, ()):
            match = next((point for point in reason_points if keyword in point and point not in selected), "")
            if match:
                _push(match)
                break

        if "budget" in focuses and scenario != "budget":
            match = next((point for point in reason_points if any(term in point for term in ("预算", "价格")) and point not in selected), "")
            if match:
                _push(match)
        if any(focus in focuses for focus in ("elder", "light_pace")) and scenario != "elder":
            match = next((point for point in reason_points if any(term in point for term in ("老人", "小孩", "轻松", "稳")) and point not in selected), "")
            if match:
                _push(match)
        if "shopping" in focuses and scenario != "shopping":
            match = next((point for point in reason_points if any(term in point for term in ("购物", "纯玩")) and point not in selected), "")
            if match:
                _push(match)
        if "first_time" in focuses and scenario != "first_time":
            match = next((point for point in reason_points if "第一次来" in point and point not in selected), "")
            if match:
                _push(match)

        complementary_tag_order = {
            "budget": ("elder", "pace", "shopping", "first_time", "suitable", "general"),
            "elder": ("budget", "shopping", "first_time", "suitable", "general"),
            "first_time": ("shopping", "budget", "elder", "pace", "suitable", "general"),
            "shopping": ("first_time", "budget", "elder", "pace", "suitable", "general"),
            "default": ("budget", "elder", "shopping", "first_time", "pace", "suitable", "general"),
        }
        for wanted_tag in complementary_tag_order.get(scenario, complementary_tag_order["default"]):
            if len(selected) >= 2:
                break
            match = next(
                (
                    point for point in reason_points
                    if point not in selected and _reason_tag(point) == wanted_tag
                ),
                "",
            )
            if match:
                _push(match)

        for point in reason_points:
            if len(selected) >= 2:
                break
            _push(point)
        return selected[:2]

    @staticmethod
    def _normalize_sales_fragment(text: str) -> str:
        cleaned = str(text or "").strip(" -")
        if not cleaned:
            return ""
        cleaned = re.sub(r"\s+([，。；：！？])", r"\1", cleaned)
        cleaned = re.sub(r"([，。；：！？])\s+", r"\1", cleaned)
        cleaned = re.sub(r"(?<=[A-Za-z0-9\u4e00-\u9fff])\s+(?=[A-Za-z0-9\u4e00-\u9fff])", "", cleaned)
        replacements = {
            "更偏向第一次了解、介意额外停留": "第一次了解、又介意额外停留的人会更省心",
            "更偏向带老人小孩": "带老人小孩会更省心",
            "第一次来时可优先从": "第一次了解时，先看",
            "第一次来时可先从": "第一次了解时，先看",
            "景点亮点主要是": "一路上主要看",
            "核心亮点是": "一路上主要看",
            "这条整体是纯玩无购物": "这条整体更省心",
            "这条整体是": "这条整体属于",
            "整体节奏会更偏纯玩省心": "整体节奏会更轻松省心",
            "整体节奏会更偏轻松": "整体节奏会更轻松",
        }
        for source, target in replacements.items():
            cleaned = cleaned.replace(source, target)
        return cleaned.strip("。；;，, ")

    @staticmethod
    def _build_grounded_plan_opening(scenario: str, recommended_route: str) -> str:
        openings = {
            "budget": f"这两个里面，如果您更看重预算和整体稳妥度，我会更推荐{recommended_route}。",
            "elder": f"如果这次是带老人、带小孩，或者想走得轻松一点，我会更推荐{recommended_route}。",
            "first_time": f"如果是第一次了解这类方案，想先选个更稳妥、不容易踩坑的，我会更建议先看{recommended_route}。",
            "shopping": f"如果您比较介意额外停留、想优先看更省心的体验，我会更推荐{recommended_route}。",
            "default": f"这两个里面，我会更推荐您看{recommended_route}。",
        }
        return openings.get(scenario, openings["default"])

    @staticmethod
    def _build_grounded_plan_closing(
        scenario: str,
        followup_action: str,
        mode: str,
    ) -> str:
        needs_date = "日期" in followup_action
        needs_party = "人数" in followup_action
        needs_elder = any(term in followup_action for term in ("老人", "小孩"))
        needs_detail = any(term in followup_action for term in ("价格", "说明", "适合对象", "包含"))

        if needs_detail and not (needs_date or needs_party or needs_elder):
            closing = "您要是想继续看，我可以接着把这个方案的价格、说明、包含项和适合对象给您拆开说。"
        elif scenario == "budget":
            closing = "您把出发日期和人数发我，我按当天班次和预算，直接给您缩成首选和备选。"
            if needs_elder:
                closing = "您把出发日期、人数，还有有没有老人小孩发我，我按预算和轻松度，直接给您缩成首选和备选。"
        elif scenario == "elder":
            closing = "您把出发日期、人数，还有老人小孩情况发我，我顺手帮您把太赶、太费体力的先排掉。"
        elif scenario == "first_time":
            closing = "您把哪天安排、几个人告诉我，我按更稳妥的思路，直接给您排个首选和备选。"
            if needs_elder:
                closing = "您把哪天安排、几个人，还有有没有老人小孩告诉我，我按稳妥又轻松一点的思路，直接给您排个首选和备选。"
        elif scenario == "shopping":
            closing = "您把出发日期和人数发我，我按更省心、少额外停留这条标准，直接帮您筛一遍。"
        else:
            closing = "您要是方便的话，把出发日期、人数和有没有老人小孩发我，我再直接帮您缩成首选和备选。"

        if needs_detail and "价格、说明" not in closing:
            closing += "如果您想一起看细点，我也可以顺手把价格、说明、包含项和适合对象拆给您。"

        if mode == "sales" and "哪条更合适" not in closing:
            closing += "如果您已经有大概日期，我也可以顺手帮您看哪条更合适。"
        return closing

    @staticmethod
    def _clean_rendered_sales_reply(reply: str) -> str:
        cleaned = str(reply or "").strip()
        if not cleaned:
            return cleaned
        cleaned = re.sub(r"\s+([，。；：！？])", r"\1", cleaned)
        cleaned = re.sub(r"([，。；：！？])\s+", r"\1", cleaned)
        cleaned = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"([。！？；])\1+", r"\1", cleaned)
        return cleaned.strip()

    def _render_grounded_plan_direct_reply(
        self,
        mode: str,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
    ) -> str:
        strategy = self._resolve_grounded_route_strategy(
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
        )
        reply = strategy.render_grounded_plan_direct_reply(
            self,
            mode=mode,
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
        )
        if str(reply or "").strip():
            return reply
        return self._legacy_render_grounded_plan_direct_reply(
            mode=mode,
            message=message,
            answer_plan=answer_plan,
            route_facts=route_facts,
        )
    
    def _legacy_render_grounded_plan_direct_reply(
        self,
        *,
        mode: str,
        message: str,
        answer_plan: Dict[str, Any],
        route_facts: Dict[str, Any],
    ) -> str:
        recommended_route = str(answer_plan.get("recommended_route") or "").strip()
        if not recommended_route:
            return ""
        route_names = [str(name or "").strip() for name in list((route_facts or {}).keys()) if str(name or "").strip()]
        focuses = self._comparison_focuses_for_plan(message)
        scenario = self._pick_grounded_plan_scenario(message, focuses)
        reason_points = self._select_grounded_plan_reason_points(
            list(answer_plan.get("reason_points") or []),
            focuses=focuses,
            scenario=scenario,
            recommended_route=recommended_route,
            route_names=route_names,
        )
        normalized_reasons = [self._normalize_sales_fragment(point) for point in reason_points if self._normalize_sales_fragment(point)]
        route_fact = dict((route_facts or {}).get(recommended_route) or {})
        price_text = str(route_fact.get("price") or "").strip()
        original_reason_count = len(normalized_reasons)
        auto_added_price_reason = False
        if price_text and not any(price_text in point for point in normalized_reasons):
            price_reason = f"已知价格口径是{price_text}"
            if not normalized_reasons:
                normalized_reasons.append(price_reason)
                auto_added_price_reason = True
            elif scenario == "budget":
                normalized_reasons.insert(0, price_reason)
                auto_added_price_reason = True
            elif len(normalized_reasons) < 2:
                normalized_reasons.append(price_reason)
                auto_added_price_reason = True
        shopping = str(route_fact.get("shopping") or "").strip()
        if shopping and not any(shopping in point for point in normalized_reasons):
            normalized_reasons.append(shopping)
        normalized_reasons = normalized_reasons[:2]
        opening = self._build_grounded_plan_opening(scenario, recommended_route)
        core_reason_count = len(
            [point for point in normalized_reasons if not price_text or price_text not in point]
        )
        if len(normalized_reasons) == 1 or (
            auto_added_price_reason
            and core_reason_count <= 1
            and scenario != "budget"
            and original_reason_count <= 1
        ):
            reason_block = f"主要看这点：{normalized_reasons[0]}。"
        else:
            reason_block = "主要看这两点：" + "；".join(normalized_reasons[:2]) + "。"
        closing = self._build_grounded_plan_closing(
            scenario,
            str(answer_plan.get("followup_action") or ""),
            mode,
        )
        return self._clean_rendered_sales_reply(f"{opening}{reason_block}{closing}")

    def _build_retrieval_prompt_sections(
        self,
        retrieved_contexts: List[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """将检索上下文拆成结构化证据包与普通知识片段两部分。

        普通知识片段会按序号编号（[1] [2] ...），便于 LLM 在回复中生成行内引用标记。
        """
        # 修复 R5/N9：限制单条 answer 长度，避免长行程说明吃掉 token 预算导致 prompt 超限
        # 修复 N9：按句子边界截断，避免截断在句子中间导致语义不完整
        MAX_ANSWER_CHARS = 300
        MAX_BUNDLE_CHARS = 600

        def _truncate_at_sentence_boundary(text: str, max_chars: int) -> str:
            """在句子边界处截断，保留完整语义"""
            if len(text) <= max_chars:
                return text
            truncated = text[:max_chars]
            # 在截断点附近寻找最近的句子结束符
            for sep in ('。', '！', '？', '；', '.', '!', '?', ';'):
                last_sep = truncated.rfind(sep)
                if last_sep > max_chars * 0.6:  # 至少保留 60% 内容
                    return truncated[:last_sep + 1]
            return truncated + "..."

        bundle_sections: List[str] = []
        standard_sections: List[str] = []

        for ctx in retrieved_contexts or []:
            if ctx.get("source") == "knowledge_bundle":
                bundle_text = str(ctx.get("answer", "") or "").strip()
                bundle_text = _truncate_at_sentence_boundary(bundle_text, MAX_BUNDLE_CHARS)
                bundle_sections.append(bundle_text)
                continue
            question = str(ctx.get("question", "") or "").strip()
            answer = str(ctx.get("answer", "") or "").strip()
            if not question and not answer:
                continue
            answer = _truncate_at_sentence_boundary(answer, MAX_ANSWER_CHARS)
            standard_sections.append(f"问题：{question}\n答案：{answer}")

        # 为普通知识片段加序号标记，便于 LLM 输出引用
        numbered_sections: List[str] = []
        for index, section in enumerate(standard_sections[:4], start=1):
            numbered_sections.append(f"[{index}] {section}")

        return (
            "\n\n".join(section for section in bundle_sections if section).strip(),
            "\n\n".join(numbered_sections).strip(),
        )

    def _get_active_schema_profile(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        """读取当前激活 Schema 的回复配置，缺失时回退到通用默认值。"""
        profile = copy.deepcopy(_DEFAULT_SCHEMA_REPLY_PROFILE)
        try:
            active_schema = get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
            metadata = active_schema.get("metadata") or {}
            schema_profile = metadata.get("reply_profile") or {}
            if isinstance(schema_profile, dict):
                for key, value in schema_profile.items():
                    if key == "base_replies" and isinstance(value, dict):
                        merged_replies = profile.get("base_replies", {}).copy()
                        merged_replies.update(value)
                        profile["base_replies"] = merged_replies
                    elif key == "domain_rules" and isinstance(value, list):
                        profile["domain_rules"] = [str(item).strip() for item in value if str(item).strip()]
                    elif value not in (None, ""):
                        profile[key] = value
            if active_schema.get("display_name"):
                profile["display_name"] = str(active_schema.get("display_name"))
            if active_schema.get("entity_type"):
                profile["entity_type"] = str(active_schema.get("entity_type"))
        except Exception as exc:
            logger.warning(f"读取行业 Schema 回复配置失败，使用默认值: {exc}")
        return profile

    def _get_active_reply_policy(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        policy = copy.deepcopy(_DEFAULT_SCHEMA_REPLY_POLICY)
        try:
            active_schema = get_active_schema_with_compat(
                get_industry_schema_service(),
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
            metadata = active_schema.get("metadata") or {}
            schema_policy = metadata.get("reply_policy") or {}
            if isinstance(schema_policy, dict):
                policy = _deep_merge_dict(policy, schema_policy)
        except Exception as exc:
            logger.warning(f"读取行业 Schema 回答策略失败，使用默认值: {exc}")
        return policy

    def _get_schema_query_understanding(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        """读取当前激活 Schema 的 query_understanding 配置，缺失时返回空字典。

        统一入口，供同义词组、代词、显式词、业务标记等领域词表使用，避免硬编码。
        当 enterprise_id 为空且 require_enterprise_binding=True 时，fail_closed 模式会返回空，
        因此回退到 settings.active_schema_id 作为 preferred_schema_id 以读取全局激活配置。
        """
        try:
            normalized_enterprise = str(enterprise_id or "").strip()
            normalized_preferred = str(preferred_schema_id or "").strip()
            schema_service = get_industry_schema_service()
            if not normalized_preferred and not normalized_enterprise:
                normalized_preferred = str(
                    (schema_service.get_settings() or {}).get("active_schema_id") or ""
                ).strip()
            active_schema = get_active_schema_with_compat(
                schema_service,
                enterprise_id=normalized_enterprise,
                preferred_schema_id=normalized_preferred,
                resolution_mode=resolution_mode,
            )
            metadata = active_schema.get("metadata") or {}
            query_understanding = metadata.get("query_understanding") or {}
            if isinstance(query_understanding, dict):
                return query_understanding
        except Exception as exc:
            logger.warning(f"读取行业 Schema query_understanding 配置失败: {exc}")
        return {}

    def _get_schema_synonym_groups(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> List[Set[str]]:
        """从 schema metadata.query_understanding.synonym_groups 读取行业专用同义词组。

        返回 List[Set[str]]，每组成员为同义关键词；schema 缺失或异常时返回空列表。
        """
        query_understanding = self._get_schema_query_understanding(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )
        raw_groups = query_understanding.get("synonym_groups") or []
        if not isinstance(raw_groups, list):
            return []
        result: List[Set[str]] = []
        for group in raw_groups:
            if not isinstance(group, list):
                continue
            normalized = {str(item).strip() for item in group if str(item or "").strip()}
            if normalized:
                result.append(normalized)
        return result

    def _get_schema_term_list(
        self,
        key: str,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> List[str]:
        """从 schema query_understanding 读取指定键的词表（如 pronoun_terms/explicit_terms/business_markers）。

        统一工具方法，避免各处重复读取 schema 并硬编码行业术语。
        """
        if not key:
            return []
        query_understanding = self._get_schema_query_understanding(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )
        raw = query_understanding.get(key) or []
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item or "").strip()]

    @staticmethod
    def _reply_policy_get(mapping: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
        current: Any = mapping or {}
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
        return default if current is None else current

    def _reply_policy_get_float(
        self,
        mapping: Optional[Dict[str, Any]],
        *keys: str,
        default: float,
    ) -> float:
        value = self._reply_policy_get(mapping, *keys, default=default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _get_cta_append_policy(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        reply_policy = self._get_active_reply_policy(
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )
        cta_policy = self._reply_policy_get(reply_policy, "cta_append_policy", default={}) or {}
        return cta_policy if isinstance(cta_policy, dict) else {}

    @staticmethod
    def _normalize_policy_terms(values: Any) -> tuple[str, ...]:
        return tuple(
            str(item or "").strip().lower()
            for item in list(values or [])
            if str(item or "").strip()
        )

    def _get_contact_material_terms(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> set[str]:
        try:
            cta_policy = self._get_cta_append_policy(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
        except TypeError:
            cta_policy = self._get_cta_append_policy(enterprise_id=enterprise_id)
        contact_material_terms = set(
            self._normalize_policy_terms(
                (cta_policy or {}).get("explicit_contact_terms")
            )
        )
        contact_material_terms.update(
            {
                "联系",
                "联系方式",
                "怎么联系",
                "资料",
                "发我资料",
                "资料发我",
                "发我",
                "报价发我",
                "方案发我",
            }
        )
        return {
            str(term or "").strip().lower()
            for term in contact_material_terms
            if str(term or "").strip()
        }

    def _is_contact_or_material_query(
        self,
        message: str,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> bool:
        normalized_message = str(message or "").strip().lower()
        if not normalized_message:
            return False
        try:
            contact_terms = self._get_contact_material_terms(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
        except TypeError:
            contact_terms = self._get_contact_material_terms(enterprise_id=enterprise_id)
        return any(term in normalized_message for term in contact_terms)

    @staticmethod
    def _parse_next_best_action(value: str) -> NextBestAction:
        normalized = str(value or "").strip().lower()
        for candidate in NextBestAction:
            if candidate.value == normalized:
                return candidate
        return NextBestAction.ANSWER_ONLY

    @staticmethod
    def _parse_cta_mode(value: str) -> CtaMode:
        normalized = str(value or "").strip().lower()
        for candidate in CtaMode:
            if candidate.value == normalized:
                return candidate
        return CtaMode.NONE

    def _get_schema_base_replies(
        self,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, str]:
        try:
            profile = self._get_active_schema_profile(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
        except TypeError:
            profile = self._get_active_schema_profile(enterprise_id=enterprise_id)
        return profile.get("base_replies", {}) or {}

    def _get_high_priority_industry_intent_names(self, enterprise_id: str = "") -> set[str]:
        try:
            active_schema = get_industry_schema_service().get_active_schema(enterprise_id=enterprise_id) or {}
            metadata = active_schema.get("metadata") or {}
            intent_config = metadata.get("intent_recognition") or {}
            configured = intent_config.get("high_priority_industry_intents") or []
            return {
                str(intent_name or "").strip()
                for intent_name in list(configured)
                if str(intent_name or "").strip()
            }
        except Exception as exc:
            logger.debug(f"读取高优先级行业意图失败: {exc}")
            return set()

    def _render_schema_reply_template(self, template: Optional[str], name: str) -> str:
        raw_template = str(template or "").strip()
        if not raw_template:
            return ""
        try:
            return raw_template.format(name=name or "您")
        except Exception:
            return raw_template.replace("{name}", name or "您")

    def _render_industry_intent_reply(self, industry_intent: str, name: str, enterprise_id: str = "") -> str:
        base_replies = self._get_schema_base_replies(enterprise_id=enterprise_id)
        industry_key = get_intent_value(industry_intent)
        rendered = self._render_schema_reply_template(base_replies.get(industry_key), name)
        if rendered:
            return rendered

        reply_policy = self._get_active_reply_policy(enterprise_id=enterprise_id)
        fallback_map = self._reply_policy_get(reply_policy, "intent_reply_fallbacks", default={}) or {}
        fallback_keys = list(fallback_map.get(industry_key) or fallback_map.get("default") or ["default", "fallback"])
        for fallback_key in fallback_keys:
            rendered = self._render_schema_reply_template(base_replies.get(fallback_key), name)
            if rendered:
                return rendered
        return ""

    def _render_contextual_reply_template(
        self,
        template: Optional[str],
        *,
        customer_name: str = "",
        message: str = "",
    ) -> str:
        raw_template = str(template or "").strip()
        if not raw_template:
            return ""
        try:
            return raw_template.format(name=customer_name or "您", message=message or "")
        except Exception:
            rendered = raw_template.replace("{name}", customer_name or "您")
            return rendered.replace("{message}", message or "")

    def _get_schema_fallback_reply(self) -> str:
        return ""

    @staticmethod
    def _extract_schema_id_from_reply_analysis(reply_analysis: Optional[Dict[str, Any]]) -> str:
        if not isinstance(reply_analysis, dict):
            return ""
        retrieval = reply_analysis.get("retrieval") or {}
        direct_schema_id = str(retrieval.get("schema_id") or "").strip()
        if direct_schema_id:
            return direct_schema_id
        for ctx in retrieval.get("contexts") or []:
            if not isinstance(ctx, dict):
                continue
            schema_id = str(ctx.get("schema_id") or "").strip()
            if schema_id:
                return schema_id
        return ""

    def _build_schema_prompt_profile(
        self,
        mode: str,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
    ) -> Dict[str, Any]:
        try:
            profile = self._get_active_schema_profile(
                enterprise_id=enterprise_id,
                preferred_schema_id=preferred_schema_id,
                resolution_mode=resolution_mode,
            )
        except TypeError:
            profile = self._get_active_schema_profile(enterprise_id=enterprise_id)
        persona = profile.get("sales_persona") if mode == "sales" else profile.get("qa_persona")
        display_name = str(profile.get("display_name") or "当前行业")
        entity_type = str(profile.get("entity_type") or "产品")
        # 实体显示标签由 schema reply_profile.entity_type_label 提供（如旅游为"线路"），默认"产品"
        entity_label = str(profile.get("entity_type_label") or entity_type or "产品")
        if entity_label in {"线路", "方案"}:
            structured_bundle_priority = "方案、价格、说明、包含项和适合对象"
        else:
            structured_bundle_priority = f"{entity_label}、价格、特点和适用对象"
        domain_rules = profile.get("domain_rules") or []
        if not domain_rules:
            domain_rules = _DEFAULT_SCHEMA_REPLY_PROFILE["domain_rules"]
        default_persona = (
            _DEFAULT_SCHEMA_REPLY_PROFILE["sales_persona"]
            if mode == "sales"
            else _DEFAULT_SCHEMA_REPLY_PROFILE["qa_persona"]
        )
        return {
            "persona": str(persona or default_persona),
            "display_name": display_name,
            "entity_type": entity_type,
            "entity_label": entity_label,
            "structured_bundle_priority": structured_bundle_priority,
            "sales_focus": str(profile.get("sales_focus") or _DEFAULT_SCHEMA_REPLY_PROFILE["sales_focus"]),
            "domain_rules": [str(item).strip() for item in domain_rules if str(item).strip()],
        }

    def _generate_base_reply(
        self,
        message: str,
        customer_name: str,
        intent_result,
        conversation_history: List[Dict],
        customer_data: Dict
    ) -> str:
        """生成无需旧包装服务的基础回复。"""
        del message

        name = customer_name or "您"
        intent = intent_result.primaryIntent
        enterprise_id = self._resolve_enterprise_id(customer_data, conversation_history)
        base_replies = self._get_schema_base_replies(enterprise_id=enterprise_id)

        intent_key_map = {
            IntentType.THANKS: "thanks",
            IntentType.GREETING: "greeting",
            IntentType.FAREWELL: "farewell",
            IntentType.CONFIRMATION: "confirmation",
            IntentType.STATUS_INQUIRY: "status_inquiry",
            IntentType.READY_CHECK: "ready_check",
            IntentType.PRICE_INQUIRY: "price_inquiry",
            IntentType.CONTACT_INQUIRY: "contact_inquiry",
            IntentType.COMPLAINT: "complaint",
            IntentType.PURCHASE_INTENT: "purchase_intent",
            IntentType.COOPERATION_INTENT: "cooperation_intent",
        }
        if intent in {IntentType.PRODUCT_INQUIRY, IntentType.SERVICE_INQUIRY}:
            return self._render_schema_reply_template(base_replies.get("product_inquiry"), name)

        intent_key = intent_key_map.get(intent)
        if not intent_key:
            if is_industry_intent(intent.value):
                return self._render_industry_intent_reply(intent.value, name, enterprise_id=enterprise_id)
            return ""
        if intent_key in {"greeting", "default", "fallback"}:
            return ""
        return self._render_schema_reply_template(base_replies.get(intent_key), name)

    def _get_retrieval_knowledge_base(self):
        """统一知识检索主链：只允许走统一知识库。"""
        if self._retrieval_knowledge_base is None:
            self._retrieval_knowledge_base = get_unified_knowledge_service()
        return self._retrieval_knowledge_base

    def _get_knowledge_items_for_graph(self):
        """获取用于构建知识图谱的知识条目列表。"""
        retrieval_kb = self._get_retrieval_knowledge_base()
        if hasattr(retrieval_kb, "list_knowledge_items"):
            return retrieval_kb.list_knowledge_items()
        if hasattr(retrieval_kb, "get_knowledge_list"):
            return retrieval_kb.get_knowledge_list()
        if hasattr(retrieval_kb, "get_all_items"):
            return retrieval_kb.get_all_items()
        if hasattr(retrieval_kb, "knowledge_items"):
            return retrieval_kb.knowledge_items
        raise AttributeError("统一知识库未提供可读取的知识条目接口")

    def _find_exact_knowledge_match(self, query: str, enterprise_id: str = "", schema_id: str = ""):
        """在租户/schema 过滤后的知识范围内优先查找精确问法或别名，避免短句被旧高分条目盖掉。"""
        normalized_query = self._normalize_query_text(query)
        if not normalized_query:
            return None

        try:
            normalized_enterprise = str(enterprise_id or "").strip() or "default"
            effective_schema_id = str(schema_id or "").strip()
            if not effective_schema_id:
                try:
                    from src.common.industry_schema_service import get_industry_schema_service

                    effective_schema_id = str(
                        get_industry_schema_service().get_effective_active_schema_id(
                            enterprise_id=normalized_enterprise
                        )
                        or ""
                    ).strip()
                except Exception:
                    effective_schema_id = ""

            retrieval_kb = self._get_retrieval_knowledge_base()
            if hasattr(retrieval_kb, "get_all_items"):
                knowledge_items = retrieval_kb.get_all_items() or []
            else:
                knowledge_items = self._get_knowledge_items_for_graph() or []
            if not knowledge_items:
                try:
                    from pathlib import Path
                    from types import SimpleNamespace
                    from src.common.knowledge_persistence import KnowledgePersistence
                    from src.infrastructure.runtime_paths import get_base_dir

                    repo_data_dir = Path(get_base_dir()) / "data"
                    runtime_data_dir = Path(str(getattr(retrieval_kb, "data_path", "") or ""))
                    if repo_data_dir.exists() and repo_data_dir != runtime_data_dir:
                        fallback_items = KnowledgePersistence(data_dir=str(repo_data_dir)).load_knowledge_base()
                        knowledge_items = [SimpleNamespace(**item) for item in fallback_items]
                except Exception:
                    pass

            runtime_available_checker = getattr(retrieval_kb, "_is_runtime_available_item", None)

            def _item_runtime_available(item: Any) -> bool:
                if callable(runtime_available_checker):
                    try:
                        return bool(runtime_available_checker(item))
                    except Exception:
                        pass
                if isinstance(item, dict):
                    tags = list(item.get("tags") or [])
                    return not ((not item.get("enabled", True)) and ("待人工审核" in tags))
                tags = list(getattr(item, "tags", []) or [])
                return not ((not getattr(item, "enabled", True)) and ("待人工审核" in tags))

            def _item_matches_scope(item: Any) -> bool:
                if not _item_runtime_available(item):
                    return False
                item_enterprise = (
                    item.get("enterprise_id", "") if isinstance(item, dict) else getattr(item, "enterprise_id", "")
                )
                normalized_item_enterprise = str(item_enterprise or "").strip() or "default"
                if normalized_item_enterprise != normalized_enterprise:
                    return False
                if not effective_schema_id:
                    return True
                item_schema_id = item.get("schema_id", "") if isinstance(item, dict) else getattr(item, "schema_id", "")
                normalized_item_schema = str(item_schema_id or "").strip()
                return normalized_item_schema == effective_schema_id

            for item in knowledge_items:
                if not _item_matches_scope(item):
                    continue
                question = item.get("question", "") if isinstance(item, dict) else getattr(item, "question", "")
                if normalized_query == self._normalize_query_text(question):
                    return item

            for item in knowledge_items:
                if not _item_matches_scope(item):
                    continue
                aliases = item.get("aliases", []) if isinstance(item, dict) else getattr(item, "aliases", [])
                for alias in list(aliases or []):
                    if normalized_query == self._normalize_query_text(alias):
                        return item
        except Exception as e:
            logger.warning(f"精确知识匹配失败，继续走常规检索: {e}")

        return None

    def _search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        business_stage: str = "",
        intent: str = "",
        enterprise_id: str = "",
        schema_id: str = "",
    ):
        """统一知识检索入口，只走统一知识库主链。"""
        retrieval_kb = self._get_retrieval_knowledge_base()
        search_kwargs: Dict[str, Any] = {}
        if business_stage:
            search_kwargs["business_stage"] = business_stage
        if intent:
            search_kwargs["intent"] = intent
        if enterprise_id:
            search_kwargs["enterprise_id"] = enterprise_id
        if schema_id:
            search_kwargs["schema_id"] = schema_id
        if search_kwargs:
            try:
                return retrieval_kb.search(query, top_k=top_k, **search_kwargs)
            except TypeError:
                if enterprise_id or schema_id:
                    raise
                legacy_kwargs = dict(search_kwargs)
                legacy_kwargs.pop("schema_id", None)
                legacy_kwargs.pop("intent", None)
                if legacy_kwargs != search_kwargs:
                    try:
                        return retrieval_kb.search(query, top_k=top_k, **legacy_kwargs)
                    except TypeError:
                        pass
                legacy_kwargs.pop("business_stage", None)
                if legacy_kwargs:
                    try:
                        return retrieval_kb.search(query, top_k=top_k, **legacy_kwargs)
                    except TypeError:
                        pass
        return retrieval_kb.search(query, top_k=top_k)

    def _resolve_request_schema_id(
        self,
        customer_data: Optional[Dict] = None,
        conversation_history: Optional[List[Dict]] = None,
        enterprise_id: str = "",
    ) -> str:
        payload = customer_data or {}
        normalized_enterprise = str(
            enterprise_id or self._resolve_enterprise_id(payload, conversation_history)
        ).strip()
        requested_schema_id = str(
            payload.get("schema_id") or payload.get("preferred_schema_id") or ""
        ).strip()
        resolution_mode = str(payload.get("tenant_resolution_mode") or "").strip() or "fail_soft"
        try:
            resolved_schema_id = str(
                get_industry_schema_service().resolve_schema_id(
                    enterprise_id=normalized_enterprise,
                    preferred_schema_id=requested_schema_id,
                    resolution_mode=resolution_mode,
                )
                or ""
            ).strip()
        except Exception as exc:
            logger.debug(f"解析请求 schema_id 失败，保留原始上下文: {exc}")
            resolved_schema_id = ""
        return resolved_schema_id or requested_schema_id

    def _search_knowledge_bundle(
        self,
        query: str,
        business_stage: str = "",
        intent: str = "",
        enterprise_id: str = "",
        schema_id: str = "",
    ):
        """结构化证据包入口；旧知识服务不支持时自动降级。"""
        retrieval_kb = self._get_retrieval_knowledge_base()
        if hasattr(retrieval_kb, "search_evidence_bundle"):
            bundle_kwargs: Dict[str, Any] = {}
            if business_stage:
                bundle_kwargs["business_stage"] = business_stage
            if intent:
                bundle_kwargs["intent"] = intent
            if enterprise_id:
                bundle_kwargs["enterprise_id"] = enterprise_id
            if schema_id:
                bundle_kwargs["schema_id"] = schema_id
            if bundle_kwargs:
                try:
                    return retrieval_kb.search_evidence_bundle(query, **bundle_kwargs)
                except TypeError:
                    if enterprise_id or schema_id:
                        raise
                    legacy_kwargs = dict(bundle_kwargs)
                    legacy_kwargs.pop("schema_id", None)
                    legacy_kwargs.pop("intent", None)
                    if legacy_kwargs != bundle_kwargs:
                        try:
                            return retrieval_kb.search_evidence_bundle(query, **legacy_kwargs)
                        except TypeError:
                            pass
                    legacy_kwargs.pop("business_stage", None)
                    if legacy_kwargs:
                        try:
                            return retrieval_kb.search_evidence_bundle(query, **legacy_kwargs)
                        except TypeError:
                            pass
            return retrieval_kb.search_evidence_bundle(query)
        return None

    @staticmethod
    def _infer_knowledge_type(item: Any) -> str:
        metadata = getattr(item, "metadata", {}) or {}
        explicit = str(metadata.get("knowledge_type") or "").strip()
        if explicit:
            return explicit
        category = str(getattr(item, "category", "") or "").strip().lower()
        mapping = {
            "price": "conversion_asset",
            "service": "objection_handling",
            "itinerary": "fact",
            "product": "comparison",
            "tips": "objection_handling",
            "contact": "conversion_asset",
        }
        return mapping.get(category, "fact")

    def _ensure_query_rewriter(self):
        """延迟初始化上下文查询改写器和指代消解器（线程安全）"""
        if self._query_rewriter is None:
            with self._query_rewriter_lock:
                if self._query_rewriter is not None:
                    return
                try:
                    from .llm_service import get_default_llm_provider
                    llm_provider = get_default_llm_provider()
                    from src.rag.context_query_rewriter import ContextAwareQueryRewriter
                    from src.rag.coreference_resolver import EnhancedCoreferenceResolver
                    self._query_rewriter = ContextAwareQueryRewriter(llm_service=llm_provider)
                    self._coreference_resolver = EnhancedCoreferenceResolver(llm_service=llm_provider)
                    logger.info("查询改写器和指代消解器初始化完成")
                except Exception as e:
                    logger.warning(f"查询改写器初始化失败: {e}")
        
        if self._learning_engine.llm_service is None:
            try:
                from .llm_service import get_default_llm_provider
                llm_provider = get_default_llm_provider()
                self._learning_engine.llm_service = llm_provider
                logger.info("学习引擎LLM服务初始化完成")
            except Exception as e:
                logger.warning(f"学习引擎LLM服务初始化失败: {e}")
    
    def _ensure_answer_reorganizer(self):
        """延迟初始化答案重组器（线程安全）"""
        if self._answer_reorganizer is None:
            with self._answer_reorganizer_lock:
                if self._answer_reorganizer is not None:
                    return
                try:
                    from .llm_service import get_default_llm_provider
                    llm_provider = get_default_llm_provider()
                    self._answer_reorganizer = get_answer_reorganizer(llm_provider)
                    logger.info("答案重组器初始化成功")
                except Exception as e:
                    logger.warning(f"答案重组器初始化失败：{e}")
                    self._answer_reorganizer = get_answer_reorganizer(None)

    def _ensure_knowledge_graph_built(self):
        """确保知识图谱已构建（非阻塞，线程安全）

        P0-1: 优先从持久化缓存加载，缓存缺失时构建并自动保存。
        P1-5: 构建时传入 LLM 服务，启用 LLM 实体抽取双路径。
        """
        with self._kg_lock:
            if self._kg_initialized or getattr(self, '_kg_building', False):
                return
            self._kg_building = True
            import threading
            def build_async():
                try:
                    # P1-5: 注入 LLM 服务以启用 LLM 实体抽取
                    try:
                        from .llm_service import get_default_llm_provider
                        llm_provider = get_default_llm_provider()
                        if llm_provider:
                            self._knowledge_graph.llm_service = llm_provider
                    except Exception as llm_exc:
                        logger.debug(f"LLM 服务注入知识图谱失败（降级为正则抽取）: {llm_exc}")

                    entity_count = self._knowledge_graph.load_or_build(
                        self._get_knowledge_items_for_graph()
                    )
                    logger.info(f"知识图谱加载/构建完成: {entity_count} 个实体")
                    self._kg_initialized = True
                except Exception as e:
                    logger.warning(f"知识图谱构建失败: {e}")
                finally:
                    self._kg_building = False

            build_thread = threading.Thread(target=build_async, daemon=True)
            build_thread.start()
            logger.info("知识图谱正在后台加载/构建中...")

    def process_message(
        self,
        message: str,
        customer_name: str = "",
        conversation_history: List[Dict] = None,
        customer_data: Dict = None,
        use_enhanced: bool = True,
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        处理客户消息（增强版）

        主链流程：
        1. 上下文理解与查询改写
        2. 统一意图识别
        3. 统一知识检索
        4. LLM 生成/兜底
        5. guardrail 与发送前编排
        """
        try:
            perf_metrics: Dict[str, float] = {}
            total_started_at = time.perf_counter()

            if not message or not isinstance(message, str) or not message.strip():
                return {
                    "reply": "您好！您把具体问题或需求发我，我直接接着说明。",
                    "intent_level": "E",
                    "intent_score": 0.0,
                    "intent_signals": [],
                    "intent_barriers": [],
                    "suggested_action": "等待客户输入",
                    "matched_knowledge": None,
                    "need_human": False,
                    "confidence": 0.0,
                    "source": "empty_defense"
                }
            message = self._strip_preview_noise_prefix(message.strip())
            customer_data = customer_data or {}
            customer_id, platform, session_id = self._build_safe_identity(
                message=message,
                customer_name=customer_name,
                customer_data=customer_data,
                conversation_history=conversation_history,
                provided_session_id=session_id,
            )
            conversation_history = self._sanitize_conversation_history(
                conversation_history,
                session_id=session_id,
                customer_id=customer_id,
                platform=platform,
                conversation_id=str(customer_data.get("conversation_id") or "").strip(),
            )
            enterprise_id = self._resolve_enterprise_id(customer_data, conversation_history)
            schema_id = self._resolve_request_schema_id(
                customer_data=customer_data,
                conversation_history=conversation_history,
                enterprise_id=enterprise_id,
            )
            customer_data = dict(customer_data or {})
            if schema_id:
                customer_data["schema_id"] = schema_id
            logger.info(
                f"[增强回复链] start session_id={session_id} customer={customer_name or '-'} "
                f"history={len(conversation_history or [])} schema_id={schema_id or '-'} message={message[:50]}"
            )

            # 上下文理解主链统一按 session_id 建窗口，避免与意图缓存/对话管理器键空间漂移。
            customer_id_for_context = session_id
            lightweight_preanalysis_enabled = self._should_enable_lightweight_preanalysis(
                message,
                conversation_history,
            )

            stage_started_at = time.perf_counter()
            context_window = self._context_understanding.get_or_create_window(customer_id_for_context)
            resolved_message = self._context_understanding.resolve_references(message, customer_id_for_context)
            self._record_perf_metric(perf_metrics, "context_resolution", stage_started_at)
            grounded_context = self._context_understanding.get_grounded_knowledge(session_id)
            grounded_routes = [
                str(route_name).strip()
                for route_name in (grounded_context.get("routes") or [])
                if str(route_name).strip()
            ]

            # 新增：增强指代消解（优先于原有消解）
            if lightweight_preanalysis_enabled:
                logger.info("轻量预分析命中：跳过 query_rewriter/coreference，保留快速单轮路径")
            else:
                stage_started_at = time.perf_counter()
                self._ensure_query_rewriter()
                self._record_perf_metric(perf_metrics, "ensure_query_rewriter", stage_started_at)
            if not lightweight_preanalysis_enabled and self._coreference_resolver:
                try:
                    stage_started_at = time.perf_counter()
                    allow_llm_coreference = not self._should_try_crag_rewrite(message, conversation_history)
                    coref_result = self._coreference_resolver.resolve(
                        message,
                        session_id,
                        conversation_history,
                        allow_llm=allow_llm_coreference,
                        grounded_entities=grounded_routes,
                    )
                    self._record_perf_metric(perf_metrics, "coreference_resolve", stage_started_at)
                    if not allow_llm_coreference:
                        logger.info("增强指代消解: 多轮歧义查询仅使用规则消解，避免与 CRAG rewrite 重复消耗 LLM")
                    if coref_result.resolved_text != message and coref_result.confidence > 0.5:
                        logger.info(f"增强指代消解: '{message}' -> '{coref_result.resolved_text}' (置信度={coref_result.confidence:.2f}, 方法={coref_result.method})")
                        resolved_message = coref_result.resolved_text
                    elif resolved_message != message:
                        logger.info(f"指代消解: '{message}' -> '{resolved_message}'")
                except Exception as e:
                    logger.debug(f"增强指代消解失败，使用原有消解: {e}")
                    if resolved_message != message:
                        logger.info(f"指代消解: '{message}' -> '{resolved_message}'")
            else:
                if resolved_message != message:
                    logger.info(f"指代消解: '{message}' -> '{resolved_message}'")

            if resolved_message != message:
                message = resolved_message

            expanded_followup_message = self._maybe_expand_domain_followup(
                message,
                conversation_history,
                session_id=session_id,
                enterprise_id=enterprise_id,
            )
            followup_expanded = expanded_followup_message != message
            if followup_expanded:
                logger.info(f"行业短句补全: '{message}' -> '{expanded_followup_message}'")
                message = expanded_followup_message

            if self._is_off_domain_business_intro(message, enterprise_id=enterprise_id):
                conversation_history = self._trim_history_for_business_intro(conversation_history)

            # 新增：上下文感知查询改写（将多轮对话中的短查询/指代查询改写为独立查询）
            retrieval_query = message
            skip_initial_query_rewrite = followup_expanded or self._should_try_crag_rewrite(message, conversation_history)
            rewrite_grounded_context = dict(grounded_context or {})
            if enterprise_id:
                rewrite_grounded_context.setdefault("enterprise_id", enterprise_id)
            if lightweight_preanalysis_enabled:
                logger.info("查询改写: 轻量预分析命中，跳过前置 rewrite")
            elif self._query_rewriter and conversation_history and not skip_initial_query_rewrite:
                try:
                    stage_started_at = time.perf_counter()
                    rewrite_result = self._query_rewriter.rewrite(
                        message,
                        conversation_history,
                        grounded_context=rewrite_grounded_context,
                    )
                    self._record_perf_metric(perf_metrics, "query_rewrite", stage_started_at)
                    if rewrite_result.rewritten_query != message and rewrite_result.confidence > 0.5:
                        retrieval_query = rewrite_result.rewritten_query
                        logger.info(f"查询改写: '{message}' -> '{retrieval_query}' (方法={rewrite_result.method})")
                except Exception as e:
                    logger.debug(f"查询改写失败: {e}")
            elif self._query_rewriter and grounded_routes and not skip_initial_query_rewrite:
                try:
                    stage_started_at = time.perf_counter()
                    rewrite_result = self._query_rewriter.rewrite(
                        message,
                        [],
                        grounded_context=rewrite_grounded_context,
                    )
                    self._record_perf_metric(perf_metrics, "query_rewrite", stage_started_at)
                    if rewrite_result.rewritten_query != message and rewrite_result.confidence > 0.5:
                        retrieval_query = rewrite_result.rewritten_query
                        logger.info(f"查询改写(grounded): '{message}' -> '{retrieval_query}' (方法={rewrite_result.method})")
                except Exception as e:
                    logger.debug(f"grounded 查询改写失败: {e}")
            elif skip_initial_query_rewrite:
                if followup_expanded:
                    logger.info("查询改写: 行业短句已完成上下文补全，跳过前置 rewrite，保留精确问法")
                else:
                    logger.info("查询改写: 检测到多轮歧义查询，跳过前置 rewrite，保留给 CRAG retry 决策")

            intent_context_signature = self._build_history_signature(conversation_history)
            stage_started_at = time.perf_counter()
            cached_intent = self._intent_cache.get(message, session_id, intent_context_signature)
            if cached_intent:
                intent_result = cached_intent
            else:
                # 企业画像场景优先走增强意图识别，确保知识画像能参与判断。
                if enterprise_id:
                    intent_result = self.intent_recognizer.recognize(
                        message,
                        session_id,
                        context=context_window,
                        enterpriseId=enterprise_id,
                    )
                # 新增：BERT意图识别优先，低置信度降级到规则引擎
                elif self._bert_recognizer.is_available():
                    try:
                        intent_result = self._bert_recognizer.predict_with_fallback(
                            message, session_id, context=context_window
                        )
                    except Exception as e:
                        logger.debug(f"BERT意图识别失败，降级到规则引擎: {e}")
                        intent_result = self.intent_recognizer.recognize(
                            message, session_id, context=context_window
                        )
                else:
                    intent_result = self.intent_recognizer.recognize(
                        message,
                        session_id,
                        context=context_window,
                        enterpriseId=enterprise_id,
                    )
            intent_result = self._refine_intent_with_grounded_context(message, intent_result, session_id)
            if not cached_intent:
                self._intent_cache.set(message, session_id, intent_result, intent_context_signature)
            self._record_perf_metric(perf_metrics, "intent_recognition", stage_started_at)
            reply_objective = self._build_reply_objective(
                message=message,
                intent_result=intent_result,
                conversation_history=conversation_history,
                customer_data=customer_data,
                enterprise_id=enterprise_id,
            )

            # 学习引擎：记录查询
            self._learning_engine.on_query_received_async(message, session_id)

            stage_started_at = time.perf_counter()
            dialogue_result = self._dialogue_manager.process_message(
                session_id=session_id,
                customer_id=customer_id,
                user_message=message,
                recognized_intent=intent_result.primaryIntent.value,
                extracted_entities=intent_result.entities,
                enterprise_id=enterprise_id,
            )
            self._record_perf_metric(perf_metrics, "dialogue_manager", stage_started_at)
            # 不再直接返回对话管理器的追问，而是继续走RAG流程
            # 对话管理器的追问作为备选

            # 修复：消费 topic_switch 标志，话题切换时截断旧上下文，防止遗留上下文污染
            # 根因：dialogue_manager 已检测到 topic_switch，但主链从未读取该标志，
            # 导致旧话题（如"冬天三峡"）的机器人回复残留在 conversation_history 中，
            # 被 llm_only fallback 塞入 prompt，LLM 将旧话题实体与新问题错误缝合
            topic_switched = bool(dialogue_result.get("topic_switch", False))
            if topic_switched and conversation_history:
                logger.info(
                    f"检测到话题切换(topic_switch=True)，截断旧上下文: "
                    f"原历史{len(conversation_history)}条 -> 0条，防止遗留上下文污染新话题回复"
                )
                conversation_history = []

            enhanced_history = conversation_history
            if dialogue_result["context_for_rag"]:
                enhanced_history = self._inject_dialogue_context(
                    conversation_history,
                    dialogue_result["context_for_rag"]
                )
            skip_mainline_retrieval = self._should_skip_mainline_retrieval(
                message,
                enhanced_history,
                session_id=session_id,
                enterprise_id=enterprise_id,
            )
            rag_need_retrieval = not skip_mainline_retrieval
            generation_mode = "sales" if rag_need_retrieval else "service"
            customer_data = dict(customer_data or {})
            if skip_mainline_retrieval:
                customer_data["_skip_mainline_retrieval_reason"] = "general_qa"
                customer_data["_reply_branch"] = "pure_llm"
            else:
                customer_data.pop("_skip_mainline_retrieval_reason", None)
                customer_data["_reply_branch"] = "retrieval"

            main_route = decide_main_reply_route(
                intent=intent_result.primaryIntent,
                confidence=float(getattr(intent_result, "confidence", 0.0) or 0.0),
                reply_objective=reply_objective,
                use_enhanced=True,
                math_result_available=False,
                lightweight_interaction_intents=self.LIGHTWEIGHT_INTERACTION_INTENTS,
                high_priority_intents={
                    *self.HIGH_PRIORITY_INTENTS,
                    *self._get_high_priority_industry_intent_names(enterprise_id=enterprise_id),
                },
                metadata={
                    "intent": intent_result.primaryIntent.value,
                    "session_id": session_id,
                    "retrieval_query": retrieval_query,
                    "rag_need_retrieval": rag_need_retrieval,
                },
            )
            logger.info(
                f"[增强回复链] route session_id={session_id} intent={intent_result.primaryIntent.value} "
                f"confidence={float(getattr(intent_result, 'confidence', 0.0) or 0.0):.2f} "
                f"route={main_route.route_name} observed_label={main_route.metadata.get('observability_label', main_route.route_name)} "
                f"execution_mode={main_route.metadata.get('execution_mode', 'standard_analysis')} "
                f"rag_need_retrieval={rag_need_retrieval} "
                f"retrieval_query={retrieval_query[:60]}"
            )

            stage_started_at = time.perf_counter()
            base_result = self._build_base_result(
                message,
                customer_name,
                enhanced_history,
                customer_data,
                intent_result,
            )
            self._record_perf_metric(perf_metrics, "build_base_result", stage_started_at)

            stage_started_at = time.perf_counter()
            result = self._standard_analysis(
                message, customer_name, enhanced_history,
                customer_data, base_result, intent_result,
                retrieval_query=retrieval_query,
                session_id=session_id,
                reply_objective=reply_objective,
                rag_need_retrieval=rag_need_retrieval,
                generation_mode=generation_mode,
            )
            self._record_perf_metric(perf_metrics, "analysis", stage_started_at)

            result["multi_turn"] = True
            result["dialogue_state"] = dialogue_result["state"]

            self._dialogue_manager.add_bot_response(
                session_id=session_id,
                response=result.get("reply", ""),
                matched_knowledge=result.get("matched_knowledge")
            )

            # 新增：持久化记忆存储
            try:
                if intent_result.primaryIntent in [IntentType.PURCHASE_INTENT, IntentType.COOPERATION_INTENT]:
                    self._memory_service.add_memory(
                        user_id=customer_id_for_context,
                        content=f"高意向客户: {message}",
                        memory_type="fact",
                        importance=0.9
                    )
                if intent_result.entities:
                    for entity_type, entity_values in intent_result.entities.items():
                        vals = entity_values if isinstance(entity_values, list) else [entity_values]
                        for val in vals:
                            self._memory_service.add_memory(
                                user_id=customer_id_for_context,
                                content=f"{entity_type}: {val}",
                                memory_type="fact",
                                importance=0.7
                            )
            except Exception as e:
                logger.debug(f"记忆存储失败: {e}")

            self._record_perf_metric(perf_metrics, "total", total_started_at)
            result.setdefault("reply_analysis", {})
            self._attach_reply_execution_routing(
                reply_analysis=result["reply_analysis"],
                main_decision=main_route,
                enhanced_result=result,
            )
            self._record_routing_decision(
                result["reply_analysis"],
                route_name=main_route.route_name,
                reason=main_route.reason,
                confidence=main_route.confidence,
                selected_strategy=main_route.selected_strategy,
                fallback_route=main_route.fallback_route,
                metadata={
                    **(main_route.metadata or {}),
                    "conversion_stage": reply_objective.conversion_stage.value,
                    "next_best_action": reply_objective.next_best_action.value,
                },
            )
            result["reply_objective"] = {
                "conversion_stage": reply_objective.conversion_stage.value,
                "next_best_action": reply_objective.next_best_action.value,
                "cta_mode": reply_objective.cta_mode.value,
                "missing_slots": list(reply_objective.missing_slots or []),
                "reason": reply_objective.reason,
            }
            result["reply_analysis"]["process_perf"] = copy.deepcopy(perf_metrics)
            self._log_perf_metrics(
                "process_message",
                perf_metrics,
                session_id=session_id,
                intent=intent_result.primaryIntent.value,
                final_action=result.get("reply_analysis", {}).get("retrieval", {}).get("final_action"),
            )
            return result

        except Exception as e:
            logger.error(f"增强客服处理失败: {e}", exc_info=True)
            try:
                fallback_intent = self.intent_recognizer.recognize(
                    message,
                    customer_data.get("sec_uid", "") if customer_data else "",
                    context=None,
                    enterpriseId=self._resolve_enterprise_id(customer_data, conversation_history),
                )
                result = self._build_base_result(
                    message,
                    customer_name,
                    conversation_history or [],
                    customer_data or {},
                    fallback_intent,
                )
                if result and isinstance(result, dict):
                    result["learning_error"] = str(e)
                    # Phase 4 兜底链：保证异常路径也有非空回复
                    if not result.get("reply") or not isinstance(result.get("reply"), str) or not result["reply"].strip():
                        try:
                            from src.common.fallback_reply_service import get_fallback_reply_service
                            universal = get_fallback_reply_service().get_universal_safe_reply(
                                customer_name=customer_name,
                                user_message=message,
                            )
                            if universal:
                                result["reply"] = universal
                                result["source"] = "exception_universal_fallback"
                                result["need_human"] = False
                            else:
                                result["reply"] = ""
                                result["need_human"] = True
                        except Exception as inner_e:
                            logger.error(f"universal_safe_reply 也失败: {inner_e}")
                            result["reply"] = ""
                            result["need_human"] = True
                else:
                    result = {"reply": "", "intent": "unknown", "need_human": True}
            except Exception as base_e:
                logger.error(f"基础客服降级也失败: {base_e}")
                # Phase 4 最后兜底：直接构造安全回复
                try:
                    from src.common.fallback_reply_service import get_fallback_reply_service
                    universal = get_fallback_reply_service().get_universal_safe_reply(
                        customer_name=customer_name,
                        user_message=message,
                    )
                    result = {
                        "reply": universal or "您说的问题我已收到，稍后给您具体回复。",
                        "intent": "unknown",
                        "need_human": True,
                        "source": "deepest_universal_fallback",
                        "error": str(e),
                    }
                except Exception:
                    result = {"reply": "您说的问题我已收到，稍后给您具体回复。", "intent": "unknown", "need_human": True, "error": str(e)}
            return result

    def _collect_retrieval_contexts(
        self,
        *,
        message: str,
        search_query: str,
        conversation_history: List[Dict],
        customer_data: Dict,
        intent_result,
        rag_need_retrieval: bool,
        rag_complexity: float,
        record_learning_match: bool,
        reply_objective: Optional[ReplyObjective] = None,
    ) -> Tuple[Any, Dict[str, Any], List[Dict]]:
        """统一执行单主链知识检索与 CRAG 过滤。"""
        retrieval_stage = getattr(self, "_retrieval_stage", None)
        if retrieval_stage is None:
            retrieval_stage = RetrievalStage()
            self._retrieval_stage = retrieval_stage
        return retrieval_stage.collect(
            service=self,
            message=message,
            search_query=search_query,
            conversation_history=conversation_history,
            customer_data=customer_data,
            intent_result=intent_result,
            rag_need_retrieval=rag_need_retrieval,
            rag_complexity=rag_complexity,
            record_learning_match=record_learning_match,
            reply_objective=reply_objective,
        )

    def _build_contextual_reply_prompt(
        self,
        *,
        mode: str,
        context_text: str,
        structured_bundle_text: str,
        history_text: str,
        message: str,
        enterprise_id: str = "",
    ) -> str:
        """构建基于检索上下文的 LLM 回复提示词。"""
        prompt_profile = self._build_schema_prompt_profile(mode, enterprise_id=enterprise_id)
        domain_rules_text = "\n".join(
            f"{index}. {rule}" for index, rule in enumerate(prompt_profile["domain_rules"], start=13)
        )
        evidence_priority = ""
        evidence_block = f"知识片段：\n{context_text}" if context_text else "知识片段：\n无"
        if structured_bundle_text:
            structured_bundle_priority = prompt_profile["structured_bundle_priority"]
            if "证据包类型：route" in structured_bundle_text or "关联线路：" in structured_bundle_text:
                structured_bundle_priority = "方案、价格、说明、包含项和适合对象"
            evidence_priority = (
                f"0. 如果提供了“结构化证据包”，优先依据其中的{structured_bundle_priority}作答；"
                "只有证据包没覆盖的细节，才参考后面的知识片段。\n"
            )
            evidence_block = f"结构化证据包（优先使用）：\n{structured_bundle_text}\n\n{evidence_block}"

        if mode == "sales":
            stage_guidance = self._build_sales_stage_guidance(message, history_text)
            return f"""{prompt_profile['persona']}

当前行业：{prompt_profile['display_name']}

规则：
{evidence_priority}1. 只使用知识库中提供的信息，主动推荐匹配当前需求的{prompt_profile['entity_label']}或方案
2. 回答要围绕客户当前追问点，优先答清价格、特点、适用对象、范围或交付方式，不要答非所问
3. 回答控制在50-150字以内，简洁有力
4. 优先只回答知识库里已经出现的价格、政策、能力边界和服务细节，不要补充上下文外的新事实
5. 只有在知识库明显没有覆盖用户核心问题时，才做泛化引导；不要补充固定报价、政策口径或未给出的产品事实
6. 不要使用"根据知识库"等元描述，直接回答问题
7. 绝对不要讨论数学计算、分数、方程式等数学问题
8. 不要说"正在查询"、"请稍等"等敷衍话术，直接给出可回答的信息
9. 像销售一样主动引导成交，但不能为了成交编造未提供的信息
10. {prompt_profile['sales_focus']}
11. 不要一上来反复索要敏感联系方式；只有当用户明确要资料、报价、预约、排期、锁定或通知时，才顺势推进留资
12. 回复口吻要像真人顾问在聊天，少用系统播报腔
13. 如果知识片段带有 [1] [2] 等编号标记，在回复末尾用"参考：[1][2]"的形式标注本次回答引用了哪些片段；若无编号标记则省略
{domain_rules_text}

当前成交阶段建议：
{stage_guidance}

检索证据：
{evidence_block}

对话历史：
{history_text}

用户问题：{message}

请直接给出回答："""

        return f"""{prompt_profile['persona']}

当前行业：{prompt_profile['display_name']}

规则：
{evidence_priority}1. 只使用知识库中提供的信息，严禁编造或添加知识库以外的内容
2. 用自然语言组织答案，不要直接复制原文，但必须保持信息准确
3. 回答控制在50-150字以内，简洁明了
4. 如果知识库内容不足以回答，请建议联系人工客服获取更详细的信息
5. 不要使用"根据知识库"等元描述，直接回答问题
6. 绝对不要讨论数学计算、分数、方程式等数学问题，即使输入看起来像数学表达式
7. 如果用户输入看起来像日期（如03/22、2025/07/03），请理解为日期而非数学表达式
8. 如果对话历史中出现数学相关讨论，忽略它，只关注 {prompt_profile['display_name']} 相关问题
9. 如果知识片段带有 [1] [2] 等编号标记，在回复末尾用"参考：[1][2]"的形式标注本次回答引用了哪些片段；若无编号标记则省略
{domain_rules_text}

检索证据：
{evidence_block}

对话历史：
{history_text}

用户问题：{message}

请直接给出回答："""

    def _build_llm_only_prompt(
        self,
        *,
        mode: str,
        history_text: str,
        message: str,
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        resolution_mode: str = "",
        is_general_qa: Optional[bool] = None,
    ) -> str:
        """构建无检索上下文时的 LLM 兜底提示词。"""
        prompt_profile = self._build_schema_prompt_profile(
            mode,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            resolution_mode=resolution_mode,
        )
        domain_rules_text = "\n".join(
            f"{index}. {rule}" for index, rule in enumerate(prompt_profile["domain_rules"], start=10)
        )
        if is_general_qa is None:
            is_general_qa = mode != "sales" and self._should_skip_mainline_retrieval(message, [])
        if mode == "sales":
            stage_guidance = self._build_sales_stage_guidance(message, history_text)
            return f"""{prompt_profile['persona']}

当前行业：{prompt_profile['display_name']}

规则：
1. 回答控制在50-150字以内，简洁有力
2. 优先根据用户当前需求做泛化引导；没有检索证据时不要编造固定价格、政策口径或产品事实
3. 根据客户需求推荐方向，用特点、适用对象和差异点引导成交
4. 不要说"正在查询"、"请稍等"等敷衍话术，直接给出产品信息
5. 绝对不要讨论数学计算、分数、方程式等数学问题
6. 像销售一样主动引导成交
7. {prompt_profile['sales_focus']}
8. 如果用户已经表现出高意向，如要报价、排期、预约、锁定、资料或通知，可以自然引导留一个方便接收资料的联系方式
9. 口吻要像真人顾问在聊天，不要写成产品说明书
10. 不要使用“这个问题我先不猜测”“根据现有资料”“我先帮您记下需求”这类模板化兜底句式，要直接回答能回答的部分，再自然追问缺失信息
{domain_rules_text}

当前成交阶段建议：
{stage_guidance}

对话历史：
{history_text}

用户问题：{message}

请直接给出回答："""

        if is_general_qa:
            return f"""{_DEFAULT_SCHEMA_REPLY_PROFILE['general_qa_persona']}

规则：
1. 直接回答用户问题，优先给出明确结论，再补一句必要说明
2. 不要代入销售、客服、顾问、产品推荐、人工转接等业务身份
3. 不要编造不确定的事实；如果拿不准，就明确说明不确定
4. 不要输出“根据现有资料”“我先帮您记下需求”“建议联系专业顾问”这类业务化模板
5. 不要主动推荐产品、方案、报价、留联系方式或引导成交
6. 回答尽量自然，像正常问答，不要写成客服话术
7. 如果对话历史与当前问题无关，忽略历史，只回答当前问题

对话历史：
{history_text}

用户问题：{message}

请直接给出答案："""

        return f"""{prompt_profile['persona']}

当前行业：{prompt_profile['display_name']}

规则：
1. 回答控制在50-150字以内，简洁明了
2. 如果不确定答案，请建议联系人工客服获取准确信息
3. 不要编造具体的产品名称、价格或政策细节
4. 保持礼貌专业的语气
5. 绝对不要讨论数学计算、分数、方程式等数学问题
6. 如果用户输入看起来像日期（如03/22），请理解为日期而非数学表达式
7. 如果对话历史中出现数学相关讨论，忽略它，只关注 {prompt_profile['display_name']} 相关问题
8. 不要使用“这个问题我先不猜测”“根据现有资料”“我先帮您记下需求”这类模板化兜底句式，要先回答能确认的部分，再明确说明还缺什么信息
{domain_rules_text}

对话历史：
{history_text}

用户问题：{message}

请直接给出回答："""

    def _generate_llm_only_fallback_reply(
        self,
        *,
        mode: str,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        reply_analysis: Optional[Dict[str, Any]] = None,
        enterprise_id: str = "",
        schema_id: str = "",
        resolution_mode: str = "",
    ) -> Optional[str]:
        """无检索证据时，统一走 LLM 兜底回复。"""
        perf_metrics: Dict[str, float] = {}
        stage_started_at = time.perf_counter()
        fallback_reason = ""
        if isinstance(reply_analysis, dict):
            fallback_reason = str((reply_analysis.get("fallback") or {}).get("reason") or "knowledge_not_found")
        effective_schema_id = str(schema_id or "").strip()
        if not effective_schema_id:
            effective_schema_id = self._extract_schema_id_from_reply_analysis(reply_analysis)
        retrieval_meta = (reply_analysis or {}).get("retrieval") if isinstance(reply_analysis, dict) else {}
        is_general_qa = bool(
            isinstance(retrieval_meta, dict)
            and (
                str(retrieval_meta.get("skip_reason", "") or "").strip() == "general_qa"
                or str(retrieval_meta.get("branch", "") or "").strip() == "pure_llm"
            )
        )
        bypass_fail_soft = self._should_bypass_fail_soft_for_llm(
            mode,
            fallback_reason,
            message,
            reply_analysis,
            enterprise_id,
            effective_schema_id,
        )
        if (
            (
                not bypass_fail_soft
                and self._is_no_evidence_fail_soft_reason(fallback_reason)
            )
            or (
                not bypass_fail_soft
                and self._should_fail_soft_weak_evidence_retry(
            message=message,
            reply_analysis=reply_analysis,
            reason=fallback_reason,
                )
            )
        ):
            fail_soft_reply = self._build_no_evidence_fail_soft_reply(
                mode=mode,
                message=message,
                conversation_history=conversation_history,
                reason=fallback_reason,
            ).strip()
            if isinstance(reply_analysis, dict):
                reply_analysis["rag_llm"] = {
                    "source": "llm_only_fail_soft",
                    "model": "policy_template",
                    "contexts_count": 0,
                    "gate_action": (reply_analysis.get("retrieval") or {}).get("final_action", "pass"),
                    "reply": fail_soft_reply,
                }
                reply_analysis["generation_perf"] = copy.deepcopy(perf_metrics)
                reply_analysis["fallback"] = {
                    "source": "llm_only_fail_soft" if fail_soft_reply else "llm_only_unavailable",
                    "reason": fallback_reason if fail_soft_reply else f"{fallback_reason}|empty_fail_soft_reply",
                }
                self._append_trace_span(
                    reply_analysis,
                    name="reply_generation_without_context",
                    duration_ms=sum(perf_metrics.values()),
                    metadata={
                        "source": "llm_only_fail_soft",
                        "reply_present": bool(fail_soft_reply),
                    },
                )
            logger.info(f"无证据场景触发 fail-soft 回复: reason={fallback_reason} message={message[:30]}")
            return fail_soft_reply or None

        from .llm_service import get_default_llm_provider

        llm_provider = get_default_llm_provider()
        self._record_perf_metric(perf_metrics, "get_llm_provider_without_context", stage_started_at)
        if not llm_provider:
            if isinstance(reply_analysis, dict):
                reply_analysis["fallback"] = {
                    "source": "llm_only_unavailable",
                    "reason": str((reply_analysis.get("fallback") or {}).get("reason") or "llm_provider_missing"),
                }
            logger.warning("无证据 LLM 兜底不可用：未获取到 llm_provider")
            return None

        history_lines: List[str] = []
        for msg in reversed(conversation_history or []):
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            role = "用户" if msg.get("direction") == "inbound" else "助手"
            history_lines.insert(0, f"{role}：{content}")
            if len(history_lines) >= 3:
                break
        history_text = "\n".join(history_lines)
        prompt = self._build_llm_only_prompt(
            mode=mode,
            history_text=history_text,
            message=message,
            enterprise_id=enterprise_id,
            preferred_schema_id=effective_schema_id,
            resolution_mode=resolution_mode,
            is_general_qa=is_general_qa,
        )
        preferred_primary_model = str(os.getenv("OLLAMA_MODEL", "qwen3.5:4b") or "qwen3.5:4b").strip()
        model_name = f"ollama/{getattr(llm_provider, 'model', type(llm_provider).__name__)}"

        stage_started_at = time.perf_counter()
        from src.common.utils import call_llm_safe

        reply = call_llm_safe(
            llm_provider,
            prompt,
            timeout=LLM_ONLY_FALLBACK_TIMEOUT_SECONDS,
            model=preferred_primary_model,
            num_ctx=OLLAMA_NUM_CTX,
            num_predict=OLLAMA_NUM_PREDICT,
            temperature=OLLAMA_TEMPERATURE,
            top_p=OLLAMA_TOP_P,
            top_k=OLLAMA_TOP_K,
        )
        self._record_perf_metric(perf_metrics, "llm_only_chat", stage_started_at)
        normalized_reply = str(reply or "").strip()

        if isinstance(reply_analysis, dict):
            reply_analysis["rag_llm"] = {
                "source": "llm_only",
                "model": f"ollama/{preferred_primary_model}" if preferred_primary_model else model_name,
                "contexts_count": 0,
                "gate_action": (reply_analysis.get("retrieval") or {}).get("final_action", "pass"),
                "reply": normalized_reply,
            }
            reply_analysis["generation_perf"] = copy.deepcopy(perf_metrics)
            reply_analysis["fallback"] = {
                "source": "llm_only_fallback" if normalized_reply else "llm_only_unavailable",
                "reason": fallback_reason if normalized_reply else f"{fallback_reason}|empty_llm_only_reply",
            }
            self._append_trace_span(
                reply_analysis,
                name="reply_generation_without_context",
                duration_ms=sum(perf_metrics.values()),
                metadata={
                    "source": "llm_only",
                    "reply_present": bool(normalized_reply),
                },
            )

        self._log_perf_metrics(
            "generate_reply_without_context",
            perf_metrics,
            mode=mode,
            reply_present=bool(normalized_reply),
        )
        if not normalized_reply:
            logger.warning(f"无证据 LLM 兜底回复为空: message={message[:30]}")
            return None
        logger.info(f"无证据 LLM 兜底生成回复: message={message[:30]}")
        return normalized_reply

    def _build_sales_stage_guidance(self, message: str, history_text: str) -> str:
        """根据当前消息和历史，为销售回复提供阶段性推进建议。"""
        # 只提取用户消息做阶段判断，避免助手历史回复中的关键词误导阶段判定
        user_only_lines = []
        for line in history_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("用户：") or stripped.startswith("用户:"):
                user_only_lines.append(stripped)
        user_history_text = "\n".join(user_only_lines)
        text = f"{user_history_text}\n{message}".lower()

        # 通用高意向词（跨行业通用），行业专用高意向词（如旅游的 查余位/报名/明天出发/上车通知）
        # 由 schema query_understanding.high_intent_markers 提供
        high_intent_keywords = [
            "预留", "锁定", "先帮我留",
        ]
        high_intent_keywords.extend(self._get_schema_term_list("high_intent_markers"))
        # 通用顾虑词（跨行业通用），行业专用顾虑词（如旅游的 踩坑/怕被坑/会不会太赶）
        # 由 schema query_understanding.objection_markers 提供
        objection_keywords = [
            "贵", "预算", "便宜点", "商量", "家里人",
        ]
        objection_keywords.extend(self._get_schema_term_list("objection_markers"))
        comparison_keywords = [
            "哪个好", "怎么选", "区别", "对比",
        ]
        # 通用需求摸底词（跨行业通用），行业专用需求摸底词（如旅游的 想去/怎么玩/有什么线路/带老人/带小孩）
        # 由 schema query_understanding.discovery_markers 提供
        discovery_keywords = [
            "推荐", "适合",
        ]
        discovery_keywords.extend(self._get_schema_term_list("discovery_markers"))

        if any(keyword.lower() in text for keyword in high_intent_keywords):
            return "当前更像高意向成交阶段：优先确认时间、人数、具体对象和可安排情况，回答后顺势推进完整资料或后续说明。"
        if any(keyword.lower() in text for keyword in objection_keywords):
            return "当前更像顾虑化解阶段：先回应预算、顾虑、商量或节奏担忧，再收集时间、人数和关注点，最后自然推进留接收资料的联系方式。"
        if any(keyword.lower() in text for keyword in comparison_keywords):
            return "当前更像比价对比阶段：先讲清两个方案的核心差别、适合对象和价格逻辑，再推动用户给出时间人数，方便发完整对比资料。"
        if any(keyword.lower() in text for keyword in discovery_keywords):
            return "当前更像需求摸底阶段：先收集时间、人数、关注方向和是否有同行限制，再推荐最匹配的方案。"
        return "当前更像普通咨询阶段：先缩小选择范围，再根据客户反馈推进资料、可安排情况或留资。"

    def _build_knowledge_only_fallback_reply(
        self,
        *,
        message: str,
        matched_knowledge,
        retrieved_contexts: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """唯一非 LLM 兜底：命中知识时直接返回清洗后的知识库答案。"""
        normalized_message = self._normalize_query_text(self._strip_preview_noise_prefix(message or ""))
        is_broad_domain_discovery = self._is_broad_domain_opening(normalized_message)

        candidate_contexts = []
        for ctx in retrieved_contexts or []:
            if not isinstance(ctx, dict):
                continue
            answer = str(ctx.get("answer") or "").strip()
            if not answer:
                continue
            candidate_contexts.append(ctx)

        if candidate_contexts:
            preferred_contexts = candidate_contexts
            if is_broad_domain_discovery:
                domain_keywords = self._get_industry_strategy().get_domain_keywords()
                if domain_keywords:
                    broad_overview_contexts = [
                        ctx
                        for ctx in candidate_contexts
                        if sum(1 for kw in domain_keywords if kw in str(ctx.get("answer") or "") or kw in str(ctx.get("question") or "")) >= 2
                    ]
                    if broad_overview_contexts:
                        preferred_contexts = broad_overview_contexts
            if not matched_knowledge or is_broad_domain_discovery:
                reply = self._sanitize_knowledge_answer_for_customer(
                    message,
                    str(preferred_contexts[0].get("answer") or ""),
                )
                if str(reply or "").strip():
                    return str(reply or "").strip()

        if not matched_knowledge or not getattr(matched_knowledge, "answer", ""):
            return None
        reply = self._sanitize_knowledge_answer_for_customer(message, matched_knowledge.answer)
        return str(reply or "").strip() or None

    @staticmethod
    def _is_llm_only_fallback_enabled() -> bool:
        return bool(ENABLE_LLM_ONLY_FALLBACK)

    @staticmethod
    def _is_safe_clarification_fallback_answer(answer: str) -> bool:
        normalized = str(answer or "").strip()
        if not normalized:
            return False
        clarification_markers = ("请补充", "方便补充", "告诉我", "发我", "留个")
        slot_markers = ("日期", "人数", "时间", "联系方式", "几个人", "出发")
        return (
            any(marker in normalized for marker in clarification_markers)
            and any(marker in normalized for marker in slot_markers)
        )

    @staticmethod
    def _is_no_evidence_fail_soft_reason(reason: str) -> bool:
        normalized = str(reason or "").strip().lower()
        if not normalized:
            return False
        no_evidence_markers = (
            "empty_contexts",
            "insufficient_evidence",
            "rewrite_failed_evidence_insufficient",
        )
        return any(marker in normalized for marker in no_evidence_markers)

    @staticmethod
    def _is_weak_evidence_fail_soft_reason(reason: str) -> bool:
        normalized = str(reason or "").strip().lower()
        if not normalized:
            return False
        weak_evidence_markers = (
            "weak_evidence_retry",
            "rewrite_failed_preserve_initial",
        )
        return any(marker in normalized for marker in weak_evidence_markers)

    @staticmethod
    def _get_retry_fallback_contexts(reply_analysis: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(reply_analysis, dict):
            return []
        retrieval = reply_analysis.get("retrieval") or {}
        contexts = retrieval.get("contexts") or []
        return [ctx for ctx in contexts if isinstance(ctx, dict)]

    def _has_cross_schema_retry_context_mismatch(
        self,
        reply_analysis: Optional[Dict[str, Any]],
        *,
        enterprise_id: str = "",
    ) -> bool:
        contexts = self._get_retry_fallback_contexts(reply_analysis)
        if not contexts:
            return False
        normalized_enterprise = str(enterprise_id or "").strip()
        if not normalized_enterprise and isinstance(reply_analysis, dict):
            normalized_enterprise = str(
                ((reply_analysis.get("retrieval") or {}).get("enterprise_id") or "")
            ).strip()
        if not normalized_enterprise:
            return False
        try:
            active_schema_id = str(
                get_industry_schema_service().get_effective_active_schema_id(
                    enterprise_id=normalized_enterprise
                )
                or ""
            ).strip()
        except Exception:
            active_schema_id = ""
        if not active_schema_id:
            return False
        context_schema_ids = {
            str(ctx.get("schema_id") or "").strip()
            for ctx in contexts
            if str(ctx.get("schema_id") or "").strip()
        }
        if not context_schema_ids:
            return False
        return any(schema_id != active_schema_id for schema_id in context_schema_ids)

    def _has_low_fidelity_retry_context(
        self,
        reply_analysis: Optional[Dict[str, Any]],
    ) -> bool:
        contexts = self._get_retry_fallback_contexts(reply_analysis)
        if not contexts:
            return True
        top_ctx = sorted(
            contexts,
            key=lambda item: float(item.get("relevance_score", 0.0) or 0.0),
            reverse=True,
        )[0]
        top_relevance = float(top_ctx.get("relevance_score", 0.0) or 0.0)
        keyword_overlap = float(top_ctx.get("keyword_overlap", 0.0) or 0.0)
        char_overlap = float(top_ctx.get("char_overlap", 0.0) or 0.0)
        category_match = float(top_ctx.get("category_match", 0.0) or 0.0)
        weak_retry_cutoff = (
            float(getattr(self, "CRAG_RETRY_THRESHOLD", 0.36) or 0.36)
            + float(getattr(self, "CRAG_PASS_THRESHOLD", 0.45) or 0.45)
        ) / 2
        return bool(
            (
                top_relevance < weak_retry_cutoff
                and keyword_overlap < 0.12
                and char_overlap < 0.08
            )
            or (
                keyword_overlap <= 0.01
                and char_overlap <= 0.02
            )
            or (
                top_relevance < float(getattr(self, "CRAG_PASS_THRESHOLD", 0.45) or 0.45)
                and category_match <= 0.0
                and keyword_overlap < 0.05
                and char_overlap < 0.05
            )
        )

    def _should_fail_soft_weak_evidence_retry(
        self,
        *,
        message: str,
        reply_analysis: Optional[Dict[str, Any]],
        reason: str,
    ) -> bool:
        if not self._is_weak_evidence_fail_soft_reason(reason):
            return False
        enterprise_id = ""
        if isinstance(reply_analysis, dict):
            enterprise_id = str(
                ((reply_analysis.get("retrieval") or {}).get("enterprise_id") or "")
            ).strip()
        has_cross_schema_mismatch = self._has_cross_schema_retry_context_mismatch(
            reply_analysis,
            enterprise_id=enterprise_id,
        )
        is_contact_or_material_query = self._is_contact_or_material_query(
            message,
            enterprise_id=enterprise_id,
        )
        is_high_risk = self._is_high_risk_detail_query(message)
        if has_cross_schema_mismatch and (is_high_risk or is_contact_or_material_query):
            return True
        if is_contact_or_material_query and self._has_low_fidelity_retry_context(reply_analysis):
            return True
        if not is_high_risk:
            return False
        return bool(
            self._has_low_fidelity_retry_context(reply_analysis)
        )

    def _is_high_risk_detail_query(self, message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        # 通用高风险细节词（跨行业通用），行业专用细节词（如旅游的 门票/行程）
        # 由 schema query_understanding.high_risk_detail_terms 提供
        detail_terms = [
            "多少钱", "价格", "费用", "报价", "票价",
            "套餐", "方案", "明细", "流程", "步骤",
            "怎么安排", "开通", "资料", "需要什么",
        ]
        detail_terms.extend(self._get_schema_term_list("high_risk_detail_terms"))
        return any(term in text for term in detail_terms)

    def _build_no_evidence_fail_soft_reply(
        self,
        *,
        mode: str,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        reason: str = "",
    ) -> str:
        # 修复：保留 conversation_history，生成上下文感知的 fallback 回复
        del mode
        # 提取最近的话题上下文关键词，用于生成上下文感知回复
        context_topic = ""
        if conversation_history:
            recent_user_msgs = [
                str(item.get("content") or item.get("message") or "").strip()
                for item in conversation_history[-3:]
                if isinstance(item, dict)
                and str(item.get("role") or item.get("sender") or "").lower() in {"user", "customer"}
            ]
            if recent_user_msgs:
                # 取最近一条用户消息的前 12 个字符作为话题上下文
                last_msg = recent_user_msgs[-1]
                context_topic = last_msg[:12] if len(last_msg) > 12 else last_msg

        if self._is_high_risk_detail_query(message):
            prefix = f"关于您提到的「{context_topic}」，" if context_topic else ""
            return (
                f"{prefix}这类价格、开通或方案细节，我这边暂时没有核对到可直接支撑的知识依据，"
                "先不能直接给您报具体口径。您可以补充具体产品或服务名称、时间范围和关注点，"
                "我再继续核对；如果需要立即确认准确信息，建议走人工确认。"
            )
        if str(reason or "").startswith("technical_"):
            prefix = f"关于您提到的「{context_topic}」，" if context_topic else ""
            return f"{prefix}我这边暂时没有核对到可直接支撑当前问题的知识依据，您可以补充更具体的信息，我再继续核对。"
        prefix = f"关于您提到的「{context_topic}」，" if context_topic else ""
        return f"{prefix}我这边暂时没有核对到可直接支撑当前问题的知识依据，您可以补充具体产品或服务名称、时间或需求范围，我再继续核对。"

    def _should_bypass_fail_soft_for_llm(
        self,
        mode: str,
        reason: str,
        message: str,
        reply_analysis: Optional[Dict[str, Any]],
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> bool:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode != "service":
            return False
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason == "rewrite_failed_evidence_insufficient":
            return False
        effective_enterprise_id = str(enterprise_id or "").strip()
        effective_schema_id = str(schema_id or "").strip()
        if not effective_enterprise_id and isinstance(reply_analysis, dict):
            effective_enterprise_id = str(
                ((reply_analysis.get("retrieval") or {}).get("enterprise_id") or "")
            ).strip()
        if not effective_schema_id:
            effective_schema_id = self._extract_schema_id_from_reply_analysis(reply_analysis)
        final_action = ""
        if isinstance(reply_analysis, dict):
            final_action = str(
                ((reply_analysis.get("retrieval") or {}).get("final_action") or "")
            ).strip().lower()
        if self._is_contact_or_material_query(
            message,
            enterprise_id=effective_enterprise_id,
            preferred_schema_id=effective_schema_id,
        ):
            return False
        if normalized_reason == "empty_contexts":
            return final_action == "no_answer" and not self._is_high_risk_detail_query(message)
        if normalized_reason == "insufficient_evidence":
            return not self._is_high_risk_detail_query(message)
        if normalized_reason == "rewrite_failed_preserve_initial":
            return not effective_enterprise_id and not effective_schema_id
        return False

    def _should_allow_llm_only_fallback_for_generation(
        self,
        *,
        mode: str,
        message: str,
        reply_analysis: Optional[Dict[str, Any]],
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> bool:
        if not self._is_llm_only_fallback_enabled():
            return False
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode != "service":
            return True
        retrieval = (reply_analysis or {}).get("retrieval") if isinstance(reply_analysis, dict) else {}
        retrieval = retrieval if isinstance(retrieval, dict) else {}
        final_action = str(retrieval.get("final_action") or "").strip().lower()
        branch = str(retrieval.get("branch") or "").strip().lower()
        skip_reason = str(retrieval.get("skip_reason") or "").strip().lower()
        final_reason = str(
            ((reply_analysis or {}).get("fallback") or {}).get("reason")
            or retrieval.get("final_reason")
            or "knowledge_not_found"
        ).strip().lower()
        effective_enterprise_id = str(enterprise_id or "").strip() or str(retrieval.get("enterprise_id") or "").strip()
        effective_schema_id = str(schema_id or "").strip() or self._extract_schema_id_from_reply_analysis(reply_analysis)
        is_general_qa = branch == "pure_llm" or skip_reason == "general_qa"
        if self._is_contact_or_material_query(
            message,
            enterprise_id=effective_enterprise_id,
            preferred_schema_id=effective_schema_id,
        ):
            return False
        if self._is_high_risk_detail_query(message):
            return False
        if final_action in {"retry", "no_answer"}:
            return is_general_qa and not self._is_no_evidence_fail_soft_reason(final_reason)
        return True

    def _generate_smart_reply(
        self,
        *,
        mode: str,
        message: str,
        retrieval_query: Optional[str],
        enterprise_id: str = "",
        session_id: str = "",
        conversation_history: List[Dict],
        matched_knowledge,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict],
        reply_objective: Optional[ReplyObjective] = None,
    ) -> Optional[str]:
        """基于检索上下文与主链生成策略生成回复。"""
        generation_stage = getattr(self, "_generation_stage", None)
        if generation_stage is None:
            generation_stage = GenerationStage()
            self._generation_stage = generation_stage
        return generation_stage.generate(
            service=self,
            mode=mode,
            message=message,
            retrieval_query=retrieval_query,
            enterprise_id=enterprise_id,
            session_id=session_id,
            conversation_history=conversation_history,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
            reply_objective=reply_objective,
        )

    @staticmethod
    def _looks_like_hidden_reasoning_leak(text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False
        lowered = content.lower()
        strong_markers = [
            r"\bthinking process\b",
            r"\banalyze the request\b",
            r"\*\*role:\*\*",
            r"\*\*task:\*\*",
            r"\*\*industry:\*\*",
            r"\*\*constraints:\*\*",
            r"\bprofessional sales consultant\b",
            r"\bgeneral service sales\b",
            r"\btravel/tourism\b",
            r"\btourism/travel\b",
        ]
        matched = sum(1 for pattern in strong_markers if re.search(pattern, lowered, re.IGNORECASE))
        if matched >= 2:
            return True
        if lowered.startswith("thinking process") or lowered.startswith("1. **analyze the request:**"):
            return True
        return False

    @staticmethod
    def _sanitize_outbound_text(text: str) -> str:
        """抖音平台合规性过滤：自动替换敏感词汇（电话、微信等）"""
        if not text:
            return ""
        
        # 修复 R10：改为上下文感知替换，避免误伤"电话咨询"等正常用词
        replacements = [
            (r"微信号", "联系账号"),
            (r"手机号", "联系号码"),
            (r"加微信", "留个接收资料的联系方式"),
            (r"加我微信", "留个联系方式"),
            # 只替换"加微"（加微信的简称），不替换单独的"微"
            (r"加微(?!信)", "联系"),
            # 只替换作为联系方式的"微信"，不替换"微信公众号"等
            (r"(?<!公众)微信(?!公众号|小程序|支付)", "接收资料的方式"),
            # 只替换"加我"后跟联系意图的，不替换"加我好友"等
            (r"加我(?!好友|微信|们)", "联系我"),
            # 只替换"拨打+数字"的拨号场景，不替换"拨打热线"等
            (r"拨打(\d)", "拨至\1"),
            # 注意：不再替换单独的"电话"，避免误伤"电话咨询""电话客服"等正常用词
        ]
        
        sanitized = text
        for pattern, replacement in replacements:
            sanitized = re.sub(pattern, replacement, sanitized)
            
        return sanitized

    def _apply_generated_reply(
        self,
        *,
        enhanced_result: Dict[str, Any],
        smart_reply: Optional[str],
        matched_knowledge,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict[str, Any]],
        message: str,
        conversation_history: Optional[List[Dict]],
        session_id: Optional[str],
        customer_name: str = "",
    ) -> None:
        """把生成结果回写到统一结果对象，并记录学习数据。"""
        guardrail_stage = getattr(self, "_guardrail_stage", None)
        if guardrail_stage is None:
            guardrail_stage = GuardrailStage()
            self._guardrail_stage = guardrail_stage
        return guardrail_stage.apply(
            service=self,
            enhanced_result=enhanced_result,
            smart_reply=smart_reply,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
            message=message,
            conversation_history=conversation_history,
            session_id=session_id,
            customer_name=customer_name,
        )

    def _build_analysis_result(
        self,
        *,
        base_result: Dict[str, Any],
        intent_result,
        intent_level: str,
        priority_decision: Dict[str, Any],
        reasoning: str,
        suggested_action: str,
        need_human: bool,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构建分析阶段的统一结果对象。"""
        enhanced_result = copy.deepcopy(base_result)
        enhanced_result["intent"] = intent_result.primaryIntent.value
        enhanced_result["intent_level"] = intent_level
        enhanced_result["priority_level"] = priority_decision.get("priority_level", "normal")
        enhanced_result["reasoning"] = reasoning
        enhanced_result["suggested_action"] = suggested_action
        enhanced_result["sentiment"] = intent_result.sentiment.value
        enhanced_result["urgency"] = intent_result.urgency.value
        enhanced_result["confidence"] = intent_result.confidence
        enhanced_result["need_human"] = need_human

        if extra_fields:
            enhanced_result.update(extra_fields)

        enhanced_result.setdefault("matched_knowledge", "")
        enhanced_result.setdefault("reply_analysis", {})
        enhanced_result.setdefault("retrieval_evidence", [])
        return enhanced_result

    def _standard_analysis(
        self,
        message: str,
        customer_name: str,
        conversation_history: List[Dict],
        customer_data: Dict,
        base_result: Dict,
        intent_result,
        retrieval_query: str = None,
        session_id: str = None,
        reply_objective: Optional[ReplyObjective] = None,
        rag_need_retrieval: bool = True,
        generation_mode: str = "sales",
    ) -> Dict[str, Any]:
        """标准分析：使用统一知识检索主链生成回复。"""
        enhanced_result = self._reply_orchestrator.run(
            service=self,
            mode=STANDARD_ANALYSIS_MODE,
            message=message,
            customer_name=customer_name,
            conversation_history=conversation_history,
            customer_data=customer_data,
            base_result=base_result,
            intent_result=intent_result,
            retrieval_query=retrieval_query,
            session_id=session_id,
            reply_objective=reply_objective,
            rag_need_retrieval=rag_need_retrieval,
            generation_mode=generation_mode,
        )

        logger.info(
            f"[增强回复链] done session_id={session_id} route=standard_analysis "
            f"need_human={bool(enhanced_result.get('need_human', False))} "
            f"reply_len={len(str(enhanced_result.get('reply', '') or ''))} "
            f"matched_knowledge={'yes' if enhanced_result.get('matched_knowledge') else 'no'}"
        )

        return enhanced_result

    def _run_proactive_service(
        self,
        customer_id: str,
        result: Dict[str, Any]
    ):
        """运行主动服务评估

        已禁用：不再主动推送消息给客户，只在客户主动发消息时回复。
        避免消息乱发问题。如需重新启用，参考 git 历史中的实现。
        """
        return

    def _run_learning(
        self,
        message: str,
        conversation_history: List[Dict],
        result: Dict,
        matched_knowledge,
        retrieved_contexts: List[Dict] = None
    ):
        """运行自学习流程"""
        try:
            if not self._learning_system.learning_enabled:
                return

            from types import SimpleNamespace
            rag_result = SimpleNamespace(
                answer=result.get("reply", ""),
                confidence=result.get("confidence", 0.5),
                sources=[matched_knowledge] if matched_knowledge else []
            )

            self._learning_system.learn_from_conversation(
                query=message,
                rag_result=rag_result,
                conversation_history=conversation_history,
                matched_knowledge=matched_knowledge,
                retrieved_contexts=retrieved_contexts,
            )

            self._run_rag_evaluation(message, result, matched_knowledge)
        except Exception as e:
            logger.error(f"学习流程执行失败: {e}")

    def _run_rag_evaluation(
        self,
        message: str,
        result: Dict[str, Any],
        matched_knowledge
    ):
        """运行RAG评估"""
        try:
            if not matched_knowledge:
                result["rag_metrics"] = {
                    "overall_score": 0.0,
                    "faithfulness": 0.0,
                    "answer_relevance": 0.0,
                    "context_relevance": 0.0,
                    "hallucination_score": 1.0,
                    "note": "无匹配知识，跳过评估"
                }
                return
            
            contexts = []
            if hasattr(matched_knowledge, 'answer') and matched_knowledge.answer:
                contexts = [matched_knowledge.answer]
            elif isinstance(matched_knowledge, dict) and matched_knowledge.get('answer'):
                contexts = [matched_knowledge['answer']]
            
            if not contexts:
                result["rag_metrics"] = {
                    "overall_score": 0.0,
                    "faithfulness": 0.0,
                    "answer_relevance": 0.0,
                    "context_relevance": 0.0,
                    "hallucination_score": 1.0,
                    "note": "知识条目无答案内容"
                }
                return

            eval_result = self._rag_evaluator.evaluate(
                query=message,
                answer=result.get("reply", ""),
                contexts=contexts
            )

            result["rag_metrics"] = {
                "overall_score": eval_result.overall_score,
                "faithfulness": eval_result.faithfulness,
                "answer_relevance": eval_result.answer_relevance,
                "context_relevance": eval_result.context_relevance,
                "hallucination_score": eval_result.hallucination_score
            }
        except Exception as e:
            logger.error(f"RAG评估失败: {e}")
            result["rag_metrics"] = {
                "overall_score": 0.0,
                "error": str(e)
            }

    def _inject_dialogue_context(
        self,
        conversation_history: List[Dict],
        dialogue_context: str
    ) -> List[Dict]:
        """注入多轮对话上下文"""
        if not conversation_history:
            conversation_history = []

        context_msg = {
            "role": "system",
            "content": f"[多轮上下文] {dialogue_context}",
            "timestamp": datetime.now().isoformat()
        }

        return conversation_history + [context_msg]

    def _estimate_retrieval_relevance(self, query: str, ctx: Dict[str, Any]) -> Dict[str, float]:
        """估算单条检索证据的相关性分解分数。"""
        query = query or ""
        question = str(ctx.get("question", "") or "")
        answer = str(ctx.get("answer", "") or "")
        category = str(ctx.get("category", "") or "")
        score = float(ctx.get("score", 0) or 0)

        generic_query_noise_terms = {
            "什么",
            "是什么",
            "什么意思",
            "是什么意思",
            "怎么",
            "怎么回事",
            "为什么",
            "为何",
            "哪里",
            "哪儿",
            "哪个",
            "哪些",
            "谁",
            "请问",
            "一下",
        }

        combined_text = f"{question} {answer}".lower()
        try:
            import jieba

            raw_query_tokens = [
                token.strip()
                for token in jieba.cut(query.lower())
                if len(token.strip()) >= 2
            ]
            text_keywords = {
                token.strip()
                for token in jieba.cut(combined_text)
                if len(token.strip()) >= 2
            }
            question_keywords = {
                token.strip()
                for token in jieba.cut(question.lower())
                if len(token.strip()) >= 2
            }
        except Exception:
            raw_query_tokens = [token for token in query.lower().split() if token]
            text_keywords = {token for token in combined_text.split() if token}
            question_keywords = {
                token for token in question.lower().split() if token
            }

        filtered_query_tokens: List[str] = []
        for token in raw_query_tokens:
            normalized_token = str(token or "").strip().lower()
            if not normalized_token:
                continue
            if normalized_token in generic_query_noise_terms:
                continue
            filtered_query_tokens.append(normalized_token)
        query_keywords = set(filtered_query_tokens)

        # 通用同义词组（跨行业通用），行业专用同义词由 schema query_understanding.synonym_groups 提供
        synonym_groups = [
            {"价格", "票价", "费用", "多少钱", "收费", "价位", "报价"},
            {"儿童", "小孩", "孩子", "幼儿"},
            {"老人", "老年人", "长者"},
            {"预订", "预定", "预约", "booking"},
            {"取消", "退订", "退款", "退票"},
            {"时间", "几点", "时长", "多久"},
            {"地址", "位置", "在哪里", "怎么去"},
            {"能", "可以", "能够", "可"},
        ]
        # 合并 schema 行业专用同义词组（如旅游的 游船/游轮/三峡 等）
        schema_synonyms = self._get_schema_synonym_groups()
        if schema_synonyms:
            synonym_groups.extend(schema_synonyms)
        def _expand_synonyms(tokens: set) -> set:
            expanded = set(tokens)
            for token in tokens:
                for group in synonym_groups:
                    if token in group:
                        expanded.update(group)
            return expanded

        query_keywords_expanded = _expand_synonyms(query_keywords)
        text_keywords_expanded = _expand_synonyms(text_keywords)

        overlap_query_text = "".join(filtered_query_tokens) or re.sub(
            r"(是什么意思|什么意思|是什么|怎么回事|为什么|哪里|哪儿|哪个|哪些|请问|一下)",
            "",
            query.lower(),
        )
        query_chars = set(overlap_query_text or query)

        char_overlap = 0.0
        if query_chars:
            text_chars = set(f"{question}{answer}")
            has_cjk_query = any('\u4e00' <= c <= '\u9fff' for c in overlap_query_text or query)
            if has_cjk_query:
                def _bigrams(s: str) -> set:
                    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}
                query_bg = _bigrams(overlap_query_text or query)
                text_bg = _bigrams(f"{question}{answer}")
                if query_bg:
                    bigram_overlap = len(query_bg & text_bg) / max(len(query_bg | text_bg), 1)
                else:
                    bigram_overlap = 0.0
                # 修复：bigram 为 0 时回退到 unigram（单字）重叠，避免"游船的价格"vs"儿童票价"完全无重叠
                if bigram_overlap == 0.0:
                    query_unigrams = set(c for c in (overlap_query_text or query) if '\u4e00' <= c <= '\u9fff')
                    text_unigrams = set(c for c in f"{question}{answer}" if '\u4e00' <= c <= '\u9fff')
                    if query_unigrams:
                        char_overlap = len(query_unigrams & text_unigrams) / max(len(query_unigrams | text_unigrams), 1)
                    else:
                        char_overlap = 0.0
                else:
                    char_overlap = bigram_overlap
            else:
                char_overlap = len(query_chars & text_chars) / max(len(query_chars | text_chars), 1)

        keyword_overlap = 0.0
        if query_keywords:
            # 修复：使用同义词扩展后的关键词集合计算重叠
            matched_keywords = len(query_keywords_expanded & text_keywords_expanded)
            keyword_overlap = matched_keywords / max(len(query_keywords), 1)
        question_keyword_overlap = 0.0
        if query_keywords:
            matched_question_keywords = len(query_keywords & question_keywords)
            question_keyword_overlap = matched_question_keywords / max(len(query_keywords), 1)

        if self._is_contact_or_material_query(
            query,
            enterprise_id=str(ctx.get("enterprise_id", "") or ""),
        ):
            normalized_query = str(query or "").strip().lower()
            normalized_question = str(question or "").strip().lower()
            anchor_terms = {
                term
                for term in self._get_contact_material_terms(
                    enterprise_id=str(ctx.get("enterprise_id", "") or ""),
                )
                if len(str(term or "").strip()) >= 2 and str(term or "").strip() not in {"发我"}
            }
            matched_anchor_terms = {
                term for term in anchor_terms
                if term and term in normalized_query
            }
            if matched_anchor_terms:
                matched_question_anchors = sum(
                    1 for term in matched_anchor_terms
                    if term in normalized_question
                )
                question_keyword_overlap = matched_question_anchors / max(len(matched_anchor_terms), 1)
            keyword_overlap = question_keyword_overlap

        query_categories = set()
        for category_name, keywords in get_active_query_category_hints(
            enterprise_id=str(ctx.get("enterprise_id", "") or ""),
            preferred_schema_id=str(ctx.get("schema_id", "") or ""),
        ).items():
            if any(keyword in query for keyword in keywords):
                query_categories.add(category_name)

        category_match = 0.0
        if query_categories:
            if category in query_categories:
                category_match = 1.0
            elif any(cat in category for cat in query_categories):
                category_match = 0.5
        else:
            category_match = 0.1

        if score > 10:
            normalized_score = min(score / 100.0, 1.0)
        elif score > 1:
            normalized_score = min(score / 10.0, 1.0)
        else:
            normalized_score = min(score, 1.0)
        relevance_score = (
            char_overlap * 0.14
            + keyword_overlap * 0.34
            + normalized_score * 0.28
            + category_match * 0.24
        )
        return {
            "char_overlap": round(char_overlap, 3),
            "keyword_overlap": round(keyword_overlap, 3),
            "question_keyword_overlap": round(question_keyword_overlap, 3),
            "category_match": round(category_match, 3),
            "normalized_score": round(normalized_score, 3),
            "relevance_score": round(min(relevance_score, 1.0), 3),
        }

    def _decide_crag_action(self, query: str, contexts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """基于检索相关性做通过/重试/拒答判定。"""
        if not contexts:
            return {
                "action": "retry",
                "reason": "empty_contexts",
                "top_relevance": 0.0,
                "avg_relevance": 0.0,
                "pass_count": 0,
                "scored_contexts": [],
            }

        scored_contexts: List[Dict[str, Any]] = []
        for ctx in contexts:
            # 复用 _crag_filter_retrieval 已计算的 metrics，避免重复调用 _estimate_retrieval_relevance
            if "relevance_score" in ctx:
                enriched_ctx = copy.deepcopy(ctx)
            else:
                metrics = self._estimate_retrieval_relevance(query, ctx)
                enriched_ctx = copy.deepcopy(ctx)
                enriched_ctx.update(metrics)
            scored_contexts.append(enriched_ctx)

        scored_contexts.sort(key=lambda item: item.get("relevance_score", 0.0), reverse=True)
        top_relevance = scored_contexts[0].get("relevance_score", 0.0)
        avg_relevance = sum(item.get("relevance_score", 0.0) for item in scored_contexts) / max(len(scored_contexts), 1)
        pass_contexts = [item for item in scored_contexts if item.get("relevance_score", 0.0) >= self.CRAG_MIN_RELEVANCE]
        pass_count = len(pass_contexts)
        top_ctx = scored_contexts[0]
        top_keyword_overlap = float(top_ctx.get("keyword_overlap", 0.0) or 0.0)
        top_char_overlap = float(top_ctx.get("char_overlap", 0.0) or 0.0)
        top_category_match = float(top_ctx.get("category_match", 0.0) or 0.0)
        strong_alignment = (
            top_keyword_overlap >= 0.18
            or top_char_overlap >= 0.10
            or top_category_match >= 1.0
        )

        # 修复 P7：放宽 CRAG 门控条件
        # 原逻辑要求 top_relevance>=0.52 且 strong_alignment 才 pass，纯语义匹配结果被误杀
        # 现移除 strong_alignment 要求，只要分数达标即可 pass
        if top_relevance >= self.CRAG_PASS_THRESHOLD and pass_count >= 1:
            action = "pass"
            reason = "strong_top_match" if strong_alignment else "score_match"
        elif top_relevance >= self.CRAG_RETRY_THRESHOLD or pass_count >= 2:
            action = "retry"
            reason = "weak_evidence_retry"
        else:
            action = "no_answer"
            reason = "insufficient_evidence"

        return {
            "action": action,
            "reason": reason,
            "top_relevance": round(top_relevance, 3),
            "avg_relevance": round(avg_relevance, 3),
            "pass_count": pass_count,
            "top_keyword_overlap": round(top_keyword_overlap, 3),
            "top_char_overlap": round(top_char_overlap, 3),
            "top_category_match": round(top_category_match, 3),
            "scored_contexts": scored_contexts,
        }

    # ------------------------------------------------------------------
    # P1-4: CRAG no_answer Web 搜索兜底
    # ------------------------------------------------------------------

    def _get_web_search_fallback_config(
        self,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, Any]:
        """从 schema retrieval_policy.web_search_fallback 读取配置。"""
        default_config = {
            "enabled": False,
            "engine": "duckduckgo",
            "max_results": 3,
            "timeout_seconds": 5,
            "trigger_threshold": 0.20,
        }
        try:
            schema = self._get_schema_query_understanding(
                enterprise_id=enterprise_id,
                preferred_schema_id=schema_id,
            )
            # _get_schema_query_understanding 返回 query_understanding，
            # web_search_fallback 在 retrieval_policy 下，需要直接读 schema
            from src.common.industry_schema_service import get_industry_schema_service
            schema_service = get_industry_schema_service()
            active_schema = get_active_schema_with_compat(
                schema_service,
                enterprise_id=str(enterprise_id or "").strip(),
                preferred_schema_id=str(schema_id or "").strip(),
                resolution_mode="",
            )
            metadata = active_schema.get("metadata") or {}
            retrieval_policy = metadata.get("retrieval_policy") or {}
            web_config = retrieval_policy.get("web_search_fallback") or {}
            if isinstance(web_config, dict):
                merged = dict(default_config)
                merged.update(web_config)
                return merged
        except Exception as exc:
            logger.debug(f"读取 web_search_fallback 配置失败: {exc}")
        return default_config

    def _try_web_search_fallback(
        self,
        query: str,
        top_relevance: float,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Optional[List[Dict[str, Any]]]:
        """no_answer 时尝试 Web 搜索兜底。

        Args:
            query: 用户查询
            top_relevance: CRAG 判定的最高相关性分数
            enterprise_id: 企业 ID
            schema_id: schema ID

        Returns:
            Optional[List[Dict]]: Web 搜索格式化的 contexts，未启用或无结果返回 None
        """
        config = self._get_web_search_fallback_config(
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        if not config.get("enabled"):
            return None

        trigger_threshold = float(config.get("trigger_threshold") or 0.20)
        # 仅在相关性低于阈值时触发（高相关性不需要 Web 搜索）
        if top_relevance >= trigger_threshold:
            return None

        try:
            from src.common.web_search_service import WebSearchService
            search_service = WebSearchService(
                engine=config.get("engine", "duckduckgo"),
                max_results=int(config.get("max_results", 3)),
                timeout_seconds=int(config.get("timeout_seconds", 5)),
            )
            results = search_service.search(query)
            if not results:
                return None
            contexts = search_service.format_results_as_context(results, query=query)
            logger.info(
                f"Web 搜索兜底触发: query='{query[:30]}', "
                f"top_relevance={top_relevance:.3f}, results={len(contexts)}"
            )
            return contexts if contexts else None
        except Exception as exc:
            logger.warning(f"Web 搜索兜底失败（降级为空）: {exc}")
            return None

    def _looks_like_date_and_party_followup(self, message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False

        date_match = re.search(
            r"(明天|后天|今天|周末|下周|本周|下下周|五一|端午|暑假|国庆|元旦|春节|"
            r"\d{1,2}月\d{1,2}[日号]?|\d{1,2}[日号]|"
            r"[零〇一二两三四五六七八九十廿]+月[零〇一二两三四五六七八九十廿]+[日号]?)",
            text,
        )
        party_match = re.search(
            r"([零〇一二两三四五六七八九十\d]+\s*[人位个]|"
            r"一个人|两个人|三个人|四个人|五个人|一家人|情侣|带老人|带小孩|带孩子)",
            text,
        )
        return bool(date_match and party_match)

    def _build_contextual_opening_reply(
        self,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        *,
        customer_name: str = "",
        enterprise_id: str = "",
    ) -> str:
        if not self._looks_like_date_and_party_followup(message):
            normalized_message = self._normalize_query_text(self._strip_preview_noise_prefix(message or ""))
            history_text = " ".join(
                str((msg or {}).get("content") or "")
                for msg in (conversation_history or [])
                if isinstance(msg, dict)
            )
            query_profile = self._get_query_understanding_profile(enterprise_id)
            for item in query_profile.get("contextual_opening_guidance") or []:
                query_terms = self._merge_unique_text_items(item.get("query_terms"))
                history_terms = self._merge_unique_text_items(item.get("history_terms"))
                if query_terms and not any(term in normalized_message for term in query_terms):
                    continue
                if history_terms and not any(term in history_text for term in history_terms):
                    continue
                reply_template = str(item.get("reply_template") or item.get("reply") or "").strip()
                rendered = self._render_contextual_reply_template(
                    reply_template,
                    customer_name=customer_name,
                    message=message,
                )
                if rendered:
                    return rendered
            return ""
        return get_sales_followup_service().build_context_followup_reply(
            message,
            customer_name=customer_name,
        )

    def _build_no_answer_reply(
        self,
        mode: str,
        message: str = "",
        conversation_history: Optional[List[Dict]] = None,
        enterprise_id: str = "",
    ) -> str:
        """低证据场景下的明确拒答策略。"""
        contextual_followup_reply = self._build_contextual_opening_reply(
            message,
            conversation_history=conversation_history,
            enterprise_id=enterprise_id,
        )
        business_intro_reply = self._render_schema_reply_template(
            self._get_schema_base_replies(enterprise_id=enterprise_id).get("business_intro"),
            "",
        )
        if self._is_off_domain_business_intro(message, enterprise_id=enterprise_id) and business_intro_reply:
            return business_intro_reply
        return get_sales_followup_service().build_no_answer_reply(
            mode,
            business_intro=self._is_off_domain_business_intro(message, enterprise_id=enterprise_id),
            contextual_followup=contextual_followup_reply,
        )

    def _build_reply_objective(
        self,
        *,
        message: str,
        intent_result,
        conversation_history: Optional[List[Dict]],
        customer_data: Optional[Dict[str, Any]],
        enterprise_id: str = "",
    ) -> ReplyObjective:
        strategy = self._get_industry_strategy(enterprise_id=enterprise_id)
        history = conversation_history or []
        customer_payload = customer_data or {}
        signals = strategy.extract_conversion_signals(message=message, history=history) or {}
        stage = strategy.infer_conversion_stage(
            message=message,
            intent_result=intent_result,
            signals=signals,
            history=history,
            customer_data=customer_payload,
        )
        required_slots = list(strategy.get_required_slots_for_stage(stage, intent_result=intent_result) or [])
        known_slots = strategy.extract_known_slots(
            message=message,
            history=history,
            customer_data=customer_payload,
        ) or {}
        missing_slots = [slot for slot in required_slots if not known_slots.get(slot)]
        missing_slots = list(strategy.prioritize_missing_slots(stage, missing_slots, intent_result=intent_result) or missing_slots)

        reply_policy = self._get_active_reply_policy(enterprise_id=enterprise_id)
        stage_objectives = self._reply_policy_get(reply_policy, "stage_objectives", default={}) or {}
        stage_policy = stage_objectives.get(stage.value) or stage_objectives.get("default") or {}
        if missing_slots:
            stage_policy = _deep_merge_dict(stage_policy, stage_objectives.get("missing_slots") or {})
        next_action = self._parse_next_best_action(stage_policy.get("next_best_action"))
        cta_mode = self._parse_cta_mode(stage_policy.get("cta_mode"))
        advance_threshold = self._reply_policy_get_float(
            reply_policy,
            "stage_thresholds",
            "buying_signal_to_order",
            default=0.85,
        )

        objective_confidence = max([float(value or 0.0) for value in signals.values()], default=0.0)
        primary_need = str(getattr(getattr(intent_result, "primaryIntent", None), "value", "") or "unknown")
        return ReplyObjective(
            conversion_stage=stage,
            next_best_action=next_action,
            cta_mode=cta_mode,
            should_capture_lead=cta_mode == CtaMode.LEAD_CAPTURE,
            should_offer_reservation=cta_mode == CtaMode.RESERVATION_OFFER,
            should_handoff=stage == ConversionStage.HANDOFF,
            should_advance_to_order=float(signals.get("buying_signal", 0.0) or 0.0) >= advance_threshold,
            missing_slots=missing_slots,
            primary_need=primary_need,
            objective_confidence=objective_confidence,
            reason=f"stage={stage.value}; next_action={next_action.value}; signals={signals}",
        )

    def _generate_stage_cta(
        self,
        *,
        reply_objective: Optional[ReplyObjective],
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        enterprise_id: str = "",
    ) -> str:
        if not reply_objective:
            return ""
        strategy = self._get_industry_strategy(enterprise_id=enterprise_id)
        return str(
            strategy.build_stage_cta(
                objective=reply_objective,
                message=message,
                history=conversation_history or [],
            ) or ""
        ).strip()

    def _build_clarification_or_no_answer_reply(
        self,
        *,
        reply_objective: Optional[ReplyObjective],
        mode: str,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        reason: str = "",
        enterprise_id: str = "",
    ) -> str:
        strategy = self._get_industry_strategy(enterprise_id=enterprise_id)
        if reply_objective and reply_objective.next_best_action == NextBestAction.CLARIFY:
            return strategy.build_clarification_reply(
                reply_objective,
                message,
                history=conversation_history or [],
            )
        safe_reason = f"technical_{reason}" if str(reason or "").startswith("technical_") else reason
        fallback = str(
            strategy.build_safe_no_answer_reply(
                safe_reason,
                message,
                history=conversation_history or [],
            ) or ""
        ).strip()
        return fallback or self._build_no_answer_reply(
            mode,
            message=message,
            conversation_history=conversation_history,
            enterprise_id=enterprise_id,
        )

    def _should_append_stage_cta(
        self,
        *,
        message: str,
        answer_text: str,
        reply_objective: Optional[ReplyObjective],
        enterprise_id: str = "",
    ) -> bool:
        if not reply_objective:
            return False
        cta_policy = self._get_cta_append_policy(enterprise_id=enterprise_id)
        disable_cta_modes = {
            str(item or "").strip().lower()
            for item in list(cta_policy.get("disable_when_cta_mode") or [])
            if str(item or "").strip()
        }
        if reply_objective.cta_mode.value in disable_cta_modes:
            return False
        disable_actions = {
            str(item or "").strip().lower()
            for item in list(cta_policy.get("disable_when_next_action") or [])
            if str(item or "").strip()
        }
        if reply_objective.next_best_action.value in disable_actions:
            return False
        normalized_message = str(message or "").strip().lower()
        fact_first_markers = self._normalize_policy_terms(cta_policy.get("fact_query_block_terms"))
        explicit_contact_markers = self._normalize_policy_terms(cta_policy.get("explicit_contact_terms"))
        if any(marker in normalized_message for marker in fact_first_markers) and not any(
            marker in normalized_message for marker in explicit_contact_markers
        ):
            return False
        if bool(cta_policy.get("require_answer_text", True)):
            return bool(str(answer_text or "").strip())
        return True

    @staticmethod
    def _merge_answer_and_cta(answer_text: str, cta_text: str) -> str:
        answer_text = str(answer_text or "").strip()
        cta_text = str(cta_text or "").strip()
        if not answer_text:
            return cta_text
        if not cta_text:
            return answer_text
        if cta_text in answer_text:
            return answer_text
        return f"{answer_text} {cta_text}".strip()

    def _count_keyword_hits(self, text: str, keywords: set[str]) -> int:
        lowered = (text or "").lower()
        return sum(1 for keyword in keywords if keyword.lower() in lowered)

    @staticmethod
    def _normalize_query_text(text: str) -> str:
        import re

        lowered = (text or "").strip().lower()
        if not lowered:
            return ""
        lowered = re.sub(r"\s+", "", lowered)
        return re.sub(r"[^\w\u4e00-\u9fff]", "", lowered)

    @classmethod
    def _strip_preview_noise_prefix(cls, text: str) -> str:
        lines = [str(line or "").strip() for line in str(text or "").splitlines()]
        trimmed = [line for line in lines if line]
        while trimmed and cls.PREVIEW_NOISE_LINE_RE.fullmatch(trimmed[0]):
            trimmed.pop(0)
        return "\n".join(trimmed).strip()

    def _extract_route_entities_from_text(self, text: str) -> List[str]:
        strategy = self._get_industry_strategy()
        domain_keywords = list(strategy.get_domain_keywords() or set())
        if not domain_keywords:
            return []
        ordered_entities: List[str] = []
        seen = set()
        searchable_text = str(text or "")
        for keyword in sorted(domain_keywords, key=len, reverse=True):
            if keyword in searchable_text and keyword not in seen:
                seen.add(keyword)
                ordered_entities.append(keyword)
        return ordered_entities

    def _remember_grounded_context(
        self,
        *,
        session_id: str,
        retrieval_query: str,
        matched_knowledge,
        reply_analysis: Dict[str, Any],
        retrieved_contexts: List[Dict[str, Any]],
    ) -> None:
        answer_plan, route_facts, route_evidence = self._extract_bundle_grounded_context(retrieved_contexts)
        strategy = self._resolve_grounded_route_strategy(
            message=retrieval_query,
            answer_plan=answer_plan,
            route_facts=route_facts,
            route_evidence=route_evidence,
            matched_knowledge=matched_knowledge,
        )
        strategy.remember_grounded_context(
            self,
            session_id=session_id,
            retrieval_query=retrieval_query,
            matched_knowledge=matched_knowledge,
            reply_analysis=reply_analysis,
            retrieved_contexts=retrieved_contexts,
        )

    def _refine_intent_with_grounded_context(self, message: str, intent_result, session_id: str):
        strategy = self._resolve_followup_strategy(
            message=message,
            session_id=session_id,
        )
        refined = strategy.refine_intent_with_grounded_context(self, message, intent_result, session_id)
        grounded_routes: List[str] = []
        context_understanding = getattr(self, "_context_understanding", None)
        if context_understanding and session_id:
            grounded = context_understanding.get_grounded_knowledge(session_id) or {}
            grounded_routes = [
                str(route or "").strip()
                for route in list(grounded.get("routes") or [])
                if str(route or "").strip()
            ]
            recommended_route = str(grounded.get("recommended_route") or "").strip()
            if recommended_route and recommended_route not in grounded_routes:
                grounded_routes.insert(0, recommended_route)
        if grounded_routes and getattr(refined, "primaryIntent", None) == getattr(intent_result, "primaryIntent", None):
            inferred_intent = self._infer_followup_intent_from_grounded_context(message, grounded_routes)
            if inferred_intent:
                fallback_refined = copy.deepcopy(refined)
                fallback_refined.primaryIntent = inferred_intent
                fallback_refined.confidence = max(float(getattr(refined, "confidence", 0.0) or 0.0), 0.72)
                fallback_refined.reasoning = str(getattr(refined, "reasoning", "") or "").strip()
                fallback_refined.reasoning = (
                    f"{fallback_refined.reasoning} | grounded_context_refine".strip(" |")
                    if fallback_refined.reasoning
                    else "grounded_context_refine"
                )
                keywords = list(getattr(fallback_refined, "keywords", []) or [])
                if "grounded_context" not in keywords:
                    keywords.append("grounded_context")
                fallback_refined.keywords = keywords
                entities = dict(getattr(fallback_refined, "entities", {}) or {})
                product_entities = list(entities.get("product") or [])
                for route_name in grounded_routes:
                    if route_name not in product_entities:
                        product_entities.append(route_name)
                if product_entities:
                    entities["product"] = product_entities
                fallback_refined.entities = entities
                return fallback_refined
        return refined

    def _infer_followup_intent_from_grounded_context(
        self,
        message: str,
        grounded_routes: List[str],
    ) -> Optional[IntentType]:
        strategy = self._resolve_followup_strategy(
            message=message,
            grounded_routes=grounded_routes,
        )
        inferred = strategy.infer_followup_intent(self, message, grounded_routes)
        if inferred:
            return inferred
        text = str(message or "").strip()
        if not text or not grounded_routes:
            return None
        if any(term in text for term in ("哪个好", "怎么选", "区别", "对比", "适合哪个", "哪个更适合", "选哪个")):
            return IntentType.COMPARISON
        # 通用联系咨询词，行业专用词（如旅游的 发行程）由 schema contact_inquiry_markers 提供
        contact_inquiry_terms = ["发我", "发资料", "联系方式", "怎么联系"]
        contact_inquiry_terms.extend(self._get_schema_term_list("contact_inquiry_markers"))
        if any(term in text for term in contact_inquiry_terms):
            return IntentType.CONTACT_INQUIRY
        # 通用服务咨询词，行业专用词（如旅游的 行程/集合/接送）由 schema service_inquiry_markers 提供
        service_inquiry_terms = ["什么时候", "几点", "怎么安排"]
        service_inquiry_terms.extend(self._get_schema_term_list("service_inquiry_markers"))
        if any(term in text for term in service_inquiry_terms):
            return IntentType.SERVICE_INQUIRY
        # 通用购买意向词，行业专用词（如旅游的 锁位/锁名额/报名）由 schema purchase_intent_markers 提供
        purchase_intent_terms = ["预订", "下单", "付款"]
        purchase_intent_terms.extend(self._get_schema_term_list("purchase_intent_markers"))
        if any(term in text for term in purchase_intent_terms):
            return IntentType.PURCHASE_INTENT
        return None

    def _get_recent_route_anchor(self, conversation_history: List[Dict] = None, session_id: str = "") -> str:
        """从最近上下文中提取行业锚点，供短句追问补全使用。"""
        strategy = self._resolve_followup_strategy(
            conversation_history=conversation_history,
            session_id=session_id,
        )
        return strategy.get_recent_anchor(self, conversation_history, session_id=session_id)

    def _is_generic_route_followup(self, message: str) -> bool:
        """判断是否是依赖上文的行业短句追问。"""
        strategy = self._resolve_followup_strategy(message=message)
        return strategy.is_generic_followup(self, message)

    def _maybe_expand_domain_followup(
        self,
        message: str,
        conversation_history: List[Dict] = None,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> str:
        """将短句追问补全为更明确的行业问句，提升上下文承接命中率。"""
        strategy = self._resolve_followup_strategy(
            message=message,
            conversation_history=conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )
        return strategy.expand_followup_query(
            self,
            message,
            conversation_history,
            session_id=session_id,
        )

    def _maybe_expand_route_followup(
        self,
        message: str,
        conversation_history: List[Dict] = None,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> str:
        expanded = self._maybe_expand_domain_followup(
            message,
            conversation_history=conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )
        if str(expanded or "").strip() != str(message or "").strip():
            return expanded
        if self._should_disable_heuristic_domain_followup(enterprise_id=enterprise_id) and not str(session_id or "").strip():
            return str(message or "").strip()
        return self._legacy_expand_route_followup(
            message,
            conversation_history=conversation_history,
            session_id=session_id,
        )

    def _legacy_expand_route_followup(
        self,
        message: str,
        conversation_history: List[Dict] = None,
        session_id: str = "",
    ) -> str:
        message = str(message or "").strip()
        if not message:
            return message

        # 仅在旅游行业 schema（entity_type=route）下启用遗留路由追问扩展；
        # 其他行业直接返回原消息，避免硬编码旅游产品名污染通用链路
        active_entity_type = str(self._get_active_schema_profile().get("entity_type") or "").strip().lower()
        if active_entity_type and active_entity_type != "route":
            return message

        route_aliases = (
            "仙女山天坑一日游",
            "天坑地缝一日游",
            "市内纯玩",
            "市内一日游",
            "重庆市内一日游",
            "仙女山天坑",
            "天坑地缝",
        )
        history = [msg for msg in (conversation_history or []) if isinstance(msg, dict)]
        grounded = {}
        context_understanding = getattr(self, "_context_understanding", None)
        if session_id and context_understanding:
            try:
                grounded = context_understanding.get_grounded_knowledge(session_id) or {}
            except Exception:
                grounded = {}

        text_parts = [message]
        text_parts.extend(str((msg or {}).get("content") or "") for msg in history)
        text_parts.extend(
            str(item or "")
            for item in (
                list(grounded.get("routes") or [])
                + [
                    grounded.get("recommended_route"),
                    grounded.get("retrieval_query"),
                    grounded.get("matched_knowledge"),
                ]
            )
            if str(item or "").strip()
        )
        combined_text = " ".join(part for part in text_parts if part)

        normalized_routes = []
        for term in route_aliases:
            if term in combined_text:
                normalized = "市内一日游" if term == "重庆市内一日游" else term
                if normalized not in normalized_routes:
                    normalized_routes.append(normalized)

        def _primary_route() -> str:
            for msg in reversed(history):
                content = str((msg or {}).get("content") or "")
                for term in route_aliases:
                    if term in content:
                        return "市内一日游" if term == "重庆市内一日游" else term
            for term in normalized_routes:
                if term in {"仙女山天坑一日游", "天坑地缝一日游", "市内纯玩", "市内一日游", "仙女山天坑", "天坑地缝"}:
                    return term
            return ""

        primary_route = _primary_route()
        has_city_pair = "市内纯玩" in combined_text and ("市内一日游" in combined_text or "重庆市内一日游" in combined_text)
        has_wulong_pair = "仙女山天坑" in combined_text and "天坑地缝" in combined_text

        if message == "那两个市内哪个好":
            return "两个市内线路景点一样，选哪个好？"
        if message == "带老人适合哪个" and has_wulong_pair:
            return "仙女山天坑和天坑地缝带老人适合哪个"
        if message == "预算不高怎么选" and ("仙女山天坑" in combined_text or "天坑地缝" in combined_text):
            return "仙女山天坑和天坑地缝预算不高怎么选"
        if message == "老人腿脚一般走哪条更稳" and ("市内一日游" in combined_text or "市内纯玩" in combined_text):
            return "老人腿脚一般市内纯玩和市内一日游走哪条更稳"
        if message == "我们两个人选哪条" and ("市内纯玩" in combined_text or "市内一日游" in combined_text):
            return "市内纯玩和市内一日游两个人选哪条"
        if message == "能赶高铁吗" and ("市内纯玩" in combined_text or has_city_pair):
            return "市内纯玩能赶高铁或飞机吗"
        if message == "包含餐饮吗" and ("市内一日游" in combined_text or "重庆市内一日游" in combined_text):
            return "一日游包吃吗"
        if message == "几点出发" and primary_route:
            return f"{primary_route}几点出发，在哪里集合"
        if message == "几点回来" and primary_route:
            return f"{primary_route}大概几点回来" if primary_route == "市内纯玩" else f"{primary_route}几点回来"
        if message == "洪崖洞" and primary_route == "市内纯玩":
            return "市内纯玩里的洪崖洞怎么玩"
        if message == "包含门票和车费吗" and primary_route == "仙女山天坑一日游":
            return "仙女山天坑一日游包含哪些费用"
        if message == "包含车费和门票吗" and primary_route == "市内纯玩":
            return "市内纯玩一日游包含车费和门票吗"
        if message == "游玩哪些地方" and primary_route == "市内纯玩":
            return "市内纯玩一日游游玩哪些地方"

        if primary_route:
            return f"{primary_route}{message}"
        if has_wulong_pair and message in {"带老人适合哪个", "带老人怎么选"}:
            return "仙女山天坑和天坑地缝带老人适合哪个"
        return message

    def _is_off_domain_business_intro(self, text: str, enterprise_id: str = "") -> bool:
        text = (text or "").strip()
        if not text:
            return False
        query_profile = self._get_query_understanding_profile(enterprise_id)
        domain_hits = self._count_keyword_hits(text, self._get_active_domain_keywords(enterprise_id))
        business_terms = set(
            self._merge_unique_text_items(
                self.OFF_DOMAIN_BUSINESS_TERMS,
                query_profile.get("business_intro_terms"),
            )
        )
        business_hits = self._count_keyword_hits(text, business_terms)
        if domain_hits > 0:
            return False
        normalized_text = self._normalize_query_text(text)
        exact_phrases = {
            self._normalize_query_text(item)
            for item in self._merge_unique_text_items(query_profile.get("business_intro_exact_phrases"))
        }
        if exact_phrases and normalized_text in exact_phrases:
            return True
        if business_hits >= 2 or ("我们是一家" in text and business_hits >= 1):
            return True
        business_intro_patterns = self._merge_unique_text_items(
            self.GENERIC_BUSINESS_INTRO_PATTERNS,
            query_profile.get("business_intro_patterns"),
        )
        for pattern in business_intro_patterns:
            try:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    return True
            except re.error as exc:
                logger.debug(f"忽略无效 business intro pattern={pattern!r}: {exc}")
        return False

    def _is_broad_domain_opening(self, normalized_message: str) -> bool:
        strategy = self._get_industry_strategy()
        domain_keywords = strategy.get_domain_keywords()
        if not domain_keywords:
            return False
        config = {}
        if hasattr(strategy, '_get_followup_config'):
            config = strategy._get_followup_config()
        opening_terms = config.get("broad_opening_terms") or ("攻略", "推荐", "怎么玩")
        msg = str(normalized_message or "").strip()
        if len(msg) > 10:
            return False
        has_domain_keyword = any(kw in msg for kw in domain_keywords)
        has_opening_term = any(term in msg for term in opening_terms)
        return has_domain_keyword and has_opening_term

    def _get_domain_anchor_terms(self) -> List[str]:
        strategy = self._get_industry_strategy()
        config = {}
        if hasattr(strategy, '_get_followup_config'):
            config = strategy._get_followup_config()
        anchor_terms = config.get("anchor_terms") or []
        return list(anchor_terms)

    def _is_domain_related_query(
        self,
        query: str,
        conversation_history: List[Dict] = None,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> bool:
        query = (query or "").strip()
        history = conversation_history or []
        if not query:
            return False

        expanded_query = self._maybe_expand_domain_followup(
            query,
            history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )
        if expanded_query != query:
            query = expanded_query

        strategy = self._resolve_followup_strategy(
            message=query,
            conversation_history=history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )
        domain_keywords = set(strategy.get_domain_keywords() or set()) or self._get_active_domain_keywords(enterprise_id)
        if self._count_keyword_hits(query, domain_keywords) > 0:
            return True
        if self._is_off_domain_business_intro(query):
            return False

        history_text = " ".join(
            str(msg.get("content") or "")
            for msg in history[-6:]
            if isinstance(msg, dict)
        )
        context_understanding = getattr(self, "_context_understanding", None)
        if session_id and context_understanding:
            grounded = context_understanding.get_grounded_knowledge(session_id) or {}
            grounded_routes = [
                str(route_name or "").strip()
                for route_name in (grounded.get("routes") or [])
                if str(route_name or "").strip()
            ]
            grounded_context_parts = [
                str(grounded.get("recommended_route") or ""),
                str(grounded.get("matched_knowledge") or ""),
                str(grounded.get("retrieval_query") or ""),
            ]
            grounded_context_parts.extend(
                grounded_routes
            )
            grounded_context_text = " ".join(part for part in grounded_context_parts if part)
            if grounded_routes and self._infer_followup_intent_from_grounded_context(query, grounded_routes):
                return True
            if grounded_context_text:
                history_text = f"{history_text} {grounded_context_text}".strip()
        history_hits = self._count_keyword_hits(history_text, domain_keywords)
        if history_hits <= 0:
            return False

        query_profile = self._get_query_understanding_profile(enterprise_id)
        follow_up_terms = self._merge_unique_text_items(
            query_profile.get("followup_terms"),
            ["这个", "那个", "好的", "可以", "怎么选", "怎么安排", "哪个好", "几点", "费用", "价格"],
        )
        return len(query) <= 12 or any(term in query for term in follow_up_terms)

    def _should_skip_mainline_retrieval(
        self,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        *,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> bool:
        """明显非领域/通识问题时，允许主链直接走 LLM only 分支。"""
        text = self._normalize_query_text(self._strip_preview_noise_prefix(message or ""))
        if not text:
            return False
        if self._is_domain_related_query(
            text,
            conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        ):
            return False
        if self._is_off_domain_business_intro(text, enterprise_id=enterprise_id):
            return False

        # 通用业务标记（跨行业通用），行业专用业务标记（如旅游的 线路/报名）
        # 由 schema query_understanding.business_markers 提供
        business_markers = [
            "你们", "你家", "主营", "产品", "服务", "课程", "方案",
            "价格", "费用", "报价", "退款", "售后", "购买", "下单", "预约", "微信", "电话",
        ]
        business_markers.extend(self._get_schema_term_list("business_markers", enterprise_id=enterprise_id))
        if any(marker in text for marker in business_markers):
            return False

        general_qa_markers = (
            "是什么", "什么意思", "是什么意思", "起源", "由来", "首都", "哪里",
            "哪国", "哪个国家", "谁写的", "诗歌", "古诗", "英文", "翻译",
            "定义", "原理", "历史", "怎么回事", "为什么", "区别",
        )
        return any(marker in text for marker in general_qa_markers)

    def _should_allow_retry_direct_answer(
        self,
        message: str,
        retrieved_contexts: List[Dict[str, Any]],
        conversation_history: List[Dict] = None,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> bool:
        if not retrieved_contexts:
            return False
        if not self._is_domain_related_query(
            message,
            conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        ):
            return False

        top_ctx = sorted(
            retrieved_contexts,
            key=lambda item: float(item.get("relevance_score", 0.0) or 0.0),
            reverse=True,
        )[0]
        top_relevance = float(top_ctx.get("relevance_score", 0.0) or 0.0)
        keyword_overlap = float(top_ctx.get("keyword_overlap", 0.0) or 0.0)
        char_overlap = float(top_ctx.get("char_overlap", 0.0) or 0.0)
        weak_retry_cutoff = (self.CRAG_RETRY_THRESHOLD + self.CRAG_PASS_THRESHOLD) / 2
        return (
            (
                top_relevance >= self.CRAG_PASS_THRESHOLD
                and (keyword_overlap >= 0.16 or char_overlap >= 0.12)
            )
            or (
                top_relevance >= weak_retry_cutoff
                and keyword_overlap >= 0.12
                and char_overlap >= 0.08
            )
        )

    def _should_route_retry_to_llm_only(
        self,
        *,
        message: str,
        retrieved_contexts: List[Dict[str, Any]],
        conversation_history: List[Dict] = None,
        session_id: str = "",
        enterprise_id: str = "",
    ) -> bool:
        if not retrieved_contexts:
            return True
        is_domain = self._is_domain_related_query(
            message,
            conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        )
        if is_domain and self._should_allow_retry_direct_answer(
            message,
            retrieved_contexts,
            conversation_history=conversation_history,
            session_id=session_id,
            enterprise_id=enterprise_id,
        ):
            return False

        top_ctx = sorted(
            retrieved_contexts,
            key=lambda item: float(item.get("relevance_score", 0.0) or 0.0),
            reverse=True,
        )[0]
        top_relevance = float(top_ctx.get("relevance_score", 0.0) or 0.0)
        keyword_overlap = float(top_ctx.get("keyword_overlap", 0.0) or 0.0)
        char_overlap = float(top_ctx.get("char_overlap", 0.0) or 0.0)
        weak_retry_cutoff = (self.CRAG_RETRY_THRESHOLD + self.CRAG_PASS_THRESHOLD) / 2
        return (
            top_relevance < weak_retry_cutoff
            and keyword_overlap < 0.12
            and char_overlap < 0.08
        )

    def _should_prefer_grounded_direct_answer(
        self,
        message: str,
        matched_knowledge,
        retrieval_query: Optional[str] = None,
    ) -> bool:
        if not matched_knowledge or not getattr(matched_knowledge, "answer", ""):
            return False

        normalized_candidates = [
            self._normalize_query_text(message),
            self._normalize_query_text(retrieval_query or ""),
        ]
        normalized_candidates = [candidate for candidate in normalized_candidates if candidate]
        if not normalized_candidates:
            return False

        def _build_bigrams(text: str) -> set[str]:
            if not text:
                return set()
            if len(text) < 2:
                return {text}
            return {text[i:i + 2] for i in range(len(text) - 1)}

        def _normalize_grounded_match_text(text: str) -> str:
            normalized = self._normalize_query_text(text)
            if not normalized:
                return ""
            replacements = (
                ("更适合", "适合"),
                ("带老人", "老人"),
                ("带小孩", "小孩"),
                ("适合老人", "老人适合"),
                ("地缝", "天坑地缝"),
                ("哪条", "哪个"),
                ("怎么选", "选哪个"),
                ("值不值得", "值不值"),
            )
            for old, new in replacements:
                normalized = normalized.replace(old, new)
            return normalized

        def _is_close_grounded_match(candidate: str, target: str) -> bool:
            candidate = _normalize_grounded_match_text(candidate)
            target = _normalize_grounded_match_text(target)
            if not candidate or not target:
                return False
            if candidate == target:
                return True
            if candidate in target or target in candidate:
                return True

            candidate_bigrams = _build_bigrams(candidate)
            target_bigrams = _build_bigrams(target)
            if not candidate_bigrams or not target_bigrams:
                return False

            overlap = len(candidate_bigrams & target_bigrams)
            overlap_ratio = overlap / max(min(len(candidate_bigrams), len(target_bigrams)), 1)
            return overlap_ratio >= 0.70

        normalized_question = self._normalize_query_text(getattr(matched_knowledge, "question", ""))
        if normalized_question and any(
            _is_close_grounded_match(candidate, normalized_question)
            for candidate in normalized_candidates
        ):
            return True

        for alias in list(getattr(matched_knowledge, "aliases", []) or []):
            normalized_alias = self._normalize_query_text(alias)
            if normalized_alias and any(
                _is_close_grounded_match(candidate, normalized_alias)
                for candidate in normalized_candidates
            ):
                return True

        return False

    def _should_try_crag_rewrite(self, query: str, conversation_history: List[Dict] = None) -> bool:
        """仅在多轮或明显歧义查询下尝试改写，避免简单查询触发昂贵 LLM。"""
        query = (query or "").strip()
        if not query:
            return False

        # 通用代词（跨行业通用），行业专用代词（如旅游的 这个团/那个团/这个线路/那个线路）
        # 由 schema query_understanding.pronoun_terms 提供
        pronoun_terms = [
            "这个", "那个",
            "它", "她", "他", "这里", "那里", "上述", "前者", "后者",
        ]
        pronoun_terms.extend(self._get_schema_term_list("pronoun_terms"))

        # 通用显式词（跨行业通用），行业专用显式词（如旅游的 门票/行程/路线/景点/发车）
        # 由 schema query_understanding.explicit_terms 提供
        explicit_terms = [
            "价格", "多少钱", "多少", "退款", "退改", "费用",
            "包含", "包含什么", "几点", "政策", "怎么退款", "能退吗",
        ]
        explicit_terms.extend(self._get_schema_term_list("explicit_terms"))

        has_history = bool(conversation_history)
        has_pronoun_reference = any(term in query for term in pronoun_terms)
        explicit = any(term in query for term in explicit_terms)

        if explicit and not has_pronoun_reference:
            return False
        if has_pronoun_reference:
            return has_history
        return has_history and len(query) <= 6 and not explicit

    def _crag_filter_retrieval(self, query: str, contexts: List[Dict]) -> List[Dict]:
        """
        CRAG检索质量门控：评估检索结果与查询的相关性
        
        对每条检索结果进行相关性评估，过滤掉不相关的结果。
        评估维度：关键词重叠度、语义相关性、分类匹配度
        """
        if not contexts:
            return contexts

        filtered = []
        for ctx in contexts:
            metrics = self._estimate_retrieval_relevance(query, ctx)
            enriched_ctx = copy.deepcopy(ctx)
            enriched_ctx.update(metrics)
            if enriched_ctx["relevance_score"] >= self.CRAG_MIN_RELEVANCE:
                filtered.append(enriched_ctx)
            else:
                logger.debug(
                    f"CRAG过滤: '{enriched_ctx.get('question', '')[:20]}' "
                    f"相关性{enriched_ctx['relevance_score']:.3f}低于阈值"
                )

        filtered.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)
        logger.info(f"CRAG门控: {len(contexts)}条检索结果中{len(filtered)}条通过相关性评估")
        return filtered

    def _crag_rewrite_and_retrieve(
        self,
        query: str,
        conversation_history: List[Dict] = None,
        intent: str = "",
        enterprise_id: str = "",
        business_stage: str = "",
        schema_id: str = "",
    ) -> List[Dict]:
        """
        CRAG查询改写：当检索结果不理想时，改写查询重新检索

        策略：提取关键词、简化问题、补充上下文

        注意：schema_id 必须透传到 _search_knowledge，避免二次检索跨 schema 扫描
        导致返回其他业务 schema 的知识片段（与主链不一致引发回复不准确）。
        """
        rewritten_contexts = []
        
        try:
            from .llm_service import get_default_llm_provider
            llm_provider = get_default_llm_provider()
            
            if not llm_provider:
                return []
            
            rewrite_prompt = f"""请将以下用户问题改写为更适合知识库检索的简洁查询。
只输出改写后的查询，不要解释。

原始问题：{query}
{f"对话上下文：{'; '.join(msg.get('content', '') for msg in (conversation_history or [])[-3:])}" if conversation_history else ""}

改写要求：
1. 提取核心关键词
2. 去除语气词和无关描述
3. 补充可能的同义词
4. 如有代词指代，用上下文中的实际名词替换

改写后的查询："""
            
            from src.common.utils import call_llm_safe
            rewritten_query = call_llm_safe(
                llm_provider,
                rewrite_prompt,
                timeout=self.CRAG_REWRITE_TIMEOUT_SECONDS,
            )
            if rewritten_query and rewritten_query.strip():
                rewritten_query = rewritten_query.strip()
                # 修复：CRAG rewrite 退化防护
                # 根因：qwen3.5:4b 对 "你好" 等短查询/问候语会原样回显（content_len=2），
                # 原代码仅校验非空，将退化输出当作有效改写，用 "你好" 做二次检索必然无果，
                # 叠加初始 contexts 被过滤清空，导致 rewrite_failed_evidence_insufficient。
                # 防护：原样回显或太短（<3字符）时不进行二次检索，直接返回空，
                # 让 retrieval_stage 走 rewrite_failed_preserve_initial 或 insufficient_evidence 路径。
                normalized_original = re.sub(r"\s+", "", query).strip().lower()
                normalized_rewrite = re.sub(r"\s+", "", rewritten_query).strip().lower()
                is_degenerate = (
                    normalized_rewrite == normalized_original
                    or len(normalized_rewrite) < 3
                )
                if is_degenerate:
                    logger.info(
                        f"CRAG查询改写退化(原样回显或太短)，跳过二次检索: "
                        f"'{query}' -> '{rewritten_query}'"
                    )
                    return rewritten_contexts
                logger.info(f"CRAG查询改写: '{query}' -> '{rewritten_query}'")

                # CRAG 二次检索直接用 _search_knowledge（已走 pipeline 四路融合）
                # 修复 A1：透传 schema_id，避免二次检索跨 schema 扫描
                knowledge_results = self._search_knowledge(
                    rewritten_query,
                    top_k=10,  # 修复 N3：提升 top_k 从 5 到 10，与主链一致
                    business_stage=business_stage,
                    intent=intent,
                    enterprise_id=enterprise_id,
                    schema_id=schema_id,
                )
                if knowledge_results:
                    for item, s in knowledge_results[:5]:
                        rewritten_contexts.append({
                            "question": item.question,
                            "answer": item.answer,
                            "category": item.category,
                            "score": s,
                            "source": "crag_rewrite",
                            "rewritten_from": query
                        })
        except Exception as e:
            logger.warning(f"CRAG查询改写失败: {e}")
        
        return rewritten_contexts

    def _map_intent_to_level(self, intent: IntentType) -> str:
        """将意图类型映射到意向等级"""
        mapping = {
            IntentType.PURCHASE_INTENT: "A",
            IntentType.COOPERATION_INTENT: "A",
            IntentType.PRICE_INQUIRY: "B",
            IntentType.PRODUCT_INQUIRY: "C",
            IntentType.SERVICE_INQUIRY: "C",
            IntentType.COMPARISON: "C",
            IntentType.COMPLAINT: "B",
            IntentType.CONSULTATION: "C",
            IntentType.FEEDBACK: "C",
            IntentType.GREETING: "D",
            IntentType.STATUS_INQUIRY: "E",
            IntentType.READY_CHECK: "E",
            IntentType.FAREWELL: "D",
            IntentType.THANKS: "D",
            IntentType.CONFIRMATION: "D",
            IntentType.REJECTION: "E",
            IntentType.CONTACT_INQUIRY: "B",
            IntentType.UNKNOWN: "E"
        }
        result = mapping.get(intent)
        if result is None and is_industry_intent(intent.value):
            return "B"
        return result or "C"

    def _map_risk_level(self, level: str) -> str:
        """映射风险等级"""
        mapping = {
            "critical": "critical",
            "high": "high",
            "medium": "medium",
            "low": "low",
            "minimal": "minimal"
        }
        return mapping.get(level, "low")

    def _build_risk_assessment(self, risk_profile) -> Dict:
        """构建风险评估结果"""
        overall_level, overall_score, risk_items = self._normalize_risk_profile(risk_profile)
        return {
            "overall_level": overall_level,
            "overall_score": overall_score,
            "churn_risk": risk_items["churn_risk"],
            "complaint_risk": risk_items["complaint_risk"],
            "deal_failure_risk": risk_items["deal_failure_risk"],
            "competitor_risk": risk_items["competitor_risk"],
        }

    def _normalize_risk_profile(self, risk_profile) -> Tuple[str, float, Dict[str, Dict[str, Any]]]:
        """将风险画像统一归一化为字典结构，供结果装配和行动建议复用。"""
        default_item = {"level": "low", "score": 0}
        risk_keys = ["churn_risk", "complaint_risk", "deal_failure_risk", "competitor_risk"]

        if isinstance(risk_profile, dict):
            overall_level = str(risk_profile.get("overall_risk_level", "low"))
            overall_score = risk_profile.get("overall_risk_score", 0)
            risk_items = {}
            for key in risk_keys:
                item = risk_profile.get(key, default_item)
                risk_items[key] = item if isinstance(item, dict) else default_item.copy()
                risk_items[key].setdefault("level", "low")
                risk_items[key].setdefault("score", 0)
            return overall_level, overall_score, risk_items

        try:
            overall_level = risk_profile.overall_risk_level.value
            overall_score = risk_profile.overall_risk_score
            risk_items = {}
            for key in risk_keys:
                item = getattr(risk_profile, key, None)
                risk_items[key] = {
                    "level": getattr(getattr(item, "risk_level", None), "value", "low"),
                    "score": getattr(item, "risk_score", 0),
                }
            return overall_level, overall_score, risk_items
        except (AttributeError, TypeError):
            return "low", 0, {key: default_item.copy() for key in risk_keys}

    def _generate_suggested_action(
        self,
        priority_decision,
        risk_profile,
        intent_result
    ) -> str:
        """生成建议行动"""
        del intent_result
        overall_risk, _, risk_items = self._normalize_risk_profile(risk_profile)
        complaint_level = str(risk_items["complaint_risk"].get("level", "low"))

        if overall_risk == "critical":
            return "立即处理，优先跟进"

        if complaint_level in ["critical", "high"]:
            return "升级处理，认真对待客户投诉"

        if priority_decision.get("priority_level") == "P0":
            return "立即响应，每小时跟进"

        if priority_decision.get("priority_level") == "P1":
            return "优先跟进，30分钟内响应"
        deal_level = str(risk_items["deal_failure_risk"].get("level", "low"))
        competitor_level = str(risk_items["competitor_risk"].get("level", "low"))

        if deal_level in ["high", "critical"]:
            return "挽留意向客户，针对性解决顾虑"

        if competitor_level in ["high", "critical"]:
            return "突出差异化价值，回应竞品对比"

        return "正常跟进，保持沟通"

    def _generate_simple_action(self, priority_decision) -> str:
        """生成简单行动建议"""
        level = priority_decision.get("priority_level", "normal")
        simple_actions = {
            "P0": "立即响应",
            "P1": "优先跟进",
            "P2": "正常跟进",
        }
        return simple_actions.get(level, "保持沟通")

    def clear_cache(self):
        """清除缓存"""
        self._intent_cache.clear()
        logger.info("意图识别缓存已清除")


_enhanced_service_instance: Optional[EnhancedCustomerService] = None
_enhanced_service_lock = threading.Lock()


def get_enhanced_customer_service() -> EnhancedCustomerService:
    """获取增强客服服务实例"""
    global _enhanced_service_instance
    if _enhanced_service_instance is None:
        with _enhanced_service_lock:
            if _enhanced_service_instance is None:
                started_at = time.perf_counter()
                _enhanced_service_instance = EnhancedCustomerService()
                logger.info(
                    "增强客服服务首次初始化完成: "
                    f"elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
                )
    return _enhanced_service_instance
