from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List


REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PYTEST_TARGETS = [
    "tests/test_main_import_lazy_services.py",
    "tests/test_enterprise_dependency.py",
    "tests/test_unified_knowledge_service_sync.py",
    "tests/test_reply_orchestrator.py",
    "tests/test_document_upload_pipeline.py",
    "tests/test_upload_route_contracts.py",
    "tests/test_rag_search_routes.py",
    "tests/test_rag_evaluation_routes.py",
    "tests/test_rag_offline_eval_scripts.py",
    "tests/test_schema_metadata_migration.py",
]


def _run_command(command: List[str]) -> int:
    print(f"\n[RUN] {' '.join(command)}")
    completed = subprocess.run(command, cwd=REPO_ROOT)
    print(f"[EXIT] code={completed.returncode}")
    return int(completed.returncode)


def _iter_pytest_targets(args: argparse.Namespace) -> Iterable[str]:
    if args.full_pytest:
        yield "tests"
        return
    if args.pytest_target:
        for target in args.pytest_target:
            yield target
        return
    yield from DEFAULT_PYTEST_TARGETS


def main() -> int:
    parser = argparse.ArgumentParser(description="运行聚焦版大范围回归入口")
    parser.add_argument(
        "--full-pytest",
        action="store_true",
        help="运行全量 tests/，不使用聚焦回归集",
    )
    parser.add_argument(
        "--pytest-target",
        action="append",
        default=[],
        help="额外指定 pytest 目标；可重复传入",
    )
    parser.add_argument(
        "--skip-intent",
        action="store_true",
        help="跳过固定样本意图回归脚本",
    )
    parser.add_argument(
        "--with-cov",
        action="store_true",
        help="为 pytest 附加覆盖率参数",
    )
    args = parser.parse_args()

    failures = []
    pytest_command = [sys.executable, "-m", "pytest"]
    if args.with_cov:
        pytest_command.extend(["--cov=src", "--cov-report=term-missing"])
    pytest_command.extend(list(_iter_pytest_targets(args)))

    if _run_command(pytest_command) != 0:
        failures.append("pytest")

    if not args.skip_intent:
        intent_script = REPO_ROOT / "scripts" / "check_intent_regression.py"
        if intent_script.exists():
            if _run_command([sys.executable, str(intent_script)]) != 0:
                failures.append("intent_regression")
        else:
            print(f"[SKIP] 缺少脚本: {intent_script}")

    print("\n" + "=" * 72)
    if failures:
        print(f"回归完成，但存在失败项: {', '.join(failures)}")
        print("=" * 72)
        return 1

    print("回归完成，聚焦回归集与意图固定样本均通过")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
