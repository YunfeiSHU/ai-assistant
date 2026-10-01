"""延迟重试队列与退避策略（``docs/08`` §5.2）。

重试最容易写错的两处：

* **退避没有抖动** —— 一批同时失败的任务会在同一毫秒一起回来，把刚恢复的下游
  再打挂一次；
* **不该重试的错误也在重试** —— 文件格式不支持重试三次只是浪费三个周期，
  最后仍要人工介入。

所以这里既断言数值区间，也断言「哪些错误码绝不重试」。
"""

from __future__ import annotations

import random

import pytest

from app.core.exceptions import ErrorCode
from app.tasks.retry import (
    CLAIM_LIMIT,
    NON_RETRYABLE_CODES,
    InMemoryRetryQueue,
    RedisRetryQueue,
    decode_member,
    encode_member,
    is_retryable,
    retry_delay,
)


class _Clock:
    """可推进的假时钟（避免用例真的 ``sleep``）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 退避
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("attempt,expected", [(1, 1.0), (2, 4.0), (3, 16.0)])
def test_retry_delay_is_exponential_without_jitter(attempt: int, expected: float) -> None:
    """``docs/08`` §5.2 的 ``1s, 4s, 16s``（抖动设为 0 时是精确值）。"""
    assert retry_delay(attempt, base=1.0, jitter=0.0) == expected


def test_retry_delay_applies_jitter_within_bounds() -> None:
    """抖动落在 ``[0.8, 1.2]`` 倍区间内（否则退避就白做了）。"""
    values = [retry_delay(3, jitter=0.2, rng=random.Random(seed)) for seed in range(50)]
    assert min(values) >= 16.0 * 0.8
    assert max(values) <= 16.0 * 1.2
    assert len(set(values)) > 1, "固定抖动 = 没有抖动"


def test_retry_delay_clamps_attempt_below_one() -> None:
    """``attempt=0`` 不能算出比首次更短的等待。"""
    assert retry_delay(0, jitter=0.0) == retry_delay(1, jitter=0.0)


def test_retry_delay_never_negative() -> None:
    """抖动参数异常大时也不能返回负数（``sleep`` 负数会抛 ValueError）。"""
    assert retry_delay(1, jitter=5.0) >= 0.0


# ---------------------------------------------------------------------------
# 可重试判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(NON_RETRYABLE_CODES))
def test_known_permanent_errors_are_not_retried(code: str) -> None:
    """这些错误重试三次仍然是同样的结果，只是浪费三个周期。"""
    assert is_retryable(code) is False


@pytest.mark.parametrize(
    "code",
    [None, "", str(ErrorCode.MQ_UNAVAILABLE), str(ErrorCode.INTERNAL_ERROR), "SOME_NEW_CODE"],
)
def test_unknown_errors_are_retried(code: str | None) -> None:
    """未知错误码一律视为可重试：默认不重试会让偶发的基础设施故障直接变终态。"""
    assert is_retryable(code) is True


# ---------------------------------------------------------------------------
# 成员编码
# ---------------------------------------------------------------------------
def test_member_roundtrip() -> None:
    """``{task_id}:{attempt}`` 编解码往返。"""
    assert encode_member("task_01H", 3) == "task_01H:3"
    assert decode_member("task_01H:3") == ("task_01H", 3)


def test_member_uses_last_colon() -> None:
    """``task_id`` 里含 ``:`` 时也能正确切分（用 ``rpartition``）。"""
    assert decode_member("task:with:colons:2") == ("task:with:colons", 2)


@pytest.mark.parametrize("member", ["", "task_only", "task:abc", ":1"])
def test_member_rejects_invalid(member: str) -> None:
    """坏成员必须抛错，否则会静默把某条任务永远留在队列里。"""
    with pytest.raises(ValueError):
        decode_member(member)


# ---------------------------------------------------------------------------
# 内存队列
# ---------------------------------------------------------------------------
async def test_in_memory_queue_honours_delay() -> None:
    """未到期的记录**不能**被认领（提前重投等于没有退避）。"""
    clock = _Clock()
    queue = InMemoryRetryQueue(clock=clock)
    await queue.schedule("task_1", attempt=1, delay=4.0)

    assert await queue.claim_due() == []
    clock.advance(4.0)
    claimed = await queue.claim_due()
    assert [entry.task_id for entry in claimed] == ["task_1"]
    assert claimed[0].attempt == 1
    # 认领即移出：否则每轮轮询都会重投同一批
    assert await queue.claim_due() == []
    await queue.close()


async def test_in_memory_close_keeps_pending_entries() -> None:
    """``close`` 只释放资源，不能把待重试的记录一起清掉。

    清掉的表现是「任务永远停在 FAILED，既不重投也不进死信」—— 没有任何错误日志。
    """
    queue = InMemoryRetryQueue(clock=_Clock())
    await queue.schedule("task_1", attempt=1, delay=1.0)
    await queue.close()
    assert queue.size == 1


async def test_in_memory_queue_resort_by_due_time() -> None:
    """按到期时间早到晚返回（先到的先重投）。"""
    clock = _Clock()
    queue = InMemoryRetryQueue(clock=clock)
    await queue.schedule("task_late", attempt=1, delay=10.0)
    await queue.schedule("task_soon", attempt=1, delay=1.0)

    clock.advance(10.0)
    claimed = await queue.claim_due()
    assert [entry.task_id for entry in claimed] == ["task_soon", "task_late"]


async def test_in_memory_queue_negative_delay_clamped() -> None:
    """负延时当作「立刻」而不是把记录丢到过去再也认领不到。"""
    queue = InMemoryRetryQueue(clock=_Clock())
    await queue.schedule("task_1", attempt=1, delay=-5.0)
    assert [entry.task_id for entry in await queue.claim_due()] == ["task_1"]


async def test_in_memory_queue_dedupes_by_task() -> None:
    """同一任务重复安排以最后一次为准（避免堆积多份同任务重试）。"""
    clock = _Clock()
    queue = InMemoryRetryQueue(clock=clock)
    await queue.schedule("task_1", attempt=1, delay=1.0)
    await queue.schedule("task_1", attempt=2, delay=1.0)
    assert queue.size == 1
    clock.advance(1.0)
    assert (await queue.claim_due())[0].attempt == 2


async def test_in_memory_queue_respects_limit() -> None:
    """一次最多认领 ``limit`` 条（保护下游，不让重试洪峰打进来）。"""
    queue = InMemoryRetryQueue(clock=_Clock())
    for index in range(5):
        await queue.schedule(f"task_{index}", attempt=1, delay=0.0)
    claimed = await queue.claim_due(limit=2)
    assert len(claimed) == 2
    assert queue.size == 3


def test_claim_limit_is_conservative() -> None:
    """单次认领上限别设太大（它是「一次轮询最多重投多少」的刹车）。"""
    assert 0 < CLAIM_LIMIT <= 64


# ---------------------------------------------------------------------------
# Redis 实现
# ---------------------------------------------------------------------------
class _FakeZset:
    """只实现 ``zadd`` / ``eval`` 的 Redis 替身。

    断言的重点是「Lua 脚本收到了什么」：这一层的正确性完全由那条脚本决定
    （它的原子性是真 Redis 的属性，不属于单测范围，集成测试里另测）。
    """

    def __init__(self, claimed: list[str] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._claimed = claimed or []

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.calls.append(("zadd", (key, mapping)))
        return len(mapping)

    async def eval(self, script: str, numkeys: int, *args: str) -> object:
        self.calls.append(("eval", (script, numkeys, args)))
        if not self._claimed:
            return []
        return [item for member in self._claimed for item in (member, 1234.5)]

    async def aclose(self) -> None:
        self.calls.append(("aclose", None))


async def test_redis_queue_scores_by_absolute_due_time() -> None:
    """score 是绝对到期时间戳（用 ``time.time()`` 而不是单调时钟：跨进程要能比对）。"""
    client = _FakeZset()
    queue = RedisRetryQueue(client)
    await queue.schedule("task_1", attempt=2, delay=0.0)
    name, payload = client.calls[0]
    assert name == "zadd"
    key, mapping = payload  # type: ignore[misc]
    assert key == "retry:zset"
    assert list(mapping) == ["task_1:2"]


async def test_redis_queue_parses_claimed_entries() -> None:
    """Lua 返回的是 ``[member, score, ...]`` 扁平数组，要成对解析。"""
    client = _FakeZset(claimed=["task_a:2", "task_b:1"])
    entries = await RedisRetryQueue(client).claim_due()
    assert [(entry.task_id, entry.attempt) for entry in entries] == [("task_a", 2), ("task_b", 1)]
    assert entries[0].due_at == 1234.5


async def test_redis_queue_close_tolerates_missing_aclose() -> None:
    """旧版 redis 只有 ``close``，替身可能两个都没有 —— 关停不该抛。"""

    class _Bare:
        pass

    await RedisRetryQueue(_Bare()).close()  # type: ignore[arg-type]
