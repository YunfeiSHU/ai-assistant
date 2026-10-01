"""任务进度流（``GET /tasks/{id}/events`` 的核心，``docs/08`` §4.5）。

**为什么把它从路由里拆出来**：这段逻辑有三处容易写错、而且错了不一定报错的地方
（订阅与快照的先后、重复帧去重、超时不能取消上游订阅），写成纯异步生成器就能
逐条单测，而不必去驱动一个真实的 HTTP 流。

三处约定：

1. **先订阅、后读快照**。反过来的话，「读快照」与「开始订阅」之间发布的事件会
   永久丢失 —— 表现是进度卡在某个阶段不动，而任务其实早已成功。
2. **快照是权威，增量是尽力而为**。总线可能丢帧（慢消费者被丢最旧帧），
   所以首帧一定来自任务表；客户端断线重连也是同一套语义（重连即重读快照）。
3. **超时不能 ``wait_for(anext(...))``**。``wait_for`` 超时会**取消**那个
   ``__anext__``，取消信号被抛进异步生成器内部，生成器随即终结，之后永远拿不
   到后续事件 —— 与 ``app/core/sse.py::frame_stream`` 里踩过的是同一个坑。
   这里用「竞速但不取消生产者」的写法，只有在**准备结束流**时才取消。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from app.core.exceptions import ErrorCode
from app.tasks.events import TaskEvent, TaskEventBus
from app.tasks.models import Task, TaskStatus
from app.tasks.retry import error_retryable
from app.tasks.service import TaskService

logger = logging.getLogger("app.tasks.stream")

#: 流可以结束的状态：``done`` 帧 + 关闭连接（``docs/08`` §4.5）
CLOSING_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELED})

#: 单连接的兜底时长上限（秒）。任务本身有 ``TASK_TIMEOUT_SECONDS``，
#: 再加一分钟余量：超过这个时长还没结束的流一定是「客户端早就不看了」。
DEFAULT_MAX_SECONDS = 1860.0


def _snapshot_events(task: Task) -> list[TaskEvent]:
    """把任务快照转成首帧序列。

    * 终态（``SUCCEEDED`` / ``CANCELED``）→ 只发 ``done``：``docs/08`` §4.5 要求
      「订阅前已是终态 → 立即推送 ``done`` 并关闭」；
    * ``FAILED`` → 只发 ``error``：**不关闭连接**，因为它可能自动重试回 ``QUEUED``
      （``docs/08`` §2 的 ``FAILED --> QUEUED``），关掉会让客户端错过后续进度；
    * 其余状态 → 发 ``progress``，客户端据此画出进度条的第一格。
    """
    if task.status in CLOSING_STATUSES:
        return [TaskEvent.done(status=str(task.status), finished_at=task.finished_at)]
    if task.status is TaskStatus.FAILED:
        error = task.error
        return [
            TaskEvent.error(
                code=error.code if error else str(ErrorCode.INTERNAL_ERROR),
                message=error.message if error else "",
                retryable=error_retryable(task),
            )
        ]
    return [
        TaskEvent.progress(
            stage=task.stage,
            progress=task.progress,
            # 与 ``TaskService.report_progress`` 同一口径：0 当作「不适用」不下发，
            # 否则非入库类任务的进度帧会凭空多出 ``chunks_total: 0``。
            chunks_done=task.chunks_done or None,
            chunks_total=task.chunks_total or None,
        )
    ]


async def stream_task_events(
    *,
    tasks: TaskService,
    bus: TaskEventBus,
    task_id: str,
    user_id: str,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncGenerator[TaskEvent, None]:
    """产出任务事件流（``progress`` / ``error`` / ``done``）。"""

    # ---- 先订阅（早于读快照），否则这中间的事件会永久丢失 ----
    subscription = bus.subscribe(task_id)
    deadline = clock() + max(0.0, max_seconds)
    primed: asyncio.Future[TaskEvent] | None = None
    try:
        # **必须预热一次**：异步生成器的函数体是在第一次 ``__anext__`` 时才执行的，
        # 「建出迭代器对象」并不等于「订阅已生效」。不预热的话，下面 ``tasks.get``
        # 那一次真实往返期间发布的事件会直接掉在地上 —— 而那个窗口正是
        # 「先订阅、后读快照」这条规则想消灭的东西（Redis 实现里
        # ``SUBSCRIBE`` 本身也要一次往返，预热让它与快照读取重叠）。
        primed = asyncio.ensure_future(anext(subscription))
        await asyncio.sleep(0)
        task = await tasks.get(task_id, user_id)
        for snapshot in _snapshot_events(task):
            yield snapshot
        if task.status in CLOSING_STATUSES:
            return

        # 订阅与快照之间可能已经推过同一帧（进度是单调的），首帧之后做一次去重。
        #
        # 去重键**必须带上** ``chunks_done``：embedding 阶段的 ``progress`` 会在 95
        # 上饱和，之后每批只涨 ``chunks_done``。只比 ``(stage, progress)`` 会把饱和
        # 之后的增量帧全部当成重复丢掉 —— 客户端看到进度条停住不动，而任务其实在推进。
        # 两侧都归一成 ``None``（而不是 0）才能对称：事件的 ``chunks_done``
        # 在计数为 0 时是**不下发**的，拿 ``None`` 与 ``0`` 比永远不会相等。
        last: tuple[Any, Any, Any] | None = (task.stage, task.progress, task.chunks_done or None)
        while True:
            event = await _next_event(subscription, deadline, clock, pending=primed)
            # 预热的那次 ``__anext__`` 只能交出去一次（要么被消费，要么被取消）
            primed = None
            if event is None:
                # 兜底超时：流必须结束，否则一个卡住的连接会一直占着资源
                yield TaskEvent.error(
                    code=str(ErrorCode.UPSTREAM_TIMEOUT),
                    message="等待任务进度超时",
                    retryable=True,
                )
                return
            if event.event == "progress":
                key = (
                    event.data.get("stage"),
                    event.data.get("progress"),
                    event.data.get("chunks_done"),
                )
                if key == last:
                    continue
                last = key
            yield event
            if event.event == "done":
                return
    finally:
        # 预热出来但没被消费的那次 ``__anext__`` 必须先取消：此时生成器处于
        # 「运行中」，直接 ``aclose()`` 会抛 ``RuntimeError``。
        if primed is not None:
            primed.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await primed
        # 客户端断连 / 正常结束都会走到这里；不关会让 Redis 订阅与队列泄漏
        await subscription.aclose()


async def _next_event(
    subscription: AsyncGenerator[TaskEvent, None],
    deadline: float,
    clock: Callable[[], float],
    *,
    pending: asyncio.Future[TaskEvent] | None = None,
) -> TaskEvent | None:
    """取下一个事件；超过 ``deadline`` 返回 ``None``。

    **竞速但不取消生产者**：先算好剩余时间，用一个 ``sleep`` 与 ``anext`` 竞速。
    只有超时这条路径才会取消 ``anext`` —— 而那条路径上我们马上要结束整个流，
    所以「生成器被取消」是期望行为（见模块 docstring）。

    Args:
        pending: 已发出的那次 ``__anext__``（见 ``stream_task_events`` 的预热）。
            给它时**不做截止时间判定**：事件已经拿到手了，丢掉它才是真的丢数据。
    """
    if pending is None:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        pending = asyncio.ensure_future(anext(subscription))
    remaining = deadline - clock()
    timer: asyncio.Future[None] = asyncio.ensure_future(asyncio.sleep(max(0.0, remaining)))
    waiters: set[asyncio.Future[Any]] = {pending, timer}
    try:
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    if pending in done:
        try:
            return pending.result()
        except StopAsyncIteration:
            # 总线自己结束了（关闭连接等）：当作「没有更多事件」
            return None
    pending.cancel()
    # **必须等到它真的结束**：``cancel()`` 只是投递取消，此刻生成器仍处于
    # 「运行中」，紧接着的 ``aclose()`` 会抛 ``RuntimeError: aclose(): asynchronous
    # generator is already running`` —— 超时这条路径同样会走到 ``finally``。
    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
        await pending
    return None


__all__ = ["CLOSING_STATUSES", "DEFAULT_MAX_SECONDS", "stream_task_events"]
