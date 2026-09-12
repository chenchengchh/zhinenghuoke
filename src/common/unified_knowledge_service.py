"""
统一知识库服务

整合多个知识库数据源，提供统一的API接口
支持：
- 统一数据格式
- 多数据源切换
- 智能检索（混合检索+重排序）
- 统计分析
"""
import os
import json
import hashlib
import copy
import contextvars
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from functools import lru_cache
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from loguru import logger
from collections import defaultdict
import re
from .rag.content_auditor import get_content_auditor as _get_content_auditor
from .rag.exceptions import GuardrailBlockedError

# 阶段三：统一单例装饰器（用于新代码）
from .core_utils.singleton import singleton as _singleton_decorator

from src.common.unified_retriever import RAGRetriever, create_rag_retriever
from src.common.retrieval_policy_service import (
    DEFAULT_RETRIEVAL_POLICY,
    RetrievalPlan,
    RetrievalPolicyService,
)
from src.common.category_display_config import get_active_category_labels
from src.rag.unified_pipeline import PipelineConfig, UnifiedRetrievalPipeline
from src.rag.bm25_retriever import create_bm25_retriever
from src.infrastructure.config import get_config
from src.infrastructure.runtime_paths import (
    get_base_dir,
    get_chroma_dir,
    get_data_dir,
)


_ACTIVE_SCHEMA_CONTEXT: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "unified_knowledge_service_active_schema_context",
    default={},
)


from src.common.types.knowledge import KnowledgeCategory, KnowledgeItem as _UnifiedKnowledgeItem

from .knowledge_normalizer import knowledge_normalizer
from .knowledge_evidence import knowledge_evidence_collector
from .industry_schema_service import get_industry_schema_service
from .knowledge_normalization_service import knowledge_normalization_service
from .knowledge_persistence import KnowledgePersistence


_CONTROL_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\ufffc]")
_FAQ_PAIR_RE = re.compile(r"Q[：:]\s*(.+?)\s*A[：:]\s*([\s\S]*?)(?=(?:\n\s*Q[：:])|\Z)")
_ROUTE_KEYWORDS = ()
_STRUCTURED_FIELD_RE = re.compile(r"^(所属产品|景点名称|景点介绍|场景介绍|费用口径|可选项目|游玩提醒)[：:]\s*(.+)$")
_PRODUCT_CODE_SUFFIX_RE = re.compile(r"[（(][A-Z0-9_-]{3,}[)）]\s*$")
_FORBIDDEN_ANALYSIS_PATTERNS = (
    re.compile(r"客户(?:说|拿).{0,20}很正常，这时候别急着"),
    re.compile(r"我不建议(?:一上来|随口|提前承诺)"),
    re.compile(r"核心还是看"),
    re.compile(r"选择建议"),
    re.compile(r"意图[-:：]"),
)
_DIRECT_REPLY_MARKERS = ("可以顺着说：", "可以顺着说:", "可以这样说：", "可以这样说:", "可以这样回复：", "可以这样回复:")
_QUESTION_PREFIX_PATTERNS = (
    re.compile(r"^(抖音端|小红书端|销售端|客服端)[：:]\s*"),
    re.compile(r"^(问题|问题是|客户问题)[：:]\s*"),
)
_INTERNAL_TAG_PATTERNS = (
    re.compile(r"^意\s*图[-:：]"),
    re.compile(r"^intent[-:：]", re.I),
)
_QUESTION_SUFFIX_REWRITES = (
    (
        re.compile(r"^(?P<subject>.+?)产品概览$"),
        lambda match: f"{_normalize_text(match.group('subject'))}怎么样？",
    ),
    (
        re.compile(r"^(?P<subject>.+?)价格多少钱$"),
        lambda match: f"{_normalize_text(match.group('subject'))}多少钱？",
    ),
    (
        re.compile(r"^(?P<subject>.+?)价格$"),
        lambda match: f"{_normalize_text(match.group('subject'))}多少钱？",
    ),
    (
        re.compile(r"^(?P<subject>.+?)行程安排$"),
        lambda match: f"{_normalize_text(match.group('subject'))}行程怎么安排？",
    ),
    (
        re.compile(r"^(?P<subject>.+?)注意事项$"),
        lambda match: f"{_normalize_text(match.group('subject'))}有哪些注意事项？",
    ),
    (
        re.compile(r"^(?P<subject>.+?)(?:景点介绍|场景介绍)$"),
        lambda match: f"{_normalize_text(match.group('subject'))}是什么样的场景？",
    ),
)
_QUESTION_INTENT_MARKERS = (
    "怎么", "怎么办", "什么", "为什么", "哪条", "哪个", "哪里", "哪种", "哪一",
    "多少", "多钱", "多少钱", "有没有", "还有没有", "哪些", "有...吗", "吗",
    "能不能", "可不可以", "是否",
    "是不是", "值不值得", "适不适合", "要不要", "需不需要", "愿不愿意", "好不好",
    "在吗", "如何", "区别", "下单", "付款", "退款", "合作", "投诉", "集合",
    "接送", "好玩", "留联", "预留", "联系", "对接", "处理",
)
_KEYWORD_STOP_WORDS = {
    "您好", "你好", "请问", "一下", "一下子", "可以", "能否", "是否",
    "怎么", "怎样", "如何", "多少", "多少钱", "几天", "多久", "哪里", "哪个", "哪些", "什么",
    "一般", "通常", "提前", "当天", "当天的", "一天", "时间", "时候",
    "关键词", "关键字", "标签", "客服", "老师", "导游",
}
_KEYWORD_NOISE_PATTERNS = (
    re.compile(r"^(?:怎么|怎样|如何|多少|多少钱|几天|多久|哪里|哪个|哪些|什么|能不能|可不可以|是否|请问)$", re.I),
    re.compile(r"^[一二三四五六七八九十两0-9]+(?:天|晚|个|次|年|月|小时|分钟)$"),
    re.compile(r"^(?:提前|当天|当日|次日|隔天)[一二三四五六七八九十两0-9]+(?:天|晚|个|次|年|月|小时|分钟)$"),
)
_KEYWORD_ALLOWED_POS_PREFIXES = ("n", "nr", "ns", "nt", "nz", "vn", "eng")


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    lines: List[str] = []
    for raw_line in text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line in {"已读", "未读"}:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _stable_item_id(question: str, answer: str = "") -> str:
    content = f"{_normalize_text(question)}|{_normalize_answer_text(answer)[:120]}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def _normalize_answer_text(answer: Any) -> str:
    text = _normalize_text(answer)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _merge_multi_question_text(text: str) -> str:
    parts = [
        segment.strip("，,。；; ")
        for segment in re.split(r"[？?]+", _normalize_text(text))
        if segment.strip("，,。；; ")
    ]
    if len(parts) <= 1:
        return _normalize_text(text)
    return f"{'，'.join(parts)}？"


def _looks_like_interrogative(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if normalized.endswith(("？", "?")):
        return True
    return any(marker in normalized for marker in _QUESTION_INTENT_MARKERS)


def _rewrite_question_by_shape(text: str, *, category: Any = "", topic: Any = "") -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""

    for pattern, builder in _QUESTION_SUFFIX_REWRITES:
        match = pattern.match(normalized)
        if match:
            return builder(match)

    normalized_category = str(category or "").strip().lower()
    normalized_topic = str(topic or "").strip().lower()
    force_shape_rewrite_categories = {"faq", "policy", "process"}
    if _looks_like_interrogative(normalized) and normalized_category not in force_shape_rewrite_categories:
        return normalized

    if normalized_category in {"product", "course"} or normalized_topic == "overview":
        return f"{normalized}怎么样？"
    if normalized_category == "price":
        return f"{normalized}多少钱？"
    if normalized_category in {"itinerary", "process"}:
        return f"{normalized}怎么安排？"
    if normalized_category == "tips":
        return f"{normalized}有哪些注意事项？"
    if normalized_category == "attractions":
        return f"{normalized}是什么样的场景？"
    if normalized_category == "faq":
        return f"{normalized}是怎么回事？"
    if normalized_category == "service":
        if normalized.startswith(("我", "客户说", "客户问", "客户只")) and not normalized.endswith(("？", "?")):
            return f"客户说“{normalized}”时怎么回复？"

    return normalized


def _normalize_question_text(
    question: Any,
    *,
    category: Any = "",
    topic: Any = "",
    enterprise_id: str = "",
    schema_id: str = "",
) -> Tuple[str, List[str]]:
    original_text = _normalize_text(question)
    text = original_text
    aliases: List[str] = []
    normalization_config = knowledge_normalization_service.get_config(
        enterprise_id=enterprise_id,
        schema_id=schema_id,
    )
    question_rewrites = normalization_config.get("question_rewrites") or {}
    for pattern in _QUESTION_PREFIX_PATTERNS:
        text = pattern.sub("", text)
    stripped_text = text.strip()
    if stripped_text and stripped_text != original_text:
        aliases.append(original_text)

    generic_rewrite = question_rewrites.get(stripped_text)
    normalized_question = _normalize_text(stripped_text).replace("?", "？")
    if generic_rewrite:
        rewritten = _normalize_text(generic_rewrite[0])
    elif normalized_question.endswith("？") and normalized_question.count("？") == 1:
        rewritten = normalized_question
    else:
        rewritten = _rewrite_question_by_shape(
            _merge_multi_question_text(stripped_text),
            category=category,
            topic=topic,
        ).strip()
    if rewritten and rewritten != stripped_text:
        aliases.append(stripped_text)

    if generic_rewrite:
        aliases.extend(generic_rewrite[1])
    aliases.extend(
        _derive_anchor_question_aliases(
            stripped_text,
            category=category,
            topic=topic,
            schema_id=schema_id,
        )
    )

    if rewritten and not rewritten.endswith(("？", "?")) and _looks_like_interrogative(rewritten):
        aliases.append(rewritten)
        rewritten = rewritten.rstrip("。；;，, ")
        rewritten = f"{rewritten}？"

    return rewritten.strip(), aliases


@lru_cache(maxsize=32)
def _get_schema_exact_alias_anchors(schema_id: str) -> Tuple[str, ...]:
    normalized_schema_id = str(schema_id or "").strip()
    if not normalized_schema_id:
        return ()
    try:
        schema = get_industry_schema_service().get_schema(normalized_schema_id) or {}
    except Exception:
        return ()
    metadata = schema.get("metadata") or {}
    followup = metadata.get("followup_strategy") or {}
    grounded_config = schema.get("grounded_config") or {}
    candidate_groups = [
        grounded_config.get("entity_alias_groups") or {},
        followup.get("signal_groups") or {},
        followup.get("route_signal_groups") or {},
        followup.get("entity_alias_groups") or {},
    ]
    anchors: List[str] = []
    for group in candidate_groups:
        if not isinstance(group, dict):
            continue
        for key in group.keys():
            anchor = _normalize_text(key)
            if anchor:
                anchors.append(anchor)
    return tuple(dict.fromkeys(anchors))


def _derive_anchor_question_aliases(
    base_text: str,
    *,
    category: Any = "",
    topic: Any = "",
    schema_id: str = "",
) -> List[str]:
    normalized_base = _normalize_text(base_text)
    if not normalized_base:
        return []
    derived: List[str] = []
    for anchor in _get_schema_exact_alias_anchors(schema_id):
        if not anchor or anchor == normalized_base:
            continue
        if anchor not in normalized_base:
            continue
        rewritten_anchor = _rewrite_question_by_shape(
            anchor,
            category=category,
            topic=topic,
        ).strip()
        if rewritten_anchor and rewritten_anchor != normalized_base:
            derived.append(rewritten_anchor)
    return _normalize_aliases(derived)


def _validate_question_for_storage(question: str, answer: str = "") -> None:
    normalized_question = _normalize_text(question).replace("?", "？")
    normalized_answer = _normalize_answer_text(answer).replace("?", "？")
    if not normalized_question:
        raise ValueError("知识问题不能为空，请整理为便于检索的问句")
    if normalized_answer and normalized_question == normalized_answer:
        raise ValueError("知识问题不能与答案相同，请整理为便于检索的问句")
    if "。" in normalized_question or "；" in normalized_question:
        raise ValueError("知识问题包含陈述句内容，请整理为单一问句")
    if normalized_question.count("？") > 1:
        raise ValueError("知识问题包含多个问句，请整理为单一问句")
    if len(normalized_question) > 90:
        raise ValueError("知识问题过长，请整理为更聚焦的问句")
    if not _looks_like_interrogative(normalized_question):
        raise ValueError("知识问题请整理为便于检索的问句")


def _normalize_list_tokens(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []

    normalized: List[str] = []
    seen = set()
    for value in values:
        text = _normalize_text(value)
        if not text:
            continue
        if any(pattern.match(text) for pattern in _INTERNAL_TAG_PATTERNS):
            continue
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


def _is_noise_keyword_token(text: Any) -> bool:
    normalized = _normalize_text(text).strip("，。！？；：、,.!?;:()[]{}<>《》\"'`")
    lowered = normalized.lower()
    if not normalized:
        return True
    if lowered in _KEYWORD_STOP_WORDS:
        return True
    if re.fullmatch(r"\d+", normalized):
        return True
    if len(normalized) == 1 and not re.search(r"[A-Za-z]", normalized):
        return True
    return any(pattern.fullmatch(normalized) for pattern in _KEYWORD_NOISE_PATTERNS)


def build_trigger_keywords(
    question: Any,
    answer: Any = "",
    *,
    provided_keywords: Any = None,
    aliases: Any = None,
    top_k: int = 5,
) -> List[str]:
    question_text = _normalize_text(question)
    answer_text = _normalize_answer_text(answer)
    normalized_aliases = _normalize_aliases(aliases or [])
    normalized_provided = _normalize_list_tokens(provided_keywords or [])

    combined_parts: List[str] = []
    if question_text:
        combined_parts.extend([question_text, question_text])
    if normalized_aliases:
        combined_parts.extend(normalized_aliases[:3])
    if answer_text:
        combined_parts.append(answer_text[:500])
    combined_text = "\n".join(part for part in combined_parts if part).strip()
    if not combined_text and normalized_provided:
        combined_text = " ".join(normalized_provided)
    if not combined_text:
        return []

    ranked_candidates: List[Tuple[str, float]] = []
    for keyword in normalized_provided:
        ranked_candidates.append((keyword, 30.0))

    try:
        import jieba.analyse

        ranked_candidates.extend(
            [
                (str(word or "").strip(), float(weight or 0))
                for word, weight in jieba.analyse.extract_tags(combined_text, topK=16, withWeight=True)
            ]
        )
    except Exception:
        ranked_candidates.extend(
            [
                (match, float(len(match)))
                for match in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,20}", combined_text)
            ]
        )

    try:
        import jieba.posseg as pseg

        for pair in pseg.cut("\n".join(part for part in [question_text, " ".join(normalized_aliases), answer_text[:200]] if part)):
            token = _normalize_text(getattr(pair, "word", ""))
            flag = str(getattr(pair, "flag", "") or "")
            if token and flag and any(flag.startswith(prefix) for prefix in _KEYWORD_ALLOWED_POS_PREFIXES):
                ranked_candidates.append((token, 8.0 if token in question_text else 4.0))
    except Exception:
        pass

    question_lower = question_text.lower()
    answer_lower = answer_text.lower()
    alias_lower = " ".join(normalized_aliases).lower()
    provided_lower = {keyword.lower() for keyword in normalized_provided}
    scored_candidates: List[Tuple[str, float]] = []
    for raw_word, base_score in ranked_candidates:
        word = _normalize_text(raw_word).strip("，。！？；：、,.!?;:()[]{}<>《》\"'`")
        word_lower = word.lower()
        if _is_noise_keyword_token(word):
            continue
        if len(word) < 2 or len(word) > 20:
            continue
        score = float(base_score or 0)
        if word_lower in provided_lower:
            score += 8.0
        if word_lower and word_lower in question_lower:
            score += 5.0
        elif word_lower and word_lower in alias_lower:
            score += 3.5
        elif word_lower and word_lower in answer_lower:
            score += 1.0
        scored_candidates.append((word, score))

    scored_candidates.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    keywords: List[str] = []
    for word, _score in scored_candidates:
        if any(word == existing or word in existing or existing in word for existing in keywords):
            continue
        keywords.append(word)
        if len(keywords) >= max(int(top_k or 5), 1):
            break
    return keywords


def _remove_product_code_suffix(text: str) -> str:
    return _PRODUCT_CODE_SUFFIX_RE.sub("", _normalize_text(text)).strip()


def _humanize_structured_answer(answer: str) -> str:
    fields: Dict[str, str] = {}
    for line in _normalize_answer_text(answer).split("\n"):
        match = _STRUCTURED_FIELD_RE.match(line)
        if not match:
            continue
        fields[match.group(1)] = _normalize_text(match.group(2))

    if not fields:
        return _normalize_answer_text(answer)

    product = _remove_product_code_suffix(fields.get("所属产品", ""))
    name = _normalize_text(fields.get("景点名称", ""))
    intro = _normalize_text(fields.get("景点介绍", "") or fields.get("场景介绍", ""))
    fee = _normalize_text(fields.get("费用口径", ""))
    optional_item = _normalize_text(fields.get("可选项目", ""))
    reminder = _normalize_text(fields.get("游玩提醒", ""))

    parts: List[str] = []
    if name and intro:
        parts.append(f"{name}这边主要看的是{intro}")
    elif intro:
        parts.append(intro)

    if product:
        parts.append(f"如果您走的是{product}，这个景点就在行程里。")
    if fee:
        parts.append(f"费用这块是这样的：{fee}")
    if optional_item:
        parts.append(f"另外有可自愿选择的项目：{optional_item}")
    if reminder:
        parts.append(f"再提醒您一下，{reminder}")

    return _normalize_answer_text(" ".join(parts))


def _extract_direct_reply(answer: str) -> str:
    text = _normalize_answer_text(answer)
    for marker in _DIRECT_REPLY_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    return text


def _humanize_analysis_answer(answer: str) -> str:
    text = _extract_direct_reply(answer)
    replacements = (
        (r"^正常做法是先把", "一般先把"),
        (r"^一般都是先把", "一般先把"),
        (r"^优惠能不能争取，核心还是看", "优惠这块主要还是看"),
        (r"^按产品资料，", ""),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"^客户(?:说|拿).{0,40}很正常，这时候别急着[^。！？]*[。！？]\s*", "", text)
    return _normalize_answer_text(text)


def _contains_forbidden_analysis_text(text: str) -> bool:
    normalized = _normalize_answer_text(text)
    return any(pattern.search(normalized) for pattern in _FORBIDDEN_ANALYSIS_PATTERNS)


def _sanitize_answer_for_storage(answer: Any) -> str:
    text = _normalize_answer_text(answer)
    if not text:
        return ""
    if any(_STRUCTURED_FIELD_RE.match(line) for line in text.split("\n")):
        text = _humanize_structured_answer(text)
    text = _humanize_analysis_answer(text)
    if _contains_forbidden_analysis_text(text):
        raise ValueError("知识答案包含内部分析或意图标注，请整理为面向客户的自然表达后再保存")
    return text


def _sanitize_knowledge_item_payload(
    item: Dict[str, Any],
    *,
    strict_question_validation: bool = True,
    enterprise_id: str = "",
    schema_id: str = "",
) -> Dict[str, Any]:
    sanitized = dict(item)
    effective_enterprise_id = str(
        enterprise_id or sanitized.get("enterprise_id") or ""
    ).strip()
    effective_schema_id = str(
        schema_id
        or sanitized.get("schema_id")
        or ((sanitized.get("metadata") or {}).get("schema_id") if isinstance(sanitized.get("metadata"), dict) else "")
        or ""
    ).strip()
    normalized_question, question_aliases = _normalize_question_text(
        sanitized.get("question", ""),
        category=sanitized.get("category", ""),
        topic=sanitized.get("topic", ""),
        enterprise_id=effective_enterprise_id,
        schema_id=effective_schema_id,
    )
    merged_aliases = list(sanitized.get("aliases", []) or [])
    merged_aliases.extend(question_aliases)
    sanitized["question"] = normalized_question
    sanitized["answer"] = _sanitize_answer_for_storage(sanitized.get("answer", ""))
    if strict_question_validation:
        _validate_question_for_storage(sanitized["question"], sanitized["answer"])
    elif not _normalize_text(sanitized["question"]):
        raise ValueError("知识问题不能为空")
    sanitized["aliases"] = _normalize_aliases(merged_aliases)
    sanitized["keywords"] = build_trigger_keywords(
        sanitized["question"],
        sanitized["answer"],
        provided_keywords=sanitized.get("keywords", []),
        aliases=sanitized["aliases"],
    )
    sanitized["tags"] = _normalize_list_tokens(sanitized.get("tags", []))
    if strict_question_validation and _contains_forbidden_analysis_text(sanitized["question"]):
        raise ValueError("知识问题包含内部分析或意图标注，请整理后再保存")
    return sanitized


def _normalize_aliases(aliases: Any) -> List[str]:
    normalized = _normalize_list_tokens(aliases)
    return [text for text in normalized if text not in {"无", "暂无", "None"}]


def _normalize_runtime_flags(item_data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item_data)
    tags = _normalize_aliases(normalized.get("tags", []))
    normalized["tags"] = tags
    if "待人工审核" in tags:
        normalized["enabled"] = False
    return normalized


def _looks_like_faq_bundle(question: str, answer: str) -> bool:
    text = _normalize_answer_text(answer)
    if text.count("Q：") + text.count("Q:") < 2:
        return False
    if text.count("A：") + text.count("A:") < 2:
        return False
    return "常见问题" in _normalize_text(question) or text.startswith("产品：")


def _derive_bundle_prefix(question: str, answer: str) -> str:
    normalized_question = _normalize_text(question)
    normalized_answer = _normalize_answer_text(answer)
    first_line = normalized_answer.split("\n", 1)[0] if normalized_answer else ""
    if first_line.startswith("产品："):
        return first_line.split("：", 1)[1].strip()
    return normalized_question.replace("常见问题", "").strip("：: ")


def _merge_keywords(base_keywords: Any, sub_question: str) -> List[str]:
    merged: List[str] = []
    seen = set()

    for keyword in base_keywords if isinstance(base_keywords, list) else []:
        text = _normalize_text(keyword)
        if text in {"FAQ", "常见问题"}:
            continue
        if text and text not in seen:
            seen.add(text)
            merged.append(text)

    for token in re.split(r"[，,、/（）()·\s]+", sub_question):
        token = token.strip("？?。！! ")
        if len(token) < 2:
            continue
        if token not in seen:
            seen.add(token)
            merged.append(token)

    return merged[:12]


def _build_bundle_question(prefix: str, sub_question: str) -> str:
    sub_question = _normalize_text(sub_question).rstrip("。")
    if not sub_question.endswith(("？", "?")):
        sub_question = f"{sub_question}？"

    if any(keyword in sub_question for keyword in _ROUTE_KEYWORDS):
        normalized_question, _ = _normalize_question_text(sub_question)
        return normalized_question

    clean_prefix = _normalize_text(prefix).strip("：: ")
    combined = f"{clean_prefix}{sub_question}" if clean_prefix else sub_question
    normalized_question, _ = _normalize_question_text(combined)
    return normalized_question


def _expand_faq_bundle_item(item_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    question = _normalize_text(item_data.get("question", ""))
    answer = _normalize_answer_text(item_data.get("answer", ""))
    if not _looks_like_faq_bundle(question, answer):
        return []

    prefix = _derive_bundle_prefix(question, answer)
    base_data = dict(item_data)
    base_data.pop("id", None)
    base_data["aliases"] = _normalize_aliases(base_data.get("aliases", []))
    base_data["tags"] = [tag for tag in base_data.get("tags", []) if _normalize_text(tag) not in {"FAQ", "常见问题"}]

    expanded_items: List[Dict[str, Any]] = []
    for sub_question, sub_answer in _FAQ_PAIR_RE.findall(answer):
        normalized_question = _build_bundle_question(prefix, sub_question)
        normalized_answer = _normalize_answer_text(sub_answer)
        if not normalized_question or not normalized_answer:
            continue

        aliases = list(base_data.get("aliases", []))
        raw_sub_question = _normalize_text(sub_question)
        if raw_sub_question and raw_sub_question not in aliases:
            aliases.append(raw_sub_question)

        new_item = {
            **base_data,
            "id": _stable_item_id(normalized_question, normalized_answer),
            "question": normalized_question,
            "answer": normalized_answer,
            "aliases": aliases,
            "keywords": _merge_keywords(base_data.get("keywords", []), raw_sub_question),
            "metadata": {
                **(base_data.get("metadata") or {}),
                "normalized_from_bundle": True,
                "bundle_question": question,
            },
        }
        expanded_items.append(new_item)

    return expanded_items


def normalize_knowledge_payloads(
    data: List[Dict[str, Any]],
    source_hint: str = "",
    *,
    strict_question_validation: bool = True,
    skip_invalid_items: bool = False,
    enterprise_id: str = "",
    schema_id: str = "",
) -> List[Dict[str, Any]]:
    normalized_items: List[Dict[str, Any]] = []

    for index, raw_item in enumerate(data or []):
        if not isinstance(raw_item, dict):
            continue

        item = dict(raw_item)
        try:
            item = _sanitize_knowledge_item_payload(
                item,
                strict_question_validation=strict_question_validation,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )
        except Exception as exc:
            if skip_invalid_items:
                logger.warning(
                    "跳过无效知识条目: "
                    f"index={index} source={source_hint or item.get('source', '') or 'unknown'} "
                    f"reason={exc}"
                )
                continue
            raise
        if source_hint and not item.get("source"):
            item["source"] = source_hint
        item["id"] = str(item.get("id") or _stable_item_id(item["question"], item["answer"]))

        expanded_items = _expand_faq_bundle_item(item)
        if expanded_items:
            normalized_items.extend(expanded_items)
            continue

        normalized_items.append(item)

    return normalized_items


class KnowledgeStatus(Enum):
    """知识状态"""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    DEPRECATED = "deprecated"


class DataSource(Enum):
    """数据源类型"""
    ALL = "all"
    MAIN = "main"
    ENTERPRISE = "enterprise"


UnifiedKnowledgeItem = _UnifiedKnowledgeItem


@dataclass
class StructuredEvidenceBundle:
    """面向回复链的结构化证据包。"""
    query: str = ""
    bundle_type: str = "route"
    grounded_config: Dict[str, Any] = field(default_factory=dict)
    field_labels: Dict[str, str] = field(default_factory=dict)
    routes: List[str] = field(default_factory=list)
    overview_items: List[UnifiedKnowledgeItem] = field(default_factory=list)
    route_evidence: Dict[str, Dict[str, UnifiedKnowledgeItem]] = field(default_factory=dict)
    route_facts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    comparison_summary: str = ""

    def to_prompt_text(self) -> str:
        lines: List[str] = [
            f"证据包类型：{self.bundle_type}",
            f"关联线路：{'、'.join(self.routes) if self.routes else '未识别'}",
        ]
        if self.overview_items:
            lines.append("总览证据：")
            for item in self.overview_items[:2]:
                lines.append(f"- {item.question}: {item.answer}")
        if self.comparison_summary:
            lines.append("事实对比摘要：")
            lines.extend(f"- {line}" for line in self.comparison_summary.split("\n") if line.strip())

        for route in self.routes:
            fact_view = self.route_facts.get(route, {})
            if fact_view:
                lines.append(f"线路事实：{route}")
                summary_fields = [
                    ("canonical_name", "线路名"),
                    ("price", "价格"),
                    ("pace", "节奏"),
                    ("budget_level", "预算"),
                    ("shopping", "购物属性"),
                ]
                for field_name, label in summary_fields:
                    value = fact_view.get(field_name)
                    if value:
                        lines.append(f"- {label}: {value}")
                for field_name, label in (
                    ("suitable_for", "适合人群"),
                    ("not_suitable_for", "不太适合"),
                    ("highlights", "亮点"),
                    ("includes", "费用包含"),
                    ("excludes", "费用不含"),
                ):
                    values = fact_view.get(field_name) or []
                    if values:
                        lines.append(f"- {label}: {'、'.join(values)}")
            evidence = self.route_evidence.get(route, {})
            if not evidence:
                continue
            lines.append(f"线路：{route}")
            for category in ("product", "price", "itinerary", "tips", "service", "attractions"):
                item = evidence.get(category)
                if not item:
                    continue
                lines.append(f"- {category}: {item.answer}")
        return "\n".join(lines).strip()

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "bundle_type": self.bundle_type,
            "schema_bundle_type": str((self.grounded_config or {}).get("bundle_type") or "").strip(),
            "comparison_fields": list((self.grounded_config or {}).get("comparison_fields") or []),
            "followup_fields": list((self.grounded_config or {}).get("followup_fields") or []),
            "routes": list(self.routes),
            "overview_questions": [item.question for item in self.overview_items[:2]],
            "has_comparison_summary": bool(self.comparison_summary),
            "route_fact_keys": {
                route: sorted(list(facts.keys()))
                for route, facts in self.route_facts.items()
                if facts
            },
            "route_categories": {
                route: sorted(list(evidence.keys()))
                for route, evidence in self.route_evidence.items()
            },
        }


@dataclass
class RouteFactSheet:
    """从现有知识条目抽取得到的线路事实视图。"""
    route_name: str
    canonical_name: str = ""
    price: str = ""
    pace: str = ""
    budget_level: str = ""
    shopping: str = ""
    suitable_for: List[str] = field(default_factory=list)
    not_suitable_for: List[str] = field(default_factory=list)
    highlights: List[str] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)
    excludes: List[str] = field(default_factory=list)
    source_questions: Dict[str, str] = field(default_factory=dict)

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "route_name": self.route_name,
            "canonical_name": self.canonical_name or self.route_name,
            "price": self.price,
            "pace": self.pace,
            "budget_level": self.budget_level,
            "shopping": self.shopping,
            "suitable_for": list(self.suitable_for),
            "not_suitable_for": list(self.not_suitable_for),
            "highlights": list(self.highlights),
            "includes": list(self.includes),
            "excludes": list(self.excludes),
            "source_questions": dict(self.source_questions),
        }


class UnifiedKnowledgeService:
    """
    统一知识库服务
    
    整合多个知识库数据源，提供统一的API接口
    """
    
    _instance = None
    _lock = threading.Lock()
    _search_executor = ThreadPoolExecutor(max_workers=2)
    DEFAULT_ENTERPRISE_ID = "default"
    LEGACY_ENTERPRISE_IDS = ["default", "main", "enterprise"]
    
    def __new__(cls, *args, **kwargs):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, data_path: Path = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        
        self.data_path = data_path or get_data_dir()
        self.unified_data_path = self.data_path / "knowledge_base.json"
        self.legacy_data_path = self.data_path / "knowledge_unified.json"
        self.enterprise_data_path = self.data_path / "knowledge" / "knowledge_items.json"
        self.vector_manifest_path = self.data_path / "vector_sync_manifest.json"
        
        self._items: List[UnifiedKnowledgeItem] = []
        self._data_lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._cache_time: float = 0
        self._cache_ttl: int = 60
        self._file_signature: Optional[Tuple[int, int]] = None
        
        # 向量存储
        self._vector_store = None
        self._vector_enabled = False
        self._vector_store_initialized = False

        self._persistence = KnowledgePersistence(data_dir=str(self.data_path))

        # 先加载主知识数据，再初始化检索器，避免检索器拿到旧/空数据快照。
        self._load_data()
        self._retrieval_policy_service = RetrievalPolicyService()
        
        # RAG检索器（混合检索+重排序）
        self._rag_retriever: Optional[RAGRetriever] = None
        self._rag_retriever_initialized = False
        self._retrieval_pipeline: Optional[UnifiedRetrievalPipeline] = None
        self._warmup_lock = threading.Lock()
        self._warmup_completed = False
        self._warmup_last_error = ""
        # 统一RAG服务（新版）
        self._update_file_signature()
        self._initialized = True
        logger.info(f"统一知识库服务初始化完成，共 {len(self._items)} 条知识")

    @property
    def knowledge_items(self) -> List[UnifiedKnowledgeItem]:
        """兼容旧接口，供知识图谱和部分RAG组件读取知识条目。"""
        self._refresh_if_external_change(resync_vector=True)
        return self._items
    
    def _init_vector_store(self):
        """
        初始化向量存储
        
        尝试初始化Chroma向量数据库用于语义检索
        """
        try:
            from src.rag.vector_store import ChromaVectorStore, ChromaConfig
            from src.rag.embedding_service import build_embedding_config_from_app_config, get_embedding_service
            
            app_config = get_config()
            config = ChromaConfig(
                persistDirectory=str(get_chroma_dir()),
                collectionPrefix="enterprise_"
            )
            
            embed_config = build_embedding_config_from_app_config(
                app_config=app_config,
            )
            embedding_service = get_embedding_service(embed_config)
            
            self._vector_store = ChromaVectorStore(config, embeddingService=embedding_service)
            
            total_vector_count = 0
            for eid in self._get_vector_enterprise_ids("all"):
                try:
                    stats = self._vector_store.getCollectionStats(eid)
                    total_vector_count += stats.get("count", 0)
                except Exception:
                    pass
            
            if total_vector_count == 0:
                try:
                    all_collections = self._vector_store.listCollections()
                    for coll in all_collections:
                        total_vector_count += coll.get("count", 0)
                except Exception:
                    pass
            
            if total_vector_count > 0:
                self._vector_enabled = True
                logger.info(f"向量存储初始化成功，已启用语义检索，向量总数: {total_vector_count}")
            else:
                logger.warning("向量存储为空，请运行 scripts/sync_knowledge_to_vector.py 同步数据")
        except Exception as e:
            logger.warning(f"向量存储初始化失败: {e}，将使用关键词检索")
            self._vector_store = None
            self._vector_enabled = False
    
    def _init_rag_retriever(self):
        """
        初始化RAG检索器
        
        在数据加载后调用，使用知识库条目初始化混合检索
        """
        try:
            # 先加载数据
            self._load_data_for_rag()
            
            if self._items:
                enterprise_ids = self._get_vector_enterprise_ids("all") if self._vector_enabled else None
                self._rag_retriever = create_rag_retriever(
                    knowledge_items=self._items,
                    vector_store=self._vector_store,
                    enterprise_ids=enterprise_ids,
                )
                logger.info("RAG检索器初始化成功，已启用混合检索+重排序")
        except Exception as e:
            logger.warning(f"RAG检索器初始化失败: {e}，将使用基础检索")
            self._rag_retriever = None
    
    def _load_data_for_rag(self):
        """为RAG检索器预加载数据"""
        if self._items:
            return
        
        warmed_dicts = self._persistence.warmup()
        if warmed_dicts:
            self._items = [UnifiedKnowledgeItem.from_dict(item) for item in warmed_dicts]
    
    def _load_data(self):
        """加载数据，优先使用统一数据文件 knowledge_base.json"""
        normalized_items = self._persistence.load_knowledge_base()
        self._items = [UnifiedKnowledgeItem.from_dict(item) for item in normalized_items]
        self._invalidate_cache()
    
    def _invalidate_cache(self):
        """清除缓存

        P0-1: 联动失效知识图谱持久化缓存，确保知识更新后图谱能重建。
        """
        self._persistence.invalidate_cache()
        self._cache = {}
        self._cache_time = 0
        # 联动失效知识图谱缓存（best-effort，失败不影响主链）
        try:
            from src.common.knowledge_graph import get_knowledge_graph_service
            graph_service = get_knowledge_graph_service()
            if graph_service is not None:
                context = self._resolve_active_schema_context()
                schema_id = str(context.get("schema_id") or "")
                if not schema_id:
                    try:
                        schema_id = str(
                            (get_industry_schema_service().get_settings() or {}).get("active_schema_id") or ""
                        ).strip()
                    except Exception:
                        pass
                graph_service.invalidate_cache(schema_id=schema_id)
        except Exception as exc:
            logger.debug(f"联动失效知识图谱缓存失败（可忽略）: {exc}")

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        """归一化搜索文本，提升中文精确问法命中率。"""
        lowered = (text or "").strip().lower()
        if not lowered:
            return ""
        lowered = re.sub(r"\s+", "", lowered)
        return re.sub(r"[^\w\u4e00-\u9fff]", "", lowered)

    @staticmethod
    def _resolve_active_schema_context(
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, str]:
        context = _ACTIVE_SCHEMA_CONTEXT.get({}) or {}
        return {
            "enterprise_id": str(enterprise_id or context.get("enterprise_id") or ""),
            "schema_id": str(schema_id or context.get("schema_id") or ""),
        }

    @classmethod
    def _get_active_schema(
        cls,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, Any]:
        context = cls._resolve_active_schema_context(
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        effective_enterprise = str(context.get("enterprise_id") or "")
        effective_schema_id = str(context.get("schema_id") or "")
        # 当 enterprise_id 和 schema_id 均为空时，fail_closed 模式会返回空 schema。
        # 回退到 settings.active_schema_id 作为 preferred_schema_id，与 schema_category_resolver 保持一致。
        if not effective_enterprise and not effective_schema_id:
            try:
                effective_schema_id = str(
                    (get_industry_schema_service().get_settings() or {}).get("active_schema_id") or ""
                ).strip()
            except Exception:
                pass
        try:
            return get_industry_schema_service().get_active_schema(
                enterprise_id=effective_enterprise,
                preferred_schema_id=effective_schema_id,
            ) or {}
        except Exception:
            return {}

    def _get_domain_config(self, key, default=None):
        context = self._resolve_active_schema_context()
        schema = {}
        if hasattr(self, "_get_active_schema"):
            try:
                schema = self._get_active_schema(
                    enterprise_id=context.get("enterprise_id", ""),
                    schema_id=context.get("schema_id", ""),
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc or ""):
                    raise
                schema = self._get_active_schema()
        if not schema:
            return default
        return schema.get("metadata", {}).get(key, default)

    def _should_expect_domain_config(self) -> bool:
        context = self._resolve_active_schema_context()
        schema = {}
        if hasattr(self, "_get_active_schema"):
            try:
                schema = self._get_active_schema(
                    enterprise_id=context.get("enterprise_id", ""),
                    schema_id=context.get("schema_id", ""),
                )
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc or ""):
                    raise
                schema = self._get_active_schema()
        if not isinstance(schema, dict) or not schema:
            return False
        if bool(schema.get("is_domain_specific", False)):
            return True
        # 通过 schema metadata 是否配置了行业专用字段判断是否为行业 schema，
        # 不再硬编码旅游专用 entity_type（route/travel/tour/itinerary）
        metadata = schema.get("metadata") or {}
        if metadata.get("query_understanding") or metadata.get("category_profiles"):
            return True
        entity_type = str(schema.get("entity_type") or "").strip().lower()
        # 通用 entity_type 不视为行业专用
        generic_entity_types = {"", "product", "service", "faq", "generic"}
        return entity_type not in generic_entity_types

    def _log_missing_schema_config_once(self, key: str, message: str) -> None:
        warned_keys = getattr(self, "_missing_schema_config_warned", None)
        if warned_keys is None:
            warned_keys = set()
            self._missing_schema_config_warned = warned_keys
        if key in warned_keys:
            return
        warned_keys.add(key)
        logger.debug(message)

    def _get_route_keywords(self):
        kw = self._get_domain_config("query_understanding", {}).get("domain_keywords", [])
        if not kw:
            kw = self._get_domain_config("domain_keywords", [])
        if not kw:
            self._log_missing_schema_config_once(
                "domain_keywords",
                "Schema未配置 domain_keywords，返回空元组",
            )
            return ()
        return tuple(kw)

    def _get_schema_core_categories(self) -> set:
        """从 schema metadata.category_profiles 读取行业核心类别集合。

        用于替代硬编码的旅游专用类别集合（如 {"product", "price", "itinerary", "tips", "attractions"}）。
        通用类别（product/price/service/policy/faq）始终包含，行业专用类别由 schema 追加。
        """
        core = {"product", "price", "service", "policy", "faq"}
        category_profiles = self._get_domain_config("category_profiles", {}) or {}
        if isinstance(category_profiles, dict):
            for key in category_profiles.keys():
                normalized = str(key or "").strip().lower()
                if normalized:
                    core.add(normalized)
        return core

    def _get_schema_boost_categories(self) -> set:
        """从 schema query_understanding.boost_categories 读取搜索加价类别集合。

        用于 _apply_query_bias 中决定哪些类别获得搜索加分。
        boost_categories 是 schema 配置的需优先曝光的类别子集（如旅游的 product/price/itinerary/tips/attractions），
        不同于 _get_schema_core_categories 返回的全部类别集合。
        缺失时回退到通用核心类别 {"product", "price", "service", "policy", "faq"}。
        """
        default_boost = {"product", "price", "service", "policy", "faq"}
        query_understanding = self._get_domain_config("query_understanding", {}) or {}
        if not isinstance(query_understanding, dict):
            return default_boost
        raw = query_understanding.get("boost_categories") or []
        if not isinstance(raw, list):
            return default_boost
        normalized = {str(item or "").strip().lower() for item in raw if str(item or "").strip()}
        return normalized if normalized else default_boost

    def _get_domain_query_terms(self):
        terms = self._get_domain_config("domain_query_terms", [])
        if not terms:
            terms = self._get_domain_config("query_understanding", {}).get("domain_keywords", [])
        if not terms:
            self._log_missing_schema_config_once(
                "domain_query_terms",
                "Schema未配置 domain_query_terms/query_understanding.domain_keywords，返回空元组",
            )
            return ()
        return tuple(terms)

    def _get_domain_item_terms(self):
        terms = self._get_domain_config("domain_item_terms", [])
        if not terms:
            followup = self._get_domain_config("followup_strategy", {})
            terms = list(followup.get("anchor_terms", []) or [])
            entity_keywords = self._get_domain_config("domain_entity_keywords", {})
            if isinstance(entity_keywords, dict):
                terms.extend(entity_keywords.get("product", []) or [])
        if not terms:
            if self._should_expect_domain_config():
                self._log_missing_schema_config_once(
                    "domain_item_terms",
                    "Schema未配置 domain_item_terms，返回空元组",
                )
            return ()
        return tuple(terms)

    def _get_domain_scenic_spots(self):
        spots = self._get_domain_config("domain_scenic_spots", [])
        if not spots:
            logger.warning("Schema未配置 domain_scenic_spots，返回空元组")
            return ()
        return tuple(spots)

    def _is_sales_script_item(self, item: UnifiedKnowledgeItem) -> bool:
        question = item.question or ""
        tags = {str(tag).strip() for tag in (item.tags or [])}
        keywords = {str(keyword).strip() for keyword in (item.keywords or [])}
        return (
            question.startswith("抖音端：")
            or "销售话术" in tags
            or "异议处理" in tags
            or "话术" in keywords
        )

    def _is_sales_script_query(self, query: str) -> bool:
        text = query or ""
        sales_terms = (
            "怎么回复", "怎么跟进", "话术", "留资", "催单", "逼单", "异议",
            "客户嫌贵", "不回复", "家人商量", "优惠怎么回", "对比别家",
        )
        return any(term in text for term in sales_terms)

    def _is_explicit_contact_query(self, query: str) -> bool:
        text = query or ""
        # 通用联系咨询词，行业专用词（如旅游的 发行程/行程发我）由 schema contact_inquiry_markers 提供
        contact_terms = [
            "联系方式", "怎么联系", "联系你", "联系您",
            "电话多少", "微信多少", "vx", "wx", "加微信",
            "发我资料", "资料发我", "报价发我",
            "留电话", "留个联系方式", "留联系方式",
        ]
        # 合并 schema 行业专用联系咨询词
        query_understanding = self._get_domain_config("query_understanding", {}) or {}
        schema_contact_terms = query_understanding.get("contact_inquiry_markers") or []
        if isinstance(schema_contact_terms, list):
            contact_terms.extend(str(t or "").strip() for t in schema_contact_terms if str(t or "").strip())
        return any(term in text for term in contact_terms)

    def _is_contact_polluted_item(self, item: UnifiedKnowledgeItem) -> bool:
        question = str(getattr(item, "question", "") or "")
        answer = str(getattr(item, "answer", "") or "")
        source = str(getattr(item, "source", "") or "").strip().lower()
        combined = f"{question}\n{answer}"
        config = knowledge_normalization_service.get_config(
            enterprise_id=getattr(item, "enterprise_id", "") or "",
            schema_id=getattr(item, "schema_id", "") or "",
        )
        if any(marker in combined for marker in list(config.get("contact_guidance_markers") or [])):
            return True
        if source == "conversation" and any(
            marker in combined for marker in list(config.get("contact_soft_markers") or [])
        ):
            return True
        return False

    def _is_domain_specific_query(self, query: str) -> bool:
        text = query or ""
        # 行业专用查询词由 schema query_understanding.domain_query_terms 提供，
        # 不再硬编码旅游专用术语（怎么选/哪个好/选哪条/第一次来/不想太累 等）
        route_terms = self._get_domain_config("query_understanding", {}).get("domain_query_terms", [])
        if not route_terms:
            route_terms = self._get_domain_query_terms()
        return any(term in text for term in route_terms)

    def _is_domain_specific_item(self, item: UnifiedKnowledgeItem) -> bool:
        haystack = " ".join(
            [
                item.question or "",
                item.answer or "",
                " ".join(item.aliases or []),
                " ".join(item.tags or []),
                " ".join(item.keywords or []),
            ]
        )
        route_terms = self._get_domain_item_terms()
        return any(term in haystack for term in route_terms)

    @staticmethod
    def _normalize_signal_text(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"[\s·•・（）()_\-]+", "", text)
        return text.lower()

    def _strip_route_label_suffix(
        self,
        text: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> str:
        cleaned = str(text or "").strip()
        cleaned = cleaned.strip("：:，,。！？!? ")
        config = knowledge_normalization_service.get_config(
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )
        patterns = [
            re.compile(str(pattern or "").strip())
            for pattern in list(config.get("label_suffix_patterns") or [])
            if str(pattern or "").strip()
        ]
        for pattern in patterns:
            updated = pattern.sub("", cleaned).strip("：:，,。！？!? ")
            if updated and updated != cleaned:
                cleaned = updated
        return cleaned.strip()

    def _choose_route_anchor(self, label: str, anchors: List[str]) -> str:
        normalized_label = self._normalize_signal_text(label)
        if not normalized_label:
            return ""
        matched_anchor = ""
        matched_len = -1
        for anchor in anchors:
            normalized_anchor = self._normalize_signal_text(anchor)
            if not normalized_anchor:
                continue
            if normalized_anchor in normalized_label or normalized_label in normalized_anchor:
                if len(normalized_anchor) > matched_len:
                    matched_anchor = str(anchor)
                    matched_len = len(normalized_anchor)
        return matched_anchor

    def _extract_route_labels_from_item(self, item: UnifiedKnowledgeItem) -> List[str]:
        labels: List[str] = []
        metadata = getattr(item, "metadata", {}) or {}
        config = knowledge_normalization_service.get_config(
            enterprise_id=getattr(item, "enterprise_id", "") or "",
            schema_id=getattr(item, "schema_id", "") or "",
        )
        generic_terms = {
            self._normalize_signal_text(term)
            for term in list(config.get("generic_terms") or [])
            if self._normalize_signal_text(term)
        }
        for candidate in (
            metadata.get("entity_name"),
            metadata.get("route_name"),
            item.question,
            *(item.aliases or []),
        ):
            text = self._strip_route_label_suffix(
                str(candidate or ""),
                enterprise_id=getattr(item, "enterprise_id", "") or "",
                schema_id=getattr(item, "schema_id", "") or "",
            )
            if not text or len(text) < 2:
                continue
            normalized = self._normalize_signal_text(text)
            if not normalized or normalized in generic_terms:
                continue
            labels.append(text)
        return self._dedupe_preserve_order(labels)

    def _normalize_signal_groups(self, groups: Dict[str, Any]) -> Dict[str, Tuple[str, ...]]:
        normalized: Dict[str, Tuple[str, ...]] = {}
        for route_name, signals in (groups or {}).items():
            values: List[str] = []
            if isinstance(signals, (list, tuple, set)):
                values.extend(str(signal or "").strip() for signal in signals)
            elif isinstance(signals, str):
                values.append(signals.strip())
            values.append(str(route_name or "").strip())
            deduped: List[str] = []
            seen = set()
            for value in values:
                if not value:
                    continue
                for candidate in (value, self._normalize_signal_text(value)):
                    candidate = str(candidate or "").strip()
                    if not candidate or candidate in seen:
                        continue
                    seen.add(candidate)
                    deduped.append(candidate)
            if deduped:
                normalized[str(route_name)] = tuple(deduped)
        return normalized

    def _ensure_cache_state(self) -> None:
        if not hasattr(self, "_cache") or not isinstance(getattr(self, "_cache", None), dict):
            self._cache = {}
        if not hasattr(self, "_cache_time"):
            self._cache_time = 0
        if not hasattr(self, "_cache_ttl"):
            self._cache_ttl = 60

    def _get_domain_signal_groups(self) -> Dict[str, Tuple[str, ...]]:
        self._ensure_cache_state()
        cache_key = "_domain_signal_groups"
        cached = self._cache.get(cache_key)
        if isinstance(cached, dict) and cached:
            return cached

        followup = self._get_domain_config("followup_strategy", {}) or {}
        groups = (
            followup.get("signal_groups")
            or followup.get("route_signal_groups")
            or followup.get("entity_alias_groups")
            or {}
        )
        normalized_groups = self._normalize_signal_groups(groups)
        if normalized_groups:
            self._cache[cache_key] = normalized_groups
            return normalized_groups

        anchors = [
            str(anchor or "").strip()
            for anchor in (followup.get("anchor_terms", []) or [])
            if str(anchor or "").strip()
        ]
        if not anchors:
            if self._should_expect_domain_config():
                self._log_missing_schema_config_once(
                    "followup_strategy.signal_groups",
                    "Schema未配置 followup_strategy.signal_groups/anchor_terms，返回空字典",
                )
            return {}

        derived: Dict[str, List[str]] = {anchor: [anchor, self._normalize_signal_text(anchor)] for anchor in anchors}
        for item in getattr(self, "_items", []) or []:
            for label in self._extract_route_labels_from_item(item):
                anchor = self._choose_route_anchor(label, anchors)
                if not anchor:
                    continue
                derived.setdefault(anchor, []).extend([label, self._normalize_signal_text(label)])

        normalized_groups = self._normalize_signal_groups(derived)
        if normalized_groups:
            self._cache[cache_key] = normalized_groups
            return normalized_groups
        if self._should_expect_domain_config():
            self._log_missing_schema_config_once(
                "followup_strategy.derived_signal_groups",
                "Schema未配置可用线路信号组，返回空字典",
            )
        return {}

    def _get_parent_domain_terms(self) -> Dict[str, Tuple[str, ...]]:
        self._ensure_cache_state()
        cache_key = "_parent_domain_terms"
        cached = self._cache.get(cache_key)
        if isinstance(cached, dict) and cached:
            return cached
        followup = self._get_domain_config("followup_strategy", {}) or {}
        parent_terms = followup.get("parent_domain_terms", {}) or {}
        normalized: Dict[str, Tuple[str, ...]] = {}
        if isinstance(parent_terms, dict) and parent_terms:
            for parent, children in parent_terms.items():
                child_values = tuple(str(child or "").strip() for child in (children or []) if str(child or "").strip())
                if child_values:
                    normalized[str(parent)] = child_values
        if normalized:
            self._cache[cache_key] = normalized
            return normalized

        groups = self._get_domain_signal_groups()
        derived: Dict[str, List[str]] = {}
        route_terms = (((followup.get("known_slot_terms") or {}).get("route_terms")) or [])
        for parent_term in route_terms:
            parent = str(parent_term or "").strip()
            if not parent:
                continue
            matches = [route_name for route_name in groups if parent != route_name and parent in route_name]
            if matches:
                derived[parent] = matches
        normalized = {key: tuple(value) for key, value in derived.items() if value}
        if normalized:
            self._cache[cache_key] = normalized
        return normalized

    def _extract_query_domain_targets(self, query: str) -> set[str]:
        text = query or ""
        matched: set[str] = set()
        for route_name, signals in self._get_domain_signal_groups().items():
            if any(signal in text for signal in signals):
                matched.add(route_name)
        parent_domain_terms = self._get_parent_domain_terms()
        if parent_domain_terms:
            for parent_term, child_routes in parent_domain_terms.items():
                if parent_term in text and not matched.intersection(child_routes):
                    matched.update(child_routes)
        return matched

    def _extract_query_route_targets(self, query: str) -> set[str]:
        return self._extract_query_domain_targets(query)

    def _extract_item_domain_targets(self, item: UnifiedKnowledgeItem) -> set[str]:
        haystack = " ".join(
            [
                item.question or "",
                item.answer or "",
                " ".join(item.aliases or []),
                " ".join(item.tags or []),
                " ".join(item.keywords or []),
            ]
        )
        matched: set[str] = set()
        for route_name, signals in self._get_domain_signal_groups().items():
            if any(signal in haystack for signal in signals):
                matched.add(route_name)
        return matched

    def _find_exact_query_item(
        self,
        query: str,
        source: str = "all",
        category: str = None,
        enterprise_id: str = "",
        retrieval_plan: Optional[RetrievalPlan] = None,
    ) -> Optional[UnifiedKnowledgeItem]:
        normalized_query = self._normalize_search_text(query)
        if not normalized_query:
            return None

        policy = self._get_retrieval_policy(retrieval_plan)
        exact_match_policy = self._policy_get(policy, "scoring", "exact_match", default={}) or {}
        active_query_types = self._detect_query_policy_types(query, retrieval_plan)
        best_exact_match: Optional[Tuple[float, UnifiedKnowledgeItem]] = None
        best_fuzzy_match: Optional[Tuple[int, UnifiedKnowledgeItem]] = None
        fuzzy_enabled_query_types = {
            str(item or "").strip()
            for item in list(exact_match_policy.get("fuzzy_enabled_query_types") or [])
            if str(item or "").strip()
        }
        prefer_fuzzy_alias = bool(active_query_types & fuzzy_enabled_query_types)
        fuzzy_terms: List[str] = []
        if prefer_fuzzy_alias:
            try:
                import jieba

                generic_terms = {
                    str(term or "").strip().lower()
                    for term in list(exact_match_policy.get("fuzzy_excluded_terms") or [])
                    if str(term or "").strip()
                }
                fuzzy_terms = [
                    str(term or "").strip().lower()
                    for term in jieba.cut(query.lower())
                    if len(str(term or "").strip()) >= 2 and str(term or "").strip() not in generic_terms
                ]
            except Exception:
                fuzzy_terms = []
            for special_term in list(exact_match_policy.get("fuzzy_extra_terms") or []):
                if special_term in query and special_term not in fuzzy_terms:
                    fuzzy_terms.append(special_term)
        priority_categories = {
            str(item or "").strip()
            for item in list(exact_match_policy.get("fuzzy_priority_categories") or [])
            if str(item or "").strip()
        }
        priority_category_bonus = int(exact_match_policy.get("fuzzy_priority_category_bonus") or 0)
        alias_base_score = int(exact_match_policy.get("fuzzy_alias_base_score") or 6)
        question_base_score = int(exact_match_policy.get("fuzzy_question_base_score") or 4)
        overlap_base_score = int(exact_match_policy.get("fuzzy_overlap_base_score") or 8)
        overlap_step_score = int(exact_match_policy.get("fuzzy_overlap_step_score") or 3)
        min_overlap = max(int(exact_match_policy.get("fuzzy_min_overlap") or 2), 1)
        for item in self.get_items(source=source, category=category, enabled=True):
            if not self._item_matches_enterprise(item, enterprise_id):
                continue
            if retrieval_plan and not self._item_matches_schema(item, retrieval_plan.schema_id):
                continue
            normalized_question = self._normalize_search_text(item.question or "")
            if normalized_question == normalized_query:
                score = self._apply_query_bias(query, item, 0.0, retrieval_plan=retrieval_plan)
                if best_exact_match is None or score > best_exact_match[0]:
                    best_exact_match = (score, item)
            for alias in item.aliases or []:
                normalized_alias = self._normalize_search_text(alias)
                if normalized_alias == normalized_query:
                    score = self._apply_query_bias(query, item, 0.0, retrieval_plan=retrieval_plan)
                    if best_exact_match is None or score > best_exact_match[0]:
                        best_exact_match = (score, item)
                if prefer_fuzzy_alias and normalized_alias and (
                    normalized_query in normalized_alias or normalized_alias in normalized_query
                ):
                    category_bonus = priority_category_bonus if getattr(item, "category", "") in priority_categories else 0
                    score = alias_base_score + category_bonus
                    if best_fuzzy_match is None or score > best_fuzzy_match[0]:
                        best_fuzzy_match = (score, item)
            if prefer_fuzzy_alias and normalized_question and (
                normalized_query in normalized_question or normalized_question in normalized_query
            ):
                category_bonus = priority_category_bonus if getattr(item, "category", "") in priority_categories else 0
                score = question_base_score + category_bonus
                if best_fuzzy_match is None or score > best_fuzzy_match[0]:
                    best_fuzzy_match = (score, item)
            if prefer_fuzzy_alias and fuzzy_terms:
                haystack = " ".join([
                    item.question or "",
                    " ".join(item.aliases or []),
                    " ".join(item.keywords or []),
                ]).lower()
                overlap = sum(1 for term in fuzzy_terms if term and term in haystack)
                if overlap >= min(min_overlap, len(fuzzy_terms)):
                    category_bonus = priority_category_bonus if getattr(item, "category", "") in priority_categories else 0
                    score = overlap_base_score + overlap * overlap_step_score + category_bonus
                    if best_fuzzy_match is None or score > best_fuzzy_match[0]:
                        best_fuzzy_match = (score, item)
        if best_exact_match:
            return best_exact_match[1]
        return best_fuzzy_match[1] if best_fuzzy_match else None

    @staticmethod
    def _query_prefers_price(query: str) -> bool:
        text = query or ""
        return any(term in text for term in ("价格", "多少钱", "费用", "收费", "报价", "票价"))

    def _query_prefers_itinerary(self, query: str) -> bool:
        text = query or ""
        # 行业专用行程类查询词由 schema query_understanding.query_type_signals.itinerary 提供，
        # 不再硬编码旅游专用术语（行程/路线/景点/玩哪些地方/怎么玩/安排）
        signals = self._get_domain_config("query_understanding", {}).get("query_type_signals", {}) or {}
        terms = signals.get("itinerary", []) if isinstance(signals, dict) else []
        if not terms:
            return False
        return any(term in text for term in terms)

    def _query_prefers_inclusion(self, query: str) -> bool:
        text = query or ""
        # 行业专用包含类查询词由 schema query_understanding.query_type_signals.inclusion 提供，
        # 不再硬编码旅游专用术语（包含/包不包/门票/车费/午餐/餐饮/费用里有）
        signals = self._get_domain_config("query_understanding", {}).get("query_type_signals", {}) or {}
        terms = signals.get("inclusion", []) if isinstance(signals, dict) else []
        if not terms:
            return False
        return any(term in text for term in terms)

    @staticmethod
    def _infer_knowledge_type(item: "UnifiedKnowledgeItem") -> str:
        metadata = getattr(item, "metadata", {}) or {}
        explicit = str(metadata.get("knowledge_type") or "").strip().lower()
        if explicit:
            return explicit
        if bool(metadata.get("reservation_supported")):
            return "reservation_policy"
        if bool(metadata.get("handoff_trigger")):
            return "conversion_asset"
        category = str(getattr(item, "category", "") or "").strip().lower()
        mapping = {
            "price": "conversion_asset",
            "contact": "conversion_asset",
            "service": "objection_handling",
            "tips": "objection_handling",
            "product": "comparison",
            "itinerary": "fact",
        }
        return mapping.get(category, "fact")

    def _apply_business_stage_bias(
        self,
        query: str,
        item: "UnifiedKnowledgeItem",
        score: float,
        business_stage: str = "",
    ) -> float:
        stage = str(business_stage or "").strip().lower()
        if not stage:
            return score

        adjusted = float(score or 0.0)
        knowledge_type = self._infer_knowledge_type(item)
        metadata = getattr(item, "metadata", {}) or {}
        stage_hint = str(metadata.get("conversion_stage") or "").strip().lower()
        reservation_supported = bool(metadata.get("reservation_supported"))
        handoff_trigger = bool(metadata.get("handoff_trigger"))

        if stage in {"consideration"}:
            if knowledge_type in {"comparison", "objection_handling"}:
                adjusted += 12.0
            elif knowledge_type == "fact":
                adjusted += 3.0
        elif stage in {"high_intent"}:
            if knowledge_type in {"conversion_asset", "objection_handling"}:
                adjusted += 14.0
            elif knowledge_type == "comparison":
                adjusted += 4.0
        elif stage in {"reservation", "handoff"}:
            if knowledge_type in {"reservation_policy", "conversion_asset"}:
                adjusted += 18.0
            elif knowledge_type == "objection_handling":
                adjusted += 6.0
            if reservation_supported:
                adjusted += 10.0
            if handoff_trigger:
                adjusted += 6.0

        if stage_hint and stage_hint == stage:
            adjusted += 6.0
        if stage in {"reservation", "handoff"} and any(term in (query or "") for term in ("联系", "资料", "预留", "预约", "下单")):
            if knowledge_type in {"conversion_asset", "reservation_policy"}:
                adjusted += 6.0
        if stage in {"high_intent", "handoff"} and any(term in (query or "") for term in ("发我", "资料", "联系方式", "怎么联系")):
            if handoff_trigger:
                adjusted += 8.0
        if self._is_contact_polluted_item(item) and not self._is_explicit_contact_query(query):
            adjusted -= 18.0
        return adjusted

    @staticmethod
    def _is_comparison_query(query: str) -> bool:
        text = query or ""
        compare_terms = ("哪个好", "怎么选", "区别", "对比", "更适合", "更值得", "选哪条", "哪条更稳")
        return any(term in text for term in compare_terms)

    @staticmethod
    def _is_selection_query(query: str) -> bool:
        text = query or ""
        selection_terms = (
            "推荐", "第一次来", "预算", "不想太累", "带老人", "带小孩", "情侣",
            "选哪个", "选哪条", "哪个好", "两个人", "我们两个", "腿脚一般", "更稳",
        )
        return any(term in text for term in selection_terms)

    def _postprocess_customer_domain_results(
        self,
        query: str,
        results: List[Tuple[UnifiedKnowledgeItem, float]],
        business_stage: str = "",
    ) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        if not results:
            return results
        if business_stage:
            stage_adjusted = [
                (item, self._apply_business_stage_bias(query, item, score, business_stage))
                for item, score in results
            ]
            stage_adjusted.sort(key=lambda pair: pair[1], reverse=True)
            results = stage_adjusted
        if self._is_sales_script_query(query) or not self._is_domain_specific_query(query):
            return results

        top_item = results[0][0]
        compare_or_selection = self._is_comparison_query(query) or self._is_selection_query(query)
        should_reorder = self._is_sales_script_item(top_item) or (
            compare_or_selection and getattr(top_item, "category", "") == "service"
        )
        if not should_reorder:
            has_script_below = any(
                self._is_sales_script_item(item)
                for item, _ in results[1:4]
            )
            if not has_script_below:
                return results

        preferred = None
        for item, score in results:
            if self._is_sales_script_item(item):
                continue
            if not self._is_domain_specific_item(item):
                continue
            if getattr(item, "category", "") == "service":
                continue
            preferred = (item, score)
            break

        if not preferred:
            return results

        preferred_id = getattr(preferred[0], "id", None)
        reordered = [preferred]
        reordered.extend(
            (item, score)
            for item, score in results
            if getattr(item, "id", None) != preferred_id
        )
        return reordered

    def _postprocess_customer_route_results(
        self,
        query: str,
        results: List[Tuple[UnifiedKnowledgeItem, float]],
        business_stage: str = "",
    ) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        return self._postprocess_customer_domain_results(
            query,
            results,
            business_stage=business_stage,
        )

    def _infer_bundle_type(self, query: str, routes: List[str]) -> str:
        if self._is_comparison_query(query) or len(routes) >= 2:
            return "comparison"
        if self._is_selection_query(query):
            return "selection"
        return "route"

    def _score_item_for_bundle(self, query: str, item: UnifiedKnowledgeItem, route_name: str = "") -> float:
        score = 0.0
        query_routes = self._extract_query_domain_targets(query)
        item_routes = self._extract_item_domain_targets(item)
        if route_name and route_name in self._extract_item_domain_targets(item):
            score += 30.0
        if query_routes and item_routes & query_routes:
            score += 24.0
        score = self._apply_query_bias(query, item, score)
        if item.category == "product":
            score += 6.0
        if self._query_prefers_price(query) and item.category == "price":
            score += 25.0
        if self._query_prefers_price(query) and item.category == "itinerary":
            score += 12.0
        if self._query_prefers_itinerary(query) and item.category == "itinerary":
            score += 25.0
        if self._query_prefers_itinerary(query) and item.category == "price":
            score += 8.0
        if self._query_prefers_inclusion(query) and item.category in {"price", "service", "tips"}:
            score += 18.0
        if self._is_comparison_query(query) and any(term in (item.question or "") for term in ("区别", "怎么选", "哪个好", "值得去")):
            score += 20.0
        if self._is_selection_query(query) and any(term in (item.answer or "") for term in ("适合", "推荐", "第一次来", "带老人", "情侣", "预算")):
            score += 12.0
        return score

    def _pick_bundle_overview_items(
        self,
        query: str,
        search_results: List[Tuple[UnifiedKnowledgeItem, float]],
    ) -> List[UnifiedKnowledgeItem]:
        ranked: List[Tuple[float, UnifiedKnowledgeItem]] = []
        for item, base_score in search_results[:5]:
            score = self._score_item_for_bundle(query, item) + float(base_score or 0.0)
            ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        seen = set()
        picked: List[UnifiedKnowledgeItem] = []
        for _score, item in ranked:
            if item.id in seen:
                continue
            seen.add(item.id)
            picked.append(item)
            if len(picked) >= 2:
                break
        return picked

    def _pick_route_evidence_for_bundle(
        self,
        query: str,
        route_name: str,
        items: List[UnifiedKnowledgeItem],
    ) -> Dict[str, UnifiedKnowledgeItem]:
        relevant_items = [
            item for item in items
            if route_name in self._extract_item_domain_targets(item)
        ]
        if not relevant_items:
            return {}

        best_by_category: Dict[str, Tuple[float, UnifiedKnowledgeItem]] = {}
        prefers_price = self._query_prefers_price(query)
        prefers_itinerary = self._query_prefers_itinerary(query)
        for item in relevant_items:
            category = item.category or "other"
            score = self._score_item_for_bundle(query, item, route_name=route_name)
            route_targets = self._extract_item_domain_targets(item)
            question_text = item.question or ""
            if route_targets == {route_name}:
                score += 120.0
            elif len(route_targets) > 1:
                score -= 60.0
                if category in self._get_schema_core_categories():
                    score -= 80.0
            if category in self._get_schema_core_categories():
                if route_name in question_text:
                    score += 90.0
                else:
                    score -= 40.0
            if prefers_price and category == "itinerary":
                score += 14.0
            if prefers_itinerary and category == "price":
                score += 10.0
            current = best_by_category.get(category)
            if current is None or score > current[0]:
                best_by_category[category] = (score, item)

        # 优先类别顺序由 schema category_profiles 键决定，通用类别兜底
        preferred_order = list(
            sorted(self._get_schema_core_categories(), key=lambda c: {"product": 0, "price": 1, "service": 2, "policy": 3, "faq": 4}.get(c, 9))
        )
        # 追加 best_by_category 中未在 preferred_order 的类别，避免遗漏行业专用类别
        for extra_category in best_by_category.keys():
            if extra_category not in preferred_order:
                preferred_order.append(extra_category)
        ordered: Dict[str, UnifiedKnowledgeItem] = {}
        for category in preferred_order:
            if category in best_by_category:
                ordered[category] = best_by_category[category][1]
        return ordered

    @staticmethod
    def _dedupe_preserve_order(values: List[str]) -> List[str]:
        seen = set()
        ordered: List[str] = []
        for value in values or []:
            text = _normalize_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        return ordered

    @staticmethod
    def _extract_money_text(text: str) -> str:
        if not text:
            return ""
        match = re.search(r"(\d+\s*元/人)", text)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_price_amount(value: str) -> Optional[int]:
        if not value:
            return None
        match = re.search(r"(\d+)", str(value))
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _build_route_fact_sheet(
        self,
        route_name: str,
        route_evidence: Dict[str, UnifiedKnowledgeItem],
    ) -> RouteFactSheet:
        sheet = RouteFactSheet(route_name=route_name, canonical_name=route_name)
        for category, item in (route_evidence or {}).items():
            if not item:
                continue
            answer = _normalize_answer_text(item.answer)
            question = _normalize_text(item.question)
            sheet.source_questions[category] = question

            if item.question and category in {"product", "price", "itinerary"}:
                canonical_match = re.split(r"[（(]", item.question, maxsplit=1)[0].strip()
                if canonical_match and not any(term in canonical_match for term in ("怎么选", "哪个好", "区别", "犹豫", "比价")):
                    sheet.canonical_name = canonical_match

            if category == "price":
                if "返20" in answer:
                    sheet.price = sheet.price or "返20元"
                elif "返40" in answer:
                    sheet.price = sheet.price or "返40元"
                else:
                    sheet.price = self._extract_money_text(answer) or sheet.price
                if "返20" in answer:
                    sheet.budget_level = sheet.budget_level or "返20"
                elif "返40" in answer:
                    sheet.budget_level = sheet.budget_level or "返40"
                if "不安排特产超市" in answer or "不进购物店" in answer:
                    sheet.shopping = sheet.shopping or "纯玩无购物"
                elif "特产超市停留" in answer:
                    sheet.shopping = sheet.shopping or "含特产超市停留"
                if "介意购物店" in answer:
                    sheet.suitable_for.append("介意购物店")
                if "第一次来" in answer:
                    sheet.suitable_for.append("第一次来重庆")

                include_hits = re.findall(r"含([^；。]+)", answer)
                exclude_hits = re.findall(r"(?:不含|自理)([^；。]+)", answer)
                sheet.includes.extend(include_hits)
                sheet.excludes.extend(exclude_hits)

            if category == "product":
                if any(term in answer for term in ("带老人", "老人小孩", "第一次来", "不想进购物店", "省心")):
                    if "带老人" in answer or "老人小孩" in answer:
                        sheet.suitable_for.append("带老人小孩")
                    if "第一次来" in answer:
                        sheet.suitable_for.append("第一次来重庆")
                    if "不想进购物店" in answer:
                        sheet.suitable_for.append("介意购物店")
                    if "省心" in answer:
                        sheet.suitable_for.append("想省心轻松")
                if any(term in answer for term in ("体力要求会稍高", "更满一点", "不想太累")):
                    if "体力要求会稍高" in answer:
                        sheet.not_suitable_for.append("不适合怕累人群")
                    if "不想太累" in answer:
                        sheet.not_suitable_for.append("不适合想轻松行程")
                if "轻松" in answer or "舒展" in answer:
                    sheet.pace = sheet.pace or "轻松"
                elif "更满" in answer or "体力要求会稍高" in answer:
                    sheet.pace = sheet.pace or "偏满偏累"

            if category == "itinerary":
                scenic_hits = [scenic for scenic in self._get_domain_scenic_spots() if scenic in answer]
                if scenic_hits:
                    sheet.highlights.extend(scenic_hits)
                else:
                    sheet.highlights.extend(self._extract_itinerary_highlights(answer))
                if "特产超市停留" in answer:
                    sheet.shopping = sheet.shopping or "含特产超市停留"
                elif "不安排特产超市停留" in answer:
                    sheet.shopping = sheet.shopping or "纯玩无购物"
                if "更纯粹" in answer:
                    sheet.pace = sheet.pace or "纯玩省心"
                elif "更满一些" in answer:
                    sheet.pace = sheet.pace or "偏满偏累"

            if category == "tips":
                if "穿舒服的鞋" in answer or "穿舒适防滑的鞋" in answer:
                    sheet.not_suitable_for.append("不适合想少走路人群")
                if "不建议再硬赶高铁或飞机" in answer:
                    sheet.not_suitable_for.append("不适合当晚赶飞机高铁")

        if not sheet.budget_level and sheet.price:
            money = re.search(r"(\d+)", sheet.price)
            if money:
                amount = int(money.group(1))
                if amount <= 280:
                    sheet.budget_level = "预算友好"
                elif amount >= 300:
                    sheet.budget_level = "预算略高"

        if not sheet.shopping:
            route_defaults = self._get_domain_config("domain_route_defaults", {})
            if route_name in route_defaults:
                sheet.shopping = route_defaults[route_name]

        sheet.suitable_for = self._dedupe_preserve_order(sheet.suitable_for)
        sheet.not_suitable_for = self._dedupe_preserve_order(sheet.not_suitable_for)
        sheet.highlights = self._dedupe_preserve_order(sheet.highlights)
        sheet.includes = self._dedupe_preserve_order(sheet.includes)
        sheet.excludes = self._dedupe_preserve_order(sheet.excludes)
        return sheet

    @staticmethod
    def _extract_itinerary_highlights(answer: str) -> List[str]:
        text = _normalize_answer_text(answer)
        if not text:
            return []

        matches: List[str] = []
        for pattern in (
            r"(?:覆盖|包含|经过|游玩|主要看|打卡)([^。；\n]+)",
            r"([^。；\n]+?)(?:这些代表点位|等代表点位)",
        ):
            for chunk in re.findall(pattern, text):
                matches.extend(
                    part.strip()
                    for part in re.split(r"[、,+/＋]", str(chunk or ""))
                    if part and len(part.strip()) >= 2
                )

        if not matches:
            matches.extend(
                part.strip()
                for part in re.split(r"[、,+/＋]", text)
                if part and len(part.strip()) >= 2
            )

        cleaned: List[str] = []
        for item in matches:
            candidate = re.sub(r"^(当天主要覆盖|当天覆盖|主要覆盖|包括|包含|游玩|打卡|经过)", "", item)
            candidate = re.sub(r"(这些代表点位|等代表点位)$", "", candidate).strip(" ：:，,。；;")
            if (
                not candidate
                or len(candidate) > 16
                or any(term in candidate for term in ("特产超市", "停留", "购物店", "这些代表点位", "代表点位", "整体节奏"))
            ):
                continue
            cleaned.append(candidate)
        return cleaned[:6]

    @staticmethod
    def _comparison_focuses(query: str) -> List[str]:
        text = query or ""
        focuses: List[str] = []
        rules = [
            ("elder", ("老人", "长辈", "腿脚", "小孩", "孩子")),
            ("budget", ("预算", "便宜", "划算", "性价比", "贵", "多少钱", "价格")),
            ("first_time", ("第一次来", "第一次去", "首次", "初次")),
            ("light_pace", ("不想太累", "轻松", "省心", "稳", "体力", "赶")),
            ("shopping", ("购物", "纯玩", "进店", "超市停留")),
        ]
        for key, terms in rules:
            if any(term in text for term in terms):
                focuses.append(key)
        return focuses

    @staticmethod
    def _map_schema_summary_field_to_fact_key(field_name: str) -> str:
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

    @staticmethod
    def _format_schema_summary_fact_value(value: Any) -> str:
        if isinstance(value, list):
            items = [str(item or "").strip() for item in value if str(item or "").strip()]
            return "、".join(items[:3])
        return str(value or "").strip()

    def _build_schema_configured_comparison_summary_lines(
        self,
        routes: List[str],
        route_facts: Dict[str, Dict[str, Any]],
        comparison_fields: List[str],
        field_labels: Dict[str, str],
    ) -> List[str]:
        selected_routes = [route for route in routes if route in route_facts]
        if len(selected_routes) < 2:
            return []

        route_a, route_b = selected_routes[:2]
        facts_a = dict(route_facts.get(route_a) or {})
        facts_b = dict(route_facts.get(route_b) or {})
        lines: List[str] = []
        for field_name in [str(name or "").strip() for name in list(comparison_fields or []) if str(name or "").strip()]:
            fact_key = self._map_schema_summary_field_to_fact_key(field_name)
            if not fact_key:
                continue
            value_a = self._format_schema_summary_fact_value(facts_a.get(fact_key))
            value_b = self._format_schema_summary_fact_value(facts_b.get(fact_key))
            if not value_a or not value_b or value_a == value_b:
                continue
            label = str(field_labels.get(field_name) or field_name).strip()
            lines.append(f"{label}上，{route_a}是{value_a}；{route_b}是{value_b}。")
        return lines

    def _build_route_comparison_summary(
        self,
        query: str,
        routes: List[str],
        route_facts: Dict[str, Dict[str, Any]],
        grounded_config: Optional[Dict[str, Any]] = None,
        field_labels: Optional[Dict[str, str]] = None,
    ) -> str:
        if not route_facts:
            return ""

        selected_routes = [route for route in routes if route in route_facts]
        if len(selected_routes) < 1:
            return ""

        focuses = self._comparison_focuses(query)
        lines: List[str] = []
        lines.extend(
            self._build_schema_configured_comparison_summary_lines(
                routes=selected_routes,
                route_facts=route_facts,
                comparison_fields=list((grounded_config or {}).get("comparison_fields") or []),
                field_labels=dict(field_labels or {}),
            )
        )

        if len(selected_routes) >= 2 and ("budget" in focuses):
            priced_routes = []
            for route in selected_routes:
                amount = self._extract_price_amount(route_facts.get(route, {}).get("price", ""))
                if amount is not None:
                    priced_routes.append((amount, route))
            priced_routes.sort()
            if len(priced_routes) >= 2:
                low_amount, low_route = priced_routes[0]
                high_amount, high_route = priced_routes[-1]
                lines.append(
                    f"预算更敏感时优先看{low_route}，当前已知价格约{low_amount}元/人；{high_route}约{high_amount}元/人。"
                )

        if len(selected_routes) >= 2 and ("elder" in focuses or "light_pace" in focuses):
            elder_friendly = None
            for route in selected_routes:
                facts = route_facts.get(route, {})
                suitable = " ".join(facts.get("suitable_for") or [])
                pace = str(facts.get("pace") or "")
                if any(term in suitable for term in ("老人", "小孩")) or any(term in pace for term in ("轻松", "省心")):
                    elder_friendly = route
                    break
            if elder_friendly:
                lines.append(f"带老人、带小孩或想走得轻松一点时，更稳的优先选择是{elder_friendly}。")

        if len(selected_routes) >= 2 and ("shopping" in focuses):
            no_shopping = None
            with_shopping = None
            for route in selected_routes:
                shopping = str(route_facts.get(route, {}).get("shopping") or "")
                if "无购物" in shopping or "纯玩" in shopping:
                    no_shopping = route
                if "超市停留" in shopping:
                    with_shopping = route
            if no_shopping:
                summary = f"介意购物停留时优先看{no_shopping}"
                if with_shopping:
                    summary += f"，{with_shopping}会有特产超市停留"
                summary += "。"
                lines.append(summary)

        if len(selected_routes) >= 2 and ("first_time" in focuses):
            first_time_routes = [
                route
                for route in selected_routes
                if "第一次来重庆" in " ".join(route_facts.get(route, {}).get("suitable_for") or [])
            ]
            if first_time_routes:
                lines.append(f"第一次来时可优先从{first_time_routes[0]}看起，更贴近首访客常见需求。")

        if not lines and len(selected_routes) >= 2:
            route_a, route_b = selected_routes[:2]
            facts_a = route_facts.get(route_a, {})
            facts_b = route_facts.get(route_b, {})
            desc_a = "、".join(
                [value for value in [facts_a.get("price"), facts_a.get("pace"), facts_a.get("shopping")] if value]
            ) or "信息待补充"
            desc_b = "、".join(
                [value for value in [facts_b.get("price"), facts_b.get("pace"), facts_b.get("shopping")] if value]
            ) or "信息待补充"
            lines.append(f"{route_a}的已知特点是{desc_a}。")
            lines.append(f"{route_b}的已知特点是{desc_b}。")

        if selected_routes:
            for route in selected_routes[:2]:
                facts = route_facts.get(route, {})
                highlights = "、".join((facts.get("highlights") or [])[:3])
                suitable = "、".join((facts.get("suitable_for") or [])[:2])
                parts = []
                if highlights:
                    parts.append(f"亮点是{highlights}")
                if suitable:
                    parts.append(f"更偏向{ suitable }")
                if parts:
                    lines.append(f"{route}{'，'.join(parts)}。")

        deduped = self._dedupe_preserve_order(lines)
        return "\n".join(deduped[:4]).strip()

    def search_evidence_bundle(
        self,
        query: str,
        top_k: int = 5,
        source: str = "all",
        business_stage: str = "",
        intent: str = "",
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Optional[StructuredEvidenceBundle]:
        """按线路/对比主题聚合结构化证据包，供主回复链和 LLM 使用。"""
        query = (query or "").strip()
        if not query:
            return None
        resolved_enterprise_id = str(enterprise_id or "").strip()
        active_schema = self._get_active_schema(
            enterprise_id=resolved_enterprise_id,
            schema_id=schema_id,
        ) or {}
        effective_schema_id = str(schema_id or active_schema.get("schema_id") or "").strip()
        schema_context_token = _ACTIVE_SCHEMA_CONTEXT.set(
            {
                "enterprise_id": resolved_enterprise_id,
                "schema_id": effective_schema_id,
            }
        )
        try:
            if not self._should_attempt_domain_evidence_bundle(query):
                return None

            grounded_config = dict(active_schema.get("grounded_config") or {})
            field_labels = {
                str((field or {}).get("name") or "").strip(): str((field or {}).get("label") or "").strip()
                for field in list(active_schema.get("fields") or [])
                if isinstance(field, dict) and str((field or {}).get("name") or "").strip()
            }

            search_kwargs = {
                "query": query,
                "top_k": max(top_k, 5),
                "source": source,
                "use_vector": True,
            }
            if business_stage:
                search_kwargs["business_stage"] = business_stage
            if intent:
                search_kwargs["intent"] = intent
            if resolved_enterprise_id:
                search_kwargs["enterprise_id"] = resolved_enterprise_id
            # 修复：传递 schema_id 避免跨 schema 知识扫描
            if effective_schema_id:
                search_kwargs["schema_id"] = effective_schema_id
            search_results = self._call_search_with_compat(**search_kwargs)
            if not search_results:
                return None

            query_routes = self._extract_query_domain_targets(query)
            result_routes: List[str] = []
            for item, _score in search_results[:5]:
                for route_name in self._extract_item_domain_targets(item):
                    if route_name not in result_routes:
                        result_routes.append(route_name)

            routes = list(query_routes) if query_routes else result_routes
            if not routes and self._is_selection_query(query):
                routes = list(self._get_domain_signal_groups().keys())
            routes = routes[:4]
            if not routes:
                return None

            bundle_type = self._infer_bundle_type(query, routes)
            all_items = self.get_all_items()
            route_evidence = {
                route_name: self._pick_route_evidence_for_bundle(query, route_name, all_items)
                for route_name in routes
            }
            route_evidence = {
                route_name: evidence
                for route_name, evidence in route_evidence.items()
                if evidence
            }
            if not route_evidence:
                return None

            route_facts = {
                route_name: self._build_route_fact_sheet(route_name, evidence).to_prompt_dict()
                for route_name, evidence in route_evidence.items()
            }
            comparison_summary = self._build_route_comparison_summary(
                query=query,
                routes=list(route_evidence.keys()),
                route_facts=route_facts,
                grounded_config=grounded_config,
                field_labels=field_labels,
            )

            return StructuredEvidenceBundle(
                query=query,
                bundle_type=bundle_type,
                grounded_config=grounded_config,
                field_labels=field_labels,
                routes=list(route_evidence.keys()),
                overview_items=self._pick_bundle_overview_items(query, search_results),
                route_evidence=route_evidence,
                route_facts=route_facts,
                comparison_summary=comparison_summary,
            )
        finally:
            _ACTIVE_SCHEMA_CONTEXT.reset(schema_context_token)

    def _should_attempt_domain_evidence_bundle(self, query: str) -> bool:
        text = (query or "").strip()
        if not text or self._is_sales_script_query(text):
            return False

        if self._extract_query_domain_targets(text):
            return True

        explicit_scope_terms = self._get_domain_query_terms()
        if not explicit_scope_terms or not any(term in text for term in explicit_scope_terms):
            return False

        return (
            self._is_comparison_query(text)
            or self._is_selection_query(text)
            or self._query_prefers_price(text)
            or self._query_prefers_itinerary(text)
            or self._query_prefers_inclusion(text)
        )

    def _call_search_with_compat(self, **kwargs):
        attempts = [
            dict(kwargs),
            {key: value for key, value in kwargs.items() if key != "intent"},
            {key: value for key, value in kwargs.items() if key not in {"intent", "business_stage"}},
        ]
        last_error: Optional[TypeError] = None
        for payload in attempts:
            try:
                return self.search(**payload)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        return self.search(**kwargs)

    def _ensure_retrieval_policy_service(self) -> RetrievalPolicyService:
        service = getattr(self, "_retrieval_policy_service", None)
        if service is None:
            service = RetrievalPolicyService()
            self._retrieval_policy_service = service
        return service

    def _get_retrieval_policy(self, retrieval_plan: Optional[RetrievalPlan] = None) -> Dict[str, Any]:
        policy = getattr(retrieval_plan, "policy", None) if retrieval_plan else None
        if isinstance(policy, dict) and policy:
            return policy
        service = self._ensure_retrieval_policy_service()
        get_active_policy = getattr(service, "get_active_policy", None)
        if callable(get_active_policy):
            try:
                resolved_policy = get_active_policy()
                if isinstance(resolved_policy, dict) and resolved_policy:
                    return resolved_policy
            except Exception:
                pass
        try:
            return copy.deepcopy(DEFAULT_RETRIEVAL_POLICY)
        except Exception:
            return {}

    @staticmethod
    def _policy_get(mapping: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
        current: Any = mapping or {}
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key)
        return default if current is None else current

    def _detect_query_policy_types(
        self,
        query: str,
        retrieval_plan: Optional[RetrievalPlan] = None,
    ) -> set[str]:
        text = str(query or "").strip().lower()
        detected: set[str] = set()
        policy = self._get_retrieval_policy(retrieval_plan)
        configured_terms = self._policy_get(policy, "query_type_terms", default={}) or {}
        for query_type, terms in configured_terms.items():
            normalized_terms = [str(term or "").strip().lower() for term in list(terms or []) if str(term or "").strip()]
            if normalized_terms and any(term in text for term in normalized_terms):
                detected.add(str(query_type or "").strip())
        if detected:
            return detected
        if self._is_comparison_query(query):
            detected.add("comparison")
        if self._is_selection_query(query):
            detected.add("selection")
        if self._query_prefers_price(query):
            detected.add("price")
        if self._query_prefers_itinerary(query):
            detected.add("itinerary")
        if self._query_prefers_inclusion(query):
            detected.add("inclusion")
        if self._is_explicit_contact_query(query):
            detected.add("contact")
        if self._is_domain_specific_query(query):
            detected.add("domain_specific")
        return detected

    def _item_matches_enterprise(self, item: Optional[UnifiedKnowledgeItem], enterprise_id: Optional[str]) -> bool:
        if not item:
            return False
        normalized_requested = self._normalize_enterprise_id(enterprise_id)
        normalized_item = self._normalize_enterprise_id(getattr(item, "enterprise_id", ""))
        return normalized_item == normalized_requested

    @staticmethod
    def _item_matches_schema(item: Optional[UnifiedKnowledgeItem], schema_id: Optional[str]) -> bool:
        if not item:
            return False
        normalized_requested = str(schema_id or "").strip()
        if not normalized_requested:
            return True
        normalized_item = str(getattr(item, "schema_id", "") or "").strip()
        return normalized_item == normalized_requested

    def _apply_query_bias(
        self,
        query: str,
        item: UnifiedKnowledgeItem,
        score: float,
        retrieval_plan: Optional[RetrievalPlan] = None,
    ) -> float:
        adjusted = float(score or 0.0)
        policy = self._get_retrieval_policy(retrieval_plan)
        scoring_policy = self._policy_get(policy, "scoring", default={}) or {}
        priority_factor = float(self._policy_get(scoring_policy, "priority_factor", default=0.5) or 0.5)
        normalized_query = self._normalize_search_text(query)
        active_query_types = self._detect_query_policy_types(query, retrieval_plan)
        query_prefers_price = "price" in active_query_types
        query_prefers_itinerary = "itinerary" in active_query_types
        is_route_query = "domain_specific" in active_query_types
        is_compare_or_selection = bool({"comparison", "selection"} & active_query_types)
        explicit_contact_query = "contact" in active_query_types
        item_source = str(getattr(item, "source", "") or "").strip().lower()
        source_weights = self._policy_get(scoring_policy, "source_weights", default={}) or {}
        adjusted += float(source_weights.get(item_source, 0.0) or 0.0)
        source_priority_multipliers = self._policy_get(
            scoring_policy,
            "source_priority_multipliers",
            default={},
        ) or {}
        priority_multiplier = float(source_priority_multipliers.get(item_source, 0.0) or 0.0)
        if priority_multiplier:
            adjusted += float(getattr(item, "priority", 0) or 0) * priority_multiplier * priority_factor
        query_type_source_weights = self._policy_get(scoring_policy, "query_type_source_weights", default={}) or {}
        for query_type in active_query_types:
            adjusted += float(
                self._policy_get(query_type_source_weights, query_type, item_source, default=0.0) or 0.0
            )
        if self._is_contact_polluted_item(item):
            contact_pollution_weights = self._policy_get(scoring_policy, "contact_pollution_weights", default={}) or {}
            if explicit_contact_query:
                adjusted += float(contact_pollution_weights.get("explicit_contact", 0.0) or 0.0)
            else:
                adjusted += float(contact_pollution_weights.get("default", 0.0) or 0.0)
                if query_prefers_price or query_prefers_itinerary or is_route_query:
                    adjusted += float(contact_pollution_weights.get("fact_query_extra", 0.0) or 0.0)
        query_routes = self._extract_query_domain_targets(query)
        item_routes = self._extract_item_domain_targets(item)
        route_weights = self._policy_get(scoring_policy, "domain_route_weights", default={}) or {}
        if query_routes:
            if item_routes & query_routes:
                adjusted += float(route_weights.get("route_match", 0.0) or 0.0)
                if query_prefers_price and item.category == "price":
                    adjusted += float(route_weights.get("route_match_price_category", 0.0) or 0.0)
                if (query_prefers_price or query_prefers_itinerary) and item.category == "itinerary":
                    adjusted += float(route_weights.get("route_match_itinerary_category", 0.0) or 0.0)
            elif item_routes:
                adjusted += float(route_weights.get("route_mismatch", 0.0) or 0.0)
                if item.category in self._get_schema_boost_categories():
                    adjusted += float(route_weights.get("route_mismatch_core_category", 0.0) or 0.0)
        if is_route_query and not self._is_sales_script_query(query):
            if self._is_sales_script_item(item):
                adjusted += float(route_weights.get("sales_script_penalty", 0.0) or 0.0)
            elif self._is_domain_specific_item(item):
                adjusted += float(route_weights.get("domain_specific_item", 0.0) or 0.0)
                if item.category in self._get_schema_boost_categories():
                    adjusted += float(route_weights.get("domain_specific_core_category", 0.0) or 0.0)
            if item.category == "service":
                adjusted += float(route_weights.get("service_penalty", 0.0) or 0.0)

            if is_compare_or_selection:
                if self._is_sales_script_item(item):
                    adjusted += float(route_weights.get("comparison_sales_script_penalty", 0.0) or 0.0)
                if item.category == "service":
                    adjusted += float(route_weights.get("comparison_service_penalty", 0.0) or 0.0)
                elif item.category in self._get_schema_boost_categories():
                    adjusted += float(route_weights.get("comparison_core_category", 0.0) or 0.0)

            if query_routes:
                if item_routes & query_routes:
                    adjusted += float(route_weights.get("query_route_extra_match", 0.0) or 0.0)
                elif item_routes:
                    adjusted += float(route_weights.get("query_route_extra_mismatch", 0.0) or 0.0)

            if query_prefers_price:
                if item.category == "price" or "价格" in (item.question or ""):
                    adjusted += float(route_weights.get("price_category", 0.0) or 0.0)
                elif item.category == "itinerary":
                    adjusted += float(route_weights.get("price_itinerary_penalty", 0.0) or 0.0)

            if query_prefers_itinerary:
                if item.category == "itinerary" or "行程" in (item.question or ""):
                    adjusted += float(route_weights.get("itinerary_category", 0.0) or 0.0)
                elif item.category == "price":
                    adjusted += float(route_weights.get("itinerary_price_penalty", 0.0) or 0.0)
        elif query_prefers_price:
            if item.category == "price" or "价格" in (item.question or ""):
                adjusted += float(
                    self._policy_get(scoring_policy, "query_category_weights", "price", "price", default=0.0) or 0.0
                )
            elif self._is_sales_script_item(item):
                adjusted += float(
                    self._policy_get(scoring_policy, "query_type_source_weights", "price", item_source, default=0.0) or 0.0
                )
        elif query_prefers_itinerary:
            if item.category == "itinerary" or "行程" in (item.question or ""):
                adjusted += float(
                    self._policy_get(scoring_policy, "query_category_weights", "itinerary", "itinerary", default=0.0) or 0.0
                )
            elif self._is_sales_script_item(item):
                adjusted += float(
                    self._policy_get(scoring_policy, "query_category_weights", "itinerary", "sales_script", default=0.0) or 0.0
                )
        if normalized_query and is_compare_or_selection:
            normalized_question = self._normalize_search_text(getattr(item, "question", ""))
            normalized_aliases = {
                self._normalize_search_text(alias)
                for alias in list(getattr(item, "aliases", []) or [])
                if self._normalize_search_text(alias)
            }
            if normalized_question == normalized_query or normalized_query in normalized_aliases:
                adjusted += float(
                    self._policy_get(scoring_policy, "exact_match", "comparison_alias_exact", default=68.0) or 0.0
                )
            elif (
                (normalized_question and (normalized_query in normalized_question or normalized_question in normalized_query))
                or any(
                    normalized_alias and (
                        normalized_query in normalized_alias or normalized_alias in normalized_query
                    )
                    for normalized_alias in normalized_aliases
                )
            ):
                adjusted += float(
                    self._policy_get(scoring_policy, "exact_match", "comparison_alias_fuzzy", default=28.0) or 0.0
                )
        if retrieval_plan:
            if retrieval_plan.enterprise_id and retrieval_plan.enterprise_id == getattr(item, "enterprise_id", ""):
                adjusted += float(scoring_policy.get("enterprise_match_boost", 6.0) or 0.0)
            if retrieval_plan.target_categories:
                category_boost = retrieval_plan.category_boosts.get(item.category, 0.0)
                if category_boost:
                    adjusted += category_boost
                elif item.category and item.category not in retrieval_plan.target_categories:
                    adjusted += float(scoring_policy.get("non_target_category_penalty", -3.0) or 0.0)
            if retrieval_plan.preferred_terms:
                haystack = " ".join([
                    str(getattr(item, "question", "") or ""),
                    str(getattr(item, "answer", "") or ""),
                    " ".join(list(getattr(item, "keywords", []) or [])),
                    " ".join(list(getattr(item, "tags", []) or [])),
                    str(getattr(item, "topic", "") or ""),
                ])
                for term in retrieval_plan.preferred_terms[:6]:
                    if term and term.lower() in haystack.lower():
                        adjusted += 3.0
        return adjusted

    def _merge_search_results(
        self,
        query: str,
        rag_results: List[Tuple[UnifiedKnowledgeItem, float]],
        basic_results: List[Tuple[UnifiedKnowledgeItem, float]],
        top_k: int,
        business_stage: str = "",
        retrieval_plan: Optional[RetrievalPlan] = None,
    ) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        """合并 RAG 与文本检索结果，确保新入库文本也能稳定命中。"""
        merged: Dict[str, Dict[str, Any]] = {}

        def _record(item: UnifiedKnowledgeItem, score: float, source_name: str) -> None:
            if not item or not item.enabled:
                return
            adjusted_score = self._apply_query_bias(query, item, score, retrieval_plan=retrieval_plan)
            adjusted_score = self._apply_business_stage_bias(query, item, adjusted_score, business_stage)
            entry = merged.setdefault(
                item.id,
                {
                    "item": item,
                    "score": 0.0,
                    "sources": set(),
                    "source_scores": {},
                },
            )
            entry["source_scores"][source_name] = adjusted_score
            entry["sources"].add(source_name)

        for item, score in rag_results or []:
            _record(item, score, "rag")
        for item, score in basic_results or []:
            _record(item, score, "basic")

        final_results: List[Tuple[UnifiedKnowledgeItem, float]] = []
        for entry in merged.values():
            corroboration_bonus = 15.0 if len(entry["sources"]) > 1 else 0.0
            source_scores = entry.get("source_scores", {})
            if len(source_scores) > 1:
                rag_s = source_scores.get("rag", 0.0)
                basic_s = source_scores.get("basic", 0.0)
                final_score = rag_s * 0.6 + basic_s * 0.4 + corroboration_bonus
            else:
                final_score = sum(source_scores.values()) + corroboration_bonus
            final_results.append((entry["item"], final_score))

        final_results.sort(key=lambda x: x[1], reverse=True)
        return final_results[:top_k]

    def _normalize_rag_merge_score(self, raw_score: float) -> float:
        """把 RAG 原始分数映射到与基础检索更接近的合并尺度。"""
        try:
            score = float(raw_score or 0.0)
        except (TypeError, ValueError):
            score = 0.0

        if score <= 0:
            return 0.0
        if score <= 1.0:
            return score * 100.0
        if score <= 3.0:
            return 100.0 + (score - 1.0) * 15.0
        return min(130.0 + (score - 3.0) * 10.0, 200.0)

    def _get_file_signature(self) -> Optional[Tuple[int, int]]:
        """返回知识文件签名，用于检测进程外变更。"""
        return self._persistence.get_file_signature()

    def _update_file_signature(self):
        """记录当前知识文件签名。"""
        self._persistence.update_file_signature()

    def _refresh_runtime_state(self):
        """数据变更后重建缓存与检索运行态。"""
        self._invalidate_cache()
        self._rag_retriever = None
        self._rag_retriever_initialized = False
        pipeline = getattr(self, "_retrieval_pipeline", None)
        if pipeline is not None:
            pipeline.reranker = None
        # 检索管道内部会缓存 BM25 文档快照和 reranker 运行态，知识热更新后需要整条重建。
        self._retrieval_pipeline = None

    def _load_vector_manifest(self) -> Dict[str, Dict[str, str]]:
        try:
            if not self.vector_manifest_path.exists():
                return {}
            payload = json.loads(self.vector_manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            entries = payload.get("items") if isinstance(payload.get("items"), dict) else payload
            if not isinstance(entries, dict):
                return {}
            normalized: Dict[str, Dict[str, str]] = {}
            for item_id, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                normalized[str(item_id)] = {
                    "enterprise_id": self._normalize_enterprise_id(entry.get("enterprise_id")),
                    "fingerprint": str(entry.get("fingerprint") or ""),
                }
            return normalized
        except Exception as exc:
            logger.warning(f"读取向量同步清单失败: {exc}")
            return {}

    def _save_vector_manifest(self, manifest: Dict[str, Dict[str, str]]) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(),
            "items": manifest,
        }
        self.vector_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix="vector_manifest_",
            suffix=".tmp",
            dir=str(self.vector_manifest_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.vector_manifest_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _build_vector_manifest_entry(self, item: UnifiedKnowledgeItem) -> Dict[str, str]:
        document = self._build_vector_document(item)
        fingerprint = hashlib.md5(
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "enterprise_id": self._normalize_enterprise_id(item.enterprise_id),
            "fingerprint": fingerprint,
        }

    def _build_runtime_vector_manifest(self) -> Dict[str, Dict[str, str]]:
        manifest: Dict[str, Dict[str, str]] = {}
        for item in self._items:
            if not self._is_runtime_available_item(item):
                continue
            manifest[item.id] = self._build_vector_manifest_entry(item)
        return manifest

    def _ensure_retrieval_pipeline(self) -> UnifiedRetrievalPipeline:
        pipeline = getattr(self, "_retrieval_pipeline", None)
        if pipeline is not None:
            if self._rag_retriever is not None:
                pipeline.reranker = getattr(self._rag_retriever, "reranker", None)
            return pipeline

        service = self

        class _KeywordRetriever:
            def __init__(self, owner: "UnifiedKnowledgeService"):
                self.owner = owner

            def search_semantic(
                self,
                query: str,
                top_k: int = 5,
                category: Optional[str] = None,
                context: Optional[Dict[str, Any]] = None,
            ):
                context = context or {}
                return self.owner._basic_search(
                    query=query,
                    top_k=top_k,
                    category=category,
                    source=str(context.get("source") or "all"),
                    use_vector=False,
                    min_score=0.0,
                    business_stage=str(context.get("business_stage") or ""),
                    retrieval_plan=context.get("retrieval_plan"),
                    enterprise_id=str(context.get("enterprise_id") or ""),
                )

        class _VectorRetriever:
            def __init__(self, owner: "UnifiedKnowledgeService"):
                self.owner = owner

            def search(
                self,
                query: str,
                top_k: int = 5,
                category: Optional[str] = None,
                context: Optional[Dict[str, Any]] = None,
            ):
                context = context or {}
                allow_vector_init = bool(context.get("allow_vector_init", True))
                if not allow_vector_init and not self.owner._rag_retriever_initialized:
                    return []
                self.owner._ensure_rag_retriever()
                if not self.owner._rag_retriever:
                    return []
                unified_result = self.owner._rag_retriever.retrieve(
                    query=query,
                    top_k=top_k,
                    use_rerank=True,
                    enterprise_id=str(context.get("enterprise_id") or ""),
                )
                # retrieve() 返回 UnifiedResult 对象，需要取 .results 字段
                raw_results = []
                if unified_result is not None:
                    if hasattr(unified_result, "results"):
                        raw_results = unified_result.results or []
                    elif isinstance(unified_result, (list, tuple)):
                        raw_results = list(unified_result)
                    else:
                        raw_results = []
                pipeline_results = []
                for result in raw_results:
                    doc_id = getattr(result, "doc_id", "") or getattr(result, "chunk_id", "") or getattr(result, "document_id", "")
                    item = self.owner.get_item_by_id(doc_id) if doc_id else None
                    if not item or not self.owner._is_runtime_available_item(item):
                        continue
                    if not self.owner._item_matches_enterprise(item, str(context.get("enterprise_id") or "")):
                        continue
                    if category and item.category != category:
                        continue
                    source = str(context.get("source") or "all")
                    if source != "all":
                        allowed_sources = self.owner._get_allowed_sources(source)
                        if item.source not in allowed_sources:
                            continue
                    score = float(getattr(result, "score", 0.0) or 0.0)
                    pipeline_results.append(
                        (
                            SimpleNamespace(
                                id=item.id,
                                content=f"{item.question} {item.answer}",
                                metadata={
                                    "question": item.question,
                                    "answer": item.answer,
                                    "category": item.category,
                                    "source": item.source,
                                    "service_score": self.owner._normalize_rag_merge_score(score),
                                },
                            ),
                            score,
                        )
                    )
                return pipeline_results

        class _BM25Retriever:
            def __init__(self, owner: "UnifiedKnowledgeService"):
                self.owner = owner
                documents: List[Dict[str, Any]] = []
                for item in owner._items:
                    if not owner._is_runtime_available_item(item):
                        continue
                    enterprise_id = owner._normalize_enterprise_id(item.enterprise_id)
                    documents.append(
                        {
                            "id": item.id,
                            "content": "\n".join(
                                part
                                for part in [
                                    str(item.question or ""),
                                    str(item.answer or ""),
                                    " ".join(list(item.aliases or [])),
                                    " ".join(list(item.keywords or [])),
                                    " ".join(list(item.tags or [])),
                                ]
                                if str(part or "").strip()
                            ),
                            "metadata": {
                                "question": item.question,
                                "answer": item.answer,
                                "category": item.category,
                                "source": item.source,
                                "enterprise_id": enterprise_id,
                                "aliases": list(item.aliases or []),
                                "keywords": list(item.keywords or []),
                                "tags": list(item.tags or []),
                            },
                        }
                    )
                self.retriever = create_bm25_retriever(documents)

            def search(
                self,
                query: str,
                top_k: int = 5,
                category: Optional[str] = None,
                context: Optional[Dict[str, Any]] = None,
            ):
                context = context or {}
                retrieval_plan = context.get("retrieval_plan")
                enterprise_id = self.owner._normalize_enterprise_id(context.get("enterprise_id"))
                source = str(context.get("source") or "all")
                allowed_sources = set(self.owner._get_allowed_sources(source)) if source != "all" else None
                merged: Dict[str, Tuple[Dict[str, Any], float]] = {}
                candidates: List[Tuple[Dict[str, Any], float]] = []
                try:
                    candidates.extend(self.retriever.search(query, top_k=max(top_k * 2, 8)))
                    candidates.extend(self.retriever.search_with_expansion(query, top_k=max(top_k * 2, 8)))
                except Exception as exc:
                    logger.warning(f"BM25检索执行失败: {exc}")
                    return []

                for doc, score in candidates:
                    metadata = doc.get("metadata", {}) or {}
                    if category and metadata.get("category") != category:
                        continue
                    if enterprise_id and metadata.get("enterprise_id") != enterprise_id:
                        continue
                    if allowed_sources is not None and metadata.get("source") not in allowed_sources:
                        continue
                    boosted_score = float(score or 0.0)
                    haystack = " ".join(
                        [
                            str(doc.get("content") or ""),
                            " ".join(list(metadata.get("keywords", []) or [])),
                            " ".join(list(metadata.get("aliases", []) or [])),
                        ]
                    ).lower()
                    for term in list(getattr(retrieval_plan, "preferred_terms", []) or [])[:6]:
                        term = str(term or "").strip().lower()
                        if term and term in haystack:
                            boosted_score += 0.35
                    existing = merged.get(doc.get("id", ""))
                    if existing is None or boosted_score > existing[1]:
                        merged[doc.get("id", "")] = (doc, boosted_score)

                return sorted(merged.values(), key=lambda item: item[1], reverse=True)[:top_k]

        # 注入查询改写器：知识库路径启用上下文感知改写
        query_rewriter = None
        try:
            from src.rag.context_query_rewriter import get_query_rewriter
            query_rewriter = get_query_rewriter()
        except ImportError:
            pass

        # 修复 P2：注入知识图谱到主链检索管线，之前未传 knowledge_graph 导致图谱检索永远返回空
        # 修复 R3：不再静默吞掉异常，记录日志便于排查
        knowledge_graph = None
        try:
            from src.common.knowledge_graph import get_knowledge_graph_service
            knowledge_graph = get_knowledge_graph_service()
            if knowledge_graph is None:
                logger.warning("[RAG] 知识图谱服务返回 None，KG 检索路径将被跳过")
            else:
                logger.info("[RAG] 知识图谱服务注入成功")
        except ImportError as e:
            logger.warning(f"[RAG] 知识图谱模块不可用，KG 检索路径将被跳过: {e}")
        except Exception as e:
            logger.error(f"[RAG] 知识图谱服务初始化失败，KG 检索路径将被跳过: {e}", exc_info=True)

        pipeline = UnifiedRetrievalPipeline(
            knowledge_base=_KeywordRetriever(service),
            vector_retriever=_VectorRetriever(service),
            bm25_retriever=_BM25Retriever(service),
            reranker=getattr(self._rag_retriever, "reranker", None) if self._rag_retriever else None,
            query_rewriter=query_rewriter,
            knowledge_graph=knowledge_graph,
            config=PipelineConfig.from_weights(
                schema=self._get_active_schema(),
                top_k=5,
                score_threshold=0.0,
                enable_reranking=bool(self._rag_retriever and getattr(self._rag_retriever, "reranker", None)),
                enable_query_rewrite=True,
                enable_query_expansion=True,
                enable_semantic_dedup=True,
                enable_relevance_filter=True,
            ),
        )
        self._retrieval_pipeline = pipeline
        return pipeline

    def _convert_pipeline_results(
        self,
        query: str,
        pipeline_results: List[Any],
        *,
        top_k: int,
        business_stage: str = "",
        retrieval_plan: Optional[RetrievalPlan] = None,
    ) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        converted: List[Tuple[UnifiedKnowledgeItem, float]] = []
        for rank, result in enumerate(pipeline_results or []):
            item = self.get_item_by_id(getattr(result, "doc_id", ""))
            if not item or not self._is_runtime_available_item(item):
                continue
            if retrieval_plan and retrieval_plan.enterprise_id and not self._item_matches_enterprise(item, retrieval_plan.enterprise_id):
                continue
            if retrieval_plan and not self._item_matches_schema(item, retrieval_plan.schema_id):
                continue
            metadata = getattr(result, "metadata", {}) or {}
            try:
                base_score = float(metadata.get("service_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                base_score = 0.0
            if base_score <= 0:
                try:
                    base_score = self._normalize_rag_merge_score(getattr(result, "score", 0.0))
                except Exception:
                    base_score = 0.0
            rank_bonus = max(0.0, 12.0 - rank * 2.0)
            final_score = self._apply_query_bias(query, item, base_score + rank_bonus, retrieval_plan=retrieval_plan)
            final_score = self._apply_business_stage_bias(query, item, final_score, business_stage)
            converted.append((item, final_score))
            if len(converted) >= top_k:
                break
        return converted

    def _pipeline_search(
        self,
        query: str,
        *,
        top_k: int,
        category: Optional[str],
        source: str,
        min_score: float,
        business_stage: str,
        retrieval_plan: Optional[RetrievalPlan],
        enterprise_id: str,
        allow_vector_init: bool,
    ) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        pipeline = self._ensure_retrieval_pipeline()
        pipeline.config.top_k = top_k
        pipeline.config.enable_reranking = bool(self._rag_retriever and getattr(self._rag_retriever, "reranker", None))
        pipeline.reranker = getattr(self._rag_retriever, "reranker", None) if self._rag_retriever else None
        results = pipeline.search(
            query=query,
            top_k=max(top_k * 2, 8),
            category=category,
            context={
                "source": source,
                "enterprise_id": enterprise_id,
                "business_stage": business_stage,
                "retrieval_plan": retrieval_plan,
                "allow_vector_init": allow_vector_init,
                # 修复 N8：标记查询已被主链改写，pipeline 内部跳过重复改写
                "query_already_rewritten": True,
            },
        )
        converted = self._convert_pipeline_results(
            query,
            results,
            top_k=max(top_k * 2, 8),
            business_stage=business_stage,
            retrieval_plan=retrieval_plan,
        )
        if not converted:
            return []
        postprocessed = self._postprocess_customer_domain_results(
            query,
            converted,
            business_stage=business_stage,
        )
        if postprocessed and postprocessed[0][1] < min_score:
            return []
        return postprocessed[:top_k]

    def _ensure_vector_store(self):
        """按需初始化向量存储，避免服务启动阶段提前加载 embedding。"""
        if self._vector_store_initialized:
            return
        started_at = time.perf_counter()
        self._init_vector_store()
        self._vector_store_initialized = True
        logger.info(
            "统一知识库按需初始化向量存储完成: "
            f"enabled={self._vector_enabled} "
            f"elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
        )

    def _ensure_rag_retriever(self):
        """按需初始化 RAG 检索器，避免服务启动阶段提前加载 reranker。"""
        if self._rag_retriever_initialized:
            return
        started_at = time.perf_counter()
        self._ensure_vector_store()
        self._init_rag_retriever()
        self._rag_retriever_initialized = True
        logger.info(
            "统一知识库按需初始化RAG检索器完成: "
            f"available={self._rag_retriever is not None} "
            f"elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
        )

    def warmup_retrieval_chain(self, force: bool = False, preload_reranker: bool = True) -> Dict[str, Any]:
        """后台预热语义检索链，避免首条真实消息承担冷启动。"""
        if self._warmup_completed and not force:
            return {"success": True, "status": "already_warm", "warmed": True}

        with self._warmup_lock:
            if self._warmup_completed and not force:
                return {"success": True, "status": "already_warm", "warmed": True}

            started_at = time.perf_counter()
            self._warmup_last_error = ""
            try:
                self._ensure_rag_retriever()
                reranker_ready = False
                if preload_reranker and self._rag_retriever is not None:
                    from src.common.unified_retriever import get_reranker

                    if getattr(self._rag_retriever, "reranker", None) is None:
                        self._rag_retriever.reranker = get_reranker()
                    reranker_ready = getattr(self._rag_retriever, "reranker", None) is not None

                self._warmup_completed = bool(self._rag_retriever_initialized)
                elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
                logger.info(
                    "统一知识库后台预热完成: "
                    f"warmed={self._warmup_completed} elapsed_ms={elapsed_ms}"
                )
                return {
                    "success": True,
                    "status": "warmed" if self._warmup_completed else "skipped",
                    "warmed": self._warmup_completed,
                    "reranker_ready": reranker_ready,
                    "elapsed_ms": elapsed_ms,
                }
            except Exception as exc:
                self._warmup_last_error = str(exc)
                logger.warning(f"统一知识库后台预热失败: {exc}")
                return {
                    "success": False,
                    "status": "failed",
                    "warmed": False,
                    "error": self._warmup_last_error,
                }

    def _refresh_if_external_change(self, resync_vector: bool = False):
        """
        检测知识库文件是否被进程外修改。

        一旦检测到外部改动，则重新加载知识数据；必要时顺带重同步向量库，
        修复“JSON 已更新但向量库未同步”的失步问题。
        """
        changed, new_items = self._persistence.hot_reload(current_items=self._items)
        if not changed:
            return

        with self._data_lock:
            previous_count = len(self._items)
            self._items = [UnifiedKnowledgeItem.from_dict(item) for item in new_items]
            self._refresh_runtime_state()
            logger.info(
                "检测到知识库文件外部变更，已重新加载数据: "
                f"{previous_count} -> {len(self._items)} 条"
            )

        if resync_vector and self._vector_store:
            result = self.resync_to_vector()
            if not result.get("success", False):
                logger.warning(f"知识库外部变更后向量重同步失败: {result}")

        if self._rag_retriever and hasattr(self._rag_retriever, 'hybrid_retriever'):
            try:
                self._rag_retriever.hybrid_retriever.refresh_index(self._items)
            except Exception:
                pass

    def _get_allowed_sources(self, source: str) -> List[str]:
        """统一数据源别名，兼容新版知识库导入来源。"""
        source_mapping = {
            "all": ["main", "knowledge_base", "enterprise", "enterprise_knowledge", "manual", "document_upload"],
            "main": ["main", "knowledge_base", "manual"],
            "enterprise": ["enterprise", "enterprise_knowledge", "document_upload"]
        }
        return source_mapping.get(source, [source])

    def _normalize_enterprise_id(self, enterprise_id: Optional[str]) -> str:
        normalized = str(enterprise_id or "").strip()
        return normalized or self.DEFAULT_ENTERPRISE_ID

    def _get_vector_enterprise_ids(self, source: str = "all") -> List[str]:
        enterprise_ids: List[str] = []
        seen = set()

        def add_enterprise_id(value: Optional[str]):
            normalized = self._normalize_enterprise_id(value)
            if normalized not in seen:
                seen.add(normalized)
                enterprise_ids.append(normalized)

        if source == "all":
            for legacy_id in self.LEGACY_ENTERPRISE_IDS:
                add_enterprise_id(legacy_id)
            for item in self._items:
                if self._is_runtime_available_item(item):
                    add_enterprise_id(item.enterprise_id)
            return enterprise_ids

        if source == "main":
            add_enterprise_id(self.DEFAULT_ENTERPRISE_ID)
            add_enterprise_id("main")
            return enterprise_ids

        if source == "enterprise":
            add_enterprise_id("enterprise")
            allowed_sources = set(self._get_allowed_sources(source))
            for item in self._items:
                if self._is_runtime_available_item(item) and (item.source or "main") in allowed_sources:
                    add_enterprise_id(item.enterprise_id)
            return enterprise_ids

        matching_source = False
        for item in self._items:
            if self._is_runtime_available_item(item) and (item.source or "main") == source:
                matching_source = True
                add_enterprise_id(item.enterprise_id)

        if not matching_source:
            add_enterprise_id(source)

        return enterprise_ids

    def _is_pending_review_item(self, item: Optional[UnifiedKnowledgeItem]) -> bool:
        if not item:
            return False
        tags = list(getattr(item, "tags", []) or [])
        return (not getattr(item, "enabled", True)) and ("待人工审核" in tags)

    def _is_runtime_available_item(self, item: Optional[UnifiedKnowledgeItem]) -> bool:
        # 页面手工维护的旧禁用知识仍要参与检索/向量同步；仅待人工审核条目需要隔离。
        return bool(item) and not self._is_pending_review_item(item)
    
    def _save_data(self):
        """原子保存数据到统一文件"""
        self._persistence.save_knowledge_base(self._items)

    def persist_runtime_mutations(self, sync_vector: bool = True) -> Dict[str, Any]:
        """
        持久化当前内存中的知识变更。

        兼容旧链路先直接修改 `knowledge_items` 再统一保存的场景，
        确保 JSON、检索缓存和向量库状态保持一致。
        """
        with self._data_lock:
            self._save_data()
            self._refresh_runtime_state()

        if sync_vector and self._vector_store:
            return self.resync_to_vector()

        return {"success": True, "synced_count": 0}

    def backfill_trigger_keywords(
        self,
        *,
        enterprise_id: str = "",
        sync_vector: bool = True,
        force_full_resync: bool = True,
    ) -> Dict[str, Any]:
        """批量回洗历史知识的触发关键词，并按需重同步向量库。"""
        self._refresh_if_external_change(resync_vector=False)
        normalized_enterprise_id = self._normalize_enterprise_id(enterprise_id) if str(enterprise_id or "").strip() else ""
        updated_ids: List[str] = []
        skipped_count = 0
        now = datetime.now().isoformat()

        with self._data_lock:
            for index, item in enumerate(self._items):
                if normalized_enterprise_id and self._normalize_enterprise_id(item.enterprise_id) != normalized_enterprise_id:
                    continue
                normalized_keywords = build_trigger_keywords(
                    item.question,
                    item.answer,
                    provided_keywords=item.keywords,
                    aliases=item.aliases,
                )
                current_keywords = _normalize_list_tokens(item.keywords)
                if normalized_keywords == current_keywords:
                    skipped_count += 1
                    continue
                updated_data = item.to_dict()
                updated_data["keywords"] = normalized_keywords
                updated_data["updated_at"] = now
                updated_data = _sanitize_knowledge_item_payload(
                    updated_data,
                    strict_question_validation=_looks_like_interrogative(updated_data.get("question", "")),
                )
                updated_data = _normalize_runtime_flags(updated_data)
                self._items[index] = UnifiedKnowledgeItem.from_dict(updated_data)
                updated_ids.append(self._items[index].id)

            if updated_ids:
                self._save_data()
                self._refresh_runtime_state()

        vector_result: Dict[str, Any] = {"success": True, "synced_count": 0, "skipped": True}
        if sync_vector and (updated_ids or force_full_resync):
            vector_result = self.resync_to_vector(force_full=force_full_resync)

        return {
            "success": True,
            "enterprise_id": normalized_enterprise_id or "all",
            "total_count": len(
                [
                    item
                    for item in self._items
                    if not normalized_enterprise_id
                    or self._normalize_enterprise_id(item.enterprise_id) == normalized_enterprise_id
                ]
            ),
            "updated_count": len(updated_ids),
            "skipped_count": skipped_count,
            "updated_item_ids": updated_ids[:50],
            "sync_vector": bool(sync_vector),
            "force_full_resync": bool(force_full_resync),
            "vector_result": vector_result,
        }
    
    def get_items(self, source: str = "all", category: str = None, 
                  status: str = None, enabled: bool = None,
                  include_pending_review: bool = False) -> List[UnifiedKnowledgeItem]:
        """
        获取知识列表
        
        Args:
            source: 数据源 (all/main/enterprise)
            category: 分类过滤
            status: 状态过滤
            enabled: 启用状态过滤
            include_pending_review: 是否包含待人工审核知识，默认不包含
            
        Returns:
            List[UnifiedKnowledgeItem]: 知识列表
        """
        self._refresh_if_external_change(resync_vector=True)
        items = self._items
        
        if source and source != "all":
            allowed_sources = self._get_allowed_sources(source)
            items = [item for item in items if item.source in allowed_sources]
        
        if category:
            items = [item for item in items if item.category == category]
        
        if status:
            items = [item for item in items if item.status == status]
        
        if enabled is not None:
            items = [item for item in items if item.enabled == enabled]

        if not include_pending_review:
            items = [item for item in items if not self._is_pending_review_item(item)]
        
        return items
    
    def get_item_by_id(self, item_id: str) -> Optional[UnifiedKnowledgeItem]:
        """根据ID获取知识条目（支持精确匹配和模糊匹配）"""
        self._refresh_if_external_change(resync_vector=True)
        for item in self._items:
            if item.id == item_id:
                return item
        
        if item_id and len(item_id) > 8:
            short_id = item_id[:8]
            for item in self._items:
                if item.id and item.id.startswith(short_id):
                    return item
        
        if item_id:
            for item in self._items:
                if item.id and item_id in item.id:
                    return item
        
        return None
    
    def get_all_items(self) -> List[UnifiedKnowledgeItem]:
        """
        获取所有知识条目
        
        Returns:
            所有知识条目列表
        """
        self._refresh_if_external_change(resync_vector=True)
        return self._items
    
    def get_statistics(self, source: str = "all", include_pending_review: bool = False) -> Dict[str, Any]:
        """
        获取统计数据
        
        Args:
            source: 数据源过滤
            include_pending_review: 是否包含待人工审核知识，默认不包含
            
        Returns:
            Dict: 统计数据
        """
        self._ensure_cache_state()
        self._refresh_if_external_change(resync_vector=False)
        cache_key = f"stats_{source}_{'all' if include_pending_review else 'visible'}"
        if cache_key in self._cache and (datetime.now().timestamp() - self._cache_time) < self._cache_ttl:
            return self._cache[cache_key]

        items = list(self._items)
        if source and source != "all":
            allowed_sources = self._get_allowed_sources(source)
            items = [item for item in items if item.source in allowed_sources]

        if not include_pending_review:
            items = [item for item in items if not self._is_pending_review_item(item)]
        
        total_count = len(items)
        enabled_count = len([i for i in items if bool(getattr(i, "enabled", True))])
        total_use_count = sum(i.use_count for i in items)
        
        category_distribution = defaultdict(int)
        for item in items:
            category_distribution[item.category] += 1
        
        source_distribution = defaultdict(int)
        for item in items:
            raw_source = item.source or "unknown"
            normalized_source = raw_source
            if raw_source in ["knowledge_base", "main"]:
                normalized_source = "main"
            elif raw_source in ["enterprise_knowledge", "enterprise"]:
                normalized_source = "enterprise"
            source_distribution[normalized_source] += 1
        
        top_used = sorted(items, key=lambda x: x.use_count, reverse=True)[:5]
        
        stats = {
            "total_count": total_count,
            "enabled_count": enabled_count,
            "disabled_count": total_count - enabled_count,
            "total_use_count": total_use_count,
            "avg_use_count": round(total_use_count / total_count, 2) if total_count > 0 else 0,
            "category_distribution": dict(category_distribution),
            "source_distribution": dict(source_distribution),
            "top_used": [
                {
                    "id": item.id,
                    "question": item.question,
                    "use_count": item.use_count,
                    "category": item.category
                }
                for item in top_used
            ]
        }
        
        self._cache[cache_key] = stats
        self._cache_time = datetime.now().timestamp()
        
        return stats
    
    def search(self, query: str, top_k: int = 5, category: str = None,
               source: str = "all", use_vector: bool = True,
               min_score: float = 8.0,
               business_stage: str = "",
               intent: str = "",
               enterprise_id: str = "",
               schema_id: str = "",
               allow_vector_init: bool = True,
               retrieval_options: Optional[Dict[str, Any]] = None) -> List[Tuple[UnifiedKnowledgeItem, float]]:
        """
        搜索知识 - 使用RAG混合检索策略
        
        结合向量语义检索、关键词检索和重排序，提供更准确的搜索结果
        
        Args:
            query: 搜索关键词
            top_k: 返回数量
            category: 分类过滤
            source: 数据源过滤
            use_vector: 是否使用向量检索
            min_score: 最小置信度阈值，低于此分数的结果将被过滤
            
        Returns:
            List[Tuple[UnifiedKnowledgeItem, float]]: 搜索结果及匹配分数
        """
        self._refresh_if_external_change(resync_vector=True)
        normalized_enterprise_id = self._normalize_enterprise_id(enterprise_id)
        retrieval_plan = self._ensure_retrieval_policy_service().build_plan(
            query=query,
            intent=intent,
            enterprise_id=normalized_enterprise_id,
            business_stage=business_stage,
            retrieval_options={
                **(retrieval_options or {}),
                "schema_id": str(schema_id or "").strip(),
                "allow_vector_init": allow_vector_init,
            },
        )
        search_query = retrieval_plan.routing_query or query
        schema_context_token = _ACTIVE_SCHEMA_CONTEXT.set(
            {
                "enterprise_id": normalized_enterprise_id,
                "schema_id": str(getattr(retrieval_plan, "schema_id", "") or ""),
            }
        )
        try:

            exact_item = self._find_exact_query_item(
                query,
                source=source,
                category=category,
                enterprise_id=normalized_enterprise_id,
                retrieval_plan=retrieval_plan,
            )
            if exact_item and self._is_runtime_available_item(exact_item):
                exact_score = float(
                    self._policy_get(retrieval_plan.policy, "scoring", "exact_match", "direct_score", default=500.0)
                )
                pipeline_results = self._pipeline_search(
                    search_query,
                    top_k=max(top_k * 2, 8),
                    category=category,
                    source=source,
                    min_score=0.0,
                    business_stage=business_stage,
                    retrieval_plan=retrieval_plan,
                    enterprise_id=normalized_enterprise_id,
                    allow_vector_init=allow_vector_init,
                )
                exact_results = [(exact_item, exact_score)]
                exact_results.extend(
                    (item, score)
                    for item, score in pipeline_results
                    if getattr(item, "id", None) != getattr(exact_item, "id", None)
                )
                return self._apply_decay_weights(exact_results)[:top_k]

            if allow_vector_init:
                self._ensure_rag_retriever()
            elif not self._rag_retriever_initialized:
                logger.info(
                    f"统一检索当前跳过向量冷启动，优先返回BM25/关键词结果: "
                    f"query={query[:30]} source={source}"
                )

            if self._rag_retriever or not allow_vector_init:
                try:
                    pipeline_results = self._pipeline_search(
                        search_query,
                        top_k=max(top_k * 2, 8),
                        category=category,
                        source=source,
                        min_score=min_score,
                        business_stage=business_stage,
                        retrieval_plan=retrieval_plan,
                        enterprise_id=normalized_enterprise_id,
                        allow_vector_init=allow_vector_init,
                    )
                    if pipeline_results:
                        return self._apply_decay_weights(pipeline_results)[:top_k]
                    logger.info(
                        f"统一检索主链无可用结果，回退到基础检索: "
                        f"query={query[:30]} source={source} min_score={min_score}"
                    )
                except Exception as e:
                    logger.warning(f"统一检索主链失败，回退到基础检索: {e}")
            else:
                logger.info(
                    f"RAG检索器不可用，直接使用基础检索: "
                    f"query={query[:30]} source={source} min_score={min_score}"
                )

            basic_results = self._basic_search(
                query=search_query,
                top_k=max(top_k * 2, 10),
                category=category,
                source=source,
                use_vector=use_vector,
                min_score=min_score,
                business_stage=business_stage,
                retrieval_plan=retrieval_plan,
                enterprise_id=normalized_enterprise_id,
            )
            if not basic_results:
                return []

            fallback_results = self._postprocess_customer_domain_results(
                query,
                basic_results,
                business_stage=business_stage,
            )
            if fallback_results and fallback_results[0][1] < min_score:
                logger.info(f"基础检索回退分数过低 ({fallback_results[0][1]:.2f} < {min_score}), 拦截无关匹配")
                return []

            return self._apply_decay_weights(fallback_results)[:top_k]
        finally:
            _ACTIVE_SCHEMA_CONTEXT.reset(schema_context_token)
    
    def _apply_decay_weights(self, results):
        try:
            from .knowledge_decay_manager import get_decay_manager
            decay_manager = get_decay_manager()
            decayed_results = []
            for item, score in results:
                decay_factor = decay_manager.calculate_decay(item.id if hasattr(item, 'id') else "")
                decayed_results.append((item, score * decay_factor))
            decayed_results.sort(key=lambda x: x[1], reverse=True)
            return decayed_results
        except Exception as e:
            logger.warning(f"衰减权重应用失败: {e}")
            return results

    def _basic_search(self, query: str, top_k: int = 5, category: str = None,
                      source: str = "all", use_vector: bool = True,
                      min_score: float = 15.0,
                      business_stage: str = "",
                      retrieval_plan: Optional[RetrievalPlan] = None,
                      enterprise_id: str = "") -> List[Tuple[UnifiedKnowledgeItem, float]]:
        """
        基础检索方法（回退方案）
        
        Args:
            query: 搜索关键词
            top_k: 返回数量
            category: 分类过滤
            source: 数据源过滤
            use_vector: 是否使用向量检索
            min_score: 最小置信度阈值，低于此分数的结果将被过滤
            
        Returns:
            List[Tuple[UnifiedKnowledgeItem, float]]: 搜索结果及匹配分数
        """
        results_map = {}
        policy = self._get_retrieval_policy(retrieval_plan)
        priority_factor = float(self._policy_get(policy, "scoring", "priority_factor", default=0.5) or 0.5)
        
        # 1. 关键词检索（基础分数）
        items = [
            item
            for item in self.get_items(source=source, category=category)
            if (
                self._is_runtime_available_item(item)
                and self._item_matches_enterprise(item, enterprise_id)
                and self._item_matches_schema(item, getattr(retrieval_plan, "schema_id", ""))
            )
        ]
        query_lower = query.lower()
        normalized_query = self._normalize_search_text(query)
        try:
            import jieba
            query_words = set(jieba.cut(query_lower))
            query_words = {w for w in query_words if len(w.strip()) > 1}
        except ImportError:
            query_words = {w for w in query_lower.split() if len(w.strip()) > 1}
        
        for item in items:
            score = 0.0
            question_lower = item.question.lower()
            answer_lower = item.answer.lower()
            normalized_question = self._normalize_search_text(item.question)
            normalized_answer = self._normalize_search_text(item.answer)
            question_exact_matched = bool(normalized_query and normalized_query == normalized_question)
            
            if question_exact_matched:
                score += 120.0
            elif normalized_query and normalized_query in normalized_question:
                score += 60.0
            elif query_lower in question_lower:
                score += 30.0
            elif any(word in question_lower for word in query_words):
                score += 15.0
            
            if normalized_query and normalized_query == normalized_answer:
                score += 25.0
            elif query_lower in answer_lower:
                score += 30.0
            elif normalized_query and normalized_query in normalized_answer:
                score += 20.0
            elif any(word in item.answer.lower() for word in query_words):
                score += 5.0
            
            for keyword in item.keywords:
                normalized_keyword = self._normalize_search_text(keyword)
                if normalized_query and normalized_keyword == normalized_query:
                    score += 35.0
                elif keyword.lower() in query_lower:
                    score += 20.0
                elif normalized_query and normalized_keyword and normalized_query in normalized_keyword:
                    score += 15.0
                elif query_lower in keyword.lower():
                    score += 10.0
            
            for alias in item.aliases:
                normalized_alias = self._normalize_search_text(alias)
                if not question_exact_matched and normalized_query and normalized_alias == normalized_query:
                    score += 100.0
                elif normalized_query and normalized_alias and (
                    normalized_alias in normalized_query or normalized_query in normalized_alias
                ):
                    score += 45.0
                elif alias.lower() in query_lower or query_lower in alias.lower():
                    score += 15.0
            
            for tag in item.tags:
                if tag.lower() in query_lower:
                    score += 8.0
            
            score += item.priority * priority_factor
            score = self._apply_query_bias(query, item, score, retrieval_plan=retrieval_plan)
            
            score = min(score, 200.0)
            
            if score >= min_score:
                results_map[item.id] = [item, score]
        
        # 2. 向量语义检索（补充分数）
        if use_vector and self._vector_enabled and self._vector_store:
            try:
                search_enterprise_ids = (
                    [self._normalize_enterprise_id(enterprise_id)]
                    if enterprise_id
                    else self._get_vector_enterprise_ids(source)
                )
                for eid in search_enterprise_ids:
                    try:
                        _vec_future = self._search_executor.submit(
                            self._vector_store.search,
                            enterpriseId=eid,
                            query=query,
                            topK=top_k * 3,
                        )
                        try:
                            vector_results = _vec_future.result(timeout=5.0)
                        except FuturesTimeoutError:
                            logger.warning(f"向量检索超时(5s): eid={eid} query={query[:30]}")
                            continue
                        
                        for doc, score in vector_results:
                            item_id = doc.get("id")
                            item = self.get_item_by_id(item_id)
                            if item and self._is_runtime_available_item(item):
                                if not self._item_matches_enterprise(item, enterprise_id or eid):
                                    continue
                                if not self._item_matches_schema(item, getattr(retrieval_plan, "schema_id", "")):
                                    continue
                                if category and item.category != category:
                                    continue
                                if source != "all":
                                    allowed_sources = self._get_allowed_sources(source)
                                    if item.source not in allowed_sources:
                                        continue
                                
                                vector_score = min(score * 100.0, 120.0)
                                
                                if item_id in results_map:
                                    base_score = results_map[item_id][1]
                                    results_map[item_id][1] = base_score * 0.6 + vector_score * 0.4
                                else:
                                    if vector_score >= min_score:
                                        results_map[item_id] = [item, vector_score]
                    except Exception:
                        continue
                        
            except Exception as e:
                logger.warning(f"向量检索失败: {e}")
        
        # 3. 排序并返回结果
        results = [(item, score) for item, score in results_map.values()]
        results.sort(key=lambda x: x[1], reverse=True)
        
        return results[:top_k]
    
    def add_item(self, item_data: Dict) -> UnifiedKnowledgeItem:
        """
        添加知识条目

        同时同步到向量数据库
        """
        # 阶段5.1：知识库内容安全审计
        # 在写入前进行敏感词扫描与自动脱敏，避免手机号/邮箱/违规内容入库
        try:
            auditor = _get_content_auditor()
            preview_text = "{} {}".format(
                item_data.get("question", "") or "",
                item_data.get("answer", "") or "",
            )
            is_safe, issues = auditor.audit(preview_text)
            if not is_safe:
                logger.warning(
                    "知识内容存在敏感词 issues={} item_id={}",
                    issues,
                    item_data.get("id"),
                )
                # 自动脱敏
                if item_data.get("question"):
                    item_data["question"] = auditor.sanitize(item_data["question"])
                if item_data.get("answer"):
                    item_data["answer"] = auditor.sanitize(item_data["answer"])
                # 记录审计日志
                auditor.record_audit(
                    str(item_data.get("id") or ""), issues
                )
        except Exception as audit_exc:  # noqa: BLE001
            # 审计失败不阻断主流程，仅记录
            logger.warning("内容安全审计异常: {}", audit_exc)

        self._refresh_if_external_change(resync_vector=True)
        with self._data_lock:
            now = datetime.now().isoformat()

            item_data.setdefault('id', self._generate_id(item_data.get('question', '')))
            item_data.setdefault('created_at', now)
            item_data.setdefault('updated_at', now)
            item_data.setdefault('source', 'main')
            item_data['enterprise_id'] = self._normalize_enterprise_id(item_data.get('enterprise_id'))
            item_data['schema_id'] = str(
                item_data.get('schema_id')
                or get_industry_schema_service().get_effective_active_schema_id(
                    enterprise_id=item_data.get('enterprise_id', '')
                )
            ).strip()
            metadata = dict(item_data.get('metadata') or {})
            metadata['schema_id'] = item_data['schema_id']
            item_data['metadata'] = metadata
            item_data = _sanitize_knowledge_item_payload(item_data)
            item_data = _normalize_runtime_flags(item_data)

            item = UnifiedKnowledgeItem.from_dict(item_data)

            try:
                from .knowledge_quality_scorer import KnowledgeQualityScorer
                scorer = KnowledgeQualityScorer()
                quality = scorer.score(item)
                if quality and hasattr(quality, 'overall_score'):
                    item.effectiveness_score = quality.overall_score
            except Exception as e:
                logger.warning(f"质量评分失败: {e}")

            try:
                from .knowledge_validator import get_knowledge_validator, ValidationStatus
                validator = get_knowledge_validator(knowledge_base=self)
                validation = validator.validate(item)
                if validation and getattr(validation, 'status', None) == ValidationStatus.REJECTED:
                    return None
            except Exception as e:
                logger.warning(f"知识验证失败: {e}")

            self._items.append(item)
            self._save_data()

        # 同步到向量数据库
        self._sync_item_to_vector(item)

        return item
    
    def update_item(self, item_id: str, updates: Dict) -> Optional[UnifiedKnowledgeItem]:
        self._refresh_if_external_change(resync_vector=True)
        updated_item = None
        previous_enterprise_id = None
        with self._data_lock:
            for i, item in enumerate(self._items):
                if item.id == item_id:
                    updates['updated_at'] = datetime.now().isoformat()
                    previous_enterprise_id = item.enterprise_id
                    if {'question', 'answer', 'aliases', 'keywords', 'tags'} & set(updates.keys()):
                        strict_question_validation = (
                            'question' in updates
                            or _looks_like_interrogative(item.question)
                        )
                        updates = _sanitize_knowledge_item_payload(
                            {**item.to_dict(), **updates},
                            strict_question_validation=strict_question_validation,
                        )
                    if 'enterprise_id' in updates:
                        updates['enterprise_id'] = self._normalize_enterprise_id(updates.get('enterprise_id'))
                    if 'schema_id' not in updates and 'enterprise_id' in updates:
                        updates['schema_id'] = str(
                            get_industry_schema_service().get_effective_active_schema_id(
                                enterprise_id=updates.get('enterprise_id', '')
                            )
                        ).strip()
                    merged_metadata = dict((item.to_dict().get('metadata') or {}))
                    merged_metadata.update(dict(updates.get('metadata') or {}))
                    effective_schema_id = str(
                        updates.get('schema_id')
                        or item.schema_id
                        or merged_metadata.get('schema_id')
                        or get_industry_schema_service().get_effective_active_schema_id(
                            enterprise_id=updates.get('enterprise_id', item.enterprise_id)
                        )
                    ).strip()
                    updates['schema_id'] = effective_schema_id
                    merged_metadata['schema_id'] = effective_schema_id
                    updates['metadata'] = merged_metadata
                    
                    updated_data = _normalize_runtime_flags({**item.to_dict(), **updates})
                    self._items[i] = UnifiedKnowledgeItem.from_dict(updated_data)
                    self._save_data()
                    updated_item = self._items[i]
                    break
        if updated_item:
            self._update_item_in_vector(
                updated_item,
                previous_enterprise_id=previous_enterprise_id,
            )
        return updated_item
    
    def delete_item(self, item_id: str) -> bool:
        self._refresh_if_external_change(resync_vector=True)
        deleted_item = None
        with self._data_lock:
            for i, item in enumerate(self._items):
                if item.id == item_id:
                    deleted_item = self._items.pop(i)
                    self._save_data()
                    break
        if deleted_item:
            self._delete_item_from_vector(deleted_item)
            return True
        return False
    
    def batch_delete(self, item_ids: List[str]) -> int:
        """批量删除"""
        deleted = 0
        for item_id in item_ids:
            if self.delete_item(item_id):
                deleted += 1
        return deleted
    
    def increment_use_count(self, item_id: str) -> bool:
        """增加使用次数"""
        return self.increment_use_counts([item_id]) > 0

    def increment_use_counts(self, item_ids: List[str]) -> int:
        """批量增加使用次数，避免命中多条知识时重复写盘。"""
        self._refresh_if_external_change(resync_vector=False)
        normalized_ids = [str(item_id or "").strip() for item_id in (item_ids or []) if str(item_id or "").strip()]
        if not normalized_ids:
            return 0

        counts: Dict[str, int] = {}
        for item_id in normalized_ids:
            counts[item_id] = counts.get(item_id, 0) + 1

        updated = 0
        with self._data_lock:
            now = datetime.now().isoformat()
            for item in self._items:
                increment = counts.get(item.id, 0)
                if increment <= 0:
                    continue
                item.use_count += increment
                item.last_used_at = now
                updated += 1

            if updated:
                self._save_data()
        return updated
    
    def _generate_id(self, question: str) -> str:
        """生成唯一ID"""
        content = f"{question}_{datetime.now().timestamp()}"
        return hashlib.md5(content.encode()).hexdigest()[:12]
    
    def get_categories(self) -> List[Dict[str, str]]:
        """获取所有分类"""
        return [
            {"value": cat.value, "label": self._get_category_label(cat.value)}
            for cat in KnowledgeCategory
        ]
    
    def _get_category_label(self, category: str) -> str:
        """获取分类中文标签"""
        context = _ACTIVE_SCHEMA_CONTEXT.get({}) or {}
        labels = get_active_category_labels(
            enterprise_id=str(context.get("enterprise_id") or ""),
            preferred_schema_id=str(context.get("schema_id") or ""),
        )
        return labels.get(category, category)
    
    def export_data(self, source: str = "all", format: str = "json") -> str:
        """导出数据"""
        items = self.get_items(source=source)
        return json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2)
    
    def import_data(self, data: List[Dict], source: str = "main") -> int:
        """导入数据"""
        count = 0
        normalized_data = normalize_knowledge_payloads(data, source_hint=source)
        for item_data in normalized_data:
            item_data['source'] = source
            self.add_item(item_data)
            count += 1
        return count
    
    def _build_vector_document(self, item: UnifiedKnowledgeItem) -> Dict[str, Any]:
        content = f"问题: {item.question}\n答案: {item.answer}"
        if item.keywords:
            content += f"\n关键词: {', '.join(item.keywords)}"
        if item.tags:
            content += f"\n标签: {', '.join(item.tags)}"

        enterprise_id = self._normalize_enterprise_id(item.enterprise_id)
        return {
            "id": item.id,
            "content": content,
            "metadata": {
                "question": item.question,
                # 修复 R7：新增 answer 作为独立 metadata 字段，检索后无需解析 content
                "answer": item.answer,
                "category": item.category,
                "domain": item.domain or "",
                "topic": item.topic or "",
                "source": item.source or "main",
                "enterprise_id": enterprise_id,
                "schema_id": str(getattr(item, "schema_id", "") or ""),
                "keywords": ",".join(item.keywords) if item.keywords else "",
                "tags": ",".join(item.tags) if item.tags else "",
                # 修复 R7：新增 priority 和 aliases，支持按优先级过滤和别名检索扩展
                "priority": int(getattr(item, "priority", 0) or 0),
                "aliases": ",".join(getattr(item, "aliases", []) or []),
            },
        }

    def _upsert_item_to_vector(
        self,
        item: UnifiedKnowledgeItem,
        previous_enterprise_id: Optional[str] = None,
    ) -> None:
        self._ensure_vector_store()
        if not self._vector_store:
            return

        current_enterprise_id = self._normalize_enterprise_id(item.enterprise_id)
        previous_normalized = (
            self._normalize_enterprise_id(previous_enterprise_id)
            if previous_enterprise_id is not None
            else current_enterprise_id
        )

        try:
            if previous_normalized != current_enterprise_id:
                self._vector_store.deleteDocument(
                    enterpriseId=previous_normalized,
                    documentId=item.id,
                )

            if not self._is_runtime_available_item(item):
                self._vector_store.deleteDocument(
                    enterpriseId=current_enterprise_id,
                    documentId=item.id,
                )
                manifest = self._load_vector_manifest()
                manifest.pop(item.id, None)
                self._save_vector_manifest(manifest)
                logger.debug(f"知识条目 {item.id} 当前不参与向量检索，已跳过同步")
                return

            self._vector_store.upsertDocuments(
                enterpriseId=current_enterprise_id,
                documents=[self._build_vector_document(item)],
                batchSize=1,
            )
            manifest = self._load_vector_manifest()
            manifest[item.id] = self._build_vector_manifest_entry(item)
            self._save_vector_manifest(manifest)
            logger.debug(f"知识条目 {item.id} 已同步到向量数据库")
        except Exception as e:
            logger.warning(f"同步知识条目到向量数据库失败: {e}")

    def _sync_item_to_vector(self, item: UnifiedKnowledgeItem):
        """
        同步单个知识条目到向量数据库
        
        Args:
            item: 知识条目
        """
        self._upsert_item_to_vector(item)
    
    def _update_item_in_vector(
        self,
        item: UnifiedKnowledgeItem,
        previous_enterprise_id: Optional[str] = None,
    ):
        """
        更新向量数据库中的知识条目
        
        Args:
            item: 知识条目
        """
        self._upsert_item_to_vector(item, previous_enterprise_id=previous_enterprise_id)
    
    def _delete_item_from_vector(self, item: UnifiedKnowledgeItem):
        """
        从向量数据库中删除知识条目
        
        Args:
            item: 知识条目
        """
        self._ensure_vector_store()
        if not self._vector_store:
            return
        
        try:
            enterprise_id = self._normalize_enterprise_id(item.enterprise_id)
            self._vector_store.deleteDocument(
                enterpriseId=enterprise_id,
                documentId=item.id
            )
            manifest = self._load_vector_manifest()
            manifest.pop(item.id, None)
            self._save_vector_manifest(manifest)
            logger.debug(f"知识条目 {item.id} 已从向量数据库删除")
        except Exception as e:
            logger.warning(f"从向量数据库删除知识条目失败: {e}")

    def _build_lightweight_vector_stats(self, runtime_knowledge_count: int) -> Dict[str, Any]:
        manifest = self._load_vector_manifest()
        manifest_count = len(manifest)
        if runtime_knowledge_count <= 0:
            sync_status = "empty"
        elif manifest_count >= runtime_knowledge_count:
            sync_status = "synced"
        elif manifest_count > 0:
            sync_status = "out_of_sync"
        else:
            sync_status = "deferred"
        return {
            "enabled": False,
            "count": manifest_count,
            "knowledge_count": runtime_knowledge_count,
            "name": "",
            "collection_details": {},
            "sync_status": sync_status,
            "init_deferred": True,
            "source": "manifest",
            "message": "向量统计未触发初始化，返回本地账本快照",
        }

    def get_vector_stats(self, warm: bool = True) -> Dict[str, Any]:
        """
        获取向量数据库统计信息
        
        聚合所有集合的文档数量，确保统计完整
        """
        self._refresh_if_external_change(resync_vector=warm)
        runtime_knowledge_count = len([item for item in self._items if self._is_runtime_available_item(item)])
        if not warm and not self._vector_store_initialized:
            return self._build_lightweight_vector_stats(runtime_knowledge_count)

        if warm:
            self._ensure_vector_store()
        if not self._vector_store:
            return {
                "enabled": False,
                "count": 0,
                "knowledge_count": runtime_knowledge_count,
                "message": "向量数据库未初始化"
            }
        
        try:
            total_count = 0
            collection_details = {}
            for eid in self._get_vector_enterprise_ids("all"):
                try:
                    stats = self._vector_store.getCollectionStats(eid)
                    c_count = stats.get("count", 0)
                    if c_count > 0:
                        total_count += c_count
                        collection_details[eid] = {
                            "count": c_count,
                            "name": stats.get("name", "")
                        }
                except Exception:
                    pass

            if total_count == 0:
                try:
                    all_collections = self._vector_store.listCollections()
                    for coll in all_collections:
                        c_count = coll.get("count", 0)
                        if c_count > 0:
                            total_count += c_count
                            collection_details[coll.get("name", "")] = {
                                "count": c_count,
                                "name": coll.get("name", "")
                            }
                except Exception:
                    pass

            return {
                "enabled": True,
                "count": total_count,
                "name": ",".join(collection_details.keys()) if collection_details else "",
                "knowledge_count": runtime_knowledge_count,
                "collection_details": collection_details,
                "sync_status": "synced" if total_count >= runtime_knowledge_count else "out_of_sync"
            }
        except Exception as e:
            return {
                "enabled": False,
                "count": 0,
                "knowledge_count": runtime_knowledge_count,
                "error": str(e)
            }
    
    def _full_resync_to_vector(self) -> Dict[str, Any]:
        """
        全量重建向量数据库
        """
        runtime_knowledge_count = len([item for item in self._items if self._is_runtime_available_item(item)])
        try:
            for eid in self._get_vector_enterprise_ids("all"):
                try:
                    self._vector_store.deleteEnterpriseData(eid)
                except Exception:
                    pass
            
            try:
                all_collections = self._vector_store.listCollections()
                for coll in all_collections:
                    try:
                        coll_name = coll.get("name", "")
                        prefix = self._vector_store.config.collectionPrefix
                        eid = coll_name[len(prefix):] if coll_name.startswith(prefix) else coll_name
                        self._vector_store.deleteEnterpriseData(eid)
                    except Exception:
                        pass
            except Exception:
                pass
            
            from collections import defaultdict
            grouped = defaultdict(list)
            for item in self._items:
                if not self._is_runtime_available_item(item):
                    continue
                enterprise_id = self._normalize_enterprise_id(item.enterprise_id)
                grouped[enterprise_id].append(self._build_vector_document(item))
            
            synced_count = 0
            target_collections = []
            for enterprise_id, source_docs in grouped.items():
                if not source_docs:
                    continue
                self._vector_store.addDocuments(
                    enterpriseId=enterprise_id,
                    documents=source_docs,
                    batchSize=50
                )
                synced_count += len(source_docs)
                target_collections.append(f"enterprise_{enterprise_id}")
            
            self._vector_enabled = True
            
            return {
                "success": True,
                "sync_mode": "full",
                "synced_count": synced_count,
                "upserted_count": synced_count,
                "deleted_count": 0,
                "total_count": runtime_knowledge_count,
                "target_collections": target_collections,
            }
        except Exception as e:
            logger.error(f"重新同步向量数据库失败: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def resync_to_vector(self, force_full: bool = False) -> Dict[str, Any]:
        """
        同步知识到向量数据库。

        默认执行增量 add/update/delete；当缺少本地同步清单或显式要求时，回退到全量重建。
        """
        self._ensure_vector_store()
        runtime_knowledge_count = len([item for item in self._items if self._is_runtime_available_item(item)])
        if not self._vector_store:
            return {
                "success": False,
                "message": "向量数据库未初始化"
            }

        previous_manifest = {} if force_full else self._load_vector_manifest()
        if force_full or not previous_manifest:
            result = self._full_resync_to_vector()
            if result.get("success"):
                self._save_vector_manifest(self._build_runtime_vector_manifest())
                result.setdefault("sync_reason", "forced_full" if force_full else "manifest_missing")
            return result

        current_manifest = self._build_runtime_vector_manifest()
        previous_ids = set(previous_manifest.keys())
        current_ids = set(current_manifest.keys())

        delete_ops: List[Tuple[str, str]] = []
        upsert_ids: List[str] = []

        for item_id in sorted(previous_ids - current_ids):
            prev = previous_manifest.get(item_id) or {}
            delete_ops.append((str(prev.get("enterprise_id") or self.DEFAULT_ENTERPRISE_ID), item_id))

        for item_id in sorted(current_ids):
            current_entry = current_manifest[item_id]
            previous_entry = previous_manifest.get(item_id)
            if not previous_entry:
                upsert_ids.append(item_id)
                continue
            if previous_entry.get("enterprise_id") != current_entry.get("enterprise_id"):
                delete_ops.append((str(previous_entry.get("enterprise_id") or self.DEFAULT_ENTERPRISE_ID), item_id))
                upsert_ids.append(item_id)
                continue
            if previous_entry.get("fingerprint") != current_entry.get("fingerprint"):
                upsert_ids.append(item_id)

        grouped_documents: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for item_id in upsert_ids:
            item = self.get_item_by_id(item_id)
            if not item or not self._is_runtime_available_item(item):
                continue
            enterprise_id = self._normalize_enterprise_id(item.enterprise_id)
            grouped_documents[enterprise_id].append(self._build_vector_document(item))

        try:
            failed_delete_ids: List[str] = []
            for enterprise_id, item_id in delete_ops:
                deleted = self._vector_store.deleteDocument(
                    enterpriseId=enterprise_id,
                    documentId=item_id,
                )
                if deleted is False:
                    failed_delete_ids.append(item_id)

            if failed_delete_ids:
                raise RuntimeError(f"vector_delete_failed:{','.join(failed_delete_ids)}")

            upserted_count = 0
            target_collections = []
            failed_upsert_ids: List[str] = []
            for enterprise_id, documents in grouped_documents.items():
                if not documents:
                    continue
                upsert_result = self._vector_store.upsertDocuments(
                    enterpriseId=enterprise_id,
                    documents=documents,
                    batchSize=50,
                )
                if isinstance(upsert_result, dict) and not upsert_result.get("success", True):
                    failed_upsert_ids.extend(str(doc.get("id") or "") for doc in documents if doc.get("id"))
                    raise RuntimeError(
                        f"vector_upsert_failed:{enterprise_id}:{upsert_result.get('error') or upsert_result.get('errors') or 'unknown'}"
                    )
                upserted_count += len(documents)
                target_collections.append(f"enterprise_{enterprise_id}")

            self._vector_enabled = True
            self._save_vector_manifest(current_manifest)
            return {
                "success": True,
                "sync_mode": "incremental",
                "synced_count": upserted_count,
                "upserted_count": upserted_count,
                "deleted_count": len(delete_ops),
                "total_count": runtime_knowledge_count,
                "target_collections": target_collections,
                "failed_delete_ids": [],
                "failed_upsert_ids": [],
            }
        except Exception as e:
            logger.error(f"增量同步向量数据库失败: {e}")
            return {
                "success": False,
                "sync_mode": "incremental",
                "error": str(e),
                "failed_delete_ids": failed_delete_ids if "failed_delete_ids" in locals() else [],
                "failed_upsert_ids": failed_upsert_ids if "failed_upsert_ids" in locals() else [],
            }


from functools import lru_cache

@lru_cache(maxsize=1)
def _create_unified_knowledge_service(data_path: Path = None) -> UnifiedKnowledgeService:
    return UnifiedKnowledgeService(data_path)


def get_unified_knowledge_service(data_path: Path = None, reset: bool = False) -> UnifiedKnowledgeService:
    """
    获取统一知识库服务实例

    .. deprecated::
        此工厂函数已被 ``get_knowledge_service()`` 取代。
        新代码请使用 ``from src.common.knowledge_service_adapter import get_knowledge_service`` 。
        本函数保留仅为向后兼容，将在未来版本移除。
    """
    import warnings as _w
    _w.warn(
        "get_unified_knowledge_service() 已废弃，请使用 get_knowledge_service() 替代。"
        " 迁移指南: from src.common.knowledge_service_adapter import get_knowledge_service",
        DeprecationWarning,
        stacklevel=2,
    )
    if reset:
        _create_unified_knowledge_service.cache_clear()
    service = _create_unified_knowledge_service(data_path)
    if data_path is None:
        expected_data_path = Path(get_data_dir())
        current_data_path = Path(getattr(service, "data_path", expected_data_path))
        if current_data_path != expected_data_path:
            _create_unified_knowledge_service.cache_clear()
            service = _create_unified_knowledge_service(None)
            current_data_path = Path(getattr(service, "data_path", expected_data_path))
        if not getattr(service, "_items", None):
            try:
                service._load_data()
            except Exception:
                pass
        fallback_data_path = Path(get_base_dir()) / "data"
        fallback_kb_path = fallback_data_path / "knowledge_base.json"
        if (
            not getattr(service, "_items", None)
            and current_data_path != fallback_data_path
            and fallback_kb_path.exists()
        ):
            _create_unified_knowledge_service.cache_clear()
            service = _create_unified_knowledge_service(fallback_data_path)
    return service


def reset_unified_knowledge_service():
    _create_unified_knowledge_service.cache_clear()
    logger.info("统一知识库服务实例已重置")
