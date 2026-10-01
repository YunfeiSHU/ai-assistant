"""确定性词法向量（``EMBEDDING_PROVIDER=hash``）。

用 feature hashing 把「词的集合 + 字符 n-gram」映射到固定维度：词元抓英文与数字，
字符 2/3-gram 抓中文（中文没有空格，字符 n-gram 是唯一不依赖分词的可行做法）。
这样「有共同字词的两段文本」余弦相似度就更高，足以让检索链路的所有环节（召回、阈值、
重排、合并、引用编号）被真实驱动和断言。

哈希用 ``blake2b`` 而不是内置 ``hash()``：内置 ``hash`` 对 ``str`` 按 ``PYTHONHASHSEED``
加盐，每个进程结果不同 —— 入库写入的向量和查询时算出的向量会不属于同一个空间，检索直接
归零，而且只在「重启后」才暴露。
每个特征同时算「位置哈希」与「符号哈希」（±1）：只用位置哈希时不同特征的碰撞会系统性
累加，符号化后碰撞期望相互抵消。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Sequence

from app.rag.embedding.base import l2_normalize

#: 词元：连续字母/数字/下划线（含 CJK 连续串）
_WORD = re.compile(r"[0-9A-Za-z_]+|[\u2e80-\u9fff]+")

#: 参与 n-gram 的最大字符数：块级文本（≈512 token）远小于这个值，
#: 上限只是防止有人把整篇文档直接丢进来时把 CPU 打满。
_MAX_NGRAM_CHARS = 20_000

_WHITESPACE = re.compile(r"\s+")


class HashEmbeddingProvider:
    """基于 feature hashing 的确定性向量化器。"""

    def __init__(self, *, dim: int = 1024, ngrams: Sequence[int] = (2, 3)) -> None:
        if dim <= 1:
            raise ValueError("dim 必须大于 1")
        self.dim = dim
        self.ngrams = tuple(ngrams)
        self.model_name = f"hash-ngram{dim}"

    # ------------------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码（纯 CPU、无网络、无权重文件）。"""
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """编码单条查询。"""
        return self._embed_one(text)

    # ------------------------------------------------------------------
    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        counts: dict[str, int] = {}
        for feature in self._features(text):
            counts[feature] = counts.get(feature, 0) + 1
        for feature, count in counts.items():
            index, sign = self._hash(feature)
            # sublinear tf：出现 100 次的词不应比出现 1 次的词重要 100 倍
            vector[index] += sign * (1.0 + math.log(count))
        normalized = l2_normalize(vector)
        if not any(normalized):
            # 空文本不会走到这里（切分阶段已丢弃），但兜底给一个合法单位向量，
            # 避免零向量在 COSINE 度量下与其他向量相似度恒为 0 而静默失配。
            fallback = [0.0] * self.dim
            fallback[0] = 1.0
            return fallback
        return normalized

    def _features(self, text: str) -> Iterable[str]:
        compact = _WHITESPACE.sub("", text)
        for token in _WORD.findall(text.lower()):
            yield f"w:{token}"
        window = compact[:_MAX_NGRAM_CHARS]
        for size in self.ngrams:
            if len(window) < size:
                continue
            for start in range(len(window) - size + 1):
                yield f"g{size}:{window[start : start + size]}"

    def _hash(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode(), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "big") % self.dim
        sign = 1.0 if digest[8] & 1 else -1.0
        return index, sign


__all__ = ["HashEmbeddingProvider"]
