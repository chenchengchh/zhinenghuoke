import os
import time
from pathlib import Path


def _bootstrap_log(message: str) -> None:
    try:
        log_dir_value = os.getenv("HUOKE_LOG_DIR", "").strip()
        if log_dir_value:
            log_dir = Path(log_dir_value)
        else:
            local_app_data = os.getenv("LOCALAPPDATA", "").strip()
            if local_app_data:
                log_dir = Path(local_app_data) / "HuokeSmartBot" / "logs"
            else:
                log_dir = Path.cwd()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "crawler_worker_entry.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | pid={os.getpid()} | {message}\n")
    except Exception:
        pass


def crawler_process_entry(
    command_queue,
    result_queue,
    stop_event,
    task_stop_event,
):
    _bootstrap_log("crawler_process_entry entered")
    from src.common.crawler_worker import crawler_process_main

    _bootstrap_log("imported crawler_process_main")
    crawler_process_main(
        command_queue,
        result_queue,
        stop_event,
        task_stop_event,
    )
