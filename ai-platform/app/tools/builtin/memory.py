"""长期记忆工具：``memory_save`` / ``memory_search``（``docs/04`` §3、``docs/07`` §5）。

这两个工具让 Agent **自主**决定什么时候记、什么时候查，与「每轮对话结束后自动抽取」
（``memory_extract`` 任务）是互补的两条路径：

| 路径 | 触发者 | 适用场景 |
| --- | --- | --- |
| 自动抽取 | 服务端 | 用户随口说出的稳定偏好 |
| ``memory_save`` | 模型 | 用户明确要求「记住这件事」 |
| ``memory_search`` | 模型 | 用户提到「我之前说过」而当前上下文里没有 |

``memory_save`` 的 ``side_effect="write"`` 是**关键声明**：它会被
:class:`~app.tools.executor.ToolExecutor` 串行执行、并受写操作放行开关约束
（``docs/04`` §4.3）。把它标成 ``read`` 会让「本轮不允许写」的护栏直接失效。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.application.memory import MemoryService
from app.core.exceptions import AppError
from app.memory.long_term import MemoryKind
from app.tools.base import BuiltinTool, ToolContext, ToolExecutionError, ToolOutcome

SAVE_TOOL_NAME = "memory_save"
SEARCH_TOOL_NAME = "memory_search"

SAVE_DESCRIPTION = (
    "把一条关于用户的长期信息（偏好或稳定事实）写入长期记忆，供以后所有会话使用。"
    "仅当用户**明确要求记住**、或某条信息明显会长期有效时才使用；"
    "一次性任务、临时上下文、密码/密钥/证件号一律不要写入。"
    "写入前无需先查询是否已存在，重复内容会被自动合并。"
)

SEARCH_DESCRIPTION = (
    "按语义检索关于当前用户的长期记忆（偏好、稳定事实）。"
    "当用户提到「我之前说过的」「按我的习惯」而当前上下文里没有相关信息时使用；"
    "不要用它检索知识库文档（那用 kb_retrieve）。"
)


class MemorySaveArgs(BaseModel):
    """``memory_save`` 参数（``docs/04`` §3）。"""

    content: str = Field(
        min_length=5,
        max_length=500,
        description="一句话描述要记住的信息，用第三人称陈述句，一条只讲一件事",
    )
    kind: str = Field(
        default="fact",
        description="preference=稳定偏好/习惯；fact=关于用户的稳定事实",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="确信程度 0..1；用户明确说出的用默认值 1.0，推测出来的调低",
    )


class MemorySearchArgs(BaseModel):
    """``memory_search`` 参数（``docs/04`` §3）。"""

    query: str = Field(min_length=1, max_length=200, description="要检索的语义主题")
    #: 字段名与工具层惯用名一致（``kb_retrieve`` 也用 ``top_k``），不要写成 ``top_n``。
    top_k: int = Field(default=3, ge=1, le=10, description="返回条数上限")


class MemorySaveTool(BuiltinTool):
    """写入长期记忆。"""

    name = SAVE_TOOL_NAME
    description = SAVE_DESCRIPTION
    input_model = MemorySaveArgs
    #: 写工具：执行器会串行执行，并要求 ``ctx.allow_write``
    side_effect = "write"
    timeout_seconds = 10.0
    example_arguments: ClassVar[dict[str, Any]] = {
        "content": "用户偏好简洁回答，不喜欢长列表",
        "kind": "preference",
        "confidence": 1.0,
    }

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = MemorySaveArgs.model_validate(arguments)
        kind: MemoryKind = "preference" if args.kind == "preference" else "fact"
        try:
            result = await self._memory.remember(
                args.content,
                user_id=ctx.user_id,
                kind=kind,
                confidence=args.confidence,
                source_conversation_id=ctx.conversation_id or "",
            )
        except AppError as exc:
            # 记忆层不可用属于「依赖故障」，作为可回复的工具结果返回
            raise ToolExecutionError(f"长期记忆暂不可用：{exc.message}") from exc
        verb = "已记住" if result.created else "已存在（已更新命中次数）"
        return ToolOutcome(
            payload={
                "mem_id": result.record.id,
                "created": result.created,
                "kind": result.record.kind,
                "hit_count": result.record.hit_count,
            },
            summary=f"{verb}：{result.record.content}",
        )


class MemorySearchTool(BuiltinTool):
    """检索长期记忆。"""

    name = SEARCH_TOOL_NAME
    description = SEARCH_DESCRIPTION
    input_model = MemorySearchArgs
    side_effect = "read"
    timeout_seconds = 10.0
    example_arguments: ClassVar[dict[str, Any]] = {"query": "用户的回答风格偏好", "top_k": 3}

    def __init__(self, memory: MemoryService) -> None:
        self._memory = memory

    async def run(self, arguments: BaseModel, ctx: ToolContext) -> ToolOutcome:
        args = MemorySearchArgs.model_validate(arguments)
        try:
            items = await self._memory.search(args.query, ctx.user_id, top_n=args.top_k)
        except AppError as exc:
            raise ToolExecutionError(f"长期记忆暂不可用：{exc.message}") from exc
        payload: dict[str, Any] = {
            "query": args.query,
            "total": len(items),
            "memories": [
                {
                    "mem_id": item.mem_id,
                    "content": item.content,
                    "kind": item.kind,
                    "score": round(item.score, 4),
                }
                for item in items
            ],
        }
        if not items:
            # 明确告诉模型「查了但没查到」，否则它会以为工具没被调用而反复重试
            payload["hint"] = "该用户没有匹配的长期记忆，请基于当前对话回答"
        summary = f"命中 {len(items)} 条长期记忆" + (f"：{items[0].content}" if items else "（无）")
        return ToolOutcome(payload=payload, summary=summary)


__all__ = [
    "SAVE_DESCRIPTION",
    "SAVE_TOOL_NAME",
    "SEARCH_DESCRIPTION",
    "SEARCH_TOOL_NAME",
    "MemorySaveArgs",
    "MemorySaveTool",
    "MemorySearchArgs",
    "MemorySearchTool",
]
