"""工具层的类型与基类（契约见 ``docs/04-Agent与工具调用.md`` §2 / §3）。

这一层刻意**不依赖 LLM**：Schema 校验、超时、并发、截断、SSRF 防护全都能在不启动模型的情况下
单测，Agent Loop 只依赖这里的抽象。

三个关键设计：

1. **``parameters`` 由 pydantic 输入模型生成**（:func:`json_schema_of`），不手写第二份 JSON Schema
   —— 「校验用的 schema」与「发给上游的 schema」必须是同一份。
2. **工具产出结构化 ``payload``，不产出渲染好的文本**：引用编号需要全局分配（同一次 Agent 运行里
   第一个片段永远是 ``[1]``），而工具看不到全局状态，所以渲染与编号由 Agent Loop 负责。
3. **参数校验失败是一种「可回复的工具结果」**，不是异常：它必须回注给模型让它改参数重试
   （``REQ-AGENT-006``），所以 :class:`ToolArgumentError` 由执行器捕获并转成 ``invalid_arguments``。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from app.rag.base import RetrievedChunk

#: 工具来源（``docs/04`` §2.1）
ToolSource = Literal["builtin", "mcp"]

#: 副作用类型：``read`` 可并发自动执行；``write`` 需显式放行且串行执行
SideEffect = Literal["read", "write"]

#: 工具调用结果状态（与 ``schemas.chat.ToolCallTrace.status`` 同源）
ToolStatus = Literal["ok", "error", "timeout", "forbidden"]

#: 工具名约束（``docs/04`` §2.1）。上游 function name 还限 64 字符，
#: 这里一并卡住——否则会在**运行时**才被上游拒绝
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

#: MCP 工具命名空间分隔符（``docs/04`` §2.2）。
#: 用双下划线而不是 ``:``：上游 OpenAI 兼容接口的 function name 通常限
#: ``^[a-zA-Z0-9_-]{1,64}$``，冒号会被上游拒绝。
MCP_SEPARATOR = "__"

#: 描述长度上限（``docs/04`` §2.1）
DESCRIPTION_MAX_CHARS = 512
#: 工具调用 summary 的长度上限（``schemas.chat.ToolCallTrace.summary``）
SUMMARY_MAX_CHARS = 500


def namespace_tool(server: str, tool: str) -> str:
    """把 MCP 工具命名空间化为 ``mcp__{server}__{tool}``。

    ``-`` / ``.`` 一律替换为 ``_``：它们合法出现在 MCP server 名里，但不被上游
    function name 规则接受。
    """
    safe_server = re.sub(r"[^a-zA-Z0-9_]", "_", server)
    safe_tool = re.sub(r"[^a-zA-Z0-9_]", "_", tool)
    return f"mcp{MCP_SEPARATOR}{safe_server}{MCP_SEPARATOR}{safe_tool}"


def json_schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """由 pydantic 模型生成上游可用的 JSON Schema。

    删掉 pydantic 自动补的 ``title``：对模型没有信息量却会占掉可观的 token（每个字段一行），
    而且有些上游网关对 schema 里的未知关键字比较敏感。
    """
    schema = model.model_json_schema()
    _strip_titles(schema)
    return schema


def _strip_titles(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


def clip(text: str, limit: int, *, marker: str = "…[truncated]") -> str:
    """按字符截断并留痕。

    截断必须有标记：模型看到半截 JSON 会以为工具返回了坏数据，看到标记才知道「还有更多，
    需要就换更窄的查询」。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + marker


class ToolArgumentError(ValueError):
    """参数不符合工具 Schema（``REQ-AGENT-006``）。

    这是**可回复的错误**：执行器会把它转成 ``invalid_arguments`` 的工具结果，让模型改参数重试。
    """

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ToolExecutionError(RuntimeError):
    """工具执行期失败（工具内部错误、外部依赖不可用等）。

    同样会被执行器转成 ``execution_failed`` 回注，而不是 500。
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具定义（``docs/04`` §2.1）。"""

    name: str
    description: str
    parameters: dict[str, Any]
    source: ToolSource = "builtin"
    side_effect: SideEffect = "read"
    mcp_server: str | None = None
    timeout_seconds: float | None = None
    enabled: bool = True
    example_arguments: dict[str, Any] = field(default_factory=dict)

    def to_upstream(self) -> dict[str, Any]:
        """转成上游 ``tools`` 参数的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_public(self) -> dict[str, Any]:
        """转成 ``GET /tools`` 的一项（``docs/04`` §4.1）。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "source": self.source,
            "mcp_server": self.mcp_server,
            "side_effect": self.side_effect,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "example_arguments": self.example_arguments,
        }


@dataclass(slots=True)
class ToolContext:
    """调用上下文。

    ``user_id`` 是所有数据访问的隔离键：``kb_retrieve`` 必须应用与 ``/chat`` 完全相同的隔离
    （``docs/04`` §3.1），否则工具会变成越权读取的入口。
    """

    user_id: str
    conversation_id: str | None = None
    #: 允许的调用集（白名单 ∩ − 黑名单）；``None`` 表示不限制
    allowed: frozenset[str] | None = None
    #: 该次运行是否允许执行 ``write`` 类工具
    allow_write: bool = True

    def permits(self, name: str) -> bool:
        """该工具名是否在允许集合内。"""
        return self.allowed is None or name in self.allowed


@dataclass(slots=True)
class ToolOutcome:
    """一次调用的结果。

    ``payload`` 是结构化结果（回注给模型的 JSON）；``summary`` 是面向用户展示的一句话摘要，
    不允许泄露内部路径、连接串（``docs/04`` §4.3）。
    """

    status: ToolStatus = "ok"
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    elapsed_ms: int = 0
    #: 仅检索类工具会带：供 Agent Loop 分配全局引用编号
    citations: list[RetrievedChunk] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@runtime_checkable
class Tool(Protocol):
    """工具协议。

    ``validate`` 与 ``invoke`` 分开，是为了让 ``POST /tools/{name}/invoke``
    的 ``dry_run=true`` 能只校验不执行（``docs/04`` §4.2）。
    """

    @property
    def spec(self) -> ToolSpec:
        """工具定义。"""
        ...

    @property
    def enabled(self) -> bool:
        """是否对外可见（``http_fetch`` 之类默认关闭）。"""
        ...

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """校验并规范化参数。

        Raises:
            ToolArgumentError: 参数非法。
        """
        ...

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """执行工具；参数已通过 :meth:`validate`。"""
        ...


class BuiltinTool(ABC):
    """内置工具的基类。

    子类只需声明 ``name`` / ``description`` / ``input_model`` 并实现 :meth:`run`。
    Schema、参数校验、描述长度校验都由基类统一处理 —— 让 5 个工具各自写一遍参数校验，
    就一定会有一个写错。
    """

    #: 工具名（必须匹配 :data:`TOOL_NAME_PATTERN`）
    name: ClassVar[str] = ""
    #: 给模型看的功能描述，必须写明「何时使用 / 何时不适用」
    description: ClassVar[str] = ""
    #: 参数模型（Schema 的唯一事实来源）
    input_model: ClassVar[type[BaseModel]]
    side_effect: ClassVar[SideEffect] = "read"
    timeout_seconds: ClassVar[float | None] = None
    example_arguments: ClassVar[dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        """默认启用；条件启用的工具（如 ``http_fetch``）覆写它。"""
        return True

    @property
    def timeout(self) -> float | None:
        """该工具的超时（秒）；``None`` 表示用全局 ``tool_timeout_seconds``。"""
        return self.timeout_seconds

    @property
    def spec(self) -> ToolSpec:
        """工具定义（每次构造新对象，避免调用方改到共享状态）。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=json_schema_of(self.input_model),
            source="builtin",
            side_effect=self.side_effect,
            timeout_seconds=self.timeout,
            enabled=self.enabled,
            example_arguments=dict(self.example_arguments),
        )

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 ``input_model`` 校验参数。"""
        try:
            # ``mode="json"`` 要求 pydantic 自己能转，这里用 python 模式即可
            model = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            # ``errors()`` 含输入值，``include_url=False`` 去掉 pydantic 文档链接
            # （它对模型毫无用处，还很占 token）
            detail = _clean_errors(exc.errors(include_url=False))
            raise ToolArgumentError(_describe(detail), detail) from exc
        return model.model_dump()

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """校验后执行。

        这里做了两次 ``model_validate``（一次在本方法、一次在 :meth:`validate`）—— 参数是几字段的
        小 dict，开销可忽略；换来的是「直接调用 ``invoke`` 也安全」。
        """
        validated = self.validate(arguments)
        return await self.run(self.input_model.model_validate(validated), ctx)

    @abstractmethod
    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        """子类实现：执行工具。"""
        ...


def _clean_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """精简 pydantic 的错误项：只留模型改参数需要的字段。"""
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        item: dict[str, Any] = {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
        }
        if error.get("type"):
            item["type"] = error["type"]
        cleaned.append(item)
    return cleaned


def _describe(errors: list[dict[str, Any]], *, limit: int = 3) -> str:
    """把校验错误压成一句话，作为 ``ToolArgumentError.message``。

    为什么拼进 message 而不只放 ``detail``：上游模型只看得到 ``message``（``detail`` 是结构化
    字段，很多上游会把它截掉）。把「哪个字段、错在哪」写进 message，模型才有可能一次改对参数 ——
    否则它只会看到「参数不符合 Schema」并原样重试。
    """
    if not errors:
        return "参数不符合工具 Schema"
    parts: list[str] = []
    for error in errors[:limit]:
        loc = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        msg = str(error.get("msg", "非法取值"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    extra = len(errors) - limit
    suffix = f"（另有 {extra} 项错误）" if extra > 0 else ""
    return "参数不符合工具 Schema —— " + "；".join(parts) + suffix


def payload_summary(payload: dict[str, Any], *, limit: int = SUMMARY_MAX_CHARS) -> str:
    """默认摘要：JSON 文本截断。

    子类可以覆写成更友好的一句话（如 ``kb_retrieve`` 的「命中 3 个片段」），但默认值必须是
    JSON 而不是空串，否则日志里只有「调用过」，没有「拿到了什么」。
    """
    return clip(json.dumps(payload, ensure_ascii=False, default=str), limit)


__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "MCP_SEPARATOR",
    "SUMMARY_MAX_CHARS",
    "TOOL_NAME_PATTERN",
    "BuiltinTool",
    "SideEffect",
    "Tool",
    "ToolArgumentError",
    "ToolContext",
    "ToolExecutionError",
    "ToolOutcome",
    "ToolSource",
    "ToolSpec",
    "ToolStatus",
    "clip",
    "json_schema_of",
    "namespace_tool",
    "payload_summary",
]
