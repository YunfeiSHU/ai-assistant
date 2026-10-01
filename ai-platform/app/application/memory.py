"""长期记忆用例编排（``REQ-MEM-004`` ~ ``REQ-MEM-007``）。

职责边界：**关系库 + 向量库的一致性**在这一层收口。底下两个仓储各自只懂自己那半边
（:mod:`app.memory.long_term` 管正文、:mod:`app.memory.vector_index` 管向量），
「双写」与「双删」的配对只能在这里保证 —— 让每个调用点自己记得同步，就等于留一个
「列表里删掉了但检索还能命中」的坑。

去重的三层语义（``docs/07`` §5.2）：

| 相似度 | 动作 | 理由 |
| --- | --- | --- |
| 完全相同（哈希命中） | ``hit_count+1``，不新增 | 同一句话被抽了两次 |
| ``score ≥ memory_dedupe_threshold`` | 用较新者覆盖正文 | 同一件事的两种说法 |
| ``[0.85, threshold)`` | **两条并存** | 宁可冗余也不丢信息（可能是两件事） |
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.ids import new_id
from app.llm.base import LLMMessage
from app.memory.context_store import (
    ConversationStore,
    ConversationSummary,
    StoredMessage,
    now_iso,
)
from app.memory.extractor import MemoryExtractor
from app.memory.long_term import (
    CONFLICT_LOW,
    MemoryKind,
    MemoryRecord,
    MemoryRepo,
    MemoryWriteResult,
    content_sha256,
    encode_memory_cursor,
    expiring,
)
from app.memory.preferences import MemoryPreference, MemoryPreferenceStore
from app.memory.summary import SummaryBuilder, SummaryOutcome
from app.memory.vector_index import MemoryVectorIndex
from app.rag.base import RetrievedChunk
from app.rag.embedding.base import EmbeddingProvider, embed_texts
from app.services.context import AssembledContext, ContextAssembler, MemoryItem

logger = logging.getLogger("app.memory.service")

#: ``memory_save`` 工具 / ``POST /memories`` 的默认置信度（手动新增 = 用户明说）
DEFAULT_MANUAL_CONFIDENCE = 1.0


@dataclass(slots=True)
class ContextSnapshot:
    """上下文视图（诊断用，不带任何副作用）。"""

    messages: list[StoredMessage]
    summary: ConversationSummary | None
    assembled: AssembledContext


class MemoryService:
    """长期记忆与摘要的统一入口。"""

    def __init__(
        self,
        settings: Settings,
        repo: MemoryRepo,
        index: MemoryVectorIndex,
        store: ConversationStore,
        embedding: EmbeddingProvider,
        assembler: ContextAssembler,
        *,
        extractor: MemoryExtractor | None = None,
        summary_builder: SummaryBuilder | None = None,
        preferences: MemoryPreferenceStore | None = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._index = index
        self._store = store
        self._embedding = embedding
        self._assembler = assembler
        self._extractor = extractor
        self._summary = summary_builder
        self._preferences = preferences
        # 时钟可注入：冷却期与过期判定要能确定性测试（否则只能 sleep）
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # 属性（装配与测试需要读到「用的是哪一份」）
    # ------------------------------------------------------------------
    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def repo(self) -> MemoryRepo:
        return self._repo

    @property
    def index(self) -> MemoryVectorIndex:
        return self._index

    @property
    def store(self) -> ConversationStore:
        return self._store

    @property
    def summary_builder(self) -> SummaryBuilder | None:
        return self._summary

    # ------------------------------------------------------------------
    # 偏好
    # ------------------------------------------------------------------
    async def preference(self, user_id: str) -> MemoryPreference:
        """取用户级偏好（未设置时返回**全局开关给出的**默认值）。"""
        if self._preferences is None:
            return MemoryPreference(
                user_id=user_id,
                enabled=self._settings.memory_enabled,
                top_n=self._settings.memory_top_n,
            )
        return await self._preferences.get(
            user_id,
            default_top_n=self._settings.memory_top_n,
            default_enabled=self._settings.memory_enabled,
        )

    async def update_preference(
        self, user_id: str, *, enabled: bool | None = None, top_n: int | None = None
    ) -> MemoryPreference:
        """更新用户级偏好（``PUT /memory-settings``）。"""
        current = await self.preference(user_id)
        updated = MemoryPreference(
            user_id=user_id,
            enabled=current.enabled if enabled is None else enabled,
            top_n=current.top_n if top_n is None else max(1, min(20, top_n)),
            cleared_at=current.cleared_at,
        )
        if self._preferences is not None:
            await self._preferences.save(updated)
        logger.info(
            "memory.audit",
            extra={
                "action": "settings_update",
                "user_id": user_id,
                "enabled": updated.enabled,
                "top_n": updated.top_n,
            },
        )
        return updated

    async def is_enabled(self, user_id: str) -> bool:
        """长期记忆对该用户是否启用（``docs/07`` §5.4：关闭时既不写也不读）。"""
        return (await self.preference(user_id)).enabled

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def remember(
        self,
        content: str,
        *,
        user_id: str,
        kind: MemoryKind = "fact",
        confidence: float = DEFAULT_MANUAL_CONFIDENCE,
        expires_at: str | None = None,
        source_conversation_id: str = "",
        source: str = "auto",
        enforce_filters: bool = False,
    ) -> MemoryWriteResult:
        """写入一条长期记忆（精确 + 语义两层去重）。

        Args:
            enforce_filters: 由抽取器调用时为 ``True``（走完整过滤清单）；
                由用户/Agent 主动写入时为 ``False`` —— 手动说的是明确意图，
                不该被「看起来像疑问句」这类启发式规则挡掉。

        Raises:
            AppError: ``INVALID_ARGUMENT``（长度越界 / ``expires_at`` 已过期）。
        """
        text = content.strip()
        self._validate(text, expires_at=expires_at)

        # ① 精确去重：同一句话只保留一条，第二次只记「被用到过」
        existing = await self._repo.find_by_hash(user_id, text)
        if existing is not None:
            touched = await self._repo.touch(existing.id, confidence=confidence)
            logger.info(
                "memory.audit",
                extra={"action": "hit", "mem_id": touched.id, "user_id": user_id},
            )
            return MemoryWriteResult(record=touched, created=False)

        vector = await self._vectorize([text])
        first = vector[0]

        # ② 语义去重：相似度够高就认为是同一件事，用较新者覆盖正文
        hits = await self._index.search(first, user_id=user_id, top_k=1)
        if hits and hits[0].score >= self._settings.memory_dedupe_threshold:
            merged = await self._merge(hits[0].mem_id, user_id, text, kind, confidence, first)
            if merged is not None:
                return merged
            # 命中了一个已不在关系库里的向量（被容量淘汰但索引没清干净）：继续走新建

        # ③ 新建
        record = MemoryRecord(
            id=new_id("mem"),
            user_id=user_id,
            content=text,
            kind=kind,
            confidence=max(0.0, min(1.0, confidence)),
            expires_at=expires_at,
            source_conversation_id=source_conversation_id,
            source=source,
        )
        result = await self._repo.add(record)
        if result.created:
            await self._index.upsert(record.id, user_id, first, kind=kind)
        logger.info(
            "memory.audit",
            extra={
                "action": "create" if result.created else "hit",
                "mem_id": result.record.id,
                "user_id": user_id,
                "kind": result.record.kind,
                "source_conversation_id": source_conversation_id or None,
            },
        )
        return result

    async def _merge(
        self,
        mem_id: str,
        user_id: str,
        content: str,
        kind: MemoryKind,
        confidence: float,
        vector: list[float],
    ) -> MemoryWriteResult | None:
        """把新正文并入已有记忆（``docs/07`` §5.2 第 2 条）。"""
        try:
            target = await self._repo.get(mem_id, user_id)
        except AppError:
            return None
        merged = replace(
            target,
            content=content,
            kind=kind,
            # 必须显式重算哈希：``dataclasses.replace`` 会把旧值一并带上，
            # ``__post_init__`` 见非空就不再推导 —— 于是唯一索引指向旧哈希，
            # 下次「改回原文」会被判成重复而静默丢弃。
            content_sha256=content_sha256(content),
            confidence=max(target.confidence, confidence),
            hit_count=target.hit_count + 1,
            updated_at=now_iso(),
        )
        saved = await self._repo.save(merged)
        await self._index.upsert(saved.id, user_id, vector, kind=saved.kind)
        logger.info(
            "memory.audit",
            extra={"action": "merge", "mem_id": saved.id, "user_id": user_id},
        )
        return MemoryWriteResult(record=saved, created=False)

    def _validate(self, content: str, *, expires_at: str | None) -> None:
        settings = self._settings
        if len(content) < settings.memory_content_min_chars:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                f"记忆内容至少 {settings.memory_content_min_chars} 个字符",
                {"min_chars": settings.memory_content_min_chars},
            )
        if len(content) > settings.memory_content_max_chars:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                f"记忆内容最多 {settings.memory_content_max_chars} 个字符",
                {"max_chars": settings.memory_content_max_chars},
            )
        if expires_at:
            try:
                moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    "expires_at 不是合法的 RFC3339 时间",
                    {"expires_at": expires_at},
                ) from exc
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            if moment <= self._clock():
                # 写入即过期 = 调用方搞错了时区/单位，早点报错比存一条永远不生效的记忆好
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    "expires_at 必须晚于当前时间",
                    {"expires_at": expires_at},
                )

    # ------------------------------------------------------------------
    # 检索与注入
    # ------------------------------------------------------------------
    async def search(
        self, query: str, user_id: str, *, top_n: int | None = None
    ) -> list[MemoryItem]:
        """按语义检索长期记忆（``REQ-MEM-006``）；不可用时抛 ``AppError``。"""
        if not await self.is_enabled(user_id):
            return []
        query = query.strip()
        if not query:
            return []
        limit = top_n or (await self.preference(user_id)).top_n
        vector = (await self._vectorize([query]))[0]
        hits = await self._index.search(vector, user_id=user_id, top_k=max(1, limit))
        items: list[MemoryItem] = []
        for hit in hits:
            if hit.score < self._settings.memory_score_threshold:
                continue
            try:
                record = await self._repo.get(hit.mem_id, user_id)
            except AppError:
                # 索引里有、关系库里没有（容量淘汰后索引未清）：跳过而不是报错
                continue
            if not record.active:
                continue
            items.append(
                MemoryItem(
                    content=record.content,
                    score=hit.score,
                    mem_id=record.id,
                    kind=record.kind,
                )
            )
        return items

    async def expire_due(self, user_id: str) -> int:
        """把已到期的记忆标记为 ``expired``（保留记录，``docs/07`` §5.4）。"""
        records = await self._repo.all_for_user(user_id)
        due = expiring(records, now_iso())
        for record in due:
            await self._repo.save(record)
        if due:
            logger.info("memory.expired", extra={"user_id": user_id, "count": len(due)})
        return len(due)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def list_memories(
        self,
        user_id: str,
        *,
        kind: MemoryKind | None = None,
        expired: bool | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[MemoryRecord], str | None]:
        """分页列出，返回 ``(items, next_cursor)``。

        先跑一次到期标记：不标记的话 ``expired=false`` 这个筛选条件会把
        「已经到期但还没人访问过」的记忆也当成有效条目返回。
        """
        await self.expire_due(user_id)
        items, has_more = await self._repo.list_page(
            user_id, kind=kind, expired=expired, limit=limit, cursor=cursor
        )
        next_cursor = encode_memory_cursor(items[-1]) if has_more and items else None
        return items, next_cursor

    async def get(self, mem_id: str, user_id: str) -> MemoryRecord:
        return await self._repo.get(mem_id, user_id)

    async def update(
        self,
        mem_id: str,
        user_id: str,
        *,
        content: str | None = None,
        kind: MemoryKind | None = None,
        expires_at: str | None = None,
        clear_expiry: bool = False,
    ) -> MemoryRecord:
        """修改记忆；正文变化时**重新向量化**（``docs/07`` §6 ``PATCH``）。"""
        current = await self._repo.get(mem_id, user_id)
        text = current.content if content is None else content.strip()
        if content is not None:
            self._validate(text, expires_at=expires_at)
        target_expiry = None if clear_expiry else (expires_at or current.expires_at)
        # 重新判断过期状态：改正文/改时间都是「用户想让它在场」的信号，
        # 不重算会让「已过期」这个标记一直粘着，改了时间也回不来
        already_due = target_expiry is not None and target_expiry <= now_iso()
        updated = replace(
            current,
            content=text,
            kind=kind or current.kind,
            content_sha256=content_sha256(text),
            expires_at=target_expiry,
            expired=already_due,
            updated_at=now_iso(),
        )
        saved = await self._repo.save(updated)
        if content is not None:
            vector = (await self._vectorize([text]))[0]
            await self._index.upsert(saved.id, user_id, vector, kind=saved.kind)
        logger.info(
            "memory.audit",
            extra={"action": "update", "mem_id": saved.id, "user_id": user_id},
        )
        return saved

    async def delete(self, mem_id: str, user_id: str) -> MemoryRecord:
        """删除单条：关系库与向量库**成对**删除（``REQ-MEM-007``）。"""
        record = await self._repo.delete(mem_id, user_id)
        await self._index.delete(mem_id)
        logger.info(
            "memory.audit",
            extra={"action": "delete", "mem_id": mem_id, "user_id": user_id},
        )
        return record

    async def delete_all(self, user_id: str) -> int:
        """清空该用户全部记忆（双删），并开启「不再重新抽取」的冷却期。"""
        removed = await self._repo.delete_all(user_id)
        await self._index.delete_all(user_id)
        if self._preferences is not None:
            await self._preferences.mark_cleared(user_id, at=self._clock())
        logger.info(
            "memory.audit",
            extra={
                "action": "delete_all",
                "user_id": user_id,
                "count": removed,
                "cooldown_hours": self._settings.memory_clear_cooldown_hours,
            },
        )
        return removed

    # ------------------------------------------------------------------
    # 抽取
    # ------------------------------------------------------------------
    async def extract_and_store(
        self,
        messages: Sequence[StoredMessage],
        *,
        user_id: str,
        conversation_id: str = "",
    ) -> list[MemoryRecord]:
        """从一轮对话抽取长期记忆并落库（``REQ-MEM-004``）。

        Returns:
            真正新增的记录（命中去重的不算）。失败时返回空列表并由调用方记
            ``degraded`` —— 抽取失败不该让对话失败。
        """
        settings = self._settings
        if self._extractor is None or not settings.memory_extract_enabled:
            return []
        if not await self.is_enabled(user_id):
            return []
        preference = await self.preference(user_id)
        if preference.is_cooling_down(
            hours=settings.memory_clear_cooldown_hours, now=self._clock()
        ):
            # 用户刚清空过：这段时间内不把旧内容重新写回来（``REQ-MEM-007``）
            logger.info("memory.extract_skipped", extra={"reason": "clear_cooldown"})
            return []

        candidates, rejected = await self._extractor.extract(messages)
        if rejected:
            logger.info(
                "memory.extract_rejected",
                extra={"count": len(rejected), "reasons": sorted({r.reason for r in rejected})},
            )
        created: list[MemoryRecord] = []
        for candidate in candidates:
            result = await self.remember(
                candidate.content,
                user_id=user_id,
                kind=_as_kind(candidate.kind),
                confidence=candidate.confidence,
                source_conversation_id=conversation_id,
            )
            if result.created:
                created.append(result.record)
        if created:
            logger.info(
                "memory.extracted",
                extra={"user_id": user_id, "created_count": len(created)},
            )
        return created

    # ------------------------------------------------------------------
    # 上下文视图（``GET /conversations/{id}/context``）
    # ------------------------------------------------------------------
    async def context_overview(
        self,
        conversation_id: str,
        user_id: str,
        *,
        query: str = "",
        rag_chunks: Sequence[RetrievedChunk] = (),
        extra_system: str = "",
    ) -> ContextSnapshot:
        """算出「下一轮真实会发给模型的 messages 与 token 占用」。

        刻意用**同一套** :class:`~app.services.context.ContextAssembler` 与真实对话
        完全一致地装配一次，而不是另外拼一份「看起来差不多」的统计：两份实现
        迟早会不一致，而不一致的代价是这个排障接口开始骗人。

        ``query`` 默认为空（这是一个不带用户新输入的诊断视角），此时 ``query``
        片段的 token 为 0，``history`` 按预算裁剪的基准也会相应宽松 —— 看
        ``used.history`` 时要知道这一点。
        """
        settings = self._settings
        await self._store.ensure(conversation_id, user_id)
        messages = await self._store.all_messages(conversation_id, user_id)
        summary = await self._store.summary(conversation_id, user_id)
        # 注入用的历史必须是 ``recent``（已摘要覆盖的部分会被过滤掉），
        # 而消息列表用 ``all_messages`` —— 两者混用会看不出「摘要是不是生效了」
        recent = await self._store.recent(
            conversation_id, user_id, turns=settings.memory_recent_turns
        )
        memories = await self.search(query, user_id) if query.strip() else []
        assembled = self._assembler.build(
            query=query,
            history=[LLMMessage(role=item.role, content=item.content) for item in recent],
            memories=memories,
            summary=summary.content if summary else None,
            rag_chunks=rag_chunks,
            extra_system=extra_system,
        )
        return ContextSnapshot(messages=messages, summary=summary, assembled=assembled)

    # ------------------------------------------------------------------
    # 摘要
    # ------------------------------------------------------------------
    async def summary(self, conversation_id: str, user_id: str) -> ConversationSummary | None:
        return await self._store.summary(conversation_id, user_id)

    async def build_summary(
        self, conversation_id: str, user_id: str, *, force: bool = False
    ) -> SummaryOutcome | None:
        """生成摘要（``force=True`` 对应 ``POST /summary/rebuild``）。"""
        if self._summary is None:
            return None
        if force:
            # 手动重建必须立即生效，不受 5 分钟防抖窗口限制
            self._summary.reset_debounce(conversation_id)
        return await self._summary.build(conversation_id, user_id, force=force)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _vectorize(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            return await embed_texts(self._embedding, list(texts))
        except AppError:
            raise
        except Exception as exc:  # 向量化失败：交给上层降级
            raise AppError(
                ErrorCode.RETRIEVAL_FAILED,
                f"记忆向量化失败：{exc}",
                {"count": len(texts)},
            ) from exc


def _as_kind(value: str) -> MemoryKind:
    return "preference" if value == "preference" else "fact"


__all__ = [
    "CONFLICT_LOW",
    "DEFAULT_MANUAL_CONFIDENCE",
    "ContextSnapshot",
    "MemoryService",
]
