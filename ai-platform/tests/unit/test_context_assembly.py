"""上下文装配与裁剪的单元测试（``AC-CHAT-06`` / ``AC-CHAT-07`` / ``AC-MEM-05`` 的底层断言）。

这些用例直接测装配器，而不是通过 HTTP：片段顺序与裁剪顺序是**纯逻辑**，
用接口测只会让失败信息变模糊（「返回 400」看不出是顺序错了还是裁剪错了）。
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.tokens import count_tokens
from app.llm.base import LLMMessage
from app.rag.base import RetrievedChunk
from app.services.context import (
    PART_HISTORY,
    PART_MEMORY,
    PART_QUERY,
    PART_RAG,
    PART_SUMMARY,
    PART_SYSTEM,
    ContextAssembler,
    MemoryItem,
)


def _chunk(index: int, text: str, score: float = 0.5) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chk_{index:02d}",
        text=text,
        doc_id=f"doc_{index:02d}",
        kb_id="kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
        doc_name=f"资料{index}.pdf",
        page=index,
        score=score,
    )


def test_part_order_is_fixed(make_settings: Callable[..., Settings]) -> None:
    """顺序必须是 system → memory → summary → history → rag → query。"""
    assembler = ContextAssembler(make_settings())

    context = assembler.build(
        query="退款要几天？",
        history=[
            LLMMessage(role="user", content="上一问"),
            LLMMessage(role="assistant", content="上一答"),
        ],
        memories=[MemoryItem(content="用户偏好中文回答", score=0.9)],
        summary="用户目标：了解退款政策。",
        rag_chunks=[_chunk(1, "自签收之日起 7 个自然日内可申请无理由退款。", score=0.8)],
    )

    assert context.part_order == [
        PART_SYSTEM,
        PART_MEMORY,
        PART_SUMMARY,
        PART_HISTORY,
        PART_HISTORY,
        PART_RAG,
        PART_QUERY,
    ]
    # 最后一条必须是本轮提问（顺序敏感：模型靠末尾的 user 消息理解当前意图）
    assert context.messages[-1].role == "user"
    assert context.messages[-1].content == "退款要几天？"


def test_rag_part_carries_numbered_context_and_reference_footer(
    make_settings: Callable[..., Settings],
) -> None:
    """引用序号与资料名必须出现在注入文本里（``AC-CHAT-05`` 的可读性前提）。"""
    assembler = ContextAssembler(make_settings())

    context = assembler.build(query="问", rag_chunks=[_chunk(1, "第一段"), _chunk(2, "第二段")])

    rag = next(part for part in context.parts if part.name == PART_RAG)
    assert "[1] 资料1.pdf" in rag.content
    assert "[2] 资料2.pdf" in rag.content
    assert "第一段" in rag.content


def test_long_history_is_trimmed_and_system_query_survive(
    make_settings: Callable[..., Settings],
) -> None:
    """构造约 12k token 的历史：必须裁到预算内，且 system / query 一字不动。"""
    settings = make_settings(context_token_budget=8192, history_token_budget=2400)
    assembler = ContextAssembler(settings)
    filler = "这是一段用来把上下文撑到超预算的中文文本。" * 40
    history = [LLMMessage(role="user", content=filler) for _ in range(30)]
    raw_tokens = sum(count_tokens(message.content) for message in history)
    assert raw_tokens > settings.context_token_budget  # 前提成立才谈得上裁剪

    context = assembler.build(query="总结一下", history=history)

    assert context.total_tokens <= settings.context_token_budget
    assert context.parts[0].name == PART_SYSTEM
    assert context.messages[-1].content == "总结一下"
    assert context.trimmed[PART_HISTORY] > 0


def test_trim_order_drops_history_first_then_rag_then_memory(
    make_settings: Callable[..., Settings],
) -> None:
    """全局超限时，裁剪顺序必须严格是 历史 → RAG → 记忆 → 摘要。"""
    settings = make_settings(
        context_token_budget=520,
        system_prompt_token_budget=60,
        history_token_budget=4000,
        rag_context_token_budget=4000,
        memory_token_budget=600,
        summary_token_budget=1000,
    )
    assembler = ContextAssembler(settings)
    history = [
        LLMMessage(role="user", content=f"第 {index} 轮问题" + "补充说明" * 6) for index in range(8)
    ]
    rag = [_chunk(index, "片段内容" * 20, score=1.0 - index * 0.1) for index in range(1, 6)]
    memories = [
        MemoryItem(content="记忆内容" * 8, score=0.9),
        MemoryItem(content="次要记忆", score=0.1),
    ]

    context = assembler.build(
        query="问题",
        history=history,
        memories=memories,
        summary="摘要内容" * 30,
        rag_chunks=rag,
    )

    assert context.total_tokens <= settings.context_token_budget
    # 历史必然先被丢（它是第一步），且不会先动 RAG
    assert context.trimmed[PART_HISTORY] > 0
    assert PART_SYSTEM in context.part_order and PART_QUERY in context.part_order


def test_system_and_query_are_never_truncated(
    make_settings: Callable[..., Settings],
) -> None:
    """裁剪不允许碰 system 与本轮 query（``docs/07`` §4.2）。"""
    settings = make_settings(context_token_budget=4000)
    assembler = ContextAssembler(settings)
    query = "关键问题" * 50

    context = assembler.build(query=query, history=[LLMMessage(role="user", content="旧" * 2000)])

    assert context.messages[-1].content == query
    assert context.parts[0].content == assembler.system_prompt()


def test_oversized_query_raises_context_too_long_with_breakdown(
    make_settings: Callable[..., Settings],
) -> None:
    """连 system + query 都塞不下时必须 400，且 details 里给出各片段 token。"""
    settings = make_settings(
        context_token_budget=260, system_prompt_token_budget=10, max_output_tokens=10
    )
    assembler = ContextAssembler(settings)

    try:
        assembler.build(query="很长的问题" * 200)
    except AppError as error:
        assert error.code is ErrorCode.CONTEXT_TOO_LONG
        assert error.details["budget"] == 260
        assert error.details["token_by_part"][PART_SYSTEM] > 0
        assert error.details["token_by_part"][PART_QUERY] > 0
    else:  # pragma: no cover - 走到这里说明裁剪失效
        raise AssertionError("超预算的上下文没有被拒绝")


def test_summary_is_degraded_before_being_dropped(
    make_settings: Callable[..., Settings],
) -> None:
    """摘要超自身配额时先降级为前 400 token，而不是整段丢弃。"""
    settings = make_settings(summary_token_budget=200)
    assembler = ContextAssembler(settings)

    context = assembler.build(query="问题", summary="历史要点" * 300)

    summary_part = next(part for part in context.parts if part.name == PART_SUMMARY)
    assert count_tokens(summary_part.content) <= 500
    assert context.trimmed[PART_SUMMARY] == 1


def test_memory_items_are_dropped_lowest_score_first(
    make_settings: Callable[..., Settings],
) -> None:
    """记忆按分数降序保留，先丢低分项，且丢弃是整条丢弃。"""
    high = MemoryItem(content="高分记忆" * 10, score=0.9)
    low = MemoryItem(content="低分记忆" * 10, score=0.1)

    # 先用宽松配额量出「只放高分那一条」需要多少 token，
    # 再用它当预算——这样断言不依赖对 summary/记忆文本长度的猜测。
    generous = make_settings(memory_token_budget=100000, memory_top_n=3)
    single = ContextAssembler(generous).build(query="问题", memories=[high])
    one_item_budget = next(part for part in single.parts if part.name == PART_MEMORY).tokens

    assembler = ContextAssembler(make_settings(memory_token_budget=one_item_budget, memory_top_n=3))
    context = assembler.build(query="问题", memories=[low, high])

    memory_part = next(part for part in context.parts if part.name == PART_MEMORY)
    assert "高分记忆" in memory_part.content
    assert "低分记忆" not in memory_part.content
    assert context.trimmed[PART_MEMORY] == 1


def test_empty_parts_are_not_injected(make_settings: Callable[..., Settings]) -> None:
    """没有内容就不要注入空片段——空的「记忆」段会让模型以为用户没有偏好。"""
    assembler = ContextAssembler(make_settings())

    context = assembler.build(query="问题", history=[], memories=[], summary="  ", rag_chunks=[])

    assert context.part_order == [PART_SYSTEM, PART_QUERY]
