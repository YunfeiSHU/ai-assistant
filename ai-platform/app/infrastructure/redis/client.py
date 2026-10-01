"""Redis 连接与取值兼容（跨模块共用的最小面）。

M5 的会话上下文（``app/memory/redis_store.py``）与 M7 的任务存储 / 事件总线 / 延迟重试队列
（``app/tasks/``）都要连 Redis，但它们的重试策略、Key 前缀、锁语义、序列化格式完全不同 ——
真正共用的只有两件事：「怎么建连接」与「连不上时抛什么」。

``create_redis_client`` **懒导入** ``redis``：它不在基础依赖里（``INFRA_BACKEND=memory`` 时
根本用不到），缺失时给一条可执行的提示。``RedisUnavailable`` 继承 ``AppError``（``503`` +
``retryable``），于是「驱动缺失」与「连不上」对上层是同一件事 —— 都走各模块自己的降级分支。
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode


class RedisUnavailable(AppError):
    """Redis 不可用（未安装驱动 / 连接失败）。

    继承 ``AppError`` 是为了让上层能直接把它归入各自的降级分支，而不必区分「驱动缺失」与
    「连不上」—— 对调用方而言都是「这个能力这次没有」。
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.DEPENDENCY_UNAVAILABLE, message, details or {})


def create_redis_client(settings: Settings, *, hint: str = "") -> Any:
    """按 ``REDIS_URL`` 建立异步客户端（``decode_responses=True``）。

    Args:
        settings: 应用配置。
        hint: 追加到错误提示里的模块级建议（例如「改用 INFRA_BACKEND=memory」）。

    Raises:
        RedisUnavailable: 未安装 ``redis`` 依赖。
    """
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - 取决于本机是否装了 redis 包
        details = {"hint": hint or "uv add redis 或将 INFRA_BACKEND 设为 memory"}
        raise RedisUnavailable("未安装 redis 依赖，无法使用 INFRA_BACKEND=real", details) from exc
    # ``decode_responses=True`` 统一产出 ``str``：否则每个取值点都要处理 bytes，而漏掉一处的
    # 表现是「比较永远不相等」这种极难定位的静默错误。
    return Redis.from_url(settings.redis_url, decode_responses=True)


def redis_text(value: Any) -> str:
    """把 Redis 返回值统一成 ``str``。

    正常路径上 ``decode_responses=True`` 已保证是 ``str``，但替身与 ``HSET`` 的返回值可能是
    ``bytes``/``int``/``str``。与其在每个取值点写 ``if isinstance(...)``，不如收口到这里 ——
    少一处漏写就少一种静默错误。
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    return str(value)


__all__ = ["RedisUnavailable", "create_redis_client", "redis_text"]
