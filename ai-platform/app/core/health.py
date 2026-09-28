"""依赖健康检查（``/health/ready`` 的数据来源）。

语义见 ``docs/10-非功能需求与可观测性.md`` §5.4：

* ``/health/live`` —— 进程在跑即 200，**不检查依赖**（依赖抖动不该导致容器被重启）；
* ``/health/ready`` —— 检查 MySQL / Redis / Milvus 连通性、``required`` MCP Server 状态、
  向量维度校验，任一必需依赖失败返回 503。

设计取舍：
* 检查**并发执行**且**各自带超时**，避免一个依赖卡死拖垮整个探活；
* 检查失败**不上抛**，而是转成 ``ok=false`` 的结果，让响应体成为排障依据；
* ``INFRA_BACKEND=memory``（本地开发）时外部依赖检查标记为 ``skipped``，保持 ready 为绿。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.core.logging import get_logger, redact

logger = get_logger("app.health")

#: 单个检查的超时（docs/10-§3.2 规定 Milvus 查询 3s 超时）
DEFAULT_CHECK_TIMEOUT_SECONDS = 3.0


@dataclass(slots=True)
class CheckResult:
    """单个依赖检查结果。"""

    name: str
    ok: bool
    latency_ms: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        """转为响应体片段。"""
        payload: dict[str, Any] = {"ok": self.ok, "latency_ms": self.latency_ms}
        if self.skipped:
            payload["skipped"] = True
        if self.error:
            payload["error"] = self.error
        payload.update(self.detail)
        return payload


HealthCheck = Callable[[], Awaitable[CheckResult]]


class HealthRegistry:
    """健康检查注册表：依赖可用性检查的统一入口。"""

    def __init__(self, timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS) -> None:
        self._checks: dict[str, HealthCheck] = {}
        self._timeout = timeout_seconds

    def register(self, name: str, check: HealthCheck) -> None:
        """注册（或覆盖）一个检查。"""
        self._checks[name] = check

    def unregister(self, name: str) -> None:
        """移除检查（如 MCP Server 被删配置后）。"""
        self._checks.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        """已注册的检查名（有序，便于稳定输出）。"""
        return tuple(self._checks)

    async def run_one(self, name: str) -> CheckResult:
        """执行单个检查（含超时与异常兜底）。"""
        check = self._checks.get(name)
        if check is None:
            return CheckResult(name, ok=False, error="check not registered")
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(check(), timeout=self._timeout)
        except TimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            return CheckResult(name, ok=False, latency_ms=elapsed, error="timeout")
        except Exception as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "health.check_failed", extra={"check": name, "error_type": type(exc).__name__}
            )
            return CheckResult(
                name,
                ok=False,
                latency_ms=elapsed,
                # 错误信息可能含连接串，必须脱敏且截断
                error=redact(str(exc))[:300] or type(exc).__name__,
            )
        return result

    async def run(self) -> dict[str, dict[str, Any]]:
        """并发执行全部检查，返回 ``{name: 明细}``。"""
        names = list(self._checks)
        if not names:
            return {}
        results = await asyncio.gather(*(self.run_one(name) for name in names))
        return {result.name: result.as_dict() for result in results}

    async def is_ready(self) -> tuple[bool, dict[str, dict[str, Any]]]:
        """返回 ``(是否就绪, 明细)``；``skipped`` 不算失败。"""
        details = await self.run()
        ready = all(item.get("ok") or item.get("skipped") for item in details.values())
        return ready, details


# ----------------------------------------------------------------------
# 内置检查
# ----------------------------------------------------------------------
def _skipped(name: str, reason: str) -> CheckResult:
    return CheckResult(name, ok=True, skipped=True, detail={"reason": reason})


def make_embedding_check(settings: Settings) -> HealthCheck:
    """向量模型配置检查（维度 Mismatch 属启动期致命错误，这里只做自检）。"""

    async def check() -> CheckResult:
        if settings.embedding_dim <= 0:
            return CheckResult("embedding", ok=False, error="EMBEDDING_DIM 必须为正整数")
        return CheckResult(
            "embedding",
            ok=True,
            detail={"dim": settings.embedding_dim, "model": settings.embedding_model},
        )

    return check


def make_storage_check(settings: Settings) -> HealthCheck:
    """存储后端说明（不是真正的连通性检查，用于排障时一眼看清当前后端）。"""

    async def check() -> CheckResult:
        return CheckResult(
            "storage",
            ok=True,
            detail={
                "backend": settings.infra_backend,
                "vector": "milvus" if settings.infra_backend == "real" else "memory",
            },
        )

    return check


def make_milvus_check(settings: Settings) -> HealthCheck:
    """Milvus 连通性检查（``memory`` 后端时跳过）。"""

    async def check() -> CheckResult:
        if settings.infra_backend != "real":
            return _skipped("milvus", "INFRA_BACKEND=memory")

        def probe() -> None:
            from pymilvus import MilvusClient

            client = MilvusClient(**settings.milvus_connection_args)
            try:
                client.list_collections()
            finally:
                client.close()

        started = time.perf_counter()
        await asyncio.to_thread(probe)
        return CheckResult(
            "milvus", ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
        )

    return check


def make_redis_check(settings: Settings) -> HealthCheck:
    """Redis 连通性检查（``memory`` 后端时跳过）。"""

    async def check() -> CheckResult:
        if settings.infra_backend != "real":
            return _skipped("redis", "INFRA_BACKEND=memory")

        from redis.asyncio import Redis

        started = time.perf_counter()
        client = Redis.from_url(settings.redis_url)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return CheckResult("redis", ok=True, latency_ms=int((time.perf_counter() - started) * 1000))

    return check


def make_mysql_check(settings: Settings) -> HealthCheck:
    """MySQL 连通性检查（``memory`` 后端时跳过）。"""

    async def check() -> CheckResult:
        if settings.infra_backend != "real":
            return _skipped("mysql", "INFRA_BACKEND=memory")

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        started = time.perf_counter()
        engine = create_async_engine(settings.mysql_dsn, pool_pre_ping=True)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return CheckResult("mysql", ok=True, latency_ms=int((time.perf_counter() - started) * 1000))

    return check


def build_health_registry(settings: Settings) -> HealthRegistry:
    """按配置装配默认检查集合。

    ``mcp`` 检查由 :mod:`app.mcp.manager` 在启动时注册（它才知道有哪些 Server）。
    """
    registry = HealthRegistry()
    registry.register("storage", make_storage_check(settings))
    registry.register("embedding", make_embedding_check(settings))
    registry.register("milvus", make_milvus_check(settings))
    registry.register("redis", make_redis_check(settings))
    registry.register("mysql", make_mysql_check(settings))
    return registry


__all__ = [
    "CheckResult",
    "HealthCheck",
    "HealthRegistry",
    "build_health_registry",
    "make_embedding_check",
    "make_milvus_check",
    "make_mysql_check",
    "make_redis_check",
    "make_storage_check",
]
