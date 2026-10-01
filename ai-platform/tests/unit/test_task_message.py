"""任务消息契约（``docs/09`` §5.2）。

消息体的字段集合是**跨语言契约**（Go 侧网关、其它消费者都可能读它），
所以这里逐个字段断言，而不是「能 round-trip 就行」——round-trip 对「多塞了
三个字段」这种漂移是完全无感的。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from tests.conftest import build_settings

from app.core.exceptions import AppError, ErrorCode
from app.tasks.models import ResourceType, Task, TaskStatus, TaskType
from app.tasks.transport import (
    DLQ_TOPIC,
    TOPIC_PREFIX,
    KafkaTaskConsumer,
    KafkaTaskProducer,
    TaskMessage,
    build_task_producer,
    partition_key,
    topic_for,
)


def _task(
    *,
    type_: TaskType = TaskType.DOCUMENT_INGEST,
    resource_id: str = "doc_1",
    user_id: str = "u_1",
    retry_count: int = 0,
) -> Task:
    return Task(
        id="task_1",
        type=type_,
        status=TaskStatus.PENDING,
        user_id=user_id,
        resource_type=ResourceType.DOCUMENT,
        resource_id=resource_id,
        retry_count=retry_count,
    )


def test_topic_is_derived_from_type() -> None:
    """主题名由类型推导（``ai.task.<type>``），不查表。"""
    assert topic_for(TaskType.DOCUMENT_INGEST) == "ai.task.document_ingest"
    assert topic_for("memory_extract") == "ai.task.memory_extract"
    assert TOPIC_PREFIX == "ai.task."
    assert DLQ_TOPIC == "ai.task.dlq"


def test_partition_key_uses_user_for_memory_extract() -> None:
    """记忆抽取按用户分区（同一用户的抽取必须串行）。"""
    task = _task(type_=TaskType.MEMORY_EXTRACT, resource_id="doc_1", user_id="u_9")
    assert partition_key(task) == "u_9"


def test_partition_key_uses_resource_for_ingest() -> None:
    """其余任务按资源分区（同一资源不并发处理）。"""
    assert partition_key(_task(resource_id="doc_7")) == "doc_7"


def test_from_task_maps_attempt_from_retry_count() -> None:
    """``attempt`` 从 1 开始计，等于 ``retry_count + 1``。"""
    message = TaskMessage.from_task(_task(retry_count=2))
    assert message.attempt == 3
    assert message.task_id == "task_1"
    assert message.topic == "ai.task.document_ingest"


def test_encode_contains_only_task_id_and_attempt() -> None:
    """消息体只含两个键（多一个都是契约漂移）。"""
    payload = json.loads(TaskMessage.from_task(_task()).encode())
    assert payload == {"task_id": "task_1", "attempt": 1}


def test_decode_restores_transport_metadata() -> None:
    """主题与 key 是传输层产物，解码时从入参补齐。"""
    raw = TaskMessage.from_task(_task()).encode()
    message = TaskMessage.decode(
        raw, topic="ai.task.document_ingest", key="doc_1", partition=2, offset=9
    )
    assert (message.task_id, message.type, message.partition, message.offset) == (
        "task_1",
        TaskType.DOCUMENT_INGEST,
        2,
        9,
    )
    assert message.partition_key == "doc_1"


def test_decode_accepts_str_and_bytes_key() -> None:
    """``aiokafka`` 给的 key 是 bytes，自己造的测试数据常是 str。"""
    raw = TaskMessage.from_task(_task()).encode()
    assert TaskMessage.decode(raw, topic="ai.task.document_ingest", key=b"doc_1").partition_key == (
        "doc_1"
    )


@pytest.mark.parametrize(
    ("raw", "topic", "reason"),
    [
        ("not json", "ai.task.document_ingest", "非法 JSON"),
        ('{"attempt": 1}', "ai.task.document_ingest", "缺 task_id"),
        ('{"task_id": ""}', "ai.task.document_ingest", "task_id 为空"),
        ('{"task_id": "t"}', "document_ingest", "主题前缀不对"),
        ('{"task_id": "t"}', "ai.task.unknown_type", "未知类型"),
    ],
)
def test_decode_rejects_invalid_messages(raw: str, topic: str, reason: str) -> None:
    """坏消息必须抛 ``ValueError``（调用方据此丢进死信，而不是崩掉循环）。"""
    with pytest.raises(ValueError):
        TaskMessage.decode(raw, topic=topic)


def test_decode_rejects_non_object_payload() -> None:
    """JSON 数组/标量不是合法任务消息。"""
    with pytest.raises(ValueError):
        TaskMessage.decode("[1,2]", topic="ai.task.document_ingest")


def test_decode_defaults_attempt_when_absent() -> None:
    """老的/手写的消息没有 ``attempt`` → 当作首次投递。"""
    message = TaskMessage.decode('{"task_id": "t1"}', topic="ai.task.document_ingest")
    assert message.attempt == 1


def test_dlq_payload_keeps_error_and_truncates() -> None:
    """死信体保留 ``task_id`` 与最后错误，长消息被截断（``docs/08`` §5.2）。"""
    message = TaskMessage.from_task(_task())
    payload = json.loads(message.dlq_payload(code="MQ_UNAVAILABLE", message="x" * 800))
    assert payload["task_id"] == "task_1"
    assert payload["error"]["code"] == "MQ_UNAVAILABLE"
    assert len(payload["error"]["message"]) == 500


def test_build_producer_requires_aiokafka() -> None:
    """缺 ``aiokafka`` 时报 ``MQ_UNAVAILABLE`` 并给出可执行提示（不是 ImportError）。"""
    import importlib.util

    settings = build_settings(task_runner="kafka")
    producer = build_task_producer(settings)
    if importlib.util.find_spec("aiokafka") is not None:  # pragma: no cover - 装了就用真实现
        assert isinstance(producer, KafkaTaskProducer)
        return
    assert isinstance(producer, KafkaTaskProducer)
    # 延迟到 ``start()`` 才导入：构造时不会因为缺依赖而失败，
    # 这让「本地 inline 开发、线上 kafka」的同一份代码都能 import 成功
    with pytest.raises(AppError) as excinfo:
        asyncio.run(producer.start())
    assert excinfo.value.code is ErrorCode.MQ_UNAVAILABLE
    assert "aiokafka" in str(excinfo.value.details)


def test_kafka_consumer_requires_aiokafka() -> None:
    """消费者同理：启动期就给出明确错误，而不是一个空转的循环。"""
    import importlib.util

    consumer = KafkaTaskConsumer(build_settings(task_runner="kafka"))
    if importlib.util.find_spec("aiokafka") is not None:  # pragma: no cover
        pytest.skip("本机装了 aiokafka")
    with pytest.raises(AppError) as excinfo:
        asyncio.run(consumer.start())
    assert excinfo.value.code is ErrorCode.MQ_UNAVAILABLE


def test_kafka_consumer_subscribes_all_task_types() -> None:
    """默认订阅**全部**任务主题。

    漏订阅一个类型的表现是「这类任务永远停在 QUEUED」，而且没有任何报错 ——
    默认值必须是把所有类型都订阅上，而不是让部署方手写清单。
    """
    consumer = KafkaTaskConsumer(build_settings(task_runner="kafka"))
    assert consumer.topics == [topic_for(type_) for type_ in TaskType]
