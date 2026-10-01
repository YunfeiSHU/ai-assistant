"""长期记忆实体与仓储（``REQ-MEM-004`` / ``REQ-MEM-005`` / ``REQ-MEM-007``）。

存储结构见 ``docs/07`` §5 与 ``docs/09``：关系库存**正文与元数据**
（``user_memory`` 表），向量库存**语义索引**（``ai_platform_memories`` 集合）。
两者必须成对增删 —— 只删一边的表现是「列表里没有了，但检索还能命中」，
而这类不一致没有任何报错，只能靠人肉发现。

因此这里的写入路径刻意只暴露一个方法 :meth:`MemoryRepo.add`，把「精确去重」
放在仓储内部：调用方（抽取器 / ``memory_save`` 工具 / ``POST /memories``）
不需要各自记得「先查哈希」—— 漏一次就会产生重复记忆。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, runtime_checkable

from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import cursor_position, decode_cursor, encode_cursor, is_after_cursor
from app.core.text import sha256_hex
from app.memory.context_store import now_iso

#: 记忆类型（``docs/07`` §5.1）
MemoryKind = Literal["preference", "fact"]

#: ``docs/07`` §5.2：相似度落在 [0.85, 0.92) 时**不合并**（宁可冗余也不丢信息）
CONFLICT_LOW = 0.85


@dataclass(slots=True)
class MemoryRecord:
    """一条长期记忆（字段与 ``docs/09`` 的 ``user_memory`` 表对齐）。"""

    id: str
    user_id: str
    content: str
    kind: MemoryKind = "fact"
    confidence: float = 1.0
    #: ``content`` 的规范化哈希（精确去重的唯一键）
    content_sha256: str = ""
    #: 被命中（重复抽取/重复写入）的次数，用于「越常用越重要」的排序
    hit_count: int = 1
    #: 来源会话（审计用，``REQ-MEM-007``）；手动创建时为空
    source_conversation_id: str = ""
    #: 来源：``auto``（轮末抽取 / ``memory_save`` 工具）或 ``manual``（``POST /memories``）。
    #: 落到 ``user_memory.source`` 列（``docs/09`` §2.5）。区分两者是审计要求：
    #: 「用户自己写的记忆」与「系统推出来的记忆」在排查“为什么模型记得这个”时
    #: 结论完全不同，而一旦都记为 ``auto`` 就再也分不出来了。
    source: str = "auto"
    #: ``kind=fact`` 可设置过期时间；到期后不再注入（保留记录并标记 ``expired``）
    expires_at: str | None = None
    expired: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.content_sha256:
            # 谁定义语义谁负责填字段：让调用方「记得算哈希」就是留坑
            self.content_sha256 = content_sha256(self.content)

    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """当前是否可注入（未过期、未被显式标记失效）。"""
        return not self.expired

    def is_expired_at(self, moment_iso: str) -> bool:
        """给定时刻是否已过期（``expires_at`` 为空表示永不过期）。"""
        deadline = self.expires_at
        # 先取出局部变量再比较：直接在属性上做 ``and`` 链，mypy 不肯把
        # 「属性非 None」的判断带到第二个操作数上（属性随时可能变）
        return deadline is not None and deadline <= moment_iso

    def to_dict(self) -> dict[str, object]:
        """对外响应结构（``docs/07`` §6 的 ``/memories`` 字段）。"""
        return {
            "id": self.id,
            "content": self.content,
            "kind": self.kind,
            "confidence": self.confidence,
            "hit_count": self.hit_count,
            "source_conversation_id": self.source_conversation_id or None,
            "expires_at": self.expires_at,
            "expired": self.expired,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def normalise_content(content: str) -> str:
    """记忆正文的规范化形式（精确去重的比较基准）。

    只把连续空白压成一个空格，**不**删除空格 —— 词边界是内容的一部分，
    「用户偏好简洁回答」与「用户偏好 简洁回答」不该被当成同一条。

    这里是**唯一**的规范化定义：仓储的唯一索引、抽取器的批内去重都调它。
    两处各写一遍迟早会出现「看起来一样但不相等」，表现为同一条记忆被存两遍
    （或者反过来：批内没去重、却被存储当成重复丢掉）。
    """
    return " ".join(content.split())


def content_sha256(content: str) -> str:
    """记忆正文的规范化哈希（对应表里的唯一索引 ``uk_mem_user_hash``）。

    「我喜欢简洁回答」与「我喜欢简洁回答  」是同一句话。若直接对原文哈希，
    精确去重会被一个空格绕过，于是同一偏好会被抽成两条。
    """
    return sha256_hex(normalise_content(content))


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    """写入结果。

    ``created=False`` 表示命中了精确去重（``hit_count`` 已 +1、``updated_at`` 已刷新）。
    把它显式返回而不是返回 ``None``，是因为 ``AC-MEM-08`` 要断言
    「同一句话抽两次 → 仍只有 1 条，且 ``hit_count=2``」。
    """

    record: MemoryRecord
    created: bool


@runtime_checkable
class MemoryRepo(Protocol):
    """长期记忆仓储（关系库侧）。"""

    async def add(self, record: MemoryRecord) -> MemoryWriteResult:
        """写入一条记忆；``(user_id, content_sha256)`` 已存在时只更新命中计数。"""
        ...

    async def find_by_hash(self, user_id: str, content: str) -> MemoryRecord | None:
        """按规范化内容的哈希查已有记忆（精确去重的**读取**侧）。

        为什么需要它而不只用 :meth:`add` 的返回值：语义去重必须在写入**之前**做。
        若先写入再去语义查重，命中时得把刚插进去的那条删掉，就会出现中间态
        （并发读者能看到重复条目）。
        """
        ...

    async def touch(self, mem_id: str, *, confidence: float = 0.0) -> MemoryRecord:
        """把 ``hit_count`` +1 并刷新 ``updated_at``（重复抽取的合并动作）。"""
        ...

    async def get(self, mem_id: str, user_id: str) -> MemoryRecord:
        """取单条；跨用户一律 ``404``。"""
        ...

    async def list_page(
        self,
        user_id: str,
        *,
        kind: MemoryKind | None = None,
        expired: bool | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[MemoryRecord], bool]:
        """分页列出；返回 ``(items, has_more)``。

        故意不叫 ``list``：方法名一旦叫 ``list``，本类后续所有 ``list[X]`` 注解
        都会被解析成这个方法（mypy ``valid-type``），而报错位置在几十行之外。
        """
        ...

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        """整体覆盖（``PATCH`` 改正文后重新向量化）。"""
        ...

    async def delete(self, mem_id: str, user_id: str) -> MemoryRecord:
        """删除单条并返回被删记录（调用方需要它来同步删向量，``REQ-MEM-007``）。"""
        ...

    async def delete_all(self, user_id: str) -> int:
        """清空该用户全部记忆，返回删除条数（幂等）。"""
        ...

    async def count(self, user_id: str, *, active_only: bool = False) -> int:
        """统计条数。"""
        ...

    async def all_for_user(self, user_id: str) -> list[MemoryRecord]:
        """取该用户全部记忆（重建向量索引用）。"""
        ...


@dataclass
class _State:
    """仓储的可变状态（对应 SQL 的 ``user_memory`` 表 + 唯一索引）。"""

    items: dict[str, MemoryRecord] = field(default_factory=dict)
    #: ``(user_id, content_sha256) -> mem_id``（唯一索引 ``uk_mem_user_hash`` 的语义）
    by_hash: dict[tuple[str, str], str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InMemoryMemoryRepo:
    """:class:`MemoryRepo` 的进程内实现。

    刻意实现唯一索引语义（同一用户同一内容哈希只存一条）与容量上限，
    否则「本地全绿、上真库报唯一键冲突」这类问题只能等上线才发现。
    """

    def __init__(self, *, max_items: int = 500) -> None:
        self._state = _State()
        self._max_items = max(1, max_items)

    # ------------------------------------------------------------------
    async def add(self, record: MemoryRecord) -> MemoryWriteResult:
        async with self._state.lock:
            key = (record.user_id, record.content_sha256)
            existing_id = self._state.by_hash.get(key)
            if existing_id is not None:
                existing = self._state.items[existing_id]
                # 命中即刷新：内容保持原有版本（新版本可能是截断/改写的），
                # 只把「被用到过」这件事记下来（``AC-MEM-08`` 断言 hit_count=2）
                existing.hit_count += 1
                existing.updated_at = now_iso()
                existing.confidence = max(existing.confidence, record.confidence)
                return MemoryWriteResult(record=replace(existing), created=False)

            self._state.items[record.id] = record
            self._state.by_hash[key] = record.id
            self._evict_locked(record.user_id)
            return MemoryWriteResult(record=replace(record), created=True)

    def _evict_locked(self, user_id: str) -> None:
        """超出容量上限时丢最旧的低置信条目（在持锁状态下调用）。"""
        owned = [item for item in self._state.items.values() if item.user_id == user_id]
        if len(owned) <= self._max_items:
            return
        # 排序：先按置信度升序，再按更新时间升序 —— 丢「最不可靠且最久没动过」的
        owned.sort(key=lambda item: (item.confidence, item.updated_at))
        for victim in owned[: len(owned) - self._max_items]:
            self._state.items.pop(victim.id, None)
            self._state.by_hash.pop((victim.user_id, victim.content_sha256), None)

    async def get(self, mem_id: str, user_id: str) -> MemoryRecord:
        async with self._state.lock:
            record = self._state.items.get(mem_id)
        if record is None or record.user_id != user_id:
            # 跨用户一律 404 而不是 403：403 等于确认「这个 ID 存在」
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
        return replace(record)

    async def find_by_hash(self, user_id: str, content: str) -> MemoryRecord | None:
        async with self._state.lock:
            mem_id = self._state.by_hash.get((user_id, content_sha256(content)))
            record = self._state.items.get(mem_id) if mem_id else None
        return replace(record) if record is not None else None

    async def touch(self, mem_id: str, *, confidence: float = 0.0) -> MemoryRecord:
        async with self._state.lock:
            record = self._state.items.get(mem_id)
            if record is None:
                raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
            record.hit_count += 1
            record.updated_at = now_iso()
            if confidence > 0:
                record.confidence = max(record.confidence, confidence)
            return replace(record)

    async def list_page(
        self,
        user_id: str,
        *,
        kind: MemoryKind | None = None,
        expired: bool | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[MemoryRecord], bool]:
        async with self._state.lock:
            items = [replace(record) for record in self._state.items.values()]
        position = decode_cursor(cursor) if cursor else None
        selected = [
            record
            for record in items
            if record.user_id == user_id
            and (kind is None or record.kind == kind)
            and (expired is None or record.expired is expired)
            and is_after_cursor(record.created_at, record.id, position)
        ]
        # 排序键与游标比较必须用同一个 cursor_position，否则「排序按字符串、比较按
        # datetime」错位会静默漏项（见 docs/12 §2.1）
        selected.sort(
            key=lambda record: cursor_position(record.created_at, record.id), reverse=True
        )
        window = selected[: limit + 1]
        return window[:limit], len(window) > limit

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        async with self._state.lock:
            existing = self._state.items.get(record.id)
            if existing is None or existing.user_id != record.user_id:
                raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
            # 正文变了 → 哈希与唯一索引必须同步更新，否则旧哈希会一直指向这条记录，
            # 导致「改回原文」时被当成重复而静默丢弃
            if existing.content_sha256 != record.content_sha256:
                self._state.by_hash.pop((existing.user_id, existing.content_sha256), None)
                self._state.by_hash[(record.user_id, record.content_sha256)] = record.id
            record.updated_at = now_iso()
            self._state.items[record.id] = record
            return replace(record)

    async def delete(self, mem_id: str, user_id: str) -> MemoryRecord:
        async with self._state.lock:
            record = self._state.items.get(mem_id)
            if record is None or record.user_id != user_id:
                raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
            self._state.items.pop(mem_id, None)
            self._state.by_hash.pop((record.user_id, record.content_sha256), None)
        return replace(record)

    async def delete_all(self, user_id: str) -> int:
        async with self._state.lock:
            targets = [
                mem_id for mem_id, record in self._state.items.items() if record.user_id == user_id
            ]
            for mem_id in targets:
                record = self._state.items.pop(mem_id)
                self._state.by_hash.pop((record.user_id, record.content_sha256), None)
        return len(targets)

    async def count(self, user_id: str, *, active_only: bool = False) -> int:
        async with self._state.lock:
            return sum(
                1
                for record in self._state.items.values()
                if record.user_id == user_id and (not active_only or record.active)
            )

    async def all_for_user(self, user_id: str) -> list[MemoryRecord]:
        async with self._state.lock:
            return [
                replace(record)
                for record in self._state.items.values()
                if record.user_id == user_id
            ]


def encode_memory_cursor(record: MemoryRecord) -> str:
    """记忆列表的游标（与其它列表接口同一个 ``(created_at, id)`` 原语）。"""
    return encode_cursor(record.created_at, record.id)


def expiring(records: Sequence[MemoryRecord], moment_iso: str) -> list[MemoryRecord]:
    """标记出已到期的记忆（``docs/07`` §5.4：保留记录、标记 ``expired=true``）。"""
    touched: list[MemoryRecord] = []
    for record in records:
        if not record.expired and record.expires_at and record.is_expired_at(moment_iso):
            record.expired = True
            touched.append(record)
    return touched


__all__ = [
    "CONFLICT_LOW",
    "InMemoryMemoryRepo",
    "MemoryKind",
    "MemoryRecord",
    "MemoryRepo",
    "MemoryWriteResult",
    "content_sha256",
    "encode_memory_cursor",
    "expiring",
    "normalise_content",
]
