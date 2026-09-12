import os
import random
import threading
import time
import traceback
from pathlib import Path

from loguru import logger

from src.common.utils import build_process_scoped_log_file
from src.common.search_video_normalizer import (
    build_canonical_search_video_url,
    extract_search_video_aweme_id,
    extract_search_video_detail_kind,
    normalize_search_video_meta,
)

_MAX_EMPTY_SEARCH_PASSES_BEFORE_STOP = max(
    int(os.getenv("MAX_EMPTY_SEARCH_PASSES_BEFORE_STOP", "4") or 4),
    1,
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


def _can_use_as_independent_search_page(candidate_page, crawler_page=None) -> bool:
    if not _page_is_usable(candidate_page):
        return False
    if crawler_page and candidate_page is crawler_page:
        return False
    return True


def _browser_context_can_open_page(browser_manager) -> bool:
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

    return _browser_context_can_open_page(browser_manager)


def _extract_search_video_aweme_id(video_meta) -> str:
    return extract_search_video_aweme_id(video_meta)


def _extract_search_video_detail_kind(video_meta) -> str:
    return extract_search_video_detail_kind(video_meta)


def _build_canonical_search_video_url(video_meta, aweme_id: str) -> str:
    return build_canonical_search_video_url(video_meta, aweme_id)


def _normalize_search_video_meta(video_meta):
    return normalize_search_video_meta(video_meta)


def _should_skip_existing_video(skip_existing_videos: bool, status: str) -> bool:
    return bool(skip_existing_videos and str(status or "").strip())


def _should_enqueue_search_video_candidate(skip_existing_videos: bool, status: str) -> bool:
    return not _should_skip_existing_video(skip_existing_videos, status)


def _should_collect_reply_comments_for_search(auto_reply_enabled: bool, remaining_reply_quota: int) -> bool:
    """永久收口为仅抓取一级评论，搜索链不再采集二级评论/回复。"""
    return False


def _resolve_video_crawl_status(crawl_result) -> tuple[str, str]:
    termination_reason = str((crawl_result or {}).get("termination_reason") or "").strip()
    risk_reason = str((crawl_result or {}).get("risk_control_reason") or "").strip()
    completeness_warning = str((crawl_result or {}).get("completeness_warning") or "").strip()
    skipped_reasons = {
        "stop_requested",
        "page_popup_interrupted",
        "redirected_to_other_video",
        "video_unavailable",
    }
    if termination_reason in skipped_reasons:
        return "skipped", termination_reason
    if termination_reason == "lead_quota_reached":
        return "completed", ""
    if termination_reason in {"top_level_comment_end_reached", "comment_end_reached"}:
        return "completed", ""
    if bool((crawl_result or {}).get("risk_control_detected")):
        return "failed", risk_reason or completeness_warning or termination_reason or "risk_control_detected"
    if str((crawl_result or {}).get("completion_status") or "").strip().startswith("completed_"):
        return "completed", ""
    if bool((crawl_result or {}).get("reached_comment_end")):
        return "completed", ""
    return "failed", completeness_warning or termination_reason or "crawl_not_completed"


def _record_video_discovery_for_crawl(db, platform: str, search_keyword: str, video_meta, aweme_id: str) -> None:
    if not db or not aweme_id:
        return
    db.upsert_crawled_videos(platform, [video_meta], search_keyword=search_keyword)
    db.mark_video_comment_crawl_started(platform, aweme_id)


def _finalize_video_discovery_crawl(db, platform: str, aweme_id: str, crawl_result, error_message: str) -> None:
    if not db or not aweme_id:
        return
    final_status, resolved_error_message = _resolve_video_crawl_status(crawl_result)
    db.finalize_video_comment_crawl(
        platform=platform,
        aweme_id=aweme_id,
        status=final_status,
        result=crawl_result,
        error_message=error_message or resolved_error_message,
    )


def _resolve_post_video_interval(crawl_result):
    termination_reason = str((crawl_result or {}).get("termination_reason") or "")
    reached_end = bool((crawl_result or {}).get("reached_comment_end"))
    risk_control = bool((crawl_result or {}).get("risk_control_detected"))
    completeness_warning = str((crawl_result or {}).get("completeness_warning") or "")
    if risk_control:
        interval = random.uniform(18, 28)
        message = f"评论区疑似触发风控，延长冷却 {interval:.1f} 秒后再处理下一个视频"
    elif termination_reason == "history_cutoff_reached":
        interval = random.uniform(4, 8)
        message = f"当前视频仅做增量复查，短暂等待 {interval:.1f} 秒后处理下一个视频"
    elif not reached_end or completeness_warning:
        interval = random.uniform(18, 30)
        message = f"当前视频尚未确认完整收尾，延长等待 {interval:.1f} 秒后再处理下一个视频"
    else:
        interval = random.uniform(8, 15)
        message = f"当前视频已稳定完成，{interval:.1f} 秒后处理下一个视频"

    return interval, message


def _cleanup_search_unfinished_videos(db, platform: str, keyword: str, *, phase: str) -> dict:
    if not db or not str(keyword or "").strip():
        return {"deleted_count": 0, "deleted_aweme_ids": [], "deleted_status_counts": {}}
    result = db.cleanup_unfinished_crawled_videos(
        platform=platform or "douyin",
        search_keyword=str(keyword or "").strip(),
    )
    deleted_count = int(result.get("deleted_count", 0) or 0)
    if deleted_count > 0:
        logger.info(
            "搜索任务未完成视频队列已清理: "
            f"phase={phase}, keyword={keyword}, deleted_count={deleted_count}, "
            f"status_counts={dict(result.get('deleted_status_counts') or {})}"
        )
    return result


def _is_search_stop_requested(stop_event, task_stop_event, crawler) -> bool:
    return bool(
        stop_event.is_set()
        or task_stop_event.is_set()
        or (crawler and crawler.is_stopped())
    )


def _resolve_search_command_limits(cmd_data) -> tuple[int, int]:
    command = dict(cmd_data or {})
    try:
        requested_video_limit = max(int(command.get("max_videos") or 0), 0)
    except (TypeError, ValueError):
        requested_video_limit = 0

    lead_quota_raw = command.get("lead_quota")
    if lead_quota_raw in (None, ""):
        lead_quota_raw = requested_video_limit or 10
    try:
        lead_quota = max(int(lead_quota_raw or 10), 1)
    except (TypeError, ValueError):
        lead_quota = 10
    return lead_quota, requested_video_limit


def _resolve_search_pass_video_limit(lead_quota: int, saved_customer_count: int, requested_video_limit: int) -> int:
    remaining_quota = max(int(lead_quota or 0) - int(saved_customer_count or 0), 0)
    if remaining_quota <= 0:
        return 0
    return max(int(requested_video_limit or 0), 0)


def _resolve_search_reply_quota(cmd_data) -> int:
    command = dict(cmd_data or {})
    raw_value = command.get("reply_quota")
    if raw_value in (None, ""):
        raw_value = 10
    try:
        return max(int(raw_value or 10), 1)
    except (TypeError, ValueError):
        return 10


def _merge_search_auto_reply_result(search_summary: dict, auto_reply_result: dict) -> dict:
    merged_summary = dict(search_summary or {})
    result = dict(auto_reply_result or {})

    merged_summary["auto_reply_attempted"] = int(
        merged_summary.get("auto_reply_attempted", 0) or 0
    ) + int(result.get("attempted", 0) or 0)
    merged_summary["auto_reply_success"] = int(
        merged_summary.get("auto_reply_success", 0) or 0
    ) + int(result.get("success", 0) or 0)
    merged_summary["auto_reply_failed"] = int(
        merged_summary.get("auto_reply_failed", 0) or 0
    ) + int(result.get("failed", 0) or 0)

    aggregated_reasons = dict(merged_summary.get("auto_reply_failure_reasons") or {})
    for reason, count in dict(result.get("failure_reasons") or {}).items():
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            continue
        aggregated_reasons[normalized_reason] = int(
            aggregated_reasons.get(normalized_reason, 0) or 0
        ) + int(count or 0)
    merged_summary["auto_reply_failure_reasons"] = aggregated_reasons

    merged_samples = list(merged_summary.get("auto_reply_failure_samples") or [])
    for sample in list(result.get("failure_samples") or []):
        if len(merged_samples) >= 5:
            break
        merged_samples.append(sample)
    merged_summary["auto_reply_failure_samples"] = merged_samples[:5]
    aggregated_diagnostics = dict(merged_summary.get("auto_reply_diagnostics") or {})
    result_diagnostics = dict(result.get("diagnostics") or {})
    for key, value in result_diagnostics.items():
        if isinstance(value, (int, float)):
            aggregated_diagnostics[key] = int(aggregated_diagnostics.get(key, 0) or 0) + int(value or 0)
    merged_summary["auto_reply_diagnostics"] = aggregated_diagnostics

    if result.get("limit_reached_reason"):
        merged_summary["auto_reply_limit_reached"] = True
    if result.get("quota_exhausted"):
        merged_summary["auto_reply_quota_exhausted"] = True
    if "remaining_reply_quota" in result:
        merged_summary["auto_reply_remaining_quota"] = max(
            int(result.get("remaining_reply_quota", 0) or 0),
            0,
        )
    return merged_summary


def _resolve_search_retry_interval(
    empty_search_passes: int,
    *,
    skip_existing_videos: bool = False,
) -> tuple[float, str]:
    empty_rounds = max(int(empty_search_passes or 0), 1)
    if skip_existing_videos:
        if empty_rounds <= 1:
            interval = random.uniform(1.2, 2.0)
        elif empty_rounds <= 3:
            interval = random.uniform(2.0, 3.2)
        else:
            interval = random.uniform(3.2, 4.8)
    elif empty_rounds <= 1:
        interval = random.uniform(1.6, 2.6)
    elif empty_rounds <= 3:
        interval = random.uniform(2.6, 3.8)
    else:
        interval = random.uniform(3.8, 5.2)
    message = (
        f"当前仍未达到目标用户数，暂未发现新的可处理视频，"
        f"{interval:.1f} 秒后继续自动搜索补抓"
    )
    return interval, message


def _should_stop_search_due_to_supply_exhaustion(empty_search_passes: int, pending_video_count: int) -> bool:
    return (
        int(pending_video_count or 0) <= 0
        and int(empty_search_passes or 0) >= _MAX_EMPTY_SEARCH_PASSES_BEFORE_STOP
    )


def _summarize_pending_aweme_ids(pending_entries, limit: int = 20) -> list[str]:
    summarized = []
    for entry in list(pending_entries or []):
        aweme_id = ""
        if isinstance(entry, dict):
            aweme_id = str(entry.get("aweme_id", "") or "").strip()
        else:
            aweme_id = str(entry or "").strip()
        if not aweme_id:
            continue
        summarized.append(aweme_id)
        if len(summarized) >= max(int(limit or 0), 1):
            break
    return summarized


def _normalize_comment_pool(comments, fallback_content: str = "") -> list[str]:
    pool = [
        str(item or "").strip()
        for item in (comments if isinstance(comments, list) else [])
        if str(item or "").strip()
    ]
    fallback = str(fallback_content or "").strip()
    if not pool and fallback:
        pool.append(fallback)
    return pool


def _sleep_with_stop_check(stop_event, task_stop_event, crawler, interval_seconds: float) -> bool:
    elapsed = 0.0
    interval = max(float(interval_seconds or 0.0), 0.0)
    while elapsed < interval:
        if _is_search_stop_requested(stop_event, task_stop_event, crawler):
            return False
        sleep_time = min(0.2, interval - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time
    return True


def _write_crawler_bootstrap_log(message: str) -> None:
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "crawler_bootstrap.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | pid={os.getpid()} | {message}\n")
    except Exception:
        pass


def _resolve_log_dir() -> Path:
    """
    解析日志目录路径，优先使用环境变量指定的目录，
    在权限不足时回退到当前工作目录下的logs文件夹
    
    Returns:
        Path: 可用的日志目录路径
    """
    log_dir_value = os.getenv("HUOKE_LOG_DIR", "").strip()
    if log_dir_value:
        return Path(log_dir_value)

    persistent_root = os.getenv("HUOKE_PERSISTENT_ROOT", "").strip()
    if persistent_root:
        return Path(persistent_root) / "logs"

    # 尝试使用LOCALAPPDATA目录，但在权限不足时回退到当前工作目录
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidate_dir = Path(local_app_data) / "HuokeSmartBot" / "logs"
        try:
            # 测试是否有写入权限
            candidate_dir.mkdir(parents=True, exist_ok=True)
            return candidate_dir
        except (PermissionError, OSError):
            # 权限不足，回退到当前工作目录
            logger.warning(f"无法创建日志目录 {candidate_dir}，回退到当前工作目录")
    
    # 回退到当前工作目录下的logs文件夹
    return Path.cwd() / "logs"


def _build_ipc_message(msg_type: str, source: str, data=None):
    return {
        "msg_type": msg_type,
        "source": source,
        "data": data or {},
        "timestamp": time.time(),
    }


def crawler_process_main(command_queue, result_queue, stop_event, task_stop_event):
    heartbeat_interval_seconds = max(float(os.getenv("CRAWLER_HEARTBEAT_INTERVAL_SECONDS", "1.0") or 1.0), 0.5)
    stall_warning_seconds = max(float(os.getenv("CRAWLER_STALL_WARNING_SECONDS", "20") or 20.0), 5.0)
    stall_force_reset_seconds = max(
        float(os.getenv("CRAWLER_STALL_FORCE_RESET_SECONDS", "90") or 90.0),
        stall_warning_seconds + 5.0,
    )
    _write_crawler_bootstrap_log("crawler_process_main entered")
    from loguru import logger

    try:
        import setproctitle

        setproctitle.setproctitle("huoketest-crawler")
    except Exception as e:
        logger.debug(f"设置进程名失败: {e}")

    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / build_process_scoped_log_file("crawler_process")),
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        enqueue=True,
    )

    logger.info("爬取/私信进程启动")
    _write_crawler_bootstrap_log(
        f"logger initialized; log_dir={log_dir}; cwd={os.getcwd()}; "
        f"frozen={getattr(__import__('sys'), 'frozen', False)}"
    )

    db = None
    browser_manager = None
    page = None
    crawler = None
    sender = None
    crawler_runtime_state = {
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
        "current_login_account_id": "",
        "current_login_account_source": "",
        "current_login_account_resolved": False,
        "expected_login_account_id": "",
        "expected_login_account_source": "",
        "expected_login_account_resolved": False,
        "login_transfer_matched": False,
    }
    current_runtime_account_id = ""
    runtime_state_lock = threading.Lock()
    heartbeat_stop_event = threading.Event()

    def _emit_state_update() -> None:
        try:
            with runtime_state_lock:
                snapshot = dict(crawler_runtime_state)
            result_queue.put(_build_ipc_message("state_update", "crawler", snapshot))
        except Exception:
            pass

    def _update_shared_state(**updates):
        now = int(time.time())
        with runtime_state_lock:
            previous = dict(crawler_runtime_state)
            snapshot = dict(previous)
            snapshot.update(updates)

            progress_changed = "progress" in updates and dict(previous.get("progress") or {}) != dict(snapshot.get("progress") or {})
            task_changed = "current_task" in updates and str(previous.get("current_task") or "") != str(snapshot.get("current_task") or "")
            busy_changed = "busy" in updates and bool(previous.get("busy", False)) != bool(snapshot.get("busy", False))
            meaningful_progress = progress_changed or task_changed or busy_changed

            if meaningful_progress:
                snapshot["last_progress_at"] = now

            if bool(snapshot.get("busy", False)):
                if not int(previous.get("busy_started_at", 0) or 0) or (busy_changed and bool(snapshot.get("busy", False))):
                    snapshot["busy_started_at"] = now
                if meaningful_progress:
                    snapshot["stalled"] = False
                    snapshot["stall_since"] = 0
                    snapshot["stall_duration_seconds"] = 0
                    snapshot["stall_reason"] = ""
                    snapshot["force_reset_recommended"] = False
            else:
                snapshot["busy_started_at"] = 0
                snapshot["stalled"] = False
                snapshot["stall_since"] = 0
                snapshot["stall_duration_seconds"] = 0
                snapshot["stall_reason"] = ""
                snapshot["force_reset_recommended"] = False

            crawler_runtime_state.update(snapshot)
        _emit_state_update()

    def _heartbeat_loop() -> None:
        while not heartbeat_stop_event.is_set() and not stop_event.is_set():
            now = int(time.time())
            with runtime_state_lock:
                snapshot = dict(crawler_runtime_state)
            updates = {"last_heartbeat": now}
            if bool(snapshot.get("busy", False)):
                last_progress_at = int(snapshot.get("last_progress_at", 0) or snapshot.get("busy_started_at", 0) or now)
                stall_duration_seconds = max(now - last_progress_at, 0)
                stalled = stall_duration_seconds >= stall_warning_seconds
                force_reset_recommended = stall_duration_seconds >= stall_force_reset_seconds
                stall_since = int(snapshot.get("stall_since", 0) or 0)
                if stalled and not stall_since:
                    stall_since = now
                if not stalled:
                    stall_since = 0
                updates.update(
                    stalled=stalled,
                    stall_since=stall_since,
                    stall_duration_seconds=stall_duration_seconds,
                    stall_reason=(
                        f"当前步骤超过 {stall_duration_seconds} 秒无进度更新"
                        if stalled else ""
                    ),
                    force_reset_recommended=force_reset_recommended,
                )
            elif any(
                bool(snapshot.get(field))
                for field in ("stalled", "stall_since", "stall_duration_seconds", "stall_reason", "force_reset_recommended")
            ):
                updates.update(
                    stalled=False,
                    stall_since=0,
                    stall_duration_seconds=0,
                    stall_reason="",
                    force_reset_recommended=False,
                )
            _update_shared_state(**updates)
            heartbeat_stop_event.wait(heartbeat_interval_seconds)

    def _set_idle_state(detail: str = ""):
        _update_shared_state(
            current_task="Idle",
            progress={"total": 0, "current": 0, "detail": detail},
            busy=False,
        )

    def _refresh_current_login_account_scope(force_refresh: bool = False) -> dict:
        nonlocal current_runtime_account_id
        if not sender:
            return {}
        try:
            scope = sender.resolve_current_account_scope(force_refresh=force_refresh)
        except Exception as exc:
            logger.debug(f"刷新当前登录账号失败: {exc}")
            scope = {}
        current_runtime_account_id = (
            str(scope.get("account_id") or "").strip()
            if bool(scope.get("resolved", False))
            else ""
        )
        _update_shared_state(
            current_login_account_id=str(scope.get("account_id") or "").strip(),
            current_login_account_source=str(scope.get("source") or "").strip(),
            current_login_account_resolved=bool(scope.get("resolved", False)),
        )
        return scope

    def _extract_login_transfer(cmd_data) -> dict:
        command = dict(cmd_data or {})
        return {
            "expected_login_account_id": str(command.get("expected_login_account_id", "") or "").strip(),
            "expected_login_account_source": str(command.get("expected_login_account_source", "") or "").strip(),
            "expected_login_account_resolved": bool(command.get("expected_login_account_resolved", False)),
        }

    def _is_expected_login_match(scope: dict, expected_login_account_id: str, expected_login_account_resolved: bool) -> bool:
        return True

    def _should_stop_current_task() -> bool:
        return bool(stop_event.is_set() or task_stop_event.is_set() or (crawler and crawler.is_stopped()))

    def _sleep_with_task_stop(total_seconds: float, chunk_seconds: float = 2.5) -> bool:
        elapsed = 0.0
        while elapsed < total_seconds:
            if _should_stop_current_task():
                return False
            sleep_time = min(max(float(chunk_seconds or 0.2), 0.05), total_seconds - elapsed, 0.2)
            time.sleep(sleep_time)
            elapsed += sleep_time
            _update_shared_state(
                last_progress_at=int(time.time()),
                stalled=False,
                stall_since=0,
                stall_duration_seconds=0,
                stall_reason="",
                force_reset_recommended=False,
            )
        return True

    def _wait_for_interact_comment_slot(
        limit_service,
        *,
        total: int,
        current_index: int,
        uid: str,
        account_id: str = "",
        current_task_label: str = "Interacting",
        detail_builder=None,
    ):
        while True:
            if _should_stop_current_task():
                return {"status": "stopped", "decision": None}

            decision = limit_service.check_send_allowed(account_id=str(account_id or "").strip())
            if decision.allowed:
                return {"status": "allowed", "decision": decision}

            retry_after_seconds = max(int(decision.details.get("retry_after_seconds", 0) or 0), 0)
            if decision.status_code == "daily_limit_exceeded":
                logger.warning(f"一键互动评论触发 24 小时上限: {decision.message}")
                return {"status": "daily_limit_reached", "decision": decision}

            wait_seconds = max(retry_after_seconds, 1)
            logger.info(
                f"一键互动评论频控等待: uid={uid}, status_code={decision.status_code}, "
                f"wait_seconds={wait_seconds}, detail={decision.message}"
            )
            deadline = time.monotonic() + wait_seconds
            while True:
                if _should_stop_current_task():
                    return {"status": "stopped", "decision": decision}
                remaining_seconds = max(int(deadline - time.monotonic() + 0.999), 0)
                if remaining_seconds <= 0:
                    break
                if callable(detail_builder):
                    progress_detail = str(detail_builder(remaining_seconds) or "").strip()
                else:
                    progress_detail = (
                        f"第 {current_index + 1}/{total} 个客户发送前触发评论频控，"
                        f"等待 {remaining_seconds} 秒后继续: {uid}"
                    )
                _update_shared_state(
                    current_task=current_task_label,
                    progress={
                        "total": total,
                        "current": current_index,
                        "detail": progress_detail,
                    },
                    busy=True,
                )
                time.sleep(min(2.0, max(deadline - time.monotonic(), 0.0)))

    def _run_comment_interactions(
        *,
        target_uids,
        comment_pool,
        platform: str,
        progress_total: int,
        progress_current: int,
        current_task_label: str,
        progress_prefix: str,
        source: str,
    ):
        if not crawler or not target_uids or not comment_pool:
            return {
                "attempted": 0,
                "success": 0,
                "stopped": False,
                "limit_reached_reason": "",
            }

        from src.common.interact_comment_limit_service import get_interact_comment_limit_service

        interact_limit_service = get_interact_comment_limit_service()
        attempted_count = 0
        success_count = 0
        limit_reached_reason = ""
        stopped = False
        total_targets = len(target_uids)
        for idx, uid in enumerate(target_uids):
            if _should_stop_current_task():
                stopped = True
                break

            slot_result = _wait_for_interact_comment_slot(
                interact_limit_service,
                total=progress_total,
                current_index=progress_current,
                uid=uid,
                account_id=current_runtime_account_id,
                current_task_label=current_task_label,
                detail_builder=(
                    lambda remaining_seconds, current_uid=uid: (
                        f"{progress_prefix}评论频控等待 {remaining_seconds} 秒后继续: {current_uid}"
                    )
                ),
            )
            if slot_result["status"] == "daily_limit_reached":
                limit_reached_reason = str(
                    (slot_result["decision"] or {}).message
                    if hasattr(slot_result.get("decision"), "message")
                    else "评论已触发 24 小时上限"
                )
                break
            if slot_result["status"] == "stopped":
                stopped = True
                break

            current_comment = random.choice(comment_pool)
            attempted_count += 1
            _update_shared_state(
                current_task=current_task_label,
                progress={
                    "total": progress_total,
                    "current": progress_current,
                    "detail": (
                        f"{progress_prefix}正在评论第 {idx + 1}/{total_targets} 个客户: {uid}"
                    ),
                },
                busy=True,
            )
            logger.info(
                f"{source} 随机评论选择: uid={uid}, "
                f"template_index={comment_pool.index(current_comment) + 1}/{len(comment_pool)}, "
                f"comment_preview={current_comment[:30]}"
            )
            ok = crawler.perform_one_click_interact(uid, current_comment)
            if ok:
                success_count += 1
                interact_limit_service.record_comment_event(
                    user_id=uid,
                    comment=current_comment,
                    account_id=current_runtime_account_id,
                    source=source,
                )
                if db:
                    db.update_customer_interact_status(uid, platform, "interacted")

            if idx < total_targets - 1:
                interval = random.uniform(15, 22)
                if not _sleep_with_task_stop(interval):
                    stopped = True
                    break

        return {
            "attempted": attempted_count,
            "success": success_count,
            "stopped": stopped,
            "limit_reached_reason": limit_reached_reason,
        }

    def _normalize_reply_targets(items):
        normalized_targets = []
        seen_keys = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            aweme_id = str(item.get("aweme_id", "") or "").strip()
            comment_id = str(item.get("comment_id", "") or "").strip()
            video_url = str(item.get("video_url", "") or "").strip()
            if not aweme_id or not comment_id or not video_url:
                continue
            dedupe_key = (aweme_id, comment_id)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            normalized_targets.append(
                {
                    "platform": str(item.get("platform", "") or "").strip() or "douyin",
                    "aweme_id": aweme_id,
                    "comment_id": comment_id,
                    "parent_comment_id": "",
                    "root_comment_id": comment_id,
                    "comment_level": 1,
                    "video_url": video_url,
                    "detail_kind": str(item.get("detail_kind", "") or "").strip(),
                    "content_type": str(item.get("content_type", "") or "").strip(),
                    "sec_uid": str(item.get("sec_uid", "") or "").strip(),
                    "unique_id": str(item.get("unique_id", "") or "").strip(),
                    "nickname": str(item.get("nickname", "") or "").strip(),
                    "comment_text": str(item.get("comment_text", "") or "").strip(),
                    "create_time": int(item.get("create_time", 0) or 0),
                    "matched_keyword": str(item.get("matched_keyword", "") or "").strip(),
                    "parent_anchor": {},
                    "reply_diagnostic_mode": bool(item.get("reply_diagnostic_mode", False)),
                }
            )
        return normalized_targets

    def _run_comment_reply_interactions(
        *,
        reply_targets,
        reply_pool,
        platform: str,
        remaining_reply_quota: int,
        progress_total: int,
        progress_current: int,
        current_task_label: str,
        progress_prefix: str,
        source: str,
        reply_diagnostic_mode: bool = False,
    ):
        if not crawler or not reply_targets or not reply_pool:
            return {
                "attempted": 0,
                "success": 0,
                "failed": 0,
                "failure_reasons": {},
                "failure_samples": [],
                "stopped": False,
                "limit_reached_reason": "",
                "quota_exhausted": False,
                "remaining_reply_quota": max(int(remaining_reply_quota or 0), 0),
            }

        from src.common.search_reply_limit_service import get_search_reply_limit_service

        reply_limit_service = get_search_reply_limit_service()
        remaining_quota = max(int(remaining_reply_quota or 0), 0)
        attempted_count = 0
        success_count = 0
        failure_reason_counts = {}
        failure_samples = []
        diagnostics_summary = {
            "context_recovered_count": 0,
            "parent_anchor_matched_count": 0,
            "surface_scope_hit_count": 0,
            "document_fallback_hit_count": 0,
        }
        limit_reached_reason = ""
        quota_exhausted = False
        stopped = False
        total_targets = len(reply_targets)
        for idx, target in enumerate(reply_targets):
            if _should_stop_current_task():
                stopped = True
                break

            if remaining_quota <= 0:
                quota_exhausted = True
                break

            target_comment_id = str((target or {}).get("comment_id", "") or "").strip()
            target_aweme_id = str((target or {}).get("aweme_id", "") or "").strip()
            target_label = target_comment_id or target_aweme_id or f"target_{idx + 1}"
            target = dict(target or {})
            target["reply_diagnostic_mode"] = bool(reply_diagnostic_mode or target.get("reply_diagnostic_mode"))

            if _should_stop_current_task():
                stopped = True
                break
            reply_limit_decision = reply_limit_service.check_send_allowed(account_id=current_runtime_account_id)
            if not reply_limit_decision.allowed:
                limit_reached_reason = str(getattr(reply_limit_decision, "message", "") or "评论下直接回复已触发频控上限")
                _update_shared_state(
                    current_task=current_task_label,
                    progress={
                        "total": progress_total,
                        "current": progress_current,
                        "detail": f"{progress_prefix}{limit_reached_reason}",
                    },
                    busy=True,
                )
                logger.warning(
                    f"评论下直接回复触发频控停止: target={target_label}, "
                    f"status_code={getattr(reply_limit_decision, 'status_code', '')}, detail={limit_reached_reason}"
                )
                break

            current_reply = random.choice(reply_pool)
            attempted_count += 1
            _update_shared_state(
                current_task=current_task_label,
                progress={
                    "total": progress_total,
                    "current": progress_current,
                    "detail": (
                        f"{progress_prefix}正在回复目标评论: {target_label} "
                        f"(剩余回复额度 {remaining_quota})"
                    ),
                },
                busy=True,
            )
            logger.info(
                f"{source} 随机回复选择: aweme_id={target_aweme_id}, comment_id={target_comment_id}, "
                f"template_index={reply_pool.index(current_reply) + 1}/{len(reply_pool)}, "
                f"reply_preview={current_reply[:30]}"
            )
            ok = crawler.reply_to_original_comment(target, current_reply)
            reply_result = {}
            with_result_reader = getattr(crawler, "get_last_original_reply_result", None)
            if callable(with_result_reader):
                try:
                    reply_result = with_result_reader() or {}
                except Exception:
                    reply_result = {}
            reply_diagnostics = dict((reply_result or {}).get("diagnostics") or {})
            if reply_diagnostics.get("context_recovered"):
                diagnostics_summary["context_recovered_count"] += 1
            if reply_diagnostics.get("parent_anchor_matched"):
                diagnostics_summary["parent_anchor_matched_count"] += 1
            surface_selector = str(reply_diagnostics.get("surface_selector", "") or "").strip()
            if surface_selector:
                diagnostics_summary["surface_scope_hit_count"] += 1
            elif reply_diagnostics.get("document_fallback_used"):
                diagnostics_summary["document_fallback_hit_count"] += 1
            if ok:
                success_count += 1
                remaining_quota = max(remaining_quota - 1, 0)
                reply_limit_service.record_reply_event(
                    target_id=target_label,
                    reply_text=current_reply,
                    account_id=current_runtime_account_id,
                    source=source,
                )
                sec_uid = str((target or {}).get("sec_uid", "") or "").strip()
                if db and sec_uid:
                    db.update_customer_interact_status(sec_uid, platform, "interacted")
            else:
                failure_reason = str((reply_result or {}).get("reason", "") or "").strip() or "unknown"
                failure_reason_counts[failure_reason] = int(failure_reason_counts.get(failure_reason, 0) or 0) + 1
                sec_uid = str((target or {}).get("sec_uid", "") or "").strip()
                if db and sec_uid:
                    db.update_customer_interact_status(sec_uid, platform, "failed")
                if len(failure_samples) < 5:
                    failure_samples.append(
                        {
                            "reason": failure_reason,
                            "message": str((reply_result or {}).get("message", "") or "").strip(),
                            "comment_id": target_comment_id,
                            "aweme_id": target_aweme_id,
                            "nickname": str((target or {}).get("nickname", "") or "").strip(),
                            "stage": str((reply_result or {}).get("stage", "") or "").strip(),
                            "diagnostics": reply_diagnostics,
                        }
                    )

            # 发送失败/无法发送时直接切换到下一个目标，不额外等待。
            if ok and idx < total_targets - 1:
                interval = random.uniform(15, 22)
                if not _sleep_with_task_stop(interval):
                    stopped = True
                    break

        return {
            "attempted": attempted_count,
            "success": success_count,
            "failed": max(attempted_count - success_count, 0),
            "failure_reasons": dict(failure_reason_counts),
            "failure_samples": list(failure_samples),
            "stopped": stopped,
            "limit_reached_reason": limit_reached_reason,
            "quota_exhausted": quota_exhausted,
            "remaining_reply_quota": remaining_quota,
            "diagnostics": diagnostics_summary,
        }

    def _cleanup_components():
        nonlocal db, browser_manager, page, crawler, sender, current_runtime_account_id

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
            current_runtime_account_id = ""
            _update_shared_state(
                current_login_account_id="",
                current_login_account_source="",
                current_login_account_resolved=False,
                expected_login_account_id="",
                expected_login_account_source="",
                expected_login_account_resolved=False,
                login_transfer_matched=False,
            )

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name="crawler-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()

    def _init_components(
        expected_login_account_id: str = "",
        expected_login_account_source: str = "",
        expected_login_account_resolved: bool = False,
    ):
        nonlocal db, browser_manager, page, crawler, sender, current_runtime_account_id
        expected_login_account_id = str(expected_login_account_id or "").strip()
        expected_login_account_source = str(expected_login_account_source or "").strip()
        expected_login_account_resolved = bool(expected_login_account_resolved)
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
                expected_login_account_id="",
                expected_login_account_source="",
                expected_login_account_resolved=False,
                login_transfer_matched=True,
            )
            return

        if any(component is not None for component in (browser_manager, page, crawler, sender)):
            logger.warning("检测到爬取/私信组件引用仍在，但浏览器页已失效，准备重建组件")
            _cleanup_components()

        _write_crawler_bootstrap_log("importing DatabaseManager")
        from src.common.database import DatabaseManager
        _write_crawler_bootstrap_log("imported DatabaseManager")
        _write_crawler_bootstrap_log("importing crawler session settings")
        from src.common.crawler_session_state import load_crawler_session_cookies
        from src.config.settings import CRAWLER_USER_DATA_DIR, DOUYIN_HOME_URL
        _write_crawler_bootstrap_log("imported crawler session settings")
        _write_crawler_bootstrap_log("importing BrowserManager")
        from src.douyin_bot.browser_manager import BrowserManager
        _write_crawler_bootstrap_log("imported BrowserManager")
        _write_crawler_bootstrap_log("importing Crawler")
        from src.douyin_bot.crawler import Crawler
        _write_crawler_bootstrap_log("imported Crawler")
        _write_crawler_bootstrap_log("importing MessageSender")
        from src.douyin_bot.message_sender import MessageSender
        _write_crawler_bootstrap_log("imported MessageSender")

        crawler_user_data_dir = Path(CRAWLER_USER_DATA_DIR)
        _write_crawler_bootstrap_log(
            f"init_components imported modules; user_data_dir={crawler_user_data_dir}"
        )
        _write_crawler_bootstrap_log("creating DatabaseManager")
        db = DatabaseManager()
        _write_crawler_bootstrap_log("created DatabaseManager")
        _write_crawler_bootstrap_log("creating BrowserManager")
        _write_crawler_bootstrap_log("database manager initialized")
        browser_manager = BrowserManager(user_data_dir=crawler_user_data_dir)
        _write_crawler_bootstrap_log("created BrowserManager")
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
        _write_crawler_bootstrap_log("created Crawler")
        crawler._stop_event = task_stop_event
        sender = MessageSender(page, db)
        _write_crawler_bootstrap_log("created MessageSender")
        current_runtime_account_id = ""
        logger.info("爬取/私信组件初始化完成（使用独立爬取标签页）")
        _write_crawler_bootstrap_log("init_components completed")
        _update_shared_state(
            browser_ready=True,
            initialized=True,
            current_task="Idle",
            progress={"total": 0, "current": 0, "detail": ""},
            busy=False,
            expected_login_account_id="",
            expected_login_account_source="",
            expected_login_account_resolved=False,
            login_transfer_matched=True,
        )

    while not stop_event.is_set():
        try:
            try:
                cmd = command_queue.get_nowait()
                cmd_type = cmd.get("type", "")
                cmd_data = cmd.get("data", {})

                logger.info(f"爬取进程收到命令: {cmd_type}")
                _write_crawler_bootstrap_log(f"received command: {cmd_type}")

                if cmd_type == "init_browser":
                    task_stop_event.clear()
                    login_transfer = _extract_login_transfer(cmd_data)
                    _init_components(**login_transfer)
                    result_queue.put(
                        _build_ipc_message("browser_ready", "crawler", {"success": browser_manager is not None})
                    )
                    _write_crawler_bootstrap_log(
                        f"browser_ready emitted; success={browser_manager is not None}"
                    )

                elif cmd_type == "search":
                    task_stop_event.clear()
                    login_transfer = _extract_login_transfer(cmd_data)
                    _init_components(**login_transfer)
                    if crawler:
                        import datetime

                        keyword = cmd_data.get("keyword", "")
                        platform = str(cmd_data.get("platform", "douyin") or "douyin").strip() or "douyin"
                        lead_quota, requested_video_limit = _resolve_search_command_limits(cmd_data)
                        comment_keywords = cmd_data.get("comment_keywords", [])
                        comment_time_start = cmd_data.get("comment_time_start", "")
                        comment_time_end = cmd_data.get("comment_time_end", "")
                        skip_crawled = bool(cmd_data.get("skip_crawled", True))
                        skip_existing_videos = bool(cmd_data.get("skip_existing_videos", False))
                        cleanup_unfinished_videos = bool(cmd_data.get("cleanup_unfinished_videos", True))
                        auto_reply_enabled = bool(
                            cmd_data.get("auto_reply_enabled", False)
                            or cmd_data.get("auto_comment_enabled", False)
                        )
                        reply_quota = _resolve_search_reply_quota(cmd_data)
                        reply_pool = _normalize_comment_pool(
                            cmd_data.get("reply_templates")
                            or cmd_data.get("comment_templates")
                        )
                        if isinstance(comment_keywords, str):
                            normalized = comment_keywords.replace("，", ",")
                            comment_keywords = [kw.strip() for kw in normalized.split(",") if kw.strip()]
                        auto_reply_enabled = bool(auto_reply_enabled and reply_pool)
                        reply_diagnostic_mode = bool(cmd_data.get("reply_diagnostic_mode", False))
                        queue_cleanup_before_start = {"deleted_count": 0, "deleted_aweme_ids": [], "deleted_status_counts": {}}
                        if cleanup_unfinished_videos:
                            queue_cleanup_before_start = _cleanup_search_unfinished_videos(
                                db,
                                platform,
                                keyword,
                                phase="task_start",
                            )
                        resume_state = (
                            {}
                            if cleanup_unfinished_videos
                            else dict(cmd_data.get("_resume_state") or {})
                            if isinstance(cmd_data.get("_resume_state"), dict)
                            else {}
                        )
                        resume_pending_entries = resume_state.get("pending_entries")
                        normalized_resume_pending_entries = []
                        if isinstance(resume_pending_entries, list):
                            for item in resume_pending_entries:
                                if not isinstance(item, dict):
                                    continue
                                resume_aweme_id = str(item.get("aweme_id", "") or "").strip()
                                resume_url = str(item.get("url", "") or "").strip()
                                if resume_aweme_id and resume_url:
                                    normalized_resume_pending_entries.append({
                                        "aweme_id": resume_aweme_id,
                                        "url": resume_url,
                                    })
                        resume_current_video_entry = dict(resume_state.get("current_video_entry") or {}) if isinstance(resume_state.get("current_video_entry"), dict) else {}
                        resume_current_aweme_id = str(resume_current_video_entry.get("aweme_id", "") or resume_state.get("current_aweme_id", "") or "").strip()
                        resume_current_video_url = str(resume_current_video_entry.get("url", "") or "").strip()
                        if resume_current_aweme_id and resume_current_video_url:
                            normalized_resume_pending_entries.insert(0, {
                                "aweme_id": resume_current_aweme_id,
                                "url": resume_current_video_url,
                            })
                        deduped_resume_pending_entries = []
                        deduped_resume_aweme_ids = set()
                        for item in normalized_resume_pending_entries:
                            item_aweme_id = str(item.get("aweme_id", "") or "").strip()
                            item_url = str(item.get("url", "") or "").strip()
                            if not item_aweme_id or not item_url or item_aweme_id in deduped_resume_aweme_ids:
                                continue
                            deduped_resume_aweme_ids.add(item_aweme_id)
                            deduped_resume_pending_entries.append({
                                "aweme_id": item_aweme_id,
                                "url": item_url,
                            })

                        search_task_id = str(resume_state.get("task_id") or "").strip() or f"search_{int(time.time() * 1000)}"
                        started_at = (
                            str(resume_state.get("started_at") or "").strip()
                            or datetime.datetime.now(datetime.timezone.utc).isoformat()
                        )
                        search_summary = {
                            "task_id": search_task_id,
                            "status": "running",
                            "keyword": keyword,
                            "started_at": started_at,
                            "lead_quota": lead_quota,
                            "target_effective_count": lead_quota,
                            "max_videos": requested_video_limit,
                            "videos_discovered": 0,
                            "videos_processed": 0,
                            "saved_customers": 0,
                            "comments_crawled": 0,
                            "effective_count": 0,
                            "partial_video_count": 0,
                            "risk_video_count": 0,
                            "failed_video_count": 0,
                            "skipped_video_count": 0,
                            "current_aweme_id": "",
                            "pending_video_count": 0,
                            "pending_aweme_ids": [],
                            "search_response_batches": 0,
                            "pending_search_batches": 0,
                            "peak_pending_search_batches": 0,
                            "dropped_search_batches_count": 0,
                            "latest_search_batch_id": 0,
                            "latest_search_batch_url": "",
                            "auto_reply_enabled": auto_reply_enabled,
                            "auto_reply_quota": reply_quota if auto_reply_enabled else 0,
                            "auto_reply_remaining_quota": reply_quota if auto_reply_enabled else 0,
                            "auto_reply_templates": len(reply_pool),
                            "auto_reply_attempted": 0,
                            "auto_reply_success": 0,
                            "auto_reply_failed": 0,
                            "auto_reply_failure_reasons": {},
                            "auto_reply_failure_samples": [],
                            "auto_reply_diagnostics": {},
                            "auto_reply_limit_reached": False,
                            "auto_reply_quota_exhausted": False,
                            "search_passes": 0,
                            "empty_search_passes": 0,
                            "quota_reached": False,
                            "remaining_quota": lead_quota,
                            "stop_reason": "",
                            "last_video_result": None,
                            "cleanup_unfinished_videos": cleanup_unfinished_videos,
                            "queue_cleanup_before_start": dict(queue_cleanup_before_start),
                            "queue_cleanup_after_finish": {},
                            "resumed_from_snapshot": bool(deduped_resume_pending_entries or resume_state),
                            "resumed_at": (
                                datetime.datetime.now(datetime.timezone.utc).isoformat()
                                if (deduped_resume_pending_entries or resume_state)
                                else ""
                            ),
                        }
                        search_page = None
                        search_crawler = crawler
                        try:
                            if browser_manager and getattr(browser_manager, "context", None):
                                candidate_search_page = browser_manager.create_search_page(force_new=True)
                                crawler_page_ref = getattr(crawler, "page", None) or getattr(browser_manager, "crawler_page", None)
                                if _can_use_as_independent_search_page(candidate_search_page, crawler_page=crawler_page_ref):
                                    from src.douyin_bot.crawler import Crawler as SearchCrawler

                                    search_page = candidate_search_page
                                    search_crawler = SearchCrawler(search_page, db)
                                else:
                                    logger.warning("综合搜索标签页不可用或与评论抓取页重叠，回退到单页串行搜索")
                        except Exception as search_page_e:
                            logger.warning(f"创建综合搜索标签页失败，回退到当前爬取页搜索: {search_page_e}")
                            search_crawler = crawler

                        discovered_count = max(int(resume_state.get("videos_discovered", 0) or 0), 0)
                        processed_count = max(int(resume_state.get("videos_processed", 0) or 0), 0)
                        saved_customer_count = max(
                            int(resume_state.get("effective_count", resume_state.get("saved_customers", 0)) or 0),
                            0,
                        )
                        skipped_count = max(int(resume_state.get("skipped_video_count", 0) or 0), 0)
                        failed_video_count = max(int(resume_state.get("failed_video_count", 0) or 0), 0)
                        pending_video_queue = list(deduped_resume_pending_entries)
                        current_aweme_id = resume_current_aweme_id
                        current_video_url = resume_current_video_url
                        remaining_auto_reply_quota = max(
                            int(resume_state.get("auto_reply_remaining_quota", reply_quota if auto_reply_enabled else 0) or 0),
                            0,
                        ) if auto_reply_enabled else 0
                        seen_aweme_ids = {
                            str(item).strip()
                            for item in (resume_state.get("seen_aweme_ids") or [])
                            if str(item or "").strip()
                        }
                        seen_aweme_ids.update(
                            str(item.get("aweme_id", "") or "").strip()
                            for item in pending_video_queue
                            if str(item.get("aweme_id", "") or "").strip()
                        )
                        if current_aweme_id:
                            seen_aweme_ids.add(current_aweme_id)
                        search_passes = max(int(resume_state.get("search_passes", 0) or 0), 0)
                        empty_search_passes = max(int(resume_state.get("empty_search_passes", 0) or 0), 0)
                        quota_reached = False
                        search_summary["partial_video_count"] = max(int(resume_state.get("partial_video_count", 0) or 0), 0)
                        search_summary["risk_video_count"] = max(int(resume_state.get("risk_video_count", 0) or 0), 0)
                        search_summary["auto_reply_attempted"] = max(int(resume_state.get("auto_reply_attempted", 0) or 0), 0)
                        search_summary["auto_reply_success"] = max(int(resume_state.get("auto_reply_success", 0) or 0), 0)
                        search_summary["auto_reply_failed"] = max(int(resume_state.get("auto_reply_failed", 0) or 0), 0)
                        search_summary["auto_reply_failure_reasons"] = dict(resume_state.get("auto_reply_failure_reasons") or {})
                        search_summary["auto_reply_failure_samples"] = list(resume_state.get("auto_reply_failure_samples") or [])
                        search_summary["auto_reply_diagnostics"] = dict(resume_state.get("auto_reply_diagnostics") or {})
                        search_summary["last_video_result"] = resume_state.get("last_video_result")

                        def _collect_search_interceptor_stats() -> dict:
                            getter = getattr(search_crawler, "get_last_search_interceptor_stats", None)
                            if not callable(getter):
                                return {}
                            try:
                                stats = getter()
                            except Exception:
                                logger.debug("读取搜索拦截器统计失败", exc_info=True)
                                return {}
                            return dict(stats or {}) if isinstance(stats, dict) else {}

                        def _build_search_resume_state() -> dict:
                            pending_entries = []
                            for item in pending_video_queue:
                                if not isinstance(item, dict):
                                    continue
                                item_aweme_id = str(item.get("aweme_id", "") or "").strip()
                                item_url = str(item.get("url", "") or "").strip()
                                if item_aweme_id and item_url:
                                    pending_entries.append({
                                        "aweme_id": item_aweme_id,
                                        "url": item_url,
                                    })
                            current_video_entry = None
                            if current_aweme_id and current_video_url:
                                current_video_entry = {
                                    "aweme_id": current_aweme_id,
                                    "url": current_video_url,
                                }
                            return {
                                "task_id": search_task_id,
                                "keyword": keyword,
                                "started_at": str(search_summary.get("started_at") or ""),
                                "lead_quota": lead_quota,
                                "target_effective_count": lead_quota,
                                "max_videos": requested_video_limit,
                                "effective_count": saved_customer_count,
                                "saved_customers": saved_customer_count,
                                "videos_discovered": discovered_count,
                                "videos_processed": processed_count,
                                "skipped_video_count": skipped_count,
                                "failed_video_count": failed_video_count,
                                "partial_video_count": int(search_summary.get("partial_video_count", 0) or 0),
                                "risk_video_count": int(search_summary.get("risk_video_count", 0) or 0),
                                "search_passes": search_passes,
                                "empty_search_passes": empty_search_passes,
                                "current_aweme_id": current_aweme_id,
                                "current_video_entry": current_video_entry,
                                "pending_entries": pending_entries,
                                "pending_aweme_ids": [item["aweme_id"] for item in pending_entries],
                                "auto_reply_enabled": auto_reply_enabled,
                                "auto_reply_remaining_quota": remaining_auto_reply_quota,
                                "auto_reply_attempted": int(search_summary.get("auto_reply_attempted", 0) or 0),
                                "auto_reply_success": int(search_summary.get("auto_reply_success", 0) or 0),
                                "auto_reply_failed": int(search_summary.get("auto_reply_failed", 0) or 0),
                                "auto_reply_failure_reasons": dict(search_summary.get("auto_reply_failure_reasons") or {}),
                                "auto_reply_failure_samples": list(search_summary.get("auto_reply_failure_samples") or []),
                                "auto_reply_diagnostics": dict(search_summary.get("auto_reply_diagnostics") or {}),
                                "last_video_result": search_summary.get("last_video_result"),
                                "seen_aweme_ids": sorted(seen_aweme_ids),
                            }

                        def _sync_search_summary_snapshot() -> dict:
                            search_summary["videos_discovered"] = discovered_count
                            search_summary["videos_processed"] = processed_count
                            search_summary["saved_customers"] = saved_customer_count
                            search_summary["effective_count"] = saved_customer_count
                            search_summary["target_effective_count"] = lead_quota
                            search_summary["remaining_quota"] = max(lead_quota - saved_customer_count, 0)
                            search_summary["search_passes"] = search_passes
                            search_summary["empty_search_passes"] = empty_search_passes
                            search_summary["failed_video_count"] = failed_video_count
                            search_summary["skipped_video_count"] = skipped_count
                            search_summary["current_aweme_id"] = current_aweme_id
                            search_summary["pending_video_count"] = len(pending_video_queue)
                            search_summary["pending_aweme_ids"] = _summarize_pending_aweme_ids(pending_video_queue)
                            search_summary.update(
                                {
                                    "search_response_batches": 0,
                                    "pending_search_batches": 0,
                                    "peak_pending_search_batches": 0,
                                    "dropped_search_batches_count": 0,
                                    "latest_search_batch_id": 0,
                                    "latest_search_batch_url": "",
                                }
                            )
                            search_summary.update(_collect_search_interceptor_stats())
                            return dict(search_summary)

                        def _update_search_runtime(task_label: str, detail: str) -> None:
                            _update_shared_state(
                                current_task=task_label,
                                progress={
                                    "total": lead_quota,
                                    "current": saved_customer_count,
                                    "detail": detail,
                                },
                                busy=True,
                                last_search_summary=_sync_search_summary_snapshot(),
                                search_resume_state=_build_search_resume_state(),
                            )

                        _update_search_runtime(
                            f"Searching: {keyword}",
                            (
                                f"正在恢复搜索任务，待处理视频 {len(pending_video_queue)} 个，"
                                f"已入库 {saved_customer_count}/{lead_quota}..."
                                if (deduped_resume_pending_entries or resume_state)
                                else "正在搜索视频列表并统计入库用户..."
                            ),
                        )

                        search_stream = None

                        while True:
                            if _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                search_summary["stop_reason"] = "stopped_by_request"
                                break
                            if saved_customer_count >= lead_quota:
                                quota_reached = True
                                search_summary["quota_reached"] = True
                                search_summary["stop_reason"] = "lead_quota_reached"
                                logger.info(f"已达到目标用户数 {saved_customer_count}/{lead_quota}，结束任务。")
                                break

                            if pending_video_queue:
                                _update_search_runtime(
                                    f"Searching: {keyword}",
                                    (
                                        f"当前待处理视频 {len(pending_video_queue)} 个，"
                                        f"继续消费队列以补足有效数据 {saved_customer_count}/{lead_quota}..."
                                    ),
                                )
                                while pending_video_queue:
                                    if _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                        search_summary["stop_reason"] = "stopped_by_request"
                                        break
                                    if saved_customer_count >= lead_quota:
                                        quota_reached = True
                                        search_summary["quota_reached"] = True
                                        search_summary["stop_reason"] = "lead_quota_reached"
                                        logger.info(
                                            f"已达到目标用户数 {saved_customer_count}/{lead_quota}，停止继续处理待处理视频。"
                                        )
                                        break

                                    pending_video = pending_video_queue.pop(0)
                                    aweme_id = str(pending_video.get("aweme_id", "") or "").strip()
                                    url = str(pending_video.get("url", "") or "").strip()
                                    if not aweme_id or not url:
                                        continue
                                    if _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                        search_summary["stop_reason"] = "stopped_by_request"
                                        current_aweme_id = ""
                                        current_video_url = ""
                                        break

                                    current_aweme_id = aweme_id
                                    current_video_url = url
                                    _update_search_runtime(
                                        f"Crawling video {processed_count + 1}",
                                        (
                                            f"正在处理第 {processed_count + 1} 个新视频 "
                                            f"(当前 aweme_id: {aweme_id}，待处理: {len(pending_video_queue)}，"
                                            f"共发现: {discovered_count}, 跳过历史: {skipped_count}, "
                                            f"已入库: {saved_customer_count}/{lead_quota})"
                                        ),
                                    )

                                    def _report_video_crawl_progress(progress_payload):
                                        payload = dict(progress_payload or {})
                                        extra_detail = str(payload.get("detail") or "").strip()
                                        refreshed_summary = _sync_search_summary_snapshot()
                                        refreshed_summary["last_video_result"] = {
                                            "aweme_id": aweme_id,
                                            "video_url": url,
                                            "completion_status": "running",
                                            "termination_reason": "",
                                            "completeness_warning": extra_detail,
                                            "crawl_session_id": str(payload.get("session_id", "") or ""),
                                            "dropped_batches_count": 0,
                                        }
                                        _update_shared_state(
                                            current_task=f"Crawling video {processed_count + 1}",
                                            progress={
                                                "total": lead_quota,
                                                "current": saved_customer_count,
                                                "detail": (
                                                    extra_detail
                                                    or (
                                                        f"正在处理第 {processed_count + 1} 个新视频 "
                                                        f"(当前 aweme_id: {aweme_id}，待处理: {len(pending_video_queue)}，"
                                                        f"共发现: {discovered_count}, 跳过历史: {skipped_count}, "
                                                        f"已入库: {saved_customer_count}/{lead_quota})"
                                                    )
                                                ),
                                            },
                                            busy=True,
                                            last_search_summary=refreshed_summary,
                                            search_resume_state=_build_search_resume_state(),
                                        )

                                    try:
                                        crawl_result = crawler.crawl_comments(
                                            url,
                                            target_keywords=comment_keywords,
                                            comment_time_start=comment_time_start,
                                            comment_time_end=comment_time_end,
                                            skip_crawled=skip_crawled,
                                            search_keyword=keyword,
                                            search_task_id=search_task_id,
                                            remaining_customer_quota=max(lead_quota - saved_customer_count, 0),
                                            progress_callback=_report_video_crawl_progress,
                                        )
                                        error_message = ""
                                    except Exception as video_error:
                                        crawl_result = {
                                            "aweme_id": aweme_id,
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
                                        error_message = str(video_error)
                                        logger.error(f"视频评论抓取失败 aweme_id={aweme_id}: {video_error}")

                                    _finalize_video_discovery_crawl(db, platform, aweme_id, crawl_result, error_message)
                                    processed_count += 1
                                    saved_customer_count += int(crawl_result.get("saved_customers", 0) or 0)
                                    search_summary["comments_crawled"] = int(
                                        search_summary.get("comments_crawled", 0) or 0
                                    ) + int(crawl_result.get("total_comments", 0) or 0)
                                    completion_status = str(crawl_result.get("completion_status", "") or "")
                                    final_video_status, _ = _resolve_video_crawl_status(crawl_result)
                                    if final_video_status == "failed":
                                        failed_video_count += 1
                                    if completion_status.startswith("partial_"):
                                        search_summary["partial_video_count"] = int(search_summary.get("partial_video_count", 0) or 0) + 1
                                    if bool(crawl_result.get("risk_control_detected")):
                                        search_summary["risk_video_count"] = int(search_summary.get("risk_video_count", 0) or 0) + 1
                                    search_summary["last_video_result"] = {
                                        "aweme_id": crawl_result.get("aweme_id", "") or aweme_id,
                                        "video_url": url,
                                        "completion_status": completion_status,
                                        "termination_reason": str(crawl_result.get("termination_reason", "") or ""),
                                        "completeness_warning": str(crawl_result.get("completeness_warning", "") or ""),
                                        "crawl_session_id": str(crawl_result.get("crawl_session_id", "") or ""),
                                        "dropped_batches_count": int(crawl_result.get("dropped_batches_count", 0) or 0),
                                    }
                                    current_aweme_id = ""
                                    current_video_url = ""

                                    if auto_reply_enabled and remaining_auto_reply_quota > 0:
                                        linked_reply_targets = _normalize_reply_targets(
                                            crawl_result.get("linked_reply_targets") or []
                                        )
                                        if linked_reply_targets:
                                            auto_reply_result = _run_comment_reply_interactions(
                                                reply_targets=linked_reply_targets,
                                                reply_pool=reply_pool,
                                                platform=platform,
                                                remaining_reply_quota=remaining_auto_reply_quota,
                                                progress_total=lead_quota,
                                                progress_current=saved_customer_count,
                                                current_task_label=f"Searching: {keyword}",
                                                progress_prefix=(
                                                    f"视频 {processed_count} 命中 {len(linked_reply_targets)} 条目标评论，"
                                                ),
                                                source="search_original_comment_reply",
                                                reply_diagnostic_mode=reply_diagnostic_mode,
                                            )
                                            remaining_auto_reply_quota = max(
                                                int(auto_reply_result.get("remaining_reply_quota", remaining_auto_reply_quota) or 0),
                                                0,
                                            )
                                            search_summary = _merge_search_auto_reply_result(
                                                search_summary,
                                                auto_reply_result,
                                            )
                                            if auto_reply_result.get("limit_reached_reason"):
                                                search_summary["auto_reply_limit_reached"] = True
                                                remaining_auto_reply_quota = 0
                                                logger.warning(
                                                    f"评论下直接回复触发上限，后续仅抓取不再回复: "
                                                    f"{auto_reply_result.get('limit_reached_reason')}"
                                                )
                                            elif auto_reply_result.get("quota_exhausted"):
                                                logger.info("评论下回复数量已用完，后续继续抓取但不再执行评论下回复")
                                            if auto_reply_result.get("stopped"):
                                                search_summary["stop_reason"] = "stopped_by_request"
                                                break

                                    _update_search_runtime(
                                        f"Crawling video {processed_count}",
                                        (
                                            f"已完成第 {processed_count} 个新视频 "
                                            f"(剩余待处理: {len(pending_video_queue)}，共发现: {discovered_count}, "
                                            f"跳过历史: {skipped_count}, 已入库: {saved_customer_count}/{lead_quota})"
                                        ),
                                    )
                                    result_queue.put(
                                        _build_ipc_message(
                                            "crawl_progress",
                                            "crawler",
                                            {
                                                "current": saved_customer_count,
                                                "total": lead_quota,
                                                "video_url": url,
                                                "saved_customers": saved_customer_count,
                                                "effective_count": saved_customer_count,
                                                "target_effective_count": lead_quota,
                                                "current_aweme_id": current_aweme_id,
                                                "pending_aweme_ids": _summarize_pending_aweme_ids(pending_video_queue),
                                                "pending_video_count": len(pending_video_queue),
                                                "partial_video_count": int(search_summary.get("partial_video_count", 0) or 0),
                                                "risk_video_count": int(search_summary.get("risk_video_count", 0) or 0),
                                                "last_video_result": search_summary.get("last_video_result"),
                                            },
                                        )
                                    )
                                    if saved_customer_count >= lead_quota:
                                        quota_reached = True
                                        search_summary["quota_reached"] = True
                                        search_summary["stop_reason"] = "lead_quota_reached"
                                        logger.info(
                                            f"视频抓取后累计入库用户 {saved_customer_count}/{lead_quota}，达到目标用户数，结束任务。"
                                        )
                                        break
                                    if not _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                        interval, interval_message = _resolve_post_video_interval(crawl_result)
                                        logger.info(
                                            f"视频 {processed_count} 完成，"
                                            f"completion_status={completion_status}, "
                                            f"termination_reason={crawl_result.get('termination_reason', '')}, "
                                            f"reached_end={bool(crawl_result.get('reached_comment_end'))}, "
                                            f"saved_customers_total={saved_customer_count}/{lead_quota}, "
                                            f"reply_expand_clicks={int(crawl_result.get('reply_expand_clicks', 0) or 0)}, "
                                            f"reply_comments={int(crawl_result.get('reply_comments', 0) or 0)}, "
                                            f"dropped_batches={int(crawl_result.get('dropped_batches_count', 0) or 0)}。"
                                            f"{interval_message}"
                                        )
                                        if not _sleep_with_task_stop(interval):
                                            search_summary["stop_reason"] = "stopped_by_request"
                                            break

                                if quota_reached or _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                    if not search_summary.get("stop_reason"):
                                        search_summary["stop_reason"] = "stopped_by_request"
                                    break

                                empty_search_passes = 0
                                continue_interval = random.uniform(0.8, 1.6)
                                logger.info(
                                    f"待处理视频已处理完成，累计入库用户 {saved_customer_count}/{lead_quota}，"
                                    f"{continue_interval:.1f} 秒后继续搜索补抓。"
                                )
                                _update_search_runtime(
                                    f"Searching: {keyword}",
                                    (
                                        f"待处理视频已消费完成，仍需补充 "
                                        f"{max(lead_quota - saved_customer_count, 0)} 个有效数据，"
                                        f"即将继续搜索新视频..."
                                    ),
                                )
                                if not _sleep_with_task_stop(continue_interval):
                                    search_summary["stop_reason"] = "stopped_by_request"
                                    break
                                continue

                            search_passes += 1
                            search_limit = _resolve_search_pass_video_limit(
                                lead_quota,
                                saved_customer_count,
                                requested_video_limit,
                            )
                            logger.info(
                                f"开始第 {search_passes} 轮搜索补抓: "
                                f"keyword={keyword}, remaining_quota={max(lead_quota - saved_customer_count, 0)}, "
                                f"video_batch_limit={search_limit or 'auto'}"
                            )
                            _update_search_runtime(
                                f"Searching: {keyword}",
                                (
                                    f"第 {search_passes} 轮正在搜索新视频 "
                                    f"(共发现: {discovered_count}, 已处理: {processed_count}, "
                                    f"待处理: {len(pending_video_queue)}, 跳过历史: {skipped_count}, "
                                    f"已入库: {saved_customer_count}/{lead_quota})"
                                ),
                            )

                            is_streaming_safe = (search_crawler != crawler)
                            batch_size = 1 if is_streaming_safe else (search_limit or 5)
                            pass_new_video_count = 0
                            
                            if not hasattr(locals(), 'search_stream') or search_stream is None:
                                search_stream = search_crawler.search_keyword_stream(keyword, max_results=0)
                            
                            while pass_new_video_count < batch_size:
                                if _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                    search_summary["stop_reason"] = "stopped_by_request"
                                    break

                                if saved_customer_count >= lead_quota:
                                    quota_reached = True
                                    search_summary["quota_reached"] = True
                                    search_summary["stop_reason"] = "lead_quota_reached"
                                    logger.info(f"已达到目标用户数 {saved_customer_count}/{lead_quota}，停止继续抓取新视频。")
                                    break
                                
                                try:
                                    video_meta = next(search_stream)
                                except StopIteration:
                                    search_stream = None
                                    break
                                except Exception as e:
                                    logger.warning(f"获取下一个视频失败: {e}")
                                    search_stream = None
                                    break

                                normalized_video_meta = _normalize_search_video_meta(video_meta)
                                url = str(normalized_video_meta.get("url", "") or "").strip()
                                aweme_id = _extract_search_video_aweme_id(normalized_video_meta)
                                if not url or not aweme_id or aweme_id in seen_aweme_ids:
                                    if not url or not aweme_id:
                                        logger.warning(f"搜索结果缺少有效视频ID或详情链接，已丢弃: {video_meta}")
                                    continue
                                seen_aweme_ids.add(aweme_id)
                                discovered_count += 1
                                if skip_existing_videos:
                                    status = db.get_crawled_video_status(platform, aweme_id) if db else None
                                    if not _should_enqueue_search_video_candidate(skip_existing_videos, status):
                                        skipped_count += 1
                                        logger.info(
                                            f"跳过历史已获取视频: {url} "
                                            f"(status={status}, 已跳过: {skipped_count})"
                                        )
                                        _update_search_runtime(
                                            f"Searching: {keyword}",
                                            (
                                                f"第 {search_passes} 轮已发现 {discovered_count} 个相关视频，"
                                                f"待处理 {len(pending_video_queue)} 个，"
                                                f"跳过已获取 {skipped_count} 个，"
                                                f"当前已入库用户 {saved_customer_count}/{lead_quota}..."
                                            ),
                                        )
                                        continue
                                pass_new_video_count += 1
                                _record_video_discovery_for_crawl(db, platform, keyword, normalized_video_meta, aweme_id)
                                logger.info(
                                    "搜索待处理队列诊断: "
                                    f"aweme_id={aweme_id}, "
                                    f"content_type={normalized_video_meta.get('content_type', '')}, "
                                    f"canonical_url={url}, "
                                    f"raw_video_url={str((video_meta or {}).get('video_url', '') or '').strip()}, "
                                    f"raw_url={str((video_meta or {}).get('url', '') or '').strip()}"
                                )
                                pending_video_queue.append({
                                    "aweme_id": aweme_id,
                                    "url": url,
                                })
                                _update_search_runtime(
                                    f"Searching: {keyword}",
                                    (
                                        f"第 {search_passes} 轮已发现 {discovered_count} 个相关视频，"
                                        f"待处理队列 {len(pending_video_queue)} 个，"
                                        f"跳过历史 {skipped_count} 个，"
                                        f"当前已入库用户 {saved_customer_count}/{lead_quota}..."
                                    ),
                                )
                            
                            if not is_streaming_safe:
                                search_stream = None

                            if quota_reached or _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                if not search_summary.get("stop_reason"):
                                    search_summary["stop_reason"] = "stopped_by_request"
                                break
                            if pending_video_queue and _is_search_stop_requested(stop_event, task_stop_event, crawler):
                                search_summary["stop_reason"] = "stopped_by_request"
                                break

                            if pass_new_video_count <= 0:
                                empty_search_passes += 1
                                if _should_stop_search_due_to_supply_exhaustion(
                                    empty_search_passes,
                                    len(pending_video_queue),
                                ):
                                    search_summary["stop_reason"] = "insufficient_video_supply"
                                    logger.warning(
                                        f"关键词搜索连续 {empty_search_passes} 轮未发现新的可处理视频，"
                                        "且待处理队列为空，按供给不足结束任务。"
                                    )
                                    _update_search_runtime(
                                        f"Searching: {keyword}",
                                        (
                                            f"连续 {empty_search_passes} 轮没有新的可处理视频，"
                                            f"当前已入库 {saved_customer_count}/{lead_quota}，"
                                            "判断为供给不足，准备结束任务。"
                                        ),
                                    )
                                    break
                                wait_interval, wait_message = _resolve_search_retry_interval(
                                    empty_search_passes,
                                    skip_existing_videos=skip_existing_videos,
                                )
                                logger.info(f"第 {search_passes} 轮未发现新的可处理视频。{wait_message}")
                                _update_search_runtime(
                                    f"Searching: {keyword}",
                                    (
                                        f"第 {search_passes} 轮没有新的可处理视频，"
                                        f"当前已入库 {saved_customer_count}/{lead_quota}，"
                                        f"待处理队列 {len(pending_video_queue)} 个，"
                                        f"{wait_interval:.1f} 秒后继续自动补抓..."
                                    ),
                                )
                                if not _sleep_with_task_stop(wait_interval):
                                    search_summary["stop_reason"] = "stopped_by_request"
                                    break
                                continue

                            empty_search_passes = 0
                            logger.info(
                                f"第 {search_passes} 轮新增 {pass_new_video_count} 个待处理视频，"
                                f"当前待处理队列 {len(pending_video_queue)} 个，开始继续消费。"
                            )
                            continue

                        queue_cleanup_after_finish = {"deleted_count": 0, "deleted_aweme_ids": [], "deleted_status_counts": {}}
                        if cleanup_unfinished_videos:
                            queue_cleanup_after_finish = _cleanup_search_unfinished_videos(
                                db,
                                platform,
                                keyword,
                                phase="task_finish",
                            )
                        search_summary["queue_cleanup_after_finish"] = dict(queue_cleanup_after_finish)

                        if search_summary.get("stop_reason") == "insufficient_video_supply":
                            final_status = "supply_exhausted"
                        else:
                            final_status = "completed" if quota_reached else "stopped"
                        search_summary["status"] = final_status
                        search_summary["videos_discovered"] = discovered_count
                        search_summary["videos_processed"] = processed_count
                        search_summary["saved_customers"] = saved_customer_count
                        search_summary["quota_reached"] = quota_reached
                        search_summary["remaining_quota"] = max(lead_quota - saved_customer_count, 0)
                        if not search_summary.get("stop_reason"):
                            search_summary["stop_reason"] = "lead_quota_reached" if quota_reached else "stopped_by_request"
                        search_summary["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        _update_shared_state(
                            current_task="Idle",
                            progress={
                                "total": 0,
                                "current": 0,
                                "detail": (
                                    "搜索任务因供给不足结束"
                                    if final_status == "supply_exhausted"
                                    else
                                    "搜索任务已严格达到目标用户数"
                                    if quota_reached
                                    else "搜索任务已停止"
                                ),
                            },
                            busy=False,
                            last_search_summary=dict(search_summary),
                            search_resume_state=None,
                        )
                        result_queue.put(
                            _build_ipc_message(
                                "search_result",
                                "crawler",
                                {
                                    "keyword": keyword,
                                    "video_count": discovered_count,
                                    "processed_count": processed_count,
                                    "saved_customers": saved_customer_count,
                                    "comments_crawled": int(search_summary.get("comments_crawled", 0) or 0),
                                    "effective_count": saved_customer_count,
                                    "target_effective_count": lead_quota,
                                    "lead_quota": lead_quota,
                                    "status": final_status,
                                    "quota_reached": quota_reached,
                                    "stop_reason": search_summary.get("stop_reason", ""),
                                    "remaining_quota": max(lead_quota - saved_customer_count, 0),
                                    "current_aweme_id": current_aweme_id,
                                    "pending_aweme_ids": _summarize_pending_aweme_ids(pending_video_queue),
                                    "pending_video_count": len(pending_video_queue),
                                    "search_passes": int(search_summary.get("search_passes", 0) or 0),
                                    "empty_search_passes": int(search_summary.get("empty_search_passes", 0) or 0),
                                    "search_response_batches": int(search_summary.get("search_response_batches", 0) or 0),
                                    "pending_search_batches": int(search_summary.get("pending_search_batches", 0) or 0),
                                    "peak_pending_search_batches": int(search_summary.get("peak_pending_search_batches", 0) or 0),
                                    "dropped_search_batches_count": int(search_summary.get("dropped_search_batches_count", 0) or 0),
                                    "latest_search_batch_id": int(search_summary.get("latest_search_batch_id", 0) or 0),
                                    "latest_search_batch_url": str(search_summary.get("latest_search_batch_url", "") or ""),
                                    "partial_video_count": int(search_summary.get("partial_video_count", 0) or 0),
                                    "risk_video_count": int(search_summary.get("risk_video_count", 0) or 0),
                                    "failed_video_count": failed_video_count,
                                    "queue_cleanup_before_start": dict(search_summary.get("queue_cleanup_before_start") or {}),
                                    "queue_cleanup_after_finish": dict(search_summary.get("queue_cleanup_after_finish") or {}),
                                    "auto_reply_enabled": auto_reply_enabled,
                                    "auto_reply_attempted": int(search_summary.get("auto_reply_attempted", 0) or 0),
                                    "auto_reply_success": int(search_summary.get("auto_reply_success", 0) or 0),
                                    "auto_reply_failed": int(search_summary.get("auto_reply_failed", 0) or 0),
                                    "auto_reply_failure_reasons": dict(search_summary.get("auto_reply_failure_reasons") or {}),
                                    "auto_reply_failure_samples": list(search_summary.get("auto_reply_failure_samples") or []),
                                    "auto_reply_diagnostics": dict(search_summary.get("auto_reply_diagnostics") or {}),
                                    "last_video_result": search_summary.get("last_video_result"),
                                },
                            )
                        )

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
                        result_queue.put(_build_ipc_message("error", "crawler", {"message": "浏览器未初始化"}))

                elif cmd_type == "interact":
                    task_stop_event.clear()
                    login_transfer = _extract_login_transfer(cmd_data)
                    _init_components(**login_transfer)
                    if crawler:
                        uids = cmd_data.get("uids", [])
                        content = cmd_data.get("content", "")
                        comments = cmd_data.get("comments")
                        platform = cmd_data.get("platform", "douyin")
                        total = len(uids)
                        comment_pool = _normalize_comment_pool(comments, content)

                        if not comment_pool:
                            result_queue.put(
                                _build_ipc_message("error", "crawler", {"message": "未提供可用的评论内容"})
                            )
                            _set_idle_state("互动任务未启动：未提供可用评论内容")
                            continue

                        _update_shared_state(
                            current_task=f"Interacting: {total} users",
                            progress={
                                "total": total,
                                "current": 0,
                                "detail": f"正在准备处理 {total} 个客户，评论模板 {len(comment_pool)} 条...",
                            },
                            busy=True,
                        )

                        interact_result = _run_comment_interactions(
                            target_uids=uids,
                            comment_pool=comment_pool,
                            platform=platform,
                            progress_total=total,
                            progress_current=0,
                            current_task_label=f"Interacting: {total} users",
                            progress_prefix="",
                            source="one_click_interact",
                        )
                        success_count = int(interact_result.get("success", 0) or 0)
                        limit_reached_reason = str(interact_result.get("limit_reached_reason", "") or "")

                        if limit_reached_reason:
                            final_status = "limit_reached"
                            final_detail = f"{limit_reached_reason} 已停止当前一键互动任务。"
                        elif _should_stop_current_task() or interact_result.get("stopped"):
                            final_status = "stopped"
                            final_detail = "互动任务已停止"
                        else:
                            final_status = "completed"
                            final_detail = f"互动任务完成 (成功: {success_count})"

                        _set_idle_state(final_detail)
                        result_queue.put(
                            _build_ipc_message(
                                "interact_result",
                                "crawler",
                                {
                                    "status": final_status,
                                    "success_count": success_count,
                                    "message": final_detail,
                                },
                            )
                        )
                    else:
                        result_queue.put(_build_ipc_message("error", "crawler", {"message": "浏览器未初始化"}))

                elif cmd_type == "send_messages":
                    task_stop_event.clear()
                    login_transfer = _extract_login_transfer(cmd_data)
                    _init_components(**login_transfer)
                    if sender:
                        account_scope = _refresh_current_login_account_scope(force_refresh=True)
                        message = cmd_data.get("message", "")
                        messages = cmd_data.get("messages")
                        max_count = cmd_data.get("max_count", 10)
                        pending_customers = db.get_pending_customers("douyin", limit=max_count) if db else []
                        _update_shared_state(
                            current_task="Sending messages",
                            progress={
                                "total": len(pending_customers),
                                "current": 0,
                                "detail": (
                                    "正在发送私信..."
                                    if not account_scope.get("account_id")
                                    else f"正在发送私信... 当前账号 {account_scope.get('account_id')}"
                                ),
                            },
                            busy=True,
                        )

                        send_summary = sender.process_pending_customers(
                            message,
                            max_count,
                            stop_check=(lambda: bool(task_stop_event.is_set() or (crawler and crawler.is_stopped()))),
                            message_list=messages,
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
                        result_queue.put(
                            _build_ipc_message(
                                "send_result",
                                "crawler",
                                {
                                    "status": send_status,
                                    "message": message,
                                    "messages": list(messages) if isinstance(messages, list) else None,
                                    "max_count": int(max_count or 0),
                                    **send_summary,
                                },
                            )
                        )
                    else:
                        result_queue.put(_build_ipc_message("error", "crawler", {"message": "浏览器未初始化"}))

                elif cmd_type == "stop_task":
                    task_stop_event.set()
                    progress = dict(
                        crawler_runtime_state.get("progress", {"total": 0, "current": 0, "detail": ""})
                        or {"total": 0, "current": 0, "detail": ""}
                    )
                    progress["detail"] = "正在停止任务..."
                    _update_shared_state(progress=progress, busy=True)
                    result_queue.put(_build_ipc_message("task_stopped", "crawler", {}))

                elif cmd_type == "shutdown":
                    break

            except Exception as e:
                if "Empty" not in str(type(e).__name__):
                    logger.debug(f"爬取命令队列读取异常: {e}")

            _update_shared_state(last_heartbeat=int(time.time()))
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"爬取进程错误: {e}")
            _write_crawler_bootstrap_log(f"crawler main loop error: {e}\n{traceback.format_exc()}")
            stop_event.wait(1)

    heartbeat_stop_event.set()
    try:
        heartbeat_thread.join(timeout=1.0)
    except Exception:
        pass

    _cleanup_components()
    logger.info("爬取/私信进程退出")
    _write_crawler_bootstrap_log("crawler process exit")
