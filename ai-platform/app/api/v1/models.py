"""模型列表路由（``docs/03`` §5）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import LLMClientDep, UserId
from app.schemas.chat import ModelInfo, ModelListResponse

router = APIRouter(tags=["对话"])


@router.get(
    "/models",
    response_model=ModelListResponse,
    summary="列出可用模型",
    description="静态配置表，不做运行期探测；`is_default` 标识当前默认模型。",
)
async def list_models(user_id: UserId, llm: LLMClientDep) -> ModelListResponse:
    """返回白名单模型及其能力。"""
    default_model = llm.default_model
    items = [
        ModelInfo(
            name=str(item.get("name", "")),
            provider=str(item.get("provider", "unknown")),
            supports_tools=bool(item.get("supports_tools", False)),
            supports_stream=bool(item.get("supports_stream", True)),
            context_window=int(item.get("context_window", 0) or 0),
            is_default=str(item.get("name", "")) == default_model,
        )
        for item in llm.available_models()
    ]
    return ModelListResponse(items=items)


__all__ = ["router"]
