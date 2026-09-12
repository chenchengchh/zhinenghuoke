from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from loguru import logger

from src.common.chat_store import ChatStoreFacade
from src.common.intent_analyzer import IntentAnalyzer
from src.common.intent_scoring_policy import (
    build_effective_intent_score,
    extract_contact_info,
    has_strong_purchase_intent,
    score_to_follow_up_priority,
    score_to_intent_level,
    score_to_lead_score,
)
from src.web.bot_service import get_bot_service
from src.web.dependencies.ai import get_enhanced_customer_ai_service

router = APIRouter(tags=["意图识别"])


class IntentAnalyzeRequest(BaseModel):
    customer_id: str
    platform: str = "douyin"
    customer_name: Optional[str] = None
    conversation_id: Optional[str] = None
    message_content: Optional[str] = None


class IntentRecognizeRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    customer_id: Optional[str] = None
    platform: str = "douyin"


class IntentPredictRequest(BaseModel):
    session_id: str
    customer_id: Optional[str] = None


def _simple_intent_recognition(message: str) -> dict:
    message_lower = message.lower()
    intent_rules = {
        "price_inquiry": ["价格", "多少钱", "费用", "收费", "套餐", "优惠"],
        "product_inquiry": ["功能", "产品", "介绍", "有什么", "能做什么", "系统"],
        "service_inquiry": ["服务", "售后", "支持", "帮助"],
        "cooperation_intent": ["合作", "代理", "加盟", "分销"],
        "purchase_intent": ["购买", "下单", "订购", "开通"],
        "complaint": ["投诉", "不满", "差评", "问题"],
        "greeting": ["你好", "您好", "在吗", "在不在"],
        "thanks": ["谢谢", "感谢", "辛苦"],
        "farewell": ["再见", "拜拜", "下次"],
    }

    detected_intent = "unknown"
    confidence = 0.5
    keywords: List[str] = []

    for intent, keywords_list in intent_rules.items():
        for keyword in keywords_list:
            if keyword in message_lower:
                detected_intent = intent
                keywords.append(keyword)
                confidence = 0.8
                break
        if detected_intent != "unknown":
            break

    sentiment = "neutral"
    if any(word in message_lower for word in ["好", "棒", "满意", "喜欢", "谢谢"]):
        sentiment = "positive"
    elif any(word in message_lower for word in ["差", "不满", "投诉", "问题", "不好"]):
        sentiment = "negative"

    urgency = "medium"
    if any(word in message_lower for word in ["急", "马上", "立即", "紧急"]):
        urgency = "high"
    elif any(word in message_lower for word in ["以后", "不急", "慢慢"]):
        urgency = "low"

    return {
        "primary_intent": detected_intent,
        "secondary_intents": [],
        "confidence": confidence,
        "keywords": keywords[:5],
        "entities": {},
        "sentiment": sentiment,
        "urgency": urgency,
        "need_human": detected_intent == "complaint" or confidence < 0.6,
        "reasoning": f"基于关键词匹配: {', '.join(keywords[:3]) if keywords else '无匹配关键词'}",
    }


async def _run_lead_analysis(request: IntentAnalyzeRequest):
    db = get_bot_service().db
    chat_store = ChatStoreFacade(db)
    customer = db.get_user_by_id(request.customer_id, request.platform)

    if not customer:
        customer = db.get_customer_by_nickname(request.customer_id, request.platform)

    if not customer:
        for conv in chat_store.get_all_conversations_dicts():
            if (
                conv.get("customer_name") == request.customer_id
                or (request.customer_name and conv.get("customer_name") == request.customer_name)
                or (request.conversation_id and conv.get("conversation_id") == request.conversation_id)
            ):
                customer = {
                    "sec_uid": conv.get("customer_id", ""),
                    "nickname": conv.get("customer_name", ""),
                    "platform": request.platform,
                    "comment_content": request.message_content or conv.get("last_message_content", ""),
                }
                break

    if not customer:
        return JSONResponse(status_code=404, content={"message": f"客户不存在: {request.customer_id}"})

    try:
        customer_name = (
            customer.get("nickname")
            or customer.get("customer_name")
            or request.customer_name
            or request.customer_id
        )

        conversations = chat_store.get_all_conversations_dicts()
        conversation = None
        for conv in conversations:
            if conv.get("platform") != request.platform:
                continue
            if request.conversation_id and conv.get("conversation_id") == request.conversation_id:
                conversation = conv
                break
            if conv.get("customer_id") == request.customer_id:
                conversation = conv
                break
            if customer_name and conv.get("customer_name") == customer_name:
                conversation = conv
                break

        conversation_id = request.conversation_id or (conversation or {}).get("conversation_id", "")
        message_history = chat_store.get_recent_messages_dicts(conversation_id, limit=200) if conversation_id else []

        if request.message_content:
            content_exists = any((msg.get("content") or "").strip() == request.message_content.strip() for msg in message_history)
            if not content_exists:
                message_history.append(
                    {
                        "conversation_id": conversation_id,
                        "content": request.message_content.strip(),
                        "direction": "inbound",
                        "created_at": datetime.now().isoformat(),
                        "sender_name": customer_name,
                        "platform": request.platform,
                    }
                )

        analyzer = IntentAnalyzer()
        customer_messages = analyzer._get_recent_messages(message_history, limit=200, customer_only=True)
        latest_customer_message = (customer_messages[-1].get("content") or "").strip() if customer_messages else ""

        enriched_customer = dict(customer)
        enriched_customer.setdefault("sec_uid", customer.get("sec_uid") or request.customer_id)
        enriched_customer.setdefault("platform", request.platform)
        enriched_customer["nickname"] = customer_name
        if not enriched_customer.get("comment_content"):
            enriched_customer["comment_content"] = request.message_content or latest_customer_message

        result = analyzer.analyze(enriched_customer, message_history)
        customer_sec_uid = enriched_customer.get("sec_uid", request.customer_id)

        raw_total_score = float(result.score.total_score or 0)
        primary_keywords = result.primary_intent.keywords_matched if result.primary_intent else []
        secondary_keywords = [
            keyword
            for intent in result.secondary_intents
            for keyword in intent.keywords_matched[:3]
        ]
        risk_factors = []
        if result.rejection_info:
            rejection_type = result.rejection_info.get("type")
            if rejection_type:
                risk_factors.append(f"拒绝信号: {rejection_type}")
            rejection_text = result.rejection_info.get("text")
            if rejection_text:
                risk_factors.append(rejection_text[:50])

        opportunity_factors = []
        if result.primary_intent:
            opportunity_factors.append(f"主意图: {result.primary_intent.intent_type.value}")
        if primary_keywords:
            opportunity_factors.extend(primary_keywords[:3])
        if secondary_keywords:
            opportunity_factors.extend(secondary_keywords[:2])
        conversion_signals = list(result.factors.get("conversion_signals", []) or [])
        if conversion_signals:
            opportunity_factors.extend([f"转化信号: {signal}" for signal in conversion_signals[:3]])

        contact_info = extract_contact_info(message_history, customer_name)
        strong_purchase_intent = has_strong_purchase_intent(
            message_history,
            {
                "lifecycle_stage": result.factors.get("purchase_stage", "awareness"),
                "signals_detected": conversion_signals,
                "opportunity_factors": opportunity_factors,
                "recommended_response_type": result.recommended_response_type,
                "predicted_next_action": result.predicted_next_action,
            },
        )
        total_score = build_effective_intent_score(
            explicit_score=raw_total_score,
            purchase_probability=round(max(0.0, min(raw_total_score, 100.0)) / 100.0, 4),
            intent_level=result.level.value,
            has_contact_info=bool(contact_info),
            has_strong_purchase_intent=strong_purchase_intent,
        )
        normalized_level = score_to_intent_level(total_score)
        lead_score = score_to_lead_score(total_score)
        follow_up_priority = score_to_follow_up_priority(total_score)

        db.update_customer_intent(
            customer_sec_uid,
            request.platform,
            normalized_level,
            total_score,
        )
        if conversation_id:
            db.update_conversation_intent(
                conversation_id,
                {
                    "total_score": total_score,
                    "intent_level": normalized_level,
                    "lead_score": lead_score,
                    "follow_up_priority": follow_up_priority,
                    "purchase_probability": round(max(0.0, min(total_score, 100.0)) / 100.0, 4),
                    "estimated_deal_size": (conversation or {}).get("estimated_deal_size", 0),
                    "lifecycle_stage": result.factors.get("purchase_stage", "awareness"),
                    "buying_role": (conversation or {}).get("buying_role", "unknown"),
                    "signals_detected": list(dict.fromkeys([*primary_keywords[:4], *secondary_keywords[:4], *conversion_signals[:4]])),
                    "risk_factors": risk_factors,
                    "opportunity_factors": opportunity_factors,
                    "predicted_next_action": result.predicted_next_action,
                    "recommended_response_type": result.recommended_response_type,
                },
            )
            try:
                from src.common.follow_up_reminder_service import get_follow_up_reminder_service

                conversation = next(
                    (
                        item
                        for item in (chat_store.get_all_conversations_dicts() or [])
                        if str(item.get("conversation_id") or "").strip() == str(conversation_id).strip()
                    ),
                    None,
                )
                if conversation:
                    messages = db.sanitize_message_records(
                        chat_store.get_recent_messages_dicts(conversation_id, limit=9999) or []
                    )
                    get_follow_up_reminder_service().evaluate_and_notify_conversation(conversation, messages)
            except Exception:
                logger.debug("刷新待跟进提醒失败", exc_info=True)

        return {
            "analysis_type": "lead_scoring",
            "customer_id": customer_sec_uid,
            "customer_name": customer_name,
            "conversation_id": conversation_id,
            "message_summary": {
                "message_count": len(message_history),
                "customer_message_count": len(customer_messages),
                "outbound_message_count": sum(1 for msg in message_history if msg.get("direction") == "outbound"),
                "latest_customer_message": latest_customer_message,
            },
            "intent_level": normalized_level,
            "intent_score": total_score,
            "raw_intent_score": raw_total_score,
            "behavior_score": result.score.behavior_score,
            "semantic_score": result.score.semantic_score,
            "interaction_score": result.score.interaction_score,
            "emotion_score": result.score.emotion_score,
            "urgency_score": result.score.urgency_score,
            "stage_score": result.score.stage_score,
            "confidence": result.score.confidence,
            "primary_intent": {
                "type": result.primary_intent.intent_type.value,
                "strength": result.primary_intent.strength.value,
                "score": result.primary_intent.score,
                "keywords": result.primary_intent.keywords_matched,
                "confidence": result.primary_intent.confidence,
            } if result.primary_intent else None,
            "secondary_intents": [
                {
                    "type": intent.intent_type.value,
                    "strength": intent.strength.value,
                    "score": intent.score,
                    "keywords": intent.keywords_matched,
                    "confidence": intent.confidence,
                }
                for intent in result.secondary_intents
            ],
            "rejection_info": result.rejection_info,
            "predicted_next_action": result.predicted_next_action,
            "recommended_response_type": result.recommended_response_type,
            "conversation_intent": {
                "lead_score": lead_score,
                "follow_up_priority": follow_up_priority,
                "purchase_probability": round(max(0.0, min(total_score, 100.0)) / 100.0, 4),
                "contact_provided": bool(contact_info),
                "strong_purchase_intent": strong_purchase_intent,
                "lifecycle_stage": result.factors.get("purchase_stage", "awareness"),
                "signals_detected": list(dict.fromkeys([*primary_keywords[:4], *secondary_keywords[:4], *conversion_signals[:4]])),
                "risk_factors": risk_factors,
                "opportunity_factors": opportunity_factors,
            },
            "factors": result.factors,
        }
    except Exception as exc:
        logger.error(f"意向分析失败: {exc}")
        return JSONResponse(status_code=500, content={"message": f"意向分析失败: {str(exc)}"})


@router.post("/api/intent/analyze")
async def analyze_intent(request: IntentAnalyzeRequest):
    """兼容旧入口：会话级意向分析/线索评分，不是消息级意图识别。"""
    return await _run_lead_analysis(request)


@router.post("/api/lead-analysis/analyze")
async def analyze_lead_scoring(request: IntentAnalyzeRequest):
    """明确语义的新入口：会话级意向分析/线索评分。"""
    return await _run_lead_analysis(request)


@router.post("/api/intent/recognize")
async def recognize_intent(request: IntentRecognizeRequest):
    try:
        from src.common.enhanced_customer_service import resolve_context_session_id

        service = get_enhanced_customer_ai_service()
        recognizer = service.intent_recognizer
        bert_recognizer = getattr(service, "_bert_recognizer", None)
        resolved_session_id = resolve_context_session_id(
            platform=request.platform or "douyin",
            customer_id=request.customer_id or "",
            provided_session_id=request.session_id,
            conversation_history=None,
        )

        context = recognizer.getContext(resolved_session_id) if resolved_session_id else None
        if bert_recognizer and bert_recognizer.is_available():
            result = bert_recognizer.predict_with_fallback(
                request.message,
                resolved_session_id or "",
                context=cast(Any, context),
            )
        else:
            result = recognizer.recognize(
                message=request.message,
                sessionId=resolved_session_id or "",
                context=cast(Any, context),
            )

        return {
            "session_id": resolved_session_id,
            "primary_intent": result.primaryIntent.value,
            "secondary_intents": [i.value for i in result.secondaryIntents],
            "confidence": result.confidence,
            "keywords": result.keywords,
            "entities": result.entities,
            "sentiment": result.sentiment.value if hasattr(result.sentiment, "value") else str(result.sentiment),
            "urgency": result.urgency.value if hasattr(result.urgency, "value") else str(result.urgency),
            "need_human": result.needHuman,
            "reasoning": result.reasoning,
        }
    except ImportError:
        return _simple_intent_recognition(request.message)
    except Exception as exc:
        logger.error(f"意图识别失败: {exc}")
        return _simple_intent_recognition(request.message)


@router.post("/api/intent/predict")
async def predict_intent(request: IntentPredictRequest):
    service = get_enhanced_customer_ai_service()
    intent_recognizer = service.intent_recognizer
    context = intent_recognizer.getContext(request.session_id) if request.session_id else None
    predictions = intent_recognizer.predict_next_intent(
        sessionId=request.session_id,
        context=cast(Any, context if context else intent_recognizer.getContext(request.session_id or "")),
    )
    return {"session_id": request.session_id, "predictions": predictions}


@router.get("/api/intent/insights/{session_id}")
async def get_intent_insights(session_id: str):
    service = get_enhanced_customer_ai_service()
    intent_recognizer = service.intent_recognizer
    context = intent_recognizer.getContext(session_id)
    return intent_recognizer.get_conversation_insights(
        sessionId=session_id,
        context=cast(Any, context if context else intent_recognizer.getContext(session_id)),
    )


@router.get("/api/intent/transition/{session_id}")
async def get_intent_transition(session_id: str):
    service = get_enhanced_customer_ai_service()
    intent_recognizer = service.intent_recognizer
    context = intent_recognizer.getContext(session_id)
    return intent_recognizer.analyze_intent_transition(
        sessionId=session_id,
        context=cast(Any, context if context else intent_recognizer.getContext(session_id)),
    )


@router.get("/api/intent/context/{session_id}")
async def get_intent_context(session_id: str):
    service = get_enhanced_customer_ai_service()
    intent_recognizer = service.intent_recognizer
    context = intent_recognizer.getContext(session_id)
    if not context:
        return {
            "session_id": session_id,
            "current_intent": None,
            "entities": {},
            "topic_flow": [],
            "sentiment_history": [],
        }

    return {
        "session_id": session_id,
        "current_intent": getattr(context, "current_intent", None),
        "entities": getattr(context, "entities", {}),
        "topic_flow": getattr(context, "topicFlow", getattr(context, "topic_flow", [])),
        "sentiment_history": getattr(context, "sentiment_history", []),
    }
