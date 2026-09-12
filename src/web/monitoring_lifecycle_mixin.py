"""
MonitoringLifecycleMixin - 消息回复生命周期管理

从 BotService 中提取的消息回复启动、停止、轮询等生命周期方法。
Mixin 中的方法通过 self 访问 BotService 的属性和其他方法，保持100%向后兼容。
"""
import time
import threading

from typing import Dict
from loguru import logger
from src.douyin_bot.rpa_launcher import RPALauncher


class MonitoringLifecycleMixin:
    """消息回复生命周期管理 Mixin"""

    def _finalize_monitor_start_success(self) -> bool:
        """启动成功后的统一收口，顺手恢复 monitor_inactive 待办。"""
        self._last_monitor_start_error = ""
        try:
            resumed = self._resume_monitor_inactive_inbound_workflows()
            if resumed:
                logger.info(f"消息回复启动后已恢复 monitor_inactive 待办: {resumed}")
            restored_retries = self._resume_persisted_retry_workflows()
            if restored_retries:
                logger.info(f"消息回复启动后已恢复持久化补偿重试: {restored_retries}")
        except Exception as e:
            logger.warning(f"恢复 monitor_inactive 待办失败: {e}")
        return True

    def _evaluate_start_prerequisites(self) -> str:
        """启动消息回复的前置评估。

        [REFACTOR-INST:convergence] 7 个分支的早期 if 收口为单值返回：
        - "already_running"：已有效监听，直接成功
        - "auto_restore_handled"：自动恢复正在处理，复用结果
        - "browser_not_running"：浏览器未启动
        - "residual_state_cleared"：残留状态已重置，可继续
        - "ready_to_start"：可进入 _start_monitoring_impl
        """
        state = self._get_runtime_state_snapshot()

        if getattr(self, "_auto_restore_monitoring_pending", False):
            monitoring_status = self._get_monitoring_status()
            if monitoring_status["effective_monitoring"]:
                return "auto_restore_handled"

        if not state["is_running"]:
            return "browser_not_running"

        monitoring_status = self._get_monitoring_status()
        if state["monitoring_state"] in {"starting", "running"} and not monitoring_status["effective_monitoring"]:
            # [P7 修复] 重置残留状态后继续启动
            self._update_runtime_state(
                is_monitoring_messages=False,
                monitor_active=False,
                monitoring_state="stopped",
            )
            return "residual_state_cleared"

        if state["monitoring_state"] in {"starting", "running"} and monitoring_status["effective_monitoring"]:
            return "already_running"

        return "ready_to_start"

    def start_message_monitoring(self, *, from_auto_restore: bool = False):
        """启动消息回复（同步等待启动结果，最多30秒超时）

        [REFACTOR-INST:convergence] 主方法只做"评估 → 执行 → 收敛"，所有 7 层 if
        折叠到 _evaluate_start_prerequisites()。
        """
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        decision = self._evaluate_start_prerequisites()
        if decision == "auto_restore_handled" and not from_auto_restore:
            logger.info("自动恢复回复任务已完成接管，复用当前有效监听态")
            return self._finalize_monitor_start_success()
        if decision == "auto_restore_handled":
            # from_auto_restore=True 时即使自动恢复也已接管，直接成功
            return self._finalize_monitor_start_success()
        if decision == "browser_not_running":
            logger.warning("浏览器未启动，无法启动消息回复")
            self._last_monitor_start_error = "浏览器未启动，无法启动消息回复"
            return False
        if decision == "already_running":
            return self._finalize_monitor_start_success()
        # decision in {"residual_state_cleared", "ready_to_start"}
        if decision == "residual_state_cleared":
            logger.warning("检测到自动回复状态残留但未真实运行，已重置后继续启动")

        self._last_monitor_start_error = ""
        self._update_runtime_state(monitoring_state="starting")

        # 避免在 Worker 线程内把启动任务再次提交到同一个队列后同步等待，
        # 否则会出现"当前任务阻塞自己后续任务"的启动假死。
        if threading.current_thread() is getattr(self, "worker_thread", None):
            try:
                self._start_monitoring_impl()
            except Exception as e:
                self._update_runtime_state(monitoring_state="faulted")
                logger.error(f"Worker线程内启动消息回复失败: {e}")
                self._last_monitor_start_error = f"Worker线程内启动消息回复失败: {e}"
                return False
            if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
                return self._finalize_monitor_start_success()
            self._update_runtime_state(monitoring_state="faulted")
            if not self._last_monitor_start_error:
                self._last_monitor_start_error = "消息回复启动失败，Worker线程直启后未进入运行态"
            return False

        future = self._submit_task(self._start_monitoring_impl)
        try:
            future.result(timeout=30)
        except FuturesTimeoutError:
            logger.warning("启动消息回复等待超时，进入短暂宽限轮询检查实际运行态")
            for _ in range(10):
                runtime_state = self._get_runtime_state_snapshot()
                if runtime_state["is_monitoring_messages"]:
                    return self._finalize_monitor_start_success()
                if future.done():
                    break
                time.sleep(1)
            runtime_state = self._get_runtime_state_snapshot()
            if runtime_state["is_monitoring_messages"]:
                return self._finalize_monitor_start_success()
            self._update_runtime_state(monitoring_state="faulted")
            if future.done():
                try:
                    future.result()
                except Exception as inner_e:
                    logger.error(f"启动消息回复失败: {inner_e}")
                    self._last_monitor_start_error = f"启动消息回复失败: {inner_e}"
                    return False
            logger.error("启动消息回复超时且宽限期内未进入运行态")
            self._last_monitor_start_error = "启动消息回复超时且宽限期内未进入运行态"
            return False
        except Exception as e:
            self._update_runtime_state(monitoring_state="faulted")
            logger.error(f"启动消息回复超时或失败: {e}")
            self._last_monitor_start_error = f"启动消息回复超时或失败: {e}"
            return False

        monitoring_status = self._get_monitoring_status()
        if monitoring_status["effective_monitoring"]:
            return self._finalize_monitor_start_success()

        self._update_runtime_state(
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="faulted",
        )
        if not self._last_monitor_start_error:
            self._last_monitor_start_error = "消息回复未进入有效监听状态，请确认已登录且聊天页可见"
        return False

    def _refresh_rpa_monitoring_readiness(self) -> bool:
        """强制刷新 RPA 监听就绪态，避免启动成功与状态查询口径不一致。"""
        if not (self.rpa_launcher and self.rpa_launcher.rpa_engine):
            return False

        engine = self.rpa_launcher.rpa_engine
        try:
            if hasattr(engine, "_last_login_check"):
                engine._last_login_check = 0
            if hasattr(engine, "_check_login"):
                engine._check_login()
        except Exception as readiness_error:
            logger.debug(f"刷新RPA监听就绪态失败: {readiness_error}")

        return bool(self._get_monitoring_status()["effective_monitoring"])

    def _retry_rpa_init(self) -> bool:
        """尝试重新初始化RPA引擎（单次尝试，不阻塞Worker线程）

        Returns:
            bool: 重试是否成功
        """
        if not self._use_rpa_mode:
            return False

        if self.rpa_launcher and self.rpa_launcher.rpa_engine:
            return True

        if not hasattr(self, '_rpa_retry_count'):
            self._rpa_retry_count = 0
            self._rpa_last_retry_time = 0

        current_time = time.time()
        if current_time - self._rpa_last_retry_time < 30:
            return False

        if self._rpa_retry_count >= 3:
            logger.error(f"RPA引擎重试已达上限({self._rpa_retry_count}次)，不再重试")
            return False

        self._rpa_last_retry_time = current_time
        self._rpa_retry_count += 1

        try:
            logger.info(f"RPA引擎重试初始化 ({self._rpa_retry_count}/3)...")
            if self.rpa_launcher is None:
                if not self.browser_manager:
                    logger.warning("browser_manager未初始化，无法重试RPA")
                    return False
                # [DEBUG-INST:greeting-no-reply] 使用 browser_manager.get_monitor_page()
                # 该方法会自动创建新页面（如果不存在），而 ChatSessionCoordinator.get_monitor_page()
                # 只是被动读取引用，stop 后 monitor_page 已被清空就会返回 None
                monitor_page = self.browser_manager.get_monitor_page()
                if not monitor_page:
                    logger.warning("获取monitor_page失败，无法重试RPA")
                    return False
                self.rpa_launcher = RPALauncher(
                    page=monitor_page,
                    browser_manager=self.browser_manager,
                    database=self.db
                )
            else:
                old_launcher = self.rpa_launcher
                try:
                    if hasattr(old_launcher, 'cleanup'):
                        old_launcher.cleanup()
                except Exception as cleanup_e:
                    logger.debug(f"清理旧RPA启动器失败: {cleanup_e}")
            self.rpa_launcher.setup()
            if self.rpa_launcher.rpa_engine:
                self.rpa_launcher.rpa_engine.message_monitor = self.message_monitor
                logger.info(f"RPA引擎重试初始化成功 (第{self._rpa_retry_count}次)")
                self._rpa_retry_count = 0
                return True
            else:
                logger.warning(f"RPA引擎重试初始化后rpa_engine仍为None (第{self._rpa_retry_count}次)")
        except Exception as e:
            logger.warning(f"RPA引擎重试初始化失败 (第{self._rpa_retry_count}次): {e}")

        return False

    def _start_monitoring_impl(self):
        """启动消息回复实现"""
        # [DEBUG-INST:greeting-no-reply] 启动前重置 RPA 重试计数，确保 stop->start 循环不会因旧重试计数耗尽而永久失败
        if hasattr(self, "_rpa_retry_count"):
            self._rpa_retry_count = 0
        if hasattr(self, "_rpa_last_retry_time"):
            self._rpa_last_retry_time = 0
        if self.browser_manager:
            self.browser_manager.set_monitoring_active(True)
        started = self._get_inbound_source_adapter().start_monitoring_sources()
        if not started:
            self._update_runtime_state(
                is_monitoring_messages=False,
                monitor_active=False,
                monitoring_state="faulted",
            )
            self._last_monitor_start_error = "消息回复启动失败，所有模式均未成功启动"
            logger.error("消息回复启动失败，所有模式均未成功启动")

        if started:
            self._last_monitor_start_error = ""
            self._start_monitor_poll_thread()
            try:
                self._state_persistor.start_auto_save(
                    interval=30,
                    get_state_callback=lambda: (self,
                                                self.rpa_launcher.rpa_engine if self.rpa_launcher and hasattr(self.rpa_launcher, 'rpa_engine') and self.rpa_launcher.rpa_engine else None,
                                                self.rpa_launcher.rpa_engine._boundary_guard if self.rpa_launcher and hasattr(self.rpa_launcher, 'rpa_engine') and self.rpa_launcher.rpa_engine and hasattr(self.rpa_launcher.rpa_engine, '_boundary_guard') and self.rpa_launcher.rpa_engine._boundary_guard else None,
                                                self.session_manager)
                )
            except Exception as auto_e:
                logger.warning(f"启动状态自动保存失败: {auto_e}")

    def stop_message_monitoring(self, reason: str = ""):
        """停止消息回复"""
        reason_text = str(reason or "").strip()
        if reason_text:
            logger.info(f"停止消息回复请求... source={reason_text}")
        else:
            logger.info("停止消息回复请求...")
        self._update_runtime_state(
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="stopping",
        )
        self._last_monitor_stop_reason = reason_text

        try:
            self._state_persistor.stop_auto_save()
        except Exception:
            pass

        if self._use_rpa_mode and self.rpa_launcher and self.rpa_launcher.rpa_engine:
            try:
                self.rpa_launcher.rpa_engine.register_message_callback(None)
                self.rpa_launcher.rpa_engine.register_state_callback(lambda *_: None)
                logger.info("RPA消息回调已清除")
            except Exception as e:
                logger.debug(f"清除RPA回调失败: {e}")

        self._submit_task_no_wait(self._stop_monitoring_impl)

    def _freeze_pending_auto_reply_work_items(self, reason: str = "") -> Dict[str, int]:
        """冻结已持久化的自动补偿待办，避免停止监听后仍继续自动发送。"""
        freeze_reason = f"auto_reply_stopped:{str(reason or '').strip()}".rstrip(":")
        frozen_outbox = 0
        frozen_workflow = 0
        cancelled_timers = self._cancel_all_pending_retry_timers()

        try:
            pending_outbox = list(self.db.list_outbox_events(statuses=["retry_pending"], limit=1000) or [])
        except Exception as outbox_e:
            logger.debug(f"读取 retry_pending 出站事件失败: {outbox_e}")
            pending_outbox = []

        for event in pending_outbox:
            outbox_id = str(event.get("outbox_id", "") or "").strip()
            if not outbox_id:
                continue
            try:
                self.db.update_outbox_event(
                    outbox_id,
                    status="cancelled",
                    reason=freeze_reason[:100],
                    next_retry_at="",
                )
                frozen_outbox += 1
            except Exception as update_e:
                logger.debug(f"冻结出站补偿事件失败: outbox_id={outbox_id}, error={update_e}")

        try:
            waiting_runs = list(self.db.list_workflow_runs(statuses=["waiting_retry"], limit=1000) or [])
        except Exception as workflow_e:
            logger.debug(f"读取 waiting_retry 工作流失败: {workflow_e}")
            waiting_runs = []

        for run in waiting_runs:
            workflow_run_id = str(run.get("workflow_run_id", "") or "").strip()
            if not workflow_run_id:
                continue
            try:
                self.db.upsert_workflow_run(
                    {
                        "workflow_run_id": workflow_run_id,
                        "status": "paused",
                        "paused_reason": freeze_reason[:200],
                    }
                )
                frozen_workflow += 1
            except Exception as update_e:
                logger.debug(f"冻结 waiting_retry 工作流失败: workflow_run_id={workflow_run_id}, error={update_e}")

        if frozen_outbox or frozen_workflow or cancelled_timers:
            logger.info(
                "停止自动回复时已冻结后台待办: "
                f"retry_pending={frozen_outbox} waiting_retry={frozen_workflow} "
                f"cancelled_retry_timers={cancelled_timers}"
            )

        return {"outbox": frozen_outbox, "workflow": frozen_workflow, "timers": cancelled_timers}

    def stop_message_monitoring_sync(self, reason: str = "", timeout_seconds: float = 8.0) -> bool:
        """同步停止消息回复，确保接口返回前尽量等到真实停下。"""
        self.stop_message_monitoring(reason=reason)

        deadline = time.monotonic() + max(float(timeout_seconds or 0), 0.5)
        while time.monotonic() < deadline:
            monitoring_status = self._get_monitoring_status()
            runtime_state = self._get_runtime_state_snapshot()
            if (
                not bool(monitoring_status.get("effective_monitoring"))
                and runtime_state.get("monitoring_state") == "stopped"
            ):
                return True
            time.sleep(0.1)

        logger.warning(
            "同步停止消息回复等待超时: "
            f"effective={self._get_monitoring_status().get('effective_monitoring')} "
            f"state={self._get_runtime_state_snapshot().get('monitoring_state')}"
        )
        return False

    def _stop_monitoring_impl(self):
        """停止消息回复实现（先设置状态标记阻止新消息，再停止消息源，最后清理缓存）"""
        logger.info("执行停止消息回复...")
        if self.browser_manager:
            self.browser_manager.set_monitoring_active(False)
        self._stop_monitor_poll_thread()
        self._update_runtime_state(
            monitor_active=False,
            is_monitoring_messages=False,
            monitoring_state="stopped",
        )

        self._get_inbound_idempotency_service().cleanup()
        self._get_outbound_idempotency_service()._evict_if_needed()
        with self._reply_locks_lock:
            keys_to_remove = []
            for k, entry in list(self._reply_locks.items()):
                # Python 3.10+ 的 threading.RLock 才有 .locked() 方法，之前的版本不存在。
                # 用 try/except 兼容：没有 .locked() 时假设锁未被持有（依赖 ref_count <= 0 已
                # 足够保证安全，且 _reply_locks_lock 持有期间其他线程不会修改 ref_count）。
                try:
                    is_locked = bool(entry['lock'].locked())  # type: ignore[attr-defined]
                except AttributeError:
                    is_locked = False
                if entry.get('ref_count', 0) <= 0 and not is_locked:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                del self._reply_locks[k]
            if self._reply_locks:
                logger.warning(f"停止回复时有 {len(self._reply_locks)} 个锁仍在使用中，已保留")
        self._rpa_retry_count = 0
        self._rpa_recovery_attempt_time = 0

        logger.info("消息回复已停止，缓存已清理")
        self._freeze_pending_auto_reply_work_items(
            reason=str(getattr(self, "_last_monitor_stop_reason", "") or "stop_monitoring")
        )

        self._get_inbound_source_adapter().stop_monitoring_sources()

    def _get_monitoring_status(self) -> Dict[str, bool]:
        """获取统一的自动回复状态

        修复：提供统一的状态查询接口，避免状态不一致
        """
        return self._get_inbound_source_adapter().get_monitoring_status()

    def _start_monitor_poll_thread(self):
        """启动消息轮询独立守护线程"""
        if self._monitor_poll_thread and self._monitor_poll_thread.is_alive():
            logger.info("消息轮询线程已在运行")
            return

        self._monitor_poll_running = True
        self._monitor_poll_stop_event.clear()
        self._monitor_poll_thread = threading.Thread(
            target=self._monitor_poll_loop,
            daemon=True,
            name="monitor_poll_thread"
        )
        self._monitor_poll_thread.start()
        logger.info("消息轮询独立守护线程已启动")

    def _stop_monitor_poll_thread(self):
        """停止消息轮询独立守护线程"""
        self._monitor_poll_running = False
        self._monitor_poll_stop_event.set()
        if self._monitor_poll_thread and self._monitor_poll_thread.is_alive():
            self._monitor_poll_thread.join(timeout=5)
        self._monitor_poll_thread = None
        logger.info("消息轮询独立守护线程已停止")

    def _monitor_poll_loop(self):
        """消息轮询定时器线程

        定时设置轮询信号，由Worker线程在安全上下文中执行实际的Playwright操作。
        此线程不直接调用任何Playwright API，避免greenlet线程切换错误。

        轮询频率：
        - RPA模式：2秒间隔
        - 传统模式（空闲）：3秒间隔
        - 传统模式（有爬取任务）：5秒间隔
        """
        logger.info("消息轮询定时器线程开始")

        while self._monitor_poll_running:
            try:
                if self._monitor_poll_stop_event.wait(timeout=0.5):
                    break

                state = self._get_runtime_state_snapshot()
                if not state["is_monitoring_messages"]:
                    continue

                current_time = time.time()

                if self._use_rpa_mode:
                    if current_time - state["last_monitor_check_time"] < 2:
                        continue
                else:
                    task_running = state["current_task"] != "Idle"
                    if task_running:
                        if current_time - state["last_monitor_check_time"] < 5:
                            continue
                    else:
                        if current_time - state["last_monitor_check_time"] < 3:
                            continue

                self._update_runtime_state(last_monitor_check_time=current_time)
                self._poll_signal.set()
                logger.debug("轮询定时器: 已设置轮询信号，等待Worker线程执行")

            except Exception as e:
                import traceback
                logger.error(f"消息轮询定时器线程异常: {e}\n{traceback.format_exc()}")
                time.sleep(1)

        logger.info("消息轮询定时器线程结束")

    def _poll_message_monitor(self):
        """轮询消息回复（在Worker线程中执行，保证Playwright线程安全）

        由轮询定时器线程通过_poll_signal触发，Worker线程检查信号后调用此方法。
        所有Playwright操作在此方法中执行，确保greenlet线程安全。
        """
        if not self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            return
        self._get_inbound_source_adapter().poll_once()
