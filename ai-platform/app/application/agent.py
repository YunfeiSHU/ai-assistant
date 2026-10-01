"""Agent 用例编排（``docs/04`` §1 / §4.3）。

与 :class:`app.application.chat.ChatService` 的分工：``ChatService`` 负责「准备上下文 +
一次性/流式生成」，``use_tools=false`` 时 RAG 是前置固定步骤；``AgentService`` 负责
「准备上下文 + 循环调用工具」，RAG 由模型通过 ``kb_retrieve`` 自主决定。

两者共用 ``ContextAssembler`` / ``ConversationStore`` / 引用编号规则，所以 ``/chat`` 与
``/agent/run`` 的引用编号方式完全一致。

刻意不做的一件事：不在 prepare 阶段做事前检索。事前检索 + 把 ``kb_retrieve`` 也放进工具
列表会让同一次提问检索两次，引用还会重复编号（``docs/04`` §1.2）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from app.agent.loop import TOOL_RESULT_NOTE, AgentLoop, AgentResult
from app.application.chat import (
    REASON_MEMORY_UNAVAILABLE,
    ChatStreamEvent,
    _append_reason,
    _finish_reason,
    _references,
)
from app.application.context import AssembledContext, ContextAssembler
from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.core.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_PING,
    EVENT_REFERENCE,
    EVENT_TOKEN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_USAGE,
)
from app.llm.base import LLMClient, LLMMessage, map_llm_exception
from app.memory.context_store import ConversationStore, StoredMessage, now_iso
from app.rag.base import RetrievedChunk
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.chat import (
    StreamDone,
    StreamMeta,
    StreamReferences,
    StreamToken,
    StreamUsage,
    ToolCallTrace,
    Usage,
)
from app.tools.base import ToolContext
from app.tools.executor import ToolCallRecord
from app.tools.registry import ToolRegistry

logger = logging.getLogger("app.agent")

#: ``use_rag=true`` 时检索能力的唯一入口（``docs/04`` §1.2）
RAG_TOOL_NAME = "kb_retrieve"


@dataclass(slots=True)
class PreparedAgent:
    """一次 Agent 运行的准备结果（路由层先 prepare 再推流，同 ``PreparedChat``）。"""

    query: str
    model: str
    temperature: float | None
    conversation_id: str | None
    message_id: str
    context: AssembledContext
    tool_names: list[str]
    max_steps: int
    degraded_reasons: list[str] = field(default_factory=list)
    persist: bool = True


class AgentService:
    """Agent 用例编排。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        store: ConversationStore,
        loop: AgentLoop,
        registry: ToolRegistry,
        *,
        assembler: ContextAssembler | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._store = store
        self._loop = loop
        self._registry = registry
        self._assembler = assembler or ContextAssembler(settings)

    # ------------------------------------------------------------------
    # 准备阶段
    # ------------------------------------------------------------------
    async def prepare(self, request: AgentRunRequest, user_id: str) -> PreparedAgent:
        """解析工具范围、组装消息；这里抛的错误都是 ``prepare`` 阶段的 HTTP 错误。"""
        query = request.query.strip()
        if not query:
            raise AppError(ErrorCode.QUERY_EMPTY, "提问内容为空")

        model = self._llm.resolve_model(request.model)
        degraded: list[str] = []
        tool_names = self._resolve_tools(request)

        conversation_id: str | None = request.conversation_id
        history: list[LLMMessage] = []
        if request.use_memory:
            if conversation_id is None:
                conversation_id = new_id("cv")
            await self._store.ensure(conversation_id, user_id)
            try:
                stored = await self._store.recent(
                    conversation_id, user_id, turns=self._settings.memory_recent_turns
                )
                history = [LLMMessage(role=item.role, content=item.content) for item in stored]
            except AppError:
                raise
            except Exception as exc:
                logger.warning("memory.read_failed", extra={"error": str(exc)})
                _append_reason(degraded, REASON_MEMORY_UNAVAILABLE)
        else:
            history = [LLMMessage(role=m.role, content=m.content) for m in request.history]

        # 不做事前检索（模块 docstring）；工具结果会在循环里进入上下文
        context = self._assembler.build(
            query=query,
            history=history,
            memories=[],
            summary=None,
            rag_chunks=[],
            extra_system=TOOL_RESULT_NOTE,
        )
        return PreparedAgent(
            query=query,
            model=model,
            temperature=request.temperature,
            conversation_id=conversation_id,
            message_id=new_id("msg"),
            context=context,
            tool_names=tool_names,
            max_steps=self._resolve_max_steps(request.max_steps),
            degraded_reasons=degraded,
            persist=request.use_memory,
        )

    def _resolve_tools(self, request: AgentRunRequest) -> list[str]:
        """计算本次开放的工具集合。

        ``docs/04`` §1.2：``use_rag=true`` 时不做事前检索，检索能力完全交给模型通过
        ``kb_retrieve`` 工具触发。这里与文档字面表述的唯一差异是「只提供 ``kb_retrieve``」
        被理解为「RAG 只通过 ``kb_retrieve`` 提供」而非「禁用 calculator 等其它工具」——
        否则 ``use_rag`` 默认为 true 会让 ``AC-AGENT-01`` 在默认请求下不可能通过。
        """
        self._assert_known(request.allowed_tools, request.denied_tools)
        names = self._registry.filter_names(
            allowed=request.allowed_tools,
            denied=request.denied_tools,
            denylist=self._settings.tool_denylist,
        )
        if not names:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                "可用工具集合为空，无法执行 Agent 请求",
                {"denied_tools": request.denied_tools, "hint": "放宽 allowed_tools/denied_tools"},
            )
        return names

    def _assert_known(self, allowed: Sequence[str] | None, denied: Sequence[str]) -> None:
        """白/黑名单里出现未注册工具名即 ``400``。静默忽略是最坏的选择：调用方以为限制生效了，实际没有。"""
        known = set(self._registry.names())
        requested = set(allowed) if allowed is not None else set()
        unknown = sorted((requested | set(denied)) - known)
        if unknown:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                f"未知工具名：{', '.join(unknown)}",
                {"known": sorted(known)},
            )

    def _resolve_max_steps(self, requested: int) -> int:
        """请求可调小上限，但不能超过配置（配置是运维的最后一道闸门）。"""
        return max(1, min(requested, max(1, self._settings.agent_max_steps)))

    def _tool_context(self, prepared: PreparedAgent, user_id: str) -> ToolContext:
        return ToolContext(
            user_id=user_id,
            conversation_id=prepared.conversation_id,
            allowed=frozenset(prepared.tool_names),
        )

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------
    async def run(self, request: AgentRunRequest, user_id: str) -> AgentRunResponse:
        """执行一轮 Agent 编排并返回最终结果（``POST /agent/run`` 的入口，非流式）。

        与 :meth:`stream` 共用 :meth:`prepare` 与同一条循环；这里等整轮结束一次性返回
        （含 ``steps`` 与用量），因此不适合需要「边生成边下发」的场景。
        """
        started = time.perf_counter()
        prepared = await self.prepare(request, user_id)
        try:
            result = await self._loop.run(
                prepared.context.messages,
                self._tool_context(prepared, user_id),
                model=prepared.model,
                temperature=prepared.temperature,
                max_steps=prepared.max_steps,
                tool_names=prepared.tool_names,
            )
        except AppError:
            raise
        except Exception as exc:  # LLM 之外的意外 → 502 而不是 500
            raise map_llm_exception(exc) from exc

        degraded = _merge_reasons(prepared.degraded_reasons, result.degraded_reasons)
        references = _references(result.citations)
        answer = result.content
        if not answer:
            logger.warning(
                "agent.empty_answer", extra={"model": result.model, "steps": result.steps}
            )

        if prepared.persist and prepared.conversation_id:
            await self._persist(prepared, user_id, result)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "agent.completed",
            extra={
                "model": result.model,
                "steps": result.steps,
                "tool_calls": len(result.calls),
                "failed_calls": result.failed_calls,
                "elapsed_ms": elapsed_ms,
                "degraded": bool(degraded),
            },
        )
        return AgentRunResponse(
            answer=answer,
            conversation_id=prepared.conversation_id,
            message_id=prepared.message_id,
            references=references,
            tool_calls=[_trace(record) for record in result.calls],
            steps=result.steps,
            usage=Usage(
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            ),
            finish_reason=_finish_reason(result.finish_reason),
            model=result.model,
            degraded=bool(degraded),
            degraded_reasons=degraded,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------
    async def stream(
        self, request: AgentRunRequest, user_id: str
    ) -> AsyncGenerator[ChatStreamEvent, None]:
        """流式版本：先 :meth:`prepare`，再由 :meth:`stream_prepared` 逐个产出事件。"""
        prepared = await self.prepare(request, user_id)
        async for event in self.stream_prepared(prepared, user_id):
            yield event

    async def stream_prepared(
        self, prepared: PreparedAgent, user_id: str
    ) -> AsyncGenerator[ChatStreamEvent, None]:
        """按 ``meta → (tool_call → tool_result → reference*) * → token* → usage → done``。

        引用帧紧跟在产生它的 ``tool_result`` 之后：Agent 场景下引用可能来自第 3 轮工具调用，
        等最后才发会让用户在看答案时没法对应页码；早到 ``tool_call`` 之前又会引用一个尚未回流
        的工具结果。后续轮次新增的引用再补发（协议允许 ``reference`` 帧多次出现，
        见 ``docs/02`` §6）。
        """
        started = time.perf_counter()
        yield ChatStreamEvent(
            EVENT_META,
            StreamMeta(
                conversation_id=prepared.conversation_id,
                message_id=prepared.message_id,
                model=prepared.model,
                created_at=now_iso(),
                degraded=bool(prepared.degraded_reasons),
            ),
        )

        result: AgentResult | None = None
        #: 已推送的引用（全局编号基准：首次出现顺序）
        collected: list[RetrievedChunk] = []
        seen: set[str] = set()
        try:
            async with aclosing(
                self._loop.stream(
                    prepared.context.messages,
                    self._tool_context(prepared, user_id),
                    model=prepared.model,
                    temperature=prepared.temperature,
                    max_steps=prepared.max_steps,
                    tool_names=prepared.tool_names,
                )
            ) as events:
                async for event in events:
                    if event.kind == "final":
                        result = event.result
                        continue
                    if event.kind == "token":
                        yield ChatStreamEvent(EVENT_TOKEN, StreamToken(delta=event.content))
                        continue
                    record = event.record
                    if record is None:  # pragma: no cover - 事件构造保证非空
                        continue
                    if event.kind == "tool_call":
                        yield ChatStreamEvent(EVENT_TOOL_CALL, _tool_call_payload(record))
                        continue
                    # 先发结果本身，再发它的引用（客户端拿到结果时才有东西可参照）
                    yield ChatStreamEvent(EVENT_TOOL_RESULT, _tool_result_payload(record))
                    fresh = [chunk for chunk in record.citations if chunk.chunk_id not in seen]
                    if fresh:
                        seen.update(chunk.chunk_id for chunk in fresh)
                        collected.extend(fresh)
                        # 每次重发全部引用：``reference`` 帧的语义是「本轮引用集合」，
                        # 只发增量会让客户端的 ``[n]`` 编号错位。
                        yield ChatStreamEvent(
                            EVENT_REFERENCE, StreamReferences(references=_references(collected))
                        )
        except asyncio.CancelledError:
            logger.info("agent.canceled", extra={"message_id": prepared.message_id})
            if prepared.persist and prepared.conversation_id:
                await self._persist(prepared, user_id, result)
            raise
        except GeneratorExit:
            raise
        except AppError as exc:
            logger.warning("agent.stream_failed", extra={"code": str(exc.code)})
            yield ChatStreamEvent(EVENT_ERROR, exc.to_envelope()["error"])
            return

        result = result or AgentResult()
        degraded = _merge_reasons(prepared.degraded_reasons, result.degraded_reasons)
        if prepared.persist and prepared.conversation_id:
            await self._persist(prepared, user_id, result)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "agent.stream_done",
            extra={
                "model": result.model,
                "steps": result.steps,
                "tool_calls": len(result.calls),
                "elapsed_ms": elapsed_ms,
                "degraded": bool(degraded),
            },
        )
        yield ChatStreamEvent(
            EVENT_USAGE,
            StreamUsage(
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens
                or result.usage.prompt_tokens + result.usage.completion_tokens,
            ),
        )
        yield ChatStreamEvent(
            EVENT_DONE,
            StreamDone(finish_reason=_finish_reason(result.finish_reason), elapsed_ms=elapsed_ms),
        )

    def ping_event(self) -> ChatStreamEvent:
        """保活帧。"""
        return ChatStreamEvent(EVENT_PING, {"ts": now_iso()})

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------
    async def _persist(
        self,
        prepared: PreparedAgent,
        user_id: str,
        result: AgentResult | None,
    ) -> None:
        """把「用户提问 + 最终回答」写回会话历史。

        只落这两条：工具调用的中间消息不落库。``ConversationStore`` 的 role 只有
        user/assistant/system，而且把检索片段重新拼进下一轮历史会让历史膨胀，
        还会带上上一轮的检索结果（对已经变化的知识库是错的）。
        """
        if result is None:  # pragma: no cover - 取消时通常已有部分结果
            return
        conversation_id = prepared.conversation_id or ""
        turns = [
            StoredMessage(
                role="user",
                content=prepared.query,
                message_id=prepared.message_id,
                created_at=now_iso(),
            )
        ]
        if result.content:
            turns.append(
                StoredMessage(
                    role="assistant",
                    content=result.content,
                    message_id=new_id("msg"),
                    created_at=now_iso(),
                )
            )
        # 一次 append 写两条：实现会拿会话锁，分两次写会让另一个请求可能插在中间
        await self._store.append(conversation_id, user_id, turns)


def _merge_reasons(*groups: Sequence[str]) -> list[str]:
    """合并降级原因并去重、保持出现顺序。"""
    merged: list[str] = []
    for group in groups:
        for reason in group:
            if reason not in merged:
                merged.append(reason)
    return merged


def _trace(record: ToolCallRecord) -> ToolCallTrace:
    """工具结果 → 对外轨迹。"""
    return ToolCallTrace(
        call_id=record.call_id,
        name=record.name,
        arguments=dict(record.arguments),
        status=record.status,
        summary=record.summary,
        elapsed_ms=record.elapsed_ms,
    )


def _tool_call_payload(record: ToolCallRecord) -> dict[str, Any]:
    """``tool_call`` 帧负载（``docs/02`` §6）。"""
    return {
        "call_id": record.call_id,
        "name": record.name,
        "arguments": dict(record.arguments),
    }


def _tool_result_payload(record: ToolCallRecord) -> dict[str, Any]:
    """``tool_result`` 帧负载；``summary`` 必须可展示、不泄漏内部信息。"""
    return {
        "call_id": record.call_id,
        "name": record.name,
        "status": record.status,
        "summary": record.summary,
        "elapsed_ms": record.elapsed_ms,
    }


__all__ = ["RAG_TOOL_NAME", "AgentService", "PreparedAgent"]
