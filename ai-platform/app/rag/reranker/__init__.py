"""重排序：端口 + 空实现 + BGE 实现。"""

from __future__ import annotations

from app.config import Settings
from app.rag.reranker.base import IdentityReranker, Reranker, RerankResult
from app.rag.reranker.bge import BgeReranker

__all__ = [
    "BgeReranker",
    "IdentityReranker",
    "RerankResult",
    "Reranker",
    "build_reranker",
]

#: 启用重排时使用的模型名片段（用于明确区分「配置关闭」与「未接入」）
_BGE_MARKER = "bge"


def build_reranker(settings: Settings) -> Reranker:
    """按配置选择重排器。

    只有 ``reranker_enabled=True`` 且模型名是 BGE 系列时才加载交叉编码器；
    否则返回 :class:`IdentityReranker`，让上层统一记 ``rerank_skipped``。
    """
    if not settings.reranker_enabled:
        return IdentityReranker(reason="reranker_disabled")
    if _BGE_MARKER not in settings.reranker_model.lower():
        return IdentityReranker(reason=f"unsupported_reranker_model: {settings.reranker_model}")
    return BgeReranker(settings)
