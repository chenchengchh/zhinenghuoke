"""
Web路由模块

按领域划分的API路由集合：
- monitor: 监控管理（RPA状态、会话）
- customer: 客户管理（CRUD、搜索、意向分析）
- analytics: 数据分析（仪表盘、统计报表）
- knowledge: 知识库管理（CRUD、检索、导入导出、向量库）
- process: 多进程与独立监听控制
- learning: 学习系统、审核与学习引擎
- intent: 意图识别与分析
- smart_reply: 统一智能回复入口
- rag: RAG、Agentic RAG、Modular RAG
- graph: 正式知识图谱路由
- enterprise: 企业管理与企业文件摄取
- query: 查询优化
- reports: 报告生成
"""

from importlib import import_module

from fastapi import APIRouter

_ROUTER_MODULES = [
    ".monitor",
    ".handoff",
    ".customer",
    ".analytics",
    ".knowledge",
    ".industry_schema",
    ".license",
    ".dingtalk",
    ".remote_control",
    ".process",
    ".learning",
    ".intent",
    ".smart_reply",
    ".doc_structuring",
    ".rag",
    ".graph",
    ".enterprise",
    ".query",
    ".reports",
]


def get_all_routers() -> list[APIRouter]:
    """获取所有路由模块。"""
    routers: list[APIRouter] = []
    for module_name in _ROUTER_MODULES:
        module = import_module(module_name, package=__name__)
        routers.append(module.router)
    return routers
