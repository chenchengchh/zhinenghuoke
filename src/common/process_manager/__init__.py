"""
process_manager - 多进程管理子包

阶段D·D-2 拆分产物：
将原 process_manager.py（1789 行单体）按职责拆为多层子模块。

当前子模块：
- core    : ProcessManager 主类 + crawler_process_main 入口 + 模块级辅助
- models  : 枚举（ProcessRole / ProcessState）+ IPCMessage
- ipc     : 占位（IPC 消息 / ACK / 结果消费 - 后续子阶段）
- commands: 占位（业务命令派发 - 后续子阶段）
- state   : 占位（进程状态 / 计划恢复 - 后续子阶段）

公共 API（保持向后兼容）：
原 process_manager.py 中导出的所有符号（公共 + 内部辅助 + 标准库 re-export），
本子包在 __all__ 中统一重新导出，旧引用（`from src.common.process_manager
import ProcessManager, _crawler_components_ready, random, mp, time, ...`）
可以无修改继续工作。

注：测试中常通过 `monkeypatch.setattr(process_manager_module.random, ...)`
来打桩子进程行为，因此 random / time / mp / threading / queue / os 等
标准库模块也必须从子包级别暴露。
"""

# 阶段D·D-2：从 core 导入公共符号（注意：单例状态不通过 core 维护，
# 改由子包级别维护，便于测试 monkeypatch）
from .core import (
    ProcessManager,
    # 注：get_process_manager / shutdown_process_manager_for_exit 由本
    # 子包级别重新定义（见下方），使用子包级别的 _process_manager_instance
    crawler_process_main,
    # 模块级内部辅助函数（保持与原 process_manager.py 同样的可见性，
    # 测试和外部诊断代码可能依赖它们）
    _page_is_usable,
    _browser_context_can_open_page,
    _crawler_components_ready,
    _extract_search_video_aweme_id,
    _should_skip_existing_video,
    _resolve_post_video_interval,
    _write_crawler_bootstrap_log,
    # 模块级配置常量
    _CRAWLER_STALL_RECOVERY_COOLDOWN_SECONDS,
    _SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS,
    # 标准库 re-export（monkeypatch 兼容）
    random,
    re,
    time,
    json,
    os,
    traceback,
    threading,
    queue_module,
    mp,
    datetime,
    Dict,
    List,
    Any,
    Optional,
    Callable,
    logger,
    extract_search_video_aweme_id,
    build_process_scoped_log_file,
    get_app_state_dir,
    get_log_dir,
)

# 阶段D·D-2：从 models 导入枚举 / IPCMessage
from .models import ProcessRole, ProcessState, IPCMessage

# 阶段D·D-2：单例状态在子包级别维护，便于测试用
# `monkeypatch.setattr(process_manager_module, "_process_manager_instance", ...)`
# 直接打桩子包级单例。
_process_manager_instance = None


def get_process_manager() -> ProcessManager:
    """获取（或惰性创建）进程管理器单例。

    单例状态保存在子包级别（而非 core 模块级别），保证：
    1. 调用方通过 `src.common.process_manager.get_process_manager()` 始终拿到同一实例
    2. 测试可通过 `monkeypatch.setattr(process_manager_module, "_process_manager_instance", ...)` 打桩
    """
    global _process_manager_instance
    if _process_manager_instance is None:
        _process_manager_instance = ProcessManager()
    return _process_manager_instance


def shutdown_process_manager_for_exit() -> None:
    """在进程退出时关闭进程管理器并清空单例。"""
    global _process_manager_instance
    instance = _process_manager_instance
    if instance is not None:
        try:
            instance.shutdown_for_exit()
        except Exception:
            pass
    _process_manager_instance = None


__all__ = [
    # 公共 API
    "ProcessManager",
    "get_process_manager",
    "shutdown_process_manager_for_exit",
    "crawler_process_main",
    "ProcessRole",
    "ProcessState",
    "IPCMessage",
    # 内部辅助（保持向后兼容）
    "_page_is_usable",
    "_browser_context_can_open_page",
    "_crawler_components_ready",
    "_extract_search_video_aweme_id",
    "_should_skip_existing_video",
    "_resolve_post_video_interval",
    "_write_crawler_bootstrap_log",
    "_CRAWLER_STALL_RECOVERY_COOLDOWN_SECONDS",
    "_SEARCH_AUTO_RESUME_DISPATCH_COOLDOWN_SECONDS",
    # 单例状态（便于 monkeypatch）
    "_process_manager_instance",
    # 标准库 re-export（monkeypatch 兼容）
    "random",
    "re",
    "time",
    "json",
    "os",
    "traceback",
    "threading",
    "queue_module",
    "mp",
    "datetime",
    "Dict",
    "List",
    "Any",
    "Optional",
    "Callable",
    "logger",
    "extract_search_video_aweme_id",
    "build_process_scoped_log_file",
    "get_app_state_dir",
    "get_log_dir",
]
