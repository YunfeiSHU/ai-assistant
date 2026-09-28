"""MCP 工具适配器与传输层归一化单测（``REQ-MCP-003``，契约见 ``docs/05`` §4）。

适配器的价值在于「**MCP 工具与内置工具在 Agent 眼里没有区别**」，所以这里断言的是
三件事：

1. **命名空间**：``mcp__{server}__{tool}``，两个 Server 的同名工具不会撞车；
2. **失败不抛异常**：``invoke`` 的任何失败都要变成 ``ToolOutcome(status="error")``
   回注给模型（工具边界必须闭合，否则一次 MCP 故障会变成 500）；
3. **写操作留审计**：``write_tools`` 里的工具要写一条含**哈希后** ``user_id``
   与**仅键名**的参数的审计日志（明文用户 id 与参数内容都不得落盘）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from tests.support.fake_mcp import FakeMcpServer

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.llm.base import LLMToolCall
from app.mcp.client import McpClient
from app.mcp.config import parse_server_config
from app.mcp.manager import McpManager
from app.mcp.session import (
    ENV_ALLOWLIST,
    McpCallResult,
    McpToolDef,
    child_env,
    normalize_call_result,
    normalize_tools,
)
from app.mcp.tools import McpTool, build_mcp_tools, sync_mcp_tools
from app.observability.metrics import Metrics, configure_metrics, get_metrics
from app.tools import ToolRegistry
from app.tools.base import ToolArgumentError, ToolContext
from app.tools.executor import ToolExecutor

READ_TOOL = McpToolDef(
    name="read_file",
    description="读文件",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
WRITE_TOOL = McpToolDef(name="write_file", description="写文件")
CTX = ToolContext(user_id="u_alice", conversation_id="conv_1")


def _client(server: FakeMcpServer, **overrides: object) -> McpClient:
    """构造一个指向假 Server 的客户端。"""
    data: dict[str, object] = {"command": "fake"}
    data.update(overrides)
    return McpClient("fs", parse_server_config("fs", data), session_factory=server.factory)


# ----------------------------------------------------------------------
# 传输层归一化
# ----------------------------------------------------------------------
class _Block:
    """最小内容块替身。"""

    def __init__(self, **fields: object) -> None:
        self.__dict__.update(fields)


class _ModelBlock:
    """带 ``model_dump`` 的内容块（对齐 SDK 的 pydantic 模型）。"""

    def __init__(self, **fields: object) -> None:
        self._fields = fields
        self.type = fields.get("type")

    def model_dump(self) -> dict[str, object]:
        return dict(self._fields)


class _SdkTool:
    """最小工具定义替身（形状对齐 SDK 的 ``Tool``）。"""

    def __init__(self, name: str, description: str = "", schema: object = None) -> None:
        self.name = name
        self.description = description
        if schema is not None:
            self.inputSchema = schema


def test_normalize_tools_reads_sdk_shape() -> None:
    """``inputSchema``（SDK 的驼峰）能被读到。"""
    result = _Block(
        tools=[_SdkTool("a", "A", {"type": "object", "properties": {}}), _SdkTool("b", "B", {})]
    )

    tools = normalize_tools(result)

    assert [t.name for t in tools] == ["a", "b"]
    assert tools[0].input_schema == {"type": "object", "properties": {}}


def test_normalize_tools_accepts_snake_case_schema() -> None:
    """有些实现给 ``input_schema``：两种形状都要认。"""
    tool = _SdkTool("a")
    tool.input_schema = {"type": "object", "properties": {}}  # type: ignore[attr-defined]

    tools = normalize_tools(_Block(tools=[tool]))

    assert tools[0].input_schema == {"type": "object", "properties": {}}


def test_normalize_tools_falls_back_for_missing_or_non_object_schema() -> None:
    """缺失/非对象 schema 退化成「任意对象」，而不是让注册表校验失败。

    第三方 Server 给脏 schema 是常态；直接抛错会让**整个应用启动失败**，
    而责任完全不在我们这边。
    """
    tools = normalize_tools(_Block(tools=[_SdkTool("a"), _SdkTool("b", schema={"type": "string"})]))

    for tool in tools:
        assert tool.input_schema == {"type": "object", "additionalProperties": True}


def test_normalize_tools_skips_nameless_entries() -> None:
    """没有名字的条目直接丢掉（没法注册）。"""
    tools = normalize_tools(_Block(tools=[_SdkTool(""), _SdkTool("ok")]))

    assert [t.name for t in tools] == ["ok"]


def test_normalize_tools_handles_empty_result() -> None:
    """``tools=None`` / 空列表都是合法的「没有工具」。"""
    assert normalize_tools(_Block(tools=None)) == []
    assert normalize_tools(_Block()) == []


def test_normalize_call_result_joins_text_blocks() -> None:
    """多个文本块按换行拼接。"""
    result = _Block(content=[_Block(type="text", text="a"), _Block(type="text", text="b")])

    normalized = normalize_call_result(result)

    assert normalized.text == "a\nb"
    assert not normalized.is_error


def test_normalize_call_result_dumps_structured_blocks() -> None:
    """非文本块序列化成 JSON —— 丢掉结构化信息会让人看不懂结果。"""
    result = _Block(content=[_ModelBlock(type="image", data="x")])

    normalized = normalize_call_result(result)

    assert "image" in normalized.text
    assert "x" in normalized.text


def test_normalize_call_result_degrades_for_unserializable_blocks() -> None:
    """连 ``model_dump`` 都没有的块退化成 ``str``，而不是抛异常。"""
    result = _Block(content=[_Block(type="weird")])

    normalized = normalize_call_result(result)

    assert normalized.text


def test_normalize_call_result_reads_is_error() -> None:
    """``isError`` 与 ``is_error`` 两种拼写都要认。"""
    assert normalize_call_result(_Block(content=[], isError=True)).is_error
    assert normalize_call_result(_Block(content=[], is_error=True)).is_error


def test_normalize_call_result_truncates() -> None:
    """超长结果截断并带标记（``…[truncated]``）。"""
    result = _Block(content=[_Block(type="text", text="x" * 50)])

    normalized = normalize_call_result(result, max_chars=10)

    assert normalized.text == "x" * 10 + "…[truncated]"


def test_normalize_call_result_keeps_short_text() -> None:
    """没超限就不动它。"""
    assert (
        normalize_call_result(
            _Block(content=[_Block(type="text", text="short")]), max_chars=10
        ).text
        == "short"
    )


# ----------------------------------------------------------------------
# 子进程环境变量
# ----------------------------------------------------------------------
def test_child_env_only_passes_allowlisted_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父进程的密钥**不能**进子进程：否则等于把 ``OPENAI_API_KEY`` 送给第三方。

    这条断言是 ``docs/05`` §6 的核心安全要求，也是「顺手 ``env=os.environ``」
    最容易踩的坑 —— 那样写一切照常工作，只是把密钥泄出去了。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("JWT_SECRET", "jwt-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = child_env(parse_server_config("fs", {"command": "fake"}))

    assert "OPENAI_API_KEY" not in env
    assert "JWT_SECRET" not in env
    assert env["PATH"] == "/usr/bin"


def test_child_env_adds_configured_variables() -> None:
    """配置里显式声明的变量要传给子进程（这是唯一受控的注入通道）。"""
    config = parse_server_config("fs", {"command": "fake", "env": {"MCP_TOKEN": "t"}})

    env = child_env(config)

    assert env["MCP_TOKEN"] == "t"


def test_child_env_does_not_pass_unknown_host_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单之外的宿主变量不进子进程，哪怕它名字看着无害。"""
    monkeypatch.setenv("SOME_UNLISTED_VAR", "x")

    env = child_env(parse_server_config("fs", {"command": "fake"}))

    assert "SOME_UNLISTED_VAR" not in env
    assert "SOME_UNLISTED_VAR" not in ENV_ALLOWLIST


# ----------------------------------------------------------------------
# 适配器：声明
# ----------------------------------------------------------------------
def test_build_uses_namespaced_name() -> None:
    """注册名是 ``mcp__{server}__{tool}``。"""
    tool = McpTool.build(_client(FakeMcpServer()), READ_TOOL)

    assert tool.name == "mcp__fs__read_file"


async def test_spec_maps_documented_fields() -> None:
    """``ToolSpec`` 的字段映射按 ``docs/05`` §4 的表。"""
    server = FakeMcpServer(tools=[READ_TOOL])
    client = _client(server, timeout_seconds=12)
    await client.connect()

    spec = build_mcp_tools(client)[0].spec

    assert spec.name == "mcp__fs__read_file"
    assert spec.description == "读文件"
    assert spec.source == "mcp"
    assert spec.mcp_server == "fs"
    assert spec.side_effect == "read"
    assert spec.timeout_seconds == 12
    assert spec.enabled is True
    assert spec.parameters == READ_TOOL.input_schema


async def test_spec_description_falls_back_when_upstream_is_silent() -> None:
    """上游没给描述时补一个可读的占位 —— 空描述会让模型无从选择。"""
    server = FakeMcpServer(tools=[McpToolDef(name="noise", description="   ")])
    client = _client(server)
    await client.connect()

    spec = build_mcp_tools(client)[0].spec

    assert spec.description
    assert "noise" in spec.description


async def test_spec_schema_falls_back_when_missing() -> None:
    """没有 schema 时用「任意对象」，否则注册表校验会拒绝。"""
    server = FakeMcpServer(tools=[McpToolDef(name="loose")])
    client = _client(server)
    await client.connect()

    spec = build_mcp_tools(client)[0].spec

    assert spec.parameters == {"type": "object", "additionalProperties": True}


async def test_write_tools_become_write_side_effects() -> None:
    """``write_tools`` 里声明的工具 ``side_effect == "write"``。"""
    server = FakeMcpServer(tools=[READ_TOOL, WRITE_TOOL])
    client = _client(server, write_tools=["write_file"])
    await client.connect()

    by_name = {tool.spec.name: tool for tool in build_mcp_tools(client)}

    assert by_name["mcp__fs__read_file"].side_effect == "read"
    assert by_name["mcp__fs__write_file"].side_effect == "write"


async def test_disabled_server_marks_tools_disabled() -> None:
    """Server 被禁用时工具对外不可见。"""
    client = _client(FakeMcpServer(tools=[READ_TOOL]), enabled=False)

    assert build_mcp_tools(client) == []


# ----------------------------------------------------------------------
# 适配器：校验与调用
# ----------------------------------------------------------------------
def test_validate_rejects_non_object_arguments() -> None:
    """``arguments`` 必须是 JSON 对象。"""
    tool = McpTool.build(_client(FakeMcpServer()), READ_TOOL)

    with pytest.raises(ToolArgumentError, match="JSON 对象"):
        tool.validate(["x"])  # type: ignore[arg-type]


def test_validate_reports_missing_required_fields() -> None:
    """漏传必需参数时提前拦下（这是模型最高频的失误）。"""
    tool = McpTool.build(_client(FakeMcpServer()), READ_TOOL)

    with pytest.raises(ToolArgumentError) as excinfo:
        tool.validate({})

    assert excinfo.value.detail == {"missing": ["path"]}


def test_validate_passes_complete_arguments() -> None:
    """参数齐了就放行，并返回一份副本（调用方改动不影响原对象）。"""
    tool = McpTool.build(_client(FakeMcpServer()), READ_TOOL)
    arguments = {"path": "/tmp/a"}

    validated = tool.validate(arguments)

    assert validated == arguments
    assert validated is not arguments


def test_validate_ignores_non_list_required() -> None:
    """``required`` 不是数组（脏 schema）时不做无意义的检查。"""
    tool = McpTool.build(
        _client(FakeMcpServer()),
        McpToolDef(name="odd", input_schema={"type": "object", "required": "path"}),
    )

    assert tool.validate({"anything": 1}) == {"anything": 1}


async def test_invoke_success_maps_to_ok_outcome() -> None:
    """成功调用 → ``status="ok"``，payload 带文本与服务/工具名。"""
    server = FakeMcpServer(tools=[READ_TOOL], result=McpCallResult(text="file body"))
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client)[0]

    outcome = await tool.invoke({"path": "/tmp/a"}, CTX)

    assert outcome.status == "ok"
    assert outcome.payload["text"] == "file body"
    assert outcome.payload["server"] == "fs"
    assert outcome.payload["tool"] == "read_file"
    assert outcome.summary == "file body"
    assert outcome.elapsed_ms >= 0


async def test_invoke_upstream_error_maps_to_error_outcome() -> None:
    """``isError=true`` → ``upstream_mcp_error``，且**不抛异常**。"""
    server = FakeMcpServer(
        tools=[READ_TOOL], result=McpCallResult(text="no such file", is_error=True)
    )
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client)[0]

    outcome = await tool.invoke({"path": "/tmp/a"}, CTX)

    assert outcome.status == "error"
    assert outcome.payload["error"] == "upstream_mcp_error"
    assert outcome.payload["detail"]["text"] == "no such file"


async def test_invoke_server_unavailable_maps_to_execution_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连不上（``AppError``）→ ``execution_failed`` 并带上错误码。

    工具失败的正常出口是**回注给模型**（让它换个工具或换个问法），
    而不是把整次请求变成 500。
    """
    server = FakeMcpServer(tools=[READ_TOOL])
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client)[0]

    async def down(*args: object, **kwargs: object) -> None:
        raise AppError(ErrorCode.MCP_SERVER_UNAVAILABLE, "MCP Server 不可用：fs", {"server": "fs"})

    monkeypatch.setattr(client, "call_tool", down)

    outcome = await tool.invoke({"path": "/tmp/a"}, CTX)

    assert outcome.status == "error"
    assert outcome.payload["error"] == "execution_failed"
    assert outcome.payload["detail"]["code"] == str(ErrorCode.MCP_SERVER_UNAVAILABLE)
    assert outcome.summary


async def test_invoke_never_raises_on_adapter_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """适配器自身的 bug 也不能冒泡：工具边界必须闭合。"""
    server = FakeMcpServer(tools=[READ_TOOL])
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client)[0]

    async def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("adapter bug")

    monkeypatch.setattr(client, "call_tool", boom)

    outcome = await tool.invoke({"path": "/tmp/a"}, CTX)

    assert outcome.status == "error"
    assert outcome.payload["error"] == "execution_failed"


async def test_invoke_rejects_invalid_arguments_without_calling_upstream() -> None:
    """参数不合法时**不打上游**（省一次网络往返，也避免上游报更难懂的错）。"""
    server = FakeMcpServer(tools=[READ_TOOL])
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client)[0]

    with pytest.raises(ToolArgumentError):
        await tool.invoke({}, CTX)

    assert server.calls == []


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------
async def test_write_tool_writes_audit_log_with_hashed_user_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """写操作留审计：``user_id`` 哈希、参数只记键名（``docs/05`` §6）。"""
    server = FakeMcpServer(tools=[WRITE_TOOL], result=McpCallResult(text="written"))
    client = _client(server, write_tools=["write_file"])
    await client.connect()
    tool = build_mcp_tools(client, pepper="pepper")[0]

    with caplog.at_level(logging.INFO, logger="app.mcp"):
        await tool.invoke({"path": "/tmp/secret.txt", "content": "机密"}, CTX)

    audit = next(r for r in caplog.records if r.message == "mcp.audit")
    assert audit.server == "fs"
    assert audit.tool == "mcp__fs__write_file"
    assert audit.user_id != "u_alice"  # 哈希后落日志
    assert audit.argument_keys == ["content", "path"]
    assert "机密" not in str(audit.__dict__)


async def test_read_tool_writes_no_audit_log(caplog: pytest.LogCaptureFixture) -> None:
    """读操作不写审计（否则日志量会随读请求量线性增长）。"""
    server = FakeMcpServer(tools=[READ_TOOL])
    client = _client(server)
    await client.connect()
    tool = build_mcp_tools(client, pepper="pepper")[0]

    with caplog.at_level(logging.INFO, logger="app.mcp"):
        await tool.invoke({"path": "/tmp/a"}, CTX)

    assert not [r for r in caplog.records if r.message == "mcp.audit"]


# ----------------------------------------------------------------------
# 注册表同步
# ----------------------------------------------------------------------
async def test_sync_registers_tools_into_the_shared_registry() -> None:
    """同步后工具出现在同一个注册表里，``source=mcp`` 可筛选。"""
    server = FakeMcpServer(tools=[READ_TOOL, WRITE_TOOL])
    manager = McpManager(
        {"fs": parse_server_config("fs", {"command": "fake"})}, session_factory=server.factory
    )
    await manager.startup()
    registry = ToolRegistry()

    names = sync_mcp_tools(registry, manager)

    assert names == ["mcp__fs__read_file", "mcp__fs__write_file"]
    assert [spec.name for spec in registry.specs(source="mcp")] == names
    assert registry.specs(source="mcp")[0].mcp_server == "fs"


async def test_sync_is_idempotent_and_drops_removed_tools() -> None:
    """重复同步不会重复注册；工具下线后旧注册项必须被摘掉。

    留着旧注册项的后果是：模型选中一个已经调不通的工具，每次都拿到执行失败，
    而看板上「工具调用失败率」升高却找不到原因。
    """
    server = FakeMcpServer(tools=[READ_TOOL, WRITE_TOOL])
    manager = McpManager(
        {"fs": parse_server_config("fs", {"command": "fake"})}, session_factory=server.factory
    )
    await manager.startup()
    registry = ToolRegistry()

    sync_mcp_tools(registry, manager)
    sync_mcp_tools(registry, manager)
    assert len(registry.names()) == 2

    server.tools = [READ_TOOL]
    await manager.reload("fs")
    names = sync_mcp_tools(registry, manager)

    assert names == ["mcp__fs__read_file"]
    assert len(registry.names()) == 1


async def test_sync_targets_one_server_only() -> None:
    """``server=`` 只同步指定 Server，其余 Server 的注册项不受影响。"""
    server = FakeMcpServer(tools=[READ_TOOL])
    configs = {name: parse_server_config(name, {"command": "fake"}) for name in ("a", "b")}
    manager = McpManager(configs, session_factory=server.factory)
    await manager.startup()
    registry = ToolRegistry()
    sync_mcp_tools(registry, manager)

    names = sync_mcp_tools(registry, manager, server="a")

    assert names == ["mcp__a__read_file"]
    assert len(registry.names()) == 2


async def test_synced_tools_work_through_the_tool_executor(
    make_settings: Callable[..., Settings],
) -> None:
    """真正接入执行器：Agent Loop 走的就是这条路。

    这一步是「MCP 工具与内置工具没有区别」的实际验证 —— 执行器不知道
    这个工具来自 MCP，却照样能调用、照样能记录指标。

    指标部分断言的是「**恰好一次**」：适配器与执行器不能各记一遍，
    否则 ``source="mcp"`` 的计数会静默翻倍（看板上的工具调用量全部偏大一倍）。
    """
    metrics = Metrics()
    previous = get_metrics()
    configure_metrics(metrics)
    try:
        server = FakeMcpServer(tools=[READ_TOOL], result=McpCallResult(text="file body"))
        manager = McpManager(
            {"fs": parse_server_config("fs", {"command": "fake"})}, session_factory=server.factory
        )
        await manager.startup()
        registry = ToolRegistry()
        sync_mcp_tools(registry, manager)
        executor = ToolExecutor(make_settings(), registry)

        record = await executor.execute_one(
            LLMToolCall(
                call_id="call_1", name="mcp__fs__read_file", arguments='{"path": "/tmp/a"}'
            ),
            CTX,
        )
    finally:
        configure_metrics(previous)

    assert record.ok
    assert record.summary == "file body"
    assert record.payload["server"] == "fs"

    sample = 'ai_tool_calls_total{source="mcp",status="ok",tool_name="mcp__fs__read_file"}'
    text = metrics.render()[0].decode("utf-8")
    assert f"{sample} 1.0" in text
    assert f"{sample} 2.0" not in text


async def test_conflict_with_builtin_tool_fails_loudly() -> None:
    """与内置工具重名时注册表抛错 —— 这是编程错误，必须启动期暴露。"""
    from app.tools.registry import ToolRegistrationError

    server = FakeMcpServer(tools=[READ_TOOL])
    manager = McpManager(
        {"fs": parse_server_config("fs", {"command": "fake"})}, session_factory=server.factory
    )
    await manager.startup()
    registry = ToolRegistry()
    sync_mcp_tools(registry, manager)

    with pytest.raises(ToolRegistrationError, match="工具名冲突"):
        registry.register(registry.get("mcp__fs__read_file"))  # type: ignore[arg-type]


async def test_mcp_failure_surfaces_as_tool_error_not_exception(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 故障经执行器后变成一条 ``status="error"`` 的工具结果，而不是异常。

    这正是「工具边界必须闭合」的端到端体现：连不上 MCP 的那一轮对话照常结束，
    模型看到的是「这个工具执行失败」，可以换个工具或换个问法。
    """
    server = FakeMcpServer(tools=[READ_TOOL])
    manager = McpManager(
        {"fs": parse_server_config("fs", {"command": "fake"})}, session_factory=server.factory
    )
    await manager.startup()
    registry = ToolRegistry()
    sync_mcp_tools(registry, manager)
    executor = ToolExecutor(make_settings(), registry)

    async def down(*args: object, **kwargs: object) -> None:
        raise AppError(ErrorCode.MCP_SERVER_UNAVAILABLE, "down", {"server": "fs"})

    # 适配器是 ``slots`` 数据类，实例属性不可改；改它依赖的客户端即可 ——
    # 这恰好也证明了适配器只通过 ``client.call_tool`` 与外界交互。
    monkeypatch.setattr(manager.get("fs"), "call_tool", down)

    record = await executor.execute_one(
        LLMToolCall(call_id="call_1", name="mcp__fs__read_file", arguments='{"path": "/tmp/a"}'),
        CTX,
    )

    assert not record.ok
    assert record.status == "error"
    assert record.payload["error"] == "execution_failed"


def test_app_error_status_codes_for_mcp() -> None:
    """MCP 相关错误码的 HTTP 状态与 ``docs/05`` §5 一致。"""
    assert AppError(ErrorCode.MCP_SERVER_NOT_FOUND).status_code == 404
    assert AppError(ErrorCode.MCP_SERVER_UNAVAILABLE).status_code == 503
    assert not AppError(ErrorCode.MCP_SERVER_NOT_FOUND).retryable
    assert AppError(ErrorCode.MCP_SERVER_UNAVAILABLE).retryable
