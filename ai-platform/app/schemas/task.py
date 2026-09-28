"""异步任务请求 / 响应契约（``docs/08`` §3 / §4）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import StrictModel


class TaskErrorOut(StrictModel):
    """任务错误（``docs/08`` §3：``{code, message, detail, at}``）。"""

    code: str
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    at: str = ""


class TaskOut(StrictModel):
    """任务实体（``docs/08`` §3）。"""

    id: str
    type: str
    status: str
    resource_type: str
    resource_id: str
    progress: int = 0
    stage: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    error: TaskErrorOut | None = None
    #: 状态机给出的「客户端能做什么」，避免前端自己维护一份状态表
    cancelable: bool = False
    retryable: bool = False
    created_at: str = ""
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""


class TaskList(StrictModel):
    """任务列表响应（``docs/08`` §4.1）。"""

    items: list[TaskOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


__all__ = ["TaskErrorOut", "TaskList", "TaskOut"]
