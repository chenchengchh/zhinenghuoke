from __future__ import annotations

import threading

from src.web.bot_service import get_bot_service

_enhanced_analytics_instance = None
_enhanced_analytics_lock = threading.Lock()


def get_enhanced_analytics_app_service():
    global _enhanced_analytics_instance
    if _enhanced_analytics_instance is None:
        with _enhanced_analytics_lock:
            if _enhanced_analytics_instance is None:
                bot_service = get_bot_service()
                from src.common.enhanced_analytics_service import get_enhanced_analytics_service

                _enhanced_analytics_instance = get_enhanced_analytics_service(
                    database=bot_service.db,
                    purchase_intent_analyzer=bot_service.purchase_intent_analyzer,
                )
    return _enhanced_analytics_instance
