"""MCP 客户端模块：用 MCP Python SDK 连接外部 MCP Server（``docs/05``）。

.. note::
   本包名为 ``app.mcp``，与站点包 ``mcp`` 并不冲突 —— Python 3 默认使用绝对导入，
   ``import mcp`` 依然指向已安装的 SDK；本包只能通过 ``app.mcp`` 访问。

分层：

* :mod:`app.mcp.config` —— 配置 Schema 与启动期校验（``REQ-MCP-001``）
* :mod:`app.mcp.session` —— 传输层（stdio / streamable_http）与结果归一化
* :mod:`app.mcp.client` —— 单个 Server 的连接生命周期（``REQ-MCP-002`` / ``004``）
* :mod:`app.mcp.manager` —— 多 Server 编排、健康明细（``REQ-MCP-005``）
* :mod:`app.mcp.tools` —— 适配成 :class:`~app.tools.base.Tool`（``REQ-MCP-003``）

:func:`build_mcp_manager` 是装配入口：从 ``Settings`` 构造管理器。**它不做任何 I/O**，
建连在 ``lifespan`` 里 ``await manager.startup()`` —— 这样 ``create_app`` 保持同步、
可被测试反复调用而不产生子进程。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp.client import McpClient, McpServerStatus, McpStatus
from app.mcp.config import (
    McpConfigError,
    McpServerConfig,
    parse_server_config,
    parse_servers,
)
from app.mcp.manager import McpManager, make_mcp_check
from app.mcp.session import McpCallResult, McpToolDef, open_session
from app.mcp.tools import McpTool, build_mcp_tools, sync_mcp_tools


async def list_tools_async(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[Any]:
    """以 stdio 方式启动一个 MCP Server，并列出它暴露的工具。

    这是**排障用**的最小实现（不经过管理器）：写配置文件之前先用它确认
    「命令能起来、工具名是什么」——``parse_server_config`` 能保证名字合法，
    但「这个 Server 到底提供了哪些工具」只能连上去看。

    Args:
        command: 可执行文件，例如 ``"uv"``、``"npx"``、``"python"``。
        args: 传给该命令的参数，例如 ``["run", "mcp_server.py"]``。
        env: 额外环境变量。

    Returns:
        ``mcp.types.Tool`` 列表。
    """
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(command=command, args=args or [], env=env)
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        return list(result.tools)


def list_tools(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> list[Any]:
    """``list_tools_async`` 的同步封装，便于脚本中直接调用。"""
    return asyncio.run(list_tools_async(command, args, env))


def build_mcp_manager(settings: Any, *, circuits: Any = None, tracing: Any = None) -> McpManager:
    """从配置构造管理器（**不建连**）。

    Args:
        settings: 应用配置。
        circuits: 熔断器注册表（``None`` 时该层不做熔断）。
        tracing: 追踪门面（``None`` 时不产生 span）。

    Raises:
        McpConfigError: 配置不合法 —— 启动期直接失败（``AC-MCP-04``）。
    """
    servers = parse_servers(getattr(settings, "mcp_servers", None) or {})
    return McpManager(
        servers,
        circuits=circuits,
        tracing=tracing,
        max_result_chars=int(getattr(settings, "tool_result_max_chars", 4000) or 0),
        startup_timeout_seconds=float(
            getattr(settings, "mcp_startup_timeout_seconds", 15.0) or 15.0
        ),
    )


__all__ = [
    "McpCallResult",
    "McpClient",
    "McpConfigError",
    "McpManager",
    "McpServerConfig",
    "McpServerStatus",
    "McpStatus",
    "McpTool",
    "McpToolDef",
    "build_mcp_manager",
    "build_mcp_tools",
    "list_tools",
    "list_tools_async",
    "make_mcp_check",
    "open_session",
    "parse_server_config",
    "parse_servers",
    "sync_mcp_tools",
]
