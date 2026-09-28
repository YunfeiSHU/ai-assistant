"""投递补偿扫描（``docs/08`` §5.1）。

**它防的是哪一种故障**：``PENDING → QUEUED`` 的语义是「消息确认落地之后才改状态」，
这个顺序保证了「不会有一条消息指向不存在的任务」。但它留下一个反向缺口 ——
**消息没能发出去**（Kafka 抖动、Worker 未部署、投递抛异常）时，任务会**永远停在
``PENDING``**：没有任何东西会再碰它，前端一直转圈，而状态机认为这是合法状态。

所以要有一个周期性扫描把「创建于 ``grace_seconds`` 之前、仍是 ``PENDING``」的任务
重新投一遍。两条终止条件缺一不可：

* **重投次数**（``max_attempts``）：快速止血，避免每 30s 空转；
* **年龄上限**（``pending_max_age``）：计数器在进程内存里，重启会清零 ——
  只有「按创建时间算」的这条是**持久**的，它保证「补偿重投一直失败 + 反复重启」
  也不会让任务无限期挂着。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.tasks.models import Task, TaskError
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService

logger = logging.getLogger("app.tasks.compensation")

#: 单次扫描最多处理多少条（避免积压时一次投递上千条把 Kafka 打挂）
SCAN_BATCH = 50


def _parse(value: str) -> datetime | None:
    """RFC3339 → 带时区 ``datetime``；解析失败返回 ``None``（= 「年龄未知」）。

    刻意**不**把坏时间戳当作「很旧」：那会直接把它判成 ``pending_too_long`` 而丢掉
    一次本可以成功的重投（放弃是不可逆的）。未知年龄的任务只受重投次数封顶，
    这仍然是有限的。
    """
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


class TaskCompensator:
    """周期性地把滞留的 ``PENDING`` 任务重新投递出去。"""

    def __init__(
        self,
        settings: Settings,
        tasks: TaskService,
        runner: TaskRunner,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._tasks = tasks
        self._runner = runner
        self._grace = settings.task_compensation_grace_seconds
        self._max_attempts = settings.task_compensation_max_attempts
        self._max_age = settings.task_pending_max_age_seconds
        self._interval = settings.task_compensation_interval_seconds
        self._now = now or (lambda: datetime.now(UTC))
        #: 进程内的重投计数（见模块 docstring：它不是唯一防线）
        self._attempts: dict[str, int] = {}

    @property
    def attempts(self) -> dict[str, int]:
        """当前重投计数快照（测试用）。"""
        return dict(self._attempts)

    async def scan_once(self) -> int:
        """扫一轮；返回**发起重投**的任务数。

        返回计数而不是 ``None``：日志与测试都需要区分「扫了但没东西可做」与
        「扫到了但投不出去」—— 后者是 Kafka 故障的信号。
        """
        before = (
            (self._now() - timedelta(seconds=self._grace))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        stale = await self._tasks.stale_pending(before=before, limit=SCAN_BATCH)
        republished = 0
        for task in stale:
            if await self._handle(task):
                republished += 1
        if republished:
            logger.info("task.compensated", extra={"count": republished, "scanned": len(stale)})
        return republished

    async def _handle(self, task: Task) -> bool:
        """处理一条滞留任务；返回是否真的发起了重投。"""
        created = _parse(task.created_at)
        if created is not None:
            age = (self._now() - created).total_seconds()
            if age > self._max_age:
                await self._give_up(
                    task,
                    reason="pending_too_long",
                    detail=f"任务创建后 {int(age)}s 仍未投递成功",
                )
                return False
        attempts = self._attempts.get(task.id, 0)
        if attempts >= self._max_attempts:
            await self._give_up(
                task,
                reason="republish_exhausted",
                detail=f"补偿重投 {attempts} 次仍未成功",
            )
            return False

        self._attempts[task.id] = attempts + 1
        try:
            await self._runner.submit(task)
        except AppError as exc:
            # 投递失败：**保持 PENDING**，下一轮再试（这里绝不能改状态，
            # 否则「一次 Kafka 抖动」会直接毁掉这条任务）
            logger.warning(
                "task.compensation_failed",
                extra={"task_id": task.id, "attempt": attempts + 1, "error": str(exc)},
            )
            return False
        self._attempts.pop(task.id, None)
        return True

    async def _give_up(self, task: Task, *, reason: str, detail: str) -> None:
        """放弃并落 ``FAILED`` + ``MQ_UNAVAILABLE``（``docs/08`` §5.1）。"""
        self._attempts.pop(task.id, None)
        logger.error(
            "task.compensation_gave_up",
            extra={"task_id": task.id, "reason": reason, "detail": detail},
        )
        try:
            await self._tasks.fail(
                task.id,
                TaskError(
                    code=str(ErrorCode.MQ_UNAVAILABLE), message=detail, detail={"reason": reason}
                ),
            )
        except AppError as exc:
            # 状态被别人改过（例如用户已取消）：不算错误，日志里能看见即可
            logger.warning(
                "task.compensation_transition_rejected",
                extra={"task_id": task.id, "error": str(exc)},
            )

    async def run_forever(self, stop: asyncio.Event) -> None:
        """循环扫描直到 ``stop`` 被置位（应用关停路径）。"""
        logger.info(
            "task.compensator_started",
            extra={"interval": self._interval, "grace": self._grace},
        )
        while not stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:  # 单轮失败不该终止扫描循环
                logger.warning("task.compensation_error", extra={"error": str(exc)})
            try:
                # ``wait_for`` 在这里是安全的：取消的是 ``Event.wait()``，
                # 不像 ``anext(生成器)`` 那样会把状态留给被取消的一方。
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
        logger.info("task.compensator_stopped")


__all__ = ["SCAN_BATCH", "TaskCompensator"]
