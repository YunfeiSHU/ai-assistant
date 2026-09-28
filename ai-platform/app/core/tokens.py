"""Token 计数与按 token 截断。

docs/10 §7.2 要求「Token 计数 MUST 使用与目标模型一致的 tokenizer，不可用字符数粗估」。
实际可达的最优选择是 ``tiktoken``（DeepSeek 未公开官方 tokenizer 的 Python 实现），
它比字符数估计准确得多；若 tiktoken 不可用或模型编码缺失，退化为**按字符类别加权**
的启发式（中文 1 字 ≈ 1 token，西文 ≈ 4 字符 / token），并明确记录退化来源。

.. warning::
   估算偏差会直接影响上下文预算裁剪的准确性。生产环境请确保 tiktoken 可用。
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Sequence
from typing import Any

#: tiktoken 的通用编码（对 GPT 系模型精确，对其它模型是同量级近似）
_ENCODING_NAME = "cl100k_base"

#: 每条消息的固定开销（role、分隔符等），与 OpenAI 的 message 计费口径同阶
MESSAGE_OVERHEAD_TOKENS = 4

#: 会话级别固定开销
REPLY_PRIMER_TOKENS = 3


@functools.lru_cache(maxsize=1)
def _encoding() -> Any | None:
    """懒加载 tiktoken 编码；不可用时返回 ``None``（触发启发式）。"""
    try:
        import tiktoken
    except Exception:  # pragma: no cover - 依赖缺失时的降级路径
        return None
    try:
        return tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:  # pragma: no cover - 需要联网下载编码文件的场景
        return None


def _heuristic_count(text: str) -> int:
    """无 tokenizer 时的启发式估算：CJK 按字计 1，其余按 4 字符计 1。"""
    cjk = 0
    other = 0
    for char in text:
        if (
            "\u2e80" <= char <= "\u9fff"
            or "\uf900" <= char <= "\ufaff"
            or "\uff00" <= char <= "\uffef"
        ):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def count_tokens(text: str) -> int:
    """统计单段文本的 token 数（空串为 0）。"""
    if not text:
        return 0
    encoding = _encoding()
    if encoding is None:
        return _heuristic_count(text)
    return len(encoding.encode(text, disallowed_special=()))


def count_tokens_many(texts: Iterable[str]) -> int:
    """统计多段文本的 token 总数。"""
    return sum(count_tokens(text) for text in texts)


def count_message_tokens(content: str) -> int:
    """统计一条消息在 prompt 中占用的 token（含固定开销）。"""
    return count_tokens(content) + MESSAGE_OVERHEAD_TOKENS


def count_messages_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """统计一组 chat messages 的总 token（含会话固定开销）。

    Args:
        messages: 形如 ``[{"role": "user", "content": "..."}, ...]``；
            非字符串 ``content`` 会被 ``str()`` 后统计。
    """
    total = REPLY_PRIMER_TOKENS
    for message in messages:
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        total += count_message_tokens(content)
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """把文本按 token 上限截断（尽量不破坏 UTF-8 字符边界）。

    Args:
        max_tokens: ≤ 0 时返回空串。
    """
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    encoding = _encoding()
    if encoding is None:
        # 启发式路径：按估算比例反推字符数，再逐字符收敛
        ratio = _heuristic_count(text) / max(len(text), 1)
        limit = max(1, int(max_tokens / max(ratio, 1e-6)))
        return text[:limit]
    return encoding.decode(encoding.encode(text, disallowed_special=())[:max_tokens])


def fits_budget(messages: Sequence[dict[str, Any]], budget: int) -> bool:
    """判断消息序列是否在 token 预算内。"""
    return count_messages_tokens(messages) <= budget


__all__ = [
    "MESSAGE_OVERHEAD_TOKENS",
    "REPLY_PRIMER_TOKENS",
    "count_message_tokens",
    "count_messages_tokens",
    "count_tokens",
    "count_tokens_many",
    "fits_budget",
    "truncate_to_tokens",
]
