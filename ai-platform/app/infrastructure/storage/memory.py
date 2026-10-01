"""进程内存储实现（``INFRA_BACKEND=memory``）。

刻意**不是**简化版：唯一约束、软删过滤、计数重算、游标分页都照 ``docs/09`` 的语义实现。
否则「本地全绿、上真库报唯一键冲突」这类问题只能等上线才发现。

结构上刻意是三个仓储 + 一份共享状态，而不是一个大类：三个协议的方法名（``add`` / ``get`` /
``list`` / ``save`` / ``soft_delete``）完全重合，合并实现就是后者覆盖前者。拆开也正好对上 SQL
实现的形状（三张表、同一个连接池）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import cursor_position, decode_cursor, encode_cursor, is_after_cursor
from app.infrastructure.storage.base import Chunk, Document, KnowledgeBase

# 三个仓储里都有名为 ``list`` 的方法，会在类作用域内遮蔽内建 ``list``，导致返回注解
# ``list[X]`` 被当成方法对象（mypy: not valid as a type）。用模块级别名绕开。
_KBPage = tuple[list[KnowledgeBase], bool]
_DocumentPage = tuple[list[Document], bool]
_ChunkPage = tuple[list[Chunk], bool]
_Documents = list[Document]


def _after(created_at: str, resource_id: str, cursor: str | None) -> bool:
    """游标判断：是否落在 ``cursor`` 之后（列表按 ``(created_at, id)`` 倒序）。"""
    if not cursor:
        return True
    return is_after_cursor(created_at, resource_id, decode_cursor(cursor))


@dataclass
class _State:
    """三个仓储共享的可变状态（对应 SQL 的同一套表 + 行锁）。"""

    kbs: dict[str, KnowledgeBase] = field(default_factory=dict)
    deleted_kbs: set[str] = field(default_factory=set)
    kbs_by_name: dict[tuple[str, str], str] = field(default_factory=dict)
    documents: dict[str, Document] = field(default_factory=dict)
    deleted_docs: set[str] = field(default_factory=set)
    docs_by_sha: dict[tuple[str, str], str] = field(default_factory=dict)
    chunks: dict[str, list[Chunk]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class InMemoryKnowledgeBaseRepo:
    """:class:`~app.infrastructure.storage.base.KnowledgeBaseRepo` 的内存实现。"""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        async with self._state.lock:
            key = (kb.user_id, kb.name)
            existing_id = self._state.kbs_by_name.get(key)
            if existing_id is not None and existing_id not in self._state.deleted_kbs:
                # uk_kb_user_name 的语义：同一用户下名字唯一（REQ-RAG-002）
                raise AppError(
                    ErrorCode.KB_NAME_CONFLICT,
                    f"知识库名称已存在：{kb.name}",
                    {"name": kb.name},
                )
            self._state.kbs[kb.id] = replace(kb)
            self._state.kbs_by_name[key] = kb.id
            return replace(kb)

    async def get(self, kb_id: str, user_id: str) -> KnowledgeBase:
        async with self._state.lock:
            kb = self._state.kbs.get(kb_id)
        if kb is None or kb_id in self._state.deleted_kbs or kb.user_id != user_id:
            # 跨用户一律 404 而不是 403：403 等于确认「这个 ID 存在」（REQ-RAG-011）
            raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")
        return replace(kb)

    async def get_internal(self, kb_id: str) -> KnowledgeBase | None:
        """不过滤 ``user_id`` 的读取（仅供 Worker 维护计数与状态）。"""
        async with self._state.lock:
            kb = self._state.kbs.get(kb_id)
        if kb is None or kb_id in self._state.deleted_kbs:
            return None
        return replace(kb)

    async def list(self, user_id: str, *, limit: int = 20, cursor: str | None = None) -> _KBPage:
        async with self._state.lock:
            items = [replace(kb) for kb in self._state.kbs.values()]
        selected = [
            kb
            for kb in items
            if kb.user_id == user_id
            and kb.id not in self._state.deleted_kbs
            and _after(kb.created_at, kb.id, cursor)
        ]
        # 排序键与 ``_after`` 的游标比较用同一个 ``cursor_position``：
        # 一旦“排序按字符串、比较按 datetime”错位，就会静默漏项
        selected.sort(key=lambda kb: cursor_position(kb.created_at, kb.id), reverse=True)
        window = selected[: limit + 1]
        return window[:limit], len(window) > limit

    async def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        async with self._state.lock:
            if kb.id not in self._state.kbs:
                raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")
            self._state.kbs[kb.id] = replace(kb)
            return replace(kb)

    async def soft_delete(self, kb_id: str, user_id: str) -> None:
        async with self._state.lock:
            kb = self._state.kbs.get(kb_id)
            if kb is None or kb.user_id != user_id or kb_id in self._state.deleted_kbs:
                raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")
            self._state.deleted_kbs.add(kb_id)
            self._state.kbs_by_name.pop((kb.user_id, kb.name), None)

    async def count(self, user_id: str) -> int:
        async with self._state.lock:
            return sum(
                1
                for kb in self._state.kbs.values()
                if kb.user_id == user_id and kb.id not in self._state.deleted_kbs
            )


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


class InMemoryDocumentRepo:
    """:class:`~app.infrastructure.storage.base.DocumentRepo` 的内存实现。"""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def add(self, document: Document) -> Document:
        async with self._state.lock:
            key = (document.kb_id, document.content_sha256)
            existing_id = self._state.docs_by_sha.get(key)
            if existing_id is not None and existing_id not in self._state.deleted_docs:
                # uk_doc_dedupe 的语义：同一 KB 内按内容哈希唯一（REQ-RAG-007）
                raise AppError(
                    ErrorCode.DOCUMENT_DUPLICATE,
                    "相同内容的文档已存在",
                    {"doc_id": existing_id},
                )
            self._state.documents[document.id] = replace(document)
            self._state.docs_by_sha[key] = document.id
            return replace(document)

    async def get(self, doc_id: str, user_id: str) -> Document:
        async with self._state.lock:
            document = self._state.documents.get(doc_id)
        if document is None or doc_id in self._state.deleted_docs or document.user_id != user_id:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
        return replace(document)

    async def find_by_sha256(self, kb_id: str, content_sha256: str) -> Document | None:
        async with self._state.lock:
            doc_id = self._state.docs_by_sha.get((kb_id, content_sha256))
            if doc_id is None or doc_id in self._state.deleted_docs:
                return None
            document = self._state.documents.get(doc_id)
        return replace(document) if document is not None else None

    async def list(
        self,
        kb_id: str,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> _DocumentPage:
        async with self._state.lock:
            items = [replace(document) for document in self._state.documents.values()]
        selected = [
            document
            for document in items
            if document.kb_id == kb_id
            and document.user_id == user_id
            and document.id not in self._state.deleted_docs
            and (status is None or document.status == status)
            and _after(document.created_at, document.id, cursor)
        ]
        selected.sort(
            key=lambda document: cursor_position(document.created_at, document.id), reverse=True
        )
        window = selected[: limit + 1]
        return window[:limit], len(window) > limit

    async def save(self, document: Document) -> Document:
        async with self._state.lock:
            if document.id not in self._state.documents:
                raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
            self._state.documents[document.id] = replace(document)
            return replace(document)

    async def soft_delete(self, doc_id: str, user_id: str) -> None:
        async with self._state.lock:
            document = self._state.documents.get(doc_id)
            if (
                document is None
                or document.user_id != user_id
                or doc_id in self._state.deleted_docs
            ):
                raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
            self._state.deleted_docs.add(doc_id)
            self._state.docs_by_sha.pop((document.kb_id, document.content_sha256), None)

    async def count_in_kb(self, kb_id: str) -> int:
        async with self._state.lock:
            return sum(
                1
                for document in self._state.documents.values()
                if document.kb_id == kb_id and document.id not in self._state.deleted_docs
            )

    async def live_documents(self, kb_id: str) -> _Documents:
        """该 KB 下所有未删除文档（删 KB 时用来级联清理对象存储）。"""
        async with self._state.lock:
            return [
                replace(document)
                for document in self._state.documents.values()
                if document.kb_id == kb_id and document.id not in self._state.deleted_docs
            ]


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------


class InMemoryChunkRepo:
    """:class:`~app.infrastructure.storage.base.ChunkRepo` 的内存实现。"""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def replace_for_document(self, doc_id: str, chunks: Sequence[Chunk]) -> int:
        """用给定切片整体替换该文档的切片（重跑入库任务幂等的前提）。"""
        async with self._state.lock:
            self._state.chunks[doc_id] = [replace(chunk) for chunk in chunks]
            return len(chunks)

    async def list_for_document(
        self, doc_id: str, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> _ChunkPage:
        async with self._state.lock:
            items = [replace(chunk) for chunk in self._state.chunks.get(doc_id, [])]
        selected = [chunk for chunk in items if chunk.user_id == user_id]
        selected.sort(key=lambda chunk: chunk.chunk_index)
        if cursor:
            # 切片游标直接就是 chunk_index：它天然单调，比时间戳稳
            try:
                start = int(cursor)
            except ValueError as exc:
                raise AppError(ErrorCode.INVALID_ARGUMENT, "游标无效") from exc
            selected = [chunk for chunk in selected if chunk.chunk_index > start]
        window = selected[: limit + 1]
        return window[:limit], len(window) > limit

    async def delete_for_document(self, doc_id: str) -> int:
        async with self._state.lock:
            return len(self._state.chunks.pop(doc_id, []))

    async def count_for_document(self, doc_id: str) -> int:
        async with self._state.lock:
            return len(self._state.chunks.get(doc_id, []))

    async def count_for_kb(self, kb_id: str) -> int:
        """该 KB 的切片总数（需先过一遍文档表，保证已软删文档的切片不计入）。"""
        async with self._state.lock:
            doc_ids = {
                document.id
                for document in self._state.documents.values()
                if document.kb_id == kb_id and document.id not in self._state.deleted_docs
            }
            return sum(len(self._state.chunks.get(doc_id, [])) for doc_id in doc_ids)


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------


class InMemoryRagRepository:
    """一次构造出三个仓储（共享同一份内存状态）。"""

    def __init__(self) -> None:
        self._state = _State()
        self.knowledge_bases = InMemoryKnowledgeBaseRepo(self._state)
        self.documents = InMemoryDocumentRepo(self._state)
        self.chunks = InMemoryChunkRepo(self._state)

    async def recount_kb(self, kb_id: str) -> tuple[int, int]:
        """重算 KB 的 ``document_count`` / ``chunk_count``，返回 ``(docs, chunks)``。

        ``docs/09`` §6 要求计数**用重算而不是 ``+1``**：入库任务可重试，``+1`` 在重试后会虚高，
        而重算天然幂等。
        """
        async with self._state.lock:
            doc_ids = {
                document.id
                for document in self._state.documents.values()
                if document.kb_id == kb_id and document.id not in self._state.deleted_docs
            }
            chunk_count = sum(len(self._state.chunks.get(doc_id, [])) for doc_id in doc_ids)
            kb = self._state.kbs.get(kb_id)
            if kb is not None:
                kb.document_count = len(doc_ids)
                kb.chunk_count = chunk_count
            return len(doc_ids), chunk_count


def encode_created_cursor(created_at: str, resource_id: str) -> str:
    """统一游标编码（KB / 文档列表复用）。"""
    return encode_cursor(created_at, resource_id)


def encode_index_cursor(chunk_index: int) -> str:
    """切片分页游标（``chunk_index`` 的字符串形式）。"""
    return str(chunk_index)


__all__ = [
    "InMemoryChunkRepo",
    "InMemoryDocumentRepo",
    "InMemoryKnowledgeBaseRepo",
    "InMemoryRagRepository",
    "encode_created_cursor",
    "encode_index_cursor",
]
