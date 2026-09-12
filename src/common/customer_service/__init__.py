"""
增强智能客服服务包

提供智能客服的核心功能，包括：
- EnhancedCustomerService: 主服务类
- IntentCache: 意图识别缓存
- 工具函数和常量

向后兼容：支持从 src.common.enhanced_customer_service 导入的所有符号
"""

# 导出主服务类和工厂函数
from .main_service import (
    EnhancedCustomerService,
    get_enhanced_customer_service,
)

# 导出意图缓存类
from .intent_cache import IntentCache

# 导出工具函数和常量
from .utils import (
    _deep_merge_dict,
    _DEFAULT_SCHEMA_REPLY_PROFILE,
    _DEFAULT_SCHEMA_REPLY_POLICY,
    _KB_FAQ_PAIR_RE,
    resolve_context_session_id,
    _is_math_related_content,
    _filter_math_history,
    _is_probable_math_query,
    _extract_math_expression,
    _resolve_math_query_locally,
    _build_math_llm_prompt,
    _safe_math_eval,
)

# 定义公共 API
__all__ = [
    # 主服务
    "EnhancedCustomerService",
    "get_enhanced_customer_service",
    
    # 意图缓存
    "IntentCache",
    
    # 工具函数
    "resolve_context_session_id",
    "_deep_merge_dict",
    "_is_probable_math_query",
    "_extract_math_expression",
    "_resolve_math_query_locally",
    "_build_math_llm_prompt",
    "_filter_math_history",
    "_is_math_related_content",
    
    # 常量
    "_DEFAULT_SCHEMA_REPLY_PROFILE",
    "_DEFAULT_SCHEMA_REPLY_POLICY",
    "_KB_FAQ_PAIR_RE",
]
