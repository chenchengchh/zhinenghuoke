from __future__ import annotations

import re
from typing import Any


INTENT_LEVEL_SCORE_FLOORS = {
    "A": 80.0,
    "B": 65.0,
    "C": 45.0,
    "D": 25.0,
    "E": 0.0,
}

LEAD_SCORE_SCORE_FLOORS = {
    "hot": 80.0,
    "warm": 60.0,
    "cool": 35.0,
    "cold": 0.0,
}

INTENT_LEVEL_REFERENCE_SCORES = {
    "A": 92.0,
    "B": 75.0,
    "C": 55.0,
    "D": 30.0,
    "E": 8.0,
}

LEAD_SCORE_REFERENCE_SCORES = {
    "hot": 88.0,
    "warm": 70.0,
    "cool": 42.0,
    "cold": 12.0,
}

HIGH_INTENT_EFFECTIVE_SCORE_THRESHOLD = 65.0
STRONG_PURCHASE_INTENT_SCORE_FLOOR = 82.0
CONTACT_PROVIDED_SCORE_FLOOR = 92.0

CONTACT_MARKER_PATTERNS = (
    re.compile(r"1[3-9][\s-]*\d[\d\s-]{8,}"),
    re.compile(r"(?:1\d{2}|\d{4})\*{3,4}\d{4}"),
    re.compile(r"[\w.-]+@[\w.-]+\.[a-zA-Z]{2,}"),
    re.compile(r"(?:微信|vx|wx|v信|加我|联系)[:：\s]*([a-zA-Z][-_a-zA-Z0-9]{5,19})", re.IGNORECASE),
)

CONTACT_LABELED_ACCOUNT_PATTERNS = (
    ("微信", re.compile(r"(?:微信|vx|wx|v信|wechat)(?:号)?(?:是|为|[:：])?\s*([a-zA-Z][-_a-zA-Z0-9]{5,19})", re.IGNORECASE)),
    ("QQ", re.compile(r"(?:QQ|qq|扣扣)(?:号)?(?:是|为|[:：])?\s*([1-9][0-9]{4,11})", re.IGNORECASE)),
    ("钉钉", re.compile(r"(?:钉钉|ding(?:talk)?)(?:号)?(?:是|为|[:：])?\s*([a-zA-Z0-9][-_a-zA-Z0-9]{4,31})", re.IGNORECASE)),
    ("飞书", re.compile(r"(?:飞书|feishu)(?:号)?(?:是|为|[:：])?\s*([a-zA-Z0-9][-_a-zA-Z0-9]{4,31})", re.IGNORECASE)),
    ("泡泡号", re.compile(r"(?:泡泡|泡泡号)(?:是|为|[:：])?\s*([a-zA-Z0-9][-_a-zA-Z0-9]{4,31})", re.IGNORECASE)),
    ("手机号", re.compile(r"(?:手机号|手机|电话|联系方式|联系号码)(?:是|为|[:：])?\s*(1[3-9]\d{9})", re.IGNORECASE)),
)

CONTACT_ONLY_DIGIT_PATTERN = re.compile(r"^[1-9][0-9]{4,11}$")
CONTACT_ONLY_PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
CONTACT_ONLY_SOCIAL_ID_PATTERN = re.compile(r"^[a-zA-Z][-_a-zA-Z0-9]{4,31}$")
CONTACT_ONLY_WRAPPER_PATTERN = re.compile(r"^[\s,.;:!@#\-_=+*/\\|()\[\]{}<>~`'\"，。；：！？、]+|[\s,.;:!@#\-_=+*/\\|()\[\]{}<>~`'\"，。；：！？、]+$")

STRONG_PURCHASE_PATTERNS = (
    re.compile(r"(?:帮我|给我|先给我|现在就|直接|马上|尽快).{0,6}(?:预留|锁定|锁位|留位|占位|报名|下单|安排|预定|预订)"),
    re.compile(r"(?:我|我们).{0,6}(?:要|想|准备|打算).{0,8}(?:报名|下单|定|订|预定|预订|付款|支付|出发)"),
    re.compile(r"(?:先|现在|马上|直接).{0,4}(?:付|交).{0,4}(?:定金|订金|尾款|全款|款)"),
    re.compile(r"(?:发我|给我).{0,6}(?:付款链接|支付链接|收款码|报价单|合同)"),
    re.compile(r"怎么(?:付款|支付|下单|报名)"),
    re.compile(r"(?:确定|确认).{0,6}(?:报名|下单|要|出发|订这个|订这条|预留|锁位)"),
)

STRONG_PURCHASE_SIGNAL_KEYS = {
    "lock_seat",
    "decision_push",
    "payment_inquiry",
    "contact_intent",
    "reservation_ready",
    "converted",
}

STRONG_PURCHASE_LIFECYCLES = {
    "decision",
    "purchase",
    "reservation_ready",
    "converted",
    "sql",
    "opportunity",
}


def clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return max(0.0, min(numeric, 100.0))


def normalize_probability_to_score(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric <= 0:
        return 0.0
    if numeric <= 1:
        numeric *= 100.0
    return clamp_score(numeric)


def extract_contact_info_from_text(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""

    for label, pattern in CONTACT_LABELED_ACCOUNT_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        value = str(match.group(1) or "").strip()
        if not value:
            continue
        if label == "手机号":
            return value
        return f"{label}:{value}"

    compact = CONTACT_ONLY_WRAPPER_PATTERN.sub("", normalized)
    if compact:
        digits_only = re.sub(r"\D", "", compact)
        if CONTACT_ONLY_PHONE_PATTERN.fullmatch(digits_only):
            return digits_only
        if CONTACT_ONLY_DIGIT_PATTERN.fullmatch(compact):
            return f"QQ:{compact}"
        if (
            CONTACT_ONLY_SOCIAL_ID_PATTERN.fullmatch(compact)
            and any(ch.isdigit() for ch in compact)
            and len(compact) <= 20
            and not re.search(r"[\u4e00-\u9fff]", compact)
        ):
            return compact

    for pattern in CONTACT_MARKER_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        if match.lastindex:
            return str(match.group(match.lastindex) or "").strip()
        value = str(match.group() or "").strip()
        digits_only = re.sub(r"\D", "", value)
        if digits_only and re.fullmatch(r"1[3-9]\d{9}", digits_only):
            return digits_only
        return value
    return ""


def extract_contact_info(messages: list[dict[str, Any]] | None = None, customer_name: str = "") -> str:
    inbound_messages = [
        str(item.get("content") or "").strip()
        for item in (messages or [])
        if str(item.get("direction") or "").lower() == "inbound"
    ]
    for text in reversed(inbound_messages):
        contact = extract_contact_info_from_text(text)
        if contact:
            return contact
    return extract_contact_info_from_text(customer_name)


def has_strong_purchase_intent(
    messages: list[dict[str, Any]] | None = None,
    conversation: dict[str, Any] | None = None,
) -> bool:
    conversation = conversation or {}
    inbound_texts = [
        str(item.get("content") or "").strip()
        for item in (messages or [])
        if str(item.get("direction") or "").lower() == "inbound"
    ]
    for text in inbound_texts:
        if any(pattern.search(text) for pattern in STRONG_PURCHASE_PATTERNS):
            return True

    lifecycle_stage = str(
        conversation.get("lifecycle_stage")
        or conversation.get("purchase_stage")
        or ""
    ).strip().lower()
    if lifecycle_stage in STRONG_PURCHASE_LIFECYCLES:
        return True

    signal_values = []
    for key in ("signals_detected", "opportunity_factors", "recommended_response_type", "predicted_next_action"):
        raw_value = conversation.get(key)
        if isinstance(raw_value, list):
            signal_values.extend(str(item or "").strip().lower() for item in raw_value)
        else:
            signal_values.append(str(raw_value or "").strip().lower())

    if any(any(marker in value for marker in STRONG_PURCHASE_SIGNAL_KEYS) for value in signal_values):
        return True

    return False


def intent_level_to_reference_score(level: Any) -> float:
    return INTENT_LEVEL_REFERENCE_SCORES.get(str(level or "").strip().upper(), 0.0)


def lead_score_to_reference_score(level: Any) -> float:
    return LEAD_SCORE_REFERENCE_SCORES.get(str(level or "").strip().lower(), 0.0)


def score_to_intent_level(score: Any) -> str:
    normalized = clamp_score(score)
    for level, floor in INTENT_LEVEL_SCORE_FLOORS.items():
        if normalized >= floor:
            return level
    return "E"


def score_to_lead_score(score: Any) -> str:
    normalized = clamp_score(score)
    for level, floor in LEAD_SCORE_SCORE_FLOORS.items():
        if normalized >= floor:
            return level
    return "cold"


def score_to_follow_up_priority(score: Any) -> str:
    normalized = clamp_score(score)
    if normalized >= 85:
        return "urgent"
    if normalized >= 65:
        return "high"
    if normalized >= 45:
        return "medium"
    return "low"


def build_effective_intent_score(
    *,
    explicit_score: Any = None,
    customer_intent_score: Any = None,
    conversation_intent_score: Any = None,
    purchase_probability: Any = None,
    intent_level: Any = None,
    lead_score: Any = None,
    profile_score: Any = None,
    has_contact_info: bool = False,
    has_strong_purchase_intent: bool = False,
) -> float:
    direct_scores = [
        clamp_score(explicit_score),
        clamp_score(customer_intent_score),
        clamp_score(conversation_intent_score),
    ]
    best_direct_score = max(direct_scores)

    probability_score = normalize_probability_to_score(purchase_probability)
    level_score = intent_level_to_reference_score(intent_level)
    lead_reference_score = lead_score_to_reference_score(lead_score)
    profile_hint_score = clamp_score(profile_score) * 0.15

    inferred_score = (
        probability_score * 0.45
        + level_score * 0.25
        + lead_reference_score * 0.20
        + profile_hint_score
    )

    if best_direct_score > 0:
        inferred_score = max(inferred_score, best_direct_score * 0.85)

    effective_score = max(best_direct_score, inferred_score)
    if has_strong_purchase_intent:
        effective_score = max(effective_score, STRONG_PURCHASE_INTENT_SCORE_FLOOR)
    if has_contact_info:
        effective_score = max(effective_score, CONTACT_PROVIDED_SCORE_FLOOR)

    return round(clamp_score(effective_score), 2)
