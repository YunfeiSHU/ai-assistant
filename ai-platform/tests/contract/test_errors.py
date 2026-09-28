"""契约测试：统一错误信封与参数校验。

覆盖 ``AC-API-04``（错误结构完整且与 ``X-Request-Id`` 关联）、``AC-API-06``（分页校验）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.pagination import encode_cursor

#: 错误信封必备字段（docs/02 §3.3）
ENVELOPE_KEYS = {"code", "message", "details", "trace_id", "retryable"}


def _client(app: FastAPI, *, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_unknown_route_returns_error_envelope(client: TestClient) -> None:
    """``AC-API-04``：未知路由也是统一信封，且 ``trace_id`` 与响应头一致。"""
    response = client.get("/api/v1/definitely-not-a-route")

    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) >= ENVELOPE_KEYS
    assert error["code"] == "NOT_FOUND"
    assert error["retryable"] is False
    assert error["details"] == {}
    assert error["trace_id"] == response.headers["X-Trace-Id"]


def test_method_not_allowed_is_enveloped(client: TestClient) -> None:
    """405 同样走信封（框架默认的 detail 信息量为零，应被我们的文案覆盖）。"""
    response = client.delete("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_business_error_envelope(probe_app: Callable[..., tuple[FastAPI, Settings]]) -> None:
    """业务异常携带结构化 ``details``，方便前端定位。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.post("/api/v1/probe/kb-not-found")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "KB_NOT_FOUND"
    assert error["message"] == "知识库不存在或无权访问"
    assert error["details"]["kb_id"].startswith("kb_")
    assert error["retryable"] is False


def test_retryable_error_carries_retry_after(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """可重试错误 MUST 给出 ``retry_after`` 与同名响应头。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.post("/api/v1/probe/rate-limited")

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["retryable"] is True
    assert error["retry_after"] == 7
    assert response.headers["Retry-After"] == "7"


def test_unexpected_error_does_not_leak_details(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """``REQ-NFR-009``：500 不得泄漏堆栈 / 内部信息。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app, raise_server_exceptions=False) as test_client:
        response = test_client.post("/api/v1/probe/crash")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "服务内部错误"
    raw = response.text
    assert "kaboom" not in raw
    assert "Traceback" not in raw


def test_validation_error_becomes_400_with_fields(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """``AC-API-06``：``limit=101`` 返回 ``400 INVALID_ARGUMENT`` 且列出字段。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/paginated", params={"limit": 101})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["details"]["fields"]
    assert "limit" in error["details"]["fields"][0]["loc"]


def test_pagination_defaults_and_cursor_roundtrip(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """``AC-API-06``：默认 ``limit=20``，合法游标可被正确解码。"""
    app, _ = probe_app(auth_enabled=False)
    cursor = encode_cursor(datetime(2026, 9, 28, 10, 0, 0, 123000, tzinfo=UTC), "doc_1")
    with _client(app) as test_client:
        default_response = test_client.get("/api/v1/probe/paginated")
        cursor_response = test_client.get("/api/v1/probe/paginated", params={"cursor": cursor})

    assert default_response.json()["limit"] == 20
    assert default_response.json()["cursor"] is None
    assert cursor_response.status_code == 200
    body = cursor_response.json()
    assert body["position"] == ["2026-09-28T10:00:00.123000+00:00", "doc_1"]


def test_invalid_cursor_rejected(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """被篡改的游标返回 400，而不是静默当作第一页。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/paginated", params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_json_body_limit_enforced(probe_app: Callable[..., tuple[FastAPI, Settings]]) -> None:
    """``REQ-NFR-009``：请求体超限由中间件早失败，不进业务逻辑。"""
    app, _ = probe_app(auth_enabled=False, max_json_body_bytes=64)
    with _client(app) as test_client:
        response = test_client.post("/api/v1/probe/kb-not-found", json={"blob": "x" * 500})

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_error_envelope_shape_is_stable(
    probe_app: Callable[..., tuple[FastAPI, Settings]],
) -> None:
    """信封字段集合稳定：多一个 ``retry_after`` 也只能出现在 ``retryable=true`` 时。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        payload: dict[str, Any] = test_client.post("/api/v1/probe/kb-not-found").json()

    assert set(payload["error"]) == ENVELOPE_KEYS
