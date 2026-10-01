"""SSE 帧构造与响应头（契约见 ``docs/03-对话与流式输出.md`` §4）。

不用 ``sse_starlette``：文档对响应头有硬性要求（``no-transform``、``X-Accel-Buffering: no``），
验收用例还要逐帧比对事件序列。自己拼帧只有几十行，换来两件确定的事：响应头完全可控、
不会随依赖升级漂移；每帧都能单独单测。

``X-Accel-Buffering: no`` 是给 Nginx 看用来关响应缓冲的；整条链路上 MUST NOT 挂 GZip
中间件（``AC-CHAT-04`` 会断言响应里没有 ``Content-Encoding: gzip``）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

#: 事件名（``docs/03`` §4.2 的事件序列）
EVENT_META = "meta"
EVENT_REFERENCE = "reference"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_TOKEN = "token"
EVENT_USAGE = "usage"
EVENT_DONE = "done"
EVENT_ERROR = "error"
#: 保活帧：不在文档的事件序列里，客户端可安全忽略；纯靠 TCP 静默无法被测试观测
EVENT_PING = "ping"

SSE_MEDIA_TYPE = "text/event-stream"

#: 心跳间隔；与 ``docs/03`` §4.4「15s 处收到 ping」一致
PING_INTERVAL_SECONDS = 15.0

#: 除 ``Content-Type`` 外的固定响应头（``Content-Type`` 交给 Starlette 按
#: ``media_type`` 生成，避免与它自带的那份重复）
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _to_payload(data: Any) -> str:
    """把 dict / pydantic 模型 / 字符串统一成一行 JSON。"""
    if isinstance(data, str):
        return data
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def format_frame(event: str, data: Any) -> bytes:
    """拼一个 SSE 帧。

    SSE 的 ``data:`` 字段以换行结尾，所以负载里不能出现裸换行 —— 这里显式拦截，
    早失败总好过前端收到被截断的 JSON。
    """
    payload = _to_payload(data)
    if "\n" in payload or "\r" in payload:
        msg = f"SSE 负载不得包含换行：{payload[:80]!r}"
        raise ValueError(msg)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def ping_frame() -> bytes:
    """保活帧。"""
    return format_frame(EVENT_PING, {"ts": datetime.now(UTC).isoformat()})


async def frame_stream(
    events: AsyncGenerator[Any, None],
    *,
    ping_interval: float = PING_INTERVAL_SECONDS,
) -> AsyncGenerator[bytes, None]:
    """把「事件对象流」转成 SSE 字节流，并夹带保活帧。

    ``events`` 里的元素只需具备 ``event`` 与 ``data`` 两个属性（鸭子类型）。

    中间加队列与 pump 任务的原因：直觉写法 ``await asyncio.wait_for(anext(events), timeout=15)``
    在超时会**取消**那个 ``__anext__``，取消信号被抛进异步生成器内部，生成器随即终结，
    之后再也拿不到任何 token —— 心跳于是变成「静默地把流杀死」。用独立任务把事件推进队列、
    消费端只等队列，取消的才是队列等待而不是生产端。

    客户端断连时，消费端被取消 → ``finally`` 取消 pump → 事件生成器收到 ``CancelledError``
    → 上层据此停止上游 LLM 调用（``REQ-CHAT-006``）。
    """
    queue: asyncio.Queue[tuple[str, Any] | BaseException | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put((event.event, event.data))
        except asyncio.CancelledError:
            raise  # 取消不是「错误」，不要塞给消费端当业务异常处理
        except BaseException as exc:
            await queue.put(exc)
        finally:
            # 显式关闭上游生成器：靠 GC 关闭不可靠，会让「断连后停止上游 LLM 调用」
            # 变成取决于垃圾回收时机的行为，而 REQ-CHAT-006 要求 1s 内停。
            await events.aclose()
            await queue.put(None)

    task = asyncio.create_task(pump(), name="sse-pump")
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=ping_interval)
            except TimeoutError:
                yield ping_frame()
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield format_frame(item[0], item[1])
    finally:
        task.cancel()
        # 刻意不 await：这里可能正处于任务取消 / GeneratorExit 的清理路径上，
        # 在 finally 里 await 会引入 "async generator ignored GeneratorExit" 的风险。
        # pump 的异常会经队列转交给消费端，不会变成「无人认领的任务异常」。


__all__ = [
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_META",
    "EVENT_PING",
    "EVENT_REFERENCE",
    "EVENT_TOKEN",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_USAGE",
    "PING_INTERVAL_SECONDS",
    "SSE_HEADERS",
    "SSE_MEDIA_TYPE",
    "format_frame",
    "frame_stream",
    "ping_frame",
]
