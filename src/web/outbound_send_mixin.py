"""
OutboundSendMixin - 出站发送相关方法

从 BotService 中提取的出站发送方法集合，包含：
1. 统一发送入口（_unified_send_reply_result / _unified_send_reply）
2. 目标发送（_send_reply_to_target_result / _send_reply_to_target）
3. 抖音发送（_send_reply_to_douyin）
4. Monitor 发送（_send_via_monitor / send_message_task / send_message_task_result）
5. 主动/手工发送（send_outbound_message）
6. 发送后置任务（_schedule_post_send_success_tasks / _run_post_send_success_tasks）
7. 自回复检测与标记（_is_recently_sent_by_us / _is_self_reply_content / _mark_sent_by_us）
8. 人工接管通知（_notify_human_handoff）
9. 发送诊断（_classify_non_retryable_send_failure / _verify_send_success / _diagnose_send_failure / _is_send_failure_retryable）
10. 营销追踪（_record_marketing_tracking_message / _update_marketing_tracking_status）
11. 发送诊断状态方法（_set_last_send_error / _clear_last_send_error / _get_last_send_error 等）
"""
import contextlib
import hashlib
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from src.common.marketing_tracking_service import marketing_tracking_service
from src.common.logical_message import build_logical_message_id
from src.douyin_bot.message_sender import MessageSender
from src.web.outbound_send_gateway import OutboundSendResult


class OutboundSendMixin:
    """出站发送 Mixin —— 从 BotService 提取的出站发送相关方法。"""

    # ---- 发送诊断系列 ----

    def _classify_non_retryable_send_failure(self, customer_name: str, failure_reason: str) -> str:
        return self._get_outbound_send_verifier().classify_non_retryable_failure(
            customer_name,
            failure_reason,
        )

    def _verify_send_success(self, send_result, *, customer_name: str = ""):
        return self._get_outbound_send_verifier().verify_send_success(
            send_result,
            customer_name=customer_name,
        )

    def _diagnose_send_failure(self, send_result, *, customer_name: str = ""):
        return self._get_outbound_send_verifier().diagnose_failure(
            send_result,
            customer_name=customer_name,
        )

    def _is_send_failure_retryable(self, failure_reason: str) -> bool:
        return self._get_outbound_send_verifier().is_retryable(failure_reason)

    # ---- 营销追踪 ----

    def _record_marketing_tracking_message(
        self,
        *,
        customer_name: str,
        conversation_id: str,
        reply_content: str,
        platform: str,
        reply_msg_id: str,
        logical_message_id: str,
    ) -> None:
        """在统一出站成功后记录营销 tracking。"""
        customer_name = (customer_name or "").strip()
        if not customer_name:
            return

        try:
            record = marketing_tracking_service.record_message(
                customer_name=customer_name,
                content=reply_content,
                status="sent",
                conversation_id=conversation_id,
                source_message_id=reply_msg_id,
                logical_message_id=logical_message_id,
                platform=platform,
            )
            record_id = (record or {}).get("id", "")
            if record_id:
                marketing_tracking_service.update_message_status(record_id, "delivered")
        except Exception as tracking_e:
            logger.debug(f"记录营销 tracking 发送事件失败: {tracking_e}")

    def _update_marketing_tracking_status(
        self,
        *,
        status: str,
        customer_name: str = "",
        conversation_id: str = "",
        allowed_current_statuses: Optional[List[str]] = None,
    ) -> bool:
        """按客户/会话更新最近一条营销消息状态。"""
        customer_name = (customer_name or "").strip() or self._resolve_customer_name_from_conversation(conversation_id)
        conversation_id = (conversation_id or "").strip()
        if not customer_name and not conversation_id:
            return False

        try:
            return marketing_tracking_service.update_latest_message_status(
                status=status,
                customer_name=customer_name,
                conversation_id=conversation_id,
                allowed_current_statuses=allowed_current_statuses or [],
            )
        except Exception as tracking_e:
            logger.debug(f"更新营销 tracking 状态失败: {tracking_e}")
            return False

    # ---- 自回复检测与标记 ----

    def _is_recently_sent_by_us(self, customer_name: str, content: str) -> bool:
        """检查消息是否是我们最近发送的，防止自回复循环——委托到 OutboundIdempotencyService。"""
        target_meta = self._get_last_send_target_meta()
        return self._get_outbound_idempotency_service().is_echo(
            customer_name,
            content,
            conversation_id=target_meta.get("conversation_id", ""),
            customer_id=target_meta.get("customer_id", ""),
            platform=target_meta.get("platform", "douyin"),
        )

    def _is_self_reply_content(self, customer_name: str, content: str) -> bool:
        """检测内容是否是系统自回复的格式——委托到 OutboundIdempotencyService。"""
        target_meta = self._get_last_send_target_meta()
        return self._get_outbound_idempotency_service().is_self_reply(
            customer_name,
            content,
            conversation_id=target_meta.get("conversation_id", ""),
            customer_id=target_meta.get("customer_id", ""),
            platform=target_meta.get("platform", "douyin"),
        )

    def _mark_sent_by_us(
        self,
        customer_name: str,
        content: str,
        conversation_id: str = "",
        *,
        customer_id: str = "",
        platform: str = "douyin",
        logical_message_id: str = "",
        trace_id: str = "",
    ):
        """标记已发送消息——委托到 OutboundIdempotencyService + RPA状态机。"""
        self._get_outbound_idempotency_service().mark_sent(
            customer_name,
            content,
            conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        self._get_outbound_recent_reply_store().record_reply(
            conversation_id=conversation_id,
            content=content,
            logical_message_id=logical_message_id,
            trace_id=trace_id,
            platform=platform,
            customer_name=customer_name,
        )
        # 主动通知 RPA 状态机：我方已发送回复，标记 sent_by_us=True
        # DOM 轮询无法可靠识别出站方向，必须由发送侧主动通知
        try:
            if (
                getattr(self, "rpa_launcher", None)
                and getattr(self.rpa_launcher, "rpa_engine", None)
            ):
                self.rpa_launcher.rpa_engine.mark_outbound_sent(customer_name, content)
        except Exception as rpa_e:
            logger.debug(f"通知RPA状态机sent_by_us失败(非致命): {rpa_e}")

    def _notify_human_handoff(self, customer_name: str, content: str, reply_content: str, conversation_id: str = ""):
        """记录人工接管事件，作为自动发送闸门的配套通知。"""
        logger.warning(f"客户 {customer_name} 命中人工接管闸门，暂停自动发送")
        try:
            self.message_bus.publish_simple(
                msg_type="human_handoff_required",
                payload={
                    "customer_name": customer_name,
                    "conversation_id": conversation_id,
                    "content": content[:200] if content else "",
                    "suggested_reply": reply_content[:200] if reply_content else "",
                }
            )
        except Exception as e:
            logger.debug(f"发布人工接管事件失败: {e}")

    # ---- 统一发送入口 ----

    def _unified_send_reply_result(
        self,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        reply_msg_id: str,
        reply_msg: dict,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
        smart_result: dict,
        session=None,
        original_content: str = "",
        logical_message_id: str = "",
        outbox_id: str = "",
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> OutboundSendResult:
        """统一消息发送方法，返回规范化发送结果并收口后置状态。

        统一处理：
        1. 熔断器保护发送
        2. 预留机制确认/取消
        3. 自回复标记
        4. RPA引擎标记
        5. 会话管理器记录
        6. 数据库保存（消息+会话+客户状态）

        Args:
            customer_name: 客户名称
            reply_content: 回复内容
            conversation_id: 会话ID
            reply_msg_id: 回复消息ID
            reply_msg: 回复消息字典
            customer_id: 客户ID
            platform: 平台
            intent_level: 意向等级
            intent_score: 意向分数
            smart_result: 智能回复结果
            session: 会话对象

        Returns:
            OutboundSendResult: 规范化发送结果
        """
        from src.common.enterprise.circuit_breaker import CircuitOpenError
        from datetime import datetime
        send_success = False
        send_failure_reason = ""
        send_result = OutboundSendResult(success=False, channel="", reason="")
        state_repository = self._get_inbound_message_state_repository()
        trace_id = str(logical_message_id or reply_msg_id or conversation_id or "")[:12] or "unknown"
        perf_metrics: Dict[str, float] = {}
        total_started_at = time.perf_counter()
        legacy_send = getattr(getattr(self, "__dict__", {}), "get", lambda *_, **__: None)("_unified_send_reply")
        if callable(legacy_send):
            success = bool(
                legacy_send(
                    customer_name=customer_name,
                    reply_content=reply_content,
                    conversation_id=conversation_id,
                    reply_msg_id=reply_msg_id,
                    reply_msg=reply_msg,
                    customer_id=customer_id,
                    platform=platform,
                    intent_level=intent_level,
                    intent_score=intent_score,
                    smart_result=smart_result,
                    session=session,
                    original_content=original_content,
                    logical_message_id=logical_message_id,
                    outbox_id=outbox_id,
                )
            )
            return OutboundSendResult(
                success=success,
                channel="legacy",
                reason="" if success else self._get_last_send_error("send_failed"),
            )
        self._ensure_outbox_event(
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            platform=platform,
            source_message_id=reply_msg_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

        try:
            self._clear_last_send_error()
            # [FIX-INST:repeat-reply] 发送前预标记 sent_by_us=True
            # 收敛前：sent_by_us 在发送成功后才标记（约15秒后），期间 DOM 轮询持续运行，
            # 检测到"内容变化+unread=1"时 was_sent_by_us=False，路径1跳过失败，
            # 走到路径4 emit，导致用户消息被重复触发自动回复。
            # 收敛后：发送前就预标记，DOM 轮询检测到变化时走路径1跳过。
            # 发送失败时会在下方 rollback。
            try:
                if (
                    getattr(self, "rpa_launcher", None)
                    and getattr(self.rpa_launcher, "rpa_engine", None)
                ):
                    self.rpa_launcher.rpa_engine.mark_outbound_sent(customer_name, reply_content)
            except Exception as pre_mark_e:
                logger.debug(f"发送前预标记sent_by_us失败(非致命): {pre_mark_e}")
            try:
                stage_started_at = time.perf_counter()
                with self.circuit_breaker:
                    send_result = self._send_reply_to_target_result(
                        customer_name,
                        reply_content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                        logical_message_id=logical_message_id,
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                    )
                    send_success = bool(send_result.success)
                    if send_success:
                        logger.info(f"自动回复发送成功：{customer_name}")
                    else:
                        send_failure_reason = str(send_result.reason or self._get_last_send_error("send_failed"))
                        logger.error(f"自动回复发送失败：{customer_name}, reason={send_failure_reason}")
                self._record_reply_chain_metric(perf_metrics, "send_target", stage_started_at)
            except CircuitOpenError:
                logger.warning(f"熔断器已打开，跳过发送: {customer_name}")
                try:
                    self.db.cancel_reply_reservation(conversation_id, reply_content)
                except Exception as cancel_e:
                    logger.debug(f"熔断器打开时取消预留失败: {cancel_e}")
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="cancelled",
                        message_id=reply_msg_id,
                        reason="circuit_open",
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel="circuit_breaker",
                    )
                except Exception:
                    pass
                self._mark_message_skipped(
                    customer_name,
                    original_content or reply_content,
                    "circuit_open",
                    reply_msg_id,
                    source_message_id=reply_msg_id or "",
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    logical_message_id=logical_message_id,
                )
                return OutboundSendResult(
                    success=False,
                    channel="circuit_breaker",
                    reason="circuit_open",
                )

            if send_success:
                sent_target_name = self._get_last_send_target_name(customer_name)
                self._mark_sent_by_us(
                    sent_target_name,
                    reply_content,
                    conversation_id,
                    customer_id=customer_id,
                    platform=platform,
                    logical_message_id=logical_message_id,
                    trace_id=trace_id,
                )
                if sent_target_name and sent_target_name != customer_name:
                    self._mark_sent_by_us(
                        customer_name,
                        reply_content,
                        conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                        logical_message_id=logical_message_id,
                        trace_id=trace_id,
                    )
                stage_started_at = time.perf_counter()
                try:
                    self.db.confirm_reply_sent(conversation_id, reply_content, reply_msg_id)
                except Exception as confirm_e:
                    logger.debug(f"确认回复预留失败: {confirm_e}")
                self._record_reply_chain_metric(perf_metrics, "confirm_reply", stage_started_at)
                stage_started_at = time.perf_counter()
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="sent",
                        message_id=reply_msg_id,
                        reason="",
                        next_retry_at="",
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel=str(send_result.channel or ""),
                    )
                except Exception:
                    pass
                self._record_reply_chain_metric(perf_metrics, "mark_outbox_sent", stage_started_at)
                conversation_data = {
                    "conversation_id": conversation_id,
                    "platform": platform,
                    "customer_id": customer_id,
                    "customer_name": sent_target_name or customer_name,
                    "last_message_content": reply_content,
                    "last_message_time": datetime.now().isoformat(),
                    "intent_level": intent_level,
                    "intent_score": intent_score,
                    "priority_level": smart_result.get("priority_level", "P2"),
                    "risk_level": (smart_result.get("risk_assessment", {}) or {}).get("overall_level", "low"),
                    "sentiment": smart_result.get("sentiment", "neutral"),
                    "suggested_action": smart_result.get("suggested_action", "")
                }
                stage_started_at = time.perf_counter()
                self._schedule_post_send_success_tasks(
                    trace_id=trace_id,
                    session=session,
                    reply_content=reply_content,
                    reply_msg=reply_msg,
                    conversation_data=conversation_data,
                    customer_id=customer_id,
                    platform=platform,
                    customer_name=sent_target_name or customer_name,
                    logical_message_id=logical_message_id,
                    reply_msg_id=reply_msg_id,
                )
                self._record_reply_chain_metric(perf_metrics, "schedule_post_send", stage_started_at)
            else:
                send_failure_reason = send_failure_reason or str(send_result.reason or self._get_last_send_error("send_failed"))
                stage_started_at = time.perf_counter()
                try:
                    self.db.cancel_reply_reservation(conversation_id, reply_content)
                except Exception as cancel_e:
                    logger.debug(f"取消回复预留失败: {cancel_e}")
                self._record_reply_chain_metric(perf_metrics, "cancel_reservation", stage_started_at)
                stage_started_at = time.perf_counter()
                try:
                    state_repository.update_outbox_event(
                        outbox_id,
                        status="failed",
                        message_id=reply_msg_id,
                        reason=send_failure_reason[:100],
                        outbound_source=outbound_source,
                        outbound_trigger=outbound_trigger,
                        delivery_channel=str(send_result.channel or ""),
                    )
                except Exception:
                    pass
                self._record_reply_chain_metric(perf_metrics, "mark_outbox_failed", stage_started_at)

        except Exception as e:
            send_success = False
            send_failure_reason = self._set_last_send_error(f"send_pipeline_exception:{str(e)[:160]}")
            send_result = OutboundSendResult(
                success=False,
                channel="pipeline",
                reason=send_failure_reason,
            )
            logger.error(f"统一发送回复时发生异常：{e}")
            try:
                self.db.cancel_reply_reservation(conversation_id, reply_content)
            except Exception:
                pass
            try:
                state_repository.update_outbox_event(
                    outbox_id,
                    status="failed",
                    message_id=reply_msg_id,
                    reason=send_failure_reason[:100],
                    outbound_source=outbound_source,
                    outbound_trigger=outbound_trigger,
                    delivery_channel="pipeline",
                )
            except Exception:
                pass

        self._record_reply_chain_metric(perf_metrics, "total", total_started_at)
        self._log_reply_chain_perf(
            "send_reply",
            trace_id,
            perf_metrics,
            customer=customer_name,
            success=send_success,
            reason=(send_failure_reason[:80] if send_failure_reason else ""),
        )
        if send_success:
            return OutboundSendResult(
                success=True,
                channel=str(send_result.channel or "unknown"),
                reason="",
                duplicate_guard_hit=bool(send_result.duplicate_guard_hit),
            )
        return OutboundSendResult(
            success=False,
            channel=str(send_result.channel or "unknown"),
            reason=str(send_failure_reason or send_result.reason or self._get_last_send_error("send_failed")),
            duplicate_guard_hit=bool(send_result.duplicate_guard_hit),
        )

    def _unified_send_reply(
        self,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        reply_msg_id: str,
        reply_msg: dict,
        customer_id: str,
        platform: str,
        intent_level: str,
        intent_score: float,
        smart_result: dict,
        session=None,
        original_content: str = "",
        logical_message_id: str = "",
        outbox_id: str = "",
        outbound_source: str = "auto_reply",
        outbound_trigger: str = "reply",
    ) -> bool:
        """兼容旧链路的统一消息发送入口，返回 bool。"""
        result = self._unified_send_reply_result(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            reply_msg_id=reply_msg_id,
            reply_msg=reply_msg,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=intent_score,
            smart_result=smart_result,
            session=session,
            original_content=original_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )
        return result.success

    # ---- 发送后置任务 ----

    def _schedule_post_send_success_tasks(
        self,
        *,
        trace_id: str,
        session: Any,
        reply_content: str,
        reply_msg: dict,
        conversation_data: dict,
        customer_id: str,
        platform: str,
        customer_name: str,
        logical_message_id: str,
        reply_msg_id: str,
    ) -> None:
        """发送成功后异步补齐非关键落库，降低主链尾延迟。"""
        executor = getattr(self, "_post_send_executor", None)
        if executor is None:
            self._run_post_send_success_tasks(
                trace_id=trace_id,
                session=session,
                reply_content=reply_content,
                reply_msg=reply_msg,
                conversation_data=conversation_data,
                customer_id=customer_id,
                platform=platform,
                customer_name=customer_name,
                logical_message_id=logical_message_id,
                reply_msg_id=reply_msg_id,
            )
            return

        executor.submit(
            self._run_post_send_success_tasks,
            trace_id=trace_id,
            session=session,
            reply_content=reply_content,
            reply_msg=reply_msg,
            conversation_data=conversation_data,
            customer_id=customer_id,
            platform=platform,
            customer_name=customer_name,
            logical_message_id=logical_message_id,
            reply_msg_id=reply_msg_id,
        )

    def _run_post_send_success_tasks(
        self,
        *,
        trace_id: str,
        session: Any,
        reply_content: str,
        reply_msg: dict,
        conversation_data: dict,
        customer_id: str,
        platform: str,
        customer_name: str,
        logical_message_id: str,
        reply_msg_id: str,
    ) -> None:
        """异步执行发送成功后的附加写库与 tracking。"""
        post_metrics: Dict[str, float] = {}
        total_started_at = time.perf_counter()

        if session:
            stage_started_at = time.perf_counter()
            try:
                self.session_manager.add_message(session.session_id, "outbound", reply_content)
            except Exception as sess_e:
                logger.debug(f"保存出站消息到会话管理器失败: {sess_e}")
            self._record_reply_chain_metric(post_metrics, "session_append", stage_started_at)

        stage_started_at = time.perf_counter()
        try:
            self.db.save_message(reply_msg)
            logger.info("自动回复已保存到数据库")
        except Exception as db_msg_e:
            logger.error(f"保存回复消息到数据库失败: {db_msg_e}")
        self._record_reply_chain_metric(post_metrics, "save_message", stage_started_at)

        merged_conversation_id = str(conversation_data.get("conversation_id") or "")
        stage_started_at = time.perf_counter()
        try:
            self.db.save_conversation(conversation_data)
            merged_conversation_id = self._maybe_merge_identity_conversations(
                customer_name=customer_name,
                customer_id=customer_id,
                platform=platform,
                conversation_id=merged_conversation_id,
            ) or merged_conversation_id
        except Exception as db_conv_e:
            logger.error(f"保存会话到数据库失败: {db_conv_e}")
        self._record_reply_chain_metric(post_metrics, "save_conversation", stage_started_at)

        if customer_id and not customer_id.startswith("temp_"):
            stage_started_at = time.perf_counter()
            try:
                self.db.update_customer_status(customer_id, platform, 'replied')
            except Exception as status_e:
                logger.debug(f"更新客户状态失败: {status_e}")
            self._record_reply_chain_metric(post_metrics, "update_customer_status", stage_started_at)

        stage_started_at = time.perf_counter()
        self._update_marketing_tracking_status(
            status="replied",
            customer_name=customer_name,
            conversation_id=merged_conversation_id,
            allowed_current_statuses=["sent", "delivered", "read"],
        )
        self._record_reply_chain_metric(post_metrics, "update_tracking", stage_started_at)

        stage_started_at = time.perf_counter()
        self._record_marketing_tracking_message(
            customer_name=customer_name,
            conversation_id=merged_conversation_id,
            reply_content=reply_content,
            platform=platform,
            reply_msg_id=reply_msg_id,
            logical_message_id=logical_message_id,
        )
        self._record_reply_chain_metric(post_metrics, "record_tracking_message", stage_started_at)

        self._record_reply_chain_metric(post_metrics, "total", total_started_at)
        self._log_reply_chain_perf(
            "post_send_success",
            trace_id,
            post_metrics,
            customer=customer_name,
            conversation_id=merged_conversation_id,
        )

    # ---- 主动/手工发送 ----

    def send_outbound_message(
        self,
        *,
        customer_name: str,
        reply_content: str,
        conversation_id: str,
        customer_id: str = "",
        platform: str = "douyin",
        intent_level: str = "E",
        intent_score: float = 0.0,
        smart_result: Optional[dict] = None,
        session=None,
        original_content: str = "",
    ) -> tuple[bool, str]:
        """统一的主动/手工/旁路发送包装，复用主发送状态机。"""
        from datetime import datetime

        reply_content = (reply_content or "").strip()
        if not reply_content:
            return False, ""

        if self._is_duplicate_reply_content(conversation_id, reply_content):
            logger.info(f"跳过重复出站消息 [{customer_name}]: 内容重复")
            return False, ""

        reply_msg_id = f"manual_{int(datetime.now().timestamp() * 1000)}_{hashlib.md5(reply_content.encode('utf-8')).hexdigest()[:6]}"
        logical_message_id = build_logical_message_id(
            platform=platform,
            conversation_id=conversation_id,
            customer_name=customer_name,
            direction="outbound",
            content=reply_content,
            source_message_id=reply_msg_id,
        )
        outbox_id = f"outbox_{logical_message_id}"

        self._ensure_outbox_event(
            outbox_id=outbox_id,
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            reply_content=reply_content,
            platform=platform,
            source_message_id=reply_msg_id,
            outbound_source="manual_outbound",
            outbound_trigger="manual_send",
        )

        can_send, reason = self.db.can_send_reply(
            conversation_id,
            min_interval_seconds=1,
            reply_content=reply_content,
            logical_message_id=logical_message_id,
            source_message_id=reply_msg_id,
        )
        if not can_send:
            logger.info(f"主动/手工发送被冷却拦截 [{customer_name}]: {reason}")
            return False, ""

        reply_msg = {
            "message_id": reply_msg_id,
            "logical_message_id": logical_message_id,
            "source_message_id": reply_msg_id,
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "platform": platform,
            "direction": "outbound",
            "message_type": "text",
            "content": reply_content,
            "sender_id": "self",
            "sender_name": "我",
            "is_read": True,
            "is_processed": True,
            "created_at": datetime.now().isoformat(),
        }
        payload = smart_result or {
            "priority_level": "P2",
            "risk_assessment": {"overall_level": "low"},
            "sentiment": "neutral",
            "suggested_action": "",
        }
        success = self._unified_send_reply(
            customer_name=customer_name,
            reply_content=reply_content,
            conversation_id=conversation_id,
            reply_msg_id=reply_msg_id,
            reply_msg=reply_msg,
            customer_id=customer_id,
            platform=platform,
            intent_level=intent_level,
            intent_score=float(intent_score or 0.0),
            smart_result=payload,
            session=session,
            original_content=original_content,
            logical_message_id=logical_message_id,
            outbox_id=outbox_id,
            outbound_source="manual_outbound",
            outbound_trigger="manual_send",
        )
        return success, reply_msg_id if success else ""

    # ---- 目标发送 ----

    def _send_reply_to_target_result(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
        logical_message_id: str = "",
        outbound_source: str = "",
        outbound_trigger: str = "",
    ) -> OutboundSendResult:
        """统一消息发送入口——委托到 OutboundSendGateway.send()。"""
        gateway = self._get_outbound_send_gateway()
        return gateway.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
            external_trace_id=logical_message_id,
            outbound_source=outbound_source,
            outbound_trigger=outbound_trigger,
        )

    def _send_reply_to_target(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> bool:
        """兼容旧链路的超薄发送包装，统一映射到结果对象入口。"""
        result = self._send_reply_to_target_result(
            customer_name,
            content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        return result.success

    def _get_monitor_message_sender(self) -> Optional[MessageSender]:
        monitor = getattr(self, "message_monitor", None)
        target_page = getattr(monitor, "page", None) if monitor else None
        if target_page is None:
            return None

        api_interceptor = getattr(monitor, "api_interceptor", None)
        sender = getattr(self, "sender", None)
        if not isinstance(sender, MessageSender) or getattr(sender, "page", None) is not target_page:
            sender = MessageSender(target_page, self.db, api_interceptor=api_interceptor)
            self.sender = sender
        else:
            sender.api_interceptor = api_interceptor
            sender.page = target_page
        return sender

    def _send_message_to_active_monitor_conversation(
        self,
        customer_name: str,
        content: str,
    ) -> OutboundSendResult:
        monitor = getattr(self, "message_monitor", None)
        if monitor is None:
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_uninitialized",
            )

        sender = self._get_monitor_message_sender()
        if sender is None:
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_uninitialized",
            )

        previous_identity_context = dict(getattr(sender, "_active_target_identity_context", {}) or {})
        try:
            sender._active_target_identity_context = {
                "expected_identities": [customer_name] if customer_name else [],
                "identity_level": "exact_name",
            }
            pre_send_snapshot = sender._collect_chat_target_snapshot()
            input_locator = sender._find_message_input_locator()
            if input_locator is None:
                return OutboundSendResult(
                    success=False,
                    channel="monitor",
                    reason=f"input_box_not_found:{customer_name}",
                    resolved_name=customer_name,
                )

            if not sender._human_helper().clear_and_type(input_locator, content):
                with contextlib.suppress(Exception):
                    input_locator.click(timeout=1500)
                sender.page.keyboard.type(content, delay=100)
            sender.page.wait_for_timeout(180)
            success = sender._dispatch_send_action(
                input_locator=input_locator,
                message=content,
                expected_identities=[customer_name] if customer_name else [],
                previous_message_tail=list(pre_send_snapshot.get("messageTail") or []),
                allow_profile_dialog_fallback=False,
            )
            reason = "" if success else (
                getattr(monitor, "last_send_error", "")
                or f"monitor_send_failed:{customer_name}"
            )
            return OutboundSendResult(
                success=success,
                channel="monitor",
                reason=reason,
                resolved_name=customer_name,
            )
        except Exception as exc:
            logger.error(f"基于当前会话的 monitor 直发失败: {exc}")
            return OutboundSendResult(
                success=False,
                channel="monitor",
                reason=f"monitor_exception:{str(exc)[:160]}",
                resolved_name=customer_name,
            )
        finally:
            sender._active_target_identity_context = previous_identity_context

    def send_message_task_result(
        self,
        customer_name: str,
        content: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """在 BotService worker 线程中执行发送，返回统一结果对象。

        当前职责：在"目标会话已准备好"的前提下，直接执行 monitor DOM 发送。
        这一层不能再回调 OutboundSendGateway.send()，否则会形成
        Gateway -> BotService -> Gateway 的 monitor fallback 循环。
        """
        self._clear_last_send_error()
        if not self.message_monitor:
            self._set_last_send_error("message_monitor_uninitialized")
            logger.error("message_monitor 未初始化")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_uninitialized",
            )

        if not self.message_monitor.page:
            self._set_last_send_error("message_monitor_page_uninitialized")
            logger.error("message_monitor.page 未初始化")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_uninitialized",
            )

        if self.message_monitor.page.is_closed():
            self._set_last_send_error("message_monitor_page_closed")
            logger.error("message_monitor.page 已关闭")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason="message_monitor_page_closed",
            )

        if not self._get_conversation_switch_service().ensure_monitor_send_target_with_identity(
            customer_name=customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        ):
            self._set_last_send_error(f"conversation_not_found:{customer_name}")
            logger.error(f"无法确保目标会话就绪: {customer_name}")
            return OutboundSendResult(
                success=False,
                channel="preflight",
                reason=f"conversation_not_found:{customer_name}",
            )

        logger.info(f"开始发送消息给 {customer_name}: {content[:30]}...")
        result_obj = self._send_message_to_active_monitor_conversation(customer_name, content)
        result = bool(result_obj.success) if result_obj is not None else False
        if result and customer_name and hasattr(self.message_monitor, "mark_message_sent"):
            try:
                self.message_monitor.mark_message_sent(customer_name, content)
            except Exception:
                pass
        if result:
            logger.info(f"消息发送成功: {customer_name}")
            self._clear_last_send_error()
            return result_obj

        failure_reason = (
            getattr(self.message_monitor, "last_send_error", "")
            or (result_obj.reason if result_obj is not None else "")
            or f"monitor_send_failed:{customer_name}"
        )
        self._set_last_send_error(failure_reason)
        logger.warning(f"消息发送失败: {customer_name}")
        return result_obj or OutboundSendResult(
            success=False,
            channel="monitor",
            reason=failure_reason,
        )

    def send_message_task(self, customer_name: str, content: str) -> bool:
        """兼容旧 worker 发送接口，统一映射到结果对象入口。"""
        return bool(self.send_message_task_result(customer_name, content).success)

    def _send_via_monitor(
        self,
        customer_name: str,
        content: str,
        max_retries: int = 3,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        platform: str = "douyin",
    ) -> OutboundSendResult:
        """通过message_monitor发送消息（带重试机制，Event.wait避免线程阻塞）

        Args:
            customer_name: 客户名称
            content: 回复内容
            max_retries: 最大重试次数

        Returns:
            统一发送结果对象
        """
        for attempt in range(1, max_retries + 1):
            try:
                if threading.current_thread() is getattr(self, "worker_thread", None):
                    result = self.send_message_task_result(
                        customer_name,
                        content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                    )
                else:
                    future = self._submit_task(
                        self.send_message_task_result,
                        customer_name,
                        content,
                        conversation_id=conversation_id,
                        customer_id=customer_id,
                        platform=platform,
                    )
                    result = future.result(timeout=30)

                if result.success:
                    return result

                if attempt < max_retries:
                    logger.warning(f"发送失败，{2 * attempt}秒后重试 ({attempt}/{max_retries})")
                    threading.Event().wait(timeout=2 * attempt)

            except Exception as e:
                self._set_last_send_error(f"monitor_exception:{str(e)[:160]}")
                logger.error(f"发送消息失败 ({attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    threading.Event().wait(timeout=2 * attempt)

        failure_reason = getattr(self, "_last_send_error", "") or f"monitor_send_failed:{customer_name}"
        if not getattr(self, "_last_send_error", ""):
            self._set_last_send_error(failure_reason)
        logger.error(f"消息发送失败，已达到最大重试次数 ({max_retries}次): {customer_name}")
        return OutboundSendResult(
            success=False,
            channel="monitor",
            reason=failure_reason,
            resolved_name=customer_name,
        )

    def _send_reply_to_douyin(self, customer_name: str, content: str, max_retries: int = 3) -> bool:
        """发送回复到抖音（委托给统一发送入口_send_reply_to_target）"""
        return self._send_reply_to_target(customer_name, content, max_retries)
