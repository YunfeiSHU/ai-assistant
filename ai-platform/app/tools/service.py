"""工具服务：``GET /tools`` 的列表与调试调用（``docs/04`` §4.1 / §4.2）。

放在服务层而不是路由里，是为了让「prod 上调试接口返回 404 而不是 403」这条规则有唯一实现处：
prod 要连「有这个东西」都不暴露，所以整个路由在 prod 下**不存在**（404），而不是存在但拒绝（403）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import decode_cursor, encode_cursor
from app.tools.base import ToolContext, ToolSpec
from app.tools.executor import ToolCallRecord, ToolExecutor
from app.tools.registry import ToolRegistry

#: 工具列表的游标时间戳基准（工具集是静态的，没有真实创建时间）
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ToolService:
    """工具查询与调试调用。"""

    #: 列表分页默认值（``GET /tools`` 的默认 limit）
    DEFAULT_LIMIT = 50
    MAX_LIMIT = 200

    def __init__(self, settings: Settings, registry: ToolRegistry, executor: ToolExecutor) -> None:
        self._settings = settings
        self._registry = registry
        self._executor = executor

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def list_specs(
        self,
        *,
        source: str | None = None,
        enabled: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> tuple[list[ToolSpec], str | None, bool]:
        """返回 ``(items, next_cursor, has_more)``。

        游标只承载工具名：工具集是进程内静态的，排序键天然稳定，不需要 ``created_at``。
        复用 :mod:`app.core.pagination` 是为了让「游标不可解析 → ``INVALID_ARGUMENT``」
        的行为与其它列表接口一致。
        """
        limit = max(1, min(limit, self.MAX_LIMIT))
        specs = self._registry.specs(source=source, enabled=enabled)
        specs.sort(key=lambda spec: spec.name)

        start = 0
        if cursor:
            _, name = decode_cursor(cursor)
            start = next(
                (index for index, spec in enumerate(specs) if spec.name > name),
                len(specs),
            )

        page = specs[start : start + limit]
        has_more = start + limit < len(specs)
        next_cursor = encode_cursor(_EPOCH, page[-1].name) if has_more and page else None
        return page, next_cursor, has_more

    # ------------------------------------------------------------------
    # 调试调用
    # ------------------------------------------------------------------
    async def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        user_id: str,
        dry_run: bool = False,
    ) -> ToolCallRecord:
        """调试调用；``prod`` 下整个接口不通（见模块 docstring）。"""
        if not self._settings.debug_tools_enabled:
            # 404 而不是 403：prod 不暴露「这些工具存在」
            raise AppError(ErrorCode.TOOL_NOT_FOUND, f"工具不存在：{name}")

        tool = self._registry.get(name)
        effective_dry_run = dry_run
        if tool is not None and tool.spec.side_effect == "write":
            # 调试接口**不允许**真的执行写操作（``docs/04`` §4.2）：这里没有对话上下文
            # 可回滚，误写一次就是脏数据。
            effective_dry_run = True

        ctx = ToolContext(user_id=user_id, allowed=_allowed_for(self._settings, name))
        return await self._executor.invoke_public(name, arguments, ctx, dry_run=effective_dry_run)


def _allowed_for(settings: Settings, name: str) -> frozenset[str] | None:
    """调试调用的授权集合：配置黑名单是硬约束，其余放行。"""
    if name in settings.tool_denylist:
        return frozenset()
    return None


__all__ = ["ToolService"]
