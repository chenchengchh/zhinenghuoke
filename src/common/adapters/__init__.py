"""
平台适配器模块初始化
"""
from src.common.adapters.douyin_adapter import DouyinAdapter
from src.common.platform_adapter import PlatformFactory, PlatformType

__all__ = [
    "PlatformFactory",
    "PlatformType", 
    "DouyinAdapter",
]
