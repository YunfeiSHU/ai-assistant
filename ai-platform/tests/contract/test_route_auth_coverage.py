"""契约测试：**所有**非公开路由都必须声明鉴权依赖。

这条测试是「忘了加鉴权」的结构性防线。docs/02 §2.1 要求除白名单外所有接口都校验 JWT；
靠人工 review 判断「这个新路由加了 ``Depends`` 吗」迟早会漏，所以用机器来把关。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.core.security import is_public_path


def _dependency_calls(dependant: Any) -> set[Any]:
    """递归收集一条路由（含子依赖）声明的全部依赖函数。"""
    calls: set[Any] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        for sub in node.dependencies:
            calls.add(sub.call)
            stack.append(sub)
    return calls


def test_every_non_public_route_requires_auth(app: FastAPI) -> None:
    """非公开路由 MUST 依赖 ``get_current_user``（直接或间接）。"""
    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if is_public_path(route.path):
            continue
        if get_current_user not in _dependency_calls(route.dependant):
            missing.append(f"{sorted(route.methods)} {route.path}")

    assert missing == [], f"以下路由未声明鉴权依赖：{missing}"


def test_health_endpoints_are_public(app: FastAPI) -> None:
    """健康检查与文档端点必须豁免鉴权（否则探针无法工作）。"""
    for path in (
        "/api/v1/health",
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/docs",
        "/openapi.json",
    ):
        assert is_public_path(path), f"{path} 应豁免鉴权"
