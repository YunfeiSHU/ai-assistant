"""工具执行器（``REQ-AGENT-006`` / ``REQ-AGENT-007``）。

**执行器从不向上层抛业务异常**：工具不存在、参数非法、执行失败、超时，全部变成一次带
``status`` 的 :class:`ToolCallRecord`，由 Agent Loop 回注给模型让它自行修正或放弃
（``docs/04`` §5）。只有调试接口 ``POST /tools/{name}/invoke`` 会把失败映射成 HTTP 错误码
—— 那里没有模型可以「自行修正」。

并发规则（``REQ-AGENT-007``）：全部是 ``read`` 时用 ``asyncio.gather`` 并发；只要含一个
``write``，**整体串行**。刻意不做「读先并发、写再串行」的精细调度：两次调用之间的可见性会
有微妙差异，而收益只有几十毫秒。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing
from app.llm.base import LLMToolCall
from app.rag.base import RetrievedChunk
from app.tools.base import (
    SUMMARY_MAX_CHARS,
    Tool,
    ToolArgumentError,
    ToolContext,
    ToolExecutionError,
    ToolOutcome,
    ToolStatus,
    clip,
    payload_summary,
)
from app.tools.registry import ToolRegistry

logger = logging.getLogger("app.tools")

#: 单工具超时上限（``docs/04`` §2.1）
TOOL_TIMEOUT_MAX_SECONDS = 60.0

#: 回注给模型的错误码（``docs/04`` §5 的「对模型/用户的呈现」列）
ERROR_NOT_ALLOWED = "tool_not_allowed"
ERROR_NOT_FOUND = "tool_not_found"
ERROR_INVALID_ARGUMENTS = "invalid_arguments"
ERROR_EXECUTION_FAILED = "execution_failed"
ERROR_TIMEOUT = "timeout"
ERROR_DUPLICATE_CALL = "duplicate_call"
ERROR_WRITE_FORBIDDEN = "write_forbidden"


@dataclass(slots=True)
class ToolCallRecord:
    """一次工具调用的完整记录（内部形态；对外见 ``schemas.chat.ToolCallTrace``）。"""

    call_id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    status: ToolStatus = "ok"
    summary: str = ""
    payload: dict[str, object] = field(default_factory=dict)
    elapsed_ms: int = 0
    #: 失败原因码（``docs/04`` §5），成功时为空
    error: str = ""
    citations: list[RetrievedChunk] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def parse_arguments(raw: str | dict[str, object] | None) -> tuple[dict[str, object], str]:
    """把模型给的 ``arguments`` 解析成 dict。

    Returns:
        ``(参数, 错误码)``；解析失败时参数为空 dict 且错误码为
        :data:`ERROR_INVALID_ARGUMENTS`。

    上游三种传法都要能接住：JSON 文本（OpenAI 规范）、已经是 dict（部分网关）、空
    （无参数工具）。只认 JSON 文本会让「无参数工具」被误判成参数非法。
    """
    if raw is None or raw == "":
        return {}, ""
    if isinstance(raw, dict):
        return dict(raw), ""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}, ERROR_INVALID_ARGUMENTS
    if not isinstance(parsed, dict):
        # ``[1,2]`` / ``"abc"`` 这类合法 JSON 但不是对象
        return {}, ERROR_INVALID_ARGUMENTS
    return parsed, ""


class ToolExecutor:
    """按注册表执行工具调用。"""

    def __init__(self, settings: Settings, registry: ToolRegistry) -> None:
        self._settings = settings
        self._registry = registry

    def _span(self, name: str, tool_name: str, *, source: str, side_effect: str = "read"):
        """工具调用 span（``docs/10`` §5.1：``tool.call``）。

        除了规范要求的 ``tool_name`` / ``source`` / ``status``，这里额外带上 ``side_effect``：
        排障时「这个慢调用是不是写操作」是第一个要问的问题，而它只在注册表里，不在请求里。
        """
        return get_tracing().span(
            name, {"tool_name": tool_name, "source": source, "side_effect": side_effect}
        )

    # ------------------------------------------------------------------
    # Agent Loop 入口
    # ------------------------------------------------------------------
    async def execute(
        self,
        calls: Sequence[LLMToolCall],
        ctx: ToolContext,
    ) -> list[ToolCallRecord]:
        """执行一组调用；返回顺序与入参一致（模型靠顺序对齐结果）。"""
        if not calls:
            return []

        planned: list[tuple[LLMToolCall, dict[str, object], str, Tool | None]] = []
        for call in calls:
            arguments, parse_error = parse_arguments(call.arguments)
            tool = self._registry.get(call.name) if not parse_error else None
            planned.append((call, arguments, parse_error, tool))

        serial = any(
            tool is not None and tool.spec.side_effect == "write" for _, _, _, tool in planned
        )
        if serial:
            # 含 write → 全部串行：不做精细调度（见模块 docstring）
            return [await self._one(*item, ctx=ctx) for item in planned]
        return list(await asyncio.gather(*(self._one(*item, ctx=ctx) for item in planned)))

    async def execute_one(
        self,
        call: LLMToolCall,
        ctx: ToolContext,
        *,
        already_called: frozenset[str] = frozenset(),
    ) -> ToolCallRecord:
        """执行单个调用（重复调用检测由调用方通过 ``already_called`` 传入）。"""
        arguments, parse_error = parse_arguments(call.arguments)
        tool = self._registry.get(call.name) if not parse_error else None
        return await self._one(call, arguments, parse_error, tool, ctx=ctx, already=already_called)

    # ------------------------------------------------------------------
    # 调试接口入口（会抛 AppError）
    # ------------------------------------------------------------------
    async def invoke_public(
        self,
        name: str,
        arguments: dict[str, object],
        ctx: ToolContext,
        *,
        dry_run: bool = False,
    ) -> ToolCallRecord:
        """调试接口：失败映射为 HTTP 错误码。

        Raises:
            AppError: ``404 TOOL_NOT_FOUND`` / ``403 TOOL_FORBIDDEN`` /
                ``400 INVALID_ARGUMENT`` / ``504 TOOL_TIMEOUT`` / ``502 TOOL_EXECUTION_FAILED``。
        """
        tool = self._registry.get(name)
        if tool is None:
            raise AppError(
                ErrorCode.TOOL_NOT_FOUND,
                f"工具不存在：{name}",
                {"allowed": self._registry.filter_names()},
            )
        if not tool.spec.enabled:
            # 未启用的工具对外不可见：返回 403 而不是 404，因为调用方是运维/开发（有鉴权），
            # 告诉他「存在但被关掉」比假装不存在更有用。
            raise AppError(
                ErrorCode.TOOL_FORBIDDEN,
                f"工具已禁用：{name}",
                {"name": name, "denylist": list(self._settings.tool_denylist)},
            )
        if name in self._settings.tool_denylist:
            raise AppError(
                ErrorCode.TOOL_FORBIDDEN,
                f"工具被配置禁用：{name}",
                {"name": name},
            )

        call = LLMToolCall(call_id="debug", name=name, arguments=json.dumps(arguments))
        if dry_run:
            try:
                normalised = tool.validate(arguments)
            except ToolArgumentError as exc:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT, exc.message, {"detail": exc.detail}
                ) from exc
            return ToolCallRecord(
                call_id="debug",
                name=name,
                arguments=normalised,
                status="ok",
                summary="dry_run：参数校验通过，未执行",
            )

        record = await self._one(call, arguments, "", tool, ctx=ctx)
        if record.ok:
            return record
        raise _as_app_error(record, name)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _one(
        self,
        call: LLMToolCall,
        arguments: dict[str, object],
        parse_error: str,
        tool: Tool | None,
        *,
        ctx: ToolContext,
        already: frozenset[str] = frozenset(),
    ) -> ToolCallRecord:
        """调用一次工具，并记录 ``ai_tool_calls_total``。

        指标放在这层薄包装而不是 ``_execute_one`` 的各个 return 处：``_fail`` 有 7 个出口，
        每次改动都要记得补一个计数 —— 一定会漏。
        """
        record = await self._execute_one(
            call, arguments, parse_error, tool, ctx=ctx, already=already
        )
        # 工具不存在 / 未授权这类“调用前的拒绝”也计入：它们同样是一次失败的调用，
        # 而“模型反复调用不存在的工具”正是需要被指标看见的问题
        get_metrics().record_tool_call(
            tool_name=record.name,
            source=tool.spec.source if tool is not None else "unknown",
            status=record.status,
        )
        return record

    async def _execute_one(
        self,
        call: LLMToolCall,
        arguments: dict[str, object],
        parse_error: str,
        tool: Tool | None,
        *,
        ctx: ToolContext,
        already: frozenset[str] = frozenset(),
    ) -> ToolCallRecord:
        record = ToolCallRecord(call_id=call.call_id, name=call.name, arguments=arguments)

        if parse_error:
            return _fail(record, ERROR_INVALID_ARGUMENTS, "arguments 不是合法的 JSON 对象")
        if tool is None:
            return _fail(record, ERROR_NOT_FOUND, f"工具不存在：{call.name}")
        if not tool.spec.enabled:
            return _fail(record, ERROR_NOT_FOUND, f"工具不可用：{call.name}")
        if not ctx.permits(call.name):
            # 白名单/黑名单命中：这是**调用方**的授权问题，不是工具的问题
            logger.warning(
                "tool.not_allowed",
                extra={"tool": call.name, "user_id": ctx.user_id, "error_code": "TOOL_FORBIDDEN"},
            )
            return _fail(record, ERROR_NOT_ALLOWED, "该工具未在本次请求的允许范围内")
        if already and _signature(call.name, arguments) in already:
            return duplicate_record(call, arguments)
        if tool.spec.side_effect == "write" and not ctx.allow_write:
            return _fail(record, ERROR_WRITE_FORBIDDEN, "本次请求未放行写操作工具")

        started = time.perf_counter()
        budget = min(
            tool.spec.timeout_seconds or self._settings.tool_timeout_seconds,
            TOOL_TIMEOUT_MAX_SECONDS,
        )
        try:
            with self._span(
                "tool.call", call.name, source=tool.spec.source, side_effect=tool.spec.side_effect
            ):
                outcome: ToolOutcome = await asyncio.wait_for(
                    tool.invoke(arguments, ctx), timeout=budget
                )
        except ToolArgumentError as exc:
            return _fail(
                record,
                ERROR_INVALID_ARGUMENTS,
                exc.message,
                detail=exc.detail,
                elapsed_ms=_elapsed(started),
            )
        except TimeoutError:
            logger.warning("tool.timeout", extra={"tool": call.name, "budget": budget})
            return _fail(
                record,
                ERROR_TIMEOUT,
                f"工具执行超时（{budget:g}s）",
                status="timeout",
                elapsed_ms=_elapsed(started),
            )
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            # 工具内部用 AppError 表达「依赖不可用」这类可展示原因（如向量库宕机）
            return _fail(
                record,
                ERROR_EXECUTION_FAILED,
                exc.message or str(exc.code),
                elapsed_ms=_elapsed(started),
            )
        except ToolExecutionError as exc:
            return _fail(record, ERROR_EXECUTION_FAILED, str(exc), elapsed_ms=_elapsed(started))
        except Exception as exc:  # 工具自己的 bug 不该让整轮对话失败
            logger.warning(
                "tool.failed", extra={"tool": call.name, "error": str(exc)}, exc_info=True
            )
            return _fail(
                record,
                ERROR_EXECUTION_FAILED,
                clip(str(exc), 200),
                elapsed_ms=_elapsed(started),
            )

        record.status = outcome.status
        record.payload = dict(outcome.payload)
        record.citations = list(outcome.citations)
        record.elapsed_ms = _elapsed(started)
        record.summary = clip(
            outcome.summary or payload_summary(outcome.payload), SUMMARY_MAX_CHARS
        )
        return record


def _signature(name: str, arguments: dict[str, object]) -> str:
    """调用的规范化签名（重复调用检测用）。

    用 ``sort_keys`` 而不是原文本：``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 是同一个调用，
    按原文本比较会漏判。
    """
    return f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)}"


def signature_of(name: str, arguments: dict[str, object]) -> str:
    """对外暴露的签名函数（Agent Loop 用它维护已执行集合）。"""
    return _signature(name, arguments)


def duplicate_record(call: LLMToolCall, arguments: dict[str, object]) -> ToolCallRecord:
    """构造「重复调用」的伪记录（**不执行**，只把原因回注给模型）。

    执行器与 Agent Loop 共用这一个构造：同一错误原因在两条路径上必须长得一样，否则模型看到
    的 payload 会随「谁先发现了重复」而变化。
    """
    message = "该调用已执行过，请换一种方式或直接回答"
    return ToolCallRecord(
        call_id=call.call_id,
        name=call.name,
        arguments=dict(arguments),
        status="error",
        error=ERROR_DUPLICATE_CALL,
        summary=clip(f"{_ERROR_SUMMARY[ERROR_DUPLICATE_CALL]}：{message}", SUMMARY_MAX_CHARS),
        payload={
            "error": ERROR_DUPLICATE_CALL,
            "message": message,
            "hint": "同一工具与同一参数的调用只执行一次",
        },
    )


def _fail(
    record: ToolCallRecord,
    error: str,
    message: str,
    *,
    detail: object = None,
    status: ToolStatus = "error",
    elapsed_ms: int = 0,
) -> ToolCallRecord:
    """构造一条失败记录；``payload`` 的形状就是回注给模型的内容（``docs/04`` §5）。"""
    record.status = status
    record.error = error
    record.elapsed_ms = elapsed_ms
    payload: dict[str, object] = {"error": error}
    if message:
        payload["message"] = clip(message, 300)
    if detail:
        payload["detail"] = detail
    record.payload = payload
    record.summary = clip(_ERROR_SUMMARY.get(error, error) + f"：{message}", SUMMARY_MAX_CHARS)
    return record


#: 面向用户展示的失败摘要（不含内部路径/连接串，``docs/04`` §4.3）
_ERROR_SUMMARY = {
    ERROR_NOT_ALLOWED: "工具未授权",
    ERROR_NOT_FOUND: "工具不存在",
    ERROR_INVALID_ARGUMENTS: "参数不合法",
    ERROR_EXECUTION_FAILED: "工具执行失败",
    ERROR_TIMEOUT: "工具执行超时",
    ERROR_DUPLICATE_CALL: "重复调用已跳过",
    ERROR_WRITE_FORBIDDEN: "写操作未放行",
}

#: 记录状态 → 对外错误码（调试接口用；无更细的错误原因时回退）
_STATUS_TO_CODE = {
    "error": ErrorCode.TOOL_EXECUTION_FAILED,
    "timeout": ErrorCode.TOOL_TIMEOUT,
    "forbidden": ErrorCode.TOOL_FORBIDDEN,
}

#: 记录内部错误原因 → 对外错误码（比 ``status`` 精确：都是 ``error`` 状态，
#: 但「参数不合法」给 400、「依赖挂了」才给 502）
_ERROR_TO_CODE = {
    ERROR_NOT_FOUND: ErrorCode.TOOL_NOT_FOUND,
    ERROR_NOT_ALLOWED: ErrorCode.TOOL_FORBIDDEN,
    ERROR_WRITE_FORBIDDEN: ErrorCode.TOOL_FORBIDDEN,
    ERROR_INVALID_ARGUMENTS: ErrorCode.INVALID_ARGUMENT,
    ERROR_DUPLICATE_CALL: ErrorCode.INVALID_ARGUMENT,
    ERROR_TIMEOUT: ErrorCode.TOOL_TIMEOUT,
    ERROR_EXECUTION_FAILED: ErrorCode.TOOL_EXECUTION_FAILED,
}


def _as_app_error(record: ToolCallRecord, name: str) -> AppError:
    """把失败记录映射成调试接口的 HTTP 错误码。

    优先看 ``record.error``（能区分「参数错」与「执行错」），只有拿不到已知原因时才用
    ``status`` 兜底。
    """
    code = _ERROR_TO_CODE.get(
        record.error, _STATUS_TO_CODE.get(record.status, ErrorCode.TOOL_EXECUTION_FAILED)
    )
    message = str(record.payload.get("message") or record.summary or "工具执行失败")
    return AppError(code, message, {"name": name, "status": record.status})


def _elapsed(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


__all__ = [
    "ERROR_DUPLICATE_CALL",
    "ERROR_EXECUTION_FAILED",
    "ERROR_INVALID_ARGUMENTS",
    "ERROR_NOT_ALLOWED",
    "ERROR_NOT_FOUND",
    "ERROR_TIMEOUT",
    "ERROR_WRITE_FORBIDDEN",
    "TOOL_TIMEOUT_MAX_SECONDS",
    "ToolCallRecord",
    "ToolExecutor",
    "duplicate_record",
    "parse_arguments",
    "signature_of",
]
