"""对话路由（``docs/03`` §3 / §4）。

流式接口的**关键点**：准备阶段必须先于 ``StreamingResponse`` 完成 ——
HTTP 状态码只有响应头发出之前能改。所以这里先 ``await service.prepare(...)``
（可能抛 400/404/429，由统一异常处理器变成正常错误响应），拿到结果后再开始推帧。

``use_tools=true`` 时这里**分派给 Agent 服务**（``docs/03`` §3.1：走
``docs/04`` 的流程）。放在路由层分派而不是在服务层内部分支，是因为 Agent 的响应
多一个 ``steps``、流式事件多出 ``tool_call``/``tool_result``；混在一个方法里会让
「返回类型取决于入参」扩散到整个服务层。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import AgentServiceDep, ChatServiceDep, UserId
from app.core.errors import AppError, ErrorCode
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, frame_stream
from app.schemas.agent import AgentRunRequest
from app.schemas.chat import ChatRequest, ChatResponse

logger = logging.getLogger("app.api.chat")

router = APIRouter(tags=["对话"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="非流式对话",
    description="一次返回完整答案、引用与用量。`stream=true` 会返回 400，请改用 `/chat/stream`。",
)
async def create_chat(
    body: ChatRequest,
    user_id: UserId,
    service: ChatServiceDep,
    agents: AgentServiceDep,
) -> ChatResponse:
    """非流式对话（``use_tools=true`` 时走 Agent 流程）。"""
    if body.stream:
        # 语义固化：/chat 只接受 stream=false（docs/03 §1），
        # 否则流式与非流式的响应体类型会混淆，OpenAPI 也无法生成两份 schema。
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            "stream=true 请改用 POST /chat/stream",
            {"hint": "/chat/stream"},
        )
    if body.use_tools:
        result = await agents.run(_as_agent_request(body), user_id)
        return ChatResponse(
            answer=result.answer,
            conversation_id=result.conversation_id,
            message_id=result.message_id,
            references=result.references,
            tool_calls=result.tool_calls,
            usage=result.usage,
            finish_reason=result.finish_reason,
            model=result.model,
            degraded=result.degraded,
            degraded_reasons=result.degraded_reasons,
            elapsed_ms=result.elapsed_ms,
        )
    return await service.complete(body, user_id)


@router.post(
    "/stream",
    summary="流式对话（SSE）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "事件序列：meta → reference* → (tool_call/tool_result)* → token* → usage → done",
        }
    },
)
async def create_chat_stream(
    body: ChatRequest,
    user_id: UserId,
    service: ChatServiceDep,
    agents: AgentServiceDep,
) -> StreamingResponse:
    """流式对话；``stream`` 字段在此路径下被忽略（以路径为准）。"""
    if body.use_tools:
        prepared_agent = await agents.prepare(_as_agent_request(body), user_id)
        return StreamingResponse(
            frame_stream(agents.stream_prepared(prepared_agent, user_id)),
            media_type=SSE_MEDIA_TYPE,
            headers=SSE_HEADERS,
        )
    prepared = await service.prepare(body, user_id)
    return StreamingResponse(
        frame_stream(service.stream_prepared(prepared, user_id)),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


def _as_agent_request(body: ChatRequest) -> AgentRunRequest:
    """``ChatRequest`` → ``AgentRunRequest``（``use_tools`` 强制为 true）。

    用 ``model_dump`` 而不是逐字段复制：``ChatRequest`` 以后新增字段时会自动带上，
    漏掉一个字段的表现是「该开关静默失效」——最难发现的一类 bug。
    """
    return AgentRunRequest(**{**body.model_dump(), "use_tools": True})


__all__ = ["router"]
