from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from loguru import logger
from src.common.monitoring import get_metrics
from src.web.conversation_switch_service import ResolvedSendTarget

if TYPE_CHECKING:
    from src.web.bot_service import BotService


@dataclass(frozen=True)
class OutboundSendResult:
    success: bool
    channel: str
    reason: str
    duplicate_guard_hit: bool = False
    identity_level: str = "exact_name"
    resolved_name: str = ""

    def get(self, key: str, default=None):
        return getattr(self, key, default)


@dataclass
class _SendState:
    last_failure_reason: str = ""
    last_target_meta: dict[str, Any] = field(default_factory=dict)
    last_trace_id: str = ""


class OutboundSendGateway:
    """统一发送入口——整个系统只能从这里发消息。

    职责：
    1. 目标解析（委托 ConversationSwitchService）
    2. 身份等级校验（弱身份阻断）
    3. 通道选择（RPA 优先，失败回退传统监听）
    4. 重试与结果规范化
    """

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service
        self._state = _SendState()
        self._state_lock = threading.Lock()

    # ==================== 统一公共接口 ====================

    def send(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        external_trace_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> OutboundSendResult:
        """统一发送入口——唯一对外发送方法。

        流程：
        1. 清理上次发送状态
        2. 解析发送目标（名称纠正 + 身份等级判定）
        3. 弱身份阻断
        4. 按通道优先级发送（RPA → 传统监听）
        """
        total_started_at = time.perf_counter()
        self._clear_send_state()
        diagnostics_store = getattr(self.bot, "_get_outbound_send_diagnostics_store", lambda: None)()
        trace_id = (
            diagnostics_store.begin_attempt(
                customer_name=customer_name,
                content=content,
                conversation_id=conversation_id,
                customer_id=customer_id,
                platform=platform,
                trace_id=external_trace_id,
                outbound_source=outbound_source,
                outbound_trigger=outbound_trigger,
            )
            if diagnostics_store is not None
            else ""
        )
        if trace_id:
            with self._state_lock:
                self._state.last_trace_id = trace_id

        resolved = self._resolve_target(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

        send_target_name = resolved.resolved_name or customer_name
        identity_level = resolved.identity_level or "exact_name"

        self._record_target_meta(resolved)
        if diagnostics_store is not None and trace_id:
            diagnostics_store.update_target_meta(
                trace_id,
                {
                    "requested_name": resolved.requested_name,
                    "resolved_name": resolved.resolved_name,
                    "source": resolved.source,
                    "identity_level": resolved.identity_level,
                    "conversation_id": resolved.conversation_id,
                    "customer_id": resolved.customer_id,
                    "platform": resolved.platform,
                            "outbound_source": str(outbound_source or "").strip(),
                            "outbound_trigger": str(outbound_trigger or "").strip(),
                },
            )

        logger.info(
            "发送目标解析完成: "
            f"requested={resolved.requested_name or '-'}, "
            f"resolved={send_target_name or '-'}, "
            f"source={resolved.source or '-'}, "
            f"identity_level={identity_level or '-'}, "
            f"conversation_id={resolved.conversation_id or '-'}, "
            f"customer_id={resolved.customer_id or '-'} "
            f"outbound_source={outbound_source or '-'} "
            f"outbound_trigger={outbound_trigger or '-'} "
            f"trace={trace_id or '-'}"
        )

        if identity_level == "name_fuzzy":
            reason = self.record_failure_reason("identity_conflict_blocked:name_fuzzy_target")
            if diagnostics_store is not None and trace_id:
                diagnostics_store.record_failure(trace_id, reason, channel="preflight", send_error=reason)
            logger.warning(
                f"发送前已阻断弱身份目标: requested={resolved.requested_name or '-'}, "
                f"resolved={send_target_name or '-'}, conversation_id={resolved.conversation_id or '-'}, "
                f"customer_id={resolved.customer_id or '-'}"
            )
            result = OutboundSendResult(
                success=False,
                channel="preflight",
                reason=reason,
                identity_level=identity_level,
                resolved_name=send_target_name,
            )
        else:
            dispatch_call = lambda: self._dispatch_to_channel(
                customer_name=send_target_name,
                content=content,
                max_retries=max_retries,
                identity_level=identity_level,
                conversation_id=resolved.conversation_id or conversation_id,
                customer_id=resolved.customer_id or customer_id,
                platform=resolved.platform or platform,
            )
            coordinator_getter = getattr(self.bot, "_get_outbound_dispatch_coordinator", None)
            coordinator = coordinator_getter() if callable(coordinator_getter) else None
            if coordinator is not None:
                if diagnostics_store is not None and trace_id:
                    conversation_key_builder = getattr(coordinator, "_build_conversation_key", None)
                    if callable(conversation_key_builder):
                        conversation_key = conversation_key_builder(
                            conversation_id=resolved.conversation_id or conversation_id,
                            customer_id=resolved.customer_id or customer_id,
                            customer_name=send_target_name,
                            platform=resolved.platform or platform,
                        )
                    else:
                        conversation_key = self._build_dispatch_conversation_key(
                            conversation_id=resolved.conversation_id or conversation_id,
                            customer_id=resolved.customer_id or customer_id,
                            customer_name=send_target_name,
                            platform=resolved.platform or platform,
                        )
                    diagnostics_store.record_dispatch(
                        trace_id,
                        queue_status="queued",
                        conversation_key=conversation_key,
                    )
                result = coordinator.run_or_enqueue(
                    customer_name=send_target_name,
                    task=dispatch_call,
                    conversation_id=resolved.conversation_id or conversation_id,
                    customer_id=resolved.customer_id or customer_id,
                    platform=resolved.platform or platform,
                    timeout_seconds=max(15.0, float(max_retries or 1) * 10.0),
                    trace_id=trace_id,
                )
            else:
                result = dispatch_call()

        metrics = get_metrics()
        if diagnostics_store is not None and trace_id:
            if result.success:
                diagnostics_store.record_success(trace_id, channel=result.channel, reason=result.reason)
            else:
                diagnostics_store.record_failure(
                    trace_id,
                    result.reason or self._get_current_failure_reason() or "send_failed",
                    channel=result.channel,
                    send_error=getattr(self.bot, "_get_last_send_error", lambda default="": default)(""),
                )
        metrics.record_outbound_send_attempt(channel=result.channel, success=result.success)
        metrics.record_outbound_send_latency(
            channel=result.channel,
            stage="send_total",
            duration=time.perf_counter() - total_started_at,
        )
        return result

    def retry_send(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """重试发送——语义等同于 send()，但保留上次失败原因供诊断。"""
        prev_reason = self._state.last_failure_reason
        result = self.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        if not result.success and prev_reason:
            logger.info(f"[重试发送]上次失败原因: {prev_reason}")
        return result

    def get_diagnostics(self, trace_id: str = "") -> dict[str, Any]:
        diagnostics_store = getattr(self.bot, "_get_outbound_send_diagnostics_store", lambda: None)()
        selected_trace_id = str(trace_id or "").strip()
        if not selected_trace_id:
            with self._state_lock:
                selected_trace_id = self._state.last_trace_id
        latest_snapshot = diagnostics_store.get_snapshot(selected_trace_id) if diagnostics_store is not None else {}
        with self._state_lock:
            fallback_target_meta = dict(self._state.last_target_meta or {})
            fallback_reason = str(self._state.last_failure_reason or "").strip()
            fallback_trace_id = str(self._state.last_trace_id or "").strip()
        return {
            "trace_id": latest_snapshot.get("trace_id") or fallback_trace_id,
            "last_failure_reason": latest_snapshot.get("last_failure_reason") or fallback_reason,
            "last_send_error": latest_snapshot.get("last_send_error")
            or getattr(self.bot, "_get_last_send_error", lambda default="": default)(""),
            "last_target_meta": latest_snapshot.get("target_meta") or fallback_target_meta,
            "status": latest_snapshot.get("status", ""),
            "channel": latest_snapshot.get("channel", ""),
            "reason": latest_snapshot.get("reason", ""),
            "outbound_source": latest_snapshot.get("outbound_source", ""),
            "outbound_trigger": latest_snapshot.get("outbound_trigger", ""),
            "resolution_level": latest_snapshot.get("resolution_level", ""),
            "recent_sends": diagnostics_store.list_recent(limit=10) if diagnostics_store is not None else [],
        }

    def ensure_target_ready(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """发送前目标会话准备——预热目标会话，不执行发送。"""
        bot = self.bot

        if getattr(bot, "_use_rpa_mode", False):
            launcher = getattr(bot, "rpa_launcher", None)
            if launcher and hasattr(launcher, "ensure_send_target"):
                ready = launcher.ensure_send_target(customer_name)
                if ready:
                    return True
                logger.warning(f"[RPA模式]目标会话预热失败: {customer_name}")

        switch_service = getattr(bot, "_get_conversation_switch_service", lambda: None)()
        if switch_service:
            return switch_service.ensure_monitor_send_target(customer_name)

        return False

    def record_failure_reason(self, reason: str) -> str:
        """记录发送失败原因（供外部诊断与重试决策）。"""
        normalized = str(reason or "").strip()[:200]
        with self._state_lock:
            self._state.last_failure_reason = normalized
        bot = self.bot
        if hasattr(bot, "_set_last_send_error"):
            bot._set_last_send_error(normalized)
        diagnostics_store = getattr(bot, "_get_outbound_send_diagnostics_store", lambda: None)()
        current_trace_id = ""
        with self._state_lock:
            current_trace_id = self._state.last_trace_id
        if diagnostics_store is not None and current_trace_id:
            diagnostics_store.record_failure(
                current_trace_id,
                normalized,
                send_error=getattr(bot, "_get_last_send_error", lambda default="": default)(""),
            )
        return normalized

    # ==================== 旧接口兼容（阶段4收口，转发到 send） ====================

    def send_reply_to_target(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int,
        identity_level: str = "exact_name",
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        external_trace_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> OutboundSendResult:
        """旧接口兼容——转发到统一 send()。"""
        return self.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            external_trace_id=external_trace_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

    # ==================== 内部方法 ====================

    def _clear_send_state(self) -> None:
        with self._state_lock:
            self._state.last_failure_reason = ""
            self._state.last_target_meta = {}
            self._state.last_trace_id = ""
        bot = self.bot
        if hasattr(bot, "_clear_last_send_error"):
            bot._clear_last_send_error()
        if hasattr(bot, "_clear_last_send_target_name"):
            bot._clear_last_send_target_name()

    def _resolve_target(
        self,
        *,
        customer_name: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> ResolvedSendTarget:
        bot = self.bot
        switch_service_getter = getattr(bot, "_get_conversation_switch_service", None)
        if switch_service_getter and callable(switch_service_getter):
            switch_service = switch_service_getter()
            if switch_service and hasattr(switch_service, "resolve_send_target"):
                return switch_service.resolve_send_target(
                    customer_name=customer_name,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                )
        return ResolvedSendTarget(
            requested_name=customer_name,
            resolved_name=customer_name,
            source="fallback",
            identity_level="exact_name",
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

    def _record_target_meta(self, resolved: ResolvedSendTarget) -> None:
        with self._state_lock:
            self._state.last_target_meta = {
                "requested_name": resolved.requested_name,
                "resolved_name": resolved.resolved_name,
                "source": resolved.source,
                "identity_level": resolved.identity_level,
                "conversation_id": resolved.conversation_id,
                "customer_id": resolved.customer_id,
                "platform": resolved.platform,
            }
            current_trace_id = self._state.last_trace_id
        bot = self.bot
        if hasattr(bot, "_set_last_send_target_name"):
            bot._set_last_send_target_name(resolved.resolved_name or resolved.requested_name)
        if hasattr(bot, "_set_last_send_target_meta"):
            bot._set_last_send_target_meta(
                requested_name=resolved.requested_name,
                resolved_name=resolved.resolved_name,
                source=resolved.source,
                identity_level=resolved.identity_level,
                conversation_id=resolved.conversation_id,
                customer_id=resolved.customer_id,
                platform=resolved.platform,
            )
        diagnostics_store = getattr(bot, "_get_outbound_send_diagnostics_store", lambda: None)()
        if diagnostics_store is not None and current_trace_id:
            diagnostics_store.update_target_meta(
                current_trace_id,
                dict(self._state.last_target_meta),
            )

    def _dispatch_to_channel(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int,
        identity_level: str,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """按通道优先级发送：RPA 优先，失败回退传统监听。"""
        bot = self.bot

        if getattr(bot, "_use_rpa_mode", False):
            rpa_result = self._send_via_rpa(
                customer_name=customer_name,
                content=content,
                max_retries=max_retries,
                identity_level=identity_level,
            )
            if rpa_result.success:
                return rpa_result

            logger.warning(
                f"[RPA模式]发送未成功，尝试回退传统监听发送: target={customer_name}, "
                f"reason={rpa_result.reason}"
            )
            if getattr(bot, "message_monitor", None):
                monitor_result = self._send_via_monitor(
                    customer_name=customer_name,
                    content=content,
                    max_retries=max_retries,
                    identity_level=identity_level,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                )
                if monitor_result.success:
                    logger.info(f"[传统回退]发送成功: {customer_name}")
                    if hasattr(bot, "_clear_last_send_error"):
                        bot._clear_last_send_error()
                    return monitor_result
                logger.warning(
                    f"[传统回退]发送失败: target={customer_name}, "
                    f"reason={monitor_result.reason}"
                )
                return monitor_result
            return rpa_result

        return self._send_via_monitor(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

    def _send_via_rpa(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int,
        identity_level: str,
    ) -> OutboundSendResult:
        bot = self.bot
        rpa_max_retries = max(1, min(max_retries, 2))
        for attempt in range(1, rpa_max_retries + 1):
            launcher = getattr(bot, "rpa_launcher", None)
            if not launcher or not hasattr(launcher, "send_reply"):
                recovered = False
                try:
                    recovered = bool(getattr(bot, "_retry_rpa_init", lambda: False)())
                except Exception as recover_exc:
                    logger.warning(f"[RPA模式]发送前恢复发送器失败: {recover_exc}")
                launcher = getattr(bot, "rpa_launcher", None)
                if recovered and launcher and hasattr(launcher, "send_reply"):
                    logger.info("[RPA模式]发送器短暂失效，已自动恢复后继续重试")
                else:
                    reason = self.record_failure_reason("rpa_launcher_unavailable")
                    logger.warning("[RPA模式]发送器不可用，停止本次发送")
                    return OutboundSendResult(
                        success=False,
                        channel="rpa",
                        reason=reason,
                        identity_level=identity_level,
                        resolved_name=customer_name,
                    )
            try:
                logger.info(
                    f"[RPA模式]发送消息(发送链内原子确认目标会话): "
                    f"{customer_name} (尝试 {attempt}/{rpa_max_retries})"
                )
                send_kwargs = {"assume_target_ready": False}
                if identity_level and identity_level != "exact_name":
                    send_kwargs["identity_level"] = identity_level
                send_result = launcher.send_reply(
                    customer_name,
                    content,
                    **send_kwargs,
                )
                if send_result:
                    if hasattr(bot, "_clear_last_send_error"):
                        bot._clear_last_send_error()
                    return OutboundSendResult(
                        success=True,
                        channel="rpa",
                        reason="",
                        identity_level=identity_level,
                        resolved_name=customer_name,
                    )

                failure_reason = self.record_failure_reason(
                    getattr(launcher, "last_send_error", "") or f"rpa_send_failed:{customer_name}"
                )
                if failure_reason == "rpa_engine_uninitialized" and attempt < rpa_max_retries:
                    recovered = False
                    try:
                        recovered = bool(getattr(bot, "_retry_rpa_init", lambda: False)())
                    except Exception as recover_exc:
                        logger.warning(f"[RPA模式]发送失败后恢复引擎失败: {recover_exc}")
                    if recovered:
                        logger.warning("[RPA模式]检测到引擎未初始化，已自动恢复后重试发送")
                        time.sleep(1)
                        continue
                if "消息已发送，请勿重复" in failure_reason:
                    logger.warning(f"[RPA模式]检测到重复发送保护命中，按已发送处理: {customer_name}")
                    get_metrics().record_outbound_idempotency_hit("rpa_duplicate_guard")
                    if hasattr(bot, "_clear_last_send_error"):
                        bot._clear_last_send_error()
                    return OutboundSendResult(
                        success=True,
                        channel="rpa",
                        reason="",
                        duplicate_guard_hit=True,
                        identity_level=identity_level,
                        resolved_name=customer_name,
                    )
                if attempt < rpa_max_retries:
                    wait_time = 1
                    logger.warning(
                        f"[RPA模式]发送失败，{wait_time}秒后重试，reason={self._state.last_failure_reason}"
                    )
                    time.sleep(wait_time)
                else:
                    logger.warning(
                        f"[RPA模式]发送失败(已重试{rpa_max_retries}次)，reason={self._state.last_failure_reason}"
                    )
            except Exception as exc:
                self.record_failure_reason(f"rpa_exception:{str(exc)[:160]}")
                if attempt < rpa_max_retries:
                    wait_time = 3
                    logger.warning(f"[RPA模式]发送异常: {exc}，{wait_time}秒后重试")
                    time.sleep(wait_time)
                else:
                    logger.warning(f"[RPA模式]发送异常(已重试{rpa_max_retries}次): {exc}")

        return OutboundSendResult(
            success=False,
            channel="rpa",
            reason=self._state.last_failure_reason or "rpa_send_failed",
            identity_level=identity_level,
            resolved_name=customer_name,
        )

    def _send_via_monitor(
        self,
        *,
        customer_name: str,
        content: str,
        max_retries: int,
        identity_level: str = "exact_name",
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        bot = self.bot
        last_result = OutboundSendResult(
            success=False,
            channel="monitor",
            reason="monitor_send_failed",
            resolved_name=customer_name,
        )
        monitor_retries = min(max_retries, 3)
        for attempt in range(1, monitor_retries + 1):
            last_result = bot._send_via_monitor(
                customer_name,
                content,
                max_retries=1,
                conversation_id=conversation_id,
                customer_id=customer_id,
                platform=platform,
            )
            if last_result.success:
                break
            if attempt < monitor_retries:
                logger.warning(f"[传统监听]发送失败，1秒后重试({attempt}/{monitor_retries})")
                time.sleep(1)
        reason = "" if last_result.success else (
            last_result.reason
            or self._state.last_failure_reason
            or bot._get_last_send_error("monitor_send_failed")
        )
        if not last_result.success and not reason:
            reason = "monitor_send_failed"
            self.record_failure_reason(reason)
        return OutboundSendResult(
            success=last_result.success,
            channel="monitor",
            reason=reason,
            resolved_name=customer_name,
        )

    def _get_current_failure_reason(self) -> str:
        with self._state_lock:
            return str(self._state.last_failure_reason or "").strip()

    @staticmethod
    def _build_dispatch_conversation_key(
        *,
        conversation_id: str = "",
        customer_id: str = "",
        customer_name: str = "",
        platform: str = "douyin",
    ) -> str:
        normalized_platform = str(platform or "douyin").strip() or "douyin"
        normalized_conversation_id = str(conversation_id or "").strip()
        if normalized_conversation_id:
            return f"conv:{normalized_platform}:{normalized_conversation_id}"
        normalized_customer_id = str(customer_id or "").strip()
        if normalized_customer_id:
            return f"cust:{normalized_platform}:{normalized_customer_id}"
        normalized_customer_name = str(customer_name or "").strip() or "unknown"
        return f"name:{normalized_platform}:{normalized_customer_name}"
