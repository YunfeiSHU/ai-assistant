"""工具注册表（``REQ-AGENT-002`` / ``003``）。

内置工具与 MCP 工具走**同一个入口**注册、查询、调用（``docs/04`` §2.2）。唯一的分支点是重名
处理：内置之间重名是编程错误（启动失败），内置与 MCP 重名则内置优先、MCP 侧被重命名。

内置重名必须 fail-fast：两个同名工具会按注册顺序静默覆盖，模型看到的 schema 与实际执行的
可能不是同一个 —— 这种问题在运行期表现为「工具行为偶尔不对」，几乎无法定位。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.tools.base import (
    TOOL_NAME_PATTERN,
    Tool,
    ToolContext,
    ToolOutcome,
    ToolSpec,
)


class ToolRegistrationError(RuntimeError):
    """注册表装配错误（重名 / 非法名）。

    这是编程错误，所以直接抛出去让应用启动失败（``AC-AGENT-07``），而不是记一条 warning
    让它带着坏数据继续跑。
    """


class ToolRegistry:
    """进程内工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: 注册顺序（``GET /tools`` 的稳定排序基准）
        self._order: list[str] = []

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, tool: Tool, *, rename: str | None = None) -> str:
        """注册一个工具，返回它的最终名字。

        Args:
            tool: 工具实例。
            rename: MCP 工具的命名空间名；``None`` 时用 ``tool.spec.name``。

        Raises:
            ToolRegistrationError: 名字非法、内置工具重名，或 ``rename`` 撞名。
        """
        spec = tool.spec
        name = rename or spec.name
        if not TOOL_NAME_PATTERN.match(name):
            raise ToolRegistrationError(
                f"工具名不合法：{name!r}（须匹配 {TOOL_NAME_PATTERN.pattern}）"
            )
        existing = self._tools.get(name)
        if existing is not None:
            # 内置之间重名 → 启动失败（AC-AGENT-07）；
            # 这里也覆盖 rename 撞名（两个 MCP server 提供了同名工具）。
            raise ToolRegistrationError(f"工具名冲突：{name}（已有 {existing.spec.source} 工具）")
        self._tools[name] = _RenamedTool(tool, name) if name != spec.name else tool
        self._order.append(name)
        return name

    def register_all(self, tools: Iterable[Tool]) -> list[str]:
        """批量注册（任一失败即抛，前面的注册保留——反正启动会失败）。"""
        return [self.register(tool) for tool in tools]

    def unregister(self, name: str) -> bool:
        """摘掉一个工具，返回是否真的存在过。

        存在理由：``POST /mcp/servers/{name}/reload`` 之后某个 Server 的工具集合可能变化
        （新增 / 重命名 / 下线），必须能把旧的摘掉，否则注册表里会同时留着「已经调不通的旧
        工具」和「新工具」，模型会选中前者并拿到执行失败。

        摘除立即生效（注册表在调用时才查表），所以正在执行中的请求若刚好要用这个工具，会得到
        「工具不存在」而不是执行失败 —— 这是可接受的：重载是低频运维动作，而「工具不存在」
        本身就是当时的真实情况。
        """
        if name not in self._tools:
            return False
        del self._tools[name]
        self._order = [item for item in self._order if item != name]
        return True

    def unregister_source(self, source: str, *, mcp_server: str | None = None) -> list[str]:
        """按来源批量摘除，返回被摘掉的工具名。

        ``mcp_server`` 非空时只摘某一个 Server 的工具（重载单个 Server 的场景）。
        """
        removed: list[str] = []
        for name in self.names():
            spec = self._tools[name].spec
            if spec.source != source:
                continue
            if mcp_server is not None and spec.mcp_server != mcp_server:
                continue
            if self.unregister(name):
                removed.append(name)
        return removed

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool | None:
        """按名取工具（未注册返回 ``None``，由调用方决定错误码）。"""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """全部工具名（按注册顺序）。"""
        return [name for name in self._order if name in self._tools]

    def all(self) -> list[Tool]:
        """全部工具实例（按注册顺序）。"""
        return [self._tools[name] for name in self.names()]

    def specs(
        self,
        *,
        source: str | None = None,
        enabled: bool | None = None,
    ) -> list[ToolSpec]:
        """按条件筛选工具定义（``GET /tools`` 的数据来源）。"""
        items: list[ToolSpec] = []
        for tool in self.all():
            spec = tool.spec
            if source is not None and spec.source != source:
                continue
            if enabled is not None and spec.enabled is not enabled:
                continue
            items.append(spec)
        return items

    def filter_names(
        self,
        *,
        allowed: Sequence[str] | None = None,
        denied: Sequence[str] = (),
        denylist: Sequence[str] = (),
        include_disabled: bool = False,
    ) -> list[str]:
        """计算本次调用可用的工具名集合。

        优先级（``docs/04`` §4.3）：``denylist``（配置）> ``denied`` > ``allowed``。结果按注册
        顺序稳定排序 —— 上游每次拿到的 ``tools`` 数组必须一致，否则 prompt 前缀每次都变，
        会打掉上游的缓存命中。

        Args:
            allowed: 白名单；``None`` 表示不限制。
            denied: 请求级黑名单。
            denylist: 配置级黑名单（``tool_denylist``）。
            include_disabled: 是否包含 ``enabled=false`` 的工具（调试查询用）。
        """
        blocked = {*denied, *denylist}
        allow = set(allowed) if allowed is not None else None
        picked: list[str] = []
        for tool in self.all():
            spec = tool.spec
            if not include_disabled and not spec.enabled:
                continue
            if spec.name in blocked:
                continue
            if allow is not None and spec.name not in allow:
                continue
            picked.append(spec.name)
        return picked

    def require(self, name: str, *, enabled: bool = True) -> Tool:
        """取工具，缺失/未启用即抛 :class:`ToolRegistrationError`。

        用于**装配期**：``tool_write_allowlist`` 里写了不存在的工具、
        Agent 服务依赖某个内置工具却没注册，都应该在启动时发现。
        """
        tool = self.get(name)
        if tool is None:
            raise ToolRegistrationError(f"工具未注册：{name}")
        if enabled and not tool.spec.enabled:
            raise ToolRegistrationError(f"工具未启用：{name}")
        return tool

    def upstream_specs(self, names: Sequence[str]) -> list[dict[str, object]]:
        """把工具名列表转成上游 ``tools`` 参数。

        名字里包含未知工具时**跳过并继续**：调用方（白名单校验）已经保证过存在性，这里再抛会
        形成一个「同一个错误两种处理」的分叉。
        """
        specs: list[dict[str, object]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                specs.append(tool.spec.to_upstream())
        return specs


class _RenamedTool:
    """给 MCP 工具套一层命名空间名（``spec.name`` 与注册名不同时用）。

    只替换 ``spec.name``：其余字段与调用路径原样透传，所以「重命名」不会让 MCP 工具的
    校验/执行语义与内置工具产生分叉。
    """

    def __init__(self, inner: Tool, name: str) -> None:
        self._inner = inner
        self._name = name

    @property
    def spec(self) -> ToolSpec:
        spec = self._inner.spec
        return ToolSpec(
            name=self._name,
            description=spec.description,
            parameters=spec.parameters,
            source=spec.source,
            side_effect=spec.side_effect,
            mcp_server=spec.mcp_server,
            timeout_seconds=spec.timeout_seconds,
            enabled=spec.enabled,
            example_arguments=spec.example_arguments,
        )

    @property
    def enabled(self) -> bool:
        return self._inner.enabled

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._inner.validate(arguments)

    async def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        return await self._inner.invoke(arguments, ctx)


__all__ = ["ToolRegistrationError", "ToolRegistry"]
