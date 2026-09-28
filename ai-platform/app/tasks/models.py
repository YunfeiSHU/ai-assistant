"""异步任务模型与状态机（契约见 ``docs/08-异步任务.md`` §2 / §3）。

状态机是这一层的核心价值：任务状态被多处代码改（接口建任务、Runner 投递、
Worker 领取、进度上报、重试、取消），如果每个调用点自己判断「现在该不该改」，
迟早出现「已 SUCCEEDED 的任务被改成 FAILED」这种脏数据。
所以允许的迁移集中成一张表，非法迁移一律抛错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.core.errors import AppError, ErrorCode


class TaskStatus(StrEnum):
    """任务状态（``docs/08`` §2）。"""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class TaskType(StrEnum):
    """任务类型（``docs/08`` §1）。"""

    DOCUMENT_INGEST = "document_ingest"
    DOCUMENT_DELETE = "document_delete"
    KB_REINDEX = "kb_reindex"
    SUMMARY_BUILD = "summary_build"
    MEMORY_EXTRACT = "memory_extract"
    MEMORY_DELETE = "memory_delete"


class ResourceType(StrEnum):
    """任务关联的资源类型（``docs/08`` §3）。"""

    DOCUMENT = "document"
    KNOWLEDGE_BASE = "knowledge_base"
    CONVERSATION = "conversation"
    MEMORY = "memory"


#: 终态：不可再变更（``FAILED`` 不算 —— 它还能重试回 ``QUEUED``）
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({TaskStatus.SUCCEEDED, TaskStatus.CANCELED})

#: 「在飞」状态：还在流程里（未到终态也**不是** ``FAILED``）。
#:
#: 与 :data:`TERMINAL_STATUSES` 的区别很重要：``FAILED`` 从状态机看是「未终结」
#: （能重试），但它**不在队列里**。用过载判断（``INGEST_QUEUE_MAX``）或补偿扫描时
#: 把它算进去，会出现「攒了一堆失败任务 → 新上传被 503 拦住」这种自相矛盾的行为。
ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING}
)

#: 允许的状态迁移。刻意用显式白名单而不是「判断是否终态」：
#: 后者表达不了「FAILED 只能回 QUEUED」这类约束。
#:
#: ``PENDING → FAILED`` 是**投递补偿**路径专用（``docs/08`` §5.1：「重投 3 次仍失败
#: → FAILED + MQ_UNAVAILABLE」）。它不在 §2 的状态图里，但 §5.1 的验收要求必须
#: 有这条边 —— 否则「永远投不出去的任务」只能停在 PENDING，而 PENDING 不是终态，
#: 既不会被任何人处理，也不满足「要么成功、要么有一个可重试的失败」。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELED, TaskStatus.FAILED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.CANCELED: frozenset(),
}

#: 允许取消的状态（``docs/08`` §4.3）
CANCELABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING}
)


def now_iso() -> str:
    """毫秒精度 UTC 时间戳（与业务接口的时间格式一致）。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(slots=True)
class TaskError:
    """任务失败详情。"""

    code: str
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail, "at": self.at}


@dataclass(slots=True)
class Task:
    """任务实体（字段与 ``docs/08`` §3 一一对应）。"""

    id: str
    type: TaskType
    status: TaskStatus
    user_id: str
    resource_type: ResourceType
    resource_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    progress: int = 0
    stage: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    error: TaskError | None = None
    idem_key: str = ""
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    #: 乐观锁版本号；每次状态/进度变更 +1
    version: int = 0

    # ------------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        """是否处于不可变更的终态。"""
        return self.status in TERMINAL_STATUSES

    @property
    def can_cancel(self) -> bool:
        """当前是否允许取消。"""
        return self.status in CANCELABLE_STATUSES

    @property
    def can_retry(self) -> bool:
        """当前是否允许重试（``docs/08`` §4.4）。"""
        return self.status is TaskStatus.FAILED and self.retry_count < self.max_retries

    def to_dict(self) -> dict[str, Any]:
        """序列化为接口结构。"""
        return {
            "id": self.id,
            "type": str(self.type),
            "status": str(self.status),
            "user_id": self.user_id,
            "resource_type": str(self.resource_type),
            "resource_id": self.resource_id,
            "payload": self.payload,
            "progress": self.progress,
            "stage": self.stage,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "error": self.error.to_dict() if self.error else None,
            "idem_key": self.idem_key,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def ensure_transition(task: Task, target: TaskStatus) -> None:
    """校验状态迁移是否合法。

    非法迁移抛 ``409 CONFLICT``：这是**调用方用错了状态机**，不是服务器错误，
    但也不该被静默忽略（静默忽略会让「任务卡在 RUNNING」这类问题无从排查）。
    """
    if target not in ALLOWED_TRANSITIONS[task.status]:
        raise AppError(
            ErrorCode.CONFLICT,
            f"任务 {task.id} 不允许从 {task.status} 迁移到 {target}",
            {"from": str(task.status), "to": str(target)},
        )


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CANCELABLE_STATUSES",
    "TERMINAL_STATUSES",
    "ResourceType",
    "Task",
    "TaskError",
    "TaskStatus",
    "TaskType",
    "ensure_transition",
    "now_iso",
]
