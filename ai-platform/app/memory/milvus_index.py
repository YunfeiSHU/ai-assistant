"""长期记忆的 Milvus 向量索引（``ai_platform_memories``，``REQ-MEM-005/006``）。

集合定义照 ``docs/09`` §3.2：主键 ``mem_id``、``user_id`` 与 ``kind`` 两个标量字段、
``FLOAT_VECTOR`` 维度来自配置、``HNSW`` + ``COSINE``，标量字段建 ``INVERTED`` 索引。

**为什么必须有标量索引**：检索总是带 ``user_id`` 过滤（租户隔离，``REQ-DATA-006``）。
没有标量索引时 Milvus 只能「先按向量取 top-k、再过滤」，本人命中的条数会被
别人的向量挤掉 —— 表现为「库里明明有这条记忆，但它很少被检索到」，
而且随着其他用户的数据增长越来越明显。这属于**静默的召回率下降**，
没有任何报错，所以建索引这件事必须写在 ``ensure_ready`` 里而不是留给运维。

``kind`` 同理：``docs/07`` 允许只注入 ``preference``，走的是同一个集合的过滤条件。

``pymilvus`` 是同步 SDK，所有调用都放进线程池（与 :mod:`app.rag.vectorstore.milvus`
同一个理由：对话 SSE 与它在同一个事件循环里）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.memory.vector_index import MemoryHit

logger = get_logger("app.memory.milvus_index")

#: 批量写入上限（Milvus 单次请求不适合塞太多向量）
UPSERT_BATCH_SIZE = 500


def _literal(value: str) -> str:
    """把字符串转成 Milvus 表达式里的字符串字面量。

    用 ``json.dumps`` 而不是 ``f'"{value}"'``：``user_id`` 来自 JWT 的 ``sub``，
    里面出现引号或反斜杠时手写拼接会**破坏表达式结构**（要么语法错误、
    要么改变过滤条件 —— 后者等于越权读到别人的记忆）。
    """
    return json.dumps(value, ensure_ascii=False)


class MilvusMemoryVectorIndex:
    """:class:`~app.memory.vector_index.MemoryVectorIndex` 的 Milvus 实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.dim = settings.milvus_vector_dim
        self._collection = settings.milvus_memory_collection
        self._client: Any | None = None
        self._ready = False

    # ------------------------------------------------------------------
    def _client_sync(self) -> Any:
        """懒加载 ``MilvusClient``（驱动缺失给明确的 503）。"""
        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:  # pragma: no cover - 需要真实部署才走到
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "未安装 pymilvus，无法使用 INFRA_BACKEND=real 的记忆向量索引",
                ) from exc
            # ``milvus_connection_args`` 是 @property，不是方法
            self._client = MilvusClient(**self._settings.milvus_connection_args)
        return self._client

    async def _run(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        import anyio

        return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))

    # ------------------------------------------------------------------
    async def ensure_ready(self) -> None:
        """创建集合与索引（幂等）。

        向量索引：``HNSW`` / ``COSINE``（与 chunk 集合同一组参数 —— 两个集合
        用不同参数会让「为什么记忆的召回看起来更差」变成一个无从下手的疑问）。
        标量索引：``user_id``（租户隔离）与 ``kind``（偏好/事实过滤）。
        """
        if self._ready:
            return
        client = self._client_sync()
        if not await self._run(client.has_collection, self._collection):
            from pymilvus import DataType, MilvusClient

            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field("mem_id", DataType.VARCHAR, max_length=64, is_primary=True)
            schema.add_field("user_id", DataType.VARCHAR, max_length=64)
            schema.add_field("kind", DataType.VARCHAR, max_length=16)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params={"M": 16, "efConstruction": 200},
            )
            for field in ("user_id", "kind"):
                index_params.add_index(field_name=field, index_type="INVERTED")
            await self._run(
                client.create_collection,
                self._collection,
                schema=schema,
                index_params=index_params,
            )
            logger.info(
                "memory.collection_created",
                extra={"collection": self._collection, "dim": self.dim},
            )
        self._ready = True

    async def upsert(
        self, mem_id: str, user_id: str, vector: Sequence[float], *, kind: str = "fact"
    ) -> None:
        """按 ``mem_id`` upsert（重跑抽取 MUST NOT 产生重复向量）。"""
        values = [float(value) for value in vector]
        if len(values) != self.dim:
            # 维度不符必须当场失败：Milvus 会拒绝写入，而内存实现如果「照收不误」，
            # 表现就是「本地全绿、上真库写不进记忆」
            raise ValueError(f"向量维度不符：{len(values)} != {self.dim}")
        await self.ensure_ready()
        client = self._client_sync()
        row = {"mem_id": mem_id, "user_id": user_id, "kind": kind, "vector": values}
        await self._run(client.upsert, self._collection, [row])

    async def search(
        self, vector: Sequence[float], *, user_id: str, top_k: int = 3
    ) -> list[MemoryHit]:
        """按余弦相似度召回（未做阈值过滤，阈值由调用方按语义决定）。"""
        values = [float(value) for value in vector]
        if len(values) != self.dim:
            raise ValueError(f"向量维度不符：{len(values)} != {self.dim}")
        if all(value == 0.0 for value in values):
            # 零向量查询没有语义：返回「全部记忆都相似度 0」比返回空更糟
            # （调用方会以为检索成功，只是命中了低分条目）
            return []
        await self.ensure_ready()
        client = self._client_sync()
        hits = await self._run(
            client.search,
            self._collection,
            data=[values],
            limit=max(1, top_k),
            filter=f"user_id == {_literal(user_id)}",
            output_fields=["mem_id"],
            search_params={"ef": self._settings.milvus_search_ef},
        )
        results: list[MemoryHit] = []
        for entity in hits[0] if hits else []:
            mem_id = (entity.get("entity") or {}).get("mem_id") or entity.get("id")
            if mem_id is None:
                continue
            results.append(MemoryHit(mem_id=str(mem_id), score=float(entity.get("distance", 0.0))))
        return results

    async def delete(self, mem_id: str) -> int:
        """删除单条（幂等），返回删除条数。"""
        await self.ensure_ready()
        client = self._client_sync()
        report = await self._run(client.delete, self._collection, ids=[mem_id])
        return int(_deleted_count(report))

    async def delete_all(self, user_id: str) -> int:
        """删除该用户全部向量（幂等），返回删除条数。"""
        await self.ensure_ready()
        client = self._client_sync()
        report = await self._run(
            client.delete, self._collection, filter=f"user_id == {_literal(user_id)}"
        )
        return int(_deleted_count(report))

    async def count(self, *, user_id: str) -> int:
        """统计该用户的向量条数（``AC-MEM-10`` 的对账断言用它）。"""
        await self.ensure_ready()
        client = self._client_sync()
        rows = await self._run(
            client.query,
            self._collection,
            filter=f"user_id == {_literal(user_id)}",
            output_fields=["count(*)"],
        )
        if not rows:
            return 0
        first = rows[0]
        return int(first.get("count(*)") or first.get("count") or 0)


def _deleted_count(report: Any) -> int:
    """从 Milvus 的删除回执里取条数。

    不同版本回执的形状不一样（``{"delete_count": n}`` / 带 ``deleteCnt``，
    偶发还会是列表）。这里逐个候选取，取不到就返回 0 —— 但**不因此报错**：
    删除是幂等的，调用方只把它当统计值用（``AC-MEM-10`` 的对账会另外去 count）。
    """
    if isinstance(report, dict):
        for key in ("delete_count", "deleteCnt", "count"):
            if key in report and report[key] is not None:
                return int(report[key])
        return 0
    if isinstance(report, list):
        total = 0
        for item in report:
            total += _deleted_count(item)
        return total
    return int(report or 0)


__all__ = ["UPSERT_BATCH_SIZE", "MilvusMemoryVectorIndex"]
