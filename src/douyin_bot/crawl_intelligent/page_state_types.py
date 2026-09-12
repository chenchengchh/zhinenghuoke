"""页面状态相关数据类型定义。

本模块包含页面状态评估所需的所有数据类型：
- PageState 枚举：页面状态
- PopupType 枚举：弹窗类型
- PopupDetectionResult 数据类：弹窗检测结果
- LayoutState 数据类：布局状态
- PageStateAssessment 数据类：完整页面状态评估
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


class PageState(Enum):
    """页面状态枚举"""
    NORMAL = "normal"                    # 正常状态
    LOGIN_POPUP = "login_popup"          # 有登录弹窗
    RECOMMENDED_VIDEO_POPUP = "recommended_video_popup"  # 推荐视频弹窗
    VIDEO_DETAIL_PAGE = "video_detail"   # 在视频详情页（非搜索页）
    RISK_CONTROL = "risk_control"        # 风控/验证码页面
    SINGLE_COLUMN_LAYOUT = "single_column"  # 单列布局
    MULTI_COLUMN_LAYOUT = "multi_column"    # 多列布局
    UNKNOWN_LAYOUT = "unknown_layout"    # 布局未知
    PAGE_NOT_LOADED = "not_loaded"       # 页面未加载
    NETWORK_ERROR = "network_error"      # 网络错误


class PopupType(Enum):
    """弹窗类型"""
    LOGIN_MODAL = "login_modal"
    RECOMMENDED_VIDEO = "recommended_video"
    ADVERTISEMENT = "advertisement"
    UNKNOWN = "unknown"


@dataclass
class PopupDetectionResult:
    """弹窗检测结果"""
    has_popup: bool
    popup_type: Optional[PopupType] = None
    close_selectors: List[str] = field(default_factory=list)
    severity: str = "low"  # low / medium / high (是否阻断主流程)
    description: str = ""


@dataclass
class LayoutState:
    """布局状态"""
    layout_type: str  # single_column / multi_column / unknown
    control_found: bool
    card_count: int
    row_distribution: List[int] = field(default_factory=list)  # 每行卡片数
    can_switch: bool = True  # 是否可以切换


@dataclass
class PageStateAssessment:
    """完整页面状态评估"""
    timestamp: float = 0.0
    url: str = ""
    page_state: PageState = PageState.NORMAL
    popup_result: Optional[PopupDetectionResult] = None
    layout_state: Optional[LayoutState] = None
    issues: List[str] = field(default_factory=list)  # 发现的问题列表
    recommended_actions: List[str] = field(default_factory=list)  # 推荐的修复动作
    is_ready_for_crawl: bool = True  # 是否准备好可以爬取
