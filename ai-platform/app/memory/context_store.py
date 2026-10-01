"""会话上下文的读写（``REQ-MEM-001``，存储结构见 ``docs/07`` §2）。

M2 实现的是进程内版本；Redis 版本的结构与 Key 约定完全一致
（``ctx:{conversation_id}`` 列表 + ``LTRIM`` + ``lock:ctx:{id}``）。

两个容易做错、这里显式处理的点：

* 归属校验：``conversation_id`` 属于别人时返回 ``404 CONVERSATION_NOT_FOUND`` 而不是
  403 —— 403 等于告诉调用方「这个 ID 是存在的」。
* 同会话写串行化：用会话级锁串行化追加，否则并发两轮对话的 user/assistant 消息会交错
  （Redis 版靠分布式锁解决同一个问题）。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """写入短期上下文的一条消息。"""

    role: str
    content: str
    message_id: str
    created_at: str
    #: 流式中断时落库的半成品（``REQ-CHAT-006``）
    partial: bool = False


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """对话摘要（``docs/07`` §3）。"""

    content: str
    covered_until: str = ""
    source_message_count: int = 0
    token_count: int = 0


@dataclass(slots=True)
class _Conversation:
    owner: str
    messages: list[StoredMessage] = field(default_factory=list)
    summary: ConversationSummary | None = None


def now_iso() -> str:
    """统一的时间戳格式（毫秒精度 UTC，便于与 ``created_at`` 比较）。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@runtime_checkable
class ConversationStore(Protocol):
    """会话上下文存储。"""

    async def ensure(self, conversation_id: str, user_id: str) -> None:
        """确保会话存在且属于该用户；否则抛 ``404``。"""
        ...

    async def append(
        self, conversation_id: str, user_id: str, messages: Sequence[StoredMessage]
    ) -> None:
        """追加消息并保留最近 ``memory_max_messages`` 条。"""
        ...

    async def recent(
        self, conversation_id: str, user_id: str, *, turns: int
    ) -> list[StoredMessage]:
        """取最近 ``turns`` 轮（user+assistant）原文。"""
        ...

    async def all_messages(self, conversation_id: str, user_id: str) -> list[StoredMessage]:
        """取全部原文（摘要生成与 ``GET /context`` 需要看到完整历史）。

        刻意与 :meth:`recent` 分开：``recent`` 是「给模型看的」（已被摘要覆盖的部分会被
        过滤掉），而摘要器必须看到包含已摘要部分在内的全量，否则增量合并无从谈起。
        """
        ...

    async def summary(self, conversation_id: str, user_id: str) -> ConversationSummary | None:
        """取对话摘要。"""
        ...

    async def save_summary(
        self, conversation_id: str, user_id: str, summary: ConversationSummary
    ) -> None:
        """落库摘要（``REQ-MEM-003``）。"""
        ...

    async def clear(self, conversation_id: str, user_id: str) -> None:
        """清空上下文（保留长期记忆，见 ``docs/07`` §5.4）。"""
        ...


class InMemoryConversationStore:
    """进程内实现：单实例有效，进程重启即清空（本地开发/测试用）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._items: dict[str, _Conversation] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ------------------------------------------------------------------
    def _get(self, conversation_id: str, user_id: str) -> _Conversation:
        conversation = self._items.get(conversation_id)
        if conversation is None or conversation.owner != user_id:
            # 归属不符与不存在返回同一个错误：不泄露资源是否存在
            raise AppError(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在")
        return conversation

    async def ensure(self, conversation_id: str, user_id: str) -> None:
        existing = self._items.get(conversation_id)
        if existing is None:
            self._items[conversation_id] = _Conversation(owner=user_id)
            return
        if existing.owner != user_id:
            raise AppError(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在")

    # ------------------------------------------------------------------
    async def append(
        self, conversation_id: str, user_id: str, messages: Sequence[StoredMessage]
    ) -> None:
        if not messages:
            return
        async with self._locks[conversation_id]:
            try:
                conversation = self._get(conversation_id, user_id)
            except AppError:
                # 首次写入即隐式建会话：调用方（ChatService）已决定要落上下文
                conversation = _Conversation(owner=user_id)
                self._items[conversation_id] = conversation
            conversation.messages.extend(messages)
            # 与 Redis 版 LTRIM 语义一致：只保留最近 N 条
            limit = self._settings.memory_max_messages
            if len(conversation.messages) > limit:
                del conversation.messages[: len(conversation.messages) - limit]

    async def recent(
        self, conversation_id: str, user_id: str, *, turns: int
    ) -> list[StoredMessage]:
        conversation = self._get(conversation_id, user_id)
        limit = max(1, turns) * 2  # 一轮 = user + assistant
        messages = conversation.messages[-limit:]
        # 摘要覆盖过的旧消息不再重复注入（``docs/07`` §2.2 第 2 条）
        summary = conversation.summary
        if summary and summary.covered_until:
            messages = [m for m in messages if m.created_at > summary.covered_until]
        return [m for m in messages if m.content.strip()]

    async def all_messages(self, conversation_id: str, user_id: str) -> list[StoredMessage]:
        return list(self._get(conversation_id, user_id).messages)

    async def summary(self, conversation_id: str, user_id: str) -> ConversationSummary | None:
        return self._get(conversation_id, user_id).summary

    async def save_summary(
        self, conversation_id: str, user_id: str, summary: ConversationSummary
    ) -> None:
        self._get(conversation_id, user_id).summary = summary

    async def clear(self, conversation_id: str, user_id: str) -> None:
        conversation = self._get(conversation_id, user_id)
        conversation.messages.clear()
        conversation.summary = None


class UnavailableConversationStore:
    """Redis 不可用时的占位实现（``INFRA_BACKEND=real`` 且驱动缺失/连不上）。

    与 :class:`~app.infrastructure.storage.unavailable.UnavailableKnowledgeBaseRepo` 同一个
    取舍：在调用点报 503，而不是在构造期抛异常。prod 强制 ``real``，构造期抛会让
    「对话 / 鉴权 / 健康检查」一起不可用；占位实现把故障限制在真正需要 Redis 的调用上，
    且 ChatService 会把读取失败降级为 ``memory_unavailable``。
    """

    #: 记入日志与 ``details`` 的原因（排障时一眼看出是「没接」而不是「挂了」）
    REASON = "Redis 会话存储尚未提供连接（驱动缺失或配置未生效）"

    def _fail(self) -> AppError:
        return AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "会话上下文存储不可用",
            {"component": "conversation_store", "reason": self.REASON},
        )

    async def ensure(self, conversation_id: str, user_id: str) -> None:
        raise self._fail()

    async def append(
        self, conversation_id: str, user_id: str, messages: Sequence[StoredMessage]
    ) -> None:
        raise self._fail()

    async def recent(
        self, conversation_id: str, user_id: str, *, turns: int
    ) -> list[StoredMessage]:
        raise self._fail()

    async def all_messages(self, conversation_id: str, user_id: str) -> list[StoredMessage]:
        raise self._fail()

    async def summary(self, conversation_id: str, user_id: str) -> ConversationSummary | None:
        raise self._fail()

    async def save_summary(
        self, conversation_id: str, user_id: str, summary: ConversationSummary
    ) -> None:
        raise self._fail()

    async def clear(self, conversation_id: str, user_id: str) -> None:
        raise self._fail()


__all__ = [
    "ConversationStore",
    "ConversationSummary",
    "InMemoryConversationStore",
    "StoredMessage",
    "UnavailableConversationStore",
    "now_iso",
]
