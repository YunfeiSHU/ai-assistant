"""任务处理器分发（``docs/08`` §5.3「Worker 按 ``type`` 分派」）。

为什么需要它：``build_task_runner`` 只接受**一个** ``handler``，而任务类型有 6 种。
M3/M4 阶段只有一个入库处理器，把 ``ingestion.handle`` 直接传进去没问题；到 M5 之后
同一进程里还有摘要与记忆抽取 —— 若不解开类型分派，``SUMMARY_BUILD`` 任务会被送进
``_ingest_document``，表现为一条「文档不存在」的莫名其妙的失败。

**未知类型必须失败**，不能静默成功。否则「注册漏了」这件事在监控上看起来是一次
成功任务，而它的业务效果从未发生；用 ``track`` 包住失败路径还能保证任务落到
``FAILED`` 而不是永久停在 ``RUNNING``（前端会一直转圈）。
"""

from __future__ import annotations

import logging

from app.core.exceptions import AppError, ErrorCode
from app.tasks.models import Task, TaskType
from app.tasks.runner import TaskHandler
from app.tasks.service import TaskService

logger = logging.getLogger("app.tasks.dispatch")


class TaskDispatcher:
    """按 ``task.type`` 分派到具体处理器。"""

    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks
        self._handlers: dict[TaskType, TaskHandler] = {}

    def register(self, type_: TaskType, handler: TaskHandler) -> None:
        """注册处理器；同一类型重复注册直接抛（装配错误应当在启动期暴露）。"""
        if type_ in self._handlers:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                f"任务类型 {type_} 的处理器已注册",
                {"type": str(type_), "handler": getattr(handler, "__qualname__", repr(handler))},
            )
        self._handlers[type_] = handler

    def register_many(self, handlers: dict[TaskType, TaskHandler]) -> None:
        """批量注册（装配期用）。任一类型重复注册会由 :meth:`register` 直接抛出。"""
        for type_, handler in handlers.items():
            self.register(type_, handler)

    @property
    def types(self) -> list[str]:
        """已注册的任务类型（启动日志用）。"""
        return sorted(str(item) for item in self._handlers)

    async def handle(self, task: Task) -> None:
        """``TaskRunner`` 调用的入口。"""
        handler = self._handlers.get(task.type)
        if handler is None:
            logger.error(
                "task.no_handler",
                extra={"task_id": task.id, "type": str(task.type), "types": self.types},
            )
            async with self._tasks.track(task.id):
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    f"没有注册 {task.type} 的处理器",
                    {"type": str(task.type)},
                )
        await handler(task)


__all__ = ["TaskDispatcher"]
