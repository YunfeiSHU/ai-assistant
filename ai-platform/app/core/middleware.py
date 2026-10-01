"""ASGI 中间件：请求上下文（trace / request id / 访问日志）与请求体大小限制。

刻意用**纯 ASGI 中间件**而非 ``BaseHTTPMiddleware``：后者会把响应包成
``anyio`` 的内存流，SSE 场景下可能被整段缓冲，直接违背「逐 token 推送」的需求
（docs/02 §6.4 明确要求关闭应用层缓冲）。
"""

from __future__ import annotations

import time
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    get_user_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    request_context,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger, hash_identifier
from app.infrastructure.observability.metrics import Metrics, get_metrics
from app.infrastructure.observability.tracing import Tracing, get_tracing

logger = get_logger("app.access")

#: 这些路径不写访问日志（探活/指标高频且无业务价值）
_QUIET_PATHS = ("/health", "/metrics", "/favicon.ico")

#: 没匹配到路由时的指标标签值。
#: **绝不能**用原始 path 当标签：``/kb/{id}`` 之类的路径变量会让标签基数
#: 随数据量增长，Prometheus 的内存会被拖垮（``docs/10`` §5.2）。
UNMATCHED_ENDPOINT = "unmatched"

#: FastAPI 存放「有效路由上下文」的 scope 键（不是公开 API，见 ``route_template``）
_FASTAPI_SCOPE_KEY = "fastapi"
_FASTAPI_EFFECTIVE_CONTEXT_KEY = "effective_route_context"


def route_template(scope: Scope) -> str:
    """还原命中路由的**完整模板**，如 ``/api/v1/knowledge-bases/{kb_id}``。

    为什么不能直接用 ``scope["route"].path``：FastAPI 0.141 起 ``include_router``
    **不再把子路由扁平化**进父路由表（内部放的是 ``_IncludedRouter`` 节点），
    于是 ``scope["route"]`` 是**最内层**那条路由，各层前缀全被丢掉。
    实测 ``GET /api/v1/health/live`` 用旧写法会得到标签 ``GET /live`` ——
    不同模块下同名的 ``/live`` 会撞进同一个标签，看板上根本分不出是哪个接口。

    完整路径只存在于 FastAPI 的「有效路由上下文」里
    （``scope["fastapi"]["effective_route_context"].path``，形如
    ``/api/v1/knowledge-bases/{kb_id}``，占位符没有被实例值替换）。这两个键名都
    不是公开 API，所以这里**逐级降级**，任何一级失败都不会影响请求：

    1. 有效路由上下文 —— 嵌套 ``include_router`` 时唯一含完整前缀的来源；
    2. ``scope["route"].path`` —— 未嵌套注册时它本身就是完整模板；
    3. :data:`UNMATCHED_ENDPOINT` —— 404 等没命中任何路由的情况。
    """
    fastapi_scope = scope.get(_FASTAPI_SCOPE_KEY)
    if isinstance(fastapi_scope, dict):
        context = fastapi_scope.get(_FASTAPI_EFFECTIVE_CONTEXT_KEY)
        path = getattr(context, "path", None)
        if isinstance(path, str) and path:
            return path
    route_path = getattr(scope.get("route"), "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return UNMATCHED_ENDPOINT


class RequestContextMiddleware:
    """为每个 HTTP 请求安装 ``trace_id`` / ``span_id`` / ``request_id``，并记录访问日志。

    ``metrics`` / ``tracing`` 允许注入：一个进程里可能同时存在多个应用实例
    （测试常态），而模块级单例只记得最后一个。不注入时回退到进程级门面。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        service: str = "ai-platform",
        pepper: str = "",
        metrics: Metrics | None = None,
        tracing: Tracing | None = None,
    ) -> None:
        self.app = app
        self.service = service
        self.pepper = pepper
        self._metrics = metrics
        self._tracing = tracing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = (headers.get("x-request-id") or "").strip() or new_id("req")

        parsed = parse_traceparent(headers.get("traceparent"))
        if parsed is not None:
            trace_id, _parent_span = parsed
        else:
            trace_id = new_trace_id()
        span_id = new_span_id()

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        endpoint = f"{method} {path}"
        status_code = 500
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                raw_headers = MutableHeaders(scope=message)
                raw_headers["X-Request-Id"] = request_id
                raw_headers["X-Trace-Id"] = trace_id
            await send(message)

        # 根 span 沿用上下文里的 id（见 Tracing.request_span）——
        # Jaeger 里的 trace 与日志 / 响应头的 trace_id 必须是同一个值
        with (
            request_context(trace_id, span_id, request_id, endpoint),
            (self._tracing or get_tracing()).request_span(
                endpoint,
                trace_id=trace_id,
                span_id=span_id,
                attributes={
                    "http.method": method,
                    "http.path": path,
                    "user_id": hash_identifier(get_user_id(), self.pepper),
                },
            ),
        ):
            try:
                await self.app(scope, receive, send_with_headers)
            except Exception:
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                self._observe(scope, method, status_code or 500, started)
                logger.exception(
                    "http.request.failed",
                    extra={
                        "method": method,
                        "path": path,
                        "elapsed_ms": elapsed,
                        "status_code": status_code,
                    },
                )
                raise
            else:
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                self._observe(scope, method, status_code, started)
                log = logger.debug if path.startswith(_QUIET_PATHS) else logger.info
                log(
                    "http.request",
                    extra={
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "elapsed_ms": elapsed,
                        "user_id": hash_identifier(get_user_id(), self.pepper),
                    },
                )

    def _observe(self, scope: Scope, method: str, status_code: int, started: float) -> None:
        """记一次 HTTP 指标（``ai_requests_total`` / ``ai_request_duration_seconds``）。

        ``endpoint`` 标签取**路由模板**而非原始路径（见 :func:`route_template`），
        否则 ``/kb/{kb_id}`` 这类路径会按数据量产生无穷多个标签值。
        """
        template = route_template(scope)
        (self._metrics or get_metrics()).observe_request(
            endpoint=f"{method} {template}",
            method=method,
            status=status_code,
            seconds=time.perf_counter() - started,
        )


class BodySizeLimitMiddleware:
    """限制 JSON 请求体大小（超限早失败，不进业务逻辑）。

    规范见 docs/10-§4「请求体大小上限 MUST 由中间件限制（JSON 2 MB，multipart 走高
    ``upload_max_mb``）」。multipart 由上传接口自身校验，这里跳过。
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_type = (headers.get("content-type") or "").lower()
        if content_type.startswith("multipart/"):
            await self.app(scope, receive, send)
            return

        declared = headers.get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = False
            if too_large:
                await self._reject(scope, receive, send)
                return

        # 未来得及声明 Content-Length（分块传输）时按实收字节兜底计数
        received = 0
        exceeded = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
            return message

        sender: Send = send

        async def guarded_send(message: Message) -> None:
            # 一旦超限且响应尚未开始，就改写成 413
            if exceeded and message["type"] == "http.response.start":
                await self._reject(scope, receive, send)
                return
            await sender(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        """返回 413 错误信封。"""
        from app.core.context import get_trace_id

        too_large = AppError(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"请求体超过上限 {self.max_bytes} 字节",
            {"max_bytes": self.max_bytes},
        )
        logger.warning(
            "http.body_too_large",
            extra={"path": scope.get("path", ""), "max_bytes": self.max_bytes},
        )
        response = JSONResponse(
            too_large.to_envelope(get_trace_id()), status_code=too_large.status_code
        )
        await response(scope, receive, send)


def install_middlewares(
    app: Any,
    *,
    max_json_body_bytes: int,
    service: str,
    pepper: str,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
) -> None:
    """安装中间件。

    顺序很重要：``RequestContextMiddleware`` MUST 最外层（最后添加），
    这样 413 / 500 的响应也能带上 ``X-Request-Id`` 与 ``trace_id``。
    """
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_json_body_bytes)
    app.add_middleware(
        RequestContextMiddleware, service=service, pepper=pepper, metrics=metrics, tracing=tracing
    )


__all__ = [
    "UNMATCHED_ENDPOINT",
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "install_middlewares",
]
