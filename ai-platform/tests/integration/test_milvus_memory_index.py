"""Milvus 长期记忆向量索引的集成测试（``docs/09`` §3.2，``REQ-MEM-005/006``）。

要证明的三件事，都是内存实现**证明不了**、且失败时**不会报错**的那种：

1. **集合真的建出来了吗**：``FLOAT_VECTOR`` 维度、``HNSW`` + ``COSINE``。
2. **标量索引真的建了吗**：``user_id`` / ``kind`` 的 ``INVERTED``。
   缺了它 Milvus 会「先取 top-k 再过滤」，于是本人的记忆被别人的向量挤掉 ——
   表现为「库里明明有这条，但很少被检索到」，随数据量增长越来越明显，且不报错。
3. **``user_id`` 过滤真的生效吗**：租户隔离（``REQ-DATA-006``）在向量库这一侧
   没有数据库外键可用，只能靠过滤表达式，所以必须有「A 搜不到 B」的断言。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.ids import new_id

pytestmark = pytest.mark.usefixtures("milvus_settings")


@pytest.fixture
async def vector_cleanup(memory_index: Any, test_user: str) -> Any:
    """用例结束后删掉本用例写入的向量（只按自己的 ``user_id``）。"""
    yield test_user
    await memory_index.delete_all(test_user)


def _vec(dim: int, value: float = 0.5) -> list[float]:
    """等值向量：与自身余弦相似度恒为 1，断言不依赖随机数。"""
    return [value] * dim


async def test_ensure_ready_is_idempotent_and_builds_indexes(
    memory_index: Any, milvus_settings: Any
) -> None:
    """第二次 ``ensure_ready`` 不报错；集合带两个标量索引（幂等的判据）。"""
    await memory_index.ensure_ready()  # 第二次
    client = memory_index._client
    collection = milvus_settings.milvus_memory_collection

    assert client.has_collection(collection)
    indexes = set(client.list_indexes(collection))
    assert {"vector", "user_id", "kind"} <= indexes

    vector_index = client.describe_index(collection, "vector")
    assert vector_index["index_type"] == "HNSW"
    assert vector_index["metric_type"] == "COSINE"

    for field in ("user_id", "kind"):
        assert client.describe_index(collection, field)["index_type"] == "INVERTED"


async def test_upsert_then_search_hits_itself(
    memory_index: Any, vector_cleanup: str, milvus_settings: Any
) -> None:
    """写入后能被自己检索到（同一份向量 → 相似度 ≈ 1）。"""
    dim = milvus_settings.milvus_vector_dim
    mem_id = new_id("mem")
    await memory_index.upsert(mem_id, vector_cleanup, _vec(dim, 0.7), kind="fact")
    memory_index._client.flush(milvus_settings.milvus_memory_collection)

    hits = await memory_index.search(_vec(dim, 0.7), user_id=vector_cleanup, top_k=3)
    assert mem_id in [hit.mem_id for hit in hits]
    assert hits[0].score == pytest.approx(1.0, abs=1e-3)


async def test_search_is_isolated_by_user(
    memory_index: Any, vector_cleanup: str, milvus_settings: Any
) -> None:
    """别人的向量 MUST NOT 出现在我的检索结果里（租户隔离）。"""
    dim = milvus_settings.milvus_vector_dim
    other = new_id("u")
    other_mem = new_id("mem")
    await memory_index.upsert(other_mem, other, _vec(dim, 0.9), kind="fact")
    memory_index._client.flush(milvus_settings.milvus_memory_collection)

    try:
        # 用与别人**完全相同**的查询向量：如果过滤失效，一定会命中别人那条
        hits = await memory_index.search(_vec(dim, 0.9), user_id=vector_cleanup, top_k=5)
        assert other_mem not in [hit.mem_id for hit in hits]
    finally:
        await memory_index.delete_all(other)


async def test_upsert_is_keyed_by_mem_id(
    memory_index: Any, vector_cleanup: str, milvus_settings: Any
) -> None:
    """同一 ``mem_id`` 重复 upsert 不产生重复向量（重跑抽取必须幂等）。"""
    dim = milvus_settings.milvus_vector_dim
    mem_id = new_id("mem")
    await memory_index.upsert(mem_id, vector_cleanup, _vec(dim, 0.3), kind="fact")
    await memory_index.upsert(mem_id, vector_cleanup, _vec(dim, 0.4), kind="preference")
    memory_index._client.flush(milvus_settings.milvus_memory_collection)

    assert await memory_index.count(user_id=vector_cleanup) == 1


async def test_wrong_dimension_fails_loudly(memory_index: Any, vector_cleanup: str) -> None:
    """维度不符必须当场失败，而不是写进去后检索时才出问题。"""
    with pytest.raises(ValueError, match="向量维度不符"):
        await memory_index.upsert(new_id("mem"), vector_cleanup, [0.1, 0.2])


async def test_zero_vector_returns_nothing(
    memory_index: Any, vector_cleanup: str, milvus_settings: Any
) -> None:
    """零向量查询没有语义 → 空结果（而不是「人人都相似度 0」的一堆命中）。"""
    dim = milvus_settings.milvus_vector_dim
    await memory_index.upsert(new_id("mem"), vector_cleanup, _vec(dim, 0.6), kind="fact")
    assert await memory_index.search([0.0] * dim, user_id=vector_cleanup, top_k=3) == []


async def test_delete_and_delete_all_are_idempotent(
    memory_index: Any, vector_cleanup: str, milvus_settings: Any
) -> None:
    """单删与全删都幂等；删完 ``count`` 归零。"""
    dim = milvus_settings.milvus_vector_dim
    first = new_id("mem")
    second = new_id("mem")
    await memory_index.upsert(first, vector_cleanup, _vec(dim, 0.2), kind="fact")
    await memory_index.upsert(second, vector_cleanup, _vec(dim, 0.8), kind="fact")
    memory_index._client.flush(milvus_settings.milvus_memory_collection)
    assert await memory_index.count(user_id=vector_cleanup) == 2

    await memory_index.delete(first)
    await memory_index.delete(first)  # 幂等
    memory_index._client.flush(milvus_settings.milvus_memory_collection)
    assert await memory_index.count(user_id=vector_cleanup) == 1

    await memory_index.delete_all(vector_cleanup)
    await memory_index.delete_all(vector_cleanup)
    memory_index._client.flush(milvus_settings.milvus_memory_collection)
    assert await memory_index.count(user_id=vector_cleanup) == 0


def test_build_memory_vector_index_uses_milvus_when_real(milvus_settings: Any) -> None:
    """``INFRA_BACKEND=real`` → Milvus 实现（构造期不连接，所以没有可用性前置条件）。"""
    from app.memory import build_memory_vector_index

    index = build_memory_vector_index(milvus_settings)
    assert index.__class__.__name__ == "MilvusMemoryVectorIndex"
    assert index.dim == milvus_settings.milvus_vector_dim
