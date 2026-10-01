"""MCP 相关数据模型（契约见 ``docs/05-MCP接入.md`` §5）。

字段直接对应 ``docs/05`` 里的表格，不加不减：多一个字段要在文档里说明来源，少一个
客户端就要猜 —— 消费者是运维脚本与排障面板，没有猜的余地。

列表响应用通用的 :class:`~app.schemas.common.Page`，不各自定义 ``XxxListResponse``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

#: 连接状态（``docs/05`` §3.1）
McpServerStatusName = Literal["connected", "connecting", "unavailable", "disabled"]

#: 传输方式（``docs/05`` §2.1）
McpTransportName = Literal["stdio", "streamable_http"]


class McpServerStatusItem(StrictModel):
    """``GET /mcp/servers`` 的一项（也是 reload 的响应体）。"""

    name: str = Field(description="配置里的 Server 名")
    transport: McpTransportName = Field(description="传输方式")
    status: McpServerStatusName = Field(description="连接状态")
    required: bool = Field(default=False, description="连不上时是否拒绝启动")
    tools_count: int = Field(default=0, ge=0, description="过滤后的已注册工具数")
    last_error: str | None = Field(default=None, description="最近一次失败原因（已脱敏）")
    last_connected_at: str | None = Field(
        default=None, description="最近一次连接成功时间（RFC3339 UTC）"
    )
    latency_ms: int | None = Field(default=None, ge=0, description="最近一次往返耗时")


class McpReloadRequest(StrictModel):
    """``POST /mcp/servers/{name}/reload`` 请求体。"""

    force: bool = Field(
        default=False,
        description="true 时忽略「正在连接」的幂等判断，强制重建",
    )


__all__ = [
    "McpReloadRequest",
    "McpServerStatusItem",
    "McpServerStatusName",
    "McpTransportName",
]
