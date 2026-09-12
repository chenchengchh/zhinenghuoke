"""
process_manager.core - 进程管理核心实现

阶段D·D-2 拆分产物：
原 process_manager.py 中"数据/枚举/IPCMessage"已抽到 models.py，
本模块只保留 ProcessManager 主类、crawler_process_main 入口、模块级
纯辅助函数。

当前只保留爬取/私信进程管理；消息回复已收口到 BotService 单主链，
不再维护独立 monitor_process 作为备用分支。
"""
import json
import os
import random
import re
import time
import traceback
import queue as queue_module
import threading
import multiprocessing as mp
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable
from loguru import logger

from src.common.search_video_normalizer import extract_search_video_aweme_id
from src.common.utils import build_process_scoped_log_file
from src.infrastructure.runtime_paths import get_app_state_dir, get_log_dir

# 阶段D·D-2：从 models 抽出枚举 / IPCMessage，保持向后兼容
from .models import ProcessRole, ProcessState, IPCMessage

_CRAWLER_STALL_RECOVERY_COOLDOWN_SECONDS = max(
    float(os.getenv("CRAWLER_STALL_RECOVERY_COOLDOWN_SECONDS", "15") or 15.0),
    5.0,
)

_SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS = max(
    float(os.getenv("SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS", "15") or 15.0),
    3.0,
)


def _page_is_usable(page) -> bool:
    if not page:
        return False
    try:
        if page.is_closed():
            return False
        _ = page.url
        return True
    except Exception:
        return False


def _browser_context_can_open_page(browser_manager) -> bool:
    """探测浏览器上下文是否还能真实创建新标签页。"""
    if not browser_manager:
        return False

    context = getattr(browser_manager, "context", None)
    if not context:
        return False

    probe_page = None
    try:
        probe_page = context.new_page()
        _ = probe_page.url
        return True
    except Exception:
        return False
    finally:
        if probe_page:
            try:
                if not probe_page.is_closed():
                    probe_page.close()
            except Exception:
                pass


def _crawler_components_ready(browser_manager, page, crawler, sender) -> bool:
    """判断爬取进程复用的浏览器组件是否仍然可用。"""
    if not (browser_manager and page and crawler and sender):
        return False

    if not _page_is_usable(page):
        return False

    browser = getattr(browser_manager, "browser", None)
    if browser and hasattr(browser, "is_connected"):
        try:
            if not browser.is_connected():
                return False
        except Exception:
            return False

    context = getattr(browser_manager, "context", None)
    if not context:
        return False

    try:
        _ = context.pages
    except Exception:
        return False

    if not _browser_context_can_open_page(browser_manager):
        return False

    return True


def _extract_search_video_aweme_id(video_meta) -> str:
    return extract_search_video_aweme_id(video_meta)


def _should_skip_existing_video(skip_existing_videos: bool, status: str) -> bool:
    return bool(skip_existing_videos and str(status or "").strip())


def _resolve_post_video_interval(crawl_result: Dict[str, Any]) -> tuple[float, str]:
    termination_reason = str((crawl_result or {}).get("termination_reason") or "")
    reached_end = bool((crawl_result or {}).get("reached_comment_end"))
    risk_control = bool((crawl_result or {}).get("risk_control_detected"))
    completeness_warning = str((crawl_result or {}).get("completeness_warning") or "")
    reply_expand_clicks = int((crawl_result or {}).get("reply_expand_clicks", 0) or 0)
    reply_comments = int((crawl_result or {}).get("reply_comments", 0) or 0)

    if risk_control:
        interval = random.uniform(18, 28)
        message = f"评论区疑似触发风控，延长冷却 {interval:.1f} 秒后再处理下一个视频"
    elif termination_reason == "history_cutoff_reached":
        interval = random.uniform(4, 8)
        message = f"当前视频仅做增量复查，短暂等待 {interval:.1f} 秒后处理下一个视频"
    elif not reached_end or completeness_warning:
        interval = random.uniform(18, 30)
        message = (
            "当前视频尚未确认完整收尾，延长等待 "
            f"{interval:.1f} 秒后再处理下一个视频"
        )
    elif reply_expand_clicks > 0 or reply_comments > 0:
        interval = random.uniform(12, 20)
        message = f"当前视频包含回复展开采集，等待 {interval:.1f} 秒后再处理下一个视频"
    else:
        interval = random.uniform(8, 15)
        message = f"当前视频已稳定完成，{interval:.1f} 秒后处理下一个视频"

    return interval, message


def _write_crawler_bootstrap_log(message: str) -> None:
    """在 frozen 子进程中追加最小化调试日志，绕过异步日志丢失问题。"""
    try:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "crawler_bootstrap.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | pid={os.getpid()} | {message}\n")
    except Exception:
        pass


# ProcessRole / ProcessState / IPCMessage 已抽到 models.py（见顶部 from .models import ...）


# ==================== 爬取/私信进程入口 ====================

def crawler_process_main(
    command_queue: mp.Queue,
    result_queue: mp.Queue,
    stop_event: mp.Event,
    task_stop_event: mp.Event,
):
    """
    爬取/私信进程主函数
    
    职责：
    1. 视频搜索爬取
    2. 评论数据采集
    3. 自动私信发送
    4. 客户数据入库
    
    Args:
        command_queue: 命令队列 (主进程→爬取进程)
        result_queue: 结果队列 (爬取进程→主进程)
        stop_event: 停止事件
        shared_state: 共享状态
    """
    _write_crawler_bootstrap_log("crawler_process_main entered")

    # 设置进程名
    try:
        import setproctitle
        setproctitle.setproctitle("huoketest-crawler")
    except Exception as e:
        logger.debug(f"设置进程名失败: {e}")
    
    # 初始化日志
    logger.add(
        str(get_log_dir() / build_process_scoped_log_file("crawler_process")),
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        enqueue=True
    )
    
    logger.info("爬取/私信进程启动")
    _write_crawler_bootstrap_log(
        f"logger initialized; log_dir={get_log_dir()}; cwd={os.getcwd()}; "
        f"frozen={getattr(__import__('sys'), 'frozen', False)}"
    )
    
    # 初始化组件
    db = None
    browser_manager = None
    page = None
    crawler = None
    sender = None
    # 阶段C·C-1：命令级超时追踪
    _command_in_flight: str = ""
    _command_started_at: float = 0.0
    _command_in_flight_trace_id: str = ""
    crawler_runtime_state = {
        "last_heartbeat": 0,
        "current_task": "Idle",
        "progress": {"total": 0, "current": 0, "detail": ""},
        "browser_ready": False,
        "initialized": False,
        "busy": False,
    }

    def _emit_state_update() -> None:
        try:
            result_queue.put(
                IPCMessage("state_update", "crawler", dict(crawler_runtime_state)).to_dict()
            )
        except Exception:
            pass
    
    def _update_shared_state(**updates):
        crawler_runtime_state.update(updates)
        _emit_state_update()

    def _set_idle_state(detail: str = ""):
        _update_shared_state(
            current_task="Idle",
            progress={"total": 0, "current": 0, "detail": detail},
            busy=False,
        )

    def _init_components():
        """初始化浏览器和爬取组件"""
        nonlocal db, browser_manager, page, crawler, sender
        _write_crawler_bootstrap_log("init_components start")
        if _crawler_components_ready(browser_manager, page, crawler, sender):
            logger.info("爬取/私信组件已初始化，直接复用")
            _write_crawler_bootstrap_log("init_components reused existing browser components")
            _update_shared_state(
                browser_ready=True,
                initialized=True,
                current_task="Idle",
                progress={"total": 0, "current": 0, "detail": ""},
                busy=False,
            )
            return

        if any(component is not None for component in (browser_manager, page, crawler, sender)):
            logger.warning("检测到爬取/私信组件引用仍在，但浏览器页已失效，准备重建组件")
            _cleanup_components()
        
        from src.common.database import DatabaseManager
        from src.common.crawler_session_state import load_crawler_session_cookies
        from src.douyin_bot.browser_manager import BrowserManager
        from src.douyin_bot.crawler import Crawler
        from src.douyin_bot.message_sender import MessageSender
        from src.config.settings import CRAWLER_USER_DATA_DIR, DOUYIN_HOME_URL
        
        _write_crawler_bootstrap_log(
            f"init_components imported modules; user_data_dir={CRAWLER_USER_DATA_DIR}"
        )
        db = DatabaseManager()
        _write_crawler_bootstrap_log("database manager initialized")
        browser_manager = BrowserManager(user_data_dir=CRAWLER_USER_DATA_DIR)
        _write_crawler_bootstrap_log("browser manager created; calling start()")
        browser_manager.start()
        _write_crawler_bootstrap_log("browser manager start() returned")

        imported_cookie_count = browser_manager.import_cookies(load_crawler_session_cookies())
        if imported_cookie_count > 0:
            _write_crawler_bootstrap_log(
                f"imported crawler session cookies; count={imported_cookie_count}"
            )

        page = browser_manager.create_crawler_page()
        _write_crawler_bootstrap_log(f"crawler page acquired; page_exists={bool(page)}")
        if page and imported_cookie_count > 0:
            try:
                page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as refresh_exc:
                logger.warning(f"crawler 登录态导入后刷新首页失败: {refresh_exc}")
        
        crawler = Crawler(page, db)
        crawler._stop_event = task_stop_event
        sender = MessageSender(page, db)
        
        logger.info("爬取/私信组件初始化完成（使用独立爬取标签页）")
        _write_crawler_bootstrap_log("init_components completed")
        _update_shared_state(
            browser_ready=True,
            initialized=True,
            current_task="Idle",
            progress={"total": 0, "current": 0, "detail": ""},
            busy=False,
        )
    
    def _cleanup_components():
        """清理组件"""
        nonlocal db, browser_manager, page, crawler, sender
        
        try:
            if browser_manager:
                browser_manager.close()
        except Exception as e:
            logger.warning(f"清理爬取组件失败: {e}")
        finally:
            browser_manager = None
            page = None
            crawler = None
            sender = None
            db = None
    
    # 主循环
    while not stop_event.is_set():
        try:
            # 检查命令
            try:
                cmd = command_queue.get_nowait()
                cmd_type = cmd.get("type", "")
                cmd_data = cmd.get("data", {})
                cmd_trace_id = str(cmd.get("trace_id") or "")
                cmd_ack_token = str(cmd.get("ack_token") or "")

                logger.info(f"爬取进程收到命令: {cmd_type} trace_id={cmd_trace_id}")
                _write_crawler_bootstrap_log(
                    f"received command: {cmd_type} trace_id={cmd_trace_id} ack_token={cmd_ack_token[:8]}..."
                )

                # 阶段B·B-3：子进程收到命令立刻 ack，父进程 5s 内能确认命令已被消费。
                if cmd_ack_token:
                    try:
                        _update_shared_state(
                            last_acknowledged={
                                "cmd_type": str(cmd_type),
                                "ack_token": cmd_ack_token,
                                "trace_id": cmd_trace_id,
                                "acked_at": int(time.time()),
                            }
                        )
                    except Exception as ack_exc:
                        logger.debug(f"子进程写 ack 失败: {ack_exc}")

                # 阶段C·C-1：开始计时该命令
                _command_in_flight = str(cmd_type)
                _command_started_at = time.time()
                _command_in_flight_trace_id = cmd_trace_id
                
                if cmd_type == "init_browser":
                    task_stop_event.clear()
                    _init_components()
                    result_queue.put(IPCMessage("browser_ready", "crawler", {
                        "success": browser_manager is not None
                    }).to_dict())
                    _write_crawler_bootstrap_log(
                        f"browser_ready emitted; success={browser_manager is not None}"
                    )
                
                elif cmd_type == "search":
                    task_stop_event.clear()
                    _init_components()
                    if crawler:
                        keyword = cmd_data.get("keyword", "")
                        lead_quota = max(int(cmd_data.get("lead_quota") or cmd_data.get("max_videos") or 10), 1)
                        comment_keywords = cmd_data.get("comment_keywords", [])
                        comment_time_start = cmd_data.get("comment_time_start", "")
                        comment_time_end = cmd_data.get("comment_time_end", "")
                        skip_crawled = bool(cmd_data.get("skip_crawled", True))
                        skip_existing_videos = bool(cmd_data.get("skip_existing_videos", False))
                        if isinstance(comment_keywords, str):
                            normalized = comment_keywords.replace("，", ",")
                            comment_keywords = [kw.strip() for kw in normalized.split(",") if kw.strip()]
                        
                        import datetime
                        started_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        search_summary = {
                            "task_id": f"search_{int(time.time())}",
                            "status": "running",
                            "keyword": keyword,
                            "started_at": started_at_iso,
                            "lead_quota": lead_quota,
                            "max_videos": lead_quota,
                            "saved_customers": 0,
                        }

                        _update_shared_state(
                            current_task=f"Searching: {keyword}",
                            progress={"total": lead_quota, "current": 0, "detail": "正在搜索视频列表并统计入库用户..."},
                            busy=True,
                            last_search_summary=search_summary
                        )
                        search_page = None
                        search_crawler = crawler
                        try:
                            if browser_manager and getattr(browser_manager, "context", None):
                                candidate_search_page = browser_manager.create_search_page(force_new=True)
                                if _page_is_usable(candidate_search_page):
                                    from src.douyin_bot.crawler import Crawler as SearchCrawler
                                    search_page = candidate_search_page
                                    search_crawler = SearchCrawler(search_page, db)
                                else:
                                    logger.warning("综合搜索标签页不可用，回退到当前爬取页搜索")
                        except Exception as search_page_e:
                            logger.warning(f"创建综合搜索标签页失败，回退到当前爬取页搜索: {search_page_e}")
                            search_crawler = crawler

                        discovered_count = 0
                        processed_count = 0
                        saved_customer_count = 0
                        skipped_count = 0
                        seen_aweme_ids = set()
                        search_limit = 0
                        for video_meta in search_crawler.search_keyword_stream(keyword, max_results=search_limit):
                            if stop_event.is_set() or task_stop_event.is_set() or crawler.is_stopped():
                                break

                            if saved_customer_count >= lead_quota:
                                logger.info(f"已达到限额流量 {lead_quota}，停止继续抓取新视频。")
                                break

                            url = video_meta.get("url", "")
                            aweme_id = _extract_search_video_aweme_id(video_meta)
                            if not url or not aweme_id or aweme_id in seen_aweme_ids:
                                continue
                            seen_aweme_ids.add(aweme_id)

                            discovered_count += 1
                            if skip_existing_videos:
                                status = db.get_crawled_video_status("douyin", aweme_id)
                                if _should_skip_existing_video(skip_existing_videos, status):
                                    skipped_count += 1
                                    logger.info(
                                        f"跳过历史已获取视频: {url} "
                                        f"(status={status}, 已跳过: {skipped_count})"
                                    )
                                    _update_shared_state(
                                        current_task=f"Searching: {keyword}",
                                        progress={
                                            "total": lead_quota,
                                            "current": saved_customer_count,
                                            "detail": (
                                                f"已发现 {discovered_count} 个相关视频，"
                                                f"跳过已获取 {skipped_count} 个，"
                                                f"当前已入库用户 {saved_customer_count} / {lead_quota}..."
                                            ),
                                        },
                                        busy=True,
                                    )
                                    continue
                            _update_shared_state(
                                current_task=f"Crawling video {processed_count + 1}",
                                progress={
                                    "total": lead_quota,
                                    "current": saved_customer_count,
                                    "detail": (
                                        f"正在处理第 {processed_count + 1} 个新视频 "
                                        f"(共发现: {discovered_count}, 跳过历史: {skipped_count}, "
                                        f"已入库: {saved_customer_count}/{lead_quota})"
                                    ),
                                },
                                busy=True,
                            )
                            crawl_result = crawler.crawl_comments(
                                url,
                                target_keywords=comment_keywords,
                                comment_time_start=comment_time_start,
                                comment_time_end=comment_time_end,
                                skip_crawled=skip_crawled,
                                search_keyword=keyword,
                                remaining_customer_quota=max(lead_quota - saved_customer_count, 0),
                            )
                            processed_count += 1
                            saved_customer_count += int(crawl_result.get("saved_customers", 0) or 0)
                            result_queue.put(IPCMessage("crawl_progress", "crawler", {
                                "current": saved_customer_count,
                                "total": lead_quota,
                                "video_url": url,
                                "saved_customers": saved_customer_count,
                            }).to_dict())

                            if saved_customer_count >= lead_quota:
                                logger.info(
                                    f"视频抓取后累计入库用户 {saved_customer_count}/{lead_quota}，达到限额流量，结束任务。"
                                )
                                break

                            if not (
                                stop_event.is_set() or task_stop_event.is_set() or crawler.is_stopped()
                            ):
                                interval, interval_message = _resolve_post_video_interval(crawl_result)
                                logger.info(
                                    f"视频 {processed_count} 完成，"
                                    f"termination_reason={crawl_result.get('termination_reason', '')}, "
                                    f"reached_end={bool(crawl_result.get('reached_comment_end'))}, "
                                    f"saved_customers_total={saved_customer_count}/{lead_quota}, "
                                    f"reply_expand_clicks={int(crawl_result.get('reply_expand_clicks', 0) or 0)}, "
                                    f"reply_comments={int(crawl_result.get('reply_comments', 0) or 0)}。"
                                    f"{interval_message}"
                                )
                                elapsed = 0.0
                                while elapsed < interval:
                                    if stop_event.is_set() or task_stop_event.is_set() or crawler.is_stopped():
                                        break
                                    sleep_time = min(2.5, interval - elapsed)
                                    time.sleep(sleep_time)
                                    elapsed += sleep_time

                        final_status = "stopped" if stop_event.is_set() or task_stop_event.is_set() or crawler.is_stopped() else "completed"
                        _set_idle_state("搜索任务已停止" if final_status == "stopped" else "搜索任务完成")
                        result_queue.put(IPCMessage("search_result", "crawler", {
                            "keyword": keyword,
                            "video_count": discovered_count,
                            "processed_count": processed_count,
                            "saved_customers": saved_customer_count,
                            "lead_quota": lead_quota,
                            "status": final_status
                        }).to_dict())

                        if search_page and search_page != page:
                            try:
                                if not search_page.is_closed():
                                    search_page.close()
                            except Exception:
                                pass
                            try:
                                if getattr(browser_manager, "search_page", None) == search_page:
                                    browser_manager.search_page = None
                            except Exception:
                                pass
                    else:
                        result_queue.put(IPCMessage("error", "crawler", {
                            "message": "浏览器未初始化"
                        }).to_dict())
                
                elif cmd_type == "send_messages":
                    task_stop_event.clear()
                    _init_components()
                    if sender:
                        message = cmd_data.get("message", "")
                        messages = cmd_data.get("messages")
                        max_count = cmd_data.get("max_count", 10)
                        pending_customers = db.get_pending_customers("douyin", limit=max_count) if db else []
                        # 阶段C·C-4：total=min(max_count, len(pending_customers))，
                        # 同时在 progress 中暴露 requested_count（用户原始请求量）
                        # 与 processable_count（实际可发送量），前端可清晰对比。
                        processable = min(int(max_count or 0), len(pending_customers))
                        _update_shared_state(
                            current_task="Sending messages",
                            progress={
                                "total": processable,
                                "current": 0,
                                "detail": f"正在发送私信（可发送 {processable} 条，请求 {max_count} 条）",
                                "requested_count": int(max_count or 0),
                                "processable_count": processable,
                            },
                            busy=True,
                        )
                        
                        send_summary = sender.process_pending_customers(
                            message, max_count,
                            stop_check=(lambda: bool(task_stop_event.is_set() or (crawler and crawler.is_stopped()))),
                            message_list=messages
                        )
                        send_summary = send_summary if isinstance(send_summary, dict) else {}
                        send_status = str(
                            send_summary.get("status")
                            or ("stopped" if task_stop_event.is_set() or (crawler and crawler.is_stopped()) else "completed")
                        )
                        idle_detail = "私信发送已停止" if send_status == "stopped" else "私信发送完成"
                        if send_status == "paused_limit":
                            idle_detail = str(send_summary.get("detail") or "私信发送已暂停，等待额度恢复")
                        _set_idle_state(idle_detail)
                        logger.info(
                            "发送子进程结果: "
                            f"status={send_status}, "
                            f"remaining={send_summary.get('remaining_count')}, "
                            f"resume_not_before={send_summary.get('resume_not_before')}"
                        )
                        
                        result_queue.put(IPCMessage("send_result", "crawler", {
                            "status": send_status,
                            "message": message,
                            "messages": list(messages) if isinstance(messages, list) else None,
                            "max_count": int(max_count or 0),
                            **send_summary,
                        }).to_dict())
                    else:
                        result_queue.put(IPCMessage("error", "crawler", {
                            "message": "浏览器未初始化"
                        }).to_dict())
                
                elif cmd_type == "stop_task":
                    task_stop_event.set()
                    progress = dict(
                        crawler_runtime_state.get("progress", {"total": 0, "current": 0, "detail": ""})
                        or {"total": 0, "current": 0, "detail": ""}
                    )
                    progress["detail"] = "正在停止任务..."
                    _update_shared_state(progress=progress, busy=True)
                    result_queue.put(IPCMessage("task_stopped", "crawler", {}).to_dict())
                    # 阶段C·C-1：命令结束
                    _command_in_flight = ""
                    _command_started_at = 0.0
                    _command_in_flight_trace_id = ""

                elif cmd_type == "shutdown":
                    # 阶段C·C-1：命令结束
                    _command_in_flight = ""
                    _command_started_at = 0.0
                    _command_in_flight_trace_id = ""
                    break
                
            except Exception as e:
                if "Empty" not in str(type(e).__name__):
                    logger.debug(f"爬取命令队列读取异常: {e}")

            # 更新共享状态
            _update_shared_state(last_heartbeat=int(time.time()))

            # 阶段C·C-1：命令级超时检测。
            # 当某个命令处理耗时超过 30s（正常 search 抓评论 / send_messages 批量发送会跑较久，
            # 但单条视频或单条私信的子操作都远小于此），且 stop_event 未触发，主动把进度置为
            # 超时态并通知父进程降级。
            if _command_in_flight and int(_command_started_at or 0) > 0:
                elapsed = time.time() - float(_command_started_at)
                if elapsed > 30.0 and not stop_event.is_set():
                    timeout_info = {
                        "cmd_type": _command_in_flight,
                        "elapsed_seconds": round(elapsed, 1),
                        "threshold_seconds": 30.0,
                        "trace_id": _command_in_flight_trace_id,
                    }
                    logger.warning(f"爬取命令执行超过 30s 未完成: {timeout_info}")
                    _write_crawler_bootstrap_log(
                        f"command timeout: cmd={_command_in_flight} elapsed={round(elapsed, 1)}s"
                    )
                    try:
                        _update_shared_state(
                            progress={
                                "total": 0,
                                "current": 0,
                                "detail": f"命令 {_command_in_flight} 执行超时（{round(elapsed, 1)}s）",
                            },
                        )
                    except Exception:
                        pass
                    try:
                        result_queue.put(
                            IPCMessage(
                                "command_timeout",
                                "crawler",
                                timeout_info,
                            ).to_dict()
                        )
                    except Exception:
                        pass

            time.sleep(0.5)

        except (MemoryError, SystemExit, KeyboardInterrupt) as fatal:
            # 阶段A·F-3：致命异常（OOM / 系统退出）— 立刻停止心跳并标记 stalled，
            # 避免前端看到"进程还活着"但实际已经无法恢复。父进程会依据 stalled 标记触发强制重置。
            logger.exception(f"爬取进程致命错误: {fatal}")
            _write_crawler_bootstrap_log(
                f"crawler main loop FATAL: {fatal}\n{traceback.format_exc()}"
            )
            try:
                _update_shared_state(
                    busy=False,
                    stalled=True,
                    current_task="Crashed",
                    progress={"total": 0, "current": 0, "detail": f"致命错误已停止心跳: {type(fatal).__name__}"},
                    last_heartbeat=int(time.time()),
                )
            except Exception:
                pass
            # 留出窗口让父进程读到 stalled 标记，再退出
            stop_event.wait(2)
            return
        except Exception as e:
            logger.error(f"爬取进程错误: {e}")
            _write_crawler_bootstrap_log(
                f"crawler main loop error: {e}\n{traceback.format_exc()}"
            )
            stop_event.wait(1)
    
    _cleanup_components()
    logger.info("爬取/私信进程退出")
    _write_crawler_bootstrap_log("crawler process exit")


class ProcessManager:
    """
    多进程管理器
    
    管理爬取/私信进程的生命周期，
    提供进程间通信和状态监控。
    """
    
    def __init__(self):
        """初始化进程管理器"""
        # 进程对象
        self._crawler_process: Optional[mp.Process] = None
        
        # 进程间通信
        # 阶段A·F-5：命令队列设置上限，防止前端狂点导致队列无限堆积。
        # 上限=20：覆盖合理的多任务缓冲，又能在异常情况下迅速反馈降级。
        self._crawler_cmd_queue = mp.Queue(maxsize=20)
        self._crawler_result_queue = mp.Queue()
        self._crawler_stop_event = mp.Event()
        self._crawler_task_stop_event = mp.Event()
        self._crawler_state_lock = threading.Lock()
        self._crawler_state = {
            "last_heartbeat": 0,
            "last_progress_at": 0,
            "busy_started_at": 0,
            "current_task": "Idle",
            "progress": {"total": 0, "current": 0, "detail": ""},
            "browser_ready": False,
            "initialized": False,
            "busy": False,
            "stalled": False,
            "stall_since": 0,
            "stall_duration_seconds": 0,
            "stall_reason": "",
            "force_reset_recommended": False,
            "last_search_summary": None,
            "scheduled_send_resume": None,
            # 阶段B·B-3：父进程记录最近一次期望 ack 的命令，超时未 ack 时降级返回。
            "expected_ack": {
                "cmd_type": "",
                "ack_token": "",
                "trace_id": "",
                "sent_at": 0,
                "acknowledged": False,
            },
            "last_acknowledged": {
                "cmd_type": "",
                "ack_token": "",
                "trace_id": "",
                "acked_at": 0,
            },
        }
        self._last_stall_recovery_attempt_at = 0.0
        self._scheduled_send_resume: Optional[Dict[str, Any]] = None
        self._scheduled_send_resume_lock = threading.Lock()
        self._scheduled_search_resume: Optional[Dict[str, Any]] = self._load_scheduled_search_resume()
        self._scheduled_search_resume_lock = threading.Lock()
        self._active_search_command_payload: Optional[Dict[str, Any]] = (
            dict(self._scheduled_search_resume.get("payload") or {})
            if isinstance(self._scheduled_search_resume, dict)
            else None
        )

        # 阶段B·B-3：ACK 等待相关状态
        self._ack_timeout_seconds = 5.0
        self._last_ack_check_at = 0.0
        
        # 状态
        self._crawler_status = ProcessState.STOPPED
        
        # 结果处理线程
        self._result_thread = None
        self._result_thread_running = False
        
        # 消息回调
        self._on_progress_callback: Optional[Callable] = None
        
        logger.info("进程管理器初始化完成")
    
    def set_callbacks(
        self,
        on_new_message: Callable = None,
        on_progress: Callable = None
    ):
        """
        设置回调函数
        
        Args:
            on_progress: 进度更新回调
        """
        self._on_progress_callback = on_progress

    def _update_crawler_state(self, **updates) -> None:
        """统一更新共享状态，避免 manager dict 局部键遗漏。"""
        with self._crawler_state_lock:
            snapshot = dict(self._crawler_state)
            now = int(time.time())
            progress_changed = "progress" in updates and dict(snapshot.get("progress") or {}) != dict(updates.get("progress") or {})
            task_changed = "current_task" in updates and str(snapshot.get("current_task") or "") != str(updates.get("current_task") or "")
            busy_changed = "busy" in updates and bool(snapshot.get("busy", False)) != bool(updates.get("busy", False))
            snapshot.update(updates)
            if progress_changed or task_changed or busy_changed:
                snapshot["last_progress_at"] = now
            if bool(snapshot.get("busy", False)):
                if not int(snapshot.get("busy_started_at", 0) or 0) or (busy_changed and bool(snapshot.get("busy", False))):
                    snapshot["busy_started_at"] = now
            else:
                snapshot["busy_started_at"] = 0
            self._crawler_state = snapshot

    def _get_crawler_state_snapshot(self) -> Dict[str, Any]:
        with self._crawler_state_lock:
            return dict(self._crawler_state)

    # ==================== 阶段B·B-3：ACK 机制 ====================

    def _record_expected_ack(self, cmd_type: str, ack_token: str, trace_id: str = "") -> None:
        """
        记录父进程期望子进程 ack 的命令。

        该信息会写进 shared_state 中，前端通过 `get_status` 接口可以读到；
        同时启动一个后台 watcher 线程，5s 内未 ack 则告警。
        """
        self._update_crawler_state(
            expected_ack={
                "cmd_type": str(cmd_type or ""),
                "ack_token": str(ack_token or ""),
                "trace_id": str(trace_id or ""),
                "sent_at": int(time.time()),
                "acknowledged": False,
            }
        )
        _write_crawler_bootstrap_log(
            f"parent expects ack: cmd={cmd_type} token={ack_token[:8]}..."
        )

    def mark_command_acknowledged(
        self,
        cmd_type: str,
        ack_token: str,
        trace_id: str = "",
    ) -> bool:
        """
        阶段B·B-3：子进程在接到命令后立即调用，标记 ack。

        Returns:
            是否匹配到当前期望 ack 记录。
        """
        ack_token = str(ack_token or "").strip()
        if not ack_token:
            return False
        with self._crawler_state_lock:
            current = dict(self._crawler_state.get("expected_ack") or {})
            if not current or str(current.get("ack_token") or "") != ack_token:
                return False
            self._crawler_state = dict(self._crawler_state)
            self._crawler_state["expected_ack"] = {
                **current,
                "acknowledged": True,
                "acked_at": int(time.time()),
            }
            self._crawler_state["last_acknowledged"] = {
                "cmd_type": str(cmd_type or current.get("cmd_type") or ""),
                "ack_token": ack_token,
                "trace_id": str(trace_id or current.get("trace_id") or ""),
                "acked_at": int(time.time()),
            }
        _write_crawler_bootstrap_log(
            f"child ack received: cmd={cmd_type} token={ack_token[:8]}..."
        )
        return True

    def check_pending_ack_timeout(self) -> Optional[Dict[str, Any]]:
        """
        阶段B·B-3：检查最近一次期望 ack 是否超时。

        距 `sent_at` 超过 `self._ack_timeout_seconds`（默认 5s）且未 ack 时返回超时信息。
        """
        now = int(time.time())
        if now - int(self._last_ack_check_at or 0) < 1:
            return None
        self._last_ack_check_at = now
        with self._crawler_state_lock:
            current = dict(self._crawler_state.get("expected_ack") or {})
        if not current or current.get("acknowledged"):
            return None
        if not current.get("ack_token"):
            return None
        elapsed = now - int(current.get("sent_at") or 0)
        if elapsed < int(self._ack_timeout_seconds):
            return None
        info = {
            "cmd_type": current.get("cmd_type", ""),
            "ack_token": current.get("ack_token", ""),
            "trace_id": current.get("trace_id", ""),
            "elapsed_seconds": int(elapsed),
            "timeout_seconds": int(self._ack_timeout_seconds),
        }
        _write_crawler_bootstrap_log(
            f"parent ack timeout: cmd={info['cmd_type']} token={info['ack_token'][:8]}... elapsed={elapsed}s"
        )
        return info

    def _reset_crawler_runtime_state(
        self,
        *,
        detail: str = "",
        browser_ready: bool = False,
        initialized: bool = False,
        last_heartbeat: int = 0,
    ) -> None:
        """在停止/崩溃/强制重置后统一收口共享状态。"""
        with self._scheduled_send_resume_lock:
            self._scheduled_send_resume = None
        self._update_crawler_state(
            last_heartbeat=last_heartbeat,
            last_progress_at=0,
            busy_started_at=0,
            current_task="Idle",
            progress={"total": 0, "current": 0, "detail": detail},
            browser_ready=browser_ready,
            initialized=initialized,
            busy=False,
            stalled=False,
            stall_since=0,
            stall_duration_seconds=0,
            stall_reason="",
            force_reset_recommended=False,
            scheduled_send_resume=None,
        )

    def _recover_stalled_crawler_if_needed(self) -> bool:
        if not self._crawler_process or not self._crawler_process.is_alive():
            return False

        crawler_state = self._get_crawler_state_snapshot()
        if not (
            bool(crawler_state.get("busy", False))
            and bool(crawler_state.get("stalled", False))
            and bool(crawler_state.get("force_reset_recommended", False))
        ):
            return False

        now = time.time()
        if (now - float(self._last_stall_recovery_attempt_at or 0.0)) < _CRAWLER_STALL_RECOVERY_COOLDOWN_SECONDS:
            return False
        self._last_stall_recovery_attempt_at = now

        stall_reason = str(crawler_state.get("stall_reason") or "").strip() or "检测到独立爬取进程长时间无进度更新"
        logger.warning(f"{stall_reason}，开始自动强制恢复独立爬取进程")
        return self.stop_crawler_process(
            join_timeout=2.0,
            terminate_timeout=2.0,
            reset_detail=f"{stall_reason}，已自动强制恢复",
        )

    def _set_scheduled_send_resume(self, plan: Optional[Dict[str, Any]]) -> None:
        normalized_plan = dict(plan or {}) if isinstance(plan, dict) else None
        with self._scheduled_send_resume_lock:
            self._scheduled_send_resume = normalized_plan
        self._update_crawler_state(scheduled_send_resume=normalized_plan)

    def _clear_scheduled_send_resume(self) -> None:
        self._set_scheduled_send_resume(None)

    @staticmethod
    def _parse_resume_at_epoch(resume_at: str) -> Optional[float]:
        text = str(resume_at or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _allow_scheduled_send_resume_without_manual_trigger() -> bool:
        """统一约束私信额度恢复后是否允许在没有新人工指令时自动续发。"""
        return False

    def _schedule_send_resume(self, result_data: Dict[str, Any]) -> None:
        remaining_count = max(int((result_data or {}).get("remaining_count") or 0), 0)
        if remaining_count <= 0:
            self._clear_scheduled_send_resume()
            return

        resume_at = str((result_data or {}).get("resume_not_before") or "").strip()
        retry_after_seconds = max(int((result_data or {}).get("retry_after_seconds") or 0), 0)
        resume_epoch = self._parse_resume_at_epoch(resume_at)
        if resume_epoch is None:
            resume_epoch = time.time() + max(retry_after_seconds, 1)
            resume_at = datetime.fromtimestamp(resume_epoch).isoformat()

        message = str((result_data or {}).get("message") or "")
        raw_messages = (result_data or {}).get("messages")
        messages = [str(item) for item in raw_messages if str(item or "").strip()] if isinstance(raw_messages, list) else None
        payload = {
            "message": message,
            "messages": messages,
            "max_count": remaining_count,
            "_auto_resume": True,
        }
        plan = {
            "resume_at": resume_at,
            "resume_epoch": float(resume_epoch),
            "retry_after_seconds": retry_after_seconds,
            "remaining_count": remaining_count,
            "limit_reason": str((result_data or {}).get("limit_reason") or ""),
            "detail": str((result_data or {}).get("detail") or ""),
            "payload": payload,
            "scheduled_at": datetime.now().isoformat(),
            "auto_resume_enabled": self._allow_scheduled_send_resume_without_manual_trigger(),
            "manual_resume_required": not self._allow_scheduled_send_resume_without_manual_trigger(),
        }
        if plan["auto_resume_enabled"]:
            logger.info(
                f"私信发送已加入自动续发计划: remaining={remaining_count}, "
                f"resume_at={resume_at}, reason={plan['limit_reason'] or 'quota_wait'}"
            )
        else:
            logger.warning(
                f"私信发送已记录剩余计划但不会自动续发: remaining={remaining_count}, "
                f"resume_at={resume_at}, reason={plan['limit_reason'] or 'quota_wait'}"
            )
        self._set_scheduled_send_resume(plan)
        crawler_state = self._get_crawler_state_snapshot()
        if (
            not bool(crawler_state.get("busy", False))
            and str(crawler_state.get("current_task") or "Idle") == "Idle"
        ):
            progress = dict(crawler_state.get("progress") or {"total": 0, "current": 0, "detail": ""})
            if plan["auto_resume_enabled"]:
                progress["detail"] = f"私信额度已用尽，预计 {resume_at} 自动继续发送剩余 {remaining_count} 条"
            else:
                progress["detail"] = f"私信额度已用尽，剩余 {remaining_count} 条待人工继续；最早恢复时间 {resume_at}"
            self._update_crawler_state(progress=progress)

    def _maybe_resume_scheduled_send(self) -> None:
        with self._scheduled_send_resume_lock:
            plan = dict(self._scheduled_send_resume or {})
        if not plan:
            return
        if not bool(plan.get("auto_resume_enabled", False)):
            return

        resume_epoch = float(plan.get("resume_epoch") or 0)
        if resume_epoch > time.time():
            return

        crawler_state = self._get_crawler_state_snapshot()
        if bool(crawler_state.get("busy", False)) or str(crawler_state.get("current_task") or "Idle") != "Idle":
            return

        payload = dict(plan.get("payload") or {})
        if not payload:
            self._clear_scheduled_send_resume()
            return

        logger.info(
            f"私信额度等待已到期，开始自动恢复剩余发送: remaining={plan.get('remaining_count')}, "
            f"scheduled_at={plan.get('scheduled_at')}, resume_at={plan.get('resume_at')}"
        )
        if self.send_crawler_command("send_messages", payload):
            self._clear_scheduled_send_resume()
            return

        retry_epoch = time.time() + 5
        plan["resume_epoch"] = retry_epoch
        plan["resume_at"] = datetime.fromtimestamp(retry_epoch).isoformat()
        with self._scheduled_send_resume_lock:
            self._scheduled_send_resume = plan
        self._update_crawler_state(scheduled_send_resume=plan)

    @staticmethod
    def _normalize_search_command_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = dict(payload or {}) if isinstance(payload, dict) else {}
        normalized.pop("_resume_state", None)
        normalized.pop("_auto_resume_search", None)
        return normalized

    def _load_scheduled_search_resume(self) -> Optional[Dict[str, Any]]:
        plan = self._read_crawler_pid_record().get("search_resume")
        return dict(plan) if isinstance(plan, dict) else None

    def _persist_scheduled_search_resume(self, plan: Optional[Dict[str, Any]]) -> None:
        path = self._get_crawler_pid_record_path()
        record = self._read_crawler_pid_record()
        if plan and isinstance(plan, dict):
            record["search_resume"] = dict(plan)
        else:
            record.pop("search_resume", None)

        if not record:
            try:
                path.unlink(missing_ok=True)
            except (OSError, PermissionError) as e:
                logger.debug(f"清理搜索恢复记录失败: {e}")
            return

        try:
            path.write_text(json.dumps(record, ensure_ascii=True), encoding="utf-8")
        except (OSError, TypeError, ValueError) as e:
            logger.debug(f"写入搜索恢复记录失败: {e}")

    def _set_scheduled_search_resume(self, plan: Optional[Dict[str, Any]]) -> None:
        normalized_plan = dict(plan or {}) if isinstance(plan, dict) else None
        with self._scheduled_search_resume_lock:
            self._scheduled_search_resume = normalized_plan
        self._persist_scheduled_search_resume(normalized_plan)

    def _clear_scheduled_search_resume(self) -> None:
        self._set_scheduled_search_resume(None)

    def _sync_search_resume_from_runtime(self, state_update: Dict[str, Any]) -> None:
        summary = state_update.get("last_search_summary")
        resume_state = state_update.get("search_resume_state")
        if not isinstance(summary, dict):
            return

        status = str(summary.get("status") or "").strip()
        if status and status != "running":
            self._active_search_command_payload = None
            self._clear_scheduled_search_resume()
            return

        if not isinstance(resume_state, dict):
            return

        payload = dict(self._active_search_command_payload or {})
        if not payload:
            existing = self._load_scheduled_search_resume()
            if isinstance(existing, dict):
                payload = dict(existing.get("payload") or {})
        if not payload:
            return

        existing_plan = self._load_scheduled_search_resume()
        plan = {
            "task_id": str(summary.get("task_id") or ""),
            "keyword": str(summary.get("keyword") or payload.get("keyword") or ""),
            "payload": payload,
            "resume_state": dict(resume_state),
            "updated_at": datetime.now().isoformat(),
            "dispatch_in_progress": False,
            "last_dispatch_at": float(existing_plan.get("last_dispatch_at", 0.0) or 0.0)
            if isinstance(existing_plan, dict)
            else 0.0,
            "dispatch_attempt_count": int(existing_plan.get("dispatch_attempt_count", 0) or 0)
            if isinstance(existing_plan, dict)
            else 0,
        }
        self._set_scheduled_search_resume(plan)

    def _maybe_resume_scheduled_search(self) -> None:
        with self._scheduled_search_resume_lock:
            plan = dict(self._scheduled_search_resume or {})
        if not plan:
            return

        if not self.is_crawler_running():
            return

        crawler_state = self._get_crawler_state_snapshot()
        if bool(crawler_state.get("busy", False)) or str(crawler_state.get("current_task") or "Idle") != "Idle":
            return

        payload = dict(plan.get("payload") or {})
        resume_state = dict(plan.get("resume_state") or {})
        if not payload or not resume_state:
            self._clear_scheduled_search_resume()
            return

        dispatch_in_progress = bool(plan.get("dispatch_in_progress", False))
        last_dispatch_at = float(plan.get("last_dispatch_at", 0.0) or 0.0)
        now = time.time()
        if dispatch_in_progress and (now - last_dispatch_at) < _SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS:
            return
        if dispatch_in_progress:
            logger.warning(
                f"待恢复搜索任务在 {_SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS:.0f} 秒内未进入运行态，准备补发续跑命令: "
                f"keyword={plan.get('keyword') or payload.get('keyword')}, last_dispatch_at={last_dispatch_at:.3f}"
            )

        resume_preview = f"{resume_state.get('effective_count', 0)}/{resume_state.get('target_effective_count', 0) or payload.get('lead_quota', 0)}"
        logger.info(
            f"检测到待恢复搜索任务，开始自动续跑: keyword={plan.get('keyword') or payload.get('keyword')}, "
            f"effective={resume_preview}, pending={len(resume_state.get('pending_entries') or [])}"
        )
        resume_payload = dict(payload)
        resume_payload["_auto_resume_search"] = True
        resume_payload["_resume_state"] = resume_state
        sent = self.send_crawler_command("search", resume_payload)
        plan["last_dispatch_at"] = now
        plan["dispatch_attempt_count"] = int(plan.get("dispatch_attempt_count", 0) or 0) + 1
        plan["last_dispatch_status"] = "sent" if sent else "send_failed"
        plan["last_dispatch_attempt_at"] = datetime.now().isoformat()
        plan["dispatch_in_progress"] = bool(sent)
        self._set_scheduled_search_resume(plan)

    def _clear_queue(self, ipc_queue: Optional[mp.Queue]) -> None:
        if ipc_queue is None:
            return

        while True:
            try:
                ipc_queue.get_nowait()
            except queue_module.Empty:
                break
            except Exception:
                break

    @staticmethod
    def _safe_process_pid(process: Optional[mp.Process]) -> int:
        if process is None:
            return 0
        try:
            return int(getattr(process, "pid", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _get_crawler_pid_record_path(self):
        try:
            get_app_state_dir().mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return get_app_state_dir() / "crawler_process.json"

    def _read_crawler_pid_record(self) -> Dict[str, Any]:
        path = self._get_crawler_pid_record_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as e:
            # 阶段C·C-5：文件读取/JSON 解析分别捕获
            logger.debug(f"读取 crawler PID 记录失败: {e}")
            return {}

    def _write_crawler_pid_record(self, pid: int) -> None:
        path = self._get_crawler_pid_record_path()
        existing = self._read_crawler_pid_record()
        payload = {
            "pid": int(pid),
            "owner_pid": int(os.getpid()),
            "recorded_at": int(time.time()),
        }
        if isinstance(existing.get("search_resume"), dict):
            payload["search_resume"] = dict(existing.get("search_resume") or {})
        try:
            path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        except Exception as e:
            logger.debug(f"写入 crawler PID 记录失败: {e}")

    def _clear_crawler_pid_record(self, pid: Optional[int] = None) -> None:
        path = self._get_crawler_pid_record_path()
        if not path.exists():
            return
        record = self._read_crawler_pid_record()
        if pid is not None:
            if int(record.get("pid", 0) or 0) not in {0, int(pid)}:
                return
        preserved_resume = dict(record.get("search_resume") or {}) if isinstance(record.get("search_resume"), dict) else None
        if preserved_resume:
            try:
                path.write_text(json.dumps({"search_resume": preserved_resume}, ensure_ascii=True), encoding="utf-8")
                return
            except (OSError, TypeError, ValueError) as e:
                logger.debug(f"保留搜索恢复记录失败: {e}")
        try:
            path.unlink(missing_ok=True)
        except (OSError, PermissionError) as e:
            logger.debug(f"删除 crawler PID 记录失败: {e}")

    def _terminate_recorded_crawler_pid(self, pid: int, *, reason: str) -> bool:
        try:
            import psutil
        except Exception as e:
            logger.warning(f"无法导入 psutil，跳过跨实例 crawler 清理: {e}")
            return False

        try:
            process = psutil.Process(int(pid))
        except psutil.NoSuchProcess:
            self._clear_crawler_pid_record(pid=pid)
            return True
        except Exception as e:
            logger.warning(f"读取残留 crawler 进程失败: pid={pid}, error={e}")
            return False

        try:
            logger.warning(f"{reason}，准备终止记录中的 crawler 进程 pid={pid}")
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            self._clear_crawler_pid_record(pid=pid)
            return True
        except psutil.NoSuchProcess:
            self._clear_crawler_pid_record(pid=pid)
            return True
        except Exception as e:
            logger.warning(f"终止残留 crawler 进程失败: pid={pid}, error={e}")
            return False

    def ensure_crawler_process_stopped_for_restart(self, stale_heartbeat_seconds: float = 8.0) -> Dict[str, Any]:
        """在新任务启动前清理卡死/残留的独立爬取进程。"""
        recorded_pid = int(self._read_crawler_pid_record().get("pid", 0) or 0)
        current_pid = self._safe_process_pid(self._crawler_process)
        if recorded_pid and recorded_pid != current_pid:
            cleaned = self._terminate_recorded_crawler_pid(
                recorded_pid,
                reason="检测到跨实例遗留的 crawler PID 记录",
            )
            if not cleaned:
                return {"cleaned": False, "reason": "检测到跨实例遗留的 crawler 进程"}

        status = self.get_status()["crawler"]
        if not status.get("alive", False):
            return {"cleaned": False, "reason": ""}

        last_heartbeat = float(status.get("last_heartbeat", 0) or 0)
        heartbeat_stale = (
            last_heartbeat > 0
            and (time.time() - last_heartbeat) > max(float(stale_heartbeat_seconds or 0), 1.0)
        )
        stop_requested = self._crawler_stop_event.is_set() or self._crawler_task_stop_event.is_set()
        current_task = str(status.get("current_task") or "Idle")
        busy = bool(status.get("busy", False))
        stalled = bool(status.get("stalled", False))
        force_reset_recommended = bool(status.get("force_reset_recommended", False))

        if not stop_requested and not (heartbeat_stale and (busy or current_task != "Idle")) and not (stalled and force_reset_recommended):
            return {"cleaned": False, "reason": ""}

        reason_parts = []
        if stop_requested:
            reason_parts.append("检测到旧停止信号残留")
        if heartbeat_stale and (busy or current_task != "Idle"):
            reason_parts.append("检测到独立爬取进程心跳超时")
        if stalled and force_reset_recommended:
            reason_parts.append(str(status.get("stall_reason") or "检测到独立爬取进程疑似卡住"))
        reason = "，".join(reason_parts) or "检测到独立爬取进程残留"
        logger.warning(f"{reason}，启动前先强制回收旧进程")

        cleaned = self.stop_crawler_process(join_timeout=2.0, terminate_timeout=2.0, reset_detail=f"{reason}，已自动回收")
        return {"cleaned": cleaned, "reason": reason}
    
    def start_crawler_process(self) -> bool:
        """
        启动爬取/私信进程
        
        Returns:
            是否启动成功
        """
        if self._crawler_process and self._crawler_process.is_alive():
            logger.warning("爬取进程已在运行")
            _write_crawler_bootstrap_log("parent start_crawler_process skipped: already alive")
            return True
        
        try:
            from src.common.crawler_worker_entry import crawler_process_entry

            self._clear_queue(self._crawler_cmd_queue)
            self._clear_queue(self._crawler_result_queue)
            self._crawler_stop_event.clear()
            self._crawler_task_stop_event.clear()
            self._crawler_status = ProcessState.STARTING
            self._reset_crawler_runtime_state()
            
            self._crawler_process = mp.Process(
                target=crawler_process_entry,
                args=(
                    self._crawler_cmd_queue,
                    self._crawler_result_queue,
                    self._crawler_stop_event,
                    self._crawler_task_stop_event,
                ),
                name="crawler-process",
                daemon=False
            )
            self._crawler_process.start()
            self._crawler_status = ProcessState.RUNNING
            self._write_crawler_pid_record(self._crawler_process.pid)
            logger.info(f"爬取进程已启动, PID: {self._crawler_process.pid}")
            _write_crawler_bootstrap_log(
                f"parent started crawler process; pid={self._crawler_process.pid}"
            )

            self._start_result_thread()

            logger.info(f"爬取进程已启动, PID={self._crawler_process.pid}")
            return True

        except (OSError, ValueError, TypeError, AttributeError) as e:
            # 阶段C·C-5：进程启动相关已知异常分类
            failed_pid = self._safe_process_pid(self._crawler_process)
            logger.error(f"启动爬取进程失败: {e}\n{traceback.format_exc()}")
            _write_crawler_bootstrap_log(
                f"parent failed to start crawler process; pid={failed_pid}; "
                f"error={e}\n{traceback.format_exc()}"
            )
            self._crawler_status = ProcessState.ERROR
            self._crawler_process = None
            self._reset_crawler_runtime_state(detail="独立爬取/发送进程启动失败")
            if failed_pid:
                self._clear_crawler_pid_record(pid=failed_pid)
            else:
                self._clear_crawler_pid_record()
            return False
    
    def stop_crawler_process(self, join_timeout: float = 10.0, terminate_timeout: float = 5.0, reset_detail: str = "") -> bool:
        """停止爬取/私信进程"""
        if not self._crawler_process or not self._crawler_process.is_alive():
            stopped_pid = self._safe_process_pid(self._crawler_process)
            self._crawler_status = ProcessState.STOPPED
            self._crawler_process = None
            self._reset_crawler_runtime_state(detail=reset_detail)
            if stopped_pid:
                self._clear_crawler_pid_record(pid=stopped_pid)
            else:
                self._clear_crawler_pid_record()
            return True
        
        try:
            self._crawler_status = ProcessState.STOPPING
            self._crawler_stop_event.set()
            self._crawler_task_stop_event.set()
            self._crawler_cmd_queue.put({"type": "shutdown"})
            
            self._crawler_process.join(timeout=max(join_timeout, 0.1))
            
            if self._crawler_process.is_alive():
                self._crawler_process.terminate()
                self._crawler_process.join(timeout=max(terminate_timeout, 0.1))

            if self._crawler_process.is_alive():
                logger.error("爬取进程在 terminate 后仍未退出")
                self._crawler_status = ProcessState.ERROR
                return False
            
            self._crawler_status = ProcessState.STOPPED
            stopped_pid = self._crawler_process.pid
            self._crawler_process = None
            self._reset_crawler_runtime_state(detail=reset_detail)
            self._clear_queue(self._crawler_cmd_queue)
            self._clear_queue(self._crawler_result_queue)
            self._clear_crawler_pid_record(pid=stopped_pid)
            logger.info("爬取进程已停止")
            return True
            
        except Exception as e:
            logger.error(f"停止爬取进程失败: {e}")
            self._crawler_status = ProcessState.ERROR
            return False
    
    def stop_all(self):
        """停止所有进程"""
        self.stop_crawler_process()
        logger.info("所有进程已停止")

    def shutdown_for_exit(self):
        """应用退出时的兜底清理。

        与普通运行态的 ``stop_all()`` 区分开，除了停止独立爬取进程外，
        还会停止结果线程并显式关闭 ``multiprocessing.Manager``，尽量避免
        Windows ``spawn`` 子进程在主进程退出后残留为孤儿进程。
        """
        try:
            self.stop_all()
        except Exception as e:
            logger.warning(f"应用退出时停止独立进程失败: {e}")

        try:
            self._stop_result_thread()
        except Exception as e:
            logger.warning(f"应用退出时停止结果线程失败: {e}")

        try:
            manager = getattr(self, "_manager", None)
            if manager is not None:
                manager.shutdown()
                logger.info("进程管理器 Manager 已关闭")
        except Exception as e:
            logger.warning(f"应用退出时关闭 Manager 失败: {e}")

        try:
            for ipc_queue in (self._crawler_cmd_queue, self._crawler_result_queue):
                try:
                    ipc_queue.close()
                except Exception:
                    pass
                try:
                    ipc_queue.join_thread()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"应用退出时关闭 IPC 队列失败: {e}")

    def stop_crawler_task(
        self,
        graceful_timeout: float = 3.0,
        *,
        force_reset_on_timeout: bool = False,
    ) -> Dict[str, Any]:
        """优先优雅停止当前任务，必要时再由调用方决定是否强制重置独立爬取进程。"""
        if not self._crawler_process or not self._crawler_process.is_alive():
            self._crawler_status = ProcessState.STOPPED
            self._reset_crawler_runtime_state()
            return {"success": True, "forced": False, "message": "独立爬取/发送进程未运行"}

        if not self.send_crawler_command("stop_task"):
            return {"success": False, "forced": False, "message": "独立爬取/发送进程停止失败"}

        deadline = time.monotonic() + max(float(graceful_timeout or 0), 0.2)
        while time.monotonic() < deadline:
            status = self.get_status()["crawler"]
            if not status.get("alive", False):
                self._reset_crawler_runtime_state()
                return {"success": True, "forced": False, "message": "独立爬取/发送进程已停止"}
            if (
                not bool(status.get("busy", False))
                and str(status.get("current_task") or "Idle") == "Idle"
            ):
                return {"success": True, "forced": False, "message": "独立爬取/发送任务已停止"}
            time.sleep(0.1)

        if not force_reset_on_timeout:
            logger.warning("独立爬取/发送任务停止超时，保留浏览器会话并继续等待当前步骤结束")
            return {
                "success": True,
                "forced": False,
                "pending": True,
                "message": "停止请求已发送，等待当前步骤安全结束",
            }

        logger.warning("独立爬取/发送任务停止超时，开始强制重置进程")
        forced = self.stop_crawler_process(
            join_timeout=2.0,
            terminate_timeout=2.0,
            reset_detail="独立爬取/发送进程已因停止超时而强制重置",
        )
        return {
            "success": forced,
            "forced": forced,
            "pending": False,
            "message": "独立爬取/发送进程已强制重置" if forced else "独立爬取/发送进程强制重置失败",
        }
    
    # ==================== 命令发送 ====================
    
    def send_crawler_command(self, cmd_type: str, data: Dict = None) -> bool:
        """
        发送命令到爬取进程

        Args:
            cmd_type: 命令类型
            data: 命令数据

        Returns:
            是否发送成功
        """
        if not self._crawler_process or not self._crawler_process.is_alive():
            logger.warning("爬取进程未运行")
            _write_crawler_bootstrap_log(
                f"parent send command aborted: process not alive; cmd={cmd_type}"
            )
            return False

        # 阶段A·F-2：除 alive 外还校验 browser_ready/initialized/current_task，
        # 防止在子进程"活着但浏览器崩溃/Idle 尚未刷新"时把命令塞进队列被空消费。
        # stop_task/init_browser 例外：前者用于恢复，后者用于重新初始化。
        if cmd_type not in {"stop_task", "init_browser"}:
            state_snapshot = self._get_crawler_state_snapshot() or {}
            if not bool(state_snapshot.get("browser_ready", False)):
                logger.warning(
                    f"拒绝向爬取进程发送 {cmd_type}：浏览器尚未就绪 (browser_ready=False)"
                )
                _write_crawler_bootstrap_log(
                    f"parent send command rejected: browser not ready; cmd={cmd_type}"
                )
                return False
            if not bool(state_snapshot.get("initialized", False)):
                logger.warning(
                    f"拒绝向爬取进程发送 {cmd_type}：爬取进程尚未完成初始化 (initialized=False)"
                )
                _write_crawler_bootstrap_log(
                    f"parent send command rejected: not initialized; cmd={cmd_type}"
                )
                return False
            current_task = str(state_snapshot.get("current_task") or "Idle")
            if current_task != "Idle":
                logger.warning(
                    f"拒绝向爬取进程发送 {cmd_type}：当前任务未结束 (current_task={current_task})"
                )
                _write_crawler_bootstrap_log(
                    f"parent send command rejected: busy; cmd={cmd_type} current_task={current_task}"
                )
                return False

        try:
            payload = data or {}
            if cmd_type == "send_messages" and bool(payload.get("_auto_resume", False)):
                logger.warning("拒绝自动续发私信命令进入爬取进程；请等待新的人工发送指令")
                return False

            # 在命令入队时先写入共享状态，避免前端/停止路由在子进程消费前误判为 Idle。
            if cmd_type == "init_browser":
                self._crawler_task_stop_event.clear()
                self._update_crawler_state(
                    current_task="Initializing browser",
                    progress={"total": 0, "current": 0, "detail": "正在初始化独立浏览器..."},
                    browser_ready=False,
                    initialized=False,
                    busy=True,
                )
            elif cmd_type == "search":
                self._crawler_task_stop_event.clear()
                is_auto_resume_search = bool(payload.get("_auto_resume_search", False))
                resume_state = dict(payload.get("_resume_state") or {}) if isinstance(payload.get("_resume_state"), dict) else {}
                normalized_search_payload = self._normalize_search_command_payload(payload)
                self._active_search_command_payload = normalized_search_payload or None
                if not is_auto_resume_search:
                    self._clear_scheduled_search_resume()
                keyword = str(payload.get("keyword", "") or "").strip()
                display_keyword = keyword or "未命名关键词"
                lead_quota = max(int(payload.get("lead_quota") or payload.get("max_videos") or 10), 1)
                current_progress = max(
                    int(resume_state.get("effective_count") or resume_state.get("saved_customers") or 0),
                    0,
                )
                self._update_crawler_state(
                    current_task=f"Searching: {display_keyword}",
                    progress={
                        "total": lead_quota,
                        "current": current_progress,
                        "detail": (
                            "正在恢复上次搜索任务..."
                            if is_auto_resume_search
                            else "正在启动搜索任务..."
                        ),
                    },
                    busy=True,
                )
            elif cmd_type == "interact":
                self._crawler_task_stop_event.clear()
                uids = payload.get("uids", [])
                self._update_crawler_state(
                    current_task=f"Interacting: {len(uids)} users",
                    progress={
                        "total": len(uids),
                        "current": 0,
                        "detail": "正在启动一键互动任务...",
                    },
                    busy=True,
                )
            elif cmd_type == "send_messages":
                self._crawler_task_stop_event.clear()
                if not bool(payload.get("_auto_resume", False)):
                    self._clear_scheduled_send_resume()
                requested_count = max(int(payload.get("max_count", 10) or 10), 0)
                # 阶段C·C-4：进度条 total 仍用 max_count（用户请求量），
                # 进度 detail 中同时暴露 requested_count，便于前端区分
                # "用户请求多少" 与 "实际可处理多少"。
                self._update_crawler_state(
                    current_task="Sending messages",
                    progress={
                        "total": requested_count,
                        "current": 0,
                        "detail": f"正在发送私信（请求 {requested_count} 条）",
                        "requested_count": requested_count,
                    },
                    busy=True,
                )
            elif cmd_type == "stop_task":
                self._crawler_task_stop_event.set()
                self._clear_scheduled_send_resume()
                progress = dict(
                    self._get_crawler_state_snapshot().get("progress", {"total": 0, "current": 0, "detail": ""})
                    or {"total": 0, "current": 0, "detail": ""}
                )
                progress["detail"] = "正在停止任务..."
                self._update_crawler_state(progress=progress, busy=True)
                return True

            try:
                # 阶段A·F-5：用 put_nowait 配合 maxsize，队列满时立刻降级返回 503，
                # 而非 put 阻塞导致父进程被拖死。
                ipc_message = {
                    "type": cmd_type,
                    "data": payload,
                    # 阶段B·B-1：trace_id 贯穿到子进程，子进程消费时把它写入 shared_state
                    # 与 startup_trace，便于跨进程关联"前端→API→IPC→子进程"全链路。
                    "trace_id": str(payload.get("_trace_id") or payload.get("trace_id") or ""),
                    # 阶段B·B-3：ACK 令牌，用于父进程 5s 内确认子进程已消费命令
                    "ack_token": str(payload.get("_ack_token") or ""),
                    "enqueued_at": int(time.time()),
                }
                self._crawler_cmd_queue.put_nowait(ipc_message)
                # 父进程把期望的 ack 写到 shared_state，等待子进程 ack
                if ipc_message["ack_token"]:
                    self._record_expected_ack(cmd_type, ipc_message["ack_token"], ipc_message["trace_id"])
            except queue_module.Full:
                logger.warning(f"爬取命令队列已满，丢弃 {cmd_type}")
                _write_crawler_bootstrap_log(
                    f"parent enqueue dropped (queue full): {cmd_type}"
                )
                # 释放本方法预先写入的"忙碌"状态（避免前端卡死）
                if cmd_type in {"init_browser", "search", "send_messages", "interact"}:
                    try:
                        self._update_crawler_state(busy=False)
                    except Exception:
                        pass
                return False
            _write_crawler_bootstrap_log(
                f"parent enqueued command: {cmd_type}; payload_keys={sorted(list(payload.keys()))}"
            )
            return True
        except (OSError, BrokenPipeError, ConnectionError) as e:
            # 子进程已死但 is_alive() 竞态未感知——给调用方一个明确错误。
            logger.error(f"爬取进程 IPC 连接已断开: {e}")
            _write_crawler_bootstrap_log(
                f"parent IPC broken: {cmd_type}; error={e}"
            )
            return False
        except Exception as e:
            logger.error(f"发送爬取命令失败: {e}")
            _write_crawler_bootstrap_log(
                f"parent failed to enqueue command: {cmd_type}; error={e}"
            )
            return False
    
    _result_thread_lock = threading.Lock()

    def _start_result_thread(self):
        """启动结果处理线程（线程安全）"""
        with self._result_thread_lock:
            if self._result_thread and self._result_thread.is_alive():
                return

            self._result_thread_running = True
            self._result_thread = threading.Thread(
                target=self._result_loop,
                daemon=True
            )
            self._result_thread.start()
    
    def _stop_result_thread(self):
        """停止结果处理线程"""
        self._result_thread_running = False
        if self._result_thread:
            self._result_thread.join(timeout=5)
    
    def _result_loop(self):
        """结果处理循环"""
        import queue as queue_module
        while self._result_thread_running:
            try:
                try:
                    result = self._crawler_result_queue.get_nowait()
                    self._handle_crawler_result(result)
                except queue_module.Empty:
                    pass
                except Exception as e:
                    logger.error(f"处理爬取结果异常: {e}")

                try:
                    self._maybe_resume_scheduled_send()
                except Exception as e:
                    logger.warning(f"检查私信自动续发计划失败: {e}")

                try:
                    self._maybe_resume_scheduled_search()
                except Exception as e:
                    logger.warning(f"检查搜索自动续跑计划失败: {e}")
                
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"结果处理错误: {e}")
                time.sleep(0.5)
    
    def _handle_crawler_result(self, result: Dict):
        """处理爬取进程结果"""
        msg = IPCMessage.from_dict(result)
        
        if msg.msg_type == "search_result":
            logger.info(
                f"搜索完成: 关键词={msg.data.get('keyword')}, 发现视频={msg.data.get('video_count')}, "
                f"处理视频={msg.data.get('processed_count')}, 状态={msg.data.get('status')}"
            )
            self._active_search_command_payload = None
            self._clear_scheduled_search_resume()
        
        elif msg.msg_type == "crawl_progress":
            if self._on_progress_callback:
                self._on_progress_callback(msg.data)

        elif msg.msg_type == "state_update":
            payload = dict(msg.data or {})
            self._update_crawler_state(**payload)
            self._sync_search_resume_from_runtime(payload)
        
        elif msg.msg_type == "send_result":
            send_status = str(msg.data.get("status") or "completed")
            logger.info(
                "主进程收到 send_result: "
                f"status={send_status}, "
                f"remaining={msg.data.get('remaining_count')}, "
                f"resume_not_before={msg.data.get('resume_not_before')}"
            )
            if send_status == "paused_limit":
                self._schedule_send_resume(dict(msg.data or {}))
                logger.info("私信发送已暂停，保留剩余发送计划但不再自动续发")
            else:
                self._clear_scheduled_send_resume()
                logger.info("私信发送完成")
        
        elif msg.msg_type == "error":
            logger.error(f"爬取进程错误: {msg.data.get('message')}")
    
    def get_status(self) -> Dict:
        """获取进程管理器状态（含崩溃检测）"""
        crawler_alive = bool(self._crawler_process and self._crawler_process.is_alive())

        if crawler_alive:
            self._recover_stalled_crawler_if_needed()
            crawler_alive = bool(self._crawler_process and self._crawler_process.is_alive())

        if not crawler_alive:
            if self._crawler_status == ProcessState.RUNNING:
                logger.warning("爬取进程已崩溃，状态已更新")
                _write_crawler_bootstrap_log("parent detected crawler process not alive in get_status")
            self._crawler_status = ProcessState.STOPPED
            crawler_state = self._get_crawler_state_snapshot()
            if (
                bool(crawler_state.get("busy", False))
                or str(crawler_state.get("current_task", "Idle") or "Idle") != "Idle"
                or bool(crawler_state.get("browser_ready", False))
                or bool(crawler_state.get("initialized", False))
            ):
                self._reset_crawler_runtime_state()

        crawler_state = self._get_crawler_state_snapshot()
        return {
            "crawler": {
                "status": self._crawler_status.value,
                "pid": self._crawler_process.pid if self._crawler_process else None,
                "alive": crawler_alive,
                "last_heartbeat": int(crawler_state.get("last_heartbeat", 0) or 0),
                "last_progress_at": int(crawler_state.get("last_progress_at", 0) or 0),
                "busy_started_at": int(crawler_state.get("busy_started_at", 0) or 0),
                "current_task": str(crawler_state.get("current_task", "Idle") or "Idle"),
                "progress": dict(crawler_state.get("progress", {"total": 0, "current": 0, "detail": ""}) or {"total": 0, "current": 0, "detail": ""}),
                "browser_ready": bool(crawler_state.get("browser_ready", False)),
                "initialized": bool(crawler_state.get("initialized", False)),
                "busy": bool(crawler_state.get("busy", False)),
                "stalled": bool(crawler_state.get("stalled", False)),
                "stall_since": int(crawler_state.get("stall_since", 0) or 0),
                "stall_duration_seconds": int(crawler_state.get("stall_duration_seconds", 0) or 0),
                "stall_reason": str(crawler_state.get("stall_reason", "") or ""),
                "force_reset_recommended": bool(crawler_state.get("force_reset_recommended", False)),
                "last_search_summary": dict(crawler_state.get("last_search_summary") or {}) if isinstance(crawler_state.get("last_search_summary"), dict) else crawler_state.get("last_search_summary"),
                "scheduled_send_resume": dict(crawler_state.get("scheduled_send_resume") or {}) if isinstance(crawler_state.get("scheduled_send_resume"), dict) else crawler_state.get("scheduled_send_resume"),
                "current_login_account_id": str(crawler_state.get("current_login_account_id") or ""),
                "current_login_account_source": str(crawler_state.get("current_login_account_source") or ""),
                "current_login_account_resolved": bool(crawler_state.get("current_login_account_resolved", False)),
                "expected_login_account_id": str(crawler_state.get("expected_login_account_id") or ""),
                "expected_login_account_source": str(crawler_state.get("expected_login_account_source") or ""),
                "expected_login_account_resolved": bool(crawler_state.get("expected_login_account_resolved", False)),
                "login_transfer_matched": bool(crawler_state.get("login_transfer_matched", False)),
            }
        }
    
    def is_crawler_running(self) -> bool:
        """爬取进程是否运行中"""
        return bool(self._crawler_process and self._crawler_process.is_alive())


# ==================== 单例管理 ====================

_process_manager_instance = None
_process_manager_lock = threading.Lock()


def get_process_manager() -> ProcessManager:
    """获取进程管理器单例"""
    global _process_manager_instance
    with _process_manager_lock:
        if _process_manager_instance is None:
            _process_manager_instance = ProcessManager()
        return _process_manager_instance


def shutdown_process_manager_for_exit() -> None:
    """仅在应用退出时调用的单例清理入口。"""
    global _process_manager_instance
    with _process_manager_lock:
        instance = _process_manager_instance
        if instance is None:
            return
        try:
            instance.shutdown_for_exit()
        finally:
            _process_manager_instance = None
