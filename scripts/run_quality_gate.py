from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "evaluation" / "quality_gate"
DEFAULT_GENERIC_SCHEMA_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "generic_schema_gate_cases.json"
)
DEFAULT_TOURISM_SCHEMA_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "tourism_schema_gate_cases.json"
)
DEFAULT_THIRD_SCHEMA_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "third_schema_validation_cases.json"
)
DEFAULT_REPLY_REPLAY_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "reply_replay_cases.json"
)
DEFAULT_REPLY_STABILITY_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "reply_stability_cases.json"
)
DEFAULT_REPLY_FAULT_INJECTION_DATASET = (
    REPO_ROOT / "tests" / "fixtures" / "reply_fault_injection_cases.json"
)
DEFAULT_REPLY_OBSERVABILITY_URL = "http://127.0.0.1:8024/api/rag/observability-samples"

GATE_MODE_PRESETS = {
    "dev": {
        "max_need_human_rate": 0.6,
        "max_no_answer_rate": 0.45,
        "max_empty_context_rate": 0.4,
        "min_avg_context_count": 0.35,
        "min_avg_top_hit_count": 0.35,
        "min_label_coverage_grounded": 0.6,
        "min_label_coverage_helpful": 0.6,
        "min_label_coverage_correct_route": 0.6,
        "fixed_max_need_human_rate": 0.45,
        "fixed_max_no_answer_rate": 0.35,
        "fixed_max_empty_context_rate": 0.35,
        "fixed_min_avg_context_count": 0.7,
        "fixed_min_avg_top_hit_count": 0.7,
        "fixed_min_label_coverage_grounded": 0.9,
        "fixed_min_label_coverage_helpful": 0.9,
        "fixed_min_label_coverage_correct_route": 0.9,
        "min_reply_replay_pass_rate": 0.95,
        "reply_replay_baseline_limit": 30,
        "reply_replay_per_category_limit": 8,
        "reply_replay_export_min_total": 8,
        "reply_replay_export_min_baseline_total": 6,
        "reply_replay_export_min_event_sequence_coverage": 0.5,
    },
    "release": {
        "max_need_human_rate": 0.35,
        "max_no_answer_rate": 0.25,
        "max_empty_context_rate": 0.2,
        "min_avg_context_count": 0.55,
        "min_avg_top_hit_count": 0.55,
        "min_label_coverage_grounded": 0.8,
        "min_label_coverage_helpful": 0.8,
        "min_label_coverage_correct_route": 0.8,
        "fixed_max_need_human_rate": 0.25,
        "fixed_max_no_answer_rate": 0.2,
        "fixed_max_empty_context_rate": 0.15,
        "fixed_min_avg_context_count": 0.85,
        "fixed_min_avg_top_hit_count": 0.85,
        "fixed_min_label_coverage_grounded": 1.0,
        "fixed_min_label_coverage_helpful": 1.0,
        "fixed_min_label_coverage_correct_route": 1.0,
        "min_reply_replay_pass_rate": 1.0,
        "reply_replay_baseline_limit": 60,
        "reply_replay_per_category_limit": 15,
        "reply_replay_export_min_total": 20,
        "reply_replay_export_min_baseline_total": 12,
        "reply_replay_export_min_event_sequence_coverage": 0.8,
    },
}


def _run_command(command: List[str]) -> int:
    print(f"\n[RUN] {' '.join(command)}")
    completed = subprocess.run(command, cwd=REPO_ROOT)
    print(f"[EXIT] code={completed.returncode}")
    return int(completed.returncode)


def _build_eval_command(
    args: argparse.Namespace,
    dataset_path: Path,
    output_dir: Path,
    *,
    thresholds: Dict[str, float] | None = None,
) -> List[str]:
    thresholds = thresholds or {
        "max_need_human_rate": args.max_need_human_rate,
        "max_no_answer_rate": args.max_no_answer_rate,
        "max_empty_context_rate": args.max_empty_context_rate,
        "min_avg_context_count": args.min_avg_context_count,
        "min_avg_top_hit_count": args.min_avg_top_hit_count,
        "min_label_coverage_grounded": args.min_label_coverage_grounded,
        "min_label_coverage_helpful": args.min_label_coverage_helpful,
        "min_label_coverage_correct_route": args.min_label_coverage_correct_route,
    }
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_rag_offline_eval.py"),
        str(dataset_path),
        "--output-dir",
        str(output_dir),
        "--max-need-human-rate",
        str(thresholds["max_need_human_rate"]),
        "--max-no-answer-rate",
        str(thresholds["max_no_answer_rate"]),
        "--max-empty-context-rate",
        str(thresholds["max_empty_context_rate"]),
        "--min-avg-context-count",
        str(thresholds["min_avg_context_count"]),
        "--min-avg-top-hit-count",
        str(thresholds["min_avg_top_hit_count"]),
        "--min-label-coverage-grounded",
        str(thresholds["min_label_coverage_grounded"]),
        "--min-label-coverage-helpful",
        str(thresholds["min_label_coverage_helpful"]),
        "--min-label-coverage-correct-route",
        str(thresholds["min_label_coverage_correct_route"]),
    ]
    return command


def _run_offline_eval_stage(
    *,
    summary: Dict[str, Any],
    args: argparse.Namespace,
    stage_name: str,
    dataset_path: Path,
    output_dir: Path,
    artifact_key: str,
    summary_key: str,
    thresholds: Dict[str, float] | None = None,
) -> None:
    offline_eval_command = _build_eval_command(
        args,
        dataset_path,
        output_dir,
        thresholds=thresholds,
    )
    offline_eval_code = _run_command(offline_eval_command)
    summary["stages"].append(
        {
            "name": stage_name,
            "passed": offline_eval_code == 0,
            "exit_code": offline_eval_code,
            "command": offline_eval_command,
            "dataset": str(dataset_path),
            "thresholds": thresholds,
        }
    )
    summary["passed"] = summary["passed"] and offline_eval_code == 0
    report_path = output_dir / "offline_eval_report.json"
    if report_path.exists():
        summary["artifacts"][artifact_key] = str(report_path)
        summary[summary_key] = json.loads(report_path.read_text(encoding="utf-8"))


def _run_reply_replay_stage(
    *,
    summary: Dict[str, Any],
    dataset_path: Path,
    output_dir: Path,
    min_pass_rate: float,
) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_reply_replay_eval.py"),
        str(dataset_path),
        "--output-dir",
        str(output_dir),
        "--min-pass-rate",
        str(min_pass_rate),
    ]
    exit_code = _run_command(command)
    summary["stages"].append(
        {
            "name": "reply_replay_eval",
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "command": command,
            "dataset": str(dataset_path),
            "thresholds": {"min_pass_rate": min_pass_rate},
        }
    )
    summary["passed"] = summary["passed"] and exit_code == 0
    report_path = output_dir / "reply_replay_report.json"
    if report_path.exists():
        summary["artifacts"]["reply_replay_report"] = str(report_path)
        summary["reply_replay"] = json.loads(report_path.read_text(encoding="utf-8"))


def _run_reply_eval_stage(
    *,
    summary: Dict[str, Any],
    stage_name: str,
    script_name: str,
    dataset_path: Path,
    output_dir: Path,
    min_pass_rate: float,
    artifact_key: str,
    summary_key: str,
    report_file_name: str,
) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / script_name),
        str(dataset_path),
        "--output-dir",
        str(output_dir),
        "--min-pass-rate",
        str(min_pass_rate),
    ]
    exit_code = _run_command(command)
    summary["stages"].append(
        {
            "name": stage_name,
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "command": command,
            "dataset": str(dataset_path),
            "thresholds": {"min_pass_rate": min_pass_rate},
        }
    )
    summary["passed"] = summary["passed"] and exit_code == 0
    report_path = output_dir / report_file_name
    if report_path.exists():
        summary["artifacts"][artifact_key] = str(report_path)
        summary[summary_key] = json.loads(report_path.read_text(encoding="utf-8"))


def _run_reply_replay_export_stage(
    *,
    summary: Dict[str, Any],
    samples_file: Path | None,
    output_dir: Path,
    limit: int,
) -> Path | None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "export_reply_replay_dataset.py"),
        "--limit",
        str(limit),
        "--output-dir",
        str(output_dir),
    ]
    if samples_file:
        command.extend(["--samples-file", str(samples_file)])
    exit_code = _run_command(command)
    summary["stages"].append(
        {
            "name": "export_reply_replay_dataset",
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "command": command,
            "samples_file": str(samples_file) if samples_file else "",
        }
    )
    summary["passed"] = summary["passed"] and exit_code == 0
    report_path = output_dir / "report.json"
    dataset_path = output_dir / "reply_replay_cases.json"
    baseline_path = output_dir / "reply_replay_baseline.json"
    samples_path = output_dir / "observability_samples.json"
    if report_path.exists():
        summary["artifacts"]["reply_replay_export_report"] = str(report_path)
        summary["reply_replay_export"] = json.loads(report_path.read_text(encoding="utf-8"))
    if samples_path.exists():
        summary["artifacts"]["reply_replay_observability_samples"] = str(samples_path)
    if baseline_path.exists():
        summary["artifacts"]["reply_replay_baseline_dataset"] = str(baseline_path)
    if dataset_path.exists():
        summary["artifacts"]["reply_replay_dataset"] = str(dataset_path)
        return baseline_path if baseline_path.exists() else dataset_path
    return None


def _run_reply_observability_capture_stage(
    *,
    summary: Dict[str, Any],
    observability_url: str,
    output_dir: Path,
    limit: int,
) -> Path | None:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "capture_reply_observability_samples.py"),
        "--observability-url",
        str(observability_url),
        "--limit",
        str(limit),
        "--output-dir",
        str(output_dir),
    ]
    exit_code = _run_command(command)
    summary["stages"].append(
        {
            "name": "capture_reply_observability",
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "command": command,
            "observability_url": observability_url,
        }
    )
    summary["passed"] = summary["passed"] and exit_code == 0
    report_path = output_dir / "capture_report.json"
    sample_path = output_dir / "reply_observability_samples.json"
    if report_path.exists():
        summary["artifacts"]["reply_observability_capture_report"] = str(report_path)
        summary["reply_observability_capture"] = json.loads(report_path.read_text(encoding="utf-8"))
    if sample_path.exists():
        summary["artifacts"]["captured_reply_observability_samples"] = str(sample_path)
        return sample_path
    return None


def _evaluate_reply_replay_export_gate(
    *,
    summary: Dict[str, Any],
    report: Dict[str, Any],
    thresholds: Dict[str, float],
) -> None:
    total = int(report.get("total", 0) or 0)
    baseline_total = int(report.get("baseline_total", 0) or 0)
    event_sequence_coverage = float(report.get("event_sequence_coverage", 0.0) or 0.0)
    checks = [
        {
            "metric": "reply_replay_export_total",
            "value": total,
            "threshold": int(thresholds["min_total"]),
            "passed": total >= int(thresholds["min_total"]),
        },
        {
            "metric": "reply_replay_export_baseline_total",
            "value": baseline_total,
            "threshold": int(thresholds["min_baseline_total"]),
            "passed": baseline_total >= int(thresholds["min_baseline_total"]),
        },
        {
            "metric": "reply_replay_export_event_sequence_coverage",
            "value": round(event_sequence_coverage, 4),
            "threshold": float(thresholds["min_event_sequence_coverage"]),
            "passed": event_sequence_coverage >= float(thresholds["min_event_sequence_coverage"]),
        },
    ]
    passed = all(item["passed"] for item in checks)
    summary["stages"].append(
        {
            "name": "reply_replay_export_gate",
            "passed": passed,
            "exit_code": 0 if passed else 1,
            "thresholds": thresholds,
            "checks": checks,
        }
    )
    summary["passed"] = summary["passed"] and passed


def _apply_gate_mode(args: argparse.Namespace) -> None:
    gate_mode = str(getattr(args, "gate_mode", "") or "").strip().lower()
    if not gate_mode:
        return
    preset = GATE_MODE_PRESETS.get(gate_mode)
    if not preset:
        raise ValueError(f"unsupported gate mode: {gate_mode}")
    for key, value in preset.items():
        setattr(args, key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="统一执行发布前质量门禁")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="统一质量门禁输出目录",
    )
    parser.add_argument(
        "--samples-file",
        type=Path,
        help="可选：使用本地 observability 样本导出评测集，避免初始化线上服务",
    )
    parser.add_argument("--limit", type=int, default=200, help="最大导出样本数")
    parser.add_argument("--skip-regression", action="store_true", help="跳过聚焦回归入口")
    parser.add_argument("--skip-export", action="store_true", help="跳过评测集导出")
    parser.add_argument("--skip-offline-eval", action="store_true", help="跳过离线评测 runner")
    parser.add_argument("--skip-intent", action="store_true", help="透传给回归入口，跳过固定样本意图回归")
    parser.add_argument("--full-pytest", action="store_true", help="透传给回归入口，执行全量 tests/")
    parser.add_argument("--pytest-target", action="append", default=[], help="透传给回归入口的 pytest 目标")
    parser.add_argument(
        "--gate-mode",
        choices=["dev", "release"],
        default="",
        help="使用预设门禁阈值模式，覆盖对应的离线评测与固定门禁阈值",
    )
    parser.add_argument("--max-need-human-rate", type=float, default=1.0)
    parser.add_argument("--max-no-answer-rate", type=float, default=0.5)
    parser.add_argument("--max-empty-context-rate", type=float, default=0.5)
    parser.add_argument("--min-avg-context-count", type=float, default=0.1)
    parser.add_argument("--min-avg-top-hit-count", type=float, default=0.1)
    parser.add_argument("--min-label-coverage-grounded", type=float, default=0.0)
    parser.add_argument("--min-label-coverage-helpful", type=float, default=0.0)
    parser.add_argument("--min-label-coverage-correct-route", type=float, default=0.0)
    parser.add_argument("--fixed-max-need-human-rate", type=float, default=0.3)
    parser.add_argument("--fixed-max-no-answer-rate", type=float, default=0.3)
    parser.add_argument("--fixed-max-empty-context-rate", type=float, default=0.3)
    parser.add_argument("--fixed-min-avg-context-count", type=float, default=0.75)
    parser.add_argument("--fixed-min-avg-top-hit-count", type=float, default=0.75)
    parser.add_argument("--fixed-min-label-coverage-grounded", type=float, default=1.0)
    parser.add_argument("--fixed-min-label-coverage-helpful", type=float, default=1.0)
    parser.add_argument("--fixed-min-label-coverage-correct-route", type=float, default=1.0)
    parser.add_argument(
        "--generic-schema-dataset",
        type=Path,
        default=DEFAULT_GENERIC_SCHEMA_DATASET,
        help="generic 固定门禁集路径",
    )
    parser.add_argument(
        "--tourism-schema-dataset",
        type=Path,
        default=DEFAULT_TOURISM_SCHEMA_DATASET,
        help="tourism 固定门禁集路径",
    )
    parser.add_argument(
        "--third-schema-dataset",
        type=Path,
        default=DEFAULT_THIRD_SCHEMA_DATASET,
        help="第三行业固定门禁集路径",
    )
    parser.add_argument(
        "--skip-fixed-schema-gates",
        action="store_true",
        help="跳过固定 schema 门禁离线评测",
    )
    parser.add_argument(
        "--reply-replay-dataset",
        type=Path,
        default=DEFAULT_REPLY_REPLAY_DATASET,
        help="自动回复 replay 数据集路径",
    )
    parser.add_argument(
        "--reply-replay-samples-file",
        type=Path,
        help="可选：从真实运行观测样本导出 reply replay 数据集；未传时复用 --samples-file",
    )
    parser.add_argument("--capture-reply-observability", action="store_true", help="先抓取最新 reply observability 样本再导出 replay 数据集")
    parser.add_argument(
        "--reply-observability-url",
        default=DEFAULT_REPLY_OBSERVABILITY_URL,
        help="reply observability 接口地址，用于自动抓取最新样本",
    )
    parser.add_argument("--skip-reply-replay-export", action="store_true", help="跳过 reply replay 数据集导出阶段")
    parser.add_argument("--skip-reply-replay", action="store_true", help="跳过自动回复 replay 回归")
    parser.add_argument("--min-reply-replay-pass-rate", type=float, default=1.0, help="reply replay 最低通过率")
    parser.add_argument("--reply-replay-baseline-limit", type=int, default=60, help="导出 replay 基线集最大样本数")
    parser.add_argument("--reply-replay-per-category-limit", type=int, default=15, help="导出 replay 基线集每类最大样本数")
    parser.add_argument("--reply-replay-export-min-total", type=int, default=0, help="reply replay 导出阶段要求的最小样本数")
    parser.add_argument("--reply-replay-export-min-baseline-total", type=int, default=0, help="reply replay 导出阶段要求的最小基线样本数")
    parser.add_argument("--reply-replay-export-min-event-sequence-coverage", type=float, default=0.0, help="reply replay 导出阶段要求的最小事件序列覆盖率")
    parser.add_argument(
        "--reply-stability-dataset",
        type=Path,
        default=DEFAULT_REPLY_STABILITY_DATASET,
        help="自动回复长稳评测数据集路径",
    )
    parser.add_argument("--skip-reply-stability", action="store_true", help="跳过自动回复长稳评测")
    parser.add_argument("--min-reply-stability-pass-rate", type=float, default=1.0, help="reply 长稳最低通过率")
    parser.add_argument(
        "--reply-fault-dataset",
        type=Path,
        default=DEFAULT_REPLY_FAULT_INJECTION_DATASET,
        help="自动回复异常注入评测数据集路径",
    )
    parser.add_argument("--skip-reply-fault", action="store_true", help="跳过自动回复异常注入评测")
    parser.add_argument("--min-reply-fault-pass-rate", type=float, default=1.0, help="reply 异常注入最低通过率")
    args = parser.parse_args()
    _apply_gate_mode(args)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    export_dir = output_dir / "exported_dataset"
    offline_eval_dir = output_dir / "offline_eval"
    third_schema_eval_dir = output_dir / "third_schema_offline_eval"
    reply_replay_dir = output_dir / "reply_replay_eval"
    reply_replay_export_dir = output_dir / "exported_reply_replay"
    reply_observability_capture_dir = output_dir / "captured_reply_observability"
    reply_stability_dir = output_dir / "reply_stability_eval"
    reply_fault_dir = output_dir / "reply_fault_injection_eval"
    summary_path = output_dir / "quality_gate_report.json"

    summary: Dict[str, Any] = {
        "passed": True,
        "output_dir": str(output_dir),
        "stages": [],
        "artifacts": {},
        "thresholds": {
            "gate_mode": args.gate_mode or "custom",
            "max_need_human_rate": args.max_need_human_rate,
            "max_no_answer_rate": args.max_no_answer_rate,
            "max_empty_context_rate": args.max_empty_context_rate,
            "min_avg_context_count": args.min_avg_context_count,
            "min_avg_top_hit_count": args.min_avg_top_hit_count,
            "min_label_coverage_grounded": args.min_label_coverage_grounded,
            "min_label_coverage_helpful": args.min_label_coverage_helpful,
            "min_label_coverage_correct_route": args.min_label_coverage_correct_route,
        },
        "fixed_gate_thresholds": {
            "gate_mode": args.gate_mode or "custom",
            "max_need_human_rate": args.fixed_max_need_human_rate,
            "max_no_answer_rate": args.fixed_max_no_answer_rate,
            "max_empty_context_rate": args.fixed_max_empty_context_rate,
            "min_avg_context_count": args.fixed_min_avg_context_count,
            "min_avg_top_hit_count": args.fixed_min_avg_top_hit_count,
            "min_label_coverage_grounded": args.fixed_min_label_coverage_grounded,
            "min_label_coverage_helpful": args.fixed_min_label_coverage_helpful,
            "min_label_coverage_correct_route": args.fixed_min_label_coverage_correct_route,
        },
        "reply_replay_thresholds": {
            "min_pass_rate": args.min_reply_replay_pass_rate,
        },
        "reply_replay_export_thresholds": {
            "baseline_limit": args.reply_replay_baseline_limit,
            "per_category_limit": args.reply_replay_per_category_limit,
            "min_total": args.reply_replay_export_min_total,
            "min_baseline_total": args.reply_replay_export_min_baseline_total,
            "min_event_sequence_coverage": args.reply_replay_export_min_event_sequence_coverage,
        },
        "reply_stability_thresholds": {
            "min_pass_rate": args.min_reply_stability_pass_rate,
        },
        "reply_fault_thresholds": {
            "min_pass_rate": args.min_reply_fault_pass_rate,
        },
    }
    fixed_gate_thresholds = dict(summary["fixed_gate_thresholds"])

    if not args.skip_regression:
        regression_command = [sys.executable, str(REPO_ROOT / "scripts" / "run_wide_regression.py")]
        if args.full_pytest:
            regression_command.append("--full-pytest")
        if args.skip_intent:
            regression_command.append("--skip-intent")
        for target in args.pytest_target:
            regression_command.extend(["--pytest-target", target])
        regression_code = _run_command(regression_command)
        summary["stages"].append(
            {
                "name": "wide_regression",
                "passed": regression_code == 0,
                "exit_code": regression_code,
                "command": regression_command,
            }
        )
        summary["passed"] = summary["passed"] and regression_code == 0

    if not args.skip_export:
        export_command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "export_rag_evaluation_dataset.py"),
            "--limit",
            str(args.limit),
            "--output-dir",
            str(export_dir),
        ]
        if args.samples_file:
            export_command.extend(["--samples-file", str(args.samples_file)])
        export_code = _run_command(export_command)
        summary["stages"].append(
            {
                "name": "export_rag_dataset",
                "passed": export_code == 0,
                "exit_code": export_code,
                "command": export_command,
            }
        )
        summary["passed"] = summary["passed"] and export_code == 0

    dataset_path = export_dir / "dataset.pretty.json"
    if not args.skip_offline_eval:
        if args.skip_export and not dataset_path.exists():
            raise FileNotFoundError(f"跳过导出时缺少评测集: {dataset_path}")
        _run_offline_eval_stage(
            summary=summary,
            args=args,
            stage_name="offline_eval",
            dataset_path=dataset_path,
            output_dir=offline_eval_dir,
            artifact_key="offline_eval_report",
            summary_key="offline_eval",
        )

    if dataset_path.exists():
        summary["artifacts"]["dataset"] = str(dataset_path)

    if not args.skip_fixed_schema_gates:
        fixed_gate_specs = [
            (
                "generic",
                args.generic_schema_dataset.resolve(),
                output_dir / "fixed_generic_offline_eval",
                "generic_schema_dataset",
                "generic_offline_eval_report",
                "generic_offline_eval",
            ),
            (
                "tourism",
                args.tourism_schema_dataset.resolve(),
                output_dir / "fixed_tourism_offline_eval",
                "tourism_schema_dataset",
                "tourism_offline_eval_report",
                "tourism_offline_eval",
            ),
            (
                "education",
                args.third_schema_dataset.resolve(),
                third_schema_eval_dir,
                "third_schema_dataset",
                "third_schema_offline_eval_report",
                "third_schema_offline_eval",
            ),
        ]
        for gate_name, gate_dataset, gate_output_dir, dataset_artifact_key, report_artifact_key, summary_key in fixed_gate_specs:
            if not gate_dataset.exists():
                raise FileNotFoundError(f"{gate_name} 固定门禁集不存在: {gate_dataset}")
            summary["artifacts"][dataset_artifact_key] = str(gate_dataset)
            _run_offline_eval_stage(
                summary=summary,
                args=args,
                stage_name=f"offline_eval_fixed_{gate_name}",
                dataset_path=gate_dataset,
                output_dir=gate_output_dir,
                artifact_key=report_artifact_key,
                summary_key=summary_key,
                thresholds=fixed_gate_thresholds,
            )

    if not args.skip_reply_replay:
        replay_samples_file: Path | None = None
        if args.capture_reply_observability:
            replay_samples_file = _run_reply_observability_capture_stage(
                summary=summary,
                observability_url=str(args.reply_observability_url),
                output_dir=reply_observability_capture_dir,
                limit=int(args.limit or 200),
            )
        elif args.reply_replay_samples_file:
            replay_samples_file = args.reply_replay_samples_file.resolve()
        elif args.samples_file:
            replay_samples_file = args.samples_file.resolve()
        replay_dataset: Path | None = None
        if not args.skip_reply_replay_export:
            export_command_dir = reply_replay_export_dir
            export_command = [
                sys.executable,
                str(REPO_ROOT / "scripts" / "export_reply_replay_dataset.py"),
                "--limit",
                str(int(args.limit or 200)),
                "--output-dir",
                str(export_command_dir),
                "--baseline-limit",
                str(int(args.reply_replay_baseline_limit or 1)),
                "--per-category-limit",
                str(int(args.reply_replay_per_category_limit or 1)),
            ]
            if replay_samples_file:
                export_command.extend(["--samples-file", str(replay_samples_file)])
            export_code = _run_command(export_command)
            summary["stages"].append(
                {
                    "name": "export_reply_replay_dataset",
                    "passed": export_code == 0,
                    "exit_code": export_code,
                    "command": export_command,
                    "samples_file": str(replay_samples_file) if replay_samples_file else "",
                }
            )
            summary["passed"] = summary["passed"] and export_code == 0
            report_path = export_command_dir / "report.json"
            dataset_path = export_command_dir / "reply_replay_cases.json"
            baseline_path = export_command_dir / "reply_replay_baseline.json"
            samples_path = export_command_dir / "observability_samples.json"
            if report_path.exists():
                summary["artifacts"]["reply_replay_export_report"] = str(report_path)
                summary["reply_replay_export"] = json.loads(report_path.read_text(encoding="utf-8"))
                _evaluate_reply_replay_export_gate(
                    summary=summary,
                    report=summary["reply_replay_export"],
                    thresholds=dict(summary["reply_replay_export_thresholds"]),
                )
            if samples_path.exists():
                summary["artifacts"]["reply_replay_observability_samples"] = str(samples_path)
            if baseline_path.exists():
                summary["artifacts"]["reply_replay_baseline_dataset"] = str(baseline_path)
            if dataset_path.exists():
                summary["artifacts"]["reply_replay_dataset"] = str(dataset_path)
                replay_dataset = baseline_path if baseline_path.exists() else dataset_path
        if replay_dataset is None:
            replay_dataset = args.reply_replay_dataset.resolve()
        if not replay_dataset.exists():
            raise FileNotFoundError(f"reply replay 数据集不存在: {replay_dataset}")
        summary["artifacts"]["reply_replay_dataset"] = str(replay_dataset)
        _run_reply_replay_stage(
            summary=summary,
            dataset_path=replay_dataset,
            output_dir=reply_replay_dir,
            min_pass_rate=float(args.min_reply_replay_pass_rate),
        )

    if not args.skip_reply_stability:
        stability_dataset = args.reply_stability_dataset.resolve()
        if not stability_dataset.exists():
            raise FileNotFoundError(f"reply 长稳数据集不存在: {stability_dataset}")
        summary["artifacts"]["reply_stability_dataset"] = str(stability_dataset)
        _run_reply_eval_stage(
            summary=summary,
            stage_name="reply_stability_eval",
            script_name="run_reply_stability_eval.py",
            dataset_path=stability_dataset,
            output_dir=reply_stability_dir,
            min_pass_rate=float(args.min_reply_stability_pass_rate),
            artifact_key="reply_stability_report",
            summary_key="reply_stability",
            report_file_name="reply_stability_report.json",
        )

    if not args.skip_reply_fault:
        fault_dataset = args.reply_fault_dataset.resolve()
        if not fault_dataset.exists():
            raise FileNotFoundError(f"reply 异常注入数据集不存在: {fault_dataset}")
        summary["artifacts"]["reply_fault_dataset"] = str(fault_dataset)
        _run_reply_eval_stage(
            summary=summary,
            stage_name="reply_fault_injection_eval",
            script_name="run_reply_fault_injection_eval.py",
            dataset_path=fault_dataset,
            output_dir=reply_fault_dir,
            min_pass_rate=float(args.min_reply_fault_pass_rate),
            artifact_key="reply_fault_report",
            summary_key="reply_fault",
            report_file_name="reply_fault_injection_report.json",
        )

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 72)
    print("质量门禁执行完成")
    print(f"report : {summary_path}")
    print(f"passed : {summary['passed']}")
    print("=" * 72)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
