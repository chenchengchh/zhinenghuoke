"""Agent基类 - 提供进程管理、浏览器生命周期、任务队列等基础能力

当前保留的多进程能力以爬取/私信为主：
- Agent通过 BrowserManager 统一启动并管理浏览器
- 通过multiprocessing.Queue接收主进程的任务指令
- 通过共享数据库进行数据通信
- 通过状态字典向主进程报告运行状态
"""
import os
import sys
import time
import logging
import multiprocessing
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Union
from enum import Enum

logger = logging.getLogger(__name__)

class AgentCommand(Enum):
    """Agent命令枚举"""
    START = "start"
    STOP = "stop"
    TASK = "task"
    PING = "ping"


class AgentStatus(Enum):
    """Agent状态枚举"""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    BUSY = "busy"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class BaseAgent(ABC):
    """Agent基类 - 所有Agent的公共父类

    子类需要实现：
    - _on_start(): Agent启动时的初始化逻辑
    - _on_stop(): Agent停止时的清理逻辑
    - _on_task(task_data): 处理从主进程接收的任务
    - _on_idle(): 空闲时的循环逻辑（如消息轮询）
    """

    def __init__(self, agent_name: str, command_queue: multiprocessing.Queue,
                 status_dict: dict,
                 shared_config: dict = None):
        """初始化Agent

        Args:
            agent_name: Agent名称，用于日志和状态标识
            command_queue: 命令队列，接收主进程的指令
            status_dict: 共享状态字典，向主进程报告状态
            shared_config: 共享配置
        """
        self.agent_name = agent_name
        self.command_queue = command_queue
        self.status_dict = status_dict
        self.shared_config = shared_config or {}

        self._running = False
        self._browser_manager = None
        self._page = None
        self._status = AgentStatus.IDLE
        self._current_task = "Idle"
        self._progress_info = {"total": 0, "current": 0, "detail": ""}
        self._stop_flag = False

        self._idle_interval = 2.0
        self._last_idle_time = 0

    def _update_status(self, status: AgentStatus, **kwargs):
        """更新共享状态字典"""
        self._status = status
        try:
            self.status_dict.update({
                'agent_name': self.agent_name,
                'status': status.value,
                'current_task': self._current_task,
                'progress': self._progress_info.copy(),
                'timestamp': time.time(),
                **kwargs
            })
        except Exception as e:
            logger.error(f"[{self.agent_name}] 更新状态失败: {e}")

    def _init_browser(self, headless: bool = False):
        """初始化浏览器。

        Args:
            headless: 是否无头模式

        Returns:
            bool: 是否初始化成功
        """
        try:
            from src.douyin_bot.browser_manager import BrowserManager

            self._browser_manager = BrowserManager()
            self._browser_manager.start()
            self._page = self._browser_manager.get_page()

            if self._page:
                logger.info(f"[{self.agent_name}] 浏览器初始化成功")
                return True
            else:
                logger.error(f"[{self.agent_name}] 浏览器初始化失败：未获取到页面")
                return False

        except Exception as e:
            logger.error(f"[{self.agent_name}] 浏览器初始化失败: {e}")
            return False

    def _close_browser(self):
        """关闭浏览器。"""
        try:
            if self._browser_manager:
                self._browser_manager.close()
                self._browser_manager = None
                self._page = None
                logger.info(f"[{self.agent_name}] 浏览器已关闭")
        except Exception as e:
            logger.error(f"[{self.agent_name}] 关闭浏览器失败: {e}")

    def _check_login(self) -> bool:
        """检查登录状态"""
        try:
            if self._page and not self._page.is_closed():
                from src.douyin_bot.login_handler import LoginHandler
                handler = LoginHandler(self._page)
                return handler.check_login_status()
        except Exception as e:
            logger.warning(f"[{self.agent_name}] 检查登录状态失败: {e}")
        return False

    def _process_commands(self):
        """处理命令队列中的指令"""
        while not self.command_queue.empty():
            try:
                cmd = self.command_queue.get_nowait()
                if cmd is None:
                    self._running = False
                    return

                cmd_type = cmd.get('type', '')
                cmd_data = cmd.get('data', {})

                if cmd_type == AgentCommand.STOP.value:
                    logger.info(f"[{self.agent_name}] 收到停止命令")
                    self._running = False
                    return
                elif cmd_type == AgentCommand.PING.value:
                    self._update_status(self._status)
                elif cmd_type == AgentCommand.TASK.value:
                    self._on_task(cmd_data)
                elif cmd_type == AgentCommand.START.value:
                    logger.info(f"[{self.agent_name}] 收到启动命令")
            except Exception as e:
                logger.error(f"[{self.agent_name}] 处理命令失败: {e}")

    def run(self):
        """Agent主运行循环（在子进程中执行）"""
        logger.info(f"[{self.agent_name}] Agent进程启动, PID={os.getpid()}")

        self._running = True
        self._update_status(AgentStatus.STARTING)

        try:
            if not self._on_start():
                self._update_status(AgentStatus.ERROR, error="启动失败")
                return

            self._update_status(AgentStatus.RUNNING)
            logger.info(f"[{self.agent_name}] Agent启动成功")

            while self._running:
                try:
                    self._process_commands()

                    if not self._running:
                        break

                    current_time = time.time()
                    if current_time - self._last_idle_time >= self._idle_interval:
                        self._on_idle()
                        self._last_idle_time = current_time
                        self._update_status(self._status)

                    time.sleep(0.1)

                except KeyboardInterrupt:
                    break
                except Exception as e:
                    logger.error(f"[{self.agent_name}] 主循环异常: {e}")
                    time.sleep(1)

        except Exception as e:
            logger.error(f"[{self.agent_name}] Agent运行异常: {e}")
            self._update_status(AgentStatus.ERROR, error=str(e))

        finally:
            self._update_status(AgentStatus.STOPPING)
            self._on_stop()
            self._close_browser()
            self._update_status(AgentStatus.STOPPED)
            logger.info(f"[{self.agent_name}] Agent已停止")

    def send_command(self, cmd_type: AgentCommand, data: dict = None):
        """向Agent发送命令"""
        self.command_queue.put({
            'type': cmd_type.value,
            'data': data or {}
        })

    @abstractmethod
    def _on_start(self) -> bool:
        """Agent启动时的初始化逻辑

        Returns:
            True表示启动成功，False表示启动失败
        """
        pass

    @abstractmethod
    def _on_stop(self):
        """Agent停止时的清理逻辑"""
        pass

    @abstractmethod
    def _on_task(self, task_data: dict):
        """处理从主进程接收的任务

        Args:
            task_data: 任务数据
        """
        pass

    @abstractmethod
    def _on_idle(self):
        """空闲时的循环逻辑（如消息轮询）

        每隔_idle_interval秒调用一次
        """
        pass


def _ensure_logs_dir():
    """确保日志目录存在"""
    logs_dir = os.path.join(os.getcwd(), 'logs')
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir, exist_ok=True)


def run_agent(agent_class, agent_name: str, command_queue: multiprocessing.Queue,
              status_dict: dict, shared_config: dict = None):
    """Agent进程入口函数

    在子进程中创建Agent实例并运行。
    此函数必须是模块级别的，以便pickle序列化。

    Args:
        agent_class: Agent类
        agent_name: Agent名称
        command_queue: 命令队列
        status_dict: 共享状态字典
        shared_config: 共享配置
    """
    _ensure_logs_dir()

    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s | %(levelname)-8s | [{agent_name}] %(name)s:%(funcName)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(f'logs/{agent_name}.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    try:
        agent = agent_class(agent_name, command_queue, status_dict, shared_config)
        agent.run()
    except Exception as e:
        logger.error(f"[{agent_name}] Agent进程异常退出: {e}")
