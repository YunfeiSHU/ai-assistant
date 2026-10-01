"""向量库：端口 + 内存实现 + Milvus 实现。"""

from __future__ import annotations

from app.core.config import Settings
from app.rag.vectorstore.base import VectorStore
from app.rag.vectorstore.memory import InMemoryVectorStore
from app.rag.vectorstore.milvus import MilvusVectorStore

__all__ = ["InMemoryVectorStore", "MilvusVectorStore", "VectorStore", "build_vector_store"]


def build_vector_store(settings: Settings) -> VectorStore:
    """按 ``INFRA_BACKEND`` 选择向量库实现。

    ``embedding_dim`` 与 ``milvus_vector_dim`` 的一致性在启动期已由
    ``Settings.validate_for_startup`` 校验（``VECTOR_DIM_MISMATCH``），
    这里只需按同一维度构造。
    """
    if settings.infra_backend == "real":
        return MilvusVectorStore(settings)
    return InMemoryVectorStore(dim=settings.milvus_vector_dim)
