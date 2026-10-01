"""记忆契约测试（``docs/07-Memory.md`` §7，``AC-MEM-01..10``）。

这一层要回答的问题是：「从 HTTP 看出去，记忆到底有没有按约定工作」。因此断言
尽量落在**可观察的对外行为**上（LLM 收到的 messages、``GET /memories``、
``GET /context`` 的 ``budget``），而不是内部状态。

三处测试设计上的取舍：

* **向量由 :class:`ScriptedEmbedding` 给定**。记忆的两个阈值（语义去重 0.92、
  注入阈值 0.45）都挂在余弦相似度上，用真实向量只能靠碰运气命中边界。脚本向量
  还让「相似但不等价」这种场景可以被**精确构造**出来。
* **``AC-MEM-02`` 按「不落上下文」理解**：``docs/03`` §3.1 规定请求未传
  ``conversation_id`` 且 ``use_memory=true`` 时由服务端新建并返回（M2 已实现并
  有契约用例），所以「空 ``conversation_id`` 不写 Redis」对应的是
  ``use_memory=false`` 的单轮问答。
* **``AC-MEM-10`` 的「Milvus 0 条」用「检索不到」来观察**：向量库条数没有对外
  接口，而「双删只删了一半」的可观察后果就是「列表空了但检索还能命中」。
  关系库/向量库两边的实际计数在
  ``tests/unit/test_memory_service.py::test_delete_all_clears_both_sides_and_marks_cooldown``
  里对账。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.support.fake_llm import FakeLLM
from tests.support.memory import (
    MemoryScriptLLM,
    ScriptedEmbedding,
    candidate,
    direction,
)

from app.application.chat import REASON_SUMMARY_FAILED, ChatService
from app.application.context import (
    DEFAULT_SYSTEM_PROMPT,
    MEMORY_LABEL,
    ContextAssembler,
)
from app.core.config import Settings
from app.core.ids import new_id
from app.memory.context_store import InMemoryConversationStore
from app.rag.base import RetrievedChunk

PREFIX = "/api/v1"
CHAT = f"{PREFIX}/chat"
MEMORIES = f"{PREFIX}/memories"
TASKS = f"{PREFIX}/tasks"

#: 脚本向量维度：必须等于 ``embedding_dim``（默认 1024）——
#: 记忆索引的维度取自配置，不一致会在 upsert 时报维度不符
DIM = 1024

#: 基准方向（与 ``tests.support.memory.BASE`` 同向，但维度对齐 ``DIM``）
BASE = direction(1.0, dim=DIM)

#: 抽取器期望的偏好（``AC-MEM-07``）
PREFERENCE = "用户偏好简洁回答，不要使用列表"

DEFAULT_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _query(text: str = "退款要几天？") -> str:
    return text


def _chat(client: TestClient, **payload: Any) -> Any:
    return client.post(CHAT, json={"query": _query(), **payload})


def _sent_text(client_llm: FakeLLM, index: int = -1) -> str:
    """把第 ``index`` 次调用发给模型的 messages 拼成一个字符串（断言用）。"""
    return "\n".join(message.content for message in client_llm.calls[index])


def _sent_token_total(client_llm: FakeLLM, index: int = -1) -> int:
    """发给模型的 messages 的 token 总量。"""
    from app.core.tokens import count_tokens

    return sum(count_tokens(message.content) for message in client_llm.calls[index])


def _wait_for(
    predicate: Callable[[], Any], *, message: str, timeout: float = DEFAULT_TIMEOUT
) -> Any:
    """轮询直到条件成立。

    固定的 ``sleep`` 在慢机器上会偶发失败；轮询 + 超时能让失败信息带上
    「最后看到的是什么」。入库/抽取都是真正的异步流水线，必须这样等。
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.02)
    raise AssertionError(f"{message}（{timeout}s 内未满足），最后状态：{last!r}")


def _list_memories(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    response = client.get(MEMORIES, params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _list_tasks(client: TestClient, type_: str) -> list[dict[str, Any]]:
    response = client.get(TASKS, params={"type": type_})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _wait_memories(client: TestClient, count: int = 1) -> list[dict[str, Any]]:
    """等到长期记忆至少 ``count`` 条（抽取是异步任务，不能假设已跑完）。"""

    def _enough() -> list[dict[str, Any]]:
        items = _list_memories(client)
        return items if len(items) >= count else []

    return _wait_for(_enough, message=f"长期记忆未达到 {count} 条")


def _context_budget(client: TestClient, conversation_id: str) -> dict[str, Any]:
    response = client.get(f"{PREFIX}/conversations/{conversation_id}/context")
    assert response.status_code == 200, response.text
    return response.json()["budget"]


class _StubRetriever:
    """返回固定片段的检索替身（``AC-MEM-06`` 需要一个确定存在的 RAG 片段）。"""

    def __init__(self, chunks: Sequence[RetrievedChunk]) -> None:
        self.chunks = list(chunks)

    async def retrieve(self, **_: Any) -> list[RetrievedChunk]:
        return list(self.chunks)


def _chunk(index: int, text: str, *, score: float = 1.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chk_{index}",
        kb_id="kb_01M3KGRC3HQ38BN4HEPYRCK73Q",
        doc_id="doc_01M3KGRC3HQ38BN4HEPYRCK73Q",
        doc_name=f"政策{index}.md",
        text=text,
        score=score,
    )


def _rebuild_chat(
    application: Any,
    settings: Settings,
    llm: FakeLLM,
    *,
    retriever: Any = None,
    with_tasks: bool = True,
) -> None:
    """按需重建 ``ChatService``（注入 stub 检索器 / 去掉任务服务）。

    ``make_memory_client`` 只覆盖 LLM 与向量化，剩下两个注入点仍要靠 ``app.state``
    替换 —— 与 ``agent_service`` / ``test_chat_degrades_when_retrieval_fails``
    的做法一致：外部依赖的注入点就是 ``app.state``，不为测试给生产装配函数加参数。
    """
    application.state.chat_service = ChatService(
        settings,
        llm=llm,
        store=application.state.conversation_store,
        retriever=retriever if retriever is not None else application.state.retriever,
        assembler=ContextAssembler(settings),
        memory=application.state.memory_service,
        tasks=application.state.task_service if with_tasks else None,
        runner=application.state.task_runner if with_tasks else None,
    )


def _headers(settings: Settings, make_token: Callable[..., str], user_id: str = "u_mem_ac") -> dict:
    return {"Authorization": f"Bearer {make_token(user_id, settings)}"}


def _contexts_of(application: Any) -> dict:
    """当前会话上下文条目（内存后端下 ``KEYS ctx:*`` 的等价物）。

    直接读实现里的字典：``ConversationStore`` 端口刻意没有「列出全部会话」的能力
    （真实 Redis 侧用的是运维命令 ``KEYS``），而 ``AC-MEM-02`` 恰恰要求断言
    「没有新增」。
    """
    store = application.state.conversation_store
    assert isinstance(store, InMemoryConversationStore), "本用例只对内存后端有意义"
    return store._items


# ---------------------------------------------------------------------------
# AC-MEM-01 / 02：短期上下文
# ---------------------------------------------------------------------------
def test_second_turn_carries_first_round(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-01``：第二轮发给模型的 messages 含第一轮的 user + assistant。"""
    chat_llm = FakeLLM(replies=["退款 7 个自然日内可申请。"])
    application, settings, _ = make_memory_client(llm=chat_llm, memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        first = _chat(client, query="退款要几天？").json()
        second = _chat(client, query="那换货呢？", conversation_id=first["conversation_id"]).json()

    assert second["conversation_id"] == first["conversation_id"]
    sent = _sent_text(chat_llm, index=1)
    assert "退款要几天？" in sent, "第一轮的 user 消息必须回灌"
    assert "退款 7 个自然日内可申请。" in sent, "第一轮的 assistant 回答必须回灌"


def test_single_turn_request_does_not_write_context(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-02``：不落上下文的单轮问答不写任何 ``ctx:*``。"""
    chat_llm = FakeLLM(replies=["好的。"])
    application, settings, _ = make_memory_client(llm=chat_llm, memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        body = _chat(client, use_memory=False).json()
        tasks = _list_tasks(client, "memory_extract")
        assert client.get(MEMORIES).status_code == 200

    assert body["conversation_id"] is None
    assert _contexts_of(application) == {}, "不落上下文时不应留下任何会话"
    assert tasks == [], "没有上下文可抽取时不应建记忆任务"


# ---------------------------------------------------------------------------
# AC-MEM-03：摘要
# ---------------------------------------------------------------------------
def test_long_conversation_triggers_summary_task(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-03``：构造 30 条历史 → 触发 ``summary_build``，完成后摘要含四段。"""
    chat_llm = FakeLLM(replies=["好的，我记下了。"])
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=MemoryScriptLLM(), summary_min_new_messages=20
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        conversation_id = None
        for index in range(15):  # 15 轮 = 30 条消息
            body = _chat(
                client,
                query=f"第 {index} 个问题：我们继续聊记忆设计。",
                conversation_id=conversation_id,
            ).json()
            conversation_id = body["conversation_id"]

        summaries = _wait_for(
            lambda: _list_tasks(client, "summary_build") or None,
            message="30 条历史仍未触发 summary_build 任务",
        )
        assert summaries[0]["resource_id"] == conversation_id

        def _summary() -> dict[str, Any] | None:
            response = client.get(f"{PREFIX}/conversations/{conversation_id}/summary")
            return response.json() if response.status_code == 200 else None

        summary = _wait_for(_summary, message="摘要任务完成后仍取不到摘要")

    assert summary["covered_until"], "covered_until 决定下一轮哪些原文不再注入，不能为空"
    assert summary["source_message_count"] > 0
    for section in ("用户目标", "已确认事实", "未决问题", "用户偏好"):
        assert f"## {section}" in summary["content"], f"缺少固定段落 {section}"


def test_summary_failure_degrades_but_keeps_chat_successful(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-04``：摘要生成失败时对话仍 200，``degraded_reasons`` 含 ``summary_failed``。

    必须走**同步兜底**路径（不配 ``TaskService``/``Runner``）才可能把失败写回本轮
    响应：异步任务的失败发生在响应返回之后，只能体现在 ``GET /tasks`` 上。
    """
    chat_llm = FakeLLM(replies=["好的。"])
    memory_llm = MemoryScriptLLM(summary_error=RuntimeError("上游挂了"))
    application, settings, _ = make_memory_client(
        llm=chat_llm,
        memory_llm=memory_llm,
        summary_min_new_messages=2,
        summary_keep_recent_turns=1,
    )
    _rebuild_chat(application, settings, chat_llm, with_tasks=False)

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        response = _chat(client)

    assert response.status_code == 200, "摘要失败绝不能把对话变成失败"
    body = response.json()
    assert body["degraded"] is True
    assert REASON_SUMMARY_FAILED in body["degraded_reasons"]
    assert memory_llm.summary_calls >= 1, "本用例要证明的正是「摘要确实试过且失败了」"


# ---------------------------------------------------------------------------
# AC-MEM-05 / 06：预算与裁剪
# ---------------------------------------------------------------------------
def test_context_budget_is_respected_and_system_query_intact(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-05``：超预算时总量不越界，且 ``system`` 与 ``query`` 完整。"""
    chat_llm = FakeLLM(replies=["好的。"])
    application, settings, _ = make_memory_client(
        llm=chat_llm,
        memory_llm=MemoryScriptLLM(),
        context_token_budget=1200,
        system_prompt_token_budget=200,
        max_output_tokens=200,
        history_token_budget=200,
        # 关掉摘要：摘要会覆盖早期原文，历史被抽走后就观察不到裁剪了
        summary_enabled=False,
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        conversation_id = None
        for index in range(3):
            body = _chat(
                client,
                query=f"第 {index} 轮：" + "很长的历史内容" * 40,
                conversation_id=conversation_id,
            ).json()
            conversation_id = body["conversation_id"]
        budget = _context_budget(client, conversation_id)
    sent = chat_llm.calls[-1]
    assert _sent_token_total(chat_llm) <= settings.context_token_budget
    assert sent[0].content == DEFAULT_SYSTEM_PROMPT, "system 提示词不允许被截断"
    assert sent[-1].content.startswith("第 2 轮："), "本轮 query 不允许被裁剪"
    assert budget["total_tokens"] <= budget["context_token_budget"]
    assert budget["trimmed"].get("history", 0) > 0, "历史超配额时必须真的被裁剪"


def test_history_is_trimmed_before_rag_chunk(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-06``：只超出约一条历史的量时，先少一条历史而不是少一条 RAG 片段。"""
    chat_llm = FakeLLM(replies=["好的。"])
    application, settings, _ = make_memory_client(
        llm=chat_llm,
        memory_llm=MemoryScriptLLM(),
        # 全局预算比「system + query + 历史」小、但比它们减去一条历史后大：
        # 于是唯一能满足预算的裁法就是丢历史，而 RAG 片段必须原封不动
        context_token_budget=600,
        system_prompt_token_budget=200,
        max_output_tokens=200,
        history_token_budget=2000,  # 单片段配额故意放宽，让**全局**那一步来裁剪
        summary_enabled=False,
    )
    chunk_text = "退款政策：自签收之日起 7 个自然日内可申请退款。"
    _rebuild_chat(
        application, settings, chat_llm, retriever=_StubRetriever([_chunk(1, chunk_text)])
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        conversation_id = None
        for index in range(2):
            body = _chat(
                client,
                query=f"第 {index} 轮：" + "需要保留的历史内容" * 30,
                conversation_id=conversation_id,
            ).json()
            conversation_id = body["conversation_id"]
        budget = _context_budget(client, conversation_id)

    assert budget["trimmed"].get("history", 0) > 0, "历史超预算时必须真的被裁剪"
    assert budget["trimmed"].get("rag", 0) == 0, "RAG 片段只有一个，任何情况下都要保住"
    assert chunk_text in _sent_text(chat_llm), "低分片段被丢说明裁剪顺序反了"


# ---------------------------------------------------------------------------
# AC-MEM-07 / 08：抽取与去重
# ---------------------------------------------------------------------------
def test_turn_triggers_memory_extraction(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-07``：对话里说出偏好 → 建 ``memory_extract`` 任务并落一条 preference。"""
    chat_llm = FakeLLM(replies=["好的，我以后回答简短些。"])
    memory_llm = MemoryScriptLLM(candidates=candidate(PREFERENCE, kind="preference"))
    application, settings, _ = make_memory_client(llm=chat_llm, memory_llm=memory_llm)

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        _chat(client, query="我喜欢简洁回答，不要用列表。")
        items = _wait_memories(client)
        tasks = _list_tasks(client, "memory_extract")

    assert [item["content"] for item in items] == [PREFERENCE]
    assert items[0]["kind"] == "preference"
    assert tasks, "每轮对话结束后都应投递抽取任务"
    assert tasks[0]["type"] == "memory_extract"
    assert memory_llm.extract_calls >= 1


def test_repeated_and_similar_memories_are_deduplicated(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-08``：同一句话两次 → 1 条 ``hit_count=2``；相似度 0.95 → 合并为 1 条。"""
    chat_llm = FakeLLM(replies=["好的。"])
    embedding = ScriptedEmbedding(
        {"用户偏好简洁回答": BASE, "用户偏好简短回答": direction(0.95, dim=DIM)}, dim=DIM
    )
    memory_llm = MemoryScriptLLM(candidates=candidate("用户偏好简洁回答", kind="preference"))
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=memory_llm, embedding=embedding, embedding_dim=DIM
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        first = _chat(client, query="我喜欢简洁回答。").json()
        _wait_memories(client)
        # 同一句话再说一遍（换一个新会话，模拟「又在别处说了同样的话」）
        _chat(client, query="再强调一次，我喜欢简洁回答。")

        def _hit_count_reached() -> bool:
            return _list_memories(client)[0]["hit_count"] == 2

        _wait_for(_hit_count_reached, message="重复内容没有累加 hit_count")
        deduped = _list_memories(client)

        # 相似度 0.95（≥ 0.92 阈值）→ 视为同一条，用较新的正文覆盖
        memory_llm.candidates = candidate("用户偏好简短回答", kind="preference")
        _chat(client, query="总之回答简短点就行。", conversation_id=first["conversation_id"])

        def _merged() -> bool:
            return _list_memories(client)[0]["content"] == "用户偏好简短回答"

        _wait_for(_merged, message="相似记忆没有被合并覆盖")

    assert len(deduped) == 1, "同一句话抽两次必须仍是 1 条"
    assert len(_list_memories(client)) == 1, "相似度 0.95 的两句必须合并成 1 条"


def test_similar_but_distinct_memories_coexist(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``REQ-MEM-005``：相似度 ∈ [0.85, 0.92) 不合并 —— 宁可冗余也不丢信息。"""
    chat_llm = FakeLLM(replies=["好的。"])
    embedding = ScriptedEmbedding(
        {"用户偏好简洁回答": BASE, "用户常驻上海": direction(0.88, dim=DIM)}, dim=DIM
    )
    memory_llm = MemoryScriptLLM(candidates=candidate("用户偏好简洁回答"))
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=memory_llm, embedding=embedding, embedding_dim=DIM
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        _chat(client, query="我喜欢简洁回答。")
        _wait_memories(client)
        memory_llm.candidates = candidate("用户常驻上海")
        _chat(client, query="我常驻上海。")
        _wait_memories(client, count=2)
        items = _list_memories(client)

    assert {item["content"] for item in items} == {"用户偏好简洁回答", "用户常驻上海"}


# ---------------------------------------------------------------------------
# AC-MEM-09 / 10：注入与清空
# ---------------------------------------------------------------------------
def test_memory_is_injected_as_separate_system_segment(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-09``：新会话提问时，messages 里出现**独立**的记忆 system 段。"""
    chat_llm = FakeLLM(replies=["好的。"])
    embedding = ScriptedEmbedding(
        {"用户偏好简洁回答": BASE, "那回答能简短点吗？": direction(0.9, dim=DIM)}, dim=DIM
    )
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=MemoryScriptLLM(), embedding=embedding, embedding_dim=DIM
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        created = client.post(MEMORIES, json={"content": "用户偏好简洁回答", "kind": "preference"})
        assert created.status_code == 201, created.text
        # 新会话（不带 conversation_id）提问
        _chat(client, query="那回答能简短点吗？")

    sent = chat_llm.calls[-1]
    assert sent[0].content == DEFAULT_SYSTEM_PROMPT, "记忆不允许混进主 system 提示词"
    memory_segments = [message.content for message in sent if MEMORY_LABEL in message.content]
    assert memory_segments, "记忆必须以独立的 system 消息注入"
    assert "用户偏好简洁回答" in memory_segments[0]
    assert "以当前对话为准" in memory_segments[0], "必须声明「冲突时以当前对话为准」"


def test_clearing_memories_removes_injection_and_requires_all_flag(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``AC-MEM-10``：``DELETE /memories?all=true`` 后列表为空且不再注入；缺参数 400。"""
    chat_llm = FakeLLM(replies=["好的。"])
    embedding = ScriptedEmbedding(
        {"用户偏好简洁回答": BASE, "那回答能简短点吗？": direction(0.9, dim=DIM)}, dim=DIM
    )
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=MemoryScriptLLM(), embedding=embedding, embedding_dim=DIM
    )

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        client.post(MEMORIES, json={"content": "用户偏好简洁回答", "kind": "preference"})
        _chat(client, query="那回答能简短点吗？")
        assert MEMORY_LABEL in _sent_text(chat_llm), "清空之前必须先证明记忆注入是生效的"

        rejected = client.delete(MEMORIES)
        assert rejected.status_code == 400, "不带 all=true 必须拒绝（防误删）"
        assert rejected.json()["error"]["code"] == "INVALID_ARGUMENT"

        cleared = client.delete(MEMORIES, params={"all": "true"})
        assert cleared.status_code == 204
        assert _list_memories(client) == []

        # 换一个会话再问同样的问题：检索不到 → messages 里不应再有记忆段
        _chat(client, query="那回答能简短点吗？")
        conversation_id = _contexts_of(application)
        assert conversation_id is not None

    assert MEMORY_LABEL not in _sent_text(chat_llm), "清空后不得再注入记忆"


def test_memory_is_tenant_scoped(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``REQ-MEM-007``：另一个用户拿不到我的长期记忆。"""
    chat_llm = FakeLLM(replies=["好的。"])
    other_llm = FakeLLM(replies=["好的。"])
    embedding = ScriptedEmbedding(
        {"用户偏好简洁回答": BASE, "那回答能简短点吗？": direction(0.9, dim=DIM)}, dim=DIM
    )
    application, settings, _ = make_memory_client(
        llm=chat_llm, memory_llm=MemoryScriptLLM(), embedding=embedding, embedding_dim=DIM
    )
    other = {"Authorization": f"Bearer {make_token('u_mem_other', settings)}"}

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        client.post(MEMORIES, json={"content": "用户偏好简洁回答", "kind": "preference"})
        # 同一个 query 换人问：检索按 user_id 隔离，不能命中别人的记忆
        _rebuild_chat(application, settings, other_llm)
        client.post(CHAT, json={"query": "那回答能简短点吗？"}, headers=other)

    assert MEMORY_LABEL not in _sent_text(other_llm)


def test_disabled_memory_is_not_read_or_written(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``docs/07`` §5.4：用户关掉记忆后既不读也不写（读写接口报 ``409``）。"""
    chat_llm = FakeLLM(replies=["好的。"])
    memory_llm = MemoryScriptLLM(candidates=candidate(PREFERENCE, kind="preference"))
    application, settings, _ = make_memory_client(llm=chat_llm, memory_llm=memory_llm)

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        client.put(f"{PREFIX}/memory-settings", json={"memory_enabled": False})
        _chat(client, query="我喜欢简洁回答，不要用列表。")
        listed = client.get(MEMORIES)
        created = client.post(MEMORIES, json={"content": "用户偏好简洁回答"})

    assert listed.status_code == 409, "关闭状态下返回空列表会与『确实没有记忆』无法区分"
    assert listed.json()["error"]["code"] == "CONFLICT"
    assert created.status_code == 409
    assert memory_llm.extract_calls == 0, "关闭后不得再调用抽取模型"


# ---------------------------------------------------------------------------
# 接口契约本身
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["preference", "fact"])
def test_memory_crud_roundtrip(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
    kind: str,
) -> None:
    """``POST`` 201 / ``GET`` 200 / ``PATCH`` 200 / ``DELETE`` 204 的状态码与字段。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        created = client.post(MEMORIES, json={"content": "用户的第 X 条稳定偏好", "kind": kind})
        assert created.status_code == 201, created.text
        mem_id = created.json()["id"]
        assert created.json()["confidence"] == 1.0  # 手动新增默认 1.0

        assert client.get(f"{MEMORIES}/{mem_id}").status_code == 200
        patched = client.patch(f"{MEMORIES}/{mem_id}", json={"content": "用户的第 Y 条稳定偏好"})
        assert patched.status_code == 200
        assert patched.json()["content"] == "用户的第 Y 条稳定偏好"
        assert patched.json()["id"] == mem_id

        assert client.get(f"{MEMORIES}/{mem_id}").json()["content"] == "用户的第 Y 条稳定偏好"
        assert client.delete(f"{MEMORIES}/{mem_id}").status_code == 204
        assert client.get(f"{MEMORIES}/{mem_id}").status_code == 404


def test_unknown_kind_filter_is_rejected(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``kind`` 过滤参数只接受枚举值，拼错要当场报错而不是返回空列表。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        response = client.get(MEMORIES, params={"kind": "prefernces"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_rebuild_endpoint_returns_task_id(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``POST /summary/rebuild`` → ``202`` + ``task_id``（``docs/07`` §6）。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())
    conversation_id = new_id("cv")

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        # 先建会话（不带归属校验的话任何登录用户都能给别人建任务）
        _chat(client, query="先聊一句。", conversation_id=conversation_id).json()
        response = client.post(f"{PREFIX}/conversations/{conversation_id}/summary/rebuild")

    assert response.status_code == 202, response.text
    assert response.json()["task_id"].startswith("task_")


def test_summary_before_generation_returns_404(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """未生成摘要 → ``404 SUMMARY_UNAVAILABLE``（而不是空字符串的 200）。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())
    conversation_id = new_id("cv")

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        _chat(client, query="先聊一句。", conversation_id=conversation_id).json()
        response = client.get(f"{PREFIX}/conversations/{conversation_id}/summary")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SUMMARY_UNAVAILABLE"


def test_clearing_context_keeps_long_term_memory(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """``DELETE /conversations/{id}/context`` 只清短期上下文（幂等）。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        created = client.post(MEMORIES, json={"content": "用户偏好简洁回答"})
        assert created.status_code == 201
        conversation_id = _chat(client, query="先聊一句。").json()["conversation_id"]

        assert client.delete(f"{PREFIX}/conversations/{conversation_id}/context").status_code == 204
        assert client.delete(f"{PREFIX}/conversations/{conversation_id}/context").status_code == 204

        context = client.get(f"{PREFIX}/conversations/{conversation_id}/context").json()

    assert context["message_count"] == 0
    assert _list_memories(client) != [], "清空上下文不得动长期记忆"


def test_other_users_conversation_is_invisible(
    make_memory_client: Callable[..., tuple[Any, Settings, FakeLLM]],
    make_token: Callable[..., str],
) -> None:
    """别人的 ``conversation_id`` 一律 ``404``（不泄露资源是否存在）。"""
    application, settings, _ = make_memory_client(memory_llm=MemoryScriptLLM())

    with TestClient(application, headers=_headers(settings, make_token)) as client:
        conversation_id = _chat(client, query="先聊一句。").json()["conversation_id"]

    other = {"Authorization": f"Bearer {make_token('u_mem_stranger', settings)}"}
    with TestClient(application, headers=other) as stranger:
        response = stranger.get(f"{PREFIX}/conversations/{conversation_id}/context")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 记忆工具（docs/04 §3）：定义必须真的出现在 /tools 上，且写护栏在 HTTP 层也生效
# ---------------------------------------------------------------------------
def test_memory_tools_are_exposed_and_declared(
    memory_client: TestClient,
) -> None:
    """``memory_save`` / ``memory_search`` 的定义、副作用与参数名以 SRS 为准。"""
    response = memory_client.get(f"{PREFIX}/tools")
    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()["items"]}

    assert {"memory_save", "memory_search"} <= set(tools)
    assert tools["memory_save"]["side_effect"] == "write"
    assert tools["memory_search"]["side_effect"] == "read"
    assert set(tools["memory_search"]["parameters"]["properties"]) == {"query", "top_k"}
    assert set(tools["memory_save"]["parameters"]["properties"]) == {
        "content",
        "kind",
        "confidence",
    }


def test_memory_search_invoke_returns_four_field_tuples(memory_client: TestClient) -> None:
    """``POST /tools/memory_search/invoke`` 的返回字段 = ``{mem_id,content,kind,score}``。

    这里用的 embedding 是配置默认的 ``hash`` 实现（非脚本替身）：它对**相同文本**
    给出完全相同的向量，所以 query 直接抄正文就能稳定命中 —— 不要在这里写一个
    「语义相近但不同字」的 query，那会把用例变成「hash 向量恰好相似」的运气题。
    """
    content = "用户偏好简洁回答"
    created = memory_client.post(MEMORIES, json={"content": content, "kind": "preference"})
    assert created.status_code == 201

    response = memory_client.post(
        f"{PREFIX}/tools/memory_search/invoke",
        json={"arguments": {"query": content, "top_k": 3}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "memory_search"
    memories = body["result"]["memories"]
    assert [set(item) for item in memories] == [{"mem_id", "content", "kind", "score"}]
    assert memories[0]["content"] == content
    assert memories[0]["kind"] == "preference"
    assert memories[0]["mem_id"] == created.json()["id"]


def test_memory_save_invoke_is_forced_dry_run(memory_client: TestClient) -> None:
    """写类工具在调试接口上强制 ``dry_run``：返回值与「没写进去」双向可观察。

    可观察信号与 M4 的 ``calculator`` 一致：``status=ok`` 但 ``result`` 为空。
    关键是后半句：**库里必须没有这条记录**。
    """
    before = len(_list_memories(memory_client))
    response = memory_client.post(
        f"{PREFIX}/tools/memory_save/invoke",
        json={"arguments": {"content": "用户偏好简洁回答", "kind": "preference"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert response.json()["result"] == {}
    assert len(_list_memories(memory_client)) == before, "调试调用不得真的写库"
