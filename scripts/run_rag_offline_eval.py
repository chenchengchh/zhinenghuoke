from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def _read_dataset(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            return payload["items"]
        if isinstance(payload.get("samples"), list):
            return payload["samples"]
    raise ValueError(f"无法识别的数据集格式: {path}")


def _distribution(values: Iterable[str]) -> Dict[str, int]:
    output: Dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        output[key] = output.get(key, 0) + 1
    return output


def _normalize_expected_hit_ids(item: Dict[str, Any]) -> List[str]:
    candidates = []
    for path in (
        ("expected", "expected_hit_ids"),
        ("retrieval", "expected_hit_ids"),
        ("metadata", "expected_hit_ids"),
    ):
        payload: Any = item
        for key in path:
            payload = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(payload, list):
            candidates.extend(str(value or "").strip() for value in payload)
    normalized: List[str] = []
    for value in candidates:
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_ranked_hit_ids(item: Dict[str, Any]) -> List[str]:
    retrieval = item.get("retrieval", {}) or {}
    top_hits = retrieval.get("top_hits", []) or []
    ranked: List[str] = []
    for hit in top_hits:
        if not isinstance(hit, dict):
            continue
        for key in ("id", "doc_id", "question"):
            value = str(hit.get(key) or "").strip()
            if value:
                ranked.append(value)
                break
    return ranked


def _compute_retrieval_metrics(dataset: List[Dict[str, Any]]) -> Dict[str, float]:
    labeled_items = []
    recall_at_1_hits = 0
    recall_at_3_hits = 0
    recall_at_5_hits = 0
    reciprocal_ranks: List[float] = []
    top1_hits = 0
    for item in dataset:
        expected_ids = _normalize_expected_hit_ids(item)
        if not expected_ids:
            continue
        ranked_ids = _normalize_ranked_hit_ids(item)
        labeled_items.append(item)
        expected_set = set(expected_ids)
        top1_hits += 1 if ranked_ids[:1] and ranked_ids[0] in expected_set else 0
        recall_at_1_hits += 1 if any(hit in expected_set for hit in ranked_ids[:1]) else 0
        recall_at_3_hits += 1 if any(hit in expected_set for hit in ranked_ids[:3]) else 0
        recall_at_5_hits += 1 if any(hit in expected_set for hit in ranked_ids[:5]) else 0
        reciprocal_rank = 0.0
        for index, hit in enumerate(ranked_ids, start=1):
            if hit in expected_set:
                reciprocal_rank = 1.0 / float(index)
                break
        reciprocal_ranks.append(reciprocal_rank)
    total = len(labeled_items)
    if total == 0:
        return {
            "labeled_retrieval_total": 0,
            "recall_at_1": 0.0,
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "mrr": 0.0,
            "top1_hit_rate": 0.0,
        }
    return {
        "labeled_retrieval_total": total,
        "recall_at_1": round(recall_at_1_hits / total, 4),
        "recall_at_3": round(recall_at_3_hits / total, 4),
        "recall_at_5": round(recall_at_5_hits / total, 4),
        "mrr": round(sum(reciprocal_ranks) / total, 4),
        "top1_hit_rate": round(top1_hits / total, 4),
    }


def _build_report(dataset: List[Dict[str, Any]]) -> Dict[str, Any]:
    if dataset and "expected" in dataset[0]:
        total = len(dataset)
        reason_codes = []
        reply_sources = []
        intents = []
        contexts_total = 0
        top_hits_total = 0
        empty_context_count = 0
        need_human_count = 0
        no_answer_count = 0
        label_fields = ("grounded", "helpful", "correct_route")
        labeled_counts = {field: 0 for field in label_fields}

        for item in dataset:
            expected = item.get("expected", {}) or {}
            retrieval = item.get("retrieval", {}) or {}
            metadata = item.get("metadata", {}) or {}
            labels = item.get("labels", {}) or {}

            reason_codes.append(str(expected.get("reason_code") or "unknown"))
            reply_sources.append(str(expected.get("reply_source") or "unknown"))
            intents.append(str(metadata.get("intent") or "unknown"))

            context_count = int(retrieval.get("context_count") or len(retrieval.get("contexts", []) or []))
            top_hit_count = int(retrieval.get("top_hit_count") or len(retrieval.get("top_hits", []) or []))
            contexts_total += context_count
            top_hits_total += top_hit_count
            empty_context_count += 1 if context_count == 0 else 0
            need_human_count += 1 if expected.get("need_human") else 0
            no_answer_count += 1 if str(expected.get("reason_code") or "").startswith("no_answer") else 0

            for field in label_fields:
                if labels.get(field) is not None:
                    labeled_counts[field] += 1

        return {
            "format": "structured_rag_dataset",
            "total": total,
            "need_human_rate": round(need_human_count / total, 4) if total else 0.0,
            "no_answer_rate": round(no_answer_count / total, 4) if total else 0.0,
            "empty_context_rate": round(empty_context_count / total, 4) if total else 0.0,
            "avg_context_count": round(contexts_total / total, 3) if total else 0.0,
            "avg_top_hit_count": round(top_hits_total / total, 3) if total else 0.0,
            "reason_code_distribution": _distribution(reason_codes),
            "reply_source_distribution": _distribution(reply_sources),
            "intent_distribution": _distribution(intents),
            "label_coverage": {
                field: round(count / total, 4) if total else 0.0
                for field, count in labeled_counts.items()
            },
            "retrieval_metrics": _compute_retrieval_metrics(dataset),
        }

    total = len(dataset)
    reason_codes = [str(item.get("reason_code") or "unknown") for item in dataset]
    reply_sources = [str(item.get("reply_source") or "unknown") for item in dataset]
    intents = [str(item.get("intent") or "unknown") for item in dataset]
    context_counts = [len(item.get("contexts", []) or []) for item in dataset]

    return {
        "format": "flat_rag_dataset",
        "total": total,
        "need_human_rate": round(
            sum(1 for item in dataset if item.get("need_human")) / total, 4
        ) if total else 0.0,
        "no_answer_rate": round(
            sum(1 for code in reason_codes if code.startswith("no_answer")) / total, 4
        ) if total else 0.0,
        "empty_context_rate": round(sum(1 for count in context_counts if count == 0) / total, 4) if total else 0.0,
        "avg_context_count": round(sum(context_counts) / total, 3) if total else 0.0,
        "reason_code_distribution": _distribution(reason_codes),
        "reply_source_distribution": _distribution(reply_sources),
        "intent_distribution": _distribution(intents),
        "retrieval_metrics": _compute_retrieval_metrics(dataset),
    }


def _build_thresholds(args: argparse.Namespace) -> Dict[str, float]:
    return {
        "max_need_human_rate": float(args.max_need_human_rate),
        "max_no_answer_rate": float(args.max_no_answer_rate),
        "max_empty_context_rate": float(args.max_empty_context_rate),
        "min_avg_context_count": float(args.min_avg_context_count),
        "min_avg_top_hit_count": float(args.min_avg_top_hit_count),
        "min_label_coverage_grounded": float(args.min_label_coverage_grounded),
        "min_label_coverage_helpful": float(args.min_label_coverage_helpful),
        "min_label_coverage_correct_route": float(args.min_label_coverage_correct_route),
    }


def _evaluate_gate(report: Dict[str, Any], thresholds: Dict[str, float]) -> Tuple[bool, List[Dict[str, Any]]]:
    checks: List[Dict[str, Any]] = []

    def add_check(metric: str, actual: float, threshold: float, operator: str) -> None:
        passed = actual <= threshold if operator == "<=" else actual >= threshold
        checks.append(
            {
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
                "actual": round(float(actual), 4),
                "passed": passed,
            }
        )

    add_check("need_human_rate", report.get("need_human_rate", 0.0), thresholds["max_need_human_rate"], "<=")
    add_check("no_answer_rate", report.get("no_answer_rate", 0.0), thresholds["max_no_answer_rate"], "<=")
    add_check("empty_context_rate", report.get("empty_context_rate", 0.0), thresholds["max_empty_context_rate"], "<=")
    add_check("avg_context_count", report.get("avg_context_count", 0.0), thresholds["min_avg_context_count"], ">=")
    add_check("avg_top_hit_count", report.get("avg_top_hit_count", 0.0), thresholds["min_avg_top_hit_count"], ">=")

    label_coverage = report.get("label_coverage", {}) or {}
    add_check(
        "label_coverage.grounded",
        float(label_coverage.get("grounded", 0.0) or 0.0),
        thresholds["min_label_coverage_grounded"],
        ">=",
    )
    add_check(
        "label_coverage.helpful",
        float(label_coverage.get("helpful", 0.0) or 0.0),
        thresholds["min_label_coverage_helpful"],
        ">=",
    )
    add_check(
        "label_coverage.correct_route",
        float(label_coverage.get("correct_route", 0.0) or 0.0),
        thresholds["min_label_coverage_correct_route"],
        ">=",
    )
    return all(item["passed"] for item in checks), checks


def _write_markdown(path: Path, report: Dict[str, Any]):
    lines = [
        "# RAG Offline Evaluation Report",
        "",
        f"- format: {report.get('format', 'unknown')}",
        f"- total: {report.get('total', 0)}",
        f"- need_human_rate: {report.get('need_human_rate', 0.0)}",
        f"- no_answer_rate: {report.get('no_answer_rate', 0.0)}",
        f"- empty_context_rate: {report.get('empty_context_rate', 0.0)}",
    ]
    if "avg_top_hit_count" in report:
        lines.append(f"- avg_top_hit_count: {report.get('avg_top_hit_count', 0.0)}")
    lines.append(f"- avg_context_count: {report.get('avg_context_count', 0.0)}")
    retrieval_metrics = report.get("retrieval_metrics", {}) or {}
    for name in ("labeled_retrieval_total", "recall_at_1", "recall_at_3", "recall_at_5", "mrr", "top1_hit_rate"):
        if name in retrieval_metrics:
            lines.append(f"- {name}: {retrieval_metrics.get(name)}")
    gate = report.get("gate", {}) or {}
    lines.append(f"- passed: {gate.get('passed', False)}")
    for key in ("reason_code_distribution", "reply_source_distribution", "intent_distribution", "label_coverage"):
        if key in report:
            lines.append("")
            lines.append(f"## {key}")
            for name, value in sorted((report.get(key) or {}).items()):
                lines.append(f"- {name}: {value}")
    if gate:
        lines.append("")
        lines.append("## quality_gate")
        thresholds = report.get("thresholds", {}) or {}
        for name, value in sorted(thresholds.items()):
            lines.append(f"- threshold.{name}: {value}")
        for item in gate.get("checks", []) or []:
            lines.append(
                "- "
                f"{item.get('metric')}: actual={item.get('actual')} "
                f"{item.get('operator')} threshold={item.get('threshold')} "
                f"passed={item.get('passed')}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 RAG 离线评测 runner")
    parser.add_argument("dataset", type=Path, help="dataset.jsonl 或 dataset.pretty.json 路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="报告输出目录，默认写回数据集所在目录",
    )
    parser.add_argument("--max-need-human-rate", type=float, default=1.0, help="need_human_rate 上限")
    parser.add_argument("--max-no-answer-rate", type=float, default=1.0, help="no_answer_rate 上限")
    parser.add_argument("--max-empty-context-rate", type=float, default=1.0, help="empty_context_rate 上限")
    parser.add_argument("--min-avg-context-count", type=float, default=0.0, help="avg_context_count 下限")
    parser.add_argument("--min-avg-top-hit-count", type=float, default=0.0, help="avg_top_hit_count 下限")
    parser.add_argument(
        "--min-label-coverage-grounded",
        type=float,
        default=0.0,
        help="label_coverage.grounded 下限",
    )
    parser.add_argument(
        "--min-label-coverage-helpful",
        type=float,
        default=0.0,
        help="label_coverage.helpful 下限",
    )
    parser.add_argument(
        "--min-label-coverage-correct-route",
        type=float,
        default=0.0,
        help="label_coverage.correct_route 下限",
    )
    args = parser.parse_args()

    dataset = _read_dataset(args.dataset)
    report = _build_report(dataset)
    thresholds = _build_thresholds(args)
    passed, checks = _evaluate_gate(report, thresholds)
    report["thresholds"] = thresholds
    report["gate"] = {
        "passed": passed,
        "checks": checks,
    }
    output_dir = args.output_dir or args.dataset.resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)

    report_json = output_dir / "offline_eval_report.json"
    report_md = output_dir / "offline_eval_report.md"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report_md, report)

    print("=" * 72)
    print("RAG 离线评测完成")
    print(f"dataset: {args.dataset}")
    print(f"report : {report_json}")
    print(f"md     : {report_md}")
    print(f"total  : {report.get('total', 0)}")
    print(f"passed : {passed}")
    print("=" * 72)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
