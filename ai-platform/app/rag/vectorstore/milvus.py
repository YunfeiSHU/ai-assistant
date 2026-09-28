"""Milvus 向量库（``INFRA_BACKEND=real``，``docs/09`` §3）。

集合定义严格照 ``docs/09`` §3.1：主键 ``chunk_id``（VARCHAR 64）、``FLOAT_VECTOR``
维度来自配置、``HNSW`` + ``COSINE``，并对 ``user_id`` / ``kb_id`` / ``doc_id`` 建
``INVERTED`` 标量索引——没有标量索引的过滤会退化成全表扫描 + 后过滤，
召回质量随数据量增长而下降（先取 top-k 再过滤，命中数会不足 ``top_k``）。

``pymilvus`` 是同步 SDK，全部调用放进线程池，避免阻塞事件循环（对话 SSE 与它同进程）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.rag.base import RetrievedChunk

logger = logging.getLogger("app.rag.vectorstore")

#: 输出字段（与 ``docs/09`` §3.1 的集合 schema 一一对应）
_OUTPUT_FIELDS = [
    "chunk_id",
    "doc_id",
    "kb_id",
    "user_id",
    "chunk_index",
    "content",
    "page",
    "heading_path",
    "doc_name",
    "char_start",
    "char_end",
]

#: 写入分批大小（``docs/06`` §4.5：默认 500 条/批）
UPSERT_BATCH_SIZE = 500


class MilvusVectorStore:
    """Milvus 适配器。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.dim = settings.milvus_vector_dim
        self._collection_name = settings.milvus_collection
        self._client: Any | None = None
        self._ready = False

    # ------------------------------------------------------------------
    def _client_sync(self) -> Any:
        """懒加载 ``MilvusClient``。"""
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:  # pragma: no cover - 需要真实部署才走到
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "未安装 pymilvus，无法使用 INFRA_BACKEND=real 的向量库",
                ) from exc
            # 注意：``milvus_connection_args`` 是 @property，不是方法
            self._client = MilvusClient(**self._settings.milvus_connection_args)
        return self._client

    async def _run(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        import anyio

        return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))

    # ------------------------------------------------------------------
    async def ensure_ready(self) -> None:
        """创建集合与索引（幂等）。"""
        if self._ready:
            return
        client = self._client_sync()
        exists = await self._run(client.has_collection, self._collection_name)
        if not exists:
            from pymilvus import DataType, MilvusClient

            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True)
            schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
            schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
            schema.add_field("user_id", DataType.VARCHAR, max_length=64)
            schema.add_field("chunk_index", DataType.INT64)
            schema.add_field("content", DataType.VARCHAR, max_length=65535)
            schema.add_field("page", DataType.INT64)
            schema.add_field("heading_path", DataType.VARCHAR, max_length=512)
            schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
            schema.add_field("char_start", DataType.INT64)
            schema.add_field("char_end", DataType.INT64)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            for field in ("user_id", "kb_id", "doc_id"):
                index_params.add_index(field_name=field, index_type="INVERTED")
            await self._run(
                client.create_collection,
                self._collection_name,
                schema=schema,
                index_params=index_params,
            )
            logger.info(
                "milvus.collection_created",
                extra={"collection": self._collection_name, "dim": self.dim},
            )
        self._ready = True

    async def upsert(
        self, chunks: Sequence[RetrievedChunk], vectors: Sequence[Sequence[float]]
    ) -> int:
        """按 ``chunk_id`` upsert（分批）。"""
        await self.ensure_ready()
        client = self._client_sync()
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "kb_id": chunk.kb_id,
                "user_id": chunk.user_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.text[:65535],
                "page": chunk.page if chunk.page is not None else -1,
                "heading_path": chunk.heading_path[:512],
                "doc_name": chunk.doc_name[:512],
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "vector": [float(value) for value in vector],
            }
            for chunk, vector in zip(chunks, vectors, strict=False)
        ]
        written = 0
        for start in range(0, len(rows), UPSERT_BATCH_SIZE):
            batch = rows[start : start + UPSERT_BATCH_SIZE]
            # upsert 而非 insert：重跑入库任务不能产生重复向量（docs/06 §4.5）
            await self._run(client.upsert, self._collection_name, batch)
            written += len(batch)
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
        """向量召回。"""
        await self.ensure_ready()
        client = self._client_sync()
        results = await self._run(
            client.search,
            self._collection_name,
            data=[[float(value) for value in vector]],
            limit=max(1, top_k),
            filter=self._filter(user_id, kb_ids, doc_ids),
            output_fields=_OUTPUT_FIELDS,
            search_params={
                "metric_type": "COSINE",
                "params": {"ef": self._settings.milvus_search_ef},
            },
        )
        hits = results[0] if results else []
        chunks: list[RetrievedChunk] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            page = entity.get("page", -1)
            chunks.append(
                RetrievedChunk(
                    chunk_id=str(hit.get("id", entity.get("chunk_id", ""))),
                    text=str(entity.get("content", "")),
                    doc_id=str(entity.get("doc_id", "")),
                    kb_id=str(entity.get("kb_id", "")),
                    doc_name=str(entity.get("doc_name", "")),
                    page=int(page) if page is not None and int(page) > 0 else None,
                    score=float(hit.get("distance", 0.0)),
                    chunk_index=int(entity.get("chunk_index", 0)),
                    heading_path=str(entity.get("heading_path", "")),
                    char_start=int(entity.get("char_start", 0)),
                    char_end=int(entity.get("char_end", 0)),
                    user_id=str(entity.get("user_id", "")),
                )
            )
        return chunks

    async def delete_by_document(self, doc_id: str) -> int:
        """按文档删除。"""
        return await self._delete(f'doc_id == "{_escape(doc_id)}"')

    async def delete_by_kb(self, kb_id: str) -> int:
        """按知识库删除。"""
        return await self._delete(f'kb_id == "{_escape(kb_id)}"')

    async def count(self, *, user_id: str, kb_ids: Sequence[str] = ()) -> int:
        """统计条数（对账用）。"""
        await self.ensure_ready()
        client = self._client_sync()
        expr = self._filter(user_id, kb_ids, ())
        result = await self._run(
            client.query, self._collection_name, filter=expr, output_fields=["count(*)"]
        )
        if result and isinstance(result[0], dict):
            return int(result[0].get("count(*)", 0))
        return len(result or [])

    # ------------------------------------------------------------------
    async def _delete(self, expression: str) -> int:
        await self.ensure_ready()
        client = self._client_sync()
        try:
            # 先查再删是为了返回「删了多少条」；Milvus 的 delete 不返回条数
            existing = await self._run(
                client.query, self._collection_name, filter=expression, output_fields=["chunk_id"]
            )
            await self._run(client.delete, self._collection_name, filter=expression)
        except Exception as exc:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"向量删除失败：{exc}",
                {"filter": expression},
            ) from exc
        return len(existing or [])

    @staticmethod
    def _filter(user_id: str, kb_ids: Sequence[str], doc_ids: Sequence[str]) -> str:
        """拼 Milvus 过滤表达式。

        ``user_id`` 是**无条件**加入的：多租户隔离不能依赖调用方，否则一次
        参数遗漏就是跨租户数据泄露。
        """
        clauses = [f'user_id == "{_escape(user_id)}"']
        if kb_ids:
            joined = ", ".join(f'"{_escape(value)}"' for value in kb_ids)
            clauses.append(f"kb_id in [{joined}]")
        if doc_ids:
            joined = ", ".join(f'"{_escape(value)}"' for value in doc_ids)
            clauses.append(f"doc_id in [{joined}]")
        return " and ".join(clauses)


def _escape(value: str) -> str:
    """转义过滤表达式里的引号与反斜杠，避免表达式注入。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = ["UPSERT_BATCH_SIZE", "MilvusVectorStore"]
