"""``python -m app.grpc`` —— 启动 AI 侧 gRPC 服务。

独立于 uvicorn 的进程入口。之所以不做成「HTTP 启动时顺带开一个 gRPC 端口」：
两者的生命周期、并发模型与故障域都不同（gRPC 挂了不该影响 HTTP 的 SSE 流），
混在一个进程里会让「网关连不上 AI 的 gRPC」这种问题变成「AI 服务整个挂了」。
"""

from __future__ import annotations

import asyncio
import sys

from app.core.config import ConfigurationError, get_settings
from app.core.logging import get_logger, setup_logging
from app.grpc.server import GrpcOptions, create_grpc_application, serve


def main(argv: list[str] | None = None) -> int:
    """进程入口；返回退出码。"""
    _ = argv
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        # 配置错误连日志系统都还没配好，只能写 stderr。
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    setup_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service=settings.otel_service_name,
        pepper=settings.api_key_pepper,
    )
    logger = get_logger("app.grpc")

    if not settings.grpc_enabled:
        # 显式报错而不是静默退出：``python -m app.grpc`` 是手动敲的命令，
        # 静默退出会让人以为「启动了但没日志」。
        logger.error(
            "grpc.disabled",
            extra={"hint": "需要 GRPC_ENABLED=true 才会监听端口", "upstream_trace_id": "-"},
        )
        return 2

    try:
        # startup 校验在 lifespan 里也会跑一次，但这里提前跑是刻意的：
        # 配置错误应当在**建应用之前**就退出，否则会看到一堆
        # 「依赖连不上」的噪音日志，把真正的原因埋在中间。
        settings.validate_for_startup()
    except ConfigurationError as exc:
        logger.error("grpc.invalid_config", extra={"error": str(exc), "upstream_trace_id": "-"})
        return 2

    application = create_grpc_application(settings)
    options = GrpcOptions.from_settings(settings)
    logger.info(
        "grpc.booting",
        extra={
            "address": options.address,
            "app_env": settings.app_env,
            "auth_required": settings.auth_required,
            "infra_backend": settings.infra_backend,
        },
    )
    try:
        asyncio.run(serve(application, options))
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        logger.info("grpc.interrupted", extra={"upstream_trace_id": "-"})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
