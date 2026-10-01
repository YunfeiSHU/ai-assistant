"""``AppError`` ↔ ``google.rpc.Status`` 的映射。

契约（docs/04 §2 / §4，接缝 J2/J3）：

* ``status.code``     —— 该错误码最贴近的 gRPC 规范码；
* ``status.message``  —— **机器可读的错误码字符串**（``KB_NOT_FOUND`` 等），
  这样非 Go 客户端（grpcurl、Python 客户端）不必解码 detail 就能辨错；
* ``status.details``  —— 一条 :class:`AiError`（承载 code / 人类文案 /
  HTTP 状态码 / 是否可重试 / trace_id / 原始 details JSON）+ 一条
  标准的 :class:`google.rpc.ErrorInfo`（让 ``grpcurl`` 之类的工具能直接读出来）。

**为什么不只用规范码**：gRPC 的码太粗。``UPSTREAM_LLM_ERROR`` 与 ``OVERLOADED``
都会落到 ``UNAVAILABLE``，``CONTEXT_TOO_LONG`` 与 ``QUERY_EMPTY`` 都会落到
``INVALID_ARGUMENT`` —— 网关要按业务码做重试与提示，粗粒度会让
「上下文超长」这种该引导用户的情况被当成「上游抖动」重试。

**为什么不把 details 放进 map**：契约里的 ``details`` 允许嵌套
（``fields`` 是数组、``hint`` 是对象），而 protobuf 的 map 只能装标量；
强行压平成 string 会在中途改变 ``details`` 的形状，
而 docs/02 §4.2 要求它对客户端逐字可见。
"""

from __future__ import annotations

import json
from typing import Any, Final

from google.protobuf import any_pb2
from google.rpc import code_pb2, error_details_pb2, status_pb2

from app.core.exceptions import AppError, ErrorCode
from app.grpc.aiplatform.v1 import chat_pb2

#: 业务错误码 → gRPC 规范码。
#:
#: 分组依据是「调用方该怎么办」，而不是状态码数字：
#: 参数/语义问题 → ``INVALID_ARGUMENT``；不存在 → ``NOT_FOUND``；
#: 状态冲突 → ``ALREADY_EXISTS``/``ABORTED``（网关侧统一还原成 409）；
#: 依赖挂了 → ``UNAVAILABLE``（可重试）；超时 → ``DEADLINE_EXCEEDED``；
#: 过载/限流 → ``RESOURCE_EXHAUSTED``（可重试）。
GRPC_CODE_BY_ERROR: Final[dict[ErrorCode, int]] = {
    # ---- 通用 ----
    ErrorCode.INVALID_ARGUMENT: code_pb2.INVALID_ARGUMENT,
    ErrorCode.UNAUTHENTICATED: code_pb2.UNAUTHENTICATED,
    ErrorCode.PERMISSION_DENIED: code_pb2.PERMISSION_DENIED,
    ErrorCode.RATE_LIMITED: code_pb2.RESOURCE_EXHAUSTED,
    ErrorCode.INTERNAL_ERROR: code_pb2.INTERNAL,
    ErrorCode.DEPENDENCY_UNAVAILABLE: code_pb2.UNAVAILABLE,
    ErrorCode.OVERLOADED: code_pb2.RESOURCE_EXHAUSTED,
    ErrorCode.UPSTREAM_TIMEOUT: code_pb2.DEADLINE_EXCEEDED,
    ErrorCode.CONFLICT: code_pb2.ALREADY_EXISTS,
    ErrorCode.NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.METHOD_NOT_ALLOWED: code_pb2.UNIMPLEMENTED,
    ErrorCode.PAYLOAD_TOO_LARGE: code_pb2.RESOURCE_EXHAUSTED,
    # ---- 对话与 Agent ----
    ErrorCode.CONVERSATION_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.CONTEXT_TOO_LONG: code_pb2.INVALID_ARGUMENT,
    ErrorCode.QUERY_EMPTY: code_pb2.INVALID_ARGUMENT,
    ErrorCode.UPSTREAM_LLM_ERROR: code_pb2.UNAVAILABLE,
    ErrorCode.UPSTREAM_LLM_AUTH_ERROR: code_pb2.UNAUTHENTICATED,
    ErrorCode.CONTENT_FILTERED: code_pb2.INVALID_ARGUMENT,
    # 这个码不作为错误返回（它表达的是 ``finish_reason``），但映射表必须完整，
    # 否则漏掉的码会退化成 ``UNKNOWN``，而 UNKNOWN 会让网关误判成「协议不兼容」。
    ErrorCode.AGENT_MAX_STEPS_EXCEEDED: code_pb2.FAILED_PRECONDITION,
    ErrorCode.TOOL_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.TOOL_EXECUTION_FAILED: code_pb2.UNAVAILABLE,
    ErrorCode.TOOL_TIMEOUT: code_pb2.DEADLINE_EXCEEDED,
    ErrorCode.TOOL_FORBIDDEN: code_pb2.PERMISSION_DENIED,
    # ---- RAG ----
    ErrorCode.KB_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.KB_NAME_CONFLICT: code_pb2.ALREADY_EXISTS,
    ErrorCode.KB_NOT_EMPTY: code_pb2.FAILED_PRECONDITION,
    ErrorCode.KB_LIMIT_EXCEEDED: code_pb2.FAILED_PRECONDITION,
    ErrorCode.KB_DOCUMENT_LIMIT_EXCEEDED: code_pb2.FAILED_PRECONDITION,
    ErrorCode.DOCUMENT_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.DOCUMENT_DUPLICATE: code_pb2.ALREADY_EXISTS,
    ErrorCode.FILE_TOO_LARGE: code_pb2.RESOURCE_EXHAUSTED,
    ErrorCode.UNSUPPORTED_FILE_TYPE: code_pb2.INVALID_ARGUMENT,
    ErrorCode.UNPROCESSABLE_DOCUMENT: code_pb2.INVALID_ARGUMENT,
    ErrorCode.CHUNK_STRATEGY_INVALID: code_pb2.INVALID_ARGUMENT,
    ErrorCode.VECTOR_DIM_MISMATCH: code_pb2.INTERNAL,
    ErrorCode.RETRIEVAL_FAILED: code_pb2.UNAVAILABLE,
    # ---- Memory ----
    ErrorCode.MEMORY_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.SUMMARY_UNAVAILABLE: code_pb2.NOT_FOUND,
    ErrorCode.SUMMARY_GENERATION_FAILED: code_pb2.UNAVAILABLE,
    # ---- 异步任务 ----
    ErrorCode.TASK_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.TASK_NOT_CANCELABLE: code_pb2.FAILED_PRECONDITION,
    ErrorCode.TASK_NOT_RETRYABLE: code_pb2.FAILED_PRECONDITION,
    ErrorCode.TASK_TIMEOUT: code_pb2.DEADLINE_EXCEEDED,
    ErrorCode.MQ_UNAVAILABLE: code_pb2.UNAVAILABLE,
    # ---- MCP ----
    ErrorCode.MCP_SERVER_NOT_FOUND: code_pb2.NOT_FOUND,
    ErrorCode.MCP_SERVER_UNAVAILABLE: code_pb2.UNAVAILABLE,
    ErrorCode.UPSTREAM_MCP_ERROR: code_pb2.UNAVAILABLE,
}


def grpc_code_for(exc: AppError) -> int:
    """取错误码对应的 gRPC 规范码。

    未登记的码退化成 ``INTERNAL`` 而不是 ``UNKNOWN``：``UNKNOWN`` 在 gRPC 里
    常被解读成「服务实现有问题」，会掩盖「只是忘了登记」这个事实。同时
    ``tests`` 里有一条遍历全部 ``ErrorCode`` 的用例，漏登记会直接红灯。
    """
    return GRPC_CODE_BY_ERROR.get(exc.code, code_pb2.INTERNAL)


def to_rpc_status(exc: AppError, trace_id: str) -> status_pb2.Status:
    """把 :class:`AppError` 转成可放进 ``grpc-status-details-bin`` 的状态。"""
    status = status_pb2.Status(
        code=grpc_code_for(exc),
        # 机器可读的错误码字符串，见模块文档。
        message=str(exc.code),
    )
    status.details.append(
        _pack(
            chat_pb2.AiError(
                code=str(exc.code),
                message=exc.message,
                http_status=exc.status_code,
                retryable=exc.retryable,
                trace_id=trace_id,
                details_json=_dumps_details(exc.details),
            )
        )
    )
    status.details.append(
        _pack(
            error_details_pb2.ErrorInfo(
                reason=str(exc.code),
                domain="ai-platform",
                metadata={
                    "trace_id": trace_id,
                    "http_status": str(exc.status_code),
                },
            )
        )
    )
    return status


def _pack(message: Any) -> any_pb2.Any:
    """把消息打包成 :class:`Any`。

    .. warning::
       Python 的 ``Any.Pack`` 是**就地修改**并返回 ``None``（与 Go 的
       ``Any.Pack`` 返回 ``*Any`` 完全不同）。写成 ``append(Any().Pack(m))``
       会把 ``None`` 塞进 repeated 字段，报错信息还是与真因毫不相关的
       ``Expected a message object, but got None``。
    """
    packed = any_pb2.Any()
    packed.Pack(message)
    return packed


def _dumps_details(details: dict[str, Any]) -> str:
    """把 details 序列化成 JSON 字符串。

    ``ensure_ascii=False``：错误文案基本全是中文，转成 ``\\uXXXX`` 之后
    日志与抓包里就完全不可读了 —— 而那正是排障时唯一能看的东西。
    ``default=str``：details 里可能混进 ``datetime``/``Path`` 之类的不可序列化
    对象；整条错误信息因为一个附带字段而构造失败是不划算的。
    """
    if not details:
        return ""
    try:
        return json.dumps(details, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底，正常不会走到
        return ""
