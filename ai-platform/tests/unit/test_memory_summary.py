"""摘要生成单测（``REQ-MEM-003``，``docs/07`` §3）。

三个「写错也不会报错、只是效果变差」的点，各自有对应用例：

* 缺段补齐：模型少写一段时，下游拼出的 system 片段会静默少一块信息；
* 增量合并：只摘「新消息」会让用户在第 3 轮说过的偏好永久消失；
* 失败只降级：摘要失败的正确后果是「这一轮上下文长一点」，而不是发不出消息。
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.support.fake_llm import FakeLLM

from app.core.config import Settings
from app.memory.context_store import (
    ConversationSummary,
    InMemoryConversationStore,
    StoredMessage,
)
from app.memory.summary import (
    SUMMARY_SECTIONS,
    SummaryBuilder,
    ensure_structure,
    should_summarize,
)

_RICH_REPLY = """## 用户目标
- 想了解退款政策
## 已确认事实
- 订单号 A123
## 未决问题
- 退款到账时间
## 用户偏好
- 喜欢简洁回答
"""


def _store(settings: Settings) -> InMemoryConversationStore:
    return InMemoryConversationStore(settings)


async def _append(store: InMemoryConversationStore, conversation_id: str, count: int) -> None:
    messages = [
        StoredMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"第 {index} 条消息",
            message_id=f"msg_{index}",
            created_at=f"2026-09-28T10:{index:02d}:00.000Z",
        )
        for index in range(count)
    ]
    await store.ensure(conversation_id, "u_1")
    await store.append(conversation_id, "u_1", messages)


# ---------------------------------------------------------------------------
# 触发条件
# ---------------------------------------------------------------------------
def test_should_summarize_on_message_count(settings: Settings) -> None:
    """消息数达到阈值即触发（``summary_min_new_messages``）。"""
    assert should_summarize(settings, history_tokens=10, new_message_count=20) is True
    assert should_summarize(settings, history_tokens=10, new_message_count=19) is False


def test_should_summarize_on_token_ratio(settings: Settings) -> None:
    """token 超过「历史配额 × ratio」即触发（``summary_trigger_ratio``）。"""
    threshold = int(settings.summary_trigger_ratio * settings.history_token_budget)
    assert should_summarize(settings, history_tokens=threshold + 1, new_message_count=2) is True
    assert should_summarize(settings, history_tokens=threshold, new_message_count=2) is False


def test_should_summarize_respects_global_switch() -> None:
    """``summary_enabled=false`` 时不触发（即使条数早就够了）。"""
    from tests.conftest import build_settings

    disabled = build_settings(summary_enabled=False)
    assert should_summarize(disabled, history_tokens=10_000, new_message_count=500) is False


def test_should_summarize_rejects_empty_history() -> None:
    """空历史不该触发：那只会得到一份「全部为 - 无」的摘要。"""
    from tests.conftest import build_settings

    settings = build_settings()
    assert should_summarize(settings, history_tokens=0, new_message_count=0) is False


# ---------------------------------------------------------------------------
# 结构规整
# ---------------------------------------------------------------------------
def test_ensure_structure_keeps_all_four_sections() -> None:
    """四段（用户目标/已确认事实/未决问题/用户偏好）齐全、正文不丢，且顺序固定。"""
    text = ensure_structure(_RICH_REPLY)
    for name in SUMMARY_SECTIONS:
        assert f"## {name}" in text
    assert "订单号 A123" in text
    # 段落顺序固定（下游靠标题切分）
    positions = [text.index(f"## {name}") for name in SUMMARY_SECTIONS]
    assert positions == sorted(positions)


def test_ensure_structure_fills_missing_sections_with_dash() -> None:
    """模型只写一段时，其余三段必须补上``- 无``而不是消失。"""
    text = ensure_structure("## 用户目标\n- 想了解退款政策")
    assert text.count("## ") == 4
    assert text.count("- 无") == 3


def test_ensure_structure_accepts_bare_and_h1_headings() -> None:
    """模型用 ``# 标题`` 或裸标题也要认得（否则整段会被丢掉）。"""
    text = ensure_structure("# 用户目标\n- 目标 A\n已确认事实\n- 事实 B")
    assert "- 目标 A" in text
    assert "- 事实 B" in text


def test_ensure_structure_strips_markdown_fence() -> None:
    """```markdown 包裹是常见输出，不剥掉会让四段标题全部落在代码块里。"""
    fenced = f"```markdown\n{_RICH_REPLY}```"
    text = ensure_structure(fenced)
    assert text.startswith("## 用户目标")
    assert "```" not in text


def test_ensure_structure_ignores_content_before_first_heading() -> None:
    """首个标题之前的寒暄/解释必须丢掉，否则会被当成某一段的正文注入 Prompt。"""
    text = ensure_structure("好的，以下是摘要：\n## 用户目标\n- 目标 A")
    assert "好的，以下是摘要" not in text
    assert "- 目标 A" in text


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
async def test_build_writes_structured_summary(settings: Settings) -> None:
    """成功生成后落库的摘要四段齐全，且 ``covered_until`` 是被覆盖消息的真实时间戳。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY])
    builder = SummaryBuilder(settings, llm, store)

    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None and outcome.ok
    assert outcome.covered_messages > 0
    saved = await store.summary("cv_1", "u_1")
    assert saved is not None
    assert saved.content.count("## ") == 4
    # ``covered_until`` 必须是被覆盖消息的时间戳：它决定下一轮哪些历史不再注入
    assert saved.covered_until != ""
    assert saved.token_count > 0


async def test_build_skips_when_conditions_unmet(settings: Settings) -> None:
    """条件不满足返回 ``None``（不是错误）：调用方据此跳过而不是降级。"""
    store = _store(settings)
    await _append(store, "cv_1", 3)
    builder = SummaryBuilder(settings, FakeLLM(replies=[_RICH_REPLY]), store)
    assert await builder.build("cv_1", "u_1") is None


async def test_build_keeps_recent_turns_untouched(settings: Settings) -> None:
    """最近 K 轮不动：它们还没「旧」到需要用摘要替代。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    builder = SummaryBuilder(settings, FakeLLM(replies=[_RICH_REPLY]), store)
    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None
    assert outcome.covered_messages == 30 - settings.summary_keep_recent_turns * 2


async def test_build_summarises_everything_when_keep_is_zero(
    make_settings: Any,
) -> None:
    """``SUMMARY_KEEP_RECENT_TURNS=0`` = 全部交给摘要。

    这条配置走 ``messages[:-0]`` 会得到空列表，于是「摘要永不生成」，而日志上
    只看得到「条件不满足」—— 与真正的「消息太少」无法区分。
    """
    settings = make_settings(summary_keep_recent_turns=0)
    store = _store(settings)
    await _append(store, "cv_1", 6)
    builder = SummaryBuilder(settings, FakeLLM(replies=[_RICH_REPLY]), store)

    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None and outcome.ok
    assert outcome.covered_messages == 6


async def test_build_merges_previous_summary_into_prompt(settings: Settings) -> None:
    """增量合并：只摘新消息会让旧偏好永久消失。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY, _RICH_REPLY])
    builder = SummaryBuilder(settings, llm, store)
    await builder.build("cv_1", "u_1", force=True)
    builder.reset_debounce("cv_1")
    await builder.build("cv_1", "u_1", force=True)

    second_prompt = llm.calls[1][-1].content
    assert "上一版摘要" in second_prompt


async def test_build_failure_returns_outcome_with_error(settings: Settings) -> None:
    """生成失败 → ``SummaryOutcome.error`` 非空，**不抛异常**（``AC-MEM-04``）。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY], complete_error=RuntimeError("上游挂了"))
    builder = SummaryBuilder(settings, llm, store)

    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None
    assert outcome.ok is False
    assert "上游挂了" in outcome.error
    # 失败时不应留下半成品摘要
    assert await store.summary("cv_1", "u_1") is None


async def test_build_failure_keeps_previous_summary(settings: Settings) -> None:
    """本次失败时返回上一版，调用方可以继续用它（而不是清空）。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    previous = ConversationSummary(content="## 用户目标\n- 旧目标", covered_until="x")
    await store.save_summary("cv_1", "u_1", previous)
    builder = SummaryBuilder(settings, FakeLLM(complete_error=RuntimeError("上游挂了")), store)
    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None and outcome.error
    assert outcome.summary.content == previous.content


async def test_build_rejects_empty_model_output(settings: Settings) -> None:
    """模型返回空串必须当失败：否则会存下一份空摘要并覆盖掉好的那版。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    builder = SummaryBuilder(settings, FakeLLM(replies=["   "]), store)
    outcome = await builder.build("cv_1", "u_1", force=True)
    assert outcome is not None and outcome.error


# ---------------------------------------------------------------------------
# 防抖
# ---------------------------------------------------------------------------
async def test_debounce_blocks_second_build_within_window() -> None:
    """5 分钟内同一会话最多生成 1 次（``docs/07`` §3.1）。

    刻意用 **非 force** 路径：自动触发（任务处理器）也走这一条。
    """
    from tests.conftest import build_settings

    settings = build_settings()
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY, _RICH_REPLY])
    builder = SummaryBuilder(settings, llm, store)

    assert await builder.build("cv_1", "u_1") is not None
    calls_after_first = len(llm.calls)
    second = await builder.build("cv_1", "u_1")
    assert second is None
    assert len(llm.calls) == calls_after_first


async def test_force_bypasses_debounce() -> None:
    """``force=True`` 是「手动重建」专用语义：必须绕过防抖，否则点了没反应。"""
    from tests.conftest import build_settings

    settings = build_settings()
    store = _store(settings)
    await _append(store, "cv_1", 30)
    builder = SummaryBuilder(settings, FakeLLM(replies=[_RICH_REPLY, _RICH_REPLY]), store)
    assert await builder.build("cv_1", "u_1", force=True) is not None
    assert await builder.build("cv_1", "u_1", force=True) is not None


async def test_force_resets_debounce(settings: Settings) -> None:
    """重置后，自动触发路径也能重新生成。"""
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY, _RICH_REPLY])
    builder = SummaryBuilder(settings, llm, store)

    await builder.build("cv_1", "u_1")
    builder.reset_debounce("cv_1")
    assert await builder.build("cv_1", "u_1") is not None


async def test_debounce_uses_injectable_clock() -> None:
    """防抖用注入时钟判定，而不是靠 ``sleep``（用例要确定性且快）。"""
    from tests.conftest import build_settings

    settings = build_settings(summary_debounce_seconds=300)
    store = _store(settings)
    await _append(store, "cv_1", 30)
    llm = FakeLLM(replies=[_RICH_REPLY, _RICH_REPLY])
    now = {"value": 0.0}
    builder = SummaryBuilder(settings, llm, store, debouncer=_fake_debouncer(now))

    assert await builder.build("cv_1", "u_1") is not None
    assert await builder.build("cv_1", "u_1") is None
    # 把时钟推过窗口：应当重新允许
    now["value"] = 400.0
    assert await builder.build("cv_1", "u_1") is not None


def _fake_debouncer(clock: dict[str, float]) -> Any:
    from app.memory.summary import _Debouncer

    return _Debouncer(window_seconds=300.0, clock=lambda: clock["value"])


async def test_build_returns_none_when_disabled() -> None:
    """``summary_enabled=false`` 时连 ``force=True`` 也不生成（总开关优先于手动重建）。"""
    from tests.conftest import build_settings

    settings = build_settings(summary_enabled=False)
    store = _store(settings)
    await _append(store, "cv_1", 30)
    builder = SummaryBuilder(settings, FakeLLM(replies=[_RICH_REPLY]), store)
    assert await builder.build("cv_1", "u_1", force=True) is None


@pytest.mark.parametrize("count", [0, 1])
def test_ensure_structure_on_empty_text(count: int) -> None:
    """空串或全空白输入也要产出四段骨架（每段``- 无``），而不是空字符串。"""
    text = ensure_structure("" if count == 0 else "   ")
    assert text.count("## ") == 4
    assert text.count("- 无") == 4
