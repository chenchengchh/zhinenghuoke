"""
增强智能客服服务 - 工具函数和常量

包含数学处理、会话ID解析、默认配置等模块级工具
"""
import logging
import re
import copy
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime

from .core_utils.safe_eval import safe_math_eval as _safe_math_eval

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
    # 延迟导入避免循环依赖
    from .main_service import EnhancedCustomerService

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
    # 延迟导入避免循环依赖
    from .main_service import EnhancedCustomerService

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
