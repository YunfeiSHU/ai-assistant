"""向量化与检索后处理单测（``docs/06`` §4.4 / §5.2）。

用 ``hash`` 向量化不是为了「跑得快」，而是为了**可断言**：同一个输入永远得到
同一个向量，所以「过滤条件、阈值、相邻合并、预算裁剪」这些逻辑才能做成确定性的
单元测试。真实语义向量的排序会随权重版本变化，把它写进断言等于埋了一颗随机失败。
"""

from __future__ import annotations

import math

import pytest
from tests.conftest import build_settings
from tests.support.fake_embedding import FakeEmbedding, FixedEmbedding

from app.core.config import Settings
from app.rag.base import RetrievedChunk
from app.rag.embedding import CachingEmbeddingProvider, build_embedding_provider
from app.rag.embedding.hash import HashEmbeddingProvider
from app.rag.reranker.base import IdentityReranker, RerankResult
from app.rag.retriever import Retriever, merge_token_budget
from app.rag.vectorstore.memory import InMemoryVectorStore


def _chunk(
    chunk_id: str,
    text: str,
    *,
    doc_id: str = "doc_1",
    index: int = 0,
    score: float = 0.5,
    page: int | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        doc_id=doc_id,
        kb_id="kb_1",
        doc_name="政策.md",
        page=page,
        score=score,
        vector_score=score,
        chunk_index=index,
        user_id="u_1",
    )


# ---------------------------------------------------------------------------
# 向量化
# ---------------------------------------------------------------------------


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    """同文本同向量、跨实例同向量（用 blake2b 而不是受 PYTHONHASHSEED 影响的内建 hash）。"""
    first = HashEmbeddingProvider(dim=64)
    second = HashEmbeddingProvider(dim=64)

    a1 = first.embed(["退款政策 7 天"])
    a2 = second.embed(["退款政策 7 天"])

    assert a1 == a2, "两个进程/实例必须得到同一个向量，否则检索会静默失配"
    assert len(a1[0]) == 64
    assert math.isclose(sum(value * value for value in a1[0]) ** 0.5, 1.0, rel_tol=1e-6)


def test_hash_embedding_separates_different_texts() -> None:
    """不同文本的相似度应明显低于相同文本（否则检索排不出序）。"""
    provider = HashEmbeddingProvider(dim=256)

    same = provider.embed(["退款政策"])[0]
    other = provider.embed(["发票抬头修改"])[0]
    same_again = provider.embed(["退款政策"])[0]

    def cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert cosine(same, same_again) == pytest.approx(1.0)
    assert cosine(same, other) < 0.6


def test_hash_embedding_handles_empty_text() -> None:
    """空文本不能产出全零向量（零向量无法归一化，检索会得到 NaN）。"""
    provider = HashEmbeddingProvider(dim=16)

    vector = provider.embed([""])[0]

    assert len(vector) == 16
    assert any(value != 0.0 for value in vector)


def test_caching_provider_hits_cache() -> None:
    """缓存按内容命中：重跑入库不该重复计算同一批向量。"""
    inner = FakeEmbedding(dim=8)
    cached = CachingEmbeddingProvider(inner, max_entries=2)

    cached.embed(["同一段文本"])
    cached.embed(["同一段文本"])

    assert cached.hits == 1
    assert cached.misses == 1
    assert len(inner.calls) == 1, "第二次不该再调内层"


def test_caching_provider_evicts_oldest() -> None:
    """LRU 有界：超过容量后淘汰最久未用的条目，不能无限增长。"""
    cached = CachingEmbeddingProvider(FakeEmbedding(dim=4), max_entries=2)

    cached.embed(["a"])
    cached.embed(["b"])
    cached.embed(["c"])  # 触发淘汰
    cached.embed(["a"])  # 已被淘汰 → 又算一次

    assert cached.misses == 4
    assert cached.hits == 0


def test_build_embedding_provider_respects_cache_switch() -> None:
    """``embedding_cache_enabled=false`` 时不应套缓存层。"""
    with_cache = build_embedding_provider(build_settings(embedding_provider="hash"))
    without = build_embedding_provider(
        build_settings(embedding_provider="hash", embedding_cache_enabled=False)
    )

    assert isinstance(with_cache, CachingEmbeddingProvider)
    assert isinstance(without, HashEmbeddingProvider)


def test_build_embedding_provider_dim_follows_settings() -> None:
    """维度来自配置：必须与 Milvus 集合声明一致（``VECTOR_DIM_MISMATCH`` 的根因）。"""
    provider = build_embedding_provider(
        build_settings(embedding_provider="hash", embedding_dim=64, milvus_vector_dim=64)
    )

    assert provider.dim == 64


# ---------------------------------------------------------------------------
# 向量库
# ---------------------------------------------------------------------------


async def test_memory_vector_store_filters_by_user_and_kb() -> None:
    """召回阶段就按 ``user_id``/``kb_id`` 过滤，而不是取回后再筛。"""
    store = InMemoryVectorStore(dim=4)
    await store.upsert(
        [
            _chunk("chk_1", "我的文档"),
            RetrievedChunk(
                chunk_id="chk_2", text="别人的文档", doc_id="doc_2", kb_id="kb_1", user_id="u_2"
            ),
            RetrievedChunk(
                chunk_id="chk_3", text="另一个库", doc_id="doc_3", kb_id="kb_9", user_id="u_1"
            ),
        ],
        [[1.0, 0.0, 0.0, 0.0]] * 3,
    )

    hits = await store.search([1.0, 0.0, 0.0, 0.0], user_id="u_1", kb_ids=["kb_1"], top_k=10)

    assert [item.chunk_id for item in hits] == ["chk_1"]


async def test_memory_vector_store_upsert_is_idempotent() -> None:
    """同 ``chunk_id`` 重复 upsert 不产生重复条目（``REQ-TASK-002`` 的幂等基础）。"""
    store = InMemoryVectorStore(dim=4)
    payload = _chunk("chk_1", "内容")

    await store.upsert([payload], [[1.0, 0.0, 0.0, 0.0]])
    await store.upsert([payload], [[0.0, 1.0, 0.0, 0.0]])

    assert await store.count(user_id="u_1", kb_ids=["kb_1"]) == 1
    hits = await store.search([0.0, 1.0, 0.0, 0.0], user_id="u_1", top_k=5)
    assert len(hits) == 1


async def test_memory_vector_store_zero_vector_query_returns_nothing() -> None:
    """零向量查询返回空（而不是 NaN 分数排序出的随机结果）。"""
    store = InMemoryVectorStore(dim=4)
    await store.upsert([_chunk("chk_1", "内容")], [[1.0, 0.0, 0.0, 0.0]])

    assert await store.search([0.0, 0.0, 0.0, 0.0], user_id="u_1", top_k=5) == []


# ---------------------------------------------------------------------------
# 检索后处理
# ---------------------------------------------------------------------------


def _retriever(store: InMemoryVectorStore, embedding: object) -> Retriever:
    return Retriever(
        settings=build_settings(reranker_enabled=False),
        embedding=embedding,  # type: ignore[arg-type]
        vector_store=store,
        reranker=IdentityReranker(),
    )


async def test_adjacent_chunks_are_merged() -> None:
    """同文档相邻（``chunk_index`` 差 ≤ 1）的命中被合并（``docs/06`` §5.2 第 4 条）。"""
    store = InMemoryVectorStore(dim=2)
    await store.upsert(
        [
            _chunk("chk_1", "第一段。", index=0, score=0.9),
            _chunk("chk_2", "第二段。", index=1, score=0.8),
            _chunk("chk_9", "很远的一段。", index=9, score=0.7),
        ],
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
    )
    retriever = _retriever(store, FixedEmbedding([1.0, 0.0], {}))

    result = await retriever.retrieve_detailed(
        query="任意", user_id="u_1", kb_ids=["kb_1"], top_k=10, rerank_top_n=10
    )

    ids = [item.chunk_id for item in result.items]
    assert "chk_1" in ids and "chk_9" in ids
    assert "chk_2" not in ids, "相邻两条应当合并成一条，而不是各留一条"
    merged = next(item for item in result.items if item.merged)
    assert "第一段。" in merged.text and "第二段。" in merged.text
    assert merged.chunk_id == "chk_1", "合并后保留较小的 chunk_id"
    assert merged.merged is True


async def test_threshold_applies_after_rerank() -> None:
    """阈值在重排之后应用；重排不可用时按向量分过滤（``docs/06`` §5.2 第 3 条）。"""
    store = InMemoryVectorStore(dim=2)
    await store.upsert(
        [_chunk("chk_1", "高分", score=0.0), _chunk("chk_2", "低分", score=0.0)],
        [[1.0, 0.0], [0.0, 1.0]],
    )
    retriever = _retriever(store, FixedEmbedding([1.0, 0.0], {}))

    result = await retriever.retrieve_detailed(
        query="任意",
        user_id="u_1",
        kb_ids=["kb_1"],
        top_k=10,
        rerank_top_n=10,
        score_threshold=0.5,
    )

    assert [item.chunk_id for item in result.items] == ["chk_1"]
    assert result.rerank_used is False


async def test_rerank_failure_degrades_to_vector_scores() -> None:
    """重排抛错时按向量分返回，且 ``rerank_used=false``（``REQ-RAG-006``）。"""

    class BoomReranker:
        async def rerank(self, query: str, documents: list[str], *, top_n: int) -> RerankResult:
            raise RuntimeError("模型加载失败")

    store = InMemoryVectorStore(dim=2)
    await store.upsert([_chunk("chk_1", "内容", score=0.0)], [[1.0, 0.0]])
    retriever = Retriever(
        settings=build_settings(),
        embedding=FixedEmbedding([1.0, 0.0], {}),  # type: ignore[arg-type]
        vector_store=store,
        reranker=BoomReranker(),  # type: ignore[arg-type]
    )

    result = await retriever.retrieve_detailed(
        query="任意", user_id="u_1", kb_ids=["kb_1"], top_k=5, rerank_top_n=5
    )

    assert result.rerank_used is False
    assert result.items, "重排坏掉不应该把检索结果也丢掉"
    assert result.items[0].score == pytest.approx(result.items[0].vector_score)


async def test_vector_score_is_preserved_when_rerank_applies() -> None:
    """重排后 ``score`` 是重排分，``vector_score`` 仍是向量分（``/search`` 要同时给出）。"""

    class Reranker:
        async def rerank(self, query: str, documents: list[str], *, top_n: int) -> RerankResult:
            return RerankResult(
                ranked=[(index, 0.9 - index * 0.1) for index in range(len(documents))]
            )

    store = InMemoryVectorStore(dim=2)
    await store.upsert([_chunk("chk_1", "内容", score=0.0)], [[1.0, 0.0]])
    retriever = Retriever(
        settings=build_settings(),
        embedding=FixedEmbedding([1.0, 0.0], {}),  # type: ignore[arg-type]
        vector_store=store,
        reranker=Reranker(),  # type: ignore[arg-type]
    )

    result = await retriever.retrieve_detailed(
        query="任意", user_id="u_1", kb_ids=["kb_1"], top_k=5, rerank_top_n=5
    )

    item = result.items[0]
    assert result.rerank_used is True
    assert item.score == pytest.approx(0.9)
    assert item.vector_score == pytest.approx(1.0, abs=1e-6)


def test_merge_token_budget_keeps_at_least_one() -> None:
    """预算裁剪按分数从低到高丢，但**至少留一条**（否则「有资料」会变成「没资料」）。"""
    chunks = [
        _chunk("chk_1", "a" * 10, score=0.9),
        _chunk("chk_2", "b" * 10, score=0.8),
        _chunk("chk_3", "c" * 10, score=0.7),
    ]

    kept, trimmed = merge_token_budget(chunks, budget=1)

    assert len(kept) == 1
    assert kept[0].chunk_id == "chk_1", "留下的应当是分数最高的那条"
    assert trimmed is True


def test_merge_token_budget_keeps_all_when_within_budget() -> None:
    """预算充裕时不做任何裁剪。"""
    chunks = [_chunk("chk_1", "短文本", score=0.9)]

    kept, trimmed = merge_token_budget(chunks, budget=10_000)

    assert len(kept) == 1
    assert trimmed is False


def test_build_settings_is_importable_from_conftest() -> None:
    """``build_settings`` 是测试基线配置的唯一入口（避免各处硬编码 JWT secret）。"""
    assert isinstance(build_settings(), Settings)
