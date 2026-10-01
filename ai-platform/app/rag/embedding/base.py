"""Embedding 端口与通用工具（``docs/06`` §4.4）。

DeepSeek 之类只提供对话接口，不提供 embedding，所以向量化必须本地跑；但本地跑 BGE 需要
下载数 GB 权重。于是拆成三档：``ark``（火山方舟云端多模态，按 token 计费且一条请求一条
向量）、``bge``（本地权重，CPU 上 8MB 要 70~100min）、``hash``（确定性词法向量，零下载）。

``hash`` 不是玩具：它保证同一个输入永远得到同一个向量（用 blake2b 而不是受
``PYTHONHASHSEED`` 影响的内置 ``hash()``），于是「检索链路」本身可以被机械断言
（upsert 什么、按什么过滤、相邻切片怎么合并、阈值怎么生效）；拿语义向量测这些会因为
权重版本变动而随机失败。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing

#: 单批 Embedding 的超时（``docs/06`` §4.4：单批超时 120s）
BATCH_TIMEOUT_SECONDS = 120.0

#: 单批失败的重试间隔（§4.4：重试 2 次，间隔 1s / 3s）
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """文本向量化端口。"""

    # 声明为只读属性（而不是裸变量）：实现方普遍用 ``@property`` 暴露维度，
    # 裸变量在结构匹配时要求可写，会把包装类（缓存/适配器）全部判为不兼容。

    @property
    def dim(self) -> int:
        """向量维度（MUST 与 Milvus 集合声明一致，``docs/09`` §3）。"""
        ...

    @property
    def model_name(self) -> str:
        """模型标识（写入 KB / chunk 元数据，便于排障复现）。"""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本列表编码为**归一化**向量（度量用 COSINE）。

        Raises:
            AppError: 编码失败时抛 ``RETRIEVAL_FAILED`` / ``VECTOR_DIM_MISMATCH``。
        """
        ...

    def embed_query(self, text: str) -> list[float]:
        """编码单条查询。"""
        ...


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """L2 归一化；零向量原样返回（避免除零）。"""
    total = sum(value * value for value in vector) ** 0.5
    if total == 0.0:
        return list(vector)
    return [value / total for value in vector]


async def embed_texts(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    timeout: float = BATCH_TIMEOUT_SECONDS,
    delays: Sequence[float] = RETRY_DELAYS,
) -> list[list[float]]:
    """带重试与超时的批量编码。

    重试的是整批而不是单条：失败几乎总是资源类问题（超时、显存不足），重试整批能自愈；
    逐条重试反而会把一次失败放大成 N 次慢调用。

    埋点放在这里而不是各个 provider 里（``docs/10`` §5.1 的 ``embed.batch``）：这是所有
    向量化的必经之路，放 provider 里就得在每个实现里各写一遍，迟早有一个漏掉。
    """
    last_error: Exception | None = None
    attempts = len(delays) + 1
    started = time.perf_counter()
    for attempt in range(attempts):
        try:
            with get_tracing().span(
                "embed.batch",
                {
                    "batch_size": len(texts),
                    "model": provider.model_name,
                    "attempt": attempt + 1,
                },
            ):
                result = await asyncio.wait_for(
                    asyncio.to_thread(provider.embed, list(texts)), timeout=timeout
                )
        except Exception as exc:
            last_error = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
        else:
            # ``cache_hit=False`` 恒真：当前没有 embedding 缓存（缓存一旦引入，
            # 这个标签才真正区分得开「命中缓存的快」与「模型真的快了」）。
            get_metrics().observe_embedding(
                model=provider.model_name,
                cache_hit=False,
                seconds=time.perf_counter() - started,
            )
            return result
    from app.core.exceptions import AppError, ErrorCode

    raise AppError(
        ErrorCode.RETRIEVAL_FAILED,
        f"Embedding 失败（已重试 {attempts} 次）：{last_error}",
        {"batch_size": len(texts)},
    ) from last_error


__all__ = [
    "BATCH_TIMEOUT_SECONDS",
    "RETRY_DELAYS",
    "EmbeddingProvider",
    "embed_texts",
    "l2_normalize",
]
