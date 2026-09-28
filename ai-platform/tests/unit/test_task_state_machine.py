"""任务状态机单测（``docs/08`` §2 / §4）。

契约测试只能看到「接口返回什么」，这里补的是**状态机本身的不变量**：
合法迁移的白名单、终态不可变、进度不可回退、幂等语义的精确形状。
这些一旦坏掉，症状是「任务偶尔卡住」或「重复执行」，从接口层很难复现。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import AppError, ErrorCode
from app.tasks.models import (
    ALLOWED_TRANSITIONS,
    CANCELABLE_STATUSES,
    TERMINAL_STATUSES,
    ResourceType,
    TaskError,
    TaskStatus,
    TaskType,
    ensure_transition,
)
from app.tasks.service import TaskService, make_idem_key
from app.tasks.store import InMemoryTaskStore


@pytest.fixture
def tasks() -> TaskService:
    """每个用例一个独立的任务服务（内存存储）。"""
    return TaskService(InMemoryTaskStore(), max_retries=2)


async def _new_task(tasks: TaskService, **kwargs: Any) -> Any:
    task, _ = await tasks.create(
        type_=kwargs.pop("type_", TaskType.DOCUMENT_INGEST),
        user_id=kwargs.pop("user_id", "u_1"),
        resource_type=kwargs.pop("resource_type", ResourceType.DOCUMENT),
        resource_id=kwargs.pop("resource_id", "doc_1"),
        **kwargs,
    )
    return task


# ---------------------------------------------------------------------------
# 静态不变式
# ---------------------------------------------------------------------------


def test_terminal_statuses_are_consistent_with_transitions() -> None:
    """终态 MUST 没有任何出边（否则「终结」只是口头约定）。"""
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset(), status


def test_cancelable_statuses_are_non_terminal() -> None:
    """可取消状态与终态不能有交集。"""
    assert frozenset() == CANCELABLE_STATUSES & TERMINAL_STATUSES


def test_ensure_transition_rejects_illegal_move() -> None:
    """非法迁移抛 ``409 CONFLICT`` 并带上起止状态（便于直接定位调用点）。"""

    class _Task:
        id = "task_stub"
        status = TaskStatus.SUCCEEDED

    with pytest.raises(AppError) as excinfo:
        ensure_transition(_Task(), TaskStatus.RUNNING)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.CONFLICT
    assert excinfo.value.details["from"] == "SUCCEEDED"
    assert excinfo.value.details["to"] == "RUNNING"


def test_ensure_transition_allows_whitelisted_move() -> None:
    """白名单内的迁移不抛异常。"""

    class _Task:
        id = "task_stub"
        status = TaskStatus.PENDING

    assert ensure_transition(_Task(), TaskStatus.QUEUED) is None  # type: ignore[arg-type]


def test_idem_key_is_64_char_hex_and_stable() -> None:
    """幂等键是 64 位 hex（对应 ``idem_key CHAR(64)``），且同输入同结果。"""
    first = make_idem_key("ingest", "doc_1", 1)
    second = make_idem_key("ingest", "doc_1", 1)
    other = make_idem_key("ingest", "doc_2", 1)

    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert first == second
    assert first != other


# ---------------------------------------------------------------------------
# 创建与幂等
# ---------------------------------------------------------------------------


async def test_create_is_idempotent_on_idem_key(tasks: TaskService) -> None:
    """同幂等键重复创建返回同一任务且 ``created=False``。"""
    first, created_first = await tasks.create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
        idem_key="same",
    )
    second, created_second = await tasks.create(
        type_=TaskType.DOCUMENT_INGEST,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
        idem_key="same",
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_get_isolates_by_user(tasks: TaskService) -> None:
    """跨用户取任务 → ``404 TASK_NOT_FOUND``（不是 403，避免泄露存在性）。"""
    task = await _new_task(tasks, user_id="u_1")

    with pytest.raises(AppError) as excinfo:
        await tasks.get(task.id, "u_2")

    assert excinfo.value.code == ErrorCode.TASK_NOT_FOUND


async def test_get_without_user_id_is_allowed(tasks: TaskService) -> None:
    """内部调用（Worker）不带 ``user_id``：它只有任务 ID。"""
    task = await _new_task(tasks, user_id="u_1")

    assert (await tasks.get(task.id)).id == task.id


# ---------------------------------------------------------------------------
# 推进
# ---------------------------------------------------------------------------


async def test_lifecycle_stamps_timestamps(tasks: TaskService) -> None:
    """每一步都留下时间戳，且 ``progress`` 单调递增到 100。"""
    task = await _new_task(tasks)

    queued = await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.report_progress(task.id, stage="embedding", progress=55)
    done = await tasks.succeed(task.id)

    assert queued.status is TaskStatus.QUEUED
    assert queued.queued_at
    assert done.status is TaskStatus.SUCCEEDED
    assert done.progress == 100
    assert done.started_at and done.finished_at
    assert done.stage == "embedding", "阶段信息保留下来，便于回答「卡在哪一步」"


async def test_progress_cannot_regress(tasks: TaskService) -> None:
    """进度回退 → ``400``：前端进度条只能前进，倒退会产生「卡在 30% 又跳回 10%」。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.report_progress(task.id, stage="embedding", progress=60)

    with pytest.raises(AppError) as excinfo:
        await tasks.report_progress(task.id, stage="parse", progress=30)

    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT
    current = await tasks.get(task.id)
    assert current.progress == 60, "被拒绝的进度不应写进去"


async def test_succeed_is_terminal(tasks: TaskService) -> None:
    """成功之后再改状态 → ``409``。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.succeed(task.id)

    with pytest.raises(AppError) as excinfo:
        await tasks.mark_running(task.id)

    assert excinfo.value.code == ErrorCode.CONFLICT


async def test_fail_records_error(tasks: TaskService) -> None:
    """失败要落错误码与消息（否则前端只能显示「失败」，用户无法处置）。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)

    failed = await tasks.fail(
        task.id, TaskError(code="UNPROCESSABLE_DOCUMENT", message="PDF 无文本层")
    )

    assert failed.status is TaskStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "UNPROCESSABLE_DOCUMENT"
    assert failed.error.at, "错误必须带时间戳"


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


async def test_cancel_pending_is_immediate(tasks: TaskService) -> None:
    """``PENDING`` 取消立即生效。"""
    task = await _new_task(tasks)

    canceled = await tasks.cancel(task.id, "u_1")

    assert canceled.status is TaskStatus.CANCELED
    assert canceled.finished_at


async def test_cancel_running_only_sets_flag(tasks: TaskService) -> None:
    """``RUNNING`` 只置取消标记，状态由 Worker 在检查点推进（``docs/08`` §4.3）。

    单批 Embedding 不可中断，所以接口不能立刻把状态改成 ``CANCELED``
    ——否则这一批还在写向量库，前端却已经显示「已取消」。
    """
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)

    returned = await tasks.cancel(task.id, "u_1")

    assert returned.status is TaskStatus.RUNNING, "不能立刻改状态：这一批还在跑"
    assert await tasks.is_cancel_requested(task.id) is True
    assert (await tasks.get(task.id)).status is TaskStatus.RUNNING, "库里也仍是 RUNNING"


async def test_cancel_running_is_idempotent(tasks: TaskService) -> None:
    """``RUNNING`` 下重复取消不报错、不改变状态。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)

    first = await tasks.cancel(task.id, "u_1")
    second = await tasks.cancel(task.id, "u_1")

    assert first.status is second.status is TaskStatus.RUNNING


async def test_cancel_twice_is_idempotent(tasks: TaskService) -> None:
    """重复取消返回当前状态，不报错（用户连点两次不该看到 409）。"""
    task = await _new_task(tasks)
    first = await tasks.cancel(task.id, "u_1")
    second = await tasks.cancel(task.id, "u_1")

    assert first.status is second.status is TaskStatus.CANCELED
    assert first.updated_at == second.updated_at


async def test_cancel_terminal_conflicts(tasks: TaskService) -> None:
    """终态取消 → ``409 TASK_NOT_CANCELABLE``。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.succeed(task.id)

    with pytest.raises(AppError) as excinfo:
        await tasks.cancel(task.id, "u_1")

    assert excinfo.value.code == ErrorCode.TASK_NOT_CANCELABLE


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


async def test_retry_resets_progress_and_started_at(tasks: TaskService) -> None:
    """重试把上一轮的进度、阶段、错误、``started_at`` 全部清掉。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.report_progress(task.id, stage="embedding", progress=70)
    await tasks.fail(task.id, TaskError(code="RETRIEVAL_FAILED", message="炸了"))

    retried, requeued = await tasks.retry(task.id, "u_1")

    assert requeued is True
    assert retried.status is TaskStatus.QUEUED
    assert retried.progress == 0
    assert retried.stage is None
    assert retried.error is None
    assert retried.finished_at is None
    assert retried.started_at is None, "上一轮的开始时间必须清掉，否则耗时会算出负数/跨轮"
    assert retried.retry_count == 1


async def test_retry_is_idempotent_while_queued(tasks: TaskService) -> None:
    """``QUEUED``/``RUNNING`` 下重试返回同一任务且 ``requeued=False``（不重复投递）。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)

    again, requeued = await tasks.retry(task.id, "u_1")

    assert requeued is False
    assert again.status is TaskStatus.QUEUED
    assert again.retry_count == 0, "幂等返回不该改变重试次数"

    await tasks.mark_running(task.id)
    running, requeued_running = await tasks.retry(task.id, "u_1")
    assert requeued_running is False
    assert running.status is TaskStatus.RUNNING


async def test_retry_stops_at_max_retries(tasks: TaskService) -> None:
    """``retry_count >= max_retries`` → ``409 TASK_NOT_RETRYABLE``。"""
    task = await _new_task(tasks, max_retries=1)
    await tasks.mark_queued(task.id)
    await tasks.mark_running(task.id)
    await tasks.fail(task.id, TaskError(code="INTERNAL_ERROR"))

    await tasks.retry(task.id, "u_1")
    await tasks.mark_running(task.id)
    await tasks.fail(task.id, TaskError(code="INTERNAL_ERROR"))

    with pytest.raises(AppError) as excinfo:
        await tasks.retry(task.id, "u_1")

    assert excinfo.value.code == ErrorCode.TASK_NOT_RETRYABLE
    assert excinfo.value.details["retry_count"] == 1


async def test_retry_pending_task_is_not_allowed(tasks: TaskService) -> None:
    """``PENDING`` 不在「允许重试」之列（它还没开始跑，无从重试）。"""
    task = await _new_task(tasks)

    with pytest.raises(AppError) as excinfo:
        await tasks.retry(task.id, "u_1")

    assert excinfo.value.code == ErrorCode.TASK_NOT_RETRYABLE


# ---------------------------------------------------------------------------
# track：执行器与状态机的契约
# ---------------------------------------------------------------------------


async def test_track_marks_succeeded(tasks: TaskService) -> None:
    """正常退出 → ``SUCCEEDED`` 且进度到 100。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)  # track 从 QUEUED 开始（PENDING 还没被投递）

    async with tasks.track(task.id):
        pass

    done = await tasks.get(task.id)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.progress == 100


async def test_track_requires_queued_state(tasks: TaskService) -> None:
    """``PENDING`` 直接 ``track`` → ``409``：没经过投递就执行 = 绕过了幂等与排队。"""
    task = await _new_task(tasks)

    with pytest.raises(AppError) as excinfo:
        async with tasks.track(task.id):
            pass

    assert excinfo.value.code == ErrorCode.CONFLICT


async def test_track_marks_failed_on_app_error(tasks: TaskService) -> None:
    """``AppError`` → ``FAILED`` 并把错误码落库（``REQ-TASK`` 的错误可见性）。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)

    with pytest.raises(AppError):
        async with tasks.track(task.id):
            raise AppError(ErrorCode.UNPROCESSABLE_DOCUMENT, "PDF 无文本层")

    failed = await tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "UNPROCESSABLE_DOCUMENT"


async def test_track_marks_failed_on_unexpected_error(tasks: TaskService) -> None:
    """未预期异常也落 ``FAILED``（码为 ``INTERNAL_ERROR``），不能停在 ``RUNNING``。"""
    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)

    with pytest.raises(RuntimeError):
        async with tasks.track(task.id):
            raise RuntimeError("意外炸了")

    failed = await tasks.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == ErrorCode.INTERNAL_ERROR.value


async def test_track_marks_canceled_on_cancellation(tasks: TaskService) -> None:
    """被取消（``CancelledError``）→ ``CANCELED``，且异常继续向上抛。"""
    import asyncio

    task = await _new_task(tasks)
    await tasks.mark_queued(task.id)

    with pytest.raises(asyncio.CancelledError):
        async with tasks.track(task.id):
            raise asyncio.CancelledError

    canceled = await tasks.get(task.id)
    assert canceled.status is TaskStatus.CANCELED


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


async def test_list_tasks_filters_and_sorts(tasks: TaskService) -> None:
    """列表按创建时间倒序，且支持多种过滤（``REQ-TASK-003``）。"""
    first = await _new_task(tasks, resource_id="doc_1")
    second = await _new_task(tasks, resource_id="doc_2", type_=TaskType.DOCUMENT_DELETE)
    await tasks.mark_queued(second.id)

    items, _ = await tasks.list_tasks(user_id="u_1", limit=10)
    assert next(item.id for item in items) == second.id, "最新的排在最前"

    by_type, _ = await tasks.list_tasks(user_id="u_1", limit=10, type_=TaskType.DOCUMENT_DELETE)
    assert [item.id for item in by_type] == [second.id]

    by_status, _ = await tasks.list_tasks(user_id="u_1", limit=10, status=[TaskStatus.PENDING])
    assert [item.id for item in by_status] == [first.id]

    by_resource, _ = await tasks.list_tasks(user_id="u_1", limit=10, resource_id="doc_1")
    assert [item.id for item in by_resource] == [first.id]

    empty, _ = await tasks.list_tasks(user_id="u_2", limit=10)
    assert empty == []


async def test_list_tasks_paginates(tasks: TaskService) -> None:
    """分页返回 ``next_cursor``，续页不重不漏。"""
    created = [await _new_task(tasks, resource_id=f"doc_{index}") for index in range(3)]

    page1, cursor = await tasks.list_tasks(user_id="u_1", limit=2)
    assert len(page1) == 2
    assert cursor is not None

    page2, cursor2 = await tasks.list_tasks(user_id="u_1", limit=2, cursor=cursor)
    assert len(page2) == 1
    assert cursor2 is None

    assert {task.id for task in page1} | {task.id for task in page2} == {
        task.id for task in created
    }
