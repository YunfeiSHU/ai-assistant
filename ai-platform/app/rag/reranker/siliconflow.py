"""硅基流动重排序（``RERANKER_PROVIDER=siliconflow``）。

换它的理由是一个实测数字：本机 ``BAAI/bge-reranker-v2-m3``（CPU）在 ``top_k=20 → top_n=5``
时要 70~83s（``ai-platform-go/docs/11-§6.6``），而 ``Qwen/Qwen3-Reranker-0.6B`` 走这个端点
只要 562~663ms（约 120×），回到了 SRS 的目标区间（``docs/10-§1``）。

实测过的接口细节（都有坑）：响应是 ``{id, results: [{index, relevance_score, document}], meta}``
—— 没有 ``usage``，计费在 ``meta`` 里；``documents`` 必须是字符串数组（传 ``[{"text": ...}]``
会 400，那是 VL 端点的形状）；``return_documents`` 默认 ``false``；实测 201 条候选仍是 200，
但为控尾延迟本实现按 ``SILICONFLOW_RERANK_MAX_DOCUMENTS`` 分片，每片各取 ``top_n`` 再全局归并。

失败必须退化而不是抛：重排是可选增强，让一次网络抖动变成对话 500 是把可选依赖变成硬依赖。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.http import post_json_with_retry
from app.rag.reranker.base import RerankResult

logger = logging.getLogger("app.rag.reranker")


class SiliconFlowReranker:
    """``Qwen/Qwen3-Reranker-*`` 等（``POST /v1/rerank``）。"""

    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.reranker_model
        self._base_url = settings.siliconflow_base_url.rstrip("/")
        self._api_key = settings.siliconflow_api_key
        self._timeout = settings.siliconflow_rerank_timeout_seconds
        self._max_retries = max(0, settings.siliconflow_rerank_max_retries)
        self._max_documents = max(1, settings.siliconflow_rerank_max_documents)
        self._instruction = settings.siliconflow_rerank_instruction.strip()
        self._client: httpx.Client | None = None
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

    def close(self) -> None:
        """释放连接（进程退出 / 测试 teardown 时调用）。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> RerankResult:
        """重排；任何异常都退化为「按原顺序返回 + applied=False」。"""
        items = list(documents)
        if not items:
            return RerankResult(ranked=[], applied=True)
        try:
            import anyio

            ranked = await anyio.to_thread.run_sync(
                lambda: self._rerank_sync(query, items, top_n=top_n)
            )
        except Exception as exc:
            logger.warning(
                "rerank.failed",
                extra={"model": self.model_name, "error": str(exc), "candidates": len(items)},
            )
            return RerankResult(
                ranked=[(index, 0.0) for index in range(min(top_n, len(items)))],
                applied=False,
                reason=f"rerank_error: {exc}",
            )
        return RerankResult(ranked=ranked[: max(0, top_n)], applied=True)

    # ------------------------------------------------------------------
    def _rerank_sync(
        self, query: str, documents: list[str], *, top_n: int
    ) -> list[tuple[int, float]]:
        if not self._api_key:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "未配置 SILICONFLOW_API_KEY，无法调用硅基流动重排",
                {"hint": "在 .env 里设置 SILICONFLOW_API_KEY"},
            )
        want = max(1, top_n)
        scores: list[tuple[int, float]] = []
        for start in range(0, len(documents), self._max_documents):
            shard = documents[start : start + self._max_documents]
            payload: dict[str, Any] = {
                "model": self.model_name,
                "query": query,
                "documents": shard,
                # 正文我们本来就有（就在 shard 里），不需要上游回传一份
                "return_documents": False,
                # 分片时每片都要给足 top_n，否则全局前 N 可能被截在某个片外
                "top_n": min(want, len(shard)),
            }
            if self._instruction:
                payload["instruction"] = self._instruction
            body = post_json_with_retry(
                self.client,
                f"{self._base_url}/rerank",
                payload=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                max_retries=self._max_retries,
                provider="siliconflow",
            )
            scores.extend(self._parse(body, offset=start, size=len(shard)))
        ranked = sorted(scores, key=lambda item: item[1], reverse=True)
        logger.info(
            "rerank.siliconflow_done",
            extra={"model": self.model_name, "candidates": len(documents), "returned": len(ranked)},
        )
        return ranked

    def _parse(self, body: dict[str, Any], *, offset: int, size: int) -> list[tuple[int, float]]:
        """``results[].index`` 是**片内**下标 ⇒ 必须加回偏移量。"""
        results = body.get("results")
        if not isinstance(results, list):
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                "硅基流动重排响应里没有 results",
                {"model": self.model_name, "type": type(results).__name__},
            )
        parsed: list[tuple[int, float]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index")
            if raw_index is None:
                # 缺 index 属于上游契约变化：显式报错 ⇒ 外层退化成「不重排」（可见的
                # rerank_skipped），好过静默丢一个候选让排序悄悄变形。
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    "硅基流动重排响应里有结果缺少 index",
                    {"model": self.model_name},
                )
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"硅基流动重排返回了非整数的 index={raw_index!r}",
                    {"model": self.model_name},
                ) from exc
            if not 0 <= index < size:
                raise AppError(
                    ErrorCode.RETRIEVAL_FAILED,
                    f"硅基流动重排返回越界 index={raw_index}（片大小 {size}）",
                    {"model": self.model_name},
                )
            parsed.append((offset + index, float(item.get("relevance_score", 0.0))))
        return parsed


__all__ = ["SiliconFlowReranker"]
