"""记忆路由（``docs/07`` §6）。

这个文件承载三组路径：``/memories``、``/memory-settings``、
``/conversations/{id}/context|summary``。它们放同一个文件而不是各起一个模块，
是因为它们共用同一份「记忆能力是否对该用户开启」的判断 —— 拆开后那段判断
会在三处各写一遍，而「一处漏判」的后果是隐私需求（``REQ-MEM-007``）静默失效。

两处刻意与「通用做法」不同：

* ``DELETE /memories`` 必须显式 ``?all=true``（``AC-MEM-10``）：清空全部记忆不
  提供无参形式，防误删。
* ``GET /memory-settings`` 与 ``GET /memories`` 在能力关闭时返回 ``409`` 而不是
  空列表：空列表会让「关闭了记忆」和「记忆里确实什么都没有」看起来一模一样。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    MemoryServiceDep,
    PaginationDep,
    TaskRunnerDep,
    TaskServiceDep,
    UserId,
)
from app.core.errors import AppError, ErrorCode
from app.core.tokens import count_tokens
from app.memory.long_term import MemoryKind, MemoryRecord
from app.schemas.memory import (
    ContextBudgetOut,
    ContextMessageOut,
    ContextSummaryOut,
    ConversationContextOut,
    MemoryCreate,
    MemoryList,
    MemoryOut,
    MemorySettingsOut,
    MemorySettingsUpdate,
    MemoryUpdate,
    SummaryOut,
    SummaryRebuildAccepted,
)
from app.services.memory import MemoryService
from app.tasks.models import ResourceType, TaskType

logger = logging.getLogger("app.api.memory")

router = APIRouter(tags=["记忆"])

_MEMORY_DISABLED_HINT = "记忆能力已关闭；PUT /memory-settings 传 memory_enabled=true 可重新开启"


def _out(record: MemoryRecord) -> MemoryOut:
    return MemoryOut.model_validate(record.to_dict())


async def _ensure_enabled(service: MemoryService, user_id: str) -> None:
    """能力关闭时拒绝读写长期记忆（``docs/07`` §5.4）。

    用 ``409`` 而不是 ``403``：这不是权限问题，而是**当前状态**不允许该操作，
    改一下设置就能重试。
    """
    if not await service.is_enabled(user_id):
        raise AppError(
            ErrorCode.CONFLICT,
            "记忆能力已关闭",
            {"hint": _MEMORY_DISABLED_HINT, "memory_enabled": False},
        )


# ----------------------------------------------------------------------
# 长期记忆
# ----------------------------------------------------------------------
@router.get("/memories", response_model=MemoryList, summary="列出长期记忆")
async def list_memories(
    user_id: UserId,
    service: MemoryServiceDep,
    pagination: PaginationDep,
    kind: Annotated[str | None, Query(description="preference | fact")] = None,
    expired: Annotated[bool | None, Query(description="按过期状态过滤")] = None,
) -> MemoryList:
    """分页列出；``kind`` / ``expired`` 可选过滤。"""
    await _ensure_enabled(service, user_id)
    items, next_cursor = await service.list_memories(
        user_id,
        kind=_parse_kind(kind),
        expired=expired,
        limit=pagination.limit,
        cursor=pagination.cursor,
    )
    return MemoryList(
        items=[_out(record) for record in items],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.post(
    "/memories",
    response_model=MemoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增长期记忆",
)
async def create_memory(
    body: MemoryCreate, user_id: UserId, service: MemoryServiceDep
) -> MemoryOut:
    """手动新增（``docs/07`` §6.2）；重复内容不会新建而是累加 ``hit_count``。"""
    await _ensure_enabled(service, user_id)
    result = await service.remember(
        body.content,
        user_id=user_id,
        kind=body.kind,
        confidence=body.confidence,
        expires_at=body.expires_at,
        # 用户显式调接口写入 → ``manual``（落到 ``user_memory.source`` 列）。
        # 与轮末抽取/``memory_save`` 工具的 ``auto`` 区分开，排查
        # 「为什么模型记得这个」时才有依据（``REQ-MEM-007``）。
        source="manual",
    )
    return _out(result.record)


@router.delete("/memories", status_code=status.HTTP_204_NO_CONTENT, summary="清空全部记忆")
async def delete_all_memories(
    user_id: UserId,
    service: MemoryServiceDep,
    all_: Annotated[bool, Query(alias="all", description="必须显式传 true")] = False,
) -> None:
    """清空该用户全部记忆（``REQ-MEM-007``）；未带 ``all=true`` → ``400``。"""
    if all_ is not True:
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            "清空全部记忆需要显式指定 all=true",
            {"hint": "该接口会删除用户全部长期记忆，不接受无参调用以防误删"},
        )
    count = await service.delete_all(user_id)
    logger.info("memory.cleared", extra={"user_id": user_id, "count": count})


@router.get("/memories/{mem_id}", response_model=MemoryOut, summary="记忆详情")
async def get_memory(mem_id: str, user_id: UserId, service: MemoryServiceDep) -> MemoryOut:
    """取详情；跨用户返回 ``404 MEMORY_NOT_FOUND``。"""
    await _ensure_enabled(service, user_id)
    return _out(await service.get(mem_id, user_id))


@router.patch("/memories/{mem_id}", response_model=MemoryOut, summary="修改记忆")
async def update_memory(
    mem_id: str, body: MemoryUpdate, user_id: UserId, service: MemoryServiceDep
) -> MemoryOut:
    """修改正文/类型/过期时间；正文变化会**重新向量化**。"""
    await _ensure_enabled(service, user_id)
    clear_expiry = "expires_at" in body.model_fields_set and body.expires_at is None
    record = await service.update(
        mem_id,
        user_id,
        content=body.content,
        kind=body.kind,
        expires_at=body.expires_at,
        clear_expiry=clear_expiry,
    )
    return _out(record)


@router.delete("/memories/{mem_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除单条记忆")
async def delete_memory(mem_id: str, user_id: UserId, service: MemoryServiceDep) -> None:
    """删除单条（关系库 + 向量库成对删除）。"""
    await service.delete(mem_id, user_id)


# ----------------------------------------------------------------------
# 用户级设置
# ----------------------------------------------------------------------
@router.get("/memory-settings", response_model=MemorySettingsOut, summary="查询记忆设置")
async def get_memory_settings(user_id: UserId, service: MemoryServiceDep) -> MemorySettingsOut:
    """查询 ``memory_enabled`` 与 ``memory_top_n``。"""
    preference = await service.preference(user_id)
    return MemorySettingsOut(
        memory_enabled=preference.enabled,
        memory_top_n=preference.top_n,
        cleared_at=preference.cleared_at.isoformat() if preference.cleared_at else None,
    )


@router.put("/memory-settings", response_model=MemorySettingsOut, summary="更新记忆设置")
async def update_memory_settings(
    body: MemorySettingsUpdate, user_id: UserId, service: MemoryServiceDep
) -> MemorySettingsOut:
    """更新设置（只改传了的字段）。"""
    preference = await service.update_preference(
        user_id, enabled=body.memory_enabled, top_n=body.memory_top_n
    )
    return MemorySettingsOut(
        memory_enabled=preference.enabled,
        memory_top_n=preference.top_n,
        cleared_at=preference.cleared_at.isoformat() if preference.cleared_at else None,
    )


# ----------------------------------------------------------------------
# 会话上下文与摘要
# ----------------------------------------------------------------------
@router.get(
    "/conversations/{conversation_id}/context",
    response_model=ConversationContextOut,
    summary="查看会话上下文",
)
async def get_conversation_context(
    conversation_id: str, user_id: UserId, service: MemoryServiceDep
) -> ConversationContextOut:
    """消息列表 + 各片段 token 占用 + 摘要状态（``docs/07`` §6.1）。"""
    snapshot = await service.context_overview(conversation_id, user_id)
    summary = snapshot.summary
    return ConversationContextOut(
        conversation_id=conversation_id,
        message_count=len(snapshot.messages),
        messages=[
            ContextMessageOut(
                role=item.role,
                message_id=item.message_id,
                content=item.content,
                # ``tokens`` 现算：落库时不存它，避免「换了 tokenizer 之后历史值
                # 全是错的」这种没法修的存量数据
                tokens=count_tokens(item.content),
                created_at=item.created_at,
                partial=item.partial,
            )
            for item in snapshot.messages
        ],
        summary=ContextSummaryOut(
            exists=summary is not None,
            covered_until=summary.covered_until if summary else None,
            token_count=summary.token_count if summary else 0,
        ),
        budget=ContextBudgetOut(
            context_token_budget=service.settings.context_token_budget,
            used=dict(snapshot.assembled.token_by_part),
            total_tokens=snapshot.assembled.total_tokens,
            trimmed=dict(snapshot.assembled.trimmed),
        ),
    )


@router.delete(
    "/conversations/{conversation_id}/context",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="清空会话上下文",
)
async def delete_conversation_context(
    conversation_id: str, user_id: UserId, service: MemoryServiceDep
) -> None:
    """清空短期上下文（幂等）；**不删**长期记忆（``docs/07`` §5.4）。"""
    await service.store.clear(conversation_id, user_id)


@router.get(
    "/conversations/{conversation_id}/summary", response_model=SummaryOut, summary="查看摘要"
)
async def get_conversation_summary(
    conversation_id: str, user_id: UserId, service: MemoryServiceDep
) -> SummaryOut:
    """取摘要；尚未生成 → ``404 SUMMARY_UNAVAILABLE``。"""
    summary = await service.summary(conversation_id, user_id)
    if summary is None or not summary.content.strip():
        raise AppError(
            ErrorCode.SUMMARY_UNAVAILABLE,
            "该会话尚未生成摘要",
            {"conversation_id": conversation_id},
        )
    return SummaryOut(
        conversation_id=conversation_id,
        content=summary.content,
        covered_until=summary.covered_until,
        source_message_count=summary.source_message_count,
        token_count=summary.token_count,
    )


@router.post(
    "/conversations/{conversation_id}/summary/rebuild",
    response_model=SummaryRebuildAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重建摘要",
)
async def rebuild_conversation_summary(
    conversation_id: str,
    user_id: UserId,
    service: MemoryServiceDep,
    tasks: TaskServiceDep,
    runner: TaskRunnerDep,
) -> SummaryRebuildAccepted:
    """异步重建摘要，返回 ``task_id``（``docs/07`` §6）。

    先 ``ensure`` 一次会话归属：不校验的话，任何登录用户都能凭一个猜到的
    ``conversation_id`` 建出一堆摘要任务（虽然它们最终会因为读不到消息而空转，
    但「能给别人建任务」本身就不该成立）。
    """
    await service.store.ensure(conversation_id, user_id)
    task, created = await tasks.create(
        type_=TaskType.SUMMARY_BUILD,
        user_id=user_id,
        resource_type=ResourceType.CONVERSATION,
        resource_id=conversation_id,
        payload={"conversation_id": conversation_id, "force": True},
    )
    if created:
        await runner.submit(task)
    logger.info(
        "memory.summary_rebuild_accepted",
        extra={"conversation_id": conversation_id, "task_id": task.id, "is_new": created},
    )
    return SummaryRebuildAccepted(task_id=task.id, status=str(task.status))


def _parse_kind(value: str | None) -> MemoryKind | None:
    """解析 ``kind`` 查询参数。"""
    if not value:
        return None
    if value not in ("preference", "fact"):
        raise AppError(
            ErrorCode.INVALID_ARGUMENT,
            f"kind 取值非法：{value}",
            {"allowed": ["preference", "fact"]},
        )
    return "preference" if value == "preference" else "fact"


__all__ = ["router"]
