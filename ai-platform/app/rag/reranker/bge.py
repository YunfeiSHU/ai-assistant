"""BGE 交叉编码器重排（``FlagEmbedding.FlagReranker``）。

模型延迟加载，且**失败不抛异常而是退化**：重排只是精度增强，让整个对话因为
重排模型没下载成功而 500，是把可选依赖变成了硬依赖。失败信息通过
:class:`~app.rag.reranker.base.RerankResult` 的 ``applied``/``reason`` 上报，
上层据此记 ``degraded_reasons=["rerank_skipped"]``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.config import Settings
from app.rag.reranker.base import RerankResult

if TYPE_CHECKING:  # 仅类型提示
    from FlagEmbedding import FlagReranker

logger = logging.getLogger("app.rag.reranker")


class BgeReranker:
    """``BAAI/bge-reranker-v2-m3`` 交叉编码器。"""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.reranker_model
        self._device = settings.reranker_device
        self._model: FlagReranker | None = None

    @property
    def model(self) -> FlagReranker:
        """懒加载 ``FlagReranker``。"""
        if self._model is None:
            from FlagEmbedding import FlagReranker

            self._model = FlagReranker(
                self.model_name,
                use_fp16=self._device.startswith("cuda"),
                devices=self._device,
            )
        return self._model

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> RerankResult:
        """重排；任何异常都退化为「按原顺序返回 + applied=False」。"""
        if not documents:
            return RerankResult(ranked=[], applied=True)
        try:
            scores = await asyncio.to_thread(self._score, query, list(documents))
        except Exception as exc:
            logger.warning(
                "rerank.failed",
                extra={"model": self.model_name, "error": str(exc), "candidates": len(documents)},
            )
            return RerankResult(
                ranked=[(index, 0.0) for index in range(min(top_n, len(documents)))],
                applied=False,
                reason=f"rerank_error: {exc}",
            )
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        return RerankResult(ranked=ranked[: max(0, top_n)], applied=True)

    def _score(self, query: str, documents: list[str]) -> list[float]:
        pairs = [[query, document] for document in documents]
        scores = self.model.compute_score(pairs, normalize=True)
        if isinstance(scores, (int, float)):  # 单条时 FlagEmbedding 返回标量
            return [float(scores)]
        return [float(value) for value in scores]


__all__ = ["BgeReranker"]
