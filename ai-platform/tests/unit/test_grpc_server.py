"""gRPC 服务端（``AiPlatform.Chat``）单元测试。

测试方式：进程内起一个 ``grpc.aio`` 服务（端口 0 = 由系统分配），
用生成的 stub 打真实调用。**不复用 FastAPI 的 TestClient** ——
gRPC 与 HTTP 是两条独立通道，用 HTTP 的测试夹具会让人误以为
「HTTP 通道测过就等于 gRPC 通道测过」。

服务实例用假实现（只依赖 ``app.state`` 上被 servicer 真正读取的三个键），
这样断言的是 **servicer 自己的映射逻辑**，不会被检索/记忆/LLM 的噪声干扰；
「``create_app`` 是否真的填好那几个键」由另一条用例单独守。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from google.rpc import code_pb2, error_details_pb2, status_pb2

import grpc
from app.application.chat import ChatStreamEvent
from app.core.config import ConfigurationError, Settings
from app.core.exceptions import ERROR_SPECS, AppError, ErrorCode
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
from app.grpc import server as grpc_server
from app.grpc.aiplatform.v1 import chat_pb2, chat_pb2_grpc
from app.schemas.chat import (
    ChatResponse,
    Reference,
    StreamDone,
    StreamMeta,
    StreamReferences,
    StreamToken,
    StreamUsage,
    ToolCallTrace,
    Usage,
)

# ----------------------------------------------------------------------
# 替身与装配
# ----------------------------------------------------------------------


class FakePrepared:
    """``PreparedChat`` 的替身：只带 ``degraded_reasons``。

    ``_prepare_stream`` 只读这一个属性，其余字段（``message_id`` / ``model`` /
    ``rag_chunks`` / ``persist``）在 servicer 这一层用不到 —— 它们在
    ``stream_prepared`` 内部被消费，而那个函数在这里是替身。
    """

    def __init__(self, degraded_reasons: list[str] | None = None) -> None:
        self.degraded_reasons = list(degraded_reasons or [])


class FakeChatService:
    """记录调用参数的对话服务替身。"""

    def __init__(
        self,
        result: ChatResponse | None = None,
        error: Exception | None = None,
        *,
        stream: Any = None,
        prepared_reasons: list[str] | None = None,
        prepare_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[Any, str]] = []
        #: ``stream_prepared`` 的替身：一个**异步生成器函数**
        #: （``async def f(prepared, user_id) -> AsyncIterator[ChatStreamEvent]``）。
        self.stream = stream
        self.prepared_reasons = list(prepared_reasons or [])
        self.prepare_error = prepare_error
        self.prepare_calls: list[tuple[Any, str]] = []
        self.stream_calls: list[tuple[Any, str]] = []

    async def complete(self, request: Any, user_id: str) -> ChatResponse:
        self.calls.append((request, user_id))
        if self.error is not None:
            raise self.error
        assert self.result is not None, "替身未配置返回值"
        return self.result

    async def prepare(self, request: Any, user_id: str) -> FakePrepared:
        self.prepare_calls.append((request, user_id))
        if self.prepare_error is not None:
            raise self.prepare_error
        return FakePrepared(self.prepared_reasons)

    def stream_prepared(self, prepared: Any, user_id: str) -> Any:
        """返回**未开始迭代**的异步生成器。

        刻意不写成 ``async def`` 再 ``yield``：真实实现就是普通函数返回
        异步生成器，写成 ``async def`` 会让调用方拿到协程而不是生成器 ——
        这类形状差异只有靠真实调用才能发现（``async for`` 会报
        ``'coroutine' object is not async iterable``）。
        """
        self.stream_calls.append((prepared, user_id))
        assert self.stream is not None, "替身未配置事件流"
        return self.stream(prepared, user_id)


class FakeAgentService:
    """记录调用参数的 Agent 替身。"""

    def __init__(
        self,
        result: ChatResponse | None = None,
        *,
        stream: Any = None,
        prepared_reasons: list[str] | None = None,
        prepare_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.stream = stream
        self.prepared_reasons = list(prepared_reasons or [])
        self.prepare_error = prepare_error
        self.calls: list[tuple[Any, str]] = []
        self.prepare_calls: list[tuple[Any, str]] = []
        self.stream_calls: list[tuple[Any, str]] = []

    async def run(self, request: Any, user_id: str) -> ChatResponse:
        self.calls.append((request, user_id))
        assert self.result is not None, "替身未配置返回值"
        return self.result

    async def prepare(self, request: Any, user_id: str) -> FakePrepared:
        self.prepare_calls.append((request, user_id))
        if self.prepare_error is not None:
            raise self.prepare_error
        return FakePrepared(self.prepared_reasons)

    def stream_prepared(self, prepared: Any, user_id: str) -> Any:
        self.stream_calls.append((prepared, user_id))
        assert self.stream is not None, "替身未配置事件流"
        return self.stream(prepared, user_id)


def build_servicer_app(
    settings: Settings,
    service: FakeChatService,
    agent: FakeAgentService | None = None,
) -> FastAPI:
    """构造只带 servicer 所需状态的最小应用实例。"""
    application = FastAPI()
    application.state.settings = settings
    application.state.chat_service = service
    if agent is not None:
        application.state.agent_service = agent
    return application


@contextlib.asynccontextmanager
async def grpc_channel(application: FastAPI) -> AsyncIterator[grpc.aio.Channel]:
    """起一个真实的 gRPC 服务并返回连上它的通道。"""
    server = grpc_server.build_grpc_server(application, grpc_server.GrpcOptions())
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0, "端口绑定失败"
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            yield channel
    finally:
        await server.stop(grace=None)


# ----------------------------------------------------------------------
# 测试数据
# ----------------------------------------------------------------------


def sample_response() -> ChatResponse:
    """一个「每个字段都有值」的响应，用来验证映射没有漏字段。"""
    return ChatResponse(
        answer="答案是 42。",
        conversation_id="cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
        message_id="msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3D",
        references=[
            Reference(
                index=1,
                chunk_id="ck_1",
                doc_id="doc_1",
                kb_id="kb_1",
                doc_name="手册.pdf",
                page=3,
                heading_path="第一章 > 1.2",
                # score=0 是合法取值（不是「没有分数」），必须原样传出去。
                score=0.0,
                snippet="片段预览",
                content_sha256="abc123",
            ),
            Reference(
                index=2,
                chunk_id="ck_2",
                doc_id="doc_2",
                kb_id="kb_1",
                doc_name="手册.pdf",
                page=None,
                heading_path=None,
                score=0.87,
                snippet="另一段",
                content_sha256="def456",
            ),
        ],
        tool_calls=[
            ToolCallTrace(
                call_id="call_1",
                name="kb.search",
                arguments={"q": "向量", "top_k": 5},
                status="ok",
                summary="命中 5 条",
                elapsed_ms=37,
            )
        ],
        usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        finish_reason="stop",
        model="deepseek-flash",
        degraded=True,
        degraded_reasons=["rerank_skipped"],
        elapsed_ms=1234,
    )


SAMPLE_KB_ID = "kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3D"

#: ``conversation_id`` 有 ULID 形状校验（``cv_`` 前缀），随便编一个会先撞 400，
#: 症状是「测试红了但看起来像流式实现有问题」。
SAMPLE_CONVERSATION_ID = "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"


def trace_trailer(exc: grpc.aio.AioRpcError) -> dict[str, Any]:
    """从错误里取出尾随元数据。

    .. note::
       aio 的 ``AioRpcError.trailing_metadata()`` 是**同步**方法
       （只有 ``grpc.aio.Call`` 上的同名方法才是协程）。写成 ``await``
       会报 ``object Metadata can't be used in 'await' expression`` ——
       这个错误很容易被误读成「实现没设尾随元数据」。
    """
    return dict(exc.trailing_metadata())


# ----------------------------------------------------------------------
# 正常路径
# ----------------------------------------------------------------------


async def test_chat_maps_every_field(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """AC-ORCH-02 的运行时一半：每个字段都要真的过线。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    service = FakeChatService(result=sample_response())
    application = build_servicer_app(settings, service)
    token = make_token("u_test", settings)

    request = chat_pb2.ChatRequest(
        query="问题",
        conversation_id="cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
        history=[chat_pb2.ChatMessage(role="user", content="上一句")],
        use_rag=True,
        kb_ids=[SAMPLE_KB_ID],
        use_memory=True,
        use_tools=False,
        model="deepseek-flash",
        temperature=0.0,
        top_k=5,
        rerank_top_n=3,
        score_threshold=0.0,
        metadata={"client": "go-gateway"},
    )

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        response = await stub.Chat(
            request,
            metadata=(
                ("authorization", f"Bearer {token}"),
                ("x-trace-id", "trace-from-gateway"),
                ("x-request-id", "req_from_gateway"),
            ),
        )

    # ---- 入参侧 ----
    assert service.calls, "服务未被调用"
    body, user_id = service.calls[0]
    assert user_id == "u_test"
    assert body.query == "问题"
    assert body.conversation_id == "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"
    assert [(m.role, m.content) for m in body.history] == [("user", "上一句")]
    assert body.kb_ids == [SAMPLE_KB_ID]
    assert body.metadata == {"client": "go-gateway"}
    # temperature=0.0 与 score_threshold=0.0 是**合法取值**，必须与「没传」区分：
    # 若实现用 `or None` 判空，它们会变成 None 从而丢失用户的显式选择。
    assert body.temperature == 0.0
    assert body.score_threshold == 0.0
    assert body.top_k == 5
    assert body.rerank_top_n == 3
    assert body.stream is False

    # ---- 出参侧 ----
    assert response.answer == "答案是 42。"
    assert response.conversation_id == "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"
    assert response.message_id == "msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3D"
    assert response.finish_reason == "stop"
    assert response.model == "deepseek-flash"
    assert response.degraded is True
    assert list(response.degraded_reasons) == ["rerank_skipped"]
    assert response.elapsed_ms == 1234
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (10, 20)
    assert response.usage.total_tokens == 30

    assert len(response.references) == 2
    first = response.references[0]
    assert first.index == 1
    assert first.chunk_id == "ck_1"
    assert first.doc_id == "doc_1"
    assert first.kb_id == "kb_1"
    assert first.doc_name == "手册.pdf"
    assert first.snippet == "片段预览"
    assert first.content_sha256 == "abc123"
    # score=0 必须显式在线上；用 0 当「缺省」会让「完全匹配」被当成「没有分数」。
    assert first.score == 0.0
    assert first.HasField("page") and first.page == 3
    assert first.HasField("heading_path") and first.heading_path == "第一章 > 1.2"

    second = response.references[1]
    assert not second.HasField("page"), "page=None 不应出现在线上"
    assert not second.HasField("heading_path"), "heading_path=None 不应出现在线上"
    assert second.score == 0.87

    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.call_id == "call_1"
    assert call.name == "kb.search"
    assert call.status == "ok"
    assert call.summary == "命中 5 条"
    assert call.elapsed_ms == 37
    # arguments 在 proto 里是**字符串**（protobuf 的 map 装不下嵌套 JSON），
    # 这一层负责双向转换；直接丢给 Go 会得到 `"{}"`。
    assert json.loads(call.arguments_json) == {"q": "向量", "top_k": 5}


async def test_chat_routes_to_agent_when_use_tools(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``use_tools=true`` 时走 Agent，与 ``POST /chat`` 的分支一致。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    chat_service = FakeChatService(result=sample_response())
    agent = FakeAgentService(result=sample_response())
    application = build_servicer_app(settings, chat_service, agent)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        await stub.Chat(
            chat_pb2.ChatRequest(query="查一下", use_tools=True),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert not chat_service.calls, "use_tools=true 时不应走普通对话服务"
    assert len(agent.calls) == 1
    _, user_id = agent.calls[0]
    assert user_id == "u_test"


async def test_chat_trace_id_is_echoed_in_trailer(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """接缝 J3：网关给的 trace id 必须回传（出错时才有地方对上日志）。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    application = build_servicer_app(
        settings, FakeChatService(error=AppError(ErrorCode.RETRIEVAL_FAILED))
    )

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q"),
                metadata=(
                    ("authorization", f"Bearer {make_token('u_test', settings)}"),
                    ("x-trace-id", "trace-abc"),
                ),
            )

    # aio 的 trailing_metadata() 是**同步**的（与 Call 上的同名方法不同）。
    assert trace_trailer(info.value).get("x-trace-id") == "trace-abc"


# ----------------------------------------------------------------------
# 错误路径（接缝 J2）
# ----------------------------------------------------------------------


async def test_app_error_becomes_rich_status(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``AppError`` 必须变成带 ``AiError`` 详情的 gRPC 状态。

    这是 Go 侧 ``appErrorFromStatus`` 的唯一数据来源：少了
    ``grpc-status-details-bin``，网关就只能看到一个粗粒度的规范码，
    契约里的 ``code`` / ``details`` / ``http_status`` / ``retryable`` 全部丢失。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    service = FakeChatService(
        error=AppError(
            ErrorCode.KB_NOT_FOUND,
            "知识库不存在",
            {"kb_id": "kb_404", "hint": {"action": "先创建"}},
        )
    )
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q"),
                metadata=(
                    ("authorization", f"Bearer {make_token('u_test', settings)}"),
                    ("x-trace-id", "trace-404"),
                ),
            )

    exc = info.value
    assert exc.code() == grpc.StatusCode.NOT_FOUND
    # message 放业务码：grpcurl 之类的工具只看 status 也能辨错。
    assert exc.details() == "KB_NOT_FOUND"

    trailer = trace_trailer(exc)
    assert "grpc-status-details-bin" in trailer, f"缺少富错误状态，实际键：{sorted(trailer)}"
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    assert status.code == code_pb2.NOT_FOUND
    assert status.message == "KB_NOT_FOUND"

    packed = {detail.type_url.rsplit("/", 1)[-1]: detail for detail in status.details}
    assert "aiplatform.v1.AiError" in packed, f"缺少 AiError，实际：{sorted(packed)}"

    ai_error = chat_pb2.AiError()
    assert packed["aiplatform.v1.AiError"].Unpack(ai_error)
    assert ai_error.code == "KB_NOT_FOUND"
    assert ai_error.message == "知识库不存在"
    assert ai_error.http_status == 404
    assert ai_error.retryable is False
    assert ai_error.trace_id == "trace-404"
    # details 必须**逐字**到达（含嵌套结构）：契约允许 details 里放数组和对象，
    # 任何「压平成 string map」的做法都会在这里露馅。
    assert json.loads(ai_error.details_json) == {
        "kb_id": "kb_404",
        "hint": {"action": "先创建"},
    }

    # 标准 ErrorInfo 也在（只为让 grpcurl 之类工具可读，网关不解析它）。
    assert "google.rpc.ErrorInfo" in packed
    info_detail = error_details_pb2.ErrorInfo()
    assert packed["google.rpc.ErrorInfo"].Unpack(info_detail)
    assert info_detail.reason == "KB_NOT_FOUND"
    assert info_detail.domain == "ai-platform"
    assert info_detail.metadata["trace_id"] == "trace-404"


async def test_invalid_params_become_400_invalid_argument(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """入参非法要与 HTTP 的 400 表现一致。

    用 ``top_k=0``（契约要求 1..100）而**不是** ``query=""``：空 query 在
    schema 层是合法的（由业务层抛 ``QUERY_EMPTY``），拿它当「参数非法」的
    例子会得到一个永远不报错的用例。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    application = build_servicer_app(settings, FakeChatService(result=sample_response()))

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q", top_k=0),
                metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
            )

    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    trailer = trace_trailer(info.value)
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    ai_error = chat_pb2.AiError()
    assert status.details[0].Unpack(ai_error)
    assert ai_error.code == "INVALID_ARGUMENT"
    assert ai_error.http_status == 400
    details = json.loads(ai_error.details_json)
    # 与 app/api/exception_handlers.py 的 400 信封同形（loc/msg/type）。
    assert details["fields"] and set(details["fields"][0]) == {"loc", "msg", "type"}


async def test_chat_requires_credentials(
    make_settings: Callable[..., Settings],
) -> None:
    """没有凭据必须 401 —— 不能悄悄用 debug 身份（生产靠
    ``validate_for_startup`` 保证 ``AUTH_ENABLED=true``）。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    application = build_servicer_app(settings, FakeChatService(result=sample_response()))

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(chat_pb2.ChatRequest(query="q"))

    assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED
    trailer = trace_trailer(info.value)
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    ai_error = chat_pb2.AiError()
    assert status.details[0].Unpack(ai_error)
    assert ai_error.code == "UNAUTHENTICATED"
    assert ai_error.http_status == 401


async def test_chat_uses_debug_user_when_auth_disabled(
    make_settings: Callable[..., Settings],
) -> None:
    """``AUTH_ENABLED=false`` 时认 ``x-debug-user-id``，与 HTTP 通道一致。

    这条是**联调可用性**的关键：两个仓库的 ``.env`` 里 JWT 密钥默认不同，
    若 gRPC 通道不认 debug 身份，「Go 网关 → AI gRPC」在本地根本跑不起来，
    而失败现场只是一个没有任何线索的 401。
    """
    settings = make_settings(auth_enabled=False, debug_user_id="u_dev")
    service = FakeChatService(result=sample_response())
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        await stub.Chat(
            chat_pb2.ChatRequest(query="q"),
            metadata=(("x-debug-user-id", "u_dev_override"),),
        )

    assert service.calls[0][1] == "u_dev_override"


async def test_internal_token_alone_is_not_enough(
    make_settings: Callable[..., Settings],
) -> None:
    """服务间凭据**不能**代替用户身份。

    ``ChatRequest`` 里刻意没有 ``user_id``（网关不许自造身份），所以
    「只有服务间凭据」时应当如实拒绝，而不是随便挑一个用户放行 ——
    后者会变成「任何拿到服务间 token 的人都能读所有用户的知识库」。
    """
    settings = make_settings(
        auth_enabled=True, jwt_secret="test-secret", internal_service_token="s3cr3t"
    )
    application = build_servicer_app(settings, FakeChatService(result=sample_response()))

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q"),
                metadata=(("x-internal-service-token", "s3cr3t"),),
            )

    assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED


async def test_service_internal_validation_error_is_not_400(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """服务内部的 ``ValidationError`` 必须是 500，不能报成 400。

    把「入参校验」与「服务内部校验」混为一谈的后果是：上游返回体不符合
    schema 时，调用方看到的是「你的参数不合法」——排查方向从第一步就错了。
    """
    from pydantic import ValidationError as PydanticValidationError

    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    raw_error = _sample_raw_validation_error()
    assert isinstance(raw_error, PydanticValidationError)
    application = build_servicer_app(settings, FakeChatService(error=raw_error))

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q"),
                metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
            )

    assert info.value.code() == grpc.StatusCode.INTERNAL
    trailer = trace_trailer(info.value)
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    ai_error = chat_pb2.AiError()
    assert status.details[0].Unpack(ai_error)
    assert ai_error.code == "INTERNAL_ERROR"


def _sample_raw_validation_error() -> Exception:
    """构造一个「服务内部抛出的」Pydantic 校验错误。"""
    from app.schemas.chat import ChatResponse

    try:
        ChatResponse(answer="缺少必需的 message_id")
    except Exception as exc:
        return exc
    raise AssertionError("构造校验错误失败")  # pragma: no cover


async def test_unexpected_error_becomes_internal(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """未预期异常必须变成 INTERNAL_ERROR，而不是让 gRPC 自己兜成 UNKNOWN。

    UNKNOWN 在 gRPC 语境里常被解读成「协议不兼容」，会让网关的
    「是否可重试」判断失准。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    application = build_servicer_app(settings, FakeChatService(error=RuntimeError("boom")))

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await stub.Chat(
                chat_pb2.ChatRequest(query="q"),
                metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
            )

    assert info.value.code() == grpc.StatusCode.INTERNAL
    trailer = trace_trailer(info.value)
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    ai_error = chat_pb2.AiError()
    assert status.details[0].Unpack(ai_error)
    assert ai_error.code == "INTERNAL_ERROR"
    assert ai_error.http_status == 500
    # 内部错误可以重试（与 ERROR_SPECS 保持一致）。
    assert ai_error.retryable is True


# ----------------------------------------------------------------------
# 映射表与两条通道的一致性
# ----------------------------------------------------------------------


def test_every_error_code_has_a_grpc_mapping() -> None:
    """``ErrorCode`` 与 ``GRPC_CODE_BY_ERROR`` 必须逐项对齐。

    漏一个的后果是它退化成 ``INTERNAL``/``UNKNOWN``，而重试与提示都会走错分支。
    """
    assert set(grpc_errors.GRPC_CODE_BY_ERROR) == set(ErrorCode), (
        f"未登记：{sorted(set(ErrorCode) - set(grpc_errors.GRPC_CODE_BY_ERROR))}；"
        f"多余：{sorted(set(grpc_errors.GRPC_CODE_BY_ERROR) - set(ErrorCode))}"
    )
    for code, grpc_code in grpc_errors.GRPC_CODE_BY_ERROR.items():
        assert grpc_code in code_pb2.Code.values(), f"{code} 映射到非法码 {grpc_code}"
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_error_specs_and_mapping_agree_on_retryability() -> None:
    """可重试的错误不应该映射成「确定失败」的规范码。

    ``INVALID_ARGUMENT`` / ``NOT_FOUND`` / ``FAILED_PRECONDITION`` 语义上都是
    「别重试了」；把 ``retryable=True`` 的错误映射过去，网关的重试逻辑会
    与 AI 侧的规格表打架。
    """
    non_retryable = {
        code_pb2.INVALID_ARGUMENT,
        code_pb2.NOT_FOUND,
        code_pb2.FAILED_PRECONDITION,
        code_pb2.PERMISSION_DENIED,
        code_pb2.UNAUTHENTICATED,
    }
    for code, spec in ERROR_SPECS.items():
        if spec.retryable and code is not ErrorCode.AGENT_MAX_STEPS_EXCEEDED:
            assert grpc_errors.GRPC_CODE_BY_ERROR[code] not in non_retryable, (
                f"{code} 标为可重试，却映射到不可重试的规范码"
            )


def test_agent_request_mapping_matches_http_layer(
    make_settings: Callable[..., Settings],
) -> None:
    """gRPC 层的 ``_as_agent_request`` 必须与 HTTP 层的同名实现等价。

    两边是同一行逻辑的两份副本（刻意不让传输层依赖 HTTP 路由层），
    这个用例就是防止它们各自演化。
    """
    from app.api.v1.chat import _as_agent_request as http_mapping
    from app.schemas.chat import ChatRequest

    body = ChatRequest(query="查一下", use_tools=False, kb_ids=[SAMPLE_KB_ID], top_k=5)
    assert grpc_server._as_agent_request(body).model_dump() == http_mapping(body).model_dump()
    assert grpc_server._as_agent_request(body).use_tools is True


def test_grpc_status_code_mapping_is_by_name() -> None:
    """``google.rpc.Code`` → ``grpc.StatusCode`` 必须按名字对应。

    两套枚举的数值不一一对应（例如两边 ``UNKNOWN`` 都是 2，但
    ``RESOURCE_EXHAUSTED`` 在 gRPC 里是 8、在 google.rpc 里也是 8 …
    相近但不保证），按数值硬挤会在某个码上静默换语义。
    """
    assert grpc_server._grpc_status_code(code_pb2.NOT_FOUND) is grpc.StatusCode.NOT_FOUND
    assert (
        grpc_server._grpc_status_code(code_pb2.RESOURCE_EXHAUSTED)
        is grpc.StatusCode.RESOURCE_EXHAUSTED
    )
    assert (
        grpc_server._grpc_status_code(code_pb2.DEADLINE_EXCEEDED)
        is grpc.StatusCode.DEADLINE_EXCEEDED
    )
    # 非法数值退化成 UNKNOWN，而不是抛异常把 servicer 打挂。
    assert grpc_server._grpc_status_code(9999) is grpc.StatusCode.UNKNOWN


def test_non_loopback_grpc_without_auth_is_rejected(
    make_settings: Callable[..., Settings],
) -> None:
    """「无鉴权 + 绑到非回环地址」必须在启动期拒绝。"""
    settings = make_settings(grpc_enabled=True, auth_enabled=False, grpc_host="0.0.0.0")
    with pytest.raises(ConfigurationError) as info:
        settings.validate_for_startup()
    assert "GRPC_HOST" in str(info.value)

    # 回环地址 + 无鉴权是本地开发的正常姿势，不能误伤。
    make_settings(
        grpc_enabled=True, auth_enabled=False, grpc_host="127.0.0.1"
    ).validate_for_startup()


def test_grpc_options_follow_settings(make_settings: Callable[..., Settings]) -> None:
    """配置项要真的被用上（否则「改了 .env 但没生效」又要靠猜）。"""
    settings = make_settings(grpc_host="127.0.0.1", grpc_port=50055, grpc_max_concurrency=7)
    options = grpc_server.GrpcOptions.from_settings(settings)
    assert options.address == "127.0.0.1:50055"
    assert options.max_concurrency == 7


def test_create_app_populates_servicer_state(
    make_settings: Callable[..., Settings],
) -> None:
    """``create_app`` 必须真的填好 servicer 依赖的 ``app.state`` 键。

    上面所有用例都用最小应用实例，这条防止「键名写错/少填」这类
    只在真实装配下才暴露的问题（表现是第一次线上调用 500）。
    """
    from app.main import create_app

    application = create_app(make_settings())
    assert application.state.settings is not None
    assert application.state.chat_service is not None
    assert application.state.agent_service is not None


# ----------------------------------------------------------------------
# ChatStream（流式）
# ----------------------------------------------------------------------


def sample_stream_events() -> list[ChatStreamEvent]:
    """一整套事件，覆盖映射表里除 ``ping`` 之外的每一种。

    顺序就是契约顺序（docs/04 §2.3：``reference`` 必须在首个 ``token`` 之前）。
    """
    return [
        ChatStreamEvent(
            EVENT_META,
            StreamMeta(
                conversation_id="cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
                message_id="msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3D",
                model="deepseek-flash",
                created_at="2026-09-30T10:00:00Z",
                degraded=False,
            ),
        ),
        ChatStreamEvent(
            EVENT_REFERENCE,
            StreamReferences(
                references=[
                    Reference(
                        index=1,
                        chunk_id="ck_1",
                        doc_id="doc_1",
                        kb_id="kb_1",
                        doc_name="手册.pdf",
                        page=None,
                        heading_path=None,
                        score=0.0,
                        snippet="片段",
                        content_sha256="abc123",
                    )
                ]
            ),
        ),
        ChatStreamEvent(EVENT_TOKEN, StreamToken(delta="你")),
        # 空 delta 也要发：上游用它表示「心跳/占位」，丢掉会让客户端
        # 的首 token 计时偏晚。
        ChatStreamEvent(EVENT_TOKEN, StreamToken(delta="")),
        ChatStreamEvent(
            EVENT_USAGE, StreamUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        ),
        ChatStreamEvent(EVENT_DONE, StreamDone(finish_reason="stop", elapsed_ms=42, partial=False)),
    ]


async def collect_stream(
    stub: chat_pb2_grpc.AiPlatformStub,
    request: chat_pb2.ChatRequest,
    metadata: tuple[tuple[str, str], ...] = (),
) -> list[chat_pb2.ChatEvent]:
    """把一条流读到自然结束。"""
    messages: list[chat_pb2.ChatEvent] = []
    async for message in stub.ChatStream(request, metadata=metadata):
        messages.append(message)
    return messages


async def test_chat_stream_maps_event_sequence(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """流式的运行时一半：事件种类、顺序、字段都要真的过线。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    service = FakeChatService(
        stream=lambda _prepared, _user_id: _emit(sample_stream_events()),
        prepared_reasons=["rerank_skipped"],
    )
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="问题", conversation_id=SAMPLE_CONVERSATION_ID),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    # 准备阶段先于任何一帧发生（否则 AppError 只能变成 error 帧）。
    assert [uid for _, uid in service.prepare_calls] == ["u_test"]
    assert len(service.stream_calls) == 1

    kinds = [m.WhichOneof("event") for m in messages]
    assert kinds == ["meta", "reference", "token", "token", "usage", "done"]

    meta = messages[0].meta
    assert meta.message_id == "msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3D"
    assert meta.model == "deepseek-flash"
    assert meta.created_at == "2026-09-30T10:00:00Z"
    assert meta.HasField("conversation_id") and meta.conversation_id == SAMPLE_CONVERSATION_ID
    # degraded 布尔与 degraded_reasons **不在同一个地方**：前者给客户端看，
    # 后者只给网关落库（docs/04 §9）。这条断言就是防止有人把 reasons 当成冗余删掉。
    assert meta.degraded is True
    assert list(meta.degraded_reasons) == ["rerank_skipped"]

    ref = messages[1].reference.references[0]
    assert ref.index == 1 and ref.chunk_id == "ck_1" and ref.score == 0.0
    # 发送方丢了 page/heading_path，接收方必须能区分「没发」与「发了 0」。
    assert not ref.HasField("page")
    assert not ref.HasField("heading_path")

    assert [m.token.delta for m in messages[2:4]] == ["你", ""]
    usage = messages[4].usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (1, 2, 3)
    done = messages[5].done
    assert (done.finish_reason, done.elapsed_ms, done.partial) == ("stop", 42, False)


async def test_chat_stream_marks_degraded_when_only_reasons_present(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``meta.degraded`` 与 reasons 可能**不同步**，以 reasons 为准。

    上游 ``StreamMeta.degraded`` 是 ``bool(prepared.degraded_reasons)``；如果
    某天有人改成本地判断，会出现「有 reasons 但 degraded=false」——
    网关据此判断是否降级，两边不一致就会漏掉降级标记。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    meta_event = ChatStreamEvent(
        EVENT_META,
        StreamMeta(
            conversation_id=None,
            message_id="msg_1",
            model="m",
            created_at="t",
            degraded=False,
        ),
    )
    service = FakeChatService(
        stream=lambda _p, _u: _emit([meta_event]), prepared_reasons=["memory_unavailable"]
    )
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert messages[0].meta.degraded is True
    assert list(messages[0].meta.degraded_reasons) == ["memory_unavailable"]
    # conversation_id 为空时**不出现**在线上（网关要能区分「AI 侧说没有会话」
    # 与「AI 侧说的会话 id 就是空串」）。
    assert not messages[0].meta.HasField("conversation_id")


async def test_chat_stream_drops_ping(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``ping`` 是 SSE 传输层的东西，MUST NOT 出现在 gRPC 事件里。

    若实现把它包成 ``unknown`` 透传，网关就会把这条事件原样写成
    ``event: unknown`` 发给浏览器 —— 客户端会看到两路心跳（一路来自网关自己的
    ``ping``，一路来自这个伪装的 unknown），并且事件序列断言会莫名其妙失败。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    events = [
        ChatStreamEvent(EVENT_PING, {"ts": "2026-09-30T10:00:00Z"}),
        ChatStreamEvent(EVENT_TOKEN, StreamToken(delta="x")),
        ChatStreamEvent(EVENT_PING, {"ts": "2026-09-30T10:00:15Z"}),
    ]
    service = FakeChatService(stream=lambda _p, _u: _emit(events))
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert [m.WhichOneof("event") for m in messages] == ["token"]


async def test_chat_stream_passes_unknown_event_through(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """未知事件 MUST 透传、MUST NOT 丢弃（docs/04 §4.1）。

    这条不是「防御性代码」：加新事件类型时，老网关要能把帧转发下去而不是把
    整条流弄断。断言的是**原样**——包括负载里的中文（不能顺手转义成 ASCII）。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    payload = {"quote": "——引用", "n": 3}
    events = [
        ChatStreamEvent("citation_note", payload),
        ChatStreamEvent(EVENT_DONE, StreamDone(finish_reason="stop", elapsed_ms=1)),
    ]
    service = FakeChatService(stream=lambda _p, _u: _emit(events))
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert [m.WhichOneof("event") for m in messages] == ["unknown", "done"]
    unknown = messages[0].unknown
    assert unknown.event == "citation_note"
    # data_json 是 bytes：网关只当它是不透明 JSON 转发，不做 UTF-8 校验。
    assert json.loads(unknown.data_json.decode("utf-8")) == payload
    assert "——引用" in unknown.data_json.decode("utf-8"), "中文不应被转义"


async def test_chat_stream_maps_tool_events(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``use_tools=true`` 时走 Agent，工具事件按 proto 形状映射。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    events = [
        ChatStreamEvent(
            EVENT_TOOL_CALL,
            {"call_id": "call_1", "name": "kb.search", "arguments": {"q": "向量", "top_k": 5}},
        ),
        ChatStreamEvent(
            EVENT_TOOL_RESULT,
            {
                "call_id": "call_1",
                "name": "kb.search",
                "status": "ok",
                "summary": "命中 5 条",
                "elapsed_ms": 37,
            },
        ),
    ]
    chat_service = FakeChatService(stream=lambda _p, _u: _emit(events))
    agent = FakeAgentService(
        stream=lambda _p, _u: _emit(events), prepared_reasons=["mcp_unavailable"]
    )
    application = build_servicer_app(settings, chat_service, agent)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="查一下", use_tools=True),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert not chat_service.prepare_calls, "use_tools=true 时不应走普通对话服务"
    assert len(agent.prepare_calls) == 1
    # 走 Agent 时 ``use_tools`` 被强制为 true（否则 Agent 分支会退回普通对话）。
    assert agent.prepare_calls[0][0].use_tools is True

    call = messages[0].tool_call
    assert (call.call_id, call.name) == ("call_1", "kb.search")
    # arguments 在 proto 里是**字符串**（和 ChatResponse.tool_calls 同一个约定）。
    assert json.loads(call.arguments_json) == {"q": "向量", "top_k": 5}

    result = messages[1].tool_result
    assert (result.call_id, result.name, result.status) == ("call_1", "kb.search", "ok")
    assert (result.summary, result.elapsed_ms) == ("命中 5 条", 37)


async def test_chat_stream_missing_tool_fields_fall_back_to_defaults(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """工具事件的 ``arguments`` / ``summary`` / ``elapsed_ms`` 是可选字段。

    上游只保证 ``call_id``/``name``/``status`` 一定在（见
    ``app/application/chat.py`` 的 tool 事件构造处与 ``ToolCallTrace`` 的默认值）。
    用 ``data["..."]`` 直取会让一次「工具没给耗时」把整条流打断，
    而症状是「偶发地在工具调用后断流」——很难复现的那种。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    events = [
        ChatStreamEvent(EVENT_TOOL_CALL, {"call_id": "c", "name": "n"}),
        ChatStreamEvent(EVENT_TOOL_RESULT, {"call_id": "c", "name": "n", "status": "timeout"}),
    ]
    service = FakeChatService(stream=lambda _p, _u: _emit(events))
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert json.loads(messages[0].tool_call.arguments_json) == {}
    result = messages[1].tool_result
    assert (result.summary, result.elapsed_ms, result.status) == ("", 0, "timeout")


async def test_chat_stream_prepare_error_aborts_before_first_frame(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """准备阶段失败必须走 ``abort``（完整信封），而不是 ``error`` 帧。

    「先 prepare 再 yield」是这条用例存在的唯一理由：一旦开始推事件，
    gRPC status 就已经发出去了，`400 INVALID_ARGUMENT` 只能退化成
    「流里冒出一个 error 帧」，网关也就没法把 HTTP 状态码回给浏览器。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    service = FakeChatService(
        stream=lambda _p, _u: _emit([]),
        prepare_error=AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在"),
    )
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await collect_stream(
                stub,
                chat_pb2.ChatRequest(query="q"),
                metadata=(
                    ("authorization", f"Bearer {make_token('u_test', settings)}"),
                    ("x-trace-id", "trace-xyz"),
                ),
            )

    assert info.value.code() is grpc.StatusCode.NOT_FOUND
    trailer = trace_trailer(info.value)
    assert trailer.get("x-trace-id") == "trace-xyz"
    status = status_pb2.Status.FromString(trailer["grpc-status-details-bin"])
    packed = {detail.type_url.rsplit("/", 1)[-1] for detail in status.details}
    assert {"aiplatform.v1.AiError", "google.rpc.ErrorInfo"} <= packed
    ai_error = chat_pb2.AiError()
    assert status.details[0].Unpack(ai_error)
    assert ai_error.code == str(ErrorCode.KB_NOT_FOUND)
    # 关键：流**一帧都没**发出去（``stream_prepared`` 不该被调用）。
    assert not service.stream_calls


async def test_chat_stream_error_frame_does_not_break_the_stream(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """``error`` 是**帧**不是状态：后面还能继续收到 ``done``。

    上游在「生成到一半失败」时会发 error 帧，然后照常收尾（落 partial）。
    如果 servicer 把它当异常抛出，网关会看到「流断了」，而实际上
    done 帧（带 partial=true）才是它判断「要不要落部分结果」的依据。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    events = [
        ChatStreamEvent(EVENT_TOKEN, StreamToken(delta="半")),
        ChatStreamEvent(
            EVENT_ERROR,
            {
                "code": str(ErrorCode.UPSTREAM_TIMEOUT),
                "message": "上游超时",
                "retryable": True,
            },
        ),
        ChatStreamEvent(EVENT_DONE, StreamDone(finish_reason="", elapsed_ms=5, partial=True)),
    ]
    service = FakeChatService(stream=lambda _p, _u: _emit(events))
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        messages = await collect_stream(
            stub,
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )

    assert [m.WhichOneof("event") for m in messages] == ["token", "error", "done"]
    error = messages[1].error
    assert (error.code, error.message, error.retryable) == (
        str(ErrorCode.UPSTREAM_TIMEOUT),
        "上游超时",
        True,
    )
    assert messages[2].done.partial is True


async def test_chat_stream_requires_credentials(
    make_settings: Callable[..., Settings],
) -> None:
    """鉴权对两条通道必须**同一套**（漏掉时网关会以为内网就不用鉴权）。"""
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    service = FakeChatService(stream=lambda _p, _u: _emit([]))
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await collect_stream(stub, chat_pb2.ChatRequest(query="q"))

    assert info.value.code() is grpc.StatusCode.UNAUTHENTICATED
    assert not service.prepare_calls


async def test_chat_stream_cancellation_reaches_upstream(
    make_settings: Callable[..., Settings], make_token: Callable[..., str]
) -> None:
    """网关断连必须让上游生成器被**关闭**（否则 LLM 会继续跑到底，照常计费）。

    上游生成器只在被 ``aclose``（CancelledError 注入挂起点）时才会执行
    ``finally``，所以这里断言的是「closed 事件真的被置位」，而不是
    「客户端不再收到消息」——后者在实现吞掉 CancelledError 时也成立。
    """
    settings = make_settings(auth_enabled=True, jwt_secret="test-secret")
    started = asyncio.Event()
    closed = asyncio.Event()

    async def blocking_stream(_prepared: Any, _user_id: str) -> AsyncIterator[ChatStreamEvent]:
        started.set()
        try:
            yield ChatStreamEvent(EVENT_TOKEN, StreamToken(delta="半"))
            await asyncio.Event().wait()  # 永不返回，只能被取消
        finally:
            closed.set()

    service = FakeChatService(stream=blocking_stream)
    application = build_servicer_app(settings, service)

    async with grpc_channel(application) as channel:
        stub = chat_pb2_grpc.AiPlatformStub(channel)
        call = stub.ChatStream(
            chat_pb2.ChatRequest(query="q"),
            metadata=(("authorization", f"Bearer {make_token('u_test', settings)}"),),
        )
        first = await call.read()
        assert first.token.delta == "半"
        await asyncio.wait_for(started.wait(), timeout=5)
        call.cancel()
        await asyncio.wait_for(closed.wait(), timeout=5)


async def _emit(events: list[ChatStreamEvent]) -> AsyncIterator[ChatStreamEvent]:
    """把一个事件列表变成异步生成器（替身的事件流）。"""
    for event in events:
        yield event
