"""Kafka 生产者/消费者的内存替身（``docs/11`` §3「外部依赖一律用替身」）。

**为什么必须自己写一个**：Worker 循环里真正容易写出线上事故的地方，全是**偏移量
语义**——「谁先跑完谁提交」会静默丢消息、「失败也提交」会让任务永久消失、
「重复投递没有跳过」会让同一份文档入库两次。这些行为只有在能精确控制
「一条消息什么时候被投出去、offset 提交成了什么」时才测得出来。

替身按三个要点实现：

* **位点是显式记录的**（``commits``），不是「提交了就丢」——测试要断言
  「A 成功了但 B 还在跑时，**不能**提交 B 的位点」这类顺序约束；
* **手动投递**（``deliver``）：不模拟 broker 的异步投递循环，避免测试里出现
  「等消息到达」的 sleep（那类用例在 CI 上必然会随机失败）；
* **失败可注入**（``fail_next``）：让 ``send`` 抛 ``AppError`` 以验证
  「投递失败时任务留在 PENDING 等补偿」，而不是被静默标成 FAILED。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.tasks.transport import TaskMessage, topic_for


class FakeTaskProducer:
    """记录投递内容的 ``TaskProducer`` 替身。"""

    def __init__(self, *, fail_next: bool = False) -> None:
        self.sent: list[TaskMessage] = []
        self.dead_letters: list[dict[str, Any]] = []
        self.started = 0
        self.stopped = 0
        #: 置位后 ``send`` 抛 ``MQ_UNAVAILABLE``（只影响下一次）
        self.fail_next = fail_next
        #: 置位后 ``send`` 一直抛（测补偿扫描的重试上限）
        self.fail_always = False

    async def start(self) -> None:
        self.started += 1

    async def send(self, message: TaskMessage) -> None:
        if self.fail_always or self.fail_next:
            self.fail_next = False
            raise AppError(ErrorCode.MQ_UNAVAILABLE, "替身：投递失败")
        self.sent.append(message)

    async def dead_letter(self, message: TaskMessage, *, code: str, detail: str) -> None:
        self.dead_letters.append({"task_id": message.task_id, "code": code, "detail": detail})

    async def stop(self) -> None:
        self.stopped += 1


class FakeTaskConsumer:
    """可手动投递的 ``TaskConsumer`` 替身。"""

    def __init__(self, *, group: str = "test-group") -> None:
        self.group = group
        self.started = 0
        self.stopped = 0
        #: 提交记录，形如 ``{("ai.task.document.ingest", 0): 3}``
        self.commits: list[dict[tuple[str, int], int]] = []
        self._queue: list[TaskMessage] = []
        self._offset: dict[tuple[str, int], int] = {}

    # -- 测试侧 API ----------------------------------------------------
    def deliver(
        self,
        task_id: str,
        *,
        type_: str = "document.ingest",
        topic: str | None = None,
        partition: int = 0,
        attempt: int = 1,
    ) -> TaskMessage:
        """投递一条消息并返回它（自动分配递增的 offset）。"""
        resolved_topic = topic or topic_for(type_)
        key = (resolved_topic, partition)
        self._offset[key] = self._offset.get(key, -1) + 1
        message = TaskMessage(
            task_id=task_id,
            type=type_,
            attempt=attempt,
            topic=resolved_topic,
            partition_key=task_id,
            partition=partition,
            offset=self._offset[key],
        )
        self._queue.append(message)
        return message

    @property
    def pending(self) -> int:
        """还没被取走的条数。"""
        return len(self._queue)

    # -- TaskConsumer --------------------------------------------------
    async def start(self) -> None:
        self.started += 1

    async def get(self, *, timeout_ms: int = 1000) -> TaskMessage | None:
        if not self._queue:
            return None
        return self._queue.pop(0)

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None:
        if offsets:
            self.commits.append(dict(offsets))

    async def stop(self) -> None:
        self.stopped += 1


__all__ = ["FakeTaskConsumer", "FakeTaskProducer"]
