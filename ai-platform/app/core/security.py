"""鉴权：JWT 校验与当前用户解析。

契约见 ``docs/02-接口规范与错误码.md`` §2.1：

1. 除白名单路径外所有接口 MUST 校验 JWT；
2. 校验签名、``exp``、``iss``、``aud``；
3. ``AUTH_ENABLED=false`` 仅在非生产生效，此时用户来自 ``X-Debug-User-Id``；
4. 任何失败一律返回 ``401 UNAUTHENTICATED``（不区分无 token / 过期 / 签名错，防探测）；
5. 拿到 ``user_id`` 后 MUST 作为**所有**存储访问的过滤条件。

.. note::
   JWT 由 Go 侧签发，Python 侧只验签不信内容之外的任何东西 —— 请求体里的
   ``user_id`` 永远不参与授权（``REQ-NFR-007``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jwt

from app.config import Settings
from app.core.context import set_user_id
from app.core.errors import AppError, ErrorCode
from app.core.ids import is_safe_key_component

#: 鉴权豁免路径（完全匹配或前缀匹配，见 §2.1）
PUBLIC_PATHS: tuple[str, ...] = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


@dataclass(frozen=True, slots=True)
class AuthUser:
    """已认证的调用者。"""

    user_id: str
    claims: dict[str, Any] = field(default_factory=dict)


def extract_bearer_token(authorization: str | None) -> str | None:
    """从 ``Authorization`` 头中取出 Bearer token；格式不对返回 ``None``。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    """校验并解码 JWT，失败统一抛 ``UNAUTHENTICATED``。"""
    if not settings.jwt_secret:
        raise AppError(ErrorCode.UNAUTHENTICATED, "服务端未配置 JWT 密钥，无法校验身份")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # 刻意不区分具体原因，只记 debug 级日志由调用方决定
        raise AppError(ErrorCode.UNAUTHENTICATED, details={"reason": type(exc).__name__}) from exc
    return claims


def create_access_token(
    user_id: str,
    settings: Settings,
    *,
    expires_in: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发一个测试/本地联调用的 HS256 token。

    .. warning::
       生产环境的 token 由 Go 侧签发；本函数仅用于本地联调与自动化测试。
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + expires_in,
    }
    if extra_claims:
        payload.update(extra_claims)
    if not settings.jwt_secret:
        raise AppError(ErrorCode.INTERNAL_ERROR, "缺少 JWT_SECRET，无法签发 token")
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def user_id_from_claims(claims: dict[str, Any]) -> str:
    """从 claims 提取并校验 ``user_id``。

    ``sub`` 会被直接拼进 Redis Key / Milvus 表达式，故 MUST 通过安全字符校验
    （docs/09-§4 的 Key 注入防护）。
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AppError(ErrorCode.UNAUTHENTICATED, "token 缺少 sub 声明")
    if not is_safe_key_component(subject):
        raise AppError(ErrorCode.UNAUTHENTICATED, "token 中的 sub 含非法字符")
    return subject


def authenticate(
    authorization: str | None, settings: Settings, debug_user_id: str | None = None
) -> AuthUser:
    """解析调用者身份。

    Args:
        authorization: ``Authorization`` 请求头原文。
        settings: 全局配置。
        debug_user_id: ``X-Debug-User-Id`` 头（仅鉴权关闭时生效）。

    Raises:
        AppError: ``UNAUTHENTICATED``。
    """
    if not settings.auth_required:
        user_id = (debug_user_id or "").strip() or settings.debug_user_id
        if not is_safe_key_component(user_id):
            raise AppError(ErrorCode.UNAUTHENTICATED, "X-Debug-User-Id 含非法字符")
        set_user_id(user_id)
        return AuthUser(user_id=user_id, claims={"debug": True})

    token = extract_bearer_token(authorization)
    if token is None:
        raise AppError(ErrorCode.UNAUTHENTICATED)

    claims = decode_access_token(token, settings)
    user_id = user_id_from_claims(claims)
    set_user_id(user_id)
    return AuthUser(user_id=user_id, claims=claims)


def is_public_path(path: str) -> bool:
    """判断路径是否豁免鉴权（健康检查与文档端点）。"""
    if path in PUBLIC_PATHS:
        return True
    return "/health" in path and path.endswith(("health", "live", "ready", "metrics"))


__all__ = [
    "PUBLIC_PATHS",
    "AuthUser",
    "authenticate",
    "create_access_token",
    "decode_access_token",
    "extract_bearer_token",
    "is_public_path",
    "user_id_from_claims",
]
