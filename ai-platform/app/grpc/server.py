"""AI 侧的 gRPC 服务：Go 网关的 ``Chat`` 调用落点（docs/04 §2，接缝 J1/J2/J3/J9）。

**为什么复用 ``create_app()`` 而不是另起一套装配**：编排链路的每一环（检索、记忆、Agent、限流、
指标）都挂在 ``app.state`` 上，由 ``create_app`` 统一构造。gRPC 入口若自己装配一遍，就会出现
「HTTP 通道有记忆、gRPC 通道没有」这种两边行为不一致的问题。

**为什么手工跑 lifespan**：``create_app()`` 是同步的（测试要靠它构造实例），真正的启动动作
（配置校验、向量库建表、MySQL 自检、MCP 连接）都在 ``lifespan`` 里。gRPC 进程不走 uvicorn，
所以要自己进 ``app.router.lifespan_context(app)``，否则会得到一个「连上了但什么都没初始化」
的服务，表现为第一次调用报一堆莫名其妙的错。
"""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, NoReturn, cast

from fastapi import FastAPI
from google.rpc import code_pb2
from pydantic import ValidationError

import grpc
from app.application.chat import ChatStreamEvent
from app.core.config import Settings
from app.core.context import (
    get_trace_id,
    new_span_id,
    new_trace_id,
    request_context,
    set_user_id,
)
from app.core.exceptions import AppError, ErrorCode
from app.core.ids import new_id
from app.core.logging import get_logger
from app.core.security import AuthUser, authenticate
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
from app.grpc import errors as grpc_errors
from app.grpc.aiplatform.v1 import chat_pb2, chat_pb2_grpc
from app.schemas.agent import AgentRunRequest
from app.schemas.chat import ChatMessage, ChatRequest, ChatResponse, Reference

logger = get_logger("app.grpc")

#: 透传 trace id 的元数据键（接缝 J3）。
META_TRACE_ID = "x-trace-id"
#: 透传请求号的元数据键。
META_REQUEST_ID = "x-request-id"
#: 用户凭据（``Bearer <jwt>``）。**必须**由网关原样转发，不允许网关自造身份。
META_AUTHORIZATION = "authorization"
#: 无鉴权模式（``AUTH_ENABLED=false``）下的兜底用户，与 HTTP 通道的 ``X-Debug-User-Id``
#: 请求头同义。仅本地开发可用：生产 ``validate_for_startup`` 会强制 ``auth_required``。
META_DEBUG_USER_ID = "x-debug-user-id"
#: 服务间凭据（网关后台任务用，接缝 J9）。
META_INTERNAL_TOKEN = "x-internal-service-token"


@dataclass(frozen=True, slots=True)
class GrpcOptions:
    """gRPC 监听参数。"""

    host: str = "127.0.0.1"
    port: int = 50051
    #: 同时在处理的请求数上限；超出后 ``RESOURCE_EXHAUSTED``（网关侧归一成过载）。
    max_concurrency: int = 64

    @classmethod
    def from_settings(cls, settings: Settings) -> GrpcOptions:
        """从 :class:`Settings` 装配（监听地址与并发上限都来自配置，便于测试注入）。"""
        return cls(
            host=settings.grpc_host,
            port=settings.grpc_port,
            max_concurrency=settings.grpc_max_concurrency,
        )

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class ChatServicer(chat_pb2_grpc.AiPlatformServicer):
    """``AiPlatform.Chat`` 的实现。

    与 ``POST /api/v1/chat`` **行为等价**（同一套 service、同一套错误映射），
    差别只在传输层：proto 入参、``google.rpc.Status`` 出参。
    """

    def __init__(self, application: FastAPI) -> None:
        self._app = application

    # ---------------------------------------------------------------- 入口
    async def Chat(
        self,
        request: chat_pb2.ChatRequest,
        context: grpc.aio.ServicerContext,
    ) -> chat_pb2.ChatResponse:
        """gRPC 一元对话：与 HTTP ``POST /chat`` 共用同一套服务层，只是传输形态不同。

        先鉴权，再把 protobuf 入参装配成契约对象；**只有**「入参 → 契约对象」这一步的校验失败
        才归为参数错误，其余错误走统一的错误码映射。
        """
        meta = _metadata_map(context)
        trace_id = (meta.get(META_TRACE_ID) or "").strip() or new_trace_id()
        request_id = (meta.get(META_REQUEST_ID) or "").strip() or new_id("req")

        with request_context(trace_id, new_span_id(), request_id, "Chat"):
            try:
                user_id = self._authenticate(meta)
                set_user_id(user_id)
                try:
                    body = _to_chat_request(request)
                except ValidationError as exc:
                    # **只有**「入参 → 契约对象」这一步的校验失败才是 400。把它扩大到整个调用
                    # 的话，服务内部的 ``ValidationError``（比如上游返回的数据不符合 schema）
                    # 会被报成「调用方参数不合法」—— 排查方向从一开始就指错了。
                    raise _validation_error(exc) from exc
                result = await self._dispatch(body, user_id)
            except AppError as exc:
                await _abort_with_app_error(context, exc, trace_id)
            except Exception as exc:
                logger.exception(
                    "grpc.chat_unhandled",
                    extra={"error": repr(exc), "upstream_trace_id": trace_id},
                )
                await _abort_with_app_error(context, AppError(ErrorCode.INTERNAL_ERROR), trace_id)

            context.set_trailing_metadata(((META_TRACE_ID, trace_id),))
            return _to_proto_response(result)

    async def ChatStream(
        self,
        request: chat_pb2.ChatRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncGenerator[chat_pb2.ChatEvent, None]:
        """``AiPlatform.ChatStream``：把编排事件映射成 ``ChatEvent``。

        与 ``POST /api/v1/chat/stream`` **行为等价**：同一套 ``prepare`` / ``stream_prepared``，
        同一套事件顺序，差别只在传输层。三个不显然的地方：

        1. **准备阶段必须在第一个 ``yield`` 之前完成。** 与 HTTP 通道「先完成 prepare 再返回
           StreamingResponse」是同一条理由：一旦开始推事件，能用的报错手段就只剩 ``error`` 帧了。
           所以 ``prepare`` 抛的 ``AppError`` 走 ``abort``。
        2. **不加心跳帧。** ``event: ping`` 是 SSE 传输层的东西，gRPC 有自己的 keepalive；
           把它包成 ``unknown`` 会让客户端收到两路心跳。
        3. **取消必须穿透到 LLM。** 网关断连时 grpc.aio 会把 ``CancelledError`` 抛进本生成器的
           挂起点，也就是 ``stream_prepared`` 内部 —— 它自带 ``aclosing`` 与
           ``except CancelledError``。本方法**不捕获** ``CancelledError``，捕获它就等于把
           「停止计费」这件事弄丢。
        """
        meta = _metadata_map(context)
        trace_id = (meta.get(META_TRACE_ID) or "").strip() or new_trace_id()
        request_id = (meta.get(META_REQUEST_ID) or "").strip() or new_id("req")

        with request_context(trace_id, new_span_id(), request_id, "ChatStream"):
            try:
                user_id = self._authenticate(meta)
                set_user_id(user_id)
                try:
                    body = _to_chat_request(request)
                except ValidationError as exc:
                    raise _validation_error(exc) from exc
                # 返回 (事件流, 降级原因)。降级原因必须**在这里**取到：``meta`` 事件里只有布尔值，
                # 而网关要把原因落进 ``assistant.degraded_reasons``（docs/04 §9）。
                events, degraded_reasons = await self._prepare_stream(body, user_id)
            except AppError as exc:
                await _abort_with_app_error(context, exc, trace_id)
                return
            except Exception as exc:  # pragma: no cover - 兜底路径
                logger.exception(
                    "grpc.chat_stream_unhandled",
                    extra={"error": repr(exc), "upstream_trace_id": trace_id},
                )
                await _abort_with_app_error(context, AppError(ErrorCode.INTERNAL_ERROR), trace_id)
                return

            # 成功路径也回传 trace id（与 ``Chat`` 一致）：两条路用同一个通道，客户端只有一处实现。
            context.set_trailing_metadata(((META_TRACE_ID, trace_id),))
            cursor = _StreamCursor(degraded_reasons)
            async for event in events:
                message = cursor.map(event)
                if message is not None:
                    yield message

    # ------------------------------------------------------------ 内部实现
    async def _prepare_stream(
        self, body: ChatRequest, user_id: str
    ) -> tuple[AsyncGenerator[ChatStreamEvent, None], list[str]]:
        """完成准备阶段并返回事件流与降级原因。

        返回的是**尚未开始迭代**的异步生成器：``prepare`` 已经执行完（它的异常在这里抛出，
        交给调用方 ``abort``），而流还没吐第一个事件。
        """
        if body.use_tools:
            agents = self._app.state.agent_service
            prepared = await agents.prepare(_as_agent_request(body), user_id)
            return agents.stream_prepared(prepared, user_id), list(prepared.degraded_reasons)
        service = self._app.state.chat_service
        prepared_chat = await service.prepare(body, user_id)
        return (
            service.stream_prepared(prepared_chat, user_id),
            list(prepared_chat.degraded_reasons),
        )

    def _authenticate(self, meta: dict[str, str]) -> str:
        """解析身份。与 HTTP 的 ``get_current_user`` 依赖走**同一个** ``authenticate``。

        两边共用同一个函数是刻意的：``authenticate`` 里那段「``auth_required=false`` 时优先用
        debug 身份」的分支如果在这里重写一遍，两条通道的鉴权行为就会各自演化。
        """
        settings = cast(Settings, self._app.state.settings)
        internal_token = (meta.get(META_INTERNAL_TOKEN) or "").strip()
        if (
            internal_token
            and not meta.get(META_AUTHORIZATION)
            and settings.internal_service_token
            and internal_token == settings.internal_service_token
        ):
            # 服务间凭据能证明「调用方是网关」，但证明不了「代表哪个用户」——
            # ``ChatRequest`` 里刻意没有 user_id（网关不许自造身份）。所以这里如实拒绝。
            raise AppError(
                ErrorCode.UNAUTHENTICATED,
                "会话接口需要用户凭据，服务间凭据不足以确定用户",
                {"hint": "网关需转发终端用户的 Authorization"},
            )
        user: AuthUser = authenticate(
            authorization=meta.get(META_AUTHORIZATION),
            settings=settings,
            debug_user_id=meta.get(META_DEBUG_USER_ID),
        )
        return user.user_id

    async def _dispatch(self, body: ChatRequest, user_id: str) -> ChatResponse:
        """按 ``use_tools`` 分流，与 ``POST /chat`` 的分支完全一致。"""
        if body.use_tools:
            agents = self._app.state.agent_service
            # 必须 await：漏掉之后返回值是一个「永不执行的协程」对象，报错现场是序列化时的
            # ``'coroutine' object has no attribute ...``，与真正的原因（少了 await）距离很远。
            result: ChatResponse = await agents.run(_as_agent_request(body), user_id)
            return result
        service = self._app.state.chat_service
        return await service.complete(body, user_id)


def _as_agent_request(body: ChatRequest) -> AgentRunRequest:
    """``ChatRequest`` → ``AgentRunRequest``（``use_tools`` 强制为 true）。

    与 ``app/api/v1/chat.py`` 里的同名私有函数是同一行逻辑。**没有直接 import 是为了不让
    「传输层」依赖「HTTP 路由层」**（路由层会随 HTTP 契约变动）；两条实现的一致性由
    ``tests/test_grpc_server.py`` 里的对照用例保证。
    """
    return AgentRunRequest(**{**body.model_dump(), "use_tools": True})


# ---------------------------------------------------------------- 元数据
def _metadata_map(context: grpc.aio.ServicerContext) -> dict[str, str]:
    """把调用元数据收成小写键的字典。

    gRPC 的键在传输中一律小写，但**不同实现的回读大小写不一致**，所以这里统一 ``lower()``：
    不统一的话 ``x-debug-user-id`` 在某个客户端下会取不到，表现为「本地开发突然 401」。
    """
    out: dict[str, str] = {}
    for item in context.invocation_metadata():
        key = getattr(item, "key", None) or item[0]
        value: Any = getattr(item, "value", None)
        if value is None:
            value = item[1]
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        out[str(key).lower()] = str(value)
    return out


# ---------------------------------------------------------------- 错误
def _validation_error(exc: ValidationError) -> AppError:
    """把 Pydantic 校验失败转成 ``400 INVALID_ARGUMENT`` + ``details.fields``。"""
    fields = [
        {
            "loc": ".".join(str(part) for part in item.get("loc", ())),
            "msg": item.get("msg", "invalid"),
            "type": item.get("type", "value_error"),
        }
        for item in exc.errors()
    ]
    return AppError(ErrorCode.INVALID_ARGUMENT, "请求参数不合法", {"fields": fields})


async def _abort_with_app_error(
    context: grpc.aio.ServicerContext, exc: AppError, trace_id: str
) -> NoReturn:
    """把 ``AppError`` 作为富错误状态回给网关。

    ``google.rpc.Status`` 必须放在 ``grpc-status-details-bin`` 尾随元数据里，否则 gRPC 只传得
    过去一个粗粒度的码，客户端拿不到业务码。``trailing_metadata`` 同时显式传进 ``abort``
    （而不是先调 ``set_trailing_metadata``）：``abort`` 的参数是**取代**语义，显式传参不依赖
    「之前设的还在不在」这个实现细节。
    """
    status = grpc_errors.to_rpc_status(exc, trace_id)
    trailer = (
        ("grpc-status-details-bin", status.SerializeToString()),
        (META_TRACE_ID, trace_id),
    )
    logger.info(
        "grpc.chat_error",
        extra={
            "error_code": str(exc.code),
            "http_status": exc.status_code,
            "upstream_trace_id": trace_id,
            "grpc_code": grpc_errors.grpc_code_for(exc),
        },
    )
    await context.abort(
        _grpc_status_code(grpc_errors.grpc_code_for(exc)),
        # message 放业务码而不是人类文案：网关只读 detail 里的 AiError，但 grpcurl 之类
        # 只看 status 的工具能一眼看到 ``KB_NOT_FOUND``。
        str(exc.code),
        trailing_metadata=trailer,
    )
    # grpc.aio 把 ``abort`` 的类型标成返回 ``None``，实际运行时它抛异常终止 handler。补一行显式
    # raise：既让上面的 ``-> NoReturn`` 自洽，也把「abort 之后不可达」写给读代码的人。
    raise AssertionError("unreachable: context.abort() 不应返回")


def _grpc_status_code(code: int) -> grpc.StatusCode:
    """``google.rpc.Code`` 数值 → ``grpc.StatusCode``。

    经**名字**转，而不是比数值：两边的枚举数值并不一一对应，而名字（``NOT_FOUND`` /
    ``RESOURCE_EXHAUSTED`` …）是一样的。按数值硬挤会在某个码上静默得到另一个语义的状态。
    """
    try:
        name = code_pb2.Code.Name(int(code))
    except ValueError:  # pragma: no cover - 只有传了非法数值才会走到
        return grpc.StatusCode.UNKNOWN
    return grpc.StatusCode.__members__.get(name, grpc.StatusCode.UNKNOWN)


# ---------------------------------------------------------------- 出入参
def _to_chat_request(request: chat_pb2.ChatRequest) -> ChatRequest:
    """proto → Pydantic。

    ``use_rag`` / ``use_memory`` / ``use_tools`` 在 proto 里是**无 presence 的 bool**（proto3
    语义：不设与 ``false`` 不可区分），所以这里不做「缺省则用 Pydantic 默认值」的猜测 ——
    ``false`` 就是 ``false``。Go 网关每次都显式下发这三个开关。

    ``conversation_id`` / ``model`` 等 ``optional`` 字段用 ``HasField`` 判存在，而不是
    ``or None``：``temperature=0``（贪心的合法取值）与「没传」必须区分开。
    """
    kwargs: dict[str, Any] = {
        "query": request.query,
        "conversation_id": request.conversation_id or None,
        "history": [ChatMessage(role=m.role, content=m.content) for m in request.history],
        "use_rag": request.use_rag,
        "kb_ids": list(request.kb_ids),
        "use_memory": request.use_memory,
        "use_tools": request.use_tools,
        # 契约里 ``stream`` 恒为 false：流式走 gRPC 的 ``ChatStream``（M4）。
        "stream": False,
        "metadata": dict(request.metadata),
    }
    if request.HasField("model"):
        kwargs["model"] = request.model or None
    for proto_name, field_name in (
        ("temperature", "temperature"),
        ("top_k", "top_k"),
        ("rerank_top_n", "rerank_top_n"),
        ("score_threshold", "score_threshold"),
    ):
        if request.HasField(proto_name):
            kwargs[field_name] = getattr(request, proto_name)
    return ChatRequest(**kwargs)


def _to_proto_response(result: ChatResponse) -> chat_pb2.ChatResponse:
    """Pydantic → proto。"""
    response = chat_pb2.ChatResponse(
        answer=result.answer,
        message_id=result.message_id,
        finish_reason=result.finish_reason,
        model=result.model,
        degraded=result.degraded,
        degraded_reasons=list(result.degraded_reasons),
        elapsed_ms=result.elapsed_ms,
    )
    if result.conversation_id:
        response.conversation_id = result.conversation_id

    for ref in result.references:
        _fill_reference(response.references.add(), ref)

    for trace in result.tool_calls:
        response.tool_calls.add(
            call_id=trace.call_id,
            name=trace.name,
            # proto 只能装字符串：``arguments`` 是任意 JSON。
            arguments_json=json.dumps(trace.arguments, ensure_ascii=False),
            status=trace.status,
            summary=trace.summary,
            elapsed_ms=trace.elapsed_ms,
        )

    usage = result.usage
    response.usage.prompt_tokens = usage.prompt_tokens
    response.usage.completion_tokens = usage.completion_tokens
    response.usage.total_tokens = usage.total_tokens
    # trace_id 走**尾随元数据**而不进响应体：这样成功与失败两条路径用的是同一个通道，
    # 客户端只有一处实现。
    return response


def _fill_reference(item: chat_pb2.Reference, ref: Reference) -> None:
    """把一条引用写进 proto（非流式与流式**共用**）。

    抽出来是因为它有 ``optional`` 字段的处理：两条路各写一遍的话，某一天加一个 optional 字段
    就只会加到其中一条上，而症状是「同一份引用在流式下少一个字段」。
    """
    item.index = ref.index
    item.chunk_id = ref.chunk_id
    item.doc_id = ref.doc_id
    item.kb_id = ref.kb_id
    item.doc_name = ref.doc_name
    item.score = ref.score
    item.snippet = ref.snippet
    item.content_sha256 = ref.content_sha256
    # ``optional`` 不设时为「无」，与 0 / "" 区分。
    if ref.page is not None:
        item.page = ref.page
    if ref.heading_path:
        item.heading_path = ref.heading_path


class _StreamCursor:
    """``ChatStreamEvent`` → ``ChatEvent`` 的有状态映射。

    「有状态」只为一件事：把 ``prepared.degraded_reasons`` 补进 ``meta`` 事件（那一帧的
    ``degraded`` 只是个布尔，而网关需要原因才能落 ``assistant.degraded_reasons``）。

    事件名不认识时**不报错**：包成 ``unknown`` 原样透传（docs/04 §4.1 要求未知事件 MUST
    透传）。这条分支不是「防御性代码」而是契约的一部分 —— 以后加了新事件类型，老网关也应当能把
    帧转发下去，而不是把整条流弄断。
    """

    __slots__ = ("_degraded_reasons",)

    def __init__(self, degraded_reasons: list[str]) -> None:
        self._degraded_reasons = degraded_reasons

    def map(self, event: ChatStreamEvent) -> chat_pb2.ChatEvent | None:
        """返回要发的消息；``None`` 表示这一条**不该映射**（目前只有 ping）。"""
        kind, data = event.event, event.data

        if kind == EVENT_PING:
            # docs/04 §2.3 的映射表：``ping`` 一栏是「*不映射*」。gRPC 有自己的 keepalive。
            return None

        if kind == EVENT_META:
            meta = chat_pb2.StreamMeta(
                message_id=data.message_id,
                model=data.model,
                created_at=data.created_at,
                degraded=bool(data.degraded or self._degraded_reasons),
                degraded_reasons=list(self._degraded_reasons),
            )
            if data.conversation_id:
                meta.conversation_id = data.conversation_id
            return chat_pb2.ChatEvent(meta=meta)

        if kind == EVENT_REFERENCE:
            payload = chat_pb2.StreamReferences()
            for ref in data.references:
                _fill_reference(payload.references.add(), ref)
            return chat_pb2.ChatEvent(reference=payload)

        if kind == EVENT_TOKEN:
            return chat_pb2.ChatEvent(token=chat_pb2.StreamToken(delta=data.delta))

        if kind == EVENT_TOOL_CALL:
            return chat_pb2.ChatEvent(
                tool_call=chat_pb2.StreamToolCall(
                    call_id=data["call_id"],
                    name=data["name"],
                    # proto 只能装字符串：``arguments`` 是任意 JSON。
                    arguments_json=json.dumps(data.get("arguments") or {}, ensure_ascii=False),
                )
            )

        if kind == EVENT_TOOL_RESULT:
            return chat_pb2.ChatEvent(
                tool_result=chat_pb2.StreamToolResult(
                    call_id=data["call_id"],
                    name=data["name"],
                    status=data["status"],
                    summary=data.get("summary", ""),
                    elapsed_ms=data.get("elapsed_ms", 0),
                )
            )

        if kind == EVENT_USAGE:
            usage = chat_pb2.Usage(
                prompt_tokens=data.prompt_tokens,
                completion_tokens=data.completion_tokens,
                total_tokens=data.total_tokens,
            )
            return chat_pb2.ChatEvent(usage=usage)

        if kind == EVENT_ERROR:
            return chat_pb2.ChatEvent(
                error=chat_pb2.StreamError(
                    code=str(data.get("code", ErrorCode.INTERNAL_ERROR)),
                    message=str(data.get("message", "")),
                    retryable=bool(data.get("retryable", False)),
                )
            )

        if kind == EVENT_DONE:
            return chat_pb2.ChatEvent(
                done=chat_pb2.StreamDone(
                    finish_reason=data.finish_reason,
                    elapsed_ms=data.elapsed_ms,
                    partial=bool(getattr(data, "partial", False)),
                )
            )

        # 未知事件：原样透传。``data_json`` 用 bytes 而不是 str，避免在 protobuf 层做一次
        # UTF-8 校验 —— 网关那边是**原样转发**，它不需要认识这段 JSON。
        try:
            raw = json.dumps(_jsonable(data), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - 只有负载不可序列化时
            raw = b"null"
        return chat_pb2.ChatEvent(unknown=chat_pb2.StreamUnknown(event=kind, data_json=raw))


def _jsonable(data: Any) -> Any:
    """把事件负载转成可 JSON 序列化的形式（Pydantic 模型走 ``model_dump``）。"""
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json")
    return data


# ---------------------------------------------------------------- 启动
def build_grpc_server(application: FastAPI, options: GrpcOptions) -> grpc.aio.Server:
    """构造（但不启动）gRPC 服务。返回对象便于测试读取实际绑定端口。"""
    server = grpc.aio.server(maximum_concurrent_rpcs=options.max_concurrency)
    chat_pb2_grpc.add_AiPlatformServicer_to_server(ChatServicer(application), server)
    return server


async def serve(
    application: FastAPI,
    options: GrpcOptions,
    *,
    ready: asyncio.Event | None = None,
) -> int:
    """在应用 lifespan 内启动 gRPC 服务并阻塞到终止。

    返回实际绑定的端口（``options.port == 0`` 时由系统分配，测试用得上）。``ready`` 会在端口
    绑定之后被 set，供外部（测试/进程管理）等待就绪，而不是靠 sleep 猜。
    """
    server = build_grpc_server(application, options)
    port = server.add_insecure_port(options.address)
    if port == 0:
        raise RuntimeError(f"gRPC 端口绑定失败：{options.address}")

    async with application.router.lifespan_context(application):
        await server.start()
        logger.info(
            "grpc.startup",
            extra={"address": f"{options.host}:{port}", "max_concurrency": options.max_concurrency},
        )
        if ready is not None:
            ready.set()
        try:
            await server.wait_for_termination()
        except asyncio.CancelledError:  # pragma: no cover - 键盘中断路径
            logger.info("grpc.cancelled", extra={"trace_id": get_trace_id()})
        finally:
            # grace：给在飞的 LLM 调用一点收尾时间，但别等到把容器拖死。
            await server.stop(grace=5)
            logger.info("grpc.stopped", extra={"upstream_trace_id": "-"})
    return port


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, server: grpc.aio.Server) -> None:
    """把 SIGINT/SIGTERM 接到优雅停机。

    Windows 的事件循环不支持 ``add_signal_handler``，所以这里**必须**容错：真让它在 Windows 上
    抛 ``NotImplementedError``，本地开发就没法用这个入口。退化成「由 KeyboardInterrupt 冒泡到
    ``asyncio.run``」同样能停，只是少了 SIGTERM 支持。
    """
    # 停机任务必须留住引用：``ensure_future`` 的返回值没人引用时，事件循环只持弱引用，
    # 任务可能在 ``server.stop()`` 执行完之前被 GC —— 表现为「收到信号但服务没停」。
    pending: set[asyncio.Task[None]] = set()

    def _on_signal() -> None:
        logger.info("grpc.signal_received", extra={"upstream_trace_id": "-"})
        task = asyncio.ensure_future(server.stop(grace=5))
        pending.add(task)
        task.add_done_callback(pending.discard)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError, AttributeError):
            continue


def create_grpc_application(settings: Settings | None = None) -> FastAPI:
    """构造应用实例（薄封装，便于入口与测试共用同一条装配路径）。"""
    from app.main import create_app  # 延迟导入：避免 app.main ↔ app.grpc 的循环

    return create_app(settings)


__all__ = [
    "ChatServicer",
    "GrpcOptions",
    "build_grpc_server",
    "create_grpc_application",
    "serve",
]
