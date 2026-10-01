"""BGE 向量化（``sentence-transformers``，``docs/06`` §4.4）。

模型**延迟加载**：只有首次真正编码时才下载 / 载入权重。否则应用启动、乃至
跑一次单元测试都会意外拉取数 GB 的文件——这是本地开发体验最容易崩的地方。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.rag.embedding.base import l2_normalize
from app.rag.torch_threads import apply_torch_num_threads

if TYPE_CHECKING:  # 仅类型提示，运行时不导入重依赖
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger("app.rag.embedding")


class BgeEmbeddingProvider:
    """基于 ``sentence-transformers`` 的语义向量化器。"""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self.dim = settings.embedding_dim
        self._device = settings.embedding_device
        self._batch_size = settings.embedding_batch_size
        self._num_threads = settings.torch_num_threads
        self._model: SentenceTransformer | None = None
        self._loaded = False

    # ------------------------------------------------------------------
    @property
    def model(self) -> SentenceTransformer:
        """懒加载 ``SentenceTransformer`` 实例。"""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - 需要真实依赖才走到
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "未安装 sentence-transformers，无法使用 EMBEDDING_PROVIDER=bge",
                    {"hint": "EMBEDDING_PROVIDER=hash"},
                ) from exc
            logger.info(
                "embedding.loading",
                extra={"model": self.model_name, "device": self._device},
            )
            # 必须在构造模型**之前**收窄线程数：torch 默认按逻辑核数开线程，
            # 一次 8MB 文档的向量化会把同机的 MySQL/Redis/网关一起拖慢。
            apply_torch_num_threads(self._num_threads)
            self._model = SentenceTransformer(self.model_name, device=self._device)
            self._loaded = True
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码为归一化向量。"""
        if not texts:
            return []
        try:
            vectors = self.model.encode(
                list(texts),
                batch_size=self._batch_size,
                # 必须归一化：集合用 COSINE 度量，未归一化会让分数失去可比性
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"向量化失败：{exc}",
                {"model": self.model_name, "batch_size": len(texts)},
            ) from exc
        result = [list(map(float, row)) for row in vectors.tolist()]
        self._check_dim(result)
        return result

    def embed_query(self, text: str) -> list[float]:
        """编码单条查询。"""
        vectors = self.embed([text])
        return vectors[0] if vectors else l2_normalize([0.0] * self.dim)

    # ------------------------------------------------------------------
    def _check_dim(self, vectors: list[list[float]]) -> None:
        """核对实际维度与集合声明是否一致。

        ``docs/09`` §3 的集合维度是**建集合时固化**的，写进去维度不符的向量
        不会立刻报错，而是「写成功但永远检索不到」。所以在第一次编码后立刻比对，
        把静默失败变成明确错误。
        """
        if not vectors:
            return
        actual = len(vectors[0])
        if actual != self.dim:
            raise AppError(
                ErrorCode.VECTOR_DIM_MISMATCH,
                f"模型输出维度（{actual}）与 EMBEDDING_DIM（{self.dim}）不一致",
                {"model": self.model_name, "actual_dim": actual, "configured_dim": self.dim},
            )


__all__ = ["BgeEmbeddingProvider"]
