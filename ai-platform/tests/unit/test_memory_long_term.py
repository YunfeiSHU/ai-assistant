"""长期记忆仓储单测（``REQ-MEM-005`` / ``REQ-MEM-007``）。

重点不在 CRUD 本身，而在两件「实现里容易漏、漏了也不会报错」的事：

1. **唯一索引语义**：同一用户同一内容哈希只应存一条（对应 MySQL 的
   ``uk_mem_user_hash``）。漏掉它，本地全绿、上真库报唯一键冲突。
2. **排序键与游标比较必须同源**：排序用字符串、比较用 ``datetime`` 会静默漏项
   （``docs/12`` §2.1 的老问题）。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import cursor_position
from app.memory.long_term import (
    InMemoryMemoryRepo,
    MemoryKind,
    MemoryRecord,
    content_sha256,
    encode_memory_cursor,
    expiring,
)


def _record(
    mem_id: str,
    *,
    user_id: str = "u_1",
    content: str | None = None,
    kind: MemoryKind = "fact",
    confidence: float = 1.0,
    created_at: str = "2026-09-28T10:00:00.000Z",
    expires_at: str | None = None,
) -> MemoryRecord:
    text = content or f"记忆 {mem_id}"
    return MemoryRecord(
        id=mem_id,
        user_id=user_id,
        content=text,
        kind=kind,
        confidence=confidence,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=created_at,
    )


# ---------------------------------------------------------------------------
# 字段与哈希
# ---------------------------------------------------------------------------
def test_hash_ignores_whitespace_differences() -> None:
    """哈希按规范化文本算：仅空白不同的一句话应视为同一条。"""
    assert content_sha256("我喜欢  简洁\n回答") == content_sha256("我喜欢 简洁 回答")


def test_record_fills_hash_on_construction() -> None:
    """谁定义语义谁负责填字段：构造时自动补哈希。"""
    record = _record("mem_1", content="用户偏好简洁回答")
    assert record.content_sha256 == content_sha256("用户偏好简洁回答")


def test_to_dict_matches_contract_keys() -> None:
    """``to_dict`` 的键必须与 ``docs/07`` §6 的响应字段一致。"""
    payload = _record("mem_1").to_dict()
    assert set(payload) >= {
        "id",
        "content",
        "kind",
        "confidence",
        "hit_count",
        "source_conversation_id",
        "expires_at",
        "expired",
        "created_at",
        "updated_at",
    }
    # 空来源会话必须序列化成 ``None`` 而不是空串，否则前端要判两种「空」
    assert payload["source_conversation_id"] is None


# ---------------------------------------------------------------------------
# 精确去重（唯一索引）
# ---------------------------------------------------------------------------
async def test_add_same_content_keeps_single_row_and_bumps_hit_count() -> None:
    """同一句话写两次 → 仍 1 条，``hit_count=2``（``AC-MEM-08``）。"""
    repo = InMemoryMemoryRepo()
    first = await repo.add(_record("mem_1", content="用户偏好简洁回答"))
    second = await repo.add(_record("mem_2", content="用户偏好简洁回答"))

    assert first.created is True
    assert second.created is False
    assert second.record.id == "mem_1"
    assert second.record.hit_count == 2
    assert await repo.count("u_1") == 1


async def test_add_same_content_for_another_user_is_isolated() -> None:
    """唯一索引是 ``(user_id, hash)``，不是 ``hash``。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", user_id="u_1", content="同一句话"))
    other = await repo.add(_record("mem_2", user_id="u_2", content="同一句话"))
    assert other.created is True
    assert await repo.count("u_1") == 1
    assert await repo.count("u_2") == 1


async def test_find_by_hash_returns_copy() -> None:
    """取出来的是副本：调用方随手改不该改到存储里的状态。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", content="用户偏好 简洁回答"))
    # 查找键按同一套规范化比较：连续空白/换行不同应命中同一条
    found = await repo.find_by_hash("u_1", "用户偏好   简洁回答")
    assert found is not None
    found.content = "被改坏了"
    assert (await repo.get("mem_1", "u_1")).content == "用户偏好 简洁回答"


async def test_find_by_hash_returns_none_when_absent() -> None:
    """没有同规范化内容的记录时返回 ``None``（让调用方走「新建」分支而不是报 404）。"""
    repo = InMemoryMemoryRepo()
    assert await repo.find_by_hash("u_1", "不存在的记忆") is None


async def test_touch_bumps_hit_count_and_confidence_only_upward() -> None:
    """``touch`` 累加命中次数，但置信度**只升不降**（用户明说过的话不该被弱化）。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", confidence=0.6))
    touched = await repo.touch("mem_1", confidence=0.9)
    assert touched.hit_count == 2
    # 传更低的值不应把置信度拉下来（用户明说过的话不该被弱化）
    again = await repo.touch("mem_1", confidence=0.3)
    assert again.confidence == 0.9


async def test_touch_missing_record_raises_not_found() -> None:
    """对不存在的记忆 ``touch`` ⇒ ``MEMORY_NOT_FOUND``（而不是静默插入一条）。"""
    repo = InMemoryMemoryRepo()
    with pytest.raises(AppError) as excinfo:
        await repo.touch("mem_missing")
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND


# ---------------------------------------------------------------------------
# 容量上限
# ---------------------------------------------------------------------------
async def test_eviction_drops_lowest_confidence_first() -> None:
    """超容量时丢「最不可靠且最久没动过」的那条，而不是丢最新的。"""
    repo = InMemoryMemoryRepo(max_items=2)
    await repo.add(_record("mem_low", confidence=0.5, created_at="2026-09-28T10:00:00.000Z"))
    await repo.add(_record("mem_mid", confidence=0.8, created_at="2026-09-28T10:01:00.000Z"))
    await repo.add(_record("mem_high", confidence=1.0, created_at="2026-09-28T10:02:00.000Z"))

    assert await repo.count("u_1") == 2
    with pytest.raises(AppError):
        await repo.get("mem_low", "u_1")
    assert (await repo.get("mem_high", "u_1")).confidence == 1.0


async def test_eviction_clears_unique_index() -> None:
    """被淘汰的记录必须从唯一索引里摘掉，否则同样的内容再也写不进来。"""
    repo = InMemoryMemoryRepo(max_items=1)
    await repo.add(_record("mem_1", content="旧内容", confidence=0.5))
    await repo.add(_record("mem_2", content="新内容", confidence=1.0))
    assert await repo.find_by_hash("u_1", "旧内容") is None
    again = await repo.add(_record("mem_3", content="旧内容", confidence=1.0))
    assert again.created is True


# ---------------------------------------------------------------------------
# 列表 / 分页 / 归属
# ---------------------------------------------------------------------------
async def test_list_page_sorts_by_cursor_position_desc() -> None:
    """列表按 ``(created_at, id)`` 倒序 —— 最新写入的在第一页最前面。"""
    repo = InMemoryMemoryRepo()
    for index, stamp in enumerate(
        [
            "2026-09-28T10:00:00.000Z",
            "2026-09-28T10:02:00.000Z",
            "2026-09-28T10:01:00.000Z",
        ]
    ):
        await repo.add(_record(f"mem_{index}", created_at=stamp))
    items, has_more = await repo.list_page("u_1", limit=10)
    assert [item.created_at for item in items] == [
        "2026-09-28T10:02:00.000Z",
        "2026-09-28T10:01:00.000Z",
        "2026-09-28T10:00:00.000Z",
    ]
    assert has_more is False


async def test_list_page_cursor_does_not_skip_or_repeat() -> None:
    """翻页不重不漏：游标用 ``(created_at, id)`` 位置，与排序键同源。"""
    repo = InMemoryMemoryRepo()
    for index in range(5):
        await repo.add(_record(f"mem_{index}", created_at=f"2026-09-28T10:0{index}:00.000Z"))
    first_page, has_more = await repo.list_page("u_1", limit=2)
    assert has_more is True
    cursor = encode_memory_cursor(first_page[-1])
    second_page, _ = await repo.list_page("u_1", limit=2, cursor=cursor)

    seen = [item.id for item in first_page] + [item.id for item in second_page]
    assert len(set(seen)) == len(seen) == 4


async def test_list_page_filters_by_kind_and_expired() -> None:
    """``kind`` 与 ``expired`` 是**独立**筛选项，可分别单独生效、也可组合。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_p", kind="preference"))
    await repo.add(_record("mem_f", kind="fact"))
    expired = _record("mem_e", kind="fact")
    expired.expired = True
    await repo.add(expired)

    preferences, _ = await repo.list_page("u_1", kind="preference")
    assert [item.id for item in preferences] == ["mem_p"]
    active_facts, _ = await repo.list_page("u_1", kind="fact", expired=False)
    assert [item.id for item in active_facts] == ["mem_f"]
    expired_only, _ = await repo.list_page("u_1", expired=True)
    assert [item.id for item in expired_only] == ["mem_e"]


async def test_cursor_position_matches_sort_key() -> None:
    """显式锁死「排序键 = 游标比较键」这条不变量。"""
    record = _record("mem_1")
    position = cursor_position(record.created_at, record.id)
    assert position[0].isoformat().startswith("2026-09-28T10:00:00")


async def test_get_across_users_is_not_found() -> None:
    """跨用户一律 404（403 等于确认这个 ID 存在）。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", user_id="u_1"))
    with pytest.raises(AppError) as excinfo:
        await repo.get("mem_1", "u_2")
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND


# ---------------------------------------------------------------------------
# 更新 / 删除
# ---------------------------------------------------------------------------
async def test_save_updates_unique_index_when_content_changes() -> None:
    """改正文后唯一索引必须跟着走，否则「改回原文」会被判成重复而静默丢弃。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", content="旧内容"))
    updated = await repo.get("mem_1", "u_1")
    updated.content = "新内容"
    updated.content_sha256 = content_sha256("新内容")
    await repo.save(updated)

    assert await repo.find_by_hash("u_1", "旧内容") is None
    assert await repo.find_by_hash("u_1", "新内容") is not None

    reverted = await repo.get("mem_1", "u_1")
    reverted.content = "旧内容"
    reverted.content_sha256 = content_sha256("旧内容")
    await repo.save(reverted)
    # 改回原文仍应是同一条（而不是新建一条重复记录）
    assert (await repo.find_by_hash("u_1", "旧内容")).id == "mem_1"  # type: ignore[union-attr]


async def test_save_missing_record_raises_not_found() -> None:
    """``save`` 是纯更新：记录不存在 ⇒ ``MEMORY_NOT_FOUND``，绝不 upsert。"""
    repo = InMemoryMemoryRepo()
    with pytest.raises(AppError) as excinfo:
        await repo.save(_record("mem_missing"))
    assert excinfo.value.code is ErrorCode.MEMORY_NOT_FOUND


async def test_delete_removes_row_and_index_entry() -> None:
    """删除要同时清掉唯一索引项，否则同样的内容以后再也写不进来。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", content="待删除内容"))
    removed = await repo.delete("mem_1", "u_1")
    assert removed.id == "mem_1"
    assert await repo.count("u_1") == 0
    assert await repo.find_by_hash("u_1", "待删除内容") is None


async def test_delete_all_is_idempotent_and_user_scoped() -> None:
    """``delete_all`` 只清自己的记录、返回实际删除条数，重复调用返回 0 而不报错。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1", user_id="u_1"))
    await repo.add(_record("mem_2", user_id="u_1"))
    await repo.add(_record("mem_3", user_id="u_2"))

    assert await repo.delete_all("u_1") == 2
    assert await repo.delete_all("u_1") == 0
    assert await repo.count("u_2") == 1


async def test_count_active_only_skips_expired() -> None:
    """``active_only=True`` 的口径是"排除已过期"，默认口径则统计全部（含过期）。"""
    repo = InMemoryMemoryRepo()
    await repo.add(_record("mem_1"))
    expired = _record("mem_2")
    expired.expired = True
    await repo.add(expired)
    assert await repo.count("u_1") == 2
    assert await repo.count("u_1", active_only=True) == 1


# ---------------------------------------------------------------------------
# 过期
# ---------------------------------------------------------------------------
def test_expiring_marks_only_due_records() -> None:
    """只有 ``expires_at`` 已到期且尚未标记的记录被标记；永久记录（无期限）永不失效。"""
    due = _record("mem_due", expires_at="2026-09-28T09:00:00.000Z")
    future = _record("mem_future", expires_at="2099-01-01T00:00:00.000Z")
    permanent = _record("mem_forever")
    touched = expiring([due, future, permanent], "2026-09-28T10:00:00.000Z")
    assert [item.id for item in touched] == ["mem_due"]
    assert due.expired is True
    assert future.expired is False
    assert permanent.expired is False


def test_expiring_is_idempotent() -> None:
    """已经标过的不要再返回（否则每轮都会重复写库）。"""
    record = _record("mem_due", expires_at="2026-09-28T09:00:00.000Z")
    record.expired = True
    assert expiring([record], "2026-09-28T10:00:00.000Z") == []


def test_is_expired_at_handles_missing_deadline() -> None:
    """没有 ``expires_at`` 的记录在任何时刻都不算过期（``None`` 不是"立刻过期"）。"""
    assert _record("mem_1").is_expired_at("2099-01-01T00:00:00.000Z") is False
