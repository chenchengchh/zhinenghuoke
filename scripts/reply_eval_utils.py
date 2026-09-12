from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


def read_cases(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("cases"), list):
            return payload["cases"]
        if isinstance(payload.get("items"), list):
            return payload["items"]
    raise ValueError(f"无法识别 replay 数据格式: {path}")


def get_nested_value(payload: Any, dotted_path: str) -> Any:
    current = payload
    for segment in str(dotted_path or "").split("."):
        part = segment.strip()
        if not part:
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _normalize_tokens(tokens: Sequence[Any]) -> List[str]:
    return [str(item or "").strip() for item in tokens if str(item or "").strip()]


def _contains_subsequence(sequence: Sequence[str], expected_subsequence: Sequence[str]) -> bool:
    if not expected_subsequence:
        return True
    cursor = 0
    for item in sequence:
        if item == expected_subsequence[cursor]:
            cursor += 1
            if cursor >= len(expected_subsequence):
                return True
    return False


def evaluate_case(
    case: Dict[str, Any],
    *,
    default_match_fields: Iterable[str] = ("action", "reason_code"),
    reply_field: str = "reply",
) -> Dict[str, Any]:
    expected = case.get("expected", {}) or {}
    actual = case.get("actual", {}) or {}
    checks: Dict[str, bool] = {}

    for field in default_match_fields:
        if field in expected:
            checks[field] = get_nested_value(actual, field) == expected.get(field)

    for dotted_path, expected_value in (expected.get("match_fields") or {}).items():
        checks[f"field:{dotted_path}"] = get_nested_value(actual, dotted_path) == expected_value

    expected_reply_contains = _normalize_tokens(expected.get("reply_contains", []) or [])
    if expected_reply_contains:
        actual_reply = str(get_nested_value(actual, reply_field) or "")
        checks["reply_contains"] = all(token in actual_reply for token in expected_reply_contains)

    expected_reply_not_contains = _normalize_tokens(expected.get("reply_not_contains", []) or [])
    if expected_reply_not_contains:
        actual_reply = str(get_nested_value(actual, reply_field) or "")
        checks["reply_not_contains"] = all(token not in actual_reply for token in expected_reply_not_contains)

    expected_event_sequence = _normalize_tokens(expected.get("event_sequence_contains", []) or [])
    if expected_event_sequence:
        actual_event_sequence = _normalize_tokens(actual.get("event_sequence", []) or [])
        checks["event_sequence_contains"] = _contains_subsequence(actual_event_sequence, expected_event_sequence)

    if not checks:
        checks["configured_checks"] = False

    failed_checks = [name for name, passed in checks.items() if not passed]
    return {
        "case_id": str(case.get("case_id") or "unknown"),
        "query": str(case.get("query") or ""),
        "category": str(case.get("category") or "uncategorized"),
        "tags": _normalize_tokens(case.get("tags", []) or []),
        "passed": not failed_checks,
        "checks": checks,
        "failed_checks": failed_checks,
    }


def build_report(
    cases: List[Dict[str, Any]],
    *,
    format_name: str,
) -> Dict[str, Any]:
    results = [evaluate_case(case) for case in cases]
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    categories: Dict[str, Dict[str, Any]] = {}

    for result in results:
        category = str(result.get("category") or "uncategorized")
        category_stats = categories.setdefault(category, {"total": 0, "passed": 0})
        category_stats["total"] += 1
        if result.get("passed"):
            category_stats["passed"] += 1

    for category_stats in categories.values():
        category_total = int(category_stats.get("total", 0) or 0)
        category_passed = int(category_stats.get("passed", 0) or 0)
        category_stats["pass_rate"] = round(category_passed / category_total, 4) if category_total else 0.0

    return {
        "format": format_name,
        "total": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "categories": categories,
        "results": results,
    }
