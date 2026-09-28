"""异步任务模块（``docs/08``）。

对外只暴露这一层：状态机与模型、仓储、执行器、进度事件与 SSE 流、重试队列、
消息契约、补偿扫描。内部模块（``redis_store`` / ``transport`` …）可以随便重组，
只要这个清单不变，调用方就不受影响。
"""

from __future__ import annotations

from app.tasks.compensation import TaskCompensator
from app.tasks.events import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_PROGRESS,
    InMemoryTaskEventBus,
    Publisher,
    RedisTaskEventBus,
    TaskEvent,
    TaskEventBus,
    build_task_event_bus,
    make_publisher,
)
from app.tasks.models import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    CANCELABLE_STATUSES,
    TERMINAL_STATUSES,
    ResourceType,
    Task,
    TaskError,
    TaskStatus,
    TaskType,
    ensure_transition,
)
from app.tasks.redis_store import RedisTaskStore, task_from_record, task_to_record
from app.tasks.retry import (
    InMemoryRetryQueue,
    RedisRetryQueue,
    RetryQueue,
    build_retry_queue,
    error_retryable,
    is_retryable,
    retry_delay,
)
from app.tasks.runner import (
    InlineTaskRunner,
    KafkaTaskRunner,
    NullTaskRunner,
    TaskHandler,
    TaskRunner,
    build_task_runner,
    require_kafka_runner,
)
from app.tasks.service import TaskService, make_idem_key
from app.tasks.store import (
    TASK_NOT_FOUND_MESSAGE,
    InMemoryTaskStore,
    TaskConflict,
    TaskStore,
    build_task_store,
    encode_task_cursor,
)
from app.tasks.stream import stream_task_events
from app.tasks.transport import (
    DLQ_TOPIC,
    TOPIC_PREFIX,
    KafkaTaskConsumer,
    KafkaTaskProducer,
    TaskConsumer,
    TaskMessage,
    TaskProducer,
    build_task_producer,
    partition_key,
    topic_for,
)

__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_TRANSITIONS",
    "CANCELABLE_STATUSES",
    "DLQ_TOPIC",
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_PROGRESS",
    "TASK_NOT_FOUND_MESSAGE",
    "TERMINAL_STATUSES",
    "TOPIC_PREFIX",
    "InMemoryRetryQueue",
    "InMemoryTaskEventBus",
    "InMemoryTaskStore",
    "InlineTaskRunner",
    "KafkaTaskConsumer",
    "KafkaTaskProducer",
    "KafkaTaskRunner",
    "NullTaskRunner",
    "Publisher",
    "RedisRetryQueue",
    "RedisTaskEventBus",
    "RedisTaskStore",
    "ResourceType",
    "RetryQueue",
    "Task",
    "TaskCompensator",
    "TaskConflict",
    "TaskConsumer",
    "TaskError",
    "TaskEvent",
    "TaskEventBus",
    "TaskHandler",
    "TaskMessage",
    "TaskProducer",
    "TaskRunner",
    "TaskService",
    "TaskStatus",
    "TaskStore",
    "TaskType",
    "build_retry_queue",
    "build_task_event_bus",
    "build_task_producer",
    "build_task_runner",
    "build_task_store",
    "encode_task_cursor",
    "ensure_transition",
    "error_retryable",
    "is_retryable",
    "make_idem_key",
    "make_publisher",
    "partition_key",
    "require_kafka_runner",
    "retry_delay",
    "stream_task_events",
    "task_from_record",
    "task_to_record",
    "topic_for",
]
