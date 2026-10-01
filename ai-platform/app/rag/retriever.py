"""检索管线：召回 → 重排 → 阈值 → 相邻合并（``docs/06`` §5.2，``REQ-RAG-006``）。

``docs/06`` §5.2 的六条规则在这里逐条落地。值得单独说明的是**步骤顺序**：

* **阈值必须在重排之后**。向量余弦相似度与交叉编码器打分虽然都大致落在 0..1，
  但分布不同：对着向量分用 0.5 做阈值会大面积误杀（余弦 0.7 其实算很相关），
  而对着重排分用 0.5 才符合直觉。同理，两者绝不能混着比较。
* **相邻合并放在阈值之后**。先合并再过滤会出现「一条合并结果里一半内容低于阈值，
  却因为另一半分数高而整体留下」，用户看到的引用就包含不相关内容。
* 合并取**较小**的 ``chunk_id``（§5.2 第 4 条），保证引用编号在多次检索之间稳定——
  否则同一次提问刷新两次会看到不同的引用 ID。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.core.config import Settings
from app.core.tokens import count_tokens
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing
from app.rag.base import RetrievalUnavailable, RetrievedChunk
from app.rag.embedding.base import EmbeddingProvider
from app.rag.reranker.base import Reranker
from app.rag.vectorstore.base import VectorStore

logger = logging.getLogger("app.rag.retriever")

#: 同一文档内视为「相邻」的 ``chunk_index`` 差值上限（§5.2 第 4 条）
ADJACENT_GAP = 1

#: 合并时的连接符（中文不加空格，英文补一个）
_MERGE_SEPARATOR = "\n"

#: 降级原因（与对话层的 ``degraded_reasons`` 同词表）
REASON_RERANK_SKIPPED = "rerank_skipped"


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """一次检索的完整结果（``§5.1`` 的调试接口需要这些统计量）。"""

    items: list[RetrievedChunk]
    #: 向量召回条数（未重排、未过滤前）
    recalled: int = 0
    #: 是否真的执行了重排（``false`` 时 ``items[].score == vector_score``）
    rerank_used: bool = False
    elapsed_ms: int = 0


class Retriever:
    """默认检索器：向量召回 + 可选重排 + 阈值 + 相邻合并。"""

    def __init__(
        self,
        *,
        settings: Settings,
        embedding: EmbeddingProvider,
        vector_store: VectorStore,
        reranker: Reranker,
    ) -> None:
        self._settings = settings
        self._embedding = embedding
        self._store = vector_store
        self._reranker = reranker

    async def retrieve(
        self,
        *,
        query: str,
        user_id: str,
        kb_ids: Sequence[str] = (),
        top_k: int = 20,
        rerank_top_n: int = 5,
        score_threshold: float = 0.0,
    ) -> list[RetrievedChunk]:
        """执行检索（只要结果时用它；需要统计量用 :meth:`retrieve_detailed`）。

        Raises:
            RetrievalUnavailable: 向量库/Embedding 不可用。由上层转成
                ``degraded_reasons=["rag_unavailable"]``，**不能让对话失败**。
        """
        detail = await self.retrieve_detailed(
            query=query,
            user_id=user_id,
            kb_ids=kb_ids,
            top_k=top_k,
            rerank_top_n=rerank_top_n,
            score_threshold=score_threshold,
        )
        return detail.items

    async def retrieve_detailed(
        self,
        *,
        query: str,
        user_id: str,
        kb_ids: Sequence[str] = (),
        top_k: int = 20,
        rerank_top_n: int = 5,
        score_threshold: float = 0.0,
        with_rerank: bool = True,
        doc_ids: Sequence[str] = (),
    ) -> RetrievalResult:
        """执行检索并返回统计量（``/search`` 调试接口需要 ``recalled`` /
        ``rerank_used``）。"""
        started = time.perf_counter()
        with get_tracing().span("rag.retrieve", {"kb_count": len(kb_ids), "top_k": top_k}):
            candidates = await self._recall(
                query=query,
                user_id=user_id,
                kb_ids=kb_ids,
                doc_ids=doc_ids,
                top_k=max(1, top_k),
            )
        # 指标在**召回之后**立刻记：它度量的是向量库那一段，不包含重排。
        # 若把两者合计成一项，就无法回答「是召回慢了还是重排慢了」这个第一问题。
        get_metrics().observe_retrieve(kb_count=len(kb_ids), seconds=time.perf_counter() - started)
        get_metrics().observe_recalled(len(candidates))
        if not candidates:
            return RetrievalResult(items=[], recalled=0, elapsed_ms=_elapsed_ms(started))
        ranked, applied = await self._rerank(query, candidates, rerank_top_n, with_rerank)
        filtered = [item for item in ranked if item.score >= score_threshold]
        merged = (
            self._merge_adjacent(filtered) if self._settings.rag_merge_adjacent_chunks else filtered
        )
        elapsed = _elapsed_ms(started)
        logger.info(
            "rag.retrieved",
            extra={
                "recalled": len(candidates),
                "returned": len(merged),
                "rerank_applied": applied,
                "elapsed_ms": elapsed,
            },
        )
        return RetrievalResult(
            items=merged,
            recalled=len(candidates),
            rerank_used=applied,
            elapsed_ms=elapsed,
        )

    # ------------------------------------------------------------------
    async def _recall(
        self,
        *,
        query: str,
        user_id: str,
        kb_ids: Sequence[str],
        doc_ids: Sequence[str],
        top_k: int,
    ) -> list[RetrievedChunk]:
        try:
            vector = self._embedding.embed_query(query)
        except Exception as exc:
            raise RetrievalUnavailable(f"查询向量化失败：{exc}") from exc
        try:
            recalled = await self._store.search(
                vector, user_id=user_id, kb_ids=kb_ids, doc_ids=doc_ids, top_k=top_k
            )
        except RetrievalUnavailable:
            raise
        except Exception as exc:
            raise RetrievalUnavailable(f"向量检索失败：{exc}") from exc
        # 在这里兜住 ``vector_score``：向量库只负责「召回什么、分数多少」，
        # 「这个分数叫什么」由检索层统一命名。交给每个 store 实现各自记得填，
        # 迟早会漏——漏了的表现是 ``/search`` 的 ``vector_score`` 恒为 0，
        # 而 ``score`` 是 0.4，看起来像「重排把分数改了」，其实只是没人赋值。
        return [replace(chunk, vector_score=chunk.score) for chunk in recalled]

    async def _rerank(
        self,
        query: str,
        candidates: list[RetrievedChunk],
        rerank_top_n: int,
        with_rerank: bool,
    ) -> tuple[list[RetrievedChunk], bool]:
        """重排并回填分数。

        返回 ``(结果, 是否真的重排)``。退化时 **保留向量分数**：把分数统一成 0 会
        让后面的阈值过滤把所有结果都丢掉，等于「重排坏了就检索不到东西」。
        """
        if not with_rerank:
            get_metrics().observe_rerank(device="none", skipped=True, seconds=0.0)
            return candidates[: max(0, rerank_top_n)], False
        texts = [candidate.text for candidate in candidates]
        started = time.perf_counter()
        try:
            with get_tracing().span("rag.rerank", {"in_count": len(candidates)}):
                result = await self._reranker.rerank(query, texts, top_n=max(0, rerank_top_n))
        except Exception as exc:
            logger.warning("rerank.unavailable", extra={"error": str(exc)})
            get_metrics().observe_rerank(
                device="unknown", skipped=True, seconds=time.perf_counter() - started
            )
            return candidates[: max(0, rerank_top_n)], False
        if not result.applied:
            get_metrics().observe_rerank(
                device="unavailable", skipped=True, seconds=time.perf_counter() - started
            )
            return candidates[: max(0, rerank_top_n)], False
        get_metrics().observe_rerank(
            device=str(getattr(self._reranker, "device", "cpu")),
            skipped=False,
            seconds=time.perf_counter() - started,
        )
        ranked: list[RetrievedChunk] = []
        for index, score in result.ranked:
            if 0 <= index < len(candidates):
                # 同时保留向量分：``/search`` 要同时给出 vector_score 与 rerank_score
                candidate = candidates[index]
                ranked.append(
                    replace(
                        candidate,
                        score=float(score),
                        vector_score=candidate.score,
                    )
                )
        return ranked, True

    # ------------------------------------------------------------------
    @staticmethod
    def _merge_adjacent(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """合并同文档内 ``chunk_index`` 相邻的条目（§5.2 第 4 条）。

        输入已按分数降序，所以合并结果继承其中**更高的分数**与更靠前的位置——
        这正是「取较小 ``chunk_id``」的效果，且不依赖输入顺序是否恰好按 index 排列。
        """
        if len(chunks) <= 1:
            return list(chunks)
        groups: dict[str, list[RetrievedChunk]] = {}
        order: list[str] = []
        for chunk in chunks:
            key = chunk.doc_id or chunk.chunk_id
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(chunk)

        merged: list[RetrievedChunk] = []
        for key in order:
            bucket = groups[key]
            # 同一文档内先按 chunk_index 升序，才谈得上「相邻」
            bucket.sort(key=lambda item: item.chunk_index)
            current: RetrievedChunk | None = None
            for chunk in bucket:
                if current is None:
                    current = chunk
                    continue
                if chunk.chunk_index - current.chunk_index <= ADJACENT_GAP:
                    current = _merge_pair(current, chunk)
                    continue
                merged.append(current)
                current = chunk
            if current is not None:
                merged.append(current)
        merged.sort(key=lambda item: item.score, reverse=True)
        return merged


def _merge_pair(left: RetrievedChunk, right: RetrievedChunk) -> RetrievedChunk:
    """把两条相邻切片拼成一条。

    ``chunk_id`` 取 ``chunk_index`` 较小的一方：向量库里一条合并结果需要有个稳定
    身份，而按分数取会让「分数因重排模型换版而变」导致引用 ID 变化。
    """
    first, second = (left, right) if left.chunk_index <= right.chunk_index else (right, left)
    text = f"{first.text}{_MERGE_SEPARATOR}{second.text}"
    return replace(
        first,
        text=text,
        score=max(left.score, right.score),
        vector_score=max(left.vector_score, right.vector_score),
        merged=True,
        char_start=min(first.char_start, second.char_start),
        char_end=max(first.char_end, second.char_end),
        page=first.page if first.page is not None else second.page,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def merge_token_budget(
    chunks: Sequence[RetrievedChunk], *, budget: int
) -> tuple[list[RetrievedChunk], bool]:
    """按 token 预算裁剪 RAG 上下文（§5.2 第 5 条：超限按分数从低到高丢弃）。

    Returns:
        ``(保留的片段, 是否发生了裁剪)``。至少保留一条——全丢等于「检索到了但
        一个字都没给模型」，用户会看到模型凭空作答（幻觉最容易发生的场景）。
    """
    if not chunks:
        return [], False
    ordered = sorted(chunks, key=lambda item: item.score, reverse=True)
    kept: list[RetrievedChunk] = []
    used = 0
    trimmed = False
    for index, chunk in enumerate(ordered):
        cost = count_tokens(chunk.text)
        if index > 0 and used + cost > budget:
            trimmed = True
            continue
        kept.append(chunk)
        used += cost
    kept.sort(key=lambda item: item.score, reverse=True)
    return kept, trimmed


__all__ = [
    "ADJACENT_GAP",
    "REASON_RERANK_SKIPPED",
    "RetrievalResult",
    "Retriever",
    "merge_token_budget",
]
