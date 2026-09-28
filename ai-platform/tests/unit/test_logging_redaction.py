"""单元测试：结构化日志字段与敏感信息脱敏。

覆盖 ``AC-NFR-08``（单行 JSON、必备字段、grep 不到真实密钥）。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    JsonFormatter,
    ModuleNoiseFilter,
    hash_identifier,
    redact,
    redact_mapping,
)


def _record(msg: str, args: tuple = (), **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=args,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_payload_has_required_fields() -> None:
    """``AC-NFR-08``：必备字段 ``ts`` / ``level`` / ``logger`` / ``msg`` / ``trace_id`` / ``span_id``。"""
    payload = json.loads(JsonFormatter(service="ai-platform").format(_record("hello")))

    assert set(payload) >= {"ts", "level", "logger", "msg", "trace_id", "span_id", "service"}
    assert payload["service"] == "ai-platform"
    assert payload["msg"] == "hello"
    assert payload["ts"].endswith("Z")


def test_output_is_single_line() -> None:
    """日志必须是单行（否则 logstash / 日志采集器会解析失败）。"""
    rendered = JsonFormatter().format(_record("第一行\n第二行"))

    assert "\n" not in rendered


def test_business_extras_are_included() -> None:
    """业务字段（``conversation_id`` 等）应被透传，便于排障。"""
    payload = json.loads(
        JsonFormatter().format(
            _record("chat", conversation_id="cv_1", elapsed_ms=42, status_code=200)
        )
    )

    assert payload["conversation_id"] == "cv_1"
    assert payload["elapsed_ms"] == 42
    assert payload["status_code"] == 200


def test_authorization_header_is_masked() -> None:
    """``headers`` 整体打码（``AC-MCP-07`` 要求日志里看不到 ``Authorization`` 原文）。"""
    payload = json.loads(
        JsonFormatter().format(
            _record("mcp", headers={"Authorization": "Bearer super-secret-token"})
        )
    )

    assert payload["headers"] == "***"
    assert "super-secret-token" not in json.dumps(payload)


def test_secret_in_message_is_redacted() -> None:
    """即使调用方直接把密钥拼进消息，也必须在格式化阶段打码。"""
    payload = json.loads(JsonFormatter().format(_record("key=%s", ("sk-abcdef1234567890",))))

    assert "sk-abcdef1234567890" not in json.dumps(payload)
    assert "***" in payload["msg"]


def test_user_id_is_hashed_not_plaintext() -> None:
    """``REQ-NFR-009``：``user_id`` 不得明文出现在日志里。"""
    payload = json.loads(JsonFormatter(pepper="p").format(_record("chat", user_id="u_secret")))

    assert payload["user_id"] != "u_secret"
    assert len(payload["user_id"]) == 16
    assert "u_secret" not in json.dumps(payload)


def test_long_fields_are_truncated() -> None:
    """整段文档 / Prompt 不允许原样进日志（截断到 200 字符）。"""
    payload = json.loads(JsonFormatter().format(_record("doc", content="字" * 2000)))

    assert len(payload["content"]) < 300
    assert "truncated" in payload["content"]


def test_exception_is_structured() -> None:
    """``REQ-NFR-012``：异常 MUST 带结构化堆栈字段。"""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert payload["error_type"] == "ValueError"
    assert payload["error_message"] == "boom"
    assert "ValueError" in payload["stack"]


def test_hash_identifier_is_stable_and_pepper_sensitive() -> None:
    """哈希必须稳定（可跨日志关联），且换 pepper 后不可关联（防反推）。"""
    assert hash_identifier("u_1", "p") == hash_identifier("u_1", "p")
    assert hash_identifier("u_1", "p") != hash_identifier("u_1", "q")
    assert hash_identifier("", "p") == ""


def test_redact_patterns() -> None:
    """常见密钥形态都要被打码。"""
    assert "sk-abcdef123456" not in redact("token=sk-abcdef123456")
    assert "Bearer abc123def456" not in redact("Authorization: Bearer abc123def456")
    assert redact("password=hunter2").endswith("***")


def test_redact_mapping_is_recursive() -> None:
    """嵌套结构里的敏感键也要打码。"""
    cleaned = redact_mapping({"outer": {"api_key": "sk-x", "note": "ok"}})

    assert cleaned["outer"]["api_key"] == "***"
    assert cleaned["outer"]["note"] == "ok"


@pytest.mark.parametrize(
    "logger_name",
    ["httpx", "httpx2", "httpx._client", "httpcore.connection", "urllib3.connectionpool"],
)
def test_noise_filter_drops_third_party_request_logs(logger_name: str) -> None:
    """第三方逐请求日志必须被丢弃。

    ``httpx2`` 这个参数是刻意保留的：本机环境真的装着这个名字的包，
    过滤器若只比对「顶层包名是否等于 httpx」就会漏掉它。这类问题靠看日志是
    发现不了的（只会觉得「日志有点多」），必须写成断言。
    """
    noise_filter = ModuleNoiseFilter()
    record = logging.LogRecord(logger_name, logging.INFO, __file__, 1, "GET /x", (), None)

    assert noise_filter.filter(record) is False


@pytest.mark.parametrize("logger_name", ["app.access", "app.core.health", "uvicorn.error"])
def test_noise_filter_keeps_our_own_logs(logger_name: str) -> None:
    """自家日志不能被误伤。"""
    noise_filter = ModuleNoiseFilter()
    record = logging.LogRecord(logger_name, logging.INFO, __file__, 1, "hello", (), None)

    assert noise_filter.filter(record) is True


@pytest.mark.parametrize("level", [logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_noise_filter_keeps_third_party_errors(level: int) -> None:
    """噪音库的 WARNING 以上必须放行：它们往往是排查上游故障的唯一线索。"""
    noise_filter = ModuleNoiseFilter()
    record = logging.LogRecord("httpx", level, __file__, 1, "connection pool exhausted", (), None)

    assert noise_filter.filter(record) is True


def test_noise_filter_drops_openai_request_debug(caplog: pytest.LogCaptureFixture) -> None:
    """SDK 自己的请求调试日志与我们的 http.request 重复，必须丢。"""
    noise_filter = ModuleNoiseFilter()
    record = logging.LogRecord(
        "openai._base_client", logging.DEBUG, __file__, 1, "HTTP Request: POST", (), None
    )

    assert noise_filter.filter(record) is False
