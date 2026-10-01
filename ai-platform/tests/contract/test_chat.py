"""``POST /chat`` 与 ``GET /models`` 的契约测试。

对齐的验收点：``AC-CHAT-01`` / ``AC-CHAT-02`` / ``AC-CHAT-06`` / ``AC-CHAT-07`` /
``AC-CHAT-08`` / ``AC-CHAT-12``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM

from app.application.chat import REASON_MEMORY_UNAVAILABLE, REASON_RAG_UNAVAILABLE
from app.core.config import Settings
from app.core.tokens import count_tokens
from app.llm.base import LLMMessage

CHAT = "/api/v1/chat"


def _post(client: TestClient, headers: dict[str, str], **payload: Any) -> Any:
    return client.post(CHAT, json={"query": "退款要几天？", **payload}, headers=headers)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_chat_returns_answer_references_and_usage(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-01``：answer / usage / finish_reason 齐备，且 total = prompt + completion。"""
    fake_llm.replies = ["自签收之日起 7 个自然日内可退款。"]
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post(client, chat_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "自签收之日起 7 个自然日内可退款。"
    assert body["finish_reason"] == "stop"
    assert body["model"] == "fake-flash"
    assert body["message_id"].startswith("msg_")
    assert body["conversation_id"].startswith("cv_")  # use_memory=true 时由服务端新建
    assert body["references"] == []
    assert body["tool_calls"] == []
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )
    assert body["elapsed_ms"] >= 0


def test_chat_without_memory_is_single_turn(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``use_memory=false`` 时不落上下文、也不新建会话。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        body = _post(client, chat_headers, use_memory=False).json()

    assert body["conversation_id"] is None
    messages = fake_llm.calls[0]
    assert messages[-1] == LLMMessage(role="user", content="退款要几天？")


def test_rag_is_degraded_when_retriever_unavailable(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-08``：检索不可用时对话仍成功，但必须显式降级。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post(client, chat_headers, use_rag=True)

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert REASON_RAG_UNAVAILABLE in body["degraded_reasons"]


def test_degraded_reasons_are_deduplicated_and_named_per_contract(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``degraded_reasons`` 只能是文档里的原因码，且不重复。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        body = _post(client, chat_headers).json()

    reasons = body["degraded_reasons"]
    assert len(reasons) == len(set(reasons)), "degraded_reasons 不得重复"
    assert set(reasons) <= {REASON_RAG_UNAVAILABLE, REASON_MEMORY_UNAVAILABLE}


# ---------------------------------------------------------------------------
# 参数与语义错误
# ---------------------------------------------------------------------------
def test_stream_true_on_chat_is_rejected(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-02``：/chat 只接受 stream=false，并提示正确的路径。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post(client, chat_headers, stream=True)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["details"]["hint"] == "/chat/stream"


def test_blank_query_returns_query_empty(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """空 query 用专属错误码，而不是笼统的 INVALID_ARGUMENT。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = client.post(CHAT, json={"query": "   "}, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "QUERY_EMPTY"


def test_model_not_in_whitelist_is_rejected(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """白名单外的模型名一律 400，并在 ``details.allowed`` 里回报可选模型（不是静默用默认）。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post(client, chat_headers, model="gpt-9")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert "fake-flash" in error["details"]["allowed"]


def test_use_tools_dispatches_to_agent(
    agent_client: TestClient,
    agent_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``use_tools=true`` 时 ``/chat`` 走 Agent 流程（``docs/03`` §3.1）。

    断言的是「分派确实发生」而不是「返回了什么」：只要这次请求带着工具跑过一轮，
    LLM 的入参里就必须出现 ``tools``；``use_tools=false`` 时它必须是 ``None``。
    """
    with_tools = agent_client.post(
        "/api/v1/chat",
        headers=agent_headers,
        json={"query": "现在几点", "use_rag": False, "use_memory": False, "use_tools": True},
    )
    assert with_tools.status_code == 200, with_tools.text
    offered = fake_llm.tools_seen[0]
    assert offered is not None
    assert "calculator" in [item["function"]["name"] for item in offered]

    without_tools = agent_client.post(
        "/api/v1/chat",
        headers=agent_headers,
        json={"query": "现在几点", "use_rag": False, "use_memory": False, "use_tools": False},
    )
    assert without_tools.status_code == 200, without_tools.text
    assert fake_llm.tools_seen[1] is None


def test_malformed_conversation_id_is_rejected(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``conversation_id`` 格式非法 ⇒ 400，且 ``details.fields`` 指出是哪个字段出错。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = _post(client, chat_headers, conversation_id="cv_bad")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    field = error["details"]["fields"][0]
    assert "conversation_id" in str(field["loc"])


def test_other_users_conversation_is_not_found(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    make_token: Callable[..., str],
    settings: Settings,
    fake_llm: FakeLLM,
) -> None:
    """跨用户访问返回 404（而不是 403）——403 等于确认「这个会话存在」。"""
    application, _ = make_chat_app(llm=fake_llm)
    other_headers = {"Authorization": f"Bearer {make_token('u_other', settings)}"}

    with TestClient(application) as client:
        created = _post(client, chat_headers).json()
        response = _post(client, other_headers, conversation_id=created["conversation_id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_unauthenticated_request_is_rejected(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    fake_llm: FakeLLM,
) -> None:
    """不带 ``Authorization`` 访问 ``/chat`` ⇒ 401 ``UNAUTHENTICATED``（认证先于参数校验）。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = client.post(CHAT, json={"query": "hi"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_upstream_failure_is_reported_as_502(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """上游 5xx 必须报 502（可重试），而不是 500（会算成我们的 bug）。"""
    fake_llm.complete_error = openai.APIStatusError(
        "upstream exploded", response=_response(503), body=None
    )
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application, raise_server_exceptions=False) as client:
        response = _post(client, chat_headers)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_LLM_ERROR"
    assert error["retryable"] is True
    assert error["trace_id"]


# ---------------------------------------------------------------------------
# 上下文装配（``AC-CHAT-06`` / ``AC-CHAT-07`` / ``AC-CHAT-12``）
# ---------------------------------------------------------------------------
def test_messages_order_is_system_then_history_then_query(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-06``：用替身捕获的 messages 断言片段顺序。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        _post(
            client,
            chat_headers,
            use_memory=False,
            history=[
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
            ],
        )

    messages = fake_llm.calls[0]
    assert messages[0].role == "system"
    assert [message.content for message in messages[1:3]] == ["第一问", "第一答"]
    assert messages[-1] == LLMMessage(role="user", content="退款要几天？")


def test_memory_disabled_uses_only_request_history(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-12``：关闭记忆后 prompt 里不得出现 memory/summary 片段。

    先在同一进程里跑一轮「带记忆」的对话制造上下文，再关掉记忆重问：
    如果实现里偷偷读了存储，这里会立刻看到上一轮的痕迹。
    """
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        first = _post(client, chat_headers, query="记住：我的订单号是 A123").json()
        second = _post(
            client,
            chat_headers,
            query="我的订单号是多少？",
            use_memory=False,
            conversation_id=first["conversation_id"],
            history=[{"role": "user", "content": "只带这一条"}],
        ).json()

    messages = fake_llm.calls[1]
    joined = "\n".join(message.content for message in messages)
    assert "A123" not in joined
    assert [message.content for message in messages[1:-1]] == ["只带这一条"]
    assert second["conversation_id"] == first["conversation_id"]


def test_history_is_trimmed_to_fit_prompt_budget(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``AC-CHAT-07``：12k token 的历史必须被裁到 ``context_token_budget`` 以内。"""
    application, settings = make_chat_app(llm=fake_llm, context_token_budget=8192)
    filler = "这是一段用来把上下文撑到超预算的中文文本。" * 40
    history = [{"role": "user", "content": filler} for _ in range(30)]
    assert sum(count_tokens(item["content"]) for item in history) > settings.context_token_budget

    with TestClient(application) as client:
        response = _post(client, chat_headers, use_memory=False, history=history)

    assert response.status_code == 200
    sent = sum(count_tokens(message.content) for message in fake_llm.calls[0])
    assert sent <= settings.context_token_budget


# ---------------------------------------------------------------------------
# 会话累积
# ---------------------------------------------------------------------------
def test_second_turn_sees_previous_exchange(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """同一会话的第二轮必须带上上一轮的 user/assistant 原文。"""
    fake_llm.replies = ["第一答", "第二答"]
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        first = _post(client, chat_headers, query="第一问").json()
        second = _post(
            client, chat_headers, query="第二问", conversation_id=first["conversation_id"]
        )

    assert second.status_code == 200
    assert second.json()["conversation_id"] == first["conversation_id"]
    earlier = [message.content for message in fake_llm.calls[1]]
    assert "第一问" in earlier and "第一答" in earlier
    assert earlier[-1] == "第二问"


# ---------------------------------------------------------------------------
# GET /models
# ---------------------------------------------------------------------------
def test_models_lists_whitelist_with_default_flag(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    chat_headers: dict[str, str],
    fake_llm: FakeLLM,
) -> None:
    """``GET /models`` 按白名单顺序列出模型，且**恰好一个** ``is_default=true``。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = client.get("/api/v1/models", headers=chat_headers)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["name"] for item in items] == ["fake-flash", "fake-pro"]
    assert [item["is_default"] for item in items] == [True, False]
    assert items[0]["supports_stream"] is True
    assert items[0]["context_window"] == 65536


def test_models_requires_auth(
    make_chat_app: Callable[..., tuple[FastAPI, Settings]],
    fake_llm: FakeLLM,
) -> None:
    """``GET /models`` 同样要鉴权：模型清单不外泄（401 而非 200）。"""
    application, _ = make_chat_app(llm=fake_llm)

    with TestClient(application) as client:
        response = client.get("/api/v1/models")

    assert response.status_code == 401


def _response(status: int) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
    )


@pytest.fixture(autouse=True)
def _no_stray_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保用例不受开发机环境变量影响。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
