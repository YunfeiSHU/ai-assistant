"""资源 ID 生成与校验（规范见 ``docs/02-接口规范与错误码.md`` §1）。

形如 ``{前缀}_{26 位 Crockford Base32（ULID）}``；后缀前 10 字符为 48 位毫秒时间戳，
后 16 字符为 80 位随机数。前缀集合见 :data:`ID_PREFIXES`。

自己实现 ULID 而不引第三方包：只有几十行，且能顺手做两件必须的事 —— 同毫秒内单调
递增（保证按时间排序分页不漏不重），前缀与正则集中定义。

``u`` / ``rt`` 也定义在这里（本服务并不生成它们）：``user_id`` 会作为 JWT 的 ``sub``
进入本服务的库表，前缀集合是两个服务共用的契约 —— 两边各维护一份时，网关新增或改名前缀
本侧就再也认不出来。只有真正由本服务生成的才调 :func:`new_id`。
"""

from __future__ import annotations

import re
import secrets
import threading
import time

#: Crockford Base32：刻意去掉易混淆的 I / L / O / U
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING_LENGTH = 32
_TIMESTAMP_CHARS = 10
_RANDOM_CHARS = 16
_RANDOM_BITS = 80
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1

#: 允许的资源前缀
ID_PREFIXES: frozenset[str] = frozenset(
    {
        "kb",
        "doc",
        "chk",
        "task",
        "mem",
        "msg",
        "srv",
        "cv",
        "req",
        # 网关签发、本服务只接收不生成：u = 用户（JWT 的 sub）、
        # rt = 刷新令牌。列在这里是为了前缀集合不出现第二份。
        "u",
        "rt",
    }
)

_ULID_CHARS = r"[0-9A-HJKMNP-TV-Z]{26}"
ULID_PATTERN = re.compile(rf"^{_ULID_CHARS}$")
ID_PATTERN = re.compile(rf"^(?:{'|'.join(sorted(ID_PREFIXES))})_{_ULID_CHARS}$")

#: Redis Key / 日志字段中允许出现的安全片段（防 Key 注入，见 docs/09-§4）
_SAFE_KEY_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_lock = threading.Lock()
_last_ms = 0
_last_random = 0


def _encode(value: int, length: int) -> str:
    """把整数按 Crockford Base32 编码为定长字符串（大端，左侧补 0）。"""
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _ALPHABET[value & (_ENCODING_LENGTH - 1)]
        value >>= 5
    return "".join(chars)


def ulid(now_ms: int | None = None) -> str:
    """生成 26 位 ULID 字符串（同毫秒内单调递增）。"""
    global _last_ms, _last_random

    ms = int(time.time() * 1000) if now_ms is None else now_ms
    with _lock:
        if ms > _last_ms:
            _last_ms = ms
            _last_random = secrets.randbits(_RANDOM_BITS)
        else:
            # 时钟回拨或同毫秒：靠随机部分自增保证单调
            ms = _last_ms
            _last_random += 1
            if _last_random > _RANDOM_MASK:
                _last_ms += 1
                ms = _last_ms
                _last_random = secrets.randbits(_RANDOM_BITS)
    return _encode(ms, _TIMESTAMP_CHARS) + _encode(_last_random, _RANDOM_CHARS)


def new_id(prefix: str) -> str:
    """生成带前缀的资源 ID，例如 ``kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C``。

    Args:
        prefix: ``kb`` / ``doc`` / ``chk`` / ``task`` / ``mem`` / ``msg`` / ``srv`` / ``cv`` / ``req``

    Raises:
        ValueError: 前缀不在白名单内（fail-fast，避免产出无法校验的 ID）。
    """
    if prefix not in ID_PREFIXES:
        raise ValueError(f"未知 ID 前缀 {prefix!r}，允许：{sorted(ID_PREFIXES)}")
    return f"{prefix}_{ulid()}"


def is_valid_id(value: object, prefix: str | None = None) -> bool:
    """校验字符串是否为合法资源 ID；给定 ``prefix`` 时额外校验前缀。"""
    if not isinstance(value, str):
        return False
    if prefix is None:
        return bool(ID_PATTERN.match(value))
    return value.startswith(f"{prefix}_") and bool(ID_PATTERN.match(value))


def is_safe_key_component(value: object) -> bool:
    """校验可作为 Redis Key 片段 / 存储过滤条件的字符串。

    ``{conversation_id}`` 等直接拼进 Key 会有注入风险（docs/09-§4）。
    """
    return isinstance(value, str) and bool(_SAFE_KEY_COMPONENT.match(value))


__all__ = [
    "ID_PATTERN",
    "ID_PREFIXES",
    "ULID_PATTERN",
    "is_safe_key_component",
    "is_valid_id",
    "new_id",
    "ulid",
]
