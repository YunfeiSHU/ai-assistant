"""脚本化 LLM 替身。

为什么必须有它（``docs/11`` §3 要求「可脚本化并记录请求的 Fake LLM」）：

* ``AC-CHAT-06`` 要断言**发给模型的 messages 顺序**——只有能捕获入参的替身才做得到；
* ``AC-CHAT-09`` 要断言**断连后上游调用被取消**——替身可以记录「有没有被 abort」；
* 让单元/契约测试完全不依赖网络与真实模型，结果稳定且毫秒级。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import AppError, ErrorCode
from app.llm.base import LLMDelta, LLMMessage, LLMResponse, LLMToolCall, LLMUsage


def tool_call(
    name: str,
    arguments: Any = None,
    *,
    call_id: str | None = None,
) -> LLMToolCall:
    """构造一次工具调用的便捷函数（参数可以是 dict 或已经拼好的字符串）。

    测试里写 ``tool_call("calculator", {"expression": "1+1"})`` 比手写
    ``LLMToolCall(call_id="c1", name=..., arguments='{"expression": "1+1"}')``
    易读得多，而且不会因为漏了转义而得到一份「看着对但 JSON 非法」的参数。
    """
    if isinstance(arguments, str):
        raw = arguments
    elif arguments is None:
        raw = "{}"
    else:
        import json

        raw = json.dumps(arguments, ensure_ascii=False)
    return LLMToolCall(call_id=call_id or f"call_{name}", name=name, arguments=raw)


@dataclass
class FakeLLM:
    """按脚本回答的 LLM。"""

    #: 依次取出作为回答；用完后重复最后一条
    replies: list[str] = field(default_factory=lambda: ["好的。"])
    model: str = "fake-flash"
    models: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "name": "fake-flash",
                "provider": "fake",
                "supports_tools": True,
                "supports_stream": True,
                "context_window": 65536,
            },
            {
                "name": "fake-pro",
                "provider": "fake",
                "supports_tools": True,
                "supports_stream": True,
                "context_window": 131072,
            },
        ]
    )
    #: 流式把回答切成几段吐出
    chunks: int = 3
    #: 每段之间的等待，用于观察「逐段到达」与心跳
    delay: float = 0.0
    #: 非流式抛出的异常
    complete_error: BaseException | None = None
    #: 流式在第 N 个 chunk 之后抛出的异常
    stream_error_after: int | None = None
    #: 单次调用返回的 prompt/completion token
    prompt_tokens: int = 100
    completion_tokens: int = 20

    #: 按**调用序号**给出的工具调用剧本：第 N 次调用返回 ``tool_scripts[N]``。
    #: 越界（或为 ``[]``）表示本次不调工具 —— 「先调工具、拿到结果后再回答」
    #: 这类多轮行为靠这个列表表达。
    tool_scripts: list[list[LLMToolCall]] = field(default_factory=list)
    #: 工具调用轮次的 finish_reason（真实上游在要求调工具时给 ``tool_calls``）
    tool_finish_reason: str = "tool_calls"

    #: 记录每次调用收到的 messages（按调用顺序）
    calls: list[list[LLMMessage]] = field(default_factory=list)
    #: 记录每次调用收到的 ``tools`` 参数（None 表示本次未开放工具）
    tools_seen: list[list[dict[str, Any]] | None] = field(default_factory=list)
    #: 被取消的次数（``AC-CHAT-09`` 用它证明上游真的被停了）
    aborts: int = 0

    # ------------------------------------------------------------------
    @property
    def default_model(self) -> str:
        return self.model

    def available_models(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.models]

    def resolve_model(self, requested: str | None) -> str:
        model = requested or self.model
        allowed = {str(item["name"]) for item in self.models}
        if model not in allowed:
            # 与真实适配器保持同一错误结构，避免测试只验证「替身的行为」
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                f"模型 {model} 不在可用列表内",
                {"allowed": sorted(allowed)},
            )
        return model

    def _next_reply(self) -> str:
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[max(index, 0)]

    def _script(self, tools: Any) -> list[LLMToolCall]:
        """本次调用要发起的工具调用（空列表 = 直接回答）。

        ``tools`` 为空时**恒为空列表**：真实模型在没被提供任何工具的情况下不可能
        发起工具调用（``tool_choice`` 不指定时它连工具列表都看不到）。让替身也遵守
        这条，才能用「步数耗尽后的收尾调用不带工具」来验证循环真的收尾了。
        """
        if not tools:
            return []
        index = len(self.calls) - 1
        if 0 <= index < len(self.tool_scripts):
            return list(self.tool_scripts[index])
        return []

    def _record(self, messages: Sequence[LLMMessage], tools: Any) -> None:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools) if tools else None)

    def _usage(self) -> LLMUsage:
        return LLMUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
        )

    # ------------------------------------------------------------------
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
        self._record(messages, tools)
        if self.complete_error is not None:
            raise self.complete_error
        script = self._script(tools)
        return LLMResponse(
            content="" if script else self._next_reply(),
            model=self.resolve_model(model),
            finish_reason=self.tool_finish_reason if script else "stop",
            usage=self._usage(),
            tool_calls=script,
        )

    async def stream(
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
        self._record(messages, tools)
        script = self._script(tools)
        reply = "" if script else self._next_reply()
        pieces = _split(reply, self.chunks)
        completed = False
        try:
            if script:
                # 真实上游的工具调用总在正文之前到达，且此时正文为空
                yield LLMDelta(tool_calls=script, model=self.resolve_model(model))
            for index, piece in enumerate(pieces):
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.stream_error_after is not None and index > self.stream_error_after:
                    raise AppError(ErrorCode.UPSTREAM_LLM_ERROR, "fake upstream failure")
                yield LLMDelta(content=piece, model=self.resolve_model(model))
            yield LLMDelta(
                finish_reason=self.tool_finish_reason if script else "stop",
                model=self.resolve_model(model),
                usage=self._usage(),
            )
            completed = True
        except asyncio.CancelledError:
            raise
        finally:
            if not completed:
                # 无论上游是因为任务取消（CancelledError）还是生成器关闭（GeneratorExit）
                # 被停掉的，都记一次 —— 这正是 REQ-CHAT-006 要断言的事实。
                self.aborts += 1


def _split(text: str, parts: int) -> list[str]:
    """把回答切成 ``parts`` 段；至少保证非空时能切出内容。"""
    if parts <= 1 or not text:
        return [text] if text else []
    size = max(1, len(text) // parts)
    chunks = [text[i : i + size] for i in range(0, len(text), size)]
    return chunks or [text]


__all__ = ["FakeLLM", "tool_call"]
