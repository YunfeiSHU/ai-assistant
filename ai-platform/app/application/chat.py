"""对话编排（``REQ-CHAT-001`` / ``002`` / ``004`` / ``005`` / ``006`` / ``007``）。

编排顺序（与 ``docs/03`` §2 一致）：

1. 校验请求（空 query / 模型白名单 / 会话归属）；
2. 收集上下文片段 —— 短期历史、摘要、长期记忆、RAG；
3. 装配 messages 并裁剪；
4. 调模型；
5. 落上下文（流式在 ``done`` 时一次性写）。

**降级而不是失败**：第 2 步的每个组件都被单独包在 try 里，任何失败只往
``degraded_reasons`` 里加一条原因码（`REQ-CHAT-007`）。这条规则的取舍是明确的：
「RAG 挂了就整个不能聊天」比「这次没带资料回答」严重得多。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from app.application.context import AssembledContext, ContextAssembler, MemoryItem
from app.application.memory import MemoryService
from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import hash_identifier
from app.core.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_PING,
    EVENT_REFERENCE,
    EVENT_TOKEN,
    EVENT_USAGE,
)
from app.core.text import sha256_hex
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing
from app.llm.base import (
    LLMClient,
    LLMDelta,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    map_llm_exception,
)
from app.memory.context_store import ConversationStore, StoredMessage, now_iso
from app.rag.base import RetrievalUnavailable, RetrievedChunk, Retriever
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    Reference,
    StreamDone,
    StreamMeta,
    StreamReferences,
    StreamToken,
    StreamUsage,
    Usage,
)
from app.tasks.models import ResourceType, TaskType
from app.tasks.runner import TaskRunner
from app.tasks.service import TaskService, make_idem_key

logger = logging.getLogger("app.chat")

#: 允许出现在 ``degraded_reasons`` 里的原因码（``docs/03`` §3.2）
REASON_RAG_UNAVAILABLE = "rag_unavailable"
REASON_MEMORY_UNAVAILABLE = "memory_unavailable"
REASON_RERANK_SKIPPED = "rerank_skipped"
REASON_MCP_UNAVAILABLE = "mcp_unavailable"
REASON_SUMMARY_FAILED = "summary_failed"

#: 上游 ``finish_reason`` → 契约值
_FINISH_REASONS = {"stop": "stop", "length": "length", "max_steps": "max_steps"}


@dataclass(slots=True)
class ChatStreamEvent:
    """一条待发送的 SSE 事件（路由层负责拼帧）。"""

    event: str
    data: Any


@dataclass(slots=True)
class PreparedChat:
    """一次对话的准备结果。

    对外可见是刻意的：路由层需要先完成准备阶段（可能抛 ``400`` / ``404`` / ``429``），
    **再**开始推 SSE 帧——一旦开始推事件就只能用 ``error`` 帧报错了。
    """

    query: str
    model: str
    temperature: float | None
    conversation_id: str | None
    message_id: str
    context: AssembledContext
    rag_chunks: list[RetrievedChunk]
    degraded_reasons: list[str] = field(default_factory=list)
    persist: bool = True


class ChatService:
    """对话用例编排。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        store: ConversationStore,
        *,
        retriever: Retriever | None = None,
        assembler: ContextAssembler | None = None,
        memory: MemoryService | None = None,
        tasks: TaskService | None = None,
        runner: TaskRunner | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._store = store
        self._retriever = retriever
        self._assembler = assembler or ContextAssembler(settings)
        self._memory = memory
        self._tasks = tasks
        self._runner = runner

    @property
    def llm(self) -> LLMClient:
        """底层 LLM 客户端（``GET /models`` 与启动期自检需要读它的模型表）。"""
        return self._llm

    @property
    def store(self) -> ConversationStore:
        """会话上下文存储。

        暴露它是为了让 :class:`~app.application.agent.AgentService` 复用**同一个**实例：
        ``/chat`` 写入的历史必须能被 ``/agent/run`` 读到（及反向），各自 new 一个
        内存实现会让「同一个会话换接口就丢上下文」——不报错，只是历史没了。
        """
        return self._store

    # ------------------------------------------------------------------
    # 准备阶段
    # ------------------------------------------------------------------
    async def prepare(self, request: ChatRequest, user_id: str) -> PreparedChat:
        """把请求规整成 :class:`PreparedChat`：语义校验 → 模型解析 → 历史/摘要/记忆/检索。

        ``use_tools=true`` **不在这里分支**，而是由路由层分派给 :class:`AgentService`
        （``docs/03`` §3.1）：两种响应的形状不同，混在一个方法里会让"返回类型取决于入参"
        扩散到整个服务层。空提问给专属的 ``QUERY_EMPTY``，而不是笼统的 ``INVALID_ARGUMENT``。
        """
        query = request.query.strip()
        if not query:
            # 语法上过 pydantic、语义上为空：必须给专属错误码而不是 INVALID_ARGUMENT
            raise AppError(ErrorCode.QUERY_EMPTY, "提问内容为空")

        # ``use_tools=true`` 由**路由层**分派给 :class:`AgentService`（见 ``docs/03`` §3.1）。
        # 放在路由而不是这里的理由：Agent 的响应体是 ``AgentRunResponse``（多一个
        # ``steps``），流式事件多出 tool_call/tool_result；在同一个方法里分支会让
        # “返回类型取决于入参”这种最难受测的形态扩散到整个服务层。

        model = self._llm.resolve_model(request.model)
        degraded: list[str] = []

        conversation_id: str | None = request.conversation_id
        history: list[LLMMessage] = []
        summary: str | None = None
        memories: list[MemoryItem] = []

        if request.use_memory:
            if conversation_id is None:
                conversation_id = new_id("cv")
            # 归属校验：不属于当前用户即 404（``docs/03`` §3.4）
            await self._store.ensure(conversation_id, user_id)
            history, summary, memories = await self._load_memory(
                conversation_id, user_id, query, degraded
            )
        else:
            # AC-CHAT-12：关闭记忆时只使用请求体里的 history
            history = [LLMMessage(role=m.role, content=m.content) for m in request.history]

        rag_chunks = await self._load_rag(request, user_id, query, degraded)

        context = self._assembler.build(
            query=query,
            history=history,
            memories=memories,
            summary=summary,
            rag_chunks=rag_chunks,
        )
        return PreparedChat(
            query=query,
            model=model,
            temperature=request.temperature,
            conversation_id=conversation_id,
            message_id=new_id("msg"),
            context=context,
            rag_chunks=rag_chunks,
            degraded_reasons=degraded,
            persist=request.use_memory,
        )

    async def _load_memory(
        self, conversation_id: str, user_id: str, query: str, degraded: list[str]
    ) -> tuple[list[LLMMessage], str | None, list[MemoryItem]]:
        """读取短期历史、摘要与长期记忆；任何一层失败只降级不报错。

        三层各自独立 try：摘要读不出来不该让长期记忆也跟着消失。
        """
        try:
            stored = await self._store.recent(
                conversation_id, user_id, turns=self._settings.memory_recent_turns
            )
            summary_record = await self._store.summary(conversation_id, user_id)
        except AppError:
            raise
        except Exception as exc:
            logger.warning("memory.read_failed", extra={"error": str(exc)})
            _append_reason(degraded, REASON_MEMORY_UNAVAILABLE)
            return [], None, []

        history = [LLMMessage(role=item.role, content=item.content) for item in stored]
        summary = summary_record.content if summary_record else None
        memories: list[MemoryItem] = []
        if self._memory is not None:
            try:
                memories = await self._memory.search(query, user_id)
            except AppError as exc:
                logger.warning("memory.search_failed", extra={"code": str(exc.code)})
                _append_reason(degraded, REASON_MEMORY_UNAVAILABLE)
            except Exception as exc:
                logger.warning("memory.search_failed", extra={"error": str(exc)})
                _append_reason(degraded, REASON_MEMORY_UNAVAILABLE)
        return history, summary, memories

    async def _load_rag(
        self, request: ChatRequest, user_id: str, query: str, degraded: list[str]
    ) -> list[RetrievedChunk]:
        """检索知识库；不可用即降级（``AC-CHAT-08``）。"""
        if not request.use_rag or self._retriever is None:
            return []
        settings = self._settings
        try:
            return await self._retriever.retrieve(
                query=query,
                user_id=user_id,
                kb_ids=list(request.kb_ids),
                top_k=request.top_k or settings.retrieval_top_k,
                rerank_top_n=request.rerank_top_n or settings.reranker_top_n,
                score_threshold=(
                    settings.score_threshold
                    if request.score_threshold is None
                    else request.score_threshold
                ),
            )
        except RetrievalUnavailable as exc:
            logger.warning("rag.unavailable", extra={"reason": str(exc)})
            _append_reason(degraded, REASON_RAG_UNAVAILABLE)
            return []
        except Exception as exc:
            logger.warning("rag.failed", extra={"error": str(exc)})
            _append_reason(degraded, REASON_RAG_UNAVAILABLE)
            return []

    # ------------------------------------------------------------------
    # 模型调用（含异常兜底映射）
    # ------------------------------------------------------------------
    async def _call_llm(self, prepared: PreparedChat) -> LLMResponse:
        """调用模型并**兜底映射**异常。

        适配器自己会映射，但那是「适配器的责任」，不是「上层可以依赖的保证」：
        任何 LLMClient 实现（含测试替身与将来新增的供应商）抛出未识别异常时，
        这里都保证它变成明确的对外错误码而不是 500 —— 否则监控会把上游故障
        算成我们自己的 bug，重试策略也会跑偏。
        """
        settings = self._settings
        try:
            return await self._llm.complete(
                prepared.context.messages,
                model=prepared.model,
                temperature=prepared.temperature,
                max_tokens=settings.max_output_tokens,
            )
        except AppError:
            raise
        except Exception as exc:
            raise map_llm_exception(exc) from exc

    async def _iter_llm(self, prepared: PreparedChat) -> AsyncGenerator[LLMDelta, None]:
        """流式调用模型；异常同样先翻译再上抛（见 :meth:`_call_llm`）。"""
        settings = self._settings
        try:
            async for delta in self._llm.stream(
                prepared.context.messages,
                model=prepared.model,
                temperature=prepared.temperature,
                max_tokens=settings.max_output_tokens,
            ):
                yield delta
        except AppError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise map_llm_exception(exc) from exc

    def _observe_first_token(self, prepared: PreparedChat, elapsed: float, first: bool) -> bool:
        """首个正文 token 到达时记一次首 token 延迟；返回「是否已记过」。"""
        if first:
            return True
        get_metrics().observe_first_token(
            model=prepared.model, use_rag=bool(prepared.rag_chunks), seconds=elapsed
        )
        return True

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------
    async def complete(self, request: ChatRequest, user_id: str) -> ChatResponse:
        """非流式对话入口：``prepare`` → 模型调用 → 落库/摘要/记忆投递，一次性返回。

        整段（含收尾逻辑）都包在 ``chat.request`` span 里 —— 看 trace 时要能分辨"慢在模型"
        还是"慢在我们自己的收尾"。``user_id`` 按 ``docs/10`` §5.1 哈希后才写进 span 属性。
        """
        started = time.perf_counter()
        prepared = await self.prepare(request, user_id)

        # span 包住**整段**处理（含落库与轮末投递），而不只是模型调用：
        # 看 trace 时要能一眼看出「慢在模型还是慢在我们自己的收尾逻辑」。
        # 属性按 ``docs/10`` §5.1：``user_id`` MUST 哈希后才允许外泄给第三方。
        with get_tracing().span(
            "chat.request",
            {
                "user_id": hash_identifier(user_id, self._settings.api_key_pepper),
                "conversation_id": prepared.conversation_id,
                "use_rag": bool(prepared.rag_chunks),
                "use_tools": False,
                "stream": False,
                "degraded": bool(prepared.degraded_reasons),
            },
        ):
            return await self._complete(prepared, user_id, started)

    async def _complete(self, prepared: PreparedChat, user_id: str, started: float) -> ChatResponse:
        """非流式的实际处理（从 :meth:`complete` 拆出，好让 span 包住整段）。"""
        result = await self._call_llm(prepared)
        # 引用必须先生成：``[n]`` 的合法上界就是引用条数（``docs/06`` §6）
        references = _references(prepared.rag_chunks)
        answer = prune_out_of_range_citations(result.content or "", max_index=len(references))
        if not answer and result.finish_reason == "stop":
            # 空回答不报错但必须留痕：多半是上游行为异常
            logger.warning("chat.empty_answer", extra={"model": result.model})

        if prepared.persist and prepared.conversation_id:
            await self._persist(prepared.conversation_id, user_id, prepared, answer)
            # 轮末动作（记忆抽取 / 摘要）必须在**返回响应之前**投递。
            # 只在流式路径里做收尾，会让「用 /chat 还是 /chat/stream」决定记忆有没有
            # 被抽取 —— 默认走的是非流式，于是记忆永远是空的，而日志上看不出异常。
            await self._after_turn(prepared.conversation_id, user_id, prepared.degraded_reasons)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "chat.completed",
            extra={
                "model": result.model,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "elapsed_ms": elapsed_ms,
                "degraded": bool(prepared.degraded_reasons),
                "trimmed": prepared.context.trimmed,
            },
        )
        return ChatResponse(
            answer=answer,
            conversation_id=prepared.conversation_id,
            message_id=prepared.message_id,
            references=references,
            tool_calls=[],
            usage=Usage(
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            ),
            finish_reason=_finish_reason(result.finish_reason),
            model=result.model,
            degraded=bool(prepared.degraded_reasons),
            degraded_reasons=prepared.degraded_reasons,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------
    async def stream(
        self, request: ChatRequest, user_id: str
    ) -> AsyncGenerator[ChatStreamEvent, None]:
        """准备 + 推流的便捷入口（导入断言顺序的测试用）。"""
        prepared = await self.prepare(request, user_id)
        async for event in self.stream_prepared(prepared, user_id):
            yield event

    async def stream_prepared(
        self, prepared: PreparedChat, user_id: str
    ) -> AsyncGenerator[ChatStreamEvent, None]:
        """按 ``meta → reference* → token* → usage → done`` 产出事件。

        准备阶段已在 :meth:`prepare` 完成，所以这里抛出的错误会以 ``error`` 帧的形式
        交给客户端（如果已经吐过 token），或以原异常上抛（还没吐过任何内容）。
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
        if prepared.rag_chunks:
            # REQ-CHAT-003：引用必须先于首个 token 推送
            yield ChatStreamEvent(
                EVENT_REFERENCE, StreamReferences(references=_references(prepared.rag_chunks))
            )

        buffer: list[str] = []
        usage = LLMUsage()
        finish_reason = "stop"
        first_token_seen = False
        try:
            # ``aclosing`` 保证无论从哪条路径退出（正常 / 异常 / 断连），
            # 上游 LLM 生成器都会被显式关闭 —— 即「停止上游调用」。
            async with aclosing(self._iter_llm(prepared)) as stream:
                async for delta in stream:
                    if delta.content:
                        # 首 token 延迟从**进入本方法**算起（包含 meta/reference 帧的开销）——
                        # 那正是用户体验到的「等多久才看到字」
                        first_token_seen = self._observe_first_token(
                            prepared, time.perf_counter() - started, first_token_seen
                        )
                        buffer.append(delta.content)
                        yield ChatStreamEvent(EVENT_TOKEN, StreamToken(delta=delta.content))
                    if delta.usage is not None:
                        usage = delta.usage
                    if delta.finish_reason:
                        finish_reason = delta.finish_reason
        except asyncio.CancelledError:
            # REQ-CHAT-006：断连 1s 内停止上游并把半成品以 partial=true 落库
            get_metrics().record_cancel("client_disconnect")
            logger.info(
                "chat.canceled", extra={"tokens": len(buffer), "message_id": prepared.message_id}
            )
            await self._persist_partial(prepared, user_id, "".join(buffer))
            raise
        except GeneratorExit:
            # 生成器被关闭时**不允许 await**（会报 "async generator ignored
            # GeneratorExit"），所以半成品落库改成后台任务。
            get_metrics().record_cancel("client_disconnect")
            logger.info(
                "chat.stream_closed",
                extra={"tokens": len(buffer), "message_id": prepared.message_id},
            )
            self._persist_partial_background(prepared, user_id, "".join(buffer))
            raise
        except AppError as exc:
            # 文档 §4.4 的验收口径：首字节超时等上游错误 → 发 ``error`` 帧并结束。
            # 此时响应头早已发出，改不了 HTTP 状态码了。
            if exc.code is ErrorCode.UPSTREAM_TIMEOUT:
                # 首字节/空闲超时与「用户关页面」是两种不同的中断，分开计数
                # （``docs/10`` §5.2 的 reason 取值就这两种），否则无法区分
                # 「体验被客户端打断」与「上游太慢」。
                get_metrics().record_cancel("timeout")
            logger.warning("chat.stream_failed", extra={"code": str(exc.code)})
            await self._persist_partial(prepared, user_id, "".join(buffer))
            yield ChatStreamEvent(EVENT_ERROR, exc.to_envelope()["error"])
            return

        answer = "".join(buffer)
        if prepared.persist and prepared.conversation_id:
            await self._persist(prepared.conversation_id, user_id, prepared, answer)
            await self._after_turn(prepared.conversation_id, user_id, prepared.degraded_reasons)

        # 流式**不能**回溯裁剪：`[9]` 可能在某个 delta 里就已经推给前端了，
        # 而把越界标注「吐一半再撤回」在 SSE 协议里没有表达方式。
        # 所以这里只做审计（``docs/06`` §6 用的是 SHOULD）：把越界标注记进日志，
        # 便于发现「模型在编引用」还是「我们少传了上下文」。
        # 真正剔越界标注由非流式路径完成（那里正文一次性产出，可以安全重写）。
        _audit_citations_best_effort(answer, max_index=len(prepared.rag_chunks))

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "chat.stream_done",
            extra={
                "model": prepared.model,
                "chunks": len(buffer),
                "elapsed_ms": elapsed_ms,
                "trimmed": prepared.context.trimmed,
            },
        )
        yield ChatStreamEvent(
            EVENT_USAGE,
            StreamUsage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens or usage.prompt_tokens + usage.completion_tokens,
            ),
        )
        yield ChatStreamEvent(
            EVENT_DONE,
            StreamDone(finish_reason=_finish_reason(finish_reason), elapsed_ms=elapsed_ms),
        )

    # ------------------------------------------------------------------
    # 轮末动作：记忆抽取与摘要（``docs/07`` §3.1 / §5.1）
    # ------------------------------------------------------------------
    async def _after_turn(self, conversation_id: str, user_id: str, degraded: list[str]) -> None:
        """一轮对话结束后的收尾：投递记忆抽取，按需生成摘要。

        两个动作都**不得**把失败冒泡给调用方：它们是在用户已经拿到回答之后执行的，
        让它们把一次成功对话变成 500 是最不划算的取舍。

        投递规则（刻意只写一次）：**有任务服务就建任务，没有就同步做掉**。
        前者是 ``docs/08`` 要求的形态（可在 ``GET /tasks`` 看到、可重试）；
        后者服务于 ``TASK_RUNNER=none`` / 直接构造 ``ChatService`` 的场景 ——
        没部署 Worker 时如果什么都不做，摘要与记忆就永远不会生成，而表面上
        一切正常（这才是真正难排查的形态）。
        """
        if self._memory is None or not self._settings.memory_enabled:
            return
        try:
            messages = await self._store.all_messages(conversation_id, user_id)
        except Exception as exc:
            # 读不到消息就什么都做不了：记下来即可（它紧接着的一次失败也会记）
            logger.warning("memory.turn_snapshot_failed", extra={"error": str(exc)})
            messages = []
        turn_marker = messages[-1].message_id if messages else "empty"
        try:
            await self._submit(
                TaskType.MEMORY_EXTRACT,
                user_id=user_id,
                resource_type=ResourceType.CONVERSATION,
                resource_id=conversation_id,
                payload={"conversation_id": conversation_id},
                turn_marker=turn_marker,
            )
        except Exception as exc:
            logger.warning("memory.extract_submit_failed", extra={"error": str(exc)})

        builder = self._memory.summary_builder
        if builder is None or not self._settings.summary_enabled:
            return
        try:
            previous = await self._store.summary(conversation_id, user_id)
            # 触发条件只有一份实现（``SummaryBuilder.should_build`` 内部就是
            # ``should_summarize``）：这里再判一次「是不是该摘要」迟早会与它分叉
            if not builder.should_build(messages, previous):
                return
            await self._submit(
                TaskType.SUMMARY_BUILD,
                user_id=user_id,
                resource_type=ResourceType.CONVERSATION,
                resource_id=conversation_id,
                payload={"conversation_id": conversation_id},
                turn_marker=turn_marker,
            )
        except AppError as exc:
            logger.warning(
                "memory.summary_failed",
                extra={"code": str(exc.code), "error": exc.message},
            )
            if exc.code is ErrorCode.SUMMARY_GENERATION_FAILED:
                # 只有**同步**兜底路径（没配 TaskService/Runner）才能把失败写回
                # 本次响应：异步任务的失败只能体现在 ``GET /tasks`` 上，
                # 它发生在响应返回之后，不可能回到这个列表里。
                _append_reason(degraded, REASON_SUMMARY_FAILED)
        except Exception as exc:
            logger.warning("memory.summary_submit_failed", extra={"error": str(exc)})

    async def _submit(
        self,
        type_: TaskType,
        *,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
        payload: dict[str, Any],
        turn_marker: str,
    ) -> None:
        """建任务并投递；没配置任务服务时退化为同步执行。

        ``turn_marker`` 必须**每轮不同**（用本轮最后一条消息的 ``message_id``）。
        ``TaskService`` 默认的幂等键是 ``(类型, 用户, 资源)``，同一会话的第二轮会
        命中第一轮那条已经终态的任务，``runner.submit`` 随即把它当成重复投递跳过 ——
        结果是「记忆抽取与摘要在第一轮之后再也不执行」，而接口全是 200、没有任何异常。
        """
        if self._tasks is None or self._runner is None:
            await self._run_inline(type_, user_id, resource_id)
            return
        task, _created = await self._tasks.create(
            type_=type_,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
            idem_key=make_idem_key(type_, user_id, resource_id, turn_marker),
        )
        await self._runner.submit(task)

    async def _run_inline(self, type_: TaskType, user_id: str, resource_id: str) -> None:
        """无任务服务时的同步兜底（见 :meth:`_after_turn` 的投递规则）。

        ``type_`` 只可能是 ``memory_extract`` / ``summary_build``：这两个值由
        :meth:`_after_turn` 写死传入，不接收外部输入。
        """
        memory = self._memory
        assert memory is not None  # 调用点已判空
        if type_ is TaskType.MEMORY_EXTRACT:
            messages = await self._store.recent(
                resource_id, user_id, turns=self._settings.memory_recent_turns
            )
            await memory.extract_and_store(messages, user_id=user_id, conversation_id=resource_id)
            return
        outcome = await memory.build_summary(resource_id, user_id)
        if outcome is not None and outcome.error:
            raise AppError(ErrorCode.SUMMARY_GENERATION_FAILED, f"摘要生成失败：{outcome.error}")

    # ------------------------------------------------------------------
    def ping_event(self) -> ChatStreamEvent:
        """保活帧（路由层按 ``PING_INTERVAL_SECONDS`` 触发）。"""
        return ChatStreamEvent(EVENT_PING, {"ts": now_iso()})

    # ------------------------------------------------------------------
    def _persist_partial_background(
        self, prepared: PreparedChat, user_id: str, partial_text: str
    ) -> None:
        """后台落库（仅用于 GeneratorExit 路径，那里不能 await）。"""
        if not prepared.persist or not prepared.conversation_id or not partial_text:
            return
        task = asyncio.create_task(self._persist_partial(prepared, user_id, partial_text))
        task.add_done_callback(_log_background_failure)

    async def _persist(
        self, conversation_id: str, user_id: str, prepared: PreparedChat, answer: str
    ) -> None:
        await self._store.append(
            conversation_id,
            user_id,
            [
                StoredMessage(
                    role="user",
                    content=prepared.query,
                    message_id=new_id("msg"),
                    created_at=now_iso(),
                ),
                StoredMessage(
                    role="assistant",
                    content=answer,
                    message_id=prepared.message_id,
                    created_at=now_iso(),
                ),
            ],
        )

    async def _persist_partial(
        self, prepared: PreparedChat, user_id: str, partial_text: str
    ) -> None:
        """中断时把已生成的部分落库；失败只记日志（不能让清理动作盖掉原始中断）。"""
        if not prepared.persist or not prepared.conversation_id or not partial_text:
            return
        try:
            await self._store.append(
                prepared.conversation_id,
                user_id,
                [
                    StoredMessage(
                        role="user",
                        content=prepared.query,
                        message_id=new_id("msg"),
                        created_at=now_iso(),
                    ),
                    StoredMessage(
                        role="assistant",
                        content=partial_text,
                        message_id=prepared.message_id,
                        created_at=now_iso(),
                        partial=True,
                    ),
                ],
            )
        except Exception as exc:
            logger.warning("chat.persist_partial_failed", extra={"error": str(exc)})


def _log_background_failure(task: asyncio.Task[None]) -> None:
    """后台任务的异常必须有人接，否则会被默默丢掉（只在事件循环关闭时吐一句）。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("chat.background_task_failed", extra={"error": str(error)})


def _append_reason(reasons: list[str], reason: str) -> None:
    """追加一条降级原因，并记 ``ai_degraded_total``。

    所有降级都经过这个函数（RAG 不可用 / 记忆不可用 / 重排跳过 / 摘要失败 …），
    所以指标只需要在这一个地方记 —— 否则「新加了一种降级」时一定会有人忘了补计数，
    而那正是核心告警指标。
    """
    if reason not in reasons:
        reasons.append(reason)
        get_metrics().record_degraded(reason)


def _references(chunks: Sequence[RetrievedChunk]) -> list[Reference]:
    """按最终顺序生成引用；``index`` 从 1 开始且与正文 ``[n]`` 对应。"""
    return [
        Reference(
            index=index,
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            kb_id=chunk.kb_id,
            doc_name=chunk.doc_name,
            page=chunk.page,
            heading_path=chunk.heading_path or None,
            score=chunk.score,
            snippet=chunk.text.strip()[:200],
            # 哈希算的是**返回给用户的这段文本**，而不是入库时的原文哈希：
            # 相邻合并会改变 text，用原文哈希会让「同一片段」判断与展示内容不符。
            content_sha256=sha256_hex(chunk.text),
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def prune_out_of_range_citations(text: str, *, max_index: int) -> str:
    """剔除正文里越界的 ``[n]`` 引用标注（``AC-RAG-14``）。

    ``docs/06`` §6 要求服务端校验并记 warning。这里**只删标注、不改动其余文本**：
    模型可能把 ``[9]`` 写在句末，整句删掉会丢内容；把 ``[9]`` 换成 ``[?]`` 又会
    污染正文。删除是唯一既不丢信息也不误导的做法。

    刻意不用 ``re.sub`` 一次替换：需要知道「剔了哪些」，否则日志里只能记一句
    「可能剔过」，线上无法定位是模型乱编还是我们漏传了上下文。
    """
    if not text or "[" not in text:
        return text
    removed: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        # 多引用写法 ``[1,2]`` / ``[1, 2]`` 逐个判断，只要有一个越界就整体重排
        kept = [part for part in re.split(r"[,，]", raw) if part.strip()]
        valid: list[str] = []
        for part in kept:
            token = part.strip()
            if not token.isdigit():
                valid.append(token)
                continue
            if int(token) > max_index:
                removed.append(token)
                continue
            valid.append(token)
        if not valid:
            return ""
        return "[" + ",".join(valid) + "]"

    pruned = re.sub(r"\[([0-9][0-9,，\s]*)\]", _replace, text)
    if removed:
        logger.warning(
            "chat.citation_out_of_range_removed",
            extra={"removed": removed, "max_index": max_index},
        )
    return pruned


def _audit_citations_best_effort(text: str, *, max_index: int) -> None:
    """只查不改：找出越界 ``[n]`` 并记警告（流式路径专用）。"""
    pruned = prune_out_of_range_citations(text, max_index=max_index)
    if pruned != text:
        logger.warning("chat.citation_out_of_range_stream", extra={"max_index": max_index})


def _finish_reason(raw: str | None) -> str:
    return _FINISH_REASONS.get((raw or "stop").lower(), "stop")


__all__ = [
    "REASON_MCP_UNAVAILABLE",
    "REASON_MEMORY_UNAVAILABLE",
    "REASON_RAG_UNAVAILABLE",
    "REASON_RERANK_SKIPPED",
    "ChatService",
    "ChatStreamEvent",
    "PreparedChat",
    "prune_out_of_range_citations",
]
