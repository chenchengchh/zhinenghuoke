"""
SearchCrawlerMixin - 搜索爬取相关方法

从 BotService 中提取的搜索爬取方法集合，包含：
1. 搜索任务预占与释放（_reserve_search_task / _release_search_task）
2. 视频队列优先级与快照（_normalize_video_queue_priority / _get_video_queue_snapshot）
3. 搜索队列摘要更新（_update_search_queue_summary）
4. 视频发现与排序（_extract_video_aweme_id / _build_discovered_video_queue_item / _sort_discovered_video_queue_items / _discover_videos_until_queue_ready）
5. 视频爬取状态解析（_resolve_video_crawl_status）
6. 独立爬取进程快照与命令分发（_get_independent_crawler_snapshot / _dispatch_independent_crawler_command）
7. 搜索/发送任务入口与停止（run_search_task / run_send_task / stop_task）
"""
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from src.common.database import DatabaseManager
from src.config.settings import (
    CRAWLER_QUEUE_PRIORITY_DEFAULT,
    CRAWLER_RESUME_STALE_MINUTES,
    CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
    CRAWLER_WORKER_THREADS_DEFAULT,
)


class SearchCrawlerMixin:
    """搜索爬取 Mixin —— 从 BotService 提取的搜索爬取相关方法。"""

    # 类级别常量（与 BotService 保持一致，Mixin 通过 self 访问 BotService 的类属性）
    # 以下常量在 BotService 中定义，Mixin 通过 self 访问：
    #   VIDEO_QUEUE_PRIORITY_LABELS
    #   COMMENT_TIME_PRESET_LABELS

    def _get_independent_crawler_snapshot(self) -> Dict[str, Any]:
        """读取独立爬取/发送进程状态，兼容旧 BotService 入口转发。"""
        try:
            from src.common.process_manager import get_process_manager

            return (get_process_manager().get_status() or {}).get("crawler", {}) or {}
        except Exception as e:
            logger.debug(f"读取独立爬取/发送进程状态失败: {e}")
            return {}

    def _dispatch_independent_crawler_command(
        self,
        cmd_type: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        success_message: str,
    ) -> Dict[str, Any]:
        """旧 BotService 搜索/发送入口统一转发到独立爬取进程。"""
        try:
            from src.common.process_manager import get_process_manager

            process_manager = get_process_manager()
            if not process_manager.start_crawler_process():
                return {"success": False, "message": "独立爬取/发送进程启动失败"}
            if not process_manager.send_crawler_command("init_browser"):
                return {"success": False, "message": "独立爬取/发送进程浏览器初始化失败"}

            crawler_status = (process_manager.get_status() or {}).get("crawler", {}) or {}
            crawler_task = str(crawler_status.get("current_task") or "Idle")
            crawler_busy = bool(crawler_status.get("busy", False))
            if bool(crawler_status.get("alive", False)) and (crawler_busy or crawler_task != "Idle"):
                return {"success": False, "message": "独立爬取/发送进程正在运行中，请先停止当前任务"}

            if not process_manager.send_crawler_command(cmd_type, data or {}):
                return {"success": False, "message": "独立爬取/发送进程未接收命令"}
            return {"success": True, "message": success_message}
        except Exception as e:
            logger.error(f"转发独立爬取/发送进程命令失败: cmd={cmd_type}, error={e}")
            return {"success": False, "message": f"转发独立爬取/发送进程命令失败: {e}"}

    def stop_task(self):
        """
        停止当前任务

        同时设置 BotService 本地停止标志，并兼容转发独立爬取/发送进程停止信号。
        """
        self._update_runtime_state(stop_flag=True)
        logger.info("Task stop signal received")

        crawler_status = self._get_independent_crawler_snapshot()
        crawler_task = str(crawler_status.get("current_task") or "Idle")
        crawler_busy = bool(crawler_status.get("busy", False))
        if bool(crawler_status.get("alive", False)) and (crawler_busy or crawler_task != "Idle"):
            try:
                from src.common.process_manager import get_process_manager

                if get_process_manager().send_crawler_command("stop_task"):
                    logger.info("Independent crawler stop flag sent")
            except Exception as e:
                logger.debug(f"发送独立爬取/发送停止信号失败: {e}")

        # 同时设置爬虫的停止标志
        if self.crawler:
            self.crawler.set_stop_flag(True)
            logger.info("Crawler stop flag set")

    def run_search_task(
        self,
        keyword: str,
        max_videos: int,
        comment_keywords: str = "",
        auto_reply_enabled: bool = False,
        reply_quota: int | None = None,
        reply_templates: list[str] | None = None,
        auto_comment_enabled: bool = False,
        comment_templates: list[str] | None = None,
        _platform: str = "douyin",
        comment_time_preset: str = "",
        comment_time_start: str = "",
        comment_time_end: str = "",
        skip_crawled: bool = True,
        skip_existing_videos: bool = CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
        crawl_priority: str = CRAWLER_QUEUE_PRIORITY_DEFAULT,
        worker_threads: int = CRAWLER_WORKER_THREADS_DEFAULT,
        lead_quota: int | None = None,
    ):
        """
        运行搜索任务

        Args:
            keyword: 搜索关键词
            max_videos: 每轮搜索优先处理的视频数量；0 表示自动扩展
            comment_keywords: 评论内容关键词，用于精准筛选客户（多个关键词用逗号分隔）
            comment_time_preset: 评论时间快捷范围
            comment_time_start: 评论时间起始范围
            comment_time_end: 评论时间结束范围
            skip_crawled: 是否跳过历史已爬取评论
        """
        logger.warning("BotService.run_search_task 已切换为独立进程兼容入口")
        resolved_lead_quota = max(int(lead_quota or max_videos or 1), 1)
        resolved_max_videos = max(int(max_videos or 0), 0)
        resolved_reply_quota = max(int(reply_quota or 10), 1)
        resolved_reply_templates = [
            str(item or "").strip()
            for item in (reply_templates or comment_templates or [])
            if str(item or "").strip()
        ]
        return self._dispatch_independent_crawler_command(
            "search",
            {
                "keyword": keyword,
                "lead_quota": resolved_lead_quota,
                "max_videos": resolved_max_videos,
                "comment_keywords": comment_keywords,
                "auto_reply_enabled": bool((auto_reply_enabled or auto_comment_enabled) and resolved_reply_templates),
                "reply_quota": resolved_reply_quota if resolved_reply_templates else 0,
                "reply_templates": resolved_reply_templates,
                "auto_comment_enabled": bool((auto_reply_enabled or auto_comment_enabled) and resolved_reply_templates),
                "comment_templates": resolved_reply_templates,
                "platform": _platform,
                "comment_time_preset": comment_time_preset,
                "comment_time_start": comment_time_start,
                "comment_time_end": comment_time_end,
                "skip_crawled": skip_crawled,
                "skip_existing_videos": skip_existing_videos,
                "cleanup_unfinished_videos": True,
                "crawl_priority": crawl_priority,
                "worker_threads": worker_threads,
            },
            success_message="搜索任务已在独立进程中开始",
        )

        search_task_id = self._reserve_search_task(keyword, max_videos)
        if not search_task_id:
            return {"success": False, "message": "搜索任务已在运行或排队中，请稍后重试"}

        def _search_impl(
            search_task_id,
            keyword,
            max_videos,
            comment_keywords,
            platform,
            comment_time_preset,
            comment_time_start,
            comment_time_end,
            skip_crawled,
            skip_existing_videos,
            crawl_priority,
            worker_threads,
        ):
            import random

            effective_comment_time_start = comment_time_start or ""
            effective_comment_time_end = comment_time_end or ""
            preset_days = 0
            preset_key = str(comment_time_preset or "").strip()
            if preset_key:
                try:
                    preset_days = max(int(preset_key), 0)
                except ValueError:
                    preset_days = 0
                if preset_days > 0 and not effective_comment_time_start:
                    effective_comment_time_start = (
                        datetime.now() - timedelta(days=preset_days)
                    ).replace(microsecond=0).isoformat(sep="T")

            try:
                if self.login_handler:
                    self._login_status_cache = self.login_handler.check_login_status()
            except Exception as e:
                logger.warning(f"检查登录状态失败: {e}")

            self._update_runtime_state(current_task=f"Searching: {keyword}", stop_flag=False)
            normalized_priority = self._normalize_video_queue_priority(crawl_priority)
            requested_worker_threads = max(int(worker_threads or 1), 1)
            effective_worker_threads = 1
            if requested_worker_threads > 1:
                logger.warning(
                    f"当前视频评论抓取链基于共享浏览器页串行执行，worker_threads={requested_worker_threads} 已自动降级为 1"
                )

            # 重置爬虫停止标志
            if self.crawler:
                self.crawler.set_stop_flag(False)

            self.progress_info = {
                "total": max_videos,
                "current": 0,
                "detail": "正在搜索视频列表..."
            }
            self.last_search_summary = {
                "task_id": search_task_id,
                "status": "running",
                "platform": platform,
                "keyword": keyword,
                "comment_keywords": [],
                "comment_time_preset_days": preset_days,
                "comment_time_preset_label": self.COMMENT_TIME_PRESET_LABELS.get(
                    preset_key,
                    f"{preset_days}天内" if preset_days else "不限",
                ),
                "comment_time_start": effective_comment_time_start,
                "comment_time_end": effective_comment_time_end,
                "skip_crawled": bool(skip_crawled),
                "skip_existing_videos": bool(skip_existing_videos),
                "videos_discovered": 0,
                "videos_planned": max_videos,
                "videos_processed": 0,
                "videos_inserted": 0,
                "videos_existing": 0,
                "videos_skipped_existing": 0,
                "videos_queued": 0,
                "videos_failed": 0,
                "pending_videos": 0,
                "in_progress_videos": 0,
                "completed_videos": 0,
                "failed_videos": 0,
                "comments_crawled": 0,
                "total_comments": 0,
                "matched_comments": 0,
                "saved_customers": 0,
                "top_level_comments": 0,
                "reply_comments": 0,
                "duplicate_comments": 0,
                "time_filtered_comments": 0,
                "keyword_filtered_comments": 0,
                "history_recorded_comments": 0,
                "risk_control_detected": False,
                "completeness_warnings": [],
                "videos": [],
                "started_at": datetime.now().isoformat(),
            }

            try:
                logger.info(f"Starting search task: {keyword}, comment_keywords: {comment_keywords}")
                if not self.crawler:
                    logger.error("爬虫未初始化，无法执行搜索任务")
                    return
                search_crawler = self.crawler
                search_page = None
                if self.browser_manager and getattr(self.browser_manager, "context", None):
                    try:
                        search_page = self.browser_manager.create_search_page(force_new=True)
                        self._setup_page_close_listener(search_page, "综合搜索标签页", cleanup_on_close=False)
                        search_crawler = Crawler(search_page, self.db)
                    except Exception as search_page_e:
                        logger.warning(f"创建综合搜索标签页失败，回退到当前爬取页搜索: {search_page_e}")
                        search_crawler = self.crawler

                self.progress_info["total"] = max_videos
                self.last_search_summary["videos_discovered"] = 0
                self.last_search_summary["videos_planned"] = max_videos

                # 解析评论关键词
                target_keywords = []
                if comment_keywords and comment_keywords.strip():
                    normalized_keywords = comment_keywords.replace("，", ",")
                    target_keywords = [kw.strip() for kw in normalized_keywords.split(',') if kw.strip()]
                    logger.info(f"评论关键词过滤: {target_keywords}")
                self.last_search_summary["comment_keywords"] = target_keywords

                queue_items = self._discover_videos_until_queue_ready(
                    search_crawler=search_crawler,
                    keyword=keyword,
                    platform=platform,
                    max_videos=max_videos,
                    normalized_priority=normalized_priority,
                    skip_existing_videos=bool(skip_existing_videos),
                )
                logger.info(
                    "视频爬取队列已生成: "
                    f"keyword={keyword}, discovered={self.last_search_summary['videos_discovered']}, "
                    f"inserted={self.last_search_summary['videos_inserted']}, existing={self.last_search_summary['videos_existing']}, "
                    f"completed={self.last_search_summary['completed_videos']}, "
                    f"pending={self.last_search_summary['pending_videos']}, "
                    f"in_progress={self.last_search_summary['in_progress_videos']}, "
                    f"failed={self.last_search_summary['failed_videos']}, "
                    f"queue={len(queue_items)}, skipped_existing={self.last_search_summary['videos_skipped_existing']}, "
                    f"skip_existing_videos={bool(skip_existing_videos)}, "
                    f"priority={normalized_priority}, requested_workers={requested_worker_threads}, effective_workers={effective_worker_threads}"
                )

                count = 0
                for queue_index, video_item in enumerate(queue_items, start=1):
                    if self._get_runtime_state_snapshot()["stop_flag"]:
                        logger.info("收到停止信号，终止评论抓取队列")
                        break

                    aweme_id = str(video_item.get("aweme_id", "") or "").strip()
                    url = str(video_item.get("video_url", "") or "").strip()
                    if not aweme_id or not url:
                        continue

                    self._update_runtime_state(current_task=f"Crawling video {queue_index}/{len(queue_items)}")
                    self.progress_info["total"] = len(queue_items)
                    self.progress_info["current"] = queue_index
                    self.progress_info["detail"] = f"正在抓取第 {queue_index} 个待处理视频评论..."
                    self.db.mark_video_comment_crawl_started(platform, aweme_id)

                    try:
                        crawl_result = self.crawler.crawl_comments(
                            url,
                            target_keywords=target_keywords,
                            comment_time_start=effective_comment_time_start,
                            comment_time_end=effective_comment_time_end,
                            skip_crawled=skip_crawled,
                            video_title=video_item.get("title", ""),
                            author_name=video_item.get("author_name", ""),
                            search_keyword=video_item.get("search_keyword", "") or keyword,
                            search_task_id=search_task_id,
                        )
                        final_status, error_message = self._resolve_video_crawl_status(crawl_result)
                        self.db.finalize_video_comment_crawl(
                            platform=platform,
                            aweme_id=aweme_id,
                            status=final_status,
                            result=crawl_result,
                            error_message=error_message,
                        )
                    except Exception as video_error:
                        crawl_result = {
                            "aweme_id": aweme_id,
                            "total_comments": 0,
                            "matched_comments": 0,
                            "saved_customers": 0,
                            "top_level_comments": 0,
                            "reply_comments": 0,
                            "duplicate_comments": 0,
                            "time_filtered_comments": 0,
                            "keyword_filtered_comments": 0,
                            "history_recorded_comments": 0,
                            "termination_reason": "exception",
                            "risk_control_detected": False,
                            "reached_comment_end": False,
                            "completeness_warning": str(video_error),
                        }
                        self.db.finalize_video_comment_crawl(
                            platform=platform,
                            aweme_id=aweme_id,
                            status=DatabaseManager.VIDEO_STATUS_FAILED,
                            result=crawl_result,
                            error_message=str(video_error),
                        )
                        logger.error(f"视频评论抓取失败 aweme_id={aweme_id}: {video_error}")

                    final_status = str(
                        crawl_result.get("video_status")
                        or self._resolve_video_crawl_status(crawl_result)[0]
                    )
                    self.last_search_summary["videos_processed"] = count + 1
                    self.last_search_summary["comments_crawled"] += int(crawl_result.get("total_comments", 0) or 0)
                    self.last_search_summary["total_comments"] = int(
                        self.last_search_summary.get("comments_crawled", 0) or 0
                    )
                    self.last_search_summary["matched_comments"] += int(crawl_result.get("matched_comments", 0) or 0)
                    self.last_search_summary["saved_customers"] += int(crawl_result.get("saved_customers", 0) or 0)
                    self.last_search_summary["top_level_comments"] += int(crawl_result.get("top_level_comments", 0) or 0)
                    self.last_search_summary["reply_comments"] += int(crawl_result.get("reply_comments", 0) or 0)
                    self.last_search_summary["duplicate_comments"] += int(crawl_result.get("duplicate_comments", 0) or 0)
                    self.last_search_summary["time_filtered_comments"] += int(crawl_result.get("time_filtered_comments", 0) or 0)
                    self.last_search_summary["keyword_filtered_comments"] += int(crawl_result.get("keyword_filtered_comments", 0) or 0)
                    self.last_search_summary["history_recorded_comments"] += int(crawl_result.get("history_recorded_comments", 0) or 0)
                    if final_status == DatabaseManager.VIDEO_STATUS_FAILED:
                        self.last_search_summary["videos_failed"] += 1
                    self.last_search_summary["risk_control_detected"] = bool(
                        self.last_search_summary["risk_control_detected"] or crawl_result.get("risk_control_detected", False)
                    )
                    completeness_warning = str(crawl_result.get("completeness_warning", "") or "").strip()
                    if completeness_warning:
                        self.last_search_summary["completeness_warnings"].append(completeness_warning)
                        self.last_search_summary["completeness_warnings"] = self.last_search_summary["completeness_warnings"][-12:]
                    self.last_search_summary["videos"].append({
                        "url": url,
                        "aweme_id": aweme_id,
                        "title": video_item.get("title", ""),
                        "author": video_item.get("author_name", "") or video_item.get("author", ""),
                        "comment_count": video_item.get("comment_count", 0),
                        "like_count": video_item.get("like_count", 0),
                        "publish_timestamp": video_item.get("publish_timestamp", 0),
                        "comment_crawl_status": final_status,
                        "total_comments": crawl_result.get("total_comments", 0),
                        "top_level_comments": crawl_result.get("top_level_comments", 0),
                        "reply_comments": crawl_result.get("reply_comments", 0),
                        "expected_comment_count": crawl_result.get("expected_comment_count", 0),
                        "matched_comments": crawl_result.get("matched_comments", 0),
                        "saved_customers": crawl_result.get("saved_customers", 0),
                        "duplicate_comments": crawl_result.get("duplicate_comments", 0),
                        "time_filtered_comments": crawl_result.get("time_filtered_comments", 0),
                        "reached_comment_end": crawl_result.get("reached_comment_end", False),
                        "risk_control_detected": crawl_result.get("risk_control_detected", False),
                        "risk_control_reason": crawl_result.get("risk_control_reason", ""),
                        "completeness_warning": crawl_result.get("completeness_warning", ""),
                        "history_cutoff_time": crawl_result.get("history_cutoff_time", ""),
                    })
                    self.last_search_summary["videos"] = self.last_search_summary["videos"][-8:]
                    self.progress_info["detail"] = (
                        f"第 {queue_index} 个视频已完成: 新入库 {crawl_result.get('saved_customers', 0)} 条, "
                        f"跳过历史 {crawl_result.get('duplicate_comments', 0)} 条, 状态 {final_status}"
                    )

                    if queue_index < len(queue_items):
                        if crawl_result.get("risk_control_detected"):
                            interval = random.uniform(18, 28)
                            logger.info(f"评论区疑似触发风控，延长冷却 {interval:.1f} 秒后处理下一个视频...")
                        elif str(crawl_result.get("termination_reason") or "") == "history_cutoff_reached":
                            interval = random.uniform(4, 8)
                            logger.info(f"当前视频仅做增量复查，短暂等待 {interval:.1f} 秒后处理下一个视频...")
                        else:
                            interval = random.uniform(8, 15)  # 8-15秒随机间隔
                            logger.info(f"视频处理完成，等待 {interval:.1f} 秒后处理下一个视频...")
                        chunk_interval = 3
                        elapsed = 0
                        while elapsed < interval:
                            sleep_time = min(chunk_interval, interval - elapsed)
                            time.sleep(sleep_time)
                            elapsed += sleep_time
                            # 处理队列中的排队任务（如启动监听、自动回复等）
                            try:
                                self._process_pending_tasks_during_wait()
                            except Exception as task_e:
                                logger.debug(f"爬取间隔中处理排队任务异常: {task_e}")
                            # 主动触发消息轮询
                            try:
                                self._poll_message_monitor()
                            except Exception as poll_e:
                                logger.debug(f"爬取间隔中消息轮询异常: {poll_e}")

                    count += 1

                final_status_counts = self.db.get_video_comment_status_counts(platform=platform, search_keyword=keyword)
                self.last_search_summary["pending_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_PENDING, 0) or 0)
                self.last_search_summary["in_progress_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_IN_PROGRESS, 0) or 0)
                self.last_search_summary["completed_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_COMPLETED, 0) or 0)
                self.last_search_summary["failed_videos"] = int(final_status_counts.get(DatabaseManager.VIDEO_STATUS_FAILED, 0) or 0)
                self.last_search_summary["status"] = (
                    "stopped" if self._get_runtime_state_snapshot()["stop_flag"] else "completed"
                )
                if search_page and search_page != self.page:
                    try:
                        if not search_page.is_closed():
                            search_page.close()
                    except Exception:
                        pass
                    try:
                        if getattr(self.browser_manager, "search_page", None) == search_page:
                            self.browser_manager.search_page = None
                    except Exception:
                        pass
                self.last_search_summary["finished_at"] = datetime.now().isoformat()
                logger.info(
                    "Search task completed: "
                    f"processed={count}, discovered={self.last_search_summary['videos_discovered']}, "
                    f"inserted={self.last_search_summary['videos_inserted']}, existing={self.last_search_summary['videos_existing']}, "
                    f"skipped_existing={self.last_search_summary['videos_skipped_existing']}, "
                    f"queue={self.last_search_summary['videos_queued']}, failed={self.last_search_summary['videos_failed']}, "
                    f"matched_comments={self.last_search_summary['matched_comments']}, "
                    f"saved_customers={self.last_search_summary['saved_customers']}, "
                    f"duplicate_comments={self.last_search_summary['duplicate_comments']}, "
                    f"time_filtered={self.last_search_summary['time_filtered_comments']}, "
                    f"history_recorded={self.last_search_summary['history_recorded_comments']}, "
                    f"completed={self.last_search_summary['completed_videos']}, "
                    f"pending={self.last_search_summary['pending_videos']}, "
                    f"in_progress={self.last_search_summary['in_progress_videos']}, "
                    f"failed_status={self.last_search_summary['failed_videos']}"
                )
            except Exception as e:
                logger.error(f"Search task failed: {e}")
                if self.last_search_summary is not None:
                    self.last_search_summary["status"] = "failed"
                    self.last_search_summary["error"] = str(e)
                    self.last_search_summary["finished_at"] = datetime.now().isoformat()
            finally:
                self._release_search_task(search_task_id)
                self._update_runtime_state(current_task="Idle", stop_flag=False)
                self.progress_info = {"total": 0, "current": 0, "detail": ""}
                if self.crawler:
                    self.crawler.set_stop_flag(False)

        accepted = self._submit_task_no_wait(
            _search_impl,
            search_task_id,
            keyword,
            max_videos,
            comment_keywords,
            _platform,
            comment_time_preset,
            comment_time_start,
            comment_time_end,
            skip_crawled,
            skip_existing_videos,
            crawl_priority,
            worker_threads,
        )
        if not accepted:
            self._release_search_task(search_task_id)
            return {"success": False, "message": "任务队列已满，请稍后重试"}
        return {"success": True, "message": "搜索任务已开始", "task_id": search_task_id}

    def run_send_task(self, message: str, count: int):
        """运行私信发送任务"""
        logger.warning("BotService.run_send_task 已切换为独立进程兼容入口")
        return self._dispatch_independent_crawler_command(
            "send_messages",
            {
                "message": message,
                "max_count": count,
            },
            success_message="发送任务已在独立进程中开始",
        )

        def _send_impl(message, count):
            from src.common.private_message_limit_service import get_private_message_limit_service

            limit_service = get_private_message_limit_service()
            try:
                if self.login_handler:
                    self._login_status_cache = self.login_handler.check_login_status()
            except Exception as e:
                logger.warning(f"检查登录状态失败: {e}")

            self._update_runtime_state(current_task="Sending messages", stop_flag=False)

            # 获取待发送的客户列表
            pending_customers = self.db.get_pending_customers("douyin", limit=count)

            if not pending_customers:
                logger.info("没有待发送的客户")
                self._update_runtime_state(current_task="Idle")
                return

            self.progress_info = {
                "total": len(pending_customers),
                "current": 0,
                "detail": "准备发送私信..."
            }

            try:
                logger.info(f"Starting send task: {len(pending_customers)} customers")

                sent_count = 0
                for i, customer in enumerate(pending_customers):
                    if self._get_runtime_state_snapshot()["stop_flag"]:
                        logger.info("Send task stopped by user")
                        break

                    self.progress_info["current"] = i + 1
                    self.progress_info["detail"] = f"正在发送给 {customer.get('nickname', '未知')}..."

                    # 发送私信
                    sec_uid = customer.get("sec_uid")
                    nickname = customer.get("nickname", "")

                    if sec_uid and self.sender:
                        account_scope = self.sender.resolve_current_account_scope(force_refresh=(i == 0))
                        account_id = str(account_scope.get("account_id") or "").strip() or "__unresolved_current_login__"
                        reservation_token = ""
                        decision = limit_service.acquire_send_permit(
                            count=1,
                            message=message,
                            user_id=sec_uid,
                            account_id=account_id,
                            source="bot_service",
                        )
                        if not decision.allowed:
                            logger.warning(f"自动私信发送已停止，原因: {decision.message}")
                            self.progress_info["detail"] = decision.message
                            break
                        reservation_token = str(decision.details.get("reservation_token") or "")
                        success = False
                        try:
                            success = self.sender.send_private_message(sec_uid, message)
                        finally:
                            limit_service.finalize_send_attempt(
                                reservation_token=reservation_token,
                                success=success,
                                count=1,
                                message=message,
                                user_id=sec_uid,
                                account_id=account_id,
                                source="bot_service",
                            )
                        if success:
                            self.db.update_customer_status(sec_uid, "douyin", "sent")
                            sent_count += 1
                            logger.info(f"发送成功: {nickname}")
                        else:
                            logger.warning(f"发送失败: {nickname}")

                    # 避免发送过快
                    time.sleep(3)

                logger.info(f"Send task completed: {sent_count}/{len(pending_customers)} sent")
            except Exception as e:
                logger.error(f"Send task failed: {e}")
            finally:
                self._update_runtime_state(current_task="Idle", stop_flag=False)
                self.progress_info = {"total": 0, "current": 0, "detail": ""}

        self._submit_task_no_wait(_send_impl, message, count)
