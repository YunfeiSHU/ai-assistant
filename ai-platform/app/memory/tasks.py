"""Memory 相关的任务处理器（``summary_build`` / ``memory_extract``）。

对应 ``docs/07`` §3.1（摘要触发）与 §5.1（抽取）。两者都刻意做成**异步任务**：
它们要调一次 LLM，而这次调用与用户当前那轮回答毫无关系 —— 放进请求路径只会让
「回答已经流完了，但响应还没结束」这种更难解释的延迟。

失败语义：处理器内部用 ``TaskService.track`` 包住，因此失败会落到 ``FAILED``
并可被 ``POST /tasks/{id}/retry`` 重试。**对话本身不会因此失败**：任务是在
响应之后投递的，它的失败只体现在 ``GET /tasks`` 上。
"""

from __future__ import annotations

import logging

from app.application.memory import MemoryService
from app.core.exceptions import AppError, ErrorCode
from app.tasks.models import Task, TaskType
from app.tasks.service import TaskService

logger = logging.getLogger("app.memory.tasks")


class MemoryTaskHandlers:
    """把任务类型映射到 :class:`MemoryService` 的用例方法。"""

    def __init__(self, tasks: TaskService, service: MemoryService) -> None:
        self._tasks = tasks
        self._service = service

    def bind(self, service: MemoryService) -> None:
        """改绑服务实例。

        为什么需要：处理器**只能注册一次**（重复注册是装配错误，分发器会直接抛），
        所以「重建记忆层」不能在边上再建一个处理器，只能把已注册的那个改绑到新服务。
        这与路由层「每次请求从 ``app.state`` 取服务」是同一个口径：注入点是
        ``app.state``，而不是构造期就写死的引用。
        """
        self._service = service

    async def handle(self, task: Task) -> None:
        """处理器入口（由 :class:`~app.tasks.dispatch.TaskDispatcher` 调用）。"""
        if task.type is TaskType.SUMMARY_BUILD:
            await self._build_summary(task)
            return
        if task.type is TaskType.MEMORY_EXTRACT:
            await self._extract(task)
            return
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"MemoryTaskHandlers 不支持的任务类型：{task.type}",
            {"type": str(task.type)},
        )

    # ------------------------------------------------------------------
    async def _build_summary(self, task: Task) -> None:
        """``resource_id`` 为会话 id；``payload.force=true`` 来自手动重建接口。"""
        # 只有手动重建才 ``force``：自动触发路径必须让防抖器生效，否则
        # 「连续多轮对话各建一个摘要任务」会各自绕过 5 分钟窗口，把上游打满。
        force = bool(task.payload.get("force", False))
        if self._service.summary_builder is None:
            # 没配摘要器却投递了任务 = 装配错误，必须失败而不是记成功
            async with self._tasks.track(task.id):
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "摘要生成器未装配",
                    {"conversation_id": task.resource_id},
                )
        async with self._tasks.track(task.id):
            outcome = await self._service.build_summary(task.resource_id, task.user_id, force=force)
            if outcome is None:
                # ``None`` 是「条件不满足或命中防抖，本轮跳过」，**不是**错误：
                # 每次对话都可能投递摘要任务，而防抖窗口内只允许生成一次 ——
                # 把跳过记成失败会让任务列表里堆满假故障。
                logger.info(
                    "memory.summary_skipped",
                    extra={"task_id": task.id, "conversation_id": task.resource_id},
                )
                return
            if outcome.error:
                raise AppError(
                    ErrorCode.SUMMARY_GENERATION_FAILED,
                    f"摘要生成失败：{outcome.error}",
                    {"conversation_id": task.resource_id},
                )
            logger.info(
                "memory.summary_task_done",
                extra={
                    "task_id": task.id,
                    "conversation_id": task.resource_id,
                    "covered_message_count": outcome.covered_messages,
                },
            )

    async def _extract(self, task: Task) -> None:
        """``payload.conversation_id`` 为来源会话（``resource_id`` 也是它）。"""
        conversation_id = str(task.payload.get("conversation_id") or task.resource_id)
        async with self._tasks.track(task.id):
            store = self._service.store
            await store.ensure(conversation_id, task.user_id)
            messages = await store.recent(
                conversation_id,
                task.user_id,
                turns=self._service.settings.memory_recent_turns,
            )
            if not messages:
                # 空会话不是错误：清空上下文后旧任务可能还会跑一次
                logger.info(
                    "memory.extract_skipped",
                    extra={"task_id": task.id, "reason": "empty_history"},
                )
                return
            created = await self._service.extract_and_store(
                messages, user_id=task.user_id, conversation_id=conversation_id
            )
            logger.info(
                "memory.extract_task_done",
                extra={"task_id": task.id, "created_count": len(created)},
            )


__all__ = ["MemoryTaskHandlers"]
