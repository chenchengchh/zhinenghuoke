from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


from reply_eval_utils import build_report, read_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="运行自动回复长稳评测")
    parser.add_argument("dataset", type=Path, help="长稳评测 JSON 数据集路径")
    parser.add_argument("--output-dir", type=Path, help="报告输出目录，默认写回数据集目录")
    parser.add_argument("--min-pass-rate", type=float, default=1.0, help="最低通过率")
    args = parser.parse_args()

    cases = read_cases(args.dataset)
    report = build_report(cases, format_name="reply_stability_eval")
    report["thresholds"] = {"min_pass_rate": float(args.min_pass_rate)}
    report["gate"] = {
        "passed": float(report.get("pass_rate", 0.0) or 0.0) >= float(args.min_pass_rate),
    }

    output_dir = args.output_dir or args.dataset.resolve().parent
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "reply_stability_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 72)
    print("Reply 长稳评测完成")
    print(f"dataset: {args.dataset}")
    print(f"report : {report_path}")
    print(f"total  : {report.get('total', 0)}")
    print(f"passed : {report.get('gate', {}).get('passed', False)}")
    print("=" * 72)
    return 0 if report.get("gate", {}).get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
