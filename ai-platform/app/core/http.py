"""云端 provider 共用的「带退避重试的 JSON POST」。

三处调用（火山方舟 / 硅基流动 embedding、硅基流动 rerank）的失败语义是同一套：
只有 429/5xx/超时值得重试，4xx 立刻失败，并尊重上游的 ``Retry-After``。各写一遍的
下场是「总有一天有一处忘了把 401 排除在重试之外」，而那处恰好是线上用的。

失败映射（与 ``app/core/exceptions`` 对齐）：重试后仍失败 ⇒ ``DEPENDENCY_UNAVAILABLE``；
4xx ⇒ ``RETRIEVAL_FAILED`` 并带出上游原文，否则「key 过期」会被埋成「重试 3 次后 503」。

两个实测细节：``Retry-After`` 可能是秒数也可能是 HTTP 日期，且可能长到超出本服务
超时预算 ⇒ 必须设上限；退避加 ±25% 抖动，否则多 Worker 被限流后会一起醒来再撞墙。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.exceptions import AppError, ErrorCode

logger = logging.getLogger("app.core.http")

#: 值得重试的状态码：限流、超时类、上游 5xx。刻意不含 400/401/403/404/422 —— 重试一万次也不会变。
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: ``Retry-After`` 的等待上限（秒）：上游说"等 600 秒"时不能真的挂 10 分钟。
MAX_RETRY_AFTER_SECONDS = 10.0

#: 退避基数与上限（秒）
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """解析 ``Retry-After``（秒数或 HTTP 日期），取不到返回 ``None``。"""
    raw = (headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:  # pragma: no cover - 解析异常已在上一步被捕获
        return None
    import datetime as _dt

    now = _dt.datetime.now(tz=target.tzinfo)
    return max(0.0, (target - now).total_seconds())


def backoff_seconds(attempt: int, *, base: float = BACKOFF_BASE_SECONDS) -> float:
    """第 ``attempt`` 次失败后的退避时长（从 0 开始计数），带 ±25% 抖动。"""
    delay = min(base * (2**attempt), BACKOFF_MAX_SECONDS)
    return delay * random.uniform(0.75, 1.25)


def post_json_with_retry(
    client: httpx.Client,
    url: str,
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
    max_retries: int,
    provider: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """POST JSON 并解析响应；按上面的规则重试与报错。

    Args:
        max_retries: 失败后的**额外**尝试次数（``0`` = 只打一次）。
        provider: 报错信息里的上游名字（如 ``"siliconflow"``），便于一眼定位。
        sleep: 注入点，测试里换成"记录而不真的睡"。
    """
    attempts = max(1, max_retries + 1)
    last_error = "unknown"
    for attempt in range(attempts):
        try:
            response = client.post(url, json=dict(payload), headers=dict(headers), timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                sleep(backoff_seconds(attempt))
                continue
            break

        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError as exc:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"{provider} 返回了非 JSON 响应（HTTP 200）",
                    {"url": url, "body_head": response.text[:200]},
                ) from exc
            if not isinstance(body, dict):
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"{provider} 响应结构不是对象",
                    {"url": url, "type": type(body).__name__},
                )
            return body

        detail = response.text[:400]
        if response.status_code not in RETRYABLE_STATUS:
            # 4xx：立刻失败 + 带出上游原文，否则「key 过期」会被埋成「重试 3 次后 503」
            # （docs/12-§4.4 同类教训）。
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"{provider} 拒绝了请求（HTTP {response.status_code}）",
                {"url": url, "status": response.status_code, "detail": detail},
            )

        last_error = f"HTTP {response.status_code}: {detail}"
        if attempt >= attempts - 1:
            break
        wait = retry_after_seconds(response.headers)
        if wait is None:
            sleep(backoff_seconds(attempt))
        else:
            sleep(min(wait, MAX_RETRY_AFTER_SECONDS))

    raise AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        f"{provider} 调用失败（已尝试 {attempts} 次）：{last_error}",
        {"url": url, "attempts": attempts},
    )


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_MAX_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "RETRYABLE_STATUS",
    "backoff_seconds",
    "post_json_with_retry",
    "retry_after_seconds",
]
