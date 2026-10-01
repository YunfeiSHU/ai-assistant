"""RAG 存储层（端口 + 实现）。"""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.infrastructure.storage.base import (
    Chunk,
    ChunkRepo,
    Document,
    DocumentRepo,
    KnowledgeBase,
    KnowledgeBaseRepo,
    ObjectStore,
    RagRepositories,
    build_object_key,
    sanitize_filename,
)
from app.infrastructure.storage.memory import (
    InMemoryChunkRepo,
    InMemoryDocumentRepo,
    InMemoryKnowledgeBaseRepo,
    InMemoryRagRepository,
    encode_created_cursor,
    encode_index_cursor,
)
from app.infrastructure.storage.objectstore import (
    InMemoryObjectStore,
    MinioObjectStore,
    build_object_store,
)
from app.infrastructure.storage.unavailable import (
    REASON,
    UnavailableChunkRepo,
    UnavailableDocumentRepo,
    UnavailableKnowledgeBaseRepo,
)

logger = get_logger("app.infrastructure.storage")

__all__ = [
    "REASON",
    "Chunk",
    "ChunkRepo",
    "Document",
    "DocumentRepo",
    "InMemoryChunkRepo",
    "InMemoryDocumentRepo",
    "InMemoryKnowledgeBaseRepo",
    "InMemoryObjectStore",
    "InMemoryRagRepository",
    "KnowledgeBase",
    "KnowledgeBaseRepo",
    "MinioObjectStore",
    "ObjectStore",
    "RagRepositories",
    "UnavailableChunkRepo",
    "UnavailableDocumentRepo",
    "UnavailableKnowledgeBaseRepo",
    "build_object_key",
    "build_object_store",
    "build_repositories",
    "encode_created_cursor",
    "encode_index_cursor",
    "sanitize_filename",
]


def build_repositories(settings: Settings) -> RagRepositories:
    """按 ``INFRA_BACKEND`` 构造三个仓储。

    ``memory`` → 进程内实现（本地/测试）；``real`` → :class:`~app.infrastructure.storage.mysql.MySqlRagRepository`
    （KB / 文档 / 切片三张表，见 ``docs/09``）。

    ``real`` 下**初始化就失败**（没装 ``sqlalchemy`` / DSN 不可解析）时返回明确报
    503 的占位仓储（而不是在构造期抛异常）：MySQL 构造失败的常见原因是部署忘了
    ``uv sync --extra mysql``，而它跟「对话 / 鉴权 / 健康检查」毫无关系 ——
    让整个进程起不来属于把故障面放得比实际大（``docs/10`` 依赖故障矩阵）。

    注意**连接能建不代表表在**：引擎是惰性的，真正的可达性检查在
    ``app/main.py`` 的启动自检里（与向量库的 ``ensure_ready`` 同一策略：只告警、
    不阻断）。表不存在时请求会拿到 ``503`` 并提示先执行
    ``deploy/mysql/001_init_schema.sql``（见 :func:`app.infrastructure.mysql.db.classify_db_error`）。
    """
    if settings.infra_backend == "real":
        from app.infrastructure.storage.mysql import MySqlRagRepository

        try:
            facade = MySqlRagRepository(settings)
        except AppError as exc:
            logger.error(
                "storage.repositories_unavailable",
                extra={"infra_backend": settings.infra_backend, "error": exc.message},
            )
            return RagRepositories(
                knowledge_bases=UnavailableKnowledgeBaseRepo(),
                documents=UnavailableDocumentRepo(),
                chunks=UnavailableChunkRepo(),
            )
        logger.info("storage.repositories_ready", extra={"backend": "mysql"})
        return RagRepositories(
            knowledge_bases=facade.knowledge_bases,
            documents=facade.documents,
            chunks=facade.chunks,
        )
    memory = InMemoryRagRepository()
    return RagRepositories(
        knowledge_bases=memory.knowledge_bases,
        documents=memory.documents,
        chunks=memory.chunks,
    )
