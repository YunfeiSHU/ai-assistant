"""单测：云端 provider 共用的重试策略（``app/core/http.py``）。

这层是三个云端调用点（方舟 embedding、硅基流动 embedding、硅基流动 rerank）的
**唯一**重试实现，所以这里钉住的每一条都是三处共用的行为：

* 429/5xx/网络 ⇒ 退避重试；4xx ⇒ 立刻失败并带出上游原文；
* 尊重 ``Retry-After``，但**必须有上限**（上游说等 600s 不能真的挂 10 分钟）；
* 重试耗尽 ⇒ ``DEPENDENCY_UNAVAILABLE``（语义是"稍后再来"）。

``sleep`` 是注入点：测试里换成"记录时长"，于是连"退避了多久"都能断言，
而且不用真的睡（否则一个用例要几十秒）。
"""

from __future__ import annotations

import httpx
import pytest

from app.core.exceptions import AppError, ErrorCode
from app.core.http import (
    MAX_RETRY_AFTER_SECONDS,
    backoff_seconds,
    post_json_with_retry,
    retry_after_seconds,
)


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _call(
    handler: object,
    *,
    max_retries: int = 2,
    sleeps: list[float] | None = None,
) -> dict[str, object]:
    slept = sleeps if sleeps is not None else []
    with _client(handler) as client:
        return post_json_with_retry(
            client,
            "https://example.invalid/v1/embeddings",
            payload={"x": 1},
            headers={"Authorization": "Bearer k"},
            timeout=5.0,
            max_retries=max_retries,
            provider="test",
            sleep=slept.append,
        )


def test_success_returns_parsed_body_without_sleeping() -> None:
    """200 直接返回解析后的正文，且一次都不退避（成功路径不能有等待开销）。"""
    sleeps: list[float] = []
    body = _call(lambda _r: httpx.Response(200, json={"ok": True}), sleeps=sleeps)

    assert body == {"ok": True}
    assert sleeps == []


def test_rate_limit_is_retried_and_retry_after_is_respected() -> None:
    """429 按 ``Retry-After`` 等待（而不是按指数退避），然后成功。"""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, text="slow down")
        return httpx.Response(200, json={"data": []})

    _call(handler, sleeps=sleeps)

    assert calls["n"] == 2
    assert sleeps == [0.25]


def test_huge_retry_after_is_capped() -> None:
    """上游说等 600 秒时只等上限 —— 否则一次限流会挂住整个请求。"""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, headers={"Retry-After": "600"})
        return httpx.Response(200, json={"ok": 1})

    _call(handler, sleeps=sleeps)

    assert sleeps == [MAX_RETRY_AFTER_SECONDS]


def test_client_error_fails_fast_with_upstream_detail() -> None:
    """401 不重试，并且把上游原文带进详情（否则"key 过期"会被埋成 503）。"""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text='{"code":30014,"message":"Token is invalid."}')

    with pytest.raises(AppError) as excinfo:
        _call(handler, sleeps=sleeps)

    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    assert "30014" in excinfo.value.details["detail"]
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_client_errors_are_not_retried(status: int) -> None:
    """除 401 之外的 4xx 同样立刻失败：重试改不了「请求本身写错了」。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="nope")

    with pytest.raises(AppError) as excinfo:
        _call(handler)
    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    assert calls["n"] == 1


def test_retryable_exhausted_reports_dependency_unavailable() -> None:
    """5xx 重试耗尽 ⇒ ``DEPENDENCY_UNAVAILABLE``（"稍后再来"，不是"请求写错了"）。"""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="upstream down")

    with pytest.raises(AppError) as excinfo:
        _call(handler, max_retries=1, sleeps=sleeps)

    assert excinfo.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert calls["n"] == 2
    assert len(sleeps) == 1  # 两次尝试之间退避一次


def test_transport_error_is_retried_then_reported() -> None:
    """连不上也要重试：网络抖动是"稍后再来"，不该直接算请求失败。"""
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(AppError) as excinfo:
        _call(handler, max_retries=1, sleeps=sleeps)

    assert excinfo.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert calls["n"] == 2
    assert excinfo.value.details["attempts"] == 2
    assert len(sleeps) == 1


def test_non_json_success_body_is_reported() -> None:
    """HTTP 200 但正文不是 JSON ⇒ 明确失败，而不是把空字典当成功。"""
    with pytest.raises(AppError) as excinfo:
        _call(lambda _r: httpx.Response(200, text="<html>gateway</html>"))

    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    assert "html" in excinfo.value.details["body_head"]


def test_retry_after_accepts_http_date_and_garbage() -> None:
    """``Retry-After`` 同时接受秒数与 HTTP 日期；空/无法解析返回 ``None``，过去的时间归 0。"""
    assert retry_after_seconds({"Retry-After": "2"}) == 2.0
    assert retry_after_seconds({"Retry-After": ""}) is None
    assert retry_after_seconds({}) is None
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    # HTTP 日期形式（过去的时间 ⇒ 0，而不是负数）
    assert retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0


def test_backoff_grows_and_is_capped() -> None:
    """退避是指数增长且有上限；抖动只影响 25% 以内。"""
    assert backoff_seconds(0) == pytest.approx(0.5, abs=0.5 * 0.25)
    assert backoff_seconds(3) == pytest.approx(4.0, abs=4.0 * 0.25)
    assert backoff_seconds(20) <= 8.0 * 1.25
