"""Embedding 工厂 + 进程内缓存。

缓存键是 **``content_sha256``** 而不是文本本身：切片内容可能很长，用哈希做键
内存占用恒定，而且与 ``REQ-RAG-007`` 的去重口径是同一种「内容同一性」。
生产环境应换成 Redis（``docs/06`` §4.4，TTL 24h）；这里先用有界 LRU 保证
「重跑入库任务不会重复算同一批向量」，这是本地最需要的性质。

四档 provider（``EMBEDDING_PROVIDER``）：
``hash``（确定性词法向量，测试/无网）、``bge``（本地权重，需下载数 GB）、
``ark``（火山方舟云端多模态，**每片一次 HTTP + 并发池**，见 ``ark.py`` 的语义说明）、
``siliconflow``（硅基流动云端，**真批量 N 进 N 出**，见 ``siliconflow.py``）。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence

from app.core.config import Settings
from app.core.text import sha256_hex
from app.rag.embedding.ark import ArkEmbeddingProvider
from app.rag.embedding.base import EmbeddingProvider
from app.rag.embedding.bge import BgeEmbeddingProvider
from app.rag.embedding.hash import HashEmbeddingProvider
from app.rag.embedding.siliconflow import SiliconFlowEmbeddingProvider

#: 缓存占用的**浮点数预算**（约 2M 个 → Python float 对象 ~32B/个 ≈ 64MB 上限）。
#: 条数由维度换算而来，而不是写死条数：同一条数在 1024 维与 2048 维下内存差一倍，
#: 写死"2000 条"在 ark/2048 档下会悄悄变成 ~130MB（本机可用内存只有 ~1.7GB）。
CACHE_MAX_FLOATS = 2_000_000


def cache_max_entries(dim: int) -> int:
    """按向量维度换算缓存条数（同一内存预算）。

    只保留这一个函数是有意的：条数必须**由实际维度算出来**，而不是写死一个常量。
    早期版本里有一个 ``CACHE_MAX_ENTRIES`` 常量（按 1024 维算好的 1953），
    换到 2048 维的档位时会悄悄把内存上限翻倍 —— 那正是这个文件顶部注释在讲的事。
    常量已删除（全仓库无引用），避免有人再拿它当默认值用。
    """
    return max(64, CACHE_MAX_FLOATS // max(1, dim))


class CachingEmbeddingProvider:
    """给任意 provider 套一层 LRU 缓存。"""

    def __init__(self, inner: EmbeddingProvider, *, max_entries: int | None = None) -> None:
        self._inner = inner
        self._max = max_entries if max_entries is not None else cache_max_entries(inner.dim)
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
        """清空缓存（测试与排障用：换 embedding 档位、或怀疑缓存串味时调用）。"""
        self._cache.clear()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """按 ``EMBEDDING_PROVIDER`` 构造向量化器。"""
    provider: EmbeddingProvider
    if settings.embedding_provider == "siliconflow":
        provider = SiliconFlowEmbeddingProvider(settings)
    elif settings.embedding_provider == "ark":
        provider = ArkEmbeddingProvider(settings)
    elif settings.embedding_provider == "bge":
        provider = BgeEmbeddingProvider(settings)
    else:
        provider = HashEmbeddingProvider(dim=settings.embedding_dim)
    if settings.embedding_cache_enabled:
        return CachingEmbeddingProvider(provider)
    return provider


__all__ = [
    "CACHE_MAX_FLOATS",
    "ArkEmbeddingProvider",
    "BgeEmbeddingProvider",
    "CachingEmbeddingProvider",
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "SiliconFlowEmbeddingProvider",
    "build_embedding_provider",
    "cache_max_entries",
]
