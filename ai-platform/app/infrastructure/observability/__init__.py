"""可观测性：指标、链路追踪与熔断（``docs/10`` §5 / §3.2）。

三个子模块的边界与「谁配置、谁使用」对齐：``metrics`` 负责指标定义与记录（``create_app`` 配置
进程级实例，深层调用用 ``get_metrics`` 取）；``tracing`` 负责 span 构造，核心设计是**沿用请求上
下文里的 ``trace_id``**，让日志与 Jaeger 看到同一个 id；``circuit`` 是进程内熔断器（无额外依赖）。

``server`` 只在 ``METRICS_PORT > 0`` 时启用。
"""

from __future__ import annotations

from app.infrastructure.observability.circuit import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
)
from app.infrastructure.observability.metrics import Metrics, configure_metrics, get_metrics
from app.infrastructure.observability.server import MetricsServer
from app.infrastructure.observability.tracing import (
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
