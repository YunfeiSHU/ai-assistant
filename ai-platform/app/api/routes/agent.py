"""Agent 路由（``docs/04`` §4.3）。

`/agent/run` 与 `/agent/run/stream` 的请求体是 ``ChatRequest`` **强制
``use_tools=true``** 并追加 ``max_steps`` / ``allowed_tools`` / ``denied_tools``。
强制在 service 里完成（不依赖调用方传对），这里只负责「先准备再推流」。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import AgentServiceDep, UserId
from app.core.sse import SSE_HEADERS, SSE_MEDIA_TYPE, frame_stream
from app.schemas.agent import AgentRunRequest, AgentRunResponse

logger = logging.getLogger("app.api.agent")

router = APIRouter(tags=["Agent"])


@router.post(
    "/agent/run",
    response_model=AgentRunResponse,
    summary="非流式 Agent 运行",
    description=(
        "执行 Agent Loop（模型自主决定是否调用工具），返回最终回答、"
        "引用、工具调用轨迹与推理轮次。`use_tools` 恒为 true。"
    ),
)
async def run_agent(
    body: AgentRunRequest,
    user_id: UserId,
    service: AgentServiceDep,
) -> AgentRunResponse:
    """非流式 Agent 运行。"""
    return await service.run(body, user_id)


@router.post(
    "/agent/run/stream",
    summary="流式 Agent 运行（SSE）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "事件序列与 /chat/stream 相同，额外保证每个 tool_call 都有"
                "同一 call_id 的 tool_result，且都出现在 done 之前"
            ),
        }
    },
)
async def run_agent_stream(
    body: AgentRunRequest,
    user_id: UserId,
    service: AgentServiceDep,
) -> StreamingResponse:
    """流式 Agent 运行；准备阶段先于 SSE 开始（错误才能变成正常 HTTP 错误码）。"""
    prepared = await service.prepare(body, user_id)
    return StreamingResponse(
        frame_stream(service.stream_prepared(prepared, user_id)),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


__all__ = ["router"]
