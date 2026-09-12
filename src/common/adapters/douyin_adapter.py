"""
抖音平台适配器实现

参考MediaCrawler架构，基于Playwright实现
"""
import time
import random
from typing import List, Optional
from datetime import datetime
from loguru import logger
from src.common.platform_adapter import (
    IPlatformAdapter, SearchResult, UserProfile, Comment, ChatMessage, PlatformType
)
from src.common.utils import random_sleep


class DouyinAdapter(IPlatformAdapter):
    """
    抖音平台适配器
    
    实现对抖音平台的爬取和消息发送功能
    """
    
    PLATFORM_NAME = "douyin"
    BASE_URL = "https://www.douyin.com"
    SEARCH_URL = "https://www.douyin.com/search"
    MESSAGE_URL = "https://www.douyin.com/message"
    USER_URL = "https://www.douyin.com/user"
    
    def __init__(self, page, db):
        super().__init__(page, db)
        self.platform_name = self.PLATFORM_NAME
        self.base_url = self.BASE_URL
    
    def login(self, login_method: str = "qrcode") -> bool:
        """抖音扫码登录"""
        try:
            logger.info("开始抖音登录...")
            self.page.goto(f"{self.BASE_URL}/login", wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            if login_method == "qrcode":
                # 等待二维码加载
                self.page.wait_for_selector(".login-qrcode, .qrcode-img", timeout=10000)
                logger.info("请在30秒内扫码登录...")
                
                # 等待登录成功 (检测用户信息元素)
                try:
                    self.page.wait_for_selector(
                        ".user-info, .avatar-wrapper, [class*='user']", 
                        timeout=120000
                    )
                    logger.info("抖音登录成功!")
                    return True
                except Exception as e:
                    logger.error(f"登录超时: {e}")
                    return False
            
            return False
            
        except Exception as e:
            logger.error(f"抖音登录失败: {e}")
            return False
    
    def check_login_status(self) -> bool:
        """检查抖音登录状态"""
        try:
            # 访问首页检查登录状态
            self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
            random_sleep(1, 2)
            
            # 检查是否存在登录后的用户元素
            logged_in = self.page.query_selector(
                ".user-info, .avatar-wrapper, [class*='user-avatar'], [data-e2e='user-avatar']"
            ) is not None
            
            return logged_in
            
        except Exception as e:
            logger.error(f"检查登录状态失败: {e}")
            return False
    
    def search(self, keyword: str, limit: int = 20) -> List[SearchResult]:
        """抖音关键词搜索"""
        results = []
        
        try:
            logger.info(f"开始搜索: {keyword}")
            
            # 访问搜索页面
            search_url = f"{self.SEARCH_URL}?keyword={keyword}"
            self.page.goto(search_url, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 等待搜索结果加载
            try:
                self.page.wait_for_selector(
                    ".search-result, .video-list, [class*='result']", 
                    timeout=10000
                )
            except Exception:
                logger.warning("未找到搜索结果容器")
            
            # 滚动加载更多内容
            for _ in range(3):
                self.page.evaluate("window.scrollBy(0, 500)")
                random_sleep(1, 2)
            
            # 提取视频元素
            video_selectors = [
                ".search-result .video-item",
                ".video-list .video-card",
                "[class*='video-item']",
                "[class*='feed-item']"
            ]
            
            video_elements = []
            for selector in video_selectors:
                elements = self.page.query_selector_all(selector)
                if elements:
                    video_elements = elements
                    break
            
            logger.info(f"找到 {len(video_elements)} 个视频")
            
            # 解析视频信息
            for element in video_elements[:limit]:
                try:
                    # 提取视频信息 (实际选择器需要根据页面调整)
                    title = ""
                    author = ""
                    content_id = ""
                    url = ""
                    
                    # 尝试多种选择器
                    title_el = element.query_selector(
                        "[class*='title'], [class*='desc'], .video-title"
                    )
                    if title_el:
                        title = title_el.inner_text().strip()
                    
                    author_el = element.query_selector(
                        "[class*='author'], [class*='nickname'], .author-name"
                    )
                    if author_el:
                        author = author_el.inner_text().strip()
                    
                    link_el = element.query_selector("a")
                    if link_el:
                        href = link_el.get_attribute("href")
                        if href:
                            url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                            # 提取视频ID
                            if "/video/" in href:
                                content_id = href.split("/video/")[-1].split("?")[0]
                    
                    if title or url:
                        result = SearchResult(
                            platform=self.platform_name,
                            content_id=content_id or str(random.randint(100000, 999999)),
                            title=title,
                            author=author,
                            author_id="",
                            url=url,
                            description=title
                        )
                        results.append(result)
                        
                except Exception as e:
                    logger.debug(f"解析视频元素失败: {e}")
            
            logger.info(f"搜索完成，找到 {len(results)} 条结果")
            
        except Exception as e:
            logger.error(f"搜索失败: {e}")
        
        return results
    
    def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """获取抖音用户资料"""
        try:
            logger.info(f"获取用户资料: {user_id}")
            
            # 访问用户主页
            user_url = f"{self.USER_URL}/{user_id}"
            self.page.goto(user_url, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 等待页面加载
            try:
                self.page.wait_for_selector(
                    "[class*='profile'], [class*='user']", 
                    timeout=10000
                )
            except Exception:
                pass
            
            # 提取用户信息
            nickname = ""
            avatar_url = ""
            unique_id = ""
            following_count = 0
            follower_count = 0
            like_count = 0
            
            # 昵称
            nickname_el = self.page.query_selector(
                "[class*='nickname'], [class*='name'], .user-name"
            )
            if nickname_el:
                nickname = nickname_el.inner_text().strip()
            
            # 头像
            avatar_el = self.page.query_selector(
                "[class*='avatar'], img[class*='avatar']"
            )
            if avatar_el:
                avatar_url = avatar_el.get_attribute("src") or ""
            
            # 抖音号
            unique_id_el = self.page.query_selector(
                "[class*='unique-id'], [class*='douyin-id'], .unique-id"
            )
            if unique_id_el:
                unique_id = unique_id_el.inner_text().strip().replace("抖音号: ", "")
            
            # 粉丝数
            follower_el = self.page.query_selector(
                "[class*='follower'], [class*='fans'] span, [data-e2e='follower']"
            )
            if follower_el:
                follower_count = self._parse_number(follower_el.inner_text())
            
            # 关注数
            following_el = self.page.query_selector(
                "[class*='following'], [data-e2e='following']"
            )
            if following_el:
                following_count = self._parse_number(following_el.inner_text())
            
            # 获赞总数
            like_el = self.page.query_selector(
                "[class*='like'], [data-e2e='like']"
            )
            if like_el:
                like_count = self._parse_number(like_el.inner_text())
            
            profile = UserProfile(
                platform=self.platform_name,
                user_id=user_id,
                sec_uid=user_id,
                nickname=nickname,
                avatar_url=avatar_url,
                unique_id=unique_id,
                following_count=following_count,
                follower_count=follower_count,
                like_count=like_count
            )
            
            logger.info(f"获取用户资料成功: {nickname}")
            return profile
            
        except Exception as e:
            logger.error(f"获取用户资料失败: {e}")
            return None
    
    def get_comments(self, content_id: str, limit: int = 100) -> List[Comment]:
        """获取视频评论"""
        comments = []
        
        try:
            logger.info(f"获取视频评论: {content_id}")
            
            # 访问视频页面
            video_url = f"{self.BASE_URL}/video/{content_id}"
            self.page.goto(video_url, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 滚动加载评论
            for i in range(5):
                self.page.evaluate("window.scrollBy(0, 300)")
                random_sleep(0.5, 1)
            
            # 等待评论加载
            try:
                self.page.wait_for_selector(
                    "[class*='comment'], .comment-list", 
                    timeout=5000
                )
            except Exception:
                pass
            
            # 提取评论元素
            comment_elements = self.page.query_selector_all(
                "[class*='comment-item'], .comment-item, [class*='item']"
            )
            
            logger.info(f"找到 {len(comment_elements)} 条评论")
            
            for element in comment_elements[:limit]:
                try:
                    # 提取评论信息
                    comment_id = element.get_attribute("data-id") or str(random.randint(10000, 99999))
                    
                    user_id = ""
                    nickname = ""
                    avatar_url = ""
                    content = ""
                    like_count = 0
                    
                    # 用户ID
                    user_id = element.get_attribute("data-user-id") or ""
                    
                    # 昵称
                    nickname_el = element.query_selector("[class*='nickname'], .nickname")
                    if nickname_el:
                        nickname = nickname_el.inner_text().strip()
                    
                    # 头像
                    avatar_el = element.query_selector("img[class*='avatar']")
                    if avatar_el:
                        avatar_url = avatar_el.get_attribute("src") or ""
                    
                    # 评论内容
                    content_el = element.query_selector(
                        "[class*='content'], .comment-content, [class*='text']"
                    )
                    if content_el:
                        content = content_el.inner_text().strip()
                    
                    # 点赞数
                    like_el = element.query_selector("[class*='like'], .like-count")
                    if like_el:
                        like_count = self._parse_number(like_el.inner_text())
                    
                    if content:
                        comment = Comment(
                            platform=self.platform_name,
                            comment_id=comment_id,
                            content_id=content_id,
                            user_id=user_id,
                            sec_uid=user_id,
                            nickname=nickname,
                            avatar_url=avatar_url,
                            content=content,
                            like_count=like_count
                        )
                        comments.append(comment)
                        
                except Exception as e:
                    logger.debug(f"解析评论失败: {e}")
            
            logger.info(f"获取评论完成，共 {len(comments)} 条")
            
        except Exception as e:
            logger.error(f"获取评论失败: {e}")
        
        return comments
    
    def send_message(self, user_id: str, content: str) -> bool:
        """发送私信"""
        try:
            logger.info(f"发送私信给用户: {user_id}")
            
            # 访问用户主页
            user_url = f"{self.USER_URL}/{user_id}"
            self.page.goto(user_url, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 点击私信按钮
            message_selectors = [
                "[class*='message'], [class*='私信']",
                "button[class*='message']",
                "[data-e2e='message']",
                "[class*='dm']"
            ]
            
            clicked = False
            for selector in message_selectors:
                try:
                    btn = self.page.query_selector(selector)
                    if btn:
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            
            if not clicked:
                # 尝试直接访问私信页面
                message_url = f"{self.MESSAGE_URL}?to={user_id}"
                self.page.goto(message_url, wait_until="domcontentloaded")
                random_sleep(2, 3)
            
            # 等待私信输入框
            self.page.wait_for_selector(
                "textarea[class*='input'], input[class*='input'], [class*='message-input']",
                timeout=10000
            )
            random_sleep(1, 2)
            
            # 输入消息
            input_selectors = [
                "textarea[class*='input']",
                "input[class*='input']", 
                "[class*='message-input']",
                "[contenteditable='true']"
            ]
            
            for selector in input_selectors:
                try:
                    input_el = self.page.query_selector(selector)
                    if input_el:
                        input_el.fill(content)
                        random_sleep(0.5, 1)
                        break
                except Exception:
                    continue
            
            # 发送消息
            self.page.press("body", "Enter")
            random_sleep(1, 2)
            
            logger.info(f"私信发送成功: {content[:20]}...")
            return True
            
        except Exception as e:
            logger.error(f"发送私信失败: {e}")
            return False
    
    def get_messages(self, limit: int = 50) -> List[ChatMessage]:
        """获取聊天消息列表"""
        messages = []
        
        try:
            logger.info("获取聊天消息列表")
            
            # 访问消息页面
            self.page.goto(self.MESSAGE_URL, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 等待消息列表加载
            try:
                self.page.wait_for_selector(
                    "[class*='chat'], [class*='message-list']", 
                    timeout=10000
                )
            except Exception:
                pass
            
            # 提取会话元素
            chat_elements = self.page.query_selector_all(
                "[class*='chat-item'], [class*='message-item'], .chat-item"
            )
            
            logger.info(f"找到 {len(chat_elements)} 个会话")
            
            for element in chat_elements[:limit]:
                try:
                    conversation_id = element.get_attribute("data-id") or str(random.randint(1000, 9999))
                    
                    sender_id = element.get_attribute("data-user-id") or ""
                    sender_name = ""
                    content = ""
                    is_read = False
                    
                    # 昵称
                    nickname_el = element.query_selector("[class*='nickname'], .nickname")
                    if nickname_el:
                        sender_name = nickname_el.inner_text().strip()
                    
                    # 最后消息
                    msg_el = element.query_selector("[class*='last-message'], .last-msg")
                    if msg_el:
                        content = msg_el.inner_text().strip()
                    
                    # 已读状态
                    is_read = "read" in element.get_attribute("class", "")
                    
                    if sender_name or content:
                        message = ChatMessage(
                            platform=self.platform_name,
                            message_id=conversation_id,
                            conversation_id=conversation_id,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            sender_avatar="",
                            receiver_id="",
                            content=content,
                            is_read=is_read
                        )
                        messages.append(message)
                        
                except Exception as e:
                    logger.debug(f"解析会话失败: {e}")
            
        except Exception as e:
            logger.error(f"获取消息列表失败: {e}")
        
        return messages
    
    def get_conversation_messages(self, conversation_id: str, limit: int = 50) -> List[ChatMessage]:
        """获取指定会话的消息历史"""
        messages = []
        
        try:
            logger.info(f"获取会话消息: {conversation_id}")
            
            # 访问会话页面
            conversation_url = f"{self.MESSAGE_URL}/chat/{conversation_id}"
            self.page.goto(conversation_url, wait_until="domcontentloaded")
            random_sleep(2, 3)
            
            # 等待消息加载
            try:
                self.page.wait_for_selector(
                    "[class*='message-content'], .message-list", 
                    timeout=10000
                )
            except Exception:
                pass
            
            # 滚动加载更多历史消息
            for _ in range(3):
                self.page.evaluate("window.scrollBy(0, -500)")
                random_sleep(1, 2)
            
            # 提取消息元素
            message_elements = self.page.query_selector_all(
                "[class*='message-item'], .message-item, [class*='msg']"
            )
            
            for element in message_elements[:limit]:
                try:
                    message_id = element.get_attribute("data-id") or str(random.randint(10000, 99999))
                    
                    sender_id = element.get_attribute("data-user-id") or ""
                    sender_name = ""
                    content = ""
                    direction = "inbound"
                    is_read = True
                    
                    # 判断消息方向
                    if "outgoing" in element.get_attribute("class", "") or "self" in element.get_attribute("class", ""):
                        direction = "outbound"
                    
                    # 昵称
                    nickname_el = element.query_selector("[class*='nickname']")
                    if nickname_el:
                        sender_name = nickname_el.inner_text().strip()
                    
                    # 内容
                    content_el = element.query_selector("[class*='content'], .text")
                    if content_el:
                        content = content_el.inner_text().strip()
                    
                    if content:
                        message = ChatMessage(
                            platform=self.platform_name,
                            message_id=message_id,
                            conversation_id=conversation_id,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            sender_avatar="",
                            receiver_id="",
                            content=content,
                            direction=direction,
                            is_read=is_read
                        )
                        messages.append(message)
                        
                except Exception as e:
                    logger.debug(f"解析消息失败: {e}")
            
        except Exception as e:
            logger.error(f"获取会话消息失败: {e}")
        
        return messages
    
    def _parse_number(self, text: str) -> int:
        """解析数字字符串 (如 10万 -> 100000)"""
        import re
        text = text.strip()
        
        # 提取数字和单位
        match = re.search(r'([\d.]+)(万|亿)?', text)
        if not match:
            return 0
        
        num = float(match.group(1))
        unit = match.group(2)
        
        if unit == "万":
            num *= 10000
        elif unit == "亿":
            num *= 100000000
        
        return int(num)


# 注册抖音适配器
from src.common.platform_adapter import PlatformFactory
PlatformFactory.register(DouyinAdapter.PLATFORM_NAME, DouyinAdapter)
