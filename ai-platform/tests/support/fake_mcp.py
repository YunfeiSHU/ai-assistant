"""MCP 测试替身：一个可脚本化的「MCP Server」。

契约测试与单测都需要「能被精确操控的 MCP Server」：什么时候连上、什么时候超时、
``tools/call`` 返回 ``isError`` 还是正常内容。真起子进程（``npx`` / Python stdio）
会把测试变慢、变成环境相关，所以这里用 :class:`FakeMcpServer` 提供
:data:`~app.mcp.session.SessionFactory` 需要的那个「异步上下文管理器」。

用法::

    server = FakeMcpServer(tools=[McpToolDef(name="echo", ...)])
    manager = McpManager(configs, session_factory=server.factory)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.mcp.config import McpServerConfig
from app.mcp.session import McpCallResult, McpToolDef


@dataclass
class FakeToolCall:
    """一次被记录下来的 ``tools/call``。"""

    name: str
    arguments: dict[str, Any]


@dataclass
class FakeMcpServer:
    """脚本化的 MCP Server 替身。

    Attributes:
        tools: ``tools/list`` 返回的工具。
        result: ``tools/call`` 的返回内容。
        connect_delay: 建连耗时（用来触发启动总超时）。
        fail_connect: 建连时抛异常（模拟 Server 没起来 / 连接已断）。
        connect_error: ``fail_connect`` 时抛出的消息。
        fail_calls: 每次 ``tools/call`` 都抛异常（模拟调用途中连接断掉）。
        fail_first_call: 只有第一次 ``tools/call`` 抛异常（模拟可自愈的断连）。
        call_delay: 单次 ``tools/call`` 耗时（用来触发调用超时）。
        connects: 建连次数（观察重连行为）。
        closes: 连接被关闭的次数（观察子进程有没有被回收）。
        calls: 收到的 ``tools/call``（观察工具名与参数）。
        configs: 每次建连时拿到的配置。
        child_envs: 每次建连时算出的子进程环境变量。
    """

    tools: list[McpToolDef] = field(default_factory=list)
    result: McpCallResult = field(default_factory=lambda: McpCallResult(text="ok"))
    connect_delay: float = 0.0
    fail_connect: bool = False
    connect_error: str = "connection refused"
    fail_calls: bool = False
    fail_first_call: bool = False
    call_delay: float = 0.0
    list_delay: float = 0.0

    connects: int = 0
    closes: int = 0
    calls: list[FakeToolCall] = field(default_factory=list)
    configs: list[McpServerConfig] = field(default_factory=list)
    child_envs: list[dict[str, str]] = field(default_factory=list)

    @property
    def call_names(self) -> list[str]:
        """被调用过的工具名列表。"""
        return [call.name for call in self.calls]

    def factory(self, config: McpServerConfig) -> Any:
        """返回一个可当作 ``SessionFactory`` 用的对象（异步上下文管理器）。"""
        return _session(self, config)


class _FakeSession:
    """已握手的连接（实现 ``McpTransportSession``）。"""

    def __init__(self, server: FakeMcpServer) -> None:
        self._server = server

    async def list_tools(self) -> list[McpToolDef]:
        if self._server.list_delay:
            await asyncio.sleep(self._server.list_delay)
        return list(self._server.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpCallResult:
        server = self._server
        if server.call_delay:
            await asyncio.sleep(server.call_delay)
        if server.fail_calls or (server.fail_first_call and not server.calls):
            server.calls.append(FakeToolCall(name, dict(arguments)))
            raise RuntimeError("stream closed")
        server.calls.append(FakeToolCall(name, dict(arguments)))
        return server.result


@asynccontextmanager
async def _session(server: FakeMcpServer, config: McpServerConfig) -> AsyncIterator[_FakeSession]:
    """建连 → 交出会话 → 关闭（与真实的 ``open_session`` 形状一致）。"""
    from app.mcp.session import child_env

    server.configs.append(config)
    server.child_envs.append(child_env(config))
    if server.connect_delay:
        await asyncio.sleep(server.connect_delay)
    if server.fail_connect:
        raise RuntimeError(server.connect_error)
    server.connects += 1
    try:
        yield _FakeSession(server)
    finally:
        server.closes += 1


__all__ = ["FakeMcpServer", "FakeToolCall"]
