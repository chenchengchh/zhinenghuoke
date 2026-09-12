import json
import os
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

from src.licensing.machine_fingerprint import (
    FINGERPRINT_VERSION,
    build_machine_fingerprint,
    collect_machine_fingerprint_components,
)
from src.licensing.request_code import build_request_code_payload, encode_request_code


def build_machine_code_payload() -> dict:
    components = collect_machine_fingerprint_components()
    machine_code = build_machine_fingerprint()
    request_payload = build_request_code_payload(machine_hash=machine_code, components=components)
    return {
        "machine_code": machine_code,
        "request_code": encode_request_code(request_payload),
        "fingerprint_version": FINGERPRINT_VERSION,
        "machine_profile": request_payload.machine_profile,
        "components": components,
    }


def _write_crash_log(text: str) -> str:
    try:
        base = Path(os.getenv("TEMP", "") or ".").resolve()
    except Exception:
        base = Path(".").resolve()
    log_path = base / "HuokeMachineCodeGenerator_crash.log"
    try:
        log_path.write_text(text, encoding="utf-8", errors="ignore")
    except Exception:
        pass
    return str(log_path)


class MachineCodeGeneratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("火客机器码生成器")
        self.root.geometry("820x620")
        self.root.minsize(760, 560)

        self.machine_code_var = tk.StringVar()
        self.request_code_var = tk.StringVar()

        self._build_ui()
        self.refresh_machine_code()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text="火客机器码生成器",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            container,
            text="用于自动读取当前电脑的授权机器码。把这串机器码发给验证码生成器，即可生成绑定本机的验证码。",
        ).pack(anchor=tk.W, pady=(6, 16))

        summary = ttk.LabelFrame(container, text="当前机器码", padding=12)
        summary.pack(fill=tk.X)

        entry = ttk.Entry(summary, textvariable=self.machine_code_var, width=96)
        entry.pack(fill=tk.X)

        ttk.Label(summary, text="授权请求码").pack(anchor=tk.W, pady=(12, 0))
        request_entry = ttk.Entry(summary, textvariable=self.request_code_var, width=96)
        request_entry.pack(fill=tk.X, pady=(4, 0))

        button_row = ttk.Frame(summary)
        button_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(button_row, text="刷新机器码", command=self.refresh_machine_code).pack(side=tk.LEFT)
        ttk.Button(button_row, text="复制机器码", command=self.copy_machine_code).pack(side=tk.LEFT, padx=12)
        ttk.Button(button_row, text="复制请求码", command=self.copy_request_code).pack(side=tk.LEFT)
        ttk.Button(button_row, text="复制完整信息", command=self.copy_full_payload).pack(side=tk.LEFT, padx=12)

        details_frame = ttk.LabelFrame(container, text="机器信息详情", padding=12)
        details_frame.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        self.output_text = tk.Text(details_frame, wrap=tk.WORD, font=("Consolas", 10))
        self.output_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text="说明：设备 ID、产品 ID 都不是本工具使用的机器码。最终机器码由系统信息组合后计算得到。",
        ).pack(anchor=tk.W, pady=(12, 0))

    def refresh_machine_code(self) -> None:
        payload = build_machine_code_payload()
        self.machine_code_var.set(str(payload.get("machine_code") or ""))
        self.request_code_var.set(str(payload.get("request_code") or ""))
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

    def copy_machine_code(self) -> None:
        machine_code = self.machine_code_var.get().strip()
        if not machine_code:
            messagebox.showwarning("提示", "当前没有可复制的机器码。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(machine_code)
        self.root.update()
        messagebox.showinfo("已复制", "机器码已复制到剪贴板。")

    def copy_request_code(self) -> None:
        request_code = self.request_code_var.get().strip()
        if not request_code:
            messagebox.showwarning("提示", "当前没有可复制的请求码。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(request_code)
        self.root.update()
        messagebox.showinfo("已复制", "请求码已复制到剪贴板。")

    def copy_full_payload(self) -> None:
        content = self.output_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "当前没有可复制的信息。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.root.update()
        messagebox.showinfo("已复制", "完整机器信息已复制到剪贴板。")


def main() -> None:
    try:
        root = tk.Tk()
        MachineCodeGeneratorApp(root)
        root.mainloop()
    except Exception:
        log_path = _write_crash_log(traceback.format_exc())
        try:
            messagebox.showerror("启动失败", f"机器码生成器启动失败。\n错误日志已保存到：\n{log_path}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
