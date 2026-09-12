from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from src.common.report_generator import ReportFormat, get_report_generator
from src.web.bot_service import get_bot_service
from src.web.dependencies.analytics import get_enhanced_analytics_app_service

router = APIRouter(tags=["报告"])
_report_generator = None
_report_generator_lock = threading.Lock()


def _get_report_generator():
    global _report_generator
    if _report_generator is None:
        with _report_generator_lock:
            if _report_generator is None:
                bot_service = get_bot_service()
                _report_generator = get_report_generator(
                    database=bot_service.db,
                    analytics_service=get_enhanced_analytics_app_service(),
                )
    return _report_generator


def _parse_report_date(date_value: Optional[str], pattern: str) -> datetime:
    return datetime.strptime(date_value, pattern) if date_value else datetime.now()


@router.get("/api/reports/daily")
async def generate_daily_report(date: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_daily_report(_parse_report_date(date, "%Y-%m-%d"))
        return {
            "success": True,
            "report": json.loads(report_generator.export_report(report, ReportFormat.JSON)),
        }
    except Exception as exc:
        logger.error(f"生成日报失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/weekly")
async def generate_weekly_report(week_start: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_weekly_report(_parse_report_date(week_start, "%Y-%m-%d"))
        return {
            "success": True,
            "report": json.loads(report_generator.export_report(report, ReportFormat.JSON)),
        }
    except Exception as exc:
        logger.error(f"生成周报失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/monthly")
async def generate_monthly_report(month: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_monthly_report(_parse_report_date(month, "%Y-%m"))
        return {
            "success": True,
            "report": json.loads(report_generator.export_report(report, ReportFormat.JSON)),
        }
    except Exception as exc:
        logger.error(f"生成月报失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/customer/{customer_name}")
async def generate_customer_report(customer_name: str):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_customer_report(customer_name)
        return {
            "success": True,
            "report": json.loads(report_generator.export_report(report, ReportFormat.JSON)),
        }
    except Exception as exc:
        logger.error(f"生成客户报告失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/{report_id}/export")
async def export_report(report_id: str, format: str = "json"):
    try:
        report_generator = _get_report_generator()
        report = report_generator.get_cached_report(report_id)
        if not report:
            return JSONResponse(status_code=404, content={"success": False, "message": "报告不存在"})

        format_mapping = {
            "json": ReportFormat.JSON,
            "html": ReportFormat.HTML,
            "markdown": ReportFormat.MARKDOWN,
        }
        content = report_generator.export_report(report, format_mapping.get(format, ReportFormat.JSON))
        return {"success": True, "format": format, "content": content}
    except Exception as exc:
        logger.error(f"导出报告失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/list")
async def list_cached_reports():
    try:
        reports = _get_report_generator().list_cached_reports()
        return {"success": True, "count": len(reports), "reports": reports}
    except Exception as exc:
        logger.error(f"获取报告列表失败: {exc}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@router.get("/api/reports/daily/html")
async def get_daily_report_html(date: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_daily_report(_parse_report_date(date, "%Y-%m-%d"))
        return HTMLResponse(content=report_generator.export_report(report, ReportFormat.HTML))
    except Exception as exc:
        logger.error(f"生成日报HTML失败: {exc}")
        return HTMLResponse(content=f"<html><body><h1>错误</h1><p>{str(exc)}</p></body></html>")


@router.get("/api/reports/weekly/html")
async def get_weekly_report_html(week_start: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_weekly_report(_parse_report_date(week_start, "%Y-%m-%d"))
        return HTMLResponse(content=report_generator.export_report(report, ReportFormat.HTML))
    except Exception as exc:
        logger.error(f"生成周报HTML失败: {exc}")
        return HTMLResponse(content=f"<html><body><h1>错误</h1><p>{str(exc)}</p></body></html>")


@router.get("/api/reports/monthly/html")
async def get_monthly_report_html(month: Optional[str] = None):
    try:
        report_generator = _get_report_generator()
        report = report_generator.generate_monthly_report(_parse_report_date(month, "%Y-%m"))
        return HTMLResponse(content=report_generator.export_report(report, ReportFormat.HTML))
    except Exception as exc:
        logger.error(f"生成月报HTML失败: {exc}")
        return HTMLResponse(content=f"<html><body><h1>错误</h1><p>{str(exc)}</p></body></html>")


__all__ = [
    "router",
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "generate_customer_report",
    "export_report",
    "list_cached_reports",
    "get_daily_report_html",
    "get_weekly_report_html",
    "get_monthly_report_html",
]
