"""
统一回复管理服务 (Reply Management Service)

整合以下冗余模块：
1. FallbackReplyService - 兜底回复
2. ReplyEligibilityService - 回复资格判断
3. SearchReplyLimitService - 搜索回复限制

提供统一的回复管理接口，简化调用方代码。
"""

from __future__ import annotations

import warnings
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# 导入原始服务（保持向后兼容）
from src.common.fallback_reply_service import (
    FallbackReplyService,
    get_fallback_reply_service,
)
from src.common.reply_eligibility_service import (
    ReplyEligibilityService,
    get_reply_eligibility_service,
)
from src.common.search_reply_limit_service import (
    SearchReplyLimitDecision,
    SearchReplyLimitService,
    get_search_reply_limit_service,
)
from src.common.types.reply_eligibility import (
    EligibilityAction,
    ReplyEligibilityDecision,
    ReplyEligibilityInput,
)


class ReplyManagementService:
    """
    统一回复管理服务

    整合兜底回复、资格判定和频控限制三大功能，
    提供简洁的统一入口点。
    """

    def __init__(
        self,
        fallback_service: Optional[FallbackReplyService] = None,
        eligibility_service: Optional[ReplyEligibilityService] = None,
        limit_service: Optional[SearchReplyLimitService] = None,
    ):
        self._fallback = fallback_service or get_fallback_reply_service()
        self._eligibility = eligibility_service or get_reply_eligibility_service()
        self._limit = limit_service or get_search_reply_limit_service()

    # ==================== 兜底回复功能 ====================

    def make_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        is_technical: bool = False,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, Any]:
        """
        生成兜底回复（业务/技术）

        Args:
            customer_name: 客户名称
            user_message: 用户消息
            reason: 兜底原因
            is_technical: 是否技术性故障
            enterprise_id: 企业ID
            schema_id: Schema ID

        Returns:
            统一格式的回复字典
        """
        if is_technical:
            return self._fallback.make_technical_fallback_reply(
                customer_name, user_message, reason
            )
        else:
            return self._fallback.make_business_fallback_reply(
                customer_name,
                user_message,
                reason,
                enterprise_id=enterprise_id,
                schema_id=schema_id,
            )

    def make_contextual_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Dict[str, Any]:
        """生成上下文感知的兜底回复"""
        return self._fallback.make_contextual_fallback_reply(
            customer_name,
            user_message,
            reason,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )

    def get_universal_safe_reply(
        self,
        customer_name: str = "",
        user_message: str = "",
    ) -> str:
        """获取通用安全兜底回复"""
        return self._fallback.get_universal_safe_reply(customer_name, user_message)

    def knowledge_fallback_reply(
        self,
        customer_name: str,
        user_message: str,
        *,
        enterprise_id: str = "",
        schema_id: str = "",
    ) -> Tuple[str, str]:
        """从知识库检索降级答案"""
        return self._fallback.knowledge_fallback_reply(
            customer_name,
            user_message,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )

    # ==================== 资格判定功能 ====================

    def check_reply_eligibility(self, payload: ReplyEligibilityInput) -> ReplyEligibilityDecision:
        """检查回复资格"""
        return self._eligibility.evaluate(payload)

    def classify_evidence_level(self, smart_result: Dict[str, Any]) -> str:
        """分类证据级别"""
        return self._eligibility.classify_evidence_level(smart_result)

    # ==================== 频控限制功能 ====================

    def check_send_allowed(
        self,
        now: Optional[datetime] = None,
        *,
        account_id: str = "",
    ) -> SearchReplyLimitDecision:
        """检查是否允许发送回复"""
        return self._limit.check_send_allowed(now=now, account_id=account_id)

    def record_reply_event(
        self,
        *,
        target_id: str = "",
        reply_text: str = "",
        account_id: str = "",
        source: str = "search_original_comment_reply",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """记录回复事件"""
        return self._limit.record_reply_event(
            target_id=target_id,
            reply_text=reply_text,
            account_id=account_id,
            source=source,
            now=now,
        )

    def get_limit_status(self, now: Optional[datetime] = None, *, account_id: str = "") -> Dict[str, Any]:
        """获取频控状态"""
        return self._limit.get_status(now=now, account_id=account_id)

    # ==================== 组合流程 ====================

    def can_send_reply(
        self,
        payload: ReplyEligibilityInput,
        now: Optional[datetime] = None,
        *,
        account_id: str = "",
    ) -> Tuple[bool, str, Optional[ReplyEligibilityDecision]]:
        """
        综合检查：频控 + 资格判定

        Returns:
            (allowed, reason_code, decision_or_none)
        """
        # 1. 检查频控限制
        limit_decision = self.check_send_allowed(now=now, account_id=account_id)
        if not limit_decision.allowed:
            return False, f"limit_{limit_decision.status_code}", None

        # 2. 检查回复资格
        eligibility_decision = self.check_reply_eligibility(payload)

        if eligibility_decision.action == EligibilityAction.PAUSE_FOR_HUMAN.value:
            return False, "pause_for_human", eligibility_decision
        if eligibility_decision.action == EligibilityAction.SKIP.value:
            return False, eligibility_decision.reason_code or "skip", eligibility_decision
        if eligibility_decision.action == EligibilityAction.RETRY_LATER.value:
            return False, "retry_later", eligibility_decision

        return True, "eligible", eligibility_decision

    def handle_fallback_with_check(
        self,
        customer_name: str,
        user_message: str,
        reason: str,
        payload: ReplyEligibilityInput,
        *,
        is_technical: bool = False,
        enterprise_id: str = "",
        schema_id: str = "",
        now: Optional[datetime] = None,
        account_id: str = "",
    ) -> Dict[str, Any]:
        """
        带检查的兜底回复处理

        先进行频控和资格检查，再生成兜底回复。
        如果检查不通过，返回包含原因的结果。
        """
        allowed, reason_code, decision = self.can_send_reply(
            payload, now=now, account_id=account_id
        )

        if not allowed:
            fallback_result = {
                "reply": "",
                "intent_level": "",
                "intent_score": 0,
                "need_human": decision.should_pause_workflow if decision else False,
                "matched_knowledge": "",
                "suggested_action": "",
                "fallback_reason": reason_code,
                "source": "check_blocked",
                "check_reason": reason_code,
            }
            return fallback_result

        # 通过检查，生成兜底回复
        return self.make_fallback_reply(
            customer_name,
            user_message,
            reason,
            is_technical=is_technical,
            enterprise_id=enterprise_id,
            schema_id=schema_id,
        )

    # ==================== 属性访问（向后兼容） ====================

    @property
    def fallback_service(self) -> FallbackReplyService:
        """访问底层兜底回复服务"""
        warnings.warn(
            "直接访问 fallback_service 已弃用，请使用 ReplyManagementService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._fallback

    @property
    def eligibility_service(self) -> ReplyEligibilityService:
        """访问底层资格判定服务"""
        warnings.warn(
            "直接访问 eligibility_service 已弃用，请使用 ReplyManagementService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._eligibility

    @property
    def limit_service(self) -> SearchReplyLimitService:
        """访问底层频控限制服务"""
        warnings.warn(
            "直接访问 limit_service 已弃用，请使用 ReplyManagementService 的公共方法",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._limit


@lru_cache(maxsize=1)
def get_reply_management_service() -> ReplyManagementService:
    """获取统一回复管理服务单例"""
    return ReplyManagementService()


__all__ = [
    "ReplyManagementService",
    "get_reply_management_service",
]
