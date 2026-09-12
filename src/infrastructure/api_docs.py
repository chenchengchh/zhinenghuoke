# -*- coding: utf-8 -*-
"""
API文档配置模块
提供OpenAPI规范的API文档配置
"""
from typing import Any, Dict, List, Optional
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def custom_openapi(app: FastAPI):
    """
    自定义OpenAPI文档配置
    
    Args:
        app: FastAPI应用实例
    """
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title="智能知识库系统 API",
        version="1.0.0",
        description="""
## 系统简介

智能知识库系统是一个企业级的智能问答和知识管理平台，提供以下核心功能：

### 核心功能

- **知识库管理**: 支持知识条目的增删改查、批量导入导出
- **智能问答**: 基于RAG的智能问答，支持多轮对话
- **向量检索**: 高效的语义检索，支持混合检索策略
- **知识图谱**: 结构化知识表示，支持关系推理
- **自学习引擎**: 自动学习和优化知识库

### 技术特性

- 🔐 企业级安全认证
- 📊 完整的审计日志
- ⚡ 高性能缓存机制
- 🔄 异步任务处理
- 📈 实时监控和告警

### 认证方式

所有API请求需要在Header中携带认证令牌：

```
Authorization: Bearer <your_access_token>
```

### 响应格式

所有API响应遵循统一格式：

```json
{
    "code": 0,
    "message": "操作成功",
    "data": {},
    "timestamp": "2024-01-01T12:00:00",
    "request_id": "req_123456"
}
```
        """,
        routes=app.routes,
        tags=[
            {
                "name": "知识库管理",
                "description": "知识条目的增删改查、批量操作等"
            },
            {
                "name": "智能问答",
                "description": "智能问答、多轮对话、意图识别等"
            },
            {
                "name": "向量检索",
                "description": "向量搜索、混合检索、重排序等"
            },
            {
                "name": "知识图谱",
                "description": "实体管理、关系管理、图谱查询等"
            },
            {
                "name": "自学习引擎",
                "description": "知识学习、质量评估、自动优化等"
            },
            {
                "name": "系统管理",
                "description": "系统配置、监控、日志等"
            }
        ]
    )
    
    openapi_schema["info"]["contact"] = {
        "name": "技术支持",
        "email": "support@example.com",
        "url": "https://example.com/support"
    }
    
    openapi_schema["info"]["license"] = {
        "name": "MIT License",
        "url": "https://opensource.org/licenses/MIT"
    }
    
    openapi_schema["servers"] = [
        {
            "url": "http://localhost:8023",
            "description": "开发环境"
        },
        {
            "url": "https://api.example.com",
            "description": "生产环境"
        }
    ]
    
    openapi_schema["components"]["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT认证令牌"
        },
        "apiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API密钥认证"
        }
    }
    
    openapi_schema["components"]["schemas"]["ErrorResponse"] = {
        "type": "object",
        "properties": {
            "code": {
                "type": "integer",
                "description": "错误码",
                "example": 400
            },
            "message": {
                "type": "string",
                "description": "错误信息",
                "example": "请求参数错误"
            },
            "timestamp": {
                "type": "string",
                "format": "date-time",
                "description": "响应时间戳"
            },
            "request_id": {
                "type": "string",
                "description": "请求追踪ID"
            },
            "details": {
                "type": "object",
                "description": "错误详情"
            }
        }
    }
    
    openapi_schema["components"]["schemas"]["PaginatedResponse"] = {
        "type": "object",
        "properties": {
            "code": {
                "type": "integer",
                "example": 0
            },
            "message": {
                "type": "string",
                "example": "操作成功"
            },
            "data": {
                "type": "array",
                "items": {}
            },
            "timestamp": {
                "type": "string",
                "format": "date-time"
            },
            "request_id": {
                "type": "string"
            },
            "page_info": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "example": 1},
                    "page_size": {"type": "integer", "example": 20},
                    "total": {"type": "integer", "example": 100},
                    "total_pages": {"type": "integer", "example": 5},
                    "has_next": {"type": "boolean", "example": True},
                    "has_prev": {"type": "boolean", "example": False}
                }
            }
        }
    }
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema


def setup_swagger_ui(app: FastAPI):
    """
    配置Swagger UI
    
    Args:
        app: FastAPI应用实例
    """
    app.openapi = lambda: custom_openapi(app)
    
    app.swagger_ui_parameters = {
        "syntaxHighlight": True,
        "syntaxHighlight.theme": "monokai",
        "displayRequestDuration": True,
        "docExpansion": "list",
        "defaultModelsExpandDepth": 2,
        "defaultModelExpandDepth": 2,
        "operationsSorter": "alpha",
        "tagsSorter": "alpha",
        "filter": True,
        "persistAuthorization": True,
        "displayOperationId": False,
        "tryItOutEnabled": True,
        "requestSnippetsEnabled": True
    }


API_DOCS_CONFIG = {
    "title": "智能知识库系统 API",
    "description": "企业级智能问答和知识管理平台",
    "version": "1.0.0",
    "docs_url": "/docs",
    "redoc_url": "/redoc",
    "openapi_url": "/openapi.json",
    "swagger_ui_parameters": {
        "syntaxHighlight": True,
        "displayRequestDuration": True,
        "docExpansion": "list",
        "persistAuthorization": True
    }
}


ERROR_CODES = {
    0: "成功",
    400: "请求参数错误",
    401: "未授权访问",
    403: "禁止访问",
    404: "资源不存在",
    409: "资源冲突",
    422: "数据验证失败",
    429: "请求频率超限",
    500: "服务器内部错误",
    501: "数据库操作失败",
    502: "外部服务调用失败",
    503: "服务暂时不可用",
    1001: "知识库内容不存在",
    1002: "向量嵌入失败",
    1003: "向量检索失败",
    1004: "LLM服务调用失败",
    1005: "RAG管道处理失败",
    2001: "会话已过期",
    2002: "令牌无效",
    2003: "权限不足",
    3001: "数据同步失败",
    3002: "缓存操作失败",
    3003: "文件上传失败"
}
