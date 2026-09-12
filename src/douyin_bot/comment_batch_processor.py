from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CommentBatchDecision:
    comment_data: dict
    comment_id: str
    is_time_filtered: bool
    should_skip_historical: bool
    is_target: bool
    customer_data: dict
    customer_comment_link: dict
    matched_comment_target: dict


def is_comment_time_filtered(
    *,
    comment_ts: int,
    requested_start_ts: int,
    requested_end_ts: int,
) -> bool:
    if requested_end_ts and comment_ts and comment_ts > requested_end_ts:
        return True
    if requested_start_ts and comment_ts and comment_ts < requested_start_ts:
        return True
    return False


def build_customer_comment_link(
    *,
    platform: str,
    aweme_id: str,
    comment_data: dict,
    search_task_id: str,
) -> dict:
    return {
        "platform": platform,
        "sec_uid": comment_data.get("sec_uid", ""),
        "aweme_id": aweme_id,
        "comment_id": comment_data.get("cid", ""),
        "matched_keyword": comment_data.get("matched_keyword", ""),
        "search_task_id": search_task_id,
    }


def evaluate_comment_batch_item(
    *,
    comment_data: dict,
    platform: str,
    aweme_id: str,
    video_url: str,
    search_task_id: str,
    requested_start_ts: int,
    requested_end_ts: int,
    skip_crawled: bool,
    has_crawled_comment: Callable[[str, str, str], bool],
    build_customer_data: Callable[[dict], dict],
    build_matched_comment_target: Callable[..., dict],
) -> CommentBatchDecision:
    comment_id = str(comment_data.get("cid", "") or "").strip()
    comment_ts = int(comment_data.get("create_time", 0) or 0)
    time_filtered = is_comment_time_filtered(
        comment_ts=comment_ts,
        requested_start_ts=int(requested_start_ts or 0),
        requested_end_ts=int(requested_end_ts or 0),
    )
    historical = bool(
        skip_crawled
        and comment_id
        and has_crawled_comment(platform, aweme_id, comment_id)
    )
    is_target = bool(comment_data.get("is_target")) and not time_filtered and not historical

    customer_data = {}
    customer_link = {}
    matched_target = {}
    if is_target:
        customer_data = dict(build_customer_data(comment_data) or {})
        if search_task_id:
            customer_data["first_crawl_task_id"] = search_task_id
        customer_link = build_customer_comment_link(
            platform=platform,
            aweme_id=aweme_id,
            comment_data=comment_data,
            search_task_id=search_task_id,
        )
        matched_target = dict(
            build_matched_comment_target(
                platform=platform,
                aweme_id=aweme_id,
                video_url=video_url,
                comment_data=comment_data,
            )
            or {}
        )

    return CommentBatchDecision(
        comment_data=dict(comment_data or {}),
        comment_id=comment_id,
        is_time_filtered=time_filtered,
        should_skip_historical=historical,
        is_target=is_target,
        customer_data=customer_data,
        customer_comment_link=customer_link,
        matched_comment_target=matched_target,
    )
