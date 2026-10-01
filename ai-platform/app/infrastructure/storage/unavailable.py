"""MySQL 仓储不可用时的占位实现（降级路径，不阻断启动）。

**什么时候会走到这里**：``INFRA_BACKEND=real`` 但仓储**初始化就失败** ——
典型是没装驱动（``uv sync --extra mysql`` 忘了加）或 DSN 无法解析。
M8 之后 MySQL 仓储已经实现（:mod:`app.storage.mysql`），所以这里不再
是「尚未提供」的临时占位，而是**依赖故障矩阵的一个分支**。

**为什么不是「启动直接报错」**：``APP_ENV=prod`` 强制 ``INFRA_BACKEND=real``
（``app/config.py``）。如果这里在构造期抛异常，那么**整个 prod 应用都起不来** ——连
``POST /chat``、鉴权、健康检查这些完全不依赖关系库的能力也一起没了。

``docs/10-非功能需求与可观测性.md`` 的依赖故障矩阵给出的口径正是本文实现的：
「MySQL 不可用 | 对话仍可用（无任务、无长期记忆）；写类接口返回
``503 DEPENDENCY_UNAVAILABLE``」。所以每个方法都抛
:data:`~app.core.errors.ErrorCode.DEPENDENCY_UNAVAILABLE`，客户端拿到的是
**明确的 503 + 可读原因**，而不是一个看起来成功、实际把数据丢进进程内存的
「静默回退」（后者才是最危险的：重启即丢数据，而且没人会发现）。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import AppError, ErrorCode
from app.storage.base import (
    Chunk,
    Document,
    KnowledgeBase,
    _DocumentPage,
    _Documents,
    _KBPage,
)

#: 降级原因。具体失败细节在 :func:`app.storage.build_repositories` 的 error 日志里
REASON = "MySQL 仓储不可用（INFRA_BACKEND=real 但仓储初始化失败，详见服务端日志）"


def _unavailable(repo: str, method: str) -> AppError:
    """统一的「依赖未提供」错误。"""
    return AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        f"{repo}.{method} 不可用：{REASON}",
        {"component": "mysql", "repo": repo, "method": method},
    )


class UnavailableKnowledgeBaseRepo:
    """满足 :class:`~app.storage.base.KnowledgeBaseRepo` 的占位实现。"""

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        raise _unavailable("knowledge_bases", "add")

    async def get(self, kb_id: str, user_id: str) -> KnowledgeBase:
        raise _unavailable("knowledge_bases", "get")

    async def get_internal(self, kb_id: str) -> KnowledgeBase | None:
        raise _unavailable("knowledge_bases", "get_internal")

    async def list(self, user_id: str, *, limit: int = 20, cursor: str | None = None) -> _KBPage:
        raise _unavailable("knowledge_bases", "list")

    async def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        raise _unavailable("knowledge_bases", "save")

    async def soft_delete(self, kb_id: str, user_id: str) -> None:
        raise _unavailable("knowledge_bases", "soft_delete")

    async def count(self, user_id: str) -> int:
        raise _unavailable("knowledge_bases", "count")


class UnavailableDocumentRepo:
    """满足 :class:`~app.storage.base.DocumentRepo` 的占位实现。"""

    async def add(self, document: Document) -> Document:
        raise _unavailable("documents", "add")

    async def get(self, doc_id: str, user_id: str) -> Document:
        raise _unavailable("documents", "get")

    async def find_by_sha256(self, kb_id: str, content_sha256: str) -> Document | None:
        raise _unavailable("documents", "find_by_sha256")

    async def list(
        self,
        kb_id: str,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> _DocumentPage:
        raise _unavailable("documents", "list")

    async def save(self, document: Document) -> Document:
        raise _unavailable("documents", "save")

    async def soft_delete(self, doc_id: str, user_id: str) -> None:
        raise _unavailable("documents", "soft_delete")

    async def count_in_kb(self, kb_id: str) -> int:
        raise _unavailable("documents", "count_in_kb")

    async def live_documents(self, kb_id: str) -> _Documents:
        raise _unavailable("documents", "live_documents")


class UnavailableChunkRepo:
    """满足 :class:`~app.storage.base.ChunkRepo` 的占位实现。"""

    async def replace_for_document(self, doc_id: str, chunks: Sequence[Chunk]) -> int:
        raise _unavailable("chunks", "replace_for_document")

    async def list_for_document(
        self, doc_id: str, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Chunk], bool]:
        raise _unavailable("chunks", "list_for_document")

    async def delete_for_document(self, doc_id: str) -> int:
        raise _unavailable("chunks", "delete_for_document")

    async def count_for_document(self, doc_id: str) -> int:
        raise _unavailable("chunks", "count_for_document")

    async def count_for_kb(self, kb_id: str) -> int:
        raise _unavailable("chunks", "count_for_kb")


__all__ = [
    "REASON",
    "UnavailableChunkRepo",
    "UnavailableDocumentRepo",
    "UnavailableKnowledgeBaseRepo",
]
