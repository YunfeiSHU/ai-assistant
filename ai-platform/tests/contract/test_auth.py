"""契约测试：鉴权行为。

覆盖 ``AC-API-02``（无 token → 401；prod 下无法关闭鉴权）、``AC-NFR-05``、``AC-NFR-06``。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings

ProbeFactory = Callable[..., tuple[FastAPI, Settings]]


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_missing_token_returns_401(probe_app: ProbeFactory) -> None:
    """``AC-API-02``：无 token 访问受保护接口返回 ``401 UNAUTHENTICATED``。"""
    app, _ = probe_app()
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/whoami")

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "UNAUTHENTICATED"
    assert error["retryable"] is False
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer not-a-jwt",
        "Bearer a.b.c",
        "Basic dXNlcjpwYXNz",
        "Bearer ",
    ],
)
def test_malformed_token_returns_401(probe_app: ProbeFactory, authorization: str) -> None:
    """格式错误 / 非 Bearer 一律 401，不区分原因（防探测）。"""
    app, _ = probe_app()
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/whoami", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_wrong_signature_returns_401(probe_app: ProbeFactory) -> None:
    """用另一把密钥签发的 token 必须被拒绝。"""
    app, settings = probe_app()
    forged = jwt.encode(
        {
            "sub": "u_attacker",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": int(time.time()) + 600,
        },
        "another-secret-that-is-long-enough-0123456789",
        algorithm="HS256",
    )
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {forged}"}
        )

    assert response.status_code == 401


def test_expired_token_returns_401(probe_app: ProbeFactory, make_token: Callable[..., str]) -> None:
    """过期 token 必须被拒绝。"""
    app, app_settings = probe_app()
    expired = make_token("u_test", app_settings, expires_in=-10)
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {expired}"}
        )

    assert response.status_code == 401


def test_wrong_issuer_rejected(probe_app: ProbeFactory) -> None:
    """``iss`` 不匹配必须被拒绝（``REQ-API-005``）。"""
    app, settings = probe_app()
    token = jwt.encode(
        {
            "sub": "u_test",
            "iss": "some-other-issuer",
            "aud": settings.jwt_audience,
            "exp": int(time.time()) + 600,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 401


def test_valid_token_yields_user_id(probe_app: ProbeFactory) -> None:
    """合法 token：``sub`` 即 ``user_id``，并作为后续存储访问的隔离键。"""
    app, settings = probe_app()
    token = jwt.encode(
        {
            "sub": "u_test_abc",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": int(time.time()) + 600,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": "u_test_abc"}


def test_subject_with_illegal_chars_rejected(probe_app: ProbeFactory) -> None:
    """``sub`` 会被拼进 Redis Key / 向量过滤表达式，非法字符必须早拒（docs/09-§4）。"""
    app, settings = probe_app()
    token = jwt.encode(
        {
            "sub": "u_bad:*:injection",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": int(time.time()) + 600,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 401


def test_auth_disabled_uses_debug_user(probe_app: ProbeFactory) -> None:
    """本地关闭鉴权时使用 ``DEBUG_USER_ID``（``REQ-API-005`` 第 3 条）。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/whoami")

    assert response.status_code == 200
    assert response.json() == {"user_id": "u_dev"}


def test_auth_disabled_honours_debug_header(probe_app: ProbeFactory) -> None:
    """关闭鉴权时可用 ``X-Debug-User-Id`` 切换用户，便于多租户联调。"""
    app, _ = probe_app(auth_enabled=False)
    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/whoami", headers={"X-Debug-User-Id": "u_local_2"})

    assert response.json() == {"user_id": "u_local_2"}


def test_prod_cannot_disable_auth(probe_app: ProbeFactory) -> None:
    """``AC-NFR-05``：``APP_ENV=prod`` 时 ``AUTH_ENABLED=false`` 不生效。"""
    app, settings = probe_app(
        app_env="prod",
        auth_enabled=False,
        infra_backend="real",
        cors_origins=["https://app.example.com"],
        openai_api_key="sk-test",
    )
    assert settings.auth_required is True

    with _client(app) as test_client:
        response = test_client.get("/api/v1/probe/whoami")

    assert response.status_code == 401


def test_prod_accepts_valid_token(probe_app: ProbeFactory) -> None:
    """prod 环境下合法 token 正常工作。"""
    app, settings = probe_app(
        app_env="prod",
        auth_enabled=False,
        infra_backend="real",
        cors_origins=["https://app.example.com"],
        openai_api_key="sk-test",
    )
    token = jwt.encode(
        {
            "sub": "u_prod",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": int(time.time()) + 600,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    with _client(app) as test_client:
        response = test_client.get(
            "/api/v1/probe/whoami", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.json() == {"user_id": "u_prod"}
