"""
消息总线处理器

为消息总线提供各种消息类型的处理器。
入站消息只负责转发到统一主链，不再保留并行自动回复旁路。
"""

import threading
from datetime import datetime
from typing import Dict, Any
from loguru import logger
from src.common.enterprise.message_bus import BusMessage, MessageType


class MessageHandlers:
    """
    消息处理器集合
    
    处理消息总线中的各类消息
    集成AGENTIC RAG和LLM智能回复
    """
    
    def __init__(self, db=None, bot_service=None):
        """
        初始化消息处理器
        
        Args:
            db: 数据库管理器
            bot_service: 机器人服务实例
        """
        self.db = db
        self.bot_service = bot_service
        self._event_log = []
        self._max_log_size = 1000
        self._event_log_lock = threading.Lock()

    @staticmethod
    def _is_trusted_inbound_bridge(message: BusMessage, payload: Dict[str, Any]) -> bool:
        headers = getattr(message, "headers", {}) or {}
        header_flag = headers.get("trusted_inbound_bridge")
        payload_flag = payload.get("trusted_inbound_bridge")
        return bool(header_flag or payload_flag)

    def handle_inbound(self, message: BusMessage):
        """
        处理入站消息（客户发来的消息）
        统一转发到 BotService 入站主链，避免消息总线维持并行回复旁路。
        
        Args:
            message: 总线消息
        """
        try:
            payload = message.payload
            if not self._is_trusted_inbound_bridge(message, payload):
                logger.warning(
                    "[入站消息] 未显式声明 trusted_inbound_bridge，拒绝总线入站桥接到自动回复主链"
                )
                self._log_event(
                    "inbound_blocked",
                    {
                        "reason": "untrusted_inbound_bridge",
                        "conversation_id": payload.get("conversation_id", ""),
                        "source_message_id": payload.get("msg_id", payload.get("message_id", "")),
                        "logical_message_id": payload.get("logical_message_id", ""),
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                return
            customer_name = payload.get("customer_name", "未知")
            content = payload.get("content", "")
            conversation_id = payload.get("conversation_id", "")
            customer_id = payload.get("customer_id", "")
            source_message_id = payload.get("msg_id", payload.get("message_id", ""))
            logical_message_id = payload.get("logical_message_id", "")
            trace_id = str(logical_message_id or source_message_id or "")[:12]
            
            logger.info(
                f"[入站消息] trace={trace_id or '-'} customer={customer_name} "
                f"conversation_id={conversation_id or '-'} source_message_id={source_message_id or '-'} "
                f"logical_message_id={logical_message_id or '-'} content={content[:50]}..."
            )
            
            # 记录到事件日志
            self._log_event("inbound", {
                "customer_name": customer_name,
                "content": content[:100],
                "conversation_id": conversation_id,
                "source_message_id": source_message_id,
                "logical_message_id": logical_message_id,
                "timestamp": datetime.now().isoformat()
            })

            if self.bot_service and hasattr(self.bot_service, "submit_inbound_message"):
                submitted, reason = self.bot_service.submit_inbound_message({
                    "customer_name": customer_name,
                    "content": content,
                    "conversation_id": conversation_id,
                    "customer_id": customer_id,
                    "platform": payload.get("platform", "douyin"),
                    "inbound_trigger": payload.get("inbound_trigger", "bus_inbound"),
                    "msg_id": payload.get("msg_id", payload.get("message_id", "")),
                    "timestamp": payload.get("timestamp", payload.get("created_at", "")),
                    "direction": payload.get("direction", "inbound"),
                    "logical_message_id": payload.get("logical_message_id", ""),
                    "workflow_run_id": payload.get("workflow_run_id", ""),
                    "enterprise_id": payload.get("enterprise_id", payload.get("enterpriseId", payload.get("tenant_id", payload.get("tenantId", "")))),
                    "preferred_schema_id": payload.get("preferred_schema_id", payload.get("preferredSchemaId", payload.get("schema_id", payload.get("schemaId", "")))),
                    "schema_id": payload.get("schema_id", payload.get("schemaId", "")),
                    "tenant_resolution_mode": payload.get("tenant_resolution_mode", payload.get("tenantResolutionMode", "")),
                })
                if submitted:
                    logger.info(
                        f"[入站消息] 已提交统一入站主链: trace={trace_id or '-'} "
                        f"customer={customer_name} conversation_id={conversation_id or '-'} "
                        f"source_message_id={source_message_id or '-'} logical_message_id={logical_message_id or '-'}"
                    )
                    return
                if reason == "duplicate":
                    logger.info(
                        f"[入站消息] 逻辑重复事件已跳过: trace={trace_id or '-'} "
                        f"customer={customer_name} conversation_id={conversation_id or '-'} "
                        f"source_message_id={source_message_id or '-'} logical_message_id={logical_message_id or '-'}"
                    )
                    return
                logger.warning(
                    f"[入站消息] 提交统一入站主链失败，消息不会再进入旧旁路: "
                    f"trace={trace_id or '-'} customer={customer_name}, "
                    f"conversation_id={conversation_id or '-'}, source_message_id={source_message_id or '-'}, "
                    f"logical_message_id={logical_message_id or '-'}, reason={reason}"
                )
                return

            logger.warning(
                f"[入站消息] bot_service 未就绪，当前消息未进入自动回复主链: "
                f"trace={trace_id or '-'} customer={customer_name} "
                f"conversation_id={conversation_id or '-'} source_message_id={source_message_id or '-'} "
                f"logical_message_id={logical_message_id or '-'}"
            )
                
        except Exception as e:
            logger.error(f"处理入站消息失败: {e}")
    
    def handle_outbound(self, message: BusMessage):
        """
        处理出站消息（发送给客户的消息）
        
        Args:
            message: 总线消息
        """
        try:
            payload = message.payload
            customer_name = payload.get("customer_name", "未知")
            content = payload.get("content", "")
            success = payload.get("success", False)
            
            logger.info(f"[出站消息] 客户: {customer_name}, 成功: {success}, 内容: {content[:50]}...")
            
            # 记录到事件日志
            self._log_event("outbound", {
                "customer_name": customer_name,
                "content": content[:100],
                "success": success,
                "timestamp": datetime.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"处理出站消息失败: {e}")
    
    def handle_event(self, message: BusMessage):
        """
        处理事件消息
        
        Args:
            message: 总线消息
        """
        try:
            payload = message.payload
            event_type = payload.get("event", "unknown")
            
            logger.info(f"[事件消息] 类型: {event_type}, 数据: {payload}")
            
            # 根据事件类型处理
            if event_type == "message_sent":
                self._handle_message_sent_event(payload)
            elif event_type == "customer_status_changed":
                self._handle_customer_status_event(payload)
            elif event_type == "intent_analyzed":
                self._handle_intent_analyzed_event(payload)
            else:
                logger.debug(f"未处理的事件类型: {event_type}")
            
            # 记录到事件日志
            self._log_event("event", {
                "event_type": event_type,
                "payload": payload,
                "timestamp": datetime.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"处理事件消息失败: {e}")
    
    def handle_system(self, message: BusMessage):
        """
        处理系统消息
        
        Args:
            message: 总线消息
        """
        try:
            payload = message.payload
            system_event = payload.get("system_event", "unknown")
            
            logger.info(f"[系统消息] 事件: {system_event}, 数据: {payload}")
            
            # 根据系统事件类型处理
            if system_event == "browser_started":
                logger.info("浏览器已启动")
            elif system_event == "browser_stopped":
                logger.info("浏览器已停止")
            elif system_event == "login_success":
                logger.info("登录成功")
            elif system_event == "login_failed":
                logger.warning("登录失败")
            elif system_event == "task_started":
                logger.info(f"任务开始: {payload.get('task_name', 'unknown')}")
            elif system_event == "task_completed":
                logger.info(f"任务完成: {payload.get('task_name', 'unknown')}")
            
            # 记录到事件日志
            self._log_event("system", {
                "system_event": system_event,
                "payload": payload,
                "timestamp": datetime.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"处理系统消息失败: {e}")
    
    def handle_command(self, message: BusMessage):
        """
        处理命令消息
        
        Args:
            message: 总线消息
        """
        try:
            payload = message.payload
            command = payload.get("command", "unknown")
            
            logger.info(f"[命令消息] 命令: {command}, 参数: {payload}")
            
            # 根据命令类型处理
            if command == "start_monitoring":
                if self.bot_service:
                    self.bot_service.start_message_monitoring()
            elif command == "stop_monitoring":
                if self.bot_service:
                    self.bot_service.stop_message_monitoring(reason="message_bus:stop_monitoring")
            elif command == "restart_browser":
                if self.bot_service:
                    self.bot_service.restart_browser()
            elif command == "update_status":
                # 更新状态命令
                pass
            
            # 记录到事件日志
            self._log_event("command", {
                "command": command,
                "payload": payload,
                "timestamp": datetime.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"处理命令消息失败: {e}")
    
    def _handle_message_sent_event(self, payload: Dict):
        """
        处理消息发送事件
        
        Args:
            payload: 事件数据
        """
        customer_name = payload.get("customer_name", "")
        success = payload.get("success", False)
        reply_content = payload.get("reply_content", "")
        
        if success:
            logger.info(f"消息发送成功 - 客户: {customer_name}, 回复: {reply_content}...")
        else:
            logger.warning(f"消息发送失败 - 客户: {customer_name}")
    
    def _handle_customer_status_event(self, payload: Dict):
        """
        处理客户状态变更事件
        
        Args:
            payload: 事件数据
        """
        customer_name = payload.get("customer_name", "")
        old_status = payload.get("old_status", "")
        new_status = payload.get("new_status", "")
        
        logger.info(f"客户状态变更 - {customer_name}: {old_status} -> {new_status}")
    
    def _handle_intent_analyzed_event(self, payload: Dict):
        """
        处理意向分析事件
        
        Args:
            payload: 事件数据
        """
        customer_name = payload.get("customer_name", "")
        intent_level = payload.get("intent_level", "")
        intent_score = payload.get("intent_score", 0)
        
        logger.info(f"意向分析完成 - {customer_name}: 等级={intent_level}, 分数={intent_score}")
    
    def _log_event(self, event_type: str, data: Dict):
        """
        记录事件到日志
        
        Args:
            event_type: 事件类型
            data: 事件数据
        """
        try:
            with self._event_log_lock:
                self._event_log.append({
                    "type": event_type,
                    "data": data
                })
                
                if len(self._event_log) > self._max_log_size:
                    self._event_log = self._event_log[-self._max_log_size:]
        except Exception as e:
            logger.error(f"记录事件日志失败: {e}")
    
    def get_event_log(self, limit: int = 100) -> list:
        """
        获取事件日志
        
        Args:
            limit: 返回数量限制
            
        Returns:
            事件日志列表
        """
        with self._event_log_lock:
            return list(self._event_log[-limit:])
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取处理器统计信息
        
        Returns:
            统计信息字典
        """
        with self._event_log_lock:
            event_log_snapshot = list(self._event_log)
        return {
            "total_events": len(event_log_snapshot),
            "event_types": {
                "inbound": len([e for e in event_log_snapshot if e["type"] == "inbound"]),
                "outbound": len([e for e in event_log_snapshot if e["type"] == "outbound"]),
                "event": len([e for e in event_log_snapshot if e["type"] == "event"]),
                "system": len([e for e in event_log_snapshot if e["type"] == "system"]),
                "command": len([e for e in event_log_snapshot if e["type"] == "command"]),
                "intelligent_reply": len([e for e in event_log_snapshot if e["type"] == "intelligent_reply"]),
                "escalation": len([e for e in event_log_snapshot if e["type"] == "escalation"])
            }
        }


def setup_message_handlers(message_bus, db=None, bot_service=None) -> MessageHandlers:
    """
    设置消息总线处理器
    
    Args:
        message_bus: 消息总线实例
        db: 数据库管理器
        bot_service: 机器人服务实例
        
    Returns:
        MessageHandlers: 消息处理器实例
    """
    handlers = MessageHandlers(db=db, bot_service=bot_service)
    
    # 订阅各类消息
    message_bus.subscribe(MessageType.INBOUND, handlers.handle_inbound)
    message_bus.subscribe(MessageType.OUTBOUND, handlers.handle_outbound)
    message_bus.subscribe(MessageType.EVENT, handlers.handle_event)
    message_bus.subscribe(MessageType.SYSTEM, handlers.handle_system)
    message_bus.subscribe(MessageType.COMMAND, handlers.handle_command)
    
    logger.info("消息总线处理器已设置完成")
    
    return handlers
