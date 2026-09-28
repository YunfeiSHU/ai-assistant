"""任务 Worker（``docs/08`` §5.4）。

启动方式：

    uv run python -m app.worker

它与 API 进程**共用同一段装配代码**（``app.main.build_rag_services``），
所以「同一个任务在 Worker 里被谁执行」与「在 API 进程里被谁执行」不会出现
两份实现漂移 —— 那正是最容易出现「本地 inline 能跑、上 Kafka 就报
『文档不存在』」这类问题的根源。
"""

from __future__ import annotations

from app.worker.loop import EMBEDDING_TYPES, POLL_TIMEOUT_MS, TaskWorker, WorkerStats, build_worker
from app.worker.offsets import OffsetTracker, Position

__all__ = [
    "EMBEDDING_TYPES",
    "POLL_TIMEOUT_MS",
    "OffsetTracker",
    "Position",
    "TaskWorker",
    "WorkerStats",
    "build_worker",
]
