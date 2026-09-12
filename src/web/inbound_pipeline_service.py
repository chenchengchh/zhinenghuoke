"""入站主链编排服务。

第一阶段目标是把 BotService 的入站主链职责抽离成独立服务，
但保持原有业务规则、日志语义和 monkeypatch 兼容能力不变。
"""

import contextlib
import hashlib
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from src.web.bot_service import BotService


class InboundPipelineService:
    """承接统一入站主链的轻量编排服务。"""

    REPLY_LOCK_TIMEOUT_SECONDS = 15
    REPLY_LOCK_RETRY_LIMIT = 6
    REPLY_LOCK_RETRY_BASE_DELAY_SECONDS = 5
    REPLY_LOCK_RETRY_MAX_DELAY_SECONDS = 20
    ALLOWED_INBOUND_TRIGGERS = {
        "live_inbound",
        "bus_inbound",
        "api_test_inbound",
        "direct_inbound",
    }
    BACKGROUND_ONLY_INBOUND_TRIGGERS = {
        "monitor_resume",
        "cooling_retry",
        "outbox_retry",
        "workflow_resume",
    }

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service
        self._conversation_reply_queues: dict[str, deque[dict]] = {}
        self._conversation_reply_queue_active: set[str] = set()
        self._conversation_reply_queue_lock = threading.RLock()

    @staticmethod
    def _record_perf_metric(metrics: dict[str, float], stage: str, started_at: float) -> float:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
        metrics[stage] = elapsed_ms
        return elapsed_ms

    @staticmethod
    def _log_perf_metrics(label: str, trace_id: str, metrics: dict[str, float], **extra_fields: Any) -> None:
        metric_parts = [f"{key}={value:.1f}ms" for key, value in metrics.items()]
        extra_parts = [
            f"{key}={value}"
            for key, value in extra_fields.items()
            if value is not None and value != ""
        ]
        logger.info(f"[入站主链] PERF {label} | {' | '.join([f'trace={trace_id}'] + metric_parts + extra_parts)}")

    def route_live_inbound_message(
        self,
        *,
        source: str,
        customer_name: str,
        content: str,
        direction: Any = "inbound",
        is_new: bool = True,
        conversation_id: str = "",
        platform: str = "douyin",
        customer_id: str = "",
        msg_id: str = "",
        timestamp: Any = "",
        signal_source: str = "",
        apply_echo_guard: bool = False,
        apply_self_reply_guard: bool = False,
        logical_message_id: str = "",
        direction_confidence: str = "high",
        enterprise_id: str = "",
        preferred_schema_id: str = "",
        schema_id: str = "",
        tenant_resolution_mode: str = "",
    ) -> tuple[bool, str]:
        """统一处理实时监听入站消息的过滤、去重和主链提交。"""
        bot = self.bot
        normalized_platform = platform or "douyin"
        resolve_conversation_id = getattr(bot, "_resolve_conversation_id", None)
        if callable(resolve_conversation_id):
            resolved_conversation_id = resolve_conversation_id(
                customer_name,
                normalized_platform,
                customer_id,
                conversation_id,
            )
        else:
            resolved_conversation_id = conversation_id
        computed_logical_message_id = ""
        build_logical_message_id = getattr(bot, "_build_logical_message_id", None)
        if callable(build_logical_message_id):
            computed_logical_message_id = str(
                build_logical_message_id(
                    platform=normalized_platform,
                    conversation_id=resolved_conversation_id,
                    customer_name=customer_name,
                    direction="inbound",
                    content=content,
                    source_message_id=msg_id,
                    source_timestamp=timestamp,
                )
                or ""
            ).strip()
        logical_message_id = str(logical_message_id or "").strip() or computed_logical_message_id
        decision = bot._get_monitoring_decision_service().evaluate_live_inbound_message(
            source=source,
            customer_name=customer_name,
            content=content,
            direction=direction,
            is_new=is_new,
            msg_id=msg_id,
            conversation_id=resolved_conversation_id,
            customer_id=customer_id,
            source_message_id=msg_id,
            apply_self_reply_guard=apply_self_reply_guard,
            logical_message_id=logical_message_id,
            direction_confidence=direction_confidence,
        )
        if not decision.accepted:
            # #region debug-point inbound-route-rejected
            try:
                _dbg_path = os.path.join(os.getcwd(), ".dbg", "reply-no-response.env")
                _dbg_url = "http://127.0.0.1:7777/event"
                _dbg_session = "reply-no-response"
                try:
                    with open(_dbg_path, encoding="utf-8") as _dbg_file:
                        for _dbg_line in _dbg_file:
                            if _dbg_line.startswith("DEBUG_SERVER_URL="):
                                _dbg_url = _dbg_line.split("=", 1)[1].strip() or _dbg_url
                            elif _dbg_line.startswith("DEBUG_SESSION_ID="):
                                _dbg_session = _dbg_line.split("=", 1)[1].strip() or _dbg_session
                except Exception:
                    pass
                __import__("urllib.request").request.urlopen(
                    __import__("urllib.request").request.Request(
                        _dbg_url,
                        data=__import__("json").dumps(
                            {
                                "sessionId": _dbg_session,
                                "runId": "pre-fix",
                                "hypothesisId": "H2",
                                "location": "src/web/inbound_pipeline_service.py:route_live_inbound_message",
                                "msg": "[DEBUG] inbound route rejected",
                                "data": {
                                    "source": source,
                                    "customer_name": customer_name,
                                    "conversation_id": resolved_conversation_id or "",
                                    "customer_id": customer_id or "",
                                    "msg_id": msg_id or "",
                                    "reason": str(decision.reason or ""),
                                    "logical_message_id": logical_message_id or "",
                                },
                            }
                        ).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                ).read()
            except Exception:
                pass
            # #endregion
            return False, decision.reason

        customer_name = decision.customer_name
        content = decision.content

        payload = {
            "customer_name": customer_name,
            "content": content,
            "direction": "inbound",
            "inbound_trigger": "live_inbound",
            "conversation_id": resolved_conversation_id,
            "platform": normalized_platform,
            "customer_id": customer_id,
            "msg_id": msg_id,
            "timestamp": timestamp,
            "signal_source": signal_source,
            "enterprise_id": str(enterprise_id or "").strip(),
            "preferred_schema_id": str(preferred_schema_id or "").strip(),
            "schema_id": str(schema_id or "").strip(),
            "tenant_resolution_mode": str(tenant_resolution_mode or "").strip(),
        }
        if logical_message_id:
            payload["logical_message_id"] = logical_message_id
        submitted, reason = bot.submit_inbound_message(payload)
        # #region debug-point inbound-route-submit
        try:
            _dbg_path = os.path.join(os.getcwd(), ".dbg", "reply-no-response.env")
            _dbg_url = "http://127.0.0.1:7777/event"
            _dbg_session = "reply-no-response"
            try:
                with open(_dbg_path, encoding="utf-8") as _dbg_file:
                    for _dbg_line in _dbg_file:
                        if _dbg_line.startswith("DEBUG_SERVER_URL="):
                            _dbg_url = _dbg_line.split("=", 1)[1].strip() or _dbg_url
                        elif _dbg_line.startswith("DEBUG_SESSION_ID="):
                            _dbg_session = _dbg_line.split("=", 1)[1].strip() or _dbg_session
            except Exception:
                pass
            __import__("urllib.request").request.urlopen(
                __import__("urllib.request").request.Request(
                    _dbg_url,
                    data=__import__("json").dumps(
                        {
                            "sessionId": _dbg_session,
                            "runId": "pre-fix",
                            "hypothesisId": "H2",
                            "location": "src/web/inbound_pipeline_service.py:route_live_inbound_message",
                            "msg": "[DEBUG] inbound route submit result",
                            "data": {
                                "source": source,
                                "customer_name": customer_name,
                                "conversation_id": resolved_conversation_id or "",
                                "customer_id": customer_id or "",
                                "msg_id": msg_id or "",
                                "logical_message_id": payload.get("logical_message_id", "") or "",
                                "submitted": bool(submitted),
                                "reason": str(reason or ""),
                            },
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            ).read()
        except Exception:
            pass
        # #endregion
        if not submitted and reason != "duplicate":
            trace_id = str(payload.get("logical_message_id", "") or payload.get("msg_id", ""))[:12]
            logger.warning(
                f"{source}提交统一入站主链失败: trace={trace_id or '-'} customer={customer_name}, "
                f"conversation_id={resolved_conversation_id or '-'}, customer_id={customer_id or '-'}, "
                f"source_message_id={msg_id or '-'}, logical_message_id={payload.get('logical_message_id', '') or '-'}, "
                f"reason={reason}"
            )
        return submitted, reason

    def submit_inbound_message(self, message: Any) -> tuple[bool, str]:
        """将入站消息统一提交到 inbox/workflow/回复主链。"""
        bot = self.bot
        perf_metrics: dict[str, float] = {}
        total_started_at = time.perf_counter()

        customer_name = (bot._extract_field(message, "customer_name", "") or "").strip()
        content = (bot._extract_field(message, "content", "") or "").strip()
        if not content:
            content = (
                bot._extract_field(
                    message,
                    "last_message_content",
                    bot._extract_field(message, "last_message", ""),
                )
                or ""
            ).strip()
        if not customer_name or not content:
            return False, "invalid"

        direction = bot._normalize_direction(
            bot._extract_field(message, "direction", None),
            default="unknown",
        )
        inbound_trigger = str(
            bot._extract_field(message, "inbound_trigger", "") or ""
        ).strip().lower()
        if direction != "inbound":
            logger.info(
                f"拒绝非入站消息进入主链: customer={customer_name}, direction={direction}, "
                f"conversation_id={bot._extract_field(message, 'conversation_id', '') or '-'}"
            )
            return False, "non_inbound"
        if inbound_trigger in self.BACKGROUND_ONLY_INBOUND_TRIGGERS:
            logger.warning(
                f"拒绝后台恢复/重试 trigger 直接进入入站主链: "
                f"customer={customer_name}, trigger={inbound_trigger}, "
                f"conversation_id={bot._extract_field(message, 'conversation_id', '') or '-'}"
            )
            return False, "background_trigger_blocked"
        if inbound_trigger not in self.ALLOWED_INBOUND_TRIGGERS:
            logger.warning(
                f"拒绝未知入站 trigger 进入主链: customer={customer_name}, "
                f"trigger={inbound_trigger or '-'}, "
                f"conversation_id={bot._extract_field(message, 'conversation_id', '') or '-'}"
            )
            return False, "invalid_inbound_trigger"

        if bot._is_non_user_message(message) or bot._is_invalid_message(content):
            logger.info(
                f"拒绝系统/助手/无效消息进入主链: customer={customer_name}, "
                f"conversation_id={bot._extract_field(message, 'conversation_id', '') or '-'}, content={content[:20]}"
            )
            return False, "non_user_or_invalid"

        platform = bot._extract_field(message, "platform", "douyin") or "douyin"
        enterprise_id = str(
            bot._extract_field(
                message,
                "enterprise_id",
                bot._extract_field(
                    message,
                    "enterpriseId",
                    bot._extract_field(
                        message,
                        "tenant_id",
                        bot._extract_field(message, "tenantId", ""),
                    ),
                ),
            )
            or ""
        ).strip()
        preferred_schema_id = str(
            bot._extract_field(
                message,
                "preferred_schema_id",
                bot._extract_field(
                    message,
                    "preferredSchemaId",
                    bot._extract_field(
                        message,
                        "schema_id",
                        bot._extract_field(message, "schemaId", ""),
                    ),
                ),
            )
            or ""
        ).strip()
        schema_id = str(
            bot._extract_field(
                message,
                "schema_id",
                bot._extract_field(message, "schemaId", ""),
            )
            or ""
        ).strip()
        if not preferred_schema_id and schema_id:
            preferred_schema_id = schema_id
        tenant_resolution_mode = str(
            bot._extract_field(
                message,
                "tenant_resolution_mode",
                bot._extract_field(message, "tenantResolutionMode", ""),
            )
            or ""
        ).strip()
        customer_id = bot._extract_field(
            message,
            "customer_id",
            bot._extract_field(message, "sender_id", ""),
        ) or ""
        msg_id = (
            bot._extract_field(
                message,
                "msg_id",
                bot._extract_field(message, "message_id", ""),
            )
            or f"synth_{hashlib.md5(f'{customer_name}:{content}'.encode('utf-8')).hexdigest()[:12]}"
        )
        explicit_conversation_id = bot._extract_field(message, "conversation_id", "") or ""
        source_timestamp = bot._extract_field(
            message,
            "timestamp",
            bot._extract_field(message, "created_at", ""),
        )
        conversation_id = bot._resolve_conversation_id(
            customer_name,
            platform,
            customer_id,
            explicit_conversation_id,
        )
        incoming_logical_message_id = str(
            bot._extract_field(message, "logical_message_id", "") or ""
        ).strip()
        logical_message_id = incoming_logical_message_id or bot._build_logical_message_id(
            platform=platform,
            conversation_id=conversation_id,
            customer_name=customer_name,
            direction="inbound",
            content=content,
            source_message_id=msg_id,
            source_timestamp=source_timestamp,
        )
        trace_id = ""
        existing_source_event = {}
        stage_started_at = time.perf_counter()
        with contextlib.suppress(Exception):
            if msg_id:
                existing_source_event = bot.db.get_inbound_event_by_source_message_id(
                    msg_id,
                    conversation_id=conversation_id,
                ) or {}
        self._record_perf_metric(perf_metrics, "lookup_duplicate", stage_started_at)
        if existing_source_event:
            existing_content = str(existing_source_event.get("content", "") or "").strip()
            existing_logical_id = str(existing_source_event.get("logical_message_id", "") or "").strip()
            if existing_content and existing_content != content:
                logger.info(
                    f"检测到同source_message_id但内容不同，视为DOM位置复用的新消息: "
                    f"customer={customer_name}, msg_id={msg_id}, "
                    f"old_content={existing_content[:20]}..., new_content={content[:20]}..."
                )
            else:
                logger.info(
                    f"检测到已处理过的 source_message_id，跳过重复入站: "
                    f"customer={customer_name}, msg_id={msg_id}, "
                    f"conversation_id={conversation_id}, logical_id={existing_source_event.get('logical_message_id', '')}, "
                    f"trace={str(existing_source_event.get('logical_message_id', '') or msg_id or '')[:12]}"
                )
                return False, "duplicate"

        trace_id = logical_message_id[:12]
        stage_started_at = time.perf_counter()
        reserved = bot.db.try_reserve_inbound_event(
            logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            content=content,
            source_message_id=msg_id,
            platform=platform,
        )
        if not reserved:
            existing_event = {}
            with contextlib.suppress(Exception):
                existing_event = bot.db.get_inbound_event(logical_message_id) or {}

            if bot._should_split_duplicate_inbound_event(
                existing_event=existing_event,
                incoming_source_message_id=msg_id,
            ):
                source_specific_logical_id = bot._build_source_specific_inbound_logical_message_id(
                    logical_message_id,
                    msg_id,
                )
                reserved = bot.db.try_reserve_inbound_event(
                    source_specific_logical_id,
                    conversation_id=conversation_id,
                    customer_name=customer_name,
                    content=content,
                    source_message_id=msg_id,
                    platform=platform,
                )
                if reserved:
                    logger.info(
                        f"检测到同内容同时间桶但不同真实消息ID，拆分为独立入站事件: "
                        f"customer={customer_name}, conversation_id={conversation_id}, "
                        f"logical_id={source_specific_logical_id}, source_message_id={msg_id}"
                    )
                    logical_message_id = source_specific_logical_id
                    trace_id = logical_message_id[:12]
        self._record_perf_metric(perf_metrics, "reserve_event", stage_started_at)

        if not reserved:
            logger.info(
                f"逻辑事件已处理，跳过重复入站消息: customer={customer_name}, "
                f"conversation_id={conversation_id}, source_message_id={msg_id}, "
                f"logical_id={logical_message_id}, trace={trace_id or '-'}"
            )
            return False, "duplicate"

        stage_started_at = time.perf_counter()
        workflow_run_id = bot._extract_field(message, "workflow_run_id", "") or bot.message_workflow_manager.start_run(
            logical_message_id=logical_message_id,
            conversation_id=conversation_id,
            customer_name=customer_name,
            content=content,
            customer_id=customer_id,
            platform=platform,
            enterprise_id=enterprise_id,
            preferred_schema_id=preferred_schema_id,
            schema_id=schema_id,
            tenant_resolution_mode=tenant_resolution_mode,
            inbound_trigger=inbound_trigger,
        )
        self._record_perf_metric(perf_metrics, "start_workflow", stage_started_at)
        msg_dict = {
            "customer_name": customer_name,
            "content": content,
            "direction": "inbound",
            "inbound_trigger": inbound_trigger,
            "conversation_id": conversation_id,
            "platform": platform,
            "customer_id": customer_id,
            "is_new": True,
            "msg_id": msg_id,
            "logical_message_id": logical_message_id,
            "workflow_run_id": workflow_run_id,
            "signal_source": str(bot._extract_field(message, "signal_source", "") or ""),
            "enterprise_id": enterprise_id,
            "preferred_schema_id": preferred_schema_id,
            "schema_id": schema_id,
            "tenant_resolution_mode": tenant_resolution_mode,
            "_pipeline_started_at": total_started_at,
        }

        logger.info(
            f"[入站主链] accepted trace={trace_id} customer={customer_name} "
            f"conversation_id={conversation_id} msg_id={msg_id} "
            f"logical_message_id={logical_message_id} signal_source={msg_dict['signal_source'] or '-'} "
            f"trigger={inbound_trigger or '-'}"
        )

        monitoring_available, monitoring_source = bot._can_accept_inbound_auto_reply()
        if not monitoring_available:
            stage_started_at = time.perf_counter()
            self._queue_monitor_inactive_inbound(
                msg_dict,
                monitor_reason=monitoring_source,
            )
            self._record_perf_metric(perf_metrics, "queue_monitor_inactive", stage_started_at)
            self._record_perf_metric(perf_metrics, "total", total_started_at)
            self._log_perf_metrics(
                "submit_inbound_message",
                trace_id,
                perf_metrics,
                customer=customer_name,
                conversation_id=conversation_id,
                result="queued_monitor_inactive",
            )
            return True, "queued_monitor_inactive"

        if bot._update_marketing_tracking_status(
            status="replied",
            customer_name=customer_name,
            conversation_id=conversation_id,
            allowed_current_statuses=["sent", "delivered"],
        ):
            logger.debug(
                f"[入站主链] 营销 tracking 已回写 replied: customer={customer_name}, conversation_id={conversation_id}"
            )

        stage_started_at = time.perf_counter()
        if bot._submit_inbound_reply_job(msg_dict):
            self._record_perf_metric(perf_metrics, "submit_reply_job", stage_started_at)
            self._record_perf_metric(perf_metrics, "total", total_started_at)
            self._log_perf_metrics(
                "submit_inbound_message",
                trace_id,
                perf_metrics,
                customer=customer_name,
                conversation_id=conversation_id,
                result="submitted",
            )
            return True, "submitted"
        self._record_perf_metric(perf_metrics, "submit_reply_job", stage_started_at)
        self._record_perf_metric(perf_metrics, "total", total_started_at)
        self._log_perf_metrics(
            "submit_inbound_message",
            trace_id,
            perf_metrics,
            customer=customer_name,
            conversation_id=conversation_id,
            result="failed",
        )
        return False, "failed"

    def _queue_monitor_inactive_inbound(self, msg_dict: dict, *, monitor_reason: str = "") -> None:
        """监听不可用时只落持久化待办，不直接进入发送，避免漏单与误发。"""
        bot = self.bot
        trace_id = str(msg_dict.get("logical_message_id", "") or "")[:12]
        workflow_run_id = str(msg_dict.get("workflow_run_id", "") or "").strip()
        reason_suffix = str(monitor_reason or "inactive").strip() or "inactive"
        paused_reason = f"monitor_inactive:{reason_suffix}"[:200]

        self.persist_inbound_message(msg_dict)

        if workflow_run_id:
            with contextlib.suppress(Exception):
                bot.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "paused",
                        "paused_reason": paused_reason,
                        "current_node": "ingest",
                    }
                )
            with contextlib.suppress(Exception):
                bot.message_workflow_manager.record_node(
                    workflow_run_id,
                    "ingest",
                    status="paused",
                    metadata={"reason": paused_reason},
                )

        logger.info(
            f"[入站主链] monitor_inactive_deferred trace={trace_id or '-'} "
            f"customer={msg_dict.get('customer_name', '')} "
            f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
            f"source_message_id={msg_dict.get('msg_id', '') or '-'} "
            f"workflow_run_id={workflow_run_id or '-'} reason={paused_reason}"
        )

    def submit_inbound_reply_job(self, msg_dict: dict, max_retries: int = 3) -> bool:
        """把统一入站事件接到持久化+回复执行主链，并处理提交重试。"""
        bot = self.bot
        message_saved = bool(msg_dict.get("_message_already_persisted"))
        retry_count = msg_dict.get("_submit_retry_count", 0)
        trace_id = str(msg_dict.get("logical_message_id", "") or "")[:12]
        customer_name = msg_dict.get("customer_name", "")
        perf_metrics: dict[str, float] = {}
        total_started_at = time.perf_counter()

        if retry_count > max_retries:
            logger.error(f"[入站主链] submit_retry_exhausted trace={trace_id} customer={customer_name}")
            bot._fail_inbound_submission(msg_dict, "submit_retry_exhausted")
            return False

        try:
            if not message_saved:
                logger.info(
                    f"[入站主链] persist_start trace={trace_id} customer={customer_name} "
                    f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                    f"source_message_id={msg_dict.get('msg_id', '') or '-'} "
                    f"logical_message_id={msg_dict.get('logical_message_id', '') or '-'}"
                )
                stage_started_at = time.perf_counter()
                bot._persist_inbound_message(msg_dict)
                message_saved = True
                self._record_perf_metric(perf_metrics, "persist_message", stage_started_at)
                logger.info(
                    f"[入站主链] persist_done trace={trace_id} customer={customer_name} "
                    f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                    f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
                )

            try:
                logger.info(
                    f"[入站主链] execute_submit trace={trace_id} customer={customer_name} "
                    f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                    f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
                )
                stage_started_at = time.perf_counter()
                self._enqueue_inbound_reply_job(msg_dict)
                self._record_perf_metric(perf_metrics, "queue_execute", stage_started_at)
                pipeline_started_at = msg_dict.get("_pipeline_started_at")
                if isinstance(pipeline_started_at, (int, float)) and pipeline_started_at > 0:
                    self._record_perf_metric(perf_metrics, "accepted_to_queued", pipeline_started_at)
                logger.info(
                    f"[入站主链] execute_queued trace={trace_id} customer={customer_name} "
                    f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                    f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
                )
                self._record_perf_metric(perf_metrics, "total", total_started_at)
                self._log_perf_metrics(
                    "submit_inbound_reply_job",
                    trace_id,
                    perf_metrics,
                    customer=customer_name,
                    retry=retry_count,
                    result="queued",
                )
                return True
            except Exception as submit_e:
                retry_msg = dict(msg_dict)
                retry_msg["_submit_retry_count"] = retry_count + 1
                delay = min(2.0 * (retry_count + 1), 10)
                logger.warning(
                    f"[入站主链] execute_submit_failed trace={trace_id} customer={customer_name} "
                    f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                    f"source_message_id={msg_dict.get('msg_id', '') or '-'} "
                    f"delay={delay}s retry={retry_count+1}/{max_retries} error={submit_e}"
                )
                if retry_count + 1 > max_retries:
                    logger.error(
                        f"[入站主链] retry_exceeded trace={trace_id} customer={customer_name} "
                        f"max_retries={max_retries}"
                    )
                    bot._fail_inbound_submission(msg_dict, "execute_retry_exceeded")
                    return False
                try:
                    bot._reply_executor.submit(bot._delayed_reply_retry, retry_msg, delay, 0)
                except Exception:
                    logger.error(
                        f"[入站主链] execute_retry_submit_failed trace={trace_id} customer={customer_name} "
                        f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                        f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
                    )
                    bot._fail_inbound_submission(msg_dict, "execute_retry_submit_failed")
                    return False
                return True

        except Exception as exc:
            retry_msg = dict(msg_dict)
            retry_msg["_submit_retry_count"] = retry_count + 1
            error_str = str(exc).lower()
            if "数据库" in error_str or "database" in error_str or "sqlite" in error_str:
                delay = min(3.0 * (retry_count + 1), 15)
            elif "网络" in error_str or "network" in error_str or "timeout" in error_str or "connection" in error_str:
                delay = min(1.0 * (retry_count + 1), 5)
            else:
                delay = min(0.5 * (retry_count + 1), 3)
            logger.warning(
                f"[入站主链] submit_failed trace={trace_id} customer={customer_name} "
                f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                f"source_message_id={msg_dict.get('msg_id', '') or '-'} "
                f"delay={delay}s retry={retry_count+1}/{max_retries} error={exc}"
            )
            if retry_count < max_retries:
                try:
                    bot._reply_executor.submit(bot._delayed_reply_retry, retry_msg, delay, 0)
                    return True
                except Exception:
                    logger.error(
                        f"[入站主链] submit_retry_schedule_failed trace={trace_id} customer={customer_name} "
                        f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                        f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
                    )
                    bot._fail_inbound_submission(msg_dict, "submit_retry_schedule_failed")
                    return False
            logger.error(
                f"[入站主链] submit_failed_final trace={trace_id} customer={customer_name} "
                f"conversation_id={msg_dict.get('conversation_id', '') or '-'} "
                f"source_message_id={msg_dict.get('msg_id', '') or '-'} error={exc}"
            )
            bot._fail_inbound_submission(msg_dict, f"submit_failed:{str(exc)[:80]}")
            return False

    def _enqueue_inbound_reply_job(self, msg_dict: dict) -> None:
        """按会话串行排队入站回复，避免高频同会话靠锁超时重试。"""
        conversation_id = str(msg_dict.get("conversation_id", "") or "").strip()
        if not conversation_id:
            direct_msg = dict(msg_dict)
            direct_msg["_conversation_queue_dispatching"] = True
            self.bot._reply_executor.submit(self.bot._execute_inbound_reply_job, direct_msg)
            return

        should_start_worker = False
        queued_size = 0
        with self._conversation_reply_queue_lock:
            queue = self._conversation_reply_queues.setdefault(conversation_id, deque())
            queue.append(dict(msg_dict))
            queued_size = len(queue)
            if conversation_id not in self._conversation_reply_queue_active:
                self._conversation_reply_queue_active.add(conversation_id)
                should_start_worker = True

        logger.info(
            f"[入站主链] conversation_queue_enqueued customer={msg_dict.get('customer_name', '')} "
            f"conversation_id={conversation_id} queued={queued_size} "
            f"source_message_id={msg_dict.get('msg_id', '') or '-'}"
        )
        if should_start_worker:
            self.bot._reply_executor.submit(
                self._drain_conversation_reply_queue,
                conversation_id,
            )

    def _drain_conversation_reply_queue(self, conversation_id: str) -> None:
        """单 worker 串行消费同一会话的回复任务。"""
        while True:
            next_message: dict | None = None
            remaining = 0
            with self._conversation_reply_queue_lock:
                queue = self._conversation_reply_queues.get(conversation_id)
                if queue:
                    next_message = queue.popleft()
                    remaining = len(queue)
                if next_message is None:
                    self._conversation_reply_queue_active.discard(conversation_id)
                    if queue is not None and not queue:
                        self._conversation_reply_queues.pop(conversation_id, None)
                    return

            dispatch_message = dict(next_message)
            dispatch_message["_conversation_queue_dispatching"] = True
            logger.info(
                f"[入站主链] conversation_queue_dispatch customer={dispatch_message.get('customer_name', '')} "
                f"conversation_id={conversation_id} remaining={remaining} "
                f"source_message_id={dispatch_message.get('msg_id', '') or '-'}"
            )
            try:
                self.execute_inbound_reply_job(dispatch_message)
            except Exception as drain_exc:
                logger.error(
                    f"[入站主链] conversation_queue_dispatch异常: "
                    f"conversation_id={conversation_id}, error={drain_exc}"
                )
                try:
                    customer_name = dispatch_message.get("customer_name", "")
                    content = dispatch_message.get("content", "")
                    msg_id = dispatch_message.get("msg_id", "")
                    logical_message_id = dispatch_message.get("logical_message_id", "")
                    self.bot._mark_message_failed(
                        customer_name, content, "queue_dispatch_error",
                        msg_id=msg_id, logical_message_id=logical_message_id,
                    )
                except Exception:
                    pass

    def persist_inbound_message(self, message: Any) -> None:
        """统一入站主链的持久化节点：保存消息并刷新会话。"""
        bot = self.bot

        try:
            if isinstance(message, dict):
                content = message.get("content", message.get("last_message_content", ""))
                message_id = message.get("message_id", message.get("msg_id", f"msg_{uuid.uuid4().hex[:16]}"))
                conversation_id = message.get("conversation_id", "")
                raw_direction = message.get("direction", None)
                direction = bot._normalize_direction(raw_direction, default="unknown")
                sender_id = message.get("customer_id", message.get("sender_id", ""))
                sender_name = message.get("customer_name", message.get("sender_name", ""))
                platform = message.get("platform", "douyin")
                raw_ts = message.get("timestamp", datetime.now())
            else:
                content = getattr(message, "content", "") or ""
                message_id = getattr(message, "message_id", None) or getattr(message, "msg_id", None) or f"msg_{uuid.uuid4().hex[:16]}"
                conversation_id = getattr(message, "conversation_id", "")
                raw_direction = getattr(message, "direction", None)
                direction = bot._normalize_direction(raw_direction, default="unknown")
                sender_id = getattr(message, "sender_id", "")
                sender_name = getattr(message, "sender_name", "") or getattr(message, "customer_name", "")
                platform = getattr(message, "platform", "douyin")
                raw_ts = getattr(message, "timestamp", datetime.now())

            if isinstance(raw_ts, (int, float)) and raw_ts > 0:
                if raw_ts > 1e12:
                    raw_ts = datetime.fromtimestamp(raw_ts / 1000)
                else:
                    raw_ts = datetime.fromtimestamp(raw_ts)
            if isinstance(raw_ts, datetime):
                timestamp = raw_ts.isoformat()
            else:
                timestamp = str(raw_ts)

            if not content or not content.strip():
                logger.debug(f"跳过空消息: conversation_id={conversation_id}")
                return

            if not sender_name or not sender_name.strip():
                sender_name = sender_name or "unknown"

            if direction != "inbound":
                logger.info(f"跳过非入站消息保存: sender={sender_name}, direction={direction}")
                return

            if bot._is_non_user_message(message):
                logger.info(f"跳过系统/助手消息保存: sender={sender_name}")
                return

            # 自回复回显检测：如果内容与我方最近的出站回复匹配，跳过保存
            # 防止 DOM 轮询将我方回复误判为用户新消息后写入 DB，导致 LLM 历史污染
            if bot._is_recently_sent_by_us(sender_name, content):
                logger.info(
                    f"跳过自回复回显保存: sender={sender_name}, "
                    f"content={content[:30]}... (与我方最近出站回复匹配)"
                )
                return

            if not conversation_id and sender_name:
                conversation_id = bot._resolve_conversation_id(sender_name, platform, sender_id)
            logical_message_id = (
                message.get("logical_message_id")
                if isinstance(message, dict)
                else getattr(message, "logical_message_id", "")
            ) or bot._build_logical_message_id(
                platform=platform,
                conversation_id=conversation_id,
                customer_name=sender_name,
                direction=direction,
                content=content,
                source_message_id=message_id,
                source_timestamp=timestamp,
            )

            if not timestamp:
                timestamp = datetime.now().isoformat()

            logger.info(f"收到新消息: {content[:50] if content else '(空)'}... [conversation_id={conversation_id}]")

            message_data = {
                "message_id": message_id,
                "logical_message_id": logical_message_id,
                "source_message_id": message_id,
                "conversation_id": conversation_id,
                "direction": direction,
                "message_type": "text",
                "content": content,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "platform": platform,
                "is_read": False,
                "is_processed": False,
                "created_at": timestamp,
                "enterprise_id": str(
                    (
                        message.get("enterprise_id", "")
                        if isinstance(message, dict)
                        else getattr(message, "enterprise_id", "")
                    )
                    or ""
                ).strip(),
                "preferred_schema_id": str(
                    (
                        message.get("preferred_schema_id", "")
                        if isinstance(message, dict)
                        else getattr(message, "preferred_schema_id", "")
                    )
                    or ""
                ).strip(),
                "schema_id": str(
                    (
                        message.get("schema_id", "")
                        if isinstance(message, dict)
                        else getattr(message, "schema_id", "")
                    )
                    or ""
                ).strip(),
                "tenant_resolution_mode": str(
                    (
                        message.get("tenant_resolution_mode", "")
                        if isinstance(message, dict)
                        else getattr(message, "tenant_resolution_mode", "")
                    )
                    or ""
                ).strip(),
            }
            try:
                bot.db.save_message(message_data)
            except Exception as msg_exc:
                logger.error(f"保存消息到数据库失败: {msg_exc}")

            conversation_data = {
                "conversation_id": conversation_id,
                "platform": platform,
                "customer_id": sender_id or sender_name,
                "customer_name": sender_name,
                "last_message_content": content,
                "last_message_time": timestamp,
            }
            try:
                bot.db.save_conversation(conversation_data)
                bot._maybe_merge_identity_conversations(
                    customer_name=sender_name,
                    customer_id=sender_id,
                    platform=platform,
                    conversation_id=conversation_id,
                )
            except Exception as conv_exc:
                logger.error(f"保存会话到数据库失败: {conv_exc}")

            try:
                refresh_follow_up = getattr(bot, "_refresh_follow_up_reminder", None)
                if callable(refresh_follow_up) and conversation_id:
                    refresh_follow_up(conversation_id)
            except Exception as follow_up_exc:
                logger.debug(f"实时刷新待跟进提醒失败: {follow_up_exc}")

        except Exception as exc:
            logger.error(f"处理新消息失败: {exc}")

    def execute_inbound_reply_job(self, message: dict, _retry_depth: int = 0) -> None:
        """统一入站主链的回复执行节点，带会话级锁防止同一会话并发回复。"""
        bot = self.bot
        monitoring_available, _ = bot._can_accept_inbound_auto_reply()
        if not monitoring_available:
            logger.info(f"自动回复未运行，消息保留待后续处理: {message.get('customer_name', '')}")
            return

        conversation_id = message.get("conversation_id", "")
        customer_name = message.get("customer_name", "")
        content = message.get("content", "") or message.get("last_message_content", "")
        msg_id = message.get("msg_id", "")
        if conversation_id and not bool(message.get("_conversation_queue_dispatching")):
            message["_conversation_queue_dispatching"] = True
            self._enqueue_inbound_reply_job(message)
            return
        logger.info(f"[自动回复] 开始处理: customer={customer_name}, conversation={conversation_id}, content={str(content)[:30]}...")
        lock = bot._get_reply_lock(conversation_id)
        if conversation_id and bool(message.get("_conversation_queue_dispatching")):
            acquired = lock.acquire()
        else:
            acquired = lock.acquire(timeout=self.REPLY_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            customer_name = message.get("customer_name", "")
            content = message.get("content", message.get("last_message_content", ""))
            retry_count = int(message.get("_retry_count", 0) or 0) + 1
            delay = min(
                self.REPLY_LOCK_RETRY_BASE_DELAY_SECONDS * retry_count,
                self.REPLY_LOCK_RETRY_MAX_DELAY_SECONDS,
            )
            logger.warning(
                f"获取会话锁超时({self.REPLY_LOCK_TIMEOUT_SECONDS}s)，延迟重试: "
                f"{conversation_id}, customer={customer_name}, retry={retry_count}/{self.REPLY_LOCK_RETRY_LIMIT}, "
                f"delay={delay}s"
            )
            bot._release_reply_lock_ref(conversation_id)
            try:
                retry_msg = dict(message)
                retry_msg["_retry_count"] = retry_count
                if retry_count <= self.REPLY_LOCK_RETRY_LIMIT:
                    bot._reply_executor.submit(bot._delayed_reply_retry, retry_msg, delay, 0)
                else:
                    logger.warning(f"锁超时重试已达上限，放弃: {conversation_id}")
                    if customer_name and content:
                        bot._mark_message_failed(
                            customer_name,
                            content,
                            msg_id,
                            conversation_id=conversation_id,
                            customer_id=message.get("customer_id", ""),
                            logical_message_id=message.get("logical_message_id", ""),
                            source_message_id=msg_id,
                            retry_count=retry_count,
                        )
            except Exception:
                if customer_name and content:
                    bot._mark_message_failed(
                        customer_name,
                        content,
                        msg_id,
                        conversation_id=conversation_id,
                        customer_id=message.get("customer_id", ""),
                        logical_message_id=message.get("logical_message_id", ""),
                        source_message_id=msg_id,
                        retry_count=retry_count,
                    )
            return
        try:
            bot._run_inbound_reply_pipeline(message)
        except Exception as exc:
            logger.error(f"自动回复执行异常: {conversation_id}, error={exc}")
            customer_name = message.get("customer_name", "")
            content = message.get("content", message.get("last_message_content", ""))
            if customer_name and content:
                existing_retry = int(message.get("_retry_count", 0) or 0)
                if existing_retry < 2:
                    retry_msg = dict(message)
                    retry_msg["_retry_count"] = existing_retry + 1
                    delay = min(2.0 * (existing_retry + 1), 8)
                    try:
                        bot._reply_executor.submit(
                            bot._delayed_reply_retry, retry_msg, delay, 0
                        )
                        logger.info(f"自动回复异常后安排重试({existing_retry + 1}/2): {conversation_id}")
                    except Exception:
                        bot._mark_message_failed(
                            customer_name,
                            content,
                            msg_id,
                            conversation_id=conversation_id,
                            customer_id=message.get("customer_id", ""),
                            logical_message_id=message.get("logical_message_id", ""),
                            source_message_id=msg_id,
                            retry_count=existing_retry + 1,
                        )
                else:
                    bot._mark_message_failed(
                        customer_name,
                        content,
                        msg_id,
                        conversation_id=conversation_id,
                        customer_id=message.get("customer_id", ""),
                        logical_message_id=message.get("logical_message_id", ""),
                        source_message_id=msg_id,
                        retry_count=existing_retry + 1,
                    )
        finally:
            lock.release()
            bot._release_reply_lock_ref(conversation_id)
