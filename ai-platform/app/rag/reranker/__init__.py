"""重排序：端口 + 空实现 + 本地 BGE 实现 + 云端（硅基流动）实现。"""

from __future__ import annotations

from app.core.config import Settings
from app.rag.reranker.base import IdentityReranker, Reranker, RerankResult
from app.rag.reranker.bge import BgeReranker
from app.rag.reranker.siliconflow import SiliconFlowReranker

__all__ = [
    "BgeReranker",
    "IdentityReranker",
    "RerankResult",
    "Reranker",
    "SiliconFlowReranker",
    "build_reranker",
]


def build_reranker(settings: Settings) -> Reranker:
    """按配置选择重排器。

    ``RERANKER_ENABLED=false`` ⇒ :class:`IdentityReranker`（上层统一记
    ``rerank_skipped``）。否则按**显式的** ``RERANKER_PROVIDER`` 选实现。

    早期版本用"模型名里含不含 ``bge``"来猜，那是个静默陷阱：换成任何名字里没有
    ``bge`` 的模型（例如现在这个 ``Qwen/Qwen3-Reranker-0.6B``）都会**悄悄退化成
    不重排**，只在日志里留一行 ``unsupported_reranker_model`` —— 检索质量下降而
    没有任何显式报错。现在改成枚举，名字不再参与判断（``docs/12-§3`` 同类教训）。
    """
    if not settings.reranker_enabled:
        return IdentityReranker(reason="reranker_disabled")
    if settings.reranker_provider == "siliconflow":
        return SiliconFlowReranker(settings)
    return BgeReranker(settings)
