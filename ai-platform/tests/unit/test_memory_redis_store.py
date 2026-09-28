"""Redis 会话上下文实现单测（``docs/07`` §2.1，``REQ-MEM-001``）。

这里**不连真 Redis**，而是手写一个 ``RedisCommands`` 替身（``FakeRedis``）。
理由：本模块的价值在「发的是哪几条命令、命令顺序是什么」，不在 Redis 本身的正确性。
真连一个容器只能证明「Redis 没坏」，反而把 `LTRIM` 有没有发、TTL 有没有刷新
这类真正会出线上问题的点漏掉。

重点校验：

* ``append`` = ``RPUSH`` → ``LTRIM(-keep,-1)`` → **两个键都** ``EXPIRE``；
* 归属检查读独立键（列表可能是空的，不能从元素里取 owner）；
* 释放锁必须是 compare-and-delete 的 Lua（直接 DEL 会删掉别人的锁）；
* 坏数据只跳过该条，不让整个会话报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from typing import Any

import pytest
from tests.conftest import build_settings

from app.core.errors import AppError, ErrorCode
from app.memory.context_store import ConversationSummary, StoredMessage
from app.memory.redis_store import (
    LOCK_TTL_SECONDS,
    RedisConversationStore,
    RedisLock,
    RedisUnavailable,
    create_redis_commands,
)


class FakeRedis:
    """记录命令调用的最小 Redis 替身（只实现本模块用到的那几条）。"""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}
        self.calls: list[tuple[Any, ...]] = []
        #: 让 ``set(nx=True)`` 永远失败，用于测锁超时
        self.reject_nx = False

    async def rpush(self, key: str, *values: str) -> int:
        self.calls.append(("rpush", key, *values))
        bucket = self.lists.setdefault(key, [])
        bucket.extend(values)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> Any:
        self.calls.append(("ltrim", key, start, stop))
        bucket = self.lists.get(key)
        if bucket is None:
            return None
        size = len(bucket)
        if start < 0:
            start = max(0, size + start)
        if stop < 0:
            stop = size + stop
        self.lists[key] = bucket[start : stop + 1] if stop >= start else []
        return None

    async def lrange(self, key: str, start: int, stop: int) -> list[Any]:
        self.calls.append(("lrange", key, start, stop))
        bucket = self.lists.get(key, [])
        size = len(bucket)
        if start < 0:
            start = max(0, size + start)
        if stop < 0:
            stop = size + stop
        return list(bucket[start : stop + 1]) if stop >= start else []

    async def expire(self, key: str, seconds: int) -> Any:
        self.calls.append(("expire", key, seconds))
        self.expires[key] = seconds
        return True

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        return self.strings.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any:
        self.calls.append(("set", key, value, nx, ex))
        if nx and (self.reject_nx or key in self.strings):
            return None
        self.strings[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self.calls.append(("delete", *keys))
        removed = 0
        for key in keys:
            if key in self.strings:
                del self.strings[key]
                removed += 1
            if key in self.lists:
                del self.lists[key]
                removed += 1
        return removed

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any:
        self.calls.append(("eval", script, numkeys, *keys_and_args))
        key, token = keys_and_args[0], keys_and_args[1]
        if self.strings.get(key) == token:
            del self.strings[key]
            return 1
        return 0

    # -- 断言辅助 ------------------------------------------------------
    def commands(self, name: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == name]


def _message(
    index: int, *, content: str | None = None, created_at: str | None = None
) -> StoredMessage:
    return StoredMessage(
        role="user",
        content=content if content is not None else f"第 {index} 条消息",
        message_id=f"msg_{index}",
        created_at=created_at or f"2026-09-28T10:0{index}:00.000Z",
    )


@pytest.fixture()
def redis_client() -> FakeRedis:
    return FakeRedis()


@pytest.fixture()
def redis_store(redis_client: FakeRedis) -> RedisConversationStore:
    return RedisConversationStore(redis_client, build_settings(memory_max_messages=4))


# ---------------------------------------------------------------------------
# 建会话与归属
# ---------------------------------------------------------------------------
async def test_ensure_creates_owner_with_nx_and_ttl(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    calls = redis_client.commands("set")
    assert calls[0][1] == "ctx:owner:cv_1"
    assert calls[0][3] is True  # nx
    assert calls[0][4] and calls[0][4] > 0  # ex = TTL 秒
    assert redis_client.strings["ctx:owner:cv_1"] == "u_1"


async def test_ensure_is_idempotent_for_same_owner(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    redis_client.calls.clear()
    await redis_store.ensure("cv_1", "u_1")
    # 第二次写必然失败（NX），随后用 GET 复核归属；关键是**不能**裸 set 覆盖 owner，
    # 否则任何人拿同一个 conversation_id 就能认领别人的会话
    assert redis_client.commands("set") == [("set", "ctx:owner:cv_1", "u_1", True, 604800)]
    assert redis_client.commands("get") == [("get", "ctx:owner:cv_1")]


async def test_ensure_rejects_foreign_owner(
    redis_store: RedisConversationStore, redis_client: FakeRedis
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    with pytest.raises(AppError) as excinfo:
        await redis_store.ensure("cv_1", "u_2")
    # 「归属不符」与「不存在」同码：不泄露资源是否存在
    assert excinfo.value.code is ErrorCode.CONVERSATION_NOT_FOUND


async def test_reads_and_writes_require_ownership(redis_store: RedisConversationStore) -> None:
    with pytest.raises(AppError):
        await redis_store.recent("cv_missing", "u_1", turns=3)
    with pytest.raises(AppError):
        await redis_store.all_messages("cv_missing", "u_1")
    with pytest.raises(AppError):
        await redis_store.clear("cv_missing", "u_1")
    with pytest.raises(AppError):
        await redis_store.save_summary(
            "cv_missing", "u_1", ConversationSummary(content="x", covered_until="")
        )


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
async def test_append_writes_trims_and_refreshes_both_ttls(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    settings = build_settings(memory_max_messages=4, memory_ttl_days=7)
    store = RedisConversationStore(redis_client, settings)
    await store.ensure("cv_1", "u_1")
    redis_client.calls.clear()

    await store.append("cv_1", "u_1", [_message(0), _message(1)])

    rpush = redis_client.commands("rpush")
    assert len(rpush) == 1
    assert rpush[0][1] == "ctx:cv_1"
    assert len(rpush[0]) == 4  # key + 2 条消息

    # 只保留最近 N 条：内存实现是切片，Redis 侧必须用 LTRIM 等价
    assert redis_client.commands("ltrim") == [("ltrim", "ctx:cv_1", -4, -1)]

    # 两个键都要续期：漏掉 owner 键会让活跃会话 7 天后「换主」
    expired = {call[1] for call in redis_client.commands("expire")}
    assert expired == {"ctx:cv_1", "ctx:owner:cv_1"}
    for call in redis_client.commands("expire"):
        assert call[2] == 7 * 86400


async def test_append_trims_to_max_messages(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    for index in range(6):
        await redis_store.append("cv_1", "u_1", [_message(index)])
    stored = redis_client.lists["ctx:cv_1"]
    assert len(stored) == 4
    assert [json.loads(item)["message_id"] for item in stored] == [
        "msg_2",
        "msg_3",
        "msg_4",
        "msg_5",
    ]


async def test_append_without_messages_is_noop(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    redis_client.calls.clear()
    await redis_store.append("cv_1", "u_1", [])
    assert redis_client.calls == []


async def test_append_creates_session_implicitly(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    """首次写入即建会话（与内存实现一致），否则新会话第一条消息会丢。"""
    await redis_store.append("cv_new", "u_1", [_message(0)])
    assert redis_client.strings["ctx:owner:cv_new"] == "u_1"
    assert len(redis_client.lists["ctx:cv_new"]) == 1


async def test_append_takes_lock_with_ttl(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    redis_client.calls.clear()
    await redis_store.append("cv_1", "u_1", [_message(0)])

    acquire = [call for call in redis_client.commands("set") if call[1] == "lock:ctx:cv_1"]
    assert len(acquire) == 1
    assert acquire[0][3] is True
    assert acquire[0][4] == LOCK_TTL_SECONDS
    # 释放用 Lua compare-and-delete，而不是裸 DEL
    assert redis_client.commands("eval")
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in redis_client.commands("eval")[0][1]


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
async def test_recent_returns_last_n_turns(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    for index in range(6):
        await redis_store.append("cv_1", "u_1", [_message(index)])
    messages = await redis_store.recent("cv_1", "u_1", turns=2)
    assert [message.message_id for message in messages] == ["msg_2", "msg_3", "msg_4", "msg_5"]


async def test_recent_skips_messages_covered_by_summary(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    """已被摘要覆盖的消息不再重复注入（否则上下文被同一段话占两遍）。"""
    await redis_store.ensure("cv_1", "u_1")
    for index in range(4):
        await redis_store.append("cv_1", "u_1", [_message(index)])
    await redis_store.save_summary(
        "cv_1",
        "u_1",
        ConversationSummary(
            content="摘要",
            covered_until="2026-09-28T10:01:00.000Z",
            source_message_count=2,
        ),
    )
    messages = await redis_store.recent("cv_1", "u_1", turns=5)
    assert [message.message_id for message in messages] == ["msg_2", "msg_3"]


async def test_recent_drops_blank_content(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    await redis_store.append("cv_1", "u_1", [_message(0, content="   "), _message(1)])
    messages = await redis_store.recent("cv_1", "u_1", turns=5)
    assert [message.message_id for message in messages] == ["msg_1"]


async def test_corrupted_element_is_skipped_not_fatal(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    """单条坏数据只跳过它自己：整段上下文不可用远比其他条丢失严重。"""
    await redis_store.ensure("cv_1", "u_1")
    await redis_store.append("cv_1", "u_1", [_message(0)])
    redis_client.lists["ctx:cv_1"].insert(0, "{不是合法 JSON")
    redis_client.lists["ctx:cv_1"].insert(1, '"合法 JSON 但不是对象"')

    messages = await redis_store.recent("cv_1", "u_1", turns=5)
    assert [message.message_id for message in messages] == ["msg_0"]
    assert [message.message_id for message in await redis_store.all_messages("cv_1", "u_1")] == [
        "msg_0"
    ]


async def test_summary_roundtrip(redis_store: RedisConversationStore) -> None:
    await redis_store.ensure("cv_1", "u_1")
    assert await redis_store.summary("cv_1", "u_1") is None

    await redis_store.save_summary(
        "cv_1",
        "u_1",
        ConversationSummary(
            content="用户目标是实现 ai-platform",
            covered_until="2026-09-28T10:00:00.000Z",
            source_message_count=20,
            token_count=128,
        ),
    )
    summary = await redis_store.summary("cv_1", "u_1")
    assert summary is not None
    assert summary.content == "用户目标是实现 ai-platform"
    assert summary.covered_until == "2026-09-28T10:00:00.000Z"
    assert summary.source_message_count == 20
    assert summary.token_count == 128


async def test_corrupted_summary_returns_none(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    await redis_store.ensure("cv_1", "u_1")
    redis_client.strings["ctx:summary:cv_1"] = "not-json"
    assert await redis_store.summary("cv_1", "u_1") is None


async def test_summary_tolerates_missing_fields(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    """老数据缺字段时按空值处理，而不是 ``KeyError`` 把上下文整个打挂。"""
    await redis_store.ensure("cv_1", "u_1")
    redis_client.strings["ctx:summary:cv_1"] = json.dumps({"content": "只有正文"})
    summary = await redis_store.summary("cv_1", "u_1")
    assert summary is not None
    assert summary.covered_until == ""
    assert summary.source_message_count == 0
    assert summary.token_count == 0


# ---------------------------------------------------------------------------
# 清空
# ---------------------------------------------------------------------------
async def test_clear_removes_context_but_keeps_ownership(
    redis_client: FakeRedis, redis_store: RedisConversationStore
) -> None:
    """清空的是「上下文」而不是「会话归属」。

    若把 owner 键一起删掉，下一次写入会把会话当成新建 —— 期间任何人拿同一个
    ``conversation_id`` 都能认领它（越权读别人的历史）。
    """
    await redis_store.ensure("cv_1", "u_1")
    await redis_store.append("cv_1", "u_1", [_message(0)])
    await redis_store.save_summary("cv_1", "u_1", ConversationSummary(content="摘要"))

    await redis_store.clear("cv_1", "u_1")

    assert redis_client.lists.get("ctx:cv_1") is None
    assert redis_client.strings.get("ctx:summary:cv_1") is None
    assert redis_client.strings["ctx:owner:cv_1"] == "u_1"
    assert await redis_store.recent("cv_1", "u_1", turns=5) == []


# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------
async def test_lock_release_uses_compare_and_delete(redis_client: FakeRedis) -> None:
    lock = RedisLock(redis_client, "lock:ctx:cv_1")
    async with lock:
        assert redis_client.strings["lock:ctx:cv_1"] != ""
    assert redis_client.strings.get("lock:ctx:cv_1") is None
    _script, numkeys, key, token = redis_client.commands("eval")[0][1:]
    assert numkeys == 1
    assert key == "lock:ctx:cv_1"
    assert token  # 释放时必须带上自己的 token，否则会删掉别人的锁


async def test_lock_release_does_not_delete_foreign_token(redis_client: FakeRedis) -> None:
    """锁已超时被他人持有时，迟到者不能释放 —— 这正是用 Lua 而非 DEL 的原因。"""
    lock = RedisLock(redis_client, "lock:ctx:cv_1")
    async with lock:
        redis_client.strings["lock:ctx:cv_1"] = "别人的 token"
    assert redis_client.strings["lock:ctx:cv_1"] == "别人的 token"


async def test_lock_release_failure_is_swallowed(
    redis_client: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """释放失败只记日志：锁会因 TTL 自动过期，不该让业务写入失败。"""

    async def boom(*_: object) -> Any:
        raise RuntimeError("redis 挂了")

    monkeypatch.setattr(redis_client, "eval", boom)
    lock = RedisLock(redis_client, "lock:ctx:cv_1")
    async with lock:
        pass  # 不应抛异常


async def test_lock_times_out_when_contended(
    redis_client: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """争抢不到锁时报「依赖不可用」而不是无限等待（上层据此降级）。"""
    redis_client.reject_nx = True

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    lock = RedisLock(redis_client, "lock:ctx:cv_1")
    with pytest.raises(RedisUnavailable) as excinfo:
        async with lock:
            pass  # pragma: no cover - 永远进不来
    assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert excinfo.value.details["key"] == "lock:ctx:cv_1"


async def test_lock_token_is_unique_per_acquisition(redis_client: FakeRedis) -> None:
    first = RedisLock(redis_client, "lock:ctx:cv_1")
    async with first:
        token_a = redis_client.strings["lock:ctx:cv_1"]
    second = RedisLock(redis_client, "lock:ctx:cv_1")
    async with second:
        token_b = redis_client.strings["lock:ctx:cv_1"]
    assert token_a != token_b


# ---------------------------------------------------------------------------
# 依赖探测
# ---------------------------------------------------------------------------
def test_create_redis_commands_reports_missing_dependency() -> None:
    """未装 ``redis`` 包时给出可执行的提示，而不是 ``ModuleNotFoundError`` 堆栈。"""
    settings = build_settings()
    if importlib.util.find_spec("redis") is None:
        with pytest.raises(RedisUnavailable) as excinfo:
            create_redis_commands(settings)
        assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
        assert "uv add redis" in str(excinfo.value.details["hint"])
    else:  # pragma: no cover - 本机未安装 redis 包
        assert create_redis_commands(settings) is not None


def test_redis_unavailable_is_apperror() -> None:
    error = RedisUnavailable("挂了", {"key": "k"})
    assert isinstance(error, AppError)
    assert error.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert error.details == {"key": "k"}


def test_lock_ttl_matches_docs() -> None:
    """``docs/07`` §2.1 约定锁 TTL 5s；被改大意味着崩溃后会话会被锁更久。"""
    assert LOCK_TTL_SECONDS == 5
