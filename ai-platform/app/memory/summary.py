"""对话摘要的生成（``REQ-MEM-003``，``docs/07`` §3）。

三个要点，都是「写错也不会报错、只是效果变差」的那类：

1. 输出结构固定四段。自由格式的摘要在下游要拼进 system 片段，没有结构时「已确认事实」
   和「用户偏好」会互相污染，而模型对后者的敏感度远高于前者。
2. 增量合并上一版摘要。只摘新消息会丢掉旧信息 —— 用户在第 3 轮说过偏好，第 30 轮做摘要
   时若不带上旧摘要，那件事就永久消失了。
3. 失败只降级不阻塞（``AC-MEM-04``）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.tokens import count_tokens, truncate_to_tokens
from app.llm.base import LLMClient, LLMMessage, map_llm_exception
from app.memory.context_store import (
    ConversationStore,
    ConversationSummary,
    StoredMessage,
)

logger = logging.getLogger("app.memory.summary")

#: 摘要的固定四段（``docs/07`` §3.2）
SUMMARY_SECTIONS: tuple[str, ...] = ("用户目标", "已确认事实", "未决问题", "用户偏好")

SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要器。把给定对话压缩成结构化摘要，只保留后续对话真正需要的信息。\n"
    "严格按以下四段输出，每段用 Markdown 二级标题，段落标题必须逐字为：\n"
    f"## {SUMMARY_SECTIONS[0]}\n## {SUMMARY_SECTIONS[1]}\n## {SUMMARY_SECTIONS[2]}\n"
    f"## {SUMMARY_SECTIONS[3]}\n"
    "每段用简短要点（- 开头）。没有内容的段落写「- 无」。\n"
    "不要编造对话中没有的信息，不要输出额外解释或前后缀。"
)


def should_summarize(
    settings: Settings,
    *,
    history_tokens: int,
    new_message_count: int,
) -> bool:
    """是否满足摘要触发条件（``docs/07`` §3.1，任一满足即可）。"""
    if not settings.summary_enabled:
        return False
    if new_message_count <= 0:
        return False
    if history_tokens > settings.summary_trigger_ratio * settings.history_token_budget:
        return True
    return new_message_count >= settings.summary_min_new_messages


def ensure_structure(text: str) -> str:
    """把模型输出规整成四段结构（缺段补「- 无」，多余内容丢弃）。

    不能直接信任模型遵守格式：一旦某段缺失，下游拼装出的 system 片段就少一块信息，
    而这种缺失在日志里看不出来。
    """
    body = text.strip()
    # 模型常见的多余包裹：```markdown ... ```
    if body.startswith("```"):
        lines = body.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            body = "\n".join(lines[1:-1])
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        matched = next(
            (name for name in SUMMARY_SECTIONS if stripped in (f"## {name}", f"# {name}", name)),
            None,
        )
        if matched is not None:
            current = matched
            sections.setdefault(current, [])
            continue
        if current is not None and stripped:
            sections[current].append(stripped)
    blocks: list[str] = []
    for name in SUMMARY_SECTIONS:
        items = sections.get(name) or ["- 无"]
        blocks.append(f"## {name}\n" + "\n".join(items))
    return "\n\n".join(blocks)


@dataclass
class _Debouncer:
    """同一会话的生成防抖（``docs/07`` §3.1：5 分钟内最多 1 次）。

    用注入的时钟而不是直接读 ``time.monotonic``：否则「防抖有没有生效」只能靠 ``sleep``
    来测，用例会变慢且不稳定。
    """

    window_seconds: float
    clock: Callable[[], float] = time.monotonic
    _last: dict[str, float] = field(default_factory=dict)

    def allows(self, key: str) -> bool:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.window_seconds:
            return False
        self._last[key] = now
        return True

    def reset(self, key: str) -> None:
        self._last.pop(key, None)


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    """一次摘要生成的结果。"""

    summary: ConversationSummary
    covered_messages: int
    #: 生成失败时非空（调用方据此往 ``degraded_reasons`` 加 ``summary_failed``）
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class SummaryBuilder:
    """按「保留最近 K 轮 + 合并上一版摘要」生成摘要并落库。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        store: ConversationStore,
        *,
        debouncer: _Debouncer | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._store = store
        self._debouncer = debouncer or _Debouncer(settings.summary_debounce_seconds)

    # ------------------------------------------------------------------
    def should_build(
        self, messages: Sequence[StoredMessage], previous: ConversationSummary | None
    ) -> bool:
        """按已有消息判断是否需要生成（``POST /summary/rebuild`` 走另一条路）。"""
        covered_until = previous.covered_until if previous else ""
        fresh = (
            [m for m in messages if m.created_at > covered_until]
            if covered_until
            else list(messages)
        )
        return should_summarize(
            self._settings,
            history_tokens=sum(count_tokens(m.content) for m in fresh),
            new_message_count=len(fresh),
        )

    async def build(
        self, conversation_id: str, user_id: str, *, force: bool = False
    ) -> SummaryOutcome | None:
        """生成并保存摘要。

        Returns:
            ``None`` 表示「条件不满足或命中防抖，本轮跳过」（不是错误）；否则返回
            :class:`SummaryOutcome`，失败时 ``error`` 非空且 ``summary`` 是上一版（或空）。
        """
        settings = self._settings
        if not settings.summary_enabled:
            return None
        messages = await self._store.all_messages(conversation_id, user_id)
        previous = await self._store.summary(conversation_id, user_id)
        if not force and not self.should_build(messages, previous):
            return None
        if not force and not self._debouncer.allows(conversation_id):
            logger.info("summary.debounced", extra={"conversation_id": conversation_id})
            return None

        # 保留最近 K 轮不动：它们还没「旧」到需要用摘要替代。
        # ``keep == 0`` 表示「全部交给摘要」—— 此时不能走 ``messages[:-0]``
        # 那条路（切片结果为空，摘要会永不生成，而且日志上看不出原因）。
        keep = max(0, settings.summary_keep_recent_turns) * 2
        pending = list(messages)
        if keep and len(messages) > keep:
            pending = list(messages[:-keep])
        if not pending:
            return None

        covered_until = pending[-1].created_at
        try:
            text = await self._generate(pending, previous)
        except Exception as exc:
            # 摘要失败绝不能阻塞对话（``AC-MEM-04``）
            reason = str(exc)
            logger.warning("summary.failed", extra={"error": reason})
            return SummaryOutcome(
                summary=previous or ConversationSummary(content="", covered_until=""),
                covered_messages=0,
                error=reason,
            )

        summary = ConversationSummary(
            content=text,
            covered_until=covered_until,
            source_message_count=len(pending),
            token_count=count_tokens(text),
        )
        await self._store.save_summary(conversation_id, user_id, summary)
        logger.info(
            "summary.built",
            extra={
                "conversation_id": conversation_id,
                "covered_messages": len(pending),
                "token_count": summary.token_count,
            },
        )
        return SummaryOutcome(summary=summary, covered_messages=len(pending))

    # ------------------------------------------------------------------
    async def _generate(
        self, messages: Sequence[StoredMessage], previous: ConversationSummary | None
    ) -> str:
        """调用模型生成摘要（``temperature=0.0``，长度受 ``summary_max_tokens`` 约束）。"""
        settings = self._settings
        lines = [f"{m.role}: {m.content}" for m in messages if m.content.strip()]
        payload: list[str] = []
        if previous and previous.content.strip():
            payload.append(f"【上一版摘要（需要合并保留）】\n{previous.content.strip()}")
        payload.append("【新增对话】\n" + "\n".join(lines))
        try:
            response = await self._llm.complete(
                [
                    LLMMessage(role="system", content=SUMMARY_SYSTEM_PROMPT),
                    LLMMessage(role="user", content="\n\n".join(payload)),
                ],
                model=self._llm.resolve_model(None),
                temperature=0.0,
                max_tokens=settings.summary_token_budget,
            )
        except Exception as exc:
            raise map_llm_exception(exc) from exc
        if not response.content.strip():
            # 必须在 ``ensure_structure`` 之前判空：它会把任何输入都补齐成四段「- 无」，
            # 于是「模型什么都没返回」会变成一份看起来格式完美的空摘要并覆盖掉上一版好摘要。
            raise RuntimeError("摘要结果为空")
        text = ensure_structure(response.content)
        if count_tokens(text) > settings.summary_token_budget:
            # 上游没有遵守 max_tokens 时自己兜底，否则摘要会挤占历史与 RAG 的配额
            text = truncate_to_tokens(text, settings.summary_token_budget)
        return text

    # ------------------------------------------------------------------
    def reset_debounce(self, conversation_id: str) -> None:
        """手动重建时清掉防抖窗口（``POST /summary/rebuild`` 必须立即生效）。"""
        self._debouncer.reset(conversation_id)


__all__ = [
    "SUMMARY_SECTIONS",
    "SUMMARY_SYSTEM_PROMPT",
    "SummaryBuilder",
    "SummaryOutcome",
    "ensure_structure",
    "should_summarize",
]
