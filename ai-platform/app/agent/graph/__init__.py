"""LangGraph 状态图：一个最小的「检索 → 生成」RAG 流程骨架。

实际接入时把 ``retrieve`` / ``generate`` 两个节点替换为真实实现即可，
节点签名保持 ``(state) -> dict`` 的部分更新语义。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """在图中流转的状态。

    ``add_messages`` 让 ``messages`` 字段按「追加」而不是「覆盖」的方式合并。
    """

    messages: Annotated[list[Any], add_messages]
    question: str
    contexts: list[str]
    answer: str


def retrieve(state: AgentState) -> dict[str, Any]:
    """检索节点：TODO 接入 ``MilvusVectorStore`` + ``RerankerService``。"""
    return {"contexts": []}


def generate(state: AgentState) -> dict[str, Any]:
    """生成节点：TODO 接入 LLM（``langchain_openai.ChatOpenAI``）。"""
    return {"answer": ""}


def build_graph() -> Any:
    """构建并编译 RAG 状态图。

    Returns:
        可直接 ``await graph.ainvoke({...})`` 的已编译图。
    """
    builder = StateGraph(AgentState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("generate", generate)
    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "generate")
    builder.add_edge("generate", END)
    return builder.compile()


__all__ = ["AgentState", "build_graph", "generate", "retrieve"]
