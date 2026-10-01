"""云端向量化：火山方舟（Ark）多模态 embedding（``EMBEDDING_PROVIDER=ark``）。

与本地 BGE 及 OpenAI ``/embeddings`` 的语义都不同：``POST {base}/embeddings/multimodal``
的 ``input`` 是一个多模态输入（若干 text / image_url 片段），**一次请求只返回一条向量**。
传 N 条文本不会得到 N 条向量，而是聚合成一条（2026-10-02 实测：1 条 / 3 条 / 256 条 都只
返回 1 条 2048 维，而 ``usage.prompt_tokens=3493`` 说明它确实读完了 256 条；A+B 的向量
与 A 的余弦 0.80、与 B 的 0.47，说明是混合而非取 A）。术语上它是文档级多模态向量。

⇒ 一个 chunk = 一次 HTTP 请求，批量只能靠并发（本类用常驻线程池；实测每条 ~490 token 的块：
并发 1 → 4.4 req/s、32 → 98.9、64 → 99.5 无增益）。成本与限流随**片数**线性增长，
所以切分配方（UP-03）在这里直接决定请求数；TPM 不够时表现为 429，本类退避重试并尊重
``Retry-After``。

两个必须记住的后果：
1. 维度默认 2048（``-251215``；同族 ``-250615`` 是 1024）⇒ 换模型必须换集合名或重建
   Milvus 集合。维度能对齐 ≠ 向量空间能混用：即使按 ``dimensions=1024`` 输出，也与
   bge-m3 的 1024 维不在同一空间，混在一个集合里检索必然错。
2. ``encoding_format="base64"``（默认开启）把 ``data.embedding`` 变成 base64 的 float32
   小端数组，响应体 34,240B → 11,219B（3.05×）。字节序是实测定的：大端与 float 格式的
   逐元素差会大 150 倍。``dimensions=N`` 支持降维输出（实测 1024 可用、512 被拒），
   与全维前 N 维余弦 0.9992，是 Matryoshka 截断。

这个 API 跨请求不是逐位确定的：同一段文本连续三次调用，``#1 == #2`` 而 ``#3`` 与它们的最大
逐元素差 4.883e-03（纯 float32 累加顺序差异）。不要写「两次调用得到同一向量」的断言；
``CachingEmbeddingProvider``（按 ``content_sha256`` 缓存）反而比上游更稳定。
"""

from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.http import post_json_with_retry
from app.rag.embedding.base import l2_normalize

logger = logging.getLogger("app.rag.embedding")


class ArkEmbeddingProvider:
    """火山方舟多模态 embedding，每片一次请求 + 并发池。"""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self.dim = settings.embedding_dim
        self._base_url = settings.ark_base_url.rstrip("/")
        self._api_key = settings.ark_api_key
        self._concurrency = max(1, settings.ark_embedding_concurrency)
        self._timeout = settings.ark_embedding_timeout_seconds
        self._max_retries = max(0, settings.ark_embedding_max_retries)
        self._encoding = settings.ark_embedding_encoding
        self._dimensions = max(0, settings.ark_embedding_dimensions)
        self._instructions = settings.ark_embedding_instructions.strip()
        self._client: httpx.Client | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 惰性资源：客户端与线程池都按需创建（与 BGE 的“懒加载权重”同一个理由：
    # 构造 provider 不该产生副作用，否则导入 app 就会连网）
    @property
    def client(self) -> httpx.Client:
        """懒加载 HTTP 客户端（复用连接：每次新建会白付 TLS 握手）。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(
                        timeout=self._timeout,
                        # 连接池要比并发数略宽：留出重试与查询编码的位置
                        limits=httpx.Limits(
                            max_connections=self._concurrency + 4,
                            max_keepalive_connections=self._concurrency + 4,
                        ),
                    )
        return self._client

    @property
    def pool(self) -> ThreadPoolExecutor:
        """常驻线程池：它同时是全局并发上限（多次 embed() 也共用它）。

        用常驻池而不是每次 ``embed()`` 新建：一次 8MB 入库会调用几百次 ``embed()``，
        每次建池既浪费又让并发上限随批次漂移。
        """
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=self._concurrency,
                        thread_name_prefix="ark-embed",
                    )
        return self._pool

    def close(self) -> None:
        """释放连接与线程池（进程退出/测试清理用）。"""
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False)
                self._pool = None
            if self._client is not None:
                self._client.close()
                self._client = None

    # ------------------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码：每条文本一次请求，由线程池并发，返回顺序与入参一致。"""
        items = list(texts)
        if not items:
            return []
        if not self._api_key:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "未配置 ARK_API_KEY，无法使用 EMBEDDING_PROVIDER=ark",
                {"hint": "在 .env 里填 ARK_API_KEY，或改回 EMBEDDING_PROVIDER=hash"},
            )

        started = time.perf_counter()
        vectors = list(self.pool.map(self._embed_one, items))
        if len(vectors) != len(items):
            # 顺序靠 ``map`` 保证，但数量对不上必须显式报错，否则会把「少了一条」
            # 静默写成「chunk 与向量错位」—— 检索结果会莫名其妙地串行
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"向量条数与输入不一致：期望 {len(items)}，得到 {len(vectors)}",
                {"model": self.model_name},
            )
        logger.info(
            "embedding.ark_batch",
            extra={
                "model": self.model_name,
                "count": len(items),
                "concurrency": min(self._concurrency, len(items)),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """编码单条查询（与入库同一个模型，否则两个向量不在同一空间）。"""
        vectors = self.embed([text])
        return vectors[0] if vectors else l2_normalize([0.0] * self.dim)

    # ------------------------------------------------------------------
    def _embed_one(self, text: str) -> list[float]:
        """单条文本的「请求 + 退避重试 + 维度自检」。

        重试语义走 :func:`app.core.http.post_json_with_retry`，与硅基流动那档、云端 rerank
        共用同一份策略（只有 429/5xx/超时重试、4xx 立刻失败并带出上游原文、尊重
        ``Retry-After``）。
        """
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": [{"type": "text", "text": text}],
            "encoding_format": self._encoding,
        }
        if self._dimensions:
            payload["dimensions"] = self._dimensions
        if self._instructions:
            payload["instructions"] = self._instructions
        body = post_json_with_retry(
            self.client,
            f"{self._base_url}/embeddings/multimodal",
            payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
            max_retries=self._max_retries,
            provider="Ark embedding",
        )
        return self._check_dim(self._extract_vector(body))

    # ------------------------------------------------------------------
    def _extract_vector(self, body: Any) -> list[float]:
        """从响应里取出向量。

        同时兼容三种形状，这样以后换成真批量的文本模型只需改 ``ARK_BASE_URL`` +
        ``EMBEDDING_MODEL``：多模态 + base64（默认）、多模态 + float、OpenAI 风格批量端点。
        """
        data = (body or {}).get("data")
        if isinstance(data, dict):
            value = data.get("embedding")
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            value = data[0].get("embedding")
        else:
            value = None

        if isinstance(value, str):
            return self._decode_base64(value)
        if isinstance(value, list) and value:
            return [float(item) for item in value]
        raise AppError(
            ErrorCode.RETRIEVAL_FAILED,
            "Ark embedding 响应里没有可用向量",
            {"model": self.model_name, "shape": type(data).__name__},
        )

    @staticmethod
    def _decode_base64(payload: str) -> list[float]:
        """解码 base64 的 float32 数组（小端）。

        字节序不是猜的：小端解出的向量与 ``encoding_format=float`` 的逐元素差等于 API 自身的
        跨请求抖动（~5e-3），大端则差 150 倍。
        """
        try:
            raw = base64.b64decode(payload, validate=False)
        except Exception as exc:  # pragma: no cover - 只有上游返回坏 base64 才走到
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"Ark embedding 的 base64 向量解码失败：{exc}",
                {"payload_chars": len(payload)},
            ) from exc
        if len(raw) % 4 != 0:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"Ark embedding 返回的字节数不是 float32 的整数倍（{len(raw)}）",
                {"payload_chars": len(payload)},
            )
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))

    def _check_dim(self, vector: list[float]) -> list[float]:
        """核对维度并 L2 归一化（集合用 COSINE，语义与本地 BGE 保持一致）。"""
        if len(vector) != self.dim:
            raise AppError(
                ErrorCode.VECTOR_DIM_MISMATCH,
                f"Ark 返回维度（{len(vector)}）与 EMBEDDING_DIM（{self.dim}）不一致",
                {
                    "model": self.model_name,
                    "actual_dim": len(vector),
                    "configured_dim": self.dim,
                    "hint": "同步 EMBEDDING_DIM 与 MILVUS_VECTOR_DIM，并重建 Milvus 集合",
                },
            )
        return l2_normalize(vector)


__all__ = ["ArkEmbeddingProvider"]
