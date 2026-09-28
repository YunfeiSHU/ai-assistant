"""单元测试：资源 ID 生成与校验。"""

from __future__ import annotations

import re

import pytest

from app.core.ids import (
    ID_PATTERN,
    ID_PREFIXES,
    is_safe_key_component,
    is_valid_id,
    new_id,
    ulid,
)

#: Crockford Base32（去掉 I / L / O / U）
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.mark.parametrize("prefix", sorted(ID_PREFIXES))
def test_new_id_matches_contract_regex(prefix: str) -> None:
    """``AC-API-03``：生成的 ID 必须匹配 ``^(前缀)_[0-9A-HJKMNP-TV-Z]{26}$``。"""
    value = new_id(prefix)

    assert ID_PATTERN.match(value)
    assert is_valid_id(value, prefix)
    assert is_valid_id(value)
    assert not is_valid_id(value, "kb" if prefix != "kb" else "doc")


def test_new_id_rejects_unknown_prefix() -> None:
    """未知前缀 fail-fast，避免产出无法被契约校验的 ID。"""
    with pytest.raises(ValueError, match="未知 ID 前缀"):
        new_id("nope")


def test_ulid_never_contains_ambiguous_chars() -> None:
    """ULID 字母表刻意排除 ``I`` ``L`` ``O`` ``U``，降低人工抄写歧义。"""
    values = [ulid() for _ in range(200)]

    assert all(ULID_RE.match(value) for value in values)
    assert not any(char in value for value in values for char in "ILOU")


def test_ulid_is_monotonic_within_same_millisecond() -> None:
    """同毫秒内生成的一批 ULID MUST 严格递增。

    这是游标分页「不漏不重」的前提：排序键是 ``(created_at, id)``，
    若同一毫秒内的 ID 无序，分页边界就会抖。
    """
    fixed = 1_800_000_000_000
    values = [ulid(fixed) for _ in range(500)]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_ulid_time_prefix_is_stable() -> None:
    """时间戳前缀相同的两次调用，前 10 位一致。"""
    fixed = 1_800_000_000_000
    first, second = ulid(fixed), ulid(fixed)

    assert first[:10] == second[:10]
    assert first[10:] != second[10:]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("u_test_abc", True),
        ("cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C", True),
        ("u_bad:colon", False),
        ("", False),
        ("a" * 65, False),
        ("含中文", False),
        (None, False),
        (123, False),
    ],
)
def test_is_safe_key_component(value: object, expected: bool) -> None:
    """Redis Key / 向量过滤表达式的片段校验（防注入）。"""
    assert is_safe_key_component(value) is expected


def test_ulid_is_not_lexicographically_time_ordered_across_ms() -> None:
    """跨毫秒时前缀单调（Base32 编码保持大端序）。"""
    assert ulid(1_800_000_000_000) < ulid(1_800_000_001_000)
