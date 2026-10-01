"""硅基流动（SiliconFlow）向量化（``EMBEDDING_PROVIDER=siliconflow``）。

**与火山方舟那档的根本区别：这个端点支持真批量。** 官方文档
（``api-docs.siliconflow.cn/docs/api/embeddings-post``）原文：

    input: string | array —— 要在单次请求中处理多个输入，请传递字符串数组。

实测（``Qwen/Qwen3-Embedding-0.6B``，490 token 的块）：传 8/32/64/128 条字符串，
**每次都返回同数量的 ``data``，每条带 ``index``**；batch=32 × 并发 8 ⇒ **379.6 片/s**
（对比方舟"一条请求一条向量"的最好成绩 98.9 req/s，且方舟的 98.9 是**请求**速率）。
⇒ 这里不需要"每片一次 HTTP + 并发"，只需要"分批 + 少量并发"。

其余实测结论：

* **原生 1024 维**；``dimensions`` 支持降维，``Qwen3-Embedding-0.6B`` 可选
  ``[64,128,256,512,768,1024]``（MRL，见 ``SILICONFLOW_EMBEDDING_DIMENSIONS``）；
* 上下文窗口 **32768 token**（``docs`` 明示；bge-m3 是 8194）⇒ 长块基本不会被截断；
* ``encoding_format="base64"`` 支持：单条 21,891B → 5,641B（**3.9×**），
  解出来是 float32 **小端**（与 float 格式逐元素完全一致，实测差 0.000e+00）；
* **可复现性比方舟好**：同请求体重复调用逐位相同（float×3、base64×2、跨编码均为
  0.000e+00），而方舟同一文本三次调用会出现 4.883e-03 的差。唯一一次 1.9e-3 的
  差异出现在**不同批量组成**的两次调用之间（见 ``docs/12``），对检索无影响。
* 错误体是 ``{"code": 20012, "message": "Model does not exist..."}`` 形状，
  401 是 ``{"code": 30014, "message": "Token is invalid."}`` ⇒ 直接把 ``message``
  带进异常详情（由 :mod:`app.core.http` 统一处理）。
"""

from __future__ import annotations

import base64
import logging
import struct
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.http import post_json_with_retry
from app.rag.embedding.base import l2_normalize

logger = logging.getLogger("app.rag.embedding")


class SiliconFlowEmbeddingProvider:
    """硅基流动 ``/v1/embeddings``（OpenAI 兼容，**N 进 N 出**）。"""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self.dim = settings.embedding_dim
        self._base_url = settings.siliconflow_base_url.rstrip("/")
        self._api_key = settings.siliconflow_api_key
        self._batch_size = max(1, settings.embedding_batch_size)
        self._concurrency = max(1, settings.siliconflow_embedding_concurrency)
        self._timeout = settings.siliconflow_embedding_timeout_seconds
        self._max_retries = max(0, settings.siliconflow_embedding_max_retries)
        self._encoding = settings.siliconflow_embedding_encoding
        self._dimensions = max(0, settings.siliconflow_embedding_dimensions)
        self._client: httpx.Client | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        """懒加载 HTTP 客户端（复用连接：每次新建会白付 TLS 握手）。"""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=self._timeout)
        return self._client

    @property
    def pool(self) -> ThreadPoolExecutor:
        """常驻线程池：**同时**是并发上限与连接复用点。"""
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=self._concurrency, thread_name_prefix="sf-embed"
                    )
        return self._pool

    def close(self) -> None:
        """释放线程池与连接（进程退出 / 测试 teardown 时调用）。"""
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """分批 + 有限并发地编码；返回顺序与 ``texts`` 严格一致。"""
        items = list(texts)
        if not items:
            return []
        batches = [items[i : i + self._batch_size] for i in range(0, len(items), self._batch_size)]
        if len(batches) == 1:
            return self._embed_batch(batches[0])
        # ``pool.map`` 保序（与方舟那档同一个选择：结果顺序必须与入参一一对应）
        vectors: list[list[float]] = []
        for chunk in self.pool.map(self._embed_batch, batches):
            vectors.extend(chunk)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """编码单条查询。"""
        vectors = self.embed([text])
        if not vectors:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED, "硅基流动未返回查询向量", {"model": self.model_name}
            )
        return vectors[0]

    # ------------------------------------------------------------------
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "未配置 SILICONFLOW_API_KEY，无法调用硅基流动向量化",
                {"hint": "在 .env 里设置 SILICONFLOW_API_KEY（只放 .env，不要提交）"},
            )
        payload: dict[str, Any] = {
            "model": self.model_name,
            "input": texts,
            "encoding_format": self._encoding,
        }
        if self._dimensions:
            payload["dimensions"] = self._dimensions
        body = post_json_with_retry(
            self.client,
            f"{self._base_url}/embeddings",
            payload=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self._timeout,
            max_retries=self._max_retries,
            provider="siliconflow",
        )
        vectors = self._parse(body, expected=len(texts))
        logger.info(
            "embedding.siliconflow_batch",
            extra={"count": len(vectors), "dim": len(vectors[0]) if vectors else 0},
        )
        return vectors

    def _parse(self, body: dict[str, Any], *, expected: int) -> list[list[float]]:
        """把 ``data`` 还原成与入参同序的向量列表。

        用 ``index`` 而不是"按返回顺序"来定位：文档承诺了 ``index``，就该用它 ——
        把顺序寄托在"上游一定按序返回"上，是最容易在某次升级后错位的地方，
        而错位的后果是"chunk 与向量串行"，检索结果会变得莫名其妙。
        """
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                "硅基流动响应里没有向量",
                {"model": self.model_name, "shape": type(data).__name__},
            )
        if len(data) != expected:
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"硅基流动返回的向量数（{len(data)}）与请求条数（{expected}）不一致",
                {"model": self.model_name},
            )
        slots: list[list[float] | None] = [None] * expected
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index", 0)
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = 0
            if not 0 <= index < expected:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"硅基流动返回了越界的 index={raw_index}",
                    {"model": self.model_name, "expected": expected},
                )
            slots[index] = self._decode(item.get("embedding"))
        if any(slot is None for slot in slots):
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                "硅基流动响应缺少部分 index 对应的向量",
                {"model": self.model_name, "expected": expected},
            )
        return [l2_normalize(self._check_dim(slot)) for slot in slots if slot is not None]

    def _decode(self, value: Any) -> list[float]:
        """``float`` 是 JSON 数组；``base64`` 是 float32 小端。"""
        if isinstance(value, str):
            try:
                raw = base64.b64decode(value, validate=False)
            except Exception as exc:  # pragma: no cover - 上游返回坏 base64 才走到
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"硅基流动 base64 向量解码失败：{exc}",
                    {"payload_chars": len(value)},
                ) from exc
            if len(raw) % 4 != 0:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"硅基流动返回的字节数不是 float32 的整数倍（{len(raw)}）",
                    {"payload_chars": len(value)},
                )
            return list(struct.unpack(f"<{len(raw) // 4}f", raw))
        if isinstance(value, list) and value:
            return [float(item) for item in value]
        raise AppError(
            ErrorCode.RETRIEVAL_FAILED,
            "硅基流动的 embedding 字段既不是数组也不是 base64 字符串",
            {"type": type(value).__name__},
        )

    def _check_dim(self, vector: list[float]) -> list[float]:
        """维度自检：不一致时立刻失败，而不是把坏向量写进 Milvus。"""
        if len(vector) != self.dim:
            raise AppError(
                ErrorCode.VECTOR_DIM_MISMATCH,
                f"向量维度不符：模型返回 {len(vector)}，配置 EMBEDDING_DIM={self.dim}",
                {
                    "model": self.model_name,
                    "actual_dim": len(vector),
                    "configured_dim": self.dim,
                    "hint": "用 EMBEDDING_DIM / MILVUS_VECTOR_DIM 对齐模型真实维度，"
                    "并换一个新的 MILVUS_COLLECTION（旧向量是旧维度，检索不出来）",
                },
            )
        return vector


__all__ = ["SiliconFlowEmbeddingProvider"]
