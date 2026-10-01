"""OpenAI 兼容协议的 LLM 适配器。

DeepSeek / Moonshot / 阿里百炼 / 本地 vLLM 都提供 OpenAI 兼容接口，所以**只需要**一个适配器
+ 不同的 ``base_url``（见 ``docs/03`` 与 ``MEMORY: llm-apis.md``）。真正需要各自定制的只有
embedding —— DeepSeek 根本不提供，必须本地跑 BGE。

三件必须由适配器负责的事：

1. **并发闸门**：``llm_max_concurrency`` 限制在途请求数，排队超时即 ``503 OVERLOADED``，
   避免上游限流时把我们自己的线程池/连接池拖垮。
2. **超时**：非流式整体超时；流式则是「首帧超时 + 帧间空闲超时」两段。
3. **模型名校验**：``model_name`` 一律以上游回给的值优先，配置值只做兜底 —— 模型名拼错时
   上游可能静默回退到另一个模型，只看配置是发现不了的。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.infrastructure.observability.metrics import get_metrics
from app.infrastructure.observability.tracing import get_tracing
from app.llm.base import (
    LLMDelta,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    map_llm_exception,
)

logger = logging.getLogger("app.llm")


def _text_of(content: Any) -> str:
    """把 langchain 的 ``str | list[block]`` 内容统一成字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def _usage_of(metadata: Any) -> LLMUsage | None:
    """从 ``usage_metadata`` 提取用量；缺失时返回 ``None`` 而不是全 0。

    区分「没有用量信息」和「用量为 0」很重要：前者要记警告，后者是正常值。
    """
    if not isinstance(metadata, dict):
        return None
    prompt = int(metadata.get("input_tokens") or 0)
    completion = int(metadata.get("output_tokens") or 0)
    total = int(metadata.get("total_tokens") or 0)
    if not (prompt or completion or total):
        return None
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or prompt + completion,
    )


def _tool_calls_of(raw_calls: Any) -> list[LLMToolCall]:
    calls: list[LLMToolCall] = []
    for index, item in enumerate(raw_calls or []):
        if not isinstance(item, dict):
            continue
        arguments = item.get("args")
        calls.append(
            LLMToolCall(
                call_id=str(item.get("id") or f"call_{index}"),
                name=str(item.get("name") or ""),
                arguments=arguments
                if isinstance(arguments, str)
                else json.dumps(arguments or {}, ensure_ascii=False),
            )
        )
    return calls


def _to_lc_message(message: LLMMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    if message.tool_calls:
        # 回注助手轮：少了它，后续 ``role="tool"`` 消息会因为找不到对应的 tool_call_id
        # 被上游拒绝（400）。
        payload["tool_calls"] = [dict(call) for call in message.tool_calls]
    return payload


class _ToolCallAccumulator:
    """按 ``index`` 累积流式工具调用分片。

    上游把一次工具调用切成多个 chunk（``id``/``name`` 只在首片，``arguments`` 逐片拼接，且
    ``index`` 是**本次响应内**的序号）。任何「按到达顺序 append 到列表」的写法都会把一次调用
    拆成多次。``index`` 可能缺失：旧版函数调用 API 只给 ``id``/``name`` 而无索引，此时用
    「已有条数」当作索引 —— 在单次调用的流里这是正确的。
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, Any]] = {}
        #: 最终一次 yield 出去过的索引，避免重复回注同一增量
        self._emitted: set[int] = set()

    def feed(self, raw_calls: Any) -> None:
        for position, item in enumerate(raw_calls or []):
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if not isinstance(index, int):
                index = len(self._calls) if position == 0 else position
            entry = self._calls.setdefault(index, {"id": "", "name": "", "args": ""})
            call_id = item.get("id")
            if isinstance(call_id, str) and call_id:
                entry["id"] = call_id
            name = item.get("name")
            if isinstance(name, str) and name:
                entry["name"] = name
            args = item.get("args")
            if isinstance(args, str):
                entry["args"] += args
            elif args:
                # 部分网关直接给已解析对象（非 OpenAI 规范）
                entry["args"] = json.dumps(args, ensure_ascii=False)

    def deltas(self, *, final: bool) -> list[LLMToolCall]:
        """返回可以安全回注的调用。

        ``final=False`` 时只回注「已经有名字」的条目：没有名字的调用无法执行，提前交给
        Agent Loop 只会让它把一个半成品记进「已调用」集合。
        """
        out: list[LLMToolCall] = []
        for index in sorted(self._calls):
            entry = self._calls[index]
            if not entry["name"]:
                continue
            if not final and index in self._emitted:
                continue
            self._emitted.add(index)
            out.append(
                LLMToolCall(
                    call_id=str(entry["id"] or f"call_{index}"),
                    name=str(entry["name"]),
                    arguments=str(entry["args"] or "{}"),
                )
            )
        return out

    def incomplete(self) -> list[int]:
        """缺名字的索引（流结束时记日志用）。"""
        return sorted(index for index, entry in self._calls.items() if not entry["name"])

    def unparsed(self) -> list[int]:
        """参数不是合法 JSON 的索引（流结束时记日志用）。"""
        bad: list[int] = []
        for index, entry in self._calls.items():
            raw = entry["args"]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                bad.append(index)
                continue
            if not isinstance(parsed, dict):
                bad.append(index)
        return sorted(bad)

    def __bool__(self) -> bool:
        return bool(self._calls)


class OpenAICompatLLM:
    """基于 ``langchain_openai.ChatOpenAI`` 的实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[tuple[str, float, int | None, bool], Any] = {}
        # 注意：``asyncio.Semaphore`` 在 3.10+ 不再于构造时绑定事件循环，
        # 所以这里可以安全地在同步的 __init__ 里创建。
        self._gate = asyncio.Semaphore(max(1, settings.llm_max_concurrency))

    # ------------------------------------------------------------------
    # 对外属性
    # ------------------------------------------------------------------
    @property
    def default_model(self) -> str:
        return self._settings.llm_model

    def available_models(self) -> list[dict[str, Any]]:
        """列出可用模型（``GET /models`` 的数据源）；返回**副本**，调用方改它不影响配置。"""
        return [dict(item) for item in self._settings.llm_models]

    def resolve_model(self, requested: str | None) -> str:
        """把请求里的模型名解析成可用模型；不在白名单内即 ``400``。

        白名单是**静态配置表**（``docs/03`` §5），不做运行期探测：探测会把一个确定性的错误
        变成一个延迟不确定的错误。
        """
        model = requested or self.default_model
        allowed = {str(item.get("name")) for item in self.available_models()}
        if model not in allowed:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                f"模型 {model} 不在可用列表内",
                {"allowed": sorted(allowed)},
            )
        return model

    # ------------------------------------------------------------------
    # 客户端构造
    # ------------------------------------------------------------------
    def _client_for(
        self,
        model: str,
        temperature: float,
        max_tokens: int | None,
        *,
        tools: bool = False,
    ) -> Any:
        """按 (模型, 温度, 上限, 是否带工具) 缓存客户端。

        ``tools`` 进缓存 key 是必要的：带工具与不带工具的调用在部分网关里会落到不同的上游端点
        （如 ``tool_choice`` 的默认值不同），复用同一个缓存的客户端虽然不会报错，但会把两边的
        请求头混在一起。
        """
        key = (model, temperature, max_tokens, tools)
        cached = self._clients.get(key)
        if cached is not None:
            return cached

        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": model,
            "base_url": self._settings.openai_base_url,
            # 本地 vLLM / Ollama 允许任意 key，但 SDK 要求非空
            "api_key": self._settings.openai_api_key or "not-configured",
            "temperature": temperature,
            "timeout": self._settings.llm_timeout_seconds,
            "max_retries": 0,  # 重试策略由上层统一决定，避免双重放大
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        try:
            client = ChatOpenAI(**kwargs, stream_usage=True)
        except TypeError:  # pragma: no cover - 老版本 SDK 没有该参数
            client = ChatOpenAI(**kwargs)
        self._clients[key] = client
        return client

    def _prepare(
        self,
        messages: Sequence[LLMMessage],
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[Any, list[dict[str, Any]], str, float]:
        """返回 ``(可调用客户端, 消息列表, 实际模型名, 实际温度)``。

        带工具时用 ``bind_tools`` 而不是自己拼 ``tools`` 字段：``ainvoke``/``astream`` 接收的是
        **消息列表**而非 OpenAI 请求体，自己拼会被当成一条消息塞进去（不报错，但工具永远不生效）。
        """
        resolved_model = self.resolve_model(model)
        resolved_temperature = (
            self._settings.llm_temperature if temperature is None else temperature
        )
        resolved_max_tokens = self._settings.max_output_tokens if max_tokens is None else max_tokens
        tool_payload = list(tools) if tools else []
        client = self._client_for(
            resolved_model,
            resolved_temperature,
            resolved_max_tokens,
            tools=bool(tool_payload),
        )
        if tool_payload:
            client = client.bind_tools(tool_payload)
        return (
            client,
            [_to_lc_message(m) for m in messages],
            resolved_model,
            resolved_temperature,
        )

    async def _acquire(self, timeout: float) -> None:
        """拿并发槽位；等太久说明整体已经过载，直接 503 而不是排到超时。"""
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise AppError(ErrorCode.OVERLOADED, "模型调用排队超时") from exc

    # ------------------------------------------------------------------
    # 非流式
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
        """一次性补全（``stream=False``）。

        ``model`` / ``temperature`` / ``max_tokens`` 为 ``None`` 时取配置默认值；``timeout``
        缺省取 ``LLM_TIMEOUT_SECONDS``。``tools`` 非空时把工具定义随请求发出，响应里的工具调用
        由上层（Agent 循环）决定如何处理。
        """
        client, payload, resolved_model, _ = self._prepare(
            messages, model, temperature, max_tokens, tools
        )
        budget = timeout or self._settings.llm_timeout_seconds

        await self._acquire(budget)
        try:
            with get_tracing().span("llm.invoke", {"model": resolved_model, "stream": False}):
                raw = await asyncio.wait_for(client.ainvoke(payload), timeout=budget)
        except AppError as exc:
            get_metrics().record_llm_error(model=resolved_model, code=str(exc.code))
            raise
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            error = map_llm_exception(exc)
            get_metrics().record_llm_error(model=resolved_model, code=str(error.code))
            raise error from exc
        finally:
            self._gate.release()

        metadata = getattr(raw, "response_metadata", {}) or {}
        usage = _usage_of(getattr(raw, "usage_metadata", None))
        actual_model = str(metadata.get("model_name") or resolved_model)
        if actual_model != resolved_model:
            # 上游回了个不同的模型名 = 模型名拼错被静默回退，必须留下痕迹
            logger.warning(
                "llm.model_mismatch", extra={"requested": resolved_model, "actual": actual_model}
            )
        if usage is None:
            logger.warning("llm.usage_missing", extra={"model": actual_model})
        else:
            # token 用量是计费依据，不管成功路径还是混合路径都要记
            get_metrics().add_llm_tokens(
                model=actual_model, prompt=usage.prompt_tokens, completion=usage.completion_tokens
            )
        return LLMResponse(
            content=_text_of(getattr(raw, "content", "")),
            model=actual_model,
            finish_reason=str(metadata.get("finish_reason") or "stop"),
            usage=usage or LLMUsage(),
            tool_calls=_tool_calls_of(getattr(raw, "tool_calls", None)),
        )

    # ------------------------------------------------------------------
    # 流式
    # ------------------------------------------------------------------
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
        """流式补全：把上游的增量逐个产出给上层，供"边生成边下发"使用。"""
        client, payload, resolved_model, _ = self._prepare(
            messages, model, temperature, max_tokens, tools
        )
        first_budget = first_token_timeout or self._settings.llm_first_token_timeout_seconds
        idle_budget = idle_timeout or self._settings.llm_timeout_seconds

        await self._acquire(first_budget)
        try:
            async with aclosing(self._raw_stream(client, payload)) as chunks:
                async for delta in self._guarded(chunks, first_budget, idle_budget):
                    if delta.model is None:
                        delta.model = resolved_model
                    # 流式响应的 token 用量通常在**最后一帧**才给（OpenAI 兼容行为），所以在这里
                    # 逐帧看、有就记，而不是等流结束再取
                    if delta.usage is not None:
                        get_metrics().add_llm_tokens(
                            model=delta.model or resolved_model,
                            prompt=delta.usage.prompt_tokens,
                            completion=delta.usage.completion_tokens,
                        )
                    yield delta
        except AppError as exc:
            get_metrics().record_llm_error(model=resolved_model, code=str(exc.code))
            raise
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            error = map_llm_exception(exc)
            get_metrics().record_llm_error(model=resolved_model, code=str(error.code))
            raise error from exc
        finally:
            self._gate.release()

    @staticmethod
    async def _raw_stream(
        client: Any, payload: list[dict[str, Any]]
    ) -> AsyncGenerator[LLMDelta, None]:
        accumulator = _ToolCallAccumulator()
        async for chunk in client.astream(payload):
            metadata = getattr(chunk, "response_metadata", {}) or {}
            accumulator.feed(getattr(chunk, "tool_calls", None))
            yield LLMDelta(
                content=_text_of(getattr(chunk, "content", "")),
                finish_reason=metadata.get("finish_reason") or None,
                model=metadata.get("model_name") or None,
                usage=_usage_of(getattr(chunk, "usage_metadata", None)),
                # 已完整的条目才回注（见 _ToolCallAccumulator.deltas）
                tool_calls=accumulator.deltas(final=False),
            )
        if accumulator:
            # 流结束时的收尾：把最后一版（含拼好的 arguments）回注一次，并记下「缺名字」与
            # 「参数不是 JSON」的索引 —— 它们**不会**报错，只会让模型看到一次莫名其妙的参数
            # 非法，日志是唯一的线索。
            missing = accumulator.incomplete()
            if missing:
                logger.warning("llm.stream_tool_calls_incomplete", extra={"indices": missing})
            unparsed = accumulator.unparsed()
            if unparsed:
                logger.warning("llm.stream_tool_args_unparsed", extra={"indices": unparsed})
            final_calls = accumulator.deltas(final=True)
            if final_calls:
                yield LLMDelta(tool_calls=final_calls)

    @staticmethod
    async def _guarded(
        chunks: AsyncIterator[LLMDelta], first_budget: float, idle_budget: float
    ) -> AsyncGenerator[LLMDelta, None]:
        """给流加两段超时：首帧超时（首字节口径）+ 帧间空闲超时。"""
        iterator = chunks.__aiter__()
        first = True
        while True:
            try:
                delta = await asyncio.wait_for(
                    iterator.__anext__(), timeout=first_budget if first else idle_budget
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                stage = "首字节" if first else "相邻增量"
                raise AppError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    f"模型服务{stage}超时",
                    {"stage": "first_token" if first else "idle"},
                ) from exc
            first = False
            yield delta

    # ------------------------------------------------------------------
    # 启动期自检
    # ------------------------------------------------------------------
    async def verify_model(self) -> str:
        """发一次最小真实调用，确认配置的模型名被上游接受。

        只读配置是发现不了「模型名拼错、上游静默回退」的（见用户经验：换供应商后必须打印
        ``response_metadata["model_name"]``）。失败时**只告警不阻断启动**。
        """
        try:
            response = await self.complete(
                [LLMMessage(role="user", content="ping")],
                max_tokens=1,
            )
        except AppError as exc:
            logger.warning(
                "llm.verify_failed", extra={"code": str(exc.code), "reason": exc.message}
            )
            return ""
        logger.info(
            "llm.verify_ok",
            extra={"requested": self.default_model, "actual": response.model},
        )
        return response.model


__all__ = ["OpenAICompatLLM"]
