"""健康检查路由：``/health``、``/health/live``、``/health/ready``。

契约见 ``docs/10-非功能需求与可观测性.md`` §5.4。三个端点的语义刻意分开：

* ``live`` —— **不检查依赖**。判断标准是「进程是否还能响应 HTTP」，
  若在这里探依赖，Milvus 抖动就会导致 K8s 把健康的 Pod 杀掉重启。
* ``ready`` —— 检查依赖。判断标准是「能不能接流量」，失败返回 503。
* ``health`` —— 综合视图，供人看；``dependencies`` 给出排障明细。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response

from app import __version__
from app.api.deps import SettingsDep
from app.core.health import HealthRegistry
from app.schemas.health import HealthResponse, LivenessResponse, ReadinessResponse

router = APIRouter()


def _registry(request: Request) -> HealthRegistry:
    return cast(HealthRegistry, request.app.state.health_registry)


@router.get("", response_model=HealthResponse, summary="综合健康检查")
async def health(request: Request, settings: SettingsDep) -> HealthResponse:
    """返回服务状态与依赖探活明细。"""
    ready, details = await _registry(request).is_ready()
    return HealthResponse(
        status="ok" if ready else "degraded",
        app=settings.app_name,
        env=settings.app_env,
        version=__version__,
        dependencies=details,
    )


@router.get("/live", response_model=LivenessResponse, summary="存活探针")
async def live() -> LivenessResponse:
    """进程存活即返回 200，不检查任何依赖。"""
    return LivenessResponse(status="alive")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="就绪探针",
    responses={503: {"model": ReadinessResponse, "description": "依赖未就绪"}},
)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    """检查必需依赖；任一失败返回 503。"""
    is_ready, checks = await _registry(request).is_ready()
    if not is_ready:
        response.status_code = 503
    return ReadinessResponse(status="ok" if is_ready else "degraded", checks=checks)


__all__ = ["router"]
