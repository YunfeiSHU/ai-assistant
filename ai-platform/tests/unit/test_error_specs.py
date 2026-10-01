"""单元测试：错误码规格表与错误信封。"""

from __future__ import annotations

import pytest

from app.core.exceptions import ERROR_SPECS, AppError, ErrorCode

#: docs/02 §5 中列出的、必须存在的错误码
DOCUMENTED_CODES = {
    # 通用
    "INVALID_ARGUMENT",
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "RATE_LIMITED",
    "INTERNAL_ERROR",
    "DEPENDENCY_UNAVAILABLE",
    "OVERLOADED",
    "UPSTREAM_TIMEOUT",
    # 对话 / Agent
    "CONVERSATION_NOT_FOUND",
    "CONTEXT_TOO_LONG",
    "QUERY_EMPTY",
    "UPSTREAM_LLM_ERROR",
    "UPSTREAM_LLM_AUTH_ERROR",
    "CONTENT_FILTERED",
    "AGENT_MAX_STEPS_EXCEEDED",
    "TOOL_NOT_FOUND",
    "TOOL_EXECUTION_FAILED",
    "TOOL_TIMEOUT",
    "TOOL_FORBIDDEN",
    # RAG
    "KB_NOT_FOUND",
    "KB_NAME_CONFLICT",
    "KB_NOT_EMPTY",
    "DOCUMENT_NOT_FOUND",
    "DOCUMENT_DUPLICATE",
    "FILE_TOO_LARGE",
    "UNSUPPORTED_FILE_TYPE",
    "UNPROCESSABLE_DOCUMENT",
    "CHUNK_STRATEGY_INVALID",
    "VECTOR_DIM_MISMATCH",
    "RETRIEVAL_FAILED",
    # Memory
    "MEMORY_NOT_FOUND",
    "SUMMARY_UNAVAILABLE",
    "SUMMARY_GENERATION_FAILED",
    # 任务
    "TASK_NOT_FOUND",
    "TASK_NOT_CANCELABLE",
    "TASK_NOT_RETRYABLE",
    "MQ_UNAVAILABLE",
    # MCP
    "MCP_SERVER_NOT_FOUND",
    "MCP_SERVER_UNAVAILABLE",
    "UPSTREAM_MCP_ERROR",
}


def test_all_documented_codes_exist() -> None:
    """文档里的错误码 MUST 都有实现（防「文档写了但代码没有」）。"""
    implemented = {str(code) for code in ErrorCode}

    assert implemented >= DOCUMENTED_CODES


def test_every_code_has_a_spec() -> None:
    """每个错误码都必须有完整规格，否则 ``to_envelope`` 会 KeyError。"""
    for code in ErrorCode:
        spec = ERROR_SPECS[code]
        assert spec.message, f"{code} 缺少默认文案"
        assert 200 <= spec.status_code < 600
        assert isinstance(spec.retryable, bool)


@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    [
        (ErrorCode.INVALID_ARGUMENT, 400, False),
        (ErrorCode.UNAUTHENTICATED, 401, False),
        (ErrorCode.KB_NOT_FOUND, 404, False),
        (ErrorCode.FILE_TOO_LARGE, 413, False),
        (ErrorCode.UNPROCESSABLE_DOCUMENT, 422, False),
        (ErrorCode.RATE_LIMITED, 429, True),
        (ErrorCode.DEPENDENCY_UNAVAILABLE, 503, True),
        (ErrorCode.UPSTREAM_TIMEOUT, 504, True),
    ],
)
def test_spec_matches_contract_table(code: ErrorCode, status: int, retryable: bool) -> None:
    """抽查若干关键行，确保没有被误改（HTTP 状态码与可重试性是外部契约）。"""
    spec = ERROR_SPECS[code]

    assert spec.status_code == status
    assert spec.retryable is retryable


def test_envelope_contains_required_fields() -> None:
    """``AC-API-04``：信封必备字段齐全。"""
    error = AppError(ErrorCode.KB_NOT_FOUND, details={"kb_id": "kb_1"})
    envelope = error.to_envelope("trace-123")

    assert set(envelope["error"]) == {
        "code",
        "message",
        "details",
        "trace_id",
        "retryable",
    }
    assert envelope["error"]["code"] == "KB_NOT_FOUND"
    assert envelope["error"]["message"] == "知识库不存在或无权访问"
    assert envelope["error"]["trace_id"] == "trace-123"
    assert envelope["error"]["details"] == {"kb_id": "kb_1"}


def test_retry_after_only_for_retryable() -> None:
    """``retry_after`` 只在可重试时出现（否则会误导客户端退避）。"""
    retryable = AppError(ErrorCode.OVERLOADED, retry_after=5)
    not_retryable = AppError(ErrorCode.INVALID_ARGUMENT, retry_after=5)

    assert retryable.to_envelope()["error"]["retry_after"] == 5
    assert "retry_after" not in not_retryable.to_envelope()["error"]


def test_default_message_and_override() -> None:
    """未给文案时用规格表默认值；给了则用调用方的。"""
    assert AppError(ErrorCode.QUERY_EMPTY).message == "提问内容为空"
    assert AppError(ErrorCode.QUERY_EMPTY, "问题不能为空").message == "问题不能为空"


def test_http_status_can_be_overridden() -> None:
    """极少数场景需要覆盖状态码（保持 ``to_envelope`` 与状态码一致）。"""
    error = AppError(ErrorCode.INVALID_ARGUMENT, http_status=413)

    assert error.status_code == 413
