"""把 MCP 工具适配成 :class:`~app.tools.base.Tool`（``REQ-MCP-003``，契约见 ``docs/05`` §4）。

适配器的存在意义是「**MCP 工具与内置工具在 Agent 眼里没有区别**」：
同一张注册表、同一个 ``ToolSpec``、同一套 ``ToolOutcome``。
Agent Loop 与 ``/tools`` 接口都不需要知道某个工具是不是来自 MCP。

三处必须做对的地方：

1. **名字命名空间化**（``mcp__{server}__{tool}``）：两个 Server 都可能提供 ``search``，
   不隔离就会撞名。
2. **``invoke`` 永不抛异常**：工具执行器会把异常转成 ``execution_failed`` 回注给模型，
   但那是「兜底」；MCP 的失败原因（连接断了 / 上游返回 ``isError``）是**可展示**的，
   由适配器自己映射成 ``error`` 状态能带上更有用的信息。
3. **参数校验只做「必需字段」这一层**：完整 JSON Schema 校验需要一个 schema 库，
   而 MCP Server 真正在意的是自己的校验 —— 我们提前拦下「模型漏传 required 字段」
   这类高频错误即可，剩下的交给 Server 报错（``isError=true`` 会被原样回注）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import AppError
from app.core.logging import get_logger, hash_identifier
from app.mcp.client import McpClient
from app.mcp.config import namespaced_name
from app.mcp.manager import McpManager
from app.mcp.session import McpToolDef
from app.tools import validate_tool_spec
from app.tools.base import (
    DESCRIPTION_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    SideEffect,
    ToolArgumentError,
    ToolContext,
    ToolOutcome,
    ToolSpec,
    clip,
)
from app.tools.registry import ToolRegistry

logger = get_logger("app.mcp")

#: 工具调用失败时回注给模型的错误码（与 ``app.tools.executor`` 的取值保持一致）
ERROR_EXECUTION_FAILED = "execution_failed"
ERROR_INVALID_ARGUMENTS = "invalid_arguments"

#: JSON Schema 里表示「必需参数」的键
_REQUIRED = "required"


@dataclass(slots=True)
class McpTool:
    """一个 MCP 工具（实现 :class:`~app.tools.base.Tool` 协议）。"""

    server: str
    client: McpClient
    definition: McpToolDef
    #: 注册表里的名字
    name: str
    side_effect: SideEffect = "read"
    #: 传给模型的 ``user_id`` 哈希盐（审计用）
    pepper: str = ""

    # ------------------------------------------------------------------
    # 声明
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, client: McpClient, definition: McpToolDef, *, pepper: str = "") -> McpTool:
        """按 Server 配置构造适配器（名字 / 副作用 / 描述都在这里定型）。"""
        return cls(
            server=client.name,
            client=client,
            definition=definition,
            name=namespaced_name(client.name, definition.name),
            side_effect=client.config.side_effect_of(definition.name),
            pepper=pepper,
        )

    @property
    def spec(self) -> ToolSpec:
        """工具定义（``docs/05`` §4 的字段映射表）。"""
        description = (
            self.definition.description.strip() or f"[{self.server}] {self.definition.name}"
        )
        return ToolSpec(
            name=self.name,
            description=clip(description, DESCRIPTION_MAX_CHARS),
            parameters=self.definition.input_schema
            or {"type": "object", "additionalProperties": True},
            source="mcp",
            side_effect=self.side_effect,
            mcp_server=self.server,
            timeout_seconds=self.client.config.timeout_seconds,
            enabled=self.client.config.enabled,
            example_arguments={},
        )

    @property
    def enabled(self) -> bool:
        """Server 被禁用时工具一并不可见。"""
        return self.client.config.enabled

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只校验「大体结构 + 必需字段」（见模块 docstring 的第 3 点）。"""
        if not isinstance(arguments, dict):
            raise ToolArgumentError(
                "arguments 必须是 JSON 对象", {"type": type(arguments).__name__}
            )
        required = self.definition.input_schema.get(_REQUIRED)
        if isinstance(required, list) and required:
            missing = [str(key) for key in required if key not in arguments]
            if missing:
                raise ToolArgumentError(f"缺少必需参数：{', '.join(missing)}", {"missing": missing})
        return dict(arguments)

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        """调用 MCP Server；**任何失败都返回 ``ToolOutcome``，不抛异常**。"""
        started = time.perf_counter()
        parameters = self.validate(arguments)
        try:
            result = await self.client.call_tool(self.definition.name, parameters)
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            outcome = ToolOutcome(
                status="error",
                payload={
                    "error": ERROR_EXECUTION_FAILED,
                    "detail": {"code": str(exc.code), "message": exc.message},
                },
                summary=clip(exc.message or str(exc.code), SUMMARY_MAX_CHARS),
            )
        except Exception as exc:  # 适配器自身的 bug 也不该冒泡（工具边界必须闭合）
            logger.warning(
                "mcp.tool_adapter_failed",
                extra={"server": self.server, "tool": self.definition.name, "error": str(exc)},
                exc_info=True,
            )
            outcome = ToolOutcome(
                status="error",
                payload={
                    "error": ERROR_EXECUTION_FAILED,
                    "detail": {"message": clip(str(exc), 200)},
                },
                summary=clip(str(exc), SUMMARY_MAX_CHARS),
            )
        else:
            outcome = self._to_outcome(result.text, result.is_error)

        outcome.elapsed_ms = int((time.perf_counter() - started) * 1000)
        # ``ai_tool_calls_total`` 由 :class:`~app.tools.executor.ToolExecutor` 统一记录
        # （内置与 MCP 走同一条路径）。这里**不**再记一次：两处都记会让每次 MCP 调用
        # 被计两次 —— 指标翻倍比缺指标难查得多，因为总量看起来「一切正常」。
        if self.side_effect == "write":
            self._audit(ctx, arguments, outcome)
        return outcome

    def _to_outcome(self, text: str, is_error: bool) -> ToolOutcome:
        """把 ``CallToolResult`` 转成 ``ToolOutcome``（``docs/05`` §4）。"""
        if is_error:
            # Server 明确说这次调用失败了 —— ``UPSTREAM_MCP_ERROR`` 对应
            # 「MCP 调用返回错误」（HTTP 502），但这里不抛异常：
            # 工具失败的正常出口是回注给模型，让它换个问法或换工具。
            return ToolOutcome(
                status="error",
                payload={"error": "upstream_mcp_error", "detail": {"text": text}},
                summary=clip(text or "MCP 工具返回错误", SUMMARY_MAX_CHARS),
            )
        return ToolOutcome(
            status="ok",
            payload={"text": text, "server": self.server, "tool": self.definition.name},
            summary=clip(text, SUMMARY_MAX_CHARS),
        )

    def _audit(self, ctx: ToolContext, arguments: dict[str, Any], outcome: ToolOutcome) -> None:
        """写副作用工具必须留审计（``docs/05`` §6）。

        ``user_id`` 用 HMAC 哈希后记录（``docs/10`` §5.1：MUST NOT 明文外泄），
        ``arguments`` 只记键名 —— 写操作的参数里常常夹着用户输入的自由文本。
        """
        logger.info(
            "mcp.audit",
            extra={
                "server": self.server,
                "tool": self.name,
                "tool_name": self.definition.name,
                "user_id": hash_identifier(ctx.user_id, self.pepper),
                "conversation_id": ctx.conversation_id,
                "argument_keys": sorted(arguments)[:20],
                "status": outcome.status,
                "elapsed_ms": outcome.elapsed_ms,
            },
        )


def build_mcp_tools(client: McpClient, *, pepper: str = "") -> list[McpTool]:
    """为某个 Server 构造全部（已过滤的）工具适配器。"""
    return [McpTool.build(client, tool, pepper=pepper) for tool in client.select_tools()]


def sync_mcp_tools(
    registry: ToolRegistry,
    manager: McpManager,
    *,
    server: str | None = None,
    pepper: str = "",
) -> list[str]:
    """把 MCP 工具同步进注册表（**幂等**），返回注册成功的工具名。

    两步：先摘掉目标 Server 已有的 MCP 工具，再注册当前的工具集合。
    「先摘后注册」而不是「只补新增」的原因是工具集合会**减少**：
    Server 升级后下线一个工具，如果旧注册项留着，模型会选中一个已经调不通的工具。

    注册前的校验走 :func:`app.tools.validate_tool_spec` —— 与内置工具同一套
    （描述非空、schema 是 ``type=object``、``mcp_server`` 已声明）。

    Raises:
        ToolRegistrationError: 与内置工具重名，或规格不合法（启动期直接失败）。
    """
    removed = registry.unregister_source("mcp", mcp_server=server)
    if removed:
        logger.info("mcp.tools_unregistered", extra={"server": server or "*", "tools": removed})

    registered: list[str] = []
    for client, definition in manager.iter_tools():
        if server is not None and client.name != server:
            continue
        tool = McpTool.build(client, definition, pepper=pepper)
        validate_tool_spec(tool.spec)
        registered.append(registry.register(tool))
    if registered:
        logger.info(
            "mcp.tools_registered",
            extra={"server": server or "*", "count": len(registered), "tools": registered[:20]},
        )
    return registered


__all__ = [
    "ERROR_EXECUTION_FAILED",
    "ERROR_INVALID_ARGUMENTS",
    "McpTool",
    "build_mcp_tools",
    "sync_mcp_tools",
]
