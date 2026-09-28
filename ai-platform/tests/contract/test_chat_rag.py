"""对话 × 检索的契约测试（``AC-RAG-13`` / ``AC-RAG-14`` / ``REQ-RAG-006``）。

RAG 的降级语义写在对话契约里（``REQ-CHAT-007``）：检索挂了要**降级而不是失败**。
所以这一层的用例重点不在「检索准不准」，而在
「注入的上下文对不对、引用编号对不对、坏掉的时候对话还活不活」。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM
from tests.support.rag import create_kb, ingest_text

from app.services.chat import prune_out_of_range_citations

PREFIX = "/api/v1"
CHAT = f"{PREFIX}/chat"
STREAM = f"{PREFIX}/chat/stream"

MARKDOWN = """# 售后政策

## 退款

自签收之日起 7 个自然日内可申请退款。
审核通过后 3 个工作日内原路退回。

## 换货

换货需提供质量问题凭证。
"""


def _post(client: TestClient, **payload: Any) -> Any:
    return client.post(CHAT, json={"query": "退款要几天？", **payload})


def test_chat_injects_retrieved_context(rag_chat_client: TestClient, fake_llm: FakeLLM) -> None:
    """入库后对话会把检索到的片段注入 prompt（``REQ-RAG-009`` 的前提）。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["退款时效以资料为准 [1]。"]

    body = _post(rag_chat_client, kb_ids=[kb["id"]]).json()

    assert body["references"], "带 kb_ids 的对话应当拿到引用"
    sent = "\n".join(message.content for message in fake_llm.calls[-1])
    assert "7 个自然日" in sent, "检索到的正文必须真的进了 prompt"
    assert "政策.md" in sent, "文档名要一起给模型，否则引用只能编"


def test_references_match_answer_citations(rag_chat_client: TestClient, fake_llm: FakeLLM) -> None:
    """``[1]``/``[2]`` 有对应引用，且 ``snippet`` 是 chunk 内容的**前缀**（``AC-RAG-13``）。"""
    kb = create_kb(rag_chat_client, chunk_size=128, chunk_overlap=16)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN * 3, doc_name="政策.md")
    fake_llm.replies = ["退款 7 个自然日内可申请 [1]，退回需 3 个工作日 [2]。"]

    body = _post(rag_chat_client, kb_ids=[kb["id"]]).json()

    references = body["references"]
    assert len(references) >= 2, "回答里引了 [1][2]，references 至少要有两条"
    assert [item["index"] for item in references] == list(range(1, len(references) + 1))
    for item in references:
        assert item["content_sha256"], "缺少内容哈希，前端无法做同片段去重"
        assert len(item["snippet"]) <= 200
        assert item["score"] >= 0
    # 引用的 snippet 必须能在注入的上下文里找到（否则引用是凭空生成的）
    sent = "\n".join(message.content for message in fake_llm.calls[-1])
    assert references[0]["snippet"][:40] in sent


def test_out_of_range_citation_is_removed(
    rag_chat_client: TestClient, fake_llm: FakeLLM, caplog: pytest.LogCaptureFixture
) -> None:
    """模型编出 ``[9]`` → 标注被剔除且产生 warning（``AC-RAG-14``）。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["根据资料 [1]，第 9 条另有规定 [9]。"]

    with caplog.at_level(logging.WARNING, logger="app.chat"):
        body = _post(rag_chat_client, kb_ids=[kb["id"]]).json()

    assert "[9]" not in body["answer"]
    assert "[1]" in body["answer"], "合法标注不能被误删"
    assert any("citation_out_of_range" in record.message for record in caplog.records)


def test_chat_without_kb_ids_searches_all_user_kbs(
    rag_chat_client: TestClient, fake_llm: FakeLLM
) -> None:
    """``kb_ids`` 为空且 ``use_rag=true`` → 检索该用户**全部** KB（``docs/03`` §3.1）。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["我不知道。"]

    body = _post(rag_chat_client).json()

    assert body["references"], "不传 kb_ids 不等于不检索，而是全库检索"
    assert all(item["kb_id"] == kb["id"] for item in body["references"])


def test_retrieval_is_tenant_scoped(
    rag_chat_client: TestClient, other_user_headers: dict[str, str], fake_llm: FakeLLM
) -> None:
    """另一个用户做全库检索时看不到我的文档（``REQ-RAG-011`` 的检索侧）。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["我不知道。"]

    body = rag_chat_client.post(
        CHAT, json={"query": "退款要几天？"}, headers=other_user_headers
    ).json()

    assert body["references"] == []
    sent = "\n".join(message.content for message in fake_llm.calls[-1])
    assert "7 个自然日" not in sent


def test_use_rag_false_skips_retrieval(rag_chat_client: TestClient, fake_llm: FakeLLM) -> None:
    """``use_rag=false`` → 即使给了 ``kb_ids`` 也不注入上下文。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["好的。"]

    body = _post(rag_chat_client, kb_ids=[kb["id"]], use_rag=False).json()

    assert body["references"] == []
    sent = "\n".join(message.content for message in fake_llm.calls[-1])
    assert "7 个自然日" not in sent


def test_chat_degrades_when_retrieval_fails(
    rag_app: tuple[Any, Any], fake_llm: FakeLLM, make_token: Any
) -> None:
    """检索不可用 → 对话仍成功，``degraded=true`` + ``rag_unavailable``（``REQ-RAG-006``）。"""
    from fastapi.testclient import TestClient as _Client

    from app.memory.context_store import InMemoryConversationStore
    from app.rag.base import NullRetriever
    from app.services.chat import REASON_RAG_UNAVAILABLE, ChatService
    from app.services.context import ContextAssembler

    application, settings = rag_app
    service = ChatService(
        settings,
        llm=fake_llm,
        store=InMemoryConversationStore(settings),
        retriever=NullRetriever(),  # 明确「检索不可用」
        assembler=ContextAssembler(settings),
    )
    application.state.chat_service = service
    application.state.llm = service.llm
    headers = {"Authorization": f"Bearer {make_token('u_rag_down', settings)}"}
    fake_llm.replies = ["没有资料也能回答。"]

    with _Client(application, headers=headers) as client:
        response = client.post(
            CHAT,
            json={"query": "退款要几天？", "kb_ids": ["kb_01M3KGRC3HQ38BN4HEPYRCK73Q"]},
        )

    assert response.status_code == 200, "检索坏掉不能让对话失败"
    body = response.json()
    assert body["degraded"] is True
    assert REASON_RAG_UNAVAILABLE in body["degraded_reasons"]
    assert body["references"] == []


def test_stream_sends_reference_before_tokens(
    rag_chat_client: TestClient, fake_llm: FakeLLM
) -> None:
    """SSE 的 ``reference`` 帧必须在首个 ``token`` 之前（``REQ-CHAT-003``）。"""
    kb = create_kb(rag_chat_client)
    ingest_text(rag_chat_client, kb["id"], MARKDOWN, doc_name="政策.md")
    fake_llm.replies = ["退款时效见资料 [1]。"]

    with rag_chat_client.stream("POST", STREAM, json={"query": "退款", "kb_ids": [kb["id"]]}) as r:
        frames = []
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                frames.append(event)
                event = None

    assert "meta" in frames
    assert "reference" in frames
    assert frames.index("reference") < frames.index("token"), frames
    assert frames[-1] == "done"


def test_stream_degrades_when_retrieval_fails(
    rag_app: tuple[Any, Any], fake_llm: FakeLLM, make_token: Any
) -> None:
    """流式路径同样降级：``meta.degraded=true``，且不推 ``reference`` 帧。"""
    from fastapi.testclient import TestClient as _Client

    from app.memory.context_store import InMemoryConversationStore
    from app.rag.base import NullRetriever
    from app.services.chat import ChatService
    from app.services.context import ContextAssembler

    application, settings = rag_app
    service = ChatService(
        settings,
        llm=fake_llm,
        store=InMemoryConversationStore(settings),
        retriever=NullRetriever(),
        assembler=ContextAssembler(settings),
    )
    application.state.chat_service = service
    application.state.llm = service.llm
    headers = {"Authorization": f"Bearer {make_token('u_rag_down_stream', settings)}"}
    fake_llm.replies = ["照常回答。"]

    with (
        _Client(application, headers=headers) as client,
        client.stream(
            "POST",
            STREAM,
            json={"query": "退款", "kb_ids": ["kb_01M3KGRC3HQ38BN4HEPYRCK73Q"]},
        ) as response,
    ):
        body = response.read().decode()

    assert "event: meta" in body
    assert '"degraded":true' in body
    assert "event: reference" not in body
    assert "event: done" in body


# ---------------------------------------------------------------------------
# 越界标注剔除（纯函数，边界条件逐个钉）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("资料说 7 天 [1]。", "资料说 7 天 [1]。"),  # 合法标注原样保留
        ("先看 [9] 再看 [1]。", "先看  再看 [1]。"),  # 越界的删掉
        ("两个都不存在 [7][8]。", "两个都不存在 。"),  # 全越界 → 只留正文
        ("引用多条的写法 [1,9]", "引用多条的写法 [1]"),  # 多引用里逐个判断
        ("这里没有引用", "这里没有引用"),  # 不含引用时不动
        ("[0]", "[0]"),  # [0] 不是有效序号，按原样保留（不猜测模型意图）
        ("数组下标 a[10] 不该被当成引用", "数组下标 a 不该被当成引用"),
    ],
)
def test_prune_citations(text: str, expected: str) -> None:
    """剔除规则要精确到「只动引用标注」，不能牵连正文。"""
    assert prune_out_of_range_citations(text, max_index=2) == expected


def test_prune_citations_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """剔除了就要留痕：否则「模型在编引用」这件事永远没人知道。"""
    with caplog.at_level(logging.WARNING, logger="app.chat"):
        prune_out_of_range_citations("见 [5]", max_index=1)

    assert caplog.records
    assert "citation_out_of_range_removed" in caplog.records[0].message
