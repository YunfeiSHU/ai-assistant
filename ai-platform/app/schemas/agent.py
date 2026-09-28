"""Agent 与工具相关的数据模型（契约见 ``docs/04-Agent与工具调用.md`` §4）。

与 ``app/schemas/chat.py`` 同样只做**语法级**校验：``allowed_tools`` 里写了不存在的
工具名属于**语义**错误（要等注册表算出来才知道），由 service 抛
``INVALID_ARGUMENT``，而不是在 pydantic 里报一个语义模糊的参数错误。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.schemas.chat import ChatRequest, Reference, ToolCallTrace, Usage
from app.schemas.common import StrictModel

#: 工具定义（``GET /tools`` 的一项，``docs/04`` §4.1）
ToolSideEffect = Literal["read", "write"]
ToolSourceName = Literal["builtin", "mcp"]


class ToolDefinition(StrictModel):
    """工具定义。"""

    name: str = Field(description="工具名（MCP 工具带 ``mcp__server__tool`` 命名空间）")
    description: str = Field(description="给模型看的功能描述")
    parameters: dict[str, Any] = Field(description="参数的 JSON Schema（type=object）")
    source: ToolSourceName = Field(default="builtin", description="来源")
    mcp_server: str | None = Field(default=None, description="MCP 服务器名（source=mcp 时必填）")
    side_effect: ToolSideEffect = Field(default="read", description="副作用类型")
    timeout_seconds: float | None = Field(default=None, description="超时（秒）")
    enabled: bool = Field(default=True, description="是否启用")
    example_arguments: dict[str, Any] = Field(default_factory=dict, description="示例参数")


class ToolListResponse(StrictModel):
    """``GET /tools`` 响应（``docs/04`` §4.1）。"""

    items: list[ToolDefinition] = Field(default_factory=list)
    next_cursor: str | None = Field(default=None)
    has_more: bool = Field(default=False)


class ToolInvokeRequest(StrictModel):
    """``POST /tools/{name}/invoke`` 请求体（``docs/04`` §4.2）。"""

    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    dry_run: bool = Field(default=False, description="只校验不执行；write 工具强制为 true")


class ToolInvokeResponse(StrictModel):
    """``POST /tools/{name}/invoke`` 响应（``docs/04`` §4.2）。"""

    name: str
    status: Literal["ok", "error", "timeout", "forbidden"]
    result: dict[str, Any] = Field(default_factory=dict, description="工具结构化结果")
    elapsed_ms: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, description="失败时的原因码")


class AgentRunRequest(ChatRequest):
    """``POST /agent/run`` 与 ``/agent/run/stream`` 的请求体（``docs/04`` §4.3）。

    继承 ``ChatRequest`` 而不是复制字段：两边的 ``kb_ids`` 格式校验、``metadata``
    长度校验、``rerank_top_n ≤ top_k`` 的跨字段校验都只有一份实现。
    ``use_tools`` 在 service 里被**强制**置为 true（文档要求），不依赖调用方传对。
    """

    max_steps: int = Field(default=8, ge=1, le=16, description="最大推理轮次")
    allowed_tools: list[str] | None = Field(
        default=None,
        max_length=64,
        description="工具白名单；null = 全部可用工具",
    )
    denied_tools: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="工具黑名单，优先级高于白名单",
    )


class AgentRunResponse(StrictModel):
    """``POST /agent/run`` 响应（``docs/04`` §4.3 = ``ChatResponse`` + ``steps``）。"""

    answer: str = Field(description="最终回答（Markdown）")
    conversation_id: str | None = Field(default=None)
    message_id: str = Field(description="本条回答的 ID")
    references: list[Reference] = Field(default_factory=list, description="引用来源（全局编号）")
    tool_calls: list[ToolCallTrace] = Field(default_factory=list, description="工具调用轨迹")
    steps: int = Field(default=0, ge=0, description="推理轮次（不含收尾调用）")
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = Field(default="stop", description="stop / length / max_steps")
    model: str = Field(default="")
    degraded: bool = Field(default=False)
    degraded_reasons: list[str] = Field(default_factory=list)
    elapsed_ms: int = Field(default=0, ge=0)


__all__ = [
    "AgentRunRequest",
    "AgentRunResponse",
    "ToolDefinition",
    "ToolInvokeRequest",
    "ToolInvokeResponse",
    "ToolListResponse",
]
