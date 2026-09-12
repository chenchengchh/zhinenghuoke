"""
BrowserLifecycleMixin - 浏览器生命周期管理

从 BotService 中提取的浏览器启动、停止、重启、页面关闭监听等生命周期方法。
Mixin 中的方法通过 self 访问 BotService 的属性和其他方法，保持100%向后兼容。
"""
import time
import threading

from loguru import logger
from src.douyin_bot.browser_manager import BrowserManager
from src.douyin_bot.login_handler import LoginHandler
from src.douyin_bot.message_monitor import MessageMonitor
from src.douyin_bot.rpa_launcher import RPALauncher
from src.config.settings import REPLY_LLM_TIMEOUT_SECONDS


class BrowserLifecycleMixin:
    """浏览器生命周期管理 Mixin"""

    def _init_browser_instance(self):
        """初始化浏览器组件

        修复：当is_running=False时，说明之前的浏览器实例已失效（用户手动关闭等），
        必须完全清理旧资源后重建，而不是尝试复用已断开连接的对象。

        此方法在Worker线程中执行，可以安全地停止Playwright。
        """
        try:
            logger.info("初始化浏览器组件...")

            if self.browser_manager:
                try:
                    browser_manager_alive = False
                    if hasattr(self.browser_manager, "is_session_alive"):
                        browser_manager_alive = bool(self.browser_manager.is_session_alive())
                    elif hasattr(self.browser_manager, "browser") and self.browser_manager.browser:
                        try:
                            browser_manager_alive = bool(self.browser_manager.browser.is_connected())
                        except Exception:
                            browser_manager_alive = False

                    if browser_manager_alive:
                        logger.info("浏览器管理器仍有效，复用现有实例")
                    else:
                        logger.info("浏览器会话已失效，清理旧资源并重新创建")
                        try:
                            self.browser_manager.close()
                        except Exception:
                            pass
                        self.browser_manager = None
                except Exception:
                    try:
                        self.browser_manager.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
                    self.browser_manager = None

            if not self.browser_manager:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        asyncio.set_event_loop(asyncio.new_event_loop())
                        logger.info("已替换关闭的asyncio事件循环")
                except RuntimeError:
                    asyncio.set_event_loop(asyncio.new_event_loop())
                    logger.info("已为Worker线程创建新的asyncio事件循环")
                managed_user_data_dir = self._resolve_browser_user_data_dir()
                logger.info(f"按账户启动浏览器 profile: {managed_user_data_dir}")
                self.browser_manager = BrowserManager(user_data_dir=managed_user_data_dir)
            self.browser_manager.start()

            coordinator = self._get_chat_session_coordinator()
            monitor_page = coordinator._acquire_monitor_page(self.browser_manager, force_new=False)
            if not monitor_page:
                raise RuntimeError("无法获取 monitor page")
            logger.info("监听标签页已创建")
            self._collapse_monitoring_to_single_page(monitor_page)

            self._setup_page_close_listener(monitor_page, "监听标签页")

            self.page = None
            # 登录状态检测应绑定到当前真实可见的聊天监听页，
            # 否则 start_monitor/check_login 会长期退化为未登录。
            self.login_handler = LoginHandler(monitor_page)
            self.crawler = None
            self.sender = None
            self.message_monitor = MessageMonitor(
                monitor_page,
                platform="douyin",
                browser_manager=self.browser_manager,
                chat_session_coordinator=self._get_chat_session_coordinator(),
            )

            # 初始化RPA引擎 (新增)
            # 注意：RPA引擎需要监听聊天页面，所以使用 monitor_page
            if self._use_rpa_mode:
                try:
                    self.rpa_launcher = RPALauncher(
                        page=monitor_page,
                        browser_manager=self.browser_manager,
                        database=self.db
                    )
                    self.rpa_launcher.setup()
                    if self.rpa_launcher.rpa_engine:
                        self.rpa_launcher.rpa_engine.message_monitor = self.message_monitor
                    logger.info("RPA引擎初始化成功")

                    try:
                        rpa_state = self._state_persistor.load_rpa_engine_state()
                        if rpa_state and self.rpa_launcher.rpa_engine:
                            self.rpa_launcher.rpa_engine.restore_persisted_state(rpa_state)
                    except Exception as rps_e:
                        logger.warning(f"恢复RPA引擎持久化状态失败: {rps_e}")

                    try:
                        bg_state = self._state_persistor.load_boundary_guard_state()
                        if bg_state and self.rpa_launcher.rpa_engine and self.rpa_launcher.rpa_engine._boundary_guard:
                            self.rpa_launcher.rpa_engine._boundary_guard.restore_persisted_state(bg_state)
                    except Exception as bg_e:
                        logger.warning(f"恢复边界保护器持久化状态失败: {bg_e}")
                except Exception as e:
                    logger.warning(f"RPA引擎初始化失败，将在启动监听时重试: {e}")
                    self.rpa_launcher = None

            self._update_runtime_state(is_running=True, browser_state="running", stop_flag=False)
            logger.info("浏览器初始化成功")

            try:
                session_state = self._state_persistor.load_session_state()
                if session_state and self.session_manager:
                    self.session_manager.restore_persisted_state(session_state)
            except Exception as sess_e:
                logger.warning(f"恢复会话管理器持久化状态失败: {sess_e}")

            try:
                mm_state = self._state_persistor.load_message_monitor_state()
                if mm_state and self.message_monitor:
                    self.message_monitor.restore_persisted_state(mm_state)
            except Exception as mm_e:
                logger.warning(f"恢复消息监控器持久化状态失败: {mm_e}")

            try:
                if self.rpa_launcher and self.rpa_launcher.rpa_engine:
                    bg = self.rpa_launcher.rpa_engine._boundary_guard
                    if bg and bg._locked_until and bg._locked_until < time.time():
                        logger.info(f"边界保护器锁定已过期(locked_until={bg._locked_until:.0f})，重置为正常状态")
                        bg._protection_level = type(bg._protection_level)('normal')
                        bg._consecutive_failures = 0
                        bg._locked_until = 0
                    elif bg and bg._protection_level.value != 'normal':
                        logger.info(f"边界保护器启动状态: level={bg._protection_level.value}, consecutive_failures={bg._consecutive_failures}，保留恢复的状态")
            except Exception as bg_e:
                logger.debug(f"浏览器初始化后: 边界保护器检查跳过: {bg_e}")

            # 初始化完成后立即同步检查登录状态
            try:
                if self.login_handler:
                    logger.info("开始同步检查登录状态...")
                    # 直接调用检测，不通过任务队列（避免异步问题）
                    status = self.login_handler.check_login_status()
                    self._login_status_cache = status
                    self._last_login_check_time = time.time()
                    logger.info(f"登录状态检测完成：{'已登录' if status else '未登录'}")
                    self.get_browser_runtime_snapshot(
                        force_scope_refresh=True,
                        persist_registry=True,
                    )
            except Exception as login_e:
                logger.warning(f"初始化时检查登录状态失败：{login_e}")
                import traceback
                logger.error(f"错误堆栈：{traceback.format_exc()}")

            self._restore_monitoring_state()

        except Exception as e:
            logger.error(f"浏览器初始化失败：{e}")
            import traceback
            logger.error(f"错误堆栈：{traceback.format_exc()}")
            self._update_runtime_state(is_running=False, browser_state="faulted")

    def _update_login_status_task(self):
        """更新登录状态"""
        try:
            if self.login_handler:
                status = self.login_handler.check_login_status()
                self._login_status_cache = status
                self._last_login_check_time = time.time()
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")

    def start_browser(self):
        """启动浏览器

        修复：当is_running=False但browser_manager等组件仍存在时（如页面被手动关闭后），
        先通过Worker线程清理旧资源再启动新实例。

        关键：浏览器上下文的清理和重建必须在同一线程（Worker线程）中完成，
        避免线程间交叉清理导致状态不一致。
        因此不在主线程中直接调用browser_manager.close()，
        而是将清理和初始化合并为一个Worker任务。
        """
        state = self._get_runtime_state_snapshot()
        if state["browser_state"] in {"starting", "running", "stopping", "recovering"}:
            return
        self._update_runtime_state(browser_state="starting", stop_flag=False)
        self._submit_task_no_wait(self._cleanup_and_init_browser)

    def start_browser_sync(self, timeout_seconds: float = 30.0) -> bool:
        """同步启动浏览器并等待结果，供需要立即继续后续动作的链路使用。"""
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        if self._sync_browser_runtime_state():
            return True

        state = self._get_runtime_state_snapshot()
        if state["browser_state"] in {"starting", "recovering"}:
            deadline = time.time() + max(timeout_seconds, 1.0)
            while time.time() < deadline:
                if self._sync_browser_runtime_state():
                    return True
                time.sleep(0.5)
            self._update_runtime_state(browser_state="faulted")
            logger.error("同步启动浏览器超时：已有启动流程但未进入运行态")
            return False

        self._update_runtime_state(browser_state="starting", stop_flag=False)

        if threading.current_thread() is getattr(self, "worker_thread", None):
            try:
                self._cleanup_and_init_browser()
            except Exception as e:
                logger.error(f"Worker线程内同步启动浏览器失败: {e}")
                self._update_runtime_state(is_running=False, browser_state="faulted")
                return False
            return bool(self._sync_browser_runtime_state())

        future = self._submit_task(self._cleanup_and_init_browser)
        try:
            future.result(timeout=max(timeout_seconds, 1.0))
        except FuturesTimeoutError:
            self._update_runtime_state(is_running=False, browser_state="faulted")
            logger.error("同步启动浏览器超时：Worker线程未在限定时间内完成初始化")
            return False
        except Exception as e:
            self._update_runtime_state(is_running=False, browser_state="faulted")
            logger.error(f"同步启动浏览器失败: {e}")
            return False

        return bool(self._sync_browser_runtime_state())

    def _cleanup_stale_managed_browser_processes(self, *, reason: str = "") -> int:
        """回收跨实例残留的自动化浏览器进程。"""
        try:
            cleaned = int(BrowserManager.cleanup_stale_managed_browser_processes() or 0)
            if cleaned > 0:
                suffix = f"（{reason}）" if reason else ""
                logger.warning(f"已回收 {cleaned} 个残留浏览器进程{suffix}")
            return cleaned
        except Exception as e:
            if reason:
                logger.warning(f"浏览器残留进程回收失败（{reason}）: {e}")
            else:
                logger.warning(f"浏览器残留进程回收失败: {e}")
            return 0

    def _cleanup_stale_browser_resources(self):
        """清理残留的浏览器资源（在Worker线程中调用，可安全停止Playwright）

        此方法在_cleanup_and_init_browser中被调用，确保重新启动前所有旧资源被正确释放。
        由于在Worker线程中执行，可以安全地停止Playwright实例。
        """
        try:
            if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
                try:
                    if hasattr(self.rpa_launcher, 'cleanup'):
                        self.rpa_launcher.cleanup()
                    elif hasattr(self.rpa_launcher, 'stop'):
                        self.rpa_launcher.stop()
                except Exception as e:
                    logger.debug(f"清理残留RPA启动器失败: {e}")
                self.rpa_launcher = None
        except Exception as e:
            logger.debug(f"清理RPA资源异常: {e}")

        self.page = None
        self.login_handler = None
        self.crawler = None
        self.sender = None
        self.message_monitor = None

        try:
            if self.browser_manager:
                self.browser_manager.close()
        except Exception as e:
            logger.debug(f"清理残留browser_manager失败: {e}")
        self.browser_manager = None

        self._login_status_cache = False
        self._cleanup_stale_managed_browser_processes(reason="cleanup_stale_browser_resources")
        logger.info("残留浏览器资源清理完成")

    def _cleanup_and_init_browser(self):
        """在Worker线程中安全地清理旧资源并初始化新浏览器

        修复：将清理和初始化合并为一个Worker任务，确保浏览器上下文的清理和重建
        在同一线程中完成。这解决了两个关键问题：
        1. 残留浏览器上下文阻止新实例接管现有 profile
        2. 在主线程中清理浏览器资源可能导致 Worker 侧状态错乱

        额外安全措施：即使on_page_close已清理资源，仍检查监控状态，
        防止因竞态条件导致监控未正确停止而影响新实例。
        """
        if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            logger.info("检测到消息回复仍在运行，先停止回复")
            try:
                self._update_runtime_state(
                    is_monitoring_messages=False,
                    monitor_active=False,
                    monitoring_state="stopping",
                )
                self._stop_monitoring_impl()
            except Exception as e:
                logger.warning(f"停止消息回复失败: {e}")

        logger.info("启动前执行浏览器残留体检")
        self._cleanup_stale_browser_resources()

        self._init_browser_instance()

    def _clear_closed_auxiliary_page_refs(self, page, page_name: str):
        """清理被关闭的辅助标签页引用，避免保留已关闭页面对象。"""
        try:
            if page_name == "爬取标签页":
                if self.page is page:
                    self.page = None
                self.login_handler = None
                self.crawler = None
                self.sender = None
                if self.browser_manager and getattr(self.browser_manager, "crawler_page", None) is page:
                    self.browser_manager.crawler_page = None
            elif page_name == "综合搜索标签页":
                if self.browser_manager and getattr(self.browser_manager, "search_page", None) is page:
                    self.browser_manager.search_page = None
        except Exception as e:
            logger.debug(f"清理{page_name}引用失败: {e}")

    def _collapse_monitoring_to_single_page(self, monitor_page) -> None:
        """监听模式下仅保留聊天页，关闭多余首页/历史页。"""
        if not self.browser_manager or not monitor_page:
            return

        context = getattr(self.browser_manager, "context", None)
        if not context:
            return

        if getattr(self.browser_manager, "crawler_page", None) or getattr(self.browser_manager, "search_page", None):
            return

        closed_count = 0
        try:
            for page in list(getattr(context, "pages", []) or []):
                if not page or page is monitor_page:
                    continue
                try:
                    if page.is_closed():
                        continue
                except Exception:
                    continue

                role = ""
                try:
                    role = str(self.browser_manager._get_page_role(page) or "").strip()
                except Exception:
                    role = ""

                if role and role not in {"main", "monitor"}:
                    continue

                try:
                    page.close()
                    closed_count += 1
                except Exception as close_exc:
                    logger.debug(f"关闭监听模式多余标签页失败: {close_exc}")

            self.browser_manager.page = monitor_page
            self.browser_manager.monitor_page = monitor_page
            if closed_count > 0:
                logger.info(f"监听模式已关闭 {closed_count} 个多余标签页，仅保留聊天页")
        except Exception as e:
            logger.debug(f"监听模式收敛标签页失败: {e}")

    def _recover_monitor_page_after_close(self, closed_page) -> bool:
        """监听标签页关闭后，仅重建自动回复页而非整浏览器停机。"""
        if not self.browser_manager:
            return False

        try:
            if getattr(self.browser_manager, "monitor_page", None) is closed_page:
                self.browser_manager.monitor_page = None

            new_monitor_page = self._get_chat_session_coordinator().recover_monitor_page(closed_page)
            if not new_monitor_page or new_monitor_page.is_closed():
                logger.warning("监听标签页关闭后重建新页面失败")
                return False

            self._setup_page_close_listener(new_monitor_page, "监听标签页")
            self.message_monitor = MessageMonitor(
                new_monitor_page,
                platform="douyin",
                browser_manager=self.browser_manager,
                chat_session_coordinator=self._get_chat_session_coordinator(),
            )

            old_launcher = getattr(self, "rpa_launcher", None)
            self.rpa_launcher = None
            if old_launcher:
                try:
                    if hasattr(old_launcher, "cleanup"):
                        old_launcher.cleanup()
                    elif hasattr(old_launcher, "stop"):
                        old_launcher.stop()
                except Exception as cleanup_e:
                    logger.debug(f"监听标签页恢复前清理旧RPA启动器失败: {cleanup_e}")

            if self._use_rpa_mode and not self._retry_rpa_init():
                logger.warning("监听标签页已恢复，但RPA启动器重建失败，将等待后续发送链路再尝试恢复")

            self._update_runtime_state(is_running=True, browser_state="running")
            logger.info("监听标签页关闭后已完成局部恢复，浏览器主运行态保持不变")
            return True
        except Exception as e:
            logger.warning(f"监听标签页关闭后局部恢复失败: {e}")
            return False

    def _setup_page_close_listener(self, page, page_name: str, cleanup_on_close: bool = True):
        """监听浏览器页面关闭事件，自动更新is_running状态并清理资源

        当用户手动关闭浏览器标签页时，Playwright会触发close事件。
        此方法注册事件监听器，在页面关闭时：
        1. 停止消息回复（直接调用_stop_monitoring_impl，避免队列竞态）
        2. 清理所有组件引用（RPA、爬虫、发送器等）
        3. 关闭 browser_manager 并释放浏览器上下文
        4. 将is_running设为False，允许用户通过"启动抖音"按钮重新启动

        关键修复：
        - 旧实现将browser_manager设为None但未调用close()，导致Playwright实例残留，
          再次启动时新Playwright实例与旧实例冲突（asyncio loop错误）
        - 旧实现通过stop_message_monitoring()提交_stop_monitoring_impl到队列，
          若用户快速重启，_stop_monitoring_impl可能在新实例创建后才执行，误停新实例
        - 现改为直接调用_stop_monitoring_impl()，确保同步完成后再清理browser_manager

        重入保护：关闭browser_manager时可能触发其他页面的close事件（同一线程重入），
        使用_page_close_cleanup_in_progress标志防止重复清理。
        """
        if not page:
            return
        try:
            def on_page_close():
                if not cleanup_on_close:
                    self._clear_closed_auxiliary_page_refs(page, page_name)
                    logger.info(f"{page_name}被关闭，但该页面仅为辅助标签页，已清理本页引用并跳过全局资源清理")
                    return
                if not self._get_runtime_state_snapshot()["is_running"] and not self.browser_manager:
                    logger.debug(f"{page_name}被关闭，但资源已清理，跳过重复处理")
                    return

                if getattr(self, '_page_close_cleanup_in_progress', False):
                    logger.debug(f"{page_name}被关闭，但清理正在进行中，跳过重复处理")
                    return

                self._page_close_cleanup_in_progress = True
                try:
                    runtime_snapshot = self._get_runtime_state_snapshot()
                    logger.warning(f"{page_name}被关闭，执行资源清理并更新is_running状态")

                    if page_name == "监听标签页":
                        was_monitoring = bool(runtime_snapshot["is_monitoring_messages"])
                        if was_monitoring:
                            try:
                                self._update_runtime_state(
                                    is_monitoring_messages=False,
                                    monitor_active=False,
                                    monitoring_state="stopping",
                                )
                                self._stop_monitoring_impl()
                            except Exception as e:
                                logger.debug(f"监听标签页关闭时停止消息回复失败: {e}")

                        if self._recover_monitor_page_after_close(page):
                            if was_monitoring:
                                try:
                                    logger.info("监听标签页恢复成功，尝试自动恢复消息回复")
                                    self._start_monitoring_impl()
                                except Exception as restart_e:
                                    logger.warning(f"监听标签页恢复后自动恢复消息回复失败: {restart_e}")
                            return

                        logger.warning("监听标签页局部恢复失败，回退到全局资源清理")

                    if runtime_snapshot["is_monitoring_messages"]:
                        try:
                            self._update_runtime_state(
                                is_monitoring_messages=False,
                                monitor_active=False,
                                monitoring_state="stopping",
                            )
                            self._stop_monitoring_impl()
                        except Exception as e:
                            logger.debug(f"页面关闭时停止消息回复失败: {e}")

                    try:
                        if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
                            if hasattr(self.rpa_launcher, 'cleanup'):
                                self.rpa_launcher.cleanup()
                            elif hasattr(self.rpa_launcher, 'stop'):
                                self.rpa_launcher.stop()
                    except Exception as e:
                        logger.debug(f"页面关闭时清理RPA启动器失败: {e}")
                    self.rpa_launcher = None

                    self.page = None
                    self.login_handler = None
                    self.crawler = None
                    self.sender = None
                    self.message_monitor = None

                    try:
                        if self.browser_manager:
                            self.browser_manager.close()
                    except Exception as e:
                        logger.warning(f"页面关闭时关闭browser_manager失败(浏览器可能已关闭): {e}")
                    self.browser_manager = None

                    self._update_runtime_state(
                        is_running=False,
                        browser_state="stopped",
                        current_task="Idle",
                        monitoring_state="stopped",
                    )
                    self._login_status_cache = False
                    logger.info(f"{page_name}关闭后的资源清理已完成，可以重新启动浏览器")
                finally:
                    self._page_close_cleanup_in_progress = False

            page.on("close", on_page_close)
            logger.info(f"已注册{page_name}关闭事件监听器")
        except Exception as e:
            logger.debug(f"注册{page_name}关闭事件监听器失败: {e}")

    def stop_browser(self, preserve_monitoring_intent: bool = False):
        """关闭浏览器（级联停止所有子模块，防止资源泄漏）

        停止顺序：
        1. 停止消息回复（含RPA引擎停止）
        2. 停止消息总线（等待队列排空）
        3. 停止会话管理器和配置管理器
        4. 保存状态
        5. 关闭浏览器管理器
        6. 清除所有组件引用
        7. 停止Worker线程
        8. 关闭线程池
        """
        runtime_snapshot = self._get_runtime_state_snapshot()
        monitoring_status = self._get_monitoring_status()
        should_preserve_monitoring = bool(
            preserve_monitoring_intent
            and (
                runtime_snapshot.get("is_monitoring_messages")
                or monitoring_status.get("effective_monitoring")
            )
        )
        preserved_monitor_active = bool(
            monitoring_status.get("rpa_monitoring")
            or monitoring_status.get("traditional_monitoring")
            or runtime_snapshot.get("monitor_active")
        )
        preserved_last_monitor_check_time = float(runtime_snapshot.get("last_monitor_check_time", 0) or 0)

        self._update_runtime_state(browser_state="stopping")
        self.stop_message_monitoring(reason="stop_browser")

        if hasattr(self, 'rpa_launcher') and self.rpa_launcher:
            try:
                if hasattr(self.rpa_launcher, 'cleanup'):
                    self.rpa_launcher.cleanup()
                elif hasattr(self.rpa_launcher, 'stop'):
                    self.rpa_launcher.stop()
            except Exception as e:
                logger.debug(f"清理RPA启动器失败: {e}")
            self.rpa_launcher = None

        if hasattr(self, 'message_bus') and self.message_bus:
            try:
                self.message_bus.stop()
            except Exception as e:
                logger.debug(f"停止消息总线失败: {e}")

        if hasattr(self, 'session_manager') and self.session_manager:
            try:
                if hasattr(self.session_manager, 'stop'):
                    self.session_manager.stop()
            except Exception as e:
                logger.debug(f"停止会话管理器失败: {e}")

        self._save_state_before_shutdown()
        if should_preserve_monitoring:
            try:
                self._state_persistor.save_monitoring_state(
                    is_monitoring=True,
                    use_rpa_mode=self._use_rpa_mode,
                    monitor_active=preserved_monitor_active,
                    last_monitor_check_time=preserved_last_monitor_check_time,
                )
                logger.info("关闭浏览器时已保留自动回复恢复意图，供下次启动自动恢复")
            except Exception as preserve_e:
                logger.warning(f"保留自动回复恢复意图失败: {preserve_e}")

        if self.browser_manager:
            try:
                self.browser_manager.close()
                logger.info("浏览器管理器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器管理器失败: {e}")
        self.browser_manager = None
        self._cleanup_stale_managed_browser_processes(reason="stop_browser")
        self.page = None
        self.login_handler = None
        self.crawler = None
        self.sender = None
        self.message_monitor = None
        self._login_status_cache = False

        self._update_runtime_state(
            is_running=False,
            browser_state="stopped",
            is_monitoring_messages=False,
            monitor_active=False,
            monitoring_state="stopped",
            current_task="Idle",
            stop_flag=False,
        )

        if hasattr(self, 'worker_thread') and self.worker_thread and self.worker_thread.is_alive():
            self.task_queue.put(self._build_shutdown_task_item())
            try:
                self.worker_thread.join(timeout=5)
                if self.worker_thread.is_alive():
                    logger.warning("Worker线程未在超时内退出")
                else:
                    logger.info("Worker线程已正常退出")
            except Exception as e:
                logger.debug(f"等待Worker线程退出时异常: {e}")

        self._shutdown_thread_pools()

    def _shutdown_thread_pools(self):
        """安全关闭所有线程池，防止资源泄漏（带超时保护）"""
        logger.info("正在关闭线程池...")

        shutdown_timeout = 15

        if hasattr(self, '_reply_executor') and self._reply_executor:
            try:
                self._reply_executor.shutdown(wait=False)
                logger.info("回复线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭回复线程池失败: {e}")

        if hasattr(self, '_llm_executor') and self._llm_executor:
            try:
                self._llm_executor.shutdown(wait=False)
                logger.info("LLM线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭LLM线程池失败: {e}")

        if hasattr(self, '_post_send_executor') and self._post_send_executor:
            try:
                self._post_send_executor.shutdown(wait=False)
                logger.info("发送后处理线程池已发起关闭")
            except Exception as e:
                logger.warning(f"关闭发送后处理线程池失败: {e}")

        coordinator = getattr(self, "_outbound_dispatch_coordinator_instance", None)
        if coordinator is not None:
            try:
                coordinator.shutdown(wait=False)
                logger.info("出站调度器已发起关闭")
            except Exception as e:
                logger.warning(f"关闭出站调度器失败: {e}")

        deadline = time.time() + shutdown_timeout
        for name, executor in [('reply', self._reply_executor), ('llm', self._llm_executor), ('post_send', self._post_send_executor)]:
            if executor and hasattr(executor, '_threads'):
                for thread in list(executor._threads):
                    if thread.is_alive():
                        wait_time = max(0, deadline - time.time())
                        if wait_time > 0:
                            thread.join(timeout=wait_time)
                        if thread.is_alive():
                            logger.warning(f"{name}线程池worker仍在运行: {thread.name}")

        logger.info("所有线程池关闭完成")

    def __del__(self):
        """析构函数：仅记录警告，不执行阻塞操作（线程池关闭应在stop_browser中完成）"""
        try:
            if hasattr(self, '_reply_executor') and self._reply_executor:
                if not self._reply_executor._shutdown:
                    logger.warning("BotService析构时线程池未关闭，请确保调用stop_browser()")
            if hasattr(self, '_llm_executor') and self._llm_executor:
                if not self._llm_executor._shutdown:
                    logger.warning("BotService析构时LLM线程池未关闭，请确保调用stop_browser()")
        except Exception:
            pass

    def restart_browser(self):
        """重启浏览器（通过Worker线程安全执行）"""
        self._submit_task_no_wait(self._restart_impl)

    def _open_url_in_browser_impl(self, url: str) -> dict:
        if not self.browser_manager:
            raise RuntimeError("浏览器未启动")

        page = self.browser_manager.open_url_in_new_tab(url)
        return {
            "success": True,
            "url": str(getattr(page, "url", "") or url),
        }

    def _wait_for_browser_ready(self, timeout: int = 45) -> bool:
        """等待浏览器启动完成，供需要复用浏览器上下文的动作调用。"""
        deadline = time.time() + max(1, int(timeout or 45))
        while time.time() < deadline:
            browser_ready = self._sync_browser_runtime_state()
            if browser_ready and self.browser_manager:
                return True

            state = self._get_runtime_state_snapshot()
            if state.get("browser_state") == "faulted":
                return False

            time.sleep(1)
        return False

    def open_url_in_same_browser(self, url: str, auto_start: bool = True) -> dict:
        """在"启动抖音"使用的同一浏览器中新开标签页打开链接。"""
        browser_ready = self._sync_browser_runtime_state()
        if (not browser_ready or not self.browser_manager) and auto_start:
            state = self._get_runtime_state_snapshot()
            if state.get("browser_state") not in {"starting", "running", "recovering"}:
                self.start_browser()
            browser_ready = self._wait_for_browser_ready(timeout=60)

        if not browser_ready or not self.browser_manager:
            raise RuntimeError("浏览器启动失败，请稍后重试或手动点击启动抖音")

        future = self._submit_task(self._open_url_in_browser_impl, url)
        return future.result(timeout=max(float(REPLY_LLM_TIMEOUT_SECONDS), 20.0))

    def _restart_impl(self):
        """重启浏览器实现（先停止回复，再关闭浏览器，最后重新初始化）"""
        logger.info("正在重启浏览器...")
        self._update_runtime_state(browser_state="recovering")

        self._save_state_before_shutdown()

        if self._get_runtime_state_snapshot()["is_monitoring_messages"]:
            logger.info("重启前先停止消息回复...")
            try:
                self._stop_monitoring_impl()
            except Exception as e:
                logger.warning(f"停止消息回复失败: {e}")

        try:
            if self.browser_manager:
                self.browser_manager.close()
                time.sleep(2)
        except Exception as e:
            logger.warning(f"关闭浏览器时出错: {e}")

        self._update_runtime_state(
            is_running=False,
            is_monitoring_messages=False,
            monitor_active=False,
            browser_state="recovering",
            monitoring_state="stopped",
            current_task="Idle",
            stop_flag=False,
        )
        self.browser_manager = None
        self._login_status_cache = False
        self._last_login_check_time = 0
        self._rpa_retry_count = 0
        self._rpa_recovery_attempt_time = 0
        self._rpa_last_retry_time = 0
        logger.info("登录状态缓存已重置")
        self._init_browser_instance()

    def check_login(self):
        """检查登录状态，优先使用缓存的登录状态，避免频繁检测和超时问题"""
        if not self.is_running:
            return False

        if self.current_task != "Idle":
            return True

        if self._login_status_cache and (time.time() - self._last_login_check_time < 30):
            return True

        try:
            if not self.login_handler:
                return False
            future = self._submit_task(self.login_handler.check_login_status)
            result = future.result(timeout=5)
            self._login_status_cache = result
            self._last_login_check_time = time.time()
            return result
        except Exception as e:
            logger.warning(f"检查登录状态超时或失败: {e}")
            if self._login_status_cache and (time.time() - self._last_login_check_time < 60):
                logger.info("使用缓存的登录状态")
                return True
            return False
