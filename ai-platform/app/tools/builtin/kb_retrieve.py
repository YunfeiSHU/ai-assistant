"""``kb_retrieve``：在用户知识库里做语义检索（``docs/04`` §3，P0）。

三个必须做对的点：

1. **租户隔离**：检索范围 MUST 用与 ``/chat`` 完全相同的 ``user_id``（``docs/04`` §3.1）。
   工具是最容易漏掉隔离的地方 —— 它看起来只是「查资料」。
2. **不合并去重**：去重与编号由 Agent Loop 统一处理（它才知道全局引用编号）。工具只负责
   「这一次查到了什么」。
3. **返回结构化 chunks**：引用编号需要全局分配，所以给结构化数据而不是渲染好的文本。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.rag.base import RetrievalUnavailable, Retriever
from app.tools.base import BuiltinTool, ToolContext, ToolExecutionError, ToolOutcome

TOOL_NAME = "kb_retrieve"
#: 描述必须写明「何时使用 / 何时不适用」（``docs/04`` §2.1）
DESCRIPTION = (
    "在用户的知识库中做语义检索，返回最相关的文档片段（含文件名、页码与片段 ID）。"
    "当问题可能涉及用户上传的个人/企业文档内容时使用；"
    "不要把常识问题或纯计算交给它。同一次提问里可以换关键词多次调用以获得更全的覆盖。"
)


class KbRetrieveArgs(BaseModel):
    """``kb_retrieve`` 参数（``docs/04`` §3 表）。"""

    query: str = Field(min_length=1, max_length=500, description="检索语句，尽量用陈述句而非问句")
    kb_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="限定检索的知识库 ID；留空表示检索该用户全部知识库",
    )
    top_k: int = Field(default=20, ge=1, le=100, description="向量召回条数")
    rerank_top_n: int = Field(default=5, ge=1, le=20, description="重排后保留条数")
    score_threshold: float | None = Field(
        default=None, ge=0.0, le=1.0, description="重排后最低分，低于该值不返回"
    )


class KbRetrieveTool(BuiltinTool):
    """知识库检索工具。"""

    name = TOOL_NAME
    description = DESCRIPTION
    input_model = KbRetrieveArgs
    side_effect = "read"
    #: 检索含向量召回 + 重排，比默认 10s 需要更多余量
    timeout_seconds = 15.0
    example_arguments: ClassVar[dict[str, Any]] = {"query": "退款政策 到账时间"}

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = KbRetrieveArgs.model_validate(arguments)
        try:
            chunks = await self._retriever.retrieve(
                query=args.query,
                user_id=ctx.user_id,
                kb_ids=args.kb_ids,
                top_k=args.top_k,
                rerank_top_n=args.rerank_top_n,
                score_threshold=args.score_threshold if args.score_threshold is not None else 0.0,
            )
        except RetrievalUnavailable as exc:
            # 检索不可用是**可展示**的失败：模型看到后可以改用其它工具或直接回答
            raise ToolExecutionError(f"知识库暂不可用：{exc}") from exc

        payload: dict[str, Any] = {
            "query": args.query,
            "total": len(chunks),
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "kb_id": chunk.kb_id,
                    "doc_name": chunk.doc_name,
                    "page": chunk.page,
                    "heading_path": chunk.heading_path or None,
                    "score": round(chunk.score, 6),
                    "content": chunk.text,
                }
                for chunk in chunks
            ],
        }
        if not chunks:
            # 明确告诉模型「没查到」而不是给空数组：它会知道该换关键词或直接回答，
            # 而不是把「空结果」当成「资料说不存在」
            payload["hint"] = "未检索到相关内容，可尝试更换关键词，或直接用已有信息回答"
        return ToolOutcome(
            status="ok",
            payload=payload,
            summary=_summary(chunks),
            citations=list(chunks),
        )


def _summary(chunks: list[Any]) -> str:
    """面向用户的摘要：只给「命中多少条 + 最高分 + 文档名」。"""
    if not chunks:
        return "未命中任何知识库片段"
    top = chunks[0]
    names = [chunk.doc_name or chunk.doc_id or "未命名" for chunk in chunks]
    # 同名文档只出现一次，避免「政策.md、政策.md、政策.md」这种噪音
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return f"命中 {len(chunks)} 个片段（最高分 {top.score:.3f}）：{'、'.join(unique[:3])}"


__all__ = ["DESCRIPTION", "TOOL_NAME", "KbRetrieveArgs", "KbRetrieveTool"]
