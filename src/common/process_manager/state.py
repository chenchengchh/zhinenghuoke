"""
process_manager.state - 进程状态与计划恢复层

阶段D·D-2 后续拆分位置：
本子模块计划承载原 ProcessManager 中"进程生命周期状态 / 计划恢复
/PID 记录 / stalled 巡检"相关的逻辑。

计划从 core.py 抽出的方法（按职责划分，非执行项）：
- _get_crawler_state_snapshot / _update_crawler_state
    状态读写
- _reset_crawler_runtime_state
    重置爬取进程运行时状态
- _recover_stalled_crawler_if_needed
    stalled 状态恢复巡检
- _set_scheduled_send_resume / _clear_scheduled_send_resume
- _parse_resume_at_epoch
- _allow_scheduled_send_resume_without_manual_trigger
- _schedule_send_resume / _maybe_resume_scheduled_send
    私信计划恢复的完整生命周期
- _safe_process_pid
- _get_crawler_pid_record_path
- _read_crawler_pid_record / _write_crawler_pid_record
- _clear_crawler_pid_record / _terminate_recorded_crawler_pid
- ensure_crawler_process_stopped_for_restart
    跨进程 PID 记录与重启前清理
- start_crawler_process / stop_crawler_process / stop_all
- shutdown_for_exit
    进程生命周期的核心入口
- get_status / is_crawler_running
    状态查询

抽取策略：
- 上述方法将以模块级函数或小类形式实现
- 与 ipc 边界：state 状态持久（写盘 + 跨进程），ipc 状态短暂
- 与 commands 边界：state 持久化跨重启的计划恢复，commands 只
  负责"本次启动"的命令派发

本文件当前仅承担"位置标记 + 计划说明"职责，**不含可执行代码**。
"""

__all__: list[str] = []
