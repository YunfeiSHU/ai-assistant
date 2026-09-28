"""工具层装配（``REQ-AGENT-002``）。

这个模块是「工具从哪来」的唯一答案：内置工具在这里注册，MCP 工具在 M6 由
``app/mcp`` 调用 :meth:`ToolRegistry.register` 加入同一个注册表。

装配期的校验刻意做在**启动路径**上（:func:`build_tool_registry` 抛异常 = 应用起不来），
因为「工具名冲突」「schema 不是合法 JSON Schema」这类问题一旦带进运行期，表现为
「模型偶尔调错工具」，几乎无法定位。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.rag.base import Retriever
from app.services.memory import MemoryService
from app.tools.base import (
    DESCRIPTION_MAX_CHARS,
    TOOL_NAME_PATTERN,
    Tool,
    ToolSpec,
)
from app.tools.builtin import (
    CalculatorTool,
    CurrentTimeTool,
    HttpFetchTool,
    KbRetrieveTool,
    MemorySaveTool,
    MemorySearchTool,
)
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistrationError, ToolRegistry
from app.tools.service import ToolService

logger = logging.getLogger("app.tools")


def build_tool_registry(
    settings: Settings,
    *,
    retriever: Retriever,
    memory: MemoryService | None = None,
    extra_tools: list[Tool] | None = None,
) -> ToolRegistry:
    """构造并校验工具注册表。

    Args:
        settings: 配置。
        retriever: ``kb_retrieve`` 依赖的检索器（与 ``/chat`` 共用同一个实例）。
        memory: 长期记忆服务（``memory_save`` / ``memory_search``）；
            ``None`` 时不注册并记 warning —— 传入 ``None`` 只应发生在**测试**里，
            生产装配缺了它就会让「用户要求记住」静默失效。
        extra_tools: 额外注册的工具（测试注入 / M6 的 MCP 工具）。

    Raises:
        ToolRegistrationError: 重名、名字不合规、描述超长、schema 非法。
    """
    registry = ToolRegistry()
    builtin: list[Tool] = [
        KbRetrieveTool(retriever),
        CalculatorTool(),
        CurrentTimeTool(),
        HttpFetchTool(enabled=settings.tool_http_fetch_enabled),
    ]
    if memory is not None:
        builtin.extend([MemorySaveTool(memory), MemorySearchTool(memory)])
    else:
        logger.warning(
            "tools.memory_tools_skipped",
            extra={"tools": [MemorySaveTool.name, MemorySearchTool.name]},
        )
    if extra_tools:
        builtin.extend(extra_tools)

    for tool in builtin:
        validate_tool_spec(tool.spec)
        registry.register(tool)

    _validate_denylist(settings, registry)

    logger.info(
        "tools.registered",
        extra={
            "tool_count": len(registry),
            "tools": registry.names(),
            "http_fetch_enabled": settings.tool_http_fetch_enabled,
        },
    )
    return registry


def build_tool_service(
    settings: Settings,
    registry: ToolRegistry,
    *,
    executor: ToolExecutor | None = None,
) -> ToolService:
    """构造工具服务（``GET /tools`` 与调试调用接口共用）。"""
    return ToolService(settings, registry, executor or ToolExecutor(settings, registry))


def validate_tool_spec(spec: ToolSpec) -> None:
    """校验单个工具定义；不合格即抛（启动失败，``AC-AGENT-07``）。

    公开导出是因为 **MCP 工具也要走同一套校验**：不能因为工具来自第三方 Server
    就跳过描述长度 / schema 形状的检查 —— 否则「模型看到一份坏 schema」这类问题
    只会在线上表现为「某个工具偶尔调不对」。
    """
    if not TOOL_NAME_PATTERN.match(spec.name):
        raise ToolRegistrationError(
            f"工具名不合法：{spec.name!r}（须匹配 {TOOL_NAME_PATTERN.pattern}）"
        )
    if not spec.description.strip():
        raise ToolRegistrationError(f"工具 {spec.name} 缺少描述")
    if len(spec.description) > DESCRIPTION_MAX_CHARS:
        raise ToolRegistrationError(
            f"工具 {spec.name} 描述过长（{len(spec.description)} > {DESCRIPTION_MAX_CHARS}）"
        )
    schema = spec.parameters
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolRegistrationError(
            f"工具 {spec.name} 的 parameters 必须是 type=object 的 JSON Schema"
        )
    if spec.source == "mcp" and not spec.mcp_server:
        raise ToolRegistrationError(f"MCP 工具 {spec.name} 必须声明 mcp_server")


def _validate_denylist(settings: Settings, registry: ToolRegistry) -> None:
    """黑名单里出现不存在的工具名时记 warning——不失败。

    ``tool_denylist`` / ``tool_write_allowlist`` 是**运维配置**，它的生命周期比
    代码长：先按发布计划写好 ``memory_save``、再等该工具上线是正常操作。所以这里
    只警告不报错；真正需要「配了就必须存在」的是装配期断言（:meth:`ToolRegistry.require`）。
    """
    unknown = [name for name in settings.tool_denylist if name not in registry]
    if unknown:
        logger.warning(
            "tools.denylist_unknown",
            extra={"unknown": unknown, "tool_count": len(registry)},
        )


def tool_diagnostics(registry: ToolRegistry) -> list[dict[str, Any]]:
    """诊断信息（``/health/detail`` 用）：工具名 + 启用状态 + 副作用。"""
    return [
        {
            "name": spec.name,
            "source": spec.source,
            "side_effect": spec.side_effect,
            "enabled": spec.enabled,
        }
        for spec in registry.specs()
    ]


__all__ = [
    "ToolRegistrationError",
    "ToolRegistry",
    "build_tool_registry",
    "build_tool_service",
    "tool_diagnostics",
    "validate_tool_spec",
]
