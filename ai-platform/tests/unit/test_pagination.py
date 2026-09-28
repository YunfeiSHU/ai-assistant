"""游标分页原语单测（``docs/02`` §7.1）。

这一组测试的存在原因是一次**静默丢数据**的故障：
``app/tasks/store.py`` 用字符串比较游标边界，而游标解出来的是 ``datetime``
（被 ``isoformat()`` 成 ``2026-09-28T09:15:45.604000+00:00``），实体里的时间戳是
``2026-09-28T09:15:45.604Z``。两者表示同一时刻，字符串比较却在第 20 个字符处
按 ``'.' > '+'`` 判出先后 → **同一毫秒创建的任务全部被当成「已翻过」**，
第二页返回空数组，不报错、不告警。

所以这里既测「正常工作」，也专门把「格式不同但时刻相同」当作用例。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.errors import AppError, ErrorCode
from app.core.pagination import (
    cursor_position,
    decode_cursor,
    encode_cursor,
    is_after_cursor,
    parse_stamp,
)

#: ``now_iso()`` 实际产出的形状（毫秒 + ``Z``）。
MILLIS = "2026-09-28T09:15:45.604Z"
#: ``datetime.isoformat()`` 对同一时刻的产出（微秒 + ``+00:00``）。
SAME_MOMENT = datetime(2026, 9, 28, 9, 15, 45, 604000, tzinfo=UTC).isoformat()


def test_parse_stamp_accepts_both_shapes_as_equal() -> None:
    """``...604Z`` 与 ``...604000+00:00`` 必须解析成同一时刻。"""
    assert parse_stamp(MILLIS) == parse_stamp(SAME_MOMENT)


def test_parse_stamp_treats_naive_as_utc() -> None:
    """无时区的 ``datetime`` 按 UTC 解释（否则与游标比较会抛 ``TypeError``）。"""
    assert parse_stamp(datetime(2026, 9, 28, 9, 15, 45)).tzinfo is UTC


def test_parse_stamp_normalizes_offset() -> None:
    """带偏移量的时间戳换算到 UTC 后与等价的 UTC 时刻相等。"""
    shifted = datetime(2026, 9, 28, 17, 15, 45, tzinfo=timezone(timedelta(hours=8)))
    assert parse_stamp(shifted) == parse_stamp(MILLIS.replace(".604Z", ""))


def test_cursor_position_is_datetime_keyed_not_string_keyed() -> None:
    """位置键的第一段必须是 ``datetime``——字符串键就是上面那个 bug 的来源。"""
    moment, resource_id = cursor_position(MILLIS, "task_a")

    assert isinstance(moment, datetime)
    assert resource_id == "task_a"
    # 这就是失败原因：同一时刻的两种写法，字符串比较不相等，datetime 比较相等
    assert MILLIS != SAME_MOMENT
    assert parse_stamp(MILLIS) == parse_stamp(SAME_MOMENT)


def test_is_after_cursor_none_means_everything() -> None:
    """首页（无游标）全部保留。"""
    assert is_after_cursor(MILLIS, "task_a", None) is True


def test_is_after_cursor_keeps_same_millisecond_items() -> None:
    """**回归用例**：与游标同一毫秒、但 ``id`` 更小的记录必须留在下一页。"""
    boundary = decode_cursor(encode_cursor(*cursor_position(MILLIS, "task_b")))

    assert is_after_cursor(MILLIS, "task_a", boundary) is True, "同毫秒 + id 更小 → 下一页"
    assert is_after_cursor(MILLIS, "task_b", boundary) is False, "自己不算「之后」"
    assert is_after_cursor(MILLIS, "task_c", boundary) is False, "同毫秒 + id 更大 → 已翻过"


def test_is_after_cursor_across_timestamps() -> None:
    """跨时间戳时按时刻先后判断。"""
    boundary = decode_cursor(encode_cursor(*cursor_position(MILLIS, "task_z")))

    assert is_after_cursor("2026-09-28T09:15:45.603Z", "task_z", boundary) is True
    assert is_after_cursor("2026-09-28T09:15:45.605Z", "task_a", boundary) is False


def test_cursor_roundtrip_is_opaque_and_stable() -> None:
    """游标是 base64url 且不含 ``=|`` 等可读结构；同输入同输出。"""
    cursor = encode_cursor(*cursor_position(MILLIS, "task_a"))

    assert "=" not in cursor and "|" not in cursor and "." not in cursor
    assert cursor == encode_cursor(*cursor_position(MILLIS, "task_a"))
    assert decode_cursor(cursor) == cursor_position(MILLIS, "task_a")


def test_encode_cursor_accepts_datetime_and_str() -> None:
    """``encode_cursor`` 两种入参都要能吃（历史调用点格式不一）。"""
    assert encode_cursor(parse_stamp(MILLIS), "task_a") == encode_cursor(MILLIS, "task_a")


def test_encode_cursor_treats_naive_datetime_as_utc() -> None:
    """朴素 ``datetime`` 入参按 UTC 编码，而不是抛 ``ValueError``。"""
    naive = datetime(2026, 9, 28, 9, 15, 45, 604000)
    assert decode_cursor(encode_cursor(naive, "task_a")) == cursor_position(MILLIS, "task_a")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-base64!!",
        "aGVsbG8",  # 解出来没有分隔符
        "fA",  # 只有分隔符，两边都空
        "MjAyNi0wOS0yOA",  # 有时间戳但没有 id
        "Zm9vfA",  # 有 id 但没有时间戳
    ],
)
def test_decode_cursor_rejects_garbage(bad: str) -> None:
    """坏游标一律 ``400 INVALID_ARGUMENT``（不能让 base64/时间解析异常漏成 500）。"""
    with pytest.raises(AppError) as excinfo:
        decode_cursor(bad)

    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT


def test_decode_cursor_rejects_unparseable_stamp() -> None:
    """结构正确但时间戳不是时间 → 仍是 400（``fromisoformat`` 的 ``ValueError`` 被包装）。"""
    import base64

    raw = base64.urlsafe_b64encode(b"yesterday|task_a").decode().rstrip("=")
    with pytest.raises(AppError) as excinfo:
        decode_cursor(raw)

    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT
