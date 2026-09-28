"""健康检查相关的数据模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """``GET /health`` 综合健康响应（兼容旧字段，新增 ``dependencies``）。"""

    status: str = Field(default="ok", description="ok / degraded")
    app: str = Field(description="应用名称")
    env: str = Field(description="运行环境")
    version: str = Field(description="应用版本")
    dependencies: dict[str, Any] = Field(default_factory=dict, description="各依赖连接的探活明细")


class LivenessResponse(BaseModel):
    """``GET /health/live`` 存活探针响应（不检查依赖）。"""

    status: str = Field(default="alive", description="固定为 alive")


class ReadinessResponse(BaseModel):
    """``GET /health/ready`` 就绪探针响应。"""

    status: str = Field(description="ok / degraded")
    checks: dict[str, Any] = Field(default_factory=dict, description="各依赖检查明细")


__all__ = ["HealthResponse", "LivenessResponse", "ReadinessResponse"]
