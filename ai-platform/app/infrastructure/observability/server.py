"""独立指标端口（``METRICS_PORT``，默认 9100）。

契约见 ``docs/10`` §5.2：「暴露方式：``GET /metrics`` 或独立端口 ``METRICS_PORT``。
默认启用独立端口，避免 ``/metrics`` 被 JWT 中间件拦住」。

本实现**只走独立端口**，不把 ``GET /metrics`` 挂到主应用上。原因就是文档那句：
抓取方（Prometheus）不带 JWT，把指标开在主应用上等于要么放宽鉴权、要么给抓取方
配令牌，两条路都比「另开一个端口」更麻烦；而放宽鉴权还会顺带泄漏路由清单与流量画像。

为什么自己写这 60 行而不用 ``prometheus_client.start_http_server``：那个版本在后台
线程里跑 WSGI 且**拿不到 server 对象**，进程退出时无法优雅关闭 —— 测试里会留下
「端口被占用」的间歇性失败。自己实现可以用 asyncio 原生 server，启停对称。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

from app.core.logging import get_logger
from app.observability.metrics import Metrics

logger = get_logger("app.metrics.server")

#: 单个请求的最大头部体积（指标端口只接受 GET，给个很小的上限即可）
MAX_HEADER_BYTES: Final[int] = 8192
#: 单个连接的读取超时
READ_TIMEOUT_SECONDS: Final[float] = 5.0


class MetricsServer:
    """极简 HTTP 服务器：只服务 ``GET /metrics``。"""

    def __init__(self, metrics: Metrics, *, host: str = "0.0.0.0", port: int = 9100) -> None:
        self._metrics = metrics
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        """实际监听端口（``port=0`` 时为内核分配的值）。"""
        server = self._server
        # ``asyncio.AbstractServer`` 协议上没有 ``sockets``（只有具体实现有），
        # 所以这里做能力探测而不是断言类型。
        sockets = getattr(server, "sockets", None) if server is not None else None
        if sockets:
            return int(sockets[0].getsockname()[1])
        return self._port

    @property
    def running(self) -> bool:
        """是否正在监听。"""
        return self._server is not None

    async def start(self) -> bool:
        """开始监听，返回是否成功；绑定失败**不抛异常**。

        ``port=0`` 表示由内核分配一个空闲端口（标准语义，测试用）；
        「要不要开这个端口」由装配方（``create_app`` 的 lifespan）决定，
        这里只负责服务，不负责判断该不该服务。

        绑定失败（端口被占）只记 warning：指标端口是观测手段，不该让服务起不来。
        """
        try:
            self._server = await asyncio.start_server(self._handle, self._host, self._port)
        except OSError as exc:
            logger.warning(
                "metrics.server_failed",
                extra={"port": self._port, "error": type(exc).__name__},
            )
            return False
        logger.info("metrics.server_started", extra={"host": self._host, "port": self.port})
        return True

    async def stop(self) -> None:
        """停止监听并等待现有连接结束。"""
        server = self._server
        if server is None:
            return
        self._server = None
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - 极少发生
            logger.warning("metrics.server_close_timeout")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=READ_TIMEOUT_SECONDS
            )
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await _respond(writer, 400, b"bad request\n")
            return
        except ConnectionError:  # pragma: no cover - 客户端提前断开
            return

        if len(request) > MAX_HEADER_BYTES:
            await _respond(writer, 431, b"header too large\n")
            return

        line = request.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        method = parts[0] if parts else ""
        path = parts[1].split("?")[0] if len(parts) > 1 else ""

        if method != "GET":
            await _respond(writer, 405, b"method not allowed\n", extra_headers=("Allow", "GET"))
            return
        if path.rstrip("/") not in ("/metrics", ""):
            await _respond(writer, 404, b"not found\n")
            return

        body, content_type = self._metrics.render()
        await _respond(writer, 200, body, content_type=content_type)


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    body: bytes,
    *,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: tuple[str, str] | None = None,
) -> None:
    """写回一个最小可用的 HTTP 响应。"""
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}.get(
        status, "Error"
    )
    lines = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if extra_headers is not None:
        lines.append(f"{extra_headers[0]}: {extra_headers[1]}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
    try:
        writer.write(head + body)
        await writer.drain()
    except (ConnectionError, RuntimeError):  # pragma: no cover - 客户端提前断开
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, RuntimeError):  # pragma: no cover
            await writer.wait_closed()


__all__ = ["MAX_HEADER_BYTES", "MetricsServer"]
