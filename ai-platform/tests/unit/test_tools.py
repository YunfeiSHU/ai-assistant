"""工具层的单元测试（``docs/04`` §2 / §3 / §5）。

这一层刻意**不依赖 LLM**：Schema 生成、参数校验、超时、并发/串行、截断、
SSRF 防护都能直接用假工具验证。用假工具而不是三个真内置工具，是为了让每个断言
只考验被测的那一件事——比如「并发」用 sleep 假工具，就与检索器的行为无关。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, Field, ValidationError
from tests.support.fake_llm import tool_call

from app.core.config import Settings
from app.llm.base import LLMToolCall
from app.rag.base import RetrievedChunk
from app.tools.base import (
    BuiltinTool,
    ToolArgumentError,
    ToolContext,
    ToolExecutionError,
    ToolOutcome,
    ToolSpec,
    clip,
    json_schema_of,
    namespace_tool,
    payload_summary,
)
from app.tools.builtin import CalculatorTool, CurrentTimeTool, HttpFetchTool, KbRetrieveTool
from app.tools.builtin.calculator import EXPONENT_MAX, CalculatorArgs, evaluate
from app.tools.builtin.http_fetch import HttpFetchArgs, _assert_public_host
from app.tools.executor import (
    ERROR_DUPLICATE_CALL,
    ERROR_EXECUTION_FAILED,
    ERROR_INVALID_ARGUMENTS,
    ERROR_NOT_ALLOWED,
    ERROR_NOT_FOUND,
    ERROR_TIMEOUT,
    ERROR_WRITE_FORBIDDEN,
    ToolExecutor,
    parse_arguments,
    signature_of,
)
from app.tools.registry import ToolRegistrationError, ToolRegistry
from app.tools.service import ToolService

# ---------------------------------------------------------------------------
# 假工具
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    """最简单的参数模型。"""

    text: str = Field(default="hi", max_length=10)


class _EchoTool(BuiltinTool):
    """回显参数；可配置耗时、超时与副作用。"""

    name = "echo"
    description = "回显传入的文本，用于验证执行器的行为"
    input_model = _EchoArgs
    side_effect = "read"

    def __init__(
        self,
        *,
        name: str = "echo",
        side_effect: str = "read",
        delay: float = 0.0,
        timeout: float = 5.0,
        failure: Exception | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.side_effect = side_effect  # type: ignore[assignment]
        self.timeout_seconds = timeout
        self._delay = delay
        self._failure = failure
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._failure is not None:
            raise self._failure
        args = _EchoArgs.model_validate(arguments)
        return ToolOutcome(payload={"echo": args.text}, summary=f"回显 {args.text}")


class _RawTool:
    """不继承 ``BuiltinTool`` 的工具（验证注册表只依赖 Protocol）。"""

    def __init__(self, spec: ToolSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def enabled(self) -> bool:
        return self._spec.enabled

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return arguments

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        return ToolOutcome(payload={"ok": True})


class _StubRetriever:
    """返回固定片段的检索器替身。"""

    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.calls: list[dict[str, Any]] = []
        self.failure: Exception | None = None

    async def retrieve(self, **kwargs: Any) -> list[RetrievedChunk]:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return list(self.chunks)


def _chunk(chunk_id: str = "chk_1", *, score: float = 0.9, doc: str = "手册.md") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=f"{chunk_id} 的正文",
        doc_id="doc_1",
        kb_id="kb_1",
        doc_name=doc,
        page=3,
        score=score,
        vector_score=score,
        user_id="u_test",
    )


def _ctx(**kwargs: Any) -> ToolContext:
    return ToolContext(user_id="u_test", **kwargs)


# ---------------------------------------------------------------------------
# base：Schema、命名空间、截断
# ---------------------------------------------------------------------------


def test_json_schema_strips_titles() -> None:
    """``title`` 对模型没有信息量却占 token，生成时必须去掉。"""
    schema = json_schema_of(_EchoArgs)
    assert schema["type"] == "object"
    assert "title" not in schema
    assert "title" not in schema["properties"]["text"]
    assert schema["properties"]["text"]["maxLength"] == 10


def test_namespace_tool_replaces_unsafe_characters() -> None:
    """``-`` / ``.`` 不被上游 function name 规则接受，必须替换。"""
    assert namespace_tool("my-server", "read.file") == "mcp__my_server__read_file"
    assert namespace_tool("s", "t") == "mcp__s__t"


def test_clip_leaves_short_text_untouched() -> None:
    """未超上限的文本原样返回：不截断、不加标记（标记会污染本来就正常的结果）。"""
    assert clip("abc", 10) == "abc"


def test_clip_marks_truncation() -> None:
    """截断必须有标记：模型看到半截 JSON 会以为工具返回了坏数据。"""
    result = clip("x" * 20, 5)
    assert result.startswith("x" * 5)
    assert result.endswith("…[truncated]")


def test_payload_summary_is_json_not_empty() -> None:
    """摘要取的是 payload 的 JSON 文本，正文内容必须真实出现在里面（不是空串/占位符）。"""
    assert "hello" in payload_summary({"msg": "hello"})


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_builtin_names() -> None:
    """内置重名是**编程错误**，必须启动失败（``AC-AGENT-07``）。

    静默覆盖的表现是「模型看到的 schema 与实际执行的不是同一个」，运行期无法定位。
    """
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolRegistrationError, match="工具名冲突"):
        registry.register(_EchoTool())


def test_registry_rejects_invalid_name() -> None:
    """不符合工具名规则的注册项（如大写开头）直接抛 ``ToolRegistrationError``。"""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="不合法"):
        registry.register(_RawTool(ToolSpec(name="BadName", description="x", parameters={})))


def test_registry_rename_returns_namespaced_name() -> None:
    """MCP 工具被重命名后，注册名与 ``spec.name`` 一致，调用路径不变。"""
    registry = ToolRegistry()
    tool = _EchoTool()
    final = registry.register(tool, rename="mcp__srv__echo")
    assert final == "mcp__srv__echo"
    assert registry.get("mcp__srv__echo") is not None
    assert registry.get("echo") is None
    assert registry.get("mcp__srv__echo").spec.name == "mcp__srv__echo"  # type: ignore[union-attr]


def test_registry_filter_prefers_denylist() -> None:
    """黑名单优先级高于白名单（``docs/04`` §4.3）。"""
    registry = ToolRegistry()
    for name in ("a_tool", "b_tool", "c_tool"):
        registry.register(_EchoTool(name=name))
    assert registry.filter_names(allowed=["a_tool", "b_tool"], denied=["a_tool"]) == ["b_tool"]


def test_registry_filter_skips_disabled_by_default() -> None:
    """默认关闭的工具不进默认集合；只有显式 ``include_disabled=True`` 才把它露出来。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(name="on_tool"))
    registry.register(_EchoTool(name="off_tool", enabled=False))
    assert registry.filter_names() == ["on_tool"]
    assert "off_tool" in registry.filter_names(include_disabled=True)


def test_registry_upstream_specs_skips_unknown() -> None:
    """给上游拼 ``tools`` 时忽略未注册的名字（不抛错），只输出真实存在的工具。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    specs = registry.upstream_specs(["echo", "nope"])
    assert [item["function"]["name"] for item in specs] == ["echo"]  # type: ignore[index]


def test_registry_require_raises_for_missing() -> None:
    """``require`` 对未注册的工具名报「未注册」错误；与 ``get``（返回 ``None``）分工不同。"""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError, match="未注册"):
        registry.require("nope")


def test_registry_specs_filters_by_source() -> None:
    """``specs(source=...)`` 按来源精确切分：builtin 与 mcp 各只回自己那一组。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    registry.register(
        _RawTool(
            ToolSpec(
                name="mcp__srv__x",
                description="远端工具",
                parameters={"type": "object"},
                source="mcp",
                mcp_server="srv",
            )
        )
    )
    assert [spec.name for spec in registry.specs(source="builtin")] == ["echo"]
    assert [spec.name for spec in registry.specs(source="mcp")] == ["mcp__srv__x"]


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


def test_parse_arguments_accepts_json_dict_and_empty() -> None:
    """三种合法入参（JSON 文本、已是 dict、``None``/空串）都要归一成 dict 且无错误。"""
    assert parse_arguments('{"a":1}')[0] == {"a": 1}
    assert parse_arguments({"a": 1})[0] == {"a": 1}
    assert parse_arguments(None)[0] == {}
    assert parse_arguments("")[0] == {}


@pytest.mark.parametrize("raw", ["not-json", "[1,2]", "12"])
def test_parse_arguments_rejects_non_object(raw: str) -> None:
    """合法 JSON 但不是对象也算参数非法——工具的参数永远是 dict。"""
    arguments, error = parse_arguments(raw)
    assert arguments == {}
    assert error == ERROR_INVALID_ARGUMENTS


async def test_executor_returns_not_found_without_raising(settings: Settings) -> None:
    """未注册的工具不抛异常，而是回一条 ``ERROR_NOT_FOUND`` 记录（模型能据此改口）。"""
    executor = ToolExecutor(settings, ToolRegistry())
    records = await executor.execute([tool_call("nope", {})], _ctx())
    assert records[0].status == "error"
    assert records[0].error == ERROR_NOT_FOUND


async def test_executor_rejects_tool_outside_allowlist(settings: Settings) -> None:
    """执行前的授权校验：空白名单下即使工具已注册也回 ``ERROR_NOT_ALLOWED``、不执行。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {"text": "x"})], _ctx(allowed=frozenset()))
    assert records[0].error == ERROR_NOT_ALLOWED


async def test_executor_maps_bad_arguments_to_replyable_error(settings: Settings) -> None:
    """参数非法必须是**可回复的工具结果**（``REQ-AGENT-006`` / ``AC-AGENT-04``），
    而不是让整轮请求失败。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {"text": "x" * 50})], _ctx())
    assert records[0].status == "error"
    assert records[0].error == ERROR_INVALID_ARGUMENTS
    assert records[0].payload["detail"]


async def test_executor_maps_timeout(settings: Settings) -> None:
    """工具超时被归成 ``status=timeout`` + ``ERROR_TIMEOUT``，而不是通用的执行失败。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(delay=0.3, timeout=0.05))
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {})], _ctx())
    assert records[0].status == "timeout"
    assert records[0].error == ERROR_TIMEOUT


async def test_executor_never_raises_on_tool_crash(settings: Settings) -> None:
    """工具自己的 bug 不该让整轮对话失败。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(failure=ToolExecutionError("boom")))
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {})], _ctx())
    assert records[0].status == "error"
    assert "boom" in records[0].summary


async def test_executor_never_raises_on_unexpected_exception(settings: Settings) -> None:
    """工具抛出意料之外的异常也要收敛成 ``execution_failed``，绝不让异常冒到调用方。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(failure=RuntimeError("内部错误")))
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {})], _ctx())
    assert records[0].error == "execution_failed"


async def test_executor_runs_read_tools_concurrently(settings: Settings) -> None:
    """``AC-AGENT-05``：3 个各睡 1s 的读工具，总耗时必须远小于 3s。

    这里把每次耗时压到 0.2s（避免拖慢整个测试套件），判据仍是「并发而非串行」。
    """
    registry = ToolRegistry()
    for name in ("read_one", "read_two", "read_three"):
        registry.register(_EchoTool(name=name, delay=0.2))
    executor = ToolExecutor(settings, registry)
    calls = [tool_call(name, {}) for name in ("read_one", "read_two", "read_three")]
    started = asyncio.get_running_loop().time()
    records = await executor.execute(calls, _ctx())
    elapsed = asyncio.get_running_loop().time() - started
    assert all(record.ok for record in records)
    assert elapsed < 0.5, f"读工具没有并发执行：{elapsed:.3f}s"


async def test_executor_serialises_when_any_write_present(settings: Settings) -> None:
    """``REQ-AGENT-007``：只要含一个 write，整批串行。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(name="read_one", delay=0.15))
    registry.register(_EchoTool(name="write_one", side_effect="write", delay=0.15))
    executor = ToolExecutor(settings, registry)
    calls = [tool_call("read_one", {}), tool_call("write_one", {})]
    started = asyncio.get_running_loop().time()
    records = await executor.execute(calls, _ctx())
    elapsed = asyncio.get_running_loop().time() - started
    assert all(record.ok for record in records)
    assert elapsed >= 0.25, f"含写操作的批次没有串行：{elapsed:.3f}s"


async def test_executor_blocks_write_when_not_allowed(settings: Settings) -> None:
    """``allow_write=False`` 时写工具被挡在 ``write_forbidden``（不是执行后再报错）。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(side_effect="write"))
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {})], _ctx(allow_write=False))
    assert records[0].error == "write_forbidden"


async def test_executor_keeps_result_order(settings: Settings) -> None:
    """结果顺序必须与模型给出的顺序一致——它靠顺序对齐结果。"""
    registry = ToolRegistry()
    for name, delay in (("slow_tool", 0.1), ("fast_tool", 0.0)):
        registry.register(_EchoTool(name=name, delay=delay))
    executor = ToolExecutor(settings, registry)
    records = await executor.execute(
        [tool_call("slow_tool", {}), tool_call("fast_tool", {})], _ctx()
    )
    assert [record.name for record in records] == ["slow_tool", "fast_tool"]


async def test_executor_empty_calls_returns_empty(settings: Settings) -> None:
    """空调用列表返回空结果列表（不报错），便于调用方直接展开而不用特判。"""
    assert await ToolExecutor(settings, ToolRegistry()).execute([], _ctx()) == []


async def test_executor_truncates_long_summary(settings: Settings) -> None:
    """过长的工具摘要被截到 500 字符量级（含截断标记），避免污染下游上下文。"""

    class _VerboseTool(_EchoTool):
        async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
            return ToolOutcome(payload={"blob": "y" * 5000}, summary="z" * 5000)

    registry = ToolRegistry()
    registry.register(_VerboseTool())
    executor = ToolExecutor(settings, registry)
    records = await executor.execute([tool_call("echo", {})], _ctx())
    assert len(records[0].summary) < 600  # 500 字符 + 截断标记


def test_signature_is_order_insensitive() -> None:
    """同一调用的不同键序必须产生同一个签名，否则重复检测会漏判。"""
    assert signature_of("echo", {"a": 1, "b": 2}) == signature_of("echo", {"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# executor.invoke_public（调试接口）
# ---------------------------------------------------------------------------


async def test_invoke_public_reports_missing_tool(settings: Settings) -> None:
    """调试接口对不存在的工具抛 ``TOOL_NOT_FOUND``（与列表接口同一错误码）。"""
    from app.core.exceptions import AppError, ErrorCode

    executor = ToolExecutor(settings, ToolRegistry())
    with pytest.raises(AppError) as excinfo:
        await executor.invoke_public("nope", {}, _ctx())
    assert excinfo.value.code == ErrorCode.TOOL_NOT_FOUND


async def test_invoke_public_dry_run_validates_only(settings: Settings) -> None:
    """``dry_run`` 只校验参数就返回 ``ok``（摘要标明 dry_run），工具实现体不得被执行。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(failure=RuntimeError("不该被执行")))
    executor = ToolExecutor(settings, registry)
    record = await executor.invoke_public("echo", {"text": "x"}, _ctx(), dry_run=True)
    assert record.ok
    assert "dry_run" in record.summary


async def test_invoke_public_maps_timeout_to_504(settings: Settings) -> None:
    """调试调用超时映射成 ``TOOL_TIMEOUT``（HTTP 504），与工具内部错误区分开。"""
    from app.core.exceptions import AppError, ErrorCode

    registry = ToolRegistry()
    registry.register(_EchoTool(delay=0.3, timeout=0.05))
    executor = ToolExecutor(settings, registry)
    with pytest.raises(AppError) as excinfo:
        await executor.invoke_public("echo", {}, _ctx())
    assert excinfo.value.code == ErrorCode.TOOL_TIMEOUT


async def test_invoke_public_rejects_denylisted_tool(settings: Settings) -> None:
    """配置在 ``tool_denylist`` 里的工具即使已注册也不能被调试接口调用（``TOOL_FORBIDDEN``）。"""
    from tests.conftest import build_settings

    from app.core.exceptions import AppError, ErrorCode

    registry = ToolRegistry()
    registry.register(_EchoTool())
    blocked = build_settings(tool_denylist=["echo"])
    executor = ToolExecutor(blocked, registry)
    with pytest.raises(AppError) as excinfo:
        await executor.invoke_public("echo", {}, _ctx())
    assert excinfo.value.code == ErrorCode.TOOL_FORBIDDEN


async def test_invoke_public_maps_bad_arguments_to_400(settings: Settings) -> None:
    """调用方参数错是 **400**，而不是 502：两者都是 ``status=error``，
    必须靠 ``record.error`` 区分，否则前端会把「自己写错了」当成「服务挂了」。
    """
    from app.core.exceptions import AppError, ErrorCode
    from app.tools.base import ToolArgumentError

    class _StrictTool(_EchoTool):
        def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
            raise ToolArgumentError("参数不符合工具 Schema —— text: 不能为空", {"text": ""})

    registry = ToolRegistry()
    registry.register(_StrictTool())
    executor = ToolExecutor(settings, registry)
    with pytest.raises(AppError) as excinfo:
        await executor.invoke_public("echo", {"text": ""}, _ctx())
    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT
    assert "不能为空" in excinfo.value.message


async def test_invoke_public_maps_write_forbidden_to_403(settings: Settings) -> None:
    """``allow_write=False`` 时调试调用写工具 ⇒ ``TOOL_FORBIDDEN``（403），不执行。"""
    from app.core.exceptions import AppError, ErrorCode
    from app.tools.base import ToolContext

    registry = ToolRegistry()
    registry.register(_EchoTool(side_effect="write"))
    executor = ToolExecutor(settings, registry)
    with pytest.raises(AppError) as excinfo:
        await executor.invoke_public("echo", {}, ToolContext(user_id="u", allow_write=False))
    assert excinfo.value.code == ErrorCode.TOOL_FORBIDDEN


async def test_execute_one_marks_duplicate(settings: Settings) -> None:
    """重复调用检测：同一个工具 + 同一参数第二次不再执行。"""
    from app.llm.base import LLMToolCall

    registry = ToolRegistry()
    registry.register(_EchoTool())
    executor = ToolExecutor(settings, registry)
    call = LLMToolCall(call_id="c2", name="echo", arguments='{"text": "x"}')
    first = await executor.execute_one(call, _ctx())
    assert first.ok
    second = await executor.execute_one(
        call, _ctx(), already_called=frozenset({signature_of("echo", first.arguments)})
    )
    assert second.status == "error"
    assert second.error == ERROR_DUPLICATE_CALL
    assert second.payload["hint"]


def test_error_to_code_covers_every_reason() -> None:
    """失败原因 → HTTP 错误码必须**穷举**：缺一个就会被静默归为 502。"""
    from app.tools.executor import _ERROR_TO_CODE, _STATUS_TO_CODE

    reasons = {
        ERROR_NOT_FOUND,
        ERROR_NOT_ALLOWED,
        ERROR_WRITE_FORBIDDEN,
        ERROR_INVALID_ARGUMENTS,
        ERROR_DUPLICATE_CALL,
        ERROR_TIMEOUT,
        ERROR_EXECUTION_FAILED,
    }
    assert reasons <= set(_ERROR_TO_CODE)
    assert set(_STATUS_TO_CODE) == {"error", "timeout", "forbidden"}


# ---------------------------------------------------------------------------
# kb_retrieve
# ---------------------------------------------------------------------------


def test_kb_retrieve_schema_is_object() -> None:
    """``kb_retrieve`` 的 ``parameters`` 必须是 ``object`` 且含检索相关的五个字段名。"""
    spec = KbRetrieveTool(_StubRetriever()).spec
    assert spec.parameters["type"] == "object"
    assert {"query", "kb_ids", "top_k", "rerank_top_n", "score_threshold"} <= set(
        spec.parameters["properties"]
    )


async def test_kb_retrieve_uses_caller_user_id() -> None:
    """``docs/04`` §3.1：检索范围 MUST 用与 ``/chat`` 相同的 ``user_id``。

    工具是最容易漏掉租户隔离的地方——它看起来只是「查资料」。
    """
    retriever = _StubRetriever([_chunk()])
    outcome = await KbRetrieveTool(retriever).invoke(
        {"query": "退款政策", "kb_ids": ["kb_1"]}, _ctx()
    )
    assert retriever.calls[0]["user_id"] == "u_test"
    assert retriever.calls[0]["kb_ids"] == ["kb_1"]
    assert outcome.payload["total"] == 1
    assert outcome.citations[0].chunk_id == "chk_1"


async def test_kb_retrieve_reports_empty_result_with_hint() -> None:
    """「没查到」必须显式告诉模型，否则它会把空结果当成「资料说不存在」。"""
    outcome = await KbRetrieveTool(_StubRetriever()).invoke({"query": "x"}, _ctx())
    assert outcome.payload["total"] == 0
    assert "hint" in outcome.payload
    assert "未命中" in outcome.summary


async def test_kb_retrieve_wraps_retrieval_failure() -> None:
    """检索层故障包装成 ``ToolExecutionError("知识库暂不可用")``，让 Agent 能降级回话。"""
    from app.rag.base import RetrievalUnavailable

    retriever = _StubRetriever()
    retriever.failure = RetrievalUnavailable("milvus down")
    tool = KbRetrieveTool(retriever)
    with pytest.raises(ToolExecutionError, match="知识库暂不可用"):
        await tool.invoke({"query": "x"}, _ctx())


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1+1", 2),
        ("(12.5 - 8) / 8 * 100", 56.25),
        ("2**10", 1024),
        ("10 % 3", 1),
        ("-3 + 5", 2),
        ("4/2", 2),
    ],
)
def test_calculator_evaluates_arithmetic(expression: str, expected: float) -> None:
    """白名单内的四则运算、幂、取模与负数都要算出正确结果。"""
    assert evaluate(__import__("ast").parse(expression, mode="eval")) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "(1).__class__",
        "a + 1",
        "[x for x in range(3)]",
        "1 if 2 else 3",
        "1 < 2",
        "lambda: 1",
        "f(1)",
        "'abc'",
    ],
)
def test_calculator_rejects_non_whitelisted_syntax(expression: str) -> None:
    """白名单 AST 求值：任何函数调用/属性访问/变量引用都必须被拒。

    ``__import__('os')`` 是最典型的攻击载荷：模型是**不可信输入源**，用户可以
    直接要求它把恶意串传给 calculator。
    """
    with pytest.raises(ValidationError):
        CalculatorArgs(expression=expression)


def test_calculator_rejects_too_long_expression() -> None:
    """表达式长度超上限时 schema 直接拒（避免超长串的解析开销与绕过尝试）。"""
    with pytest.raises(ValidationError):
        CalculatorArgs(expression="1+" * 200)


async def test_calculator_rejects_oversized_exponent() -> None:
    """``9**999999`` 会吃满 CPU/内存，指数必须封顶（``docs/04`` §3.1）。"""
    tool = CalculatorTool()
    with pytest.raises(ToolExecutionError):
        await tool.invoke({"expression": f"2**{EXPONENT_MAX + 1}"}, _ctx())


async def test_calculator_reports_division_by_zero() -> None:
    """除以 0 报可读的 ``ToolExecutionError("除数为 0")``，而不是让 ``ZeroDivisionError`` 冒出去。"""
    tool = CalculatorTool()
    with pytest.raises(ToolExecutionError, match="除数为 0"):
        await tool.invoke({"expression": "1/0"}, _ctx())


async def test_calculator_returns_structured_result() -> None:
    """结果是结构化 payload 且摘要里带 ``= 2``，方便模型直接引用数值。"""
    outcome = await CalculatorTool().invoke({"expression": "1+1"}, _ctx())
    assert outcome.payload["result"] == 2
    assert "= 2" in outcome.summary


# ---------------------------------------------------------------------------
# current_time
# ---------------------------------------------------------------------------


async def test_current_time_defaults_to_shanghai() -> None:
    """不传时区时默认 ``Asia/Shanghai``，返回的 ISO 串必须带 ``+08:00`` 偏移。"""
    outcome = await CurrentTimeTool().invoke({}, _ctx())
    assert outcome.payload["timezone"] == "Asia/Shanghai"
    assert outcome.payload["iso"].endswith("+08:00")


async def test_current_time_rejects_unknown_timezone() -> None:
    """拼错的时区名是**参数**问题，模型能自己改（不是 execution_failed）。"""
    with pytest.raises(ToolArgumentError, match="未知时区"):
        await CurrentTimeTool().invoke({"timezone": "Asia/Shangai"}, _ctx())


async def test_current_time_accepts_utc() -> None:
    """显式传 ``UTC`` 时按 UTC 返回（ISO 串以 ``+00:00`` 结尾），不被默认时区覆盖。"""
    outcome = await CurrentTimeTool().invoke({"timezone": "UTC"}, _ctx())
    assert outcome.payload["iso"].endswith("+00:00")


# ---------------------------------------------------------------------------
# http_fetch（SSRF）
# ---------------------------------------------------------------------------


def test_http_fetch_disabled_by_default() -> None:
    """``docs/04`` §3.1：``http_fetch`` 默认 ``enabled=false``。"""
    assert HttpFetchTool().enabled is False
    assert HttpFetchTool(enabled=True).enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6379/",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "https://0.0.0.0/",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "http:///nohost",
    ],
)
def test_http_fetch_rejects_ssrf_targets(url: str) -> None:
    """内网/回环/链路本地/保留地址必须在校验阶段被拒（``docs/04`` §3.1）。"""
    with pytest.raises(ValidationError):
        HttpFetchArgs(url=url)


@pytest.mark.parametrize(
    "hostname",
    [
        "93.184.216.34",
        "8.8.8.8",
        "2001:4860:4860::8888",
    ],
)
def test_assert_public_host_accepts_public_literals(hostname: str) -> None:
    """公网 IPv4/IPv6 字面量必须放行（校验过严会把合法目标一起挡掉）。"""
    _assert_public_host(hostname)


def test_http_fetch_rejects_dns_rebinding_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个域名可以同时解析出公网与私网 IP（DNS rebinding），**逐个**检查才有效。"""
    import socket

    from app.tools.builtin import http_fetch as module

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0)),
        ]

    monkeypatch.setattr(module.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="内网"):
        HttpFetchArgs(url="http://evil.example.com/")


def test_http_fetch_rejects_unresolvable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """域名解析失败时按「无法解析」直接拒（不能让请求带着未校验的主机名发出去）。"""
    import socket

    from app.tools.builtin import http_fetch as module

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("name resolution failed")

    monkeypatch.setattr(module.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="无法解析"):
        HttpFetchArgs(url="http://nope.invalid/")


# ---------------------------------------------------------------------------
# ToolService（列表与 prod 开关）
# ---------------------------------------------------------------------------


def _service(settings: Settings) -> tuple[ToolService, ToolRegistry]:
    registry = ToolRegistry()
    for name in ("a_tool", "b_tool", "c_tool"):
        registry.register(_EchoTool(name=name))
    executor = ToolExecutor(settings, registry)
    return ToolService(settings, registry, executor), registry


def test_tool_service_paginates(settings: Settings) -> None:
    """``list_specs`` 按 ``limit`` 分页：首页给游标，翻到最后一页 ``has_more=False``。"""
    service, _ = _service(settings)
    page, cursor, has_more = service.list_specs(limit=2)
    assert [spec.name for spec in page] == ["a_tool", "b_tool"]
    assert has_more and cursor

    page2, cursor2, has_more2 = service.list_specs(limit=2, cursor=cursor)
    assert [spec.name for spec in page2] == ["c_tool"]
    assert not has_more2 and cursor2 is None


def test_tool_service_filters_by_source(settings: Settings) -> None:
    """``list_specs(source="mcp")`` 在只有内置工具时返回空页，不会把内置工具混进来。"""
    service, _ = _service(settings)
    page, _, _ = service.list_specs(source="mcp")
    assert page == []


async def test_tool_service_invoke_blocked_in_prod() -> None:
    """``AC-AGENT-08``：prod 下调试调用必须 404（连「存在」都不暴露）。"""
    from tests.conftest import build_settings

    from app.core.config import Settings as _Settings
    from app.core.exceptions import AppError, ErrorCode

    prod = _Settings(
        _env_file=None,
        app_env="prod",
        infra_backend="real",
        jwt_secret="x",
        openai_api_key="sk-x",
        cors_origins=["https://a.example"],
    )
    assert prod.debug_tools_enabled is False
    service, _ = _service(prod)
    with pytest.raises(AppError) as excinfo:
        await service.invoke("a_tool", {}, user_id="u_test")
    assert excinfo.value.code == ErrorCode.TOOL_NOT_FOUND

    # 对照：本地/开发环境下同一调用是通的
    local = build_settings()
    local_service, _ = _service(local)
    record = await local_service.invoke("a_tool", {}, user_id="u_test")
    assert record.ok


async def test_tool_service_forces_dry_run_for_write_tools(settings: Settings) -> None:
    """调试接口不允许真的执行写操作（``docs/04`` §4.2）。"""
    registry = ToolRegistry()
    registry.register(
        _EchoTool(name="mem_tool", side_effect="write", failure=RuntimeError("不该执行"))
    )
    executor = ToolExecutor(settings, registry)
    service = ToolService(settings, registry, executor)
    record = await service.invoke("mem_tool", {"text": "x"}, user_id="u_test")
    assert record.ok
    assert "dry_run" in record.summary


# ---------------------------------------------------------------------------
# LLM 流式 tool_calls 分片累积
# ---------------------------------------------------------------------------


def test_tool_call_accumulator_merges_fragments() -> None:
    """上游把一次工具调用切成多片（``id``/``name`` 只在首片，参数逐片拼接）。

    任何「按到达顺序 append」的写法都会把一次调用拆成多次。
    """
    from app.llm.openai_compat import _ToolCallAccumulator

    accumulator = _ToolCallAccumulator()
    accumulator.feed([])  # 有些 chunk 完全不带工具调用
    accumulator.feed([{"index": 0, "id": "c1", "name": "calculator", "args": ""}])
    accumulator.feed([{"index": 0, "args": '{"expr'}])
    accumulator.feed([{"index": 0, "args": 'ession":"1+1"}'}])
    calls = accumulator.deltas(final=True)
    assert len(calls) == 1
    assert calls[0].name == "calculator"
    assert json.loads(calls[0].arguments) == {"expression": "1+1"}


def test_tool_call_accumulator_tracks_parallel_calls_by_index() -> None:
    """上游并行下发多个工具调用时按 ``index`` 分别累积，且**各自**的片段不会串台。"""
    from app.llm.openai_compat import _ToolCallAccumulator

    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "c1", "name": "a_tool", "args": "{"}])
    accumulator.feed([{"index": 1, "id": "c2", "name": "b_tool", "args": "{"}])
    accumulator.feed([{"index": 0, "args": "}"}])
    accumulator.feed([{"index": 1, "args": "}"}])
    names = [call.name for call in accumulator.deltas(final=True)]
    assert names == ["a_tool", "b_tool"]
    args = [json.loads(call.arguments) for call in accumulator.deltas(final=True)]
    assert args == [{}, {}]


def test_tool_call_accumulator_skips_nameless_before_final() -> None:
    """没有名字的调用无法执行，提前交给 Agent Loop 只会让它记进「已调用」集合。"""
    from app.llm.openai_compat import _ToolCallAccumulator

    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "c1", "args": "{}"}])
    assert accumulator.deltas(final=False) == []
    assert accumulator.deltas(final=True) == []
    assert accumulator.incomplete() == [0]


def test_tool_call_accumulator_flags_malformed_arguments() -> None:
    """参数分片拼完仍不是合法 JSON 时，该 index 必须被登记为「解析失败」而不是静默当空参数。"""
    from app.llm.openai_compat import _ToolCallAccumulator

    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"index": 0, "id": "c1", "name": "a_tool", "args": "{not json"}])
    assert accumulator.unparsed() == [0]


def test_tool_call_accumulator_without_index_uses_position() -> None:
    """旧版函数调用 API 只给 ``id``/``name`` 而无索引。"""
    from app.llm.openai_compat import _ToolCallAccumulator

    accumulator = _ToolCallAccumulator()
    accumulator.feed([{"id": "c1", "name": "a_tool", "args": "{}"}])
    calls = accumulator.deltas(final=True)
    assert len(calls) == 1
    assert calls[0].call_id == "c1"


def test_openai_llm_message_includes_tool_calls() -> None:
    """回注助手轮必须带上 tool_calls，否则后续 ``role=tool`` 消息会被上游拒绝。"""
    from app.llm.base import LLMMessage
    from app.llm.openai_compat import _to_lc_message

    message = LLMMessage(
        role="assistant",
        content="",
        tool_calls=(
            {"id": "c1", "type": "function", "function": {"name": "calculator", "arguments": "{}"}},
        ),
    )
    payload = _to_lc_message(message)
    assert payload["tool_calls"][0]["function"]["name"] == "calculator"


def test_llm_tool_call_keeps_raw_arguments_string() -> None:
    """参数保持 JSON **文本**：流式下参数是分片下发的，只有文本能拼接后解析。"""
    call = LLMToolCall(call_id="c1", name="echo", arguments='{"a":1}')
    assert isinstance(call.arguments, str)
