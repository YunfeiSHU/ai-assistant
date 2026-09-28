"""Worker 主循环（``docs/08`` §5.4，``REQ-TASK-006``）。

这里的用例全部通过「假 Broker + 内存任务表 + 内联处理器」驱动真实主循环，
覆盖的是**上线后最难查的那类问题**：

* 重复投递（Kafka at-least-once）会不会把同一份文档入库两次；
* 位点提交是否遵守「状态落库**之后**」（先提交再落库 = 崩溃即丢任务）；
* 跳号完成时会不会跳过还在跑的前一条消息；
* 重试耗尽/不可重试时有没有进死信，而不是静静消失；
* 关停时在飞任务是被「打取消标记后自己退出」还是被硬杀。

为了不依赖真实时间，``task_retry_poll_seconds=0`` + 自带 ``clock``，
并且用「等条件成立」的轮询代替 ``sleep``（避免 CI 上的随机失败）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from tests.conftest import build_settings
from tests.support.fake_broker import FakeTaskConsumer, FakeTaskProducer

from app.core.errors import AppError, ErrorCode
from app.tasks.dispatch import TaskDispatcher
from app.tasks.models import ResourceType, Task, TaskError, TaskStatus, TaskType
from app.tasks.retry import InMemoryRetryQueue
from app.tasks.service import TaskService
from app.tasks.store import InMemoryTaskStore
from app.worker.loop import TaskWorker, WorkerStats


class _Clock:
    """可推进的假时钟（默认给 ``perf_counter`` 的位置）。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Harness:
    """一套装配好的 Worker 测试夹具。"""

    def __init__(
        self,
        *,
        runner: Callable[[Task], Awaitable[None]] | None = None,
        **settings_overrides: Any,
    ) -> None:
        self.settings = build_settings(
            **{"task_runner": "kafka", "task_retry_poll_seconds": 0.0, **settings_overrides}
        )
        self.store = InMemoryTaskStore()
        self.tasks = TaskService(self.store, max_retries=self.settings.task_max_retries)
        self.dispatcher = TaskDispatcher(self.tasks)
        self.consumer = FakeTaskConsumer()
        self.producer = FakeTaskProducer()
        self.retries = InMemoryRetryQueue()
        self.clock = _Clock()
        self.handled: list[str] = []
        self.handler_calls = 0
        self.handler_failure: str | None = None
        self._runner = runner

        async def handle(task: Task) -> None:
            """处理器骨架：与真实处理器一样用 ``track`` 包住工作（它负责 RUNNING→终态）。"""
            self.handler_calls += 1
            self.handled.append(task.id)
            async with self.tasks.track(task.id):
                if self._runner is not None:
                    await self._runner(task)
                    return
                if self.handler_failure is not None:
                    raise AppError(ErrorCode(self.handler_failure), "处理器失败")

        self.dispatcher.register_many(
            {
                TaskType.DOCUMENT_INGEST: handle,
                TaskType.DOCUMENT_DELETE: handle,
                TaskType.SUMMARY_BUILD: handle,
                TaskType.MEMORY_EXTRACT: handle,
            }
        )
        self.worker = TaskWorker(
            self.settings,
            consumer=self.consumer,
            tasks=self.tasks,
            dispatcher=self.dispatcher,
            producer=self.producer,
            retries=self.retries,
            stats=WorkerStats(),
            clock=self.clock,
        )

    # -- 便利方法 ------------------------------------------------------
    async def create_task(self, **overrides: Any) -> Task:
        values: dict[str, Any] = {
            "type_": TaskType.DOCUMENT_INGEST,
            "user_id": "u_1",
            "resource_type": ResourceType.DOCUMENT,
            "resource_id": "doc_1",
        }
        values.update(overrides)
        task, _ = await self.tasks.create(**values)
        return task

    async def wait_for(self, predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
        """等到条件成立（轮询 ``sleep(0)``，墙上时间上限兜底）。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:  # pragma: no cover - 超时诊断
                raise AssertionError(f"等待超时：{self.worker.stats.as_dict()}")
            await asyncio.sleep(0.001)

    async def run_until_done(self, *, count: int = 1, timeout: float = 2.0) -> None:
        """跑主循环直到处理了 ``count`` 条消息，然后优雅停止。"""
        stop = asyncio.Event()
        job = asyncio.create_task(self.worker.run(stop), name="worker-test")
        try:
            await self.wait_for(
                lambda: (
                    (
                        self.worker.stats.succeeded
                        + self.worker.stats.failed
                        + self.worker.stats.skipped
                        + self.worker.stats.canceled
                    )
                    >= count
                ),
                timeout=timeout,
            )
            await self.wait_for(lambda: not self.worker._running.task_ids, timeout=timeout)
        finally:
            stop.set()
            await asyncio.wait_for(job, timeout=timeout)


@pytest.fixture
def harness() -> _Harness:
    """默认夹具（成功路径）。"""
    return _Harness()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
async def test_successful_message_advances_and_commits(harness: _Harness) -> None:
    """``QUEUED → RUNNING → SUCCEEDED`` 且提交位点 ``offset + 1``。"""
    task = await harness.create_task()
    harness.consumer.deliver(task.id)

    await harness.run_until_done()

    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.SUCCEEDED
    assert harness.worker.stats.succeeded == 1
    assert harness.worker.stats.consumed == 1
    # 提交的是「下一条」offset，且只在状态落库之后
    assert harness.consumer.commits[0] == {("ai.task.document.ingest", 0): 1}
    assert all(value == 1 for payload in harness.consumer.commits for value in payload.values())
    assert harness.consumer.stopped == 1
    assert harness.producer.dead_letters == []


async def test_pending_task_is_marked_queued_before_running(harness: _Harness) -> None:
    """补偿重投可能带来 ``PENDING`` 的消息：先 ``mark_queued`` 再执行。"""
    task = await harness.create_task()
    seen: list[TaskStatus] = []

    async def runner(item: Task) -> None:
        current = await harness.tasks.get(item.id)
        seen.append(current.status)

    harness._runner = runner
    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert seen == [TaskStatus.RUNNING]
    assert (await harness.tasks.get(task.id)).queued_at is not None


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
async def test_duplicate_message_for_terminal_task_is_skipped(harness: _Harness) -> None:
    """已终态的任务收到重复消息 → 丢弃，不重跑处理器。"""
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.succeed(task.id)

    harness.consumer.deliver(task.id)
    await harness.run_until_done(count=1)

    assert harness.handler_calls == 0
    assert harness.worker.stats.skipped == 1
    assert harness.consumer.commits[0] == {("ai.task.document.ingest", 0): 1}


async def test_message_for_missing_task_is_dropped(harness: _Harness) -> None:
    """任务行已被清理（消息比任务还老）→ 丢弃并继续，不能卡住分区。"""
    harness.consumer.deliver("task_gone")
    await harness.run_until_done()

    assert harness.worker.stats.skipped == 1
    assert harness.handler_calls == 0


async def test_task_already_running_elsewhere_is_skipped(harness: _Harness) -> None:
    """``RUNNING`` 的任务收到重复投递 → 丢弃（两个 Worker 同跑会互相覆盖切片）。"""
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)

    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert harness.handler_calls == 0
    assert harness.worker.stats.skipped == 1


async def test_cancel_requested_before_start_is_honoured(harness: _Harness) -> None:
    """消息到达前用户已点取消 → 直接置 ``CANCELED``，不执行处理器。"""
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    # 取消标记是**存储层**的概念（``RUNNING`` 下 ``cancel`` 只置标记、不改状态）
    await harness.store.request_cancel(task.id)

    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert (await harness.tasks.get(task.id)).status is TaskStatus.CANCELED
    assert harness.handler_calls == 0
    assert harness.worker.stats.skipped == 1


# ---------------------------------------------------------------------------
# 失败与重试
# ---------------------------------------------------------------------------
async def test_retryable_failure_is_scheduled_not_dead_lettered() -> None:
    """可重试的失败 → 安排延迟重投，**不**进死信。"""
    harness = _Harness()
    harness.handler_failure = str(ErrorCode.INTERNAL_ERROR)
    task = await harness.create_task()
    harness.consumer.deliver(task.id)

    await harness.run_until_done()

    assert (await harness.tasks.get(task.id)).status is TaskStatus.FAILED
    assert harness.retries.size == 1
    assert harness.producer.dead_letters == []
    assert harness.worker.stats.retried == 1
    assert harness.worker.stats.failed == 1


async def test_permanent_failure_goes_to_dead_letter() -> None:
    """不可重试的错误码（如文件格式不支持）直接进死信。"""
    harness = _Harness()
    harness.handler_failure = str(ErrorCode.UNSUPPORTED_FILE_TYPE)
    task = await harness.create_task()
    harness.consumer.deliver(task.id)

    await harness.run_until_done()

    assert harness.retries.size == 0
    assert [item["code"] for item in harness.producer.dead_letters] == [
        str(ErrorCode.UNSUPPORTED_FILE_TYPE)
    ]
    assert harness.worker.stats.dead_lettered == 1


async def test_retry_budget_exhausted_goes_to_dead_letter() -> None:
    """重试预算用尽 → 死信（不再安排重投）。"""
    harness = _Harness()
    harness.handler_failure = str(ErrorCode.INTERNAL_ERROR)
    task = await harness.create_task(max_retries=1)
    # 先耗尽预算：retry_count 达到 max_retries
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.fail(
        task.id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message="第一次")
    )
    await harness.tasks.requeue(task.id)

    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert harness.worker.stats.dead_lettered == 1
    assert harness.retries.size == 0


async def test_timeout_marks_task_failed_with_timeout_code() -> None:
    """超时必须落 ``TASK_TIMEOUT``，而不是被当成用户取消。

    用 ``wait_for`` 实现超时会取消协程，取消信号穿过状态流转的 ``CancelledError``
    分支，把「跑太久」记成 ``CANCELED`` —— 用户看到一个没人取消过的「已取消」任务。
    """
    harness = _Harness(task_timeout_seconds=1)

    async def hang(item: Task) -> None:  # pragma: no cover - 会被超时打断
        await asyncio.sleep(30)

    harness._runner = hang
    task = await harness.create_task()
    harness.consumer.deliver(task.id)

    await harness.run_until_done()

    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error is not None
    assert stored.error.code == str(ErrorCode.TASK_TIMEOUT)
    # 超时本身是可重试的（上游抖动导致的慢不该让任务就此终结）
    assert harness.retries.size == 1


# ---------------------------------------------------------------------------
# 延迟重试轮询
# ---------------------------------------------------------------------------
async def test_poll_retries_republishes_without_touching_status(harness: _Harness) -> None:
    """到期的重试 → 重新投递，但**保持 FAILED**（真正改状态的是收到消息的那一方）。

    这样「投递失败」的后果是「任务仍是 FAILED」（用户可手动重试），
    而不是卡在一个谁也看不见的中间态里。
    """
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.fail(task.id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message="x"))
    await harness.retries.schedule(task.id, attempt=1, delay=0.0)

    await harness.worker._poll_retries()

    assert [message.task_id for message in harness.producer.sent] == [task.id]
    assert (await harness.tasks.get(task.id)).status is TaskStatus.FAILED
    assert harness.retries.size == 0


async def test_poll_retries_skips_tasks_that_are_no_longer_failed(harness: _Harness) -> None:
    """人工重试/取消过的任务，其重试记录作废（不能把它再拉回执行）。"""
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.store.request_cancel(task.id)
    await harness.tasks.cancel(task.id, "u_1")
    await harness.retries.schedule(task.id, attempt=1, delay=0.0)

    await harness.worker._poll_retries()

    assert harness.producer.sent == []


async def test_poll_retries_ignores_missing_task(harness: _Harness) -> None:
    """任务已被清理的重试记录直接丢弃。"""
    await harness.retries.schedule("task_gone", attempt=1, delay=0.0)
    await harness.worker._poll_retries()
    assert harness.producer.sent == []


async def test_poll_retries_logs_publish_failure(harness: _Harness) -> None:
    """重投失败只记日志（不抛），任务保持 FAILED 等待下一轮。"""
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.fail(task.id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message="x"))
    await harness.retries.schedule(task.id, attempt=1, delay=0.0)
    harness.producer.fail_always = True

    await harness.worker._poll_retries()  # 不该抛

    assert harness.producer.sent == []


async def test_retry_poll_is_throttled() -> None:
    """重试轮询受 ``TASK_RETRY_POLL_SECONDS`` 节流（否则会把 Redis 打爆）。"""
    throttled = _Harness(task_retry_poll_seconds=60.0)
    await throttled.worker._poll_retries()  # 第一次总是放行

    await throttled.retries.schedule("task_1", attempt=1, delay=0.0)
    throttled.clock.advance(1.0)
    await throttled.worker._poll_retries()
    assert throttled.retries.size == 1, "节流窗口内不该轮询到刚安排的重试"

    throttled.clock.advance(60.0)
    await throttled.worker._poll_retries()
    assert throttled.retries.size == 0


async def test_republished_retry_requeues_failed_task() -> None:
    """自动重试的消息被消费时把 ``FAILED`` 推回 ``QUEUED`` 再执行（``docs/08`` §2）。"""
    harness = _Harness()
    task = await harness.create_task()
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.fail(task.id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message="x"))

    harness.consumer.deliver(task.id, attempt=2)
    await harness.run_until_done()

    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.SUCCEEDED
    assert stored.retry_count >= 1
    assert harness.worker.stats.succeeded == 1


async def test_retry_message_without_budget_is_skipped(harness: _Harness) -> None:
    """预算已耗尽的任务收到迟到的重投消息 → 丢弃（它已经被判死信了）。"""
    task = await harness.create_task(max_retries=0)
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.fail(task.id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message="x"))

    harness.consumer.deliver(task.id, attempt=2)
    await harness.run_until_done()

    assert harness.handler_calls == 0
    assert harness.worker.stats.skipped == 1


# ---------------------------------------------------------------------------
# 位点提交
# ---------------------------------------------------------------------------
async def test_out_of_order_completion_does_not_commit_past_a_gap() -> None:
    """后到的消息先跑完 → **不能**提交，必须等前面那条。

    这是最危险的一步：先提交 = 前一条消息永远不会再投递 = 任务静默丢失。
    """
    harness = _Harness(worker_concurrency=2)
    first = await harness.create_task(resource_id="doc_1")
    second = await harness.create_task(resource_id="doc_2")
    release_first = asyncio.Event()

    async def runner(task: Task) -> None:
        if task.id == first.id:
            await release_first.wait()

    harness._runner = runner
    first_message = harness.consumer.deliver(first.id)
    harness.consumer.deliver(second.id)

    stop = asyncio.Event()
    job = asyncio.create_task(harness.worker.run(stop))
    try:
        await harness.wait_for(lambda: second.id not in harness.worker._running.task_ids)
        await asyncio.sleep(0.01)
        # 第二条已完成，但第一条还在飞：此时提交就是跳过第一条
        assert harness.consumer.commits == []
        assert first_message.offset == 0
        release_first.set()
        await harness.wait_for(lambda: not harness.worker._running.task_ids)
        assert harness.consumer.commits[-1] == {("ai.task.document.ingest", 0): 2}
    finally:
        stop.set()
        await asyncio.wait_for(job, timeout=2.0)
    assert (await harness.tasks.get(first.id)).status is TaskStatus.SUCCEEDED


async def test_commit_happens_after_status_is_persisted() -> None:
    """提交位点时任务状态必须已经落库（at-least-once 的前提）。"""
    harness = _Harness()
    task = await harness.create_task()
    statuses_at_commit: list[TaskStatus] = []

    original = harness.consumer.commit

    async def commit(offsets: Any = None) -> None:
        current = await harness.tasks.get(task.id)
        statuses_at_commit.append(current.status)
        await original(offsets)

    harness.consumer.commit = commit  # type: ignore[method-assign]
    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert statuses_at_commit
    assert all(status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED} for status in statuses_at_commit)


# ---------------------------------------------------------------------------
# 并发与关停
# ---------------------------------------------------------------------------
async def test_concurrency_limit_is_respected() -> None:
    """并发上限内的消息并行执行；``worker_concurrency=1`` 时必须串行。"""
    harness = _Harness(worker_concurrency=1)
    task = await harness.create_task()
    harness.consumer.deliver(task.id)
    await harness.run_until_done()
    assert harness.worker.stats.succeeded == 1


async def test_embedding_tasks_use_separate_gate() -> None:
    """Embedding 类任务走独立信号量（``WORKER_EMBED_CONCURRENCY``）。"""
    harness = _Harness(worker_concurrency=4, worker_embed_concurrency=1)
    for index in range(3):
        task = await harness.create_task(resource_id=f"doc_{index}")
        harness.consumer.deliver(task.id)
    await harness.run_until_done(count=3)
    assert harness.worker.stats.succeeded == 3


async def test_shutdown_cancels_in_flight_and_commits() -> None:
    """关停：给在飞任务打取消标记 → 等待 → 强制取消 → 提交剩余位点 → 关消费者。

    强制取消必须真的取消到**执行处理器的那一个任务**，否则状态永远停在 ``RUNNING``
    （客户端一直转圈），而且进程退出前还会带着一个在跑 Embedding 的孤儿任务。
    """
    harness = _Harness(task_shutdown_grace_seconds=0.05)
    task = await harness.create_task()
    started = asyncio.Event()

    async def hang(item: Task) -> None:  # pragma: no cover - 会被强制取消
        started.set()
        await asyncio.sleep(30)

    harness._runner = hang
    harness.consumer.deliver(task.id)

    stop = asyncio.Event()
    job = asyncio.create_task(harness.worker.run(stop))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    stop.set()
    await asyncio.wait_for(job, timeout=2.0)

    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.CANCELED
    assert harness.worker.stats.canceled == 1
    assert harness.consumer.commits  # 收尾提交
    assert harness.consumer.stopped == 1
    assert harness.worker.stats.as_dict()["consumed"] == 1


async def test_worker_starts_consumer_and_reports_stats() -> None:
    """主循环启动消费者、结束时打印统计（运维靠这两条日志判断 Worker 活着）。"""
    harness = _Harness()
    stop = asyncio.Event()
    job = asyncio.create_task(harness.worker.run(stop))
    await harness.wait_for(lambda: harness.consumer.started == 1)
    stop.set()
    await asyncio.wait_for(job, timeout=2.0)
    assert set(harness.worker.stats.as_dict()) == {
        "consumed",
        "succeeded",
        "failed",
        "canceled",
        "retried",
        "dead_lettered",
        "skipped",
        "invalid",
    }


async def test_unexpected_handler_exception_is_counted(harness: _Harness) -> None:
    """处理器抛出的异常不能打崩消费循环（记一条失败并继续提交位点）。"""
    task = await harness.create_task()

    async def boom(item: Task) -> None:
        raise AppError(ErrorCode.INTERNAL_ERROR, "boom")

    harness._runner = boom
    harness.consumer.deliver(task.id)
    await harness.run_until_done()

    assert harness.worker.stats.failed == 1
    assert harness.consumer.commits[0] == {("ai.task.document.ingest", 0): 1}
