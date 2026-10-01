"""Worker 进程入口：``uv run python -m app.worker``。

与 API 进程的三点差异，都是刻意的：

* **不导出 HTTP 接口**：Worker 只消费消息（``docs/08`` §5.4「独立进程」）。
  它的指标服务仍然会起 —— 但 ``METRICS_PORT`` 与 API 相同，所以运维必须给
  Worker 一个**不同的端口**（见下方启动日志里的告警）。
* **不跑补偿扫描**：补偿扫描属于「建任务的那一方」（API），Worker 重复跑一遍
  只会把同一个任务多投几次。
* **任务存储必须是共享的**：内存任务表下 Worker 拿到消息也查不到任务行，
  那就是一条条「任务不存在」的日志 —— 直接拒绝启动，不留这种只能靠日志
  猜的配置组合。
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging
from app.infrastructure.observability import (
    Metrics,
    MetricsServer,
    configure_metrics,
    configure_tracing,
    setup_tracing,
    shutdown_tracing,
)
from app.llm.openai_compat import OpenAICompatLLM
from app.main import build_memory_services, build_rag_services
from app.memory import build_conversation_store
from app.tasks.events import build_task_event_bus, make_publisher
from app.tasks.store import build_task_store
from app.worker.loop import build_worker

logger = get_logger("app.worker")

#: 配置不满足 Worker 运行前提时的退出码（非 0，便于编排系统重试/告警）
EXIT_MISCONFIGURED = 2


def _install_signal_handlers(stop: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    """把 SIGTERM/SIGINT 变成 ``stop`` 置位（``docs/08`` §5.4 优雅退出）。

    Windows 上事件循环不支持 ``add_signal_handler``（会抛 ``NotImplementedError``），
    退回 ``signal.signal``。两条路径都必须有：前者能安全地在事件循环里回调，
    后者是 Windows 唯一的办法。
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: stop.set())


async def run_worker(settings: Settings) -> int:
    """跑一个 Worker 直到收到停止信号，返回进程退出码。"""
    setup_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service=f"{settings.otel_service_name}-worker",
        pepper=settings.api_key_pepper,
    )
    metrics = Metrics(enabled=settings.metrics_enabled, service=settings.otel_service_name)
    configure_metrics(metrics)
    configure_tracing(setup_tracing(settings))

    if not settings.uses_shared_task_store:
        logger.error(
            "worker.misconfigured",
            extra={
                "reason": "任务存储不是共享的（INFRA_BACKEND=memory）",
                "hint": "Worker 与 API 必须是两个进程，请用 INFRA_BACKEND=real（Redis）",
            },
        )
        return EXIT_MISCONFIGURED
    if settings.task_runner != "kafka":
        logger.warning(
            "worker.runner_not_kafka",
            extra={
                "task_runner": settings.task_runner,
                "hint": "Worker 通常配合 TASK_RUNNER=kafka",
            },
        )

    stop = asyncio.Event()
    _install_signal_handlers(stop, asyncio.get_running_loop())

    store = build_task_store(settings)
    bus = build_task_event_bus(settings)
    # 与 API 走**同一段装配**：处理器集合、向量库实例、embedding 提供者的构造
    # 方式完全一致，避免「Worker 里少了某个 handler」这类只在上线后才暴露的偏差。
    rag = build_rag_services(settings, task_store=store, events=make_publisher(bus))
    memory = build_memory_services(
        settings,
        llm=OpenAICompatLLM(settings),
        store=build_conversation_store(settings),
        embedding=rag["embedding"],
        tasks=rag["tasks"],
        dispatcher=rag["dispatcher"],
    )
    worker = build_worker(
        settings,
        tasks=rag["tasks"],
        dispatcher=rag["dispatcher"],
    )
    metrics_server = MetricsServer(metrics, host=settings.metrics_host, port=settings.metrics_port)
    if settings.metrics_enabled:
        started = await metrics_server.start()
        logger.info(
            "worker.metrics_ready",
            extra={
                "host": settings.metrics_host,
                "port": metrics_server.port if started else 0,
                "note": "与 API 同机部署时请显式设置不同的 METRICS_PORT",
            },
        )
    logger.info(
        "worker.bootstrap",
        extra={
            "infra_backend": settings.infra_backend,
            "task_runner": settings.task_runner,
            "task_types": rag["dispatcher"].types,
            "memory_handlers": type(memory["handlers"]).__name__,
            "group": settings.kafka_group_id,
        },
    )
    try:
        await worker.run(stop)
    finally:
        await metrics_server.stop()
        shutdown_tracing()
        closer = getattr(rag["tasks"], "close", None)
        if callable(closer):
            await _maybe_await(closer())
        closer = getattr(store, "close", None)
        if callable(closer):
            await _maybe_await(closer())
        closer = getattr(bus, "close", None)
        if callable(closer):
            await _maybe_await(closer())
    return 0


async def _maybe_await(value: Any) -> None:
    """``close()`` 在两种实现上可能返回 coroutine 也可能返回 ``None``。"""
    if hasattr(value, "__await__"):
        await value


def main() -> int:
    """同步入口（``python -m app.worker``）。"""
    settings = get_settings()
    try:
        settings.validate_for_startup()
    except Exception as exc:
        print(f"配置校验失败：{exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    try:
        return asyncio.run(run_worker(settings))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
