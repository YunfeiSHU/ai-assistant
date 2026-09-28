"""知识库请求 / 响应契约（``docs/06`` §2）。

约束值刻意与 SRS 表格**逐格对齐**（``chunk_size`` 128..2048、``name`` ≤ 64 等）：
这些边界同时被 ``app.rag.service`` 再校验一次。看起来重复，但两份校验拦的
不是同一件事——pydantic 给客户端一个「哪个字段错了」的精确 400，服务层保证
无论从哪条路径进来（路由、Worker、将来可能的批量脚本）都不会绕过业务规则。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator, model_validator

from app.rag.service import (
    CHUNK_OVERLAP_DEFAULT,
    CHUNK_SIZE_DEFAULT,
)
from app.schemas.common import StrictModel

#: ``metadata`` 的键值长度上限（§2.1）
METADATA_KEY_MAX = 64


def _validate_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    """校验 ``metadata``：键值都必须是短标量，避免把整个大对象塞进元数据。"""
    if not value:
        return {}
    for key, item in value.items():
        if len(str(key)) > METADATA_KEY_MAX:
            msg = f"metadata 键过长（>{METADATA_KEY_MAX}）：{key}"
            raise ValueError(msg)
        if len(str(item)) > METADATA_KEY_MAX:
            msg = f"metadata 值过长（>{METADATA_KEY_MAX}）：{key}"
            raise ValueError(msg)
    return dict(value)


class KnowledgeBaseCreate(StrictModel):
    """``POST /knowledge-bases`` 请求体。"""

    name: str = Field(min_length=1, max_length=64, description="知识库名称（同用户内唯一）")
    description: str = Field(default="", max_length=500, description="描述")
    # 切分参数**不在这里**做范围/关系校验：HTTP 层的 pydantic 校验只能给出
    # ``INVALID_ARGUMENT``，而 ``docs/02`` 要求切分参数不合法时返回
    # ``CHUNK_STRATEGY_INVALID``。校验器又无法在 pydantic 里抛 ``AppError``
    # （非 ValueError 会穿透出去变成 500），所以统一交给服务层的
    # ``resolve_chunk_params()`` —— KB 创建、KB 修改、文档级覆盖三个入口共用它。
    chunk_size: int = Field(default=CHUNK_SIZE_DEFAULT, description="切片 token 目标长度")
    chunk_overlap: int = Field(default=CHUNK_OVERLAP_DEFAULT, description="重叠长度")
    embedding_model: str | None = Field(default=None, description="null 表示使用全局模型")
    retrieval_top_k: int = Field(default=20, ge=1, le=100, description="默认召回数")
    rerank_top_n: int = Field(default=5, ge=1, le=20, description="重排保留数")
    score_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="重排后最低分")
    metadata: dict[str, Any] = Field(default_factory=dict, description="自定义标签")

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_metadata(value)

    @model_validator(mode="after")
    def _check_params(self) -> KnowledgeBaseCreate:
        if self.rerank_top_n > self.retrieval_top_k:
            msg = "rerank_top_n 必须小于等于 retrieval_top_k"
            raise ValueError(msg)
        return self


class KnowledgeBaseUpdate(StrictModel):
    """``PATCH /knowledge-bases/{kb_id}`` 请求体（全部可选）。

    ``chunk_size`` / ``chunk_overlap`` 只影响**后续**入库的文档（``REQ-RAG-002``），
    已有文档的参数已固化在 chunk 元数据里。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    # 同 ``KnowledgeBaseCreate``：切分参数交给服务层；而且 PATCH 是局部的，
    # 「overlap 是否小于 size」必须与**库里已有值合并后**判断，
    # 这一层看不到已存值（例：只传 ``chunk_overlap=800``，库里 size=512）。
    chunk_size: int | None = Field(default=None)
    chunk_overlap: int | None = Field(default=None)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=100)
    rerank_top_n: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] | None = None

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _validate_metadata(value)

    @model_validator(mode="after")
    def _check_params(self) -> KnowledgeBaseUpdate:
        if (
            self.retrieval_top_k is not None
            and self.rerank_top_n is not None
            and self.rerank_top_n > self.retrieval_top_k
        ):
            msg = "rerank_top_n 必须小于等于 retrieval_top_k"
            raise ValueError(msg)
        return self


class KnowledgeBaseOut(StrictModel):
    """知识库响应体（``docs/06`` §2.1 响应 201）。"""

    id: str
    name: str
    description: str = ""
    chunk_size: int = CHUNK_SIZE_DEFAULT
    chunk_overlap: int = CHUNK_OVERLAP_DEFAULT
    embedding_model: str | None = None
    embedding_dim: int = 1024
    retrieval_top_k: int = 20
    rerank_top_n: int = 5
    score_threshold: float = 0.0
    status: str = "active"
    document_count: int = 0
    chunk_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class KnowledgeBaseList(StrictModel):
    """KB 列表响应。"""

    items: list[KnowledgeBaseOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class KnowledgeBaseDeleteResult(StrictModel):
    """删除 KB 的清理统计（便于排障时确认「真的清干净了」）。"""

    deleted: bool = True
    documents: int = 0
    vectors: int = 0
    objects: int = 0


class SearchRequest(StrictModel):
    """``POST /knowledge-bases/{kb_id}/search`` 请求体（``docs/06`` §5.1）。"""

    query: str = Field(min_length=1, max_length=2000, description="检索语句")
    top_k: int | None = Field(default=None, ge=1, le=100, description="覆盖 KB 默认召回数")
    rerank_top_n: int | None = Field(default=None, ge=1, le=20, description="覆盖 KB 默认重排数")
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    with_rerank: bool = Field(default=True, description="false 时只看向量召回原始结果")

    @model_validator(mode="after")
    def _check_params(self) -> SearchRequest:
        if (
            self.top_k is not None
            and self.rerank_top_n is not None
            and self.rerank_top_n > self.top_k
        ):
            msg = "rerank_top_n 必须小于等于 top_k"
            raise ValueError(msg)
        return self


class SearchItem(StrictModel):
    """单条检索结果。"""

    index: int
    chunk_id: str
    doc_id: str
    doc_name: str = ""
    page: int | None = None
    heading_path: str | None = None
    vector_score: float = 0.0
    #: 未重排时 MUST 为 ``null``（``AC-RAG-12``），而不是把向量分复制一份
    rerank_score: float | None = None
    score: float = 0.0
    merged: bool = False
    content: str = ""


class SearchResponse(StrictModel):
    """检索调试响应。"""

    query: str
    recalled: int = 0
    returned: int = 0
    rerank_used: bool = False
    elapsed_ms: int = 0
    items: list[SearchItem] = Field(default_factory=list)


__all__ = [
    "KnowledgeBaseCreate",
    "KnowledgeBaseDeleteResult",
    "KnowledgeBaseList",
    "KnowledgeBaseOut",
    "KnowledgeBaseUpdate",
    "SearchItem",
    "SearchRequest",
    "SearchResponse",
]
