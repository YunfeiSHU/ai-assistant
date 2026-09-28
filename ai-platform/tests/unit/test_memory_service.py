"""长期记忆用例编排单测（``REQ-MEM-004`` ~ ``REQ-MEM-007``）。

这里测的是**三层去重与双写一致性**：

| 场景 | 期望 |
| --- | --- |
| 完全相同 | 不新建，``created=False`` 且 ``hit_count`` +1 |
| 相似度 ≥ 0.92 | 合并成一条（用较新正文覆盖） |
| 相似度 ∈ [0.85, 0.92) | **两条并存**（宁可冗余也不丢信息） |

向量由 :class:`ScriptedEmbedding` 给定，而不是靠真实模型 —— 否则「0.92 阈值」这类
断言只能靠碰运气命中边界，用例会随模型版本偶发失败。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.conftest import build_settings
from tests.support.fake_llm import FakeLLM
from tests.support.memory import BASE as _BASE
from tests.support.memory import DEFAULT_DIM, ScriptedEmbedding
from tests.support.memory import blend as _blend
from tests.support.memory import direction as _direction

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.memory.context_store import InMemoryConversationStore, StoredMessage
from app.memory.extractor import MemoryExtractor
from app.memory.long_term import InMemoryMemoryRepo
from app.memory.preferences import InMemoryMemoryPreferenceStore
from app.memory.summary import SummaryBuilder
from app.memory.vector_index import InMemoryMemoryVectorIndex
from app.services.context import ContextAssembler
from app.services.memory import DEFAULT_MANUAL_CONFIDENCE, MemoryService

DIM = DEFAULT_DIM


def _service(
    settings: Settings | None = None,
    *,
    embedding: ScriptedEmbedding | None = None,
    extractor_llm: FakeLLM | None = None,
    summary_llm: FakeLLM | None = None,
    clock: Any = None,
) -> MemoryService:
    resolved = settings or build_settings()
    store = InMemoryConversationStore(resolved)
    return MemoryService(
        resolved,
        InMemoryMemoryRepo(max_items=resolved.memory_max_items),
        InMemoryMemoryVectorIndex(dim=DIM),
        store,
        embedding or ScriptedEmbedding(),
        ContextAssembler(resolved),
        extractor=MemoryExtractor(resolved, extractor_llm or FakeLLM(replies=["[]"])),
        summary_builder=SummaryBuilder(resolved, summary_llm or FakeLLM(replies=["[]"]), store),
        preferences=InMemoryMemoryPreferenceStore(),
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 写入：三层去重
# ---------------------------------------------------------------------------
async def test_first_write_creates_record() -> None:
    service = _service()
    result = await service.remember("用户偏好简洁回答", user_id="u_1", kind="preference")
    assert result.created is True
    assert result.record.id.startswith("mem_")
    assert result.record.confidence == DEFAULT_MANUAL_CONFIDENCE
    assert await service.repo.count("u_1") == 1
    assert await service.index.count(user_id="u_1") == 1


async def test_hash_hit_returns_created_false_and_bumps_hit_count() -> None:
    """同一句话写两次 → 1 条，``created=False``，``hit_count=2``（``AC-MEM-08``）。"""
    service = _service()
    await service.remember("用户偏好简洁回答", user_id="u_1")
    second = await service.remember("  用户偏好简洁回答  ", user_id="u_1")
    assert second.created is False
    assert second.record.hit_count == 2
    assert await service.repo.count("u_1") == 1


async def test_semantic_dedupe_merges_above_threshold() -> None:
    """相似度 ≥ 0.92 视为同一件事：合并、不新增，正文取较新的。"""
    settings = build_settings()
    embedding = ScriptedEmbedding({"用户偏好简洁回答": _BASE, "用户偏好简短回答": _direction(0.99)})
    service = _service(settings, embedding=embedding)
    first = await service.remember("用户偏好简洁回答", user_id="u_1", kind="preference")
    merged = await service.remember("用户偏好简短回答", user_id="u_1", kind="preference")

    assert merged.created is False
    assert merged.record.id == first.record.id
    assert merged.record.content == "用户偏好简短回答"
    assert merged.record.hit_count == 2
    assert await service.repo.count("u_1") == 1


async def test_similar_but_distinct_memories_coexist() -> None:
    """相似度落在 [0.85, 0.92) → 两条并存（``REQ-MEM-005``）。"""
    embedding = ScriptedEmbedding({"用户偏好简洁回答": _BASE, "用户常驻上海": _direction(0.88)})
    service = _service(embedding=embedding)
    await service.remember("用户偏好简洁回答", user_id="u_1")
    other = await service.remember("用户常驻上海", user_id="u_1")
    assert other.created is True
    assert await service.repo.count("u_1") == 2
    assert await service.index.count(user_id="u_1") == 2


async def test_merge_recomputes_content_hash() -> None:
    """合并后唯一索引必须指向**新**正文。

    这是 ``dataclasses.replace`` 最容易踩的坑：它会把旧 ``content_sha256`` 一起带上，
    而 ``__post_init__`` 只在字段为空时才推导 —— 于是索引指向旧哈希，
    「改回原文」会被判成重复而静默丢弃。
    """
    embedding = ScriptedEmbedding({"用户偏好简洁回答": _BASE, "用户偏好简短回答": _direction(0.99)})
    service = _service(embedding=embedding)
    await service.remember("用户偏好简洁回答", user_id="u_1")
    await service.remember("用户偏好简短回答", user_id="u_1")

    assert await service.repo.find_by_hash("u_1", "用户偏好简短回答") is not None
    assert await service.repo.find_by_hash("u_1", "用户偏好简洁回答") is None


async def test_orphan_vector_does_not_block_creation() -> None:
    """索引里有、关系库里没有（容量淘汰后的残留）时应继续新建，而不是报错。"""
    service = _service()
    await service.index.upsert("mem_ghost", "u_1", _BASE)
    result = await service.remember("用户偏好简洁回答", user_id="u_1")
    assert result.created is True


async def test_write_rejects_too_short_and_too_long() -> None:
    settings = build_settings(memory_content_min_chars=5, memory_content_max_chars=20)
    service = _service(settings)
    with pytest.raises(AppError) as short:
        await service.remember("短", user_id="u_1")
    assert short.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AppError) as long:
        await service.remember("很长的偏好" * 10, user_id="u_1")
    assert long.value.code is ErrorCode.INVALID_ARGUMENT


async def test_write_rejects_past_expiry() -> None:
    """``expires_at`` 必须晚于当前时间，否则会存一条永不生效的记忆。"""
    service = _service()
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(AppError) as excinfo:
        await service.remember("用户偏好简洁回答", user_id="u_1", expires_at=past)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


async def test_write_accepts_future_expiry() -> None:
    service = _service()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    result = await service.remember("用户偏好简洁回答", user_id="u_1", expires_at=future)
    assert result.record.expires_at == future


async def test_write_rejects_malformed_expiry() -> None:
    service = _service()
    with pytest.raises(AppError) as excinfo:
        await service.remember("用户偏好简洁回答", user_id="u_1", expires_at="明天")
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# 检索与注入
# ---------------------------------------------------------------------------
async def test_search_filters_by_score_threshold() -> None:
    """低于 ``memory_score_threshold`` 的候选不注入（``docs/07`` §5.3）。"""
    settings = build_settings(memory_score_threshold=0.45)
    embedding = ScriptedEmbedding({"查询文本": _BASE, "用户偏好简洁回答": _direction(0.9)})
    service = _service(settings, embedding=embedding)
    await service.remember("用户偏好简洁回答", user_id="u_1")

    hits = await service.search("查询文本", "u_1")
    assert [item.content for item in hits] == ["用户偏好简洁回答"]

    far = ScriptedEmbedding({"查询文本": _direction(0.1), "用户偏好简洁回答": _BASE})
    other = _service(settings, embedding=far)
    await other.remember("用户偏好简洁回答", user_id="u_1")
    assert await other.search("查询文本", "u_1") == []


async def test_search_respects_top_n_preference() -> None:
    contents = [f"用户的第 {index} 条稳定偏好" for index in range(5)]
    # 5 条记忆彼此正交（不会互相合并），但都与查询向量保持 0.6 的相似度
    embedding = ScriptedEmbedding(
        {"任意查询": _BASE, **{text: _blend(index, 0.6) for index, text in enumerate(contents)}}
    )
    service = _service(embedding=embedding)
    for text in contents:
        await service.remember(text, user_id="u_1")
    assert await service.repo.count("u_1") == 5

    await service.update_preference("u_1", top_n=2)
    assert len(await service.search("任意查询", "u_1")) == 2


async def test_search_returns_empty_when_disabled() -> None:
    """能力关闭时**不读**（``docs/07`` §5.4）。"""
    service = _service()
    await service.remember("用户偏好简洁回答", user_id="u_1")
    await service.update_preference("u_1", enabled=False)
    assert await service.search("任意查询", "u_1") == []


async def test_search_skips_orphan_and_expired_records() -> None:
    """索引里的「幽灵」向量与已过期记忆都不能进入上下文。

    这里刻意让两个向量都与查询保持高相似度，否则「返回空」可能只是被阈值过滤，
    测不到真正要测的跳过逻辑。
    """
    embedding = ScriptedEmbedding({"任意查询": _BASE, "用户偏好简洁回答": _BASE})
    service = _service(embedding=embedding)
    await service.index.upsert("mem_ghost", "u_1", _BASE)
    record = (await service.remember("用户偏好简洁回答", user_id="u_1")).record
    assert len(await service.search("任意查询", "u_1")) == 1

    expired = await service.update(record.id, "u_1", expires_at="2020-01-01T00:00:00.000Z")
    assert expired.expired is True
    assert await service.search("任意查询", "u_1") == []


async def test_search_empty_query_returns_nothing() -> None:
    service = _service()
    await service.remember("用户偏好简洁回答", user_id="u_1")
    assert await service.search("   ", "u_1") == []


# ---------------------------------------------------------------------------
# 偏好
# ---------------------------------------------------------------------------
async def test_preference_defaults_follow_global_switch() -> None:
    """全局关掉记忆时，未设置过偏好的用户默认也是关。"""
    settings = build_settings(memory_enabled=False, memory_top_n=4)
    service = _service(settings)
    preference = await service.preference("u_1")
    assert preference.enabled is False
    assert preference.top_n == 4


async def test_update_preference_clamps_top_n() -> None:
    service = _service()
    assert (await service.update_preference("u_1", top_n=0)).top_n == 1
    assert (await service.update_preference("u_1", top_n=99)).top_n == 20


async def test_update_preference_keeps_other_field() -> None:
    service = _service()
    await service.update_preference("u_1", enabled=False, top_n=5)
    kept = await service.update_preference("u_1", top_n=2)
    assert kept.enabled is False
    assert kept.top_n == 2


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
async def test_list_memories_returns_cursor() -> None:
    settings = build_settings()
    service = _service(settings)
    for index in range(3):
        await service.remember(f"用户的第 {index} 条稳定偏好", user_id="u_1")
    # 未登记的文本拿到正交方向 → 三条不会被语义去重合并
    assert await service.repo.count("u_1") == 3

    items, next_cursor = await service.list_memories("u_1", limit=2)
    assert len(items) == 2
    assert next_cursor is not None
    rest, cursor_again = await service.list_memories("u_1", limit=2, cursor=next_cursor)
    assert len(rest) == 1
    assert cursor_again is None


async def test_list_memories_marks_due_records_expired() -> None:
    """列出前先跑到期标记：否则 ``expired=false`` 会把到期的也算成有效。"""
    service = _service()
    record = (await service.remember("用户偏好简洁回答", user_id="u_1")).record
    await service.update(record.id, "u_1", expires_at="2020-01-01T00:00:00.000Z")
    # ``update`` 会重算 expired；这里直接改回未标记状态模拟「时间流逝」
    stored = await service.repo.get(record.id, "u_1")
    stored.expired = False
    await service.repo.save(stored)

    assert await service.expire_due("u_1") == 1
    assert (await service.get(record.id, "u_1")).expired is True


async def test_update_content_revectorises() -> None:
    """改正文必须重新向量化，否则检索命中的还是旧语义。"""
    settings = build_settings()
    embedding = ScriptedEmbedding({"新内容": _direction(0.0)})
    service = _service(settings, embedding=embedding)
    record = (await service.remember("用户偏好简洁回答", user_id="u_1")).record
    await service.update(record.id, "u_1", content="用户不喜欢列表式回答")

    hits = await service.index.search(_direction(0.0), user_id="u_1", top_k=1)
    assert hits[0].mem_id == record.id


async def test_update_without_content_keeps_vector() -> None:
    service = _service()
    record = (await service.remember("用户偏好简洁回答", user_id="u_1")).record
    updated = await service.update(record.id, "u_1", kind="preference")
    assert updated.kind == "preference"
    assert updated.content == record.content


async def test_update_clear_expiry() -> None:
    service = _service()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    record = (await service.remember("用户偏好简洁回答", user_id="u_1", expires_at=future)).record
    cleared = await service.update(record.id, "u_1", clear_expiry=True)
    assert cleared.expires_at is None
    assert cleared.expired is False


async def test_delete_removes_from_repo_and_index() -> None:
    """双删：只删关系库会留下「列表里没有、检索还能命中」的幽灵（``REQ-MEM-007``）。"""
    service = _service()
    record = (await service.remember("用户偏好简洁回答", user_id="u_1")).record
    await service.delete(record.id, "u_1")
    assert await service.repo.count("u_1") == 0
    assert await service.index.count(user_id="u_1") == 0


async def test_delete_all_clears_both_sides_and_marks_cooldown() -> None:
    clock = {"value": datetime.now(UTC)}
    service = _service(clock=lambda: clock["value"])
    for index in range(3):
        await service.remember(f"用户的第 {index} 条稳定偏好", user_id="u_1")

    assert await service.delete_all("u_1") == 3
    assert await service.repo.count("u_1") == 0
    assert await service.index.count(user_id="u_1") == 0
    preference = await service.preference("u_1")
    assert preference.cleared_at is not None


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------
def _messages(*contents: str) -> list[StoredMessage]:
    return [
        StoredMessage(
            role="user",
            content=content,
            message_id=f"msg_{index}",
            created_at=f"2026-09-28T10:0{index}:00.000Z",
        )
        for index, content in enumerate(contents)
    ]


async def test_extract_and_store_writes_candidates() -> None:
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(extractor_llm=llm)
    created = await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1")
    assert [record.content for record in created] == ["用户偏好简洁回答"]
    assert (await service.repo.count("u_1")) == 1


async def test_extract_and_store_dedupes_repeated_candidates() -> None:
    """同一句话抽两次 → 仍 1 条（``AC-MEM-08``）。"""
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(extractor_llm=llm)
    await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1")
    await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1")
    assert await service.repo.count("u_1") == 1
    stored = (await service.list_memories("u_1"))[0][0]
    assert stored.hit_count == 2


async def test_extract_skipped_during_clear_cooldown() -> None:
    """清空后 24h 内不重新抽取旧内容（``REQ-MEM-007``）。"""
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(extractor_llm=llm)
    await service.delete_all("u_1")
    assert await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1") == []
    assert await service.repo.count("u_1") == 0


async def test_extract_resumes_after_cooldown() -> None:
    now = {"value": datetime.now(UTC)}
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(extractor_llm=llm, clock=lambda: now["value"])
    await service.delete_all("u_1")
    now["value"] = now["value"] + timedelta(hours=25)
    created = await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1")
    assert len(created) == 1


async def test_extract_skipped_when_disabled() -> None:
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(extractor_llm=llm)
    await service.update_preference("u_1", enabled=False)
    assert await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1") == []


async def test_extract_skipped_when_globally_disabled() -> None:
    settings = build_settings(memory_extract_enabled=False)
    llm = FakeLLM(
        replies=['[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]']
    )
    service = _service(settings, extractor_llm=llm)
    assert await service.extract_and_store(_messages("我喜欢简洁回答"), user_id="u_1") == []


# ---------------------------------------------------------------------------
# 上下文视图
# ---------------------------------------------------------------------------
async def test_context_overview_reports_budget() -> None:
    service = _service()
    store = service.store
    await store.ensure("cv_1", "u_1")
    await store.append("cv_1", "u_1", _messages("我喜欢简洁回答"))
    snapshot = await service.context_overview("cv_1", "u_1")
    assert len(snapshot.messages) == 1
    assert snapshot.assembled.total_tokens > 0
    assert "system" in snapshot.assembled.token_by_part
    assert snapshot.summary is None


async def test_context_overview_injects_memories() -> None:
    """带 query 时上下文里应出现独立的 memory 片段（``AC-MEM-09``）。"""
    embedding = ScriptedEmbedding({"查询文本": _BASE, "用户偏好简洁回答": _BASE})
    service = _service(embedding=embedding)
    await service.remember("用户偏好简洁回答", user_id="u_1", kind="preference")
    await service.store.ensure("cv_1", "u_1")
    snapshot = await service.context_overview("cv_1", "u_1", query="查询文本")
    assert "memory" in snapshot.assembled.part_order
    memory_part = next(part for part in snapshot.assembled.parts if part.name == "memory")
    assert "用户偏好简洁回答" in memory_part.content


async def test_context_overview_hides_long_term_memories_from_other_users() -> None:
    service = _service()
    await service.remember("用户偏好简洁回答", user_id="u_1")
    await service.store.ensure("cv_1", "u_2")
    snapshot = await service.context_overview("cv_1", "u_2", query="任意查询")
    assert "memory" not in snapshot.assembled.part_order


async def test_build_summary_returns_none_without_builder() -> None:
    settings = build_settings()
    store = InMemoryConversationStore(settings)
    service = MemoryService(
        settings,
        InMemoryMemoryRepo(),
        InMemoryMemoryVectorIndex(dim=DIM),
        store,
        ScriptedEmbedding(),
        ContextAssembler(settings),
    )
    assert await service.build_summary("cv_1", "u_1") is None
