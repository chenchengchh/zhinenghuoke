"""监听源适配层。

阶段3收口目标：
- 对外只保留 start() / stop() / poll_once() / health()
- BotService 不再知道 RPA / traditional 两种监听模式细节
- 内部兼容 fallback 能力，但只在内部
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from src.web.inbound_decision_engine import InboundDecision, InboundDecisionEngine, InboundEvidence
from src.web.inbound_dispatcher import (
    InboundDispatcher,
    InboundDispatchResult,
    InboundRouteRequest,
)
from src.web.inbound_detector import InboundDetectionResult, InboundDetector
from src.web.outbound_send_gateway import OutboundSendGateway, OutboundSendResult

if TYPE_CHECKING:
    from src.web.bot_service import BotService

class InboundSourceAdapter:
    """统一封装 RPA 与传统监听源分派。"""

    def __init__(self, bot_service: "BotService"):
        self.bot = bot_service
        self._inbound_detector = InboundDetector()
        self._inbound_dispatcher = InboundDispatcher(bot_service)
        self._outbound_send_gateway = OutboundSendGateway(bot_service)
        self._decision_engine = InboundDecisionEngine()
        # [REFACTOR-INST:convergence] 入站模式:
        #   callback（默认）：RPA 走 callback，轮询 evidence 跳过
        #   poll：RPA 走轮询 evidence，不注册 callback
        #   hybrid：双链路并存（不推荐，易重复消费）
        self._inbound_mode = os.environ.get("HUOKE_INBOUND_MODE", "callback").strip().lower()

    # ==================== 统一公共接口 ====================

    def start(self) -> bool:
        """启动监听源（统一入口）。"""
        return self.start_monitoring_sources()

    def stop(self) -> None:
        """停止所有监听源（统一入口）。"""
        self.stop_monitoring_sources()

    def health(self) -> Dict[str, bool]:
        """查询监听健康状态（统一入口）。"""
        return self.get_monitoring_status()

    def start_monitoring_sources(self) -> bool:
        """启动监听源，优先 RPA，失败后回退传统模式。"""
        bot = self.bot
        logger.info(
            f"启动消息回复实现: _use_rpa_mode={getattr(bot, '_use_rpa_mode', False)}, "
            f"rpa_launcher={getattr(bot, 'rpa_launcher', None) is not None}"
        )

        started = False
        if getattr(bot, "_use_rpa_mode", False):
            started = self._start_rpa_monitoring()

        if not started and getattr(bot, "message_monitor", None):
            started = self._start_traditional_monitoring()

        return started

    def stop_monitoring_sources(self) -> None:
        """停止所有监听源。

        [简化] 收敛前：只设置 launcher._is_running = False，但不调用 launcher.stop()，
        导致 rpa_engine 不会被设为 None，下次启动时可能复用不一致的实例。
        收敛后：调用 launcher.stop() 正确清理 rpa_engine，下次启动时重新 setup。
        """
        bot = self.bot
        message_monitor = getattr(bot, "message_monitor", None)
        if message_monitor:
            try:
                message_monitor.stop()
                logger.info("message_monitor.stop() 已调用")
            except Exception as exc:
                logger.error(f"停止 message_monitor 失败: {exc}")

        # 直接停止 RPA 引擎（不依赖 launcher.stop()，避免 rpa_engine 被提前设为 None）
        if getattr(bot, "_use_rpa_mode", False):
            launcher = getattr(bot, "rpa_launcher", None)
            if launcher:
                rpa_engine = getattr(launcher, "rpa_engine", None)
                if rpa_engine:
                    try:
                        rpa_engine.stop_monitoring()
                        logger.info("RPA引擎.stop_monitoring() 已调用")
                    except Exception as exc:
                        logger.warning(f"停止RPA引擎失败: {exc}")
                # [简化] 调用 launcher.stop() 正确清理 rpa_engine 实例，
                # 下次启动时 _start_rpa_monitoring 会自动重新 setup
                try:
                    launcher.stop()
                    logger.info("RPA launcher.stop() 已调用，rpa_engine 已清理")
                except Exception as exc:
                    logger.warning(f"停止RPA launcher失败: {exc}")

    def get_monitoring_status(self) -> Dict[str, bool]:
        """统一计算监听状态。"""
        bot = self.bot
        launcher = getattr(bot, "rpa_launcher", None)
        rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None
        rpa_monitoring = bool(
            getattr(bot, "_use_rpa_mode", False)
            and rpa_engine
            and getattr(rpa_engine, "is_monitoring", False)
        )
        traditional_monitoring = bool(
            getattr(bot, "_monitor_active", False)
            and getattr(bot, "message_monitor", None) is not None
            and getattr(bot.message_monitor, "_running", False)
            and getattr(bot.message_monitor, "_chat_page_ensured", False)
        )
        rpa_monitoring = bool(
            rpa_monitoring
            and getattr(rpa_engine, "_chat_page_ensured", False)
            and getattr(rpa_engine, "_login_valid", False)
        )
        state = bot._get_runtime_state_snapshot()
        return {
            "is_monitoring": state["is_monitoring_messages"],
            "rpa_monitoring": rpa_monitoring,
            "traditional_monitoring": traditional_monitoring,
            "effective_monitoring": rpa_monitoring or traditional_monitoring,
        }

    def poll_once(self) -> None:
        """执行一次监听轮询，按当前可用源统一检测并分发。"""
        detections, source = self.detect_inbound_results_once()
        self._emit_poll_cycle_debug_point(source, detections)
        if detections:
            self.dispatch_detected_inbound_events(detections, source=source)

    def _emit_poll_cycle_debug_point(
        self,
        source: str,
        detections: List[InboundDetectionResult],
    ) -> None:
        """[DEBUG-INSTR:poll-cycle] 上报轮询周期埋点。
        
        收敛前：45 行内联在 poll_once 中，含 4 次 __import__ 反射调用。
        收敛后：单方法，所有反射调用在 import 阶段完成。
        """
        now = time.time()
        last = float(getattr(self, "_reply_no_response_last_poll_debug_at", 0.0) or 0.0)
        if not (detections or (source and (now - last) >= 15)):
            return
        setattr(self, "_reply_no_response_last_poll_debug_at", now)
        try:
            url, session = self._read_debug_endpoint()
            status = self.get_monitoring_status()
            payload = {
                "sessionId": session,
                "runId": "pre-fix",
                "hypothesisId": "H1",
                "location": "src/web/inbound_source_adapter.py:poll_once",
                "msg": "[DEBUG] inbound poll cycle",
                "data": {
                    "source": source or "",
                    "detection_count": len(detections or []),
                    "customers": [str(getattr(item, "customer_name", "") or "") for item in (detections or [])[:5]],
                    "effective_monitoring": bool(status.get("effective_monitoring")),
                    "rpa_monitoring": bool(status.get("rpa_monitoring")),
                    "traditional_monitoring": bool(status.get("traditional_monitoring")),
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req).read()
        except Exception:
            pass

    @staticmethod
    def _read_debug_endpoint() -> tuple[str, str]:
        """从 .dbg/reply-no-response.env 读取 DEBUG_SERVER_URL / DEBUG_SESSION_ID。"""
        url = "http://127.0.0.1:7777/event"
        session = "reply-no-response"
        path = os.path.join(os.getcwd(), ".dbg", "reply-no-response.env")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("DEBUG_SERVER_URL="):
                        url = line.split("=", 1)[1].strip() or url
                    elif line.startswith("DEBUG_SESSION_ID="):
                        session = line.split("=", 1)[1].strip() or session
        except Exception:
            pass
        return url, session

    def detect_inbound_results_once(self) -> tuple[List[InboundDetectionResult], str]:
        """执行一次统一检测，返回已归并的入站检测结果。"""
        bot = self.bot
        current_time = time.time()
        use_rpa_mode = bool(getattr(bot, "_use_rpa_mode", False))
        launcher = getattr(bot, "rpa_launcher", None)
        rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None
        rpa_available = bool(use_rpa_mode and rpa_engine and getattr(rpa_engine, "is_monitoring", False))

        if use_rpa_mode and not rpa_available:
            rpa_available = self._recover_or_fallback_rpa(current_time)

        if rpa_available:
            evidence = self.collect_evidence_from_all_sources(network_truth_only=True)
            decisions = self.decide_from_evidence(evidence)
            return self._decisions_to_detection_results(decisions), "RPA轮询"

        if not getattr(bot, "_monitor_active", False):
            return [], ""

        evidence = self.collect_evidence_from_all_sources(network_truth_only=False)
        decisions = self.decide_from_evidence(evidence)
        if decisions:
            return self._decisions_to_detection_results(decisions), "传统监听轮询"
        events = self._collect_traditional_inbound_events()
        return self.merge_detection_results(events), "传统监听轮询"

    def collect_evidence_from_all_sources(self, *, network_truth_only: bool = False) -> list[InboundEvidence]:
        """从所有可用源收集原始证据，不判定，只采集。"""
        evidence_list: list[InboundEvidence] = []
        bot = self.bot

        rpa_evidence = self._collect_rpa_evidence()
        evidence_list.extend(rpa_evidence)

        api_evidence = self._collect_api_evidence()
        evidence_list.extend(api_evidence)

        if not network_truth_only:
            traditional_evidence = self._collect_traditional_evidence()
            evidence_list.extend(traditional_evidence)

        return evidence_list

    def decide_from_evidence(
        self,
        evidence_list: list[InboundEvidence],
        *,
        prev_state: Optional[dict[str, Any]] = None,
    ) -> list[InboundDecision]:
        """将原始证据提交给 InboundDecisionEngine 做统一决策。"""
        if not evidence_list:
            return []

        grouped: dict[str, list[InboundEvidence]] = {}
        for evidence in evidence_list:
            key = str(evidence.conversation_id or evidence.customer_name or "").strip()
            if not key:
                continue
            grouped.setdefault(key, []).append(evidence)

        decisions: list[InboundDecision] = []
        for key, bundle in grouped.items():
            decision = self._decision_engine.decide_inbound_event(
                bundle,
                prev_state=prev_state,
            )
            if self._decision_engine.should_emit(decision):
                decisions.append(decision)

        return decisions

    def collect_rpa_callback_inbound_events(self, messages: List[Any]) -> List[InboundDetectionResult]:
        """将 RPA 回调消息标准化为统一入站事件。"""
        normalized = [self._inbound_detector.normalize_rpa_message(msg) for msg in (messages or [])]
        return self.merge_detection_results([event for event in normalized if event])

    def _decisions_to_detection_results(
        self,
        decisions: List[InboundDecision],
    ) -> List[InboundDetectionResult]:
        results: List[InboundDetectionResult] = []
        for decision in decisions:
            results.append(
                InboundDetectionResult(
                    customer_name=str(decision.customer_name or ""),
                    content=str(decision.content or ""),
                    direction="inbound",
                    is_new=True,
                    conversation_id=str(decision.conversation_id or ""),
                    platform="douyin",
                    customer_id=str(decision.customer_id or ""),
                    msg_id=str(decision.msg_id or ""),
                    timestamp=time.time(),
                    signal_source=str(decision.signal_source or ""),
                    direction_confidence="high",
                    direction_source=str(decision.signal_source or ""),
                )
            )
        return self.merge_detection_results(results)

    def dispatch_detected_inbound_events(
        self,
        events: List[Any],
        *,
        source: str,
        apply_echo_guard: bool = False,
        apply_self_reply_guard: bool = False,
    ) -> List[InboundDispatchResult]:
        """将检测结果提交到主链，并返回逐条处理结果。"""
        route_requests = self.build_route_requests(
            events,
            source=source,
            apply_echo_guard=apply_echo_guard,
            apply_self_reply_guard=apply_self_reply_guard,
        )
        return self.submit_route_requests(route_requests)

    def build_route_requests(
        self,
        events: List[Any],
        *,
        source: str,
        apply_echo_guard: bool = False,
        apply_self_reply_guard: bool = False,
    ) -> List[InboundRouteRequest]:
        """把检测结果转换为显式主链提交请求。"""
        detections: List[InboundDetectionResult] = []
        for event in events:
            detection = self._inbound_detector.coerce_detection_result(event)
            if detection is None:
                continue
            detections.append(detection)
        return self._inbound_dispatcher.build_route_requests(
            detections,
            source=source,
            apply_echo_guard=apply_echo_guard,
            apply_self_reply_guard=apply_self_reply_guard,
        )

    def submit_route_requests(
        self,
        route_requests: List[InboundRouteRequest],
    ) -> List[InboundDispatchResult]:
        """提交显式主链请求，并返回逐条处理结果。"""
        return self._inbound_dispatcher.submit_route_requests(route_requests)

    def merge_detection_results(self, events: List[Any]) -> List[InboundDetectionResult]:
        """公共检测归并入口，供适配层主流程与后续调用方统一复用。"""
        return self._inbound_detector.merge_detection_results(events)

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
    ) -> bool:
        """按当前适配模式分派发送——转发到 OutboundSendGateway.send()。"""
        result = self._outbound_send_gateway.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )
        return result.success

    def send_reply_to_target_result(
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
        """按当前适配模式分派发送，并返回规范化结果——转发到 OutboundSendGateway.send()。"""
        return self._outbound_send_gateway.send(
            customer_name=customer_name,
            content=content,
            max_retries=max_retries,
            conversation_id=conversation_id,
            customer_id=customer_id,
            platform=platform,
        )

    def _start_rpa_monitoring(self) -> bool:
        bot = self.bot
        launcher = getattr(bot, "rpa_launcher", None)
        rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None

        # [DEBUG-INST:greeting-no-reply] 主动重新初始化：rpa_launcher 被 stop 清空后，
        # 启动路径必须能自动恢复，不能依赖外部 _retry_rpa_init 的节流/上限
        if not rpa_engine:
            try:
                if hasattr(bot, "_retry_rpa_init") and bot._retry_rpa_init():
                    launcher = getattr(bot, "rpa_launcher", None)
                    rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None
                    if rpa_engine:
                        logger.info("RPA引擎在 _start_rpa_monitoring 入口完成自动重初始化")
            except Exception as reinit_exc:
                logger.debug(f"_start_rpa_monitoring 内自动重初始化失败: {reinit_exc}")

        if rpa_engine:
            try:
                return self._do_start_rpa(launcher, rpa_engine, channel="RPA")
            except Exception as exc:
                logger.error(f"RPA引擎首次启动失败: {exc}")
                import traceback
                logger.error(f"错误堆栈: {traceback.format_exc()}")
                return False

        logger.warning("RPA引擎未就绪，尝试重新初始化...")
        if not bot._retry_rpa_init():
            logger.warning("RPA引擎重试初始化失败，将降级到传统模式")
            return False

        launcher = getattr(bot, "rpa_launcher", None)
        rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None
        if launcher is None:
            logger.warning("RPA启动器在重试后仍为 None")
            return False
        try:
            return self._do_start_rpa(launcher, rpa_engine, channel="RPA重试")
        except Exception as exc:
            logger.error(f"RPA引擎重试后启动失败: {exc}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            return False

    def _do_start_rpa(
        self,
        launcher,
        rpa_engine,
        *,
        channel: str,
    ) -> bool:
        """RPA 引擎启动的单一入口——被首次启动和重试路径共用。

        职责：注册 callback → 启动 launcher → 刷新就绪态 → 标记运行态 → 准备 chat page。
        任何修改（如加埋点、改启动时序）只改这里一处即可。
        """
        bot = self.bot
        if not rpa_engine or not launcher:
            return False

        try:
            # [REFACTOR-INST:convergence] 依据入站模式决定 callback 注册
            if self._inbound_mode in ("callback", "hybrid"):
                rpa_engine.register_message_callback(bot._on_rpa_message)
                rpa_engine.register_state_callback(bot._on_rpa_state)
                logger.info(f"{channel}引擎消息回调已注册（模式: {self._inbound_mode}）")
            else:
                logger.info(f"{channel}引擎入站模式=poll，不注册 callback（轮询 evidence 处理）")
            launcher.start()
            rpa_effective = bot._refresh_rpa_monitoring_readiness()
            logger.info(
                f"{channel}引擎启动完成: is_monitoring={rpa_engine.is_monitoring}, "
                f"effective_monitoring={rpa_effective}"
            )
            if rpa_effective:
                self._mark_monitoring_running(channel)
                self._prepare_monitor_chat_page(f"{channel}模式")
                return True
            logger.warning(f"{channel}引擎启动后未进入有效监听态，准备降级到传统模式")
            try:
                launcher.stop()
            except Exception as stop_exc:
                logger.debug(f"停止未就绪{channel}引擎失败: {stop_exc}")
        except Exception as exc:
            logger.error(f"{channel}引擎启动失败: {exc}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
        return False

    def _start_traditional_monitoring(self) -> bool:
        bot = self.bot
        logger.info("RPA模式未启动或启动失败，尝试启动传统消息回复模式...")
        try:
            if bot.message_monitor.start():
                self._mark_monitoring_running("传统")
                return True
            logger.warning("传统消息回复模式启动未通过就绪校验")
        except Exception as exc:
            logger.error(f"传统消息回复模式启动失败: {exc}")
        return False

    def _mark_monitoring_running(self, channel: str) -> None:
        """统一标记消息回复为运行态——所有启动路径必须走这里。

        Args:
            channel: 启动通道标识（"RPA" | "RPA重试" | "传统" | "RPA恢复"），
                     用于日志和埋点，**不再允许在各分支中重复拼写状态字段**。
        """
        bot = self.bot
        bot._update_runtime_state(
            is_monitoring_messages=True,
            monitor_active=True,  # [P7 修复] 之前误写为 False，导致 Poll 线程持续跳过
            monitoring_state="running",
        )
        logger.info(f"{channel}消息回复已启动")

    def _mark_monitoring_faulted(self, reason: str) -> None:
        """统一标记启动失败/降级后的 faulted 态。"""
        bot = self.bot
        bot._update_runtime_state(
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="faulted",
        )
        bot._last_monitor_start_error = reason
        logger.error(reason)

    def _prepare_monitor_chat_page(self, label: str) -> None:
        bot = self.bot
        if getattr(bot, "message_monitor", None):
            try:
                bot.message_monitor._prepare_chat_page()
                logger.info(f"{label}下自动回复聊天页面已准备")
            except Exception as exc:
                logger.warning(f"{label}下准备自动回复聊天页面失败: {exc}")

    def _recover_or_fallback_rpa(self, current_time: float) -> bool:
        bot = self.bot
        if not hasattr(bot, "_rpa_recovery_attempt_time"):
            bot._rpa_recovery_attempt_time = 0
        if current_time - bot._rpa_recovery_attempt_time <= 60:
            return False
        bot._rpa_recovery_attempt_time = current_time
        logger.warning("RPA不可用，尝试恢复...")
        if bot._retry_rpa_init():
            try:
                launcher = getattr(bot, "rpa_launcher", None)
                rpa_engine = getattr(launcher, "rpa_engine", None) if launcher else None
                if not launcher or not rpa_engine:
                    logger.warning("RPA恢复后 launcher/rpa_engine 仍缺失")
                    return False
                # [REFACTOR-INST:convergence] 走统一的启动路径
                started = self._do_start_rpa(launcher, rpa_engine, channel="RPA恢复")
                if not started:
                    return False
                bot._rpa_retry_count = 0
                bot._rpa_recovery_attempt_time = 0
                logger.info("RPA引擎恢复成功")
                if getattr(bot, "_monitor_active", False) and getattr(bot, "message_monitor", None):
                    try:
                        bot.message_monitor.stop()
                        bot._update_runtime_state(monitor_active=False)
                        logger.info("RPA恢复成功，已停止传统监听模式")
                    except Exception as stop_exc:
                        logger.warning(f"停止传统监听模式失败: {stop_exc}")
                try:
                    resumed = bot._resume_monitor_inactive_inbound_workflows()
                    if resumed:
                        logger.info(f"RPA恢复后已恢复 monitor_inactive 待办: {resumed}")
                except Exception as resume_e:
                    logger.warning(f"RPA恢复后恢复 monitor_inactive 待办失败: {resume_e}")
                return True
            except Exception as exc:
                logger.error(f"RPA引擎恢复启动失败: {exc}")
                return False

        logger.warning("RPA恢复失败，降级到传统模式")
        if getattr(bot, "message_monitor", None) and not getattr(bot, "_monitor_active", False):
            try:
                bot.message_monitor.start()
                bot._update_runtime_state(monitor_active=True)
                logger.info("已降级到传统消息回复模式")
                try:
                    resumed = bot._resume_monitor_inactive_inbound_workflows()
                    if resumed:
                        logger.info(f"降级到传统模式后已恢复 monitor_inactive 待办: {resumed}")
                except Exception as resume_e:
                    logger.warning(f"降级到传统模式后恢复 monitor_inactive 待办失败: {resume_e}")
            except Exception as fallback_exc:
                logger.error(f"降级到传统模式也失败: {fallback_exc}")
        return False

    def _collect_rpa_inbound_events(self) -> List[InboundDetectionResult]:
        bot = self.bot
        try:
            launcher = getattr(bot, "rpa_launcher", None)
            if launcher is None:
                return []
            rpa_engine = getattr(launcher, "rpa_engine", None)
            if rpa_engine is None:
                return []
            check_login = getattr(rpa_engine, "check_login_status", None) or getattr(rpa_engine, "_check_login", None)
            if check_login:
                check_login()
            messages = rpa_engine.fetch_messages()
            if messages:
                logger.info(f"RPA轮询获取到 {len(messages)} 条新消息")
            callback_registered = bool(getattr(rpa_engine, "_message_callback", None))
            if callback_registered:
                if messages:
                    logger.info("RPA轮询消息已由回调路径处理，统一分发层跳过重复消费")
                rpa_engine._process_send_queue()
                return []
            normalized = [self._inbound_detector.normalize_rpa_message(msg) for msg in (messages or [])]
            rpa_engine._process_send_queue()
            return [event for event in normalized if event]
        except Exception as exc:
            logger.error(f"RPA轮询异常: {exc}")
            return []

    def _collect_traditional_inbound_events(self) -> List[InboundDetectionResult]:
        bot = self.bot
        if not getattr(bot, "message_monitor", None):
            logger.warning("message_monitor 未初始化")
            return []
        try:
            messages = bot.message_monitor.fetch_messages()
            return [self._inbound_detector.normalize_traditional_message(msg) for msg in (messages or []) if msg]
        except Exception as exc:
            logger.error(f"轮询消息回复错误: {exc}")
            return []

    # ==================== 证据采集（阶段3新增） ====================

    def _collect_rpa_evidence(self) -> list[InboundEvidence]:
        """从 RPA 引擎采集原始证据。

        [REFACTOR-INST:convergence] 入站模式由 HUOKE_INBOUND_MODE 控制：
        - callback（默认）：关闭轮询 evidence 采集，由 _on_rpa_message 处理
        - poll：关闭 callback 注册，仅靠轮询 evidence
        - hybrid：保留旧的双链路 + callback_registered 去重（不推荐）
        """
        bot = self.bot
        launcher = getattr(bot, "rpa_launcher", None)
        if not launcher:
            return []
        rpa_engine = getattr(launcher, "rpa_engine", None)
        if not rpa_engine or not getattr(rpa_engine, "is_monitoring", False):
            return []

        try:
            check_login = getattr(rpa_engine, "check_login_status", None) or getattr(rpa_engine, "_check_login", None)
            if check_login:
                check_login()

            messages = rpa_engine.fetch_messages()
            if not messages:
                return []

            # [REFACTOR-INST:convergence] callback 模式下不再从轮询路径消费
            if self._inbound_mode == "callback" and bool(getattr(rpa_engine, "_message_callback", None)):
                rpa_engine._process_send_queue()
                return []

            evidence_list: list[InboundEvidence] = []
            for msg in messages:
                ev = self._to_evidence(msg, source="rpa_observer")
                if ev is not None:
                    evidence_list.append(ev)

            rpa_engine._process_send_queue()
            return evidence_list
        except Exception as exc:
            logger.error(f"RPA证据采集异常: {exc}")
            return []

    def _to_evidence(
        self,
        msg: Any,
        *,
        source: str,
        content_keys: tuple = ("content",),
        customer_id_keys: tuple = ("customer_id",),
        direction_filter: Optional[set] = None,
    ) -> Optional[InboundEvidence]:
        """把原始消息 dict/对象转成 InboundEvidence。

        [REFACTOR-INST:convergence] 收敛 RPA / traditional / API 三处 evidence 构造。
        direction_filter 不为空时，非允许方向的证据返回 None（被过滤）。
        """
        if msg is None:
            return None

        def _get(key, default=""):
            if isinstance(msg, dict):
                return msg.get(key, default)
            return getattr(msg, key, default)

        direction = str(_get("direction", "inbound") or "inbound").strip().lower()
        if direction_filter is not None and direction not in direction_filter:
            return None

        content = ""
        for key in content_keys:
            val = _get(key, "")
            if val:
                content = str(val)
                break

        customer_id = ""
        for key in customer_id_keys:
            val = _get(key, "")
            if val:
                customer_id = str(val)
                break

        return InboundEvidence(
            source=source,
            customer_name=str(_get("customer_name", "") or ""),
            content=content,
            direction=direction or "inbound",
            conversation_id=str(_get("conversation_id", "") or ""),
            customer_id=customer_id,
            msg_id=str(_get("msg_id", "") or ""),
            has_unread=bool(_get("has_unread", False)),
            message_time=str(_get("timestamp", "") or ""),
        )

    def _collect_traditional_evidence(self) -> list[InboundEvidence]:
        """从传统 MessageMonitor 采集原始证据。"""
        bot = self.bot
        monitor = getattr(bot, "message_monitor", None)
        if not monitor or not getattr(monitor, "_running", False):
            return []

        try:
            messages = monitor.fetch_messages()
            if not messages:
                return []
            return [
                ev for ev in (
                    self._to_evidence(
                        msg,
                        source="conversation_preview",
                        content_keys=("last_message_content", "content"),
                    )
                    for msg in messages
                )
                if ev is not None
            ]
        except Exception as exc:
            logger.error(f"传统监听证据采集异常: {exc}")
            return []

    def _collect_api_evidence(self) -> list[InboundEvidence]:
        """从 APIInterceptor 采集原始证据。"""
        bot = self.bot
        monitor = getattr(bot, "message_monitor", None)
        if not monitor:
            return []
        interceptor = getattr(monitor, "api_interceptor", None)
        if not interceptor:
            return []

        try:
            messages = interceptor.get_captured_messages()
            if not messages:
                return []
            return [
                ev for ev in (
                    self._to_evidence(
                        msg,
                        source="api_intercept",
                        content_keys=("content", "last_message_content"),
                        customer_id_keys=("customer_id", "sender_id"),
                        direction_filter={"inbound", "incoming"},
                    )
                    for msg in messages
                )
                if ev is not None
            ]
        except Exception as exc:
            logger.error(f"API拦截证据采集异常: {exc}")
            return []
