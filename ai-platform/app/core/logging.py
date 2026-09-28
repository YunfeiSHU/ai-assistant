"""结构化日志与敏感信息脱敏。

契约见 ``docs/10-非功能需求与可观测性.md`` §5.3：

* 单行 JSON（生产）或彩色文本（本地）；
* 必备字段 ``ts`` / ``level`` / ``logger`` / ``msg`` / ``trace_id`` / ``span_id`` / ``service``；
* ``Authorization`` / ``api_key`` / ``token`` / ``password`` 一律脱敏为 ``***``；
* 禁止打印整段 Prompt 或完整文档内容。

实现要点：**脱敏发生在格式化阶段**（而不是写入阶段），因此不管是
``logger.info("token=%s", raw)`` 还是 ``extra={"body": raw}`` 都会被覆盖 —— 只靠
调用方自觉脱敏是不可靠的。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.core.context import get_request_id, get_span_id, get_trace_id, get_user_id

#: 由 LogRecord 自身提供、不应重复输出的属性
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "stacklevel",
        "thread",
        "threadName",
        "taskName",
    }
)

#: 需要脱敏的字段名（小写匹配）
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "openai_api_key",
        "jwt_secret",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "milvus_token",
        "minio_secret_key",
        "headers",
    }
)

_REDACTED = "***"

#: ``sk-xxxxx`` 形式的密钥
_SECRET_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{6,})")
#: ``Bearer xxxxx``
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{6,}")
#: ``key=value`` / ``key: value`` / ``"key": "value"`` 形式
_KEY_VALUE_RE = re.compile(
    r"(?i)\b("
    r"authorization|api[_-]?key|openai_api_key|jwt_secret|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|token|milvus_token|minio_secret_key"
    r")(\"?\s*[:=]\s*\"?)([^\s\"',;}]+)"
)

#: 单条日志字符串字段的最大长度（超出截断，防日志被整段文档撑爆）
MAX_LOG_FIELD_CHARS = 200


def hash_identifier(value: str, pepper: str = "") -> str:
    """对 ``user_id`` 等标识做 HMAC-SHA256 并截断为 16 位 hex。

    规范要求（docs/10 §5.1）``user_id`` MUST NOT 明文外泄；用 pepper 做 HMAC
    而非裸 SHA256，避免攻击者用彩虹表反推小空间 ID。
    """
    if not value:
        return ""
    digest = hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:16]


def redact(text: str) -> str:
    """对自由文本做敏感信息脱敏（供日志与错误响应共用）。"""
    if not text:
        return text
    text = _SECRET_TOKEN_RE.sub(lambda m: m.group(1)[:3] + _REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return text


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """对字典做敏感字段脱敏（递归，``headers`` 整体打码）。"""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
            result[key] = _REDACTED
        elif isinstance(value, dict):
            result[key] = redact_mapping(value)
        elif isinstance(value, list):
            result[key] = [
                redact_mapping(item) if isinstance(item, dict) else item for item in value
            ]
        elif isinstance(value, str):
            result[key] = redact(value)
        else:
            result[key] = value
    return result


def _iso_timestamp(created: float) -> str:
    """``datetime`` → ``2026-09-28T10:00:00.123Z``（毫秒精度、UTC）。"""
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _truncate(value: Any) -> Any:
    """超长字符串截断（保留前后各一半，避免只剩前缀看不清尾因）。"""
    if isinstance(value, str) and len(value) > MAX_LOG_FIELD_CHARS:
        half = MAX_LOG_FIELD_CHARS // 2
        return (
            f"{value[:half]}…[truncated {len(value) - MAX_LOG_FIELD_CHARS} chars]…{value[-half:]}"
        )
    return value


#: 逐请求打 INFO/DEBUG 日志、对本服务无价值的第三方库（按名字前缀匹配）
_NOISY_PREFIXES: frozenset[str] = frozenset(
    {
        "httpx",  # 注意：本环境还装着 httpx2，必须按前缀匹配而不是精确名
        "httpcore",
        "urllib3",
        "openai",  # SDK 自己的请求/响应调试日志与我们的 http.request 完全重复
        "sentence_transformers",
        "transformers",
        "huggingface_hub",
        "filelock",
    }
)


class ModuleNoiseFilter(logging.Filter):
    """低于 WARNING 的噪音库日志一律丢弃。

    两条刻意的设计：

    * 挂在 handler 上而不是只靠 ``setLevel``：前者对**启动之后才创建**的 logger
      同样生效，也能拦住名字带变体（``httpx2``）或绕过层级判断的记录。
    * **只丢 WARNING 以下**：这些库的 ERROR 往往正是我们排查上游故障的唯一线索
      （比如「连接池耗尽」），连错误一起静音会让排障直接失明。
    """

    def __init__(self, prefixes: frozenset[str] = _NOISY_PREFIXES) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        # 用**名字前缀**而不是「顶层包名等值」：本环境就存在 httpx2 这种变体包，
        # 只比对顶层名会漏拦。
        return not record.name.startswith(self._prefixes)


class _ContextMixin:
    """把请求上下文与业务字段注入到记录中。"""

    service: str = "ai-platform"
    pepper: str = ""

    def build_payload(self, record: logging.LogRecord) -> dict[str, Any]:
        """构造日志字典（已脱敏、已截断）。"""
        payload: dict[str, Any] = {
            "ts": _iso_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": _truncate(redact(record.getMessage())),
            "trace_id": get_trace_id(),
            "span_id": get_span_id(),
            "service": self.service,
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        discovered_user = get_user_id()
        if discovered_user and "user_id" not in record.__dict__:
            payload["user_id"] = hash_identifier(discovered_user, self.pepper)

        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key in _SENSITIVE_KEYS:
                payload[key] = _REDACTED
            elif key == "user_id" and isinstance(value, str):
                # 调用方显式传入时同样哈希，杜绝「忘了哈希」这一类漏洞
                payload[key] = hash_identifier(value, self.pepper)
            else:
                payload[key] = _truncate(redact_mapping({key: value})[key])

        if record.exc_info:
            error_type, error_message = _describe_exception(record.exc_info)
            payload["error_type"] = error_type
            payload["error_message"] = _truncate(redact(error_message))
            payload["stack"] = redact(logging.Formatter().formatException(record.exc_info))

        return payload


def _describe_exception(exc_info: Any) -> tuple[str, str]:
    """从 ``sys.exc_info()`` 三元组提取异常类型与消息。"""
    exc_type, exc_value = exc_info[0], exc_info[1]
    return (
        getattr(exc_type, "__name__", "Exception"),
        "" if exc_value is None else str(exc_value),
    )


class JsonFormatter(_ContextMixin, logging.Formatter):
    """单行 JSON 格式化器（生产默认）。"""

    def __init__(self, service: str = "ai-platform", pepper: str = "") -> None:
        super().__init__()
        self.service = service
        self.pepper = pepper

    def format(self, record: logging.LogRecord) -> str:
        payload = self.build_payload(record)
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 兜底，不该发生
            return json.dumps(
                {"ts": payload["ts"], "level": payload["level"], "msg": payload["msg"]},
                ensure_ascii=False,
            )


class ConsoleFormatter(_ContextMixin, logging.Formatter):
    """本地开发用的可读格式化器（保持 key=value，便于 grep）。"""

    _COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "\x1b[36m",
        "INFO": "\x1b[32m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[41m",
    }
    _RESET = "\x1b[0m"

    def __init__(self, service: str = "ai-platform", pepper: str = "") -> None:
        super().__init__()
        self.service = service
        self.pepper = pepper

    def format(self, record: logging.LogRecord) -> str:
        payload = self.build_payload(record)
        level = payload.pop("level")
        ts = payload.pop("ts")
        logger_name = payload.pop("logger")
        message = payload.pop("msg")
        payload.pop("service", None)
        trace_id = payload.pop("trace_id", "-")
        payload.pop("span_id", None)

        color = self._COLORS.get(level, "")
        extras = " ".join(f"{key}={value}" for key, value in payload.items())
        line = f"{ts} {color}{level:<7}{self._RESET} [{trace_id[:8]}] {logger_name}: {message}"
        if extras:
            line = f"{line}  {extras}"
        stack = payload.get("stack")
        if stack:
            line = f"{line}\n{stack}"
        return line


def setup_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    service: str = "ai-platform",
    pepper: str = "",
) -> None:
    """配置根日志器与 uvicorn 日志器（幂等，可重复调用）。

    Args:
        level: 日志级别名，如 ``INFO`` / ``DEBUG``。
        fmt: ``json`` 或 ``console``。
        service: 写入日志的 ``service`` 字段。
        pepper: ``user_id`` 哈希用的 pepper。
    """
    formatter: logging.Formatter = (
        JsonFormatter(service=service, pepper=pepper)
        if fmt == "json"
        else ConsoleFormatter(service=service, pepper=pepper)
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(ModuleNoiseFilter())

    resolved_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved_level)

    # uvicorn 自带 handler 会绕过我们的格式化器，这里统一接管
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        target = logging.getLogger(name)
        for existing in list(target.handlers):
            target.removeHandler(existing)
        target.handlers = [handler]
        target.propagate = False
        target.setLevel(resolved_level)

    for prefix in _NOISY_PREFIXES:
        logging.getLogger(prefix).setLevel(max(resolved_level, logging.WARNING))


def get_logger(name: str) -> logging.Logger:
    """获取业务日志器（统一入口，便于后续替换实现）。"""
    return logging.getLogger(name)


__all__ = [
    "MAX_LOG_FIELD_CHARS",
    "ConsoleFormatter",
    "JsonFormatter",
    "ModuleNoiseFilter",
    "get_logger",
    "hash_identifier",
    "redact",
    "redact_mapping",
    "setup_logging",
]
