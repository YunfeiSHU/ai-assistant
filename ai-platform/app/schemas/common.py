"""通用请求 / 响应契约：错误信封与游标分页（契约见 ``docs/02-接口规范与错误码.md`` §3）。

列表不返回 ``total``：游标分页下算总数要额外全表计数，成本高且与当前页不一致，
需要总数时应走独立统计接口。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    """错误信封的 ``error`` 字段。"""

    code: str = Field(description="业务错误码")
    message: str = Field(description="人类可读信息，可直接展示给用户")
    details: dict[str, Any] = Field(default_factory=dict, description="结构化补充信息")
    trace_id: str = Field(description="排障用 trace id，与日志 / 链路追踪一致")
    retryable: bool = Field(description="客户端能否安全重试")
    retry_after: int | None = Field(default=None, description="建议重试秒数")


class ErrorEnvelope(BaseModel):
    """统一错误响应体。"""

    error: ErrorDetail


class Page[T](BaseModel):
    """游标分页响应。"""

    items: list[T] = Field(default_factory=list, description="当前页数据")
    next_cursor: str | None = Field(default=None, description="下一页游标；null 表示没有下一页")
    has_more: bool = Field(default=False, description="是否还有更多（与 next_cursor 等价）")


class StrictModel(BaseModel):
    """请求体基类：忽略未知字段（``REQ-API-002``）。

    ``extra="ignore"`` 是刻意的：客户端多传字段（如 camelCase 的 ``useRag``）应当被
    忽略并使用默认值而不是 400，否则前端加个埋点字段就会打挂接口。
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


__all__ = ["ErrorDetail", "ErrorEnvelope", "Page", "StrictModel"]
