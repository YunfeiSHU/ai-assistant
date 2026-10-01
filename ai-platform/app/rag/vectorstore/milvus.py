"""Milvus 向量库（``INFRA_BACKEND=real``，``docs/09`` §3）。

集合定义照 ``docs/09`` §3.1：主键 ``chunk_id``、``FLOAT_VECTOR``、``HNSW`` + ``COSINE``，
并对 ``user_id`` / ``kb_id`` / ``doc_id`` 建 ``INVERTED`` 标量索引 —— 没有标量索引的过滤会
退化成全表扫描 + 后过滤，命中数会不足 ``top_k``。

``pymilvus`` 是同步 SDK，全部调用放进线程池，避免阻塞事件循环（对话 SSE 与它同进程）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.rag.base import RetrievedChunk

logger = logging.getLogger("app.rag.vectorstore")


def _import_pymilvus_guarded() -> Any:
    """导入 ``pymilvus``，并撤销它在 import 期对 ``os.environ`` 的污染。

    ``pymilvus/settings.py`` 直接调了 ``load_dotenv()``（不是我们的代码）：只要 import
    到它，就会把从 CWD 往上找到的第一个 ``.env`` 灌进 ``os.environ``。说「多几个环境
    变量」是不够的 —— ``Settings(_env_file=None, ...)`` 仍会读到它们，因为环境变量的
    优先级与 env_file 无关。实测后果：pytest 里第一个 import 了 pymilvus 的用例之后，
    整个进程的 ``Settings(_env_file=None, embedding_provider="hash")`` 都会带上开发机
    ``.env`` 的 ``EMBEDDING_DIM=2048`` ⇒ 契约用例「就绪探针报 1024 维」直接失败；
    生产进程同理，且会被子进程继承。

    所以这里做「快照 → import → 恢复」：pymilvus 自己想读的 ``MILVUS_*`` 已由
    :class:`~app.core.config.Settings` 显式传入。
    """
    snapshot = dict(os.environ)
    try:
        import pymilvus
    except ImportError as exc:  # pragma: no cover - 需要真实部署才走到
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "未安装 pymilvus，无法使用 INFRA_BACKEND=real 的向量库",
        ) from exc
    finally:
        # 只回滚它加进来/改掉的键，不做 clear()，避免出现"环境为空"的瞬间
        for key in set(os.environ) - set(snapshot):
            del os.environ[key]
        for key, value in snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
    return pymilvus


def _import_pymilvus_client() -> Any:
    """``MilvusClient``（import 走 :func:`_import_pymilvus_guarded`）。"""
    return _import_pymilvus_guarded().MilvusClient


def _import_pymilvus_schema_types() -> tuple[Any, Any]:
    """``(DataType, MilvusClient)``：建集合时用。"""
    module = _import_pymilvus_guarded()
    return module.DataType, module.MilvusClient


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


def _vector_dim_of(described: Any) -> int | None:
    """从 ``describe_collection`` 的结果里取向量字段的维度（取不到返回 ``None``）。

    写成独立函数：``describe_collection`` 的返回结构随 pymilvus 版本变过，而这里的用途是
    「能判就判、判不了不拦」—— 真正的防线还有写入时的显式 ``_check_dim``。
    """
    fields = (described or {}).get("fields") if isinstance(described, dict) else None
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        if field.get("name") != "vector":
            continue
        params = field.get("params") or {}
        dim = params.get("dim") if isinstance(params, dict) else None
        try:
            return int(dim) if dim is not None else None
        except (TypeError, ValueError):
            return None
    return None


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
            MilvusClient = _import_pymilvus_client()
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
            DataType, MilvusClient = _import_pymilvus_schema_types()

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
        else:
            # 集合已存在时必须核对维度：Milvus 的向量维度是建集合时固化的，维度不符时
            # 写入不会立刻报错（表现为「写成功但永远检索不到」）。换 embedding 模型
            # （bge-m3 1024 → ark/2048）就会踩这一步，所以在启动期把它变成明确的错误。
            described = await self._run(client.describe_collection, self._collection_name)
            existing = _vector_dim_of(described)
            if existing is not None and int(existing) != self.dim:
                raise AppError(
                    ErrorCode.VECTOR_DIM_MISMATCH,
                    f"Milvus 集合 {self._collection_name} 的向量维度是 {existing}，"
                    f"而配置是 {self.dim}",
                    {
                        "collection": self._collection_name,
                        "existing_dim": int(existing),
                        "configured_dim": self.dim,
                        "hint": (
                            "换 embedding 模型后旧向量不可用：请 drop 该集合重建，"
                            "或用 MILVUS_COLLECTION 换一个新集合名（旧数据保留作回滚）"
                        ),
                    },
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

        ``user_id`` 是无条件加入的：多租户隔离不能依赖调用方，否则一次参数遗漏
        就是跨租户数据泄露。
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
