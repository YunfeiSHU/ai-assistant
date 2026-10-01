"""任务仓储（``REQ-TASK-001`` / ``REQ-TASK-003``）。

**为什么用「乐观锁 + 变更函数」而不是一堆 ``update_status`` 方法**：
``docs/08`` §2 要求状态变更必须带 ``WHERE status = :expected AND version = :v``。
把它写成通用原语（``update(task_id, mutate)`` 内部做版本比对与重试），
每个业务动作就只需描述「改什么」，不必各自重写一遍并发控制 —— 重写就一定会漏。
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import (
    cursor_position,
    decode_cursor,
    encode_cursor,
    is_after_cursor,
)
from app.tasks.models import ACTIVE_STATUSES, Task, TaskStatus, TaskType, now_iso

logger = logging.getLogger("app.tasks")

#: 任务不存在的统一文案（内存/Redis 两种实现必须逐字相同：
#: 它会出现在错误响应里，也会被按 404 判定的调用方当作断言依据）
TASK_NOT_FOUND_MESSAGE = "任务不存在"

#: 任务列表的类型别名。**不能**在类体内直接写 ``list[Task]``：仓储协议里有一个
#: 名为 ``list`` 的方法，类体内那个名字会遮蔽内置类型，my.py 会把注解解析成
#: 「方法不能当类型用」并报错（运行时无影响，但类型检查就废了）。
_TaskList = builtins.list[Task]


class TaskConflict(RuntimeError):
    """乐观锁冲突：调用方应重新读取任务再试。"""


@runtime_checkable
class TaskStore(Protocol):
    """任务存储。"""

    async def create(self, task: Task) -> Task:
        """写入新任务；``idem_key`` 已存在时返回既有任务（幂等）。"""
        ...

    async def get(self, task_id: str, user_id: str | None = None) -> Task:
        """按 ID 取任务；``user_id`` 不为空时同时校验归属。"""
        ...

    async def list(
        self,
        *,
        user_id: str,
        status: Sequence[TaskStatus] = (),
        type_: TaskType | None = None,
        resource_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[_TaskList, bool]:
        """分页列出任务，返回 ``(items, has_more)``。"""
        ...

    async def update(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        """在乐观锁保护下就地修改任务。

        Raises:
            TaskConflict: 版本已被他人推进（调用方重试）。
        """
        ...

    async def request_cancel(self, task_id: str) -> bool:
        """置取消标记；返回是否为「首次置位」。"""
        ...

    async def is_cancel_requested(self, task_id: str) -> bool:
        """查询取消标记（Worker 在每个检查点调用）。"""
        ...

    async def list_stale_pending(self, *, before: str, limit: int = 50) -> _TaskList:
        """列出「创建于 ``before`` 之前且仍停在 ``PENDING``」的任务（``docs/08`` §5.1）。

        补偿扫描需要**全局**（跨用户）视图：投递失败的任务属于哪个用户都一样
        没人管，而按用户逐个扫会产生 N 次查询、还漏掉永远不会被再次访问的用户。
        """
        ...

    async def count_open(self) -> int:
        """在飞任务数（``PENDING``/``QUEUED``/``RUNNING``），供过载判断用（``docs/08`` §5.4）。"""
        ...


class InMemoryTaskStore:
    """进程内任务表：:class:`~app.tasks.store.TaskStore` 的实现，语义与 MySQL 版对齐（含 idem_key 唯一与版本号）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._by_idem: dict[str, str] = {}
        self._cancels: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def create(self, task: Task) -> Task:
        async with self._lock:
            existing_id = self._by_idem.get(task.idem_key)
            if existing_id is not None:
                # 幂等：同一幂等键重复建任务时返回既有任务，而不是报错
                return replace(self._tasks[existing_id])
            self._tasks[task.id] = replace(task)
            self._by_idem[task.idem_key] = task.id
            return replace(task)

    async def get(self, task_id: str, user_id: str | None = None) -> Task:
        async with self._lock:
            task = self._tasks.get(task_id)
        if task is None or (user_id is not None and task.user_id != user_id):
            # 跨用户访问返回 404 而不是 403，避免枚举（REQ-RAG-011 的同一条原则）
            raise AppError(ErrorCode.TASK_NOT_FOUND, TASK_NOT_FOUND_MESSAGE)
        return replace(task)

    async def list(
        self,
        *,
        user_id: str,
        status: Sequence[TaskStatus] = (),
        type_: TaskType | None = None,
        resource_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[_TaskList, bool]:
        async with self._lock:
            items = [replace(task) for task in self._tasks.values()]
        selected = [
            task
            for task in items
            if task.user_id == user_id
            and (not status or task.status in status)
            and (type_ is None or task.type is type_)
            and (resource_id is None or task.resource_id == resource_id)
        ]
        # 排序键与游标比较必须**是同一个键**：``(created_at, id)``。
        # 这里踩过坑：早期用 ``task.created_at``（字符串）排序、却把游标解出的
        # ``datetime`` 又 ``isoformat()`` 回字符串来比较 —— 两者格式不同
        # （``...604Z`` vs ``...604000+00:00``），字符串比较会把同一毫秒创建的任务
        # 全部判成「已翻过」，第二页直接返回空数组。走 ``cursor_position`` 统一成
        # 带时区 ``datetime`` 再比，同一时间戳下由 ``id`` 兜底（Windows 时间戳粒度粗）。
        selected.sort(key=lambda task: cursor_position(task.created_at, task.id), reverse=True)
        if cursor:
            moment, cursor_id = decode_cursor(cursor)
            selected = [
                task
                for task in selected
                if is_after_cursor(task.created_at, task.id, (moment, cursor_id))
            ]
        page = selected[: limit + 1]
        return page[:limit], len(page) > limit

    async def update(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        async with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise AppError(ErrorCode.TASK_NOT_FOUND, TASK_NOT_FOUND_MESSAGE)
            candidate = replace(current)
            mutate(candidate)
            if candidate.version != current.version:
                # 变更函数不许自己动版本号：版本由 store 统一推进，
                # 否则「乐观锁」会变成各调用点自己说了算的形式主义
                raise TaskConflict(f"变更函数不得修改 version（任务 {task_id}）")
            candidate.version = current.version + 1
            # ``updated_at`` 必须真的前进：它在接口契约里（docs/08 §3），
            # 而“永远等于 created_at”会让「这个任务多久没动了」这类排障问题
            # 得到错误答案（也会让前端的“最后更新时间”永远不变）。
            candidate.updated_at = now_iso()
            self._tasks[task_id] = candidate
            return replace(candidate)

    async def request_cancel(self, task_id: str) -> bool:
        async with self._lock:
            if task_id in self._cancels:
                return False
            self._cancels.add(task_id)
            return True

    async def is_cancel_requested(self, task_id: str) -> bool:
        async with self._lock:
            return task_id in self._cancels

    async def list_stale_pending(self, *, before: str, limit: int = 50) -> _TaskList:
        """见 :class:`TaskStore`。"""
        cutoff = cursor_position(before, "")
        async with self._lock:
            candidates = [
                replace(task)
                for task in self._tasks.values()
                if task.status is TaskStatus.PENDING and _before_or_unparsable(task, cutoff)
            ]
        candidates.sort(key=_sort_key)
        return candidates[: max(1, limit)]

    async def count_open(self) -> int:
        """见 :class:`TaskStore`。"""
        async with self._lock:
            return sum(1 for task in self._tasks.values() if task.status in ACTIVE_STATUSES)


def encode_task_cursor(task: Task) -> str:
    """把任务位置编码为游标（复用统一的不透明游标格式）。"""
    return encode_cursor(*cursor_position(task.created_at, task.id))


#: 时间戳解析不了时的位置：排在**最前**（等价于「非常久以前」）。
#: 补偿扫描宁可多重投一次，也不能因为一行坏数据而整个停摆 ——
#: ``list_stale_pending`` 抛异常会让**所有**滞留任务都得不到补偿，
#: 而原因只是一行 ``created_at`` 被写坏了。
_UNPARSABLE = datetime.min.replace(tzinfo=UTC)


def _sort_key(task: Task) -> tuple[datetime, str]:
    """按 ``(created_at, id)`` 排序；时间戳坏掉时退回「最旧」。"""
    try:
        return cursor_position(task.created_at, task.id)
    except (ValueError, TypeError):
        return (_UNPARSABLE, task.id)


def _before_or_unparsable(task: Task, cutoff: tuple[datetime, str]) -> bool:
    """``(created_at, id) < cutoff``；时间戳坏掉时视为「在 cutoff 之前」。"""
    try:
        return cursor_position(task.created_at, task.id) < cutoff
    except (ValueError, TypeError):
        return True


def build_task_store(settings: Settings) -> TaskStore:
    """按 ``INFRA_BACKEND`` 选择任务仓储。

    * ``memory`` —— 进程内（本地开发 / 单进程测试）。**与 API 同进程**，
      所以 ``TASK_RUNNER=kafka`` 下它不可用（Worker 查不到任务行）；
    * ``real``   —— Redis（跨进程共享）。MySQL 仓储是 ``docs/09`` 指定的权威实现，
      端口已定好，替换不影响业务代码（见 ``docs/12`` 的待办）。

    Redis 不可用（缺依赖 / 连不上）时**不阻断启动**，退化成内存仓储并告警：
    代价写在脸上（跨进程执行失效），而生产环境由 ``validate_for_startup`` 拦住。
    """
    if not settings.uses_shared_task_store:
        return InMemoryTaskStore()
    from app.infrastructure.redis.client import RedisUnavailable
    from app.tasks.redis_store import RedisTaskStore

    try:
        return RedisTaskStore.from_settings(settings)
    except RedisUnavailable as exc:
        logger.warning("task.store_degraded", extra={"error": str(exc)})
        return InMemoryTaskStore()


__all__ = [
    "TASK_NOT_FOUND_MESSAGE",
    "InMemoryTaskStore",
    "TaskConflict",
    "TaskStore",
    "build_task_store",
    "encode_task_cursor",
]
