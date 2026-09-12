from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, List

from loguru import logger
from src.common.monitoring import get_metrics


class OutboundIdempotencyService:
    """出站消息幂等服务——统一管理发送去重与回显检测。

    职责：
    1. 记录已发送消息（mark_sent）
    2. 检测回显/自回复（is_echo / is_self_reply）
    3. 过期清理
    """

    def __init__(
        self,
        *,
        max_cache_size: int = 500,
        echo_ttl_short: int = 60,
        echo_ttl_long: int = 600,
        entry_ttl: int = 7200,
    ):
        self._cache: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._max_cache_size = max_cache_size
        self._echo_ttl_short = echo_ttl_short
        self._echo_ttl_long = echo_ttl_long
        self._entry_ttl = entry_ttl

    @staticmethod
    def _normalize_platform(platform: str = "douyin") -> str:
        normalized = str(platform or "douyin").strip()
        return normalized or "douyin"

    @staticmethod
    def _normalize_customer_name(customer_name: str) -> str:
        return str(customer_name or "").strip()

    @staticmethod
    def _normalize_customer_id(customer_id: str = "") -> str:
        return str(customer_id or "").strip()

    @staticmethod
    def _normalize_conversation_id(conversation_id: str = "") -> str:
        return str(conversation_id or "").strip()

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.md5(str(content or "").encode("utf-8")).hexdigest()[:16]

    def _build_cache_key(
        self,
        *,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> str:
        normalized_platform = self._normalize_platform(platform)
        normalized_conversation_id = self._normalize_conversation_id(conversation_id) or "unresolved_conversation"
        normalized_customer_id = self._normalize_customer_id(customer_id) or "unknown_customer"
        normalized_customer_name = self._normalize_customer_name(customer_name) or "unknown_name"
        return (
            f"outbound:{normalized_platform}:{normalized_conversation_id}:"
            f"{normalized_customer_id}:{normalized_customer_name}:{self._content_hash(content)}"
        )

    def _make_entry(
        self,
        *,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        sent_at: float | None = None,
    ) -> Dict[str, Any]:
        return {
            "time": float(sent_at if sent_at is not None else time.time()),
            "content": str(content or ""),
            "customer_name": self._normalize_customer_name(customer_name),
            "conversation_id": self._normalize_conversation_id(conversation_id),
            "customer_id": self._normalize_customer_id(customer_id),
            "platform": self._normalize_platform(platform),
            "content_hash": self._content_hash(content),
        }

    def _matches_identity(
        self,
        entry: Dict[str, Any],
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        entry_platform = self._normalize_platform(entry.get("platform", "douyin"))
        if entry_platform != self._normalize_platform(platform):
            return False

        normalized_conversation_id = self._normalize_conversation_id(conversation_id)
        entry_conversation_id = self._normalize_conversation_id(entry.get("conversation_id", ""))
        if normalized_conversation_id:
            return entry_conversation_id == normalized_conversation_id

        normalized_customer_id = self._normalize_customer_id(customer_id)
        entry_customer_id = self._normalize_customer_id(entry.get("customer_id", ""))
        if normalized_customer_id and entry_customer_id:
            return entry_customer_id == normalized_customer_id

        normalized_customer_name = self._normalize_customer_name(customer_name)
        entry_customer_name = self._normalize_customer_name(entry.get("customer_name", ""))
        return bool(normalized_customer_name) and entry_customer_name == normalized_customer_name

    def mark_sent(
        self,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> None:
        with self._lock:
            sent_key = self._build_cache_key(
                customer_name=customer_name,
                content=content,
                conversation_id=conversation_id,
                customer_id=customer_id,
                platform=platform,
            )
            existing = self._cache.get(sent_key)
            if isinstance(existing, dict):
                existing.update(
                    self._make_entry(
                        customer_name=customer_name,
                        content=content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                    )
                )
            else:
                self._cache[sent_key] = self._make_entry(
                    customer_name=customer_name,
                    content=content,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                )
            self._evict_if_needed()

    def is_echo(
        self,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        with self._lock:
            current_time = time.time()
            content_hash = self._content_hash(content)
            ttl = self._echo_ttl_short if len(content) <= 10 else self._echo_ttl_long
            for entry in self._cache.values():
                if not isinstance(entry, dict):
                    continue
                sent_time = float(entry.get("time", 0) or 0)
                if current_time - sent_time >= ttl:
                    continue
                if entry.get("content_hash") != content_hash:
                    continue
                if not self._matches_identity(
                    entry,
                    customer_name=customer_name,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                ):
                    continue
                get_metrics().record_outbound_idempotency_hit("echo_exact")
                return True
            self._evict_expired(current_time)
        return False

    def is_self_reply(
        self,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        self_reply_prefixes = [
            f"{customer_name}您好！",
            f"{customer_name}您好~",
        ]
        with self._lock:
            current_time = time.time()
            for prefix in self_reply_prefixes:
                if content.startswith(prefix) and len(content) > 15:
                    for value in self._cache.values():
                        if not isinstance(value, dict):
                            continue
                        if not self._matches_identity(
                            value,
                            customer_name=customer_name,
                            conversation_id=conversation_id,
                            customer_id=customer_id,
                            platform=platform,
                        ):
                            continue
                        sent_content = value.get("content", "")
                        if sent_content and content[:30] == sent_content[:30]:
                            get_metrics().record_outbound_idempotency_hit("self_reply_prefix")
                            return True
            for value in list(self._cache.values()):
                if not isinstance(value, dict):
                    continue
                if not self._matches_identity(
                    value,
                    customer_name=customer_name,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                ):
                    continue
                sent_time = value.get("time", 0)
                if current_time - sent_time > self._echo_ttl_long:
                    continue
                sent_content = value.get("content", "")
                if not sent_content or len(sent_content) < 10:
                    continue
                if content in sent_content and len(content) >= len(sent_content) * 0.5:
                    has_question_signal = any(q in content for q in ('？', '?', '吗', '呢', '怎么', '如何', '什么', '为什么', '哪', '多少', '能否', '可以', '能不能', '好不好'))
                    if not has_question_signal:
                        logger.info(f"自回复片段检测: 入站内容是已发送消息的子串(长度占比{len(content)/len(sent_content):.0%})，跳过: {customer_name}")
                        get_metrics().record_outbound_idempotency_hit("self_reply_substring")
                        return True
                if len(content) > 15 and sent_content.startswith(content[:15]):
                    prefix_ratio = len(content) / len(sent_content)
                    if prefix_ratio > 0.85:
                        has_question_signal = any(q in content for q in ('？', '?', '吗', '呢', '怎么', '如何', '什么', '为什么', '哪', '多少', '能否', '可以', '能不能', '好不好'))
                        if not has_question_signal:
                            logger.info(f"自回复片段检测: 入站内容与已发送消息前缀匹配(占比{prefix_ratio:.0%})，跳过: {customer_name}")
                            get_metrics().record_outbound_idempotency_hit("self_reply_prefix_similarity")
                            return True
        return False

    def get_recent_entries(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            current_time = time.time()
            matches = [
                dict(entry)
                for entry in self._cache.values()
                if isinstance(entry, dict)
                and current_time - float(entry.get("time", 0) or 0) <= self._entry_ttl
                and self._matches_identity(
                    entry,
                    customer_name=customer_name,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                )
            ]
        matches.sort(key=lambda item: float(item.get("time", 0) or 0), reverse=True)
        return matches[: max(int(limit or 0), 0)]

    def get_cache_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._cache)

    def restore_cache(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            if not isinstance(snapshot, dict):
                return
            current_time = time.time()
            validated = {}
            for k, v in snapshot.items():
                if isinstance(v, dict):
                    if "time" in v and isinstance(v["time"], (int, float)):
                        if current_time - v["time"] <= self._entry_ttl:
                            entry = dict(v)
                            if "customer_name" not in entry:
                                key_prefix = str(k).split(":", 1)[0]
                                entry["customer_name"] = key_prefix
                            entry.setdefault("conversation_id", "")
                            entry.setdefault("customer_id", "")
                            entry.setdefault("platform", "douyin")
                            entry.setdefault("content_hash", self._content_hash(entry.get("content", "")))
                            normalized_key = str(k)
                            if not normalized_key.startswith("outbound:"):
                                normalized_key = self._build_cache_key(
                                    customer_name=entry.get("customer_name", ""),
                                    content=entry.get("content", ""),
                                    conversation_id=entry.get("conversation_id", ""),
                                    customer_id=entry.get("customer_id", ""),
                                    platform=entry.get("platform", "douyin"),
                                )
                            validated[normalized_key] = entry
                elif isinstance(v, (int, float)):
                    if current_time - v <= self._entry_ttl:
                        validated[k] = v
            self._cache.update(validated)

    def _evict_if_needed(self) -> None:
        if len(self._cache) <= self._max_cache_size:
            return
        current_time = time.time()
        expired = [
            k for k, v in self._cache.items()
            if (isinstance(v, dict) and current_time - v.get("time", 0) > self._entry_ttl)
            or (isinstance(v, (int, float)) and current_time - v > self._entry_ttl)
        ]
        for k in expired:
            del self._cache[k]
        if len(self._cache) > self._max_cache_size:
            sorted_items = sorted(
                self._cache.items(),
                key=lambda x: x[1].get("time", 0) if isinstance(x[1], dict) else (x[1] if isinstance(x[1], (int, float)) else 0),
            )
            remove_count = len(self._cache) - self._max_cache_size // 2
            for k, _ in sorted_items[:remove_count]:
                del self._cache[k]
            logger.debug(f"已清理发送缓存，移除{remove_count}条，剩余{len(self._cache)}条")

    def _evict_expired(self, current_time: float) -> None:
        expired = [
            k for k, v in self._cache.items()
            if (isinstance(v, dict) and current_time - v.get("time", 0) > self._entry_ttl)
            or (isinstance(v, (int, float)) and current_time - v > self._entry_ttl)
        ]
        for k in expired:
            del self._cache[k]
