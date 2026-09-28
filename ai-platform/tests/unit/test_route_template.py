"""``route_template`` 的降级链（指标 ``endpoint`` 标签的来源）。

为什么不放在契约测试里：这条函数的三个分支里有两个**只在降级时才走到**
（拿不到 FastAPI 的有效路由上下文、连 ``route`` 都没有），
在真实请求里没法稳定复现，只能直接构造 scope。
"""

from __future__ import annotations

from typing import Any

from starlette.routing import Route
from starlette.types import Scope

from app.core.middleware import UNMATCHED_ENDPOINT, route_template


class _Context:
    """替身：FastAPI ``_EffectiveRouteContext`` 只要有 ``path`` 就够用。"""

    def __init__(self, path: Any) -> None:
        self.path = path


def _scope(**extra: Any) -> Scope:
    scope: Scope = {"type": "http", "path": "/api/v1/health/live", "method": "GET"}
    scope.update(extra)
    return scope


def _route(path: str) -> Route:
    return Route(path, endpoint=lambda: None)


def test_effective_context_wins_over_the_inner_route_path() -> None:
    """嵌套 ``include_router`` 时必须用完整模板，而不是最内层路由的片段。

    回归保险：FastAPI 0.141 起 ``scope["route"]`` 是最内层路由（``/live``），
    直接用它的 ``path`` 会让所有模块的 ``/live`` 撞进同一个标签。
    """
    scope = _scope(
        route=_route("/live"),
        fastapi={"effective_route_context": _Context("/api/v1/health/live")},
    )

    assert route_template(scope) == "/api/v1/health/live"


def test_path_placeholders_are_kept_as_is() -> None:
    """上下文里的 ``path`` 是模板（占位符未替换），直接可用。"""
    scope = _scope(
        path="/api/v1/knowledge-bases/kb_x",
        route=_route("/knowledge-bases/{kb_id}"),
        fastapi={"effective_route_context": _Context("/api/v1/knowledge-bases/{kb_id}")},
    )

    assert route_template(scope) == "/api/v1/knowledge-bases/{kb_id}"


def test_falls_back_to_route_path_without_fastapi_scope() -> None:
    """没有 FastAPI 上下文（未嵌套注册）时，``route.path`` 本身就是完整模板。"""
    scope = _scope(path="/health/live", route=_route("/health/live"))

    assert route_template(scope) == "/health/live"


def test_falls_back_to_unmatched_without_any_route() -> None:
    """404 时干净地退化成固定标签，绝不能拿原始 path 去当标签。"""
    scope = _scope(path="/api/v1/definitely-not-a-route")

    assert route_template(scope) == UNMATCHED_ENDPOINT


def test_ignores_malformed_effective_context() -> None:
    """私有 scope 键随时可能变：坏数据必须降级，而不是让请求 500。"""
    for broken in (
        None,
        "not-a-dict",
        {"effective_route_context": None},
        {"effective_route_context": _Context("")},
        {"effective_route_context": _Context(123)},
        {"effective_route_context": object()},
    ):
        scope = _scope(route=_route("/live"), fastapi=broken)
        assert route_template(scope) == "/live"
