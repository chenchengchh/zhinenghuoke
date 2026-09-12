import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict
from urllib import request as urllib_request

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


APP_NAME = "HuokeSmartBot"
_STDIO_SINK = None


def ensure_stdio_streams() -> None:
    global _STDIO_SINK

    if sys.stdout is not None and sys.stderr is not None:
        return

    sink_path = None
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        sink_path = Path(local_app_data).resolve() / APP_NAME / "logs" / "remote_control_server.log"

    try:
        if sink_path is not None:
            sink_path.parent.mkdir(parents=True, exist_ok=True)
            sink = open(sink_path, "a", encoding="utf-8", buffering=1)
        else:
            sink = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        sink = open(os.devnull, "w", encoding="utf-8")

    _STDIO_SINK = sink
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


ensure_stdio_streams()


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False) and str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.remote_control.service import RemoteControlService  # noqa: E402


app = FastAPI(title="Huoke Remote Control Server", version="1.0.0")
_service = RemoteControlService()


def get_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data).resolve() / "HuokeSmartBot"
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


DEFAULT_STORE_PATH = get_runtime_root() / "data" / "remote_control_server" / "policies.json"


def get_store_path() -> Path:
    raw = os.getenv("HUOKE_REMOTE_SERVER_STORE", "").strip()
    return Path(raw) if raw else DEFAULT_STORE_PATH


def auto_open_admin_page(host: str, port: int) -> None:
    if os.getenv("HUOKE_REMOTE_AUTO_OPEN_BROWSER", "1").strip().lower() in {"0", "false", "no"}:
        return

    browser_host = host if host not in {"0.0.0.0", "::"} else "127.0.0.1"
    url = f"http://{browser_host}:{port}/"

    def _wait_and_open() -> None:
        for _ in range(30):
            try:
                with urllib_request.urlopen(url, timeout=2) as response:
                    if 200 <= getattr(response, "status", 200) < 500:
                        webbrowser.open(url)
                        return
            except Exception:
                time.sleep(1)

    threading.Thread(target=_wait_and_open, name="huoke-remote-browser-opener", daemon=True).start()


def load_policies() -> Dict[str, dict]:
    store_path = get_store_path()
    if not store_path.exists():
        return {}
    try:
        return json.loads(store_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_policies(policies: Dict[str, dict]) -> None:
    store_path = get_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(policies, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_instance_key(instance_id: str) -> str:
    value = str(instance_id or "").strip()
    return value or "*"


def require_admin_token(x_admin_token: str | None) -> None:
    expected = os.getenv("HUOKE_REMOTE_ADMIN_TOKEN", "").strip()
    if not expected:
        return
    if x_admin_token != expected:
        raise HTTPException(status_code=401, detail="admin token invalid")


class UpsertPolicyRequest(BaseModel):
    instance_id: str = ""
    app_enabled: bool = True
    crawler_enabled: bool = True
    monitor_enabled: bool = True
    auto_reply_enabled: bool = True
    message: str = ""
    policy_id: str = ""


ADMIN_PAGE_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>火客远程控制台</title>
    <style>
        body { font-family: Arial, sans-serif; background:#f5f7fb; margin:0; padding:24px; color:#1f2937; }
        .wrap { max-width: 1080px; margin: 0 auto; }
        .card { background:#fff; border-radius:12px; box-shadow:0 8px 24px rgba(15,23,42,.08); padding:20px; margin-bottom:20px; }
        h1,h2 { margin:0 0 16px; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
        label { display:block; font-size:14px; margin-bottom:6px; color:#374151; }
        input, textarea { width:100%; box-sizing:border-box; padding:10px 12px; border:1px solid #d1d5db; border-radius:8px; font-size:14px; }
        textarea { min-height:88px; resize:vertical; }
        .actions { display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }
        button { border:0; border-radius:8px; padding:10px 16px; cursor:pointer; font-size:14px; }
        .primary { background:#2563eb; color:#fff; }
        .danger { background:#dc2626; color:#fff; }
        .secondary { background:#e5e7eb; color:#111827; }
        .muted { color:#6b7280; font-size:13px; }
        table { width:100%; border-collapse:collapse; margin-top:12px; }
        th, td { border-bottom:1px solid #e5e7eb; padding:10px 8px; text-align:left; vertical-align:top; }
        .badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; }
        .ok { background:#dcfce7; color:#166534; }
        .warn { background:#fef3c7; color:#92400e; }
        .err { background:#fee2e2; color:#991b1b; }
        pre { white-space:pre-wrap; word-break:break-all; background:#0f172a; color:#e2e8f0; padding:12px; border-radius:10px; }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="card">
            <h1>火客远程控制台</h1>
            <div class="muted">在浏览器里直接维护实例策略，不再依赖脚本手工调用。</div>
        </div>

        <div class="card">
            <h2>管理员令牌</h2>
            <div class="grid">
                <div>
                    <label for="admin-token">X-Admin-Token</label>
                    <input id="admin-token" type="password" placeholder="如未配置可留空">
                </div>
            </div>
            <div class="actions">
                <button class="secondary" onclick="saveAdminToken()">保存令牌</button>
                <button class="secondary" onclick="loadPolicies()">刷新策略列表</button>
            </div>
        </div>

        <div class="card">
            <h2>新增或更新策略</h2>
            <div class="grid">
                <div>
                    <label for="instance-id">实例 ID</label>
                    <input id="instance-id" type="text" placeholder="留空表示默认策略(*)">
                </div>
                <div>
                    <label for="policy-id">策略编号</label>
                    <input id="policy-id" type="text" placeholder="可选，自定义编号">
                </div>
            </div>
            <div class="grid" style="margin-top:12px;">
                <label><input type="checkbox" id="app-enabled" checked> 启用整个软件</label>
                <label><input type="checkbox" id="crawler-enabled" checked> 启用爬取</label>
                <label><input type="checkbox" id="monitor-enabled" checked> 启用监听</label>
                <label><input type="checkbox" id="auto-reply-enabled" checked> 启用自动回复</label>
            </div>
            <div style="margin-top:12px;">
                <label for="policy-message">提示信息</label>
                <textarea id="policy-message" placeholder="例如：服务器已暂停自动回复，请联系管理员。"></textarea>
            </div>
            <div class="actions">
                <button class="primary" onclick="upsertPolicy()">保存策略</button>
                <button class="secondary" onclick="previewPolicy()">预览当前表单生成结果</button>
            </div>
        </div>

        <div class="card">
            <h2>策略列表</h2>
            <table>
                <thead>
                    <tr>
                        <th>实例</th>
                        <th>状态</th>
                        <th>提示</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody id="policy-table-body">
                    <tr><td colspan="4" class="muted">加载中...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>预览</h2>
            <pre id="preview-box">暂无预览</pre>
        </div>
    </div>

    <script>
        const tokenInput = document.getElementById('admin-token');
        const previewBox = document.getElementById('preview-box');

        function getAdminToken() {
            return tokenInput.value.trim();
        }

        function saveAdminToken() {
            localStorage.setItem('huoke_remote_admin_token', getAdminToken());
            alert('管理员令牌已保存');
        }

        function loadSavedToken() {
            tokenInput.value = localStorage.getItem('huoke_remote_admin_token') || '';
        }

        function adminHeaders() {
            const headers = { 'Content-Type': 'application/json' };
            const token = getAdminToken();
            if (token) headers['x-admin-token'] = token;
            return headers;
        }

        function getFormPayload() {
            return {
                instance_id: document.getElementById('instance-id').value.trim(),
                policy_id: document.getElementById('policy-id').value.trim(),
                app_enabled: document.getElementById('app-enabled').checked,
                crawler_enabled: document.getElementById('crawler-enabled').checked,
                monitor_enabled: document.getElementById('monitor-enabled').checked,
                auto_reply_enabled: document.getElementById('auto-reply-enabled').checked,
                message: document.getElementById('policy-message').value.trim()
            };
        }

        function renderStatus(item) {
            if (!item.app_enabled) return '<span class="badge err">整机禁用</span>';
            const limited = [];
            if (!item.crawler_enabled) limited.push('爬取');
            if (!item.monitor_enabled) limited.push('监听');
            if (!item.auto_reply_enabled) limited.push('回复');
            if (limited.length) return `<span class="badge warn">限制: ${limited.join('/')}</span>`;
            return '<span class="badge ok">全部启用</span>';
        }

        async function loadPolicies() {
            const tbody = document.getElementById('policy-table-body');
            tbody.innerHTML = '<tr><td colspan="4" class="muted">加载中...</td></tr>';
            try {
                const res = await fetch('/admin/policies', { headers: adminHeaders() });
                const data = await res.json();
                if (!data.success) throw new Error(data.detail || data.message || '加载失败');
                const items = data.items || {};
                const rows = Object.entries(items).map(([instanceId, item]) => `
                    <tr>
                        <td>${instanceId}</td>
                        <td>${renderStatus(item)}</td>
                        <td>${item.message || '-'}</td>
                        <td><button class="danger" onclick="deletePolicy('${instanceId}')">删除</button></td>
                    </tr>
                `);
                tbody.innerHTML = rows.length ? rows.join('') : '<tr><td colspan="4" class="muted">暂无策略</td></tr>';
            } catch (e) {
                tbody.innerHTML = `<tr><td colspan="4" class="err">${e.message}</td></tr>`;
            }
        }

        async function upsertPolicy() {
            const payload = getFormPayload();
            previewBox.textContent = JSON.stringify(payload, null, 2);
            const res = await fetch('/admin/policies/upsert', {
                method: 'POST',
                headers: adminHeaders(),
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (!data.success) {
                alert(data.detail || data.message || '保存失败');
                return;
            }
            previewBox.textContent = JSON.stringify(data.policy, null, 2);
            alert('策略已保存');
            loadPolicies();
        }

        async function deletePolicy(instanceId) {
            if (!confirm(`确认删除实例 ${instanceId} 的策略吗？`)) return;
            const res = await fetch(`/admin/policies/${encodeURIComponent(instanceId)}`, {
                method: 'DELETE',
                headers: adminHeaders()
            });
            const data = await res.json();
            if (!data.success) {
                alert(data.detail || data.message || '删除失败');
                return;
            }
            loadPolicies();
        }

        function previewPolicy() {
            previewBox.textContent = JSON.stringify(getFormPayload(), null, 2);
        }

        loadSavedToken();
        loadPolicies();
    </script>
</body>
</html>
"""


@app.get("/health")
async def health():
    return {"success": True, "store_path": str(get_store_path())}


@app.get("/", response_class=HTMLResponse)
async def admin_page():
    return HTMLResponse(content=ADMIN_PAGE_HTML)


@app.get("/policy")
async def get_policy(instance_id: str = Query(default="")):
    policies = load_policies()
    policy = policies.get(normalize_instance_key(instance_id)) or policies.get("*")
    if policy:
        return {"success": True, "policy": policy}

    default_policy = _service.generate_signed_policy(
        instance_id=instance_id,
        message="未配置专属策略，默认允许使用。",
    )
    return {"success": True, "policy": default_policy}


@app.get("/admin/policies")
async def list_policies(x_admin_token: str | None = Header(default=None)):
    require_admin_token(x_admin_token)
    return {"success": True, "items": load_policies()}


@app.post("/admin/policies/upsert")
async def upsert_policy(request: UpsertPolicyRequest, x_admin_token: str | None = Header(default=None)):
    require_admin_token(x_admin_token)
    policies = load_policies()
    instance_key = normalize_instance_key(request.instance_id)
    payload = _service.generate_signed_policy(
        instance_id="" if instance_key == "*" else request.instance_id,
        app_enabled=request.app_enabled,
        crawler_enabled=request.crawler_enabled,
        monitor_enabled=request.monitor_enabled,
        auto_reply_enabled=request.auto_reply_enabled,
        message=request.message,
        policy_id=request.policy_id,
    )
    policies[instance_key] = payload
    save_policies(policies)
    return {"success": True, "instance_id": instance_key, "policy": payload}


@app.delete("/admin/policies/{instance_id}")
async def delete_policy(instance_id: str, x_admin_token: str | None = Header(default=None)):
    require_admin_token(x_admin_token)
    policies = load_policies()
    instance_key = normalize_instance_key(instance_id)
    removed = policies.pop(instance_key, None)
    save_policies(policies)
    return {"success": True, "removed": bool(removed), "instance_id": instance_key}


def main() -> None:
    host = os.getenv("HUOKE_REMOTE_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("HUOKE_REMOTE_SERVER_PORT", "8091").strip() or "8091")
    auto_open_admin_page(host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
