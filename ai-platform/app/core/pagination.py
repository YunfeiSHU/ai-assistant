"""游标分页的编解码（契约见 ``docs/02-接口规范与错误码.md`` §7.1）。

游标对客户端不透明，服务端用 ``(created_at, id)`` 二元组编码，保证同一时间戳下不漏不重。
不用 offset：offset 分页在「边翻页边插入」时会重复或漏项，而本项目的文档 / 任务 /
记忆列表天然会被后台任务持续写入。
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime

from app.core.exceptions import AppError, ErrorCode

_SEPARATOR = "|"


def parse_stamp(value: str | datetime) -> datetime:
    """把实体里的时间戳（``...Z`` / ISO 字符串 / ``datetime``）统一成带时区 ``datetime``。

    必须归一：时间戳当字符串比较就会出错。``"2026-09-28T09:15:45.604Z"`` 与
    ``"2026-09-28T09:15:45+00:00"`` 是同一时刻，字符串比较却在第 20 个字符处
    按 ``'.' > '+'`` 判出先后 —— 把同一毫秒创建的记录全部判为「已翻过去了」，
    列表静默丢数据且不报错。
    """
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def encode_cursor(created_at: datetime | str, resource_id: str) -> str:
    """把 ``(created_at, id)`` 编码为不透明游标。

    入参是 ``datetime`` 还是字符串都先经 :func:`parse_stamp` 归一，否则同一位置
    会随入参类型产出两条不同的游标串，排查时会误以为「位置变了」。
    """
    stamp = parse_stamp(created_at).isoformat()
    raw = f"{stamp}{_SEPARATOR}{resource_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解码游标。

    Raises:
        AppError: ``INVALID_ARGUMENT``（游标被篡改或来自旧版本）。
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, _, resource_id = raw.partition(_SEPARATOR)
        if not stamp or not resource_id:
            raise ValueError("cursor 结构不完整")
        moment = datetime.fromisoformat(stamp)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            "游标无效",
            {"cursor": cursor[:64]},
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment, resource_id


def cursor_position(created_at: str | datetime, resource_id: str) -> tuple[datetime, str]:
    """列表排序与游标比较的统一键 ``(created_at, id)``。

    排序与游标比较必须是同一个键，否则会出现「排序按秒、比较按毫秒」这类错位。
    同一时间戳下用 ``id`` 兜底保证顺序稳定（Windows 上 ``time.Now`` 粒度本来就粗）。
    """
    return parse_stamp(created_at), resource_id


def is_after_cursor(
    created_at: str | datetime, resource_id: str, position: tuple[datetime, str] | None
) -> bool:
    """该条记录是否落在游标位置之后（列表按 ``(created_at, id)`` 倒序）。

    ``position`` 为 ``None``（首页）时恒为 ``True``。
    """
    if position is None:
        return True
    return (parse_stamp(created_at), resource_id) < position


__all__ = [
    "cursor_position",
    "decode_cursor",
    "encode_cursor",
    "is_after_cursor",
    "parse_stamp",
]
