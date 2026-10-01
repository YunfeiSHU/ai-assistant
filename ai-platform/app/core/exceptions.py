"""统一错误码与错误信封。

契约见 ``docs/02-接口规范与错误码.md`` §3.3 / §5：错误响应体固定为

.. code-block:: json

    {"error": {"code": ..., "message": ..., "details": {...},
               "trace_id": ..., "retryable": ...}}

**为什么用「枚举 + 规格表」而不是继承体系**：错误码是**对外契约的一部分**，
必须能被机械地列举、比对与快照测试；散落成几十个异常子类后，谁也没法一眼看全。
规格表让「HTTP 状态码 / 是否可重试 / 默认文案」三件事集中在一处，也便于文档同步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """业务错误码（机器可读，对外稳定）。"""

    # ---- 通用 ----
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    OVERLOADED = "OVERLOADED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    CONFLICT = "CONFLICT"
    #: 路由不存在（框架层 404）
    NOT_FOUND = "NOT_FOUND"
    #: 方法不允许（框架层 405）
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    #: JSON 请求体超过中间件上限
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"

    # ---- 对话与 Agent ----
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    CONTEXT_TOO_LONG = "CONTEXT_TOO_LONG"
    QUERY_EMPTY = "QUERY_EMPTY"
    UPSTREAM_LLM_ERROR = "UPSTREAM_LLM_ERROR"
    UPSTREAM_LLM_AUTH_ERROR = "UPSTREAM_LLM_AUTH_ERROR"
    CONTENT_FILTERED = "CONTENT_FILTERED"
    #: 不作为 HTTP 错误返回，而是正常响应里的 ``finish_reason``
    AGENT_MAX_STEPS_EXCEEDED = "AGENT_MAX_STEPS_EXCEEDED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_FORBIDDEN = "TOOL_FORBIDDEN"

    # ---- RAG ----
    KB_NOT_FOUND = "KB_NOT_FOUND"
    KB_NAME_CONFLICT = "KB_NAME_CONFLICT"
    KB_NOT_EMPTY = "KB_NOT_EMPTY"
    KB_LIMIT_EXCEEDED = "KB_LIMIT_EXCEEDED"
    KB_DOCUMENT_LIMIT_EXCEEDED = "KB_DOCUMENT_LIMIT_EXCEEDED"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    DOCUMENT_DUPLICATE = "DOCUMENT_DUPLICATE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    UNPROCESSABLE_DOCUMENT = "UNPROCESSABLE_DOCUMENT"
    CHUNK_STRATEGY_INVALID = "CHUNK_STRATEGY_INVALID"
    VECTOR_DIM_MISMATCH = "VECTOR_DIM_MISMATCH"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"

    # ---- Memory ----
    MEMORY_NOT_FOUND = "MEMORY_NOT_FOUND"
    SUMMARY_UNAVAILABLE = "SUMMARY_UNAVAILABLE"
    SUMMARY_GENERATION_FAILED = "SUMMARY_GENERATION_FAILED"

    # ---- 异步任务 ----
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_NOT_CANCELABLE = "TASK_NOT_CANCELABLE"
    TASK_NOT_RETRYABLE = "TASK_NOT_RETRYABLE"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    MQ_UNAVAILABLE = "MQ_UNAVAILABLE"

    # ---- MCP ----
    MCP_SERVER_NOT_FOUND = "MCP_SERVER_NOT_FOUND"
    MCP_SERVER_UNAVAILABLE = "MCP_SERVER_UNAVAILABLE"
    UPSTREAM_MCP_ERROR = "UPSTREAM_MCP_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """错误码的对外规格。"""

    status_code: int
    retryable: bool
    message: str
    #: ``trace_id`` 之外还会附带的固定提示（如 ``retry_after`` 的语义说明）
    hint: str = ""


#: 错误码总表（docs/02 §5 的机械化表达）
ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    # ---- 通用 ----
    ErrorCode.INVALID_ARGUMENT: ErrorSpec(400, False, "请求参数不合法"),
    ErrorCode.UNAUTHENTICATED: ErrorSpec(401, False, "身份未认证"),
    ErrorCode.PERMISSION_DENIED: ErrorSpec(403, False, "无权访问该资源"),
    ErrorCode.RATE_LIMITED: ErrorSpec(429, True, "请求过于频繁"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, True, "服务内部错误"),
    ErrorCode.DEPENDENCY_UNAVAILABLE: ErrorSpec(503, True, "依赖服务不可用"),
    ErrorCode.OVERLOADED: ErrorSpec(503, True, "服务繁忙"),
    ErrorCode.UPSTREAM_TIMEOUT: ErrorSpec(504, True, "上游服务超时"),
    ErrorCode.CONFLICT: ErrorSpec(409, False, "资源状态冲突"),
    ErrorCode.NOT_FOUND: ErrorSpec(404, False, "请求的资源不存在"),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(405, False, "请求方法不被支持"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(413, False, "请求体过大"),
    # ---- 对话与 Agent ----
    ErrorCode.CONVERSATION_NOT_FOUND: ErrorSpec(404, False, "会话不存在"),
    ErrorCode.CONTEXT_TOO_LONG: ErrorSpec(400, False, "上下文超出模型上限"),
    ErrorCode.QUERY_EMPTY: ErrorSpec(400, False, "提问内容为空"),
    ErrorCode.UPSTREAM_LLM_ERROR: ErrorSpec(502, True, "模型服务返回错误"),
    ErrorCode.UPSTREAM_LLM_AUTH_ERROR: ErrorSpec(502, False, "模型服务鉴权失败"),
    ErrorCode.CONTENT_FILTERED: ErrorSpec(400, False, "内容不合规被拦截"),
    # 注意：这是「正常结果」，HTTP 仍为 200（docs/02 §5.2 脚注）
    ErrorCode.AGENT_MAX_STEPS_EXCEEDED: ErrorSpec(200, False, "已达最大推理步数"),
    ErrorCode.TOOL_NOT_FOUND: ErrorSpec(404, False, "工具不存在"),
    ErrorCode.TOOL_EXECUTION_FAILED: ErrorSpec(502, True, "工具执行失败"),
    ErrorCode.TOOL_TIMEOUT: ErrorSpec(504, True, "工具执行超时"),
    ErrorCode.TOOL_FORBIDDEN: ErrorSpec(403, False, "工具被禁用"),
    # ---- RAG ----
    ErrorCode.KB_NOT_FOUND: ErrorSpec(404, False, "知识库不存在或无权访问"),
    ErrorCode.KB_NAME_CONFLICT: ErrorSpec(409, False, "同名知识库已存在"),
    ErrorCode.KB_NOT_EMPTY: ErrorSpec(409, False, "知识库非空，无法删除"),
    ErrorCode.KB_LIMIT_EXCEEDED: ErrorSpec(409, False, "知识库数量已达上限"),
    ErrorCode.KB_DOCUMENT_LIMIT_EXCEEDED: ErrorSpec(409, False, "知识库文档数已达上限"),
    ErrorCode.DOCUMENT_NOT_FOUND: ErrorSpec(404, False, "文档不存在或无权访问"),
    ErrorCode.DOCUMENT_DUPLICATE: ErrorSpec(409, False, "相同内容的文档已存在"),
    ErrorCode.FILE_TOO_LARGE: ErrorSpec(413, False, "文件超过大小限制"),
    ErrorCode.UNSUPPORTED_FILE_TYPE: ErrorSpec(415, False, "不支持的文件类型"),
    ErrorCode.UNPROCESSABLE_DOCUMENT: ErrorSpec(422, False, "文档无有效文本内容"),
    ErrorCode.CHUNK_STRATEGY_INVALID: ErrorSpec(400, False, "切分参数不合法"),
    ErrorCode.VECTOR_DIM_MISMATCH: ErrorSpec(500, False, "向量维度与集合定义不一致"),
    ErrorCode.RETRIEVAL_FAILED: ErrorSpec(503, True, "检索失败"),
    # ---- Memory ----
    ErrorCode.MEMORY_NOT_FOUND: ErrorSpec(404, False, "记忆不存在或无权访问"),
    ErrorCode.SUMMARY_UNAVAILABLE: ErrorSpec(404, False, "摘要尚未生成"),
    ErrorCode.SUMMARY_GENERATION_FAILED: ErrorSpec(502, True, "摘要生成失败"),
    # ---- 异步任务 ----
    ErrorCode.TASK_NOT_FOUND: ErrorSpec(404, False, "任务不存在或无权访问"),
    ErrorCode.TASK_NOT_CANCELABLE: ErrorSpec(409, False, "当前状态不支持取消"),
    ErrorCode.TASK_NOT_RETRYABLE: ErrorSpec(409, False, "当前状态不支持重试"),
    # 与「用户取消」区分开：超时是**失败**，可重试；取消是用户意图，不重试。
    # 混用会让「任务被取消」与「任务跑超时」在指标与告警里长得一模一样。
    ErrorCode.TASK_TIMEOUT: ErrorSpec(504, True, "任务执行超时"),
    ErrorCode.MQ_UNAVAILABLE: ErrorSpec(503, True, "消息队列不可用"),
    # ---- MCP ----
    ErrorCode.MCP_SERVER_NOT_FOUND: ErrorSpec(404, False, "MCP 服务未配置"),
    ErrorCode.MCP_SERVER_UNAVAILABLE: ErrorSpec(503, True, "MCP 服务不可用"),
    ErrorCode.UPSTREAM_MCP_ERROR: ErrorSpec(502, True, "MCP 调用返回错误"),
}


@dataclass(eq=False)
class AppError(Exception):
    """业务异常：携带错误码、可展示文案与结构化补充信息。

    .. note::
      刻意**不用** ``slots=True``：``dataclass(slots=True)`` 会重建类对象，
       导致 ``__post_init__`` 里的零参 ``super()`` 指向旧类而抛
       ``TypeError: obj must be an instance or subtype of type``。
       异常对象不在热路径上，这点开销不值得换取踩坑风险。

       ``eq=False`` 保持异常的标识语义（默认的按值相等会让两个 ``KB_NOT_FOUND``
       在所有字段相同时相等，容易掩盖测试里的真实差异）。
    """

    code: ErrorCode
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    retry_after: int | None = None
    #: 覆盖默认 HTTP 状态码（极少使用，仅用于特殊语义）
    http_status: int | None = None

    def __post_init__(self) -> None:
        if not self.message:
            self.message = ERROR_SPECS[self.code].message
        super().__init__(self.message)

    # ---- 便捷访问 ----
    @property
    def status_code(self) -> int:
        """该错误对应的 HTTP 状态码。"""
        if self.http_status is not None:
            return self.http_status
        return ERROR_SPECS[self.code].status_code

    @property
    def retryable(self) -> bool:
        """客户端能否安全重试。"""
        return ERROR_SPECS[self.code].retryable

    def to_envelope(self, trace_id: str = "-") -> dict[str, Any]:
        """序列化为统一错误信封（docs/02 §3.3）。"""
        error: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "details": self.details,
            "trace_id": trace_id,
            "retryable": self.retryable,
        }
        if self.retry_after is not None and self.retryable:
            error["retry_after"] = self.retry_after
        return {"error": error}

    def __repr__(self) -> str:
        return (
            f"AppError(code={self.code!r}, message={self.message!r}, "
            f"status={self.status_code}, details={self.details!r})"
        )


def bad_request(message: str = "", **details: Any) -> AppError:
    """构造 ``400 INVALID_ARGUMENT``。"""
    return AppError(ErrorCode.INVALID_ARGUMENT, message, dict(details))


def not_found(code: ErrorCode, message: str = "", **details: Any) -> AppError:
    """构造 ``404`` 类错误（跨用户访问一律走这里，避免暴露资源存在性）。"""
    return AppError(code, message, dict(details))


__all__ = [
    "ERROR_SPECS",
    "AppError",
    "ErrorCode",
    "ErrorSpec",
    "bad_request",
    "not_found",
]
