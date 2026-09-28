"""全局异常处理器：把任何失败都收敛为统一错误信封。

契约见 ``docs/02-接口规范与错误码.md`` §3.3 / §4。三条纪律：

1. **不泄漏实现细节** —— 500 只给通用文案，堆栈只进日志（``REQ-NFR-009``）；
2. **参数校验失败也是 400** —— FastAPI 默认的 422 与我们的契约冲突，这里统一改写；
3. **可重试性显式表达** —— ``retryable`` / ``retry_after`` 必须如实填写，客户端据此退避。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_trace_id
from app.core.errors import ERROR_SPECS, AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger("app.errors")

#: 框架层 HTTP 状态码 → 业务错误码
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_ARGUMENT,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.UPSTREAM_LLM_ERROR,
    503: ErrorCode.OVERLOADED,
    504: ErrorCode.UPSTREAM_TIMEOUT,
}


def error_response(error: AppError) -> JSONResponse:
    """把 :class:`AppError` 渲染为 HTTP 响应（含标准响应头）。"""
    headers: dict[str, str] = {}
    if error.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error.retryable and error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        content=jsonable_encoder(error.to_envelope(get_trace_id())),
        status_code=error.status_code,
        headers=headers or None,
    )


async def handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    """业务异常：直接使用其错误码与文案。"""
    assert isinstance(exc, AppError)
    log = logger.warning if exc.status_code < 500 else logger.error
    log(
        "request.failed",
        extra={
            "code": str(exc.code),
            "status_code": exc.status_code,
        },
    )
    return error_response(exc)


async def handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    """请求参数校验失败 → ``400 INVALID_ARGUMENT`` + ``details.fields``。"""
    assert isinstance(exc, RequestValidationError)
    fields: list[dict[str, Any]] = []
    for item in exc.errors():
        location = item.get("loc", ())
        fields.append(
            {
                "loc": ".".join(str(part) for part in location),
                "msg": item.get("msg", "invalid"),
                "type": item.get("type", "value_error"),
            }
        )
    error = AppError(
        ErrorCode.INVALID_ARGUMENT,
        "请求参数不合法",
        {"fields": jsonable_encoder(fields)},
    )
    logger.info("request.invalid", extra={"fields": [f["loc"] for f in fields]})
    return error_response(error)


async def handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    """框架抛出的 HTTPException（含 404 路由不存在、405 方法不支持）。"""
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INVALID_ARGUMENT)
    message = ERROR_SPECS[code].message
    if isinstance(exc.detail, str) and exc.detail and exc.status_code in (404, 405):
        # 框架自带的 detail（如 "Not Found"）信息量为零，用我们的文案覆盖
        message = ERROR_SPECS[code].message
    error = AppError(code, message, http_status=exc.status_code)
    return error_response(error)


async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    """未预期异常 → ``500 INTERNAL_ERROR``（堆栈只进日志）。"""
    logger.exception("request.unhandled", extra={"error_type": type(exc).__name__})
    return error_response(AppError(ErrorCode.INTERNAL_ERROR))


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器。

    .. note::
       ``Exception`` 的处理器由 Starlette 的 ``ServerErrorMiddleware`` 承接，
       因此即使是「处理器自身抛异常」也有兜底；但它**不会**捕获
       ``BaseException``（如 ``CancelledError``），这是刻意的。
    """
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)


__all__ = [
    "error_response",
    "handle_app_error",
    "handle_http_exception",
    "handle_unexpected",
    "handle_validation_error",
    "register_exception_handlers",
]
