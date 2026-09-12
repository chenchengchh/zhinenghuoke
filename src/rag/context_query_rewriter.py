"""
上下文感知查询改写器

将当前查询与对话历史合并，生成独立的完整查询
解决多轮对话中指代消解和上下文丢失问题

原理：
1. 获取最近N轮对话历史
2. LLM将当前查询与历史合并，生成独立查询
3. 改写后的查询可直接用于检索，无需对话上下文

示例：
  历史: ["产品介绍"] + 当前: ["它多少钱"] → 改写: "产品价格多少钱"
"""

import re
from loguru import logger
from typing import List, Dict, Optional, Any
from dataclasses import dataclass



@dataclass
class RewrittenQuery:
    """改写后的查询"""
    original_query: str
    rewritten_query: str
    confidence: float = 1.0
    method: str = "context_aware"


class ContextAwareQueryRewriter:
    """
    上下文感知查询改写器

    支持两种模式：
    1. LLM模式：使用LLM进行智能改写（高质量）
    2. 规则模式：基于指代消解规则的快速改写（低延迟）
    """

    SHORT_QUERY_THRESHOLD = 10
    DEFAULT_GENERIC_TOPIC_LABELS = {
        "价格", "产品", "服务", "流程", "实施", "效果", "规则", "公司", "联系", "支持"
    }
    DEFAULT_SPECIFIC_ANCHOR_PATTERNS = (
        r"([\u4e00-\u9fa5]{2,16}(?:一日游|纯玩|天坑地缝|天坑|地缝))",
        r"(基础版|专业版|企业版|旗舰版|标准版|高级版)",
        r"([A-Za-z0-9\u4e00-\u9fa5]{2,20}(?:套餐|版本|方案|产品|服务包|模块))",
    )

    PRONOUN_MAP = {
        "他": ["人", "先生", "女士", "经理", "客服"],
        "她": ["人", "先生", "女士", "经理", "客服"],
        "它": ["产品", "系统", "软件", "平台", "工具", "功能", "服务", "方案"],
        "这个": ["产品", "系统", "功能", "方案", "价格", "服务", "平台"],
        "那个": ["产品", "系统", "功能", "方案", "价格", "服务", "平台"],
        "这些": ["功能", "产品", "服务", "方案"],
        "那些": ["功能", "产品", "服务", "方案"],
        "此": ["产品", "系统", "功能"],
        "其": ["产品", "系统", "功能"],
    }
    ANCHOR_PREFIXES = ("我更推荐", "更推荐", "推荐", "建议选", "建议", "可以选", "选", "走")

    DEFAULT_TOPIC_KEYWORDS = {
        "价格": ["价格", "多少钱", "费用", "收费", "报价", "定价", "预算"],
        "产品": ["产品", "系统", "软件", "平台", "工具", "功能", "模块", "方案", "介绍"],
        "服务": ["服务", "售后", "支持", "培训", "教程", "维护", "咨询", "客服"],
        "流程": ["流程", "步骤", "如何开始", "怎么开通", "怎么用", "如何部署", "怎么安排"],
        "实施": ["实施", "部署", "上线", "交付", "对接", "配置", "培训"],
        "效果": ["效果", "收益", "价值", "提升", "适合", "案例"],
        "规则": ["规则", "政策", "条款", "退款", "变更", "限制", "协议"],
        "公司": ["公司", "团队", "品牌", "资质", "案例", "经验"],
        "联系": ["联系", "怎么联系", "联系方式", "电话", "微信", "邮箱", "客服", "找谁"],
    }

    def __init__(self, llm_service=None, max_history_turns: int = 3):
        """
        初始化查询改写器

        Args:
            llm_service: LLM服务实例
            max_history_turns: 最大使用历史轮数
        """
        self.llm_service = llm_service
        self.max_history_turns = max_history_turns

    def _get_active_schema(self, grounded_context: Optional[Dict[str, Any]] = None):
        grounded_context = grounded_context or {}
        try:
            from src.common.industry_schema_service import IndustrySchemaService
            return IndustrySchemaService().get_active_schema(
                enterprise_id=str(grounded_context.get("enterprise_id") or ""),
                preferred_schema_id=str(grounded_context.get("schema_id") or ""),
            ) or {}
        except Exception:
            return {}

    def _get_anchor_patterns(self, grounded_context: Optional[Dict[str, Any]] = None):
        schema = self._get_active_schema(grounded_context)
        try:
            metadata = schema.get("metadata", {}) or {}
            rewrite_cfg = metadata.get("query_rewriting", {}) or {}
            configured_patterns = rewrite_cfg.get("specific_anchor_patterns") or []
            if configured_patterns:
                return tuple(str(pattern) for pattern in configured_patterns if str(pattern).strip())
            anchor_terms = metadata.get("followup_strategy", {}).get("anchor_terms") or []
            if anchor_terms:
                escaped = [re.escape(term) for term in anchor_terms]
                return (r"(" + "|".join(escaped) + ")",)
        except Exception:
            pass
        return tuple(self.DEFAULT_SPECIFIC_ANCHOR_PATTERNS)

    def _get_generic_topic_labels(self, grounded_context: Optional[Dict[str, Any]] = None) -> set[str]:
        schema = self._get_active_schema(grounded_context)
        try:
            metadata = schema.get("metadata", {}) or {}
            rewrite_cfg = metadata.get("query_rewriting", {}) or {}
            labels = rewrite_cfg.get("generic_topic_labels") or []
            normalized = {str(label).strip() for label in labels if str(label).strip()}
            if normalized:
                return normalized
        except Exception:
            pass
        return set(self.DEFAULT_GENERIC_TOPIC_LABELS)

    def _get_topic_keywords(self, grounded_context: Optional[Dict[str, Any]] = None) -> Dict[str, List[str]]:
        schema = self._get_active_schema(grounded_context)
        try:
            metadata = schema.get("metadata", {}) or {}
            rewrite_cfg = metadata.get("query_rewriting", {}) or {}
            topic_keywords = rewrite_cfg.get("topic_keywords") or {}
            if isinstance(topic_keywords, dict) and topic_keywords:
                normalized: Dict[str, List[str]] = {}
                for topic, keywords in topic_keywords.items():
                    values = [str(keyword).strip() for keyword in (keywords or []) if str(keyword).strip()]
                    if values:
                        normalized[str(topic)] = values
                if normalized:
                    return normalized
        except Exception:
            pass
        return {key: list(values) for key, values in self.DEFAULT_TOPIC_KEYWORDS.items()}

    def _call_llm(self, prompt: str) -> str:
        """兼容不同LLM服务接口的统一调用方法（委托给call_llm_safe）"""
        from src.common.utils import call_llm_safe
        from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS
        return call_llm_safe(self.llm_service, prompt, timeout=REPLY_LLM_TIMEOUT_SECONDS)

    def rewrite(
        self,
        query: str,
        conversation_history: Optional[List[Dict]] = None,
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> RewrittenQuery:
        """
        改写查询

        Args:
            query: 当前查询
            conversation_history: 对话历史

        Returns:
            改写后的查询
        """
        if not query or not query.strip():
            return RewrittenQuery(
                original_query=query or "",
                rewritten_query=query or "",
                method="empty_input"
            )

        grounded_context = grounded_context or {}
        effective_history = self._merge_grounded_context_into_history(
            conversation_history,
            grounded_context,
        )

        if not effective_history:
            return RewrittenQuery(
                original_query=query,
                rewritten_query=query,
                method="no_context"
            )

        recent_history = self._get_recent_history(effective_history)
        if not recent_history:
            return RewrittenQuery(
                original_query=query,
                rewritten_query=query,
                method="no_history"
            )

        has_pronoun = self._has_pronoun(query)
        is_short_query = len(query) <= self.SHORT_QUERY_THRESHOLD
        grounded_routes = [
            str(route_name).strip()
            for route_name in (grounded_context.get("routes") or [])
            if str(route_name).strip()
        ]
        looks_like_context_followup = self._looks_like_context_dependent_query(query)

        needs_short_query_rewrite = is_short_query and self._looks_like_short_elliptical_query(query)

        if not has_pronoun and not needs_short_query_rewrite and not (grounded_routes and looks_like_context_followup):
            return RewrittenQuery(
                original_query=query,
                rewritten_query=query,
                method="no_rewrite_needed"
            )

        if self.llm_service and (has_pronoun or needs_short_query_rewrite or (grounded_routes and looks_like_context_followup)):
            return self._llm_rewrite(query, recent_history, grounded_context=grounded_context)

        return self._rule_rewrite(query, recent_history, grounded_context=grounded_context)

    def _merge_grounded_context_into_history(
        self,
        history: Optional[List[Dict]],
        grounded_context: Dict[str, Any],
    ) -> List[Dict]:
        merged_history = list(history or [])
        grounded_routes = [
            str(route_name).strip()
            for route_name in (grounded_context.get("routes") or [])
            if str(route_name).strip()
        ]
        recommended_route = str(grounded_context.get("recommended_route") or "").strip()
        comparison_summary = str(grounded_context.get("comparison_summary") or "").strip()

        if not grounded_routes and not recommended_route and not comparison_summary:
            return merged_history

        grounded_lines = []
        if grounded_routes:
            grounded_lines.append(f"最近明确线路：{'、'.join(grounded_routes[:3])}")
        if recommended_route:
            grounded_lines.append(f"最近推荐线路：{recommended_route}")
        if comparison_summary:
            grounded_lines.append(f"最近知识结论：{comparison_summary}")

        if grounded_lines:
            merged_history.append(
                {
                    "direction": "outbound",
                    "content": "；".join(grounded_lines),
                    "source": "grounded_context",
                }
            )
        return merged_history

    def _get_recent_history(self, history: List[Dict]) -> List[Dict]:
        """获取最近的对话历史"""
        recent = history[-self.max_history_turns * 2:] if history else []
        return recent

    def _has_pronoun(self, text: str) -> bool:
        """检查文本中是否包含指代词"""
        for pronoun in self.PRONOUN_MAP:
            if pronoun in text:
                return True
        return False

    def _looks_like_context_dependent_query(self, text: str) -> bool:
        normalized = str(text or "").strip()
        followup_terms = (
            "适合哪个", "选哪个", "哪个好", "怎么选", "更稳", "值不值", "值不值得",
            "包含什么", "哪些费用", "怎么落地", "怎么实施", "多久上线",
            "多久交付", "有什么限制", "怎么对接", "是否支持",
        )
        return any(term in normalized for term in followup_terms)

    def _looks_like_standalone_query(self, text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        if self._has_pronoun(normalized):
            return False
        if self._looks_like_context_dependent_query(normalized):
            return False
        if self._extract_specific_anchor_from_text(normalized):
            return True
        standalone_patterns = (
            r".{2,}的.{1,}(是|有|在|叫).{0,6}(哪里|哪儿|哪个|哪位|什么|多少|几|为什么|为何|怎么|如何)",
            r".{3,}(是什么|是哪里|在哪里|叫什么|有哪些|有多少|为什么|怎么|如何|什么意思)",
        )
        return any(re.search(pattern, normalized) for pattern in standalone_patterns)

    def _looks_like_short_elliptical_query(self, text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized or self._looks_like_standalone_query(normalized):
            return False
        if self._looks_like_context_dependent_query(normalized):
            return True
        elliptical_patterns = (
            r"^(多少钱|多少|价格|费用|报价)",
            r"^(几点|多久|多长时间|什么时候)",
            r"^(怎么|如何)(联系|安排|选|开通|部署|实施|对接|使用)",
            r"^(包含|包括|包不包|有没有|有吗|能不能|可以吗)",
            r"^(值不值|值不值得|适不适合)",
        )
        return any(re.search(pattern, normalized) for pattern in elliptical_patterns)

    def _llm_rewrite(
        self,
        query: str,
        history: List[Dict],
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> RewrittenQuery:
        """
        使用LLM改写查询

        Args:
            query: 当前查询
            history: 最近对话历史

        Returns:
            改写后的查询
        """
        history_text = self._format_history(history)

        prompt = f"""基于以下对话历史，将用户的最新问题改写为一个独立的、完整的查询。
只输出改写后的查询，不要解释，不要加引号。

对话历史：
{history_text}

最新问题：{query}

改写后的独立查询："""

        try:
            response = self._call_llm(prompt)
            if response:
                rewritten = response.strip().strip('"\'""''')
                LLM_ERROR_PATTERNS = [
                    '感谢您的咨询', '请稍后', '人工客服', '无法回答',
                    '抱歉', '我不能', 'I cannot', '作为AI',
                    '请稍候', '系统繁忙', '服务不可用', '404',
                    '请求失败', '连接超时', '模型加载失败',
                ]
                is_error_response = any(p in rewritten for p in LLM_ERROR_PATTERNS)
                if is_error_response:
                    logger.warning(f"LLM返回错误响应，回退到规则改写: '{rewritten[:50]}'")
                    return self._rule_rewrite(query, history, grounded_context=grounded_context)
                if rewritten and rewritten != query:
                    logger.debug(f"查询改写: '{query}' → '{rewritten}'")
                    return RewrittenQuery(
                        original_query=query,
                        rewritten_query=rewritten,
                        confidence=0.9,
                        method="llm"
                    )
        except Exception as e:
            logger.warning(f"LLM查询改写失败: {e}")

        return self._rule_rewrite(query, history, grounded_context=grounded_context)

    def _rule_rewrite(
        self,
        query: str,
        history: List[Dict],
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> RewrittenQuery:
        """
        基于规则的查询改写

        Args:
            query: 当前查询
            history: 最近对话历史

        Returns:
            改写后的查询
        """
        rewritten = query
        grounded_context = grounded_context or {}
        rewrite_anchor = self._extract_last_anchor(history, grounded_context=grounded_context)
        last_topic = self._extract_last_topic(history, grounded_context=grounded_context)
        if last_topic in self._get_generic_topic_labels(grounded_context):
            last_topic = None
        grounded_routes = [
            str(route_name).strip()
            for route_name in (grounded_context.get("routes") or [])
            if str(route_name).strip()
        ]
        replacement_anchor = (
            rewrite_anchor
            or last_topic
            or str(grounded_context.get("recommended_route") or "").strip()
            or (grounded_routes[0] if grounded_routes else "")
        )

        if replacement_anchor:
            for pronoun, replacements in self.PRONOUN_MAP.items():
                if pronoun in rewritten:
                    if replacement_anchor in replacements:
                        rewritten = rewritten.replace(pronoun, replacement_anchor)
                    else:
                        rewritten = rewritten.replace(pronoun, replacement_anchor)
                    break

            if (
                (len(rewritten) <= self.SHORT_QUERY_THRESHOLD or self._looks_like_context_dependent_query(query))
                and replacement_anchor not in rewritten
            ):
                rewritten = f"{replacement_anchor}{rewritten}"

        if rewritten != query:
            logger.debug(f"规则查询改写: '{query}' → '{rewritten}'")
            return RewrittenQuery(
                original_query=query,
                rewritten_query=rewritten,
                confidence=0.7,
                method="rule"
            )

        return RewrittenQuery(
            original_query=query,
            rewritten_query=query,
            method="no_change"
        )

    def _format_history(self, history: List[Dict]) -> str:
        """格式化对话历史"""
        lines = []
        for msg in history:
            direction = msg.get('direction', 'inbound')
            content = msg.get('content', '')
            role = "用户" if direction == 'inbound' else "助手"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _extract_last_topic(
        self,
        history: List[Dict],
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        从对话历史中提取最近讨论的主题

        Args:
            history: 对话历史

        Returns:
            最近主题关键词
        """
        for msg in reversed(history):
            if msg.get("source") == "grounded_context":
                content = msg.get("content", "")
                schema = self._get_active_schema(grounded_context)
                try:
                    anchor_terms = schema.get("metadata", {}).get("followup_strategy", {}).get("anchor_terms") or []
                except Exception:
                    anchor_terms = []
                route_match = [
                    route_name
                    for route_name in (anchor_terms if anchor_terms else [])
                    if route_name in content
                ]
                if route_match:
                    return route_match[0]
            if msg.get('direction') != 'inbound':
                continue
            content = msg.get('content', '')

            for topic, keywords in self._get_topic_keywords(grounded_context).items():
                for keyword in keywords:
                    if keyword in content:
                        return topic

        for msg in reversed(history):
            if msg.get('direction') == 'inbound':
                content = msg.get('content', '')
                if len(content) >= 2:
                    return content[:4]

        return None

    def _extract_last_anchor(
        self,
        history: List[Dict],
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """优先提取最近的具体实体锚点，避免用“价格/产品/服务”这类主题类别词做改写。"""
        for msg in reversed(history):
            content = str(msg.get("content", "") or "").strip()
            if not content:
                continue
            anchor = self._extract_specific_anchor_from_text(content, grounded_context=grounded_context)
            if anchor:
                return anchor
        return None

    def _extract_specific_anchor_from_text(
        self,
        text: str,
        grounded_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        grounded_context = grounded_context or {}
        schema = self._get_active_schema(grounded_context)
        try:
            anchor_terms = [
                str(item or "").strip()
                for item in (schema.get("metadata", {}).get("followup_strategy", {}).get("anchor_terms") or [])
                if str(item or "").strip()
            ]
        except Exception:
            anchor_terms = []
        explicit_candidates = [
            str(grounded_context.get("recommended_route") or "").strip(),
            *[
                str(route_name).strip()
                for route_name in (grounded_context.get("routes") or [])
                if str(route_name).strip()
            ],
            *anchor_terms,
        ]
        for candidate in sorted({item for item in explicit_candidates if item}, key=len, reverse=True):
            if candidate in normalized:
                return candidate
        all_patterns = list(self.DEFAULT_SPECIFIC_ANCHOR_PATTERNS) + list(
            self._get_anchor_patterns(grounded_context)
        )
        for pattern in all_patterns:
            match = re.search(pattern, normalized)
            if match:
                candidate = str(match.group(1) or "").strip()
                for prefix in self.ANCHOR_PREFIXES:
                    if candidate.startswith(prefix) and len(candidate) > len(prefix) + 1:
                        candidate = candidate[len(prefix):].strip()
                        break
                if candidate and candidate not in self._get_generic_topic_labels(grounded_context):
                    return candidate
        return None


_query_rewriter: Optional[ContextAwareQueryRewriter] = None


def get_query_rewriter(llm_service=None) -> ContextAwareQueryRewriter:
    """获取查询改写器单例"""
    global _query_rewriter
    if _query_rewriter is None:
        _query_rewriter = ContextAwareQueryRewriter(llm_service)
    return _query_rewriter
