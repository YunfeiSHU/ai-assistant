"""单个 MCP Server 的连接管理（``REQ-MCP-002`` / ``REQ-MCP-004``，契约见 ``docs/05`` §3）。

一个 :class:`McpClient` 负责一个 Server 的**全生命周期**：建连 → 列表 → 调用 →
懒重连 → 关闭。管理器只负责「按名字找客户端」和「统一注册工具」，不碰连接细节。

三处刻意的设计：

1. **连接是「一次性上下文」，不是「长连接对象」**。用 :class:`~contextlib.AsyncExitStack`
   持有 ``open_session`` 的上下文，关闭时必须走 ``aclose()`` —— 否则 stdio 子进程会成为
   僵尸（``docs/05`` §3 明确要求显式关停，否则 ``uvicorn --reload`` 会不断堆积）。
2. **握手超时交给 transport，外层只是兜底**。``AsyncExitStack.enter_async_context``
   在 ``__aenter__`` 返回后才登记上下文管理器：如果在 ``__aenter__`` 中途被取消，
   半建好的子进程不会被回收。所以主超时放在 transport 内部（包住 ``initialize()``），
   外层超时只在极端情况下生效并尽力 ``aclose()``。
3. **失败要落到状态上**。``docs/05`` §3.1 的四种状态是**对外可见**的
   （``/mcp/servers``、``/health/ready``），所以每一次连接失败都要更新
   ``state`` + ``last_error``，不能只写日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger, redact, redact_mapping
from app.mcp.config import McpServerConfig
from app.mcp.session import (
    McpCallResult,
    McpToolDef,
    McpTransportSession,
    SessionFactory,
    open_session,
)
from app.observability.circuit import CircuitBreaker
from app.observability.metrics import get_metrics
from app.observability.tracing import Tracing

logger = get_logger("app.mcp")

#: 对外可见的连接状态（``docs/05`` §3.1）
McpStatus = Literal["connected", "connecting", "unavailable", "disabled"]

#: 懒重连的退避序列（``docs/05`` §3：0.5s / 1s / 2s）
RECONNECT_DELAYS: tuple[float, ...] = (0.5, 1.0)

#: 状态 → 指标取值
_STATUS_TO_METRIC: dict[McpStatus, str] = {
    "connected": "connected",
    "connecting": "connecting",
    "unavailable": "unavailable",
    "disabled": "disabled",
}


@dataclass(slots=True)
class McpServerStatus:
    """``GET /mcp/servers`` 的一项（内部形态；对外见 ``schemas.mcp``）。"""

    name: str
    transport: str
    status: McpStatus
    required: bool = False
    tools_count: int = 0
    last_error: str | None = None
    last_connected_at: str | None = None
    latency_ms: int | None = None


@dataclass(slots=True)
class _Runtime:
    """客户端运行期状态。"""

    tools: list[McpToolDef] = field(default_factory=list)
    last_error: str | None = None
    last_connected_at: datetime | None = None
    latency_ms: int | None = None


class McpClient:
    """一个 MCP Server 的连接与调用。"""

    def __init__(
        self,
        name: str,
        config: McpServerConfig,
        *,
        session_factory: SessionFactory | None = None,
        breaker: CircuitBreaker | None = None,
        max_result_chars: int = 4000,
        tracing: Tracing | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self._factory: SessionFactory = session_factory or open_session
        self._breaker = breaker
        self._max_result_chars = max_result_chars
        self._tracing = tracing
        self._state: McpStatus = "disabled" if not config.enabled else "unavailable"
        self._session: McpTransportSession | None = None
        self._stack: AsyncExitStack | None = None
        self._runtime = _Runtime()
        self._lock = asyncio.Lock()
        self._sync_metric()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def status(self) -> McpStatus:
        """当前状态。"""
        if not self.config.enabled:
            return "disabled"
        return self._state

    @property
    def tools(self) -> list[McpToolDef]:
        """最近一次成功 ``tools/list`` 的结果（未过滤）。"""
        return list(self._runtime.tools)

    @property
    def tools_count(self) -> int:
        """已注册工具数（allowlist/denylist 过滤**之后**）。"""
        return len(self.select_tools())

    @property
    def breaker(self) -> CircuitBreaker | None:
        """熔断器（未配置时为 ``None``）。"""
        return self._breaker

    def select_tools(self) -> list[McpToolDef]:
        """按 allowlist / denylist 过滤工具（``docs/05`` §2.2 / §4）。"""
        return [tool for tool in self._runtime.tools if self.config.allows_tool(tool.name)]

    def snapshot(self) -> McpServerStatus:
        """对外状态快照。"""
        return McpServerStatus(
            name=self.name,
            transport=self.config.transport,
            status=self.status,
            required=self.config.required,
            tools_count=self.tools_count,
            last_error=self._runtime.last_error,
            last_connected_at=(
                self._runtime.last_connected_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if self._runtime.last_connected_at
                else None
            ),
            latency_ms=self._runtime.latency_ms,
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> bool:
        """建连并拉取工具列表；返回是否成功。

        **不抛异常**：调用方（启动流程 / 重载接口）需要的是「成功与否 + 状态」，
        失败细节通过 :attr:`state` 与日志暴露。
        """
        if not self.config.enabled:
            self._state = "disabled"
            self._sync_metric()
            return False
        async with self._lock:
            return await self._connect_locked()

    async def _connect_locked(self) -> bool:
        self._set_state("connecting")
        stack = AsyncExitStack()
        started = time.perf_counter()
        with self._span("mcp.connect", transport=self.config.transport):
            try:
                session = await stack.enter_async_context(self._factory(self.config))
                tools = await asyncio.wait_for(
                    session.list_tools(), timeout=self.config.connect_timeout_seconds
                )
            except asyncio.CancelledError:
                # 关机 / 取消：回收已建资源后原样抛出（不能吞掉取消）
                await _silent_close(stack)
                self._set_state("unavailable", error="cancelled")
                raise
            except Exception as exc:
                await _silent_close(stack)
                elapsed = _elapsed_ms(started)
                self._set_state("unavailable", error=_safe_error(exc))
                logger.warning(
                    "mcp.connect_failed",
                    extra={
                        "server": self.name,
                        "transport": self.config.transport,
                        "elapsed_ms": elapsed,
                        "error_type": type(exc).__name__,
                    },
                )
                return False

            self._stack = stack
            self._session = session
            self._runtime.tools = tools
            self._runtime.last_error = None
            self._runtime.last_connected_at = datetime.now(UTC)
            self._runtime.latency_ms = _elapsed_ms(started)
            self._set_state("connected")
            logger.info(
                "mcp.connected",
                extra={
                    "server": self.name,
                    "transport": self.config.transport,
                    "tools_count": len(tools),
                    "latency_ms": self._runtime.latency_ms,
                    # ``headers`` 一律整体打码（AC-MCP-07）：记录它的**形状**
                    # （有没有配 Authorization）对排障有用，记录内容则等于泄密。
                    "headers": redact_mapping(dict(self.config.headers)),
                },
            )
            return True

    async def close(self) -> None:
        """关闭连接（stdio 会回收子进程）。"""
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await _silent_close(stack)
        if self.config.enabled:
            self._set_state("unavailable", error=self._runtime.last_error)

    def mark_unavailable(self, reason: str) -> None:
        """由管理器在外部中断（启动超时 / 关机）时强制标记状态。

        存在的理由：``connect()`` 被外层超时取消后，客户端自己**没有机会**
        知道自己已经不在连接流程里了（取消点可能在任意 await 上），
        状态会永远停在 ``connecting`` —— 而 ``connecting`` 对 ``/health/ready``
        是「未就绪」，会让健康检查永久 503。
        """
        if self.config.enabled and self._state != "unavailable":
            self._set_state("unavailable", error=reason)

    async def reload(self, *, force: bool = False) -> bool:
        """关闭旧连接并重建（``REQ-MCP-004``）。

        ``force=false`` 且当前仍在 ``connecting`` 时直接返回 —— 幂等（``docs/05`` §5.3），
        不做并发重建。
        """
        if not self.config.enabled:
            return False
        if self._state == "connecting" and not force:
            return False
        await self.close()
        if self._breaker is not None:
            self._breaker.reset()
        return await self.connect()

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------
    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        """调用工具；必要时先懒重连。

        Raises:
            AppError: ``503 MCP_SERVER_UNAVAILABLE``（连不上 / 熔断已打开 /
                调用失败且重连后仍失败）。
        """
        if not self.config.enabled:
            raise AppError(
                ErrorCode.MCP_SERVER_UNAVAILABLE,
                f"MCP Server 已禁用：{self.name}",
                {"server": self.name},
            )
        if self._breaker is not None and not self._breaker.allow():
            self._set_state("unavailable", error="circuit_open")
            raise AppError(
                ErrorCode.MCP_SERVER_UNAVAILABLE,
                f"MCP Server 熔断中：{self.name}",
                {"server": self.name, "reason": "circuit_open"},
                retry_after=max(int(self._breaker.retry_after()), 1),
            )

        with self._span("mcp.call", server=self.name, tool=tool):
            try:
                result = await self._call_with_reconnect(tool, arguments)
            except AppError:
                if self._breaker is not None:
                    self._breaker.record_failure()
                    if self._breaker.state == "open":
                        self._set_state("unavailable", error="circuit_open")
                raise
            # 注意：这里**不能**写成 ``try/except/else`` 里的 ``return``。
            # ``else`` 子句只在 try 块「自然落到末尾」时才执行，而 ``return`` 是
            # 直接开始栈展开 —— 也就是说 ``try: return await ...`` 会让 ``else``
            # **永远不执行**。后果是熔断器只记得住失败、记不住成功：一旦打开，
            # 半开试探成功后永远回不到 closed，该 Server 就永久不可用了。
            if self._breaker is not None:
                self._breaker.record_success()
            return result

    async def _call_with_reconnect(self, tool: str, arguments: dict[str, Any]) -> McpCallResult:
        # 「连接没建立」和「状态不是 connected」是同一件事的两种表现，合并判断：
        # 只要不满足就直接尝试重连，重连失败即抛出（不往下走，避免用 None 会话调用）
        if (self._session is None or self._state != "connected") and not await self._reconnect():
            raise self._unavailable("连接不可用")

        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                _require(self._session).call_tool(tool, arguments),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            self._set_state("unavailable", error="call timeout")
            raise self._unavailable(f"调用超时（{self.config.timeout_seconds:g}s）") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 连接可能在调用过程中断掉：重连一次再试
            logger.warning(
                "mcp.call_failed",
                extra={"server": self.name, "tool": tool, "error_type": type(exc).__name__},
            )
            if not await self._reconnect():
                raise self._unavailable(_safe_error(exc)) from exc
            try:
                result = await asyncio.wait_for(
                    _require(self._session).call_tool(tool, arguments),
                    timeout=self.config.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as retry_exc:
                self._set_state("unavailable", error=_safe_error(retry_exc))
                raise self._unavailable(_safe_error(retry_exc)) from retry_exc

        self._runtime.latency_ms = _elapsed_ms(started)
        if result.is_error:
            # MCP 层的「工具执行失败」：Server 是通的，是这次调用失败。
            # 这不改变连接状态（一个工具坏掉不代表 Server 掉线）。
            logger.info(
                "mcp.tool_error",
                extra={"server": self.name, "tool": tool, "elapsed_ms": self._runtime.latency_ms},
            )
        return _truncate(result, self._max_result_chars)

    async def _reconnect(self) -> bool:
        """按退避序列重连；任一次成功即返回 ``True``。"""
        for delay in RECONNECT_DELAYS:
            await asyncio.sleep(delay)
            await self.close()
            if await self.connect():
                return True
        return False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _unavailable(self, reason: str) -> AppError:
        return AppError(
            ErrorCode.MCP_SERVER_UNAVAILABLE,
            f"MCP Server 不可用：{self.name}",
            {"server": self.name, "reason": redact(reason)[:200]},
        )

    def _set_state(self, state: McpStatus, *, error: str | None = None) -> None:
        self._state = state
        if error is not None:
            # 失败原因可能含路径 / token，落库与日志前必须脱敏且截断
            self._runtime.last_error = redact(error)[:300]
        elif state == "connected":
            self._runtime.last_error = None
        self._sync_metric()

    def _sync_metric(self) -> None:
        get_metrics().set_mcp_server_state(server=self.name, state=_STATUS_TO_METRIC[self.status])

    def _span(self, name: str, **attributes: Any):
        """追踪 span（未启用时是空操作）。"""
        if self._tracing is None:
            return contextlib.nullcontext()
        return self._tracing.span(name, attributes)


def _require(session: McpTransportSession | None) -> McpTransportSession:
    """断言连接存在（``_reconnect`` 成功后必然为真）。"""
    if session is None:  # pragma: no cover - 防御性分支
        raise AppError(ErrorCode.MCP_SERVER_UNAVAILABLE, "MCP 连接未建立")
    return session


def _truncate(result: McpCallResult, max_chars: int) -> McpCallResult:
    """按 ``tool_result_max_chars`` 截断结果文本（``docs/05`` §4）。"""
    if max_chars <= 0 or len(result.text) <= max_chars:
        return result
    return McpCallResult(text=result.text[:max_chars] + "…[truncated]", is_error=result.is_error)


async def _silent_close(stack: AsyncExitStack) -> None:
    """关闭上下文栈，吞掉关闭期的异常（关闭失败不该掩盖原始错误）。"""
    with contextlib.suppress(Exception):
        await stack.aclose()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _safe_error(exc: BaseException) -> str:
    """把异常压成一句可对外展示的原因（脱敏 + 截断）。"""
    text = redact(str(exc)).strip()
    return text[:200] if text else type(exc).__name__


__all__ = [
    "RECONNECT_DELAYS",
    "McpClient",
    "McpServerStatus",
    "McpStatus",
]
