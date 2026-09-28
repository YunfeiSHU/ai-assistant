"""契约测试：健康检查与 OpenAPI 可访问性。

覆盖 ``AC-API-01``、``AC-API-04``（部分）、``AC-NFR-11``。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    """``AC-API-01``：``/health`` 返回基本信息且包含依赖明细。"""
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "ai-platform"
    assert body["env"] == "local"
    assert body["version"]
    assert "storage" in body["dependencies"]


def test_health_returns_request_id_header(client: TestClient) -> None:
    """``AC-API-04``：响应头回写 ``X-Request-Id`` 与 ``X-Trace-Id``。"""
    response = client.get("/api/v1/health")

    assert response.headers["X-Request-Id"].startswith("req_")
    assert len(response.headers["X-Trace-Id"]) == 32


def test_health_echoes_client_request_id(client: TestClient) -> None:
    """客户端传入 ``X-Request-Id`` 时 MUST 原样回写（便于端到端串联）。"""
    response = client.get(
        "/api/v1/health", headers={"X-Request-Id": "req_CLIENT_SUPPLIED_0000000000"}
    )

    assert response.headers["X-Request-Id"] == "req_CLIENT_SUPPLIED_0000000000"


def test_health_live_ignores_dependencies(client: TestClient) -> None:
    """``AC-NFR-11``：存活探针不检查依赖，进程在跑即 200。"""
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_health_ready_ok_in_memory_backend(client: TestClient) -> None:
    """``INFRA_BACKEND=memory`` 时外部依赖检查被跳过，就绪为 200。"""
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # 外部依赖在 memory 后端下必须显式标记为 skipped，而不是假装探活过
    assert body["checks"]["milvus"]["skipped"] is True
    assert body["checks"]["mysql"]["skipped"] is True
    assert body["checks"]["redis"]["skipped"] is True
    assert body["checks"]["embedding"]["dim"] == 1024


def test_openapi_and_docs_available(client: TestClient) -> None:
    """``AC-API-01``：``/openapi.json`` 与 ``/docs`` 始终可访问。"""
    openapi = client.get("/openapi.json")
    docs = client.get("/docs")

    assert openapi.status_code == 200
    assert docs.status_code == 200
    paths = openapi.json()["paths"]
    assert "/api/v1/health" in paths
    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths


def test_traceparent_propagated(client: TestClient) -> None:
    """``REQ-NFR-010``：入站 ``traceparent`` 的 trace id 被沿用（链路可串）。"""
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = client.get(
        "/api/v1/health",
        headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"},
    )

    assert response.headers["X-Trace-Id"] == trace_id
