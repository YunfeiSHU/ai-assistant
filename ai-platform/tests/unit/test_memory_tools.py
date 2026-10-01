"""长期记忆内置工具单测（``memory_save`` / ``memory_search``，``docs/07`` §5）。

这两个工具是 Agent 的**自主**记忆通道，与轮末自动抽取互补。这里覆盖三件容易
只写一半的事：

* ``memory_save`` 必须声明 ``side_effect="write"`` —— 声明成 ``read`` 会让
  「本轮不允许写」的护栏直接失效（执行器只看这个字段）；
* 记忆层不可用要转成 ``ToolExecutionError``（可回复的工具结果），而不是把
  ``AppError`` 原样抛给 Agent 循环 —— 否则依赖故障会变成整轮失败；
* 空结果要带 ``hint``，否则模型会以为工具没被调用而反复重试。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from tests.conftest import build_settings
from tests.support.fake_llm import tool_call
from tests.support.memory import DEFAULT_DIM, ScriptedEmbedding
from tests.support.memory import blend as _blend
from tests.support.memory import direction as _direction

from app.application.context import ContextAssembler
from app.application.memory import MemoryService
from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.memory.context_store import InMemoryConversationStore
from app.memory.long_term import InMemoryMemoryRepo
from app.memory.vector_index import InMemoryMemoryVectorIndex
from app.tools.base import ToolContext, ToolExecutionError
from app.tools.builtin.memory import (
    SAVE_TOOL_NAME,
    SEARCH_TOOL_NAME,
    MemorySaveArgs,
    MemorySaveTool,
    MemorySearchArgs,
    MemorySearchTool,
)
from app.tools.executor import ERROR_WRITE_FORBIDDEN, ToolExecutor
from app.tools.registry import ToolRegistry

DIM = DEFAULT_DIM
USER = "u_mem_tool"


def _service(
    settings: Settings | None = None, *, embedding: ScriptedEmbedding | None = None
) -> MemoryService:
    resolved = settings or build_settings()
    store = InMemoryConversationStore(resolved)
    return MemoryService(
        resolved,
        InMemoryMemoryRepo(max_items=resolved.memory_max_items),
        InMemoryMemoryVectorIndex(dim=DIM),
        store,
        embedding or ScriptedEmbedding(),
        ContextAssembler(resolved),
    )


def _ctx(**kwargs: Any) -> ToolContext:
    return ToolContext(user_id=USER, **kwargs)


def test_save_tool_is_declared_as_write() -> None:
    """写声明是护栏的**唯一**依据；这里钉住它，避免以后被顺手改成 read。"""
    tool = MemorySaveTool(_service())
    assert tool.name == SAVE_TOOL_NAME
    assert tool.side_effect == "write"
    assert MemorySearchTool(_service()).side_effect == "read"


async def test_save_tool_writes_and_reports_created() -> None:
    """``memory_save`` 落库成功并回 ``created``/``mem_id``，同时记下来源会话与置信度。"""
    service = _service()
    tool = MemorySaveTool(service)
    outcome = await tool.run(
        MemorySaveArgs(content="用户偏好用表格回答", kind="preference", confidence=0.95),
        _ctx(conversation_id="cv_source"),
    )
    assert outcome.status == "ok"
    assert outcome.payload["created"] is True
    assert outcome.payload["kind"] == "preference"
    assert outcome.payload["hit_count"] == 1
    assert outcome.summary.startswith("已记住")
    record = await service.repo.get(outcome.payload["mem_id"], USER)
    assert record is not None
    # 工具路径也要落来源会话，否则无法回溯「这条记忆是哪轮对话说的」
    assert record.source_conversation_id == "cv_source"
    assert record.confidence == 0.95


async def test_save_tool_reports_existing_content() -> None:
    """重复写入是**正常**路径（模型常重复调用），summary 必须说清「已存在」。"""
    service = _service()
    tool = MemorySaveTool(service)
    await tool.run(MemorySaveArgs(content="用户偏好用表格回答"), _ctx())
    again = await tool.run(MemorySaveArgs(content="用户偏好用表格回答"), _ctx())

    assert again.payload["created"] is False
    assert again.payload["hit_count"] == 2
    assert "已存在" in again.summary
    assert await service.repo.count(USER) == 1


async def test_save_tool_maps_unknown_kind_to_fact() -> None:
    """模型给出未识别的 kind 时退化为 fact，而不是报错打断整轮。"""
    service = _service()
    outcome = await MemorySaveTool(service).run(
        MemorySaveArgs(content="用户偏好用表格回答", kind="something_else"), _ctx()
    )
    assert outcome.payload["kind"] == "fact"


async def test_save_tool_rejects_too_short_content() -> None:
    """参数校验交给 pydantic（``min_length``），不用自己再写一遍长度判断。"""
    with pytest.raises(ValidationError) as excinfo:
        MemorySaveArgs(content="短")
    assert "content" in str(excinfo.value)


async def test_save_tool_maps_dependency_failure() -> None:
    """写入被依赖条件拒绝（内容太短触发的依赖故障）⇒ ``ToolExecutionError``，不抛出领域错误。"""
    service = _service(build_settings(memory_content_min_chars=50))
    with pytest.raises(ToolExecutionError) as excinfo:
        await MemorySaveTool(service).run(MemorySaveArgs(content="用户偏好用表格回答"), _ctx())
    assert "长期记忆暂不可用" in str(excinfo.value)


async def test_search_tool_returns_hits() -> None:
    """命中项必须含 ``mem_id``/``content``/``kind``/``score`` 四元组，且与非空结果不带 ``hint``。"""
    embedding = ScriptedEmbedding(
        {"用户偏好用表格回答": _direction(1.0), "用户的回答风格偏好": _direction(1.0)}
    )
    service = _service(embedding=embedding)
    await service.remember("用户偏好用表格回答", user_id=USER, kind="preference")
    outcome = await MemorySearchTool(service).run(
        MemorySearchArgs(query="用户的回答风格偏好", top_k=3), _ctx()
    )

    assert outcome.payload["total"] == 1
    assert outcome.payload["query"] == "用户的回答风格偏好"
    # 返回字段是 docs/04 §3 写明的四元组，不能只回 content/score
    hit = outcome.payload["memories"][0]
    assert set(hit) == {"mem_id", "content", "kind", "score"}
    assert hit["mem_id"].startswith("mem_")
    assert hit["kind"] == "preference"
    assert hit["content"] == "用户偏好用表格回答"
    assert hit["score"] > 0.9
    listed, _cursor = await service.list_memories(USER)
    assert hit["mem_id"] == listed[0].id
    assert "hint" not in outcome.payload
    assert outcome.summary.startswith("命中 1 条")


async def test_search_tool_suggests_when_empty() -> None:
    """查空必须带 ``hint``：不然模型会以为工具没被调用而反复重试。"""
    service = _service()
    outcome = await MemorySearchTool(service).run(
        MemorySearchArgs(query="用户的回答风格偏好"), _ctx()
    )

    assert outcome.payload["total"] == 0
    assert outcome.payload["memories"] == []
    assert "hint" in outcome.payload
    assert outcome.summary == "命中 0 条长期记忆（无）"


async def test_search_tool_respects_top_n() -> None:
    """返回条数不超过入参 ``top_k``（``top_k=2`` 时不回 5 条）。"""
    contents = [f"用户的第 {index} 条稳定偏好" for index in range(5)]
    embedding = ScriptedEmbedding(
        {
            "稳定偏好查询": _direction(1.0),
            **{text: _blend(index, 0.9) for index, text in enumerate(contents)},
        }
    )
    service = _service(embedding=embedding)
    for text in contents:
        await service.remember(text, user_id=USER)

    outcome = await MemorySearchTool(service).run(
        MemorySearchArgs(query="稳定偏好查询", top_k=2), _ctx()
    )
    assert outcome.payload["total"] == 2


async def test_search_tool_never_sees_other_users_memories() -> None:
    """工具用的是 ``ctx.user_id``，不接收用户参数 —— 越权在结构上就不可能。"""
    embedding = ScriptedEmbedding({"用户的回答风格偏好": _direction(1.0)})
    service = _service(embedding=embedding)
    await service.remember("用户偏好用表格回答", user_id="u_other")

    outcome = await MemorySearchTool(service).run(
        MemorySearchArgs(query="用户的回答风格偏好"), _ctx()
    )
    assert outcome.payload["total"] == 0


async def test_search_tool_maps_dependency_failure() -> None:
    """检索抛 ``AppError`` 时转为 ``ToolExecutionError``（依赖故障不该打断整轮）。"""

    class _Boom(MemoryService):
        async def search(self, *args: Any, **kwargs: Any) -> Any:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "向量库不可用")

    service = _Boom(
        build_settings(),
        InMemoryMemoryRepo(),
        InMemoryMemoryVectorIndex(dim=DIM),
        InMemoryConversationStore(build_settings()),
        ScriptedEmbedding(),
        ContextAssembler(build_settings()),
    )
    with pytest.raises(ToolExecutionError) as excinfo:
        await MemorySearchTool(service).run(MemorySearchArgs(query="任意主题"), _ctx())
    assert "长期记忆暂不可用" in str(excinfo.value)


def test_tools_expose_json_schema_names() -> None:
    """工具名与参数模型是对外契约（``GET /tools`` 直接暴露），改名字要一起改文档。"""
    assert MemorySaveTool.input_model is MemorySaveArgs
    assert MemorySearchTool.input_model is MemorySearchArgs
    assert SEARCH_TOOL_NAME == "memory_search"
    assert set(MemorySaveArgs.model_json_schema()["properties"]) == {
        "content",
        "kind",
        "confidence",
    }
    # 参数名必须与 docs/04 §3 一致：是 top_k 不是 top_n
    assert set(MemorySearchArgs.model_json_schema()["properties"]) == {"query", "top_k"}
    assert MemorySaveArgs.model_fields["confidence"].default == 1.0


@pytest.mark.parametrize("kind", ["preference", "fact"])
async def test_save_tool_accepts_both_kinds(kind: str) -> None:
    """``preference`` 与 ``fact`` 两类都要能写入，且回显的 ``kind`` 与入参一致。"""
    outcome = await MemorySaveTool(_service()).run(
        MemorySaveArgs(content=f"用户的第 {kind} 类信息", kind=kind), _ctx()
    )
    assert outcome.payload["kind"] == kind


# ---------------------------------------------------------------------------
# 经执行器：写护栏必须真的拦住
# ---------------------------------------------------------------------------
async def test_executor_blocks_memory_save_when_write_not_allowed() -> None:
    """``side_effect="write"`` 的**唯一**用途就是被这里拦住。

    原则（12-§9 第 6 条）：护栏必须有一条专门证明「它拦住了」的测试。
    没有这条用例，把 ``side_effect`` 改成 ``read`` 不会有任何测试变红。
    """
    service = _service()
    registry = ToolRegistry()
    registry.register(MemorySaveTool(service))
    executor = ToolExecutor(build_settings(), registry)

    records = await executor.execute(
        [tool_call(SAVE_TOOL_NAME, {"content": "用户偏好用表格回答"})],
        _ctx(allow_write=False),
    )

    assert records[0].error == ERROR_WRITE_FORBIDDEN
    assert await service.repo.count(USER) == 0


async def test_executor_runs_memory_save_when_allowed() -> None:
    """放行时经执行器写入成功：顺带验证 JSON 参数 → pydantic 的整条路径。"""
    service = _service()
    registry = ToolRegistry()
    registry.register(MemorySaveTool(service))
    executor = ToolExecutor(build_settings(), registry)

    records = await executor.execute(
        [
            tool_call(
                SAVE_TOOL_NAME,
                {"content": "用户偏好用表格回答", "kind": "preference", "confidence": 0.8},
            )
        ],
        _ctx(allow_write=True),
    )

    assert records[0].ok
    assert records[0].payload["kind"] == "preference"
    record = await service.repo.get(records[0].payload["mem_id"], USER)
    assert record is not None
    assert record.confidence == 0.8


async def test_executor_reports_invalid_arguments_as_replyable_result() -> None:
    """``REQ-AGENT-006``：参数不合法要变成「可回复的工具结果」，而不是整轮失败。"""
    registry = ToolRegistry()
    registry.register(MemorySaveTool(_service()))
    executor = ToolExecutor(build_settings(), registry)

    records = await executor.execute([tool_call(SAVE_TOOL_NAME, {"content": "短"})], _ctx())
    assert not records[0].ok
    assert records[0].error == "invalid_arguments"
