import os
import shutil
from pathlib import Path
from typing import Optional
from cloakbrowser import launch_persistent_context
from cloakbrowser.config import get_binary_path, get_cache_dir
from playwright.sync_api import BrowserContext, Page
from loguru import logger
from src.config.settings import (
    CLOAKBROWSER_BINARY_PATH,
    CLOAKBROWSER_CACHE_DIR,
    CLOAKBROWSER_DOWNLOAD_URL,
    HEADLESS,
    USER_DATA_DIR,
    VIEWPORT_SIZE,
    MOBILE_VIEWPORT_SIZE,
    MOBILE_USER_AGENT,
    USE_MOBILE_MODE,
    DOUYIN_HOME_URL,
    DOUYIN_CHAT_URL,
    SERVER_PORT,
)

class BrowserManager:
    """
    浏览器管理类，负责 CloakBrowser 持久化上下文的初始化和管理。

    支持多标签页：
    - 主页面 (page): 用于登录检测和备用
    - 搜索页面 (search_page): 用于综合页流式发现相关视频
    - 爬取页面 (crawler_page): 用于进入视频详情页抓评论、发送私信
    - 自动回复页面 (monitor_page): 用于消息回复、自动回复发送
    多个标签页共享同一个浏览器上下文（共享登录状态）
    """
    PAGE_ROLE_PREFIX = "HUOKE_PAGE_ROLE:"
    MANAGED_BROWSER_NAMES = {"chrome.exe", "msedge.exe", "chromium.exe", "chrome", "msedge", "chromium"}

    @staticmethod
    def _find_cached_binary(cache_dir: str) -> Path | None:
        cache_path = Path(str(cache_dir or "").strip())
        if not cache_path.exists():
            return None
        candidate_patterns = [
            "chromium-*/chrome.exe",
            "chromium-*/chrome",
            "chromium-*/Chromium.app/Contents/MacOS/Chromium",
        ]
        for pattern in candidate_patterns:
            matches = sorted(cache_path.glob(pattern))
            if matches:
                return matches[-1]
        return None

    def __init__(self, user_data_dir=None):
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.search_page: Optional[Page] = None
        self.monitor_page: Optional[Page] = None
        self.crawler_page: Optional[Page] = None
        self.user_data_dir = Path(user_data_dir or USER_DATA_DIR)
        self._monitoring_active = False

    def open_url_in_new_tab(self, url: str) -> Page:
        """在当前抖音浏览器上下文中新开标签页并打开指定链接。"""
        target_url = str(url or "").strip()
        if not target_url:
            raise ValueError("URL不能为空")
        if not self.context:
            raise RuntimeError("浏览器上下文未初始化")

        logger.info(f"在现有浏览器中新开标签页: {target_url}")
        new_page = self.context.new_page()
        try:
            try:
                new_page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
            except Exception as nav_error:
                current_url = ""
                try:
                    current_url = self._get_page_url(new_page)
                except Exception:
                    current_url = ""
                logger.warning(
                    f"新标签页导航超时或异常，继续保留标签页: {nav_error}; "
                    f"current_url={current_url or 'about:blank'}"
                )
            try:
                new_page.bring_to_front()
            except Exception:
                pass
            return new_page
        except Exception:
            if new_page and not new_page.is_closed():
                try:
                    new_page.close()
                except Exception:
                    pass
            raise

    @staticmethod
    def _prepare_cloakbrowser_runtime_env() -> None:
        """在启动前补齐 CloakBrowser 运行时环境变量。

        修复跨电脑部署问题：当 CLOAKBROWSER_BINARY_PATH 指向的文件不存在时，
        不再直接抛 FileNotFoundError，而是清空该变量并降级到系统浏览器自动探测，
        避免因开发机硬编码的 Chrome 路径在目标机器上不存在而直接崩溃。
        """
        binary_path = str(os.getenv("CLOAKBROWSER_BINARY_PATH", "") or CLOAKBROWSER_BINARY_PATH or "").strip()
        cache_dir = str(os.getenv("CLOAKBROWSER_CACHE_DIR", "") or CLOAKBROWSER_CACHE_DIR or "").strip()
        download_url = str(os.getenv("CLOAKBROWSER_DOWNLOAD_URL", "") or CLOAKBROWSER_DOWNLOAD_URL or "").strip()

        if binary_path:
            if not os.path.exists(binary_path):
                # 修复：硬编码的浏览器路径在目标机器上不存在时，降级到系统浏览器自动探测
                logger.warning(
                    f"CLOAKBROWSER_BINARY_PATH 指向的文件不存在: {binary_path}，"
                    f"将自动降级到系统浏览器探测（Chrome/Edge）"
                )
                os.environ.pop("CLOAKBROWSER_BINARY_PATH", None)
            else:
                os.environ["CLOAKBROWSER_BINARY_PATH"] = binary_path
                logger.info(f"使用本地 CloakBrowser 二进制: {binary_path}")

        if cache_dir:
            os.environ["CLOAKBROWSER_CACHE_DIR"] = cache_dir
            logger.info(f"使用 CloakBrowser 缓存目录: {cache_dir}")
            if not os.environ.get("CLOAKBROWSER_BINARY_PATH"):
                cached_binary = BrowserManager._find_cached_binary(cache_dir)
                if cached_binary and cached_binary.exists():
                    os.environ["CLOAKBROWSER_BINARY_PATH"] = str(cached_binary)
                    logger.info(f"使用缓存中的 CloakBrowser 二进制: {cached_binary}")

        if download_url:
            os.environ["CLOAKBROWSER_DOWNLOAD_URL"] = download_url
            logger.info(f"使用自定义 CloakBrowser 下载地址: {download_url}")

    @staticmethod
    def _iter_system_browser_binary_candidates() -> list[Path]:
        candidates: list[Path] = []

        def _append(path_value: str) -> None:
            raw = str(path_value or "").strip()
            if not raw:
                return
            candidate = Path(raw).expanduser()
            if candidate not in candidates:
                candidates.append(candidate)

        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base_dir = os.getenv(env_key, "").strip()
            if not base_dir:
                continue
            _append(Path(base_dir) / "Google" / "Chrome" / "Application" / "chrome.exe")
            _append(Path(base_dir) / "Microsoft" / "Edge" / "Application" / "msedge.exe")

        for command_name in ("chrome.exe", "chrome", "msedge.exe", "msedge"):
            resolved = shutil.which(command_name)
            if resolved:
                _append(resolved)

        return candidates

    @classmethod
    def _resolve_system_browser_binary(cls, exclude_paths=None) -> Path | None:
        excluded = {
            str(Path(path).expanduser().resolve()).lower()
            for path in (exclude_paths or [])
            if str(path or "").strip()
        }
        for candidate in cls._iter_system_browser_binary_candidates():
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            if not resolved.exists():
                continue
            if str(resolved).lower() in excluded:
                continue
            return resolved
        return None

    @staticmethod
    def _looks_like_bundled_cloakbrowser_binary(binary_path: str) -> bool:
        normalized = str(binary_path or "").strip().lower().replace("/", "\\")
        if not normalized:
            return False
        if "\\google\\chrome\\application\\" in normalized or "\\microsoft\\edge\\application\\" in normalized:
            return False
        if "\\cloakbrowser\\" in normalized or "\\models\\cloakbrowser\\" in normalized:
            return True
        return "\\chromium-" in normalized and normalized.endswith("\\chrome.exe")

    @classmethod
    def _should_retry_with_system_browser(cls, exc: Exception, binary_path: str) -> bool:
        if not cls._looks_like_bundled_cloakbrowser_binary(binary_path):
            return False
        error_text = str(exc or "").lower()
        return any(
            marker in error_text
            for marker in (
                "timeout",
                "network service crashed",
                "v8_initializer",
                "startup snapshot",
                "browsertype.launch_persistent_context",
            )
        )

    @staticmethod
    def _is_profile_crypto_incompatibility(error: Exception) -> bool:
        """判断启动失败是否由跨机器复制 profile 导致的本地解密不兼容。"""
        error_text = str(error or "").lower()
        return any(
            marker in error_text
            for marker in (
                "os_crypt",
                "failed to decrypt",
                "0x8009000b",
                "该 项不适于在指定状态下使用",
            )
        )

    def _backup_incompatible_user_data_dir(self) -> Path | None:
        """备份当前不兼容的浏览器 profile，并为本机创建新的空 profile 目录。"""
        if not self.user_data_dir.exists():
            return None

        try:
            self.cleanup_stale_managed_browser_processes(user_data_dirs=[self.user_data_dir])
        except Exception as cleanup_error:
            logger.warning(f"备份异常 profile 前清理残留进程失败: {cleanup_error}")

        backup_base = self.user_data_dir.with_name(f"{self.user_data_dir.name}_incompatible_backup")
        backup_path = backup_base
        backup_index = 1
        while backup_path.exists():
            backup_path = self.user_data_dir.with_name(
                f"{self.user_data_dir.name}_incompatible_backup_{backup_index}"
            )
            backup_index += 1

        shutil.move(str(self.user_data_dir), str(backup_path))
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        return backup_path

    @staticmethod
    def _build_cloakbrowser_startup_error(error: Exception) -> RuntimeError:
        error_text = str(error or "").strip()
        binary_hint = str(CLOAKBROWSER_BINARY_PATH or os.getenv("CLOAKBROWSER_BINARY_PATH", "")).strip()
        cache_dir = str(get_cache_dir())
        expected_binary = str(get_binary_path())
        download_url = str(CLOAKBROWSER_DOWNLOAD_URL or os.getenv("CLOAKBROWSER_DOWNLOAD_URL", "")).strip()

        hint_lines = [
            "CloakBrowser 启动失败，当前阻塞在浏览器二进制准备阶段。",
            f"原始错误: {error_text or repr(error)}",
            f"默认缓存目录: {cache_dir}",
            f"预期二进制路径: {expected_binary}",
        ]

        if binary_hint:
            hint_lines.append(f"当前本地二进制配置: {binary_hint}")
        else:
            hint_lines.append("未配置本地二进制路径，可设置 CLOAKBROWSER_BINARY_PATH。")

        if download_url:
            hint_lines.append(f"当前自定义下载地址: {download_url}")
        else:
            hint_lines.append("如网络受限，可设置 CLOAKBROWSER_DOWNLOAD_URL 指向可访问镜像。")

        hint_lines.extend(
            [
                "可选解决方案:",
                "1. 预先下载 CloakBrowser 二进制并设置 CLOAKBROWSER_BINARY_PATH",
                "2. 预热/复制缓存目录到 CLOAKBROWSER_CACHE_DIR",
                "3. 提供可访问镜像并设置 CLOAKBROWSER_DOWNLOAD_URL",
            ]
        )

        return RuntimeError("\n".join(hint_lines))

    def launch_browser(self):
        """兼容旧调用的别名"""
        self.start()
        return self.context

    def export_cookies(self):
        if not self.context:
            return []
        try:
            return list(self.context.cookies() or [])
        except Exception as exc:
            logger.warning(f"导出浏览器 cookies 失败: {exc}")
            return []

    def import_cookies(self, cookies) -> int:
        if not self.context:
            return 0

        normalized = []
        for item in cookies or []:
            if not isinstance(item, dict):
                continue
            if not item.get("name") or not item.get("domain"):
                continue
            normalized.append(dict(item))

        if not normalized:
            return 0

        try:
            self.context.add_cookies(normalized)
            logger.info(f"已导入浏览器 cookies: count={len(normalized)}")
            return len(normalized)
        except Exception as exc:
            logger.warning(f"导入浏览器 cookies 失败: {exc}")
            return 0

    @classmethod
    def _is_managed_browser_process(cls, process, user_data_dirs=None) -> bool:
        try:
            name = str(getattr(process, "name", lambda: "")() or "").strip().lower()
        except Exception:
            name = ""
        if name not in cls.MANAGED_BROWSER_NAMES:
            return False

        try:
            cmdline_parts = process.cmdline()
        except Exception:
            return False

        cmdline = " ".join(str(part or "") for part in cmdline_parts).lower()
        candidate_dirs = user_data_dirs or [USER_DATA_DIR]
        normalized_dirs = [
            str(path or "").strip().lower()
            for path in candidate_dirs
            if str(path or "").strip()
        ]
        return any(user_data_dir in cmdline for user_data_dir in normalized_dirs)

    @classmethod
    def cleanup_stale_managed_browser_processes(cls, exclude_pids=None, user_data_dirs=None) -> int:
        """清理遗留的自动化浏览器进程，避免跨实例阻塞。"""
        exclude_pid_set = {int(pid) for pid in (exclude_pids or []) if pid}
        try:
            import psutil
        except Exception as e:
            logger.warning(f"无法导入 psutil，跳过浏览器残留清理: {e}")
            return 0

        cleaned = 0
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = int(getattr(process, "pid", 0) or 0)
            if pid in exclude_pid_set:
                continue
            if not cls._is_managed_browser_process(process, user_data_dirs=user_data_dirs):
                continue
            try:
                logger.warning(f"发现遗留浏览器进程，准备终止: pid={pid}")
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)
                cleaned += 1
            except psutil.NoSuchProcess:
                cleaned += 1
            except Exception as e:
                logger.warning(f"终止遗留浏览器进程失败: pid={pid}, error={e}")
        return cleaned

    def start(self, mobile_mode: bool = None):
        """
        启动浏览器并加载持久化上下文。

        Args:
            mobile_mode: 是否使用移动端模式，为None时使用配置文件的USE_MOBILE_MODE
        """
        try:
            self._start_automated_browser(mobile_mode)
        except Exception as e:
            import traceback
            logger.error(f"浏览器启动失败: {e}")
            logger.error(traceback.format_exc())
            raise

    @staticmethod
    def _get_page_url(page: Page) -> str:
        try:
            return str(page.url or "").strip()
        except Exception:
            return ""

    def _get_page_role(self, page: Page) -> str:
        if not page or page.is_closed():
            return ""
        role = str(getattr(page, "_huoke_page_role", "") or "").strip()
        if role:
            return role
        try:
            marker = str(page.evaluate("() => window.name || ''") or "").strip()
        except Exception:
            marker = ""
        if marker.startswith(self.PAGE_ROLE_PREFIX):
            role = marker[len(self.PAGE_ROLE_PREFIX):].strip()
            if role:
                try:
                    setattr(page, "_huoke_page_role", role)
                except Exception:
                    pass
                return role
        return ""

    def _set_page_role(self, page: Page, role: str) -> None:
        if not page or page.is_closed():
            return
        normalized = str(role or "").strip()
        if not normalized:
            return
        try:
            setattr(page, "_huoke_page_role", normalized)
        except Exception:
            pass
        try:
            page.evaluate(
                """role => {
                    try {
                        window.name = 'HUOKE_PAGE_ROLE:' + role;
                        return window.name;
                    } catch (e) {
                        return '';
                    }
                }""",
                normalized,
            )
        except Exception:
            pass

    def _clear_page_role(self, page: Page) -> None:
        if not page or page.is_closed():
            return
        try:
            setattr(page, "_huoke_page_role", "")
        except Exception:
            pass
        try:
            current_marker = str(page.evaluate("() => window.name || ''") or "").strip()
            if current_marker.startswith(self.PAGE_ROLE_PREFIX):
                page.evaluate("() => { try { window.name = ''; } catch (e) {} }")
        except Exception:
            pass

    def _is_preferred_douyin_page(self, page: Page) -> bool:
        if not page or page.is_closed():
            return False
        url = self._get_page_url(page)
        if not url:
            return False
        if url.startswith("devtools://"):
            return False
        return "douyin.com" in url

    def _select_existing_page(self, exclude_pages=None) -> Page:
        """
        优先复用现有抖音页，避免重复新建标签页导致上下文膨胀。
        """
        if not self.context:
            return None

        excluded = {id(page) for page in (exclude_pages or []) if page}
        candidates = []
        for page in self.context.pages:
            if not page or page.is_closed() or id(page) in excluded:
                continue
            url = self._get_page_url(page)
            if not url or url.startswith("devtools://"):
                continue
            if "douyin.com" not in url:
                continue
            candidates.append((page, url))

        if not candidates:
            return None

        def score(item):
            _, url = item
            if "/?" in url or "recommend=1" in url:
                return 0
            if "/video/" in url:
                return 1
            if "/chat" in url:
                return 2
            return 3

        candidates.sort(key=score)
        return candidates[0][0]

    def _select_existing_unclaimed_page(self, exclude_pages=None) -> Page:
        """为爬取/搜索流程挑选未被其他角色占用的非聊天抖音页。"""
        if not self.context:
            return None

        excluded = {id(page) for page in (exclude_pages or []) if page}
        owned_candidates = []
        unclaimed_candidates = []
        for page in self.context.pages:
            if not page or page.is_closed() or id(page) in excluded:
                continue
            url = self._get_page_url(page)
            if not url or url.startswith("devtools://"):
                continue
            if "douyin.com" not in url or "/chat" in url:
                continue
            role = self._get_page_role(page)
            if role in {"crawler", "search"}:
                owned_candidates.append((page, url))
            elif not role:
                unclaimed_candidates.append((page, url))

        candidates = owned_candidates or unclaimed_candidates
        if not candidates:
            return None

        def score(item):
            _, url = item
            if "/?" in url or "recommend=1" in url:
                return 0
            if "/video/" in url:
                return 1
            return 2

        candidates.sort(key=score)
        return candidates[0][0]

    def _select_existing_chat_page(self, exclude_pages=None) -> Page:
        """优先复用已打开的抖音聊天页。"""
        if not self.context:
            return None

        excluded = {id(page) for page in (exclude_pages or []) if page}
        owned_candidates = []
        unclaimed_candidates = []
        for page in self.context.pages:
            if not page or page.is_closed() or id(page) in excluded:
                continue
            url = self._get_page_url(page)
            if not url or url.startswith("devtools://"):
                continue
            if "douyin.com" in url and "/chat" in url:
                role = self._get_page_role(page)
                if role == "monitor":
                    owned_candidates.append(page)
                elif not role:
                    unclaimed_candidates.append(page)
        if owned_candidates:
            return owned_candidates[0]
        if unclaimed_candidates:
            return unclaimed_candidates[0]
        return None

    def _cleanup_blank_pages(self, keep_pages=None) -> None:
        """清理孤立的 about:blank 页面，避免启动后遗留空白标签页。"""
        if not self.context:
            return

        keep_ids = {id(page) for page in (keep_pages or []) if page}
        live_pages = [page for page in self.context.pages if page and not page.is_closed()]
        real_pages = []
        for page in live_pages:
            url = self._get_page_url(page)
            if url and url != "about:blank" and not url.startswith("devtools://"):
                real_pages.append(page)

        # 如果当前上下文里还没有任何真实页面，不关闭唯一空白页，避免把浏览器清空。
        if not real_pages:
            return

        for page in live_pages:
            if id(page) in keep_ids:
                continue
            url = self._get_page_url(page)
            if url != "about:blank":
                continue
            try:
                logger.info("关闭孤立的空白标签页 about:blank")
                page.close()
            except Exception as e:
                logger.warning(f"关闭空白标签页失败: {e}")

    def _ensure_home_page_for_main_entry(self, selected_page: Page) -> Page:
        """启动抖音时优先把主入口放到首页，避免直接落到上次聊天会话。"""
        if not self.context:
            return selected_page

        url = self._get_page_url(selected_page)
        if selected_page and not selected_page.is_closed() and "douyin.com" in url and "/chat" not in url:
            return selected_page

        reusable_page = self._select_existing_page(exclude_pages=[selected_page])
        reusable_url = self._get_page_url(reusable_page) if reusable_page else ""
        if reusable_page and "douyin.com" in reusable_url and "/chat" not in reusable_url:
            try:
                reusable_page.bring_to_front()
            except Exception:
                pass
            logger.info(f"主入口命中了聊天页，复用现有非聊天抖音页作为首页入口: {reusable_url}")
            return reusable_page

        try:
            new_page = self.context.new_page()
            logger.info("主入口命中了聊天页，改为新开首页标签页避免卡在历史会话")
            new_page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                new_page.bring_to_front()
            except Exception:
                pass
            self._cleanup_blank_pages(keep_pages=[selected_page, new_page])
            return new_page
        except Exception as e:
            logger.warning(f"创建首页标签页失败，回退到现有页面: {e}")
            if selected_page and not selected_page.is_closed():
                try:
                    selected_page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    return selected_page
                except Exception as nav_e:
                    logger.warning(f"回退导航首页失败: {nav_e}")
            return selected_page

    def _start_automated_browser(self, mobile_mode: bool = None):
        """
        通过 CloakBrowser 直启持久化浏览器上下文。
        """
        if mobile_mode is None:
            mobile_mode = USE_MOBILE_MODE
        
        viewport = MOBILE_VIEWPORT_SIZE if mobile_mode else VIEWPORT_SIZE
        user_agent = MOBILE_USER_AGENT if mobile_mode else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        mode_str = "移动端(iPhone X)" if mobile_mode else "桌面端"
        
        logger.info(f"正在启动自动化浏览器 ({mode_str})...")
        
        if not self.user_data_dir.exists():
            self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self._prepare_cloakbrowser_runtime_env()
        configured_binary = str(os.getenv("CLOAKBROWSER_BINARY_PATH", "") or "").strip()

        launch_args = [
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--disable-web-security",
        ]

        # [DEBUG-INST:empty-line-diagnosis] 可选的 Chrome DevTools 远程调试端口。
        # 默认不启用。诊断"空一行"问题时设环境变量 RPA_DEBUG_PORT=9222，
        # 然后用户可在浏览器打开 chrome://inspect 接管 RPA 浏览器看真实 DOM。
        # 收敛前：无法从外部接管 RPA 浏览器，诊断只能靠模拟 mock
        # 收敛后：通过环境变量启用 debug port，外部可以实时看真实 input_box DOM
        debug_port = str(os.getenv("RPA_DEBUG_PORT", "") or "").strip()
        if debug_port:
            launch_args.append(f"--remote-debugging-port={debug_port}")
            launch_args.append("--remote-debugging-address=0.0.0.0")
            logger.warning(
                f"[DEBUG-INST] 已启用 RPA 浏览器远程调试端口: {debug_port}，"
                f"可访问 chrome://inspect 接管浏览器（注意：任何能访问此端口的客户端都能控制浏览器）"
            )
        
        if mobile_mode:
            launch_args.append("--start-fullscreen")
        else:
            launch_args.extend([
                "--start-maximized",
                f"--window-size={viewport['width']},{viewport['height']}"
            ])
            
        # [FIX-INST:start-reply-bug] 启动超时从 30s 提升到 90s：系统 Chrome 首次启动
        # （含大量 disable/enable features、--no-sandbox 重复项、profile 兼容初始化）
        # 经常超过 30s；30s 超时后 Playwright 与 Chrome 进程握手未完成，DevTools 已监听
        # 也会抛 TimeoutError，触发"浏览器启动失败"使自动回复链路无法开启。
        launch_kwargs = dict(
            headless=HEADLESS,
            viewport=None if not mobile_mode else viewport,
            args=launch_args,
            user_agent=user_agent,
            accept_downloads=True,
            timeout=90000,
        )

        try:
            self.context = launch_persistent_context(str(self.user_data_dir), **launch_kwargs)
        except Exception as exc:
            fallback_binary = self._resolve_system_browser_binary(exclude_paths=[configured_binary] if configured_binary else None)
            if self._is_profile_crypto_incompatibility(exc):
                backup_path = self._backup_incompatible_user_data_dir()
                if backup_path:
                    logger.warning(
                        "检测到浏览器 profile 与当前机器的本地加密环境不兼容，"
                        f"已备份旧 profile 并使用新 profile 重试: {backup_path}"
                    )
                    self.context = launch_persistent_context(str(self.user_data_dir), **launch_kwargs)
                else:
                    raise self._build_cloakbrowser_startup_error(exc) from exc
            elif fallback_binary and (
                not configured_binary
                or self._should_retry_with_system_browser(exc, configured_binary)
            ):
                # 修复：configured_binary 为空（路径不存在被清空）时也允许走系统浏览器兜底
                reason = "CLOAKBROWSER_BINARY_PATH 未配置或指向不存在的路径" if not configured_binary else "Bundled CloakBrowser 启动失败"
                logger.warning(
                    f"{reason}，切换到系统浏览器重试: "
                    f"from={configured_binary or '(empty)'} to={fallback_binary}"
                )
                os.environ["CLOAKBROWSER_BINARY_PATH"] = str(fallback_binary)
                os.environ.pop("CLOAKBROWSER_CACHE_DIR", None)
                self.context = launch_persistent_context(str(self.user_data_dir), **launch_kwargs)
            else:
                raise self._build_cloakbrowser_startup_error(exc) from exc
        self.browser = None
        logger.info("CloakBrowser 持久化上下文启动成功")
        logger.info(f"当前浏览器 profile 目录: {self.user_data_dir}")
        
        try:
            all_pages = self.context.pages
            logger.info(f"浏览器启动时共有 {len(all_pages)} 个页面")
            
            for i, page in enumerate(all_pages):
                url = page.url
                logger.info(f"  页面 {i+1}: {url}")
                
                if "douyin.com" not in url and url != "about:blank":
                    logger.info(f"  关闭非抖音页面：{url}")
                    try:
                        page.close()
                    except Exception as e:
                        logger.warning(f"  关闭页面失败：{e}")
            
            remaining_pages = [p for p in self.context.pages if not p.is_closed()]
            if remaining_pages:
                self._cleanup_blank_pages()
                self.page = self._select_existing_page() or remaining_pages[0]
                self.page = self._ensure_home_page_for_main_entry(self.page)
                self._set_page_role(self.page, "main")
                self._cleanup_blank_pages(keep_pages=[self.page])
                if self.page.url == "about:blank" or "douyin.com" not in self.page.url:
                    logger.info("正在导航到抖音首页...")
                    self.page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                    logger.info(f"导航完成，当前 URL: {self.page.url}")
            else:
                self.page = self.context.new_page()
                logger.info("创建新页面并导航到抖音首页...")
                self.page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded", timeout=30000)
                self._set_page_role(self.page, "main")
                logger.info(f"导航完成，当前 URL: {self.page.url}")
            
            logger.info("浏览器启动成功")
        except Exception as e:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
                self.context = None
            logger.error(f"浏览器启动后初始化失败: {e}")
            raise
    def set_monitoring_active(self, active: bool) -> None:
        self._monitoring_active = active
        if active:
            logger.info("页面角色保护已启用：monitor_page 禁止被复用为爬取页")

    def is_monitoring_active(self) -> bool:
        return self._monitoring_active

    def get_page(self) -> Page:
        """获取当前页面对象"""
        return self.page

    def is_session_alive(self) -> bool:
        """检查当前浏览器会话是否仍然可用。"""
        if not self.context:
            return False

        try:
            pages = [page for page in self.context.pages if page and not page.is_closed()]
            if pages:
                return True
        except Exception:
            return False

        for page in (self.page, self.crawler_page, self.monitor_page, self.search_page):
            if not page:
                continue
            try:
                if not page.is_closed():
                    _ = page.url
                    return True
            except Exception:
                return False

        return False
    
    def get_monitor_page(self, force_new: bool = False) -> Page:
        """
        获取自动回复专用标签页
        
        如果不存在则自动创建，导航到抖音消息页面
        与主页面共享同一个浏览器上下文（共享登录状态）
        
        Args:
            force_new: 强制创建新标签页，忽略已有的标签页
            
        Returns:
            自动回复专用页面对象
        """
        if self.monitor_page and not self.monitor_page.is_closed():
            current_url = self._get_page_url(self.monitor_page)
            if "douyin.com" in current_url and "/chat" in current_url:
                return self.monitor_page

        if not self.context:
            logger.warning("浏览器上下文未初始化，返回主页面")
            return self.page

        reusable_chat_page = self._select_existing_chat_page(
            exclude_pages=[self.search_page, self.crawler_page]
        )
        if reusable_chat_page:
            if self.monitor_page and self.monitor_page is not reusable_chat_page:
                self._clear_page_role(self.monitor_page)
            self.monitor_page = reusable_chat_page
            self._set_page_role(self.monitor_page, "monitor")
            logger.info(f"复用现有抖音聊天页作为自动回复标签页，URL: {self.monitor_page.url}")
            return self.monitor_page

        reusable_primary_page = self.page
        if (
            reusable_primary_page
            and reusable_primary_page is not self.search_page
            and reusable_primary_page is not self.crawler_page
        ):
            try:
                if not reusable_primary_page.is_closed():
                    current_url = self._get_page_url(reusable_primary_page)
                    if "douyin.com" in current_url and "/chat" not in current_url:
                        logger.info("复用当前主页面切换到聊天页，避免额外新开监听标签页")
                        reusable_primary_page.goto(
                            DOUYIN_CHAT_URL,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        self.monitor_page = reusable_primary_page
                        self._set_page_role(self.monitor_page, "monitor")
                        return self.monitor_page
            except Exception as e:
                logger.warning(f"复用主页面切换聊天页失败，继续尝试新开标签页: {e}")

        # 没有现成聊天页时才创建新标签页
        if force_new or not self.monitor_page or self.monitor_page.is_closed():
            if not self.context:
                logger.warning("浏览器上下文未初始化，返回主页面")
                return self.page
            
            try:
                logger.info("创建自动回复专用标签页...")
                if self.monitor_page and not self.monitor_page.is_closed():
                    self._clear_page_role(self.monitor_page)
                self.monitor_page = self.context.new_page()
                
                self.monitor_page.goto(
                    DOUYIN_CHAT_URL,
                    wait_until="domcontentloaded",
                    timeout=30000
                )
                self._set_page_role(self.monitor_page, "monitor")
                logger.info(f"自动回复标签页已创建，URL: {self.monitor_page.url}")
                
                return self.monitor_page
                
            except Exception as e:
                logger.error(f"创建自动回复标签页失败: {e}")
                return self.page
        
        return self.monitor_page

    def get_main_page(self) -> Page:
        """返回主页面引用。"""
        return self.page

    def get_crawler_page(self, force_new: bool = False) -> Page:
        """返回爬取专用页，必要时创建。"""
        if not force_new and self.crawler_page and not self.crawler_page.is_closed():
            return self.crawler_page
        return self.create_crawler_page(force_new=force_new)

    def get_search_page(self, force_new: bool = False) -> Page:
        """返回综合搜索页，必要时创建。"""
        if not force_new and self.search_page and not self.search_page.is_closed():
            return self.search_page
        return self.create_search_page(force_new=force_new)

    def ensure_monitor_chat_page(self, force_new: bool = False) -> Page:
        """确保 monitor page 已绑定到聊天页。"""
        page = self.get_monitor_page(force_new=force_new)
        if not page or page.is_closed():
            return page

        current_url = self._get_page_url(page)
        if "douyin.com" in current_url and "/chat" in current_url:
            self.monitor_page = page
            self._set_page_role(page, "monitor")
            return page

        try:
            logger.info(
                f"监听页不在聊天页，准备导航恢复: current_url={current_url or 'about:blank'}"
            )
            page.goto(
                DOUYIN_CHAT_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            try:
                page.wait_for_selector(
                    '[data-e2e="conversation-item"], [class*="conversationItem"]',
                    timeout=5000,
                )
            except Exception:
                page.wait_for_timeout(1500)
        except Exception as exc:
            logger.warning(f"恢复监听聊天页失败: {exc}")
            return page

        self.monitor_page = page
        self._set_page_role(page, "monitor")
        return page
    
    def create_crawler_page(self, force_new: bool = False) -> Page:
        """
        获取爬取专用标签页
        
        如果已存在则复用，否则创建新的标签页
        与主页面共享同一个浏览器上下文（共享登录状态）
        用于搜索和爬取操作，不影响自动回复
        
        Args:
            force_new: 强制创建新标签页，忽略已有的标签页
            
        Returns:
            爬取专用页面对象
        """
        # 强制创建新标签页
        if force_new or not self.crawler_page or self.crawler_page.is_closed():
            if not self.context:
                logger.warning("浏览器上下文未初始化，返回主页面")
                return self.page

            primary_page = self.page
            if (
                primary_page
                and primary_page is not self.search_page
                and primary_page is not self.monitor_page
                and not primary_page.is_closed()
            ):
                primary_url = self._get_page_url(primary_page)
                if "douyin.com" in primary_url and "/chat" not in primary_url:
                    if self.crawler_page and self.crawler_page is not primary_page:
                        self._clear_page_role(self.crawler_page)
                    self.crawler_page = primary_page
                    self._set_page_role(self.crawler_page, "crawler")
                    logger.info(f"复用主页面作为爬取标签页，URL: {self.crawler_page.url}")
                    return self.crawler_page

            if self._monitoring_active and self.monitor_page and not self.monitor_page.is_closed():
                logger.warning(
                    "页面角色保护：监控已激活，禁止将 monitor_page 复用为爬取页，改为新建标签页"
                )
            else:
                reusable_page = self._select_existing_unclaimed_page(
                    exclude_pages=[self.search_page, self.monitor_page, self.crawler_page]
                )
                if reusable_page:
                    if self.crawler_page and self.crawler_page is not reusable_page:
                        self._clear_page_role(self.crawler_page)
                    self.crawler_page = reusable_page
                    self._set_page_role(self.crawler_page, "crawler")
                    logger.info(f"复用现有抖音页面作为爬取标签页，URL: {self.crawler_page.url}")
                    return self.crawler_page

            new_page = None
            try:
                logger.info("创建爬取专用标签页...")
                new_page = self.context.new_page()
                if self.crawler_page and self.crawler_page is not new_page and not self.crawler_page.is_closed():
                    self._clear_page_role(self.crawler_page)
                self.crawler_page = new_page

                try:
                    self.crawler_page.goto(
                        DOUYIN_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=12000
                    )
                except Exception as nav_error:
                    current_url = ""
                    try:
                        current_url = self.crawler_page.url
                    except Exception:
                        current_url = ""
                    logger.warning(
                        f"爬取标签页首页导航超时，继续复用该标签页: {nav_error}; "
                        f"current_url={current_url or 'about:blank'}"
                    )
                self._set_page_role(self.crawler_page, "crawler")
                logger.info(f"爬取标签页已创建，URL: {self.crawler_page.url}")
                
                return self.crawler_page
                
            except Exception as e:
                logger.error(f"创建爬取标签页失败: {e}")
                if new_page and not new_page.is_closed():
                    try:
                        new_page.close()
                    except Exception:
                        pass
                if self.crawler_page == new_page:
                    self.crawler_page = None
                return self.page
        
        return self.crawler_page

    def create_search_page(self, force_new: bool = False) -> Page:
        """
        获取综合搜索专用标签页

        该标签页只负责在综合页持续发现相关视频，避免与评论抓取页面互相打断。
        """
        if force_new or not self.search_page or self.search_page.is_closed():
            if not self.context:
                logger.warning("浏览器上下文未初始化，返回主页面")
                return self.page

            reusable_page = self._select_existing_unclaimed_page(
                exclude_pages=[self.crawler_page, self.monitor_page, self.search_page]
            )
            if reusable_page:
                if self.search_page and self.search_page is not reusable_page:
                    self._clear_page_role(self.search_page)
                self.search_page = reusable_page
                self._set_page_role(self.search_page, "search")
                logger.info(f"复用现有抖音页面作为综合搜索标签页，URL: {self.search_page.url}")
                return self.search_page

            new_page = None
            try:
                logger.info("创建综合搜索专用标签页...")
                new_page = self.context.new_page()
                if self.search_page and self.search_page is not new_page and not self.search_page.is_closed():
                    self._clear_page_role(self.search_page)
                self.search_page = new_page
                try:
                    self.search_page.goto(
                        DOUYIN_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=12000
                    )
                except Exception as nav_error:
                    current_url = ""
                    try:
                        current_url = self.search_page.url
                    except Exception:
                        current_url = ""
                    logger.warning(
                        f"综合搜索标签页首页导航超时，继续复用该标签页: {nav_error}; "
                        f"current_url={current_url or 'about:blank'}"
                    )
                self._set_page_role(self.search_page, "search")
                logger.info(f"综合搜索标签页已创建，URL: {self.search_page.url}")
                return self.search_page
            except Exception as e:
                logger.error(f"创建综合搜索标签页失败: {e}")
                if new_page and not new_page.is_closed():
                    try:
                        new_page.close()
                    except Exception:
                        pass
                if self.search_page == new_page:
                    self.search_page = None
                return self.page

        return self.search_page

    def close_unwanted_pages(self):
        """
        关闭非抖音页面的弹窗和广告页面
        用于在自动回复运行期间清理不需要的页面
        """
        if not self.context:
            return
        
        try:
            all_pages = self.context.pages
            closed_count = 0
            
            # 允许的域名列表
            allowed_domains = [
                "douyin.com",
                "about:blank",
                f"localhost:{SERVER_PORT}",
                f"127.0.0.1:{SERVER_PORT}",
            ]
            
            # 常见广告域名（用于识别广告页面）
            ad_domains = [
                "ad.",
                "ads.",
                "adv.",
                "advertisement",
                "doubleclick",
                "googlesyndication",
                "googleads",
                "facebook.com/tr",
                "analytics",
                "tracking",
                "pixel.",
            ]
            
            for page in all_pages:
                if page.is_closed():
                    continue
                
                # 保护主页面、搜索标签页、自动回复标签页和爬取标签页不被关闭
                if (
                    page == self.page
                    or page == self.search_page
                    or page == self.monitor_page
                    or page == self.crawler_page
                ):
                    continue
                    
                url = page.url.lower()
                
                # 检查是否是允许的域名
                is_allowed = any(domain in url for domain in allowed_domains)
                
                # 检查是否是广告域名
                is_ad = any(ad_domain in url for ad_domain in ad_domains)
                
                if is_allowed and not is_ad:
                    continue
                
                # 关闭广告或不相关的页面
                logger.info(f"关闭非抖音页面: {url}")
                try:
                    page.close()
                    closed_count += 1
                except Exception as e:
                    logger.warning(f"关闭页面失败: {e}")
            
            if closed_count > 0:
                logger.info(f"已关闭 {closed_count} 个非抖音页面")
                
        except Exception as e:
            logger.error(f"清理页面时出错: {e}")
    
    def close_popups_on_page(self):
        """
        关闭当前页面上的弹窗广告
        通过JavaScript查找并关闭常见的弹窗元素
        """
        if not self.page:
            return
        
        try:
            # 关闭弹窗的JavaScript代码
            close_popup_js = """
            () => {
                let closed = 0;
                
                // 关闭常见的弹窗关闭按钮
                const closeSelectors = [
                    '[class*="close"]',
                    '[class*="dismiss"]',
                    '[class*="cancel"]',
                    '[aria-label*="关闭"]',
                    '[aria-label*="close"]',
                    'button[class*="close"]',
                    '.modal-close',
                    '.popup-close',
                    '.ad-close',
                    '[data-close]',
                ];
                
                for (const selector of closeSelectors) {
                    const buttons = document.querySelectorAll(selector);
                    for (const btn of buttons) {
                        try {
                            if (btn.offsetParent !== null) {
                                btn.click();
                                closed++;
                            }
                        } catch(e) {}
                    }
                }
                
                // 移除常见的广告覆盖层
                const adSelectors = [
                    '[class*="ad-overlay"]',
                    '[class*="popup-overlay"]',
                    '[class*="modal-overlay"]',
                    '[id*="ad-"]',
                ];
                
                for (const selector of adSelectors) {
                    const elements = document.querySelectorAll(selector);
                    for (const el of elements) {
                        try {
                            el.style.display = 'none';
                            el.remove();
                        } catch(e) {}
                    }
                }
                
                return closed;
            }
            """
            
            result = self.page.evaluate(close_popup_js)
            if result > 0:
                logger.info(f"已关闭 {result} 个弹窗元素")
                
        except Exception as e:
            logger.debug(f"关闭弹窗时出错: {e}")

    def close(self):
        """关闭浏览器资源。"""
        self.page = None
        self.search_page = None
        self.monitor_page = None
        self.crawler_page = None
        self.browser = None

        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None

        logger.info("浏览器已关闭")
