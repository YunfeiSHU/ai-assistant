"""RAG 用例服务：知识库 CRUD、文档上传、Worker 侧流水线、检索调试接口。

四层边界与 ``docs/06`` 的接口一一对应：:class:`KnowledgeBaseService`（KB CRUD 与参数覆盖）、
:class:`DocumentService`（上传：只做校验 + 建任务，``REQ-RAG-004``）、
:class:`IngestionService`（Worker 侧解析 → 清洗 → 切分 → 向量化 → 落库）、
:class:`SearchService`（``/search`` 调试）。

「建任务」与「真正入库」分在两个类是 ``REQ-RAG-004`` 的直接要求（接口 P95 ≤ 200ms）：
写在同一个类里，迟早会有人顺手在接口路径上多调一次解析，P95 立刻崩掉。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.core.text import sha256_hex
from app.infrastructure.storage.base import (
    Chunk,
    Document,
    KnowledgeBase,
    ObjectStore,
    RagRepositories,
    build_object_key,
    sanitize_filename,
)
from app.infrastructure.storage.memory import encode_created_cursor
from app.rag.base import RetrievedChunk
from app.rag.chunking import ChunkDraft, ChunkingService
from app.rag.embedding.base import EmbeddingProvider, embed_texts
from app.rag.parsers import detect_mime, get_parser, normalize_extension, parse_document
from app.rag.retriever import RetrievalResult, Retriever
from app.rag.vectorstore.base import VectorStore
from app.tasks.models import ResourceType, Task, TaskType, now_iso
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService

logger = logging.getLogger("app.rag.service")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 文档状态（``docs/06`` §3.3）
DOC_PENDING = "PENDING"
DOC_PARSING = "PARSING"
DOC_CHUNKING = "CHUNKING"
DOC_EMBEDDING = "EMBEDDING"
DOC_INDEXED = "INDEXED"
DOC_FAILED = "FAILED"
DOC_DELETING = "DELETING"

#: KB 状态（``docs/06`` §2.1）
KB_ACTIVE = "active"
KB_INDEXING = "indexing"
KB_FAILED = "failed"

#: 切分参数边界（``docs/06`` §2.1）
CHUNK_SIZE_MIN = 128
CHUNK_SIZE_MAX = 2048
CHUNK_SIZE_DEFAULT = 512
CHUNK_OVERLAP_DEFAULT = 64

#: ``text`` 直接提交的长度上限（``docs/06`` §3.1：≤ 1MB）
TEXT_MAX_BYTES = 1024 * 1024

#: 进度区间：向量化从 55% 走到 95%
_EMBED_PROGRESS_START = 55
_EMBED_PROGRESS_SPAN = 40


def resolve_chunk_params(chunk_size: Any, chunk_overlap: Any) -> tuple[int, int]:
    """校验切分参数并规范化。

    只有一份实现：KB 创建、KB 修改、文档级覆盖三个入口都要过，各写一遍必然出现
    「某个入口漏了校验」，而产生一个永远切不出正常切片的 KB。
    """
    size = int(chunk_size if chunk_size is not None else CHUNK_SIZE_DEFAULT)
    overlap = int(chunk_overlap if chunk_overlap is not None else CHUNK_OVERLAP_DEFAULT)
    if not CHUNK_SIZE_MIN <= size <= CHUNK_SIZE_MAX:
        raise AppError(
            ErrorCode.CHUNK_STRATEGY_INVALID,
            f"chunk_size 必须在 {CHUNK_SIZE_MIN}..{CHUNK_SIZE_MAX} 之间",
            {"chunk_size": size},
        )
    if overlap < 0 or overlap >= size:
        raise AppError(
            ErrorCode.CHUNK_STRATEGY_INVALID,
            "chunk_overlap 必须满足 0 <= overlap < chunk_size",
            {"chunk_size": size, "chunk_overlap": overlap},
        )
    return size, overlap


def _resolve_doc_name(doc_name: str | None, fallback: str) -> str:
    """确定文档显示名，并保证带可用扩展名。

    ``doc_name`` 在 ``docs/06`` §3.1 里是「显示名」，但下游解析器分派只看扩展名。直接
    拿显示名当文件名，用户在 ``doc_name`` 里写「售后政策」就会在解析阶段得到一个
    「不支持的文件类型」—— 一个自己填的字段把文件搞成打不开，很难自证。
    所以：显示名优先，缺扩展名时从真实文件名借用，仍无扩展名则按纯文本处理。
    """
    base = sanitize_filename(doc_name or fallback)
    if not normalize_extension(base):
        base = f"{base}{normalize_extension(fallback) or '.txt'}"
    return base


def _to_vector_payload(chunk: Chunk) -> RetrievedChunk:
    """切片 → 向量库载荷（补上检索需要的 ``doc_name`` 与定位字段）。"""
    return RetrievedChunk(
        chunk_id=chunk.id,
        text=chunk.content,
        doc_id=chunk.doc_id,
        kb_id=chunk.kb_id,
        doc_name=str(chunk.metadata.get("doc_name", "")),
        page=chunk.page,
        chunk_index=chunk.chunk_index,
        heading_path=chunk.heading_path,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        user_id=chunk.user_id,
    )


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class KnowledgeBaseService:
    """知识库 CRUD（``REQ-RAG-001`` / ``REQ-RAG-002``）。"""

    def __init__(
        self,
        settings: Settings,
        repos: RagRepositories,
        objects: ObjectStore,
        vector_store: VectorStore,
        tasks: TaskService,
    ) -> None:
        self._settings = settings
        self._repos = repos
        self._objects = objects
        self._vectors = vector_store
        self._tasks = tasks

    async def create(self, *, user_id: str, payload: dict[str, Any]) -> KnowledgeBase:
        """创建知识库。"""
        count = await self._repos.knowledge_bases.count(user_id)
        if count >= self._settings.max_kb_count:
            raise AppError(
                ErrorCode.KB_LIMIT_EXCEEDED,
                f"知识库数量已达上限（{self._settings.max_kb_count}）",
                {"limit": self._settings.max_kb_count},
            )
        chunk_size, chunk_overlap = resolve_chunk_params(
            payload.get("chunk_size"), payload.get("chunk_overlap")
        )
        retrieval_top_k = int(payload.get("retrieval_top_k") or self._settings.retrieval_top_k)
        rerank_top_n = int(payload.get("rerank_top_n") or self._settings.reranker_top_n)
        _ensure_top_n(rerank_top_n, retrieval_top_k)
        moment = now_iso()
        kb = KnowledgeBase(
            id=new_id("kb"),
            user_id=user_id,
            name=str(payload["name"]),
            description=str(payload.get("description") or ""),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            embedding_model=payload.get("embedding_model"),
            embedding_dim=self._settings.embedding_dim,
            retrieval_top_k=retrieval_top_k,
            rerank_top_n=rerank_top_n,
            score_threshold=float(payload.get("score_threshold") or 0.0),
            status=KB_ACTIVE,
            metadata=dict(payload.get("metadata") or {}),
            created_at=moment,
            updated_at=moment,
        )
        created = await self._repos.knowledge_bases.add(kb)
        logger.info("kb.created", extra={"kb_id": created.id, "user_id": user_id})
        return created

    async def list(
        self, *, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[KnowledgeBase], str | None]:
        """分页列出。"""
        items, has_more = await self._repos.knowledge_bases.list(
            user_id, limit=limit, cursor=cursor
        )
        return items, _next_cursor(items, has_more)

    async def get(self, kb_id: str, user_id: str) -> KnowledgeBase:
        """取详情（跨用户 404，``REQ-RAG-011``）。"""
        return await self._repos.knowledge_bases.get(kb_id, user_id)

    async def update(self, kb_id: str, user_id: str, patch: dict[str, Any]) -> KnowledgeBase:
        """局部更新；``chunk_size``/``chunk_overlap`` 只影响后续文档（``REQ-RAG-002``）。"""
        kb = await self._repos.knowledge_bases.get(kb_id, user_id)
        if patch.get("name") is not None:
            kb.name = str(patch["name"])
        if patch.get("description") is not None:
            kb.description = str(patch["description"])
        if patch.get("metadata") is not None:
            kb.metadata = dict(patch["metadata"])
        if patch.get("score_threshold") is not None:
            kb.score_threshold = float(patch["score_threshold"])
        if patch.get("retrieval_top_k") is not None:
            kb.retrieval_top_k = int(patch["retrieval_top_k"])
        if patch.get("rerank_top_n") is not None:
            kb.rerank_top_n = int(patch["rerank_top_n"])
        if patch.get("chunk_size") is not None or patch.get("chunk_overlap") is not None:
            kb.chunk_size, kb.chunk_overlap = resolve_chunk_params(
                patch.get("chunk_size", kb.chunk_size),
                patch.get("chunk_overlap", kb.chunk_overlap),
            )
        _ensure_top_n(kb.rerank_top_n, kb.retrieval_top_k)
        kb.updated_at = now_iso()
        saved = await self._repos.knowledge_bases.save(kb)
        logger.info("kb.updated", extra={"kb_id": kb_id, "fields": sorted(patch)})
        return saved

    async def delete(self, kb_id: str, user_id: str, *, force: bool) -> dict[str, int]:
        """删除知识库：先清向量与文档，再删 KB 记录（``docs/06`` §7）。

        Returns:
            清理统计 ``{"documents", "vectors", "objects"}``。
        """
        await self._repos.knowledge_bases.get(kb_id, user_id)
        documents = await self._repos.documents.live_documents(kb_id)
        if documents and not force:
            raise AppError(
                ErrorCode.KB_NOT_EMPTY,
                "知识库非空，请先删除文档或使用 force=true",
                {"document_count": len(documents)},
            )
        # 顺序照 docs/06 §7：向量 → 关系库 → 对象存储
        vectors = await self._vectors.delete_by_kb(kb_id)
        for document in documents:
            await self._repos.chunks.delete_for_document(document.id)
            await self._repos.documents.soft_delete(document.id, user_id)
            try:
                await self._objects.delete(document.object_key)
            except Exception as exc:
                logger.warning(
                    "kb.object_delete_failed",
                    extra={"doc_id": document.id, "error": str(exc)},
                )
        await self._repos.knowledge_bases.soft_delete(kb_id, user_id)
        logger.info(
            "kb.deleted",
            extra={"kb_id": kb_id, "documents": len(documents), "vectors": vectors},
        )
        return {"documents": len(documents), "vectors": vectors, "objects": len(documents)}


def _ensure_top_n(rerank_top_n: int, retrieval_top_k: int) -> None:
    if rerank_top_n > retrieval_top_k:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            "rerank_top_n 不得大于 retrieval_top_k",
            {"rerank_top_n": rerank_top_n, "retrieval_top_k": retrieval_top_k},
        )


def _next_cursor(items: Sequence[Any], has_more: bool) -> str | None:
    """列表游标：用最后一条的 ``(created_at, id)``。"""
    if not has_more or not items:
        return None
    last = items[-1]
    return encode_created_cursor(last.created_at, last.id)


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UploadResult:
    """上传接口的 202 响应（``docs/06`` §3.1）。"""

    doc_id: str
    task_id: str | None
    status: str
    doc_name: str
    content_sha256: str
    duplicated: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """转成 ``POST /documents`` 的响应字段 —— 键名就是对外契约，改这里等于改接口。"""
        return {
            "doc_id": self.doc_id,
            "task_id": self.task_id,
            "status": self.status,
            "doc_name": self.doc_name,
            "content_sha256": self.content_sha256,
            "duplicated": self.duplicated,
            "created_at": self.created_at,
        }


class DocumentService:
    """文档上传与查询（``REQ-RAG-004`` / ``REQ-RAG-007``）。"""

    def __init__(
        self,
        settings: Settings,
        repos: RagRepositories,
        objects: ObjectStore,
        tasks: TaskService,
        runner: TaskRunner,
    ) -> None:
        self._settings = settings
        self._repos = repos
        self._objects = objects
        self._tasks = tasks
        self._runner = runner

    # ------------------------------------------------------------------
    async def upload(
        self,
        *,
        kb_id: str,
        user_id: str,
        filename: str = "",
        raw: bytes | None = None,
        text: str | None = None,
        doc_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
    ) -> UploadResult:
        """校验 + 建任务（``REQ-RAG-004``：MUST NOT 在此解析或向量化）。"""
        kb = await self._repos.knowledge_bases.get(kb_id, user_id)
        payload, final_name = self._resolve_payload(
            filename=filename, raw=raw, text=text, doc_name=doc_name
        )
        # 扩展名 + 魔数校验放在接口层：``docs/06`` §3.2 要求类型不符时返回 ``415``，
        # 而任务一旦建出来就只能以 FAILED 收场，用户拿不到「类型不对」这个明确原因
        # （只有一句「入库失败」）。这一步不做任何解析，不违反 ``REQ-RAG-004`` 的时延约束。
        get_parser(final_name, payload)
        doc_count = await self._repos.documents.count_in_kb(kb_id)
        if doc_count >= self._settings.max_kb_documents:
            raise AppError(
                ErrorCode.KB_DOCUMENT_LIMIT_EXCEEDED,
                f"知识库文档数已达上限（{self._settings.max_kb_documents}）",
                {"limit": self._settings.max_kb_documents, "hint": "请新建知识库"},
            )

        digest = sha256_hex(payload)
        existing = await self._repos.documents.find_by_sha256(kb_id, digest)
        if existing is not None:
            if existing.status == DOC_INDEXED:
                # AC-RAG-05：同内容已入库 → 409，并带上已有 doc_id
                raise AppError(
                    ErrorCode.DOCUMENT_DUPLICATE,
                    "相同内容的文档已入库",
                    {"doc_id": existing.id, "doc_name": existing.doc_name},
                )
            if existing.status != DOC_FAILED:
                # 仍在处理中 → 幂等返回同一 doc_id/task_id（REQ-RAG-007）
                logger.info(
                    "document.duplicated_inflight",
                    extra={"doc_id": existing.id, "status": existing.status},
                )
                return UploadResult(
                    doc_id=existing.id,
                    task_id=existing.task_id,
                    status=existing.status,
                    doc_name=existing.doc_name,
                    content_sha256=existing.content_sha256,
                    duplicated=True,
                    created_at=existing.created_at,
                )
            return await self._reindex_existing(existing, payload, kb)

        size, overlap = resolve_chunk_params(
            chunk_size if chunk_size is not None else kb.chunk_size,
            chunk_overlap if chunk_overlap is not None else kb.chunk_overlap,
        )
        moment = now_iso()
        doc_id = new_id("doc")
        document = Document(
            id=doc_id,
            kb_id=kb_id,
            user_id=user_id,
            doc_name=final_name,
            file_ext=normalize_extension(final_name),
            mime_type=detect_mime(final_name),
            size_bytes=len(payload),
            object_key=build_object_key(user_id, kb_id, doc_id, final_name),
            content_sha256=digest,
            status=DOC_PENDING,
            chunk_size=size,
            chunk_overlap=overlap,
            metadata=dict(metadata or {}),
            created_at=moment,
            updated_at=moment,
        )
        await self._repos.documents.add(document)

        task, _ = await self._tasks.create(
            type_=TaskType.DOCUMENT_INGEST,
            user_id=user_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc_id,
            payload={"doc_id": doc_id, "kb_id": kb_id},
            idem_key=f"ingest:{doc_id}",
        )
        document.task_id = task.id
        await self._repos.documents.save(document)
        await self._mark_kb_indexing(kb)

        # 对象先落地再投递：投递后 Worker 立刻就会去读，顺序反了必然读到空
        await self._objects.put(document.object_key, payload, document.mime_type)
        await self._runner.submit(task)
        logger.info(
            "document.accepted",
            extra={"doc_id": doc_id, "kb_id": kb_id, "task_id": task.id, "size": len(payload)},
        )
        return UploadResult(
            doc_id=doc_id,
            task_id=task.id,
            status=document.status,
            doc_name=document.doc_name,
            content_sha256=digest,
            duplicated=False,
            created_at=moment,
        )

    async def list_documents(
        self,
        *,
        kb_id: str,
        user_id: str,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Document], str | None]:
        """分页列出 KB 内文档。"""
        await self._repos.knowledge_bases.get(kb_id, user_id)
        items, has_more = await self._repos.documents.list(
            kb_id, user_id, status=status, limit=limit, cursor=cursor
        )
        return items, _next_cursor(items, has_more)

    async def get_document(self, doc_id: str, user_id: str) -> Document:
        """取文档详情。"""
        return await self._repos.documents.get(doc_id, user_id)

    async def list_chunks(
        self, *, doc_id: str, user_id: str, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Chunk], str | None]:
        """分页查看切片（调试用；游标就是 ``chunk_index``）。"""
        await self._repos.documents.get(doc_id, user_id)
        items, has_more = await self._repos.chunks.list_for_document(
            doc_id, user_id, limit=limit, cursor=cursor
        )
        return items, (str(items[-1].chunk_index) if has_more and items else None)

    async def delete_document(self, *, doc_id: str, user_id: str) -> tuple[Document, Task]:
        """删除文档：建删除任务并投递（``docs/06`` §3.3 允许 202 + task_id）。"""
        document = await self._repos.documents.get(doc_id, user_id)
        task, _ = await self._tasks.create(
            type_=TaskType.DOCUMENT_DELETE,
            user_id=user_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=doc_id,
            payload={"doc_id": doc_id, "kb_id": document.kb_id},
            idem_key=f"delete:{doc_id}:{document.status}",
        )
        await self._runner.submit(task)
        return document, task

    # ------------------------------------------------------------------
    async def _reindex_existing(
        self, document: Document, payload: bytes, kb: KnowledgeBase
    ) -> UploadResult:
        """复用「同内容但失败」的文档行，重新投递入库任务。

        重跑必须换幂等键：沿用原来的键会命中旧任务记录，于是 ``create`` 返回旧任务而
        ``submit`` 看到它不是 ``PENDING`` 直接跳过 —— 表现为「重传成功但什么都没发生」。
        """
        task, _ = await self._tasks.create(
            type_=TaskType.DOCUMENT_INGEST,
            user_id=document.user_id,
            resource_type=ResourceType.DOCUMENT,
            resource_id=document.id,
            payload={"doc_id": document.id, "kb_id": document.kb_id},
            idem_key=f"ingest:{document.id}:{now_iso()}",
        )
        document.status = DOC_PENDING
        document.task_id = task.id
        document.error_code = None
        document.error_message = None
        document.updated_at = now_iso()
        await self._repos.documents.save(document)
        await self._mark_kb_indexing(kb)
        await self._objects.put(document.object_key, payload, document.mime_type)
        await self._runner.submit(task)
        return UploadResult(
            doc_id=document.id,
            task_id=task.id,
            status=document.status,
            doc_name=document.doc_name,
            content_sha256=document.content_sha256,
            duplicated=False,
            created_at=document.created_at,
        )

    async def _mark_kb_indexing(self, kb: KnowledgeBase) -> None:
        kb.status = KB_INDEXING
        kb.updated_at = now_iso()
        await self._repos.knowledge_bases.save(kb)

    def _resolve_payload(
        self,
        *,
        filename: str,
        raw: bytes | None,
        text: str | None,
        doc_name: str | None,
    ) -> tuple[bytes, str]:
        """确定文件内容与显示名（``file`` 与 ``text`` 二选一）。"""
        if (raw is None) == (text is None):
            raise AppError(ErrorCode.INVALID_ARGUMENT, "必须且只能提供 file 或 text 之一")
        if raw is not None:
            if len(raw) > self._settings.upload_max_bytes:
                raise AppError(
                    ErrorCode.FILE_TOO_LARGE,
                    f"文件超过大小限制（{self._settings.upload_max_mb} MB）",
                    {"size_bytes": len(raw), "limit_mb": self._settings.upload_max_mb},
                )
            return raw, _resolve_doc_name(doc_name, filename or "unnamed")
        assert text is not None
        encoded = text.encode()
        if len(encoded) > TEXT_MAX_BYTES:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE,
                "text 内容超过 1MB 上限，请改用文件上传",
                {"size_bytes": len(encoded)},
            )
        return encoded, _resolve_doc_name(doc_name, "text.txt")


# ---------------------------------------------------------------------------
# Worker 侧流水线
# ---------------------------------------------------------------------------


class IngestionService:
    """解析 → 清洗 → 切分 → 向量化 → 落库（``docs/06`` §1 的下半段）。"""

    def __init__(
        self,
        settings: Settings,
        repos: RagRepositories,
        objects: ObjectStore,
        embedding: EmbeddingProvider,
        vector_store: VectorStore,
        tasks: TaskService,
    ) -> None:
        self._settings = settings
        self._repos = repos
        self._objects = objects
        self._embedding = embedding
        self._vectors = vector_store
        self._tasks = tasks

    # ------------------------------------------------------------------
    async def handle(self, task: Task) -> None:
        """任务处理器入口（``TaskRunner`` 调用的就是它）。"""
        try:
            async with self._tasks.track(task.id):
                if task.type is TaskType.DOCUMENT_DELETE:
                    await self._delete_document(task)
                else:
                    await self._ingest_document(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_document_failed(task, exc)
            raise

    # ------------------------------------------------------------------
    async def _ingest_document(self, task: Task) -> None:
        doc_id = _doc_id_of(task)
        document = await self._repos.documents.get(doc_id, task.user_id)

        await self._set_status(document, DOC_PARSING)
        await self._report(task, DOC_PARSING, 10)

        raw = await self._objects.get(document.object_key)
        # 解析是纯同步 CPU 函数，必须甩进线程池。直接写在协程体里会占住事件循环：
        # 8MB 文档要几十秒，这期间整个进程连 ``/health`` 都不应答 —— 网关的就绪探测会把
        # 「正在解析」误判成「AI 挂了」。
        parsed = await asyncio.to_thread(
            parse_document, document.doc_name, raw, min_chars=self._settings.min_doc_chars
        )
        if parsed.page_count > self._settings.max_doc_pages:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                f"文档页数超过上限（{parsed.page_count} > {self._settings.max_doc_pages}）",
                {"page_count": parsed.page_count},
            )

        await self._check_cancel(task)
        await self._set_status(document, DOC_CHUNKING)
        await self._report(task, DOC_CHUNKING, 35)

        # 用文档上「固化」的参数切分，而不是 KB 当前配置（REQ-RAG-002）
        chunker = ChunkingService(
            chunk_size=document.chunk_size, chunk_overlap=document.chunk_overlap
        )
        # 切分同样是同步 CPU（tiktoken 逐块计数，8MB 正文实测切出 1.7 万块），同一理由进线程池。
        drafts = await asyncio.to_thread(chunker.split_blocks, parsed.blocks)
        # 先记住截断前的总数：它就是 ``document.chunks_total``（UP-01）要暴露的那个数，
        # 也是「要不要告诉调用方丢了多少」的判据。截断之后再取长度只剩实际入库数。
        total_drafts = len(drafts)
        truncated = False
        if total_drafts > self._settings.max_doc_chunks:
            logger.warning(
                "ingest.chunks_truncated",
                extra={
                    "doc_id": doc_id,
                    "total": total_drafts,
                    "limit": self._settings.max_doc_chunks,
                },
            )
            drafts = drafts[: self._settings.max_doc_chunks]
            truncated = True
        if not drafts:
            raise AppError(
                ErrorCode.UNPROCESSABLE_DOCUMENT,
                "文档切分后无有效切片",
                {"filename": document.doc_name},
            )

        # 截断事实落库（``docs/10`` UP-01）。只打一条 warning 的后果是「入库成功」为假：
        # 8MB 测试正文实测切出 16,969 片、只入库 10,000 片（丢 41% 正文），而接口照旧 202。
        # ``chunk_count`` 在下面由实际入库数赋值，两个字段并存才能让调用方既有「本该有
        # 多少」也有「实际有多少」。
        document.chunks_total = total_drafts
        document.truncated = truncated
        # 总数定下后立刻上报（``docs/10`` UP-02）：这是客户端算 ETA 的起点。
        # 此处传的是截断后的待处理数（= 真正要跑的向量化量），与 ``document.chunks_total``
        # （截断前产出数）是两个不同的量，故意不混用：进度条的分母必须是实际工作量，
        # 否则 ETA 永远算不准。
        await self._report(task, DOC_CHUNKING, 35, chunks_total=len(drafts), chunks_done=0)
        # 逐块 sha256 + 组装实体也是同步 CPU（每块一次哈希），一并进线程池。
        chunks = await asyncio.to_thread(self._build_chunks, document, drafts)

        await self._check_cancel(task)
        await self._set_status(document, DOC_EMBEDDING)
        await self._report(
            task, DOC_EMBEDDING, _EMBED_PROGRESS_START, chunks_total=len(chunks), chunks_done=0
        )

        stored = await self._embed_and_upsert(task, chunks)

        await self._repos.chunks.replace_for_document(document.id, chunks)
        document.chunk_count = len(chunks)
        document.char_count = parsed.char_count
        document.page_count = parsed.page_count
        document.indexed_at = now_iso()
        document.error_code = None
        document.error_message = None
        await self._set_status(document, DOC_INDEXED)
        await self._recount_kb(document.kb_id, failed=False)
        # 终帧也带上计数：这是客户端能看到的最后一帧进度，两个数相等且等于
        # ``chunks_total`` 就是「全部切片都已入库」的自证（UP-02）。
        await self._report(task, DOC_INDEXED, 98, chunks_total=len(chunks), chunks_done=len(chunks))
        logger.info(
            "ingest.completed",
            extra={
                "doc_id": document.id,
                "chunks": len(chunks),
                "chunks_total": document.chunks_total,
                "vectors": stored,
                "pages": parsed.page_count,
                "truncated": truncated,
            },
        )

    async def _embed_and_upsert(self, task: Task, chunks: Sequence[Chunk]) -> int:
        """分批向量化并 upsert，每批后上报进度（``docs/06`` §4.5）。

        按窗口并发（``INGEST_EMBED_WINDOW``）的理由：早期实现是「交一批 → 等它返回 →
        再交下一批」的严格串行。本地 BGE 时代这没问题（算力是本机的），但换成云端
        provider 之后每次调用都是一次网络往返，串行等于只维持一个请求在飞。实测
        （8MB / 5,821 片 / 硅基流动）串行 98.2s，而 provider 在并发 8 下裸测是 379.6 片/s
        （约 15s）—— 差的 6 倍全在等待上。

        窗口内并发、窗口内顺序 upsert：既拿回吞吐，又保留原有的「逐批进度上报 +
        逐批取消检查」语义（``docs/06`` §4.5 的硬要求）。
        """
        batch_size = max(1, self._settings.embedding_batch_size)
        window = max(1, self._settings.ingest_embed_window)
        total = len(chunks)
        stored = 0
        for window_start in range(0, total, batch_size * window):
            await self._check_cancel(task)
            batches = [
                chunks[start : start + batch_size]
                for start in range(
                    window_start, min(window_start + batch_size * window, total), batch_size
                )
            ]
            vectors_per_batch = await asyncio.gather(
                *(
                    embed_texts(self._embedding, [chunk.content for chunk in batch])
                    for batch in batches
                )
            )
            done = window_start
            for batch, vectors in zip(batches, vectors_per_batch, strict=True):
                # 按 chunk_id upsert：重跑任务不产生重复向量（AC-RAG-08）
                stored += await self._vectors.upsert(
                    [_to_vector_payload(chunk) for chunk in batch], vectors
                )
                done += len(batch)
                progress = _EMBED_PROGRESS_START + int(
                    _EMBED_PROGRESS_SPAN * min(1.0, done / total)
                )
                # 带上切片计数（UP-02）：``progress`` 会在 95 上饱和，之后只有 ``chunks_done``
                # 继续走 —— 没有它，客户端在收尾阶段就只能看到一个不动的进度条，
                # 分不出「在算」与「卡住」。
                await self._report(
                    task,
                    DOC_EMBEDDING,
                    min(progress, 95),
                    chunks_done=done,
                    chunks_total=total,
                )
        return stored

    async def _delete_document(self, task: Task) -> None:
        """删除文档：Milvus → MySQL → MinIO（``docs/06`` §7）。"""
        doc_id = _doc_id_of(task)
        document = await self._repos.documents.get(doc_id, task.user_id)

        await self._report(task, DOC_DELETING, 30)
        await self._vectors.delete_by_document(doc_id)

        await self._report(task, DOC_DELETING, 60)
        await self._repos.chunks.delete_for_document(doc_id)
        await self._repos.documents.soft_delete(doc_id, task.user_id)

        await self._report(task, DOC_DELETING, 85)
        # 对象删除放最后：先删记录再删对象，最坏是留下孤儿对象（可再清理）；反过来最坏是
        # 记录还在但对象没了（引用直接失效，用户可见）
        try:
            await self._objects.delete(document.object_key)
        except Exception as exc:
            logger.warning("delete.object_failed", extra={"doc_id": doc_id, "error": str(exc)})
        await self._recount_kb(document.kb_id, failed=False)
        logger.info("delete.completed", extra={"doc_id": doc_id})

    # ------------------------------------------------------------------
    def _build_chunks(self, document: Document, drafts: Sequence[ChunkDraft]) -> list[Chunk]:
        """把切分草稿落成 ``Chunk`` 实体（切分参数固化进元数据）。"""
        moment = now_iso()
        chunks: list[Chunk] = []
        for draft in drafts:
            content = draft.content
            chunks.append(
                Chunk(
                    id=new_id("chk"),
                    doc_id=document.id,
                    kb_id=document.kb_id,
                    user_id=document.user_id,
                    chunk_index=draft.chunk_index,
                    content=content,
                    content_sha256=sha256_hex(content),
                    char_start=draft.char_start,
                    char_end=draft.char_end,
                    token_count=draft.token_count,
                    page=draft.page,
                    heading_path=draft.heading_path,
                    created_at=moment,
                    # 固化切分参数：KB 之后改了 chunk_size，老切片仍能解释自己
                    metadata={
                        "doc_name": document.doc_name,
                        "chunk_size": document.chunk_size,
                        "chunk_overlap": document.chunk_overlap,
                        "merged": draft.merged,
                    },
                )
            )
        return chunks

    async def _set_status(self, document: Document, status: str) -> None:
        document.status = status
        document.updated_at = now_iso()
        await self._repos.documents.save(document)

    async def _recount_kb(self, kb_id: str, *, failed: bool) -> None:
        """重算 KB 计数与状态（``docs/09`` §6：必须重算，不能 ``+1``）。"""
        kb = await self._repos.knowledge_bases.get_internal(kb_id)
        if kb is None:
            logger.info("kb.recount_skipped", extra={"kb_id": kb_id})
            return
        kb.document_count = await self._repos.documents.count_in_kb(kb_id)
        kb.chunk_count = await self._repos.chunks.count_for_kb(kb_id)
        kb.status = KB_FAILED if failed else KB_ACTIVE
        kb.updated_at = now_iso()
        await self._repos.knowledge_bases.save(kb)

    async def _mark_document_failed(self, task: Task, exc: Exception) -> None:
        """把任务失败写回文档（``AC-RAG-16`` 要求 ``status=FAILED`` + 错误码）。"""
        doc_id = _doc_id_of(task)
        try:
            document = await self._repos.documents.get(doc_id, task.user_id)
        except AppError:
            return
        if document.status == DOC_INDEXED:
            return
        code = str(exc.code) if isinstance(exc, AppError) else str(ErrorCode.INTERNAL_ERROR)
        document.status = DOC_FAILED
        document.error_code = code
        document.error_message = str(exc)
        document.updated_at = now_iso()
        await self._repos.documents.save(document)
        await self._recount_kb(document.kb_id, failed=True)
        logger.warning("ingest.failed", extra={"doc_id": doc_id, "code": code, "error": str(exc)})

    async def _report(
        self,
        task: Task,
        stage: str,
        progress: int,
        *,
        chunks_done: int | None = None,
        chunks_total: int | None = None,
    ) -> None:
        """上报阶段、进度与切片计数（``docs/10`` UP-02）。"""
        try:
            await self._tasks.report_progress(
                task.id,
                stage=stage,
                progress=progress,
                chunks_done=chunks_done,
                chunks_total=chunks_total,
            )
        except AppError as exc:
            # 进度上报冲突不该让入库失败（真正的失败由状态机兜底）
            logger.debug("ingest.progress_skipped", extra={"error": str(exc)})

    async def _check_cancel(self, task: Task) -> None:
        """检查点：已请求取消则中断执行（``docs/08`` §4.3）。"""
        if await self._tasks.is_cancel_requested(task.id):
            logger.info("ingest.canceled", extra={"task_id": task.id})
            raise asyncio.CancelledError


def _doc_id_of(task: Task) -> str:
    """从任务载荷取 ``doc_id``（载荷缺失时退回 ``resource_id``）。"""
    return str(task.payload.get("doc_id") or task.resource_id)


# ---------------------------------------------------------------------------
# 检索调试接口
# ---------------------------------------------------------------------------


class SearchService:
    """``POST /knowledge-bases/{kb_id}/search``（``REQ-RAG-008``）。"""

    def __init__(self, settings: Settings, repos: RagRepositories, retriever: Retriever) -> None:
        self._settings = settings
        self._repos = repos
        self._retriever = retriever

    async def search(
        self,
        *,
        kb_id: str,
        user_id: str,
        query: str,
        top_k: int | None = None,
        rerank_top_n: int | None = None,
        score_threshold: float | None = None,
        with_rerank: bool = True,
    ) -> dict[str, Any]:
        """执行检索并组装调试响应。

        参数优先级：``请求 > KB 默认 > 全局默认``（``REQ-RAG-002``）；
        ``with_rerank=false`` 时不调用重排器，``rerank_score`` 固定为 ``null``（``AC-RAG-12``）。
        """
        kb = await self._repos.knowledge_bases.get(kb_id, user_id)
        result: RetrievalResult = await self._retriever.retrieve_detailed(
            query=query,
            user_id=user_id,
            kb_ids=[kb_id],
            top_k=int(top_k if top_k is not None else kb.retrieval_top_k),
            rerank_top_n=int(rerank_top_n if rerank_top_n is not None else kb.rerank_top_n),
            score_threshold=float(
                score_threshold if score_threshold is not None else kb.score_threshold
            ),
            with_rerank=with_rerank and self._settings.reranker_enabled,
        )
        items: list[dict[str, Any]] = []
        for index, chunk in enumerate(result.items, start=1):
            items.append(
                {
                    "index": index,
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "doc_name": chunk.doc_name,
                    "page": chunk.page,
                    "heading_path": chunk.heading_path or None,
                    "vector_score": round(chunk.vector_score, 6),
                    # 未重排时给 null，而不是把向量分复制一份（AC-RAG-12/AC-RAG-11）
                    "rerank_score": round(chunk.score, 6) if result.rerank_used else None,
                    "score": round(chunk.score, 6),
                    "merged": chunk.merged,
                    "content": chunk.text,
                }
            )
        return {
            "query": query,
            "recalled": result.recalled,
            "returned": len(items),
            "rerank_used": result.rerank_used,
            "elapsed_ms": result.elapsed_ms,
            "items": items,
        }


__all__ = [
    "CHUNK_OVERLAP_DEFAULT",
    "CHUNK_SIZE_DEFAULT",
    "CHUNK_SIZE_MAX",
    "CHUNK_SIZE_MIN",
    "DOC_CHUNKING",
    "DOC_DELETING",
    "DOC_EMBEDDING",
    "DOC_FAILED",
    "DOC_INDEXED",
    "DOC_PARSING",
    "DOC_PENDING",
    "KB_ACTIVE",
    "KB_FAILED",
    "KB_INDEXING",
    "TEXT_MAX_BYTES",
    "DocumentService",
    "IngestionService",
    "KnowledgeBaseService",
    "SearchService",
    "UploadResult",
    "resolve_chunk_params",
]
