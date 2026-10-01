"""对话请求/响应模型的校验测试。

重点是「哪些错误由 schema 拦、哪些由 service 拦」的分工：
schema 只做**语法级**校验，语义级（空 query、stream=true）由 service 抛专属错误码 ——
否则客户端只会收到笼统的 ``INVALID_ARGUMENT``，无法区分该改哪个字段。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest, Reference, Usage

VALID_CV = "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"
VALID_KB = "kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"


def test_minimal_request_defaults_match_contract() -> None:
    """默认值属于契约（``docs/03`` §3.1），改动会直接影响前端行为。"""
    request = ChatRequest(query="你好")

    assert request.conversation_id is None
    assert request.use_rag is True
    assert request.use_memory is True
    assert request.use_tools is False
    assert request.stream is False
    assert request.history == []
    assert request.kb_ids == []
    assert request.metadata == {}


def test_blank_query_passes_schema_so_service_can_return_query_empty() -> None:
    """全空白 query 必须能过 schema —— 否则只会得到笼统的 ``INVALID_ARGUMENT``。"""
    assert ChatRequest(query="   ").query == "   "


def test_query_length_is_capped() -> None:
    """``query`` 上限 8000 字符，超出必须在 schema 层被拒（``docs/03`` §3.1）。"""
    with pytest.raises(ValidationError):
        ChatRequest(query="x" * 8001)


def test_conversation_id_must_match_ulid_pattern() -> None:
    """``conversation_id`` 必须匹配 ``cv_`` + ULID；前缀写错或 ULID 尾字符非法都要拒。"""
    assert ChatRequest(query="q", conversation_id=VALID_CV).conversation_id == VALID_CV
    for bad in ("cv_short", "kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C", "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3I"):
        with pytest.raises(ValidationError):
            ChatRequest(query="q", conversation_id=bad)


def test_kb_ids_must_be_valid_and_bounded() -> None:
    """``kb_ids`` 每项须是 ``kb_`` + ULID，且最多 10 个（``docs/03`` §3.1）。"""
    assert ChatRequest(query="q", kb_ids=[VALID_KB]).kb_ids == [VALID_KB]
    with pytest.raises(ValidationError):
        ChatRequest(query="q", kb_ids=["not-a-kb-id"])
    with pytest.raises(ValidationError):
        ChatRequest(query="q", kb_ids=[VALID_KB] * 11)


def test_rerank_top_n_cannot_exceed_top_k() -> None:
    """重排保留数不得超过召回数 —— 否则等于凭空多出候选，必须被交叉校验拦下。"""
    with pytest.raises(ValidationError):
        ChatRequest(query="q", top_k=3, rerank_top_n=5)
    assert ChatRequest(query="q", top_k=10, rerank_top_n=5).rerank_top_n == 5


def test_metadata_key_value_length_is_bounded() -> None:
    """``metadata`` 的键与值都不得超过 64 字符（埋点透传字段有存储成本上限）。"""
    with pytest.raises(ValidationError):
        ChatRequest(query="q", metadata={"k": "v" * 65})
    with pytest.raises(ValidationError):
        ChatRequest(query="q", metadata={"k" * 65: "v"})


def test_history_is_bounded_and_typed() -> None:
    """``history`` 最多 200 条，且 ``role`` 只接受文档定义的四类角色。"""
    with pytest.raises(ValidationError):
        ChatRequest(query="q", history=[{"role": "user", "content": "x"}] * 201)
    with pytest.raises(ValidationError):
        ChatRequest(query="q", history=[{"role": "robot", "content": "x"}])


def test_usage_total_is_backfilled_from_parts() -> None:
    """上游只给分项时也要保证 ``total = prompt + completion``（``AC-CHAT-01``）。"""
    usage = Usage(prompt_tokens=812, completion_tokens=64)

    assert usage.total_tokens == 876


def test_usage_total_is_not_overwritten_when_provided() -> None:
    """上游已经给出 ``total_tokens`` 时不得被分项之和覆盖（上游口径优先）。"""
    assert Usage(prompt_tokens=1, completion_tokens=1, total_tokens=99).total_tokens == 99


def test_reference_index_must_start_at_one() -> None:
    """引用序号从 1 起（正文角标 ``[n]`` 与之一一对应），``index=0`` 必须拒。"""
    with pytest.raises(ValidationError):
        Reference(index=0, chunk_id="chk_1", doc_id="doc_1", kb_id=VALID_KB)
