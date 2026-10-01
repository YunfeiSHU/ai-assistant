"""多个 MCP Server 的连接编排（``REQ-MCP-002`` / ``REQ-MCP-003`` / ``REQ-MCP-004``）。

管理器只做四件事：并发建连 + 总超时（``REQ-MCP-002``）、``required`` 失败则启动失败
（``AC-MCP-02``）、非 ``required`` 失败则降级启动（``AC-MCP-01``，服务照常起、状态
``unavailable``）、按名字重载单个 Server（``AC-MCP-05``，不影响其它 Server）。

**建连必须带「总超时」而不只是「每个 Server 各自超时」**：启动阶段是串行阻塞的（uvicorn 在
``lifespan`` 返回前不接受请求），N 个 Server 各等 10s 会让启动时间随数量线性增长；
``MCP_STARTUP_TIMEOUT`` 是整体预算，超了就全部按失败处理并继续启动。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

from app.core.exceptions import AppError, ErrorCode
from app.core.health import CheckResult, HealthCheck
from app.core.logging import get_logger
from app.infrastructure.observability.circuit import CircuitRegistry
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import Tracing
from app.mcp.client import McpClient, McpServerStatus
from app.mcp.config import McpServerConfig
from app.mcp.session import McpToolDef, SessionFactory

logger = get_logger("app.mcp")

#: 关闭阶段的宽限时间：先优雅关，超时后强制取消（``AC-NFR-12``）
SHUTDOWN_GRACE_SECONDS = 5.0

#: 默认启动总超时（``docs/05`` §2.2 的 ``MCP_STARTUP_TIMEOUT`` 默认值）
DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0


class McpManager:
    """所有已配置 MCP Server 的集合。"""

    def __init__(
        self,
        servers: dict[str, McpServerConfig],
        *,
        session_factory: SessionFactory | None = None,
        circuits: CircuitRegistry | None = None,
        tracing: Tracing | None = None,
        max_result_chars: int = 4000,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._circuits = circuits
        self._tracing = tracing
        self._startup_timeout = startup_timeout_seconds
        self._clients: dict[str, McpClient] = {
            name: McpClient(
                name,
                config,
                session_factory=session_factory,
                breaker=circuits.get(f"mcp:{name}") if circuits is not None else None,
                max_result_chars=max_result_chars,
                tracing=tracing,
            )
            for name, config in servers.items()
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def __contains__(self, name: object) -> bool:
        return name in self._clients

    @property
    def names(self) -> list[str]:
        """全部已配置的 Server 名（按配置顺序）。"""
        return list(self._clients)

    def clients(self) -> Iterator[McpClient]:
        """遍历全部客户端。"""
        return iter(self._clients.values())

    def statuses(self) -> list[McpServerStatus]:
        """全部 Server 的状态快照（``GET /mcp/servers`` 用）。"""
        return [client.snapshot() for client in self._clients.values()]

    def get(self, name: str) -> McpClient:
        """按名字取客户端。

        Raises:
            AppError: ``404 MCP_SERVER_NOT_FOUND``
        """
        client = self._clients.get(name)
        if client is None:
            raise AppError(
                ErrorCode.MCP_SERVER_NOT_FOUND,
                f"MCP Server 未配置：{name}",
                {"server": name, "configured": self.names[:20]},
            )
        return client

    def iter_tools(self) -> Iterator[tuple[McpClient, McpToolDef]]:
        """遍历「当前可用」的 Server 及其**已通过过滤**的工具。

        只产出 ``connected`` 的 Server：``unavailable`` 时的工具定义是上次连接的残留，
        注册出去会让模型调用一个必然失败的工具。
        """
        for client in self._clients.values():
            if client.status != "connected":
                continue
            for tool in client.select_tools():
                yield client, tool

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        """并发建连。

        Raises:
            AppError: 存在 ``required=true`` 且未连上的 Server（``AC-MCP-02``）。
        """
        if not self._clients:
            logger.info("mcp.no_servers", extra={"configured": 0})
            return
        clients = list(self._clients.values())
        results = await _gather_with_timeout(
            [client.connect() for client in clients], timeout=self._startup_timeout
        )
        for client, ok in zip(clients, results, strict=True):
            if not ok and client.status == "connecting":
                # 外层总超时导致任务被取消：必须显式落状态，否则永远是 connecting
                client.mark_unavailable("startup timeout")

        connected = [c for c in clients if c.status == "connected"]
        failed = [c for c in clients if c.status not in {"connected", "disabled"}]
        logger.info(
            "mcp.startup_done",
            extra={
                "configured": len(clients),
                "connected": len(connected),
                "failed": len(failed),
                "servers": [c.name for c in connected][:20],
            },
        )
        if failed:
            # 失败只记名字与原因，不打整份配置（可能含密钥引用）
            logger.warning(
                "mcp.servers_unavailable",
                extra={
                    "servers": [
                        {
                            "server": c.name,
                            "required": c.config.required,
                            "error": c.snapshot().last_error,
                        }
                        for c in failed
                    ][:20]
                },
            )

        blocking = [c for c in failed if c.config.required]
        if blocking:
            names = ", ".join(c.name for c in blocking)
            raise AppError(
                ErrorCode.MCP_SERVER_UNAVAILABLE,
                f"必需的 MCP Server 连接失败：{names}",
                {
                    "servers": [
                        {"server": c.name, "error": c.snapshot().last_error} for c in blocking
                    ]
                },
            )

    async def shutdown(self) -> None:
        """关闭全部连接；超时后强制取消（``AC-NFR-12``）。"""
        if not self._clients:
            return
        await _gather_with_timeout(
            [client.close() for client in self._clients.values()], timeout=SHUTDOWN_GRACE_SECONDS
        )
        for client in self._clients.values():
            get_metrics().forget_mcp_server(client.name)
        logger.info("mcp.shutdown", extra={"servers": len(self._clients)})

    async def reload(self, name: str, *, force: bool = False) -> McpServerStatus:
        """重载单个 Server（``AC-MCP-05``）。

        Raises:
            AppError: ``404 MCP_SERVER_NOT_FOUND`` / ``503 MCP_SERVER_UNAVAILABLE``
        """
        client = self.get(name)
        if not client.config.enabled:
            raise AppError(
                ErrorCode.MCP_SERVER_UNAVAILABLE,
                f"MCP Server 已禁用：{name}",
                {"server": name, "status": client.status},
            )
        ok = await client.reload(force=force)
        if not ok:
            raise AppError(
                ErrorCode.MCP_SERVER_UNAVAILABLE,
                f"MCP Server 重载失败：{name}",
                {"server": name, "reason": client.snapshot().last_error},
            )
        logger.info(
            "mcp.reloaded",
            extra={"server": name, "tools_count": client.tools_count, "force": force},
        )
        return client.snapshot()

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_detail(self) -> dict[str, Any]:
        """``/health/ready`` 的 ``checks.mcp`` 明细（``AC-MCP-06``）。"""
        servers = {
            client.name: {
                "status": client.status,
                "required": client.config.required,
                "tools_count": client.tools_count,
                "last_error": client.snapshot().last_error,
            }
            for client in self._clients.values()
        }
        required_failed = [
            name
            for name, detail in servers.items()
            if detail["required"] and detail["status"] != "connected"
        ]
        return {
            "available": len(servers),
            "connected": sum(1 for d in servers.values() if d["status"] == "connected"),
            "required_unavailable": required_failed,
            "servers": servers,
        }

    def is_ready(self) -> bool:
        """``required`` 的 Server 是否全部连上。"""
        return not self.health_detail()["required_unavailable"]


async def _gather_with_timeout(awaitables: list[Any], *, timeout: float) -> list[Any]:
    """并发执行并把**总耗时**限制在 ``timeout`` 内；返回每个任务的成功标志。

    ``asyncio.gather(return_exceptions=True)`` + ``wait_for``：超时时未完成的任务被取消，
    已完成任务的结果保留 —— 「部分成功」正是这里想要的语义。
    """
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    if not tasks:
        return []
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning(
            "mcp.startup_timeout", extra={"timeout_seconds": timeout, "tasks": len(tasks)}
        )
        return [
            bool(task.done() and not task.cancelled() and task.exception() is None)
            for task in tasks
        ]
    return [not isinstance(result, BaseException) for result in results]


def make_mcp_check(manager: McpManager) -> HealthCheck:
    """构造 ``/health/ready`` 的 ``mcp`` 检查（``REQ-MCP-005`` / ``AC-MCP-06``）。

    判定口径：**只有 ``required=true`` 的 Server 未连上才算失败**。非必需的 Server 掉线是
    预期内的降级（``AC-MCP-01`` 就是这么定义的），把它算成不健康会让整个服务在「某个可选
    工具挂了」时被摘出负载均衡。

    没有配置任何 Server 时返回 ``skipped`` —— 「没配 MCP」不是故障。
    """

    async def check() -> CheckResult:
        detail = manager.health_detail()
        if not detail["available"]:
            return CheckResult("mcp", ok=True, skipped=True, detail={"reason": "未配置 MCP Server"})
        blocking: list[str] = detail["required_unavailable"]
        return CheckResult(
            "mcp",
            ok=not blocking,
            error=f"必需的 MCP Server 不可用：{', '.join(blocking)}" if blocking else None,
            detail=detail,
        )

    return check


__all__ = [
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "SHUTDOWN_GRACE_SECONDS",
    "McpManager",
    "make_mcp_check",
]
