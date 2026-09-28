"""``TaskService`` 的事件发布、过载保护与错误可重试判定的单测（M7）。

**为什么这几个点要单独测**：它们是 HTTP 契约里「看不见但会错」的部分 ——
``GET /tasks/{id}/events`` 的每一帧都来自 :meth:`TaskService._publish`，
过载保护只在任务多起来之后才触发，而 ``retryable`` 判错会让客户端做三次
注定失败的重试。三者都不好在契约层构造（见 ``tests/contract/test_tasks.py``
里关于 ``TestClient`` 会跑完整个流的说明）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from app.core.errors import AppError, ErrorCode
from app.tasks.events import InMemoryTaskEventBus, TaskEvent, make_publisher
from app.tasks.models import ResourceType, TaskError, TaskStatus, TaskType
from app.tasks.retry import NON_RETRYABLE_CODES, error_retryable
from app.tasks.service import TaskService
from app.tasks.store import InMemoryTaskStore, build_task_store


class _Recorder:
    """记录所有已发布事件（``Publisher`` 的替身）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, TaskEvent]] = []

    async def __call__(self, task_id: str, event: TaskEvent) -> None:
        self.events.append((task_id, event))

    def named(self, name: str) -> list[TaskEvent]:
        return [event for _, event in self.events if event.event == name]


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def make_service(recorder: _Recorder) -> Callable[..., TaskService]:
    """构造一个发布事件被记录的 ``TaskService``。"""

    def _build(**kwargs: Any) -> TaskService:
        return TaskService(InMemoryTaskStore(), events=recorder, **kwargs)

    return _build


async def _running(service: TaskService, **payload: Any) -> str:
    """建一个跑起来的任务并返回 id（PENDING → QUEUED → RUNNING）。"""
    task, _ = await service.create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
        payload=payload,
    )
    await service.mark_queued(task.id)
    await service.mark_running(task.id)
    return task.id


# ---------------------------------------------------------------------------
# 事件发布
# ---------------------------------------------------------------------------
async def test_create_and_transitions_publish_nothing(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """建任务与 ``PENDING/QUEUED/RUNNING`` 迁移不发事件：客户端靠快照拿这些。"""
    service = make_service()
    await _running(service)
    assert recorder.events == []


async def test_progress_publishes_frame(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """上报进度 → 推 ``progress`` 帧（客户端进度条就靠它走）。"""
    service = make_service()
    task_id = await _running(service)

    await service.report_progress(task_id, stage="chunking", progress=40)

    frames = recorder.named("progress")
    assert len(frames) == 1
    assert frames[0].data == {"stage": "chunking", "progress": 40}
    assert recorder.events[0][0] == task_id


async def test_progress_is_clamped_and_must_not_regress(
    make_service: Callable[..., TaskService],
) -> None:
    """进度夹到 0..100，且**回退直接报错**（掩盖阶段乱序执行是最难查的 bug）。"""
    service = make_service()
    task_id = await _running(service)

    assert (await service.report_progress(task_id, stage="s", progress=999)).progress == 100
    with pytest.raises(AppError) as excinfo:
        await service.report_progress(task_id, stage="s", progress=10)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


async def test_succeed_publishes_done(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """成功 → 推 ``done``（带终态与结束时间）。"""
    service = make_service()
    task_id = await _running(service)

    await service.succeed(task_id)

    frames = recorder.named("done")
    assert len(frames) == 1
    assert frames[0].data["status"] == "SUCCEEDED"
    assert frames[0].data["finished_at"]


async def test_fail_publishes_error_only(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """失败 → 只推 ``error``（``docs/02`` §6.3：``error`` 与 ``done`` 互斥）。"""
    service = make_service()
    task_id = await _running(service)

    await service.fail(task_id, TaskError(code=str(ErrorCode.MQ_UNAVAILABLE), message="投递失败"))

    assert recorder.named("done") == []
    frames = recorder.named("error")
    assert len(frames) == 1
    assert frames[0].data == {
        "code": "MQ_UNAVAILABLE",
        "message": "投递失败",
        "retryable": True,
    }


async def test_fail_marks_deterministic_error_not_retryable(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """确定性错误（``UNPROCESSABLE_DOCUMENT``）必须报 ``retryable=false``。

    这条是**照 ``docs/02`` §5.3 抄的**：扫描版 PDF 重试三次还是同一份扫描版 PDF。
    只看 ``task.can_retry``（重试预算）会把它报成可重试，客户端于是白等三轮。
    """
    service = make_service()
    task_id = await _running(service)

    await service.fail(
        task_id, TaskError(code=str(ErrorCode.UNPROCESSABLE_DOCUMENT), message="无有效文本")
    )

    assert recorder.named("error")[0].data["retryable"] is False


async def test_cancel_queued_publishes_done(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """取消排队中的任务 → 立刻推 ``done(CANCELED)`` 并关流。"""
    service = make_service()
    task, _ = await service.create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
    )

    await service.cancel(task.id, "u_1")

    frames = recorder.named("done")
    assert len(frames) == 1
    assert frames[0].data["status"] == "CANCELED"


async def test_cancel_running_publishes_nothing(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """在跑的任务只是「记下取消请求」：真正转 ``CANCELED`` 由 Worker 在检查点做。"""
    service = make_service()
    task_id = await _running(service)

    task = await service.cancel(task_id, "u_1")

    assert task.status is TaskStatus.RUNNING
    assert recorder.events == []
    assert await service.is_cancel_requested(task_id) is True


async def test_retry_resets_and_publishes_zero_progress(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """重试 → 进度归零也要发出来，否则前端进度条停在上一轮的位置。"""
    service = make_service()
    task_id = await _running(service)
    await service.report_progress(task_id, stage="embedding", progress=70)
    await service.fail(task_id, TaskError(code=str(ErrorCode.MQ_UNAVAILABLE), message="x"))
    recorder.events.clear()

    task, requeued = await service.retry(task_id, "u_1")

    assert requeued is True
    assert (task.status, task.progress, task.stage, task.error) == (
        TaskStatus.QUEUED,
        0,
        None,
        None,
    )
    assert recorder.named("progress")[0].data == {"stage": None, "progress": 0}


async def test_requeue_publishes_zero_progress(
    make_service: Callable[..., TaskService], recorder: _Recorder
) -> None:
    """自动重试（Worker 调 ``requeue``）与用户重试在事件上要一致。"""
    service = make_service()
    task_id = await _running(service)
    await service.fail(task_id, TaskError(code=str(ErrorCode.UPSTREAM_TIMEOUT), message="超时"))
    recorder.events.clear()

    task = await service.requeue(task_id)

    assert task.status is TaskStatus.QUEUED
    # 记账在服务层：``requeue`` 每被调用一次就是一轮尝试，「要不要再来一轮」由
    # Worker 按预算判（这里不判、也不投递，退避重投由它自己安排）。
    assert task.retry_count == 1
    assert recorder.named("progress")[0].data == {"stage": None, "progress": 0}


async def test_publish_failure_does_not_break_business_flow() -> None:
    """总线挂了不能把业务动作一起带走（进度是尽力而为，状态才是权威）。

    这条覆盖的是生产接线：``make_publisher`` 是唯一在错误发生时兜住的地方，
    而它的输入（``bus.publish``）此时是抛异常的。
    """

    class _BrokenBus:
        async def publish(self, task_id: str, event: TaskEvent) -> None:
            raise RuntimeError("bus down")

    service = TaskService(InMemoryTaskStore(), events=make_publisher(_BrokenBus()))  # type: ignore[arg-type]
    task_id = await _running(service)

    task = await service.succeed(task_id)

    assert task.status is TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# 过载保护
# ---------------------------------------------------------------------------
async def test_overloaded_threshold_is_exclusive(
    make_service: Callable[..., TaskService],
) -> None:
    """未结束任务数达到上限即拒绝：``queue_max`` 就是真正的同时上限。"""
    service = make_service(queue_max=2)

    for index in range(2):
        await service.create(
            type_=TaskType.DOCUMENT_INGEST,
            user_id="u_1",
            resource_type=ResourceType.DOCUMENT,
            resource_id=f"doc_{index}",
        )

    with pytest.raises(AppError) as excinfo:
        await service.create(
            type_=TaskType.DOCUMENT_INGEST,
            user_id="u_1",
            resource_type=ResourceType.DOCUMENT,
            resource_id="doc_3",
        )
    assert excinfo.value.code is ErrorCode.OVERLOADED
    assert excinfo.value.details == {"open": 2, "queue_max": 2}
    assert await service.count_open() == 2


async def test_terminal_tasks_do_not_count_towards_capacity(
    make_service: Callable[..., TaskService],
) -> None:
    """已结束的任务不占额度，否则跑久了必然「过载」。"""
    service = make_service(queue_max=1)
    task_id = await _running(service)
    await service.succeed(task_id)

    task, _ = await service.create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_2",
    )
    assert task.status is TaskStatus.PENDING


async def test_queue_max_none_disables_check(make_service: Callable[..., TaskService]) -> None:
    """``queue_max=None`` = 不做检查（测试与本地默认）。"""
    service = make_service()
    for index in range(5):
        await service.create(
            type_=TaskType.DOCUMENT_INGEST,
            user_id="u_1",
            resource_type=ResourceType.DOCUMENT,
            resource_id=f"doc_{index}",
        )
    assert await service.count_open() == 5


# ---------------------------------------------------------------------------
# error_retryable：两个条件都要满足
# ---------------------------------------------------------------------------
def _failed(code: str | None, *, retry_count: int = 0, max_retries: int = 3) -> Any:
    from app.tasks.models import Task

    return Task(
        id="task_1",
        type=TaskType.DOCUMENT_INGEST,
        status=TaskStatus.FAILED,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
        retry_count=retry_count,
        max_retries=max_retries,
        error=TaskError(code=code or "", message="x") if code else None,
    )


@pytest.mark.parametrize("code", sorted(NON_RETRYABLE_CODES))
def test_error_retryable_false_for_deterministic_codes(code: str) -> None:
    """确定性错误码即使「重试预算还剩」也报 ``false``。"""
    assert error_retryable(_failed(code)) is False


@pytest.mark.parametrize("code", [str(ErrorCode.MQ_UNAVAILABLE), str(ErrorCode.UPSTREAM_TIMEOUT)])
def test_error_retryable_true_for_transient_codes(code: str) -> None:
    """偶发类错误 + 还有预算 → ``true``。"""
    assert error_retryable(_failed(code)) is True


def test_error_retryable_false_when_budget_exhausted() -> None:
    """预算用尽 → ``false``（再点重试只会拿到 409）。"""
    assert error_retryable(_failed(str(ErrorCode.MQ_UNAVAILABLE), retry_count=3)) is False


def test_error_retryable_without_error_record() -> None:
    """没有错误记录（数据异常）→ 按「未知错误码可重试」处理。"""
    assert error_retryable(_failed(None)) is True


async def test_service_uses_event_bus_when_given_one() -> None:
    """把真的事件总线接进去时，运行中的任务能收到进度帧（端到端的一小段）。

    注意**必须先预热订阅**：``subscribe`` 是异步生成器，函数体要到第一次
    ``__anext__`` 才执行，光拿到迭代器对象并不代表订阅已生效 —— 先发布再
    ``anext`` 的话那一帧会掉在地上，然后这里就永远等不到（这正是
    ``stream_task_events`` 里那次预热的由来）。
    """
    bus = InMemoryTaskEventBus()
    service = TaskService(InMemoryTaskStore(), events=bus.publish)
    task_id = await _running(service)
    subscription = bus.subscribe(task_id)
    pending = asyncio.ensure_future(anext(subscription))
    await asyncio.sleep(0)

    await service.report_progress(task_id, stage="embedding", progress=55)
    frame = await pending

    assert frame.data == {"stage": "embedding", "progress": 55}
    await subscription.aclose()


def test_build_task_store_defaults_to_memory() -> None:
    """非 ``real`` 后端一律内存实现（Worker 会因此拒绝启动，见 ``app/worker/__main__.py``）。"""
    from tests.conftest import build_settings

    assert isinstance(build_task_store(build_settings(infra_backend="memory")), InMemoryTaskStore)
