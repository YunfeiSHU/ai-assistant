"""任务用例服务（``REQ-TASK-001`` / ``REQ-TASK-003`` / ``REQ-TASK-004``）。

职责：把「谁能改任务状态、怎么改」收口到一处。业务代码（入库、删除、摘要）
只调用这里的方法，不直接碰 :class:`~app.tasks.store.TaskStore`。

乐观锁冲突的处理口径：**读-改-重试**。冲突说明有并发的状态推进（例如 Worker
正在上报进度、用户同时点了取消），重试即可收敛；直接把冲突抛给调用方会把
「并发上报进度」变成用户可见的 409。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.tasks.events import Publisher, TaskEvent
from app.tasks.models import (
    ResourceType,
    Task,
    TaskError,
    TaskStatus,
    TaskType,
    ensure_transition,
    now_iso,
)
from app.tasks.retry import error_retryable
from app.tasks.store import TaskConflict, TaskStore, encode_task_cursor

logger = logging.getLogger("app.tasks")

#: 乐观锁冲突的最大重试次数
MAX_UPDATE_RETRIES = 5


def make_idem_key(*parts: object) -> str:
    """生成幂等键（sha256）。

    ``docs/09`` §2.4 的 ``idem_key`` 是 ``CHAR(64)`` 且带唯一索引，所以固定产出
    64 位 hex；调用方只需给出「能唯一刻画这次意图」的字段组合。
    """
    raw = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _stamp_queued(task: Task) -> None:
    task.queued_at = now_iso()


def _stamp_started(task: Task) -> None:
    task.started_at = now_iso()


def _stamp_finished(task: Task) -> None:
    task.finished_at = now_iso()


def _stamp_progress_complete(task: Task) -> None:
    task.progress = 100
    task.finished_at = now_iso()


class TaskService:
    """任务创建、推进、取消与重试。"""

    def __init__(
        self,
        store: TaskStore,
        *,
        max_retries: int = 3,
        events: Publisher | None = None,
        queue_max: int | None = None,
    ) -> None:
        self._store = store
        self._max_retries = max_retries
        #: 状态变更后的广播（``GET /tasks/{id}/events`` 的事件源）。
        #: 刻意放在 service 而不是 Worker 里：状态变更的**唯一入口**就在这里，
        #: 放在 Worker 会让 ``inline`` 执行器与「接口直接取消任务」两条路径都静默不推事件。
        self._events = events
        #: 未结束任务的条数上限（``INGEST_QUEUE_MAX``）；``None`` = 不做过载检查。
        #: 放在 service 而非各业务路由：``docs/08`` §174 说的是「新**任务**创建」
        #: 返回 503，六个任务类型都得走这里，散在各个路由注定会漏。
        self._queue_max = queue_max

    async def _publish(self, task_id: str, event: TaskEvent) -> None:
        """广播一次状态变更。

        这里**不吞异常**：吞异常的策略在 :func:`app.tasks.events.make_publisher`
        （生产里唯一的发布者，失败只记日志）。两处都写 try/except 会让「总线连不上」
        这类真问题被静默两遍，排查时连一条日志都对不上号。
        """
        if self._events is None:
            return
        await self._events(task_id, event)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def get(self, task_id: str, user_id: str | None = None) -> Task:
        """取任务详情。"""
        return await self._store.get(task_id, user_id)

    async def list_tasks(
        self,
        *,
        user_id: str,
        status: Sequence[TaskStatus] = (),
        type_: TaskType | None = None,
        resource_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Task], str | None]:
        """分页列表；返回 ``(items, next_cursor)``。"""
        items, has_more = await self._store.list(
            user_id=user_id,
            status=status,
            type_=type_,
            resource_id=resource_id,
            limit=limit,
            cursor=cursor,
        )
        next_cursor = encode_task_cursor(items[-1]) if has_more and items else None
        return items, next_cursor

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------
    async def create(
        self,
        *,
        type_: TaskType,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        payload: dict[str, Any] | None = None,
        idem_key: str | None = None,
        max_retries: int | None = None,
    ) -> tuple[Task, bool]:
        """创建任务（``PENDING``）。

        Returns:
            ``(task, created)``；``created=False`` 表示命中幂等键、返回了既有任务。

        Raises:
            AppError: 未结束任务数超过 ``INGEST_QUEUE_MAX`` 时抛 ``503 OVERLOADED``。
        """
        await self._ensure_capacity()
        task = Task(
            id=new_id("task"),
            type=type_,
            status=TaskStatus.PENDING,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload or {},
            idem_key=idem_key or make_idem_key(type_, user_id, resource_id),
            max_retries=self._max_retries if max_retries is None else max_retries,
        )
        stored = await self._store.create(task)
        created = stored.id == task.id
        logger.info(
            # 键名刻意避开 ``created``：``LogRecord`` 自带 ``created``（记录时间戳），
            # 用同名键会抛 ``KeyError: Attempt to overwrite 'created' in LogRecord``，
            # 而它只在日志级别允许输出时才触发 —— 于是「测试里好好的、生产上建任务即 500」。
            "task.created",
            extra={"task_id": stored.id, "type": str(stored.type), "is_new": created},
        )
        return stored, created

    async def _ensure_capacity(self) -> None:
        """队列过载就拒绝新建（``docs/08`` §174 / ``docs/10`` §3.3）。

        检查在 ``store.create`` **之前**：饱和时连幂等重放也会拿到 503。这是刻意的
        ——503 本身可重试，客户端退避后再来就能拿到那条既有任务；而为了区分「重放」
        去给端点加一个 ``get_by_idem`` 查询，代价大于收益（``docs/12`` 已记录取舍）。

        **边界取「已达上限」而不是「超过上限」**：统计的是**本次入队之前**的未结束
        任务数，所以 ``open_count == queue_max`` 意味着「加上这一个正好越界」，此
        时就该拒绝。写成 ``>`` 会让实际并发数变成 ``queue_max + 1`` —— 配置项叫
        ``MAX``，差一的话没人能一眼看出真正的上限。
        """
        if self._queue_max is None:
            return
        open_count = await self._store.count_open()
        if open_count >= self._queue_max:
            logger.warning(
                "task.overloaded", extra={"open": open_count, "queue_max": self._queue_max}
            )
            raise AppError(
                ErrorCode.OVERLOADED,
                "任务队列已满，请稍后重试",
                {"open": open_count, "queue_max": self._queue_max},
            )

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------
    async def mark_queued(self, task_id: str) -> Task:
        """``PENDING → QUEUED``；MUST 在投递 MQ 成功之后调用（``docs/08`` §2 不变式）。"""
        return await self._transition(task_id, TaskStatus.QUEUED, _stamp_queued)

    async def mark_running(self, task_id: str) -> Task:
        """``QUEUED → RUNNING``。"""
        return await self._transition(task_id, TaskStatus.RUNNING, _stamp_started)

    async def report_progress(
        self,
        task_id: str,
        *,
        stage: str,
        progress: int,
        chunks_done: int | None = None,
        chunks_total: int | None = None,
    ) -> Task:
        """上报阶段与进度（``REQ-TASK-002``）。进度只允许单调不减。

        ``chunks_done`` / ``chunks_total``（``docs/10`` UP-02）是可选的**绝对口径**：
        百分比只能画进度条，只有计数能回答「还要多久」，也才能让客户端自己发现
        「总数比最终入库数大 ⇒ 被截断了」。

        三条不变量与 ``progress`` 同源，回退一律报错：

        * ``progress`` 单调不减；
        * ``chunks_done`` 单调不减；
        * ``chunks_total`` 一旦定下就不再变化（由切分阶段一次算定）。
          允许它漂移会让客户端算出的 ETA 在批与批之间突然跳变，而跳变的原因
          （重算？截断？）从响应里完全看不出来。
        """
        bounded = max(0, min(100, int(progress)))
        total = None if chunks_total is None else max(0, int(chunks_total))
        done = None if chunks_done is None else max(0, int(chunks_done))

        def mutate(task: Task) -> None:
            if bounded < task.progress:
                # 允许回退会掩盖「阶段乱序执行」这类真问题，也会让前端进度条倒退
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"进度不得回退（{task.progress} → {bounded}）",
                )
            if total is not None:
                if task.chunks_total and task.chunks_total != total:
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"切片总数不得变更（{task.chunks_total} → {total}）",
                    )
                task.chunks_total = total
            if done is not None:
                if done < task.chunks_done:
                    raise AppError(
                        ErrorCode.INVALID_ARGUMENT,
                        f"切片进度不得回退（{task.chunks_done} → {done}）",
                    )
                task.chunks_done = done
            task.stage = stage
            task.progress = bounded

        task = await self._store_update(task_id, mutate)
        await self._publish(
            task_id,
            TaskEvent.progress(
                stage=task.stage,
                progress=task.progress,
                # 0 表示「还不知道 / 不适用」而不是「 0 片」：不转成 None 的话，
                # 摘要/记忆抽取这类没有切片概念的任务，其进度帧会多出
                # `chunks_total: 0`，把「无此概念」渲染成「一片都没有」。
                chunks_done=task.chunks_done or None,
                chunks_total=task.chunks_total or None,
            ),
        )
        return task

    async def succeed(self, task_id: str) -> Task:
        """``RUNNING → SUCCEEDED``。"""
        task = await self._transition(task_id, TaskStatus.SUCCEEDED, _stamp_progress_complete)
        await self._publish(
            task_id, TaskEvent.done(status=str(task.status), finished_at=task.finished_at)
        )
        return task

    async def fail(self, task_id: str, error: TaskError) -> Task:
        """``RUNNING → FAILED``（后续能否自动重试由 ``retry`` 决定）。"""

        def mutate(task: Task) -> None:
            task.error = error
            task.finished_at = now_iso()

        task = await self._transition(task_id, TaskStatus.FAILED, mutate)
        # 失败只发 ``error``、**不发 done**：``docs/02`` §6.3 规定 ``error`` 与 ``done``
        # 互斥。对任务而言 ``FAILED`` 还不是终点（可能自动重试回 ``QUEUED``），
        # 所以流也不关 —— 客户端看到 ``error`` 后继续等，可能等来新的 ``progress``。
        await self._publish(
            task_id,
            TaskEvent.error(
                code=error.code, message=error.message, retryable=error_retryable(task)
            ),
        )
        return task

    async def cancel(self, task_id: str, user_id: str) -> Task:
        """取消任务（``docs/08`` §4.3）。

        * ``PENDING`` / ``QUEUED`` → 直接 ``CANCELED``；
        * ``RUNNING`` → 置取消标记，由 Worker 在下一个检查点退出（**不会**立刻改状态）；
        * 终态 → ``409 TASK_NOT_CANCELABLE``；重复调用幂等返回当前状态。
        """
        task = await self._store.get(task_id, user_id)
        if task.status is TaskStatus.CANCELED:
            return task
        if not task.can_cancel:
            raise AppError(
                ErrorCode.TASK_NOT_CANCELABLE,
                f"任务处于 {task.status}，不可取消",
                {"status": str(task.status)},
            )
        await self._store.request_cancel(task_id)
        if task.status is TaskStatus.RUNNING:
            # 单批 Embedding 不可中断，最多等一批完成
            return task
        canceled = await self._transition(task_id, TaskStatus.CANCELED, _stamp_finished)
        await self._publish(
            task_id,
            TaskEvent.done(status=str(canceled.status), finished_at=canceled.finished_at),
        )
        return canceled

    async def retry(self, task_id: str, user_id: str) -> tuple[Task, bool]:
        """重试失败任务（``FAILED → QUEUED``，``docs/08`` §4.4）。

        Returns:
            ``(任务, 是否重新排队)``。第二个值是给调用方判断**要不要投递**的：
            ``docs/08`` §4.4 要求「置 QUEUED 并投递」，同时要求 ``QUEUED/RUNNING``
            下的重复调用「不重复投递」。只返回任务的话，路由层无法区分
            「这次真的重置了」和「本来就排着队」，要么漏投（任务永远卡 QUEUED，
            而且不报错），要么重复投（同一个文档被并行入库两次）。
        """
        task = await self._store.get(task_id, user_id)
        if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            return task, False  # 幂等：不重复投递
        if not task.can_retry:
            raise AppError(
                ErrorCode.TASK_NOT_RETRYABLE,
                "任务不可重试",
                {"status": str(task.status), "retry_count": task.retry_count},
            )

        def mutate(current: Task) -> None:
            ensure_transition(current, TaskStatus.QUEUED)
            current.status = TaskStatus.QUEUED
            current.retry_count += 1
            current.progress = 0
            current.stage = None
            # 切片计数与 progress 同样必须归零：本轮会从解析开始重跑，
            # 留着上一轮的总数会让新一次切分的 ``chunks_total`` 与它不等并直接被拒。
            current.chunks_total = 0
            current.chunks_done = 0
            current.error = None
            current.finished_at = None
            # ``started_at`` 必须一起清：它是**本次**尝试的开始时间，留着上一轮的
            # 会让「耗时 = finished - started」算出跨轮次的怪数字，也会让
            # 「这个任务跑了多久」这类排障问题得到错误答案。
            current.started_at = None
            current.queued_at = now_iso()

        task = await self._store_update(task_id, mutate)
        # 进度归零要让订阅者看得见，否则前端进度条会停在上一轮的位置
        await self._publish(task_id, TaskEvent.progress(stage=None, progress=0))
        return task, True

    async def is_cancel_requested(self, task_id: str) -> bool:
        """Worker 检查点：是否有取消请求。"""
        return await self._store.is_cancel_requested(task_id)

    async def requeue(self, task_id: str) -> Task:
        """自动重试：``FAILED → QUEUED``（``docs/08`` §2 的 ``FAILED --> QUEUED``）。

        与用户可见的 :meth:`retry` 的区别：

        * 不做 ``409 TASK_NOT_RETRYABLE`` 的语义判断 —— 重试预算由 Worker 判
          （它才是那个知道「这是第几次尝试」的人）；非法迁移仍会被状态机拦住；
        * **不投递**：调用方（Worker）自己安排延迟重投（``retry:zset``）。
          在这里投递会把「延迟退避」变成「立即重试」，与 ``docs/08`` §5.2 的
          ``1s/4s/16s`` 直接冲突。
        """

        def mutate(current: Task) -> None:
            ensure_transition(current, TaskStatus.QUEUED)
            current.status = TaskStatus.QUEUED
            current.retry_count += 1
            # 进度必须归零：``report_progress`` 不允许回退，而重试会从第一阶段重跑。
            # 不归零的后果是「第二次尝试的 progress=10 被拒」，任务卡在上一轮的最大值。
            current.progress = 0
            current.stage = None
            # 同 progress：切片计数不归零会让新一轮的 ``chunks_total`` 被「总数不得变更」拦住
            current.chunks_total = 0
            current.chunks_done = 0
            current.error = None
            current.finished_at = None
            current.started_at = None
            current.queued_at = now_iso()

        task = await self._store_update(task_id, mutate)
        await self._publish(task_id, TaskEvent.progress(stage=None, progress=0))
        return task

    async def stale_pending(self, *, before: str, limit: int = 50) -> list[Task]:
        """列出滞留的 ``PENDING`` 任务（补偿扫描用，``docs/08`` §5.1）。"""
        return await self._store.list_stale_pending(before=before, limit=limit)

    async def count_open(self) -> int:
        """在飞任务数（过载判断用，``docs/08`` §5.4）。"""
        return await self._store.count_open()

    # ------------------------------------------------------------------
    @asynccontextmanager
    async def track(self, task_id: str) -> AsyncIterator[None]:
        """把「领取 → 执行 → 成功/失败」的状态流转打包。

        ``RUNNING`` 之后必须落到 ``SUCCEEDED`` / ``FAILED`` / ``CANCELED`` 之一，
        否则任务永远卡在 RUNNING（前端就一直转圈）。用上下文管理器统一收尾，
        而不是指望每个执行函数都记得写 try/finally。
        """
        await self.mark_running(task_id)
        try:
            yield
        except asyncio.CancelledError:
            await self._cancel_or_keep(task_id)
            raise
        except AppError as exc:
            await self.fail(
                task_id, TaskError(code=str(exc.code), message=exc.message, detail=exc.details)
            )
            raise
        except Exception as exc:
            await self.fail(
                task_id, TaskError(code=str(ErrorCode.INTERNAL_ERROR), message=str(exc))
            )
            raise
        else:
            await self.succeed(task_id)

    async def _cancel_or_keep(self, task_id: str) -> None:
        """取消收尾：只在「确实是取消」时落 ``CANCELED``。

        **不能无条件落 ``CANCELED``**：``FAILED`` 也能迁到 ``CANCELED``，
        于是「Worker 超时把它判失败（``TASK_TIMEOUT``）→ 再取消执行协程」
        会把刚写好的失败覆盖成取消 —— 用户看到一个自己从没取消过的「已取消」
        任务，而 ``error`` 里写着「任务执行超时」。

        判据（按顺序）：

        1. 存储里有取消标记 → 确实有人请求过取消（``docs/08`` §4.3）→ 落 ``CANCELED``；
        2. 没标记但状态仍是 ``RUNNING`` → 没人负责收尾（例如被外部硬取消）→
           仍然落 ``CANCELED``，否则任务永远停在 ``RUNNING`` 转圈；
        3. 其他情况（已被超时判失败等）→ **保留**现有状态。
        """
        try:
            if not await self._store.is_cancel_requested(task_id):
                current = await self._store.get(task_id)
                if current.status is not TaskStatus.RUNNING:
                    return
        except AppError:
            # 任务行已被清理：没什么可收尾的
            return
        await self._safe_transition(task_id, TaskStatus.CANCELED)

    async def _safe_transition(self, task_id: str, target: TaskStatus) -> None:
        """尽力而为的状态迁移（收尾路径用，不能再抛异常盖掉原始错误）。"""
        try:
            await self._transition(task_id, target, _stamp_finished)
        except Exception as exc:
            logger.warning(
                "task.transition_failed",
                extra={"task_id": task_id, "target": str(target), "error": str(exc)},
            )

    # ------------------------------------------------------------------
    async def _transition(
        self, task_id: str, target: TaskStatus, mutate: Callable[[Task], None] | None = None
    ) -> Task:
        def apply(task: Task) -> None:
            ensure_transition(task, target)
            task.status = target
            if mutate is not None:
                mutate(task)

        return await self._store_update(task_id, apply)

    async def _store_update(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        """带重试的乐观锁更新。"""
        last_conflict: Exception | None = None
        for _ in range(MAX_UPDATE_RETRIES):
            try:
                return await self._store.update(task_id, mutate)
            except TaskConflict as exc:  # pragma: no cover - 内存实现下窗口极窄
                last_conflict = exc
                await asyncio.sleep(0)
        raise AppError(
            ErrorCode.CONFLICT, "任务状态并发冲突，请重试", {"task_id": task_id}
        ) from last_conflict


__all__ = ["TaskService", "make_idem_key"]
