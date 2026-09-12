"""
智能知识库系统 - 开发模式启动脚本
提供热重载支持，并明确分离开发与生产启动流程。
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import webbrowser

from src.infrastructure.env_loader import load_project_env

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
load_project_env(base_dir=PROJECT_ROOT, override=False)


def _default_port() -> int:
    for env_name in ("HUOKE_DEV_PORT", "SERVER_PORT", "PORT"):
        raw_value = os.environ.get(env_name, "").strip()
        if not raw_value:
            continue
        try:
            return int(raw_value)
        except ValueError:
            print(f"[WARN] Ignore invalid {env_name}={raw_value!r}; fallback to 8023")
    return 8023


def _default_host() -> str:
    for env_name in ("HUOKE_DEV_HOST", "SERVER_HOST", "HOST"):
        raw_value = os.environ.get(env_name, "").strip()
        if raw_value:
            return raw_value
    return "127.0.0.1"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Huoke Smart Bot 开发模式启动")
    parser.add_argument("--host", default=_default_host(), help="监听地址")
    parser.add_argument("--port", type=int, default=_default_port(), help="优先使用的监听端口")
    parser.add_argument("--max-port-tries", type=int, default=20, help="端口被占用时最多顺延尝试次数")
    parser.add_argument("--reload", dest="reload", action="store_true", help="启用热重载")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="禁用热重载")
    parser.set_defaults(reload=True)
    parser.add_argument("--open-browser", dest="open_browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-open-browser", dest="open_browser", action="store_false", help="启动后不自动打开浏览器")
    parser.set_defaults(open_browser=True)
    return parser.parse_args(argv)


def _can_bind(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _resolve_port(host: str, preferred_port: int, max_port_tries: int) -> int:
    for offset in range(max_port_tries + 1):
        candidate = preferred_port + offset
        if _can_bind(host, candidate):
            if offset > 0:
                print(f"[WARN] Port {preferred_port} is occupied; fallback to {candidate}")
            return candidate
    raise RuntimeError(
        f"Cannot find an available port from {preferred_port} to {preferred_port + max_port_tries}"
    )


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _open_browser_later(url: str, delay_seconds: float = 1.5) -> None:
    def _worker():
        try:
            webbrowser.open(url)
        except Exception as exc:
            print(f"[WARN] Failed to open browser automatically: {exc}")

    timer = threading.Timer(delay_seconds, _worker)
    timer.daemon = True
    timer.start()


def main(argv=None):
    args = parse_args(argv)

    print("=" * 60)
    print("Huoke Smart Bot - Development Mode Startup")
    print("=" * 60)

    # 确保在项目根目录执行
    base_dir = PROJECT_ROOT
    os.chdir(base_dir)

    read_source = os.environ.get("HUOKE_MESSAGE_STORE_READ_SOURCE", "").strip().lower()
    if not read_source:
        os.environ["HUOKE_MESSAGE_STORE_READ_SOURCE"] = "sqlite"
        read_source = "sqlite"
        print("[INFO] Defaulting chat store read source to SQLite in development mode.")
    else:
        print(f"[INFO] Chat store read source from environment: {read_source}")
    print(f"[INFO] HUOKE_MESSAGE_STORE_READ_SOURCE={read_source}")

    resolved_port = _resolve_port(args.host, args.port, args.max_port_tries)
    open_url = f"http://{_display_host(args.host)}:{resolved_port}/"

    print(f"[INFO] Starting Uvicorn development server on {args.host}:{resolved_port}...")
    print(f"[INFO] Hot reload: {'enabled' if args.reload else 'disabled'}")
    print(f"[INFO] Please open in browser: {open_url}")
    print("-" * 60)

    if args.open_browser:
        _open_browser_later(open_url)

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.web.main:app",
        "--host",
        args.host,
        "--port",
        str(resolved_port),
    ]
    if args.reload:
        cmd.append("--reload")

    try:
        subprocess.run(cmd, cwd=base_dir, check=False)
    except KeyboardInterrupt:
        print("\n[INFO] Server stopped by user.")
    except Exception as e:
        print(f"\n[ERROR] Failed to start server: {e}")


if __name__ == "__main__":
    main()
