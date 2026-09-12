"""
RPA引擎启动器

专门用于启动和管理RPA引擎的模块
提供与现有BotService的集成接口
"""

import time
from typing import Optional, Callable
from loguru import logger
from src.common.chat_store import ChatStoreFacade
from src.common.conversation_id import build_conversation_id

from src.douyin_bot.rpa_engine import DouYinRPAEngine, RPAMessage, MessageDirection, PageState
from src.douyin_bot.boundary_guard import BoundaryGuard
from src.douyin_bot.rag_service_adapter import RAGServiceAdapter


class RPALauncher:
    """
    RPA引擎启动器

    提供：
    1. RPA引擎生命周期管理
    2. 与现有系统的集成接口
    3. 统一的配置管理
    """

    def __init__(self, page, browser_manager, database):
        """
        初始化RPA启动器

        Args:
            page: Playwright页面对象
            browser_manager: 浏览器管理器
            database: 数据库管理器
        """
        self.page = page
        self.browser_manager = browser_manager
        self.db = database
        self.chat_store = ChatStoreFacade(database) if database else None

        self.rpa_engine: Optional[DouYinRPAEngine] = None
        self.boundary_guard: Optional[BoundaryGuard] = None
        self.rag_adapter: Optional[RAGServiceAdapter] = None

        self._is_running = False
        self.last_send_error = ""

    def setup(self):
        """设置RPA引擎"""
        try:
            logger.info("=== RPA引擎设置开始 ===")

            self.rpa_engine = DouYinRPAEngine(
                page=self.page,
                browser_manager=self.browser_manager
            )

            self.boundary_guard = self.rpa_engine._boundary_guard

            self.rag_adapter = RAGServiceAdapter(self.rpa_engine)

            logger.info("=== RPA引擎设置完成 ===")

        except Exception as e:
            self.rpa_engine = None
            self.boundary_guard = None
            self.rag_adapter = None
            logger.error(f"RPA引擎设置失败: {e}")
            raise

    def start(self):
        """启动RPA引擎（idempotent，Phase 6 改进）"""
        if not self.rpa_engine:
            self.setup()

        # Phase 6 幂等性：无论之前状态如何，确保最终进入运行态
        if self._is_running:
            logger.info("RPA引擎已在运行中（幂等）")
            return

        self.rpa_engine.start_monitoring()
        self._is_running = True
        logger.info("RPA引擎已启动")

    def stop(self):
        """停止RPA引擎（idempotent，Phase 6 改进）"""
        # Phase 6 幂等性：无论之前状态如何，确保最终进入停止态
        if not self._is_running and not self.rpa_engine:
            logger.info("RPA引擎未运行（幂等停止）")
            return
        if self.rpa_engine:
            try:
                self.rpa_engine.stop_monitoring()
            except Exception as e:
                logger.error(f"停止RPA引擎异常: {e}")
            finally:
                self._is_running = False
                # 销毁旧实例，以便下次 start 时重新 setup 获取新的 browser_context 和 page
                self.rpa_engine = None
                self.boundary_guard = None
                self.rag_adapter = None
                logger.info("RPA引擎已停止并清理旧实例")

    def send_reply(
        self,
        customer_name: str,
        content: str,
        *,
        assume_target_ready: bool = False,
        identity_level: str = "exact_name",
    ) -> bool:
        """
        发送回复（内部方法，外部应通过 OutboundSendGateway.send() 发送）。

        Args:
            customer_name: 客户名称
            content: 回复内容

        Returns:
            bool: 是否发送成功
        """
        self.last_send_error = ""
        if not self.rpa_engine:
            self.last_send_error = "rpa_engine_uninitialized"
            logger.error("RPA引擎未初始化")
            return False

        result = self.rpa_engine.send_message(
            content,
            customer_name,
            assume_target_ready=assume_target_ready,
            identity_level=identity_level,
        )

        if result.success:
            logger.info(f"发送成功: {customer_name}")
            return True
        else:
            self.last_send_error = str(result.error or "rpa_send_failed")[:200]
            logger.error(f"发送失败: {result.error}")
            return False

    def ensure_send_target(
        self,
        customer_name: str,
        *,
        conversation_id: str = "",
        customer_id: str = "",
        identity_level: str = "exact_name",
        target_candidates: list[str] | tuple[str, ...] | None = None,
    ) -> bool:
        """在真正发送前预热目标会话，统一暴露给上层发送编排。"""
        self.last_send_error = ""
        if not self.rpa_engine:
            self.last_send_error = "rpa_engine_uninitialized"
            logger.error("RPA引擎未初始化")
            return False

        prepared = self.rpa_engine.prepare_send_target(
            customer_name,
            conversation_id=conversation_id,
            customer_id=customer_id,
            identity_level=identity_level,
            target_candidates=target_candidates,
        )
        if prepared:
            logger.info(f"目标会话已就绪: {customer_name}")
            return True

        self.last_send_error = f"rpa_prepare_target_failed:{customer_name}"
        logger.error(f"目标会话预热失败: {customer_name}")
        return False

    def process_with_rag(self, message) -> Optional[str]:
        """
        使用RAG处理消息并返回回复内容

        Args:
            message: 消息对象

        Returns:
            str: 回复内容或None
        """
        try:
            if self.rag_adapter:
                conversation_id = ""
                if hasattr(message, 'conversation_id'):
                    conversation_id = message.conversation_id
                elif isinstance(message, dict):
                    conversation_id = message.get('conversation_id', '')

                if not conversation_id:
                    customer_name = ''
                    if isinstance(message, dict):
                        customer_name = message.get('customer_name', '')
                    else:
                        customer_name = getattr(message, 'customer_name', '')
                    if not customer_name:
                        logger.warning("无法确定conversation_id: 缺少customer_name")
                        return None
                    conversation_id = build_conversation_id(customer_name, "douyin")

                if self.chat_store:
                    history = self.chat_store.get_recent_messages_dicts(conversation_id, limit=10)
                else:
                    history = []

                response = self.rag_adapter.process(message, history)

                if response:
                    return response.reply

            return None

        except Exception as e:
            logger.error(f"RAG处理失败: {e}")
            return None

    def get_stats(self) -> dict:
        """获取统计信息"""
        if self.rpa_engine:
            return self.rpa_engine.get_stats()
        return {}

    def get_status(self) -> dict:
        """获取运行状态"""
        return {
            "is_running": self._is_running,
            "is_initialized": self.rpa_engine is not None,
            "stats": self.get_stats()
        }

    def cleanup(self):
        """清理资源"""
        boundary_guard = self.boundary_guard
        self.stop()

        if boundary_guard:
            try:
                boundary_guard.stop()
            except Exception as e:
                logger.error(f"停止BoundaryGuard异常: {e}")

        self.rpa_engine = None
        self.boundary_guard = None
        self.rag_adapter = None
        self._is_running = False

        logger.info("RPA启动器资源已清理")
