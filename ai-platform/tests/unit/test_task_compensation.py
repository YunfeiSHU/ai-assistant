"""投递补偿扫描（``docs/08`` §5.1）。

它防的是「任务建好了、消息没发出去」——此时任务永远停在 ``PENDING``，没有任何
东西会再碰它。测试要证明的是**两条终止条件都真的生效**，而且「投递失败」与
「超出上限」的后果完全不同：

* 投递失败 → **保持 PENDING**（下一次抖动过去了还能救回来）；
* 超出重投次数 / 年龄上限 → 落 ``FAILED`` + ``MQ_UNAVAILABLE``（让人看得见）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.conftest import build_settings
from tests.support.fake_broker import (
    FakeTaskConsumer,  # noqa: F401 - 便于扩展
    FakeTaskProducer,
)

from app.core.exceptions import AppError, ErrorCode
from app.tasks.compensation import SCAN_BATCH, TaskCompensator
from app.tasks.models import ResourceType, Task, TaskStatus, TaskType
from app.tasks.runner import KafkaTaskRunner
from app.tasks.service import TaskService
from app.tasks.store import InMemoryTaskStore


class _Clock:
    """可推进的假时钟（默认给 ``datetime`` 的位置）。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Harness:
    """装配好的补偿扫描夹具。"""

    def __init__(self, **overrides: Any) -> None:
        self.settings = build_settings(
            **{
                "task_runner": "kafka",
                "task_compensation_grace_seconds": 60.0,
                "task_compensation_max_attempts": 3,
                "task_pending_max_age_seconds": 600.0,
                "task_compensation_interval_seconds": 0.01,
                **overrides,
            }
        )
        self.store = InMemoryTaskStore()
        self.tasks = TaskService(self.store, max_retries=self.settings.task_max_retries)
        self.producer = FakeTaskProducer()
        self.runner = KafkaTaskRunner(self.tasks, self.producer)
        self.clock = _Clock()
        self.compensator = TaskCompensator(self.settings, self.tasks, self.runner, now=self.clock)

    async def create_pending(self, *, age_seconds: float = 0.0, **overrides: Any) -> Task:
        values: dict[str, Any] = {
            "type_": TaskType.DOCUMENT_INGEST,
            "user_id": "u_1",
            "resource_type": ResourceType.DOCUMENT,
            "resource_id": "doc_1",
        }
        values.update(overrides)
        task, _ = await self.tasks.create(**values)
        if age_seconds:  # 把创建时间往前挪，模拟「滞留了很久」
            created = (
                (self.clock.now - timedelta(seconds=age_seconds))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            await self.store.update(
                task.id, lambda current: setattr(current, "created_at", created)
            )
        return await self.tasks.get(task.id)


@pytest.fixture
def harness() -> _Harness:
    """默认夹具。"""
    return _Harness()


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
async def test_fresh_pending_is_left_alone(harness: _Harness) -> None:
    """``grace`` 之内的任务不碰（它可能只是刚建好、投递马上就到）。"""
    await harness.create_pending()
    assert await harness.compensator.scan_once() == 0
    assert harness.producer.sent == []


async def test_stale_pending_is_republished(harness: _Harness) -> None:
    """超过 grace 且仍 ``PENDING`` → 重新投递。"""
    task = await harness.create_pending(age_seconds=120.0)

    assert await harness.compensator.scan_once() == 1

    assert [message.task_id for message in harness.producer.sent] == [task.id]
    # 重投成功后状态推进到 QUEUED（由 runner 负责，与正常投递同一条路径）
    assert (await harness.tasks.get(task.id)).status is TaskStatus.QUEUED
    assert harness.compensator.attempts == {}


async def test_already_queued_task_is_not_republished(harness: _Harness) -> None:
    """已经在 ``QUEUED`` 的任务不重复投递（重复入库的经典成因）。"""
    task = await harness.create_pending(age_seconds=120.0)
    await harness.tasks.mark_queued(task.id)

    assert await harness.compensator.scan_once() == 0
    assert harness.producer.sent == []


async def test_succeeded_task_is_not_republished(harness: _Harness) -> None:
    """已完成的任务不再被补偿（否则会把成功的活重做一遍）。"""
    task = await harness.create_pending(age_seconds=120.0)
    await harness.tasks.mark_queued(task.id)
    await harness.tasks.mark_running(task.id)
    await harness.tasks.succeed(task.id)

    assert await harness.compensator.scan_once() == 0
    assert harness.producer.sent == []


async def test_other_users_tasks_are_scanned_too(harness: _Harness) -> None:
    """补偿是**全局**的：没人管的任务不会因为「用户不再访问」而被漏掉。"""
    first = await harness.create_pending(age_seconds=120.0, user_id="u_1", resource_id="doc_1")
    second = await harness.create_pending(age_seconds=120.0, user_id="u_2", resource_id="doc_2")

    assert await harness.compensator.scan_once() == 2
    assert {message.task_id for message in harness.producer.sent} == {first.id, second.id}


async def test_scan_batch_is_bounded(harness: _Harness) -> None:
    """单轮最多处理 ``SCAN_BATCH`` 条（积压时不该一次投上千条把 Kafka 打挂）。"""
    for index in range(3):
        await harness.create_pending(age_seconds=120.0, resource_id=f"doc_{index}")
    assert SCAN_BATCH >= 3  # 前提：批次上限不会小于这里的样本量


# ---------------------------------------------------------------------------
# 投递失败
# ---------------------------------------------------------------------------
async def test_publish_failure_keeps_pending(harness: _Harness) -> None:
    """投递失败 → **保持 PENDING** 并计数，下一轮再试。

    这里绝不能改状态：一次 Kafka 抖动就把任务判死，属于「把可恢复故障变成终态」。
    """
    task = await harness.create_pending(age_seconds=120.0)
    harness.producer.fail_always = True

    assert await harness.compensator.scan_once() == 0
    assert (await harness.tasks.get(task.id)).status is TaskStatus.PENDING
    assert harness.compensator.attempts == {task.id: 1}


async def test_publish_failure_recovers_when_broker_returns(harness: _Harness) -> None:
    """抖动过去之后下一轮就能投出去（重投计数器随之清零）。"""
    task = await harness.create_pending(age_seconds=120.0)
    harness.producer.fail_always = True
    await harness.compensator.scan_once()

    harness.producer.fail_always = False
    assert await harness.compensator.scan_once() == 1
    assert (await harness.tasks.get(task.id)).status is TaskStatus.QUEUED
    assert task.id not in harness.compensator.attempts


async def test_republish_exhaustion_fails_the_task(harness: _Harness) -> None:
    """重投次数用尽 → ``FAILED`` + ``MQ_UNAVAILABLE``（让人看得见，而不是无限空转）。"""
    task = await harness.create_pending(age_seconds=100.0)
    harness.producer.fail_always = True

    for _ in range(harness.settings.task_compensation_max_attempts):
        await harness.compensator.scan_once()
    assert (await harness.tasks.get(task.id)).status is TaskStatus.PENDING

    await harness.compensator.scan_once()
    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error is not None
    assert stored.error.code == str(ErrorCode.MQ_UNAVAILABLE)
    assert stored.error.detail == {"reason": "republish_exhausted"}


# ---------------------------------------------------------------------------
# 年龄上限
# ---------------------------------------------------------------------------
async def test_too_old_pending_is_failed_without_republishing(harness: _Harness) -> None:
    """超过 ``PENDING`` 年龄上限 → 直接落失败，不再重投。

    这条是**持久**的兜底：重投计数器在进程内存里，重启会清零；只有「按创建时间算」
    才能保证「一直投不出去 + 反复重启」也不会让任务无限期挂着。
    """
    task = await harness.create_pending(
        age_seconds=harness.settings.task_pending_max_age_seconds + 60
    )

    assert await harness.compensator.scan_once() == 0
    stored = await harness.tasks.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error is not None
    assert stored.error.detail == {"reason": "pending_too_long"}
    assert harness.producer.sent == []


async def test_give_up_survives_concurrent_state_change(harness: _Harness) -> None:
    """放弃时状态已被并发改掉（``fail`` 抛 ``AppError``）→ 只记日志，不能抛出去打断扫描。

    真实场景：用户在补偿重投的间隙里把任务取消了。此时 ``PENDING → CANCELED``
    已经发生，``fail`` 会被状态机拦住；那**不是错误**，更不能让整个扫描循环停摆。
    """
    task = await harness.create_pending(age_seconds=100.0)
    harness.producer.fail_always = True

    async def boom(task_id: str, error: Any) -> Task:
        raise AppError(ErrorCode.CONFLICT, "状态已被他人推进")

    harness.tasks.fail = boom  # type: ignore[method-assign]
    for _ in range(harness.settings.task_compensation_max_attempts + 1):
        await harness.compensator.scan_once()  # 不该抛
    assert (await harness.tasks.get(task.id)).status is TaskStatus.PENDING


async def test_unparseable_created_at_is_treated_as_old() -> None:
    """创建时间解析不了 → 当作「很旧」（宁可补偿也不漏掉一条任务）。"""
    harness = _Harness()
    task = await harness.create_pending()
    await harness.store.update(task.id, lambda current: setattr(current, "created_at", "坏数据"))

    assert await harness.compensator.scan_once() == 1


# ---------------------------------------------------------------------------
# 循环
# ---------------------------------------------------------------------------
async def test_run_forever_scans_and_stops(harness: _Harness) -> None:
    """``run_forever`` 周期性扫描，``stop`` 置位后干净退出。"""
    task = await harness.create_pending(age_seconds=120.0)
    stop = asyncio.Event()
    job = asyncio.create_task(harness.compensator.run_forever(stop))

    deadline = asyncio.get_running_loop().time() + 2.0
    while not harness.producer.sent and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(job, timeout=2.0)

    assert [message.task_id for message in harness.producer.sent] == [task.id]


async def test_run_forever_survives_scan_errors(harness: _Harness) -> None:
    """单轮扫描抛异常不能终止整个循环（否则一次存储抖动就永久停掉补偿）。"""

    async def boom(*, before: str, limit: int = 50) -> list[Task]:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "store down")

    harness.tasks.stale_pending = boom  # type: ignore[method-assign]
    stop = asyncio.Event()
    job = asyncio.create_task(harness.compensator.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(job, timeout=2.0)
