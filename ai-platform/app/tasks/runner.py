"""任务投递器：把「建好任务」变成「任务被执行」（``docs/08`` §1 / §2）。

三种模式对应三种真实部署形态，而不是三个复杂度档位：

* ``none``   —— 只建任务、不执行。Worker 未部署（本地只调接口）或测试要断言
  「任务已创建、但向量尚未写入」时用（``AC-RAG-04``）。
* ``inline`` —— 进程内后台任务。本地单进程开发用，避免为了跑通入库还要起 Kafka。
* ``kafka``  —— 投递到 Kafka，由独立 Worker 消费（``python -m app.worker``）。生产形态。

**投递与状态的关系**：``PENDING → QUEUED`` 必须发生在「任务行已提交」**且**
「消息已投递成功」之后（``docs/08`` §2）。顺序反了会出现两种数据不一致——
消息已发出但任务行还没提交（Worker 查不到任务），或任务标记为 QUEUED 但消息
发送失败（任务永远卡住、没有 Worker 会捡它）。

**投递失败不能只是把错误抛给用户**：抛 ``503 MQ_UNAVAILABLE`` 之后任务仍是
``PENDING``，它不会自己好起来 —— 所以 ``TASK_RUNNER=kafka`` 时应用 MUST 同时跑
:class:`~app.tasks.compensation.TaskCompensator`（``docs/08`` §5.1）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.observability.metrics import get_metrics
from app.observability.tracing import get_tracing
from app.tasks.models import Task, TaskStatus
from app.tasks.service import TaskService
from app.tasks.transport import TaskMessage, TaskProducer, build_task_producer

logger = logging.getLogger("app.tasks.runner")

#: 任务处理器：拿到任务行，自己负责用 ``TaskService.track`` 包住状态流转
TaskHandler = Callable[[Task], Awaitable[None]]


@runtime_checkable
class TaskRunner(Protocol):
    """任务投递端口。"""

    async def submit(self, task: Task) -> None:
        """投递任务（``PENDING → QUEUED``）。"""
        ...


class NullTaskRunner:
    """只建任务、不执行（``TASK_RUNNER=none``）。"""

    async def submit(self, task: Task) -> None:
        logger.info(
            "task.submit_skipped",
            extra={"task_id": task.id, "reason": "runner_disabled"},
        )


class InlineTaskRunner:
    """进程内后台执行（``TASK_RUNNER=inline``）。"""

    def __init__(
        self,
        tasks: TaskService,
        handler: TaskHandler,
        *,
        concurrency: int = 2,
    ) -> None:
        self._tasks = tasks
        self._handler = handler
        self._gate = asyncio.Semaphore(max(1, concurrency))
        # 强引用住后台任务：只 create_task 不保存会被 GC 回收，任务会静默消失
        self._jobs: set[asyncio.Task[None]] = set()
        #: 正在执行的任务 id（防止重复投递导致同一文档被并行入库）
        self._inflight: set[str] = set()

    async def submit(self, task: Task) -> None:
        """确保任务被执行（幂等，同一任务不会在本进程内重复跑）。

        状态门限刻意是 ``PENDING | QUEUED`` 而不是只允许 ``PENDING``：
        ``docs/08`` §4.4 要求重试时先置 ``QUEUED`` 再投递，于是投递时任务已经是
        ``QUEUED``。只收 ``PENDING`` 会把重试的任务静默跳过——接口返回 200、
        状态是 ``QUEUED``、日志也没有异常，但没有任何东西会执行它。
        真正要挡住的是「已在执行 / 已终态」与「重复投递」。
        """
        current = await self._tasks.get(task.id)
        if current.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
            logger.info(
                "task.submit_skipped",
                extra={"task_id": task.id, "status": str(current.status)},
            )
            return
        if task.id in self._inflight:
            # 重复投递：同一个文档并行跑两次入库会互相覆盖切片，必须挡住
            logger.info("task.submit_skipped", extra={"task_id": task.id, "reason": "inflight"})
            return
        if current.status is TaskStatus.PENDING:
            await self._tasks.mark_queued(task.id)
        self._inflight.add(task.id)
        self._jobs.add(asyncio.create_task(self._run(task.id), name=f"task:{task.id}"))

    async def _run(self, task_id: str) -> None:
        started = time.perf_counter()
        task_type = ""
        status = "unknown"
        try:
            async with self._gate:
                task = await self._tasks.get(task_id)
                task_type = str(task.type)
                with get_tracing().span(
                    "task.run",
                    {
                        "task_id": task_id,
                        "type": task_type,
                        "stage": str(task.stage or ""),
                    },
                ):
                    if await self._tasks.is_cancel_requested(task_id):
                        await self._tasks.cancel(task_id, task.user_id)
                        status = str(TaskStatus.CANCELED)
                        return
                    await self._handler(task)
                # 处理器内部已通过 ``TaskService.track`` 落终态，这里回读一次真实状态：
                # 猜「跑完 handler 就是 succeeded」在 handler 内部捕获异常时是错的
                status = str((await self._tasks.get(task_id)).status)
        except Exception as exc:
            # 处理器内部已通过 TaskService.track 落 FAILED，这里只保证不被吞掉
            status = str(TaskStatus.FAILED)
            logger.warning("task.runner_failed", extra={"task_id": task_id, "error": str(exc)})
        finally:
            self._inflight.discard(task_id)
            # 指标只在得到**终态**时记（``docs/10`` §5.2：``ai_task_total`` 是终态计数）。
            # canceled 也会走到这里：它同样是「一次任务结束了」，只是结果不同。
            if status not in {"unknown", str(TaskStatus.PENDING), str(TaskStatus.RUNNING)}:
                get_metrics().record_task(
                    task_type=task_type or "unknown",
                    status=status,
                    seconds=time.perf_counter() - started,
                )

    async def drain(self) -> None:
        """等所有在飞任务结束（优雅停机 / 测试收尾）。"""
        while self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)

    async def shutdown(self) -> None:
        """取消所有在飞任务（进程退出路径）。"""
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)


class KafkaTaskRunner:
    """投递到 Kafka（``TASK_RUNNER=kafka``，``docs/09`` §5.2）。

    只做两件事，顺序固定：**先确认消息落地，再改状态**。反过来的话，「标记为
    QUEUED 但消息没发出去」的任务会一直等着一个不存在的消息，而补偿扫描要等
    ``grace_seconds`` 之后才会救它。

    主题与分区键的推导在 :mod:`app.tasks.transport`（连同「消息体只含
    ``task_id`` + ``attempt``」这条约定一起单测）。
    """

    def __init__(self, tasks: TaskService, producer: TaskProducer) -> None:
        self._tasks = tasks
        self._producer = producer

    async def submit(self, task: Task) -> None:
        """投递任务（``docs/08`` §5.1）。"""
        message = TaskMessage.from_task(task)
        await self._producer.send(message)
        queued = await self._tasks.mark_queued(task.id)
        logger.info(
            "task.queued",
            extra={"task_id": queued.id, "topic": message.topic, "attempt": message.attempt},
        )

    async def close(self) -> None:
        """关闭生产者连接（应用关停路径）。"""
        await self._producer.stop()


def build_task_runner(
    settings: Settings,
    tasks: TaskService,
    handler: TaskHandler | None,
    *,
    producer: TaskProducer | None = None,
) -> TaskRunner:
    """按 ``TASK_RUNNER`` 选择投递方式。

    ``producer`` 允许注入：契约/单测要用一个「不落地的生产者」断言投递顺序与
    投递失败语义，而不是真的起一个 Kafka（与 ``docs/11`` §3 里 MCP 用替身
    替掉传输层是同一条纪律）。
    """
    mode = settings.task_runner
    if mode == "kafka":
        return KafkaTaskRunner(tasks, producer or build_task_producer(settings))
    if mode == "none" or handler is None:
        return NullTaskRunner()
    return InlineTaskRunner(tasks, handler, concurrency=settings.worker_concurrency)


def require_kafka_runner(runner: TaskRunner) -> KafkaTaskRunner:
    """断言投递器是 Kafka 版（启动期用）。

    写成一个函数而不是把 ``isinstance`` 散在各处：``TASK_RUNNER=kafka`` 却没装出
    Kafka 投递器 = 任务永远不会被执行，这种事必须在启动期就炸出来。
    """
    if not isinstance(runner, KafkaTaskRunner):
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "TASK_RUNNER=kafka 但装配出的不是 KafkaTaskRunner",
            {"runner": type(runner).__name__},
        )
    return runner


__all__ = [
    "InlineTaskRunner",
    "KafkaTaskRunner",
    "NullTaskRunner",
    "TaskHandler",
    "TaskRunner",
    "build_task_runner",
    "require_kafka_runner",
]
