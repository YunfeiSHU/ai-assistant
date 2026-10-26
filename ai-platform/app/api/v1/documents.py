"""文档路由（``docs/06`` §3.3）。

单独一个模块是因为路径前缀 ``/documents`` 与 ``/knowledge-bases`` 平级，
放在同一个 ``APIRouter(prefix=...)`` 里无法表达。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Response, status
from fastapi.responses import RedirectResponse

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


@router.get(
    "/{doc_id}/download-url",
    summary="获取原文下载信息（预签名）",
)
async def get_document_download_url(
    doc_id: str, user_id: UserId, service: DocumentServiceDep
) -> dict[str, object]:
    """真实对象存储返回预签名 URL；内存存储返回 ``url=null``，前端改走 /download 字节流。"""
    document, url, _data = await service.download_document(doc_id, user_id)
    return {
        "url": url,
        "expires_in": 300 if url else 0,
        "doc_name": document.doc_name,
        "mime_type": document.mime_type,
    }


@router.get(
    "/{doc_id}/download",
    summary="下载/预览原文",
    responses={
        200: {"content": {"application/octet-stream": {}}},
        302: {"description": "预签名 URL（真实对象存储）"},
    },
)
async def download_document(
    doc_id: str, user_id: UserId, service: DocumentServiceDep
) -> Response:
    """真实存储 302 到预签名 URL；内存存储直接返回字节。"""
    document, url, data = await service.download_document(doc_id, user_id)
    if url:
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    if data is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    filename = quote(document.doc_name or "document")
    return Response(
        content=data,
        media_type=document.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{filename}"},
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
