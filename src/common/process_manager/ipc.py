"""
process_manager.ipc - 进程间通信（IPC）层

阶段D·D-2 后续拆分位置：
本子模块计划承载原 ProcessManager 中"IPC 消息 / ACK 跟踪 / 命令
超时检测 / 结果队列消费"相关的逻辑。

计划从 core.py 抽出的方法（按职责划分，非执行项）：
- _record_expected_ack
    记录"已派发但尚未 ACK"的命令
- mark_command_acknowledged
    标记命令已被爬取进程确认
- check_pending_ack_timeout
    巡检 ACK 超时，避免命令静默丢失
- _start_result_thread / _stop_result_thread / _result_loop
    启动/停止结果消费线程
- _handle_crawler_result
    单条结果消息的处理入口
- _update_shared_state / _set_idle_state
    子进程向主进程回写共享状态的辅助
- _clear_queue
    优雅清空命令队列

抽取策略：
- 上述方法将以模块级函数或小类形式实现
- 与 commands 边界：ipc 负责"消息收发 + ACK 状态机"，
  commands 负责"业务命令的派发条件与发送"
- 与 state 边界：ipc 状态短暂（仅在派发到 ACK 之间），
  state 状态持久（计划恢复、PID 记录等）

本文件当前仅承担"位置标记 + 计划说明"职责，**不含可执行代码**。
"""

__all__: list[str] = []
