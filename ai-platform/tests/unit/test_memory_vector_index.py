"""长期记忆向量索引单测（``REQ-MEM-005`` / ``REQ-MEM-006``）。

这个实现（进程内暴力余弦）不只是一个「测试替身」：``INFRA_BACKEND=memory`` 时它
就是真实实现。因此要测的性质是**检索语义**，而不是「它有没有被调用」：

* 按 ``user_id`` 过滤（跨用户串味是隐私事故，不是 bug）；
* 相似度必须真的反映语义（用正交向量验证 0 分，用同向向量验证 1 分）；
* 结果顺序确定（同分时不能因字典序不稳定而每次不同）。
"""

from __future__ import annotations

import pytest

from app.memory.vector_index import InMemoryMemoryVectorIndex

DIM = 4


def _unit(*values: float) -> list[float]:
    return list(values)


async def test_search_filters_by_user() -> None:
    """跨用户必须完全看不到（``REQ-MEM-006`` 的 ``user_id`` 过滤）。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_a", "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    await index.upsert("mem_b", "u_2", _unit(1.0, 0.0, 0.0, 0.0))

    hits = await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=5)
    assert [hit.mem_id for hit in hits] == ["mem_a"]


async def test_search_scores_are_cosine_similarity() -> None:
    """同向 = 1.0，正交 = 0.0，反向 = -1.0。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_same", "u_1", _unit(2.0, 0.0, 0.0, 0.0))
    await index.upsert("mem_orth", "u_1", _unit(0.0, 3.0, 0.0, 0.0))
    await index.upsert("mem_opposite", "u_1", _unit(-1.0, 0.0, 0.0, 0.0))

    hits = await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=3)
    scores = {hit.mem_id: round(hit.score, 4) for hit in hits}
    assert scores["mem_same"] == 1.0
    assert scores["mem_orth"] == 0.0
    assert scores["mem_opposite"] == -1.0


async def test_search_respects_top_k_and_is_sorted() -> None:
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_low", "u_1", _unit(0.2, 1.0, 0.0, 0.0))
    await index.upsert("mem_high", "u_1", _unit(1.0, 0.1, 0.0, 0.0))
    await index.upsert("mem_mid", "u_1", _unit(0.7, 0.3, 0.0, 0.0))

    hits = await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=2)
    assert [hit.mem_id for hit in hits] == ["mem_high", "mem_mid"]
    assert hits[0].score > hits[1].score


async def test_ties_are_deterministic_by_insertion_order() -> None:
    """同分时必须稳定排序：每次返回不同顺序会让「引用编号」类断言随机翻车。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    for name in ("mem_c", "mem_a", "mem_b"):
        await index.upsert(name, "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    first = [
        hit.mem_id for hit in await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=3)
    ]
    second = [
        hit.mem_id for hit in await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=3)
    ]
    assert first == second == ["mem_c", "mem_a", "mem_b"]


async def test_zero_vector_query_returns_empty() -> None:
    """零向量查询没有语义 → 返回空，而不是「所有记忆得分 0 都算命中」。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_1", "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    assert await index.search(_unit(0.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=1) == []


async def test_stored_zero_vector_does_not_poison_other_scores() -> None:
    """库里混进零向量时，其它条目的分数不能变成 ``nan``。

    ``nan >= 阈值`` 恒为假，一旦出现 NaN，表现为「记忆检索时好时坏」，
    而日志里什么异常都没有。
    """
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_zero", "u_1", _unit(0.0, 0.0, 0.0, 0.0))
    await index.upsert("mem_real", "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    hits = await index.search(_unit(1.0, 0.0, 0.0, 0.0), user_id="u_1", top_k=2)
    scores = {hit.mem_id: hit.score for hit in hits}
    assert scores["mem_real"] == 1.0
    assert scores["mem_zero"] == 0.0


async def test_upsert_overwrites_existing_vector() -> None:
    """同 id 重复 upsert 是覆盖（``PATCH`` 改正文后重新向量化就靠它）。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_1", "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    await index.upsert("mem_1", "u_1", _unit(0.0, 1.0, 0.0, 0.0))

    assert await index.count(user_id="u_1") == 1
    hits = await index.search(_unit(0.0, 1.0, 0.0, 0.0), user_id="u_1", top_k=1)
    assert hits[0].score == 1.0


async def test_delete_and_delete_all() -> None:
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.upsert("mem_1", "u_1", _unit(1.0, 0.0, 0.0, 0.0))
    await index.upsert("mem_2", "u_1", _unit(0.0, 1.0, 0.0, 0.0))
    await index.upsert("mem_3", "u_2", _unit(0.0, 0.0, 1.0, 0.0))

    await index.delete("mem_1")
    assert await index.count(user_id="u_1") == 1
    await index.delete_all("u_1")
    assert await index.count(user_id="u_1") == 0
    # 只清指定用户（``AC-MEM-10`` 的「Milvus 该 user_id 向量为 0」）
    assert await index.count(user_id="u_2") == 1


async def test_delete_all_is_idempotent() -> None:
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.delete_all("u_1")
    assert await index.count(user_id="u_1") == 0


async def test_delete_missing_id_is_noop() -> None:
    """重复删除不该报错（幂等），否则「删了两次」会变成一次 404 用户可见错误。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.delete("mem_missing")


async def test_upsert_rejects_dimension_mismatch() -> None:
    """维度不符必须立刻失败：静默截断/补零会让相似度全部失真。"""
    index = InMemoryMemoryVectorIndex(dim=DIM)
    with pytest.raises(ValueError):
        await index.upsert("mem_1", "u_1", _unit(1.0, 0.0))


async def test_ensure_ready_is_a_noop_for_in_memory_index() -> None:
    index = InMemoryMemoryVectorIndex(dim=DIM)
    await index.ensure_ready()
