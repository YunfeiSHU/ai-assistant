"""Embedding 工厂 + 进程内缓存。

缓存键是 **``content_sha256``** 而不是文本本身：切片内容可能很长，用哈希做键
内存占用恒定，而且与 ``REQ-RAG-007`` 的去重口径是同一种「内容同一性」。
生产环境应换成 Redis（``docs/06`` §4.4，TTL 24h）；这里先用有界 LRU 保证
「重跑入库任务不会重复算同一批向量」，这是本地最需要的性质。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

from app.config import Settings
from app.core.text import sha256_hex
from app.rag.embedding.base import EmbeddingProvider
from app.rag.embedding.bge import BgeEmbeddingProvider
from app.rag.embedding.hash import HashEmbeddingProvider

#: LRU 容量：按 1024 维 float 估算，2000 条约 16 MB，足够覆盖一次重跑
CACHE_MAX_ENTRIES = 2000


class CachingEmbeddingProvider:
    """给任意 provider 套一层 LRU 缓存。"""

    def __init__(self, inner: EmbeddingProvider, *, max_entries: int = CACHE_MAX_ENTRIES) -> None:
        self._inner = inner
        self._max = max_entries
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """命中缓存的直接返回，未命中的整批交给内层（批量编码效率更高）。"""
        keys = [sha256_hex(text) for text in texts]
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for index, key in enumerate(keys):
            cached = self._cache.get(key)
            if cached is None:
                missing.append(index)
                continue
            self.hits += 1
            self._cache.move_to_end(key)
            results[index] = cached

        if missing:
            self.misses += len(missing)
            computed = self._inner.embed([texts[index] for index in missing])
            for offset, index in enumerate(missing):
                if offset >= len(computed):
                    continue
                vector = computed[offset]
                results[index] = vector
                self._cache[keys[index]] = vector
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

        fallback = [0.0] * self.dim
        return [vector if vector is not None else fallback for vector in results]

    def embed_query(self, text: str) -> list[float]:
        """单条查询编码（同样走缓存）。"""
        vectors = self.embed([text])
        return vectors[0] if vectors else [0.0] * self.dim

    def clear(self) -> None:
        self._cache.clear()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """按 ``EMBEDDING_PROVIDER`` 构造向量化器。"""
    provider: EmbeddingProvider
    if settings.embedding_provider == "bge":
        provider = BgeEmbeddingProvider(settings)
    else:
        provider = HashEmbeddingProvider(dim=settings.embedding_dim)
    if settings.embedding_cache_enabled:
        return CachingEmbeddingProvider(provider)
    return provider


__all__ = [
    "CACHE_MAX_ENTRIES",
    "BgeEmbeddingProvider",
    "CachingEmbeddingProvider",
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "build_embedding_provider",
]
