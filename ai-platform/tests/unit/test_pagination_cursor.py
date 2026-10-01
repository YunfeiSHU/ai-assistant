"""单元测试：游标分页编解码。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import decode_cursor, encode_cursor


def test_cursor_roundtrip_preserves_millisecond_precision() -> None:
    """毫秒精度 MUST 保留：丢了精度就无法与 ``DATETIME(3)`` 精确比较。"""
    moment = datetime(2026, 9, 28, 10, 0, 0, 123000, tzinfo=UTC)
    cursor = encode_cursor(moment, "doc_01J8ZQ3K7N9P2V6R4T8W1Y5B3C")

    decoded_moment, decoded_id = decode_cursor(cursor)

    assert decoded_moment == moment
    assert decoded_id == "doc_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"


def test_cursor_is_opaque() -> None:
    """游标对客户端不透明：不应是明文可读的 ``id``。"""
    cursor = encode_cursor(datetime.now(UTC), "doc_1")

    assert "doc_1" not in cursor
    assert cursor.isascii()


def test_naive_datetime_assumed_utc() -> None:
    """朴素时间按 UTC 解释（避免本机时区把游标整体平移）。"""
    aware = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    assert decode_cursor(encode_cursor(aware, "x"))[0] == aware
    assert decode_cursor(encode_cursor(aware.replace(tzinfo=None), "x"))[0] == aware


@pytest.mark.parametrize("cursor", ["", "not-base64!!!", "Zm9v", "Zm9vfA", "MTAwfA"])
def test_invalid_cursor_raises_invalid_argument(cursor: str) -> None:
    """非法游标一律 ``INVALID_ARGUMENT``，绝不静默当作第一页。"""
    with pytest.raises(AppError) as excinfo:
        decode_cursor(cursor)

    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_ordering_is_consistent_with_time() -> None:
    """同一资源的先后两条游标：解码后可比较出先后（分页不漏不重的前提）。"""
    base = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)
    first = decode_cursor(encode_cursor(base, "doc_1"))
    second = decode_cursor(encode_cursor(base + timedelta(milliseconds=1), "doc_2"))

    assert first < second
