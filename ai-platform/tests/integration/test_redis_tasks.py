"""Redis 任务存储 / 事件总线 / 延迟重试队列的真实连接集成测试（``docs/11`` §2）。

**为什么这几条必须真连**：它们验证的是「命令的语义」而不是「代码的分支」——
Lua 脚本的原子性、``SET NX`` 的幂等抢占、ZSET 的游标分页、pub/sub 的
「订阅之前发的帧不会重放」。内存替身里这些都是我们自己写的 Python 代码，
测得再细也只能证明「替身自洽」。

标记 ``integration``：``pytest -m "not integration"`` 可在没有容器的 CI 上跳过。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import ResponseError

from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.tasks.events import RedisTaskEventBus, TaskEvent
from app.tasks.models import ResourceType, Task, TaskError, TaskStatus, TaskType
from app.tasks.redis_store import OPEN_ZSET, RedisTaskStore
from app.tasks.retry import RedisRetryQueue
from app.tasks.store import TaskConflict, encode_task_cursor

pytestmark = pytest.mark.integration

USER = "u_redis"


def _task(
    *,
    user_id: str = USER,
    status: TaskStatus = TaskStatus.PENDING,
    type_: TaskType = TaskType.DOCUMENT_INGEST,
    resource_id: str = "doc_1",
    idem_key: str | None = None,
    payload: dict[str, Any] | None = None,
    error: TaskError | None = None,
) -> Task:
    return Task(
        id=new_id("task"),
        type=type_,
        status=status,
        user_id=user_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=resource_id,
        idem_key=idem_key or f"idem:{resource_id}:{new_id('doc')}",
        payload=payload or {"doc_name": "测试文档"},
        error=error,
    )


def _iso_offset(*, seconds: float) -> str:
    """「当前时间 ± seconds」的 ISO 字符串（集成层用真时间，不注入时钟）。"""
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _stop_pending(pending: asyncio.Future[Any]) -> None:
    """取消一次挂着的 ``__anext__`` 并等它收尾。

    **必须先取消再 ``aclose()``**：生成器此时「正在运行」（挂在 ``await`` 上），
    直接 ``aclose()`` 会抛 ``RuntimeError: aclose(): asynchronous generator is
    already running``。这也是 ``stream_task_events`` 里那段 ``primed.cancel()``
    的由来。
    """
    pending.cancel()
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await pending


# ---------------------------------------------------------------------------
# 任务存储
# ---------------------------------------------------------------------------
async def test_round_trip_keeps_every_field(redis_client: Any) -> None:
    """写入再读出必须**逐字段**一致（漏一个字段就是「重连后进度没了」）。"""
    store = RedisTaskStore(redis_client)
    task = _task(payload={"doc_name": "中文名.md", "kb_id": "kb_1"})

    created = await store.create(task)
    loaded = await store.get(created.id, USER)

    assert loaded == created
    assert loaded.payload == {"doc_name": "中文名.md", "kb_id": "kb_1"}
    # 新建任务版本是 0，第一次 ``update`` 才推到 1（CAS 的基准值来自记录本身）
    assert loaded.version == 0


async def test_get_is_tenant_scoped(redis_client: Any) -> None:
    """跨用户取任务 → ``TASK_NOT_FOUND``（不是 403：避免枚举）。"""
    store = RedisTaskStore(redis_client)
    created = await store.create(_task())

    with pytest.raises(AppError) as excinfo:
        await store.get(created.id, "u_someone_else")

    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


async def test_idem_key_is_reserved_atomically(redis_client: Any) -> None:
    """同一 ``idem_key`` 只能有一条任务：第二次 ``create`` 返回既有那条。"""
    store = RedisTaskStore(redis_client)
    first = _task(idem_key="idem:same")
    second = _task(idem_key="idem:same")

    created = await store.create(first)
    again = await store.create(second)

    assert again.id == created.id
    with pytest.raises(AppError):
        await store.get(second.id, USER)


async def test_two_concurrent_creates_with_same_idem_key_yield_one_task(
    redis_client: Any,
) -> None:
    """并发创建也不能双写：幂等键用 ``SET NX`` 抢占，不是「先查再写」。"""
    store = RedisTaskStore(redis_client)
    a, b = _task(idem_key="idem:race"), _task(idem_key="idem:race")

    results = await asyncio.gather(store.create(a), store.create(b))

    assert results[0].id == results[1].id
    assert await store.count_open() == 1


async def test_update_bumps_version_and_rejects_version_tampering(redis_client: Any) -> None:
    """``update`` 走 CAS：版本自增，且变更函数不许自己改版本号。"""
    store = RedisTaskStore(redis_client)
    created = await store.create(_task())

    updated = await store.update(created.id, lambda task: setattr(task, "progress", 30))

    assert updated.version == created.version + 1
    assert updated.progress == 30

    def tamper(task: Task) -> None:
        task.version += 5

    with pytest.raises(TaskConflict):
        await store.update(created.id, tamper)


async def test_cancel_mark_is_first_only_and_has_ttl(redis_client: Any) -> None:
    """取消标记：首次置位为真、有 TTL（否则任务表被清后标记永远不会过期）。"""
    store = RedisTaskStore(redis_client, cancel_ttl=60)
    created = await store.create(_task())

    assert await store.request_cancel(created.id) is True
    assert await store.request_cancel(created.id) is False
    assert await store.is_cancel_requested(created.id) is True
    assert 0 < await redis_client.ttl(f"cancel:{created.id}") <= 60


async def test_open_count_tracks_only_active_tasks(redis_client: Any) -> None:
    """``count_open`` 只数未结束的任务（终态要出索引，否则跑久了必然「过载」）。"""
    store = RedisTaskStore(redis_client)
    first = await store.create(_task(resource_id="doc_a"))
    await store.create(_task(resource_id="doc_b"))
    assert await store.count_open() == 2

    await store.update(
        first.id,
        lambda task: (
            setattr(task, "status", TaskStatus.SUCCEEDED),
            setattr(task, "progress", 100),
        ),
    )

    assert await store.count_open() == 1
    assert await redis_client.zcard(OPEN_ZSET) == 1


async def test_list_filters_and_cursor_pages(redis_client: Any) -> None:
    """列表：统一 ZSET 倒序 + 游标翻页不重不漏（顺序键必须是 ``(created_at, id)``）。"""
    store = RedisTaskStore(redis_client)
    for index in range(3):
        await store.create(_task(resource_id=f"doc_{index}"))
        await asyncio.sleep(0.01)  # 让 created_at 真的不同（毫秒精度）

    page, has_more = await store.list(user_id=USER, limit=2)
    assert has_more is True
    assert [task.resource_id for task in page] == ["doc_2", "doc_1"]

    cursor = encode_task_cursor(page[-1])
    rest, has_more = await store.list(user_id=USER, limit=2, cursor=cursor)
    assert [task.resource_id for task in rest] == ["doc_0"]
    assert has_more is False

    filtered, _ = await store.list(user_id=USER, resource_id="doc_1")
    assert [task.resource_id for task in filtered] == ["doc_1"]

    other, _ = await store.list(user_id="u_nobody")
    assert other == []


async def test_list_stale_pending_uses_cutoff(redis_client: Any) -> None:
    """滞留扫描按「创建时间早于 cutoff」筛选（补偿重投的输入）。"""
    store = RedisTaskStore(redis_client)
    created = await store.create(_task())

    assert await store.list_stale_pending(before=_iso_offset(seconds=-60)) == []
    still_pending = await store.list_stale_pending(before=_iso_offset(seconds=60))
    assert [task.id for task in still_pending] == [created.id]


async def test_task_record_has_ttl(redis_client: Any) -> None:
    """任务记录带 TTL（``TASK_TTL_SECONDS``）：过期即 404，不会无限堆积。"""
    store = RedisTaskStore(redis_client, task_ttl=120)
    created = await store.create(_task())

    ttl = await redis_client.ttl(f"task:{created.id}")
    assert 0 < ttl <= 120


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------
async def test_publish_reaches_subscriber(redis_client: Any) -> None:
    """跨连接 pub/sub：一个连接发布、另一个订阅（真实跨进程模型）。"""
    publisher = RedisTaskEventBus(redis_client, poll_timeout=0.05)
    subscriber = RedisTaskEventBus(redis_client, poll_timeout=0.05)
    task_id = "task_events_1"

    stream = subscriber.subscribe(task_id)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # 订阅是异步生成器：预热一次才会真正 SUBSCRIBE
    await publisher.publish(task_id, TaskEvent.progress(stage="embedding", progress=42))

    event = await asyncio.wait_for(pending, timeout=3)

    assert event.event == "progress"
    assert event.data == {"stage": "embedding", "progress": 42}
    assert publisher.channel(task_id) == f"task:events:{task_id}"
    await stream.aclose()


async def test_frames_published_before_subscribe_are_not_replayed(redis_client: Any) -> None:
    """pub/sub 不重放历史帧 —— 这是「重连先读快照」这条约定的存在理由。"""
    bus = RedisTaskEventBus(redis_client, poll_timeout=0.05)
    task_id = "task_events_2"
    await bus.publish(task_id, TaskEvent.progress(stage="chunking", progress=10))

    stream = bus.subscribe(task_id)
    pending = asyncio.ensure_future(anext(stream))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), timeout=0.3)
    finally:
        await _stop_pending(pending)
        await stream.aclose()


async def test_shutdown_releases_subscription(redis_client: Any) -> None:
    """取消挂着的订阅后频道上没有残留订阅者（实例重启不能留僵尸订阅）。"""
    bus = RedisTaskEventBus(redis_client, poll_timeout=0.05)
    stream = bus.subscribe("task_events_3")
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    assert "task:events:task_events_3" in await redis_client.pubsub_channels()

    await _stop_pending(pending)

    channels = await redis_client.pubsub_channels()
    assert "task:events:task_events_3" not in channels


async def test_corrupted_frame_does_not_break_stream(redis_client: Any) -> None:
    """脏帧只丢一帧：多实例灰度期间可能混着旧格式，不能掐断整条进度流。"""
    bus = RedisTaskEventBus(redis_client, poll_timeout=0.05)
    task_id = "task_events_4"
    stream = bus.subscribe(task_id)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)

    await redis_client.publish(bus.channel(task_id), "not-json")
    done = TaskEvent.done(status="SUCCEEDED", finished_at=None)
    await redis_client.publish(bus.channel(task_id), done.encode())

    event = await asyncio.wait_for(pending, timeout=3)

    assert event.event == "done"
    await stream.aclose()


# ---------------------------------------------------------------------------
# 延迟重试队列（Lua 原子认领）
# ---------------------------------------------------------------------------
async def test_claim_is_atomic_across_claimers(redis_client: Any) -> None:
    """两个 Worker 同时认领：同一个条目**不能**被两边同时拿到。

    「先 ``ZRANGEBYSCORE`` 再 ``ZREM``」的写法在这里必然翻车 —— 那正是这段 Lua
    存在的唯一理由，所以这条用例就是它的存在性证明。
    """
    queue = RedisRetryQueue(redis_client)
    for index in range(6):
        await queue.schedule(f"task_{index}", attempt=1, delay=0)

    first, second = await asyncio.gather(queue.claim_due(limit=4), queue.claim_due(limit=4))

    claimed = [entry.task_id for entry in first + second]
    assert sorted(claimed) == [f"task_{index}" for index in range(6)]
    assert len(set(claimed)) == 6
    assert await redis_client.zcard("retry:zset") == 0


async def test_claim_respects_delay_and_limit(redis_client: Any) -> None:
    """未到期的条目不会被认领；``limit`` 限制单次认领量。"""
    queue = RedisRetryQueue(redis_client)
    await queue.schedule("task_later", attempt=1, delay=3600)
    for index in range(5):
        await queue.schedule(f"task_due_{index}", attempt=2, delay=0)

    due = await queue.claim_due(limit=2)

    assert len(due) == 2
    assert all(entry.attempt == 2 for entry in due)
    assert await redis_client.zcard("retry:zset") == 4


async def test_schedule_dedupes_same_attempt(redis_client: Any) -> None:
    """同一任务的同一轮尝试重复入队 → ZSET 去重（member 相同，score 覆盖）。"""
    queue = RedisRetryQueue(redis_client)
    await queue.schedule("task_dup", attempt=1, delay=0)
    await queue.schedule("task_dup", attempt=1, delay=0)

    assert await redis_client.zcard("retry:zset") == 1
    due = await queue.claim_due()
    assert [entry.task_id for entry in due] == ["task_dup"]


async def test_good_members_survive_a_corrupted_member(redis_client: Any) -> None:
    """成员格式坏了不能把整批认领带走（直接塞一个非约定格式的成员）。"""
    queue = RedisRetryQueue(redis_client)
    await redis_client.zadd("retry:zset", {"bad-member": 0})
    await queue.schedule("task_ok", attempt=1, delay=0)

    due = await queue.claim_due()

    assert "task_ok" in [entry.task_id for entry in due]


async def test_lua_scripts_survive_concurrent_traffic(redis_client: Any) -> None:
    """混合负载下不报脚本错误（``NOSCRIPT``/参数错位这类问题只在真实连接上暴露）。"""
    store = RedisTaskStore(redis_client)
    queue = RedisRetryQueue(redis_client)

    async def churn(index: int) -> None:
        task = await store.create(_task(resource_id=f"doc_{index}"))
        await store.update(task.id, lambda current: setattr(current, "progress", 10))
        await store.request_cancel(task.id)
        await queue.schedule(task.id, attempt=1, delay=0)

    try:
        await asyncio.gather(*(churn(index) for index in range(8)))
    except ResponseError as exc:  # pragma: no cover - 脚本有问题时才会走到
        pytest.fail(f"Lua 脚本在并发下报错：{exc}")

    assert await store.count_open() == 8
    assert len(await queue.claim_due(limit=32)) == 8
