"""LLM 协议、数据结构与异常映射（不含任何 SDK 依赖）。

**为什么把异常映射放在这里**：上游 SDK 的错误类型是「实现细节」，而
``UPSTREAM_TIMEOUT`` / ``UPSTREAM_LLM_ERROR`` 是**对外契约**。映射一旦散落在
适配器里，换 SDK 时错误码就会跟着变。集中一处，测试可以直接喂异常对象断言映射结果。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.core.exceptions import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """发给模型的一条消息。

    ``tool_call_id`` / ``name`` 只在 ``role="tool"`` 与工具调用回复时使用。
    ``tool_calls`` 只在回注助手轮时使用，元素是**OpenAI 格式**的
    ``{"id": ..., "function": {"name": ..., "arguments": "<JSON 文本>"}}``。

    为什么用 tuple 而不是 list：这是 frozen+slots 的不可变结构，list 会让「两个
    消息是否相等」依赖可变对象，而这类相等性比较正是测试断言消息序列的手段。
    """

    role: str
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class LLMUsage:
    """Token 用量；上游缺省时保持 0 并由调用方记警告。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class LLMToolCall:
    """一次工具调用请求；``arguments`` 保持**原始 JSON 文本**。

    刻意不做成 ``dict``：不同上游回传的形态不同（有的给 JSON 字符串，有的给已解析
    对象），而**流式**场景下参数是分片下发的（`{"expr` + `ession":"1+1"}`），
    只有保留文本才能在拼接后再解析。解析统一发生在 :mod:`app.tools.executor`。
    """

    call_id: str
    name: str
    arguments: str = "{}"


@dataclass(slots=True)
class LLMResponse:
    """非流式结果。"""

    content: str = ""
    model: str = ""
    finish_reason: str = "stop"
    usage: LLMUsage = field(default_factory=LLMUsage)
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(slots=True)
class LLMDelta:
    """流式增量。"""

    content: str = ""
    finish_reason: str | None = None
    model: str | None = None
    usage: LLMUsage | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@runtime_checkable
class LLMClient(Protocol):
    """对话补全客户端。"""

    @property
    def default_model(self) -> str:
        """配置里的默认模型名。"""
        ...

    def available_models(self) -> list[dict[str, Any]]:
        """白名单模型表（``GET /models`` 的数据来源）。"""
        ...

    def resolve_model(self, requested: str | None) -> str:
        """把请求里的模型名解析为白名单内的模型；非法即 ``400``。"""
        ...

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """一次性返回完整结果。

        Args:
            tools: 上游 ``tools`` 参数（见 :meth:`app.tools.base.ToolSpec.to_upstream`）；
                ``None`` 或空列表表示本次不允许调用工具。
        """
        ...

    def stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        first_token_timeout: float | None = None,
        idle_timeout: float | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMDelta]:
        """按增量返回结果；带工具时工具调用也会以增量形式返回。"""
        ...


def _is_transport_timeout(exc: BaseException) -> bool:
    """是否为底层 HTTP 栈的超时。

    需要同时认 ``httpcore`` 与 ``httpx``：``httpcore.ConnectTimeout`` **不是**
    ``httpx.TimeoutException`` 的子类（反过来才是）。只判 httpx 会把一半的
    连接超时误报成 502，进而让客户端的重试策略跑偏。
    """
    try:
        import httpcore
        import httpx
    except ImportError:  # pragma: no cover - 二者都是硬依赖
        return False
    return isinstance(exc, (httpx.TimeoutException, httpcore.TimeoutException))


def map_llm_exception(exc: BaseException) -> AppError:
    """把上游 SDK 异常翻译成对外错误码。

    优先级从「精确」到「宽泛」：鉴权 → 限流 → 超时 → 连接 → 其它 HTTP 状态。
    未识别的异常统归 ``UPSTREAM_LLM_ERROR``（502，可重试）：对话失败**不能**被
    报成 500，否则监控会把上游问题算到我们头上。
    """
    # ---- 超时类（含 stdlib TimeoutError，asyncio.wait_for 抛的就是它） ----
    if isinstance(exc, TimeoutError):
        return AppError(ErrorCode.UPSTREAM_TIMEOUT, "模型服务响应超时")

    if _is_transport_timeout(exc):
        return AppError(ErrorCode.UPSTREAM_TIMEOUT, "模型服务响应超时")

    try:  # 上游 SDK 可能没装（比如纯本地测试）
        import openai
    except ImportError:  # pragma: no cover - 装了 langchain-openai 就一定有
        return AppError(ErrorCode.UPSTREAM_LLM_ERROR, str(exc) or "模型服务返回错误")

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return AppError(ErrorCode.UPSTREAM_LLM_AUTH_ERROR)
    if isinstance(exc, openai.RateLimitError):
        return AppError(ErrorCode.RATE_LIMITED)
    if isinstance(exc, openai.APITimeoutError):
        return AppError(ErrorCode.UPSTREAM_TIMEOUT)
    if isinstance(exc, openai.APIConnectionError):
        return AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "无法连接模型服务")
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0)
        if status == 429:
            return AppError(ErrorCode.RATE_LIMITED)
        if status in (401, 403):
            return AppError(ErrorCode.UPSTREAM_LLM_AUTH_ERROR)
        if status in (408, 504):
            return AppError(ErrorCode.UPSTREAM_TIMEOUT)
        return AppError(ErrorCode.UPSTREAM_LLM_ERROR, f"模型服务返回 {status}")
    return AppError(ErrorCode.UPSTREAM_LLM_ERROR, str(exc) or "模型服务返回错误")


__all__ = [
    "LLMClient",
    "LLMDelta",
    "LLMMessage",
    "LLMResponse",
    "LLMToolCall",
    "LLMUsage",
    "map_llm_exception",
]
