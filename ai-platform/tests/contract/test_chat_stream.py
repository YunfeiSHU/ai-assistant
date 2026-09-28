"""``POST /chat/stream`` 的 SSE 契约测试。

对齐的验收点：``AC-CHAT-03``（事件序列）/ ``AC-CHAT-04``（头部）/ ``AC-CHAT-09``
（断连停上游）/ ``AC-CHAT-10``（心跳）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from contextlib import aclosing
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM

from app.config import Settings
from app.core.sse import frame_stream
from app.memory.context_store import InMemoryConversationStore
from app.rag.base import RetrievedChunk
from app.schemas.chat import ChatRequest
from app.services.chat import ChatService
from app.services.context import ContextAssembler

STREAM = "/api/v1/chat/stream"


class _StubRetriever:
    """返回固定片段的检索替身。"""

    def __init__(self, chunks: Sequence[RetrievedChunk]) -> None:
        self._chunks = list(chunks)

    async def retrieve(self, **_: Any) -> list[RetrievedChunk]:
        return list(self._chunks)


def _events(body: str) -> list[tuple[str, str]]:
    """把 SSE 文本解析成 ``(event, data)`` 列表。"""
    parsed: list[tuple[str, str]] = []
    for block in body.strip().split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if name:
            parsed.append((name, data))
    return parsed


def _post_stream(client: TestClient, headers: dict[str, str], **payload: Any) -> Any:
    return client.post(STREAM, json={"query": "你好", **payload}, headers=headers)


# ---------------------------------------------------------------------------
# 事件序列与响应头
# ---------------------------------------------------------------------------
def test_event_sequence_is_meta_first_and_done_last(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-03``：meta 必须是第一帧、done 必须是最后一帧。"""
    fake_llm.replies = ["一二三四五六"]
    fake_llm.chunks = 3
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post_stream(client, chat_headers)

    assert response.status_code == 200
    names = [name for name, _ in _events(response.text)]
    assert names[0] == "meta"
    assert names[-1] == "done"
    assert names.count("token") >= 2  # 「逐段」而不是一次性
    assert names.index("usage") < names.index("done")
    deltas = "".join(
        json.loads(data)["delta"] for name, data in _events(response.text) if name == "token"
    )
    assert deltas == "一二三四五六"


def test_stream_headers_disable_buffering_and_compression(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-04``：SSE 头部必须禁缓存/禁转换，且没有 gzip。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post_stream(client, chat_headers)

    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in response.headers


def test_meta_carries_conversation_and_message_ids(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post_stream(client, chat_headers)

    meta = json.loads(dict(_events(response.text))["meta"])
    assert meta["conversation_id"].startswith("cv_")
    assert meta["message_id"].startswith("msg_")
    assert meta["model"] == "fake-flash"
    assert meta["created_at"].endswith("Z")


def test_reference_frames_arrive_before_first_token(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-03`` / ``REQ-CHAT-003``：引用必须先于首个 token，且序号从 1 开始。"""
    retriever = _StubRetriever(
        [
            RetrievedChunk(
                chunk_id="chk_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
                text="自签收之日起 7 个自然日内可无理由退款。",
                doc_id="doc_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
                kb_id="kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
                doc_name="售后政策 v3.pdf",
                page=4,
                score=0.8123,
            )
        ]
    )
    application, _ = make_chat_app(llm=fake_llm, retriever=retriever)

    with TestClient(application) as client:
        response = _post_stream(client, chat_headers, use_rag=True)

    parsed = _events(response.text)
    names = [name for name, _ in parsed]
    assert names.index("reference") < names.index("token")
    references = json.loads(dict(parsed)["reference"])["references"]
    assert references[0]["index"] == 1
    assert references[0]["doc_name"] == "售后政策 v3.pdf"
    assert references[0]["page"] == 4
    assert references[0]["snippet"].startswith("自签收之日起")


def test_stream_error_after_tokens_becomes_error_frame(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """已经吐过 token 之后失败：只能发 ``error`` 帧（响应头早就发出去了）。"""
    fake_llm.replies = ["一二三四五六"]
    fake_llm.chunks = 3
    fake_llm.stream_error_after = 0
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post_stream(client, chat_headers)

    assert response.status_code == 200
    parsed = _events(response.text)
    names = [name for name, _ in parsed]
    assert "error" in names
    assert "done" not in names
    assert json.loads(dict(parsed)["error"])["code"] == "UPSTREAM_LLM_ERROR"


def test_preparation_errors_still_return_http_status(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """准备阶段失败必须还是 HTTP 错误（``400 QUERY_EMPTY``），而不是 200 + error 帧。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = client.post(STREAM, json={"query": "  "}, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "QUERY_EMPTY"


# ---------------------------------------------------------------------------
# 断连与心跳（服务层直接驱动，避免依赖 TestClient 的缓冲行为）
# ---------------------------------------------------------------------------
def test_disconnect_stops_upstream_and_persists_partial(
    make_settings: Callable[..., Settings],
) -> None:
    """``AC-CHAT-09``：客户端断开后上游被停，且半成品以 ``partial=True`` 落库。"""
    settings = make_settings()
    fake_llm = FakeLLM(replies=["一二三四五六七八九十"], chunks=10, delay=0.01)
    store = InMemoryConversationStore(settings)
    service = ChatService(
        settings,
        llm=fake_llm,
        store=store,
        retriever=_StubRetriever([]),
        assembler=ContextAssembler(settings),
    )

    async def scenario() -> tuple[int, list[Any]]:
        request = ChatRequest(query="你好")
        prepared = await service.prepare(request, "u_dc")
        frames = 0
        # 用 ``aclosing`` 确保「断开」是确定性的关闭动作，而不是等 GC
        async with aclosing(frame_stream(service.stream_prepared(prepared, "u_dc"))) as stream:
            async for _frame in stream:
                frames += 1
                if frames >= 3:  # 收到 meta + 2 个 token 后「断开」
                    break
        await asyncio.sleep(0.05)  # 让后台落库任务跑完
        assert prepared.conversation_id is not None
        stored = await store.recent(prepared.conversation_id, "u_dc", turns=5)
        return frames, stored

    frames, stored = asyncio.run(scenario())

    assert frames == 3
    assert fake_llm.aborts == 1, "上游 LLM 调用必须被停止"
    assert stored and stored[-1].partial is True, "半成品必须以 partial=True 落库"


def test_ping_is_emitted_while_upstream_is_quiet(
    make_settings: Callable[..., Settings],
) -> None:
    """``AC-CHAT-10``：上游长时间不出 token 时必须能收到 ping，且流不被杀死。"""
    settings = make_settings()
    # 上游 0.2s 才吐第一个 token，心跳间隔 0.05s：
    # 两者差距足够大，不会因 Windows 上 ~15ms 的定时器粒度变成刮跑局。
    fake_llm = FakeLLM(replies=["迟到"], chunks=1, delay=0.2)
    service = ChatService(
        settings,
        llm=fake_llm,
        store=InMemoryConversationStore(settings),
        retriever=_StubRetriever([]),
        assembler=ContextAssembler(settings),
    )

    async def scenario() -> list[str]:
        prepared = await service.prepare(ChatRequest(query="你好"), "u_ping")
        names: list[str] = []
        async for frame in frame_stream(
            service.stream_prepared(prepared, "u_ping"), ping_interval=0.05
        ):
            names.append(frame.split(b"\n", 1)[0].decode())
        return names

    names = asyncio.run(scenario())

    assert "event: ping" in names
    assert names[0] == "event: meta"
    assert names[-1] == "event: done"
