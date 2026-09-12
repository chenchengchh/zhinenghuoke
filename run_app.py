"""
PyInstaller专用启动入口

解决frozen模式下的路径问题：
1. 修正sys.path使src模块可导入
2. 修正BASE_DIR指向EXE所在目录
3. 修正static/templates路径
4. 系统初始化检查与自动修复
5. 优雅关闭信号处理（SIGTERM/SIGINT）
6. 启动前清理Python缓存，确保代码修改生效
7. 启动前检测端口占用，自动清理旧进程
"""
import os
import sys
import signal
import shutil
import threading
import time
import webbrowser
import zipfile
import multiprocessing as mp
import re
import tempfile
from pathlib import Path
from urllib import request as urllib_request

import psutil


APP_NAME = "HuokeSmartBot"
_STDIO_SINK = None


def _is_writable_stream(stream) -> bool:
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
        return True
    except Exception:
        return False


def _handle_frozen_multiprocessing_bootstrap() -> None:
    """Ensure frozen multiprocessing helpers never fall through to app startup."""
    if __name__ != "__main__":
        return

    argv = list(sys.argv[1:])
    if getattr(sys, "frozen", False) and argv[:1] == ["--multiprocessing-fork"]:
        from multiprocessing.spawn import freeze_support as spawn_freeze_support

        spawn_freeze_support()
        raise RuntimeError("multiprocessing helper args were not consumed as expected")

    # PyInstaller frozen builds on Windows need freeze_support before any
    # module-level app bootstrap, otherwise spawned multiprocessing children
    # can hang while re-entering the main executable.
    mp.freeze_support()


_handle_frozen_multiprocessing_bootstrap()


def ensure_stdio_streams() -> None:
    """Provide writable stdout/stderr for windowed frozen builds."""
    global _STDIO_SINK

    if getattr(sys, "frozen", False) and _STDIO_SINK is not None:
        sys.stdout = _STDIO_SINK
        sys.stderr = _STDIO_SINK
        return

    if not getattr(sys, "frozen", False) and _is_writable_stream(sys.stdout) and _is_writable_stream(sys.stderr):
        return

    candidate_dirs = []
    if getattr(sys, "frozen", False):
        candidate_dirs.append(Path(sys.executable).resolve().parent / "_runtime" / "logs")
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidate_dirs.append(Path(local_app_data).resolve() / APP_NAME / "logs")
    else:
        candidate_dirs.append(Path(__file__).resolve().parent / "logs")

    sink = None
    for log_dir in candidate_dirs:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            sink = open(log_dir / "startup_console.log", "a", encoding="utf-8", buffering=1)
            break
        except Exception:
            continue

    if sink is None:
        sink = open(os.devnull, "w", encoding="utf-8")

    _STDIO_SINK = sink
    if getattr(sys, "frozen", False):
        sys.stdout = sink
        sys.stderr = sink
        return

    if not _is_writable_stream(sys.stdout):
        sys.stdout = sink
    if not _is_writable_stream(sys.stderr):
        sys.stderr = sink


def _copy_if_missing(source: Path, target: Path) -> None:
    if not source.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree_contents_if_missing(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        target_path = target_dir / item.name
        if target_path.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, target_path)
        else:
            shutil.copy2(item, target_path)


def _extract_server_config_values(config_path: Path) -> tuple[str | None, int | None]:
    if not config_path.exists() or not config_path.is_file():
        return None, None

    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception:
        return None, None

    block_match = re.search(r"(?ms)^server:\s*\n(?P<body>(?:^[ \t].*\n?)*)", text)
    if not block_match:
        return None, None

    body = block_match.group("body")
    host_match = re.search(r'(?m)^\s+host\s*:\s*"?([^"\n]+)"?\s*$', body)
    port_match = re.search(r"(?m)^\s+port\s*:\s*(\d+)\s*$", body)

    host = host_match.group(1).strip() if host_match else None
    port = int(port_match.group(1)) if port_match else None
    return host, port


def _rewrite_server_block(config_text: str, host: str | None, port: int | None) -> str:
    lines = config_text.splitlines()
    normalized_lines: list[str] = []
    in_server_block = False

    for line in lines:
        stripped = line.strip()
        if stripped == "server:":
            in_server_block = True
            normalized_lines.append(line)
            continue

        if in_server_block:
            if host is not None and re.match(r"^\s+host\s*:", line):
                indent = line[: len(line) - len(line.lstrip())]
                normalized_lines.append(f'{indent}host: "{host}"')
                continue
            if port is not None and re.match(r"^\s+port\s*:", line):
                indent = line[: len(line) - len(line.lstrip())]
                normalized_lines.append(f"{indent}port: {port}")
                continue
            if stripped and not line.startswith((" ", "\t")):
                in_server_block = False

        normalized_lines.append(line)

    rewritten = "\n".join(normalized_lines)
    if config_text.endswith("\n"):
        rewritten += "\n"
    return rewritten


def _sync_packaged_server_config(source_config: Path, target_config: Path) -> bool:
    source_host, source_port = _extract_server_config_values(source_config)
    if source_host is None and source_port is None:
        return False

    if not target_config.exists():
        try:
            target_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_config, target_config)
            return True
        except Exception as exc:
            _log_startup(f"[启动] 初始化配置文件失败 {target_config}: {exc}")
            return False

    try:
        original_text = target_config.read_text(encoding="utf-8")
    except Exception as exc:
        _log_startup(f"[启动] 读取配置文件失败 {target_config}: {exc}")
        return False

    rewritten = _rewrite_server_block(original_text, source_host, source_port)
    if rewritten == original_text:
        return False

    try:
        target_config.write_text(rewritten, encoding="utf-8")
        return True
    except Exception as exc:
        _log_startup(f"[启动] 同步配置文件失败 {target_config}: {exc}")
        return False


def _sync_packaged_server_configs(source_dir: Path, target_dir: Path) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        return

    for relative_path in (Path("system_config.yaml"), Path("app_config.yaml")):
        source_config = source_dir / relative_path
        target_config = target_dir / relative_path
        if _sync_packaged_server_config(source_config, target_config):
            _log_startup(f"[启动] 已同步运行时配置: {target_config}")


def _extract_zip_if_needed(archive_path: Path, target_dir: Path) -> bool:
    if not archive_path.exists():
        return False
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(target_dir)
        return True
    except Exception as exc:
        _log_startup(f"[启动] 解压浏览器资源失败 {archive_path}: {exc}")
        return False


def _log_startup(message: str) -> None:
    print(message)


def _find_cloakbrowser_binary(cache_dir: Path) -> Path | None:
    candidate_patterns = [
        "chromium-*/chrome.exe",
        "chromium-*/chrome",
        "chromium-*/Chromium.app/Contents/MacOS/Chromium",
    ]
    for pattern in candidate_patterns:
        matches = sorted(cache_dir.glob(pattern))
        if matches:
            return matches[-1]
    return None


def _is_directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_test_{os.getpid()}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _resolve_persistent_root(application_path: Path) -> Path:
    candidates: list[Path] = []

    explicit_root = os.getenv("HUOKE_PERSISTENT_ROOT", "").strip()
    if explicit_root:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(explicit_root))).resolve())

    # 安装版优先把运行数据写入安装目录下的 _runtime，便于随安装目录整体迁移。
    candidates.append(application_path / "_runtime")

    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data).resolve() / APP_NAME)

    user_profile = os.getenv("USERPROFILE", "").strip()
    if user_profile:
        candidates.append(Path(user_profile).resolve() / "AppData" / "Local" / APP_NAME)

    try:
        candidates.append(Path.home().resolve() / "AppData" / "Local" / APP_NAME)
    except Exception:
        pass

    candidates.append(Path(tempfile.gettempdir()).resolve() / APP_NAME)

    for candidate in candidates:
        if _is_directory_writable(candidate):
            return candidate

    return application_path / "_runtime"


def clear_pycache():
    """清理Python缓存(__pycache__)，确保代码修改生效

    避免Python使用旧的.pyc文件导致代码修改不生效的问题。
    在每次启动时清理所有__pycache__目录。
    """
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
        src_path = os.path.join(base_dir, '_internal', 'src')
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        src_path = os.path.join(base_dir, 'src')

    cleaned_count = 0
    for root, dirs, files in os.walk(src_path):
        for d in dirs:
            if d == '__pycache__':
                cache_dir = os.path.join(root, d)
                try:
                    shutil.rmtree(cache_dir)
                    cleaned_count += 1
                except Exception as e:
                    print(f"[清理] 删除缓存目录失败 {cache_dir}: {e}")

    if cleaned_count > 0:
        print(f"[清理] 已清理 {cleaned_count} 个__pycache__目录")
    else:
        print("[清理] 无需清理缓存")


def _resolve_browser_host(host: str) -> str:
    return host if host not in {"0.0.0.0", "::"} else "127.0.0.1"


def _build_web_url(host: str, port: int, path: str = "/") -> str:
    browser_host = _resolve_browser_host(host)
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"http://{browser_host}:{port}{normalized_path}"


def _is_server_responsive(host: str, port: int, path: str = "/health", timeout: float = 2.0) -> bool:
    try:
        with urllib_request.urlopen(_build_web_url(host, port, path), timeout=timeout) as response:
            return 200 <= getattr(response, "status", 200) < 500
    except Exception:
        return False


def _looks_like_our_app_process(process: psutil.Process) -> bool:
    try:
        process_name = (process.name() or "").strip().lower()
    except Exception:
        process_name = ""

    if process_name == "huokesmartbot.exe":
        return True

    try:
        exe_name = Path(process.exe()).name.strip().lower()
    except Exception:
        exe_name = ""

    if exe_name == "huokesmartbot.exe":
        return True

    try:
        cmdline = " ".join(process.cmdline()).strip().lower()
    except Exception:
        cmdline = ""

    return "run_app.py" in cmdline or "huokesmartbot" in cmdline


def check_and_kill_port(port: int) -> bool:
    """检测端口是否被占用，如果被占用则尝试清理旧进程

    Args:
        port: 需要检测的端口号

    Returns:
        bool: 端口是否可用（True=可用，False=仍被占用）
    """
    try:
        pids_to_kill = set()
        foreign_pids = set()
        for connection in psutil.net_connections(kind="inet"):
            local_address = getattr(connection, "laddr", None)
            if not local_address or getattr(local_address, "port", None) != port:
                continue
            if connection.status != psutil.CONN_LISTEN:
                continue
            pid = connection.pid
            if pid and pid != os.getpid():
                try:
                    process = psutil.Process(int(pid))
                    if _looks_like_our_app_process(process):
                        pids_to_kill.add(pid)
                    else:
                        foreign_pids.add(pid)
                except Exception:
                    foreign_pids.add(pid)

        if foreign_pids:
            print(f"[端口] 端口 {port} 被非本程序进程占用: {sorted(foreign_pids)}")
            return False

        if not pids_to_kill:
            return True

        for pid in pids_to_kill:
            print(f"[端口] 检测到端口 {port} 被进程 PID={pid} 占用，尝试终止...")
            try:
                process = psutil.Process(int(pid))
                process.terminate()
                try:
                    process.wait(timeout=5)
                    print(f"[端口] 已终止旧进程 PID={pid}")
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    print(f"[端口] 已强制终止旧进程 PID={pid}")
            except Exception as e:
                print(f"[端口] 终止进程 PID={pid} 失败: {e}")
        time.sleep(1)

        for connection in psutil.net_connections(kind="inet"):
            local_address = getattr(connection, "laddr", None)
            if not local_address or getattr(local_address, "port", None) != port:
                continue
            if connection.status == psutil.CONN_LISTEN:
                print(f"[端口] 警告: 端口 {port} 仍被占用")
                return False

        print(f"[端口] 端口 {port} 已释放")
        return True

    except Exception as e:
        print(f"[端口] 端口检测异常: {e}")
        return False


def _open_url_in_browser(url: str) -> bool:
    """用系统默认/候选浏览器打开 URL，失败时逐个尝试 Edge / Chrome / Firefox。

    返回是否成功打开（仅在显式 webbrowser.open 抛异常或返回 False 时才视为失败）。
    """
    try:
        if webbrowser.open(url):
            print(f"[启动] 已自动打开页面（默认浏览器）: {url}")
            return True
    except Exception as e:
        print(f"[启动] webbrowser.open 失败: {e}")

    # 兜底：Windows 平台尝试显式启动 Edge / Chrome / Firefox
    if sys.platform == "win32":
        candidates = [
            (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
             "--new-window"),
            (os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
             "--new-window"),
            (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
             "--new-window"),
            (os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
             "--new-window"),
            (os.path.expandvars(r"%LocalAppData%\Mozilla Firefox\firefox.exe"),
             "-new-window"),
        ]
        for exe, arg in candidates:
            if exe and Path(exe).exists():
                try:
                    import subprocess
                    subprocess.Popen(
                        [exe, arg, url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    print(f"[启动] 已通过候选浏览器打开: {exe} -> {url}")
                    return True
                except Exception as e:
                    print(f"[启动] 候选浏览器 {exe} 启动失败: {e}")
    return False


def auto_open_web_ui(host: str, port: int) -> None:
    """后台等待Web服务就绪后自动打开浏览器。

    改进点：
    1. 探活优先访问 /health（轻量），避免在首页 Content-Length 异常时误判
    2. webbrowser.open 加多浏览器 fallback
    3. 整个过程显式 print，方便用户观察启动状态
    """
    if os.getenv("HUOKE_AUTO_OPEN_BROWSER", "1").strip().lower() in {"0", "false", "no"}:
        print("[启动] HUOKE_AUTO_OPEN_BROWSER=0，跳过自动打开浏览器")
        return

    url = _build_web_url(host, port)
    health_url = _build_web_url(host, port, "/health")
    print(f"[启动] 计划自动打开页面: {url}")

    def _wait_and_open() -> None:
        opened = False
        for attempt in range(1, 61):
            try:
                with urllib_request.urlopen(health_url, timeout=2) as response:
                    if 200 <= getattr(response, "status", 200) < 500:
                        print(f"[启动] /health 第 {attempt} 次探活成功，准备打开浏览器")
                        if _open_url_in_browser(url):
                            opened = True
                        return
            except Exception:
                time.sleep(1)
        if not opened:
            print(f"[启动] 页面未在 60 秒内就绪，请手动打开: {url}")

    threading.Thread(target=_wait_and_open, name="huoke-browser-opener", daemon=True).start()


def reuse_existing_instance_if_running(host: str, port: int) -> bool:
    """重复启动时复用已运行实例，避免新实例争抢端口导致服务闪退。"""
    if not _is_server_responsive(host, port):
        return False

    url = _build_web_url(host, port)
    print(f"[启动] 检测到已运行实例，复用现有服务: {url}")
    if os.getenv("HUOKE_AUTO_OPEN_BROWSER", "1").strip().lower() not in {"0", "false", "no"}:
        webbrowser.open(url)
    return True

def setup_frozen_env():
    """配置PyInstaller frozen环境"""
    if getattr(sys, 'frozen', False):
        application_path = Path(sys.executable).resolve().parent
        internal_path = application_path / "_internal"
        try:
            mp.set_executable(sys.executable)
        except Exception:
            pass
        sys._MEIPASS = str(internal_path)

        src_path = internal_path / "src"
        if src_path.exists() and str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))
        if str(application_path) not in sys.path:
            sys.path.insert(0, str(application_path))

        data_dir_env = os.getenv("HUOKE_DATA_DIR", "").strip()
        log_dir_env = os.getenv("HUOKE_LOG_DIR", "").strip()
        config_dir_env = os.getenv("HUOKE_CONFIG_DIR", "").strip()
        preferred_runtime_root = application_path / "_runtime"
        persistent_root = _resolve_persistent_root(application_path)

        data_dir = (
            Path(os.path.expandvars(os.path.expanduser(data_dir_env))).resolve()
            if data_dir_env
            else persistent_root / "data"
        )
        log_dir = (
            Path(os.path.expandvars(os.path.expanduser(log_dir_env))).resolve()
            if log_dir_env
            else persistent_root / "logs"
        )
        config_dir = (
            Path(os.path.expandvars(os.path.expanduser(config_dir_env))).resolve()
            if config_dir_env
            else persistent_root / "config"
        )
        persistent_root.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        if persistent_root == preferred_runtime_root:
            _log_startup(f"[启动] 运行数据目录: {persistent_root} (安装目录内)")
        else:
            _log_startup(
                f"[启动] 安装目录不可写，运行数据已回退到可写目录: {persistent_root}"
            )

        try:
            from src.infrastructure.frozen_data_migration import migrate_legacy_runtime_data

            migrate_legacy_runtime_data(
                application_path=application_path,
                persistent_data_dir=data_dir,
                working_dir=Path.cwd(),
                logger=_log_startup,
            )
        except Exception as exc:
            _log_startup(f"[迁移] 旧运行时数据迁移失败，继续使用当前目录: {exc}")

        static_dir = internal_path / "src" / "web" / "static"
        templates_dir = internal_path / "src" / "web" / "templates"
        if not static_dir.exists():
            alt_static = application_path / "static"
            if alt_static.exists():
                static_dir = alt_static
        if not templates_dir.exists():
            alt_templates = application_path / "templates"
            if alt_templates.exists():
                templates_dir = alt_templates

        bundled_config_dir = internal_path / "config"
        if not bundled_config_dir.exists():
            bundled_config_dir = application_path / "config"
        bundled_data_dir = internal_path / "data"
        if not bundled_data_dir.exists():
            bundled_data_dir = application_path / "data"
        _copy_tree_contents_if_missing(bundled_config_dir, config_dir)
        _sync_packaged_server_configs(bundled_config_dir, config_dir)
        _copy_if_missing(bundled_data_dir / "knowledge_base.json", data_dir / "knowledge_base.json")

        os.environ["HUOKE_ENV_FILE"] = str(application_path / ".env")

        # Frozen installs must prefer bundled browser/runtime resources so the
        # build can be verified on a clean machine instead of borrowing local
        # developer configuration.
        os.environ.pop("BROWSER_EXECUTABLE_PATH", None)
        os.environ.pop("CLOAKBROWSER_BINARY_PATH", None)
        os.environ.pop("CLOAKBROWSER_CACHE_DIR", None)

        persistent_cloakbrowser_dir = persistent_root / "cloakbrowser"
        bundled_cloakbrowser_archive = internal_path / "browser_payload" / "cloakbrowser_bundle.zip"
        if not bundled_cloakbrowser_archive.exists():
            bundled_cloakbrowser_archive = application_path / "browser_payload" / "cloakbrowser_bundle.zip"

        bundled_cloakbrowser_dir = internal_path / "cloakbrowser"
        if not bundled_cloakbrowser_dir.exists():
            bundled_cloakbrowser_dir = application_path / "cloakbrowser"

        # 发布版优先使用我们显式生成的 browser payload，避免 Python 包自带的
        # cloakbrowser 资源（版本可能不同）覆盖掉已验证的离线浏览器目录。
        if bundled_cloakbrowser_archive.exists():
            if persistent_cloakbrowser_dir.exists():
                shutil.rmtree(persistent_cloakbrowser_dir, ignore_errors=True)
            _extract_zip_if_needed(bundled_cloakbrowser_archive, persistent_cloakbrowser_dir)
        elif bundled_cloakbrowser_dir.exists():
            _copy_tree_contents_if_missing(bundled_cloakbrowser_dir, persistent_cloakbrowser_dir)
        if persistent_cloakbrowser_dir.exists():
            os.environ["CLOAKBROWSER_CACHE_DIR"] = str(persistent_cloakbrowser_dir)
            bundled_binary = _find_cloakbrowser_binary(persistent_cloakbrowser_dir)
            if bundled_binary and bundled_binary.exists():
                os.environ["CLOAKBROWSER_BINARY_PATH"] = str(bundled_binary)

        os.environ['HUOKE_BASE_DIR'] = str(application_path)
        os.environ['HUOKE_PERSISTENT_ROOT'] = str(persistent_root)
        os.environ['HUOKE_STATIC_DIR'] = str(static_dir)
        os.environ['HUOKE_TEMPLATES_DIR'] = str(templates_dir)
        os.environ['HUOKE_DATA_DIR'] = str(data_dir)
        os.environ['HUOKE_LOG_DIR'] = str(log_dir)
        os.environ['HUOKE_CONFIG_DIR'] = str(config_dir)

        models_dir = application_path / "models"
        if not models_dir.exists():
            internal_models_dir = internal_path / "models"
            if internal_models_dir.exists():
                models_dir = internal_models_dir
        if models_dir.exists():
            os.environ['HUOKE_MODELS_DIR'] = str(models_dir)

        kb_data_dir = application_path / "kb_data"
        if not kb_data_dir.exists():
            internal_kb_data_dir = internal_path / "kb_data"
            if internal_kb_data_dir.exists():
                kb_data_dir = internal_kb_data_dir
        if kb_data_dir.exists():
            os.environ['HUOKE_KB_DATA_DIR'] = str(kb_data_dir)

        print(f"[启动] 应用目录: {application_path}")
        print(f"[启动] 数据目录: {data_dir}")
        print(f"[启动] 日志目录: {log_dir}")
        print(f"[启动] 配置目录: {config_dir}")
        print(f"[启动] 内部目录: {sys._MEIPASS}")
        if os.getenv("CLOAKBROWSER_CACHE_DIR", "").strip():
            print(f"[启动] CloakBrowser缓存目录: {os.environ['CLOAKBROWSER_CACHE_DIR']}")
        if os.getenv("CLOAKBROWSER_BINARY_PATH", "").strip():
            print(f"[启动] CloakBrowser二进制: {os.environ['CLOAKBROWSER_BINARY_PATH']}")
    else:
        application_path = Path(__file__).resolve().parent
        os.environ['HUOKE_BASE_DIR'] = str(application_path)
        os.environ.setdefault("HUOKE_ENV_FILE", str(application_path / ".env"))


def ensure_directories():
    """确保必要的目录存在"""
    from src.infrastructure.runtime_paths import ensure_runtime_directories

    ensure_runtime_directories()


def check_ollama_service():
    """检查Ollama服务是否运行"""
    import requests
    ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        response = requests.get(f"{ollama_host}/api/tags", timeout=3)
        if response.status_code == 200:
            models = response.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            print(f"[检查] Ollama服务正常，可用模型: {', '.join(model_names[:5])}")
            return True
    except Exception as e:
        print(f"[警告] Ollama服务未运行或不可访问: {e}")
        print("[提示] 请确保Ollama已安装并运行: ollama serve")
        return False
    return False


def check_knowledge_base():
    """检查知识库数据"""
    import json
    from src.infrastructure.runtime_paths import get_knowledge_base_path

    kb_file = get_knowledge_base_path()

    if kb_file.exists():
        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            count = len(data) if isinstance(data, list) else 0
            print(f"[检查] 知识库已加载，包含 {count} 条知识")
            return count > 0
        except Exception as e:
            print(f"[警告] 知识库读取失败: {e}")
            return False
    else:
        print("[警告] 知识库文件不存在")
        return False


ensure_stdio_streams()
setup_frozen_env()
ensure_directories()
clear_pycache()


def _graceful_shutdown(signum, frame):
    """优雅关闭信号处理器，确保应用状态被保存

    使用已有的BotService单例实例保存状态，而非创建新实例。
    新实例的is_running=False会导致状态保存被完全跳过。
    """
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    print(f"\n[信号] 收到 {sig_name} 信号，开始优雅关闭...")

    try:
        from src.web.bot_service import BotService
        if BotService._instance is not None and getattr(BotService._instance, '_initialized', False):
            instance = BotService._instance
            if instance.is_running:
                print("[关闭] 保存BotService状态...")
                try:
                    instance._save_state_before_shutdown()
                    print("[关闭] BotService状态已保存")
                except Exception as e:
                    print(f"[关闭] 保存BotService状态失败: {e}")
            else:
                print("[关闭] BotService未在运行，跳过状态保存")
        else:
            print("[关闭] BotService未初始化，跳过状态保存")
    except Exception as e:
        print(f"[关闭] BotService状态保存跳过: {e}")

    try:
        from src.common.process_manager import shutdown_process_manager_for_exit

        shutdown_process_manager_for_exit()
        print("[关闭] 独立进程管理器已清理")
    except Exception as e:
        print(f"[关闭] 独立进程管理器清理跳过: {e}")

    try:
        from src.douyin_bot.app_state_persistor import get_app_state_persistor
        persistor = get_app_state_persistor()
        persistor.stop_auto_save()
        print("[关闭] 状态自动保存已停止")
    except Exception as e:
        print(f"[关闭] 停止状态自动保存跳过: {e}")

    print("[关闭] 优雅关闭完成，进程退出")
    sys.exit(0)


signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)

from src.web.main import parse_web_server_args, start_web_server

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("智能知识库系统 - 启动检查")
    print("=" * 60)

    ollama_ok = check_ollama_service()
    kb_ok = check_knowledge_base()

    print("=" * 60)

    if not ollama_ok:
        print("[提示] LLM服务将尝试使用云端API作为备选")

    if not kb_ok:
        print("[提示] 知识库为空，部分功能可能受限")

    cli_args = parse_web_server_args(sys.argv[1:])
    host = cli_args.host
    port = cli_args.port

    if reuse_existing_instance_if_running(host, port):
        sys.exit(0)

    port_ready = check_and_kill_port(port)
    if not port_ready:
        if reuse_existing_instance_if_running(host, port):
            sys.exit(0)
        print(f"[启动] 端口 {port} 仍不可用，取消本次启动，避免打开不可用页面")
        sys.exit(1)

    auto_open_web_ui(host, port)

    print("\n[启动] 正在启动Web服务...")
    start_web_server(host=host, port=port)
