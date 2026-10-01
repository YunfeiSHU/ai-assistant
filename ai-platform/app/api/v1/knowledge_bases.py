"""知识库路由（``docs/06`` §2 / §3.1 / §5.1）。

路径里同时出现 ``/knowledge-bases/{kb_id}/documents`` 与 ``/documents/{doc_id}``，
所以这个文件里**只**注册 KB 自己的路径（含 KB 下的文档子资源），文档根路径放在
``documents.py``，任务放在 ``tasks.py``。全部用 ``APIRouter`` 且
``redirect_slashes=False``：否则 ``GET /knowledge-bases/`` 会被 307 到 ``/knowledge-bases``，
而 307 会保留方法，前端拿到一个「看起来是重定向」但语义不清的响应。
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.api.deps import (
    DocumentServiceDep,
    KBServiceDep,
    PaginationDep,
    SearchServiceDep,
    UserId,
)
from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.storage.base import Document, KnowledgeBase
from app.schemas.document import DocumentList, UploadAccepted
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseDeleteResult,
    KnowledgeBaseList,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
    SearchRequest,
    SearchResponse,
)

logger = logging.getLogger("app.api.knowledge_bases")

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


def _kb_out(kb: KnowledgeBase) -> KnowledgeBaseOut:
    return KnowledgeBaseOut.model_validate(kb.to_dict())


def _document_out(document: Document) -> Any:
    from app.schemas.document import DocumentOut

    return DocumentOut.model_validate(document.to_dict())


@router.post(
    "",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    user_id: UserId,
    service: KBServiceDep,
) -> KnowledgeBaseOut:
    """创建知识库；同名 → ``409 KB_NAME_CONFLICT``。"""
    kb = await service.create(user_id=user_id, payload=body.model_dump())
    return _kb_out(kb)


@router.get("", response_model=KnowledgeBaseList, summary="列出知识库")
async def list_knowledge_bases(
    user_id: UserId,
    service: KBServiceDep,
    pagination: PaginationDep,
) -> KnowledgeBaseList:
    """分页列出当前用户的知识库。"""
    items, next_cursor = await service.list(
        user_id=user_id, limit=pagination.limit, cursor=pagination.cursor
    )
    return KnowledgeBaseList(
        items=[_kb_out(kb) for kb in items],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseOut, summary="知识库详情")
async def get_knowledge_base(
    kb_id: str, user_id: UserId, service: KBServiceDep
) -> KnowledgeBaseOut:
    """取详情；跨用户访问返回 ``404``（``REQ-RAG-011``）。"""
    return _kb_out(await service.get(kb_id, user_id))


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut, summary="更新知识库")
async def update_knowledge_base(
    kb_id: str,
    body: KnowledgeBaseUpdate,
    user_id: UserId,
    service: KBServiceDep,
) -> KnowledgeBaseOut:
    """更新；``chunk_size``/``chunk_overlap`` 只影响后续入库的文档。"""
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise AppError(ErrorCode.INVALID_ARGUMENT, "请求体为空，未提供任何可更新字段")
    return _kb_out(await service.update(kb_id, user_id, patch))


@router.delete(
    "/{kb_id}",
    response_model=KnowledgeBaseDeleteResult,
    summary="删除知识库",
)
async def delete_knowledge_base(
    kb_id: str,
    user_id: UserId,
    service: KBServiceDep,
    force: Annotated[bool, Query(description="非空时强制删除")] = False,
) -> KnowledgeBaseDeleteResult:
    """删除知识库；非空且 ``force=false`` → ``409 KB_NOT_EMPTY``。"""
    stats = await service.delete(kb_id, user_id, force=force)
    return KnowledgeBaseDeleteResult(
        documents=stats["documents"], vectors=stats["vectors"], objects=stats["objects"]
    )


# ---------------------------------------------------------------------------
# KB 下的文档
# ---------------------------------------------------------------------------


@router.post(
    "/{kb_id}/documents",
    response_model=UploadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步入库）",
    description="只做校验与建任务，解析/向量化由 Worker 完成，立即返回 202 + task_id。",
)
async def upload_document(
    kb_id: str,
    user_id: UserId,
    service: DocumentServiceDep,
    file: Annotated[UploadFile | None, File(description="上传文件")] = None,
    text: Annotated[str | None, Form(description="直接提交纯文本（≤1MB）")] = None,
    doc_name: Annotated[str | None, Form(description="显示名")] = None,
    metadata: Annotated[str | None, Form(description="JSON 字符串")] = None,
    chunk_size: Annotated[int | None, Form()] = None,
    chunk_overlap: Annotated[int | None, Form()] = None,
) -> UploadAccepted:
    """上传（multipart/form-data）。

    ``file`` 与 ``text`` 二选一由服务层校验：两者都给或都不给都返回
    ``400 INVALID_ARGUMENT``，与「静默挑一个」相比，明确报错能省掉一次线上排查。
    """
    raw = await file.read() if file is not None else None
    filename = (file.filename or "") if file is not None else ""
    parsed_metadata = _parse_metadata(metadata)
    result = await service.upload(
        kb_id=kb_id,
        user_id=user_id,
        filename=filename,
        raw=raw,
        text=text,
        doc_name=doc_name,
        metadata=parsed_metadata,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return UploadAccepted.model_validate(result.to_dict())


@router.get(
    "/{kb_id}/documents",
    response_model=DocumentList,
    summary="列出知识库下的文档",
)
async def list_documents(
    kb_id: str,
    user_id: UserId,
    service: DocumentServiceDep,
    pagination: PaginationDep,
    doc_status: Annotated[str | None, Query(alias="status", description="按状态过滤")] = None,
) -> DocumentList:
    """分页列出文档（可按 ``status`` 过滤）。"""
    items, next_cursor = await service.list_documents(
        kb_id=kb_id,
        user_id=user_id,
        status=doc_status,
        limit=pagination.limit,
        cursor=pagination.cursor,
    )
    return DocumentList(
        items=[_document_out(document) for document in items],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.post(
    "/{kb_id}/search",
    response_model=SearchResponse,
    summary="检索调试（REQ-RAG-008）",
)
async def search_knowledge_base(
    kb_id: str,
    body: SearchRequest,
    user_id: UserId,
    service: SearchServiceDep,
) -> SearchResponse:
    """按查询检索该 KB，返回召回与重排的完整细节。"""
    payload = await service.search(
        kb_id=kb_id,
        user_id=user_id,
        query=body.query,
        top_k=body.top_k,
        rerank_top_n=body.rerank_top_n,
        score_threshold=body.score_threshold,
        with_rerank=body.with_rerank,
    )
    return SearchResponse.model_validate(payload)


def _parse_metadata(raw: str | None) -> dict[str, Any]:
    """解析表单里的 JSON 元数据。

    解析失败直接 ``400`` 而不是忽略：用户显式传了一个坏 JSON，静默丢掉的后果是
    「元数据没生效但上传成功」，比报错难查得多。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT, f"metadata 不是合法 JSON：{exc}", {"field": "metadata"}
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT, "metadata 必须是 JSON 对象", {"field": "metadata"}
        )
    return parsed


__all__ = ["router"]
