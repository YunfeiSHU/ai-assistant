"""MCP Server 配置的固定 Schema 与启动期校验（``REQ-MCP-001``，契约见 ``docs/05`` §2.2）。

**用 ``extra="forbid"``**：MCP 配置写错（``comand`` 而不是 ``command``）时，「忽略未知字段」
的表现是「Server 静默地用默认参数启动」或「永远连不上」，而错误信息里什么线索都没有。
配置错误必须**在启动期指名道姓地报出来**（``AC-MCP-04``）。

**密钥引用**：``headers`` / ``env`` 里的值支持 ``${ENV_VAR}`` 形式（``docs/05`` §6），这样
配置文件里不出现明文密钥；展开失败的引用 MUST 报错，而不是留下一个字面量 ``${...}`` 去当令牌
用 —— 那会变成一个「401 但看起来配置没问题」的谜题。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.tools.base import MCP_SEPARATOR

#: ``tools/call`` 的超时上限（docs/05 §4：``MUST ≤ 60s``）
TOOL_TIMEOUT_MAX_SECONDS = 60.0

#: Server 名允许的字符集：它会拼进工具名 ``mcp__{server}__{tool}``
SERVER_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: ``${ENV_VAR}`` 引用
_SECRET_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

McpTransport = Literal["stdio", "streamable_http"]


class McpConfigError(RuntimeError):
    """MCP 配置不合法（启动期抛出 → 应用起不来）。"""

    def __init__(self, message: str, *, server: str = "", field: str = "") -> None:
        super().__init__(message)
        self.server = server
        self.field = field


class McpServerConfig(BaseModel):
    """单个 MCP Server 的配置（字段与 docs/05 §2.2 的表一一对应）。"""

    model_config = ConfigDict(extra="forbid")

    transport: McpTransport = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    #: 只注册这些工具；``None`` = 全部
    tools_allowlist: list[str] | None = None
    #: 明确禁用（优先级高于 allowlist）
    tools_denylist: list[str] = Field(default_factory=list)
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    #: ``true`` 时连不上则**应用启动失败**（``REQ-MCP-002``）
    required: bool = False
    #: 副作用为 ``write`` 的工具名（未声明的按 ``read`` 处理，见 docs/05 §4）
    write_tools: list[str] = Field(default_factory=list)

    @field_validator("timeout_seconds", "connect_timeout_seconds")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("必须为正数")
        return value

    @model_validator(mode="after")
    def _check_transport_fields(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("transport=stdio 时 command 必填")
        elif not self.url.strip():
            raise ValueError("transport=streamable_http 时 url 必填")
        if self.timeout_seconds > TOOL_TIMEOUT_MAX_SECONDS:
            # 超时超过 60s 会超出 Agent 的整体预算，等于没有超时
            raise ValueError(f"timeout_seconds 不得超过 {TOOL_TIMEOUT_MAX_SECONDS:g}")
        return self

    # ------------------------------------------------------------------
    # 工具过滤
    # ------------------------------------------------------------------
    def allows_tool(self, tool: str) -> bool:
        """按 allowlist / denylist 判断某工具是否注册。

        优先级：``tools_denylist`` > ``tools_allowlist``（``docs/05`` §2.2）。
        """
        if tool in self.tools_denylist:
            return False
        if self.tools_allowlist is None:
            return True
        return tool in self.tools_allowlist

    def side_effect_of(self, tool: str) -> Literal["read", "write"]:
        """工具的副作用类型；未在 ``write_tools`` 里声明的按 ``read`` 处理。"""
        return "write" if tool in self.write_tools else "read"


def expand_references(
    values: Mapping[str, str], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """展开 ``${ENV_VAR}`` 引用。

    Raises:
        McpConfigError: 引用的环境变量不存在（**不放行字面量**，见模块 docstring）。
    """
    source = os.environ if environ is None else environ
    return {key: _expand_value(value, source, key) for key, value in values.items()}


def _expand_value(value: str, environ: Mapping[str, str], field: str) -> str:
    """展开单个值里的全部 ``${ENV_VAR}`` 引用。"""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = environ.get(name)
        if resolved is None:
            raise McpConfigError(f"配置 {field} 引用了未设置的环境变量 ${{{name}}}", field=field)
        return resolved

    return _SECRET_REFERENCE.sub(substitute, value)


def parse_server_config(
    name: str,
    raw: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> McpServerConfig:
    """解析并校验单个 Server 配置。

    Raises:
        McpConfigError: 名字非法、未知字段、类型错误、密钥引用缺失。
    """
    if not SERVER_NAME_PATTERN.match(name):
        raise McpConfigError(
            f"MCP Server 名不合法：{name!r}（须匹配 {SERVER_NAME_PATTERN.pattern}）", server=name
        )
    try:
        config = McpServerConfig.model_validate(dict(raw))
    except ValidationError as exc:
        raise McpConfigError(_describe(name, exc), server=name) from exc

    if config.transport == "stdio":
        config.env = expand_references(config.env, environ)
    else:
        config.headers = expand_references(config.headers, environ)
    return config


def parse_servers(
    raw: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, McpServerConfig]:
    """解析整张 ``MCP_SERVERS`` 配置表。

    Raises:
        McpConfigError: 任一项不合法（一次只报第一项，但信息里带 Server 名与字段名）。
    """
    if not raw:
        return {}
    return {name: parse_server_config(name, value, environ=environ) for name, value in raw.items()}


def _describe(name: str, exc: ValidationError) -> str:
    """把 pydantic 的校验错误压成一句含 Server 名与字段名的话（``AC-MCP-04``）。"""
    parts: list[str] = []
    for error in exc.errors(include_url=False)[:3]:
        loc = ".".join(str(part) for part in error.get("loc", ()))
        msg = str(error.get("msg", "非法取值"))
        if error.get("type") == "extra_forbidden":
            msg = "未知字段（请核对 docs/05 §2.2 的字段表）"
        parts.append(f"{loc}: {msg}" if loc else msg)
    return f"MCP Server {name!r} 配置不合法 —— " + "；".join(parts)


def namespaced_name(server: str, tool: str) -> str:
    """把 ``(server, tool)`` 转成注册表里合法的工具名（``docs/05`` §4）。

    与 :func:`app.tools.base.namespace_tool` 的差别只有一处，但很关键：**这里保证结果合法**。
    MCP 工具名来自 Server，可能是 ``readFile``、``foo.bar`` 之类，而注册表要求
    ``^[a-z][a-z0-9_]{1,63}$`` —— 直接拼出来的名字会让 :meth:`ToolRegistry.register` 抛错、
    进而让整个应用启动失败，而且是第三方 Server 的名字导致的。所以这里做三件事：

    1. 小写 + 非法字符换 ``_``；
    2. 若发生过替换则追加源名哈希 —— 否则 ``a.b`` 与 ``a_b`` 会撞成同一个工具名；
    3. 超过 64 字符时截断并追加哈希（上游 function name 限 64 字符）。
    """

    def sanitize(text: str) -> str:
        return re.sub(r"[^a-z0-9_]", "_", text.lower())

    safe_server = sanitize(server) or "server"
    raw_tool = tool
    safe_tool = sanitize(tool) or "tool"
    if not safe_tool[0].isalpha():
        safe_tool = f"t{safe_tool}"
    changed = safe_tool != raw_tool.lower()
    digest = hashlib.sha256(f"{server}{MCP_SEPARATOR}{tool}".encode()).hexdigest()
    suffix = f"_{digest[:6]}" if changed else ""

    full = f"mcp{MCP_SEPARATOR}{safe_server}{MCP_SEPARATOR}{safe_tool}{suffix}"
    if len(full) <= 64:
        return full
    reserved = len(f"mcp{MCP_SEPARATOR}{MCP_SEPARATOR}_{digest[:6]}") + len(suffix)
    keep = max(64 - reserved, 1)
    return f"mcp{MCP_SEPARATOR}{safe_server[:keep]}{MCP_SEPARATOR}{digest[:6]}{suffix}"[:64]


__all__ = [
    "SERVER_NAME_PATTERN",
    "TOOL_TIMEOUT_MAX_SECONDS",
    "McpConfigError",
    "McpServerConfig",
    "McpTransport",
    "expand_references",
    "namespaced_name",
    "parse_server_config",
    "parse_servers",
]
