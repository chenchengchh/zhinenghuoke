"""
Web层公共工具模块

提供统一的错误处理、响应格式化、输入验证、数据转换等通用功能，
减少业务代码中的重复逻辑，提升代码可维护性和一致性。

主要功能分类：
- 响应构建：统一的API成功/错误响应格式
- 安全执行：装饰器模式统一异常处理
- 数据解析：日期时间、JSON等安全解析
- 数据处理：分页、字段验证、字符串清理
- 类型转换：安全的类型转换和校验
"""

import json
import re
import traceback
import hashlib
from datetime import datetime, date, timedelta
from typing import Any, Dict, Optional, Callable, TypeVar, Union
from functools import wraps, lru_cache
from loguru import logger

T = TypeVar('T')


# ==================== 响应构建 ====================

def api_error_response(
    message: str = "操作失败",
    status_code: int = 500,
    detail: Optional[str] = None,
    exc: Optional[Exception] = None,
    error_code: Optional[str] = None
) -> dict:
    """
    统一的API错误响应格式
    
    Args:
        message: 用户可见的错误消息（前端展示用）
        status_code: HTTP状态码（用于日志记录）
        detail: 详细技术信息（调试用，可选）
        exc: 异常对象（用于记录完整堆栈）
        error_code: 业务错误码（用于前端判断错误类型）
    
    Returns:
        dict: 标准化的错误响应 {
            "success": False,
            "message": "...",
            "detail": "...",      # 可选
            "error_code": "..."   # 可选
        }
    
    Example:
        >>> api_error_response("用户不存在", status_code=404, error_code="USER_NOT_FOUND")
        {"success": False, "message": "用户不存在", "error_code": "USER_NOT_FOUND"}
    """
    if exc:
        logger.error(f"[{status_code}] {message}: {exc}")
        logger.debug(traceback.format_exc())
    
    result = {"success": False, "message": message}
    
    if error_code:
        result["error_code"] = error_code
    
    if detail:
        result["detail"] = detail
    
    # 添加时间戳便于问题追踪
    result["timestamp"] = datetime.now().isoformat()
    
    return result


def api_success_response(
    data: Any = None,
    message: str = "操作成功",
    **extra
) -> dict:
    """
    统一的API成功响应格式
    
    Args:
        data: 返回的业务数据
        message: 成功消息描述
        **extra: 额外字段（如total, page等）
    
    Returns:
        dict: 标准化的成功响应 {
            "success": True,
            "message": "...",
            "data": {...},
            ...
        }
    
    Example:
        >>> api_success_response({"id": 1}, "查询成功", total=100)
        {"success": True, "message": "查询成功", "data": {"id": 1}, "total": 100}
    """
    result = {"success": True, "message": message}
    
    if data is not None:
        result["data"] = data
    
    result.update(extra)
    
    # 添加时间戳
    result["timestamp"] = datetime.now().isoformat()
    
    return result


# ==================== 安全执行 ====================

def safe_execute(
    func: Optional[Callable[..., T]] = None,
    *,
    error_message: str = "操作失败",
    default_value: Any = None,
    reraise: bool = False,
    log_error: bool = True,
    on_error: Optional[Callable[[Exception], Any]] = None
):
    """
    安全执行装饰器/函数，统一处理异常
    
    支持两种使用方式：
    
    方式1：作为装饰器
        @safe_execute(error_message="获取数据失败")
        def get_data():
            return risky_operation()
    
    方式2：直接调用包装函数
        result = safe_execute(lambda: risky_operation(), error_message="操作失败")
    
    Args:
        func: 被包装的函数（装饰器模式时自动传入）
        error_message: 错误时的提示消息
        default_value: 异常时的默认返回值（None则返回error_response）
        reraise: 是否重新抛出异常
        log_error: 是否记录错误日志
        on_error: 异常时的回调函数（可用于清理资源）
    
    Returns:
        装饰后的函数或执行结果
    
    Example:
        >>> @safe_execute(error_message="保存失败", default_value={"saved": False})
        ... def save_data(data):
        ...     db.save(data)
        ...     return {"saved": True}
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., Union[T, dict]]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> Union[T, dict]:
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if log_error:
                    logger.error(f"{error_message}: {e}")
                    logger.debug(traceback.format_exc())
                
                # 执行错误回调
                if on_error:
                    try:
                        on_error(e)
                    except Exception as callback_err:
                        logger.error(f"错误回调执行失败: {callback_err}")
                
                if reraise:
                    raise
                
                if default_value is not None:
                    return default_value
                
                return api_error_response(message=error_message, exc=e)
        
        return wrapper
    
    # 支持直接调用或作为装饰器
    if func is not None:
        return decorator(func)
    
    return decorator


# ==================== 数据解析 ====================

# 缓存常用的时间格式解析结果
@lru_cache(maxsize=128)
def _try_parse_datetime(value: str, fmt: str) -> Optional[datetime]:
    """尝试用指定格式解析时间（带缓存优化）"""
    try:
        return datetime.strptime(value, fmt)
    except (ValueError, TypeError):
        return None


def parse_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
    """
    安全解析日期时间字符串
    
    支持多种输入格式：
    - datetime对象：直接返回
    - int/float：视为Unix时间戳
    - 字符串：按指定格式或常见格式尝试解析
    
    Args:
        value: 输入值
        fmt: 首选的日期时间格式
    
    Returns:
        datetime 或 None（解析失败时）
    
    Example:
        >>> parse_datetime("2024-01-15 10:30:00")
        datetime(2024, 1, 15, 10, 30)
        >>> parse_datetime(1705312200)
        datetime(2024, 1, 15, 10, 30)
    """
    if value is None:
        return None
    
    if isinstance(value, datetime):
        return value
    
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time())
    
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError, OverflowError) as e:
            logger.debug(f"时间戳解析失败: value={value}, error={e}")
            return None
    
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        
        # 先尝试指定格式
        result = _try_parse_datetime(value, fmt)
        if result:
            return result
        
        # 尝试常见格式列表
        common_formats = [
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y%m%d%H%M%S",
            "%Y年%m月%d日 %H时%M分",
            "%b %d, %Y %H:%M:%S",  # Jan 15, 2024 10:30:00
            "%B %d, %Y %I:%M %p",   # January 15, 2024 10:30 AM
        ]
        
        for alt_fmt in common_formats:
            result = _try_parse_datetime(value, alt_fmt)
            if result:
                return result
        
        logger.debug(f"所有时间格式均无法解析: {value[:50]}")
        return None
    
    logger.warning(f"不支持的日期时间类型: type={type(value)}")
    return None


def safe_json_loads(value: str, default: Any = None) -> Any:
    """
    安全的JSON解析
    
    自动处理各种异常情况：
    - 空值或非字符串
    - JSON格式错误
    - 编码问题
    
    Args:
        value: JSON字符串
        default: 解析失败时的默认返回值
    
    Returns:
        解析结果或默认值
    
    Example:
        >>> safe_json_loads('{"name": "test"}')
        {'name': 'test'}
        >>> safe_json_loads('invalid json', default={})
        {}
    """
    if not value or not isinstance(value, str):
        return default
    
    value = value.strip()
    if not value:
        return default
    
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        logger.debug(f"JSON解析失败: content={value[:100]}, error={e}")
        return default
    except TypeError as e:
        logger.debug(f"JSON类型错误: error={e}")
        return default


# ==================== 数据处理 ====================

def paginate_list(
    items: list,
    page: int = 1,
    page_size: int = 20,
    max_page_size: int = 100
) -> dict:
    """
    列表分页处理（带边界保护）
    
    自动修正非法参数：
    - page < 1 → 1
    - page_size < 1 → 默认值
    - page_size > max_page_size → max_page_size
    
    Args:
        items: 原始数据列表
        page: 页码（从1开始）
        page_size: 每页数量
        max_page_size: 最大每页数量限制（防DoS）
    
    Returns:
        dict: 分页结果 {
            "items": 当前页数据,
            "total": 总数,
            "page": 当前页码,
            "page_size": 每页大小,
            "total_pages": 总页数,
            "has_next": 是否有下一页,
            "has_prev": 是否有上一页
        }
    
    Example:
        >>> paginate_list(list(range(95)), page=3, page_size=20)
        {"items": [40..59], "total": 95, "page": 3, "page_size": 20, 
         "total_pages": 5, "has_next": True, "has_prev": True}
    """
    total = len(items)
    
    # 边界保护
    page_size = max(1, min(page_size or 20, max_page_size))
    page = max(1, page or 1)
    
    total_pages = max(1, (total + page_size - 1) // page_size)
    
    # 超出范围时修正到最后一页
    if page > total_pages:
        page = total_pages
    
    start = (page - 1) * page_size
    end = start + page_size
    
    return {
        "items": items[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1
    }


def validate_required_fields(data: dict, required: list[str], context: str = "") -> tuple[bool, str]:
    """
    验证必填字段是否存在且非空
    
    Args:
        data: 待验证的字典
        required: 必填字段名列表
        context: 上下文描述（用于生成友好的错误消息）
    
    Returns:
        (is_valid, error_message): 验证结果和错误消息
    
    Example:
        >>> validate_required_fields({"name": "test"}, ["name", "age"])
        (False, "缺少必填字段: age")
        >>> validate_required_fields({"name": ""}, ["name"])
        (False, "字段不能为空: name")
    """
    if not isinstance(data, dict):
        return False, f"无效的数据格式: 期望dict，实际{type(data).__name__}"
    
    for field in required:
        if field not in data:
            return False, f"缺少必填字段: {field}"
        
        value = data[field]
        if value is None:
            return False, f"字段不能为空: {field}"
        
        if isinstance(value, str) and not value.strip():
            return False, f"字段不能为空: {field}"
    
    return True, ""


def sanitize_string(
    value: Any,
    max_length: int = 1000,
    strip_html: bool = False,
    strip_special_chars: bool = False
) -> str:
    """
    字符串安全清理
    
    多层安全处理：
    1. 空值处理
    2. 类型转换
    3. HTML标签移除（可选）
    4. 特殊字符过滤（可选）
    5. 长度截断
    
    Args:
        value: 输入值（任意类型）
        max_length: 最大长度限制
        strip_html: 是否移除HTML标签
        strip_special_chars: 是否移除特殊控制字符
    
    Returns:
        清理后的安全字符串
    
    Example:
        >>> sanitize_string("<script>alert(1)</script>", strip_html=True)
        "alert(1)"
        >>> sanitize_string("a" * 2000, max_length=100)
        "aaa...aaa(truncated)"
    """
    if value is None:
        return ""
    
    text = str(value)
    
    # 移除HTML标签
    if strip_html:
        text = re.sub(r'<[^>]+>', '', text)
        # 清理HTML实体
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    
    # 移除特殊控制字符（保留换行和制表符）
    if strip_special_chars:
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    
    # 长度限制
    if len(text) > max_length:
        truncated = text[:max_length]
        return f"{truncated}...[truncated, original={len(text)}]"
    
    return text.strip()


def calculate_hash(content: str | bytes | dict, algorithm: str = "md5") -> str:
    """
    计算内容的哈希值
    
    用于数据去重、缓存key生成、完整性校验等场景
    
    Args:
        content: 待计算的内容（支持字符串、字节、字典）
        algorithm: 哈希算法 (md5/sha1/sha256)
    
    Returns:
        十六进制哈希字符串
    
    Example:
        >>> calculate_hash("hello world")
        "5eb63bbbe01eeed093cb22bb8f5acdc3"
        >>> calculate_hash({"key": "value"})
        "af85c9bc6b48a8c7e..."
    """
    if isinstance(content, dict):
        content = json.dumps(content, sort_keys=True, ensure_ascii=False)
    
    if isinstance(content, str):
        content = content.encode('utf-8')
    
    hash_func = getattr(hashlib, algorithm.lower(), None)
    if not hash_func:
        raise ValueError(f"不支持的哈希算法: {algorithm}")
    
    return hash_func(content).hexdigest()


def mask_sensitive_info(value: str, mask_char: str = "*", keep_first: int = 2, keep_last: int = 4) -> str:
    """
    敏感信息脱敏处理
    
    用于手机号、邮箱、身份证等隐私数据的日志输出
    
    Args:
        value: 原始敏感信息
        mask_char: 掩码字符
        keep_first: 保留前几位
        keep_last: 保留后几位
    
    Returns:
        脱敏后的字符串
    
    Example:
        >>> mask_sensitive_info("13812345678")
        "138****5678"
        >>> mask_sensitive_info("user@example.com", keep_last=0)
        "u***@example.com"
    """
    if not value or len(value) <= keep_first + keep_last:
        return value
    
    masked_middle = mask_char * (len(value) - keep_first - keep_last)
    return f"{value[:keep_first]}{masked_middle}{value[-keep_last:] if keep_last > 0 else ''}"


# ==================== 类型转换 ====================

def to_int(value: Any, default: int = 0, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    """
    安全转换为整数（带范围限制）
    
    Args:
        value: 输入值
        default: 转换失败的默认值
        min_val: 最小值限制
        max_val: 最大值限制
    
    Returns:
        转换后的整数
    
    Example:
        >>> to_int("42")
        42
        >>> to_int("invalid", default=10)
        10
        >>> to_int(150, max_val=100)
        100
    """
    try:
        result = int(float(value)) if not isinstance(value, int) else value
        
        if min_val is not None:
            result = max(result, min_val)
        if max_val is not None:
            result = min(result, max_val)
        
        return result
    except (ValueError, TypeError):
        return default


def to_float(value: Any, default: float = 0.0, precision: Optional[int] = None) -> float:
    """
    安全转换为浮点数（带精度控制）
    
    Args:
        value: 输入值
        default: 转换失败的默认值
        precision: 小数位数（None表示不限制）
    
    Returns:
        转换后的浮点数
    """
    try:
        result = float(value)
        
        if precision is not None:
            result = round(result, precision)
        
        return result
    except (ValueError, TypeError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    """
    安全转换为布尔值
    
    支持多种真值表示：true, yes, 1, on, y
    
    Args:
        value: 输入值
        default: 无法识别时的默认值
    
    Returns:
        布尔值
    """
    if isinstance(value, bool):
        return value
    
    if isinstance(value, (int, float)):
        return bool(value)
    
    if isinstance(value, str):
        return value.lower() in ('true', 'yes', '1', 'on', 'y')
    
    return default


def to_list(value: Any, separator: Optional[str] = None, item_type: Optional[type] = None) -> list:
    """
    安全转换为列表
    
    Args:
        value: 输入值（支持字符串分割、元组转换等）
        separator: 字符串分隔符（如 ",","|" ）
        item_type: 元素目标类型（int/str等）
    
    Returns:
        列表
    
    Example:
        >>> to_list("a,b,c", separator=",")
        ['a', 'b', 'c']
        >>> to_list((1, 2, 3))
        [1, 2, 3]
        >>> to_list(None)
        []
    """
    if value is None:
        return []
    
    if isinstance(value, list):
        return value
    
    if isinstance(value, (tuple, set)):
        return list(value)
    
    if isinstance(value, str) and separator:
        items = [item.strip() for item in value.split(separator) if item.strip()]
        
        if item_type:
            try:
                items = [item_type(item) for item in items]
            except (ValueError, TypeError):
                pass
        
        return items
    
    return [value]


# ==================== 时间工具 ====================

def format_duration(seconds: float | int, show_ms: bool = False) -> str:
    """
    格式化持续时间
    
    Args:
        seconds: 秒数
        show_ms: 是否显示毫秒
    
    Returns:
        格式化字符串 (如 "1h 23m 45s" 或 "1h 23m 45.678s")
    
    Example:
        >>> format_duration(3661.5)
        "1h 1m 1s"
        >>> format_duration(3661.5, show_ms=True)
        "1h 1m 1.500s"
    """
    if seconds < 0:
        seconds = abs(seconds)
    
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    
    parts = []
    
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    
    if show_ms and secs >= 0:
        parts.append(f"{secs:.3f}s")
    else:
        parts.append(f"{int(secs)}s")
    
    return " ".join(parts) if parts else "0s"


def get_time_ago(timestamp: datetime | str | float, now: Optional[datetime] = None) -> str:
    """
    计算相对时间描述（如"3分钟前"、"2小时前"）
    
    Args:
        timestamp: 目标时间
        now: 参照时间点（默认为当前时间）
    
    Returns:
        相对时间描述字符串
    
    Example:
        >>> get_time_ago(datetime.now() - timedelta(minutes=5))
        "5分钟前"
    """
    parsed = parse_datetime(timestamp) if not isinstance(timestamp, datetime) else timestamp
    
    if not parsed:
        return "未知"
    
    now = now or datetime.now()
    delta = now - parsed
    
    if delta.total_seconds() < 0:
        return "刚刚"
    
    seconds = delta.total_seconds()
    
    if seconds < 60:
        return "刚刚"
    elif seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    elif seconds < 86400:
        return f"{int(seconds // 3600)}小时前"
    elif seconds < 604800:
        return f"{int(seconds // 86400)}天前"
    elif seconds < 2592000:
        return f"{int(seconds // 604800)}周前"
    elif seconds < 31536000:
        return f"{int(seconds // 2592000)}个月前"
    else:
        return f"{int(seconds // 31536000)}年前"


# ==================== 响应构建器 ====================

class ResponseBuilder:
    """
    链式响应构建器
    
    提供流畅的API来构建标准化的API响应，
    减少重复代码并保持响应格式一致。
    
    Example:
        >>> builder = ResponseBuilder()
        >>> response = builder.success("查询成功").with_data(items).with_total(100).build()
        >>> print(response)
        {"success": True, "message": "查询成功", "data": [...], "total": 100, "timestamp": "..."}
    """
    
    def __init__(self):
        self._data: Dict[str, Any] = {}
    
    def success(self, message: str = "操作成功") -> 'ResponseBuilder':
        """设置成功状态"""
        self._data["success"] = True
        self._data["message"] = message
        return self
    
    def error(self, message: str = "操作失败", status_code: int = 500) -> 'ResponseBuilder':
        """设置错误状态"""
        self._data["success"] = False
        self._data["message"] = message
        self._data["status_code"] = status_code
        return self
    
    def with_data(self, data: Any) -> 'ResponseBuilder':
        """添加业务数据"""
        self._data["data"] = data
        return self
    
    def with_total(self, total: int) -> 'ResponseBuilder':
        """添加总数"""
        self._data["total"] = total
        return self
    
    def with_pagination(self, page: int, page_size: int, total: int) -> 'ResponseBuilder':
        """添加分页信息"""
        from math import ceil
        self._data["page"] = page
        self._data["page_size"] = page_size
        self._data["total"] = total
        self._data["total_pages"] = ceil(total / page_size) if page_size > 0 else 0
        return self
    
    def with_extra(self, **kwargs) -> 'ResponseBuilder':
        """添加额外字段"""
        self._data.update(kwargs)
        return self
    
    def with_timestamp(self, timestamp: Optional[datetime] = None) -> 'ResponseBuilder':
        """添加自定义时间戳"""
        self._data["timestamp"] = timestamp.isoformat() if timestamp else datetime.now().isoformat()
        return self
    
    def build(self) -> dict:
        """构建最终响应字典"""
        # 如果没有显式设置时间戳，自动添加
        if "timestamp" not in self._data:
            self._data["timestamp"] = datetime.now().isoformat()
        
        return self._data.copy()
    
    def __repr__(self) -> str:
        state = "success" if self._data.get("success") else "error"
        return f"<ResponseBuilder [{state}] keys={list(self._data.keys())}>"
