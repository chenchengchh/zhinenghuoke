from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict

from src.common.database import DatabaseManager
from src.common.dingtalk_client import DingTalkClientError, get_dingtalk_client
from src.common.dingtalk_config_service import get_dingtalk_config_service
from src.common.follow_up_reminder_service import FollowUpCandidate


logger = logging.getLogger(__name__)


class DingTalkNotifyService:
    def __init__(self, database: DatabaseManager | None = None) -> None:
        self._db = database or DatabaseManager()

    def notify_follow_up(self, candidate: FollowUpCandidate, conversation: Dict[str, Any] | None) -> Dict[str, Any]:
        conversation = conversation or {}
        config = get_dingtalk_config_service().load_config()
        should_send, reason = self.should_send(candidate, conversation, config)
        if not should_send:
            return {
                "sent": False,
                "status": "skipped",
                "error": reason,
                "sent_at": "",
                "task_id": "",
            }

        content = self.build_follow_up_text(candidate)
        try:
            client = get_dingtalk_client()
            access_token = client.get_access_token(
                str(config.get("app_key") or "").strip(),
                str(config.get("app_secret") or "").strip(),
            )
            response = client.send_text_message(
                access_token=access_token,
                agent_id=str(config.get("agent_id") or "").strip(),
                userid=str(config.get("receiver_userid") or "").strip(),
                content=content,
            )
            sent_at = datetime.now().isoformat()
            self._db.mark_dingtalk_notification_sent(
                candidate.conversation_id,
                candidate.notification_signature,
                response.get("task_id", ""),
            )
            return {
                "sent": True,
                "status": "sent",
                "error": "",
                "sent_at": sent_at,
                "task_id": response.get("task_id", ""),
            }
        except DingTalkClientError as exc:
            logger.warning("发送钉钉待跟进提醒失败: %s", exc)
            self._db.mark_dingtalk_notification_failed(
                candidate.conversation_id,
                candidate.notification_signature,
                str(exc),
            )
            return {
                "sent": False,
                "status": "failed",
                "error": str(exc),
                "sent_at": "",
                "task_id": "",
            }
        except Exception as exc:
            logger.exception("钉钉待跟进提醒异常")
            self._db.mark_dingtalk_notification_failed(
                candidate.conversation_id,
                candidate.notification_signature,
                str(exc),
            )
            return {
                "sent": False,
                "status": "failed",
                "error": str(exc),
                "sent_at": "",
                "task_id": "",
            }

    def should_send(
        self,
        candidate: FollowUpCandidate,
        conversation: Dict[str, Any],
        config: Dict[str, Any],
    ) -> tuple[bool, str]:
        if not bool(config.get("enabled")):
            return False, "disabled"
        if bool(config.get("notify_contact_provided_only", True)) and candidate.trigger_type != "contact_provided_follow_up":
            return False, "contact_provided_only"
        if not str(config.get("receiver_userid") or "").strip():
            return False, "receiver_not_bound"
        if str(conversation.get("last_completed_follow_up_signature") or "").strip() == candidate.notification_signature:
            return False, "already_completed"
        if (
            str(conversation.get("last_dingtalk_notification_signature") or "").strip() == candidate.notification_signature
            and str(conversation.get("last_dingtalk_notification_status") or "").strip() == "sent"
        ):
            return False, "already_sent"
        errors = get_dingtalk_config_service().validate_config(config)
        if errors:
            return False, "config_incomplete"
        return True, ""

    def build_follow_up_text(self, candidate: FollowUpCandidate) -> str:
        follow_up_type = self._build_type_label(candidate)
        suggested_action = self._build_simple_action(candidate)
        lines = [
            "待跟进提醒",
            f"类型：{follow_up_type}",
            f"客户：{candidate.customer_name}",
        ]
        if candidate.contact_info:
            lines.append(f"联系方式：{candidate.contact_info}")
        if candidate.score:
            lines.append(f"意向分：{round(float(candidate.score or 0), 1)}")
        lines.append(f"原因：{candidate.trigger_reason}")
        lines.append(f"建议：{suggested_action}")
        if candidate.last_message_time:
            lines.append(f"最近互动：{candidate.last_message_time}")
        lines.append(f"来源：{candidate.platform}")
        return "\n".join(lines)

    @staticmethod
    def _build_type_label(candidate: FollowUpCandidate) -> str:
        if candidate.trigger_type == "contact_provided_follow_up":
            return "已留资"
        if candidate.trigger_type == "high_intent_follow_up":
            return "高意向"
        return "待跟进"

    @staticmethod
    def _build_simple_action(candidate: FollowUpCandidate) -> str:
        if candidate.trigger_type == "contact_provided_follow_up":
            return "请尽快联系客户并承接需求。"
        if candidate.trigger_type == "high_intent_follow_up":
            return "请尽快跟进并推动留资或成交。"
        return "请及时跟进。"


_dingtalk_notify_service: DingTalkNotifyService | None = None
_dingtalk_notify_service_lock = threading.Lock()


def get_dingtalk_notify_service() -> DingTalkNotifyService:
    global _dingtalk_notify_service
    if _dingtalk_notify_service is None:
        with _dingtalk_notify_service_lock:
            if _dingtalk_notify_service is None:
                _dingtalk_notify_service = DingTalkNotifyService()
    return _dingtalk_notify_service
