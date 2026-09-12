"""
PurchaseIntentMixin - 购买意向分析相关方法

从 BotService 中提取的购买意向分析方法集合，包含：
1. 自动意向分析（_auto_analyze_intent）
2. 手动意向分析（analyze_purchase_intent）
3. 线索评分（score_lead）
4. 热线索获取（get_hot_leads）
5. 转化漏斗（get_conversion_funnel）
6. 培育活动（start_nurture_campaign / get_nurture_message）
7. 批量意向分析（analyze_all_customers_intent）
"""
from typing import Any, Dict, List, Optional

from loguru import logger


class PurchaseIntentMixin:
    """购买意向分析 Mixin —— 从 BotService 提取的购买意向分析相关方法。"""

    def _auto_analyze_intent(self, customer_name: str, messages: Optional[List[Dict]] = None) -> dict:
        """
        自动分析客户购买意向 - 内部方法

        在消息同步后自动调用，更新数据库中的意向分析字段

        Args:
            customer_name: 客户名称
            messages: 消息列表（可选，如果不提供则从数据库获取）

        Returns:
            dict: 分析结果摘要
        """
        try:
            conversation_id = self.db.find_best_conversation_id(
                customer_name=customer_name,
                platform="douyin",
            ) or self._make_conversation_id(customer_name)

            # 如果没有提供消息，从数据库获取
            if not messages:
                messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)

            if not messages:
                return {"lead_score": "cold", "total_score": 0}

            messages = self.db.sanitize_message_records(messages)

            # 获取客户数据
            customer_data = {
                "customer_id": customer_name,
                "customer_name": customer_name,
                "platform": "douyin"
            }

            # 使用购买意向分析器
            intent_result = self.purchase_intent_analyzer.analyze(customer_data, messages)

            # 使用获客服务进行线索评分
            lead_result = self.customer_acquisition_service.score_lead(customer_data, messages)

            # 合并结果
            combined_result = {
                "total_score": intent_result.score.total_score,
                "lead_score": lead_result.lead_score.value,
                "follow_up_priority": lead_result.follow_up_priority.value,
                "purchase_probability": intent_result.score.purchase_probability,
                "estimated_deal_size": intent_result.score.estimated_deal_size,
                "lifecycle_stage": intent_result.lifecycle_stage.value,
                "buying_role": intent_result.buying_role.value,
                "signals_detected": [s.value for s in intent_result.score.signals_detected],
                "risk_factors": intent_result.risk_factors,
                "opportunity_factors": intent_result.opportunity_factors,
                "suggested_follow_up_time": lead_result.suggested_follow_up_time.isoformat() if lead_result.suggested_follow_up_time else None
            }

            # 更新数据库中的意向分析字段
            self.db.update_conversation_intent(conversation_id, combined_result)
            self._refresh_follow_up_reminder(conversation_id, messages)

            logger.info(f"自动意向分析完成: {customer_name} - {lead_result.lead_score.value} ({intent_result.score.total_score:.1f}分)")

            return combined_result

        except Exception as e:
            logger.error(f"自动意向分析失败: {e}")
            return {"lead_score": "cold", "total_score": 0, "error": str(e)}

    def analyze_purchase_intent(self, customer_name: str) -> dict:
        """
        分析客户购买意向

        Args:
            customer_name: 客户名称

        Returns:
            dict: 分析结果
        """
        try:
            conversation_id = self._make_conversation_id(customer_name)

            # 获取消息历史
            messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)

            if not messages:
                return {
                    "success": False,
                    "message": "没有找到对话记录",
                    "customer_name": customer_name
                }

            # 获取客户数据
            customer_data = {
                "customer_id": customer_name,
                "customer_name": customer_name,
                "platform": "douyin"
            }

            # 使用购买意向分析器
            result = self.purchase_intent_analyzer.analyze(customer_data, messages)

            return {
                "success": True,
                "customer_name": customer_name,
                "score": {
                    "total_score": result.score.total_score,
                    "confidence": result.score.confidence,
                    "purchase_probability": result.score.purchase_probability,
                    "estimated_deal_size": result.score.estimated_deal_size,
                    "estimated_close_days": result.score.estimated_close_days,
                    "churn_risk": result.score.churn_risk,
                    "engagement_score": result.score.engagement_score,
                    "interest_score": result.score.interest_score,
                    "urgency_score": result.score.urgency_score,
                    "budget_score": result.score.budget_score,
                    "authority_score": result.score.authority_score,
                    "need_score": result.score.need_score,
                    "timeline_score": result.score.timeline_score
                },
                "lifecycle_stage": result.lifecycle_stage.value,
                "buying_role": result.buying_role.value,
                "signals_detected": [s.value for s in result.score.signals_detected],
                "recommended_actions": result.recommended_actions,
                "risk_factors": result.risk_factors,
                "opportunity_factors": result.opportunity_factors,
                "best_contact_time": result.best_contact_time,
                "analysis_details": result.analysis_details
            }

        except Exception as e:
            logger.error(f"分析购买意向失败: {e}")
            return {
                "success": False,
                "message": f"分析失败: {str(e)}",
                "customer_name": customer_name
            }

    def score_lead(self, customer_name: str) -> dict:
        """
        对线索进行评分

        Args:
            customer_name: 客户名称

        Returns:
            dict: 评分结果
        """
        try:
            conversation_id = self._make_conversation_id(customer_name)

            # 获取消息历史
            messages = self._get_chat_store().get_recent_messages_dicts(conversation_id, limit=9999)

            # 获取客户数据
            customer_data = {
                "customer_id": customer_name,
                "customer_name": customer_name,
                "platform": "douyin"
            }

            # 使用获客服务进行评分
            result = self.customer_acquisition_service.score_lead(customer_data, messages)

            return {
                "success": True,
                "customer_name": customer_name,
                "lead_score": result.lead_score.value,
                "follow_up_priority": result.follow_up_priority.value,
                "scores": {
                    "intent_score": result.intent_score,
                    "engagement_score": result.engagement_score,
                    "fit_score": result.fit_score,
                    "timing_score": result.timing_score
                },
                "recommended_actions": result.recommended_actions,
                "suggested_follow_up_time": result.suggested_follow_up_time.isoformat() if result.suggested_follow_up_time else None,
                "suggested_channel": result.suggested_channel,
                "suggested_content": result.suggested_content,
                "risk_alerts": result.risk_alerts,
                "conversion_probability": result.conversion_probability,
                "estimated_value": result.estimated_value
            }

        except Exception as e:
            logger.error(f"线索评分失败: {e}")
            return {
                "success": False,
                "message": f"评分失败: {str(e)}",
                "customer_name": customer_name
            }

    def get_hot_leads(self) -> dict:
        """
        获取所有热线索

        Returns:
            dict: 热线索列表
        """
        try:
            hot_leads = self.customer_acquisition_service.get_hot_leads()

            return {
                "success": True,
                "count": len(hot_leads),
                "leads": [
                    {
                        "customer_id": lead.customer_id,
                        "lead_score": lead.lead_score.value,
                        "follow_up_priority": lead.follow_up_priority.value,
                        "intent_score": lead.intent_score,
                        "conversion_probability": lead.conversion_probability,
                        "estimated_value": lead.estimated_value,
                        "suggested_follow_up_time": lead.suggested_follow_up_time.isoformat() if lead.suggested_follow_up_time else None
                    }
                    for lead in hot_leads
                ]
            }

        except Exception as e:
            logger.error(f"获取热线索失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}"
            }

    def get_conversion_funnel(self) -> dict:
        """
        获取转化漏斗分析

        Returns:
            dict: 漏斗分析结果
        """
        try:
            funnel_result = self.customer_acquisition_service.analyze_conversion_funnel()

            return {
                "success": True,
                "data": funnel_result
            }

        except Exception as e:
            logger.error(f"获取转化漏斗失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}"
            }

    def start_nurture_campaign(self, customer_name: str) -> dict:
        """
        启动客户培育活动

        Args:
            customer_name: 客户名称

        Returns:
            dict: 培育活动配置
        """
        try:
            customer = self.db.get_customer_by_nickname(customer_name, "douyin") if self.db else None
            customer_id = customer.get("sec_uid", customer_name) if customer else customer_name
            result = self.customer_acquisition_service.start_nurture_campaign(customer_id)

            return {
                "success": True,
                "customer_name": customer_name,
                "nurture_config": result
            }

        except Exception as e:
            logger.error(f"启动培育活动失败: {e}")
            return {
                "success": False,
                "message": f"启动失败: {str(e)}",
                "customer_name": customer_name
            }

    def get_nurture_message(self, customer_name: str) -> dict:
        """
        获取培育消息

        Args:
            customer_name: 客户名称

        Returns:
            dict: 培育消息
        """
        try:
            customer = self.db.get_customer_by_nickname(customer_name, "douyin") if self.db else None
            customer_id = customer.get("sec_uid", customer_name) if customer else customer_name
            content = self.customer_acquisition_service.get_nurture_message(customer_id)

            if content:
                return {
                    "success": True,
                    "customer_name": customer_name,
                    "content": content
                }
            else:
                return {
                    "success": False,
                    "message": "没有更多培育消息或培育已完成",
                    "customer_name": customer_name
                }

        except Exception as e:
            logger.error(f"获取培育消息失败: {e}")
            return {
                "success": False,
                "message": f"获取失败: {str(e)}",
                "customer_name": customer_name
            }

    def analyze_all_customers_intent(self) -> dict:
        """
        分析所有客户的购买意向

        Returns:
            dict: 批量分析结果
        """
        try:
            conversations = self._get_chat_store().get_all_conversations_dicts()

            results = []
            high_intent_count = 0
            medium_intent_count = 0
            low_intent_count = 0

            for conv in conversations:
                customer_name = conv.get("customer_name", "")
                if not customer_name:
                    continue

                analysis = self.analyze_purchase_intent(customer_name)

                if analysis.get("success"):
                    score = analysis.get("score", {}).get("total_score", 0)

                    if score >= 70:
                        high_intent_count += 1
                    elif score >= 40:
                        medium_intent_count += 1
                    else:
                        low_intent_count += 1

                    results.append({
                        "customer_name": customer_name,
                        "total_score": score,
                        "lifecycle_stage": analysis.get("lifecycle_stage"),
                        "buying_role": analysis.get("buying_role"),
                        "purchase_probability": analysis.get("score", {}).get("purchase_probability", 0)
                    })

            # 按分数排序
            results.sort(key=lambda x: x["total_score"], reverse=True)

            return {
                "success": True,
                "total_customers": len(results),
                "summary": {
                    "high_intent": high_intent_count,
                    "medium_intent": medium_intent_count,
                    "low_intent": low_intent_count
                },
                "results": results
            }

        except Exception as e:
            logger.error(f"批量分析购买意向失败: {e}")
            return {
                "success": False,
                "message": f"分析失败: {str(e)}"
            }
