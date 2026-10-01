"""长期记忆与摘要的请求 / 响应契约（``docs/07-Memory.md`` §6）。

两处刻意与通用做法不同：

1. ``DELETE /memories`` 必须显式带 ``all=true``（``AC-MEM-10``），缺参一律 400：
   做成无参可调用等于让一次误点或重试直接抹掉用户数据。
2. ``GET /context`` 返回 ``budget``：对用户无用，但它是唯一能回答「模型为什么没看到
   某条信息」的东西，排障时不必翻服务端日志。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import StrictModel

MemoryKindLiteral = Literal["preference", "fact"]


class MemoryOut(StrictModel):
    """一条长期记忆（``docs/07`` §6 ``/memories``）。"""

    id: str
    content: str
    kind: str = "fact"
    confidence: float = 1.0
    hit_count: int = 1
    source_conversation_id: str | None = None
    expires_at: str | None = None
    expired: bool = False
    created_at: str = ""
    updated_at: str = ""


class MemoryList(StrictModel):
    """记忆列表响应（游标分页，``docs/02`` §3）。"""

    items: list[MemoryOut] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class MemoryCreate(StrictModel):
    """``POST /memories`` 请求体。

    长度上限这里只做宽松兜底（挡住明显异常的巨型请求体），真正的 ``5..500`` 由服务层
    按配置校验 —— 否则「配置改了但 Schema 没改」会让边界在两处不一致。
    """

    content: str = Field(min_length=1, max_length=2000)
    kind: MemoryKindLiteral = "fact"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    expires_at: str | None = None

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        text = value.strip()
        if not text:
            msg = "content 不能为空白"
            raise ValueError(msg)
        return text


class MemoryUpdate(StrictModel):
    """``PATCH /memories/{mem_id}`` 请求体（字段全可选，只改传了的）。

    ``expires_at`` 的「不改」与「清空」靠路由层检查 ``"expires_at" in
    body.model_fields_set`` 区分：``Optional`` 的默认 ``None`` 天然无法区分这两种意图。
    """

    content: str | None = Field(default=None, min_length=1, max_length=2000)
    kind: MemoryKindLiteral | None = None
    expires_at: str | None = None

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            msg = "content 不能为空白"
            raise ValueError(msg)
        return text


class MemorySettingsOut(StrictModel):
    """``GET /memory-settings`` 响应。"""

    memory_enabled: bool = True
    memory_top_n: int = 3
    cleared_at: str | None = None


class MemorySettingsUpdate(StrictModel):
    """``PUT /memory-settings`` 请求体（字段全可选）。"""

    memory_enabled: bool | None = None
    memory_top_n: int | None = Field(default=None, ge=1, le=20)


class ContextMessageOut(StrictModel):
    """``GET /context`` 里的一条消息。"""

    role: str
    message_id: str
    content: str
    tokens: int = 0
    created_at: str = ""
    partial: bool = False


class ContextSummaryOut(StrictModel):
    """上下文里摘要的存在性与覆盖范围。"""

    exists: bool = False
    covered_until: str | None = None
    token_count: int = 0


class ContextBudgetOut(StrictModel):
    """token 占用明细（``docs/07`` §6.1）。"""

    context_token_budget: int = 0
    used: dict[str, int] = Field(default_factory=dict)
    total_tokens: int = 0
    trimmed: dict[str, int] = Field(default_factory=dict)


class ConversationContextOut(StrictModel):
    """``GET /conversations/{id}/context`` 响应。"""

    conversation_id: str
    message_count: int = 0
    messages: list[ContextMessageOut] = Field(default_factory=list)
    summary: ContextSummaryOut = Field(default_factory=ContextSummaryOut)
    budget: ContextBudgetOut = Field(default_factory=ContextBudgetOut)


class SummaryOut(StrictModel):
    """``GET /conversations/{id}/summary`` 响应（四段结构）。"""

    conversation_id: str
    content: str
    covered_until: str = ""
    source_message_count: int = 0
    token_count: int = 0


class SummaryRebuildAccepted(StrictModel):
    """``POST /summary/rebuild`` 的 ``202`` 响应。"""

    task_id: str
    status: str = "queued"


__all__ = [
    "ContextBudgetOut",
    "ContextMessageOut",
    "ContextSummaryOut",
    "ConversationContextOut",
    "MemoryCreate",
    "MemoryList",
    "MemoryOut",
    "MemorySettingsOut",
    "MemorySettingsUpdate",
    "MemoryUpdate",
    "SummaryOut",
    "SummaryRebuildAccepted",
]
