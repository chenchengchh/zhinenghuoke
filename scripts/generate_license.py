import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.licensing.license_models import LicenseFeatures, LicenseType  # noqa: E402
from src.licensing.request_code import decode_request_code  # noqa: E402
from src.licensing.license_service import LicenseService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成火客智能客服授权码。")
    parser.add_argument(
        "--type",
        required=True,
        choices=[
            LicenseType.TRIAL_7D.value,
            LicenseType.MONTH_1.value,
            LicenseType.MONTH_3.value,
            LicenseType.MONTH_6.value,
            LicenseType.YEAR_1.value,
        ],
        help="授权类型：7天试用、1个月、3个月、6个月、1年。",
    )
    parser.add_argument("--customer-name", default="", help="客户名称，用于写入授权信息。")
    parser.add_argument("--phone-number", required=True, help="注册手机号，软件激活时必须填写同一手机号。")
    parser.add_argument("--license-id", default="", help="可选，自定义授权编号。")
    request_group = parser.add_mutually_exclusive_group(required=True)
    request_group.add_argument("--request-code", default="", help="目标机器生成的请求码。")
    request_group.add_argument("--request-file", default="", help="包含请求码内容的文本文件路径。")
    parser.add_argument("--disable-crawler", action="store_true", help="禁用爬取功能。")
    parser.add_argument("--disable-monitor", action="store_true", help="禁用监听功能。")
    parser.add_argument("--disable-auto-reply", action="store_true", help="禁用自动回复功能。")
    parser.add_argument("--json", action="store_true", help="按 JSON 输出，便于复制保存。")
    return parser


def _resolve_request_code(args: argparse.Namespace) -> str:
    if args.request_code:
        return str(args.request_code).strip()
    request_file = Path(str(args.request_file or "").strip())
    if not request_file.exists():
        raise FileNotFoundError(f"请求码文件不存在: {request_file}")
    return request_file.read_text(encoding="utf-8").strip()



def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    features = LicenseFeatures(
        crawler=not args.disable_crawler,
        monitor=not args.disable_monitor,
        auto_reply=not args.disable_auto_reply,
    )
    license_type = LicenseType(args.type)
    service = LicenseService()
    request_code = _resolve_request_code(args)
    request_payload = decode_request_code(request_code)
    license_key = service.generate_license_key(
        license_type=license_type,
        customer_name=args.customer_name,
        phone_number=args.phone_number,
        machine_hash=request_payload.machine_hash,
        features=features,
        license_id=args.license_id,
    )

    payload = {
        "license_type": license_type.value,
        "customer_name": args.customer_name,
        "phone_number": args.phone_number,
        "machine_hash": request_payload.machine_hash,
        "fingerprint_version": request_payload.fingerprint_version,
        "features": features.model_dump(),
        "license_key": license_key,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"授权类型: {payload['license_type']}")
    print(f"客户名称: {payload['customer_name'] or '-'}")
    print(f"绑定手机号: {payload['phone_number']}")
    print(f"绑定设备: {payload['machine_hash']}")
    print(f"功能权限: {json.dumps(payload['features'], ensure_ascii=False)}")
    print("license_key:")
    print(payload["license_key"])


if __name__ == "__main__":
    main()
