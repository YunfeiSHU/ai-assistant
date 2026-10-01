"""上游异常 → 对外错误码的映射测试。

这张映射表是**对外契约**（客户端据此决定「重试还是放弃」），所以逐条钉死。
"""

from __future__ import annotations

import httpcore
import httpx
import openai
import pytest

from app.core.exceptions import ErrorCode
from app.llm.base import map_llm_exception


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=_request())


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (TimeoutError("too slow"), ErrorCode.UPSTREAM_TIMEOUT),
        (openai.APITimeoutError(request=_request()), ErrorCode.UPSTREAM_TIMEOUT),
        (
            openai.AuthenticationError("bad key", response=_response(401), body=None),
            ErrorCode.UPSTREAM_LLM_AUTH_ERROR,
        ),
        (
            openai.PermissionDeniedError("nope", response=_response(403), body=None),
            ErrorCode.UPSTREAM_LLM_AUTH_ERROR,
        ),
        (
            openai.RateLimitError("slow down", response=_response(429), body=None),
            ErrorCode.RATE_LIMITED,
        ),
        (
            openai.APIConnectionError(request=_request()),
            ErrorCode.DEPENDENCY_UNAVAILABLE,
        ),
        (
            openai.APIStatusError("server error", response=_response(500), body=None),
            ErrorCode.UPSTREAM_LLM_ERROR,
        ),
        (
            openai.APIStatusError("bad request", response=_response(400), body=None),
            ErrorCode.UPSTREAM_LLM_ERROR,
        ),
        (ValueError("unexpected"), ErrorCode.UPSTREAM_LLM_ERROR),
    ],
)
def test_exception_maps_to_contract_code(exception: BaseException, expected: ErrorCode) -> None:
    """每一种上游异常都必须落到明确的对外错误码上。"""
    error = map_llm_exception(exception)

    assert error.code is expected
    assert error.status_code > 0
    assert error.message  # 不允许空文案：客户端要能直接展示


def test_status_error_message_contains_upstream_status() -> None:
    """上游 5xx 要把状态码带进文案，否则排障时无法区分「限流」和「上游挂了」。"""
    error = map_llm_exception(
        openai.APIStatusError("bad gateway", response=_response(502), body=None)
    )

    assert "502" in error.message


def test_connection_error_is_retryable_but_auth_error_is_not() -> None:
    """可重试性是契约的一部分：连不上能重试，密钥错重试多少次都没用。"""
    assert map_llm_exception(openai.APIConnectionError(request=_request())).retryable is True
    assert (
        map_llm_exception(
            openai.AuthenticationError("bad key", response=_response(401), body=None)
        ).retryable
        is False
    )


def test_httpcore_timeout_is_translated() -> None:
    """底层库的超时也要认（否则会退化成 500/502，重试策略就会跑偏）。"""
    error = map_llm_exception(httpcore.ConnectTimeout("connect timeout"))

    assert error.code is ErrorCode.UPSTREAM_TIMEOUT


def test_httpx_timeout_is_translated() -> None:
    """httpx 自己抛的超时同理。"""
    assert map_llm_exception(httpx.ConnectTimeout("timeout")).code is ErrorCode.UPSTREAM_TIMEOUT
