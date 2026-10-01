"""工具路由（``docs/04`` §4.1 / §4.2）。

两条路由的鉴权策略刻意不同：``GET /tools`` 需要鉴权（工具清单会暴露内部能力边界）；
``POST /tools/{name}/invoke`` 在 prod 下不存在（404），连「有这个工具」都不该被外部确认。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import PaginationDep, ToolServiceDep, UserId
from app.schemas.agent import (
    ToolDefinition,
    ToolInvokeRequest,
    ToolInvokeResponse,
    ToolListResponse,
)

router = APIRouter(tags=["工具"])


@router.get(
    "/tools",
    response_model=ToolListResponse,
    summary="列出可用工具",
    description=(
        "返回模型的可用工具定义（内置 + MCP）。`parameters` 是合法的 JSON Schema；"
        "`enabled=false` 的工具（如默认关闭的 `http_fetch`）默认不返回。"
    ),
)
async def list_tools(
    user_id: UserId,
    service: ToolServiceDep,
    pagination: PaginationDep,
    source: Annotated[
        str | None,
        Query(description="按来源过滤：builtin / mcp"),
    ] = None,
    enabled: Annotated[
        bool | None,
        Query(description="按启用状态过滤；不传则只返回已启用的工具"),
    ] = None,
) -> ToolListResponse:
    """列出工具定义。"""
    # 不传 enabled 时默认只返回启用的：默认视图应该是「模型实际能用的集合」，
    # 否则调用方会照着清单去调用一个必然被拒的工具。
    effective_enabled = True if enabled is None else enabled
    specs, next_cursor, has_more = service.list_specs(
        source=source,
        enabled=effective_enabled,
        limit=pagination.limit,
        cursor=pagination.cursor,
    )
    return ToolListResponse(
        items=[ToolDefinition(**spec.to_public()) for spec in specs],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post(
    "/tools/{name}/invoke",
    response_model=ToolInvokeResponse,
    summary="调试调用工具",
    description=(
        "仅 `local` / `dev` 环境可用（`prod` 返回 404）。"
        "`dry_run=true` 只校验参数不执行；`write` 类工具强制按 `dry_run` 处理。"
    ),
)
async def invoke_tool(
    name: str,
    body: ToolInvokeRequest,
    user_id: UserId,
    service: ToolServiceDep,
) -> ToolInvokeResponse:
    """调试调用单个工具（失败映射为 HTTP 错误码）。"""
    record = await service.invoke(
        name,
        body.arguments,
        user_id=user_id,
        dry_run=body.dry_run,
    )
    return ToolInvokeResponse(
        name=record.name,
        status=record.status,
        result=dict(record.payload),
        elapsed_ms=record.elapsed_ms,
        error=record.error or None,
    )


__all__ = ["router"]
