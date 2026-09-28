"""SSE 帧与心跳的单元测试。

``frame_stream`` 里那个「队列 + pump 任务」的写法不直观，所以必须有测试把
**为什么不能直接用 ``asyncio.wait_for(anext(...))``** 这件事锁住：
一旦有人「简化」回去，``test_ping_does_not_kill_the_stream`` 会立刻失败。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from app.core.sse import format_frame, frame_stream, ping_frame


@dataclass
class _Event:
    event: str
    data: object


async def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [frame async for frame in stream]


def test_format_frame_uses_event_and_single_line_json() -> None:
    frame = format_frame("token", {"delta": "你好"})

    assert frame == 'event: token\ndata: {"delta":"你好"}\n\n'.encode()
    # 中文不转义（避免日志与抓包里满屏 \\uXXXX）
    assert b"\\u" not in frame


def test_format_frame_rejects_newlines_in_payload() -> None:
    """裸换行会让 SSE 帧被截断，必须早失败。"""
    with pytest.raises(ValueError, match="换行"):
        format_frame("token", "line1\nline2")


def test_ping_frame_is_a_real_event() -> None:
    """心跳用真实事件名而不是注释行：这样客户端与测试都能观测到它。"""
    assert ping_frame().startswith(b"event: ping\n")


def test_format_frame_accepts_pydantic_like_objects() -> None:
    class _Model:
        def model_dump(self) -> dict[str, object]:
            return {"delta": "x"}

    assert b'"delta":"x"' in format_frame("token", _Model())


@pytest.mark.asyncio
async def test_frame_stream_preserves_event_order() -> None:
    async def events() -> AsyncIterator[_Event]:
        yield _Event("meta", {"a": 1})
        yield _Event("token", {"delta": "一"})
        yield _Event("token", {"delta": "二"})
        yield _Event("done", {"finish_reason": "stop"})

    frames = await _collect(frame_stream(events(), ping_interval=10))

    assert [frame.split(b"\n", 1)[0] for frame in frames] == [
        b"event: meta",
        b"event: token",
        b"event: token",
        b"event: done",
    ]


@pytest.mark.asyncio
async def test_ping_does_not_kill_the_stream() -> None:
    """上游 0.05s 才吐一个 token，期间必须收到 ping 且**流还在**。

    这是 ``AC-CHAT-10`` 的单元级版本；用等待超时实现心跳的写法会在这里挂掉
    （超时会取消 __anext__ 从而终结异步生成器，后续 token 全部丢失）。
    """
    gate = asyncio.Event()

    async def events() -> AsyncIterator[_Event]:
        yield _Event("meta", {})
        await gate.wait()
        yield _Event("token", {"delta": "late"})
        yield _Event("done", {})

    frames: list[bytes] = []
    stream = frame_stream(events(), ping_interval=0.02)
    async for frame in stream:
        frames.append(frame)
        if frame.startswith(b"event: ping") and len(frames) >= 3:
            gate.set()

    kinds = [frame.split(b"\n", 1)[0] for frame in frames]
    assert b"event: ping" in kinds
    # 关键：打点之后仍然收到了迟到的 token 与 done
    assert kinds[-2:] == [b"event: token", b"event: done"]


@pytest.mark.asyncio
async def test_frame_stream_propagates_producer_error() -> None:
    """生产端异常要原样抛给消费端，而不是变成「流安静地结束」。"""

    async def events() -> AsyncIterator[_Event]:
        yield _Event("meta", {})
        raise RuntimeError("upstream boom")

    with pytest.raises(RuntimeError, match="upstream boom"):
        await _collect(frame_stream(events(), ping_interval=10))
