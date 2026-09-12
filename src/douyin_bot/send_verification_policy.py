from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, List


NormalizeFn = Callable[[Any], str]


@dataclass(frozen=True)
class SendVerificationDecision:
    success: bool
    reason_code: str


def normalize_tail_entries(
    items: Iterable[Any],
    *,
    normalize_text: NormalizeFn,
    extract_text: Callable[[Any], Any] | None = None,
    limit: int = 8,
) -> List[str]:
    normalized: List[str] = []
    extractor = extract_text or (lambda item: item)
    for item in items or []:
        text = normalize_text(extractor(item))
        if text:
            normalized.append(text)
    if limit <= 0:
        return normalized
    return normalized[-limit:]


def message_tail_has_advanced(
    *,
    before_items: Iterable[Any],
    after_items: Iterable[Any],
    original_content: str,
    normalize_text: NormalizeFn,
    extract_text: Callable[[Any], Any] | None = None,
    content_prefix: int = 30,
    limit: int = 8,
) -> bool:
    before_tail = normalize_tail_entries(
        before_items,
        normalize_text=normalize_text,
        extract_text=extract_text,
        limit=limit,
    )
    after_tail = normalize_tail_entries(
        after_items,
        normalize_text=normalize_text,
        extract_text=extract_text,
        limit=limit,
    )
    if not after_tail:
        return False

    normalized_content = normalize_text(original_content)
    needle = normalized_content[:content_prefix] if normalized_content and content_prefix > 0 else normalized_content
    if needle:
        before_contains = any(needle in item for item in before_tail)
        after_contains = any(needle in item for item in after_tail)
        if after_contains and not before_contains:
            return True

    if len(after_tail) > len(before_tail):
        return True
    return tuple(after_tail[-3:]) != tuple(before_tail[-3:])


def collection_has_advanced_match(
    *,
    before_items: Iterable[Any],
    after_items: Iterable[Any],
    original_content: str,
    normalize_text: NormalizeFn,
    extract_text: Callable[[Any], Any] | None = None,
    limit: int = 0,
) -> bool:
    before_values = normalize_tail_entries(
        before_items,
        normalize_text=normalize_text,
        extract_text=extract_text,
        limit=limit,
    )
    after_values = normalize_tail_entries(
        after_items,
        normalize_text=normalize_text,
        extract_text=extract_text,
        limit=limit,
    )
    if not after_values:
        return False

    normalized_content = normalize_text(original_content)
    if normalized_content:
        before_contains = any(normalized_content in item for item in before_values)
        after_contains = any(normalized_content in item for item in after_values)
        if after_contains and not before_contains:
            return True

    before_set = set(before_values)
    for item in after_values:
        if item not in before_set and (not normalized_content or normalized_content in item):
            return True
    return False


def decide_send_confirmation(
    *,
    has_explicit_failure: bool = False,
    input_cleared: bool = False,
    tail_advanced: bool = False,
    message_seen_in_tail: bool = False,
    platform_outbound_seen: bool = False,
) -> SendVerificationDecision:
    if has_explicit_failure:
        return SendVerificationDecision(success=False, reason_code="explicit_failure_marker")
    if message_seen_in_tail:
        return SendVerificationDecision(success=True, reason_code="message_seen_in_tail")
    if platform_outbound_seen and (tail_advanced or input_cleared):
        return SendVerificationDecision(success=True, reason_code="platform_outbound_confirmed")
    if input_cleared and tail_advanced:
        return SendVerificationDecision(success=True, reason_code="input_cleared_and_tail_advanced")
    if platform_outbound_seen:
        return SendVerificationDecision(success=True, reason_code="platform_outbound_seen")
    if input_cleared:
        return SendVerificationDecision(success=False, reason_code="input_cleared_only")
    if tail_advanced:
        return SendVerificationDecision(success=True, reason_code="tail_advanced")
    return SendVerificationDecision(success=False, reason_code="pending_or_failed")
