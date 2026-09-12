import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.settings import (  # noqa: E402
    CRAWLER_QUEUE_PRIORITY_DEFAULT,
    CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
    CRAWLER_WORKER_THREADS_DEFAULT,
)
from src.web.bot_service import get_bot_service  # noqa: E402


VALID_PRIORITIES = [
    "latest_unprocessed",
    "publish_desc",
    "hot_desc",
    "discovered_desc",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行视频搜索与评论爬取任务，支持视频去重、增量队列和断点续爬。"
    )
    parser.add_argument("keyword", help="视频搜索关键词")
    parser.add_argument("--lead-quota", type=int, default=None, help="目标入库用户数量，默认兼容回退到 --max-videos")
    parser.add_argument("--max-videos", type=int, default=5, help="每轮优先处理的视频数量，0 表示自动扩展并持续补抓")
    parser.add_argument("--comment-keywords", default="", help="评论关键词，多个用英文逗号分隔")
    parser.add_argument("--platform", default="douyin", help="平台标识，默认 douyin")
    parser.add_argument("--comment-time-preset", default="", help="评论时间预设天数，如 1/7/30")
    parser.add_argument("--comment-time-start", default="", help="评论时间起始，ISO 格式")
    parser.add_argument("--comment-time-end", default="", help="评论时间结束，ISO 格式")
    parser.add_argument(
        "--crawl-priority",
        default=CRAWLER_QUEUE_PRIORITY_DEFAULT,
        choices=VALID_PRIORITIES,
        help="未处理视频队列优先级",
    )
    parser.add_argument(
        "--worker-threads",
        type=int,
        default=CRAWLER_WORKER_THREADS_DEFAULT,
        help="请求的并发线程数，当前执行层会自动降级为安全值",
    )
    parser.add_argument(
        "--wait-login-seconds",
        type=int,
        default=180,
        help="等待浏览器启动并扫码登录的最长秒数",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="轮询任务状态的间隔秒数",
    )
    parser.add_argument(
        "--stop-browser-on-exit",
        action="store_true",
        help="任务结束后自动关闭浏览器",
    )
    parser.add_argument(
        "--skip-crawled",
        dest="skip_crawled",
        action="store_true",
        default=True,
        help="跳过历史已爬取评论（默认开启）",
    )
    parser.add_argument(
        "--no-skip-crawled",
        dest="skip_crawled",
        action="store_false",
        help="关闭评论级历史去重",
    )
    parser.add_argument(
        "--skip-existing-videos",
        dest="skip_existing_videos",
        action="store_true",
        default=CRAWLER_SKIP_EXISTING_VIDEOS_DEFAULT,
        help="跳过数据库中已完成评论抓取的视频",
    )
    parser.add_argument(
        "--no-skip-existing-videos",
        dest="skip_existing_videos",
        action="store_false",
        help="允许重新处理数据库中已完成的视频",
    )
    return parser


def wait_until_ready(bot_service, timeout_seconds: int) -> bool:
    deadline = time.time() + max(int(timeout_seconds or 0), 1)
    boot_logged = False
    while time.time() < deadline:
        status = bot_service.get_status()
        is_running = bool(status.get("is_running"))
        browser_state = status.get("browser_state") or "unknown"
        if is_running and bot_service.check_login():
            return True
        if not boot_logged:
            print(f"[crawler] 浏览器状态: {browser_state}，请完成扫码登录...", flush=True)
            boot_logged = True
        time.sleep(2)
    return False


def print_summary(summary: dict) -> None:
    print("[crawler] 任务摘要:", flush=True)
    print(json.dumps(summary or {}, ensure_ascii=False, indent=2), flush=True)


def run() -> int:
    args = build_parser().parse_args()
    bot_service = get_bot_service()

    try:
        status = bot_service.get_status()
        if not status.get("is_running"):
            print("[crawler] 正在启动浏览器...", flush=True)
            bot_service.start_browser()

        ready = wait_until_ready(bot_service, args.wait_login_seconds)
        if not ready:
            print("[crawler] 浏览器未就绪或登录超时，请确认已扫码登录。", flush=True)
            return 2

        current_status = bot_service.get_status()
        if current_status.get("current_task") != "Idle":
            print(f"[crawler] 当前已有运行中的任务: {current_status.get('current_task')}", flush=True)
            return 3

        result = bot_service.run_search_task(
            keyword=args.keyword,
            max_videos=max(int(args.max_videos or 5), 0),
            comment_keywords=args.comment_keywords,
            _platform=args.platform,
            comment_time_preset=args.comment_time_preset,
            comment_time_start=args.comment_time_start,
            comment_time_end=args.comment_time_end,
            skip_crawled=bool(args.skip_crawled),
            skip_existing_videos=bool(args.skip_existing_videos),
            crawl_priority=args.crawl_priority,
            worker_threads=max(int(args.worker_threads or 1), 1),
            lead_quota=max(int(args.lead_quota or args.max_videos or 5), 1),
        )
        if not result.get("success"):
            print(f"[crawler] 启动失败: {result.get('message', '未知错误')}", flush=True)
            return 4

        task_id = result.get("task_id", "")
        print(f"[crawler] 搜索任务已启动，task_id={task_id}", flush=True)

        last_progress = None
        last_status = None
        poll_interval = max(float(args.poll_interval or 1.0), 0.5)
        while True:
            status = bot_service.get_status()
            summary = status.get("last_search_summary") or {}
            progress = status.get("progress") or {}
            summary_task_id = summary.get("task_id", "")
            summary_status = summary.get("status", "")
            progress_text = (
                f"{progress.get('current', 0)}/{progress.get('total', 0)}"
                f" {progress.get('detail', '')}"
            ).strip()

            if summary_task_id == task_id:
                if progress_text and progress_text != last_progress:
                    print(f"[crawler] 进度: {progress_text}", flush=True)
                    last_progress = progress_text
                if summary_status and summary_status != last_status:
                    print(f"[crawler] 状态: {summary_status}", flush=True)
                    last_status = summary_status
                if summary_status in {"completed", "stopped", "failed"}:
                    print_summary(summary)
                    return 0 if summary_status == "completed" else 5

            time.sleep(poll_interval)
    finally:
        if getattr(args, "stop_browser_on_exit", False):
            try:
                print("[crawler] 正在关闭浏览器...", flush=True)
                bot_service.stop_browser()
            except Exception as exc:
                print(f"[crawler] 关闭浏览器失败: {exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(run())
