"""独立指标端口单测（``docs/10`` §5.2 的 ``METRICS_PORT``）。

这一组测试覆盖的是「抓取方到底能看到什么」：状态码、content-type、方法限制。
另外两条是**回归性质**的：

* ``port=0`` 必须能被内核分配成真实端口 —— 测试与多实例部署都靠它避开端口冲突；
* ``stop()`` 必须真的释放端口 —— 早期用 ``prometheus_client.start_http_server``
  时拿不到 server 对象，测试间会残留监听，表现为间歇性的「地址已占用」。
"""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.observability import server as server_module
from app.infrastructure.observability.metrics import Metrics
from app.infrastructure.observability.server import MAX_HEADER_BYTES, MetricsServer


async def _request(port: int, raw: bytes) -> bytes:
    """发一段原始 HTTP 报文，读回完整响应（连接是 ``Connection: close``）。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout=5.0)
    finally:
        writer.close()


async def _get(port: int, path: str = "/metrics") -> tuple[int, bytes]:
    """发一个最小 GET，返回 ``(状态码, 报文)``。"""
    response = await _request(
        port, f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    )
    return int(response.split(b" ", 2)[1]), response


@pytest.fixture
async def server() -> MetricsServer:
    """监听随机空闲端口的指标服务器。"""
    metrics = Metrics(service="ai-platform")
    metrics.observe_agent_steps(finish_reason="stop", steps=1)
    instance = MetricsServer(metrics, host="127.0.0.1", port=0)
    assert await instance.start() is True
    try:
        yield instance
    finally:
        await instance.stop()


async def test_port_zero_is_replaced_by_a_real_port(server: MetricsServer) -> None:
    """``port=0`` 时对外报告的是内核实际分配的端口，而不是 0。"""
    assert server.running
    assert server.port > 0


async def test_metrics_endpoint_returns_prometheus_text(server: MetricsServer) -> None:
    """``GET /metrics`` 返回 Prometheus 文本格式，且能看到刚记录的指标。"""
    status, body = await _get(server.port)

    assert status == 200
    assert b"Content-Type: text/plain; version=0.0.4" in body
    assert b"ai_agent_steps" in body


async def test_trailing_slash_is_accepted(server: MetricsServer) -> None:
    """``/metrics/`` 与 ``/metrics`` 等价：抓取方有时会带斜杠。"""
    status, _ = await _get(server.port, "/metrics/")

    assert status == 200


async def test_other_paths_are_not_found(server: MetricsServer) -> None:
    """只服务 ``/metrics``：其余路径 404（端口上不暴露任何别的信息）。"""
    status, body = await _get(server.port, "/debug/pprof")

    assert status == 404
    assert b"not found" in body


async def test_post_is_rejected_with_allow_header(server: MetricsServer) -> None:
    """非 GET 一律 405，并带 ``Allow: GET``（符合 HTTP 语义）。"""
    response = await _request(server.port, b"POST /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")

    assert b"405" in response.split(b"\r\n", 1)[0]
    assert b"Allow: GET" in response


async def test_oversized_header_is_rejected(server: MetricsServer) -> None:
    """超长头部返回 431 而不是无上限读内存。"""
    padding = b"X-Pad: " + b"a" * (MAX_HEADER_BYTES + 1024) + b"\r\n"
    response = await _request(server.port, b"GET /metrics HTTP/1.1\r\n" + padding + b"\r\n")

    assert b"431" in response.split(b"\r\n", 1)[0]


async def test_incomplete_request_times_out_to_400(
    server: MetricsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """请求头迟迟读不完（连接吊着不发 ``\\r\\n\\r\\n``）→ 超时返回 400。

    把读超时压到 0.1s 再测：真等默认的 5 秒会让这条用例凭空慢 5 秒。
    """
    monkeypatch.setattr(server_module, "READ_TIMEOUT_SECONDS", 0.1)

    response = await _request(server.port, b"GET /metrics")

    assert b"400" in response.split(b"\r\n", 1)[0]


async def test_stop_releases_the_port() -> None:
    """``stop()`` 之后端口必须能被立刻重新绑定 —— 否则测试之间会互相干扰。"""
    metrics = Metrics()
    first = MetricsServer(metrics, host="127.0.0.1", port=0)
    await first.start()
    port = first.port

    await first.stop()
    assert not first.running

    second = MetricsServer(metrics, host="127.0.0.1", port=port)
    try:
        assert await second.start() is True
    finally:
        await second.stop()


async def test_stop_is_idempotent() -> None:
    """重复 ``stop()``（未启动 / 已停止）不抛异常 —— lifespan 收尾可能被调两次。"""
    server = MetricsServer(Metrics(), host="127.0.0.1", port=0)

    await server.stop()
    await server.start()
    await server.stop()
    await server.stop()

    assert not server.running


async def test_bind_failure_is_reported_not_raised() -> None:
    """端口被占用时返回 ``False`` 而不是抛异常：指标端口不该让服务起不来。"""
    holder = MetricsServer(Metrics(), host="127.0.0.1", port=0)
    await holder.start()
    try:
        clash = MetricsServer(Metrics(), host="127.0.0.1", port=holder.port)
        assert await clash.start() is False
        assert not clash.running
        # 未启动时 ``port`` 仍返回配置值（便于日志里说明「想开哪个端口」）
        assert clash.port == holder.port
    finally:
        await holder.stop()


async def test_unobserved_metrics_still_render(server: MetricsServer) -> None:
    """没有任何业务数据时也必须能渲染（Prometheus 抓空报表不该报错）。"""
    status, body = await _get(server.port)

    assert status == 200
    assert b"ai_build_info" in body


async def test_disabled_metrics_render_explanation() -> None:
    """指标被关闭时端口仍要能给出可读的说明文本，而不是空响应。"""
    server = MetricsServer(Metrics(enabled=False), host="127.0.0.1", port=0)
    await server.start()
    try:
        status, body = await _get(server.port)
    finally:
        await server.stop()

    assert status == 200
    assert b"metrics disabled" in body
