from __future__ import annotations

from typing import Any, Callable


def normalize_reply_target(
    target: dict,
    *,
    infer_detail_kind_from_url: Callable[[str], str],
) -> dict:
    normalized = dict(target or {})

    def _safe_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    comment_id = str(normalized.get("comment_id", "") or "").strip()
    comment_text = str(normalized.get("comment_text", "") or "").strip()
    video_url = str(normalized.get("video_url", "") or "").strip()
    detail_kind = (
        str(normalized.get("detail_kind", "") or "").strip().lower()
        or str(normalized.get("content_type", "") or "").strip().lower()
        or infer_detail_kind_from_url(video_url)
    )
    if detail_kind not in {"video", "note"}:
        detail_kind = "video"

    normalized["comment_id"] = comment_id
    normalized["parent_comment_id"] = ""
    normalized["root_comment_id"] = comment_id
    normalized["comment_text"] = comment_text
    normalized["comment_text_prefix"] = comment_text[:24]
    normalized["comment_text_short_prefix"] = comment_text[:12]
    normalized["comment_level"] = 1
    normalized["detail_kind"] = detail_kind
    normalized["content_type"] = detail_kind
    normalized["unique_id"] = str(normalized.get("unique_id", "") or "").strip()
    normalized["sec_uid"] = str(normalized.get("sec_uid", "") or "").strip()
    normalized["create_time"] = _safe_int(normalized.get("create_time", 0))
    normalized["reply_parent_probe_id"] = comment_id
    normalized["parent_anchor"] = {}
    return normalized


def build_matched_comment_target(
    *,
    platform: str,
    aweme_id: str,
    video_url: str,
    comment_data: dict,
    infer_detail_kind_from_url: Callable[[str], str],
    normalize_target: Callable[[dict], dict],
) -> dict:
    return normalize_target(
        {
            "platform": platform,
            "aweme_id": aweme_id,
            "comment_id": comment_data.get("cid", ""),
            "parent_comment_id": "",
            "root_comment_id": comment_data.get("cid", ""),
            "comment_level": 1,
            "video_url": video_url,
            "detail_kind": infer_detail_kind_from_url(video_url),
            "content_type": infer_detail_kind_from_url(video_url),
            "sec_uid": comment_data.get("sec_uid", ""),
            "unique_id": comment_data.get("unique_id", ""),
            "nickname": comment_data.get("nickname", ""),
            "comment_text": comment_data.get("text", ""),
            "create_time": comment_data.get("create_time", 0),
            "matched_keyword": comment_data.get("matched_keyword", ""),
            "parent_anchor": {},
        }
    )


def append_linked_reply_targets(
    session: dict,
    linked_customer_comment_links: list,
    matched_comment_targets: list,
    *,
    normalize_target: Callable[[dict], dict],
) -> None:
    if not isinstance(session, dict) or not linked_customer_comment_links or not matched_comment_targets:
        return

    linked_target_list = session.setdefault("linked_reply_targets", [])
    target_map = {}
    for item in matched_comment_targets:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id", "") or "").strip()
        comment_id = str(item.get("comment_id", "") or "").strip()
        if aweme_id and comment_id:
            target_map[(aweme_id, comment_id)] = item

    existing_keys = {
        (
            str(item.get("aweme_id", "") or "").strip(),
            str(item.get("comment_id", "") or "").strip(),
        )
        for item in linked_target_list
        if isinstance(item, dict)
    }

    for link_data in linked_customer_comment_links:
        aweme_id = str((link_data or {}).get("aweme_id", "") or "").strip()
        comment_id = str((link_data or {}).get("comment_id", "") or "").strip()
        dedupe_key = (aweme_id, comment_id)
        if dedupe_key in existing_keys:
            continue
        target = target_map.get(dedupe_key)
        if not target:
            continue
        linked_target_list.append(normalize_target(target))
        existing_keys.add(dedupe_key)
