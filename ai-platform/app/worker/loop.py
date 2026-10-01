"""任务 Worker 主循环（``docs/08`` §5.4，``REQ-TASK-006``）。

**它为什么必须是独立进程**：``docs/10`` §5.1 的首 token 延迟目标要求 API 进程
不被 CPU 密集的解析/向量化抢占；``docs/10`` §4 更明确要求「文件解析 MUST 在
独立进程（Worker）中执行，解析器崩溃不得影响 API 进程」。

一次消息处理的状态推进顺序（每一步都有对应的失败后果）：

1. 读任务行并发**幂等判断**（已终态 / 正在跑 / 重复消息 → 丢弃）；
2. 有取消标记 → 置 ``CANCELED`` 并丢弃（用户点过取消）；
3. ``FAILED`` 且还有预算 → ``requeue``（``docs/08`` §2 的 ``FAILED --> QUEUED``）；
4. ``PENDING`` → ``mark_queued``；``QUEUED`` → 直接执行；
5. 执行（整套状态流转由 ``TaskService.track`` 负责，含超时/取消/异常）；
6. 失败时决定**自动重试**（延迟重投，不阻塞 Worker）还是**进死信**；
7. **状态落库之后**才提交 offset（``docs/08`` §5.4）。

第 7 步是唯一容易被写反的一步，也是唯一「写反了看不出来」的一步：
先提交再落库，崩溃后任务消息就永远不会回来了（at-most-once），
而日志里什么都没有。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing
from app.tasks.dispatch import TaskDispatcher
from app.tasks.models import (
    TERMINAL_STATUSES,
    Task,
    TaskError,
    TaskStatus,
    TaskType,
)
from app.tasks.retry import RetryQueue, build_retry_queue, is_retryable, retry_delay
from app.tasks.service import TaskService
from app.tasks.transport import (
    KafkaTaskConsumer,
    TaskConsumer,
    TaskMessage,
    TaskProducer,
    build_task_producer,
)
from app.worker.offsets import OffsetTracker, Position

logger = logging.getLogger("app.worker")

#: 需要额外限流的任务类型（``docs/08`` §5.4：Embedding 类用单独的信号量，默认 1）
EMBEDDING_TYPES: frozenset[TaskType] = frozenset({TaskType.DOCUMENT_INGEST, TaskType.KB_REINDEX})

#: 一次 ``get`` 的等待上限：留出间隙跑「延迟重试队列」与「回收已完成任务」
POLL_TIMEOUT_MS = 500


@dataclass
class WorkerStats:
    """Worker 计数（启动/关停日志与测试断言用）。"""

    consumed: int = 0
    succeeded: int = 0
    failed: int = 0
    canceled: int = 0
    retried: int = 0
    dead_lettered: int = 0
    skipped: int = 0
    invalid: int = 0

    def as_dict(self) -> dict[str, int]:
        """给日志用的扁平字典。"""
        return {
            "consumed": self.consumed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "canceled": self.canceled,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "skipped": self.skipped,
            "invalid": self.invalid,
        }


@dataclass(slots=True)
class _Running:
    """在飞任务簿记。"""

    jobs: set[asyncio.Task[None]] = field(default_factory=set)
    task_ids: set[str] = field(default_factory=set)


class TaskWorker:
    """消费任务消息并执行（单进程内可并发多条，按分区保序提交位点）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        consumer: TaskConsumer,
        tasks: TaskService,
        dispatcher: TaskDispatcher,
        producer: TaskProducer,
        retries: RetryQueue,
        stats: WorkerStats | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._settings = settings
        self._consumer = consumer
        self._tasks = tasks
        self._dispatcher = dispatcher
        self._producer = producer
        self._retries = retries
        self._clock = clock
        self.stats = stats or WorkerStats()

        self._concurrency = max(1, settings.worker_concurrency)
        self._gate = asyncio.Semaphore(self._concurrency)
        self._embed_gate = asyncio.Semaphore(max(1, settings.worker_embed_concurrency))
        self._timeout = float(settings.task_timeout_seconds)
        self._grace = float(settings.task_shutdown_grace_seconds)
        self._retry_base = settings.task_retry_base_seconds
        self._retry_jitter = settings.task_retry_jitter
        self._tracker = OffsetTracker()
        self._running = _Running()
        #: 重试队列的轮询间隔。没有它会变成「每循环一次查一次 Redis」：
        #: 消息稀少时循环可以跑上千次/秒，把 Redis 打成瓶颈而任务吞吐毫无变化。
        self._retry_poll = max(0.0, float(settings.task_retry_poll_seconds))
        self._next_retry_poll = float("-inf")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        """消费直到 ``stop`` 被置位。"""
        await self._consumer.start()
        logger.info(
            "worker.started",
            extra={
                "group": self._settings.kafka_group_id,
                "concurrency": self._concurrency,
                "embed_concurrency": max(1, self._settings.worker_embed_concurrency),
                "types": self._dispatcher.types,
            },
        )
        try:
            while not stop.is_set():
                await self._poll_retries()
                await self._wait_any(block=False)
                if len(self._running.jobs) >= self._concurrency:
                    # 并发额度已满：等一个腾出位置再取新消息（不要空转拉消息）
                    await self._wait_any(block=True)
                    continue
                message = await self._consumer.get(timeout_ms=POLL_TIMEOUT_MS)
                if message is None:
                    # 让出控制权。消费者实现（尤其是替身与内存实现）可能在无消息时
                    # **不挂起**，那样这个循环就成了纯 CPU 忙等，把事件循环饿死
                    # ——连 ``stop`` 都没机会被置位。
                    await asyncio.sleep(0)
                    continue
                self._start(message)
        finally:
            await self._shutdown()

    # ------------------------------------------------------------------
    def _start(self, message: TaskMessage) -> None:
        """登记并启动一条消息的处理任务。

        刻意是**同步**方法：它只登记位点、建 task 就返回。写成 ``async def`` 会
        让调用方以为里面有 await（实际上一个都没有），那种「异步函数里全是同步代码」
        的写法很容易在后续修改中变成真正的串行卡点。
        """
        self._tracker.track(
            Position(topic=message.topic, partition=message.partition, offset=message.offset)
        )
        job = asyncio.create_task(self._run_job(message), name=f"worker:{message.task_id}")
        self._running.jobs.add(job)

    async def _run_job(self, message: TaskMessage) -> None:
        """单条消息的完整生命周期：执行 → 提交位点（状态落库**之后**）。"""
        try:
            await self._execute(message)
        except asyncio.CancelledError:
            # 关停路径：``_shutdown`` 先给每条在飞任务打了取消标记，所以这里落成
            # CANCELED 是**约定**（而不是猜测）。``_record_outcome`` 走不到，
            # 计数只能在这里补 —— 否则关停日志里 ``canceled=0``，
            # 与 ``worker.drained`` 报的条数对不上，排查时会被误导。
            self.stats.canceled += 1
            raise
        except Exception as exc:
            self.stats.failed += 1
            logger.error(
                "worker.message_failed",
                extra={"task_id": message.task_id, "error": str(exc)},
            )
        finally:
            self._running.jobs.discard(asyncio.current_task())
            self._running.task_ids.discard(message.task_id)
            await self._commit(message)

    async def _commit(self, message: TaskMessage) -> None:
        """提交可安全提交的位点（连续水位线，见 :mod:`app.worker.offsets`）。"""
        offsets = self._tracker.complete(
            Position(topic=message.topic, partition=message.partition, offset=message.offset)
        )
        if offsets:
            await self._consumer.commit(offsets)

    async def _wait_any(self, *, block: bool) -> None:
        """回收已结束的处理任务（``block=False`` 时只做非阻塞回收）。"""
        jobs = {job for job in self._running.jobs if job.done()}
        if not jobs and block and self._running.jobs:
            done, _ = await asyncio.wait(self._running.jobs, return_when=asyncio.FIRST_COMPLETED)
            jobs = set(done)
        for job in jobs:
            self._running.jobs.discard(job)
            if job.cancelled():
                continue
            exc = job.exception()
            if exc is not None:  # pragma: no cover - 由 _run_job 兜底
                logger.error("worker.job_error", extra={"error": str(exc)})

    # ------------------------------------------------------------------
    # 单条消息
    # ------------------------------------------------------------------
    async def _execute(self, message: TaskMessage) -> None:
        """处理一条消息（幂等：重复消息在这里被丢弃）。"""
        self.stats.consumed += 1
        try:
            task = await self._tasks.get(message.task_id)
        except AppError:
            # 任务行已被清理（或消息比任务还老）：丢弃，不能因此卡住分区
            self.stats.skipped += 1
            logger.warning(
                "worker.task_missing",
                extra={"task_id": message.task_id, "topic": message.topic},
            )
            return

        if task.status in TERMINAL_STATUSES:
            # ``docs/08`` §5.3：已 SUCCEEDED/CANCELED → 直接 ack 丢弃
            self.stats.skipped += 1
            logger.info(
                "worker.task_already_finished",
                extra={"task_id": task.id, "status": str(task.status)},
            )
            return
        if task.status is TaskStatus.RUNNING or task.id in self._running.task_ids:
            # 重复投递（Kafka at-least-once）或另一个 Worker 正在跑：
            # 强行再跑一次会让两次执行互相覆盖切片，而且第二次 ``mark_running``
            # 必然撞上状态机冲突。直接丢弃。
            self.stats.skipped += 1
            logger.info("worker.task_already_running", extra={"task_id": task.id})
            return

        if await self._tasks.is_cancel_requested(task.id):
            self.stats.skipped += 1
            await self._tasks.cancel(task.id, task.user_id)
            return

        ready = await self._advance(task)
        if ready is None:
            return
        task = ready

        self._running.task_ids.add(task.id)
        started = self._clock()
        try:
            await self._run_handler(task)
            current = await self._tasks.get(task.id)
            self._record_outcome(current, seconds=max(0.0, self._clock() - started))
            if current.status is TaskStatus.FAILED:
                await self._handle_failure(message, current)
        finally:
            self._running.task_ids.discard(task.id)

    async def _advance(self, task: Task) -> Task | None:
        """把任务推进到可执行状态；返回 ``None`` 表示这条消息应当丢弃。"""
        if task.status is TaskStatus.FAILED:
            if not self._can_retry(task):
                # 重试预算已用尽：消息是重投晚到的（已被判死信），丢弃即可
                self.stats.skipped += 1
                logger.info("worker.retry_not_allowed", extra={"task_id": task.id})
                return None
            # 自动重试的消息：把状态推回 QUEUED（``docs/08`` §2 的 FAILED --> QUEUED）
            return await self._tasks.requeue(task.id)
        if task.status is TaskStatus.PENDING:
            # 正常情况下投递时已是 QUEUED；补偿重投可能带来 PENDING 的消息
            return await self._tasks.mark_queued(task.id)
        return task

    async def _run_handler(self, task: Task) -> None:
        """执行处理器（带整体超时与 Embedding 额外限流）。"""
        span_attrs = {"task_id": task.id, "type": str(task.type), "stage": str(task.stage or "")}
        with get_tracing().span("task.run", span_attrs):
            if task.type in EMBEDDING_TYPES:
                async with self._embed_gate:
                    await self._invoke(task)
            else:
                await self._invoke(task)

    async def _invoke(self, task: Task) -> None:
        """``dispatcher.handle`` + 超时控制。

        **超时不能用 ``wait_for``**：``wait_for`` 超时会取消那个协程，取消信号穿过
        ``TaskService.track`` 的 ``CancelledError`` 分支，把「跑太久」记成
        ``CANCELED`` —— 用户看到一个「被取消」的任务，而其实谁都没取消它。
        所以这里先**主动落 ``FAILED``**，再取消协程；``track`` 在取消时会先看
        取消标记，看到「没人请求过取消」就保留 ``FAILED``（见
        ``TaskService._cancel_or_keep``）。
        """
        job = asyncio.ensure_future(self._dispatcher.handle(task))
        try:
            done, _ = await asyncio.wait({job}, timeout=self._timeout)
        except asyncio.CancelledError:
            # 关停路径：``wait`` 被取消时**不会**取消它正在等的任务。不显式取消就会
            # 留下一个还在跑 Embedding 的孤儿任务（进程退出前一直在吃 CPU），
            # 而且 ``track`` 收不到取消信号 —— 任务会永远停在 ``RUNNING``。
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            raise
        if job in done:
            exc = job.exception()
            if exc is not None:
                # ``track`` 已把任务落成 FAILED / 记录了错误码，这里只补一条日志
                logger.warning(
                    "worker.handler_failed",
                    extra={"task_id": task.id, "error": str(exc)},
                )
            return
        logger.error(
            "worker.task_timeout",
            extra={"task_id": task.id, "timeout": self._timeout},
        )
        try:
            await self._tasks.fail(
                task.id,
                TaskError(
                    code=str(ErrorCode.TASK_TIMEOUT),
                    message=f"任务执行超过 {self._timeout:.0f}s",
                ),
            )
        except AppError as exc:  # pragma: no cover - 并发下的状态冲突
            logger.warning("worker.timeout_mark_failed_failed", extra={"error": str(exc)})
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    def _record_outcome(self, task: Task, *, seconds: float) -> None:
        """记录指标与统计（只记终态/失败，避免把 RUNNING 记成一次结束）。"""
        status = str(task.status)
        if task.status is TaskStatus.SUCCEEDED:
            self.stats.succeeded += 1
        elif task.status is TaskStatus.CANCELED:
            self.stats.canceled += 1
        elif task.status is TaskStatus.FAILED:
            self.stats.failed += 1
        else:
            # 处理器返回后仍是中间态：不该发生，但也绝不能静默 ——
            # 它意味着「任务永远停在那里」，必须留下线索。
            logger.error(
                "worker.task_not_finalized",
                extra={"task_id": task.id, "status": status},
            )
            return
        get_metrics().record_task(task_type=str(task.type), status=status, seconds=seconds)

    async def _handle_failure(self, message: TaskMessage, task: Task) -> None:
        """失败收尾：延迟自动重试，或进死信（``docs/08`` §5.2）。"""
        error = task.error
        code = error.code if error is not None else str(ErrorCode.INTERNAL_ERROR)
        detail = error.message if error is not None else ""
        if self._can_retry(task) and is_retryable(code):
            attempt = task.retry_count + 1
            delay = retry_delay(attempt, base=self._retry_base, jitter=self._retry_jitter)
            await self._retries.schedule(task.id, attempt=attempt, delay=delay)
            self.stats.retried += 1
            logger.info(
                "worker.retry_scheduled",
                extra={
                    "task_id": task.id,
                    "attempt": attempt,
                    "delay": round(delay, 3),
                    "code": code,
                },
            )
            return
        # 重试耗尽或不可重试：进死信供人工排查（docs/08 §5.2）
        self.stats.dead_lettered += 1
        logger.warning(
            "worker.dead_letter",
            extra={
                "task_id": task.id,
                "code": code,
                "retry_count": task.retry_count,
                "max_retries": task.max_retries,
            },
        )
        await self._producer.dead_letter(message, code=code, detail=detail)

    @staticmethod
    def _can_retry(task: Task) -> bool:
        """还有自动重试预算吗（``docs/08`` §5.2）。"""
        return task.retry_count < task.max_retries

    # ------------------------------------------------------------------
    # 延迟重试队列
    # ------------------------------------------------------------------
    async def _poll_retries(self) -> None:
        """把到期的重试重新投递出去（``docs/08`` §5.2）。

        **只投递、不改状态**：真正把 ``FAILED → QUEUED`` 的是收到消息的 Worker
        （见 :meth:`_advance`）。这样「投递失败」的后果是「任务仍是 FAILED」——
        用户可以手动重试，而不是卡在一个谁也看不见的中间态里。
        """
        now = self._clock()
        if now < self._next_retry_poll:
            return
        self._next_retry_poll = now + self._retry_poll
        try:
            due = await self._retries.claim_due()
        except Exception as exc:
            logger.warning("worker.retry_poll_failed", extra={"error": str(exc)})
            return
        for entry in due:
            try:
                task = await self._tasks.get(entry.task_id)
            except AppError:
                continue
            if task.status is not TaskStatus.FAILED:
                # 已被人工重试 / 取消：重试记录过期作废
                continue
            try:
                await self._producer.send(TaskMessage.from_task(task))
            except AppError as exc:
                logger.error(
                    "worker.retry_publish_failed",
                    extra={"task_id": task.id, "error": str(exc)},
                )

    # ------------------------------------------------------------------
    # 关停
    # ------------------------------------------------------------------
    async def _shutdown(self) -> None:
        """优雅退出（``docs/08`` §5.4）。

        顺序：① 给在飞任务打取消标记（它们在**下一个检查点**自己退出，而不是被
        硬杀）；② 等待至多 ``grace`` 秒；③ 仍未结束的强制取消（``track`` 会落
        ``CANCELED``）；④ 提交剩余位点。
        """
        in_flight = sorted(self._running.task_ids)
        if in_flight:
            logger.info("worker.draining", extra={"in_flight": in_flight, "grace": self._grace})
            for task_id in in_flight:
                try:
                    task = await self._tasks.get(task_id)
                    await self._tasks.cancel(task_id, task.user_id)
                except AppError as exc:
                    logger.debug(
                        "worker.cancel_request_failed",
                        extra={"task_id": task_id, "error": str(exc)},
                    )
            done, pending = await asyncio.wait(self._running.jobs, timeout=self._grace)
            for job in pending:
                job.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            logger.info(
                "worker.drained",
                extra={"finished": len(done), "canceled": len(pending)},
            )

        try:
            positions = self._tracker.commit_positions()
            if positions:
                await self._consumer.commit(positions)
        except Exception as exc:  # pragma: no cover - 关停路径
            logger.warning("worker.final_commit_failed", extra={"error": str(exc)})
        await self._consumer.stop()
        await self._retries.close()
        logger.info("worker.stopped", extra=self.stats.as_dict())


def build_worker(
    settings: Settings,
    *,
    tasks: TaskService,
    dispatcher: TaskDispatcher,
    consumer: TaskConsumer | None = None,
    producer: TaskProducer | None = None,
    retries: RetryQueue | None = None,
) -> TaskWorker:
    """装配 Worker。

    三个外部依赖都可注入：单测用一个「假 Broker」（``tests/support/fake_broker.py``）
    驱动整个消费循环，从而覆盖「幂等丢弃 / 自动重试 / 死信 / 取消 / 优雅退出」——
    这些行为靠人工起一个 Kafka 去验证是不可维护的（与 ``docs/11`` §3 用替身替掉
    MCP 传输层是同一条纪律）。
    """
    return TaskWorker(
        settings,
        consumer=consumer or KafkaTaskConsumer(settings),
        tasks=tasks,
        dispatcher=dispatcher,
        producer=producer or build_task_producer(settings),
        retries=retries or build_retry_queue(settings),
    )


__all__ = [
    "EMBEDDING_TYPES",
    "POLL_TIMEOUT_MS",
    "TaskWorker",
    "WorkerStats",
    "build_worker",
]
