from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List


def persist_crawled_comment_records(
    *,
    db: Any,
    session: dict,
    platform: str,
    aweme_id: str,
    video_url: str,
    crawled_comment_records: Iterable[dict] | None,
) -> Dict[str, int]:
    records = list(crawled_comment_records or [])
    if not db or not records:
        return {"history_recorded_count": 0, "fact_recorded_count": 0}

    return {
        "history_recorded_count": int(
            db.record_crawled_comments(
                platform=platform,
                aweme_id=aweme_id,
                video_url=video_url,
                comments=records,
                video_name=session.get("video_name", ""),
                author_name=session.get("author_name", ""),
            )
            or 0
        ),
        "fact_recorded_count": int(
            db.record_comment_facts(
                platform=platform,
                aweme_id=aweme_id,
                video_url=video_url,
                comments=records,
                capture_session_id=session.get("session_id", ""),
                video_title=session.get("video_name", ""),
                author_name=session.get("author_name", ""),
            )
            or 0
        ),
    }


def persist_matched_comment_batch(
    *,
    db: Any,
    session: dict,
    matched_customers: Iterable[dict] | None,
    customer_comment_links: Iterable[dict] | None,
    matched_comment_targets: Iterable[dict] | None,
    persist_matched_customers_with_quota: Callable[[dict, List[dict], List[dict]], List[dict]],
    append_linked_reply_targets: Callable[[dict, List[dict], List[dict]], None],
) -> Dict[str, Any]:
    customers = list(matched_customers or [])
    comment_links = list(customer_comment_links or [])
    reply_targets = list(matched_comment_targets or [])
    if not db or not customers:
        return {"customer_linked_count": 0, "linked_customer_comment_links": []}

    linked_customer_comment_links = list(
        persist_matched_customers_with_quota(session, customers, comment_links) or []
    )
    if not linked_customer_comment_links:
        return {"customer_linked_count": 0, "linked_customer_comment_links": []}

    customer_linked_count = int(db.link_customer_comments(linked_customer_comment_links) or 0)
    append_linked_reply_targets(session, linked_customer_comment_links, reply_targets)
    return {
        "customer_linked_count": customer_linked_count,
        "linked_customer_comment_links": linked_customer_comment_links,
    }
