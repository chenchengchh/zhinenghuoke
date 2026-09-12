import base64
import json
import os
import sys
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from src.licensing.license_models import LicenseFeatures, LicenseType, get_license_type_label, get_license_valid_days
from src.licensing.request_code import decode_request_code
from src.licensing.license_service import LicenseService


LICENSE_TYPE_TRIAL_7D = LicenseType.TRIAL_7D.value
LICENSE_TYPE_MONTH_1 = LicenseType.MONTH_1.value
LICENSE_TYPE_MONTH_3 = LicenseType.MONTH_3.value
LICENSE_TYPE_MONTH_6 = LicenseType.MONTH_6.value
LICENSE_TYPE_YEAR_1 = LicenseType.YEAR_1.value
LICENSE_TYPE_OPTIONS = [
    LICENSE_TYPE_TRIAL_7D,
    LICENSE_TYPE_MONTH_1,
    LICENSE_TYPE_MONTH_3,
    LICENSE_TYPE_MONTH_6,
    LICENSE_TYPE_YEAR_1,
]


def decode_license_key(license_key: str) -> dict:
    normalized = str(license_key or "").strip()
    decoded = base64.urlsafe_b64decode(normalized.encode("utf-8") + b"===")
    return json.loads(decoded.decode("utf-8"))


def generate_license_key(
    *,
    license_type: str,
    customer_name: str,
    phone_number: str,
    request_code: str = "",
    machine_hash: str = "",
    valid_days: int = 7,
    features: dict | None = None,
    license_id: str = "",
) -> tuple[str, dict]:
    """为测试和脚本调用暴露无 GUI 的标准库生成入口。"""
    normalized_features = LicenseFeatures(**(features or {}))
    service = LicenseService()
    resolved_machine_hash = str(machine_hash or "").strip()
    if request_code:
        resolved_machine_hash = decode_request_code(str(request_code).strip()).machine_hash
    if not resolved_machine_hash:
        raise ValueError("请求码不能为空")
    license_key = service.generate_license_key(
        license_type=LicenseType(str(license_type).strip()),
        customer_name=str(customer_name or "").strip(),
        phone_number=str(phone_number or "").strip(),
        machine_hash=resolved_machine_hash,
        features=normalized_features,
        valid_days=int(valid_days or 7),
        license_id=str(license_id or "").strip(),
    )
    return license_key, decode_license_key(license_key)


def _write_crash_log(text: str) -> str:
    try:
        base = Path(os.getenv("TEMP", "") or ".").resolve()
    except Exception:
        base = Path(".").resolve()
    log_path = base / "HuokeLicenseGenerator_crash.log"
    try:
        log_path.write_text(text, encoding="utf-8", errors="ignore")
    except Exception:
        # Last resort: ignore.
        pass
    return str(log_path)

class LicenseGeneratorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.license_service = LicenseService()
        self.root.title("火客验证码生成器")
        self.root.geometry("780x680")
        self.root.minsize(720, 620)

        self.customer_name_var = tk.StringVar()
        self.phone_number_var = tk.StringVar()
        self.license_id_var = tk.StringVar()
        self.request_code_var = tk.StringVar()
        self.license_type_var = tk.StringVar(value=LICENSE_TYPE_TRIAL_7D)
        self.duration_hint_var = tk.StringVar(value="7天免费试用")

        self.crawler_var = tk.BooleanVar(value=True)
        self.monitor_var = tk.BooleanVar(value=True)
        self.auto_reply_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._on_license_type_change()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(container, text="火客智能客服验证码生成器", font=("Microsoft YaHei UI", 16, "bold"))
        title.pack(anchor=tk.W)

        desc = ttk.Label(
            container,
            text="支持生成 7天试用、1个月、3个月、6个月、1年的验证码。正式发码前必须导入目标机器请求码，生成结果会与该设备绑定。",
        )
        desc.pack(anchor=tk.W, pady=(6, 16))

        form = ttk.LabelFrame(container, text="生成参数", padding=12)
        form.pack(fill=tk.X)

        self._add_entry(form, "客户名称", self.customer_name_var, 0)
        self._add_entry(form, "注册手机号", self.phone_number_var, 1)
        self._add_entry(form, "授权编号", self.license_id_var, 2)
        self._add_entry(form, "设备请求码", self.request_code_var, 3, width=60)
        ttk.Button(form, text="导入请求码文件", command=self.load_request_code_file).grid(row=3, column=2, sticky=tk.W, padx=(12, 0), pady=8)

        ttk.Label(form, text="授权类型").grid(row=4, column=0, sticky=tk.W, padx=(0, 12), pady=8)
        type_box = ttk.Combobox(
            form,
            textvariable=self.license_type_var,
            state="readonly",
            values=LICENSE_TYPE_OPTIONS,
            width=24,
        )
        type_box.grid(row=4, column=1, sticky=tk.W, pady=8)
        type_box.bind("<<ComboboxSelected>>", lambda _e: self._on_license_type_change())

        ttk.Label(form, text="授权期限").grid(row=5, column=0, sticky=tk.W, padx=(0, 12), pady=8)
        self.duration_hint_label = ttk.Label(form, textvariable=self.duration_hint_var)
        self.duration_hint_label.grid(row=5, column=1, sticky=tk.W, pady=8)

        feature_frame = ttk.LabelFrame(container, text="功能权限", padding=12)
        feature_frame.pack(fill=tk.X, pady=(16, 0))
        ttk.Checkbutton(feature_frame, text="启用爬取", variable=self.crawler_var).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Checkbutton(feature_frame, text="启用监听", variable=self.monitor_var).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Checkbutton(feature_frame, text="启用自动回复", variable=self.auto_reply_var).pack(side=tk.LEFT)

        button_frame = ttk.Frame(container)
        button_frame.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(button_frame, text="生成验证码", command=self.generate_license).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="复制验证码", command=self.copy_license).pack(side=tk.LEFT, padx=12)
        ttk.Button(button_frame, text="复制完整信息", command=self.copy_all).pack(side=tk.LEFT)

        output_frame = ttk.LabelFrame(container, text="生成结果", padding=12)
        output_frame.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        self.output_text = tk.Text(output_frame, wrap=tk.WORD, font=("Consolas", 10))
        self.output_text.pack(fill=tk.BOTH, expand=True)

        hint = ttk.Label(
            container,
            text="说明：手机号必填，请求码必填。软件端激活时会同时校验手机号和绑定设备；同一授权码在其他电脑上会被拒绝。",
        )
        hint.pack(anchor=tk.W, pady=(12, 0))

    def _add_entry(self, parent: ttk.LabelFrame, label: str, variable: tk.StringVar, row: int, width: int = 48) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 12), pady=8)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=1, sticky=tk.W, pady=8)

    def load_request_code_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="选择请求码文件",
            filetypes=[("文本文件", "*.txt"), ("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not file_path:
            return
        try:
            content = Path(file_path).read_text(encoding="utf-8").strip()
        except Exception as exc:
            messagebox.showerror("导入失败", f"读取请求码文件失败：{exc}")
            return
        self.request_code_var.set(content)

    def _on_license_type_change(self) -> None:
        try:
            license_type = LicenseType(self.license_type_var.get())
            self.duration_hint_var.set(get_license_type_label(license_type))
        except Exception:
            self.duration_hint_var.set("未识别的授权类型")

    def generate_license(self) -> None:
        try:
            license_type = self.license_type_var.get().strip()
            if license_type not in set(LICENSE_TYPE_OPTIONS):
                raise ValueError(f"不支持的授权类型: {license_type}")
            phone_number = self.phone_number_var.get().strip()
            if not phone_number:
                raise ValueError("注册手机号不能为空")
            request_code = self.request_code_var.get().strip()
            if not request_code:
                raise ValueError("设备请求码不能为空")
            request_payload = decode_request_code(request_code)
        except Exception as exc:
            messagebox.showerror("生成失败", f"参数无效：{exc}")
            return

        features = {
            "crawler": self.crawler_var.get(),
            "monitor": self.monitor_var.get(),
            "auto_reply": self.auto_reply_var.get(),
        }

        license_key = self.license_service.generate_license_key(
            license_type=LicenseType(license_type),
            customer_name=self.customer_name_var.get().strip(),
            phone_number=phone_number,
            machine_hash=request_payload.machine_hash,
            features=LicenseFeatures(**features),
            license_id=self.license_id_var.get().strip(),
        )
        signed_payload = decode_license_key(license_key)

        payload = {
            "license_type": license_type,
            "license_type_label": get_license_type_label(LicenseType(license_type)),
            "customer_name": self.customer_name_var.get().strip(),
            "phone_number": phone_number,
            "license_id": self.license_id_var.get().strip(),
            "machine_hash": request_payload.machine_hash,
            "fingerprint_version": request_payload.fingerprint_version,
            "valid_days": get_license_valid_days(LicenseType(license_type)),
            "features": features,
            "license_key": license_key,
            "signed_payload": signed_payload,
        }

        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

    def copy_license(self) -> None:
        content = self.output_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "请先生成验证码。")
            return
        try:
            payload = json.loads(content)
            license_key = payload.get("license_key", "")
        except Exception:
            license_key = ""
        if not license_key:
            messagebox.showwarning("提示", "未找到验证码，请重新生成。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(license_key)
        self.root.update()
        messagebox.showinfo("已复制", "验证码已复制到剪贴板。")

    def copy_all(self) -> None:
        content = self.output_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "请先生成验证码。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.root.update()
        messagebox.showinfo("已复制", "完整信息已复制到剪贴板。")


def main() -> None:
    try:
        root = tk.Tk()
        LicenseGeneratorApp(root)
        root.mainloop()
    except Exception:
        log_path = _write_crash_log(traceback.format_exc())
        try:
            messagebox.showerror("启动失败", f"验证码生成器启动失败。\n错误日志已保存到：\n{log_path}")
        except Exception:
            # If tkinter messagebox itself fails, at least keep the log.
            pass
        raise


if __name__ == "__main__":
    main()
