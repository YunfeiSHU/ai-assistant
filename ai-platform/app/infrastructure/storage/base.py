"""RAG 存储端口（KB / 文档 / 切片 / 对象存储）。

**为什么先定端口再写实现**：``docs/09`` §2 已经把六张表的结构定死了，但 M3 的
出口标准是「S2、S4 跑通」，本地不一定有 MySQL / MinIO。端口 + 内存实现让整条
入库与检索链路**在没有任何容器的情况下可跑、可测**；等接真库时只换实现，
业务代码一行不动。

字段与 ``docs/09`` 的 DDL **逐列对齐**（含软删列与计数列），这样 SQL 实现是
机械翻译，不会出现「内存版没这个概念、SQL 版多一列」的语义漂移。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 两个仓储都有名为 ``list`` 的方法，会在类作用域里遮蔽内建 ``list``，
# 导致返回注解 `list[X]` 被当成方法对象（mypy: not valid as a type）。
# 这里在**模块级**定义别名（此时 ``list`` 还是内建），实体名用前向引用字符串。
_KBPage = tuple[list["KnowledgeBase"], bool]
_DocumentPage = tuple[list["Document"], bool]
_Documents = list["Document"]

# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class KnowledgeBase:
    """``knowledge_base`` 表（``docs/09`` §2.1）。"""

    id: str
    user_id: str
    name: str
    description: str = ""
    chunk_size: int = 512
    chunk_overlap: int = 64
    embedding_model: str | None = None
    embedding_dim: int = 1024
    retrieval_top_k: int = 20
    rerank_top_n: int = 5
    score_threshold: float = 0.0
    status: str = "active"
    document_count: int = 0
    chunk_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """接口结构（``docs/06`` §2.1）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "retrieval_top_k": self.retrieval_top_k,
            "rerank_top_n": self.rerank_top_n,
            "score_threshold": self.score_threshold,
            "status": self.status,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class Document:
    """``document`` 表（``docs/09`` §2.2）。"""

    id: str
    kb_id: str
    user_id: str
    doc_name: str
    file_ext: str
    mime_type: str
    size_bytes: int
    object_key: str
    content_sha256: str
    status: str = "PENDING"
    page_count: int | None = None
    chunk_count: int = 0
    char_count: int = 0
    #: 切分器产出的切片数（**截断前**）。``chunk_count`` 是实际入库数，
    #: 两者不等就说明被 ``MAX_DOC_CHUNKS`` 截断了（见 ``truncated``）。
    #: ``None`` = 还没走到切分（PENDING/PARSING），而不是「0 片」。
    chunks_total: int | None = None
    #: 是否因 ``MAX_DOC_CHUNKS`` 丢弃了尾部切片。
    #:
    #: 这是**必须让调用方看见**的事实：8MB 测试正文实测切出 16,969 片、只入库
    #: 10,000 片（丢 41% 正文），而截断前只打了一条 warning、接口照旧 202 ——
    #: 「入库成功」因此是假的（见 ``docs/10`` 的 UP-01）。
    truncated: bool = False
    chunk_size: int = 512
    chunk_overlap: int = 64
    task_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    indexed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """接口结构（``docs/06`` §3.3）。"""
        return {
            "id": self.id,
            "kb_id": self.kb_id,
            "doc_name": self.doc_name,
            "file_ext": self.file_ext,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "char_count": self.char_count,
            "chunks_total": self.chunks_total,
            "truncated": self.truncated,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "content_sha256": self.content_sha256,
            "task_id": self.task_id,
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code
                else None
            ),
            "metadata": self.metadata,
            "indexed_at": self.indexed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class Chunk:
    """``document_chunk`` 表 + Milvus 检索所需字段（``docs/09`` §2.3 / §3.1）。"""

    id: str
    doc_id: str
    kb_id: str
    user_id: str
    chunk_index: int
    content: str
    content_sha256: str
    char_start: int
    char_end: int
    token_count: int
    page: int | None = None
    heading_path: str = ""
    created_at: str = ""
    #: 切片级元数据：至少包含 ``doc_name`` / ``chunk_size`` / ``chunk_overlap``
    #: （切分参数固化，``REQ-RAG-002``），使老切片在 KB 改配置后仍能解释自己。
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """调试接口结构（``GET /documents/{id}/chunks``）。"""
        return {
            "chunk_id": self.id,
            "doc_id": self.doc_id,
            "kb_id": self.kb_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "content_sha256": self.content_sha256,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "token_count": self.token_count,
            "page": self.page,
            "heading_path": self.heading_path or None,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# 端口
# ---------------------------------------------------------------------------


@runtime_checkable
class KnowledgeBaseRepo(Protocol):
    """知识库仓储（所有查询都带 ``user_id`` 与 ``deleted_at IS NULL``）。"""

    async def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        """新增；同名冲突抛 ``409 KB_NAME_CONFLICT``。"""
        ...

    async def get(self, kb_id: str, user_id: str) -> KnowledgeBase:
        """取详情；不存在或不属于该用户 → ``404 KB_NOT_FOUND``。"""
        ...

    async def get_internal(self, kb_id: str) -> KnowledgeBase | None:
        """**不过滤 ``user_id``** 的读取，仅供 Worker 内部维护计数与状态。

        为什么必须单独开一个方法：Worker 手里只有 ``kb_id``（来自任务载荷），没有
        发起人的 ``user_id``。若让它拿某个哨兵值去调带隔离的 ``get``，会得到一个
        永久的「找不到」，于是计数永远更新不了——而且不报错。把「内部访问」显式
        命名出来，代码审阅时一眼能看出这是刻意绕过隔离，而不是写错了。
        """
        ...

    async def list(self, user_id: str, *, limit: int = 20, cursor: str | None = None) -> _KBPage:
        """分页列出。"""
        ...

    async def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        """整体保存（更新）。"""
        ...

    async def soft_delete(self, kb_id: str, user_id: str) -> None:
        """软删除（``deleted_at``）。"""
        ...

    async def count(self, user_id: str) -> int:
        """当前用户的 KB 数量（校验 ``max_kb_count``）。"""
        ...


@runtime_checkable
class DocumentRepo(Protocol):
    """文档仓储。"""

    async def add(self, document: Document) -> Document:
        """新增。"""
        ...

    async def get(self, doc_id: str, user_id: str) -> Document:
        """取详情；不存在或不属于该用户 → ``404 DOCUMENT_NOT_FOUND``。"""
        ...

    async def find_by_sha256(self, kb_id: str, content_sha256: str) -> Document | None:
        """按 ``(kb_id, content_sha256)`` 精确查重（``REQ-RAG-007``）。"""
        ...

    async def list(
        self,
        kb_id: str,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> _DocumentPage:
        """分页列出。"""
        ...

    async def save(self, document: Document) -> Document:
        """整体保存。"""
        ...

    async def soft_delete(self, doc_id: str, user_id: str) -> None:
        """软删除。"""
        ...

    async def count_in_kb(self, kb_id: str) -> int:
        """KB 内文档数（校验 ``max_kb_documents``）。"""
        ...

    async def live_documents(self, kb_id: str) -> _Documents:
        """该 KB 下所有未删除文档（删 KB 时用来级联清理对象存储）。"""
        ...


@runtime_checkable
class ChunkRepo(Protocol):
    """切片仓储（管理后台 / 调试用；检索只走向量库）。"""

    async def replace_for_document(self, doc_id: str, chunks: Sequence[Chunk]) -> int:
        """用给定切片**整体替换**该文档的切片，返回写入条数。

        整体替换（而不是增量追加）是为了让「重跑入库任务」天然幂等：
        ``docs/09`` §6 要求重试可幂等补齐，增量写会留下重复行。
        """
        ...

    async def list_for_document(
        self, doc_id: str, user_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Chunk], bool]:
        """按 ``chunk_index`` 正序分页。"""
        ...

    async def delete_for_document(self, doc_id: str) -> int:
        """删除该文档全部切片，返回删除条数（幂等）。"""
        ...

    async def count_for_document(self, doc_id: str) -> int:
        """该文档的切片数。"""
        ...

    async def count_for_kb(self, kb_id: str) -> int:
        """该 KB 的切片总数（重算 ``knowledge_base.chunk_count`` 用）。"""
        ...


@dataclass(frozen=True, slots=True)
class RagRepositories:
    """三个仓储的组合，便于整体注入。

    不把三个仓储塞进一个大接口：SQL 实现会按表拆分（各自的 SQL 不同），而内存
    实现共享一份状态。组合类型让两种实现都能自然满足调用方需求。
    """

    knowledge_bases: KnowledgeBaseRepo
    documents: DocumentRepo
    chunks: ChunkRepo


@runtime_checkable
class ObjectStore(Protocol):
    """对象存储（MinIO，``docs/09`` §5.1）。"""

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """写入对象。"""
        ...

    async def get(self, key: str) -> bytes:
        """读取对象。"""
        ...

    async def delete(self, key: str) -> None:
        """删除对象（不存在也算成功，保证删除幂等）。"""
        ...

    async def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...


def build_object_key(user_id: str, kb_id: str, doc_id: str, filename: str) -> str:
    """拼对象路径：``{user_id}/{kb_id}/{doc_id}/{sanitized_filename}``。

    文件名清洗是**安全要求**而不只是整洁（``AC-DATA-05``）：``../`` 必须被剥掉，
    否则上传可以写到别人的目录里去。
    """
    return f"{user_id}/{kb_id}/{doc_id}/{sanitize_filename(filename)}"


def sanitize_filename(filename: str) -> str:
    """清洗文件名：去掉路径分隔符与控制字符，保留扩展名，长度 ≤ 128。"""
    name = filename.replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char.isprintable())
    name = name.strip(". ") or "file"
    if len(name) <= 128:
        return name
    stem, _, ext = name.rpartition(".")
    if stem and len(ext) <= 16:
        return stem[: 128 - len(ext) - 1] + "." + ext
    return name[:128]


__all__ = [
    "Chunk",
    "ChunkRepo",
    "Document",
    "DocumentRepo",
    "KnowledgeBase",
    "KnowledgeBaseRepo",
    "ObjectStore",
    "RagRepositories",
    "build_object_key",
    "sanitize_filename",
]
