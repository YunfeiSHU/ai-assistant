"""长期记忆的向量索引（``ai_platform_memories`` 集合，``REQ-MEM-005`` / ``REQ-MEM-006``）。

为什么**不复用** chunk 的 :class:`~app.rag.vectorstore.base.VectorStore`：
那个端口的过滤条件是 ``(user_id, kb_ids, doc_ids)``，而记忆既不属于知识库也不
属于文档。硬塞进去就得传一组恒为空的 ``kb_ids``，读代码的人会以为「记忆是挂在
知识库下的」；将来想给集合加一个「按 kind 过滤」的能力，也会被那个签名挡住。

职责边界：**这里只管向量**。正文、置信度、命中计数都在
:mod:`app.memory.long_term` 的关系库侧。两边靠 ``mem_id`` 对齐 —— 因此
:meth:`MemoryVectorIndex.delete` 必须与关系库删除成对调用（``REQ-MEM-007``
要求「双删」），漏一边的表现是「列表里没有了，但检索还能命中」。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """一次记忆向量召回的命中。"""

    mem_id: str
    score: float


@runtime_checkable
class MemoryVectorIndex(Protocol):
    """长期记忆向量索引端口。"""

    async def ensure_ready(self) -> None:
        """确保集合/索引存在（幂等）。"""
        ...

    async def upsert(
        self, mem_id: str, user_id: str, vector: Sequence[float], *, kind: str = "fact"
    ) -> None:
        """按 ``mem_id`` upsert（重跑抽取 MUST NOT 产生重复向量）。

        ``kind`` 是写进集合的标量字段（``docs/09`` §3.2）。它不属于向量本身，
        但必须与向量写成**同一次请求** ——分成两次写入的话，中间失败就会出现
        「向量在、kind 是旧的」，而 ``docs/07`` §5.3 的偏好过滤会据此漏掉它。
        """
        ...

    async def search(
        self, vector: Sequence[float], *, user_id: str, top_k: int = 3
    ) -> list[MemoryHit]:
        """按余弦相似度召回（未做阈值过滤，阈值由调用方按语义决定）。"""
        ...

    async def delete(self, mem_id: str) -> int:
        """删除单条（幂等），返回删除条数。"""
        ...

    async def delete_all(self, user_id: str) -> int:
        """删除该用户全部向量（幂等），返回删除条数。"""
        ...

    async def count(self, *, user_id: str) -> int:
        """统计该用户的向量条数（``AC-MEM-10`` 的对账断言用它）。"""
        ...


class InMemoryMemoryVectorIndex:
    """进程内实现（暴力余弦，精确且确定性）。

    与 chunk 侧的内存实现同一个取舍：本地/测试数据量在千级以内，暴力扫描没有
    召回率损失，且同一输入永远得到同一结果 —— 用近似索引会让「去重阈值」这类
    断言依赖索引构建时机，出现偶发失败。
    """

    def __init__(self, *, dim: int) -> None:
        self.dim = dim
        self._vectors: dict[str, list[float]] = {}
        self._owners: dict[str, str] = {}
        self._kinds: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        """内存实现无需建集合。"""
        return None

    async def upsert(
        self, mem_id: str, user_id: str, vector: Sequence[float], *, kind: str = "fact"
    ) -> None:
        values = [float(value) for value in vector]
        if len(values) != self.dim:
            # 维度不符必须当场失败：Milvus 会拒绝写入，而内存实现如果「照收不误」，
            # 表现就是「本地全绿、上真库写不进记忆」。这里主动对齐真库行为。
            raise ValueError(f"向量维度不符：{len(values)} != {self.dim}")
        async with self._lock:
            self._vectors[mem_id] = values
            self._owners[mem_id] = user_id
            self._kinds[mem_id] = kind

    async def search(
        self, vector: Sequence[float], *, user_id: str, top_k: int = 3
    ) -> list[MemoryHit]:
        import numpy as np

        query = np.asarray(list(vector), dtype=np.float32)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0.0:
            # 零向量查询没有语义：返回「全部记忆都相似度 0」比返回空更糟
            # （调用方会以为检索成功，只是命中了低分条目）。
            return []
        async with self._lock:
            items = [
                (mem_id, self._vectors[mem_id])
                for mem_id, owner in self._owners.items()
                if owner == user_id
            ]
        if not items:
            return []
        query = query / query_norm

        ids = [mem_id for mem_id, _ in items]
        matrix = np.asarray([stored for _, stored in items], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)
        # 零向量（理论上不该出现）用 1 兜底，避免整个矩阵出 NaN
        norms[norms == 0.0] = 1.0
        scores = (matrix / norms[:, None]) @ query
        order = np.argsort(-scores, kind="stable")[: max(0, top_k)]
        return [
            MemoryHit(mem_id=ids[int(position)], score=float(scores[int(position)]))
            for position in order
        ]

    async def delete(self, mem_id: str) -> int:
        async with self._lock:
            removed = self._vectors.pop(mem_id, None)
            self._owners.pop(mem_id, None)
            self._kinds.pop(mem_id, None)
        return 1 if removed is not None else 0

    async def delete_all(self, user_id: str) -> int:
        async with self._lock:
            targets = [mem_id for mem_id, owner in self._owners.items() if owner == user_id]
            for mem_id in targets:
                self._vectors.pop(mem_id, None)
                self._owners.pop(mem_id, None)
                self._kinds.pop(mem_id, None)
        return len(targets)

    async def count(self, *, user_id: str) -> int:
        async with self._lock:
            return sum(1 for owner in self._owners.values() if owner == user_id)


__all__ = ["InMemoryMemoryVectorIndex", "MemoryHit", "MemoryVectorIndex"]
