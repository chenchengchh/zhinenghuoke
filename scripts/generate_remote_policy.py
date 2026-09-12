import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.remote_control.service import RemoteControlService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成远程控制策略 JSON。")
    parser.add_argument("--instance-id", default="", help="可选，指定目标机器实例 ID。")
    parser.add_argument("--disable-app", action="store_true", help="禁用整个软件。")
    parser.add_argument("--disable-crawler", action="store_true", help="禁用爬取能力。")
    parser.add_argument("--disable-monitor", action="store_true", help="禁用监听能力。")
    parser.add_argument("--disable-auto-reply", action="store_true", help="禁用自动回复能力。")
    parser.add_argument("--message", default="", help="下发到客户端的提示信息。")
    parser.add_argument("--policy-id", default="", help="可选，自定义策略编号。")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    service = RemoteControlService()
    payload = service.generate_signed_policy(
        app_enabled=not args.disable_app,
        crawler_enabled=not args.disable_crawler,
        monitor_enabled=not args.disable_monitor,
        auto_reply_enabled=not args.disable_auto_reply,
        message=args.message,
        instance_id=args.instance_id,
        policy_id=args.policy_id,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
