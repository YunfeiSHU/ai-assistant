"""任务事件总线与事件编码（``docs/08`` §4.5）。

事件编码看着琐碎，但它是**跨进程**的（Worker 发布、API 订阅），而且 pub/sub 的
负载必须是单行 —— 所以这里逐字段断言：``progress`` / ``done`` / ``error`` 的
``data`` 形状一旦漂移，前端进度条就会在某个阶段永久卡住。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from tests.conftest import build_settings

from app.core.exceptions import AppError, ErrorCode
from app.tasks.events import (
    CHANNEL_PREFIX,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_PROGRESS,
    InMemoryTaskEventBus,
    RedisTaskEventBus,
    TaskEvent,
    build_task_event_bus,
    make_publisher,
)


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------
def test_progress_event_shape() -> None:
    """``progress`` → ``{stage, progress}``。"""
    event = TaskEvent.progress(stage="chunking", progress=40)
    assert event.event == EVENT_PROGRESS
    assert event.data == {"stage": "chunking", "progress": 40}


def test_done_event_shape() -> None:
    """``done`` → ``{status, finished_at}``（客户端据此关闭进度条）。"""
    event = TaskEvent.done(status="succeeded", finished_at="2026-01-01T00:00:00+00:00")
    assert event.event == EVENT_DONE
    assert event.data["status"] == "succeeded"


def test_error_event_shape_matches_chat_sse() -> None:
    """``error`` → ``{code, message, retryable}``（与对话 SSE 同构）。"""
    event = TaskEvent.error(code=str(ErrorCode.MQ_UNAVAILABLE), message="投递失败", retryable=True)
    assert event.event == EVENT_ERROR
    assert event.data == {"code": "MQ_UNAVAILABLE", "message": "投递失败", "retryable": True}


def test_event_encodes_to_single_line() -> None:
    """负载必须是单行：SSE 用 ``data:`` 逐行解析，换行会把 JSON 截成两行。"""
    raw = TaskEvent.progress(stage="a\nb", progress=1).encode()
    assert "\n" not in raw
    assert json.loads(raw)["data"]["stage"] == "a\nb"


def test_event_roundtrip() -> None:
    """编解码往返（Redis pub/sub 上跑的就是这一对）。"""
    event = TaskEvent.progress(stage="embedding", progress=77)
    assert TaskEvent.decode(event.encode()) == event


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '{"data": {}}', '{"event": ""}'])
def test_decode_rejects_corrupted(raw: str) -> None:
    """脏数据必须抛错，让订阅端有机会「只丢这一帧」。"""
    with pytest.raises(ValueError):
        TaskEvent.decode(raw)


def test_decode_tolerates_missing_data() -> None:
    """只有 ``event`` 时也要能解出（灰度期间可能出现精简格式）。"""
    assert TaskEvent.decode('{"event": "done"}').data == {}


# ---------------------------------------------------------------------------
# 内存总线
# ---------------------------------------------------------------------------
async def test_publish_reaches_subscriber() -> None:
    """订阅者能收到发布的事件。"""
    bus = InMemoryTaskEventBus()
    received: list[TaskEvent] = []

    async def consume() -> None:
        async for event in bus.subscribe("task_1"):
            received.append(event)
            return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # 让订阅先注册上
    await bus.publish("task_1", TaskEvent.progress(stage="x", progress=1))
    await consumer
    assert len(received) == 1


async def test_publish_without_subscriber_is_noop() -> None:
    """没有订阅者时直接返回（不缓存、不重放：增量是尽力而为的）。"""
    bus = InMemoryTaskEventBus()
    await bus.publish("task_none", TaskEvent.progress(stage=None, progress=0))
    assert bus.subscriber_count == 0


async def test_events_are_scoped_per_task() -> None:
    """只推给订阅了**这个**任务的连接（否则用户会看到别人的进度）。"""
    bus = InMemoryTaskEventBus()
    received: list[TaskEvent] = []

    async def consume(task_id: str) -> None:
        async for event in bus.subscribe(task_id):
            received.append(event)
            return

    consumer = asyncio.create_task(consume("task_a"))
    await asyncio.sleep(0)
    await bus.publish("task_b", TaskEvent.progress(stage="other", progress=1))
    await bus.publish("task_a", TaskEvent.progress(stage="mine", progress=2))
    await consumer
    assert [event.data["stage"] for event in received] == ["mine"]


async def test_broken_consumer_does_not_block_publisher() -> None:
    """慢消费者满了就丢**最旧**的帧，发布端永不阻塞。

    一个卡住的浏览器连接不该拖住整条入库流水线 —— 这是刻意的取舍
    （进度是单调的，丢中间态不影响最终一致）。
    """
    bus = InMemoryTaskEventBus(queue_size=2)
    subscribed = asyncio.Event()

    async def consume_forever() -> None:
        async for _ in bus.subscribe("task_1"):
            subscribed.set()
            await asyncio.sleep(3600)  # 永不消费

    consumer = asyncio.create_task(consume_forever())
    await asyncio.sleep(0)
    await asyncio.wait_for(
        asyncio.gather(
            *(bus.publish("task_1", TaskEvent.progress(stage="x", progress=i)) for i in range(5))
        ),
        timeout=1.0,
    )
    consumer.cancel()


async def test_unsubscribe_on_aclose() -> None:
    """客户端断连（``aclose``）必须摘掉队列，否则每次断连漏一个订阅者。

    注意 ``aclose`` 只能在生成器**不处于运行中**时调用（挂在 ``queue.get()`` 上的
    生成器是「运行中」，此时 ``aclose`` 会抛 ``RuntimeError``）—— 真实断连路径是
    先取消消费者任务，这条路径由下一个用例覆盖。
    """
    bus = InMemoryTaskEventBus()
    subscription = bus.subscribe("task_1")
    consumer = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)  # 让生成器跑到 ``queue.get()``（此时订阅已登记）
    assert bus.subscriber_count == 1

    await bus.publish("task_1", TaskEvent.progress(stage="x", progress=1))
    await consumer  # 取到一帧后生成器停在 ``yield`` 处
    await subscription.aclose()
    assert bus.subscriber_count == 0


async def test_cancelling_consumer_unsubscribes() -> None:
    """消费者任务被取消（客户端断连 / 应用关停）时也要退订。"""
    bus = InMemoryTaskEventBus()
    subscription = bus.subscribe("task_1")
    consumer = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert bus.subscriber_count == 0


async def test_close_stops_publishing() -> None:
    """关停之后 ``publish`` 变空操作（订阅者由上层取消唤醒）。"""
    bus = InMemoryTaskEventBus()
    await bus.close()
    await bus.publish("task_1", TaskEvent.progress(stage=None, progress=0))
    assert bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# 发布器封装
# ---------------------------------------------------------------------------
async def test_make_publisher_swallows_errors() -> None:
    """推事件失败**绝不能**影响状态落库（总线是尽力而为的旁路）。"""

    class _Broken:
        async def publish(self, task_id: str, event: TaskEvent) -> None:
            raise RuntimeError("bus down")

        async def subscribe(self, task_id: str) -> Any:  # pragma: no cover - 未用到
            raise NotImplementedError

        async def close(self) -> None:  # pragma: no cover - 未用到
            return None

    publish = make_publisher(_Broken())  # type: ignore[arg-type]
    assert publish is not None
    await publish("task_1", TaskEvent.progress(stage=None, progress=1))


async def test_make_publisher_none_returns_none() -> None:
    """没传总线时返回 ``None``，``TaskService`` 据此跳过推送。"""
    assert make_publisher(None) is None


# ---------------------------------------------------------------------------
# 构造选择
# ---------------------------------------------------------------------------
def test_build_bus_uses_memory_when_not_real() -> None:
    """``INFRA_BACKEND=memory`` → 进程内总线（无外部依赖）。"""
    assert isinstance(build_task_event_bus(build_settings()), InMemoryTaskEventBus)


def test_build_bus_degrades_without_redis() -> None:
    """``real`` 但装不上/连不上 Redis → 退化为内存总线并告警，而不是启动失败。"""
    import importlib.util

    if importlib.util.find_spec("redis") is not None:  # pragma: no cover
        pytest.skip("本机装了 redis，无法测降级路径")
    bus = build_task_event_bus(build_settings(infra_backend="real", task_runner="kafka"))
    assert isinstance(bus, InMemoryTaskEventBus)


def test_channel_name_is_namespaced() -> None:
    """频道名带前缀，避免与其它业务键撞车。"""
    assert RedisTaskEventBus(None, channel_prefix=CHANNEL_PREFIX).channel("t1") == "task:events:t1"


def test_publish_failure_is_logged_not_raised() -> None:
    """Redis 实现里发布失败只记日志（任务不该因推事件失败而失败）。"""

    class _BrokenClient:
        async def publish(self, channel: str, payload: str) -> None:
            raise ConnectionError("redis down")

    async def scenario() -> None:
        await RedisTaskEventBus(_BrokenClient()).publish(  # type: ignore[arg-type]
            "task_1", TaskEvent.progress(stage=None, progress=1)
        )

    asyncio.run(scenario())


def test_redis_bus_raises_dependency_error_without_redis() -> None:
    """Redis 客户端建不出来时抛的是 ``RedisUnavailable``（依赖错误，不是 ``ConnectionError``）。

    上层靠这个异常做降级判断（退回进程内总线），所以错误类型是接口的一部分。
    """
    import importlib.util

    if importlib.util.find_spec("redis") is not None:  # pragma: no cover - 装了就走真连接
        pytest.skip("本机装了 redis，无法测缺依赖路径")
    from app.infrastructure.redis.client import RedisUnavailable

    with pytest.raises(AppError) as excinfo:
        RedisTaskEventBus.from_settings(build_settings(infra_backend="real"))
    assert isinstance(excinfo.value, RedisUnavailable)
    assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
