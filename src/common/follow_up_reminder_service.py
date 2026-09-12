from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.common.intent_scoring_policy import (
    build_effective_intent_score,
    extract_contact_info,
    has_strong_purchase_intent,
)


logger = logging.getLogger(__name__)


@dataclass
class FollowUpCandidate:
    conversation_id: str
    customer_id: str
    customer_name: str
    platform: str
    score: float
    estimated_value: float
    priority: str
    trigger_type: str
    trigger_reason: str
    description: str
    suggested_action: str
    contact_info: str = ""
    last_message_time: str = ""
    notification_signature: str = ""

    def to_action_item(self) -> Dict:
        return {
            "conversation_id": self.conversation_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "platform": self.platform,
            "type": self.trigger_type,
            "priority": self.priority,
            "description": self.description,
            "suggested_action": self.suggested_action,
            "score": self.score,
            "estimated_value": self.estimated_value,
            "contact_info": self.contact_info,
            "trigger_reason": self.trigger_reason,
            "last_message_time": self.last_message_time,
            "notification_signature": self.notification_signature,
        }


class FollowUpReminderService:
    """Unifies follow-up qualification and DingTalk reminders."""

    HIGH_INTENT_SCORE_THRESHOLD = float(os.getenv("FOLLOW_UP_HIGH_INTENT_SCORE_THRESHOLD", "70"))
    HIGH_INTENT_PROBABILITY_THRESHOLD = float(os.getenv("FOLLOW_UP_HIGH_INTENT_PROBABILITY_THRESHOLD", "0.65"))
    WEBHOOK_URL = (
        os.getenv("ENTERPRISE_WECHAT_WEBHOOK_URL", "").strip()
        or os.getenv("WECOM_WEBHOOK_URL", "").strip()
        or os.getenv("WECHAT_WORK_WEBHOOK_URL", "").strip()
    )

    HIGH_INTENT_LIFECYCLES = {"quote_requested", "reservation_ready", "sql", "opportunity"}
    HIGH_INTENT_LEAD_SCORES = {"hot"}
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._sent_signatures: Dict[str, str] = {}

    def build_action_items(
        self,
        conversations: List[Dict],
        messages_by_conversation: Optional[Dict[str, List[Dict]]] = None,
        *,
        notify: bool = False,
        limit: int = 20,
    ) -> List[Dict]:
        items: List[Dict] = []
        messages_by_conversation = messages_by_conversation or {}

        for conv in conversations or []:
            conversation_id = str(conv.get("conversation_id") or "").strip()
            candidate = self.evaluate_conversation(
                conv,
                messages_by_conversation.get(conversation_id, []),
            )
            if not candidate:
                continue
            item = candidate.to_action_item()
            if notify:
                item.update(self._notify_via_dingtalk(candidate, conv))
            else:
                item.update(self._build_dingtalk_status_snapshot(conv))
            items.append(item)

        priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        items.sort(
            key=lambda item: (
                priority_order.get(str(item.get("priority") or "low"), 3),
                -float(item.get("score", 0) or 0),
            )
        )
        return items[:limit]

    def evaluate_conversation(
        self,
        conversation: Dict,
        messages: Optional[List[Dict]] = None,
    ) -> Optional[FollowUpCandidate]:
        conversation = conversation or {}
        messages = messages or []

        conversation_id = str(conversation.get("conversation_id") or "").strip()
        if not conversation_id:
            return None

        customer_name = str(
            conversation.get("customer_name")
            or conversation.get("customer_id")
            or conversation_id
        ).strip()
        customer_id = str(conversation.get("customer_id") or "").strip()
        platform = str(conversation.get("platform") or "douyin").strip() or "douyin"
        score = round(float(conversation.get("purchase_intent_score", 0) or 0), 2)
        estimated_value = round(float(conversation.get("estimated_deal_size", 0) or 0), 2)
        purchase_probability = float(conversation.get("purchase_probability", 0) or 0)
        lead_score = str(conversation.get("lead_score") or "").lower()
        lifecycle_stage = str(conversation.get("lifecycle_stage") or "").lower()
        recommended_action = self._pick_recommended_action(conversation)
        contact_info = extract_contact_info(messages, customer_name)
        strong_purchase_intent = has_strong_purchase_intent(messages, conversation)
        effective_score = build_effective_intent_score(
            explicit_score=score,
            purchase_probability=purchase_probability,
            intent_level=conversation.get("intent_level"),
            lead_score=lead_score,
            has_contact_info=bool(contact_info),
            has_strong_purchase_intent=strong_purchase_intent,
        )
        last_message_time = str(conversation.get("last_message_time") or "").strip()

        if contact_info:
            signature = self._build_signature(
                conversation_id=conversation_id,
                trigger_type="contact_provided_follow_up",
                score=score,
                contact_info=contact_info,
                last_message_time=last_message_time,
            )
            candidate = FollowUpCandidate(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer_name=customer_name,
                platform=platform,
                score=effective_score,
                estimated_value=estimated_value,
                priority="urgent",
                trigger_type="contact_provided_follow_up",
                trigger_reason="客户已提供联系方式",
                description="客户已留资，建议立即人工跟进并尽快承接转化。",
                suggested_action=recommended_action or "优先联系负责人，确认客户需求并安排后续承接。",
                contact_info=contact_info,
                last_message_time=last_message_time,
                notification_signature=signature,
            )
            if self._is_completed(conversation, candidate.notification_signature):
                return None
            return candidate

        if self._is_high_intent(
            lead_score=lead_score,
            lifecycle_stage=lifecycle_stage,
            score=effective_score,
            purchase_probability=purchase_probability,
            strong_purchase_intent=strong_purchase_intent,
        ):
            signature = self._build_signature(
                conversation_id=conversation_id,
                trigger_type="high_intent_follow_up",
                score=score,
                contact_info="",
                last_message_time=last_message_time,
            )
            candidate = FollowUpCandidate(
                conversation_id=conversation_id,
                customer_id=customer_id,
                customer_name=customer_name,
                platform=platform,
                score=effective_score,
                estimated_value=estimated_value,
                priority="high" if effective_score < 85 else "urgent",
                trigger_type="high_intent_follow_up",
                trigger_reason="客户已具备高意向成交信号",
                description="客户已出现高意向成交信号，建议尽快人工跟进推进成交或留资。",
                suggested_action=recommended_action or "优先确认出发日期、人数、线路和是否方便接收完整资料。",
                contact_info="",
                last_message_time=last_message_time,
                notification_signature=signature,
            )
            if self._is_completed(conversation, candidate.notification_signature):
                return None
            return candidate

        return None

    def evaluate_and_notify_conversation(
        self,
        conversation: Dict,
        messages: Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        candidate = self.evaluate_conversation(conversation, messages)
        if not candidate:
            return None
        item = candidate.to_action_item()
        item.update(self._notify_via_dingtalk(candidate, conversation))
        return item

    def _is_high_intent(
        self,
        *,
        lead_score: str,
        lifecycle_stage: str,
        score: float,
        purchase_probability: float,
        strong_purchase_intent: bool,
    ) -> bool:
        return (
            strong_purchase_intent
            or lead_score in self.HIGH_INTENT_LEAD_SCORES
            or lifecycle_stage in self.HIGH_INTENT_LIFECYCLES
            or score >= self.HIGH_INTENT_SCORE_THRESHOLD
            or purchase_probability >= self.HIGH_INTENT_PROBABILITY_THRESHOLD
        )

    def _pick_recommended_action(self, conversation: Dict) -> str:
        trend = conversation.get("intent_trend", {}) or {}
        suggestion = str(trend.get("follow_up_suggestion") or "").strip()
        if suggestion:
            return suggestion

        priority = str(conversation.get("follow_up_priority") or "").lower()
        if priority in {"urgent", "high"}:
            return "建议由人工立即接手，优先推进预留、报价或资料承接。"
        return "建议尽快复核客户需求，并安排人工继续跟进。"

    def _is_completed(self, conversation: Dict, signature: str) -> bool:
        return str(conversation.get("last_completed_follow_up_signature") or "").strip() == str(signature or "").strip()

    def _build_signature(
        self,
        *,
        conversation_id: str,
        trigger_type: str,
        score: float,
        contact_info: str,
        last_message_time: str,
    ) -> str:
        if trigger_type == "contact_provided_follow_up":
            # 留资提醒按“同一会话 + 同一联系方式”稳定去重，避免客户后续继续聊天时重复触发钉钉。
            return "|".join(
                [
                    conversation_id,
                    trigger_type,
                    contact_info or "-",
                ]
            )
        score_bucket = int(score // 5) * 5
        return "|".join(
            [
                conversation_id,
                trigger_type,
                contact_info or "-",
                str(score_bucket),
                last_message_time or "-",
            ]
        )

    def _notify_via_dingtalk(self, candidate: FollowUpCandidate, conversation: Dict) -> Dict:
        from src.common.dingtalk_notify_service import get_dingtalk_notify_service

        result = get_dingtalk_notify_service().notify_follow_up(candidate, conversation)
        return {
            "notification_sent": bool(result.get("sent")),
            "dingtalk_notification_status": str(result.get("status") or "skipped"),
            "dingtalk_notification_error": str(result.get("error") or ""),
            "dingtalk_notification_at": str(result.get("sent_at") or ""),
            "dingtalk_notification_task_id": str(result.get("task_id") or ""),
        }

    def _build_dingtalk_status_snapshot(self, conversation: Dict) -> Dict:
        return {
            "notification_sent": str(conversation.get("last_dingtalk_notification_status") or "") == "sent",
            "dingtalk_notification_status": str(conversation.get("last_dingtalk_notification_status") or ""),
            "dingtalk_notification_error": str(conversation.get("last_dingtalk_notification_error") or ""),
            "dingtalk_notification_at": str(conversation.get("last_dingtalk_notification_at") or ""),
            "dingtalk_notification_task_id": str(conversation.get("last_dingtalk_notification_task_id") or ""),
        }


_follow_up_reminder_service: Optional[FollowUpReminderService] = None
_follow_up_reminder_service_lock = threading.Lock()


def get_follow_up_reminder_service() -> FollowUpReminderService:
    global _follow_up_reminder_service
    if _follow_up_reminder_service is None:
        with _follow_up_reminder_service_lock:
            if _follow_up_reminder_service is None:
                _follow_up_reminder_service = FollowUpReminderService()
    return _follow_up_reminder_service
