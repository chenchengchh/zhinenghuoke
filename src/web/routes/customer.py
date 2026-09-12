"""
客户管理路由模块

处理客户CRUD、搜索、意向分析等功能
"""

from datetime import datetime
from io import BytesIO
from typing import Optional
from urllib.parse import quote
import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger

from src.web.dependencies.enterprise import bind_request_tenant_context

router = APIRouter(prefix="/api/customers", tags=["客户管理"])
MAX_CUSTOMER_PAGE_SIZE = 500


def _safe_excel_sheet_name(name: str) -> str:
    safe = "".join("_" if ch in r'[]:*?/\\' else ch for ch in str(name or "Sheet1"))
    safe = safe.strip() or "Sheet1"
    return safe[:31]


def _build_customer_export_rows(customers: list[dict]) -> list[dict]:
    rows = []
    for index, customer in enumerate(customers or [], start=1):
        rows.append(
            {
                "序号": index,
                "平台": customer.get("platform", ""),
                "客户昵称": customer.get("nickname", ""),
                "客户ID": customer.get("sec_uid", ""),
                "意向等级": customer.get("intent_level", ""),
                "意向分数": customer.get("intent_score", ""),
                "状态": customer.get("status", ""),
                "评论内容": customer.get("comment_content", ""),
                "地理位置": customer.get("ip_location", ""),
                "评论时间": customer.get("comment_time", ""),
                "来源视频标题": customer.get("source_video_title", ""),
                "来源视频链接": customer.get("source_video_url", ""),
                "主页链接": customer.get("profile_url", ""),
                "采集时间": customer.get("created_at", ""),
                "更新时间": customer.get("updated_at", ""),
                "标签": "、".join(customer.get("tags", []) or []),
            }
        )
    return rows


def _build_excel_response(rows: list[dict], *, file_prefix: str, sheet_name: str) -> StreamingResponse:
    dataframe = pd.DataFrame(rows or [{}])
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name=_safe_excel_sheet_name(sheet_name))
    output.seek(0)
    filename = f"{file_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    ascii_filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    encoded_filename = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename={ascii_filename}; filename*=UTF-8''{encoded_filename}",
    }
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


class CustomerUpdateRequest(BaseModel):
    """客户更新请求模型"""
    sec_uid: str
    platform: str = "douyin"
    status: Optional[str] = None
    interact_status: Optional[str] = None
    intent_level: Optional[str] = None
    intent_score: Optional[float] = None
    tags: Optional[list] = None


class CustomerExportSelectionItem(BaseModel):
    sec_uid: str
    platform: str = "douyin"


class CustomerExportSelectedRequest(BaseModel):
    items: list[CustomerExportSelectionItem]


class CustomerBatchDeleteRequest(BaseModel):
    items: list[CustomerExportSelectionItem]


@router.get("")
async def get_customers(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    interact_status: Optional[str] = None,
    intent_level: Optional[str] = None,
    comment_time_start: Optional[str] = None,
    comment_time_end: Optional[str] = None,
    created_after: Optional[str] = None,
    created_by_task_id: Optional[str] = None,
):
    """
    获取客户列表
    
    Args:
        platform: 平台过滤 (douyin/xiaohongshu)
        status: 状态过滤 (pending/sent/replied)
        intent_level: 意向等级过滤 (A/B/C/D/E)
    
    Returns:
        dict: 分页客户列表
    """
    try:
        bind_request_tenant_context(request)
        from src.common.database import DatabaseManager
        page_size = max(1, min(int(page_size or 20), MAX_CUSTOMER_PAGE_SIZE))
        
        db = DatabaseManager()
        return await db.async_get_customers_page(
            page=page,
            page_size=page_size,
            platform=platform or None,
            status=status or None,
            interact_status=interact_status or None,
            intent_level=intent_level or None,
            comment_time_start=comment_time_start or None,
            comment_time_end=comment_time_end or None,
            created_after=created_after or None,
            created_by_task_id=created_by_task_id or None,
        )
    except Exception as e:
        logger.error(f"获取客户列表失败: {e}")
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 0,
            "summary": {
                "total": 0,
                "pending": 0,
                "sent": 0,
                "replied": 0,
                "intent_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
            }
        }


@router.get("/search")
async def search_customers(
    request: Request,
    q: str,
    page: int = 1,
    page_size: int = 20,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    interact_status: Optional[str] = None,
    comment_time_start: Optional[str] = None,
    comment_time_end: Optional[str] = None,
    created_after: Optional[str] = None,
    created_by_task_id: Optional[str] = None,
):
    """搜索客户"""
    try:
        bind_request_tenant_context(request)
        from src.common.database import DatabaseManager
        page_size = max(1, min(int(page_size or 20), MAX_CUSTOMER_PAGE_SIZE))

        db = DatabaseManager()
        return await db.async_search_customers_page(
            keyword=q,
            page=page,
            page_size=page_size,
            platform=platform or None,
            status=status or None,
            interact_status=interact_status or None,
            comment_time_start=comment_time_start or None,
            comment_time_end=comment_time_end or None,
            created_after=created_after or None,
            created_by_task_id=created_by_task_id or None,
        )
    except Exception as e:
        logger.error(f"搜索客户失败: {e}")
        return {
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "total_pages": 0,
            "summary": {
                "total": 0,
                "pending": 0,
                "sent": 0,
                "replied": 0,
                "intent_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
            }
        }


@router.get("/export")
async def export_customers(
    request: Request,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    interact_status: Optional[str] = None,
    intent_level: Optional[str] = None,
    q: Optional[str] = None,
    comment_time_start: Optional[str] = None,
    comment_time_end: Optional[str] = None,
    created_after: Optional[str] = None,
    created_by_task_id: Optional[str] = None,
):
    """按当前筛选条件导出客户列表 Excel。"""
    try:
        bind_request_tenant_context(request)
        from src.common.database import DatabaseManager

        db = DatabaseManager()
        keyword = (q or "").strip()
        page_size = 100000
        if keyword:
            payload = await db.async_search_customers_page(
                keyword=keyword,
                page=1,
                page_size=page_size,
                platform=platform or None,
                status=status or None,
                interact_status=interact_status or None,
                comment_time_start=comment_time_start or None,
                comment_time_end=comment_time_end or None,
                created_after=created_after or None,
                created_by_task_id=created_by_task_id or None,
            )
        else:
            payload = await db.async_get_customers_page(
                page=1,
                page_size=page_size,
                platform=platform or None,
                status=status or None,
                interact_status=interact_status or None,
                intent_level=intent_level or None,
                comment_time_start=comment_time_start or None,
                comment_time_end=comment_time_end or None,
                created_after=created_after or None,
                created_by_task_id=created_by_task_id or None,
            )
        rows = _build_customer_export_rows(payload.get("items", []))
        return _build_excel_response(rows, file_prefix="客户列表", sheet_name="客户列表")
    except Exception as e:
        logger.error(f"导出客户列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")


@router.post("/export-selected")
async def export_selected_customers(request: CustomerExportSelectedRequest, http_request: Request):
    """导出选中的客户 Excel。"""
    try:
        bind_request_tenant_context(http_request)
        from src.common.database import DatabaseManager

        db = DatabaseManager()
        customers: list[dict] = []
        seen_keys: set[str] = set()
        for item in request.items or []:
            sec_uid = str(item.sec_uid or "").strip()
            platform = str(item.platform or "douyin").strip() or "douyin"
            if not sec_uid:
                continue
            key = f"{platform}::{sec_uid}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            customer = db.get_user_by_id(sec_uid, platform)
            if customer:
                customers.append(customer)

        rows = _build_customer_export_rows(customers)
        return _build_excel_response(rows, file_prefix="选中客户", sheet_name="选中客户")
    except Exception as e:
        logger.error(f"导出选中客户失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出失败: {str(e)}")


@router.post("/delete-selected")
async def delete_selected_customers(request: CustomerBatchDeleteRequest, http_request: Request):
    """批量删除选中的客户。"""
    try:
        bind_request_tenant_context(http_request)
        from src.common.database import DatabaseManager

        db = DatabaseManager()
        seen_keys: set[str] = set()
        deleted_count = 0
        missing_count = 0

        for item in request.items or []:
            sec_uid = str(item.sec_uid or "").strip()
            platform = str(item.platform or "douyin").strip() or "douyin"
            if not sec_uid:
                continue
            key = f"{platform}::{sec_uid}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if db.delete_user(sec_uid, platform):
                deleted_count += 1
            else:
                missing_count += 1

        if not seen_keys:
            raise HTTPException(status_code=400, detail="请先选择要删除的客户")

        return {
            "success": True,
            "message": f"成功删除 {deleted_count} 位客户",
            "deleted_count": deleted_count,
            "missing_count": missing_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量删除选中客户失败: {e}")
        raise HTTPException(status_code=500, detail=f"批量删除失败: {str(e)}")


@router.get("/{sec_uid}")
async def get_customer(sec_uid: str, request: Request, platform: str = "douyin"):
    """获取单个客户详情"""
    bind_request_tenant_context(request)
    from src.common.database import DatabaseManager
    
    db = DatabaseManager()
    customer = db.get_user_by_id(sec_uid, platform)
    if customer:
        return customer
    raise HTTPException(status_code=404, detail="客户不存在")


@router.put("/{sec_uid}")
async def update_customer(sec_uid: str, request: CustomerUpdateRequest, http_request: Request):
    """更新客户信息"""
    try:
        bind_request_tenant_context(http_request)
        from src.common.database import DatabaseManager
        
        db = DatabaseManager()
        existing = db.get_user_by_id(sec_uid, request.platform) or {}
        
        if request.status is not None:
            db.update_customer_status(sec_uid, request.platform, request.status)
        if request.interact_status is not None:
            db.update_customer_interact_status(sec_uid, request.platform, request.interact_status)
        
        if request.intent_level is not None or request.intent_score is not None:
            current_level = request.intent_level or existing.get('intent_level') or 'D'
            current_score = request.intent_score if request.intent_score is not None else existing.get('intent_score', 0)
            db.update_customer_intent(sec_uid, request.platform, current_level, current_score)
        
        if request.tags is not None:
            db.update_customer_tags(sec_uid, request.platform, request.tags)
        
        return {"message": "更新成功", "success": True}
    except Exception as e:
        logger.error(f"更新客户失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新失败: {str(e)}")


@router.delete("/{sec_uid}")
async def delete_customer(sec_uid: str, request: Request, platform: str = "douyin"):
    """删除客户"""
    try:
        bind_request_tenant_context(request)
        from src.common.database import DatabaseManager

        db = DatabaseManager()
        success = db.delete_user(sec_uid, platform)
        if success:
            return {"message": "删除成功", "success": True}
        raise HTTPException(status_code=404, detail="客户不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除客户失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


class IntentAnalyzeRequest(BaseModel):
    """意向分析请求模型"""
    customer_id: str
    platform: str = "douyin"
    message_content: Optional[str] = None


@router.post("/{sec_uid}/analyze-intent")
async def analyze_intent(sec_uid: str, request: IntentAnalyzeRequest, http_request: Request):
    """
    分析客户意向
    
    基于消息内容分析客户的购买意向等级和具体意图类型
    """
    try:
        bind_request_tenant_context(http_request)
        from src.common.enhanced_customer_service import get_enhanced_customer_service
        
        service = get_enhanced_customer_service()
        
        result = service.analyze_and_update_intent(
            user_id=sec_uid,
            message=request.message_content or "",
            context={"platform": request.platform}
        )
        
        return {
            "success": True,
            "intent_level": result.get("intent_level"),
            "primary_intent": result.get("primary_intent"),
            "confidence": result.get("confidence"),
            "factors": result.get("factors")
        }
    except Exception as e:
        logger.error(f"意向分析失败: {e}")
        return {"success": False, "message": f"分析失败: {str(e)}"}
