"""用户级记忆偏好（``docs/07`` §5.4「关闭能力」、§6 ``/memory-settings``）。

为什么单独一层而不是塞进 :class:`~app.memory.long_term.MemoryRecord`：
``memory_enabled=false`` 表示「**既不写入也不读取**长期记忆」。把开关和记忆条目
放一起，会出现「用户清空记忆后开关也跟着没了」这类耦合 —— 清空记忆不该改变偏好。

`clear_marker` 是 ``REQ-MEM-007`` 要求的「清空后 24h 内不重新抽取旧内容」的落点：
抽取器拿不到「刚清过」这个事实就会在本轮对话里把用户刚删掉的偏好原样写回来，
用户看到的是「删了又回来了」。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class MemoryPreference:
    """用户级记忆偏好。"""

    user_id: str
    enabled: bool = True
    top_n: int = 3
    #: 最近一次 ``DELETE /memories?all=true`` 的时间（冷却期起点）
    cleared_at: datetime | None = None

    def is_cooling_down(self, *, hours: int, now: datetime | None = None) -> bool:
        """是否仍在「清空后不再抽取」的冷却窗口内。"""
        if self.cleared_at is None or hours <= 0:
            return False
        return (now or _now()) - self.cleared_at < timedelta(hours=hours)


@dataclass
class _State:
    items: dict[str, MemoryPreference] = field(default_factory=dict)


@runtime_checkable
class MemoryPreferenceStore(Protocol):
    """记忆偏好存储端口。"""

    async def get(
        self, user_id: str, *, default_top_n: int, default_enabled: bool = True
    ) -> MemoryPreference:
        """取偏好；不存在时返回默认值（而不是抛错 —— 未设置就是「用默认」）。

        ``default_enabled`` 是**全局**开关（``MEMORY_ENABLED``）：它必须能成为
        新用户的初始值，否则运维把全局开关关掉之后，用户级默认仍然是「开」。
        """
        ...

    async def save(self, preference: MemoryPreference) -> MemoryPreference:
        """保存偏好。"""
        ...

    async def mark_cleared(self, user_id: str, *, at: datetime) -> MemoryPreference:
        """记录「清空全部记忆」的时刻（冷却期起点）。"""
        ...


class InMemoryMemoryPreferenceStore:
    """:class:`MemoryPreferenceStore` 的进程内实现。"""

    def __init__(self) -> None:
        self._state = _State()

    async def get(
        self, user_id: str, *, default_top_n: int, default_enabled: bool = True
    ) -> MemoryPreference:
        existing = self._state.items.get(user_id)
        if existing is None:
            return MemoryPreference(user_id=user_id, enabled=default_enabled, top_n=default_top_n)
        # 返回副本：调用方随手改返回值不该改到存储里的状态
        return MemoryPreference(
            user_id=existing.user_id,
            enabled=existing.enabled,
            top_n=existing.top_n,
            cleared_at=existing.cleared_at,
        )

    async def save(self, preference: MemoryPreference) -> MemoryPreference:
        self._state.items[preference.user_id] = MemoryPreference(
            user_id=preference.user_id,
            enabled=preference.enabled,
            top_n=preference.top_n,
            cleared_at=preference.cleared_at,
        )
        return preference

    async def mark_cleared(self, user_id: str, *, at: datetime) -> MemoryPreference:
        current = self._state.items.get(user_id) or MemoryPreference(user_id=user_id)
        current.cleared_at = at
        self._state.items[user_id] = current
        return current


def recent_fingerprints(contents: Sequence[str]) -> set[str]:
    """把一批正文规范成指纹集合（冷却期「不重新抽取旧内容」的判据）。

    当前实现用「整个冷却窗口内不抽取」达成同一个目的（见
    ``MemoryService.extract_and_store``），这个函数留给 M6 里的「同一会话逐条比对」
    方案；先把语义（按空白规范化后比较）定下来，避免两处各自实现一套规范化。
    """
    return {" ".join(content.split()) for content in contents}


__all__ = [
    "InMemoryMemoryPreferenceStore",
    "MemoryPreference",
    "MemoryPreferenceStore",
    "recent_fingerprints",
]
