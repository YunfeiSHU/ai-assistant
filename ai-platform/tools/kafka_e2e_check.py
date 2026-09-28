"""Kafka + Worker 端到端验收脚本（``docs/11`` §4 E2E-7 的自动化版）。

**为什么需要它**：契约测试用的 ``TestClient`` 会把应用跑到流结束才交回响应
（见 ``tests/contract/test_tasks.py`` 的说明），而 Kafka 这条链路天生**跨进程** ——
「API 投递 → Worker 消费 → 状态流转 → 事件总线推帧」只能在两个真实进程之间验证。

前置：

1. ``docker compose -f ai-platform/deploy/infra/compose.yml up -d redis``
   （Kafka 同文件，见 ``--profile kafka``）
2. ``uv sync --extra redis --extra kafka``
3. 另开一个终端跑 Worker：
   ``INFRA_BACKEND=real TASK_RUNNER=kafka REDIS_URL=redis://localhost:6379/3 python -m app.worker``
4. 本脚本：``INFRA_BACKEND=real TASK_RUNNER=kafka REDIS_URL=redis://localhost:6379/3 \
   python tools/kafka_e2e_check.py``

成功判据（脚本会逐条打印）：

* 投递后任务从 ``PENDING`` → ``QUEUED`` → ``RUNNING`` → ``SUCCEEDED``；
* 事件总线上能看到与状态一致的 ``progress`` / ``done`` 帧；
* Redis 里任务记录出「打开索引」（``count_open`` 归零）。

选 ``memory_extract`` 类型是为了**不依赖 MySQL/MinIO/Milvus**：空会话会走
「无可抽取 → 直接成功」这条路径，于是能验证整条链路而不被 M5/M6 的未实现项挡住。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time

from app.config import Settings
from app.core.errors import AppError
from app.core.logging import setup_logging
from app.tasks.events import TaskEvent, TaskEventBus, build_task_event_bus, make_publisher
from app.tasks.models import ResourceType, TaskStatus, TaskType
from app.tasks.service import TaskService
from app.tasks.store import build_task_store
from app.tasks.transport import TaskMessage, build_task_producer

TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}
TIMEOUT_SECONDS = float(os.getenv("E2E_TIMEOUT_SECONDS", "30"))


async def _watch_events(bus: TaskEventBus, task_id: str, seen: list[TaskEvent]) -> None:
    """后台订阅事件总线（先把订阅预热，避免漏掉投递瞬间的帧）。"""
    stream = bus.subscribe(task_id)
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    try:
        while True:
            event = await pending
            seen.append(event)
            print(f"  [bus] {event.event} {json.dumps(event.data, ensure_ascii=False)}", flush=True)
            if event.event == "done":
                return
            pending = asyncio.ensure_future(anext(stream))
    finally:
        if not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        await stream.aclose()


async def main() -> int:
    setup_logging(service="kafka-e2e-check", level="INFO")
    settings = Settings(_env_file=None)
    print(f"infra_backend={settings.infra_backend} task_runner={settings.task_runner}")
    if settings.infra_backend != "real":
        print("!! 需要 INFRA_BACKEND=real（任务状态必须跨进程共享）")
        return 2
    if settings.task_runner != "kafka":
        print("!! 需要 TASK_RUNNER=kafka")
        return 2
    print(f"redis_url={settings.redis_url} kafka={settings.kafka_bootstrap_servers}")

    store = build_task_store(settings)
    bus = build_task_event_bus(settings)
    service = TaskService(store, max_retries=settings.task_max_retries, events=make_publisher(bus))
    producer = build_task_producer(settings)

    task, created = await service.create(
        type_=TaskType.MEMORY_EXTRACT,
        user_id="u_e2e",
        resource_type=ResourceType.CONVERSATION,
        resource_id="cv_e2e",
        payload={"conversation_id": "cv_e2e"},
    )
    print(f"created={created} task_id={task.id} status={task.status}")
    assert created, "幂等键命中（上一轮的残留任务）——换一个 resource_id 再跑"

    seen: list[TaskEvent] = []
    watcher = asyncio.create_task(_watch_events(bus, task.id, seen))
    await asyncio.sleep(0.2)  # 让订阅真正建立

    message = TaskMessage.from_task(task)
    print(f"publish topic={message.topic} key={message.partition_key} attempt={message.attempt}")
    await producer.send(message)
    # ``mark_queued`` 必须在投递成功之后：docs/08 §2 的不变式「QUEUED 蕴含已投递」
    await service.mark_queued(task.id)

    history: list[str] = []
    deadline = time.monotonic() + TIMEOUT_SECONDS
    final = task
    while time.monotonic() < deadline:
        current = await service.get(task.id)
        marker = f"{current.status}/{current.stage}/{current.progress}"
        if not history or history[-1] != marker:
            history.append(marker)
            print(f"  [task] {marker} retry_count={current.retry_count}", flush=True)
        final = current
        if current.status in TERMINAL:
            break
        await asyncio.sleep(0.2)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(watcher, timeout=5)

    print("\n--- 结果 ---")
    print(f"状态轨迹：{' -> '.join(history)}")
    print(f"最终状态：{final.status} error={final.error}")
    print(f"事件帧：{[event.event for event in seen]}")
    print(f"在飞任务数：{await service.count_open()}")

    ok = final.status is TaskStatus.SUCCEEDED and final.status in TERMINAL
    if ok and "done" not in [event.event for event in seen]:
        print("!! 任务成功但没收到 done 帧 —— 事件发布链路有问题")
        ok = False

    await producer.stop()
    await bus.close()
    closer = getattr(store, "close", None)
    if closer is not None:
        await closer()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(main()))
    except AppError as exc:
        print(f"依赖不可用：{exc.code} {exc.message} {exc.details}")
        sys.exit(2)
