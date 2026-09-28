"""可控 Embedding 替身（确定性向量 + 可控失败）。

为什么不直接用项目里的 ``HashEmbeddingProvider``：真检索场景下需要**精确控制
向量方向**才能写出可断言的用例（例如「查询向量与 chunk A 完全同向 → A 必须排
第一」）。哈希向量也能做到，但要从文本反推向量，用例会变得不可读。

``failures`` 用于验证 ``REQ-RAG-006`` 的降级路径：检索组件失败时对话 MUST 仍然
成功，只是 ``degraded=true`` + ``reasons=["rag_unavailable"]``。

注意 ``EmbeddingProvider.embed`` / ``embed_query`` 是**同步**方法（真实实现内部
用线程池 + 超时包，见 ``app/rag/embedding/base.py::embed_texts``），替身必须与
之一致，否则 ``asyncio.to_thread(provider.embed, ...)`` 会拿到协程对象。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import AppError, ErrorCode
from app.rag.embedding.base import l2_normalize


class FakeEmbedding:
    """把文本映射成**确定性**向量；相同文本 → 相同向量。"""

    def __init__(self, dim: int = 8, model_name: str = "fake-embed") -> None:
        self._dim = dim
        self._model_name = model_name
        #: 记录每次编码的输入，用于断言「是否真的调用了向量化」
        self.calls: list[list[str]] = []
        #: 可注入的失败次数（0 表示不失败）
        self.failures = 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return self._model_name

    def _maybe_fail(self) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise AppError(ErrorCode.RETRIEVAL_FAILED, "注入的向量化失败")

    def _vector(self, text: str) -> list[float]:
        """按字符位置做特征哈希：同文本同向量，异文本大概率不同向。"""
        raw = [0.0] * self._dim
        for index, char in enumerate(text):
            raw[(ord(char) + index) % self._dim] += 1.0
        if not any(raw):
            raw[0] = 1.0
        return l2_normalize(raw)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        self._maybe_fail()
        return [self._vector(text) for text in batch]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        self._maybe_fail()
        return self._vector(text)


class FixedEmbedding:
    """对任意输入都返回**预先指定**的向量。

    用于「按构造决定排序」的用例：直接给出查询向量与各 chunk 向量的余弦关系，
    断言召回 / 阈值 / 重排行为，而不依赖哈希函数的偶然分布。
    """

    def __init__(
        self, query_vector: Sequence[float], doc_vectors: dict[str, Sequence[float]]
    ) -> None:
        self._query = l2_normalize(list(query_vector))
        self._docs = {text: l2_normalize(list(vec)) for text, vec in doc_vectors.items()}
        self._dim = len(self._query)
        self.calls: list[list[str]] = []

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return "fixed-embed"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._docs.get(text, [0.0] * self._dim) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return list(self._query)


__all__ = ["FakeEmbedding", "FixedEmbedding"]
