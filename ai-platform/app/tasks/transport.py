"""任务消息传输层（Kafka 生产/消费端口，``docs/08`` §5.1 / ``docs/09`` §5.2）。

把「消息长什么样」和「怎么发」分开：``docs/09`` §5.2 硬约束消息体 MUST 只含
``{"task_id": ..., "attempt": 1}``，业务参数一律从库里读。写成可单测的编解码函数，就能用
一条断言钉住「有人顺手往消息里塞 payload」这件迟早会发生的事 —— 塞进去之后的表现是「改了
库里的参数但消息里还是老的」，靠读代码几乎发现不了。

三处容易被忽略的约定：

* **主题名即任务类型**（``ai.task.<type>``）：docs/09 §5.2 列出的四个主题与这个规则完全一致，
  所以直接推导而不查表 —— 查表漏一行的表现是「任务建好了但投到了不存在的主题」；
* **分区键按类型分档**：``memory_extract`` 用 ``user_id``（同一用户的抽取必须串行，否则两条
  消息并发 merge 同一行记忆），其余用 ``resource_id``；
* **一次只取一条消息再提交 offset**：``commit()`` 提交的是「已返回记录的当前位置」，一次取一批
  会让「前一条还没处理完就提交了后一条的位置」—— 那是 at-most-once，会**静默丢任务**。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.tasks.models import Task, TaskType

logger = logging.getLogger("app.tasks.transport")

#: 主题前缀：``ai.task.<type>``（``docs/09`` §5.2）
TOPIC_PREFIX = "ai.task."

#: 死信主题（``docs/08`` §5.2 / ``docs/09`` §5.2）
DLQ_TOPIC = "ai.task.dlq"

#: 单次拉取的消息条数。**不要调大**：见模块 docstring 关于 offset 提交的说明。
FETCH_MAX_RECORDS = 1


def topic_for(type_: TaskType | str) -> str:
    """任务类型 → 主题名。

    ``docs/09`` §5.2 的四个主题（``ai.task.document_ingest`` / ``document_delete`` /
    ``summary_build`` / ``memory_extract``）与 ``ai.task.<type>`` 完全一致，所以直接推导。
    """
    return f"{TOPIC_PREFIX}{type_}"


#: 需要用 ``user_id`` 分区的任务类型（``docs/09`` §5.2：按用户保序）
USER_PARTITIONED: frozenset[TaskType] = frozenset({TaskType.MEMORY_EXTRACT})


def partition_key(task: Task) -> str:
    """分区键：``memory_extract`` 用 ``user_id``（同一用户的抽取串行，避免并发 merge 同一行
    记忆），其余用 ``resource_id``（``docs/08`` §5.1：同一资源不并发处理）。

    两者都是 docs 明确要求的，冲突时以 docs/09 §5.2 的分区键列优先 —— 它按主题逐个写明，
    比 docs/08 的通用规则更具体。
    """
    if task.type in USER_PARTITIONED:
        return task.user_id
    return task.resource_id


@dataclass(frozen=True, slots=True)
class TaskMessage:
    """一条任务消息（``{"task_id", "attempt"}`` + 传输元数据）。

    ``type`` / ``topic`` / ``partition_key`` 都是传输层的产物（分别由主题名与消息 key 解出），
    不写进消息体 —— 消息体越小，能漂移的地方就越少。``partition`` / ``offset`` 是提交位点的
    依据（见 :class:`~app.worker.offsets.OffsetTracker`：并发消费时必须按分区的**连续水位线**
    提交，按完成顺序提交会跳消息）。
    """

    task_id: str
    type: TaskType
    attempt: int
    topic: str
    partition_key: str = ""
    partition: int = 0
    offset: int = 0

    @classmethod
    def from_task(cls, task: Task) -> TaskMessage:
        """由任务行构造投递消息（``attempt`` 从 1 开始计）。"""
        return cls(
            task_id=task.id,
            type=task.type,
            attempt=task.retry_count + 1,
            topic=topic_for(task.type),
            partition_key=partition_key(task),
        )

    def encode(self) -> bytes:
        """消息体（``docs/09`` §5.2：只含 ``task_id`` 与 ``attempt``）。"""
        return json.dumps(
            {"task_id": self.task_id, "attempt": self.attempt}, separators=(",", ":")
        ).encode()

    def dlq_payload(self, *, code: str, message: str) -> bytes:
        """死信消息体（``docs/08`` §5.2：保留 ``task_id`` + 最后错误）。"""
        return json.dumps(
            {
                "task_id": self.task_id,
                "attempt": self.attempt,
                "error": {"code": code, "message": message[:500]},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    @classmethod
    def decode(
        cls,
        raw: bytes | str,
        *,
        topic: str,
        key: bytes | str = "",
        partition: int = 0,
        offset: int = 0,
    ) -> TaskMessage:
        """反序列化。

        Raises:
            ValueError: 消息体不是合法 JSON / 缺 ``task_id`` / 主题名不符合约定。
        """
        text = raw.decode() if isinstance(raw, bytes) else raw
        payload = json.loads(text)
        if not isinstance(payload, Mapping):
            msg = f"任务消息必须是 JSON 对象：{text[:80]!r}"
            raise ValueError(msg)
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            msg = f"任务消息缺少 task_id：{text[:80]!r}"
            raise ValueError(msg)
        if not topic.startswith(TOPIC_PREFIX):
            msg = f"任务主题不符合 ai.task.<type> 约定：{topic!r}"
            raise ValueError(msg)
        raw_type = topic[len(TOPIC_PREFIX) :]
        try:
            type_ = TaskType(raw_type)
        except ValueError:
            msg = f"未知的任务类型（主题 {topic!r}）"
            raise ValueError(msg) from None
        attempt = payload.get("attempt", 1)
        decoded_key = key.decode() if isinstance(key, bytes) else key
        return cls(
            task_id=task_id,
            type=type_,
            attempt=int(attempt) if isinstance(attempt, (int, str)) else 1,
            topic=topic,
            partition_key=decoded_key,
            partition=int(partition),
            offset=int(offset),
        )


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------
@runtime_checkable
class TaskProducer(Protocol):
    """消息生产者（投递 + 死信）。"""

    async def start(self) -> None: ...

    async def send(self, message: TaskMessage) -> None:
        """投递并等待确认（``docs/08`` §5.1：确认成功后才允许置 ``QUEUED``）。"""
        ...

    async def dead_letter(self, message: TaskMessage, *, code: str, detail: str) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class TaskConsumer(Protocol):
    """消息消费者（一次一条 + 显式提交 offset）。"""

    async def start(self) -> None: ...

    async def get(self, *, timeout_ms: int = 1000) -> TaskMessage | None:
        """取一条消息；无消息时返回 ``None``（调用方借这个间隙做取消/重试轮询）。"""
        ...

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None:
        """提交 offset（MUST 在任务状态落库之后调用）。

        Args:
            offsets: ``{(topic, partition): 下一个待消费的 offset}``；
                ``None`` 表示按消费者当前位点提交。
        """
        ...

    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Kafka 实现（懒导入 aiokafka：它不是基础依赖）
# ---------------------------------------------------------------------------
def _require_aiokafka(component: str) -> Any:
    """导入 ``aiokafka``；缺失时给出可执行的提示。"""
    try:
        import aiokafka
    except ImportError as exc:  # pragma: no cover - 取决于本机是否装了 aiokafka
        raise AppError(
            ErrorCode.MQ_UNAVAILABLE,
            f"未安装 aiokafka 依赖，无法使用 {component}；请先 uv add aiokafka",
            {"hint": "uv add aiokafka 或把 TASK_RUNNER 设为 inline/none"},
        ) from exc
    return aiokafka


class KafkaTaskProducer:
    """Kafka 生产者（**常驻连接**）。

    早期实现是「每条消息 start/stop 一个 producer」：既丢掉批处理与长连接，又让每次投递
    多付一次 TCP+TLS 握手；更要紧的是 ``stop()`` 失败会被吞掉 —— 表现为「消息发出去了但
    状态没改」或反之，两个方向都不好查。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._producer: Any = None

    async def start(self) -> None:
        """建立连接（幂等）。"""
        if self._producer is not None:
            return
        aiokafka = _require_aiokafka("TASK_RUNNER=kafka 的投递")
        producer = aiokafka.AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            # acks=all：投递「成功」必须是所有同步副本都写入，否则会丢任务
            acks="all",
            enable_idempotence=True,
        )
        await producer.start()
        self._producer = producer

    async def send(self, message: TaskMessage) -> None:
        """投递到 ``ai.task.<type>``；失败抛 ``MQ_UNAVAILABLE``（上层保持 PENDING）。"""
        await self.start()
        try:
            await self._producer.send_and_wait(
                message.topic,
                message.encode(),
                # 分区键：同一资源的任务落同一分区，天然保序且不并发
                key=message.partition_key.encode() or None,
            )
        except Exception as exc:
            raise AppError(
                ErrorCode.MQ_UNAVAILABLE,
                f"任务投递失败：{exc}",
                {"topic": message.topic, "task_id": message.task_id},
            ) from exc

    async def dead_letter(self, message: TaskMessage, *, code: str, detail: str) -> None:
        """复制到死信主题；失败**只告警**。

        死信是「人工排查用」的旁路：重试耗尽的任务本身已经落 ``FAILED`` + ``error``，
        若这里再抛异常，会把「已经处理完的失败」变成「消费循环崩溃」。
        """
        await self.start()
        try:
            await self._producer.send_and_wait(
                DLQ_TOPIC,
                message.dlq_payload(code=code, message=detail),
                key=message.partition_key.encode() or None,
            )
        except Exception as exc:
            logger.warning(
                "task.dlq_failed",
                extra={"task_id": message.task_id, "error": str(exc)},
            )

    async def stop(self) -> None:
        """冲刷并关闭（幂等）。"""
        producer, self._producer = self._producer, None
        if producer is None:
            return
        try:
            await producer.stop()
        except Exception as exc:  # pragma: no cover - 关停路径
            logger.warning("kafka.producer_stop_failed", extra={"error": str(exc)})


class KafkaTaskConsumer:
    """Kafka 消费者（消费组 ``KAFKA_GROUP_ID``，**手动提交 offset**）。"""

    def __init__(self, settings: Settings, *, types: list[TaskType] | None = None) -> None:
        self._settings = settings
        # 默认订阅全部任务主题：Worker 是「全类型执行者」，漏订阅一个类型的表现是
        # 「这类任务永远停在 QUEUED」，而且没有任何报错。
        self._types = list(types) if types else list(TaskType)
        self._consumer: Any = None

    @property
    def topics(self) -> list[str]:
        """订阅的主题清单（启动日志用）。"""
        return [topic_for(type_) for type_ in self._types]

    async def start(self) -> None:
        """启动消费（幂等）。"""
        if self._consumer is not None:
            return
        aiokafka = _require_aiokafka("Worker 消费")
        consumer = aiokafka.AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.kafka_group_id,
            # 必须手动提交：docs/08 §5.4 要求「状态落库成功之后」才提交 offset。
            # 自动提交会把「正在跑的任务」的 offset 提前提交掉 —— 进程崩了就是丢任务。
            enable_auto_commit=False,
            # 从最早开始：新消费组不设的话 ``auto_offset_reset`` 默认 latest，会让
            # 「先建任务、后起 Worker」的场景下那批任务被永久跳过。
            auto_offset_reset="earliest",
        )
        await consumer.start()
        self._consumer = consumer

    async def get(self, *, timeout_ms: int = 1000) -> TaskMessage | None:
        """取一条消息；无消息返回 ``None``。"""
        await self.start()
        batch = await self._consumer.getmany(timeout_ms=timeout_ms, max_records=FETCH_MAX_RECORDS)
        for _partition, records in batch.items():
            for record in records:
                try:
                    return TaskMessage.decode(
                        record.value,
                        topic=record.topic,
                        key=record.key or b"",
                        partition=record.partition,
                        offset=record.offset,
                    )
                except ValueError as exc:
                    # 脏消息：记日志后跳过（不能让它把整个消费组卡在同一个 offset 上）
                    logger.error(
                        "task.message_invalid",
                        extra={"topic": record.topic, "error": str(exc)},
                    )
        return None

    async def commit(self, offsets: Mapping[tuple[str, int], int] | None = None) -> None:
        """提交 offset（调用方 MUST 保证任务状态已落库）。"""
        if self._consumer is None:  # pragma: no cover - 未启动时不该被调用
            return
        try:
            if offsets is None:
                await self._consumer.commit()
            else:
                from aiokafka import TopicPartition
                from aiokafka.structs import OffsetAndMetadata

                await self._consumer.commit(
                    {
                        TopicPartition(topic, partition): OffsetAndMetadata(next_offset, "")
                        for (topic, partition), next_offset in offsets.items()
                    }
                )
        except Exception as exc:
            # 提交失败 = 消息会被重复投递，而 Worker 端是幂等的（docs/08 §5.3），
            # 所以只告警。反过来若在这里抛异常，会打断消费循环。
            logger.warning("task.offset_commit_failed", extra={"error": str(exc)})

    async def stop(self) -> None:
        """关闭消费者（幂等）。"""
        consumer, self._consumer = self._consumer, None
        if consumer is None:
            return
        try:
            await consumer.stop()
        except Exception as exc:  # pragma: no cover - 关停路径
            logger.warning("kafka.consumer_stop_failed", extra={"error": str(exc)})


def build_task_producer(settings: Settings) -> TaskProducer:
    """构造 Kafka 生产者。

    只有 ``TASK_RUNNER=kafka`` 才会走到这里（``build_task_runner`` 负责分档）：
    ``none`` / ``inline`` 没有「投递」这个动作，各自有独立的 runner 实现。
    """
    return KafkaTaskProducer(settings)


__all__ = [
    "DLQ_TOPIC",
    "FETCH_MAX_RECORDS",
    "TOPIC_PREFIX",
    "USER_PARTITIONED",
    "KafkaTaskConsumer",
    "KafkaTaskProducer",
    "TaskConsumer",
    "TaskMessage",
    "TaskProducer",
    "build_task_producer",
    "partition_key",
    "topic_for",
]
