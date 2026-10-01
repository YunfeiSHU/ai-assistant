"""进程内向量库（``INFRA_BACKEND=memory``）。

用暴力余弦检索而不是近似最近邻：本地与测试的数据量在万级以内，暴力扫描是
**精确**的（没有召回率损失），而且结果是确定性的——用近似索引会让测试断言
依赖构建时机，出现「同一查询偶尔少一条」的偶发失败。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

from app.rag.base import RetrievedChunk


class InMemoryVectorStore:
    """内存向量库（暴力余弦，精确）：:class:`~app.rag.vectorstore.base.VectorStore` 的进程内实现。"""

    def __init__(self, *, dim: int) -> None:
        self.dim = dim
        self._vectors: dict[str, list[float]] = {}
        self._payloads: dict[str, RetrievedChunk] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """内存实现无需建集合。"""
        return None

    async def upsert(
        self, chunks: Sequence[RetrievedChunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        async with self._lock:
            written = 0
            for chunk, vector in zip(chunks, vectors, strict=False):
                self._vectors[chunk.chunk_id] = [float(value) for value in vector]
                self._payloads[chunk.chunk_id] = chunk
                written += 1
            return written

    async def search(
        self,
        vector: Sequence[float],
        *,
        user_id: str,
        kb_ids: Sequence[str] = (),
        doc_ids: Sequence[str] = (),
        top_k: int = 20,
    ) -> list[RetrievedChunk]:
        """按余弦相似度取 top-k。"""
        import numpy as np

        async with self._lock:
            items = [
                (chunk_id, self._vectors[chunk_id], self._payloads[chunk_id])
                for chunk_id in self._vectors
            ]
        if not items:
            return []
        query = np.asarray(list(vector), dtype=np.float32)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            return []
        query = query / query_norm

        allowed_kbs = set(kb_ids)
        allowed_docs = set(doc_ids)
        ids: list[str] = []
        matrix: list[list[float]] = []
        for chunk_id, stored, payload in items:
            if payload.user_id != user_id:
                continue
            if allowed_kbs and payload.kb_id not in allowed_kbs:
                continue
            if allowed_docs and payload.doc_id not in allowed_docs:
                continue
            ids.append(chunk_id)
            matrix.append(stored)
        if not ids:
            return []
        array = np.asarray(matrix, dtype=np.float32)
        norms = np.linalg.norm(array, axis=1)
        # 零向量（理论上不该出现）用 1 兜底，避免整个矩阵出 NaN
        norms[norms == 0.0] = 1.0
        scores = (array / norms[:, None]) @ query
        order = np.argsort(-scores, kind="stable")[: max(0, top_k)]
        results: list[RetrievedChunk] = []
        for position in order:
            chunk = self._payloads[ids[int(position)]]
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    doc_id=chunk.doc_id,
                    kb_id=chunk.kb_id,
                    doc_name=chunk.doc_name,
                    page=chunk.page,
                    score=float(scores[int(position)]),
                    chunk_index=chunk.chunk_index,
                    heading_path=chunk.heading_path,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    user_id=chunk.user_id,
                )
            )
        return results

    async def delete_by_document(self, doc_id: str) -> int:
        async with self._lock:
            targets = [key for key, value in self._payloads.items() if value.doc_id == doc_id]
            for key in targets:
                self._vectors.pop(key, None)
                self._payloads.pop(key, None)
            return len(targets)

    async def delete_by_kb(self, kb_id: str) -> int:
        async with self._lock:
            targets = [key for key, value in self._payloads.items() if value.kb_id == kb_id]
            for key in targets:
                self._vectors.pop(key, None)
                self._payloads.pop(key, None)
            return len(targets)

    async def count(self, *, user_id: str, kb_ids: Sequence[str] = ()) -> int:
        allowed = set(kb_ids)
        async with self._lock:
            return sum(
                1
                for payload in self._payloads.values()
                if payload.user_id == user_id and (not allowed or payload.kb_id in allowed)
            )

    def cosine_of_stored(self, chunk_id: str) -> float:
        """调试用：返回某条向量的模长（校验写入时是否已归一化）。"""
        stored = self._vectors.get(chunk_id)
        if stored is None:
            return 0.0
        return math.sqrt(sum(value * value for value in stored))


__all__ = ["InMemoryVectorStore"]
