"""MCP 客户端与管理器单测（``REQ-MCP-002`` ~ ``REQ-MCP-005``，契约见 ``docs/05`` §3）。

四条验收标准直接对应四组用例：

* ``AC-MCP-01`` —— 非 ``required`` 的 Server 连不上时**服务照常启动**，
  该 Server 状态为 ``unavailable``；
* ``AC-MCP-02`` —— ``required=true`` 的 Server 连不上时**启动失败**，且错误里点名；
* ``AC-MCP-05`` —— 重载单个 Server 不影响其它 Server；
* ``AC-MCP-06`` —— ``/health/ready`` 的 ``mcp`` 明细里能看到每个 Server 的状态。

另外两条回归性质的断言：

* 建连必须有**总超时**（``MCP_STARTUP_TIMEOUT``）。N 个 Server 各等 10s 会让启动
  时间随数量线性增长，而 uvicorn 在 ``lifespan`` 返回前不接受任何请求。
* 被外层超时取消的客户端必须被显式标成 ``unavailable``。``connect()`` 的取消点
  可能在任意 ``await`` 上，客户端自己没机会改状态 —— 状态若停在 ``connecting``，
  健康检查会**永久 503**。
"""

from __future__ import annotations

import asyncio

import pytest
from tests.support.fake_mcp import FakeMcpServer

from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.observability.circuit import CircuitRegistry
from app.mcp import client as client_module
from app.mcp.client import RECONNECT_DELAYS, McpClient
from app.mcp.config import McpServerConfig, parse_server_config
from app.mcp.manager import McpManager, make_mcp_check
from app.mcp.session import McpCallResult, McpToolDef

ECHO = McpToolDef(
    name="echo",
    description="回显",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
)
WRITE = McpToolDef(name="write.file", description="写文件")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把懒重连的退避序列压成「一次零延迟尝试」。

    真实退避是 0.5s + 1.0s，每条涉及重连的用例会凭空慢 1.5s；而这里要断言的
    是「重连了没有」，不是「等了多久」。保留**一次**尝试的语义不能省：
    ``_reconnect`` 是「任一次成功即返回 True」，压成空序列会把它变成「永不重连」，
    那是另一种行为。
    """
    monkeypatch.setattr(client_module, "RECONNECT_DELAYS", (0.0,))


class _Clock:
    """可手动推进的单调时钟（用于跨过熔断的恢复窗口）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config(**overrides: object) -> McpServerConfig:
    """一个默认指向假 Server 的配置。"""
    data: dict[str, object] = {"command": "fake"}
    data.update(overrides)
    return parse_server_config("fs", data)


def _manager(server: FakeMcpServer, **overrides: object) -> McpManager:
    """单 Server 的管理器（全部连接走假 Server）。"""
    return McpManager(
        {"fs": _config(**overrides)},
        session_factory=server.factory,
        startup_timeout_seconds=5.0,
    )


# ----------------------------------------------------------------------
# 建连
# ----------------------------------------------------------------------
async def test_connect_success_sets_state_and_tools() -> None:
    """连上后状态为 ``connected``，工具列表可用，并记录了延迟。"""
    server = FakeMcpServer(tools=[ECHO, WRITE])
    client = McpClient("fs", _config(), session_factory=server.factory)

    assert await client.connect() is True

    snapshot = client.snapshot()
    assert snapshot.status == "connected"
    assert snapshot.tools_count == 2
    assert snapshot.latency_ms is not None
    assert snapshot.last_error is None
    assert snapshot.last_connected_at is not None


async def test_connect_failure_never_raises_and_records_error() -> None:
    """建连失败**不抛异常**：调用方要的是「成功与否 + 状态」，不是堆栈。"""
    server = FakeMcpServer(fail_connect=True, connect_error="spawn failed")
    client = McpClient("fs", _config(), session_factory=server.factory)

    assert await client.connect() is False

    snapshot = client.snapshot()
    assert snapshot.status == "unavailable"
    assert snapshot.last_error == "spawn failed"


async def test_disabled_server_stays_disabled() -> None:
    """``enabled=false`` 时既不建连也不报错，状态固定为 ``disabled``。"""
    server = FakeMcpServer(tools=[ECHO])
    client = McpClient("fs", _config(enabled=False), session_factory=server.factory)

    assert await client.connect() is False
    assert client.status == "disabled"
    assert server.connects == 0


async def test_close_releases_the_transport() -> None:
    """``close()`` 必须走上下文退出（真实 stdio 下这才回收子进程）。"""
    server = FakeMcpServer(tools=[ECHO])
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()

    await client.close()

    assert server.closes == 1
    assert client.snapshot().status == "unavailable"


async def test_mark_unavailable_overrides_connecting() -> None:
    """外层超时后由管理器显式落状态：``connecting`` 对就绪探针是「未就绪」。"""
    client = McpClient("fs", _config(), session_factory=FakeMcpServer().factory)
    client._set_state("connecting")

    client.mark_unavailable("startup timeout")

    assert client.status == "unavailable"
    assert client.snapshot().last_error == "startup timeout"


async def test_allowlist_filters_tools() -> None:
    """``tools_allowlist`` 在 ``tools_count`` 与 ``select_tools`` 上都生效。"""
    server = FakeMcpServer(tools=[ECHO, WRITE])
    client = McpClient("fs", _config(tools_allowlist=["echo"]), session_factory=server.factory)
    await client.connect()

    assert [tool.name for tool in client.tools] == ["echo", "write.file"]
    assert [tool.name for tool in client.select_tools()] == ["echo"]
    assert client.snapshot().tools_count == 1


async def test_tools_list_is_a_copy() -> None:
    """``tools`` 返回副本：调用方改动不会污染客户端内部状态。"""
    server = FakeMcpServer(tools=[ECHO])
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()

    client.tools.clear()

    assert len(client.tools) == 1


# ----------------------------------------------------------------------
# 调用与重连
# ----------------------------------------------------------------------
async def test_call_tool_returns_normalized_result() -> None:
    """调用成功后返回文本结果，并更新延迟。"""
    server = FakeMcpServer(tools=[ECHO], result=McpCallResult(text="hello"))
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()

    result = await client.call_tool("echo", {"text": "hi"})

    assert result.text == "hello"
    assert not result.is_error
    assert server.calls[0].name == "echo"
    assert server.calls[0].arguments == {"text": "hi"}


async def test_call_tool_truncates_long_result() -> None:
    """超过 ``max_result_chars`` 时截断并带标记（模型据此知道还有更多）。"""
    server = FakeMcpServer(tools=[ECHO], result=McpCallResult(text="x" * 100))
    client = McpClient("fs", _config(), session_factory=server.factory, max_result_chars=10)
    await client.connect()

    result = await client.call_tool("echo", {})

    assert result.text == "x" * 10 + "…[truncated]"


async def test_reconnect_before_first_call() -> None:
    """会话还没建立时先重连再调用（懒连接）。"""
    server = FakeMcpServer(tools=[ECHO], result=McpCallResult(text="ok"))
    client = McpClient("fs", _config(), session_factory=server.factory)
    # 刻意不调 connect()，模拟「刚刚启动完成但还没建连」

    result = await client.call_tool("echo", {})

    assert result.text == "ok"
    assert client.status == "connected"


async def test_call_retries_once_after_connection_drop() -> None:
    """调用中途连接断掉 → 重连一次再试（对用户透明）。"""
    server = FakeMcpServer(
        tools=[ECHO], result=McpCallResult(text="recovered"), fail_first_call=True
    )
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()

    result = await client.call_tool("echo", {})

    assert result.text == "recovered"
    assert server.connects == 2  # 初次 + 重连


async def test_call_fails_when_reconnect_fails() -> None:
    """重连也失败 → ``503 MCP_SERVER_UNAVAILABLE``（含 Server 名）。"""
    server = FakeMcpServer(tools=[ECHO], fail_calls=True)
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()
    server.fail_connect = True

    with pytest.raises(AppError) as excinfo:
        await client.call_tool("echo", {})

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE
    assert excinfo.value.details["server"] == "fs"


async def test_call_timeout_maps_to_unavailable() -> None:
    """单次调用超时 → ``503``，而不是无限等待。"""
    server = FakeMcpServer(tools=[ECHO], call_delay=1.0)
    client = McpClient("fs", _config(timeout_seconds=0.05), session_factory=server.factory)
    await client.connect()

    with pytest.raises(AppError) as excinfo:
        await client.call_tool("echo", {})

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE
    assert client.status == "unavailable"


async def test_call_on_disabled_server_raises() -> None:
    """禁用状态下调用直接报错，不打上游。"""
    client = McpClient("fs", _config(enabled=False), session_factory=FakeMcpServer().factory)

    with pytest.raises(AppError) as excinfo:
        await client.call_tool("echo", {})

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE


async def test_tool_error_result_is_returned_not_raised() -> None:
    """``isError=true`` 是「这次调用失败」而不是「连接失败」，不抛异常。

    连接状态也不该被它改掉：一个工具坏掉不代表 Server 掉线。
    """
    server = FakeMcpServer(tools=[ECHO], result=McpCallResult(text="bad input", is_error=True))
    client = McpClient("fs", _config(), session_factory=server.factory)
    await client.connect()

    result = await client.call_tool("echo", {})

    assert result.is_error
    assert client.status == "connected"


# ----------------------------------------------------------------------
# 熔断
# ----------------------------------------------------------------------
async def test_breaker_opens_after_repeated_failures() -> None:
    """连续失败到阈值后熔断打开，后续调用被**本地拒绝**（不打上游）。"""
    server = FakeMcpServer(tools=[ECHO], fail_calls=True)
    circuits = CircuitRegistry()
    breaker = circuits.get("mcp:fs")
    client = McpClient("fs", _config(), session_factory=server.factory, breaker=breaker)
    await client.connect()

    for _ in range(3):
        with pytest.raises(AppError):
            await client.call_tool("echo", {})
    calls_before = len(server.calls)

    with pytest.raises(AppError) as excinfo:
        await client.call_tool("echo", {})

    assert breaker.state == "open"
    assert excinfo.value.details["reason"] == "circuit_open"
    assert excinfo.value.retry_after and excinfo.value.retry_after >= 1
    assert len(server.calls) == calls_before  # 没有打到上游


async def test_breaker_closes_after_success() -> None:
    """成功调用会把失败计数清零，避免偶发失败累积成误熔断。

    这条断言同时是 ``app/mcp/client.py`` 里 ``try/return/else`` 陷阱的回归测试：
    ``else`` 子句在 try 体里出现 ``return`` 时**永不执行**，于是熔断器只记得住失败。
    一旦打开就再也回不到 ``closed``，该 Server 永久不可用。
    """
    server = FakeMcpServer(tools=[ECHO], result=McpCallResult(text="ok"))
    circuits = CircuitRegistry()
    breaker = circuits.get("mcp:fs")
    client = McpClient("fs", _config(), session_factory=server.factory, breaker=breaker)
    await client.connect()

    breaker.record_failure()
    breaker.record_failure()
    await client.call_tool("echo", {})

    assert breaker.state == "closed"
    assert breaker.failure_count == 0


async def test_half_open_success_recovers_the_server() -> None:
    """半开试探成功后熔断闭合 —— 验证「打开 → 半开 → 恢复」整条路径。

    用可推进的假时钟：真实的 ``mcp`` 恢复窗口是 30s，sleep 等它既慢又不稳。
    """
    clock = _Clock()
    server = FakeMcpServer(tools=[ECHO], fail_calls=True)
    circuits = CircuitRegistry(clock=clock)
    breaker = circuits.get("mcp:fs")
    client = McpClient("fs", _config(), session_factory=server.factory, breaker=breaker)
    await client.connect()
    for _ in range(3):
        with pytest.raises(AppError):
            await client.call_tool("echo", {})
    assert breaker.state == "open"

    # 上游恢复 + 恢复窗口过去 → 下一个调用应当被放行并成功，熔断随之处置闭合
    server.fail_calls = False
    clock.advance(31.0)
    await client.call_tool("echo", {})

    assert breaker.state == "closed"


# ----------------------------------------------------------------------
# 重载
# ----------------------------------------------------------------------
async def test_reload_reconnects_and_resets_breaker() -> None:
    """重载会关旧连接、复位熔断、重新建连。"""
    server = FakeMcpServer(tools=[ECHO])
    circuits = CircuitRegistry()
    breaker = circuits.get("mcp:fs")
    client = McpClient("fs", _config(), session_factory=server.factory, breaker=breaker)
    await client.connect()
    breaker.record_failure()

    assert await client.reload() is True

    assert server.connects == 2
    assert breaker.state == "closed"
    assert client.status == "connected"


async def test_reload_is_idempotent_while_connecting() -> None:
    """``force=false`` 且正在建连时直接返回，不做并发重建。"""
    client = McpClient("fs", _config(), session_factory=FakeMcpServer().factory)
    client._set_state("connecting")

    assert await client.reload() is False


async def test_reload_disabled_server_returns_false() -> None:
    """禁用状态下重载是无操作。"""
    client = McpClient("fs", _config(enabled=False), session_factory=FakeMcpServer().factory)

    assert await client.reload() is False


# ----------------------------------------------------------------------
# 管理器：并发启动 + 总超时
# ----------------------------------------------------------------------
async def test_manager_starts_without_servers() -> None:
    """没配 Server 时启动是空操作（不是错误）。"""
    manager = McpManager({})

    await manager.startup()

    assert manager.names == []
    assert manager.is_ready()


async def test_manager_connects_all_servers_concurrently() -> None:
    """并发建连：三个各耗时 0.15s 的 Server 不应串行花 0.45s。

    串行化的代价是真实的：``lifespan`` 返回前 uvicorn 不接受任何请求，
    启动耗时随 Server 数量线性增长。
    """
    servers = {name: FakeMcpServer(tools=[ECHO], connect_delay=0.15) for name in ("a", "b", "c")}
    configs = {name: parse_server_config(name, {"command": name}) for name in servers}
    manager = McpManager(
        configs,
        session_factory=lambda config: servers[config.command].factory(config),
        startup_timeout_seconds=5.0,
    )

    started = asyncio.get_running_loop().time()
    await manager.startup()
    elapsed = asyncio.get_running_loop().time() - started

    assert all(client.status == "connected" for client in manager.clients())
    assert elapsed < 0.45


async def test_non_required_failure_degrades_gracefully() -> None:
    """``AC-MCP-01``：非必需 Server 连不上 → 启动成功，该 Server 为 ``unavailable``。"""
    server = FakeMcpServer(fail_connect=True)
    manager = _manager(server)

    await manager.startup()  # 不抛异常

    snapshot = manager.get("fs").snapshot()
    assert snapshot.status == "unavailable"
    assert snapshot.required is False
    assert manager.is_ready()


async def test_required_failure_blocks_startup() -> None:
    """``AC-MCP-02``：必需 Server 连不上 → 启动失败，且错误里点名是哪个。"""
    server = FakeMcpServer(fail_connect=True, connect_error="no such command")
    manager = _manager(server, required=True)

    with pytest.raises(AppError) as excinfo:
        await manager.startup()

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE
    assert "fs" in excinfo.value.message
    assert excinfo.value.details["servers"][0]["error"] == "no such command"


async def test_total_timeout_marks_hanging_servers_unavailable() -> None:
    """总超时生效：卡在建连的 Server 被取消并落成 ``unavailable``（不会停在 connecting）。"""
    hanging = FakeMcpServer(connect_delay=10.0)
    manager = McpManager(
        {"hang": parse_server_config("hang", {"command": "fake"})},
        session_factory=hanging.factory,
        startup_timeout_seconds=0.05,
    )

    await manager.startup()

    assert manager.get("hang").status == "unavailable"


async def test_total_timeout_still_fails_for_required_server() -> None:
    """超时的 Server 若是 ``required``，启动仍然失败。"""
    hanging = FakeMcpServer(connect_delay=10.0)
    manager = McpManager(
        {"hang": parse_server_config("hang", {"command": "fake", "required": True})},
        session_factory=hanging.factory,
        startup_timeout_seconds=0.05,
    )

    with pytest.raises(AppError, match="hang"):
        await manager.startup()


async def test_partial_success_keeps_the_connected_server() -> None:
    """部分成功是有效结果：断掉的那个降级，连上的那个保持可用。"""
    good = FakeMcpServer(tools=[ECHO])
    bad = FakeMcpServer(fail_connect=True)
    servers = {"good": good, "bad": bad}
    configs = {name: parse_server_config(name, {"command": name}) for name in servers}
    manager = McpManager(
        configs,
        session_factory=lambda config: servers[config.command].factory(config),
        startup_timeout_seconds=5.0,
    )

    await manager.startup()

    assert manager.get("good").status == "connected"
    assert manager.get("bad").status == "unavailable"
    # 只有连上的那个 Server 的工具会被注册出去
    assert [tool.name for _, tool in manager.iter_tools()] == ["echo"]


# ----------------------------------------------------------------------
# 管理器：查询与重载
# ----------------------------------------------------------------------
async def test_get_unknown_server_raises_not_found() -> None:
    """``404 MCP_SERVER_NOT_FOUND``，并在 details 里列出已配置的名字。"""
    manager = McpManager({"fs": _config()}, session_factory=FakeMcpServer().factory)

    with pytest.raises(AppError) as excinfo:
        manager.get("nope")

    assert excinfo.value.code is ErrorCode.MCP_SERVER_NOT_FOUND
    assert excinfo.value.details["configured"] == ["fs"]


async def test_iter_tools_skips_unavailable_servers() -> None:
    """``unavailable`` 的工具定义是上次的残留：注册出去会让模型选中必然失败的工具。"""
    server = FakeMcpServer(tools=[ECHO], fail_connect=True)
    manager = _manager(server)
    await manager.startup()

    assert list(manager.iter_tools()) == []


async def test_iter_tools_yields_filtered_tools() -> None:
    """只产出通过 allowlist 过滤的工具。"""
    server = FakeMcpServer(tools=[ECHO, WRITE])
    manager = _manager(server, tools_allowlist=["echo"])
    await manager.startup()

    assert [tool.name for _, tool in manager.iter_tools()] == ["echo"]


async def test_reload_one_server_does_not_touch_others() -> None:
    """``AC-MCP-05``：重载单个 Server 不影响其它 Server 的连接。"""
    server = FakeMcpServer(tools=[ECHO])
    configs = {
        "one": parse_server_config("one", {"command": "fake"}),
        "two": parse_server_config("two", {"command": "fake"}),
    }
    manager = McpManager(configs, session_factory=server.factory)
    await manager.startup()
    connects_after_startup = server.connects

    snapshot = await manager.reload("one")

    assert snapshot.status == "connected"
    assert server.connects == connects_after_startup + 1


async def test_reload_unknown_server_raises_not_found() -> None:
    """重载一个没配的 Server → 404。"""
    manager = McpManager({"fs": _config()}, session_factory=FakeMcpServer().factory)

    with pytest.raises(AppError) as excinfo:
        await manager.reload("nope")

    assert excinfo.value.code is ErrorCode.MCP_SERVER_NOT_FOUND


async def test_reload_disabled_server_raises_unavailable() -> None:
    """重载被禁用的 Server → 503（不是 404：它确实存在）。"""
    manager = McpManager({"fs": _config(enabled=False)}, session_factory=FakeMcpServer().factory)

    with pytest.raises(AppError) as excinfo:
        await manager.reload("fs")

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE


async def test_reload_failure_raises_unavailable_with_reason() -> None:
    """重载失败（建连不上）→ 503，并把原因带出去。"""
    server = FakeMcpServer(tools=[ECHO])
    manager = _manager(server)
    await manager.startup()
    server.fail_connect = True

    with pytest.raises(AppError) as excinfo:
        await manager.reload("fs")

    assert excinfo.value.code is ErrorCode.MCP_SERVER_UNAVAILABLE
    assert excinfo.value.details["reason"]


async def test_shutdown_closes_every_client() -> None:
    """关停时全部连接被关闭（stdio 子进程不能留成僵尸）。"""
    server = FakeMcpServer(tools=[ECHO])
    manager = _manager(server)
    await manager.startup()

    await manager.shutdown()

    assert server.closes == 1
    assert manager.get("fs").status == "unavailable"


# ----------------------------------------------------------------------
# 健康检查
# ----------------------------------------------------------------------
async def test_health_detail_reports_each_server() -> None:
    """``AC-MCP-06``：``/health/ready`` 的 ``mcp`` 明细含每个 Server 的状态。"""
    server = FakeMcpServer(tools=[ECHO])
    manager = _manager(server)
    await manager.startup()

    detail = manager.health_detail()

    assert detail["available"] == 1
    assert detail["connected"] == 1
    assert detail["required_unavailable"] == []
    assert detail["servers"]["fs"]["status"] == "connected"
    assert detail["servers"]["fs"]["tools_count"] == 1


async def test_health_check_skipped_without_servers() -> None:
    """没配 MCP 时健康检查是 ``skipped`` —— 「没配 MCP」不是故障。"""
    check = make_mcp_check(McpManager({}))

    result = await check()

    assert result.ok
    assert result.skipped


async def test_health_check_ok_when_only_optional_server_is_down() -> None:
    """非必需 Server 掉线是**预期内的降级**：不能把整个服务判成不健康。"""
    server = FakeMcpServer(fail_connect=True)
    manager = _manager(server)
    await manager.startup()
    check = make_mcp_check(manager)

    result = await check()

    assert result.ok
    assert not result.skipped
    assert result.detail["servers"]["fs"]["status"] == "unavailable"


async def test_health_check_fails_for_required_server() -> None:
    """必需 Server 掉线 → 检查失败，错误信息里点名（供运维直接定位）。"""
    manager = _manager(FakeMcpServer(fail_connect=True), required=True)
    with pytest.raises(AppError):
        await manager.startup()
    check = make_mcp_check(manager)

    result = await check()

    assert not result.ok
    assert "fs" in (result.error or "")


def test_reconnect_backoff_sequence_is_bounded() -> None:
    """退避序列是有限的：无限重连会让一次调用挂到超时为止。"""
    assert RECONNECT_DELAYS == (0.5, 1.0)
