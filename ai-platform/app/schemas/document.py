"""文档请求 / 响应契约（``docs/06`` §3）。"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.schemas.common import StrictModel


class UploadAccepted(StrictModel):
    """``POST /knowledge-bases/{kb_id}/documents`` 的 202 响应（``docs/06`` §3.1）。"""

    doc_id: str
    task_id: str | None = None
    status: str = "PENDING"
    doc_name: str = ""
    content_sha256: str = ""
    #: 命中同哈希且仍在处理中 → ``true`` 且复用同一 ``doc_id``（``REQ-RAG-007``）
    duplicated: bool = False
    created_at: str = ""


class DocumentError(StrictModel):
    """入库失败原因（``AC-RAG-16`` 要求 ``error.code`` 可见）。"""

    code: str
    message: str = ""


class DocumentOut(StrictModel):
    """文档详情。"""

    id: str
    kb_id: str
    doc_name: str
    file_ext: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    status: str = "PENDING"
    page_count: int | None = None
    chunk_count: int = 0
    char_count: int = 0
    #: 切分产出的切片数（截断前）；与 ``chunk_count`` 不等即说明被 ``MAX_DOC_CHUNKS`` 截断
    chunks_total: int | None = None
    #: 是否因 ``MAX_DOC_CHUNKS`` 丢弃了尾部切片（``docs/10`` UP-01）
    truncated: bool = False
    chunk_size: int = 512
    chunk_overlap: int = 64
    content_sha256: str = ""
    task_id: str | None = None
    error: DocumentError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


class DocumentList(StrictModel):
    """文档列表响应。"""

    items: list[DocumentOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class ChunkOut(StrictModel):
    """切片（调试视图）。"""

    chunk_id: str
    doc_id: str
    kb_id: str
    chunk_index: int
    content: str
    content_sha256: str = ""
    char_start: int = 0
    char_end: int = 0
    token_count: int = 0
    page: int | None = None
    heading_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ChunkList(StrictModel):
    """切片列表响应。"""

    items: list[ChunkOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class DocumentDeleteResult(StrictModel):
    """``DELETE /documents/{doc_id}`` 的 202 响应。"""

    doc_id: str
    task_id: str
    status: str = "PENDING"


__all__ = [
    "ChunkList",
    "ChunkOut",
    "DocumentDeleteResult",
    "DocumentError",
    "DocumentList",
    "DocumentOut",
    "UploadAccepted",
]
