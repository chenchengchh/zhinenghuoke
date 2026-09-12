from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping


def _clean_list(values: Iterable[Any] | None) -> list[str]:
    cleaned: list[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text:
            cleaned.append(text)
    return cleaned


def build_search_command_payload(
    *,
    keyword: str,
    lead_quota: int,
    max_videos: int,
    comment_keywords: str | None = None,
    auto_reply_enabled: bool,
    reply_quota: int,
    reply_templates: Iterable[Any] | None,
    platform: str,
    comment_time_preset: str | None = None,
    comment_time_start: str | None,
    comment_time_end: str | None,
    skip_crawled: bool,
    skip_existing_videos: bool,
    crawl_priority: str,
    worker_threads: int,
    reply_diagnostic_mode: bool,
    login_transfer: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "keyword": str(keyword or "").strip(),
        "lead_quota": int(lead_quota or 0),
        "max_videos": int(max_videos or 0),
        "comment_keywords": str(comment_keywords or "").strip(),
        "auto_reply_enabled": bool(auto_reply_enabled),
        "reply_quota": int(reply_quota or 0) if auto_reply_enabled else 0,
        "reply_templates": _clean_list(reply_templates),
        "auto_comment_enabled": bool(auto_reply_enabled),
        "comment_templates": _clean_list(reply_templates),
        "platform": str(platform or "").strip(),
        "comment_time_preset": str(comment_time_preset or "").strip(),
        "comment_time_start": comment_time_start,
        "comment_time_end": comment_time_end,
        "skip_crawled": bool(skip_crawled),
        "skip_existing_videos": bool(skip_existing_videos),
        "cleanup_unfinished_videos": True,
        "crawl_priority": str(crawl_priority or "").strip(),
        "worker_threads": int(worker_threads or 0),
        "reply_diagnostic_mode": bool(reply_diagnostic_mode),
        **dict(login_transfer or {}),
    }


def build_send_messages_payload(
    *,
    message: str,
    messages: Iterable[Any] | None,
    max_count: int,
    login_transfer: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "message": str(message or "").strip(),
        "messages": _clean_list(messages),
        "max_count": int(max_count or 0),
        **dict(login_transfer or {}),
    }
