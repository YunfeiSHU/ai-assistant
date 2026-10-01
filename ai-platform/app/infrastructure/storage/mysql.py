"""MySQL 仓储（``INFRA_BACKEND=real``）：知识库 / 文档 / 切片。

**用 SQLAlchemy Core 而不是 ORM**：这三个仓储没有对象图（没有懒加载、关系导航、身份映射），
真正需要的只是「参数化 SQL + 连接池 + 行到实体的映射」。Core 的 ``Table`` 定义同时给出列清单
与 SQL 构造器，而 ORM 会额外引入 session 生命周期（在 FastAPI 请求作用域外很容易用错）。

**与内存实现的关系**：:mod:`app.infrastructure.storage.memory` 是语义基准，这里逐条对齐 ——
软删过滤（``deleted_at IS NULL``）、同名/同哈希唯一（先查后插 + 唯一约束兜底）、分页
（行值比较 ``(created_at, id) < (?, ?)``）、多取一条判 ``has_more``（``LIMIT limit + 1``）、
切片整体替换（同一事务内 ``DELETE`` + 批量 ``INSERT``）。

**先查后插 + 唯一约束兜底**这条最容易写错：只靠先查后插，并发下会两个都插进去；只靠唯一约束，
则无法给出 ``409`` details 里那个「已存在的 ID」。所以两者都要：应用层查一次给出友好错误，
约束做最后一道防线，并把 ``IntegrityError`` 翻译回同一个领域错误。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NoReturn

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    tuple_,
    update,
)
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.pagination import decode_cursor
from app.infrastructure.mysql.db import (
    FETCH_AHEAD,
    create_engine_from_settings,
    db_now,
    from_db,
    release_engine,
    reraise,
    to_db,
)
from app.infrastructure.storage.base import Chunk, Document, KnowledgeBase

logger = get_logger("app.infrastructure.storage.mysql")

# 方法名 ``list`` 会在类作用域里遮蔽内建 ``list``（``app.infrastructure.storage.memory`` 同样处理）
_KBPage = tuple[list[KnowledgeBase], bool]
_DocumentPage = tuple[list[Document], bool]
_ChunkPage = tuple[list[Chunk], bool]
_Documents = list[Document]

metadata_ = MetaData()


# ---------------------------------------------------------------------------
# 表定义（与 deploy/mysql/001_init_schema.sql 逐列对齐）
#
# ``deleted_key``（生成列）**刻意不在这里定义**：它只服务于唯一约束，应用侧既不读也不写。
# 定义它反而会诱使某处 ``INSERT`` 带上它 —— MySQL 对生成列会直接报错。
# ---------------------------------------------------------------------------

knowledge_base_table = Table(
    "knowledge_base",
    metadata_,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("name", String(64), nullable=False),
    Column("description", String(500), nullable=False, server_default=""),
    Column("chunk_size", Integer, nullable=False),
    Column("chunk_overlap", Integer, nullable=False),
    Column("embedding_model", String(128)),
    Column("embedding_dim", Integer, nullable=False),
    Column("retrieval_top_k", Integer, nullable=False),
    Column("rerank_top_n", Integer, nullable=False),
    Column("score_threshold", Float, nullable=False),
    Column("status", String(16), nullable=False),
    Column("document_count", Integer, nullable=False, server_default="0"),
    Column("chunk_count", BigInteger, nullable=False, server_default="0"),
    Column("metadata", JSON),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("deleted_at", DateTime),
)

document_table = Table(
    "document",
    metadata_,
    Column("id", String(32), primary_key=True),
    Column("kb_id", String(32), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("doc_name", String(512), nullable=False),
    Column("file_ext", String(16), nullable=False),
    Column("mime_type", String(128), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("page_count", Integer),
    Column("object_key", String(1024), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("chunk_count", Integer, nullable=False, server_default="0"),
    Column("char_count", Integer, nullable=False, server_default="0"),
    # 截断事实（``docs/10`` UP-01）：``chunks_total`` 是切分产出数、``chunk_count`` 是实际
    # 入库数。``truncated`` 显式冗余一份是为了让「被截断」可被 SQL 直接筛出来（推理式
    # ``chunks_total > chunk_count`` 在 NULL 上不成立）。
    Column("chunks_total", Integer),
    Column("truncated", Boolean, nullable=False, server_default="0"),
    Column("chunk_size", Integer, nullable=False),
    Column("chunk_overlap", Integer, nullable=False),
    Column("task_id", String(32)),
    Column("error_code", String(64)),
    Column("error_message", String(1000)),
    Column("metadata", JSON),
    Column("indexed_at", DateTime),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("deleted_at", DateTime),
)

document_chunk_table = Table(
    "document_chunk",
    metadata_,
    Column("id", String(32), primary_key=True),
    Column("doc_id", String(32), nullable=False),
    Column("kb_id", String(32), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("char_start", Integer, nullable=False),
    Column("char_end", Integer, nullable=False),
    Column("page", Integer),
    Column("heading_path", String(512)),
    Column("token_count", Integer, nullable=False),
    # 切分参数快照（``REQ-RAG-002``）：老切片在 KB 改配置后仍要能解释自己。
    # ``docs/09`` §2.3 的表格漏了这一列，见 ``deploy/mysql/003_add_chunk_metadata.sql``。
    Column("metadata", JSON),
    Column("created_at", DateTime, nullable=False),
)

#: ``metadata`` 是 ``Table`` 的保留属性名（``Table.metadata`` 是 MetaData 对象），
#: 所以取列一律走下标访问。写成常量，避免每处都重复这个知识点。
_META = "metadata"


#: UPDATE 时不得改写的列
_IMMUTABLE_ON_UPDATE = ("id", "created_at")


def _update_values(values: dict[str, Any]) -> dict[str, Any]:
    """把 INSERT 的值字典转成 UPDATE 的值字典。

    剔除主键与创建时间，并刷新 ``updated_at``：``SET id = <同一个 id>`` 本身无害，但一旦调用方
    传来的实体与库里那一行不是同一条，它会把**另一行的主键**改掉；``SET created_at`` 更隐蔽，
    它让创建时间随最后一次保存漂移，于是「按创建时间倒序」的列表顺序会在编辑后变化。
    """
    trimmed = {key: value for key, value in values.items() if key not in _IMMUTABLE_ON_UPDATE}
    trimmed["updated_at"] = db_now()
    return trimmed


# ---------------------------------------------------------------------------
# 行 ↔ 实体
# ---------------------------------------------------------------------------


def _kb_values(kb: KnowledgeBase) -> dict[str, Any]:
    return {
        "id": kb.id,
        "user_id": kb.user_id,
        "name": kb.name,
        "description": kb.description,
        "chunk_size": kb.chunk_size,
        "chunk_overlap": kb.chunk_overlap,
        "embedding_model": kb.embedding_model,
        "embedding_dim": kb.embedding_dim,
        "retrieval_top_k": kb.retrieval_top_k,
        "rerank_top_n": kb.rerank_top_n,
        "score_threshold": kb.score_threshold,
        "status": kb.status,
        "document_count": kb.document_count,
        "chunk_count": kb.chunk_count,
        _META: kb.metadata or {},
        "created_at": to_db(kb.created_at) or db_now(),
        "updated_at": to_db(kb.updated_at) or db_now(),
    }


def _kb_from_row(row: dict[str, Any]) -> KnowledgeBase:
    return KnowledgeBase(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"] or "",
        chunk_size=int(row["chunk_size"]),
        chunk_overlap=int(row["chunk_overlap"]),
        embedding_model=row["embedding_model"],
        embedding_dim=int(row["embedding_dim"]),
        retrieval_top_k=int(row["retrieval_top_k"]),
        rerank_top_n=int(row["rerank_top_n"]),
        score_threshold=float(row["score_threshold"]),
        status=row["status"],
        document_count=int(row["document_count"]),
        chunk_count=int(row["chunk_count"]),
        metadata=row[_META] or {},
        created_at=from_db(row["created_at"]) or "",
        updated_at=from_db(row["updated_at"]) or "",
    )


def _document_values(document: Document) -> dict[str, Any]:
    return {
        "id": document.id,
        "kb_id": document.kb_id,
        "user_id": document.user_id,
        "doc_name": document.doc_name,
        "file_ext": document.file_ext,
        "mime_type": document.mime_type,
        "size_bytes": document.size_bytes,
        "page_count": document.page_count,
        "object_key": document.object_key,
        "content_sha256": document.content_sha256,
        "status": document.status,
        "chunk_count": document.chunk_count,
        "char_count": document.char_count,
        "chunks_total": document.chunks_total,
        "truncated": bool(document.truncated),
        "chunk_size": document.chunk_size,
        "chunk_overlap": document.chunk_overlap,
        "task_id": document.task_id,
        "error_code": document.error_code,
        "error_message": document.error_message,
        _META: document.metadata or {},
        "indexed_at": to_db(document.indexed_at),
        "created_at": to_db(document.created_at) or db_now(),
        "updated_at": to_db(document.updated_at) or db_now(),
    }


def _document_from_row(row: dict[str, Any]) -> Document:
    return Document(
        id=row["id"],
        kb_id=row["kb_id"],
        user_id=row["user_id"],
        doc_name=row["doc_name"],
        file_ext=row["file_ext"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        object_key=row["object_key"],
        content_sha256=row["content_sha256"],
        status=row["status"],
        page_count=None if row["page_count"] is None else int(row["page_count"]),
        chunk_count=int(row["chunk_count"]),
        char_count=int(row["char_count"]),
        chunks_total=None if row["chunks_total"] is None else int(row["chunks_total"]),
        truncated=bool(row["truncated"]),
        chunk_size=int(row["chunk_size"]),
        chunk_overlap=int(row["chunk_overlap"]),
        task_id=row["task_id"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        metadata=row[_META] or {},
        indexed_at=from_db(row["indexed_at"]),
        created_at=from_db(row["created_at"]) or "",
        updated_at=from_db(row["updated_at"]) or "",
    )


def _chunk_values(chunk: Chunk) -> dict[str, Any]:
    return {
        "id": chunk.id,
        "doc_id": chunk.doc_id,
        "kb_id": chunk.kb_id,
        "user_id": chunk.user_id,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "content_sha256": chunk.content_sha256,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "page": chunk.page,
        "heading_path": chunk.heading_path or None,
        "token_count": chunk.token_count,
        _META: chunk.metadata or {},
        "created_at": to_db(chunk.created_at) or db_now(),
    }


def _chunk_from_row(row: dict[str, Any]) -> Chunk:
    return Chunk(
        id=row["id"],
        doc_id=row["doc_id"],
        kb_id=row["kb_id"],
        user_id=row["user_id"],
        chunk_index=int(row["chunk_index"]),
        content=row["content"],
        content_sha256=row["content_sha256"],
        char_start=int(row["char_start"]),
        char_end=int(row["char_end"]),
        token_count=int(row["token_count"]),
        page=None if row["page"] is None else int(row["page"]),
        heading_path=row["heading_path"] or "",
        metadata=row[_META] or {},
        created_at=from_db(row["created_at"]) or "",
    )


def _rows(result: Any) -> list[dict[str, Any]]:
    """``Result`` → ``list[dict]``（映射函数只认 dict，不认 ``Row``）。"""
    return [dict(row._mapping) for row in result]


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


class MySqlKnowledgeBaseRepo:
    """:class:`~app.infrastructure.storage.base.KnowledgeBaseRepo` 的 MySQL 实现。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        values = _kb_values(kb)
        # 先查：给出「已存在的 ID」这种可操作的 details（唯一约束只能给出一个键名）
        existing = await self._find_live_by_name(kb.user_id, kb.name)
        if existing is not None:
            raise AppError(
                ErrorCode.KB_NAME_CONFLICT,
                f"知识库名称已存在：{kb.name}",
                {"name": kb.name, "kb_id": existing},
            )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(insert(knowledge_base_table).values(**values))
        except IntegrityError as exc:  # 并发下的兜底：两个请求同时通过了上面的查询
            await self._raise_name_conflict(kb, exc)
        return await self.get_internal(kb.id) or kb

    async def _find_live_by_name(self, user_id: str, name: str) -> str | None:
        table = knowledge_base_table
        statement = select(table.c.id).where(
            table.c.user_id == user_id,
            table.c.name == name,
            table.c.deleted_at.is_(None),
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).first()
        return None if row is None else str(row[0])

    async def _raise_name_conflict(self, kb: KnowledgeBase, exc: IntegrityError) -> NoReturn:
        existing = await self._find_live_by_name(kb.user_id, kb.name)
        if existing is not None:
            raise AppError(
                ErrorCode.KB_NAME_CONFLICT,
                f"知识库名称已存在：{kb.name}",
                {"name": kb.name, "kb_id": existing},
            ) from exc
        reraise(exc)

    async def get(self, kb_id: str, user_id: str) -> KnowledgeBase:
        table = knowledge_base_table
        statement = select(table).where(
            table.c.id == kb_id,
            table.c.user_id == user_id,
            table.c.deleted_at.is_(None),
        )
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        if not rows:
            # 跨用户一律 404 而不是 403：403 等于确认「这个 ID 存在」（REQ-RAG-011）
            raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")
        return _kb_from_row(rows[0])

    async def get_internal(self, kb_id: str) -> KnowledgeBase | None:
        """不过滤 ``user_id`` 的读取（Worker 维护计数与状态用，见协议 docstring）。"""
        table = knowledge_base_table
        statement = select(table).where(table.c.id == kb_id, table.c.deleted_at.is_(None))
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        return _kb_from_row(rows[0]) if rows else None

    async def list(self, user_id: str, *, limit: int = 20, cursor: str | None = None) -> _KBPage:
        table = knowledge_base_table
        statement = select(table).where(table.c.user_id == user_id, table.c.deleted_at.is_(None))
        statement = _apply_cursor(statement, table.c.created_at, table.c.id, cursor)
        statement = statement.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(
            limit + FETCH_AHEAD
        )
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        items = [_kb_from_row(row) for row in rows[:limit]]
        return items, len(rows) > limit

    async def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        table = knowledge_base_table
        values = _update_values(_kb_values(kb))
        statement = (
            update(table)
            .where(table.c.id == kb.id, table.c.user_id == kb.user_id, table.c.deleted_at.is_(None))
            .values(**values)
        )
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(statement)
        except IntegrityError as exc:
            # 改名成已存在的名字 → 与 add 同一个错误码（否则用户看到 500）
            await self._raise_name_conflict(kb, exc)
        if result.rowcount == 0:
            raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")
        return await self.get_internal(kb.id) or kb

    async def soft_delete(self, kb_id: str, user_id: str) -> None:
        table = knowledge_base_table
        now = db_now()
        statement = (
            update(table)
            .where(table.c.id == kb_id, table.c.user_id == user_id, table.c.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在")

    async def count(self, user_id: str) -> int:
        table = knowledge_base_table
        statement = (
            select(func.count())
            .select_from(table)
            .where(table.c.user_id == user_id, table.c.deleted_at.is_(None))
        )
        async with self._engine.connect() as connection:
            total = (await connection.execute(statement)).scalar_one()
        return int(total)


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------


class MySqlDocumentRepo:
    """:class:`~app.infrastructure.storage.base.DocumentRepo` 的 MySQL 实现。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def add(self, document: Document) -> Document:
        existing = await self.find_by_sha256(document.kb_id, document.content_sha256)
        if existing is not None:
            # uk_doc_dedupe 的语义：同一 KB 内按内容哈希唯一（REQ-RAG-007）
            raise AppError(
                ErrorCode.DOCUMENT_DUPLICATE,
                "相同内容的文档已存在",
                {"doc_id": existing.id, "doc_name": existing.doc_name},
            )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    insert(document_table).values(**_document_values(document))
                )
        except IntegrityError as exc:
            again = await self.find_by_sha256(document.kb_id, document.content_sha256)
            if again is not None:
                raise AppError(
                    ErrorCode.DOCUMENT_DUPLICATE,
                    "相同内容的文档已存在",
                    {"doc_id": again.id, "doc_name": again.doc_name},
                ) from exc
            reraise(exc)
        return await self._require(document.id, document.user_id)

    async def get(self, doc_id: str, user_id: str) -> Document:
        table = document_table
        statement = select(table).where(
            table.c.id == doc_id,
            table.c.user_id == user_id,
            table.c.deleted_at.is_(None),
        )
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        if not rows:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
        return _document_from_row(rows[0])

    async def _require(self, doc_id: str, user_id: str) -> Document:
        """刚写完就读回来（保证返回值与库一致），失败说明写入没生效。"""
        try:
            return await self.get(doc_id, user_id)
        except AppError:
            raise AppError(
                ErrorCode.INTERNAL_ERROR, "文档写入后无法读回", {"doc_id": doc_id}
            ) from None

    async def find_by_sha256(self, kb_id: str, content_sha256: str) -> Document | None:
        table = document_table
        statement = select(table).where(
            table.c.kb_id == kb_id,
            table.c.content_sha256 == content_sha256,
            table.c.deleted_at.is_(None),
        )
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        return _document_from_row(rows[0]) if rows else None

    async def list(
        self,
        kb_id: str,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> _DocumentPage:
        table = document_table
        statement = select(table).where(
            table.c.kb_id == kb_id,
            table.c.user_id == user_id,
            table.c.deleted_at.is_(None),
        )
        if status is not None:
            statement = statement.where(table.c.status == status)
        statement = _apply_cursor(statement, table.c.created_at, table.c.id, cursor)
        statement = statement.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(
            limit + FETCH_AHEAD
        )
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        items = [_document_from_row(row) for row in rows[:limit]]
        return items, len(rows) > limit

    async def save(self, document: Document) -> Document:
        table = document_table
        values = _update_values(_document_values(document))
        statement = (
            update(table)
            .where(table.c.id == document.id, table.c.deleted_at.is_(None))
            .values(**values)
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
        return await self._require(document.id, document.user_id)

    async def soft_delete(self, doc_id: str, user_id: str) -> None:
        table = document_table
        now = db_now()
        statement = (
            update(table)
            .where(table.c.id == doc_id, table.c.user_id == user_id, table.c.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")

    async def count_in_kb(self, kb_id: str) -> int:
        table = document_table
        statement = (
            select(func.count())
            .select_from(table)
            .where(table.c.kb_id == kb_id, table.c.deleted_at.is_(None))
        )
        async with self._engine.connect() as connection:
            total = (await connection.execute(statement)).scalar_one()
        return int(total)

    async def live_documents(self, kb_id: str) -> _Documents:
        table = document_table
        statement = select(table).where(table.c.kb_id == kb_id, table.c.deleted_at.is_(None))
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        return [_document_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# 切片
# ---------------------------------------------------------------------------


class MySqlChunkRepo:
    """:class:`~app.infrastructure.storage.base.ChunkRepo` 的 MySQL 实现。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def replace_for_document(self, doc_id: str, chunks: Sequence[Chunk]) -> int:
        """整体替换该文档的切片（重跑入库任务幂等的前提）。

        ``DELETE`` + 批量 ``INSERT`` 必须在**同一事务**里：分开提交的话，中途失败会留下
        「切片全没了」的文档 —— 而任务状态还是 ``RUNNING``，看起来像「正在重跑」。
        """
        table = document_chunk_table
        try:
            async with self._engine.begin() as connection:
                await connection.execute(delete(table).where(table.c.doc_id == doc_id))
                if chunks:
                    await connection.execute(
                        insert(table), [_chunk_values(chunk) for chunk in chunks]
                    )
        except IntegrityError as exc:
            # uk_chunk_doc_idx (doc_id, chunk_index)：同一文档出现两个相同序号。
            # 这是**调用方的 bug**（切片器产出了重复序号），必须报出来而不是吞掉。
            mapped = AppError(
                ErrorCode.INTERNAL_ERROR,
                "切片序号重复，无法写入",
                {"doc_id": doc_id, "constraint": "uk_chunk_doc_idx"},
            )
            raise mapped from exc
        return len(chunks)

    async def list_for_document(
        self, doc_id: str, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> _ChunkPage:
        table = document_chunk_table
        statement = select(table).where(table.c.doc_id == doc_id, table.c.user_id == user_id)
        if cursor:
            # 切片游标就是 chunk_index 本身：它天然单调，比时间戳稳
            try:
                start = int(cursor)
            except ValueError as exc:
                raise AppError(ErrorCode.INVALID_ARGUMENT, "游标无效") from exc
            statement = statement.where(table.c.chunk_index > start)
        statement = statement.order_by(table.c.chunk_index.asc()).limit(limit + FETCH_AHEAD)
        async with self._engine.connect() as connection:
            rows = _rows(await connection.execute(statement))
        items = [_chunk_from_row(row) for row in rows[:limit]]
        return items, len(rows) > limit

    async def delete_for_document(self, doc_id: str) -> int:
        table = document_chunk_table
        statement = delete(table).where(table.c.doc_id == doc_id)
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        return int(result.rowcount or 0)

    async def count_for_document(self, doc_id: str) -> int:
        table = document_chunk_table
        statement = select(func.count()).select_from(table).where(table.c.doc_id == doc_id)
        async with self._engine.connect() as connection:
            total = (await connection.execute(statement)).scalar_one()
        return int(total)

    async def count_for_kb(self, kb_id: str) -> int:
        """该 KB 的切片总数。

        **必须 join 文档表**：已软删文档的切片不计入（内存实现同样是先取 ``live doc_ids``
        再求和）。只按 ``kb_id`` 直接 count 会把已删文档的切片算进去，于是删完文档后 KB
        列表里的 ``chunk_count`` 不降反升。
        """
        table = document_chunk_table
        statement = (
            select(func.count())
            .select_from(table.join(document_table, document_table.c.id == table.c.doc_id))
            .where(table.c.kb_id == kb_id, document_table.c.deleted_at.is_(None))
        )
        async with self._engine.connect() as connection:
            total = (await connection.execute(statement)).scalar_one()
        return int(total)


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------


#: 建表脚本必须创建的表（启动自检用）。故意只列**本模块会读写的**三张：
#: 别的表（task / user_memory / ...）由各自的仓储负责，缺了也不该在这里报错。
REQUIRED_TABLES = ("knowledge_base", "document", "document_chunk")


class MySqlRagRepository:
    """一次构造出三个仓储（共享同一个连接池）。

    与 :class:`~app.infrastructure.storage.memory.InMemoryRagRepository` 同形：业务代码只依赖
    :class:`~app.infrastructure.storage.base.RagRepositories`，不关心实现。
    """

    def __init__(self, settings: Settings) -> None:
        self._engine = create_engine_from_settings(settings)
        self.knowledge_bases = MySqlKnowledgeBaseRepo(self._engine)
        self.documents = MySqlDocumentRepo(self._engine)
        self.chunks = MySqlChunkRepo(self._engine)

    @property
    def engine(self) -> Any:
        """底层引擎（健康检查与自检脚本用）。"""
        return self._engine

    async def ping(self) -> None:
        """连通性探测（启动自检）：连不上会抛 ``AppError(503)``。"""
        from sqlalchemy import text

        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:
            reraise(exc)

    async def missing_tables(self) -> list[str]:
        """列出自检表里**不存在**的表（空列表 = 结构齐备）。"""
        from sqlalchemy import inspect

        async with self._engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
        return [name for name in REQUIRED_TABLES if name not in names]

    async def aclose(self) -> None:
        """归还引擎（lifespan 结束时调用）。

        不关的话 ``uvicorn --reload`` 每次重启都会留下一批到 MySQL 的半开连接，直到
        ``wait_timeout`` 才被服务端回收。

        注意是**归还**不是 ``dispose``：记忆仓储与 RAG 仓储共享同一个引擎（同 DSN），
        直接关会把对方的连接一起断掉。
        """
        await release_engine(self._engine)


def _apply_cursor(statement: Any, created_at: Any, resource_id: Any, cursor: str | None) -> Any:
    """给列表查询套上 ``(created_at, id)`` 倒序游标条件。

    用**行值比较**（``(created_at, id) < (?, ?)``）而不是 ``created_at < ? OR
    (created_at = ? AND id < ?)``：两者语义相同，但前者能被优化器用上 ``idx_*_user_created``，
    后者常退化成全表扫描；更重要的是手写展开形式时忘记加括号（``a AND b OR c``）不会报错，
    只会静默地多返回或少返回数据。
    """
    if not cursor:
        return statement
    moment, resource_id_value = decode_cursor(cursor)
    return statement.where(
        tuple_(created_at, resource_id) < tuple_(to_db(moment), resource_id_value)
    )


__all__ = [
    "REQUIRED_TABLES",
    "MySqlChunkRepo",
    "MySqlDocumentRepo",
    "MySqlKnowledgeBaseRepo",
    "MySqlRagRepository",
    "document_chunk_table",
    "document_table",
    "knowledge_base_table",
]
