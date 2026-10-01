"""分布式链路追踪（``REQ-NFR-010``，契约见 ``docs/10`` §5.1）。

**本模块的核心设计：让日志里的 ``trace_id`` 与 Jaeger 里的 trace id 是同一个值。**

项目已有一套轻量请求上下文（``app/core/context.py``），日志、错误信封、``X-Trace-Id`` 响应头
都读它。若再让 OTel 自己生成一个 trace id，就会出现「日志说 A、Jaeger 说 B」，排障时必须在
两套 id 之间人工对照 —— 而这正是 ``REQ-GEN-006``「任一请求可按 ``trace_id`` 串起全链路」
要避免的事。做法：把上下文里的 ``trace_id`` / ``span_id`` 包装成 OTel 的 ``SpanContext``，
作为**远端父上下文**传给根 span，于是 OTel 会沿用我们的 trace id，子 span 自动归到同一棵树上。

依赖缺失或 ``OTEL_ENABLED=false`` 时全部退化为空操作：``opentelemetry-api`` 在没有 provider
时本身就会返回 non-recording span，无需另写一套空实现。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any, Final

from app.core.context import get_span_id, get_trace_id
from app.core.logging import get_logger

logger = get_logger("app.tracing")

#: 服务名兜底值
DEFAULT_SERVICE_NAME: Final[str] = "ai-platform"


class Tracing:
    """追踪门面。

    只暴露两件事：``request_span``（根 span，沿用上下文里的 id）与 ``span``（业务子 span）。
    刻意不暴露 provider / exporter —— 那是配置层的事。
    """

    def __init__(self, *, enabled: bool, service_name: str = DEFAULT_SERVICE_NAME) -> None:
        self._enabled = enabled
        self._service_name = service_name
        self._tracer: Any = None
        if not enabled:
            return
        try:
            from opentelemetry import trace
        except Exception:  # pragma: no cover - 仅在依赖缺失的环境触发
            logger.warning("tracing.disabled", extra={"reason": "opentelemetry-api 未安装"})
            self._enabled = False
            return
        self._tracer = trace.get_tracer(service_name)

    @property
    def enabled(self) -> bool:
        """追踪是否生效。"""
        return self._enabled

    # ------------------------------------------------------------------
    # span
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def request_span(
        self,
        name: str,
        *,
        trace_id: str,
        span_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        """根 span：**沿用上下文里的 trace_id / span_id**。

        这样 Jaeger 里的 trace id 与日志 / ``X-Trace-Id`` 完全一致。
        id 非法（如上下文未安装）时退化成普通 span（新起一条 trace）。
        """
        with self._start(name, attributes, parent=(trace_id, span_id)):
            yield

    @contextlib.contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        """业务子 span；自动挂在当前 span 下。"""
        with self._start(name, attributes, parent=None):
            yield

    @contextlib.contextmanager
    def _start(
        self,
        name: str,
        attributes: dict[str, Any] | None,
        *,
        parent: tuple[str, str] | None,
    ) -> Iterator[None]:
        if not self._enabled:
            yield
            return
        from opentelemetry import trace

        context = None
        if parent is not None:
            remote = _remote_context(*parent)
            if remote is not None:
                context = remote
        with self._tracer.start_as_current_span(name, context=context) as span:
            for key, value in (attributes or {}).items():
                if value is not None:
                    span.set_attribute(key, value)
            if trace.get_current_span() is not span:  # pragma: no cover - 理论上不可能
                logger.debug("tracing.span_mismatch", extra={"span": name})
            yield


def _remote_context(trace_id: str, span_id: str) -> Any | None:
    """把 ``(trace_id, span_id)`` 十六进制串包装成 OTel 父上下文。

    非法或全零的 id 返回 ``None``（调用方退化为新建 trace）—— 与
    :func:`app.core.context.parse_traceparent` 的判定保持一致。
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
    except Exception:  # pragma: no cover - 依赖缺失
        return None
    if len(trace_id) != 32 or len(span_id) != 16:
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    try:
        span_context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except (ValueError, TypeError):  # pragma: no cover - 非法十六进制
        return None
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def setup_tracing(settings: Any) -> Tracing:
    """按配置初始化追踪；返回门面对象。

    只在 ``OTEL_ENABLED=true`` 时构造 provider 与导出器。导出器**失败不阻断**：collector
    没起来不该让服务起不来（与 Milvus 探测同一口径，见 ``app/main.py``）。

    OTel 的 ``TracerProvider`` 是**进程级单例**，``set_tracer_provider`` 只允许设置一次（第二次
    会被忽略）。一个进程里创建多个应用（测试常态）时，这里**复用**已装入的 provider 并只补一次
    导出器 —— 否则除了第一个应用之外，其余应用的 span 都会被静默丢弃。
    """
    if not settings.otel_enabled:
        return Tracing(enabled=False, service_name=settings.otel_service_name)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as exc:  # pragma: no cover - 仅在依赖缺失的环境触发
        logger.warning("tracing.disabled", extra={"reason": type(exc).__name__})
        return Tracing(enabled=False, service_name=settings.otel_service_name)

    target = _resolve_provider(trace, TracerProvider, settings)
    if not getattr(target, _EXPORTER_MARKER, False):
        try:
            exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
            target.add_span_processor(BatchSpanProcessor(exporter))
            setattr(target, _EXPORTER_MARKER, True)
        except Exception as exc:  # pragma: no cover - 建导出器失败（地址非法等）
            logger.warning("tracing.exporter_failed", extra={"error": type(exc).__name__})
    logger.info(
        "tracing.enabled",
        extra={
            "endpoint": settings.otel_exporter_otlp_endpoint,
            "service": settings.otel_service_name,
            "sampler_arg": settings.otel_traces_sampler_arg,
        },
    )
    return Tracing(enabled=True, service_name=settings.otel_service_name)


def _resolve_provider(trace: Any, provider_type: Any, settings: Any) -> Any:
    """取得本进程要用的 ``TracerProvider``（首次创建，之后复用）。"""
    global _provider_owned
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    existing = trace.get_tracer_provider()
    if isinstance(existing, provider_type):
        logger.debug("tracing.provider_reused", extra={"service": settings.otel_service_name})
        return existing
    provider = provider_type(
        resource=Resource.create({"service.name": settings.otel_service_name}),
        sampler=ParentBased(TraceIdRatioBased(float(settings.otel_traces_sampler_arg))),
    )
    trace.set_tracer_provider(provider)
    _provider_owned = True
    return provider


def shutdown_tracing() -> None:
    """刷出未发送的 span（``SIGTERM`` 优雅退出时调用）。

    必须调用：``BatchSpanProcessor`` 是异步批量发送的，进程退出时未刷出的 span 会**静默丢失**
    —— 表现为「链路上少了最后几秒的请求」，而且没有任何错误信息。

    只关闭**本进程创建**的那个 provider：一个进程里多个应用实例时复用同一个 provider，任何一个
    实例退出都把它关掉，会让余下的实例再也发不出 span。
    """
    global _provider_owned
    if not _provider_owned:
        return
    try:
        from opentelemetry import trace
    except Exception:  # pragma: no cover - 依赖缺失
        return
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        with contextlib.suppress(Exception):  # 刷出失败不该阻断退出
            shutdown()
    _provider_owned = False


#: 进程级当前门面（与 ``app.infrastructure.observability.metrics`` 的 ``_active`` 同一套约定）
_active: Tracing | None = None

#: 装在 ``TracerProvider`` 上的「导出器已挂好」标记（复用 provider 时避免重复挂）
_EXPORTER_MARKER = "_ai_platform_exporter_attached"

#: 本进程是否由我们创建了 provider（决定退出时该不该关它）
_provider_owned: bool = False


def configure_tracing(tracing: Tracing) -> None:
    """设置进程级门面（``create_app`` 调用）。"""
    global _active
    _active = tracing


def get_tracing() -> Tracing:
    """取进程级门面；未配置时返回一个「关闭」的门面（不是 ``None``）。

    返回空对象而不是 ``None``，让调用点写 ``get_tracing().span(...)`` 即可，不必到处写
    ``if tracing is not None`` —— 少一类分支就少一处漏判。
    """
    global _active
    if _active is None:
        _active = Tracing(enabled=False)
    return _active


def current_ids() -> tuple[str, str]:
    """返回 ``(trace_id, span_id)``（供无法拿到请求对象的深层代码使用）。"""
    return get_trace_id(), get_span_id()


__all__ = [
    "DEFAULT_SERVICE_NAME",
    "Tracing",
    "configure_tracing",
    "current_ids",
    "get_tracing",
    "setup_tracing",
    "shutdown_tracing",
]
