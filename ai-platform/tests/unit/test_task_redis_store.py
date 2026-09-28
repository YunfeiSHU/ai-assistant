"""跨进程任务仓储（``docs/08`` §4，``docs/09`` §4）的**纯逻辑**部分。

真 Redis 的语义（Lua 原子性、TTL、ZSET 排序）属于集成测试；这里测的是那些
「不依赖 Redis 也一定会出错」的地方：

* 记录的字段集合（``docs/09`` 的表结构是跨语言契约，漏一个字段就是丢数据）；
* 反序列化的**宽容度**（灰度期间新旧格式并存，严格解析会把旧记录变成 500）；
* 时间戳 → ZSET 分数（差 1000 倍会让列表顺序完全错乱，而错误不会报出来）；
* 按 ``INFRA_BACKEND`` 的选择与降级（缺依赖时不能让应用起不来）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import build_settings

from app.tasks.models import ResourceType, Task, TaskError, TaskStatus, TaskType
from app.tasks.redis_store import (
    RedisTaskStore,
    task_from_record,
    task_to_record,
)
from app.tasks.store import InMemoryTaskStore, TaskStore, build_task_store


def _task() -> Task:
    return Task(
        id="task_1",
        type=TaskType.DOCUMENT_INGEST,
        status=TaskStatus.RUNNING,
        user_id="u_1",
        resource_type=ResourceType.DOCUMENT,
        resource_id="doc_1",
        payload={"doc_id": "doc_1", "extra": [1, 2]},
        progress=42,
        stage="embedding",
        retry_count=1,
        max_retries=3,
        error=TaskError(
            code="INTERNAL_ERROR",
            message="上一次失败",
            detail={"hint": "x"},
            at="2026-01-01T00:00:00+00:00",
        ),
        idem_key="hashed",
        created_at="2026-01-01T00:00:00+00:00",
        queued_at="2026-01-01T00:00:01+00:00",
        started_at="2026-01-01T00:00:02+00:00",
        finished_at="2026-01-01T00:00:03+00:00",
        updated_at="2026-01-01T00:00:04+00:00",
    )


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def test_record_carries_every_contract_field() -> None:
    """记录里必须带全 ``docs/09`` 的列（漏一个就是丢数据）。"""
    record = task_to_record(_task())
    assert set(record) == {
        "id",
        "type",
        "status",
        "user_id",
        "resource_type",
        "resource_id",
        "payload",
        "progress",
        "stage",
        "retry_count",
        "max_retries",
        "error",
        "idem_key",
        "created_at",
        "queued_at",
        "started_at",
        "finished_at",
        "updated_at",
        "version",
    }


def test_record_roundtrip_preserves_everything() -> None:
    """往返之后逐字段一致（不能靠「能读出来」来判定，太多字段会被默认值掩盖）。"""
    original = _task()
    restored = task_from_record(task_to_record(original))
    assert restored == original


def test_missing_fields_fall_back_to_defaults() -> None:
    """缺字段用默认值：灰度/回滚期间新旧格式并存，严格解析会把旧记录变成 500。"""
    restored = task_from_record({"id": "task_1"})
    assert restored.status is TaskStatus.PENDING
    assert restored.type is TaskType.DOCUMENT_INGEST
    assert restored.payload == {}
    assert restored.progress == 0
    assert restored.retry_count == 0
    assert restored.max_retries == 0
    assert restored.error is None


def test_unknown_fields_are_ignored() -> None:
    """未知字段被忽略（回滚时新字段还在记录里）。"""
    record = task_to_record(_task())
    record["something_new"] = {"a": 1}
    assert task_from_record(record).id == "task_1"


@pytest.mark.parametrize("bad", ["nope", 5, None, {"a": 1}, ["x"]])
def test_bad_enum_values_fall_back(bad: object) -> None:
    """枚举值坏掉时退回默认值，而不是抛 ``ValueError`` 让整条记录读不出来。"""
    restored = task_from_record({"id": "t", "status": bad, "type": bad, "resource_type": bad})
    assert restored.status is TaskStatus.PENDING
    assert restored.type is TaskType.DOCUMENT_INGEST
    assert restored.resource_type is ResourceType.DOCUMENT


@pytest.mark.parametrize("bad", ["string", [1, 2], 7, None])
def test_bad_payload_becomes_empty_dict(bad: object) -> None:
    """``payload`` 不是对象时退化成空字典（它是 JSON 列，写坏了不该拖垮读路径）。"""
    assert task_from_record({"id": "t", "payload": bad}).payload == {}


def test_partial_error_record_is_not_dropped() -> None:
    """``error`` 只有 code 时也要能读出（部分写入的记录）。"""
    restored = task_from_record({"id": "t", "error": {"code": "MQ_UNAVAILABLE"}})
    assert restored.error is not None
    assert restored.error.code == "MQ_UNAVAILABLE"
    assert restored.error.at  # 缺失时补一个时间戳，避免前端拿到空字段


def test_error_null_is_preserved() -> None:
    """``error`` 为 ``null`` 表示「没有错误」，不能凭空造一个错误出来。"""
    assert task_from_record({"id": "t", "error": None}).error is None


def test_error_can_be_round_tripped_to_null() -> None:
    """无错误的任务序列化后 ``error`` 为 ``None``。"""
    task = _task()
    task.error = None
    assert task_to_record(task)["error"] is None
    assert task_from_record(task_to_record(task)).error is None


# ---------------------------------------------------------------------------
# 时间戳 → 分数
# ---------------------------------------------------------------------------
def test_iso_to_epoch_millis_is_utc_and_millisecond_precise() -> None:
    """ZSET 分数是**毫秒**时间戳（差 1000 倍会让列表顺序彻底乱掉且不报错）。"""
    from app.tasks.redis_store import _iso_to_epoch_millis

    value = _iso_to_epoch_millis("2026-01-01T00:00:01.500Z")
    moment = datetime.fromtimestamp(value / 1000, tz=UTC)
    assert (moment.year, moment.month, moment.day, moment.second) == (2026, 1, 1, 1)
    assert value % 1000 == 500


def test_iso_to_epoch_millis_tolerates_naive_and_bad_input() -> None:
    """没有时区的（或坏掉的）时间戳不能抛出去。

    坏时间戳一律落到 ``0``（= 1970）：它在 ZSET 里等价于「最旧」，
    于是补偿扫描会扫到它（宁可多重投一次），而列表接口把它排到最后。
    """
    from app.tasks.redis_store import _iso_to_epoch_millis

    assert _iso_to_epoch_millis("2026-01-01T00:00:00") > 0
    assert _iso_to_epoch_millis("坏数据") == 0.0


# ---------------------------------------------------------------------------
# 构造与降级
# ---------------------------------------------------------------------------
def test_memory_backend_uses_in_memory_store() -> None:
    """``INFRA_BACKEND=memory`` → 进程内实现。"""
    assert isinstance(build_task_store(build_settings()), InMemoryTaskStore)


def test_real_backend_without_redis_degrades() -> None:
    """``real`` 但缺 ``redis`` 依赖 → 降级为内存实现（并告警），而不是启动失败。"""
    import importlib.util

    if importlib.util.find_spec("redis") is not None:  # pragma: no cover
        pytest.skip("本机装了 redis，无法测降级路径")
    store = build_task_store(build_settings(infra_backend="real", task_runner="kafka"))
    assert isinstance(store, InMemoryTaskStore)


def test_uses_shared_task_store_follows_infra_backend() -> None:
    """``uses_shared_task_store`` 是「任务状态是否跨进程」的唯一判据。"""
    assert build_settings().uses_shared_task_store is False
    assert build_settings(infra_backend="real", task_runner="kafka").uses_shared_task_store is True


def test_redis_store_satisfies_the_port() -> None:
    """``RedisTaskStore`` 必须满足 ``TaskStore`` 协议（否则换后端就是改业务代码）。"""
    store: TaskStore = RedisTaskStore(object())
    assert isinstance(store, TaskStore)


def test_redis_store_key_layout() -> None:
    """Key 规范见 ``docs/09`` §4（改 Key 等于丢数据）。"""
    from app.tasks.redis_store import _cancel_key, _idem_key, _task_key, _user_key

    assert _task_key("t1") == "task:t1"
    assert _idem_key("abc") == "task:idem:abc"
    assert _user_key("u1") == "task:user:u1"
    assert _cancel_key("t1") == "cancel:t1"


def test_open_index_is_a_zset_of_all_users() -> None:
    """``count_open`` 依赖全局 ZSET；它的键名必须在测试里钉住。"""
    from app.tasks.redis_store import OPEN_ZSET

    assert OPEN_ZSET == "task:open"
