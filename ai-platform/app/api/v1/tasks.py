"""任务路由（``docs/08`` §4.1 ～ §4.5）。

``GET /tasks/{id}/events`` 复用 ``/chat/stream`` 的帧构造与保活机制
（``app/core/sse.py``），事件源是任务事件总线（``app/tasks/events.py``）：``inline``
执行器下是进程内广播，``INFRA_BACKEND=real`` 下是 Redis pub/sub。

总线只做尽力而为的增量推送，所以正确用法是「先连这个流拿到快照首帧，再按增量更新」——
首帧永远读任务表（见 ``app/tasks/stream.py``）。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import (
    PaginationDep,
    SettingsDep,
    TaskEventBusDep,
    TaskRunnerDep,
    TaskServiceDep,
    UserId,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, frame_stream
from app.schemas.task import TaskErrorOut, TaskList, TaskOut
from app.tasks.models import Task, TaskStatus, TaskType
from app.tasks.stream import stream_task_events

logger = logging.getLogger("app.api.tasks")

router = APIRouter(prefix="/tasks", tags=["任务"])


def task_out(task: Task) -> TaskOut:
    """任务实体 → 响应模型（附上状态机算出的可操作标志）。"""
    error = None
    if task.error is not None:
        error = TaskErrorOut(
            code=task.error.code,
            message=task.error.message,
            detail=task.error.detail,
            at=task.error.at,
        )
    return TaskOut(
        id=task.id,
        type=str(task.type),
        status=str(task.status),
        resource_type=str(task.resource_type),
        resource_id=task.resource_id,
        progress=task.progress,
        stage=task.stage,
        chunks_total=task.chunks_total,
        chunks_done=task.chunks_done,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        error=error,
        cancelable=task.can_cancel,
        retryable=task.can_retry,
        created_at=task.created_at,
        queued_at=task.queued_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        updated_at=task.updated_at,
    )


@router.get("", response_model=TaskList, summary="列出任务")
async def list_tasks(
    user_id: UserId,
    service: TaskServiceDep,
    pagination: PaginationDep,
    task_status: Annotated[list[str] | None, Query(alias="status", description="可多值")] = None,
    task_type: Annotated[str | None, Query(alias="type")] = None,
    resource_id: Annotated[str | None, Query()] = None,
) -> TaskList:
    """按 ``status`` / ``type`` / ``resource_id`` 过滤并分页（``REQ-TASK-003``）。"""
    statuses = [_parse_status(value) for value in (task_status or [])]
    parsed_type = _parse_type(task_type)
    items, next_cursor = await service.list_tasks(
        user_id=user_id,
        status=statuses,
        type_=parsed_type,
        resource_id=resource_id,
        limit=pagination.limit,
        cursor=pagination.cursor,
    )
    return TaskList(
        items=[task_out(task) for task in items],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.get("/{task_id}", response_model=TaskOut, summary="任务详情")
async def get_task(task_id: str, user_id: UserId, service: TaskServiceDep) -> TaskOut:
    """取单个任务；跨用户返回 ``404 TASK_NOT_FOUND``。"""
    return task_out(await service.get(task_id, user_id))


@router.post("/{task_id}/cancel", response_model=TaskOut, summary="取消任务")
async def cancel_task(task_id: str, user_id: UserId, service: TaskServiceDep) -> TaskOut:
    """取消任务；终态调用 → ``409 TASK_NOT_CANCELABLE``，重复调用幂等。"""
    return task_out(await service.cancel(task_id, user_id))


@router.post("/{task_id}/retry", response_model=TaskOut, summary="重试任务")
async def retry_task(
    task_id: str, user_id: UserId, service: TaskServiceDep, runner: TaskRunnerDep
) -> TaskOut:
    """重试失败任务；``retry_count >= max_retries`` → ``409 TASK_NOT_RETRYABLE``。

    重置状态之后必须投递（``docs/08`` §4.4）：只改数据库状态的话任务永远停在
    ``QUEUED``，接口看起来成功、日志里也没异常，但没有任何东西会来执行它。
    """
    task, requeued = await service.retry(task_id, user_id)
    if requeued:
        await runner.submit(task)
    return task_out(task)


@router.get(
    "/{task_id}/events",
    summary="任务进度（SSE）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "`progress` → （可选多个）`error` → `done`；\n\n"
                "订阅前已是终态时立即推 `done` 并关闭；`FAILED` 只推 `error`"
                "且不关闭（可能自动重试回 `QUEUED`）；15s 无事件推 `ping`"
            ),
        }
    },
)
async def stream_events(
    task_id: str,
    user_id: UserId,
    service: TaskServiceDep,
    bus: TaskEventBusDep,
    settings: SettingsDep,
) -> StreamingResponse:
    """任务进度流（``docs/08`` §4.5）。

    鉴权与「任务存在吗」在 SSE 之前完成：这样越权/不存在仍是普通的
    ``404 TASK_NOT_FOUND`` JSON 响应，而不是先返回 200、再在流里推错误的接口 ——
    后者会让所有 HTTP 客户端都看不出失败。
    """
    await service.get(task_id, user_id)
    return StreamingResponse(
        frame_stream(
            stream_task_events(
                tasks=service,
                bus=bus,
                task_id=task_id,
                user_id=user_id,
                # 任务自身的超时 + 1 分钟余量：比这更久的流一定是客户端早就不看了
                max_seconds=float(settings.task_timeout_seconds) + 60.0,
            )
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


def _parse_status(value: str) -> TaskStatus:
    """解析 ``status`` 查询参数（非法值报错，保持与其它接口一致的错误结构）。"""
    try:
        return TaskStatus(value.upper())
    except ValueError:
        allowed = [str(item) for item in TaskStatus]
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"status 取值非法：{value}",
            {"allowed": allowed},
        ) from None


def _parse_type(value: str | None) -> TaskType | None:
    """解析 ``type`` 查询参数。"""
    if not value:
        return None
    try:
        return TaskType(value.lower())
    except ValueError:
        allowed = [str(item) for item in TaskType]
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"type 取值非法：{value}",
            {"allowed": allowed},
        ) from None


__all__ = ["router", "task_out"]
