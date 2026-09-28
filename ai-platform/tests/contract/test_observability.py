"""可观测性契约测试（docs/10-§5.2、§7）。

这里断言的是「接线是否正确」——指标名/标签、抓取端点、脱敏——
而不是 Prometheus 客户端本身的行为（那部分在 ``tests/unit/test_observability_*.py``）。
"""

from __future__ import annotations

import urllib.error
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings

#: 独立抓取端口在测试里由操作系统分配，因此必须从 ``app.state`` 读实际端口
_URL = "http://127.0.0.1:{port}/metrics"


def _render(app: FastAPI) -> str:
    """取当前应用自己的 Prometheus 文本快照。"""
    body, content_type = app.state.metrics.render()
    assert content_type.startswith("text/plain")
    return body.decode("utf-8")


def _sample(text: str, metric: str, **labels: str) -> float | None:
    """按 ``指标名 + 标签`` 取值；不存在返回 ``None``。

    只做字符串匹配（不引入 prometheus_client 的解析器），保证断言的是
    **真实暴露出去的文本**，而不是注册表内部对象。
    """
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        if not all(f'{key}="{value}"' in line for key, value in labels.items()):
            continue
        if " " not in line:
            continue
        return float(line.rsplit(" ", 1)[1])
    return None


def _fetch(port: int) -> tuple[int, str]:
    """直接抓一次独立端口的 ``/metrics``。"""
    try:
        with urllib.request.urlopen(_URL.format(port=port), timeout=5) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - 断言失败时才走到
        return exc.code, ""


# ----------------------------------------------------------------------
# 抓取端点
# ----------------------------------------------------------------------
def test_metrics_is_not_exposed_on_the_main_app(client: TestClient) -> None:
    """``/metrics`` **只在独立端口**暴露，主应用不提供该路由。

    理由（docs/10-§5.2）：主应用挂着 JWT 中间件，若把 ``/metrics`` 挂上去，
    要么给它开未鉴权白名单（把路由清单和流量画像泄漏给任何人），
    要么让 Prometheus 持有一个业务 token —— 两条路都不如独立端口干净。
    """
    response = client.get("/metrics")

    assert response.status_code == 404


def test_standalone_port_serves_prometheus_text(app: FastAPI, client: TestClient) -> None:
    """独立端口在 lifespan 里真的被监听，且能抓到 ``ai_build_info``。

    ``METRICS_PORT=0`` 表示「端口交给 OS 分配」，**不是**「关闭指标端口」——
    否则测试里就没法既避开端口冲突、又验证真实监听路径。
    """
    server = app.state.metrics_server
    assert app.state.metrics_server_ready is True
    assert server is not None
    assert server.port > 0

    status, text = _fetch(server.port)

    assert status == 200
    assert "ai_build" in text


# ----------------------------------------------------------------------
# HTTP 指标
# ----------------------------------------------------------------------
def test_http_metrics_labelled_by_route_template(client: TestClient) -> None:
    """``endpoint`` 标签是**路由模板**，不是原始路径（避免基数爆炸）。"""
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/live").status_code == 200

    text = _render(client.app)
    value = _sample(
        text,
        "ai_requests_total",
        endpoint="GET /api/v1/health/live",
        method="GET",
        status="200",
    )

    assert value == 2.0
    assert (
        _sample(
            text,
            "ai_request_duration_seconds_count",
            endpoint="GET /api/v1/health/live",
        )
        == 2.0
    )


def test_path_parameters_are_not_used_as_labels(client: TestClient) -> None:
    """路径参数必须被模板占位符掉，否则每个 kb_id 都会造一个新标签。

    这条用例同时是「FastAPI 0.141 嵌套 ``include_router`` 后
    ``scope["route"].path`` 只有 ``/knowledge-bases/{kb_id}`` 而无前缀」的回归
    保险：带前缀的完整模板只有从有效路由上下文里才拿得到。
    """
    # 未带认证 → 401，但路由已经命中，指标标签已经产生
    assert client.get("/api/v1/knowledge-bases/kb_labelled").status_code == 401

    text = _render(client.app)

    assert _sample(text, "ai_requests_total", endpoint="GET /api/v1/knowledge-bases/{kb_id}") == 1.0
    assert "kb_labelled" not in text


def test_unmatched_path_falls_back_to_a_fixed_label(client: TestClient) -> None:
    """未命中路由时退化为固定字符串 ``unmatched``（否则任意路径都能造基数）。"""
    assert client.get("/api/v1/definitely-not-a-route").status_code == 404

    text = _render(client.app)

    assert _sample(text, "ai_requests_total", endpoint="GET unmatched", status="404") == 1.0


def test_metrics_never_contain_request_level_identifiers(app: FastAPI, client: TestClient) -> None:
    """指标里**绝不能**出现用户 / 请求 / 会话级标识（docs/10-§5.2 基数约束）。"""
    response = client.get("/api/v1/health/live")

    text = _render(app)
    request_id = response.headers["x-request-id"]

    assert request_id not in text
    assert "user_id" not in text
    assert "conversation_id" not in text


# ----------------------------------------------------------------------
# 熔断 / 追踪 / MCP 状态接线
# ----------------------------------------------------------------------
def test_circuit_registry_is_wired_into_the_app(app: FastAPI, client: TestClient) -> None:
    """熔断器与指标是连在一起的：状态一变，``ai_circuit_breaker_state`` 就跟着变。

    ``DEFAULT_RULES["mcp"] = (3, 30.0)``，而 ``mcp:fs`` 走的是「家族前缀」回退，
    所以失败 3 次就打开 —— 这条用例同时验证了规则装配与家族回退两件事。
    """
    registry = app.state.circuit_registry
    breaker = registry.get("mcp:fs")

    assert breaker.state == "closed"
    assert registry.get("mcp:git") is not breaker  # 每个 Server 一个独立熔断器

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "open"
    assert registry.snapshot()["mcp:fs"] == "open"
    assert registry.get("never-heard-of").state == "closed"

    text = _render(app)
    assert _sample(text, "ai_circuit_breaker_state", target="mcp:fs") == 2.0  # open

    # 复位后指标跟着回到 closed：Gauge 只在状态变化时同步，
    # 所以「状态变了但指标没变」这类接线错误能被这条断言抓住
    breaker.reset()
    assert _sample(_render(app), "ai_circuit_breaker_state", target="mcp:fs") == 0.0


def test_tracing_is_disabled_but_usable_in_tests(app: FastAPI, client: TestClient) -> None:
    """测试基线关掉 OTel 导出，但 ``get_tracing()`` 仍返回可用的空实现。"""
    tracing = app.state.tracing

    assert tracing.enabled is False
    with tracing.span("noop") as span:
        assert span is None


def test_mcp_server_state_metric_reflects_connections(
    mcp_app: tuple[FastAPI, Settings, TestClient],
) -> None:
    """``AC-MCP-06``：每个 Server 的状态是**互斥**的一组取值，不是各自累加。"""
    app, _settings, test_client = mcp_app
    with test_client:
        text = _render(app)

    for name in ("fs", "git"):
        assert _sample(text, "ai_mcp_server_state", server=name, state="connected") == 1.0
        assert _sample(text, "ai_mcp_server_state", server=name, state="connecting") == 0.0
        assert _sample(text, "ai_mcp_server_state", server=name, state="unavailable") == 0.0
        assert _sample(text, "ai_mcp_server_state", server=name, state="disabled") == 0.0
