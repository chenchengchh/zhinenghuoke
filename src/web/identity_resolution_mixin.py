"""
IdentityResolutionMixin - 身份解析与归并相关方法

从 BotService 中提取的身份解析方法集合，包含：
1. 客户名解析（_resolve_customer_name_from_conversation）
2. 名称规范化（_normalize_live_target_name）
3. 持久化状态加载（_load_persisted_live_state_sources）
4. Live会话名采样（_get_live_conversation_name_samples）
5. 发送目标候选收集（_collect_send_target_candidates）
6. Live发送目标名解析（_resolve_live_send_target_name）
7. 身份归并（_maybe_merge_identity_conversations）
8. 会话快照（_get_conversation_snapshot）
9. 会话ID生成与解析（_make_conversation_id / _resolve_conversation_id）
10. 逻辑消息ID构建（_build_logical_message_id）
"""
from typing import Any, Dict, List, Optional

from loguru import logger

from src.common.conversation_id import build_conversation_id, resolve_conversation_id
from src.common.logical_message import build_logical_message_id


class IdentityResolutionMixin:
    """身份解析 Mixin —— 从 BotService 提取的身份解析与归并相关方法。"""

    def _resolve_customer_name_from_conversation(self, conversation_id: str) -> str:
        """根据会话补全客户名，供营销 tracking 状态回写使用。"""
        if not conversation_id:
            return ""

        try:
            for conversation in reversed(self._get_chat_store().get_all_conversations_dicts() or []):
                if conversation.get("conversation_id") != conversation_id:
                    continue
                candidate = (
                    conversation.get("customer_name")
                    or conversation.get("name")
                    or conversation.get("nickname")
                    or conversation.get("customer_id")
                    or ""
                ).strip()
                if candidate:
                    return candidate
        except Exception as conv_e:
            logger.debug(f"通过会话查找客户名失败: {conv_e}")

        try:
            for message in reversed(self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=20) or []):
                candidate = (
                    message.get("customer_name")
                    or message.get("sender_name")
                    or message.get("nickname")
                    or message.get("customer_id")
                    or ""
                ).strip()
                if candidate and candidate not in {"我", "self"}:
                    return candidate
        except Exception as msg_e:
            logger.debug(f"通过消息历史查找客户名失败: {msg_e}")

        return ""

    @staticmethod
    def _normalize_live_target_name(name: str) -> str:
        text = str(name or "").strip().lower()
        if text.startswith("@"):
            text = text[1:]
        return text.replace(" ", "").replace("\u200b", "")

    def _load_persisted_live_state_sources(self) -> List[Dict[str, Any]]:
        sources: List[Dict[str, Any]] = []
        try:
            persistor = getattr(self, "_state_persistor", None)
            if persistor is None:
                from src.douyin_bot.app_state_persistor import get_app_state_persistor

                persistor = get_app_state_persistor()
            if not persistor:
                return sources

            load_rpa_state = getattr(persistor, "load_rpa_engine_state", None)
            if callable(load_rpa_state):
                rpa_state: Any = load_rpa_state() or {}
                if isinstance(rpa_state.get("states"), dict) and rpa_state["states"]:
                    sources.append(rpa_state["states"])

            load_monitor_state = getattr(persistor, "load_message_monitor_state", None)
            if callable(load_monitor_state):
                monitor_state: Any = load_monitor_state() or {}
                conversation_states = monitor_state.get("conversation_states")
                if isinstance(conversation_states, dict) and conversation_states:
                    sources.append(conversation_states)
        except Exception as persisted_e:
            logger.debug(f"读取持久化 live 状态失败: {persisted_e}")
        return sources

    def _get_live_conversation_name_samples(self, limit: int = 12) -> List[str]:
        samples: List[str] = []
        seen = set()
        sources = []
        try:
            rpa_engine = getattr(getattr(self, "rpa_launcher", None), "rpa_engine", None)
            if rpa_engine:
                sources.append(getattr(rpa_engine, "_states", {}) or {})
        except Exception as rpa_e:
            logger.debug(f"收集 RPA live 会话名失败: {rpa_e}")
        try:
            if getattr(self, "message_monitor", None):
                sources.append(getattr(self.message_monitor, "_conversation_states", {}) or {})
        except Exception as monitor_e:
            logger.debug(f"收集 monitor live 会话名失败: {monitor_e}")
        if not sources:
            sources.extend(self._load_persisted_live_state_sources())

        for source in sources:
            for raw_name, state in source.items():
                candidates = [raw_name]
                if isinstance(state, dict):
                    candidates.extend(state.get("aliases", []) or [])
                    candidates.append(state.get("customer_id", ""))
                for candidate in candidates:
                    name = str(candidate or "").strip()
                    normalized = self._normalize_live_target_name(name)
                    if not name or not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    samples.append(name)
                    if len(samples) >= limit:
                        return samples
        return samples

    def _collect_send_target_candidates(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> List[str]:
        candidates: List[str] = []
        seen = set()

        def _add(candidate: str):
            text = str(candidate or "").strip()
            if not text:
                return
            normalized = self._normalize_live_target_name(text)
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append(text)

        _add(customer_name)
        if customer_id and not str(customer_id).startswith("temp_"):
            _add(customer_id)

        try:
            all_conversations = self._get_chat_store().get_all_conversations_dicts() or []
        except Exception as conv_e:
            logger.debug(f"收集发送目标候选时读取会话失败: {conv_e}")
            all_conversations = []

        if conversation_id:
            for conversation in reversed(all_conversations):
                if str(conversation.get("conversation_id", "")).strip() != str(conversation_id).strip():
                    continue
                _add(conversation.get("customer_name", ""))
                _add(conversation.get("name", ""))
                _add(conversation.get("nickname", ""))
                _add(conversation.get("customer_id", ""))
                break
            try:
                for message in reversed(self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=20) or []):
                    _add(message.get("customer_name", ""))
                    _add(message.get("sender_name", ""))
                    _add(message.get("nickname", ""))
                    _add(message.get("customer_id", ""))
            except Exception as msg_e:
                logger.debug(f"收集发送目标候选时读取消息历史失败: {msg_e}")

        if customer_id:
            for conversation in reversed(all_conversations):
                if str(conversation.get("platform", "douyin")).strip() != str(platform or "douyin").strip():
                    continue
                if str(conversation.get("customer_id", "")).strip() != str(customer_id).strip():
                    continue
                _add(conversation.get("customer_name", ""))
                _add(conversation.get("name", ""))
                _add(conversation.get("nickname", ""))

        return candidates

    def _resolve_live_send_target_name(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> tuple[str, str]:
        live_names = self._get_live_conversation_name_samples(limit=20)
        if not live_names:
            return (str(customer_name or "").strip(), "original")

        normalized_live_pairs = [
            (self._normalize_live_target_name(live_name), live_name)
            for live_name in live_names
            if str(live_name or "").strip()
        ]

        candidates = self._collect_send_target_candidates(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        for candidate in candidates:
            normalized_candidate = self._normalize_live_target_name(candidate)
            if not normalized_candidate:
                continue
            for normalized_live_name, live_name in normalized_live_pairs:
                if normalized_live_name == normalized_candidate:
                    source = "original" if candidate == customer_name else "alias"
                    return live_name, source
            for normalized_live_name, live_name in normalized_live_pairs:
                if not normalized_live_name:
                    continue
                if (
                    normalized_live_name.startswith(normalized_candidate)
                    or normalized_candidate.startswith(normalized_live_name)
                ):
                    source = "original" if candidate == customer_name else "alias"
                    return live_name, source

        return (str(customer_name or "").strip(), "original")

    def _maybe_merge_identity_conversations(
        self,
        *,
        customer_name: str,
        customer_id: str = "",
        platform: str = "douyin",
        conversation_id: str = "",
    ) -> str:
        normalized_customer_id = str(customer_id or "").strip()
        normalized_platform = str(platform or "douyin").strip() or "douyin"
        primary_conversation_id = str(conversation_id or "").strip()
        if not normalized_customer_id or normalized_customer_id.startswith("temp_"):
            return primary_conversation_id
        merge_method = getattr(self.db, "merge_customer_conversations_by_customer_id", None)
        if not callable(merge_method):
            return primary_conversation_id
        try:
            merge_result = merge_method(
                customer_id=normalized_customer_id,
                platform=normalized_platform,
                primary_conversation_id=primary_conversation_id,
                preferred_customer_name=str(customer_name or "").strip(),
            ) or {}
            merge_result_typed: Dict[str, Any] = merge_result if isinstance(merge_result, dict) else {}  # type: ignore[assignment]
            merged_conversation_id = str(
                merge_result_typed.get("primary_conversation_id") or primary_conversation_id
            ).strip()
            alias_ids = merge_result_typed.get("alias_ids", []) or []
            if merge_result_typed.get("merged") and alias_ids:
                logger.info(
                    f"按稳定身份归并会话完成: customer_id={normalized_customer_id}, "
                    f"primary={merged_conversation_id}, aliases={alias_ids}"
                )
            return merged_conversation_id or primary_conversation_id
        except Exception as merge_e:
            logger.debug(f"按稳定身份归并会话失败: {merge_e}")
            return primary_conversation_id

    def _get_conversation_snapshot(self, conversation_id: str) -> Dict[str, Any]:
        normalized_conversation_id = str(conversation_id or "").strip()
        if not normalized_conversation_id:
            return {}
        try:
            for conversation in reversed(self._get_chat_store().get_all_conversations_dicts() or []):
                if str(conversation.get("conversation_id", "")).strip() == normalized_conversation_id:
                    return conversation
        except Exception as conv_e:
            logger.debug(f"读取会话快照失败: {conv_e}")
        return {}

    @staticmethod
    def _make_conversation_id(
        customer_name: str,
        platform: str = "douyin",
        customer_id: str = "",
    ) -> str:
        """生成稳定会话ID，优先使用平台客户ID，缺失时退回昵称哈希。"""
        return build_conversation_id(customer_name, platform, customer_id)

    def _resolve_conversation_id(
        self,
        customer_name: str,
        platform: str = "douyin",
        customer_id: str = "",
        explicit_conversation_id: str = "",
    ) -> str:
        """兼容历史数据，优先显式会话ID，其次沿用已有旧会话。"""
        return resolve_conversation_id(
            db=getattr(self, "db", None),
            customer_name=customer_name,
            platform=platform,
            customer_id=customer_id,
            explicit_conversation_id=explicit_conversation_id,
        )

    @staticmethod
    def _build_logical_message_id(**kwargs) -> str:
        return build_logical_message_id(**kwargs)
