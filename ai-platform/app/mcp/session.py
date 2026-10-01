"""MCP 传输层：把 stdio / streamable_http 两种连接方式收敛成同一个接口。

``docs/05`` §2.1 的两种 transport 在 SDK 里的用法差别很大（一个要管理子进程，一个要管理 HTTP
会话），但上层（客户端 / 管理器 / 工具适配）看到的应当是同一件事：「能列工具、能调工具、能关掉」。
所以这里只暴露 :class:`McpTransportSession`。

两个安全要求落在本模块：

1. **子进程环境变量按白名单传递**（``docs/05`` §6）：直接把 ``os.environ`` 交给 MCP Server，
   等于把 ``OPENAI_API_KEY`` / ``JWT_SECRET`` 送给一个第三方进程。
2. **结果归一化 + 截断**（``docs/05`` §4）：``content[]`` 拼成文本，超过
   ``tool_result_max_chars`` 截断。必须做在这里而不是工具层 —— 工具层拿到的是已经展开的
   字符串，再截断就会把结构化信息丢掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.mcp.config import McpServerConfig

logger = get_logger("app.mcp")

#: 允许传给 MCP 子进程的父进程环境变量白名单（``docs/05`` §6）
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PYTHONPATH",
    "UV_CACHE_DIR",
    "NODE_PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "HF_ENDPOINT",
)

#: MCP Server 返回的内容块类型里，直接取文本的
_TEXT_CONTENT = "text"


@dataclass(frozen=True, slots=True)
class McpToolDef:
    """一个 MCP 工具的定义（归一化后的形态）。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpCallResult:
    """一次 ``tools/call`` 的归一化结果。"""

    text: str = ""
    is_error: bool = False


@runtime_checkable
class McpTransportSession(Protocol):
    """一次已握手的连接。"""

    async def list_tools(self) -> list[McpToolDef]:
        """列出该 Server 暴露的工具。"""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        """调用工具。"""
        ...


class McpSessionError(RuntimeError):
    """连接或握手失败（由客户端映射成 ``MCP_SERVER_UNAVAILABLE``）。"""


#: 会话工厂：接受配置并返回一个「异步上下文管理器 → 已握手会话」
SessionFactory = Callable[[McpServerConfig], AbstractAsyncContextManager[McpTransportSession]]


def child_env(config: McpServerConfig) -> dict[str, str]:
    """构造子进程环境变量：父进程白名单 + 配置显式追加（``docs/05`` §6）。"""
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.update(config.env)
    return env


# ----------------------------------------------------------------------
# 归一化
# ----------------------------------------------------------------------
def normalize_tools(result: Any) -> list[McpToolDef]:
    """把 SDK 的 ``ListToolsResult``（或任意同形对象）转成 :class:`McpToolDef` 列表。"""
    tools: list[McpToolDef] = []
    for item in getattr(result, "tools", None) or ():
        name = str(getattr(item, "name", "") or "")
        if not name:
            continue
        schema = getattr(item, "inputSchema", None) or getattr(item, "input_schema", None) or {}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            # 缺失或非对象 schema：退化成「任意对象」并记警告（docs/05 §4）
            logger.warning(
                "mcp.tool_schema_fallback",
                extra={"tool": name, "schema_type": type(schema).__name__},
            )
            schema = {"type": "object", "additionalProperties": True}
        tools.append(
            McpToolDef(
                name=name,
                description=str(getattr(item, "description", "") or ""),
                input_schema=schema,
            )
        )
    return tools


def normalize_call_result(result: Any, *, max_chars: int = 4000) -> McpCallResult:
    """把 SDK 的 ``CallToolResult`` 转成 :class:`McpCallResult`（拼接 + 截断）。

    ``isError=true`` 时返回 ``is_error=True``，由适配层决定映射成哪个错误码
    （``docs/05`` §4：``isError=true`` → ``status="error"``）。
    """
    blocks = getattr(result, "content", None) or ()
    parts: list[str] = []
    for block in blocks:
        block_type = getattr(block, "type", None)
        if block_type == _TEXT_CONTENT or (block_type is None and hasattr(block, "text")):
            parts.append(str(getattr(block, "text", "")))
        else:
            parts.append(_dump_block(block))
    text = "\n".join(part for part in parts if part)
    is_error = bool(getattr(result, "isError", None) or getattr(result, "is_error", False))
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + "…[truncated]"
    return McpCallResult(text=text, is_error=is_error)


def _dump_block(block: Any) -> str:
    """非文本内容块：尽量序列化成 JSON，失败则退化成 ``str``。"""
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        with contextlib.suppress(Exception):
            return json.dumps(dump(), ensure_ascii=False, default=str)
    return json.dumps(block, ensure_ascii=False, default=str)


# ----------------------------------------------------------------------
# 真实 transport
# ----------------------------------------------------------------------
@asynccontextmanager
async def open_session(config: McpServerConfig) -> AsyncIterator[McpTransportSession]:
    """按配置建连并握手；退出时关闭连接（stdio 会回收子进程）。"""
    if config.transport == "stdio":
        async with _stdio_session(config) as session:
            yield session
    else:
        async with _http_session(config) as session:
            yield session


@asynccontextmanager
async def _stdio_session(config: McpServerConfig) -> AsyncIterator[McpTransportSession]:
    """stdio 传输：启动子进程作为 MCP Server。"""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=child_env(config),
    )
    # 合并写法 ``async with A as a, B as b:`` 与嵌套写法完全等价（退出顺序相反），
    # 这里用合并形式只是为了少一层缩进。
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await asyncio.wait_for(session.initialize(), timeout=config.connect_timeout_seconds)
        yield _SdkSession(session)


@asynccontextmanager
async def _http_session(config: McpServerConfig) -> AsyncIterator[McpTransportSession]:
    """streamable_http 传输：连接远端 Server。

    SDK 的签名是 ``streamable_http_client(url, *, http_client=None, ...)``：headers 与超时
    只能通过自建的 HTTP 客户端传入（MCP 2.2.0 没有 ``headers=``）。所以这里显式构造
    ``httpx2.AsyncClient`` —— 顺手把「握手超时」与「单次调用超时」分开：客户端超时取
    ``timeout_seconds``，``initialize`` 另受 ``connect_timeout_seconds`` 约束。
    """
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    from mcp import ClientSession

    client_timeout = max(config.timeout_seconds, config.connect_timeout_seconds)
    async with (
        httpx2.AsyncClient(
            headers=dict(config.headers) if config.headers else None,
            timeout=client_timeout,
        ) as http_client,
        streamable_http_client(config.url, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await asyncio.wait_for(session.initialize(), timeout=config.connect_timeout_seconds)
        yield _SdkSession(session)


class _SdkSession:
    """把 MCP SDK 的 ``ClientSession`` 适配成 :class:`McpTransportSession`。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_tools(self) -> list[McpToolDef]:
        return normalize_tools(await self._session.list_tools())

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        result = await self._session.call_tool(name, arguments)
        return normalize_call_result(result)


__all__ = [
    "ENV_ALLOWLIST",
    "McpCallResult",
    "McpSessionError",
    "McpToolDef",
    "McpTransportSession",
    "SessionFactory",
    "child_env",
    "normalize_call_result",
    "normalize_tools",
    "open_session",
]
