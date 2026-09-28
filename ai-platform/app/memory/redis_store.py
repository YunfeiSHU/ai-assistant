"""会话上下文的 Redis 实现（``docs/07`` §2.1，``INFRA_BACKEND=real``）。

与 :class:`~app.memory.context_store.InMemoryConversationStore` **结构完全一致**
（``ctx:{id}`` 列表 + ``LTRIM`` + TTL + 会话级锁），只是换了载体。这样
「同一个会话在两个后端上的行为」是同一段语义，只有存储换了。

三个必须留在代码里的细节：

* **归属写在独立键里**（``ctx:owner:{id}``）而不是塞进消息元素。列表可能是空的
  （刚 ``clear`` 过），从元素里取 owner 会在最需要判断归属的时候取不到。
* **同会话写串行化**用 ``SET NX EX`` + Lua compare-and-delete，而不是「先 GET 再 DEL」：
  后者在锁超时后可能删掉**别人**的锁，于是两个写者交错 —— 正是锁要防的事。
* **读失败与「不存在」必须区分**：读失败记 ``memory_unavailable`` 并降级；
  「不存在」才是 ``404``。两者混在一起会让 Redis 抖动变成用户可见的 404。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.redis import RedisUnavailable, create_redis_client
from app.memory.context_store import ConversationSummary, StoredMessage

logger = logging.getLogger("app.memory.redis")

#: 消息列表的 Key 前缀（``docs/09`` §4）
_CTX_PREFIX = "ctx:"
_OWNER_PREFIX = "ctx:owner:"
_SUMMARY_PREFIX = "ctx:summary:"
_LOCK_PREFIX = "lock:ctx:"

#: 分布式锁的 TTL（``docs/07`` §2.1：5s）。锁只在「读-改-写列表」期间持有，
#: 单次操作远小于 5s；TTL 的存在是为了进程崩溃后锁能自动释放。
LOCK_TTL_SECONDS = 5

#: 释放锁的标准脚本：只有值仍是自己的 token 时才删（compare-and-delete）。
#: 不用「GET 再 DEL」是因为锁可能已经超时并被别人重新持有，那时 DEL 删掉的是
#: **别人的**锁，两个写者会同时进入临界区。
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


@runtime_checkable
class RedisCommands(Protocol):
    """本模块用到的 Redis 命令子集（便于注入替身）。"""

    async def rpush(self, key: str, *values: str) -> int: ...

    async def ltrim(self, key: str, start: int, stop: int) -> Any: ...

    async def lrange(self, key: str, start: int, stop: int) -> list[Any]: ...

    async def expire(self, key: str, seconds: int) -> Any: ...

    async def get(self, key: str) -> Any: ...

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> Any: ...

    async def delete(self, *keys: str) -> int: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


def create_redis_commands(settings: Settings) -> RedisCommands:
    """按配置建立 Redis 连接（**懒导入**，实现见 :mod:`app.core.redis`）。

    ``redis`` 不在基础依赖里：本地与测试用内存实现，只有 ``INFRA_BACKEND=real``
    才需要它。缺失时给出可执行的提示，而不是一个 ``ModuleNotFoundError`` 堆栈。
    """
    return create_redis_client(
        settings, hint="uv add redis 或将 INFRA_BACKEND 设为 memory（会话存储）"
    )


class RedisLock:
    """基于 ``SET NX EX`` 的会话级互斥锁。"""

    def __init__(self, client: RedisCommands, key: str, *, ttl: int = LOCK_TTL_SECONDS) -> None:
        self._client = client
        self._key = key
        self._ttl = ttl
        self._token = ""

    async def __aenter__(self) -> RedisLock:
        import time

        # token 必须唯一：它区分「我的锁」与「超时后别人的锁」
        self._token = f"{id(self):x}-{time.monotonic_ns():x}"
        for _ in range(50):  # 最多等约 5s（50 × 0.1s）
            acquired = await self._client.set(self._key, self._token, nx=True, ex=self._ttl)
            if acquired:
                return self
            import asyncio

            await asyncio.sleep(0.1)
        raise RedisUnavailable("会话上下文被占用，获取锁超时", {"key": self._key})

    async def __aexit__(self, *_: object) -> None:
        try:
            await self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
        except Exception:  # 释放失败只记日志：锁会因 TTL 自动过期
            logger.warning("memory.lock_release_failed", extra={"key": self._key})


class RedisConversationStore:
    """:class:`~app.memory.context_store.ConversationStore` 的 Redis 实现。"""

    def __init__(self, client: RedisCommands, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # ------------------------------------------------------------------
    @property
    def _ttl_seconds(self) -> int:
        return max(1, self._settings.memory_ttl_days * 86400)

    def _ctx_key(self, conversation_id: str) -> str:
        return f"{_CTX_PREFIX}{conversation_id}"

    def _owner_key(self, conversation_id: str) -> str:
        return f"{_OWNER_PREFIX}{conversation_id}"

    def _summary_key(self, conversation_id: str) -> str:
        return f"{_SUMMARY_PREFIX}{conversation_id}"

    # ------------------------------------------------------------------
    async def ensure(self, conversation_id: str, user_id: str) -> None:
        """确保会话存在且属于该用户（不存在则建立，语义与内存实现一致）。"""
        owner_key = self._owner_key(conversation_id)
        created = await self._client.set(owner_key, user_id, nx=True, ex=self._ttl_seconds)
        if created:
            return
        current = await self._client.get(owner_key)
        if current != user_id:
            # 归属不符与不存在返回同一个错误：不泄露资源是否存在
            raise AppError(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在")

    async def _assert_owner(self, conversation_id: str, user_id: str) -> None:
        current = await self._client.get(self._owner_key(conversation_id))
        if current != user_id:
            raise AppError(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在")

    async def append(
        self, conversation_id: str, user_id: str, messages: Sequence[StoredMessage]
    ) -> None:
        if not messages:
            return
        async with RedisLock(self._client, f"{_LOCK_PREFIX}{conversation_id}"):
            try:
                await self._assert_owner(conversation_id, user_id)
            except AppError:
                # 首次写入即隐式建会话（与内存实现一致）
                await self._client.set(
                    self._owner_key(conversation_id), user_id, ex=self._ttl_seconds
                )
            ctx_key = self._ctx_key(conversation_id)
            payloads = [
                json.dumps(
                    {
                        "role": message.role,
                        "content": message.content,
                        "message_id": message.message_id,
                        "tokens": 0,
                        "created_at": message.created_at,
                        "partial": message.partial,
                    },
                    ensure_ascii=False,
                )
                for message in messages
            ]
            await self._client.rpush(ctx_key, *payloads)
            # 与内存实现的「只保留最近 N 条」语义一致（内存里是切片，这里是 LTRIM）
            keep = max(1, self._settings.memory_max_messages)
            await self._client.ltrim(ctx_key, -keep, -1)
            # 每次写入刷新 TTL，否则活跃会话会在 7 天后突然「失忆」
            await self._client.expire(ctx_key, self._ttl_seconds)
            await self._client.expire(self._owner_key(conversation_id), self._ttl_seconds)

    async def recent(
        self, conversation_id: str, user_id: str, *, turns: int
    ) -> list[StoredMessage]:
        await self._assert_owner(conversation_id, user_id)
        limit = max(1, turns) * 2
        raw = await self._client.lrange(self._ctx_key(conversation_id), -limit, -1)
        messages = [_decode(raw_item) for raw_item in raw]
        summary = await self.summary(conversation_id, user_id)
        if summary and summary.covered_until:
            messages = [m for m in messages if m and m.created_at > summary.covered_until]
        return [m for m in messages if m is not None and m.content.strip()]

    async def all_messages(self, conversation_id: str, user_id: str) -> list[StoredMessage]:
        await self._assert_owner(conversation_id, user_id)
        raw = await self._client.lrange(self._ctx_key(conversation_id), 0, -1)
        return [message for message in (_decode(item) for item in raw) if message is not None]

    async def summary(self, conversation_id: str, user_id: str) -> ConversationSummary | None:
        raw = await self._client.get(self._summary_key(conversation_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("memory.summary_corrupted", extra={"conversation_id": conversation_id})
            return None
        return ConversationSummary(
            content=str(payload.get("content") or ""),
            covered_until=str(payload.get("covered_until") or ""),
            source_message_count=int(payload.get("source_message_count") or 0),
            token_count=int(payload.get("token_count") or 0),
        )

    async def save_summary(
        self, conversation_id: str, user_id: str, summary: ConversationSummary
    ) -> None:
        await self._assert_owner(conversation_id, user_id)
        await self._client.set(
            self._summary_key(conversation_id),
            json.dumps(
                {
                    "content": summary.content,
                    "covered_until": summary.covered_until,
                    "source_message_count": summary.source_message_count,
                    "token_count": summary.token_count,
                },
                ensure_ascii=False,
            ),
            ex=self._ttl_seconds,
        )

    async def clear(self, conversation_id: str, user_id: str) -> None:
        await self._assert_owner(conversation_id, user_id)
        # owner 键保留：清空的是「上下文」而不是「会话归属」。一并删掉会让下一次
        # 写入把会话当成新建，且期间任何人拿同一个 id 都能认领它。
        await self._client.delete(
            self._ctx_key(conversation_id), self._summary_key(conversation_id)
        )


def _decode(raw: Any) -> StoredMessage | None:
    """把列表元素还原成消息；坏数据只跳过（不整个会话报错）。"""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("memory.message_corrupted")
        return None
    if not isinstance(payload, dict):
        return None
    return StoredMessage(
        role=str(payload.get("role") or ""),
        content=str(payload.get("content") or ""),
        message_id=str(payload.get("message_id") or ""),
        created_at=str(payload.get("created_at") or ""),
        partial=bool(payload.get("partial")),
    )


__all__ = [
    "LOCK_TTL_SECONDS",
    "RedisCommands",
    "RedisConversationStore",
    "RedisLock",
    "RedisUnavailable",
    "create_redis_commands",
]
