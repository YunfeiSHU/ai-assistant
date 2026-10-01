"""对话相关的数据模型（契约见 ``docs/03-对话与流式输出.md`` §3 / §4 / §5）。

设计要点：

* 响应体字段名与类型**逐条对照文档**，不做「顺手优化」——前端与验收用例都按文档写。
* 流式事件的负载也定义在这里，保证 SSE 帧与 OpenAPI 描述同源，不会出现
  「文档一个字段名、代码另一个」的漂移。
* 校验器只做**语法级**校验；语义级错误（空 query、``stream=true`` 等）由 service /
  路由层抛 :class:`~app.core.exceptions.AppError`，这样才能返回文档要求的专属错误码
  （``QUERY_EMPTY`` / ``INVALID_ARGUMENT``）。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.core.ids import ULID_PATTERN
from app.schemas.common import StrictModel

#: 资源 ID 正则与 :mod:`app.core.ids` 同源，避免两处硬编码后走偏
_ULID_BODY = ULID_PATTERN.pattern[1:-1]
_CV_PATTERN = rf"cv_{_ULID_BODY}"
_KB_PATTERN = re.compile(rf"kb_{_ULID_BODY}")

ChatRole = Literal["system", "user", "assistant", "tool"]


class ChatMessage(StrictModel):
    """单条对话消息。"""

    role: ChatRole = Field(default="user", description="角色")
    content: str = Field(default="", max_length=32000, description="消息内容")


class Reference(StrictModel):
    """引用来源（结构见 ``docs/06-RAG知识库.md`` §6）。

    ``index`` 与回答正文里的 ``[n]`` 一一对应，前端据此做角标跳转。
    ``snippet`` 取 chunk 前 200 字符、``content_sha256`` 用来做「同一片段」去重高亮。
    """

    index: int = Field(ge=1, description="引用序号，与正文 [n] 对应")
    chunk_id: str = Field(description="片段 ID")
    doc_id: str = Field(description="文档 ID")
    kb_id: str = Field(description="知识库 ID")
    doc_name: str = Field(default="", description="文档显示名")
    page: int | None = Field(default=None, ge=1, description="页码（PDF 有效）")
    heading_path: str | None = Field(default=None, description="Markdown/HTML 标题路径")
    score: float = Field(default=0.0, description="最终排序分")
    snippet: str = Field(default="", description="引用片段预览（前 200 字符）")
    content_sha256: str = Field(default="", description="片段内容哈希，便于前端去重高亮")


class ToolCallTrace(StrictModel):
    """工具调用轨迹（``docs/03`` §3.2）。"""

    call_id: str = Field(description="模型给出的调用 ID")
    name: str = Field(description="工具名（可能含 MCP 命名空间）")
    arguments: dict[str, Any] = Field(default_factory=dict, description="实际执行参数")
    status: Literal["ok", "error", "timeout", "forbidden"] = Field(description="结果状态")
    summary: str = Field(default="", description="结果摘要（≤500 字符）")
    elapsed_ms: int = Field(default=0, ge=0, description="工具耗时")


class Usage(StrictModel):
    """Token 用量。"""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fill_total(self) -> Usage:
        """上游偶尔只给分项不给总数；在这里补齐，保证 ``total = prompt + completion``。

        ``AC-CHAT-01`` 会断言这个恒等式，所以必须在出口处兜住，不能指望上游。
        """
        if self.total_tokens == 0 and (self.prompt_tokens or self.completion_tokens):
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


class ChatRequest(StrictModel):
    """``POST /chat`` 与 ``POST /chat/stream`` 的公共请求体（``docs/03`` §3.1）。"""

    query: str = Field(max_length=8000, description="用户提问（去空白后 1..8000 字符）")
    conversation_id: str | None = Field(
        default=None,
        pattern=_CV_PATTERN,
        description="会话 ID；为空时不落上下文（单轮问答）",
    )
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=200,
        description="客户端自带历史；use_memory=false 时使用",
    )
    use_rag: bool = Field(default=True, description="是否启用知识库检索")
    kb_ids: list[str] = Field(default_factory=list, max_length=10, description="检索范围")
    use_memory: bool = Field(default=True, description="是否读写会话上下文与长期记忆")
    use_tools: bool = Field(default=False, description="是否启用 Agent Loop")
    stream: bool = Field(default=False, description="兼容字段；/chat 只接受 false")
    model: str | None = Field(default=None, description="覆盖默认模型（须在白名单内）")
    temperature: float | None = Field(default=None, ge=0.0, le=2.0, description="覆盖默认温度")
    top_k: int | None = Field(default=None, ge=1, le=100, description="召回数覆盖")
    rerank_top_n: int | None = Field(default=None, ge=1, le=20, description="重排保留数覆盖")
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0, description="重排后最低分")
    metadata: dict[str, str] = Field(default_factory=dict, description="埋点透传（不进 Prompt）")

    @field_validator("kb_ids")
    @classmethod
    def _check_kb_ids(cls, value: list[str]) -> list[str]:
        for kb_id in value:
            if not _KB_PATTERN.fullmatch(kb_id):
                msg = f"kb_id 格式非法：{kb_id}"
                raise ValueError(msg)
        return value

    @field_validator("metadata")
    @classmethod
    def _check_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if len(key) > 64 or len(item) > 64:
                msg = "metadata 的键值长度不得超过 64 字符"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_cross_fields(self) -> ChatRequest:
        if (
            self.rerank_top_n is not None
            and self.top_k is not None
            and self.rerank_top_n > self.top_k
        ):
            msg = "rerank_top_n 不能大于 top_k"
            raise ValueError(msg)
        return self


class ChatResponse(StrictModel):
    """``POST /chat`` 响应（``docs/03`` §3.2）。"""

    answer: str = Field(description="完整回答（Markdown）")
    conversation_id: str | None = Field(default=None, description="会话 ID（服务端可能新建）")
    message_id: str = Field(description="本条回答的 ID")
    references: list[Reference] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = Field(default="stop", description="stop / length / max_steps")
    model: str = Field(default="", description="实际上游模型名")
    degraded: bool = Field(default=False, description="是否发生降级")
    degraded_reasons: list[str] = Field(default_factory=list)
    elapsed_ms: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# 流式事件负载（``docs/03`` §4.2）
# ---------------------------------------------------------------------------


class StreamMeta(StrictModel):
    """``meta`` 首帧。"""

    conversation_id: str | None = None
    message_id: str
    model: str
    created_at: str
    degraded: bool = False


class StreamReferences(StrictModel):
    """``reference`` 帧：一次性推送本轮全部引用。"""

    references: list[Reference] = Field(default_factory=list)


class StreamToken(StrictModel):
    """``token`` 帧：增量文本。"""

    delta: str


class StreamUsage(StrictModel):
    """``usage`` 帧。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class StreamDone(StrictModel):
    """``done`` 末帧。"""

    finish_reason: str = "stop"
    elapsed_ms: int = 0
    partial: bool = False


class ModelInfo(StrictModel):
    """``GET /models`` 的单项（``docs/03`` §5）。"""

    name: str
    provider: str = "unknown"
    supports_tools: bool = False
    supports_stream: bool = True
    context_window: int = 0
    is_default: bool = False


class ModelListResponse(StrictModel):
    """``GET /models`` 响应。"""

    items: list[ModelInfo] = Field(default_factory=list)


__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatRole",
    "ModelInfo",
    "ModelListResponse",
    "Reference",
    "StreamDone",
    "StreamMeta",
    "StreamReferences",
    "StreamToken",
    "StreamUsage",
    "ToolCallTrace",
    "Usage",
]
