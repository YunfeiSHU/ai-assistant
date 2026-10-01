"""MCP 路由（``docs/05`` §5）。

三条路由都要鉴权：MCP 状态会暴露「本服务能碰到哪些外部系统」（Server 名、工具名、
连接错误），这些信息的价值对攻击者高于对运维的便利。

重载不做成「热改配置」：``MCP_SERVERS`` 需要重启才能生效，而 ``reload`` 解决的是
另一个问题 —— Server 侧更新了工具列表 / 连接掉线后要恢复。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from app.api.deps import McpManagerDep, PaginationDep, ToolRegistryDep, UserId
from app.mcp.tools import sync_mcp_tools
from app.schemas.agent import ToolDefinition
from app.schemas.common import Page
from app.schemas.mcp import McpReloadRequest, McpServerStatusItem

router = APIRouter(tags=["MCP"])


@router.get(
    "/mcp/servers",
    response_model=Page[McpServerStatusItem],
    summary="列出 MCP Server 状态",
    description=(
        "返回配置里的全部 MCP Server 及其实时连接状态。"
        "`required=true` 且状态非 `connected` 时 `/health/ready` 会返回 503。"
    ),
)
async def list_mcp_servers(
    user_id: UserId,
    manager: McpManagerDep,
    pagination: PaginationDep,
) -> Page[McpServerStatusItem]:
    """列出 MCP Server 状态。

    游标是对**内存列表**做的偏移分页：Server 数量是配置规模（个位数），
    用 ``created_at`` 排序键反而要对状态对象硬塞一个它没有的字段。
    """
    statuses = manager.statuses()
    start = _decode_offset(pagination.cursor)
    window = statuses[start : start + pagination.limit]
    has_more = start + len(window) < len(statuses)
    return Page[McpServerStatusItem](
        # ``McpServerStatus`` 是 ``slots=True`` 的 dataclass，没有 ``__dict__``，
        # 必须用 ``dataclasses.asdict`` 取字段（直接 ``__dict__`` 会 AttributeError）
        items=[McpServerStatusItem(**asdict(item)) for item in window],
        next_cursor=str(start + len(window)) if has_more else None,
        has_more=has_more,
    )


@router.get(
    "/mcp/servers/{name}/tools",
    response_model=Page[ToolDefinition],
    summary="列出某个 MCP Server 的工具",
    description="与 `GET /tools?source=mcp` 的子集一致，便于单 Server 排障。",
)
async def list_mcp_server_tools(
    name: str,
    user_id: UserId,
    manager: McpManagerDep,
    registry: ToolRegistryDep,
    pagination: PaginationDep,
) -> Page[ToolDefinition]:
    """列出某个 Server 注册后的工具。

    数据源是工具注册表而不是 ``manager`` 里缓存的 ``tools/list`` 结果：注册表是「模型实际
    能调到的东西」，而 Server 返回的列表可能包含被 allowlist 过滤掉的工具 —— 这个接口的
    用途正是确认「过滤有没有按预期生效」。
    """
    client = manager.get(name)
    specs = [
        spec
        for spec in registry.specs(source="mcp", enabled=None)
        if spec.mcp_server == client.name
    ]
    start = _decode_offset(pagination.cursor)
    window = specs[start : start + pagination.limit]
    has_more = start + len(window) < len(specs)
    return Page[ToolDefinition](
        items=[ToolDefinition(**spec.to_public()) for spec in window],
        next_cursor=str(start + len(window)) if has_more else None,
        has_more=has_more,
    )


@router.post(
    "/mcp/servers/{name}/reload",
    response_model=McpServerStatusItem,
    summary="重载单个 MCP Server",
    description=(
        "关闭旧连接并重建，成功后刷新该 Server 的工具注册。"
        "重载期间该 Server 的工具调用会失败（`TOOL_EXECUTION_FAILED`），其它 Server 不受影响。"
    ),
)
async def reload_mcp_server(
    name: str,
    body: McpReloadRequest,
    user_id: UserId,
    manager: McpManagerDep,
    registry: ToolRegistryDep,
) -> McpServerStatusItem:
    """重载单个 Server（``AC-MCP-05``）。

    Raises:
        AppError: ``404 MCP_SERVER_NOT_FOUND`` / ``503 MCP_SERVER_UNAVAILABLE``
    """
    status = await manager.reload(name, force=body.force)
    # 重连后工具列表可能变了（新增 / 下线）：注册表必须跟着刷新，
    # 否则「重载成功」但模型调的还是旧工具集合 —— 那是比失败更坏的结果。
    sync_mcp_tools(registry, manager, server=name)
    return McpServerStatusItem(**asdict(status))


def _decode_offset(cursor: str | None) -> int:
    """把游标解成偏移量；非法游标按 0 处理（等价于从头开始）。

    不复用 ``decode_cursor``：那是给 ``(created_at, id)`` 排序键用的，而 MCP 的列表没有
    时间维度。用一个明确的、自己的偏移语义，比硬套一个「看起来通用」的游标格式更容易看懂。
    """
    if not cursor:
        return 0
    try:
        return max(int(cursor), 0)
    except ValueError:
        return 0


__all__ = ["router"]
