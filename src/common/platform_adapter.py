"""
平台适配器模块 - 多平台统一接口抽象层

参考MediaCrawler架构设计，实现对多平台的支持
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum
from loguru import logger


class PlatformType(Enum):
    """平台类型枚举"""
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    KUAISHOU = "kuaishou"
    WEIBO = "weibo"
    BILIBILI = "bilibili"
    ZHIHU = "zhihu"


@dataclass
class SearchResult:
    """搜索结果数据"""
    platform: str
    content_id: str
    title: str
    author: str
    author_id: str
    url: str
    cover_url: str = ""
    description: str = ""
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class UserProfile:
    """用户资料数据"""
    platform: str
    user_id: str
    sec_uid: str
    nickname: str
    avatar_url: str
    unique_id: str
    gender: str = ""  # m/f
    birthday: str = ""
    description: str = ""
    following_count: int = 0
    follower_count: int = 0
    like_count: int = 0
    is_verified: bool = False
    verified_info: str = ""


@dataclass
class Comment:
    """评论数据"""
    platform: str
    comment_id: str
    content_id: str
    user_id: str
    sec_uid: str
    nickname: str
    avatar_url: str
    content: str
    like_count: int = 0
    reply_count: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    parent_id: str = ""


@dataclass
class ChatMessage:
    """聊天消息数据"""
    platform: str
    message_id: str
    conversation_id: str
    sender_id: str
    sender_name: str
    sender_avatar: str
    receiver_id: str
    content: str
    message_type: str = "text"  # text/image/video/link
    direction: str = "inbound"  # inbound/outbound
    timestamp: datetime = field(default_factory=datetime.now)
    is_read: bool = False


class IPlatformAdapter(ABC):
    """
    平台适配器抽象基类
    
    所有平台适配器必须实现以下接口方法
    """
    
    def __init__(self, page, db):
        self.page = page
        self.db = db
        self.platform_name = ""
        self.base_url = ""
    
    @abstractmethod
    def login(self, login_method: str = "qrcode") -> bool:
        """
        执行平台登录
        
        Args:
            login_method: 登录方式 (qrcode/phone/password)
            
        Returns:
            bool: 登录是否成功
        """
        pass
    
    @abstractmethod
    def check_login_status(self) -> bool:
        """
        检查登录状态
        
        Returns:
            bool: 是否已登录
        """
        pass
    
    @abstractmethod
    def search(self, keyword: str, limit: int = 20) -> List[SearchResult]:
        """
        关键词搜索
        
        Args:
            keyword: 搜索关键词
            limit: 返回结果数量限制
            
        Returns:
            List[SearchResult]: 搜索结果列表
        """
        pass
    
    @abstractmethod
    def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """
        获取用户资料
        
        Args:
            user_id: 用户ID
            
        Returns:
            Optional[UserProfile]: 用户资料
        """
        pass
    
    @abstractmethod
    def get_comments(self, content_id: str, limit: int = 100) -> List[Comment]:
        """
        获取内容评论
        
        Args:
            content_id: 内容ID (视频/帖子ID)
            limit: 返回评论数量限制
            
        Returns:
            List[Comment]: 评论列表
        """
        pass
    
    @abstractmethod
    def send_message(self, user_id: str, content: str) -> bool:
        """
        发送私信
        
        Args:
            user_id: 接收者用户ID
            content: 消息内容
            
        Returns:
            bool: 发送是否成功
        """
        pass
    
    @abstractmethod
    def get_messages(self, limit: int = 50) -> List[ChatMessage]:
        """
        获取聊天消息列表
        
        Args:
            limit: 返回消息数量限制
            
        Returns:
            List[ChatMessage]: 消息列表
        """
        pass
    
    @abstractmethod
    def get_conversation_messages(self, conversation_id: str, limit: int = 50) -> List[ChatMessage]:
        """
        获取指定会话的消息历史
        
        Args:
            conversation_id: 会话ID
            limit: 返回消息数量限制
            
        Returns:
            List[ChatMessage]: 消息列表
        """
        pass
    
    def random_sleep(self, min_seconds: float = 1.0, max_seconds: float = 3.0):
        """随机休眠，模拟人类行为"""
        import random
        import time
        time.sleep(random.uniform(min_seconds, max_seconds))
    
    def save_customer(self, user_id: str, sec_uid: str, nickname: str, 
                     unique_id: str, profile_url: str, source: str = ""):
        """保存客户到数据库"""
        customer_data = {
            "sec_uid": sec_uid,
            "nickname": nickname,
            "unique_id": unique_id,
            "profile_url": profile_url,
            "source_video_url": source,
            "comment_content": "",
            "comment_time": datetime.now().isoformat(),
            "platform": self.platform_name
        }
        self.db.add_customer(customer_data)


class PlatformFactory:
    """平台适配器工厂类"""
    
    _adapters: Dict[str, type] = {}
    
    @classmethod
    def register(cls, platform_name: str, adapter_class: type):
        """注册平台适配器"""
        cls._adapters[platform_name] = adapter_class
        logger.info(f"注册平台适配器: {platform_name}")
    
    @classmethod
    def get_adapter(cls, platform_name: str, page=None, db=None) -> Optional[IPlatformAdapter]:
        """获取平台适配器实例"""
        adapter_class = cls._adapters.get(platform_name)
        if adapter_class and page and db:
            return adapter_class(page, db)
        return None
    
    @classmethod
    def get_supported_platforms(cls) -> List[str]:
        """获取支持的平台列表"""
        return list(cls._adapters.keys())
