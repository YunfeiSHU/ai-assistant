"""单元测试：token 计数与按 token 截断。"""

from __future__ import annotations

from app.core.tokens import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    count_tokens_many,
    fits_budget,
    truncate_to_tokens,
)


def test_empty_text_costs_nothing() -> None:
    """空串不计 token（否则空消息会污染预算计算）。"""
    assert count_tokens("") == 0
    assert count_tokens_many([]) == 0


def test_chinese_costs_more_than_latin_per_char() -> None:
    """中文单字信息密度高于拉丁字母，token/字符比必须更高。

    这条断言是「不能按字符数估算 token」的可执行证据。
    """
    chinese = "这是一段中文文本"
    latin = "a" * len(chinese) * 2

    assert count_tokens(chinese) / len(chinese) > count_tokens(latin) / len(latin)


def test_message_tokens_include_overhead() -> None:
    """每条消息有固定开销（role / 分隔符），不能只算内容。"""
    content = "hello world"

    assert count_message_tokens(content) > count_tokens(content)


def test_messages_tokens_are_cumulative() -> None:
    """消息条数增加时总 token 单调递增。"""
    one = [{"role": "user", "content": "你好"}]
    two = [*one, {"role": "assistant", "content": "你好，有什么可以帮你？"}]

    assert count_messages_tokens(two) > count_messages_tokens(one)


def test_non_string_content_is_tolerated() -> None:
    """多模态 / 结构化 content 不应导致崩溃。"""
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    assert count_messages_tokens(messages) > 0


def test_truncate_to_tokens_respects_limit() -> None:
    """截断后 MUST 落在预算内，且内容为原文本前缀。"""
    text = "上下文预算很紧。" * 200
    limit = 50

    truncated = truncate_to_tokens(text, limit)

    assert count_tokens(truncated) <= limit
    assert text.startswith(truncated)


def test_truncate_to_tokens_is_noop_within_limit() -> None:
    """未超限时原样返回（不做无意义改写）。"""
    text = "短文本"

    assert truncate_to_tokens(text, 1000) == text


def test_truncate_to_tokens_handles_non_positive_budget() -> None:
    """预算 ≤ 0 时返回空串（调用方据此丢弃片段）。"""
    assert truncate_to_tokens("abc", 0) == ""
    assert truncate_to_tokens("abc", -5) == ""


def test_fits_budget_boundary() -> None:
    """边界判断：恰好等于预算算通过。"""
    messages = [{"role": "user", "content": "你好"}]
    exact = count_messages_tokens(messages)

    assert fits_budget(messages, exact) is True
    assert fits_budget(messages, exact - 1) is False
