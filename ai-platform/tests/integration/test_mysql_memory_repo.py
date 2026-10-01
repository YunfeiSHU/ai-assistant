"""MySQL 长期记忆仓储的集成测试（``docs/09`` §2.5，``REQ-MEM-004/005/007``）。

这一层要证明的是「SQL 语义与内存实现一致」，重点是三处**只有真库才能验证**的行为：

* ``hit_count`` 用 ``UPDATE ... hit_count + 1`` **单条语句自增**：并发重复写入时
  不能丢更新（读改写会）。这是我在实现里刻意不用「读出来 +1 再写回」的原因，
  所以必须有用例把它钉住。
* ``GREATEST(confidence, ?)`` 只升不降：并发的低置信写入不能把高置信记录拉低。
* ``ORDER BY confidence ASC, updated_at ASC LIMIT n`` 的淘汰：超出 ``MEMORY_MAX_ITEMS``
  时丢掉的必须是「最不可靠且最久没动过」的那条。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.conftest import build_settings

from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.memory.long_term import MemoryRecord
from app.memory.mysql_repo import MySqlMemoryRepo

pytestmark = pytest.mark.usefixtures("mysql_dsn")


def _record(user_id: str, content: str, **overrides: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "id": new_id("mem"),
        "user_id": user_id,
        "content": content,
        "kind": "fact",
        "confidence": 0.9,
    }
    values.update(overrides)
    return MemoryRecord(**values)


@pytest.fixture
async def memory_repo(mysql_settings: Any) -> Any:
    repo = MySqlMemoryRepo(mysql_settings)
    try:
        yield repo
    finally:
        await repo.aclose()


async def test_add_and_get_roundtrip(memory_repo: Any, cleanup_user: str) -> None:
    """新增 → 读回：正文、类型、置信度、时间戳格式逐一对齐。"""
    result = await memory_repo.add(_record(cleanup_user, "用户偏好中文回答"))
    assert result.created is True
    loaded = await memory_repo.get(result.record.id, cleanup_user)
    assert loaded.content == "用户偏好中文回答"
    assert loaded.kind == "fact"
    assert loaded.confidence == pytest.approx(0.9)
    # 时间戳 MUST 与内存后端同格式（否则契约测试过、真机前端解析失败）
    assert loaded.created_at.endswith("Z")
    assert loaded.source == "auto"
    assert loaded.hit_count == 1


async def test_manual_source_is_persisted(memory_repo: Any, cleanup_user: str) -> None:
    """``source`` 列落到库里（``POST /memories`` 传 ``manual``）。"""
    await memory_repo.add(_record(cleanup_user, "手动写入", source="manual"))
    found = await memory_repo.find_by_hash(cleanup_user, "手动写入")
    assert found is not None and found.source == "manual"


async def test_duplicate_content_bumps_hit_count(memory_repo: Any, cleanup_user: str) -> None:
    """同一句话重复写入 → 不新增，``hit_count`` 累加（精确去重）。"""
    first = await memory_repo.add(_record(cleanup_user, "我叫小明"))
    second = await memory_repo.add(_record(cleanup_user, "我叫小明"))

    assert first.created is True
    assert second.created is False
    assert second.record.id == first.record.id
    assert second.record.hit_count == 2
    assert await memory_repo.count(cleanup_user) == 1


async def test_concurrent_duplicate_writes_do_not_lose_updates(
    memory_repo: Any, cleanup_user: str
) -> None:
    """并发写同一句话：只有一次创建，其余全部计为命中且**一个都不丢**。

    这条用例针对的是「读改写」这种写法：两个请求都读到 ``hit_count=1``、
    都写回 ``2``，于是三次命中只涨到 2。实现用的是 ``hit_count + 1`` 表达式，
    这里就是它的证明。
    """
    rounds = 5
    results = await asyncio.gather(
        *[memory_repo.add(_record(cleanup_user, "并发去重目标")) for _ in range(rounds)]
    )

    created = [item for item in results if item.created]
    assert len(created) == 1
    stored = await memory_repo.find_by_hash(cleanup_user, "并发去重目标")
    assert stored is not None
    assert stored.hit_count == rounds
    assert await memory_repo.count(cleanup_user) == 1


async def test_touch_never_lowers_confidence(memory_repo: Any, cleanup_user: str) -> None:
    """``touch`` 的置信度只升不降（``GREATEST``）——低置信命中不能把记录拉低。"""
    created = await memory_repo.add(_record(cleanup_user, "高置信事实", confidence=0.95))
    lowered = await memory_repo.touch(created.record.id, confidence=0.1)
    assert lowered.confidence == pytest.approx(0.95)
    assert lowered.hit_count == 2

    raised = await memory_repo.touch(created.record.id, confidence=0.99)
    assert raised.confidence == pytest.approx(0.99)


async def test_cross_user_access_is_not_found(memory_repo: Any, cleanup_user: str) -> None:
    """跨用户读/删 → ``404``（``REQ-MEM-007`` 的隔离要求）。"""
    created = await memory_repo.add(_record(cleanup_user, "私有记忆"))
    other = new_id("u")
    with pytest.raises(AppError) as excinfo:
        await memory_repo.get(created.record.id, other)
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND

    with pytest.raises(AppError):
        await memory_repo.delete(created.record.id, other)
    # 本人的记录仍在
    assert await memory_repo.count(cleanup_user) == 1


async def test_list_page_filters_and_paginates(memory_repo: Any, cleanup_user: str) -> None:
    """分页 + ``kind`` 过滤 + ``expired`` 过滤（行值游标）。"""
    for index in range(3):
        await memory_repo.add(_record(cleanup_user, f"事实 {index}", kind="fact"))
    await memory_repo.add(_record(cleanup_user, "偏好：中文", kind="preference"))

    facts, has_more = await memory_repo.list_page(cleanup_user, kind="fact", limit=2)
    assert len(facts) == 2
    assert has_more is True
    assert all(item.kind == "fact" for item in facts)

    from app.memory.long_term import encode_memory_cursor

    page2, has_more2 = await memory_repo.list_page(
        cleanup_user, kind="fact", limit=2, cursor=encode_memory_cursor(facts[-1])
    )
    assert len(page2) == 1
    assert has_more2 is False

    prefs, _ = await memory_repo.list_page(cleanup_user, kind="preference")
    assert [item.content for item in prefs] == ["偏好：中文"]

    active, _ = await memory_repo.list_page(cleanup_user, expired=False)
    assert len(active) == 4


async def test_save_updates_hash_and_conflicts(memory_repo: Any, cleanup_user: str) -> None:
    """改正文必须重算哈希；改成一个已存在的正文 → ``409``。

    哈希不重算的后果很具体：唯一索引指向旧哈希，于是「改回原文」会被判成
    重复而静默丢弃（``docs/12`` 记过这个坑）。
    """
    first = await memory_repo.add(_record(cleanup_user, "旧正文"))
    await memory_repo.add(_record(cleanup_user, "已有的另一条"))

    updated = MemoryRecord(
        id=first.record.id, user_id=cleanup_user, content="新的正文", kind="fact", confidence=0.9
    )
    saved = await memory_repo.save(updated)
    assert saved.content == "新的正文"
    assert await memory_repo.find_by_hash(cleanup_user, "新的正文") is not None
    assert await memory_repo.find_by_hash(cleanup_user, "旧正文") is None

    clash = MemoryRecord(
        id=first.record.id,
        user_id=cleanup_user,
        content="已有的另一条",
        kind="fact",
        confidence=0.9,
    )
    with pytest.raises(AppError) as excinfo:
        await memory_repo.save(clash)
    assert excinfo.value.code is ErrorCode.CONFLICT


async def test_delete_returns_the_removed_record(memory_repo: Any, cleanup_user: str) -> None:
    """删除返回被删记录 —— 调用方要用它同步删向量（``REQ-MEM-007`` 的双删）。"""
    created = await memory_repo.add(_record(cleanup_user, "待删除"))
    removed = await memory_repo.delete(created.record.id, cleanup_user)
    assert removed.id == created.record.id
    assert await memory_repo.count(cleanup_user) == 0
    # 幂等性不需要：第二次删除应当报 404（与内存实现一致）
    with pytest.raises(AppError):
        await memory_repo.delete(created.record.id, cleanup_user)


async def test_delete_all_returns_count(memory_repo: Any, cleanup_user: str) -> None:
    """``delete_all`` 返回本次实际删除条数，用户已空时返回 0（可重复调用）。"""
    for index in range(3):
        await memory_repo.add(_record(cleanup_user, f"第 {index} 条"))
    assert await memory_repo.delete_all(cleanup_user) == 3
    assert await memory_repo.count(cleanup_user) == 0
    assert await memory_repo.delete_all(cleanup_user) == 0


async def test_eviction_drops_lowest_confidence_first(cleanup_user: str) -> None:
    """容量上限：超出 ``MEMORY_MAX_ITEMS`` 时丢掉置信度最低的那条。"""
    settings = build_settings(infra_backend="real", memory_max_items=2)
    repo = MySqlMemoryRepo(settings)
    try:
        low = await repo.add(_record(cleanup_user, "低置信", confidence=0.2))
        await repo.add(_record(cleanup_user, "中置信", confidence=0.6))
        await repo.add(_record(cleanup_user, "高置信", confidence=0.99))

        items = await repo.all_for_user(cleanup_user)
        assert len(items) == 2
        assert low.record.id not in {item.id for item in items}
        assert {item.content for item in items} == {"中置信", "高置信"}
    finally:
        await repo.aclose()


def test_build_memory_repo_uses_mysql_when_real(mysql_settings: Any) -> None:
    """``INFRA_BACKEND=real`` → 真实 MySQL 仓储。"""
    from app.memory import build_memory_repo

    assert build_memory_repo(mysql_settings).__class__.__name__ == "MySqlMemoryRepo"


def test_build_memory_repo_falls_back_when_driver_missing(monkeypatch: Any) -> None:
    """驱动缺失 → 占位实现（记忆接口 503，对话降级但可用）。"""
    from app.memory import build_memory_repo

    def _boom(_settings: Any) -> Any:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "未安装 sqlalchemy")

    monkeypatch.setattr("app.memory.mysql_repo.MySqlMemoryRepo", _boom)
    repo = build_memory_repo(build_settings(infra_backend="real"))
    assert repo.__class__.__name__ == "UnavailableMemoryRepo"
