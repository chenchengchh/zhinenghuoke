from __future__ import annotations

import hashlib
import threading
import time
from typing import Dict, List, Tuple

from loguru import logger


class InboundIdempotencyService:
    """入站消息幂等服务——统一管理消息去重与处理状态。

    职责：
    1. 构建多维度缓存键（logical_message_id / source_message_id / conversation_id+content / customer_id+content / msg_id）
    2. 判定消息是否已处理（processing / done / skipped / failed）
    3. 标记消息状态（processing / done / skipped / failed）
    4. 过期清理
    """

    _INHERENT_SKIP_REASONS = {"bot_pattern", "invalid", "empty", "outbound", "short_prefix"}
    _ECHO_SKIP_REASONS = {"echo", "self_sent", "duplicate_echo", "db_self_sent"}
    _TRANSIENT_SKIP_REASONS = {"no_session", "empty_reply", "spam_filtered", "duplicate_reply"}

    @staticmethod
    def _is_synthetic_message_id(message_id: str) -> bool:
        normalized = str(message_id or "").strip().lower()
        if not normalized:
            return True
        return normalized.startswith(("dom_", "msg_", "auto_", "synth_"))

    def __init__(
        self,
        *,
        processed_messages_ttl: int = 7200,
        short_message_done_ttl: int = 90,
        short_message_skip_ttl: int = 60,
        short_message_processing_ttl: int = 60,
        failed_message_retry_gate_ttl: int = 60,
        processing_timeout: int = 300,
        max_cache_size: int = 1000,
    ):
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._source_content_hash_map: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._processed_messages_ttl = processed_messages_ttl
        self._short_message_done_ttl = short_message_done_ttl
        self._short_message_skip_ttl = short_message_skip_ttl
        self._short_message_processing_ttl = short_message_processing_ttl
        self._failed_message_retry_gate_ttl = failed_message_retry_gate_ttl
        self._processing_timeout = processing_timeout
        self._max_cache_size = max_cache_size

    @staticmethod
    def _is_short_content(content: str) -> bool:
        return len(str(content or "").strip()) <= 10

    def build_cache_keys(
        self,
        *,
        customer_name: str,
        content: str,
        msg_id: str = "",
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        direction: str = "",
    ) -> List[str]:
        normalized_customer_name = str(customer_name or "").strip()
        normalized_content = str(content or "")
        normalized_msg_id = str(msg_id or "").strip()
        normalized_conversation_id = str(conversation_id or "").strip()
        normalized_customer_id = str(customer_id or "").strip()
        normalized_logical_message_id = str(logical_message_id or "").strip()
        normalized_source_message_id = str(source_message_id or normalized_msg_id or "").strip()
        synthetic_source_message_id = self._is_synthetic_message_id(normalized_source_message_id)
        synthetic_msg_id = self._is_synthetic_message_id(normalized_msg_id)
        dir_suffix = f":{direction.strip().lower()}" if direction and direction.strip() else ""

        content_hash = hashlib.md5(normalized_content.encode("utf-8")).hexdigest()[:16]
        keys: List[str] = []

        def add_key(key: str) -> None:
            normalized_key = str(key or "").strip()
            if normalized_key and normalized_key not in keys:
                keys.append(normalized_key)

        add_key(f"lmid:{normalized_logical_message_id}" if normalized_logical_message_id else "")
        if normalized_source_message_id and not synthetic_source_message_id:
            add_key(f"src:{normalized_source_message_id}")
        if normalized_conversation_id:
            add_key(f"conv:{normalized_conversation_id}:content:{content_hash}{dir_suffix}")
            if normalized_customer_id:
                add_key(f"conv:{normalized_conversation_id}:cust:{normalized_customer_id}:content:{content_hash}{dir_suffix}")
        elif normalized_customer_id:
            add_key(f"cust:{normalized_customer_id}:content:{content_hash}{dir_suffix}")
        if normalized_msg_id and not synthetic_msg_id:
            add_key(f"mid:{normalized_msg_id}")
        if normalized_conversation_id:
            add_key(f"{normalized_customer_name}:conv:{normalized_conversation_id}:{content_hash}{dir_suffix}" if normalized_customer_name else "")
        else:
            add_key(f"{normalized_customer_name}:{content_hash}{dir_suffix}" if normalized_customer_name else "")
        return keys

    def _verify_dom_source_key_with_content(
        self,
        key: str,
        cache_keys: List[str],
        customer_name: str,
        current_time: float,
        content: str = "",
    ) -> bool | None:
        dom_source_keys = {k for k in cache_keys if k.startswith(("src:", "mid:"))}
        if key not in dom_source_keys:
            return None
        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16] if content else ""
        stored_hash = self._source_content_hash_map.get(key, "")
        if stored_hash and content_hash:
            if stored_hash != content_hash:
                logger.info(
                    f"[幂等] DOM source键({key})命中但内容hash不同"
                    f"(stored={stored_hash}, current={content_hash})，"
                    f"视为DOM位置复用的新消息: {customer_name}"
                )
                for k in cache_keys:
                    self._cache[k] = (current_time, "processing")
                for sk in dom_source_keys:
                    self._source_content_hash_map[sk] = content_hash
                return False
            else:
                logger.debug(
                    f"[幂等] DOM source键({key})命中且内容hash相同，确认为真正重复: {customer_name}"
                )
                return None
        if not stored_hash and content_hash:
            content_keys = [k for k in cache_keys if not k.startswith(("lmid:", "src:", "mid:"))]
            if content_keys:
                any_content_new = any(ck not in self._cache for ck in content_keys)
                if any_content_new:
                    logger.info(
                        f"[幂等] DOM source键({key})命中但无存储hash且content键不存在，"
                        f"视为DOM位置复用的新消息: {customer_name}"
                    )
                    for k in cache_keys:
                        self._cache[k] = (current_time, "processing")
                    for sk in dom_source_keys:
                        self._source_content_hash_map[sk] = content_hash
                    return False
            logger.debug(
                f"[幂等] DOM source键({key})命中无存储hash，保守按重复处理: {customer_name}"
            )
            return None
        return None

    def is_message_processed(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        direction: str = "",
    ) -> bool:
        if not content:
            content = ""
        current_time = time.time()
        cache_keys = self.build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
            direction=direction,
        )
        source_like_keys = [key for key in cache_keys if key.startswith(("lmid:", "src:", "mid:"))]
        is_short_content = self._is_short_content(content)
        identity_log = (
            f"customer={customer_name} source_message_id={str(source_message_id or msg_id or '').strip() or '-'} "
            f"conversation_id={str(conversation_id or '').strip() or '-'} "
            f"logical_message_id={str(logical_message_id or '').strip() or '-'} "
            f"keys={cache_keys[:4]}"
        )

        def _check_key(key: str):
            if key not in self._cache:
                return None
            cached_time, status = self._cache[key]
            elapsed = current_time - cached_time

            if status == "done":
                done_ttl = self._short_message_done_ttl if is_short_content else self._processed_messages_ttl
                if current_time - cached_time < done_ttl:
                    logger.debug(
                        f"[幂等] 消息已完成处理(done, {elapsed:.0f}s, ttl={done_ttl}s)，跳过: "
                        f"{identity_log}"
                    )
                    return True
            elif status.startswith("skipped"):
                skip_reason = ""
                if ":" in status:
                    skip_reason = status.split(":", 1)[1]

                if is_short_content:
                    skip_ttl = self._short_message_skip_ttl
                elif skip_reason in self._INHERENT_SKIP_REASONS:
                    skip_ttl = 300
                elif skip_reason in self._ECHO_SKIP_REASONS:
                    skip_ttl = 300
                elif skip_reason in self._TRANSIENT_SKIP_REASONS:
                    skip_ttl = 300
                else:
                    skip_ttl = 300

                if current_time - cached_time < skip_ttl:
                    logger.debug(
                        f"[幂等] 消息已跳过(skipped:{skip_reason}, {elapsed:.0f}s, ttl={skip_ttl}s)，暂不处理: "
                        f"{identity_log}"
                    )
                    return True
                else:
                    logger.info(
                        f"[幂等] 消息skipped:{skip_reason}状态已超时({skip_ttl}s)，允许重新评估: "
                        f"{identity_log}"
                    )
                    self._reset_all_keys_for_message(key, current_time)
                    return False
            elif status == "processing":
                processing_ttl = self._short_message_processing_ttl if is_short_content else self._processing_timeout
                if current_time - cached_time < processing_ttl:
                    logger.debug(
                        f"[幂等] 消息正在处理中(processing, {elapsed:.0f}s, ttl={processing_ttl}s)，跳过: "
                        f"{identity_log}"
                    )
                    return True
                else:
                    logger.warning(
                        f"[幂等] 消息processing状态超时({processing_ttl}s)，允许重新处理: "
                        f"{identity_log}"
                    )
                    self._cache[key] = (current_time, "processing")
                    return False
            elif status.startswith("failed_"):
                try:
                    parts = status.split("_")
                    retry_count = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 3
                except Exception:
                    retry_count = 3

                if retry_count >= 3:
                    logger.info(f"[幂等] 消息已重试{retry_count}次，标记为done: {identity_log}")
                    self._cache[key] = (current_time, "done")
                    return True
                else:
                    if current_time - cached_time < self._failed_message_retry_gate_ttl:
                        logger.info(
                            f"[幂等] 消息失败状态({status}, {elapsed:.0f}s)，等待outbox/人工处理，暂不重新入链: "
                            f"{identity_log}"
                        )
                        return True
                    logger.info(f"[幂等] 消息失败状态({status})已过保护期，允许重新处理: {identity_log}")
                    self._cache[key] = (current_time, "processing")
                    return False
            return None

        need_cleanup = False
        dom_source_rejected = False
        with self._lock:
            prioritized_check_keys = cache_keys if not source_like_keys else source_like_keys
            for key in prioritized_check_keys:
                result = _check_key(key)
                if result is not None:
                    if result is True:
                        content_verify = self._verify_dom_source_key_with_content(
                            key, cache_keys, customer_name, current_time,
                            content=content,
                        )
                        if content_verify is False:
                            dom_source_rejected = True
                            need_cleanup = len(self._cache) > self._max_cache_size
                            break
                    return result

            if dom_source_rejected:
                if need_cleanup:
                    self._cleanup_unlocked()
                return False

            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16] if content else ""
            if source_like_keys:
                logger.info(
                    f"[幂等] 检测到新的消息ID，优先按 source_message_id/logical_message_id 幂等判定后放行新消息: "
                    f"{identity_log}"
                )
                for key in cache_keys:
                    self._cache[key] = (current_time, "processing")
                for sk in source_like_keys:
                    if content_hash:
                        self._source_content_hash_map[sk] = content_hash
                need_cleanup = len(self._cache) > self._max_cache_size
            else:
                logger.info(
                    f"[幂等] 消息未被处理过，按 conversation/content 维度标记为processing: "
                    f"{identity_log}"
                )
                for key in cache_keys:
                    self._cache[key] = (current_time, "processing")
                need_cleanup = len(self._cache) > self._max_cache_size

            if need_cleanup:
                self._cleanup_unlocked()

        return False

    def is_message_done_status(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> bool:
        if not content:
            content = ""
        current_time = time.time()
        cache_keys = self.build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )

        with self._lock:
            for key in cache_keys:
                if key not in self._cache:
                    continue
                cached_time, status = self._cache[key]
                if status != "done":
                    continue
                done_ttl = self._short_message_done_ttl if len(content) <= 10 else self._processed_messages_ttl
                if current_time - cached_time >= done_ttl:
                    continue

                content_verify = self._verify_dom_source_key_with_content(
                    key, cache_keys, customer_name, current_time,
                    content=content,
                )
                if content_verify is False:
                    need_cleanup = len(self._cache) > self._max_cache_size
                    if need_cleanup:
                        self.cleanup()
                    return False

                logger.debug(
                    f"[幂等] 回复阶段二次检查: 消息已完成(done, {current_time - cached_time:.0f}s)，跳过: {customer_name}"
                )
                return True

        return False

    def mark_skipped(
        self,
        customer_name: str,
        content: str,
        reason: str = "",
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> None:
        content = content or ""
        current_time = time.time()
        cache_keys = self.build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
        logger.info(f"[消息状态] 标记skipped: customer={customer_name}, reason={reason}, keys={cache_keys[:3]}...")
        with self._lock:
            for key in cache_keys:
                self._cache[key] = (current_time, f"skipped:{reason}")
            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
            for key in cache_keys:
                if key.startswith(("src:", "mid:")):
                    self._source_content_hash_map[key] = content_hash

    def mark_done(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
    ) -> None:
        content = content or ""
        current_time = time.time()
        cache_keys = self.build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
        with self._lock:
            for key in cache_keys:
                self._cache[key] = (current_time, "done")
            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
            for key in cache_keys:
                if key.startswith(("src:", "mid:")):
                    self._source_content_hash_map[key] = content_hash

    def mark_failed(
        self,
        customer_name: str,
        content: str,
        msg_id: str = "",
        *,
        conversation_id: str = "",
        customer_id: str = "",
        logical_message_id: str = "",
        source_message_id: str = "",
        retry_count: int = 0,
    ) -> None:
        content = content or ""
        current_time = time.time()
        cache_keys = self.build_cache_keys(
            customer_name=customer_name,
            content=content,
            msg_id=msg_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            logical_message_id=logical_message_id,
            source_message_id=source_message_id,
        )
        with self._lock:
            for key in cache_keys:
                self._cache[key] = (current_time, f"failed_{retry_count}")

    def _reset_all_keys_for_message(self, matched_key: str, current_time: float) -> None:
        matched_ts, _ = self._cache.get(matched_key, (0, ""))
        related_keys = [
            k for k, (ts, status) in self._cache.items()
            if abs(ts - matched_ts) < 1.0 and status.startswith("skipped:")
        ]
        for k in related_keys:
            self._cache[k] = (current_time, "processing")

    def _cleanup_unlocked(self) -> None:
        current_time = time.time()
        expired = [
            k for k, (ts, _status) in self._cache.items()
            if current_time - ts > self._processed_messages_ttl
        ]
        for k in expired:
            del self._cache[k]
            self._source_content_hash_map.pop(k, None)
        if len(self._cache) > self._max_cache_size:
            sorted_items = sorted(self._cache.items(), key=lambda x: x[1][0])
            remove_count = len(self._cache) - self._max_cache_size // 2
            for k, (_, status) in sorted_items[:remove_count]:
                if status == "processing":
                    continue
                del self._cache[k]
                self._source_content_hash_map.pop(k, None)
        stale_source_keys = [
            k for k in self._source_content_hash_map
            if k not in self._cache
        ]
        for k in stale_source_keys:
            del self._source_content_hash_map[k]

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_unlocked()

    def get_cache_snapshot(self) -> Dict[str, Tuple[float, str]]:
        with self._lock:
            return dict(self._cache)

    def get_source_content_hash_snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._source_content_hash_map)

    def restore_cache(self, snapshot) -> None:
        with self._lock:
            if isinstance(snapshot, tuple) and len(snapshot) == 2:
                self._cache.update(snapshot[0])
                self._source_content_hash_map.update(snapshot[1])
            elif isinstance(snapshot, dict):
                self._cache.update(snapshot)
                for key in list(self._source_content_hash_map):
                    if key not in self._cache:
                        del self._source_content_hash_map[key]
