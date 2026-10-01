"""请求级上下文（``contextvars``）。

把 ``trace_id`` / ``request_id`` / ``user_id`` 透传到任意深度的调用栈而不必层层传参：
日志格式化器、指标记录器、上游客户端都从这里取。``contextvars`` 在 asyncio 下按任务隔离，
并发请求互不串味。
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Iterator
from contextvars import ContextVar

TRACE_ID_LENGTH = 32
SPAN_ID_LENGTH = 16

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_span_id: ContextVar[str] = ContextVar("span_id", default="")
_request_id: ContextVar[str] = ContextVar("request_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")
_endpoint: ContextVar[str] = ContextVar("endpoint", default="")


def new_trace_id() -> str:
    """生成 32 位小写十六进制 trace id（与 W3C Trace Context 一致）。"""
    return secrets.token_hex(TRACE_ID_LENGTH // 2)


def new_span_id() -> str:
    """生成 16 位小写十六进制 span id。"""
    return secrets.token_hex(SPAN_ID_LENGTH // 2)


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """解析 W3C ``traceparent``，非法或全零时返回 ``None``（由调用方新建 trace）。

    格式：``version-traceid-spanid-flags``，例如
    ``00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01``。
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, span_id = parts[0], parts[1], parts[2]
    if len(version) != 2 or len(trace_id) != 32 or len(span_id) != 16:
        return None
    if not all(c in "0123456789abcdefABCDEF" for c in trace_id + span_id):
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id.lower(), span_id.lower()


def format_traceparent(trace_id: str, span_id: str) -> str:
    """组装出站请求用的 ``traceparent`` 头。"""
    return f"00-{trace_id}-{span_id}-01"


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------
def get_trace_id() -> str:
    """当前 trace id；无上下文时返回 ``"-"``（便于直接写日志）。"""
    return _trace_id.get() or "-"


def get_span_id() -> str:
    """当前 span id；无上下文时返回 ``"-"``。"""
    return _span_id.get() or "-"


def get_request_id() -> str:
    """当前请求号（``req_*``）。"""
    return _request_id.get()


def get_user_id() -> str:
    """当前登录用户 id（未鉴权时为空串）。"""
    return _user_id.get()


def get_endpoint() -> str:
    """当前请求的端点标识。"""
    return _endpoint.get()


def set_user_id(user_id: str) -> None:
    """鉴权成功后写入用户 id。"""
    _user_id.set(user_id)


def set_endpoint(endpoint: str) -> None:
    """写入端点标识（形如 ``GET /api/v1/chat``）。"""
    _endpoint.set(endpoint)


@contextlib.contextmanager
def request_context(
    trace_id: str,
    span_id: str,
    request_id: str,
    endpoint: str = "",
) -> Iterator[None]:
    """在一个请求处理周期内安装上下文，退出时自动还原（中间件使用）。"""
    tokens = (
        _trace_id.set(trace_id),
        _span_id.set(span_id),
        _request_id.set(request_id),
        _endpoint.set(endpoint),
        _user_id.set(""),
    )
    try:
        yield
    finally:
        _user_id.reset(tokens[4])
        _endpoint.reset(tokens[3])
        _request_id.reset(tokens[2])
        _span_id.reset(tokens[1])
        _trace_id.reset(tokens[0])


__all__ = [
    "SPAN_ID_LENGTH",
    "TRACE_ID_LENGTH",
    "format_traceparent",
    "get_endpoint",
    "get_request_id",
    "get_span_id",
    "get_trace_id",
    "get_user_id",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "request_context",
    "set_endpoint",
    "set_user_id",
]
