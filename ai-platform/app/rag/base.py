"""检索层协议（正文检索实现见 M3 ``docs/06-知识库``）。

这一层在 M2 就建好，是因为 **``use_rag=true`` 时的降级语义**属于对话契约
（``REQ-CHAT-007``）：检索失败不能拖垮对话，而必须变成 ``degraded=true`` +
``degraded_reasons=["rag_unavailable"]``。协议先定下来，M3 只要填实现。

:class:`NullRetriever` 刻意**抛异常**而不是返回空列表 —— 返回空列表会被上层理解成
「库里确实没有相关资料」，把「功能没接」伪装成「检索没命中」，是最难查的一类问题。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class RetrievalUnavailable(RuntimeError):
    """检索组件不可用（未接入 / 向量库宕机）。该失败 MUST NOT 让对话失败。"""


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """一条召回片段。

    除「文本 + 分数」之外还带着 :attr:`chunk_index` 与定位字段，因为检索后处理
    需要它们：相邻切片合并靠 ``chunk_index`` 判「是否相邻」，引用溯源靠
    ``heading_path`` / ``char_start`` / ``char_end`` 告诉用户「答自哪里」。
    如果只传文本，后续两步就只能退回字符串匹配，既不准也不可测。

    ``score`` 的 **语义会变**：向量召回阶段是余弦相似度，重排阶段是交叉编码器
    打分（同量级但不同分布）。因此所有下游阈值（``score_threshold``）只能在
    重排之后统一应用。
    """

    chunk_id: str
    text: str
    doc_id: str = ""
    kb_id: str = ""
    doc_name: str = ""
    page: int | None = None
    score: float = 0.0
    #: 向量召回原始分（重排后仍保留，``§5.1`` 的 ``vector_score``）
    vector_score: float = 0.0
    #: 是否由相邻切片合并而来（``§5.2`` 第 4 条的 ``merged``）
    merged: bool = False
    #: 在所属文档内的序号（相邻合并用）
    chunk_index: int = 0
    #: 标题路径，如 ``售后政策 > 退款 > 时效``
    heading_path: str = ""
    #: 在整篇正文中的字符区间（引用溯源用）
    char_start: int = 0
    char_end: int = 0
    #: 归属用户（向量库实现里用于多租户过滤。）
    user_id: str = ""


@runtime_checkable
class Retriever(Protocol):
    """向量 + 重排检索。"""

    async def retrieve(
        self,
        *,
        query: str,
        user_id: str,
        kb_ids: Sequence[str] = (),
        top_k: int = 20,
        rerank_top_n: int = 5,
        score_threshold: float = 0.0,
    ) -> list[RetrievedChunk]:
        """按查询召回片段，已按相关性从高到低排序。"""
        ...


class NullRetriever:
    """M2 占位实现：明确告知「尚未接入」。"""

    async def retrieve(
        self,
        *,
        query: str,
        user_id: str,
        kb_ids: Sequence[str] = (),
        top_k: int = 20,
        rerank_top_n: int = 5,
        score_threshold: float = 0.0,
    ) -> list[RetrievedChunk]:
        raise RetrievalUnavailable("RAG 检索尚未接入")


__all__ = ["NullRetriever", "RetrievalUnavailable", "RetrievedChunk", "Retriever"]
