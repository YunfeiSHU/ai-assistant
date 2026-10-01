"""任务进度流（``docs/08`` §4.5 的 SSE 内核）。

这是整个 M7 里最容易「看起来对但其实错」的一段，所以断言得很细：

* 订阅与快照的**先后**（反了会永久丢事件，且只在真跑起来才看得出来）；
* 快照是权威（终态直接给 ``done``、``FAILED`` 给 ``error`` 但**不关流**）；
* 兜底超时必须**结束整个流**（否则一个卡住的连接会长期占着资源）；
* 首帧去重（快照与增量之间必然重叠一帧）。

时钟与截止时间都是注入的，所以「等超时」在测试里是瞬间的事，没有 ``sleep``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.tasks.events import InMemoryTaskEventBus, TaskEvent
from app.tasks.models import ResourceType, Task, TaskError, TaskStatus, TaskType
from app.tasks.stream import CLOSING_STATUSES, stream_task_events


class _Clock:
    """可推进的假单调时钟。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeTaskService:
    """只实现 ``get`` 的任务服务替身（返回预设任务或抛 404）。"""

    def __init__(self, task: Task | None) -> None:
        self.task = task
        self.unauthorized = False

    async def get(self, task_id: str, user_id: str | None = None) -> Task:
        if self.task is None or self.unauthorized:
            raise AppError(ErrorCode.TASK_NOT_FOUND, "任务不存在")
        return self.task


def _task(*, status: TaskStatus = TaskStatus.RUNNING, **overrides: Any) -> Task:
    values: dict[str, Any] = {
        "id": "task_1",
        "type": TaskType.DOCUMENT_INGEST,
        "status": status,
        "user_id": "u_1",
        "resource_type": ResourceType.DOCUMENT,
        "resource_id": "doc_1",
        "progress": 30,
        "stage": "embedding",
        "finished_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    return Task(**values)


async def _collect(
    generator: AsyncGenerator[TaskEvent, None],
) -> list[TaskEvent]:
    """把生成器拉干，返回所有事件。"""
    return [event async for event in generator]


# ---------------------------------------------------------------------------
# 快照首帧
# ---------------------------------------------------------------------------
async def test_running_task_yields_progress_snapshot() -> None:
    """在跑的任务 → 首帧是当前进度（客户端据此画出第一格）。"""
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task()),  # type: ignore[arg-type]
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert events[0].event == "progress"
    assert events[0].data == {"stage": "embedding", "progress": 30}


@pytest.mark.parametrize("status", sorted(CLOSING_STATUSES, key=str))
async def test_terminal_task_yields_done_and_closes(status: TaskStatus) -> None:
    """已是终态 → 只推 ``done`` 并立即关闭（``docs/08`` §4.5）。"""
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task(status=status)),  # type: ignore[arg-type]
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
        )
    )
    assert [event.event for event in events] == ["done"]
    assert events[0].data["status"] == str(status)


async def test_failed_task_yields_error_without_closing() -> None:
    """``FAILED`` 只推 ``error`` **且不关闭**：它可能自动重试回 ``QUEUED``。

    关掉连接会让客户端错过后续的进度，表现是「失败之后进度条永远停在原地」。
    证据就是：流最终是被**兜底超时**结束的，而不是在 ``error`` 之后立即关闭。
    """
    task = _task(
        status=TaskStatus.FAILED,
        error=TaskError(code=str(ErrorCode.MQ_UNAVAILABLE), message="投递失败"),
    )
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(task),  # type: ignore[arg-type]
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert events[0].event == "error"
    assert events[0].data["code"] == "MQ_UNAVAILABLE"
    assert events[0].data["retryable"] is True
    assert events[-1].event == "error"
    assert events[-1].data["code"] == str(ErrorCode.UPSTREAM_TIMEOUT)


async def test_failed_task_reports_deterministic_error_as_not_retryable() -> None:
    """确定性错误 → 快照里的 ``error`` 帧也必须报 ``retryable=false``。

    与增量帧共用同一个判据（``error_retryable``），否则「重连后重读快照」看到的
    说法会和「当时收到的那一帧」不一样 —— 客户端据此决定要不要自动重试，
    两次不一的后果就是「重连一次反而多跑三轮注定失败的任务」。
    """
    task = _task(
        status=TaskStatus.FAILED,
        error=TaskError(code=str(ErrorCode.UNPROCESSABLE_DOCUMENT), message="扫描版 PDF"),
    )
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(task),  # type: ignore[arg-type]
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert events[0].data["retryable"] is False


async def test_failed_task_without_error_record_uses_internal_code() -> None:
    """``FAILED`` 但没有 ``error`` 记录（数据异常）时不能推出空 code。"""
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task(status=TaskStatus.FAILED)),
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert events[0].data["code"] == str(ErrorCode.INTERNAL_ERROR)


async def test_pending_task_is_not_treated_as_terminal() -> None:
    """``PENDING`` 是活跃状态：要发 progress 并继续等。"""
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task(status=TaskStatus.PENDING)),
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert [event.event for event in events] == ["progress", "error"]


# ---------------------------------------------------------------------------
# 增量
# ---------------------------------------------------------------------------
async def test_subscription_is_live_before_snapshot_is_read() -> None:
    """读快照期间上游推的事件**不能丢**（这是「先订阅、后读快照」的真正含义）。

    关键在于：异步生成器的函数体在第一次 ``__anext__`` 时才执行，所以
    「建出迭代器」并不等于「订阅已生效」。这里让 ``get`` 卡在闸门上，
    验证闸门打开前事件已经能送达。
    """
    bus = InMemoryTaskEventBus()
    clock = _Clock()
    gate = asyncio.Event()

    class _SlowService:
        async def get(self, task_id: str, user_id: str | None = None) -> Task:
            await gate.wait()
            return _task(progress=30, stage="embedding")

    stream = stream_task_events(
        tasks=_SlowService(),  # type: ignore[arg-type]
        bus=bus,
        task_id="task_1",
        user_id="u_1",
        max_seconds=5.0,
        clock=clock,
    )
    snapshot = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # 走到 ``tasks.get`` 上（订阅已生效）
    assert bus.subscriber_count == 1

    await bus.publish("task_1", TaskEvent.progress(stage="indexing", progress=50))
    gate.set()
    assert (await snapshot).data["progress"] == 30  # 快照仍是权威首帧
    assert (await anext(stream)).data["progress"] == 50

    # 收尾：推进时钟直接触发兜底超时（避免真的等 5 秒）
    clock.advance(100.0)
    tail = [event async for event in stream]
    assert tail[-1].data["code"] == str(ErrorCode.UPSTREAM_TIMEOUT)


async def test_duplicate_first_progress_frame_is_dropped() -> None:
    """快照与增量重叠的那一帧只出现一次（客户端不该看到重复进度）。"""
    bus = InMemoryTaskEventBus()
    clock = _Clock()
    stream = stream_task_events(
        tasks=_FakeTaskService(_task(progress=30, stage="embedding")),  # type: ignore[arg-type]
        bus=bus,
        task_id="task_1",
        user_id="u_1",
        max_seconds=5.0,
        clock=clock,
    )
    first = await anext(stream)  # 快照首帧（此时订阅已建立）
    assert first.data["progress"] == 30
    # 同一个位置再推一次（重叠帧）+ 一次真正的新进度 + 结束
    await bus.publish("task_1", TaskEvent.progress(stage="embedding", progress=30))
    await bus.publish("task_1", TaskEvent.progress(stage="indexing", progress=90))
    await bus.publish("task_1", TaskEvent.done(status="succeeded", finished_at=None))
    rest = await _collect(stream)
    assert [(event.event, event.data.get("progress")) for event in rest] == [
        ("progress", 90),
        ("done", None),
    ]


async def test_progress_frames_after_saturation_are_not_deduped() -> None:
    """``progress`` 饱和后，仅靠 ``chunks_done`` 前进的帧**不能**被当成重复丢掉。

    embedding 阶段的 ``progress`` 封顶在 95，之后每批只涨 ``chunks_done``。
    去重键只比 ``(stage, progress)`` 的话，客户端会看到进度条停住 ——
    而任务其实在推进，「停住」与「卡死」在响应上就再也分不出来（``docs/10`` UP-02）。
    """
    bus = InMemoryTaskEventBus()
    clock = _Clock()
    stream = stream_task_events(
        tasks=_FakeTaskService(  # type: ignore[arg-type]
            _task(progress=95, stage="EMBEDDING", chunks_total=100, chunks_done=10)
        ),
        bus=bus,
        task_id="task_1",
        user_id="u_1",
        max_seconds=5.0,
        clock=clock,
    )
    first = await anext(stream)
    assert first.data["chunks_done"] == 10, "快照首帧也要带上计数，否则客户端起点算不出 ETA"

    # 两帧的 (stage, progress) 完全相同，只有 chunks_done 在走 —— 都必须发出去
    for done in (20, 30):
        await bus.publish(
            "task_1",
            TaskEvent.progress(stage="EMBEDDING", progress=95, chunks_done=done, chunks_total=100),
        )
    await bus.publish("task_1", TaskEvent.done(status="succeeded", finished_at=None))
    rest = await _collect(stream)
    assert [(event.event, event.data.get("chunks_done")) for event in rest] == [
        ("progress", 20),
        ("progress", 30),
        ("done", None),
    ]


async def test_done_frame_ends_the_stream() -> None:
    """收到 ``done`` 立即返回（不等兜底超时）。"""
    task = _task()
    bus = InMemoryTaskEventBus()

    async def scenario() -> list[TaskEvent]:
        stream = stream_task_events(
            tasks=_FakeTaskService(task),  # type: ignore[arg-type]
            bus=bus,
            task_id="task_1",
            user_id="u_1",
            max_seconds=60.0,
            clock=_Clock(),
        )
        await anext(stream)
        await bus.publish("task_1", TaskEvent.done(status="succeeded", finished_at=None))
        return await _collect(stream)

    events = await scenario()
    assert [event.event for event in events] == ["done"]


async def test_error_frame_does_not_end_the_stream() -> None:
    """``error`` 之后继续等（自动重试会让它回到 ``QUEUED``）。"""
    bus = InMemoryTaskEventBus()
    clock = _Clock()

    async def scenario() -> list[TaskEvent]:
        stream = stream_task_events(
            tasks=_FakeTaskService(_task()),  # type: ignore[arg-type]
            bus=bus,
            task_id="task_1",
            user_id="u_1",
            max_seconds=5.0,
            clock=clock,
        )
        await anext(stream)
        await bus.publish("task_1", TaskEvent.error(code="MQ_UNAVAILABLE", retryable=True))
        clock.advance(100.0)  # 直接把截止时间推过去，等价于「等超时」
        return [event async for event in stream]

    events = await scenario()
    assert [event.event for event in events] == ["error", "error"]
    assert events[-1].data["code"] == str(ErrorCode.UPSTREAM_TIMEOUT)


# ---------------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------------
async def test_deadline_ends_stream_when_nothing_happens() -> None:
    """一直没事件 → 兜底超时结束（不能挂着一个永不结束的连接）。"""
    events = await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task()),  # type: ignore[arg-type]
            bus=InMemoryTaskEventBus(),
            task_id="task_1",
            user_id="u_1",
            max_seconds=0.0,
        )
    )
    assert events[-1].event == "error"
    assert events[-1].data["retryable"] is True


async def test_missing_task_propagates_before_streaming() -> None:
    """任务不存在/越权 → 抛 ``404``，由路由转成正常的 HTTP 错误。"""
    with pytest.raises(AppError) as excinfo:
        await _collect(
            stream_task_events(
                tasks=_FakeTaskService(None),  # type: ignore[arg-type]
                bus=InMemoryTaskEventBus(),
                task_id="task_1",
                user_id="u_1",
            )
        )
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


async def test_cross_user_access_is_rejected() -> None:
    """越权读取必须报 ``404``（由服务层负责判定归属）。"""
    service = _FakeTaskService(_task())
    service.unauthorized = True
    with pytest.raises(AppError) as excinfo:
        await _collect(
            stream_task_events(
                tasks=service,  # type: ignore[arg-type]
                bus=InMemoryTaskEventBus(),
                task_id="task_1",
                user_id="u_other",
            )
        )
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


async def test_subscription_is_closed_on_early_exit() -> None:
    """终态早退也要退订（否则每连一次就漏一个订阅者）。"""
    bus = InMemoryTaskEventBus()
    await _collect(
        stream_task_events(
            tasks=_FakeTaskService(_task(status=TaskStatus.SUCCEEDED)),  # type: ignore[arg-type]
            bus=bus,
            task_id="task_1",
            user_id="u_1",
        )
    )
    assert bus.subscriber_count == 0
