"""CrawlAgent - 爬取Agent

运行在独立进程中，负责：
1. 搜索视频 - 按关键词搜索抖音视频
2. 爬取评论 - 爬取视频评论并筛选客户
3. 发送私信 - 向筛选出的客户发送私信
4. 客户管理 - 将客户信息保存到数据库
"""
import time
import random
import logging
import multiprocessing
from typing import Optional, Dict, Any

from src.agents.base_agent import BaseAgent, AgentStatus, AgentCommand

logger = logging.getLogger(__name__)


class CrawlAgent(BaseAgent):
    """爬取Agent - 在独立进程中运行搜索爬取和私信发送

    架构：
    - 启动并管理独立浏览器上下文
    - 独立标签页（crawler_page）用于搜索和爬取
    - Crawler组件进行搜索和评论爬取
    - MessageSender组件进行私信发送
    - 通过共享数据库与主进程通信
    """

    def __init__(self, agent_name: str, command_queue: multiprocessing.Queue,
                 status_dict: dict,
                 shared_config: dict = None):
        super().__init__(agent_name, command_queue, status_dict, shared_config)

        self._crawler = None
        self._sender = None
        self._crawler_page = None
        self._db = None
        self._login_handler = None

        self._is_searching = False
        self._is_sending = False

    def _on_start(self) -> bool:
        """启动 CrawlAgent 并准备爬取标签页。"""
        try:
            logger.info(f"[{self.agent_name}] 正在初始化CrawlAgent...")

            if not self._init_browser():
                logger.error(f"[{self.agent_name}] 浏览器初始化失败，CrawlAgent无法启动")
                return False

            from src.common.database import DatabaseManager
            self._db = DatabaseManager()

            self._crawler_page = self._browser_manager.create_crawler_page()
            logger.info(f"[{self.agent_name}] 爬取标签页创建成功")

            from src.douyin_bot.crawler import Crawler
            self._crawler = Crawler(self._crawler_page, self._db)

            from src.douyin_bot.message_sender import MessageSender
            self._sender = MessageSender(self._crawler_page, self._db)

            from src.douyin_bot.login_handler import LoginHandler
            self._login_handler = LoginHandler(self._page)

            self._idle_interval = 5.0

            logger.info(f"[{self.agent_name}] CrawlAgent初始化完成")
            return True

        except Exception as e:
            logger.error(f"[{self.agent_name}] CrawlAgent启动失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def _on_stop(self):
        """停止CrawlAgent"""
        try:
            self._stop_flag = True
            self._is_searching = False
            self._is_sending = False

            if self._crawler:
                self._crawler.set_stop_flag(True)

            logger.info(f"[{self.agent_name}] CrawlAgent已停止")
        except Exception as e:
            logger.error(f"[{self.agent_name}] 停止CrawlAgent失败: {e}")

    def _on_task(self, task_data: dict):
        """处理任务指令"""
        task_type = task_data.get('task_type', '')

        if task_type == 'search':
            keyword = task_data.get('keyword', '')
            max_videos = task_data.get('max_videos', 5)
            comment_keywords = task_data.get('comment_keywords', '')
            comment_time_start = task_data.get('comment_time_start', '')
            comment_time_end = task_data.get('comment_time_end', '')
            skip_crawled = bool(task_data.get('skip_crawled', True))
            self._run_search(
                keyword,
                max_videos,
                comment_keywords,
                comment_time_start,
                comment_time_end,
                skip_crawled,
            )
        elif task_type == 'send':
            message = task_data.get('message', '')
            count = task_data.get('count', 10)
            self._run_send(message, count)
        elif task_type == 'stop_task':
            self._stop_current_task()
        else:
            logger.warning(f"[{self.agent_name}] 未知任务类型: {task_type}")

    def _on_idle(self):
        """空闲时检查登录状态"""
        if not self._is_searching and not self._is_sending:
            try:
                if self._login_handler:
                    is_logged_in = self._login_handler.check_login_status()
                    self._update_status(AgentStatus.RUNNING, logged_in=is_logged_in)
            except Exception:
                pass

    def _run_search(
        self,
        keyword: str,
        max_videos: int,
        comment_keywords: str = "",
        comment_time_start: str = "",
        comment_time_end: str = "",
        skip_crawled: bool = True,
    ):
        """运行搜索任务（分片执行，不阻塞命令处理）"""
        try:
            self._is_searching = True
            self._stop_flag = False

            if self._crawler:
                self._crawler.set_stop_flag(False)

            try:
                is_logged_in = self._login_handler.check_login_status()
            except Exception:
                is_logged_in = False

            self._current_task = f"Searching: {keyword}"
            self._progress_info = {
                "total": max_videos,
                "current": 0,
                "detail": "正在搜索视频列表..."
            }
            self._update_status(AgentStatus.BUSY)

            target_keywords = []
            if comment_keywords and comment_keywords.strip():
                target_keywords = [kw.strip() for kw in comment_keywords.split(',') if kw.strip()]
            search_page = None
            search_crawler = self._crawler
            try:
                if self._browser_manager and getattr(self._browser_manager, "context", None):
                    search_page = self._browser_manager.create_search_page(force_new=True)
                    from src.douyin_bot.crawler import Crawler
                    search_crawler = Crawler(search_page, self._db)
            except Exception as search_page_e:
                logger.warning(f"[{self.agent_name}] 创建综合搜索标签页失败，回退到当前爬取页搜索: {search_page_e}")
                search_crawler = self._crawler

            discovered_count = 0
            processed_count = 0
            for video_meta in search_crawler.search_keyword_stream(keyword, max_results=max_videos):
                if self._stop_flag:
                    break
                url = video_meta.get("url", "")
                if not url:
                    continue

                discovered_count += 1
                processed_count += 1
                self._current_task = f"Crawling video {processed_count}/{max_videos}"
                self._progress_info["current"] = processed_count
                self._progress_info["detail"] = (
                    f"已发现 {discovered_count}/{max_videos} 个相关视频，"
                    f"正在处理第 {processed_count} 个视频..."
                )
                self._update_status(AgentStatus.BUSY)

                try:
                    crawl_result = self._crawler.crawl_comments(
                        url,
                        target_keywords=target_keywords,
                        comment_time_start=comment_time_start or "",
                        comment_time_end=comment_time_end or "",
                        skip_crawled=bool(skip_crawled),
                        search_keyword=keyword,
                    )
                except Exception as e:
                    crawl_result = {}
                    logger.error(f"[{self.agent_name}] 爬取视频评论失败: {e}")

                if processed_count < max_videos and not self._stop_flag:
                    if crawl_result.get("risk_control_detected"):
                        interval = random.uniform(18, 28)
                        logger.info(
                            f"[{self.agent_name}] 评论区疑似触发风控，延长冷却 {interval:.1f} 秒后处理下一个视频"
                        )
                    elif str(crawl_result.get("termination_reason") or "") == "history_cutoff_reached":
                        interval = random.uniform(4, 8)
                        logger.info(
                            f"[{self.agent_name}] 当前视频仅做增量复查，短暂等待 {interval:.1f} 秒后处理下一个视频"
                        )
                    else:
                        interval = random.uniform(8, 15)
                        logger.info(
                            f"[{self.agent_name}] 视频 {processed_count}/{max_videos} 完成，{interval:.1f}秒后处理下一个"
                        )
                    time.sleep(interval)

            if search_page and search_page != self._crawler_page:
                try:
                    if not search_page.is_closed():
                        search_page.close()
                except Exception:
                    pass
                try:
                    if getattr(self._browser_manager, "search_page", None) == search_page:
                        self._browser_manager.search_page = None
                except Exception:
                    pass

            self._finish_search()

        except Exception as e:
            logger.error(f"[{self.agent_name}] 搜索任务失败: {e}")
            self._finish_search()

    def _finish_search(self):
        """完成搜索任务"""
        processed_count = int((self._progress_info or {}).get("current", 0) or 0)
        logger.info(f"[{self.agent_name}] 搜索任务完成: 处理了 {processed_count} 个视频")
        self._current_task = "Idle"
        self._progress_info = {"total": 0, "current": 0, "detail": ""}
        self._is_searching = False
        self._stop_flag = False
        if self._crawler:
            self._crawler.set_stop_flag(False)
        self._update_status(AgentStatus.RUNNING)

    def _run_send(self, message: str, count: int):
        """运行私信发送任务"""
        from src.common.private_message_limit_service import get_private_message_limit_service

        limit_service = get_private_message_limit_service()
        try:
            self._is_sending = True
            self._stop_flag = False
            self._current_task = "Sending messages"

            pending_customers = self._db.get_pending_customers("douyin", limit=count)

            if not pending_customers:
                logger.info(f"[{self.agent_name}] 没有待发送的客户")
                self._finish_send()
                return

            self._progress_info = {
                "total": len(pending_customers),
                "current": 0,
                "detail": "正在发送私信..."
            }
            self._update_status(AgentStatus.BUSY)

            sent_count = 0
            for i, customer in enumerate(pending_customers):
                if self._stop_flag:
                    break

                try:
                    nickname = customer.get('nickname', '')
                    sec_uid = customer.get('sec_uid', '')

                    self._progress_info["current"] = i + 1
                    self._progress_info["detail"] = f"正在发送给 {nickname}..."

                    account_scope = self._sender.resolve_current_account_scope(force_refresh=(i == 0))
                    account_id = str(account_scope.get("account_id") or "").strip() or "__unresolved_current_login__"
                    reservation_token = ""
                    decision = limit_service.acquire_send_permit(
                        count=1,
                        message=message,
                        user_id=sec_uid,
                        account_id=account_id,
                        source="crawl_agent",
                    )
                    if not decision.allowed:
                        logger.warning(f"[{self.agent_name}] 自动私信发送已停止，原因: {decision.message}")
                        self._progress_info["detail"] = decision.message
                        break

                    reservation_token = str(decision.details.get("reservation_token") or "")
                    success = False
                    try:
                        success = self._sender.send_private_message(sec_uid, message)
                    finally:
                        limit_service.finalize_send_attempt(
                            reservation_token=reservation_token,
                            success=success,
                            count=1,
                            message=message,
                            user_id=sec_uid,
                            account_id=account_id,
                            source="crawl_agent",
                        )
                    if success:
                        sent_count += 1
                        self._db.update_customer_status(sec_uid, "douyin", "sent")

                    interval = random.uniform(5, 10)
                    time.sleep(interval)

                except Exception as e:
                    logger.error(f"[{self.agent_name}] 发送私信失败: {e}")

            logger.info(f"[{self.agent_name}] 私信发送完成: 成功 {sent_count}/{len(pending_customers)}")
            self._finish_send()

        except Exception as e:
            logger.error(f"[{self.agent_name}] 私信发送任务失败: {e}")
            self._finish_send()

    def _finish_send(self):
        """完成发送任务"""
        self._current_task = "Idle"
        self._progress_info = {"total": 0, "current": 0, "detail": ""}
        self._is_sending = False
        self._stop_flag = False
        self._update_status(AgentStatus.RUNNING)

    def _stop_current_task(self):
        """停止当前任务"""
        self._stop_flag = True
        if self._crawler:
            self._crawler.set_stop_flag(True)
        logger.info(f"[{self.agent_name}] 收到停止任务信号")


def run_crawl_agent(command_queue: multiprocessing.Queue,
                    status_dict: dict,
                    shared_config: dict = None):
    """CrawlAgent进程入口函数"""
    from src.agents.base_agent import _ensure_logs_dir
    _ensure_logs_dir()

    agent_name = "crawl-agent"
    logging.basicConfig(
        level=logging.INFO,
        format=f'%(asctime)s | %(levelname)-8s | [{agent_name}] %(name)s:%(funcName)s:%(lineno)d - %(message)s',
        handlers=[
            logging.FileHandler(f'logs/{agent_name}.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

    try:
        agent = CrawlAgent(agent_name, command_queue, status_dict, shared_config)
        agent.run()
    except Exception as e:
        logger.error(f"[{agent_name}] Agent进程异常退出: {e}")
