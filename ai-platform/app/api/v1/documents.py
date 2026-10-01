"""文档路由（``docs/06`` §3.3）。

单独一个模块是因为路径前缀 ``/documents`` 与 ``/knowledge-bases`` 平级，
放在同一个 ``APIRouter(prefix=...)`` 里无法表达。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status

from app.api.deps import DocumentServiceDep, PaginationDep, UserId
from app.schemas.document import (
    ChunkList,
    ChunkOut,
    DocumentDeleteResult,
    DocumentOut,
)

logger = logging.getLogger("app.api.documents")

router = APIRouter(prefix="/documents", tags=["文档"])


@router.get("/{doc_id}", response_model=DocumentOut, summary="文档详情")
async def get_document(doc_id: str, user_id: UserId, service: DocumentServiceDep) -> DocumentOut:
    """取详情：``status`` / ``chunk_count`` / ``error`` / ``indexed_at``。"""
    document = await service.get_document(doc_id, user_id)
    return DocumentOut.model_validate(document.to_dict())


@router.get(
    "/{doc_id}/chunks",
    response_model=ChunkList,
    summary="查看切片（调试）",
)
async def list_document_chunks(
    doc_id: str,
    user_id: UserId,
    service: DocumentServiceDep,
    pagination: PaginationDep,
) -> ChunkList:
    """按 ``chunk_index`` 正序分页返回切片内容。"""
    items, next_cursor = await service.list_chunks(
        doc_id=doc_id, user_id=user_id, limit=pagination.limit, cursor=pagination.cursor
    )
    return ChunkList(
        items=[ChunkOut.model_validate(chunk.to_dict()) for chunk in items],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.delete(
    "/{doc_id}",
    response_model=DocumentDeleteResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="删除文档（异步）",
    description="返回删除任务；清理顺序为 Milvus 向量 → MySQL 切片/文档 → MinIO 对象。",
)
async def delete_document(
    doc_id: str, user_id: UserId, service: DocumentServiceDep
) -> DocumentDeleteResult:
    """删除文档：建任务并投递，返回 ``202``。"""
    _document, task = await service.delete_document(doc_id=doc_id, user_id=user_id)
    return DocumentDeleteResult(doc_id=doc_id, task_id=task.id, status=str(task.status))


__all__ = ["router"]
