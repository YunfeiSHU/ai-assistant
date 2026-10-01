"""可观测性：指标、链路追踪与熔断（``docs/10`` §5 / §3.2）。

三个子模块的边界刻意与「谁配置、谁使用」对齐：

* :mod:`app.observability.metrics` —— 指标定义与记录；``create_app`` 配置进程级实例，
  深层调用用 :func:`~app.observability.metrics.get_metrics` 取。
* :mod:`app.observability.tracing` —— span 构造；核心设计是**沿用请求上下文里的
  ``trace_id``**，让日志与 Jaeger 看到同一个 id。
* :mod:`app.observability.circuit` —— 进程内熔断器（无额外依赖）。

:mod:`app.observability.server` 只在 ``METRICS_PORT > 0`` 时启用。
"""

from __future__ import annotations

from app.observability.circuit import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
)
from app.observability.metrics import Metrics, configure_metrics, get_metrics
from app.observability.server import MetricsServer
from app.observability.tracing import (
    Tracing,
    configure_tracing,
    get_tracing,
    setup_tracing,
    shutdown_tracing,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitRegistry",
    "Metrics",
    "MetricsServer",
    "Tracing",
    "configure_metrics",
    "configure_tracing",
    "get_metrics",
    "get_tracing",
    "setup_tracing",
    "shutdown_tracing",
]
