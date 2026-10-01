"""任务仓储的 Redis 实现（``INFRA_BACKEND=real``，``docs/09`` §4）。

**它解决的是「跨进程」这个问题**：``docs/08`` §5.4 要求 Worker 是独立进程，
而 API 进程把任务建在自己内存里的话，Worker 拿到消息也查不到任务行 ——
表现是「消息被 ack 掉、任务永远停在 PENDING」，日志里一条错误都没有。
所以只要 ``TASK_RUNNER=kafka``，任务状态就必须落在进程外的存储里。

MySQL 是 ``docs/09`` 指定的权威存储，但仓储端口已经定好，本实现与
:class:`~app.tasks.store.InMemoryTaskStore` **语义完全对齐**（含 ``idem_key``
唯一与版本号乐观锁），所以后续换成 MySQL 不影响业务代码。

三个实现细节值得写在这里：

* **整条记录存一个 JSON 字段**（``task:{id}`` 的 ``json`` 字段），而不是逐字段
  ``HSET``。乐观锁的要求是「``version`` 没变才允许写」，逐字段写需要「先读回再
  逐字段比对」，字段一多就一定漏；整段 JSON 的**字符串比较**天然覆盖所有字段，
  且能塞进一段 Lua 里原子完成（``HSET`` + ``EXPIRE`` 一次往返）。
* **消息重复与并发改状态靠 Cas 收敛**：冲突抛 :class:`TaskConflict`，由
  :class:`~app.tasks.service.TaskService` 读-改-重试（与内存实现同一口径）。
* **索引键与数据键分开**：``task:open`` 是「在飞任务」的 ZSET，供补偿扫描与
  过载判断用；``task:user:{uid}`` 供列表查询。**不能用 ``SCAN`` 取任务** ——
  那在 key 数量上万后会拖慢整个 Redis 实例。
"""

from __future__ import annotations

import builtins
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.pagination import cursor_position, decode_cursor, is_after_cursor
from app.infrastructure.redis.client import create_redis_client, redis_text
from app.tasks.models import (
    ACTIVE_STATUSES,
    ResourceType,
    Task,
    TaskError,
    TaskStatus,
    TaskType,
    now_iso,
)
from app.tasks.store import TASK_NOT_FOUND_MESSAGE, TaskConflict

logger = logging.getLogger("app.tasks.redis")

#: 任务列表别名：类体内有个叫 ``list`` 的方法，直接写 ``list[Task]`` 会被 mypy
#: 当成「方法当类型用」（同 :mod:`app.tasks.store` 里的同名别名）。
_TaskList = builtins.list[Task]

#: ``task:{id}`` 的 TTL（``docs/09`` §4：30d）
TASK_TTL_SECONDS = 30 * 24 * 3600
#: ``task:idem:{key}`` 与索引键的 TTL：与任务本身同寿命
INDEX_TTL_SECONDS = TASK_TTL_SECONDS
#: ``cancel:{id}`` 的 TTL（``docs/09`` §4：1d）
CANCEL_TTL_SECONDS = 24 * 3600

#: 列表查询一次最多扫描的用户任务数。用户维度的任务数是有界的（受 KB/文档上限约束），
#: 但不设上限意味着一个刷接口的账号能把一次查询变成全表扫描。
LIST_SCAN_LIMIT = 2000

#: 创建：仅当键不存在时写入（``HSET`` + ``EXPIRE`` 一次原子完成）
_CREATE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    return 0
end
redis.call('HSET', KEYS[1], 'json', ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

#: 乐观锁写入：期望值与当前值**逐字节相等**才写入
_CAS_SCRIPT = """
local current = redis.call('HGET', KEYS[1], 'json')
if not current then
    return 0
end
if current ~= ARGV[1] then
    return 0
end
redis.call('HSET', KEYS[1], 'json', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


def _task_key(task_id: str) -> str:
    return f"task:{task_id}"


def _idem_key(idem_key: str) -> str:
    return f"task:idem:{idem_key}"


def _user_key(user_id: str) -> str:
    return f"task:user:{user_id}"


def _cancel_key(task_id: str) -> str:
    return f"cancel:{task_id}"


#: 在飞任务的 ZSET（``task:open`` 沿用 ``task:`` 前缀，便于运维按前缀排查）
OPEN_ZSET = "task:open"


def _iso_to_epoch_millis(value: str) -> float:
    """RFC3339 → epoch 毫秒（用于 ZSET score）。

    ``created_at`` 在模型里是字符串（接口契约），而 ZSET 的 score 必须是数字。
    解析失败一律退回 ``0``：一个时间戳坏了的任务最多排到列表末尾，
    不该让整次查询抛异常。
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return 0.0


def task_to_record(task: Task) -> dict[str, Any]:
    """任务 → 可 JSON 序列化的字典（字段名与 ``Task`` 一一对应）。"""
    return {
        "id": task.id,
        "type": str(task.type),
        "status": str(task.status),
        "user_id": task.user_id,
        "resource_type": str(task.resource_type),
        "resource_id": task.resource_id,
        "payload": task.payload,
        "progress": task.progress,
        "stage": task.stage,
        "chunks_total": task.chunks_total,
        "chunks_done": task.chunks_done,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "error": task.error.to_dict() if task.error else None,
        "idem_key": task.idem_key,
        "queued_at": task.queued_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "version": task.version,
    }


def task_from_record(record: dict[str, Any]) -> Task:
    """字典 → 任务。

    刻意**宽容**（缺字段用默认值、未知字段忽略）：灰度或回滚期间，同一个 Key
    会同时存在新旧两种格式的记录。严格解析会把「旧格式记录」变成
    ``500``，而它本来完全可以正常读出来。
    """
    error = record.get("error")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return Task(
        id=str(record.get("id") or ""),
        type=_as_enum(TaskType, record.get("type"), TaskType.DOCUMENT_INGEST),
        status=_as_enum(TaskStatus, record.get("status"), TaskStatus.PENDING),
        user_id=str(record.get("user_id") or ""),
        resource_type=_as_enum(ResourceType, record.get("resource_type"), ResourceType.DOCUMENT),
        resource_id=str(record.get("resource_id") or ""),
        payload=payload,
        progress=int(record.get("progress") or 0),
        stage=record.get("stage") if isinstance(record.get("stage"), str) else None,
        chunks_total=int(record.get("chunks_total") or 0),
        chunks_done=int(record.get("chunks_done") or 0),
        retry_count=int(record.get("retry_count") or 0),
        max_retries=int(record.get("max_retries") or 0),
        error=TaskError(
            code=str((error or {}).get("code") or ""),
            message=str((error or {}).get("message") or ""),
            detail=(error or {}).get("detail") or {},
            at=str((error or {}).get("at") or now_iso()),
        )
        if isinstance(error, dict)
        else None,
        idem_key=str(record.get("idem_key") or ""),
        queued_at=record.get("queued_at") or None,
        started_at=record.get("started_at") or None,
        finished_at=record.get("finished_at") or None,
        created_at=str(record.get("created_at") or now_iso()),
        updated_at=str(record.get("updated_at") or now_iso()),
        version=int(record.get("version") or 0),
    )


def _as_enum(enum_type: Any, value: Any, default: Any) -> Any:
    """字符串 → 枚举；非法值退回默认（同样出于「宽容读取」的考虑）。"""
    try:
        return enum_type(value)
    except ValueError:
        return default


class RedisTaskStore:
    """共享任务仓储（Redis）。"""

    def __init__(
        self,
        client: Any,
        *,
        task_ttl: int = TASK_TTL_SECONDS,
        cancel_ttl: int = CANCEL_TTL_SECONDS,
        scan_limit: int = LIST_SCAN_LIMIT,
    ) -> None:
        self._client = client
        self._task_ttl = max(1, task_ttl)
        self._cancel_ttl = max(1, cancel_ttl)
        self._scan_limit = max(1, scan_limit)

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisTaskStore:
        """按 ``REDIS_URL`` 构造。"""
        return cls(
            create_redis_client(
                settings, hint="uv add redis 或将 INFRA_BACKEND 设为 memory（任务存储）"
            )
        )

    # ------------------------------------------------------------------
    async def create(self, task: Task) -> Task:
        """写入新任务；``idem_key`` 命中时返回既有任务（幂等）。

        幂等键用 ``SET NX`` 抢占：**先抢键再写记录**，而不是「先查再写」——
        后者两个并发请求会双双查到「不存在」然后各写一条，唯一约束就形同虚设。
        """
        acquired = await self._client.set(
            _idem_key(task.idem_key), task.id, nx=True, ex=self._task_ttl
        )
        if not acquired:
            existing_id = redis_text(await self._client.get(_idem_key(task.idem_key)))
            if existing_id and existing_id != task.id:
                return await self.get(existing_id)
            # 幂等键还在但记录已过期（极少见）：清掉陈旧键让本次写入成功
            await self._client.delete(_idem_key(task.idem_key))
            await self._client.set(_idem_key(task.idem_key), task.id, nx=True, ex=self._task_ttl)

        created = await self._client.eval(
            _CREATE_SCRIPT,
            # 只传一次序列化结果：CAS/Lua 的入参是字符串，Lua 里不能调 __repr__
            1,
            _task_key(task.id),
            _dump(task),
            str(self._task_ttl),
        )
        if not created:  # pragma: no cover - 同一 task_id 重复创建（UUID 冲突级概率）
            return await self.get(task.id)
        await self._index(task)
        return replace(task)

    async def get(self, task_id: str, user_id: str | None = None) -> Task:
        """按 ID 取任务；``user_id`` 不为空时同时校验归属。"""
        raw = await self._client.hget(_task_key(task_id), "json")
        if raw is None:
            raise AppError(ErrorCode.TASK_NOT_FOUND, TASK_NOT_FOUND_MESSAGE)
        task = task_from_record(json.loads(redis_text(raw)))
        if user_id is not None and task.user_id != user_id:
            # 跨用户访问返回 404 而不是 403，避免枚举（REQ-RAG-011 的同一条原则）
            raise AppError(ErrorCode.TASK_NOT_FOUND, TASK_NOT_FOUND_MESSAGE)
        return task

    async def list(
        self,
        *,
        user_id: str,
        status: Sequence[TaskStatus] = (),
        type_: TaskType | None = None,
        resource_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[_TaskList, bool]:
        """分页列出任务（``docs/08`` §4.1）。

        **先取全量 id 再过滤**：ZSET 里的顺序只按 ``created_at``，而过滤条件有
        三个。若「先 ``ZREVRANGE`` 取 limit 条再过滤」，一旦最新的几十条都不满足
        条件，返回的就是空页 —— 而第二页还有数据。用户维度的任务数有界，
        所以取全量是划算的（截断见 ``LIST_SCAN_LIMIT``）。
        """
        members = await self._client.zrevrange(_user_key(user_id), 0, self._scan_limit - 1)
        tasks = await self._load_many([redis_text(item) for item in members])
        selected = [
            task
            for task in tasks
            if task.user_id == user_id
            and (not status or task.status in status)
            and (type_ is None or task.type is type_)
            and (resource_id is None or task.resource_id == resource_id)
        ]
        # 排序键与游标比较必须是同一个键：``(created_at, id)``（与内存实现一致）
        selected.sort(key=lambda task: cursor_position(task.created_at, task.id), reverse=True)
        if cursor:
            moment, cursor_id = decode_cursor(cursor)
            selected = [
                task
                for task in selected
                if is_after_cursor(task.created_at, task.id, (moment, cursor_id))
            ]
        page = selected[: limit + 1]
        return page[:limit], len(page) > limit

    async def update(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        """在乐观锁保护下就地修改（``docs/08`` §2 不变式）。"""
        key = _task_key(task_id)
        raw = await self._client.hget(key, "json")
        if raw is None:
            raise AppError(ErrorCode.TASK_NOT_FOUND, TASK_NOT_FOUND_MESSAGE)
        current_text = redis_text(raw)
        current = task_from_record(json.loads(current_text))

        candidate = replace(current)
        mutate(candidate)
        if candidate.version != current.version:
            # 变更函数不许自己动版本号：版本由 store 统一推进
            raise TaskConflict(f"变更函数不得修改 version（任务 {task_id}）")
        candidate.version = current.version + 1
        candidate.updated_at = now_iso()

        written = await self._client.eval(
            _CAS_SCRIPT, 1, key, current_text, _dump(candidate), str(self._task_ttl)
        )
        if not written:
            raise TaskConflict(f"任务 {task_id} 已被并发修改")
        await self._touch_index(candidate)
        return candidate

    async def request_cancel(self, task_id: str) -> bool:
        """置取消标记；返回是否为「首次置位」。"""
        acquired = await self._client.set(_cancel_key(task_id), "1", nx=True, ex=self._cancel_ttl)
        return bool(acquired)

    async def is_cancel_requested(self, task_id: str) -> bool:
        """查询取消标记。"""
        return bool(await self._client.exists(_cancel_key(task_id)))

    # ------------------------------------------------------------------
    # 供补偿扫描与过载判断（``docs/08`` §5.1 / §5.4）
    # ------------------------------------------------------------------
    async def list_stale_pending(self, *, before: str, limit: int = 50) -> _TaskList:
        """列出「创建于 ``before`` 之前且仍是 PENDING」的任务。"""
        members = await self._client.zrangebyscore(
            OPEN_ZSET, "-inf", _iso_to_epoch_millis(before), start=0, num=max(1, limit)
        )
        tasks = await self._load_many([redis_text(item) for item in members])
        # 时间下界由 ZRANGEBYSCORE 完成；这里只再确认「现在还是 PENDING」——
        # ZSET 里的成员是「最近一次写入时的在飞任务」，从写入到读取之间它可能已经跑完了。
        return [task for task in tasks if task.status is TaskStatus.PENDING]

    async def count_open(self) -> int:
        """在飞任务数（``PENDING``/``QUEUED``/``RUNNING``）。"""
        return int(await self._client.zcard(OPEN_ZSET))

    async def close(self) -> None:
        """关闭连接（应用关停路径）。"""
        closer = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if closer is None:  # pragma: no cover - 替身
            return
        result = closer()
        if hasattr(result, "__await__"):
            await result

    # ------------------------------------------------------------------
    async def _load_many(self, task_ids: Sequence[str]) -> _TaskList:
        """批量取任务；读不到（已过期）的 id 直接跳过。"""
        if not task_ids:
            return []
        pipe = self._client.pipeline()
        for task_id in task_ids:
            pipe.hget(_task_key(task_id), "json")
        raws = await pipe.execute()
        tasks: list[Task] = []
        for raw in raws:
            if raw is None:
                continue
            try:
                tasks.append(task_from_record(json.loads(redis_text(raw))))
            except (ValueError, TypeError):  # pragma: no cover - 脏数据
                logger.warning("task.record_corrupted")
        return tasks

    async def _index(self, task: Task) -> None:
        """写入用户索引与在飞索引。"""
        pipe = self._client.pipeline()
        pipe.zadd(_user_key(task.user_id), {task.id: _iso_to_epoch_millis(task.created_at)})
        pipe.expire(_user_key(task.user_id), INDEX_TTL_SECONDS)
        if task.status in ACTIVE_STATUSES:
            pipe.zadd(OPEN_ZSET, {task.id: _iso_to_epoch_millis(task.created_at)})
            pipe.expire(OPEN_ZSET, INDEX_TTL_SECONDS)
        await pipe.execute()

    async def _touch_index(self, task: Task) -> None:
        """按最新状态维护在飞索引（进入/离开 ``PENDING|QUEUED|RUNNING``）。"""
        pipe = self._client.pipeline()
        if task.status in ACTIVE_STATUSES:
            pipe.zadd(OPEN_ZSET, {task.id: _iso_to_epoch_millis(task.created_at)})
            pipe.expire(OPEN_ZSET, INDEX_TTL_SECONDS)
        else:
            pipe.zrem(OPEN_ZSET, task.id)
        await pipe.execute()


def _dump(task: Task) -> str:
    """序列化为紧凑 JSON（空格会参与乐观锁的逐字节比较，紧凑格式更省带宽）。"""
    return json.dumps(task_to_record(task), ensure_ascii=False, separators=(",", ":"), default=str)


__all__ = [
    "CANCEL_TTL_SECONDS",
    "INDEX_TTL_SECONDS",
    "LIST_SCAN_LIMIT",
    "OPEN_ZSET",
    "TASK_TTL_SECONDS",
    "RedisTaskStore",
    "task_from_record",
    "task_to_record",
]
