"""向量库端口（``docs/06`` §4.5 / §5.2，``docs/09`` §3）。

检索**同时**需要「向量相似度」和「结构化过滤」，所以端口的过滤条件是显式的
``(user_id, kb_ids, doc_ids)`` 而不是一个自由表达式串。原因很实际：多租户隔离
靠的就是 ``user_id`` 过滤，把它藏在调用方拼出的表达式中，一次拼接失误就是跨租户
数据泄露；把它写进端口签名，实现方无法「忘记加」。

``upsert`` 语义是硬要求（§4.5）：重跑入库任务 MUST NOT 产生重复向量，否则同一段
文本会以多个 ``chunk_id`` 出现在检索结果里，重复占用上下文预算。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.rag.base import RetrievedChunk


@runtime_checkable
class VectorStore(Protocol):
    """向量库端口。"""

    async def ensure_ready(self) -> None:
        """确保集合/索引存在（幂等）。"""
        ...

    async def upsert(
        self, chunks: Sequence[RetrievedChunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        """按 ``chunk_id`` 主键 upsert，返回写入条数。"""
        ...

    async def search(
        self,
        vector: Sequence[float],
        *,
        user_id: str,
        kb_ids: Sequence[str] = (),
        doc_ids: Sequence[str] = (),
        top_k: int = 20,
    ) -> list[RetrievedChunk]:
        """向量召回（未做重排与阈值过滤）。"""
        ...

    async def delete_by_document(self, doc_id: str) -> int:
        """按文档删除向量（幂等），返回删除条数。"""
        ...

    async def delete_by_kb(self, kb_id: str) -> int:
        """按知识库删除向量（幂等）。"""
        ...

    async def count(self, *, user_id: str, kb_ids: Sequence[str] = ()) -> int:
        """统计向量条数（对账用）。"""
        ...


__all__ = ["VectorStore"]
