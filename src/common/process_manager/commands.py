"""
process_manager.commands - 业务命令派发层

阶段D·D-2 后续拆分位置：
本子模块计划承载原 ProcessManager 中"业务命令的派发条件 / 构造 /
发送"相关的方法。

计划从 core.py 抽出的方法（按职责划分，非执行项）：
- send_crawler_command
    派发业务命令到爬取/私信子进程（search / send / interact / stop）
- stop_crawler_task
    派发任务级停止信号
- _normalize_search_command_payload
    规范化搜索命令的 payload
- _load_scheduled_search_resume / _persist_scheduled_search_resume
    搜索计划恢复的读写
- _set_scheduled_search_resume / _clear_scheduled_search_resume
    搜索计划恢复的状态管理
- _sync_search_resume_from_runtime
    从 runtime 状态同步搜索恢复计划
- _maybe_resume_scheduled_search
    巡检是否需要自动恢复搜索

抽取策略：
- 上述方法将以模块级函数或小类形式实现
- 与 ipc 边界：commands 负责"业务命令构造 + 派发前置校验"，
  ipc 负责"消息传输 + ACK"
- 与 state 边界：commands 写入短期派发计划，state 持久化跨重启
  的计划恢复记录

本文件当前仅承担"位置标记 + 计划说明"职责，**不含可执行代码**。
"""

__all__: list[str] = []
