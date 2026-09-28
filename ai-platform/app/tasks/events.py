"""任务进度事件总线（``docs/08`` §4.5 的 SSE 事件来源）。

**为什么需要一层总线，而不是「SSE 路由直接轮询任务表」**：
``docs/08`` §5.4 要求 Worker 是**独立进程**。进程内的事件队列在跨进程时形同虚设
（API 进程永远收不到 Worker 发的事件），于是 SSE 会退化成「连上了但一直不推
进度」——比不提供更糟。所以这里定义一个端口，两种实现：

* :class:`InMemoryTaskEventBus` —— 进程内广播（``inline`` 执行器 / 测试）；
* :class:`RedisTaskEventBus` —— Redis pub/sub（``INFRA_BACKEND=real``，跨进程）。

**丢事件比阻塞重要**：慢消费者（网络卡住的浏览器）MUST NOT 阻塞 Worker 上报
进度，否则一个卡住的 SSE 连接会把整条入库流水线拖住。所以订阅端队列有上限，
满了丢**最旧**的进度帧 —— 进度是单调的，丢中间态不影响最终一致（客户端只要
看到最新值就能画出进度条）。

**事件不可信也就不重放**：总线只做「尽力而为」的增量推送；断线重连的正确姿势是
先 ``GET /tasks/{id}`` 拿权威快照，再订阅增量。SSE 路由正是这么写的（见
``app/api/routes/tasks.py``）：**先订阅、后读快照**，两者之间无缝隙。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.config import Settings
from app.core.redis import RedisUnavailable, create_redis_client, redis_text

logger = logging.getLogger("app.tasks.events")

#: 事件类型（``docs/08`` §4.5）
EVENT_PROGRESS = "progress"
EVENT_DONE = "done"
EVENT_ERROR = "error"

#: Redis 频道前缀：``task:events:{task_id}``（``docs/09`` §4 的 Key 规范同族）
CHANNEL_PREFIX = "task:events:"

#: 订阅端队列上限；满时丢最旧的进度帧（见模块 docstring）
SUBSCRIBER_QUEUE_SIZE = 64


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """一条任务事件。

    字段名刻意是 ``event`` / ``data``：``app/core/sse.py::frame_stream`` 按鸭子类型
    取值，于是任务 SSE 与对话 SSE 能复用**同一段**帧构造与心跳逻辑，
    不必为任务再写一份（写两份必然出现「一个有心跳、一个没有」这类不一致）。
    """

    event: str
    data: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def encode(self) -> str:
        """序列化为**单行** JSON（pub/sub 与 SSE 都要求无换行）。"""
        return json.dumps(
            {"event": self.event, "data": self.data},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def decode(cls, raw: str) -> TaskEvent:
        """反序列化；脏数据抛 ``ValueError``（由调用方决定是丢弃还是上抛）。"""
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            msg = f"任务事件必须是 JSON 对象：{raw[:80]!r}"
            raise ValueError(msg)
        event = payload.get("event")
        if not isinstance(event, str) or not event:
            msg = f"任务事件缺少 event 字段：{raw[:80]!r}"
            raise ValueError(msg)
        data = payload.get("data")
        return cls(event=event, data=data if isinstance(data, dict) else {})

    # ------------------------------------------------------------------
    @classmethod
    def progress(cls, *, stage: str | None, progress: int) -> TaskEvent:
        """``progress``：阶段与进度（``{stage, progress}``）。"""
        return cls(EVENT_PROGRESS, {"stage": stage, "progress": int(progress)})

    @classmethod
    def done(cls, *, status: str, finished_at: str | None) -> TaskEvent:
        """``done``：任务进入终态（``{status, finished_at}``）。"""
        return cls(EVENT_DONE, {"status": status, "finished_at": finished_at})

    @classmethod
    def error(cls, *, code: str, message: str = "", retryable: bool = False) -> TaskEvent:
        """``error``：任务失败（``{code, message, retryable}``，与对话 SSE 同构）。"""
        return cls(EVENT_ERROR, {"code": code, "message": message, "retryable": retryable})


@runtime_checkable
class TaskEventBus(Protocol):
    """任务事件总线端口。"""

    async def publish(self, task_id: str, event: TaskEvent) -> None:
        """发布事件（**尽力而为**：失败只记日志，不影响任务本身）。"""
        ...

    def subscribe(self, task_id: str) -> AsyncGenerator[TaskEvent, None]:
        """订阅某任务的事件流。

        刻意声明为**同步函数返回异步迭代器**（而不是 ``async def`` 直接返回
        ``AsyncIterator``）：这样它就是一个原生异步生成器，
        ``frame_stream`` 里的 ``aclose()`` 能真正触达 ``finally`` 做退订，
        而不是留下一个永远订阅着的连接。
        """
        ...

    async def close(self) -> None:
        """释放连接（应用关停路径）。"""
        ...


class InMemoryTaskEventBus:
    """进程内事件广播（``INFRA_BACKEND=memory`` / ``inline`` 执行器 / 测试）。"""

    def __init__(self, *, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = max(1, queue_size)
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent]]] = {}
        #: 应用关停标记：置位后 ``publish`` 变空操作（订阅者由上层取消唤醒）
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        """当前订阅者总数（测试用）。"""
        return sum(len(queues) for queues in self._subscribers.values())

    async def publish(self, task_id: str, event: TaskEvent) -> None:
        """广播事件；没有订阅者时直接返回（不缓存、不重放）。"""
        if self._closed:
            return
        for queue in list(self._subscribers.get(task_id, ())):
            if queue.full():
                # 丢最旧的进度帧：进度单调，客户端看到最新值即可（见模块 docstring）
                with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - 仅并发窗口
                    queue.get_nowait()
            await queue.put(event)

    async def subscribe(self, task_id: str) -> AsyncGenerator[TaskEvent, None]:
        """见 :class:`TaskEventBus`。"""
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.setdefault(task_id, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            # 退订必须放在 ``finally``：客户端断连时 ``frame_stream`` 会 ``aclose()``
            # 这个生成器，只有这里能保证队列被摘掉，否则每次断连都漏一个订阅者。
            queues = self._subscribers.get(task_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._subscribers.pop(task_id, None)

    async def close(self) -> None:
        """标记关闭；之后 ``publish`` 变成空操作（唤醒订阅者由上层取消完成）。"""
        self._closed = True
        self._subscribers.clear()


class RedisTaskEventBus:
    """Redis pub/sub 事件总线（``INFRA_BACKEND=real``，跨进程）。

    **pub/sub 而不是 Stream/List**：进度事件是「过期即无用」的增量，
    没有重放需求（重连请先读任务快照）。用 Stream 反而要处理消费组与
    ``XACK``/``XTRIM`` 一整串生命周期，收益为零。
    """

    def __init__(
        self,
        client: Any,
        *,
        channel_prefix: str = CHANNEL_PREFIX,
        poll_timeout: float = 1.0,
    ) -> None:
        self._client = client
        self._channel_prefix = channel_prefix
        self._poll_timeout = poll_timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisTaskEventBus:
        """按 ``REDIS_URL`` 构造。"""
        return cls(
            create_redis_client(
                settings, hint="uv add redis 或将 INFRA_BACKEND 设为 memory（任务事件总线）"
            )
        )

    def channel(self, task_id: str) -> str:
        """任务对应的频道名。"""
        return f"{self._channel_prefix}{task_id}"

    async def publish(self, task_id: str, event: TaskEvent) -> None:
        """PUBLISH 一帧；连接故障只记日志（任务本身不该因推事件失败而失败）。"""
        try:
            await self._client.publish(self.channel(task_id), event.encode())
        except Exception as exc:
            logger.warning(
                "task.event_publish_failed",
                extra={"task_id": task_id, "event": event.event, "error": str(exc)},
            )

    async def subscribe(self, task_id: str) -> AsyncGenerator[TaskEvent, None]:
        """见 :class:`TaskEventBus`。"""
        channel = self.channel(task_id)
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=self._poll_timeout
                )
                if message is None:
                    # 超时是正常的：保持长连接，让 SSE 层去发心跳帧
                    continue
                raw = redis_text(message.get("data"))
                if not raw:
                    continue
                try:
                    yield TaskEvent.decode(raw)
                except (ValueError, json.JSONDecodeError):
                    # 单帧脏数据不该掐断整个进度流（多实例灰度期间可能出现旧格式）
                    logger.warning("task.event_corrupted", extra={"task_id": task_id})
        finally:
            await self._safe_close(pubsub, channel)

    async def close(self) -> None:
        """关闭底层连接（``aclose`` 是 redis-py 的新名，旧版只有 ``close``）。"""
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is None:  # pragma: no cover - 替身没有关闭语义
            return
        result = closer()
        if isinstance(result, Awaitable):
            await result

    async def _safe_close(self, pubsub: Any, channel: str) -> None:
        """退订并关闭 pubsub；任何异常都吞掉。

        这里处在**客户端断连**的清理路径上：此时抛出任何异常都会盖掉原始原因，
        而且 ``async generator ignored GeneratorExit`` 这类报错极难排查。
        """
        for closer in (
            getattr(pubsub, "unsubscribe", None),
            getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None),
        ):
            if closer is None:
                continue
            try:
                result = closer(channel) if closer.__name__ == "unsubscribe" else closer()
                if isinstance(result, Awaitable):
                    await result
            except Exception as exc:  # pragma: no cover - 断连路径
                logger.debug(
                    "task.event_unsubscribe_failed", extra={"channel": channel, "error": str(exc)}
                )


def build_task_event_bus(settings: Settings) -> TaskEventBus:
    """按 ``INFRA_BACKEND`` 选择事件总线。

    ``real`` 下若 Redis 依赖缺失/连不上，**不让应用起不来**：退化成进程内总线并
    告警（与 ``app/memory`` 对 Redis 的口径一致）。代价是跨进程推事件失效，
    表现是 SSE 只推首帧 —— 这比「整个服务不可用」好得多，而且日志里能看见原因。
    """
    if not settings.uses_shared_task_store:
        return InMemoryTaskEventBus()
    try:
        return RedisTaskEventBus.from_settings(settings)
    except RedisUnavailable as exc:
        logger.warning("task.event_bus_degraded", extra={"error": str(exc)})
        return InMemoryTaskEventBus()


#: 事件发布的可注入入口（``TaskService`` 用它，避免直接依赖具体总线）
Publisher = Callable[[str, TaskEvent], Awaitable[None]]


def make_publisher(bus: TaskEventBus | None) -> Publisher | None:
    """把总线包成「永不抛异常」的发布函数。

    ``TaskService`` 的状态变更路径上不允许出现「推事件失败导致状态没落库」，
    所以这里统一吞异常并记日志（`docs/10` §3.1：**写路径宁可失败也不要静默丢弃**，
    但「推事件」不是写路径的一部分 —— 权威状态在任务表里）。
    """
    if bus is None:
        return None

    async def publish(task_id: str, event: TaskEvent) -> None:
        try:
            await bus.publish(task_id, event)
        except Exception as exc:  # pragma: no cover - 总线实现已各自兜底
            logger.warning(
                "task.event_publish_failed",
                extra={"task_id": task_id, "event": event.event, "error": str(exc)},
            )

    return publish


__all__ = [
    "CHANNEL_PREFIX",
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_PROGRESS",
    "SUBSCRIBER_QUEUE_SIZE",
    "InMemoryTaskEventBus",
    "Publisher",
    "RedisTaskEventBus",
    "TaskEvent",
    "TaskEventBus",
    "build_task_event_bus",
    "make_publisher",
]
