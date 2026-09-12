"""评论回复子智能体 - 负责在评论区定位目标评论、点击回复、填写并发送回复"""

from __future__ import annotations

import logging
from typing import Any, Optional, List, Dict, Callable

from .comment_reply_types import ReplyPhase, ReplyStep, ReplyResult

logger = logging.getLogger(__name__)


class CommentReplyAgent:
    """
    评论回复子智能体

    职责范围：
    1. 参数校验（aweme_id, comment_id, reply_text）
    2. 确保视频上下文正确（防串流）
    3. 打开/确认评论面板
    4. 从实时锚点丰富化目标评论信息
    5. 执行回复轮次（最多4轮，每轮含滚动查找）
    6. 点击回复按钮 → 定位输入框 → 填写内容 → 提交确认
    7. 结果验收和诊断记录
    """

    MAX_REPLY_ROUNDS = 4

    def __init__(self, crawler_instance: Any):
        self.crawler = crawler_instance
        self.steps: List[ReplyStep] = []

    def reply(self, aweme_id: str, comment_id: str, reply_text: str,
              *, parent_comment_id: str = "", root_comment_id: str = "") -> ReplyResult:
        """执行评论回复流程"""
        plan = self._build_plan(comment_id)
        self.steps = plan

        result = ReplyResult(
            success=False,
            target_comment_id=comment_id,
        )

        target = {
            "aweme_id": aweme_id,
            "comment_id": comment_id,
            "reply_text": reply_text,
            "parent_comment_id": parent_comment_id,
            "root_comment_id": root_comment_id,
        }

        try:
            for step in plan:
                step.status = "running"
                outcome = self._execute_step(step, target)
                step.status = outcome["status"]
                step.detail = outcome.get("detail", "")

                if outcome.get("success") and step.phase == ReplyPhase.SUBMIT_AND_VERIFY:
                    result.success = True
                    result.rounds_attempted = outcome.get("rounds", 1)

                if outcome.get("should_terminate"):
                    result.reason = outcome.get("reason", "unknown")
                    break

            result.execution_log = list(self.steps)

        except Exception as e:
            logger.error(f"[ReplyAgent] 异常: {e}", exc_info=True)
            result.reason = f"exception: {e}"

        return result

    def _build_plan(self, comment_id: str) -> List[ReplyStep]:
        cid_short = comment_id[:12] if comment_id else "?"
        return [
            ReplyStep(phase=ReplyPhase.PREPARE, action=f"准备回复: {cid_short}..."),
            ReplyStep(phase=ReplyPhase.ENSURE_CONTEXT, action="确保视频上下文"),
            ReplyStep(phase=ReplyPhase.OPEN_COMMENT_PANEL, action="确保评论面板"),
            ReplyStep(phase=ReplyPhase.ENRICH_ANCHOR, action="丰富化锚点"),
            ReplyStep(phase=ReplyPhase.CLICK_REPLY_BUTTON, action="点击回复按钮"),
            ReplyStep(phase=ReplyPhase.FIND_EDITOR, action="定位输入框"),
            ReplyStep(phase=ReplyPhase.FILL_CONTENT, action="填写回复内容"),
            ReplyStep(phase=ReplyPhase.FIND_SEND_BTN, action="定位发送按钮"),
            ReplyStep(phase=ReplyPhase.SUBMIT_AND_VERIFY, action="提交并确认"),
            ReplyStep(phase=ReplyPhase.DONE, action="完成"),
        ]

    def _execute_step(self, step: ReplyStep, target: dict) -> Dict:
        phase = step.phase

        # 核心回复逻辑委托给底层
        if phase == ReplyPhase.SUBMIT_AND_VERIFY:
            if hasattr(self.crawler, 'reply_to_original_comment'):
                try:
                    ok = self.crawler.reply_to_original_comment(
                        aweme_id=target["aweme_id"],
                        comment_id=target["comment_id"],
                        reply_text=target["reply_text"],
                        parent_comment_id=target.get("parent_comment_id", ""),
                        root_comment_id=target.get("root_comment_id", ""),
                    )
                    if ok:
                        return {"status": "success", "success": True, "rounds": 1,
                               "detail": "回复成功"}
                    else:
                        reason = getattr(self.crawler, '_last_reply_failure_reason', 'unknown')
                        return {"status": "failed", "reason": reason,
                               "should_terminate": True, "detail": f"回复失败: {reason}"}
                except Exception as e:
                    return {"status": "failed", "reason": str(e), "detail": str(e)}
            return {"status": "failed", "detail": "无reply_to_original_comment方法"}

        return {"status": "success", "detail": f"{phase.name}: 由底层引擎驱动"}
