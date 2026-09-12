from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, Iterable


_ID_PARAM_NAMES = (
    "aweme_id",
    "modal_id",
    "item_id",
    "group_id",
    "note_id",
    "video_id",
)

_URL_FIELD_NAMES = (
    "url",
    "video_url",
    "share_url",
    "detail_url",
    "web_url",
    "jump_url",
    "schema",
    "uri",
)

_ID_FIELD_NAMES = (
    "aweme_id",
    "item_id",
    "group_id",
    "note_id",
    "modal_id",
    "video_id",
)

_NOTE_AWEME_TYPES = {"68", "150"}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _is_supported_aweme_id(value: Any) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    if not re.fullmatch(r"[0-9A-Za-z_-]{8,64}", text):
        return False
    return bool(re.search(r"\d", text))


def _extract_aweme_id_from_candidate_value(value: Any) -> str:
    text = _normalize_text(value)
    if not text:
        return ""
    decoded = urllib.parse.unquote(text)
    if _is_supported_aweme_id(decoded):
        return decoded

    for pattern in (
        r"/(?:video|note)/([0-9A-Za-z_-]+)",
        r"/share/(?:video|note)/([0-9A-Za-z_-]+)",
        r"/aweme/detail/([0-9A-Za-z_-]+)",
        r"[?&#](?:aweme_id|modal_id|item_id|group_id|note_id|video_id)=([0-9A-Za-z_-]+)",
        r"aweme_id%3D([0-9A-Za-z_-]+)",
        r"modal_id%3D([0-9A-Za-z_-]+)",
        r"item_id%3D([0-9A-Za-z_-]+)",
        r"group_id%3D([0-9A-Za-z_-]+)",
        r"note_id%3D([0-9A-Za-z_-]+)",
    ):
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            candidate = _normalize_text(match.group(1))
            if _is_supported_aweme_id(candidate):
                return candidate

    parsed = urllib.parse.urlparse(decoded)
    if parsed.query:
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for name in _ID_PARAM_NAMES:
            for candidate in params.get(name, []):
                if _is_supported_aweme_id(candidate):
                    return _normalize_text(candidate)

    path = _normalize_text(parsed.path)
    if path:
        for segment in reversed([part for part in path.split("/") if _normalize_text(part)]):
            if _is_supported_aweme_id(segment):
                return segment

    return ""


def _iter_search_video_candidate_values(video_meta: Any) -> Iterable[Any]:
    if not isinstance(video_meta, dict):
        yield video_meta
        return

    candidate_dicts = [video_meta]
    aweme_info = video_meta.get("aweme_info")
    if isinstance(aweme_info, dict):
        candidate_dicts.append(aweme_info)

    video_info = video_meta.get("video")
    if isinstance(video_info, dict):
        candidate_dicts.append(video_info)

    card_item = video_meta.get("card_item")
    if isinstance(card_item, dict):
        candidate_dicts.append(card_item)
        card_aweme_info = card_item.get("aweme_info")
        if isinstance(card_aweme_info, dict):
            candidate_dicts.append(card_aweme_info)

    for candidate_dict in candidate_dicts:
        for name in _ID_FIELD_NAMES + _URL_FIELD_NAMES:
            value = candidate_dict.get(name)
            if value not in (None, ""):
                yield value
        share_info = candidate_dict.get("share_info")
        if isinstance(share_info, dict):
            for name in ("share_url", "url", "schema"):
                value = share_info.get(name)
                if value not in (None, ""):
                    yield value


def extract_search_video_aweme_id(video_meta: Any) -> str:
    for candidate in _iter_search_video_candidate_values(video_meta):
        resolved = _extract_aweme_id_from_candidate_value(candidate)
        if resolved:
            return resolved
    return ""


def extract_search_video_detail_kind(video_meta: Any) -> str:
    if isinstance(video_meta, dict):
        for candidate_dict in (
            video_meta,
            video_meta.get("aweme_info"),
            video_meta.get("video"),
            (video_meta.get("card_item") or {}).get("aweme_info"),
        ):
            if not isinstance(candidate_dict, dict):
                continue
            content_type = _normalize_text(candidate_dict.get("content_type")).lower()
            if content_type in {"video", "note"}:
                return content_type
            aweme_type = _normalize_text(candidate_dict.get("aweme_type") or candidate_dict.get("awemeType"))
            if aweme_type in _NOTE_AWEME_TYPES:
                return "note"
            if candidate_dict.get("images") or candidate_dict.get("image_infos") or candidate_dict.get("image_post_info"):
                return "note"
            if _normalize_text(candidate_dict.get("note_id")):
                return "note"

    for candidate in _iter_search_video_candidate_values(video_meta):
        raw = _normalize_text(candidate).lower()
        if "/note/" in raw or "note_id=" in raw:
            return "note"
    return "video"


def build_canonical_search_video_url(video_meta: Any, aweme_id: str) -> str:
    normalized_aweme_id = _normalize_text(aweme_id)
    if not _is_supported_aweme_id(normalized_aweme_id):
        return ""
    detail_kind = extract_search_video_detail_kind(video_meta)
    return f"https://www.douyin.com/{detail_kind}/{normalized_aweme_id}"


def normalize_search_video_meta(video_meta: Any) -> Dict[str, Any]:
    if not isinstance(video_meta, dict):
        return {}
    aweme_id = extract_search_video_aweme_id(video_meta)
    if not aweme_id:
        return {}
    canonical_url = build_canonical_search_video_url(video_meta, aweme_id)
    if not canonical_url:
        return {}
    extracted_from_url = extract_search_video_aweme_id({"url": canonical_url})
    if extracted_from_url != aweme_id:
        return {}
    normalized = dict(video_meta)
    normalized["aweme_id"] = aweme_id
    normalized["url"] = canonical_url
    normalized["video_url"] = canonical_url
    normalized["content_type"] = extract_search_video_detail_kind(video_meta)
    return normalized
