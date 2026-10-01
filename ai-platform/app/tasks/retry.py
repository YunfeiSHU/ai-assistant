"""延迟重试队列（``docs/08`` §5.2，``REQ-TASK-004``）。

不能直接 ``await asyncio.sleep(backoff)`` 再重试：那会占住一个 Worker 并发额度最长 16s，而
文档明确要求重试**不阻塞 Worker**。所以退避后的重投交给一个延迟队列 —— 到期时间当 score
存 ZSET，Worker 主循环在两条消息之间顺手把到期的捞出来重投。

捞取用 Lua 是因为 ``ZRANGEBYSCORE`` 与 ``ZREM`` 之间若被另一个 Worker 插进来，同一任务会被
重投两次；Worker 端幂等（``docs/08`` §5.3）能兜住，但那意味着白跑一遍可能几分钟的入库。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import ErrorCode
from app.infrastructure.redis.client import RedisUnavailable, create_redis_client, redis_text

if TYPE_CHECKING:  # 只为标注：``models`` 不认识 ``retry``，不会构成循环导入
    from app.tasks.models import Task

logger = logging.getLogger("app.tasks.retry")

#: 延迟重试队列的 Key（``docs/09`` §4）
RETRY_ZSET = "retry:zset"

#: 一次最多认领多少条（防止积压时一次拉爆内存）
CLAIM_LIMIT = 32

#: **不可重试**的错误码（``docs/08`` §5.2）。判定依据是「重试一次结果会不会变」：
#: 文件类型、文档内容、向量维度、参数合法性都是确定的，重试只会浪费 3 个任务周期、
#: 刷满 ``retry_count``，对用户而言只是「等了 21 秒才拿到同一个错误」。
NON_RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        str(ErrorCode.UNSUPPORTED_FILE_TYPE),
        str(ErrorCode.UNPROCESSABLE_DOCUMENT),
        str(ErrorCode.VECTOR_DIM_MISMATCH),
        str(ErrorCode.INVALID_ARGUMENT),
        # 文档已被删除/不存在：重试不会让它回来，人工处理更合适
        str(ErrorCode.DOCUMENT_NOT_FOUND),
        str(ErrorCode.KB_NOT_FOUND),
    }
)


def is_retryable(code: str | None) -> bool:
    """该错误码是否值得自动重试（``docs/08`` §5.2）。

    未知错误码一律视为可重试：默认「不值得重试」会让偶发的基础设施故障（连接池耗尽、
    DNS 抖动、上游 503 用了项目自定义码）直接变成终态失败，而这类失败恰恰是重试最有效的场景。
    """
    if not code:
        return True
    return code not in NON_RETRYABLE_CODES


def error_retryable(task: Task) -> bool:
    """失败事件（``error`` 帧 / SSE ``event: error``）里的 ``retryable``。

    两个条件同时成立才算可重试：错误码本身值得重试（``is_retryable``）、重试预算还没用尽
    （``task.can_retry``）。只判后者会把 ``UNPROCESSABLE_DOCUMENT`` 这类确定性错误报成
    「可重试」（``docs/02`` §5.3 明确标为不可重试），客户端于是乖乖重试到预算耗尽，最后还是
    同一个错误；反过来只判错误码，又会让预算已耗尽的任继续鼓励用户点重试。
    """
    code = task.error.code if task.error else None
    return is_retryable(code) and task.can_retry


#: 原子「查到期 + 认领」的脚本，返回 ``[member, score, ...]`` 扁平数组。
#: 不用 ``ZADD GT``/``ZPOPMIN`` 组合：``ZPOPMIN`` 会把**未到期**的也弹出来。
_CLAIM_SCRIPT = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #due == 0 then
    return {}
end
local out = {}
for i = 1, #due do
    local score = redis.call('ZSCORE', KEYS[1], due[i])
    out[#out + 1] = due[i]
    out[#out + 1] = score
end
redis.call('ZREM', KEYS[1], unpack(due))
return out
"""


def retry_delay(
    attempt: int,
    *,
    base: float = 1.0,
    jitter: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    """第 ``attempt`` 次重试前的等待秒数（``docs/08`` §5.2：``1s, 4s, 16s`` × ``[0.8, 1.2]``）。

    ``attempt`` 从 1 开始。抖动是必需的：否则一批同时失败的任务会在同一毫秒一起回来，
    把刚恢复的下游再打挂一次（thundering herd）。
    """
    if attempt < 1:
        attempt = 1
    generator = rng or random
    raw = base * (4 ** (attempt - 1))
    factor = 1.0 + generator.uniform(-jitter, jitter if jitter else 0.0)
    return max(0.0, raw * factor)


@dataclass(frozen=True, slots=True)
class RetryEntry:
    """一条到期待重投的重试记录。"""

    task_id: str
    attempt: int
    due_at: float


def encode_member(task_id: str, attempt: int) -> str:
    """ZSET 成员编码 ``{task_id}:{attempt}``（``task_id`` 不含 ``:``）。"""
    return f"{task_id}:{attempt}"


def decode_member(member: str) -> tuple[str, int]:
    """成员解码；格式非法时抛 ``ValueError``。"""
    task_id, _, raw = member.rpartition(":")
    if not task_id or not raw.isdigit():
        msg = f"重试队列成员格式非法：{member!r}"
        raise ValueError(msg)
    return task_id, int(raw)


@runtime_checkable
class RetryQueue(Protocol):
    """延迟重试队列端口。"""

    async def schedule(self, task_id: str, *, attempt: int, delay: float) -> None:
        """安排在 ``delay`` 秒后重投。"""
        ...

    async def claim_due(self, *, limit: int = CLAIM_LIMIT) -> list[RetryEntry]:
        """原子认领所有已到期的记录（认领到的记录会被移出队列）。"""
        ...

    async def close(self) -> None: ...


class InMemoryRetryQueue:
    """进程内实现（``INFRA_BACKEND=memory`` / 测试）。

    时钟可注入：测试推进假时间就能断言退避行为，不必 ``sleep(1)``（``docs/11`` §2.2
    要求用例既稳定又快）。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, RetryEntry] = {}

    async def schedule(self, task_id: str, *, attempt: int, delay: float) -> None:
        """见 :class:`RetryQueue`。同一任务重复安排以**最后一次**为准。"""
        self._items[task_id] = RetryEntry(
            task_id=task_id, attempt=attempt, due_at=self._clock() + max(0.0, delay)
        )

    async def claim_due(self, *, limit: int = CLAIM_LIMIT) -> list[RetryEntry]:
        """见 :class:`RetryQueue`。"""
        now = self._clock()
        due = [item for item in self._items.values() if item.due_at <= now]
        due.sort(key=lambda item: item.due_at)
        selected = due[: max(1, limit)]
        for item in selected:
            self._items.pop(item.task_id, None)
        return selected

    async def close(self) -> None:
        """释放资源；**不清空待重试记录**。

        ``close`` 是「关连接」不是「清数据」：清空会把已安排退避的任务静默丢掉，
        它们再也不会被重投、也不会进死信。进程内实现没有连接可关，所以是空操作
        （与 Redis 实现同语义）。
        """
        return None

    @property
    def size(self) -> int:
        """当前排队条数（测试与诊断用）。"""
        return len(self._items)


class RedisRetryQueue:
    """Redis ZSET 实现（``INFRA_BACKEND=real``，跨进程共享）。"""

    def __init__(self, client: Any, *, key: str = RETRY_ZSET) -> None:
        self._client = client
        self._key = key

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisRetryQueue:
        """按 ``REDIS_URL`` 构造。"""
        return cls(
            create_redis_client(
                settings, hint="uv add redis 或将 INFRA_BACKEND 设为 memory（延迟重试队列）"
            )
        )

    async def schedule(self, task_id: str, *, attempt: int, delay: float) -> None:
        """``ZADD`` 以到期时间戳为 score（``docs/09`` §4）。"""
        due_at = time.time() + max(0.0, delay)
        await self._client.zadd(self._key, {encode_member(task_id, attempt): due_at})

    async def claim_due(self, *, limit: int = CLAIM_LIMIT) -> list[RetryEntry]:
        """Lua 原子认领（见模块 docstring）。"""
        raw = await self._client.eval(
            _CLAIM_SCRIPT, 1, self._key, str(time.time()), str(max(1, limit))
        )
        return _parse_claim(raw)

    async def close(self) -> None:
        """关闭连接（``aclose`` 是新名，旧版只有 ``close``）。"""
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is None:  # pragma: no cover - 替身
            return
        result = closer()
        if asyncio.iscoroutine(result):
            await result


def _parse_claim(raw: Any) -> list[RetryEntry]:
    """把 Lua 返回的扁平数组解析成 ``RetryEntry`` 列表。

    脏成员（格式非法）直接丢弃并告警：队列里的一条坏数据不该让整个 Worker 主循环抛异常。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    entries: list[RetryEntry] = []
    for index in range(0, len(raw) - 1, 2):
        member = redis_text(raw[index])
        try:
            task_id, attempt = decode_member(member)
        except ValueError:
            logger.warning("task.retry_member_invalid", extra={"member": member})
            continue
        entries.append(
            RetryEntry(task_id=task_id, attempt=attempt, due_at=_as_float(raw[index + 1]))
        )
    return entries


def _as_float(value: Any) -> float:
    """把 Redis 返回的 score 转成 ``float``（可能是 ``bytes``/``str``/``float``）。"""
    try:
        return float(redis_text(value))
    except ValueError:  # pragma: no cover - score 一定可转
        return 0.0


def build_retry_queue(settings: Settings) -> RetryQueue:
    """按 ``INFRA_BACKEND`` 选择实现。

    Redis 不可用时**退化成进程内队列**并告警：重试只在同一个进程内的 Worker 上生效
    （跨进程失效），但比让 Worker 起不来好 —— 重试的价值是「尽力再试一次」，
    不是可用性前置条件。
    """
    if not settings.uses_shared_task_store:
        return InMemoryRetryQueue()
    try:
        return RedisRetryQueue.from_settings(settings)
    except RedisUnavailable as exc:
        logger.warning("task.retry_queue_degraded", extra={"error": str(exc)})
        return InMemoryRetryQueue()


__all__ = [
    "CLAIM_LIMIT",
    "NON_RETRYABLE_CODES",
    "RETRY_ZSET",
    "InMemoryRetryQueue",
    "RedisRetryQueue",
    "RetryEntry",
    "RetryQueue",
    "build_retry_queue",
    "decode_member",
    "encode_member",
    "error_retryable",
    "is_retryable",
    "retry_delay",
]
