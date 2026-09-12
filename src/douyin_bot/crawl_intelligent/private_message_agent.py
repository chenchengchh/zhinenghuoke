"""自动私信子智能体 - 负责导航到用户主页、定位私信入口、发送私信消息"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, List, Dict, Callable

from .private_message_types import PMPhase, PMStep, PMResult

logger = logging.getLogger(__name__)


class PrivateMessageAgent:
    """
    自动私信子智能体

    职责范围：
    1. 前置检查（重复发送、用户存在性、身份门槛）
    2. 导航到用户主页
    3. 智能打分定位私信入口按钮（支持多种DOM形态）
    4. 多策略点击（human_click → locator → force → DOM dispatch）
    5. 等待聊天界面就绪
    6. 目标会话二次校验
    7. 定位输入框并发送消息
    8. 发送确认验证
    9. 后置处理（标记状态、关闭弹窗、限额结算）
    """

    def __init__(self, message_sender: Any):
        self.sender = message_sender
        self.steps: List[PMStep] = []

    def send(self, user_id: str, message: str, *,
             conversation_id: str = "",
             customer_id: str = "") -> PMResult:
        """执行私信发送流程"""
        plan = self._build_plan(user_id)
        self.steps = plan

        result = PMResult(success=False, user_id=user_id)

        try:
            for step in plan:
                step.status = "running"
                outcome = self._execute_step(step, user_id, message,
                                            conversation_id, customer_id)
                step.status = outcome["status"]
                step.detail = outcome.get("detail", "")

                if outcome.get("should_terminate"):
                    result.reason = outcome.get("reason", "unknown")
                    break

                if outcome.get("success") and step.phase == PMPhase.SEND_ACTION:
                    result.success = True

            result.execution_log = list(self.steps)

        except Exception as e:
            logger.error(f"[PMAgent] 异常: {e}", exc_info=True)
            result.reason = f"exception: {e}"

        return result

    def _build_plan(self, user_id: str) -> List[PMStep]:
        return [
            PMStep(phase=PMPhase.PRE_CHECK, action=f"前置检查: {user_id}"),
            PMStep(phase=PMPhase.NAVIGATE_PROFILE, action="导航到主页"),
            PMStep(phase=PMPhase.FIND_ENTRY_BUTTON, action="定位私信入口"),
            PMStep(phase=PMPhase.CLICK_ENTRY, action="点击私信入口"),
            PMStep(phase=PMPhase.WAIT_CHAT_READY, action="等待聊天就绪"),
            PMStep(phase=PMPhase.VALIDATE_TARGET, action="校验目标会话"),
            PMStep(phase=PMPhase.LOCATE_INPUT, action="定位输入框"),
            PMStep(phase=PMPhase.TYPE_MESSAGE, action="输入消息"),
            PMStep(phase=PMPhase.SEND_ACTION, action="发送消息"),
            PMStep(phase=PMPhase.VERIFY_RESULT, action="确认发送结果"),
            PMStep(phase=PMPhase.POST_PROCESS, action="后置处理"),
        ]

    def _execute_step(self, step: PMStep, user_id: str, message: str,
                     conv_id: str, cust_id: str) -> Dict:
        phase = step.phase

        # 大部分步骤委托给底层的 MessageSender
        # 这里做的是包装和增强：增加步骤追踪、诊断能力

        if phase == PMPhase.PRE_CHECK:
            return {"status": "success", "detail": "前置检查由底层处理"}
        elif phase == PMPhase.SEND_ACTION:
            # 核心发送动作
            if hasattr(self.sender, 'send_private_message'):
                try:
                    ok = self.sender.send_private_message(user_id, message)
                    if ok:
                        return {"status": "success", "success": True, "detail": "发送成功"}
                    else:
                        reason = getattr(self.sender, '_last_send_failure_reason', 'unknown')
                        return {"status": "failed", "reason": reason,
                               "should_terminate": True, "detail": f"发送失败: {reason}"}
                except Exception as e:
                    return {"status": "failed", "reason": str(e), "detail": str(e)}
            return {"status": "failed", "detail": "无send_private_message方法"}

        # 其他步骤标记为 delegated（由 send_private_message 内部处理）
        return {"status": "success", "detail": f"{phase.name}: 由底层引擎驱动"}
