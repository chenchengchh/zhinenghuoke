import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.licensing.license_signing import generate_ed25519_keypair  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成火客授权 Ed25519 签名密钥对。")
    parser.add_argument("--json", action="store_true", help="按 JSON 输出。")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = generate_ed25519_keypair()
    env_payload = {
        "HUOKE_LICENSE_SIGNATURE_ALG": payload["signature_alg"],
        "HUOKE_LICENSE_PRIVATE_KEY": payload["private_key"],
        "HUOKE_LICENSE_PUBLIC_KEY": payload["public_key"],
    }
    if args.json:
        print(json.dumps(env_payload, ensure_ascii=False, indent=2))
        return

    print("# 发码端保留以下全部变量")
    for key in ("HUOKE_LICENSE_SIGNATURE_ALG", "HUOKE_LICENSE_PRIVATE_KEY", "HUOKE_LICENSE_PUBLIC_KEY"):
        print(f"{key}={env_payload[key]}")
    print()
    print("# 客户端/校验端只需要以下两个变量")
    for key in ("HUOKE_LICENSE_SIGNATURE_ALG", "HUOKE_LICENSE_PUBLIC_KEY"):
        print(f"{key}={env_payload[key]}")


if __name__ == "__main__":
    main()
