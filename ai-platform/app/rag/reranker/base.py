"""重排序端口（``docs/06`` §4.4 / §5.2）。

重排是**可选增强**：``docs/06`` §5.2 允许重排失败时退化，但 MUST 在
``degraded_reasons`` 里给出 ``rerank_skipped``。所以端口设计成「可以明确地说
『我没做』」，而不是「悄悄地按原分返回」——后者会让调用方以为分数经过了精排，
阈值判断的语义就悄悄变了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RerankResult:
    """重排结果。"""

    #: ``[(原始下标, 分数)]``，按分数降序
    ranked: list[tuple[int, float]]
    #: 是否真的执行了重排（``False`` = 退化，调用方 MUST 记 ``rerank_skipped``）
    applied: bool = True
    #: 退化原因（仅排障用）
    reason: str = ""


@runtime_checkable
class Reranker(Protocol):
    """交叉编码器重排端口。"""

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> RerankResult:
        """对候选文档重排。"""
        ...


class IdentityReranker:
    """不做重排：按原有顺序返回并标记 ``applied=False``。

    存在的意义是让「未启用重排」与「重排失败」走同一条可观测路径：两种情况
    上层都会记录 ``rerank_skipped``，用户看到的是同样的降级提示，而不是
    「有时有提示、有时没有」这种随配置变化的契约。
    """

    def __init__(self, *, reason: str = "reranker_disabled") -> None:
        self.reason = reason

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> RerankResult:
        ranked = [(index, 0.0) for index in range(len(documents))]
        return RerankResult(ranked=ranked[: max(0, top_n)], applied=False, reason=self.reason)


__all__ = ["IdentityReranker", "RerankResult", "Reranker"]
