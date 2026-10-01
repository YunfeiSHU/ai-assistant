"""长期记忆仓储的 MySQL 实现（``user_memory`` 表，``REQ-MEM-004/005/007``）。

不复用 :mod:`app.infrastructure.storage.mysql` 的引擎与表定义：记忆不属于知识库域，两张表
之间没有任何引用；把 ``user_memory`` 塞进 RAG 的表元数据里会让「哪张表属于哪条链路」
变得难以判断，而删除某条链路时就会漏掉另一半。真正需要共享的（引擎构造、时间戳格式、
错误分类）在 :mod:`app.infrastructure.mysql.db`，语义一致性靠共用工具而非共用容器保证。

与 :class:`~app.memory.long_term.InMemoryMemoryRepo` 逐条对齐：精确去重对齐内存的
``by_hash``；命中即刷新对齐 ``hit_count += 1``；容量淘汰对齐「丢最不可靠且最旧的」；
分页对齐 ``(created_at, id)`` 倒序。

``hit_count`` 用自增表达式而不是「读出来 +1 再写回去」：后者在两次抽取并发时会互相覆盖
（都读到 1、都写 2，实际命中了 3 次）。内存实现有全局锁没这个问题，SQL 实现只能靠
单条 UPDATE 的原子性。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    tuple_,
    update,
)
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.pagination import decode_cursor
from app.infrastructure.mysql.db import (
    FETCH_AHEAD,
    create_engine_from_settings,
    db_now,
    from_db,
    release_engine,
    reraise,
    to_db,
)
from app.memory.long_term import (
    MemoryKind,
    MemoryRecord,
    MemoryWriteResult,
    content_sha256,
)

logger = get_logger("app.memory.mysql")

metadata_ = MetaData()

user_memory_table = Table(
    "user_memory",
    metadata_,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("content", String(2000), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("source", String(32), nullable=False, server_default="auto"),
    Column("source_conversation_id", String(64)),
    Column("hit_count", Integer, nullable=False, server_default="1"),
    Column("expired", Boolean, nullable=False, server_default="0"),
    Column("expires_at", DateTime),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)


def _record_values(record: MemoryRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "user_id": record.user_id,
        "kind": record.kind,
        "content": record.content,
        "content_sha256": record.content_sha256,
        "confidence": record.confidence,
        "source": record.source,
        "source_conversation_id": record.source_conversation_id or None,
        "hit_count": record.hit_count,
        "expired": record.expired,
        "expires_at": to_db(record.expires_at),
        "created_at": to_db(record.created_at) or db_now(),
        "updated_at": to_db(record.updated_at) or db_now(),
    }


def _record_from_row(row: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        user_id=row["user_id"],
        content=row["content"],
        kind=_as_kind(row["kind"]),
        confidence=float(row["confidence"]),
        content_sha256=row["content_sha256"],
        hit_count=int(row["hit_count"]),
        source=row["source"] or "auto",
        source_conversation_id=row["source_conversation_id"] or "",
        expires_at=from_db(row["expires_at"]),
        expired=bool(row["expired"]),
        created_at=from_db(row["created_at"]) or "",
        updated_at=from_db(row["updated_at"]) or "",
    )


def _as_kind(value: Any) -> MemoryKind:
    """库里的 ``kind`` → ``Literal``。

    没有做「未知值就静默变成 fact」的兜底：库里出现第三种 kind 说明表与代码不同步
    （例如有人手工插了数据），静默归一化会让那条记忆永远筛不出来。
    """
    text = str(value)
    if text not in ("preference", "fact"):
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"未知的记忆类型：{text}",
            {"kind": text},
        )
    return "preference" if text == "preference" else "fact"


class MySqlMemoryRepo:
    """:class:`~app.memory.long_term.MemoryRepo` 的 MySQL 实现。"""

    def __init__(self, settings: Settings, *, engine: Any | None = None) -> None:
        self._engine = engine if engine is not None else create_engine_from_settings(settings)
        self._max_items = max(1, settings.memory_max_items)

    # ------------------------------------------------------------------
    async def add(self, record: MemoryRecord) -> MemoryWriteResult:
        table = user_memory_table
        existing = await self.find_by_hash(record.user_id, record.content)
        if existing is not None:
            # 命中即刷新：内容保持原有版本（新版本可能是截断/改写的），
            # 只把「被用到过」这件事记下来（AC-MEM-08 断言 hit_count=2）
            touched = await self._bump_hit(
                existing.id, confidence=record.confidence, expected_user=record.user_id
            )
            return MemoryWriteResult(record=touched, created=False)

        try:
            async with self._engine.begin() as connection:
                await connection.execute(insert(table).values(**_record_values(record)))
        except IntegrityError as exc:
            # 并发下两个请求同时通过了上面的查询：这里退化成「命中」而不是报错 ——
            # 去重的语义是「同一句话只存一条」，撞唯一键恰好证明这个目的已达成。
            #
            # 重查要带上短暂等待：并发的另一个事务可能还没提交，此刻再查依旧查不到
            # （InnoDB 的 READ COMMITTED 下看不到未提交行），于是会走到 ``reraise`` 报
            # 409 —— 用户明明只是重复说了一句话，却拿到一个错误。
            for _ in range(3):
                again = await self.find_by_hash(record.user_id, record.content)
                if again is not None:
                    touched = await self._bump_hit(
                        again.id, confidence=record.confidence, expected_user=record.user_id
                    )
                    return MemoryWriteResult(record=touched, created=False)
                await asyncio.sleep(0.05)
            reraise(exc)

        await self._evict(record.user_id)
        stored = await self._get_or_none(record.id)
        return MemoryWriteResult(record=stored or record, created=True)

    async def _bump_hit(
        self, mem_id: str, *, confidence: float, expected_user: str | None = None
    ) -> MemoryRecord:
        """``hit_count + 1`` 并刷新 ``updated_at``（单条 UPDATE，原子）。"""
        table = user_memory_table
        values: dict[str, Any] = {
            "hit_count": table.c.hit_count + 1,
            "updated_at": db_now(),
        }
        if confidence > 0:
            # GREATEST 而不是「取较大者再写回」：并发下后者会丢更新
            values["confidence"] = func.greatest(table.c.confidence, confidence)
        conditions = [table.c.id == mem_id]
        if expected_user is not None:
            conditions.append(table.c.user_id == expected_user)
        async with self._engine.begin() as connection:
            result = await connection.execute(update(table).where(*conditions).values(**values))
        if result.rowcount == 0:
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
        record = await self._get_or_none(mem_id)
        if record is None:
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
        return record

    async def _evict(self, user_id: str) -> None:
        """超出 ``MEMORY_MAX_ITEMS`` 时淘汰：先挑 id，再按 id 删。

        不写 ``DELETE ... ORDER BY ... LIMIT`` 那种单语句形式：它只在 MySQL 上成立，
        而这里的两步写法在任何方言上都一样，且「淘汰了哪几条」能顺手记进日志 ——
        容量淘汰是**丢数据**的行为，只发生在 SQL 端而不留痕迹会很难受。
        """
        table = user_memory_table
        total = await self.count(user_id)
        overflow = total - self._max_items
        if overflow <= 0:
            return
        # 先按置信度升序、再按更新时间升序 —— 丢「最不可靠且最久没动过」的
        pick = (
            select(table.c.id)
            .where(table.c.user_id == user_id)
            .order_by(table.c.confidence.asc(), table.c.updated_at.asc())
            .limit(overflow)
        )
        victims: list[Any] = []
        async with self._engine.begin() as connection:
            victims = [row[0] for row in (await connection.execute(pick)).all()]
            if victims:
                await connection.execute(delete(table).where(table.c.id.in_(victims)))
        if victims:
            logger.info(
                "memory.evicted",
                extra={"user_id": user_id, "count": len(victims), "max_items": self._max_items},
            )

    async def _get_or_none(self, mem_id: str) -> MemoryRecord | None:
        table = user_memory_table
        statement = select(table).where(table.c.id == mem_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).first()
        return _record_from_row(dict(row._mapping)) if row is not None else None

    # ------------------------------------------------------------------
    async def find_by_hash(self, user_id: str, content: str) -> MemoryRecord | None:
        table = user_memory_table
        statement = select(table).where(
            table.c.user_id == user_id, table.c.content_sha256 == content_sha256(content)
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).first()
        return _record_from_row(dict(row._mapping)) if row is not None else None

    async def touch(self, mem_id: str, *, confidence: float = 0.0) -> MemoryRecord:
        return await self._bump_hit(mem_id, confidence=confidence)

    async def get(self, mem_id: str, user_id: str) -> MemoryRecord:
        table = user_memory_table
        statement = select(table).where(table.c.id == mem_id, table.c.user_id == user_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).first()
        if row is None:
            # 跨用户一律 404 而不是 403：403 等于确认「这个 ID 存在」
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
        return _record_from_row(dict(row._mapping))

    async def list_page(
        self,
        user_id: str,
        *,
        kind: MemoryKind | None = None,
        expired: bool | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[MemoryRecord], bool]:
        table = user_memory_table
        statement = select(table).where(table.c.user_id == user_id)
        if kind is not None:
            statement = statement.where(table.c.kind == kind)
        if expired is not None:
            statement = statement.where(table.c.expired.is_(expired))
        if cursor:
            moment, resource_id = decode_cursor(cursor)
            statement = statement.where(
                tuple_(table.c.created_at, table.c.id) < tuple_(to_db(moment), resource_id)
            )
        statement = statement.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(
            limit + FETCH_AHEAD
        )
        async with self._engine.connect() as connection:
            rows = [dict(row._mapping) for row in (await connection.execute(statement)).all()]
        items = [_record_from_row(row) for row in rows[:limit]]
        return items, len(rows) > limit

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        table = user_memory_table
        values = _record_values(record)
        # 主键与创建时间不可改（见 app/infrastructure/storage/mysql.py 的同名说明）
        values.pop("id", None)
        values.pop("created_at", None)
        values["updated_at"] = db_now()
        statement = (
            update(table)
            .where(table.c.id == record.id, table.c.user_id == record.user_id)
            .values(**values)
        )
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(statement)
        except IntegrityError as exc:
            # 改正文后撞上了另一条已存在的记忆（同一用户同一哈希）：
            # 这是**可回复**的业务冲突，不是 500
            raise AppError(
                ErrorCode.CONFLICT,
                "已存在相同内容的记忆",
                {"content_sha256": record.content_sha256},
            ) from exc
        if result.rowcount == 0:
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在或无权访问")
        return await self.get(record.id, record.user_id)

    async def delete(self, mem_id: str, user_id: str) -> MemoryRecord:
        """删除单条并**返回被删记录**（调用方要用它同步删向量，``REQ-MEM-007``）。"""
        record = await self.get(mem_id, user_id)
        table = user_memory_table
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(table).where(table.c.id == mem_id, table.c.user_id == user_id)
            )
        return record

    async def delete_all(self, user_id: str) -> int:
        table = user_memory_table
        async with self._engine.begin() as connection:
            result = await connection.execute(delete(table).where(table.c.user_id == user_id))
        return int(result.rowcount or 0)

    async def count(self, user_id: str, *, active_only: bool = False) -> int:
        table = user_memory_table
        statement = select(func.count()).select_from(table).where(table.c.user_id == user_id)
        if active_only:
            statement = statement.where(table.c.expired.is_(False))
        async with self._engine.connect() as connection:
            total = (await connection.execute(statement)).scalar_one()
        return int(total)

    async def all_for_user(self, user_id: str) -> list[MemoryRecord]:
        """取该用户全部记忆（重建向量索引用）。

        按 ``created_at`` 升序返回：重建索引时「先建的先写」，失败重跑的顺序也稳定，
        这样两次重建的中间态是一样的，排查时容易对账。
        """
        table = user_memory_table
        statement = (
            select(table)
            .where(table.c.user_id == user_id)
            .order_by(table.c.created_at.asc(), table.c.id.asc())
        )
        async with self._engine.connect() as connection:
            rows = [dict(row._mapping) for row in (await connection.execute(statement)).all()]
        return [_record_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """归还引擎（lifespan 结束时调用）——共享引擎，不能直接 ``dispose``。"""
        await release_engine(self._engine)


__all__ = [
    "MySqlMemoryRepo",
    "user_memory_table",
]
