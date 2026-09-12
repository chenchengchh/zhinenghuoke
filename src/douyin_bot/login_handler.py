from playwright.sync_api import Page
import time
from src.common.utils import logger, random_sleep
from src.config.settings import DOUYIN_HOME_URL

class LoginHandler:
    """
    处理抖音登录逻辑
    """

    def __init__(self, page: Page):
        self.page = page

    def check_login_status(self) -> bool:
        """检查当前是否已登录（优化等待时间，避免长时间阻塞Worker线程）"""
        try:
            from src.config.settings import PAGE_LOAD_TIMEOUT
            
            current_url = ""
            try:
                current_url = self.page.url
            except Exception as e:
                logger.debug(f"获取当前页面URL失败: {e}")
            
            logger.info(f"当前页面URL: {current_url}")
            
            if current_url == "about:blank" or not current_url:
                logger.info("正在打开抖音首页...")
                self.page.goto(DOUYIN_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
                self.page.wait_for_load_state('domcontentloaded')
                time.sleep(2)
                logger.info("抖音首页加载完成")
            elif "douyin.com" not in current_url:
                logger.info(f"当前不在抖音页面，正在导航到抖音首页...")
                self.page.goto(DOUYIN_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)
                self.page.wait_for_load_state('domcontentloaded')
                time.sleep(2)
            else:
                logger.info("已在抖音页面，等待页面加载完成...")
                try:
                    self.page.wait_for_load_state('domcontentloaded', timeout=10000)
                except Exception as e:
                    logger.debug(f"等待 DOM 加载完成失败：{e}")
                time.sleep(1)
                
            # 获取页面标题和 URL 用于调试
            page_title = ""
            page_url = ""
            try:
                page_title = self.page.title()
                page_url = self.page.url
            except Exception:
                pass
            logger.info(f"页面标题：{page_title}, URL: {page_url}")
            
            return self._is_logged_in()
        except Exception as e:
            logger.warning(f"检查登录状态失败: {e}")
            # 尝试重新加载页面
            try:
                logger.info("尝试重新加载抖音首页...")
                self.page.reload(timeout=PAGE_LOAD_TIMEOUT)
                self.page.wait_for_load_state('domcontentloaded')
                time.sleep(2)
                return self._is_logged_in()
            except Exception as reload_e:
                logger.error(f"重新加载页面失败：{reload_e}")
                return False

    def wait_for_login(self) -> bool:
        """等待用户登录"""
        # 确保在首页
        if DOUYIN_HOME_URL not in self.page.url:
             self.page.goto(DOUYIN_HOME_URL, timeout=PAGE_LOAD_TIMEOUT)

        # 等待用户手动登录，最长等待5分钟
        max_wait = 300
        start_time = time.time()
        last_log_time = start_time
        
        while time.time() - start_time < max_wait:
            if self._is_logged_in():
                return True
            
            time.sleep(2)
            if time.time() - last_log_time >= 10:
                logger.info("等待登录中...")
                last_log_time = time.time()

        logger.error("登录超时")
        return False

    def login(self):
        """
        执行登录流程（兼容旧调用）
        """
        if self.check_login_status():
            logger.info("检测到已登录状态")
            return True
            
        logger.info("未检测到登录状态，请手动扫码或登录...")
        if self.wait_for_login():
            logger.info("登录成功！")
            random_sleep(3, 5)
            return True
            
        return False


    def _is_logged_in(self) -> bool:
        """
        检查是否已登录
        """
        try:
            logger.info("开始检查登录状态...")
            
            if not self.page or self.page.is_closed():
                logger.debug("页面已关闭，无法检查登录状态")
                return False
            
            if not self.page.context:
                logger.debug("浏览器上下文不存在，无法检查登录状态")
                return False
            
            cookies = self.page.context.cookies()
            logger.info(f"获取到 {len(cookies)} 个Cookie")
            
            sessionid_cookie = None
            ttwid_cookie = None
            passport_cookie = None
            passport_assist_user_cookie = None
            login_time_cookie = None
            
            for c in cookies:
                if c['name'] == 'sessionid':
                    sessionid_cookie = c
                elif c['name'] == 'ttwid':
                    ttwid_cookie = c
                elif c['name'] == 'passport_csrf_id':
                    passport_cookie = c
                elif c['name'] == 'passport_assist_user':
                    passport_assist_user_cookie = c
                elif c['name'] == 'login_time':
                    login_time_cookie = c
            
            if sessionid_cookie and len(sessionid_cookie['value']) > 10:
                logger.info("通过Cookie检测到已登录 (sessionid存在且有效)")
                return True
            
            if passport_assist_user_cookie and len(passport_assist_user_cookie['value']) > 10:
                logger.info("通过Cookie检测到已登录 (passport_assist_user存在)")
                return True
            
            if login_time_cookie:
                logger.info("通过Cookie检测到已登录 (login_time存在)")
                return True
            
            if ttwid_cookie and passport_cookie and sessionid_cookie and len(sessionid_cookie['value']) > 5:
                logger.info("通过Cookie检测到已登录 (ttwid + passport_csrf_id + sessionid 存在)")
                return True
            
            logger.debug(f"Cookie检测: sessionid={sessionid_cookie is not None}, ttwid={ttwid_cookie is not None}, passport={passport_cookie is not None}, passport_assist_user={passport_assist_user_cookie is not None}")
            
            positive_selectors = [
                "a:has-text('我的')",
                "text='我的'", 
                "text='消息'", 
                "text='创作服务'",
                ".avatar-component-wrapper",
                "div[data-e2e='arrow-avatar']",
                "li:has-text('个人中心')",
                "[class*='user-avatar']",
                "[class*='UserAvatar']"
            ]
            
            for selector in positive_selectors:
                try:
                    el = self.page.query_selector(selector)
                    if el and el.is_visible():
                        logger.info(f"通过元素 '{selector}' 检测到已登录")
                        return True
                except Exception:
                    pass
            
            avatar_selectors = [
                "img[class*='avatar']",
                "img[src*='avatar']",
                "img[data-e2e='avatar']",
            ]
            for selector in avatar_selectors:
                try:
                    el = self.page.query_selector(selector)
                    if el and el.is_visible():
                        logger.info(f"通过头像元素 '{selector}' 检测到已登录")
                        return True
                except Exception:
                    pass
            
            try:
                login_btn = self.page.query_selector("header button:has-text('登录')")
                if login_btn and login_btn.is_visible():
                    logger.info("检测到顶部登录按钮，判定为未登录")
                    return False
            except Exception:
                pass
            
            try:
                modal = self.page.query_selector(".dy-account-close")
                if modal and modal.is_visible():
                    logger.info("检测到登录弹窗，判定为未登录")
                    return False
            except Exception:
                pass
            
            logger.info("未找到明确登录或未登录标志，默认返回False")
            return False
            
        except Exception as e:
            logger.warning(f"检查登录状态出错: {e}")
            return False
