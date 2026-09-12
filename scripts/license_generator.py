#!/usr/bin/env python3
"""
火客验证码生成器 - 命令行版本
用法:
    python -m scripts.license_generator --help
    python -m scripts.license_generator --request-code "<请求码>" --phone 13800138000 --type trial_7d
    python -m scripts.license_generator --machine-hash "<机器哈希>" --phone 13800138000 --type year_1 --name "客户名称"
"""
import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.licensing.license_models import LicenseFeatures, LicenseType, get_license_type_label, get_license_valid_days
from src.licensing.request_code import decode_request_code
from src.licensing.license_service import LicenseService


LICENSE_TYPE_OPTIONS = [lt.value for lt in LicenseType]


def decode_license_key(license_key: str) -> dict:
    """解码 license_key 返回原始 payload"""
    normalized = str(license_key or "").strip()
    decoded = base64.urlsafe_b64decode(normalized.encode("utf-8") + b"===")
    return json.loads(decoded.decode("utf-8"))


def generate_license_key(
    *,
    license_type: str,
    customer_name: str = "",
    phone_number: str = "",
    request_code: str = "",
    machine_hash: str = "",
    features: dict | None = None,
    license_id: str = "",
) -> tuple[str, dict]:
    """
    生成验证码核心函数。

    Args:
        license_type: 授权类型 (trial_7d, month_1, month_3, month_6, year_1, permanent)
        customer_name: 客户名称
        phone_number: 注册手机号（必填）
        request_code: 设备请求码（优先于 machine_hash）
        machine_hash: 机器哈希（当 request_code 为空时使用）
        features: 功能权限字典
        license_id: 授权编号

    Returns:
        (license_key, decoded_payload) 元组

    Raises:
        ValueError: 参数无效时
    """
    normalized_features = LicenseFeatures(**(features or {}))
    service = LicenseService()
    resolved_machine_hash = str(machine_hash or "").strip()

    # 如果提供了请求码，从中提取 machine_hash
    if request_code:
        try:
            request_payload = decode_request_code(str(request_code).strip())
            resolved_machine_hash = request_payload.machine_hash
        except Exception as exc:
            raise ValueError(f"请求码无效：{exc}")

    if not resolved_machine_hash:
        raise ValueError("设备请求码或机器哈希不能为空")

    if not phone_number:
        raise ValueError("注册手机号不能为空")

    try:
        lic_type = LicenseType(str(license_type).strip())
    except ValueError:
        raise ValueError(f"不支持的授权类型：{license_type}，可选值：{LICENSE_TYPE_OPTIONS}")

    license_key = service.generate_license_key(
        license_type=lic_type,
        customer_name=str(customer_name or "").strip(),
        phone_number=str(phone_number or "").strip(),
        machine_hash=resolved_machine_hash,
        features=normalized_features,
        license_id=str(license_id or "").strip(),
    )

    decoded_payload = decode_license_key(license_key)
    return license_key, decoded_payload


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="license_generator",
        description="火客验证码生成器 - 生成绑定特定设备的授权验证码",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用请求码生成7天试用授权
  python -m scripts.license_generator --request-code "REQxxxxx" --phone 13800138000

  # 直接使用机器哈希生成一年授权
  python -m scripts.license_generator --machine-hash "abc123..." --phone 13800138000 --type year_1 --name "张三"

  # 启用所有功能
  python -m scripts.license_generator --request-code "REQxxxxx" --phone 13800138000 --crawler --monitor --auto-reply
        """,
    )

    parser.add_argument("--request-code", "-r", type=str, default="",
                        help="设备请求码（base64编码的请求码字符串）")
    parser.add_argument("--machine-hash", "-m", type=str, default="",

                        help="机器哈希（当 request-code 为空时使用）")
    parser.add_argument("--phone", "-p", type=str, required=True,
                        help="注册手机号（必填）")
    parser.add_argument("--type", "-t", type=str, default="trial_7d",
                        choices=LICENSE_TYPE_OPTIONS,
                        help=f"授权类型（默认：trial_7d），可选：{LICENSE_TYPE_OPTIONS}")
    parser.add_argument("--name", "-n", type=str, default="",
                        help="客户名称")
    parser.add_argument("--license-id", "-i", type=str, default="",
                        help="授权编号（留空则自动生成）")
    parser.add_argument("--crawler", action="store_true", default=True,
                        help="启用爬取功能（默认启用）")
    parser.add_argument("--no-crawler", dest="crawler", action="store_false",
                        help="禁用爬取功能")
    parser.add_argument("--monitor", action="store_true", default=True,
                        help="启用监听功能（默认启用）")
    parser.add_argument("--no-monitor", dest="monitor", action="store_false",
                        help="禁用监听功能")
    parser.add_argument("--auto-reply", action="store_true", default=True,
                        help="启用自动回复功能（默认启用）")
    parser.add_argument("--no-auto-reply", dest="auto_reply", action="store_false",
                        help="禁用自动回复功能")
    parser.add_argument("--output", "-o", type=str, default="",
                        help="输出文件路径（留空则输出到stdout）")
    parser.add_argument("--json", "-j", action="store_true",
                        help="输出纯JSON格式（不含额外说明文字）")

    args = parser.parse_args()

    # 构建功能权限
    features = {
        "crawler": args.crawler,
        "monitor": args.monitor,
        "auto_reply": args.auto_reply,
    }

    try:
        license_key, decoded_payload = generate_license_key(
            license_type=args.type,
            customer_name=args.name,
            phone_number=args.phone,
            request_code=args.request_code,
            machine_hash=args.machine_hash,
            features=features,
            license_id=args.license_id,
        )

        lic_type = LicenseType(args.type)
        output = {
            "license_key": license_key,
            "license_type": args.type,
            "license_type_label": get_license_type_label(lic_type),
            "valid_days": get_license_valid_days(lic_type),
            "customer_name": args.name,
            "phone_number": args.phone,
            "license_id": decoded_payload.get("license_id", ""),
            "machine_hash": decoded_payload.get("machine_hash", ""),
            "fingerprint_version": decoded_payload.get("fingerprint_version", ""),
            "issued_at": decoded_payload.get("issued_at", ""),
            "expire_at": decoded_payload.get("expire_at", ""),
            "features": features,
            "signature_alg": decoded_payload.get("signature_alg", ""),
        }

        if args.json:
            content = json.dumps(output, ensure_ascii=False, indent=2)
        else:
            content = f"""╔══════════════════════════════════════════════════════════════╗
║                    火客验证码生成结果                       ║
╠══════════════════════════════════════════════════════════════╣
║ 授权类型: {output['license_type_label']:<44} ║
║ 有效天数: {output['valid_days']:<44} ║
║ 客户名称: {output['customer_name']:<44} ║
║ 注册手机: {output['phone_number']:<44} ║
║ 授权编号: {output['license_id']:<44} ║
║ 机器哈希: {output['machine_hash'][:40]}...                        ║
║ 发行时间: {output['issued_at'][:19] if output['issued_at'] else '':<44} ║
║ 到期时间: {output['expire_at'][:19] if output['expire_at'] else '':<44} ║
║ 签名算法: {output['signature_alg']:<44} ║
║ 功能权限: 爬取={'✓' if features['crawler'] else '✗'} 监听={'✓' if features['monitor'] else '✗'} 自动回复={'✓' if features['auto_reply'] else '✗'}                      ║
╠══════════════════════════════════════════════════════════════╣
║                         验证码                             ║
╠══════════════════════════════════════════════════════════════╣
{license_key}
╚══════════════════════════════════════════════════════════════╝"""

        if args.output:
            Path(args.output).write_text(content, encoding="utf-8")
            print(f"✅ 验证码已保存到: {args.output}")
        else:
            print(content)

        return 0

    except ValueError as exc:
        print(f"❌ 参数错误: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌ 生成失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
