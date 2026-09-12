# -*- coding: utf-8 -*-
"""
中间件模块
提供企业级应用的中间件功能
"""
import time
import uuid
import json
import os
import threading
from typing import Any, Callable, Dict, List, Optional
from fastapi import Request, Response, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware as StarletteCORSMiddleware
from starlette.types import ASGIApp

from src.infrastructure.exceptions import AppException, ErrorCode, handle_exception
from src.infrastructure.logger import get_logger, request_context, get_request_context
from src.infrastructure.response import ErrorResponse


logger = get_logger("middleware")


def _get_server_port() -> int:
    raw_port = os.getenv("SERVER_PORT") or os.getenv("PORT") or "8023"
    try:
        return int(str(raw_port).strip())
    except (TypeError, ValueError):
        return 8023


class RequestMiddleware(BaseHTTPMiddleware):
    """
    请求处理中间件
    
    功能：
    - 生成请求追踪ID
    - 记录请求开始时间
    - 设置请求上下文
    """
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        trace_id = request.headers.get("X-Trace-ID", request_id.replace("-", ""))
        span_id = uuid.uuid4().hex[:16]
        start_time = time.time()
        
        ctx = {
            "request_id": request_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "root_span_id": span_id,
            "current_span_id": span_id,
            "span_stack": [span_id],
            "start_time": start_time,
            "user_id": None,
            "enterprise_id": None
        }
        request_context.set(ctx)
        
        response = await call_next(request)
        
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Span-ID"] = span_id
        response.headers["X-Response-Time"] = f"{(time.time() - start_time) * 1000:.2f}ms"
        
        return response


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """
    错误处理中间件
    
    功能：
    - 统一异常捕获
    - 标准化错误响应
    - 错误日志记录
    """
    
    def __init__(self, app: ASGIApp, debug: bool = False):
        super().__init__(app)
        self.debug = debug
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except HTTPException as exc:
            logger.warning(f"HTTP异常: {exc.status_code} - {exc.detail}")
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "code": exc.status_code,
                    "message": str(exc.detail),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "request_id": request.headers.get("X-Request-ID", "")
                }
            )
        except AppException as exc:
            logger.error(f"应用异常: {exc}", exc=exc.cause)
            return JSONResponse(
                status_code=self._get_http_status(exc.error_code),
                content=exc.to_dict()
            )
        except Exception as exc:
            app_exc = handle_exception(exc)
            logger.critical(f"未处理异常: {exc}", exc=exc)
            
            content = app_exc.to_dict()
            if self.debug:
                import traceback
                content["traceback"] = traceback.format_exc()
            
            return JSONResponse(
                status_code=500,
                content=content
            )
    
    def _get_http_status(self, error_code: ErrorCode) -> int:
        """将错误码转换为HTTP状态码"""
        code = error_code.code
        if code < 100:
            return 200
        elif code < 500:
            return code
        elif code < 600:
            return code
        elif code < 2000:
            return 500
        else:
            return 400


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    日志记录中间件
    
    功能：
    - 记录请求/响应日志
    - 性能监控
    - 敏感信息脱敏
    """
    
    SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key"}
    SENSITIVE_BODY_FIELDS = {"password", "token", "secret", "credential"}
    
    def __init__(self, app: ASGIApp, exclude_paths: Optional[List[str]] = None):
        super().__init__(app)
        self.exclude_paths = exclude_paths or ["/health", "/metrics", "/favicon.ico"]
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            return await call_next(request)
        
        start_time = time.time()
        ctx = get_request_context()
        
        request_body = await self._get_request_body(request)
        
        logger.info(
            f"请求开始: {request.method} {request.url.path}",
            method=request.method,
            path=request.url.path,
            query=str(request.query_params),
            client_ip=self._get_client_ip(request),
            user_id=ctx.get("user_id"),
            request_body=self._sanitize_body(request_body)
        )
        
        response = await call_next(request)
        
        duration_ms = (time.time() - start_time) * 1000
        
        logger.log_request(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_id=ctx.get("user_id"),
            enterprise_id=ctx.get("enterprise_id")
        )
        
        return response
    
    async def _get_request_body(self, request: Request) -> Dict[str, Any]:
        """获取请求体（缓存以便后续处理程序读取）"""
        try:
            if request.method in ["POST", "PUT", "PATCH"]:
                body = await request.body()
                if body:
                    async def receive():
                        return {"type": "http.request", "body": body}
                    request._receive = receive
                    return json.loads(body)
        except Exception:
            pass
        return {}
    
    def _sanitize_body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """脱敏请求体"""
        if not body:
            return body
        
        result = {}
        for key, value in body.items():
            if key.lower() in self.SENSITIVE_BODY_FIELDS:
                result[key] = "***REDACTED***"
            elif isinstance(value, dict):
                result[key] = self._sanitize_body(value)
            else:
                result[key] = value
        return result
    
    def _get_client_ip(self, request: Request) -> str:
        """获取客户端IP（仅信任直连IP，X-Forwarded-For需在反向代理层验证）"""
        return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    请求限流中间件
    
    功能：
    - IP级别限流
    - 用户级别限流
    - 滑动窗口算法
    """
    
    def __init__(
        self,
        app: ASGIApp,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        exclude_paths: Optional[List[str]] = None
    ):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.exclude_paths = exclude_paths or ["/health", "/metrics"]
        self._request_history: Dict[str, List[float]] = {}
        self._last_cleanup: float = time.time()
        self._cleanup_interval: float = 300.0
        self._history_lock = threading.Lock()
    
    def _cleanup_stale_ips(self, current_time: float):
        """清理长时间无请求的IP记录，防止内存泄漏"""
        if current_time - self._last_cleanup < self._cleanup_interval:
            return
        with self._history_lock:
            self._last_cleanup = current_time
            stale_ips = [
                ip for ip, times in self._request_history.items()
                if not times or current_time - times[-1] > 3600
            ]
            for ip in stale_ips:
                del self._request_history[ip]
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            return await call_next(request)
        
        client_ip = self._get_client_ip(request)
        current_time = time.time()
        self._cleanup_stale_ips(current_time)
        
        with self._history_lock:
            if client_ip not in self._request_history:
                self._request_history[client_ip] = []
            
            self._request_history[client_ip] = [
                t for t in self._request_history[client_ip]
                if current_time - t < 3600
            ]
            
            requests_last_minute = sum(
                1 for t in self._request_history[client_ip]
                if current_time - t < 60
            )
            
            requests_last_hour = len(self._request_history[client_ip])
        
        if requests_last_minute >= self.requests_per_minute:
            logger.log_security(
                event="rate_limit_exceeded",
                ip_address=client_ip,
                details={"limit": "per_minute", "count": requests_last_minute}
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "请求频率超限，请稍后再试",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "retry_after": 60
                },
                headers={"Retry-After": "60"}
            )
        
        if requests_last_hour >= self.requests_per_hour:
            logger.log_security(
                event="rate_limit_exceeded",
                ip_address=client_ip,
                details={"limit": "per_hour", "count": requests_last_hour}
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": 429,
                    "message": "请求频率超限，请稍后再试",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "retry_after": 3600
                },
                headers={"Retry-After": "3600"}
            )
        
        with self._history_lock:
            self._request_history[client_ip].append(current_time)
        
        return await call_next(request)
    
    def _get_client_ip(self, request: Request) -> str:
        """获取客户端IP（仅信任直连IP）"""
        return request.client.host if request.client else "unknown"


class CORSMiddleware:
    """
    CORS中间件配置
    
    功能：
    - 跨域请求支持
    - 安全头部设置
    """
    
    @staticmethod
    def create(
        allow_origins: List[str] = None,
        allow_methods: List[str] = None,
        allow_headers: List[str] = None,
        allow_credentials: bool = False,
        max_age: int = 600
    ):
        """创建CORS中间件配置"""
        server_port = _get_server_port()
        origins = allow_origins or [
            f"http://localhost:{server_port}",
            f"http://127.0.0.1:{server_port}",
        ]
        if "*" in origins and allow_credentials:
            logger.warning("CORS配置: allow_origins=['*'] + allow_credentials=True 不符合规范，已自动关闭credentials")
            allow_credentials = False
        return {
            "allow_origins": origins,
            "allow_methods": allow_methods or ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": allow_headers or ["Content-Type", "Authorization"],
            "allow_credentials": allow_credentials,
            "max_age": max_age
        }


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    安全头部中间件
    
    功能：
    - 添加安全相关HTTP头部
    - 防止常见Web攻击
    """
    
    def __init__(
        self,
        app: ASGIApp,
        enable_csp: bool = True,
        csp_policy: str = "default-src 'self'",
        enable_hsts: bool = True,
        hsts_https_only: bool = True,
        x_frame_options: str = "DENY",
    ):
        super().__init__(app)
        self.enable_csp = enable_csp
        self.csp_policy = csp_policy
        self.enable_hsts = enable_hsts
        self.hsts_https_only = hsts_https_only
        self.x_frame_options = x_frame_options
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = self.x_frame_options
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if self.enable_hsts and (not self.hsts_https_only or request.url.scheme == "https"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if self.enable_csp and self.csp_policy:
            response.headers["Content-Security-Policy"] = self.csp_policy

        return response


class ContentLengthFixMiddleware:
    """修复 uvicorn 0.30.x 在 Windows 上的 Content-Length 异常。

    [ROOT-CAUSE:uvicorn-bug] 现象：偶发
        RuntimeError: Response content longer than Content-Length
    触发：uvicorn.httptools_impl 在 send body 时，starlette middleware chain
    设置了 Content-Length 头，但经过 JSONResponse.render() 后实际 body 长度
    （尤其是包含中文 UTF-8 编码、含 emoji 时）可能多于 Content-Length。

    修复策略：
    1. 用纯 ASGI 接口，不通过 BaseHTTPMiddleware 缓冲（避免其 chunked 重写）
    2. 在最内层 Response 发送 body 之前，重新计算并覆盖 Content-Length 头
    3. 强制使用 Content-Length 语义（移除 Transfer-Encoding: chunked）
    4. 兜底：捕获并吞掉 RuntimeError "Response content longer than Content-Length"

    [REFACTOR-INST:convergence] 这是生产可观测的稳定性 bug，不影响业务逻辑，
    修复必须独立于业务代码。
    """

    def __init__(self, app: ASGIApp, enable_gzip: bool = True, gzip_min_size: int = 1024):
        self.app = app
        self.enable_gzip = enable_gzip
        self.gzip_min_size = gzip_min_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 收集下游要发送的消息，重写 content-length
        state = {"body_chunks": [], "started": False, "status_code": 200, "headers": []}

        async def _send_with_fix(message):
            t = message["type"]
            if t == "http.response.start":
                state["status_code"] = message.get("status", 200)
                state["headers"] = list(message.get("headers", []))
                state["started"] = True
            elif t == "http.response.body":
                body = message.get("body", b"") or b""
                state["body_chunks"].append(body)
                # 不在这里发，等收到最后一块（more_body=False）时合并发送
                if not message.get("more_body", False):
                    full_body = b"".join(state["body_chunks"])
                    # 重写 headers：删除旧 Content-Length、Transfer-Encoding
                    new_headers = [
                        (k, v) for (k, v) in state["headers"]
                        if k.lower() not in (b"content-length", b"transfer-encoding")
                    ]
                    new_headers.append((b"content-length", str(len(full_body)).encode("latin-1")))
                    await send({"type": "http.response.start", "status": state["status_code"], "headers": new_headers})
                    await send({"type": "http.response.body", "body": full_body, "more_body": False})
                    state["body_chunks"] = []
                    state["started"] = False
            else:
                await send(message)

        try:
            await self.app(scope, receive, _send_with_fix)
            # 兜底：如果下游没走 http.response.body 完整流程，把已收集的发出
            if state["started"] and state["body_chunks"]:
                full_body = b"".join(state["body_chunks"])
                new_headers = [
                    (k, v) for (k, v) in state["headers"]
                    if k.lower() not in (b"content-length", b"transfer-encoding")
                ]
                new_headers.append((b"content-length", str(len(full_body)).encode("latin-1")))
                await send({"type": "http.response.start", "status": state["status_code"], "headers": new_headers})
                await send({"type": "http.response.body", "body": full_body, "more_body": False})
        except RuntimeError as re_exc:
            msg = str(re_exc)
            if "Response content longer than Content-Length" in msg:
                # 兜底：吞掉 uvicorn bug 异常，不再向客户端发任何东西（uvicorn 已记录 500）
                try:
                    logger.warning(
                        f"[ContentLengthFixMiddleware] 已吞掉 uvicorn Content-Length 异常: {msg[:200]}"
                    )
                except Exception:
                    pass
                return
            raise
