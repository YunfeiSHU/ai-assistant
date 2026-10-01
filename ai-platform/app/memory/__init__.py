"""Memory 层（短期上下文 / 摘要 / 长期记忆）。

三个子模块对应 ``docs/07`` 的三层结构，边界刻意按「载体」而不是按「功能」切：

| 模块 | 载体 | 职责 |
| --- | --- | --- |
| :mod:`app.memory.context_store` | Redis / 内存 | 短期上下文 + 摘要**存储** |
| :mod:`app.memory.redis_store` | Redis | 上面那个协议的 Redis 实现 |
| :mod:`app.memory.long_term` | MySQL / 内存 | 长期记忆正文与元数据 |
| :mod:`app.memory.mysql_repo` | MySQL | 上面那个协议的 MySQL 实现 |
| :mod:`app.memory.vector_index` | Milvus / 内存 | 长期记忆的语义索引 |
| :mod:`app.memory.milvus_index` | Milvus | 上面那个协议的 Milvus 实现 |
| :mod:`app.memory.summary` | — | 摘要**生成**（触发条件、结构、增量合并） |
| :mod:`app.memory.extractor` | — | 从对话抽取长期记忆候选并过滤 |

``summary`` / ``extractor`` 是纯逻辑（只依赖 LLM 与 Settings），因此可以完全脱开
存储来测；``context_store`` / ``long_term`` 反过来只依赖存储，可以脱开 LLM 来测。
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.memory.context_store import (
    ConversationStore,
    ConversationSummary,
    InMemoryConversationStore,
    StoredMessage,
    UnavailableConversationStore,
    now_iso,
)
from app.memory.long_term import (
    InMemoryMemoryRepo,
    MemoryKind,
    MemoryRecord,
    MemoryRepo,
    MemoryWriteResult,
    content_sha256,
    encode_memory_cursor,
)
from app.memory.vector_index import (
    InMemoryMemoryVectorIndex,
    MemoryHit,
    MemoryVectorIndex,
)

logger = get_logger("app.memory")

__all__ = [
    "ConversationStore",
    "ConversationSummary",
    "InMemoryConversationStore",
    "InMemoryMemoryRepo",
    "InMemoryMemoryVectorIndex",
    "MemoryHit",
    "MemoryKind",
    "MemoryRecord",
    "MemoryRepo",
    "MemoryVectorIndex",
    "MemoryWriteResult",
    "StoredMessage",
    "UnavailableConversationStore",
    "UnavailableMemoryRepo",
    "build_conversation_store",
    "build_memory_repo",
    "build_memory_vector_index",
    "content_sha256",
    "encode_memory_cursor",
    "now_iso",
]


def build_conversation_store(settings: Settings) -> ConversationStore:
    """按 ``INFRA_BACKEND`` 构造短期上下文存储。

    ``memory`` → 进程内实现（本地/测试）；``real`` → Redis。

    ``real`` 下**驱动缺失或连接串无效**时返回明确报 503 的占位实现，而不是静默
    退回内存实现：后者在单进程里「看起来一切正常」，一旦多副本部署就会表现为
    「同一会话换一个实例就丢上下文」——没有报错、没有告警，只有用户发现。
    """
    if settings.infra_backend != "real":
        return InMemoryConversationStore(settings)
    from app.memory.redis_store import (
        RedisConversationStore,
        RedisUnavailable,
        create_redis_commands,
    )

    try:
        return RedisConversationStore(create_redis_commands(settings), settings)
    except RedisUnavailable as exc:
        logger.warning(
            "memory.conversation_store_unavailable",
            extra={"infra_backend": settings.infra_backend, "error": str(exc)},
        )
        return UnavailableConversationStore()


def build_memory_repo(settings: Settings) -> MemoryRepo:
    """按 ``INFRA_BACKEND`` 构造长期记忆仓储。

    ``memory`` → 进程内实现；``real`` → :class:`~app.memory.mysql_repo.MySqlMemoryRepo`
    （``user_memory`` 表，见 ``docs/09`` §2.5）。

    ``real`` 下 MySQL 初始化失败时给出占位实现（接口 503、对话降级为
    ``memory_unavailable``），理由见 :mod:`app.memory.unavailable`。
    """
    if settings.infra_backend != "real":
        return InMemoryMemoryRepo(max_items=settings.memory_max_items)
    from app.memory.mysql_repo import MySqlMemoryRepo
    from app.memory.unavailable import UnavailableMemoryRepo

    try:
        repo = MySqlMemoryRepo(settings)
    except AppError as exc:
        logger.error(
            "memory.repo_unavailable",
            extra={"infra_backend": settings.infra_backend, "error": exc.message},
        )
        return UnavailableMemoryRepo()
    logger.info("memory.repo_ready", extra={"backend": "mysql"})
    return repo


def build_memory_vector_index(settings: Settings) -> MemoryVectorIndex:
    """按 ``INFRA_BACKEND`` 构造长期记忆向量索引。

    ``memory`` → 进程内暴力余弦；``real`` → :class:`~app.memory.milvus_index.MilvusMemoryVectorIndex`
    （``ai_platform_memories`` 集合，见 ``docs/09`` §3.2）。

    构造期**不连接** Milvus（``pymilvus`` 懒导入），所以这里不会因为
    Milvus 没起来而失败 —— 它只在真正检索时把 ``503`` 报给请求方。
    """
    if settings.infra_backend != "real":
        return InMemoryMemoryVectorIndex(dim=settings.embedding_dim)
    from app.memory.milvus_index import MilvusMemoryVectorIndex

    logger.info(
        "memory.vector_index_ready",
        extra={
            "backend": "milvus",
            "collection": settings.milvus_memory_collection,
            "dim": settings.milvus_vector_dim,
        },
    )
    return MilvusMemoryVectorIndex(settings)
