from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["查询优化"])


class QueryOptimizeRequest(BaseModel):
    query: str


@router.post("/api/query/optimize")
async def optimize_query(request: QueryOptimizeRequest):
    # 修复 R4：AdvancedQueryPreprocessor 已删除，该接口返回 410 Gone
    from fastapi import HTTPException
    raise HTTPException(
        status_code=410,
        detail="该接口已废弃，查询改写由 ContextAwareQueryRewriter 在主链中自动处理",
    )


__all__ = ["router", "QueryOptimizeRequest", "optimize_query"]
