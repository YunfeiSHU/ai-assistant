"""长期记忆抽取（``REQ-MEM-004``，``docs/07`` §5.1）。

这一层做两件事，且**刻意分开**：

1. :func:`parse_candidates` —— 把模型输出变成结构化候选。模型经常带 ```json 包裹、
   前后加解释、把 ``confidence`` 写成 ``"0.9"``；解析必须容错，否则「抽取失败」会
   伪装成「这轮没有值得记的东西」。
2. :func:`accept_candidates` —— 按规则过滤。**这是安全边界**，不是格式整理：
   敏感凭证一旦被记住，就会在此后每一次对话里被注入 Prompt。

过滤规则宁可保守（漏掉一些偏好）也不能激进（记住一串 API Key）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm.base import LLMClient, LLMMessage, map_llm_exception
from app.memory.context_store import StoredMessage
from app.memory.long_term import normalise_content

logger = logging.getLogger("app.memory.extractor")

MEMORY_KINDS = ("preference", "fact")

EXTRACT_SYSTEM_PROMPT = (
    "你从对话中抽取值得**长期**记住的用户信息，只输出 JSON 数组，不要任何解释。\n"
    "数组元素形如：\n"
    '  {"content": "用户偏好简洁回答，不喜欢列表", "kind": "preference", "confidence": 0.9}\n'
    "规则：\n"
    "1. kind 只能是 preference（稳定偏好/习惯）或 fact（关于用户的稳定事实）。\n"
    "2. confidence 取 0..1，只有明确说出的才给高分。\n"
    "3. 不要抽取：一次性任务、临时上下文、疑问句、你的推测、任何密码/密钥/证件号/银行卡号。\n"
    "4. content 用第三人称陈述句，5..500 字，一条只讲一件事。\n"
    "5. 没有可抽取的内容时输出 []。"
)

#: 敏感凭证：命中即整条丢弃（不是打码 —— 打码后的残片仍然泄露信息量）
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(密码|口令|passwd|password|passcode)", re.IGNORECASE),
    re.compile(r"(密钥|secret|api[\s_-]?key|access[\s_-]?key|token)", re.IGNORECASE),
    re.compile(r"(身份证|护照号|银行卡|信用卡|卡号|cvv|验证码)"),
    re.compile(r"\b\d{15,19}\b"),  # 长数字串：卡号/账号
    re.compile(r"\b(sk|ak|ghp|xox[baprs])[-_][A-Za-z0-9_-]{16,}"),  # 常见密钥前缀
)

#: 疑问句：问句不是「关于用户的事实」，记住它只会污染后续上下文。
#:
#: 多字疑问词（是不是/要不要…）**不要求**后面跟问号：抽取器的输入是模型改写过的
#: 第三人称句子，问号经常被丢掉，要求标点等于让这条规则半失效。
#: 单字语气助词（吗/呢）只在句尾才算，避免误伤「干吗」这类正常词。
_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[?？]\s*$"),
    re.compile(r"(是不是|能不能|可不可以|有没有|要不要|会不会|能不能|何必)"),
    re.compile(r"(吗|呢)\s*[?？]?\s*$"),
)

#: 模型自身推测：把推测当事实记住，会在后续对话里被当成既定前提
_SPECULATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(可能|大概|也许|似乎|看起来|应该是|估计)"),
    re.compile(r"(用户可能|用户大概|用户似乎)"),
)

#: 一次性任务信息：与「长期」的定义直接冲突
_ONEOFF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(本次|这次|这轮|刚才|刚刚)"),
    re.compile(r"(今天|明天|昨天|本周|上周|下周)"),
    re.compile(r"(帮我|请帮|现在)(查|看|算|找|写)"),
    re.compile(r"^\s*(你好|hi|hello|谢谢|好的|收到)"),
)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """一条待写入的记忆候选。"""

    content: str
    kind: str = "fact"
    confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """被过滤掉的候选（连同原因）。

    保留原因而不是静默丢弃：``docs/07`` §7 的验收要求「过滤规则生效」可被观察，
    否则规则写错了也看不出来（表现为「明明抽取了却一条没写」）。
    """

    content: str
    reason: str


def parse_candidates(raw: str) -> list[MemoryCandidate]:
    """把模型输出解析成候选列表（容错：代码块包裹、前后解释、字符串型置信度）。"""
    payload = _extract_json_array(raw)
    if payload is None:
        return []
    candidates: list[MemoryCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in MEMORY_KINDS:
            kind = "fact"
        candidates.append(
            MemoryCandidate(
                content=content, kind=kind, confidence=_to_float(item.get("confidence"))
            )
        )
    return candidates


def _extract_json_array(raw: str) -> list[Any] | None:
    """从任意文本里摘出第一个 JSON 数组。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
        text = text.removeprefix("json").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        # 模型偶尔会输出单引号或尾逗号；逐个片段再试一次，全失败就当作「没有候选」
        logger.info("memory.extract_unparsable", extra={"preview": text[:120]})
        return None
    return parsed if isinstance(parsed, list) else None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def rejection_reason(content: str, confidence: float, settings: Settings) -> str | None:
    """给出拒绝原因；``None`` 表示通过。

    顺序即优先级：**先查敏感信息**。哪怕置信度只有 0.1，一串 API Key 也不能因为
    「置信度不够」以外的原因被留下。
    """
    text = content.strip()
    if any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS):
        return "sensitive"
    if len(text) < settings.memory_content_min_chars:
        return "too_short"
    if len(text) > settings.memory_content_max_chars:
        return "too_long"
    if any(pattern.search(text) for pattern in _QUESTION_PATTERNS):
        return "question"
    if any(pattern.search(text) for pattern in _SPECULATION_PATTERNS):
        return "speculation"
    if any(pattern.search(text) for pattern in _ONEOFF_PATTERNS):
        return "one_off"
    if confidence < settings.memory_min_confidence:
        return "low_confidence"
    return None


def accept_candidates(
    candidates: Sequence[MemoryCandidate], settings: Settings
) -> tuple[list[MemoryCandidate], list[RejectedCandidate]]:
    """按规则分流候选（``docs/07`` §5.1 的保留条件与过滤清单）。"""
    accepted: list[MemoryCandidate] = []
    rejected: list[RejectedCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        reason = rejection_reason(candidate.content, candidate.confidence, settings)
        if reason is None:
            # 同一轮里的精确重复也只在内存里去一次；跨轮次的判重由仓储的唯一索引负责。
            # 规范化必须调**同一个**函数（见 ``normalise_content``），否则两处规则
            # 一旦有一丝差异就会出现「批内不去重、但存储把它当重复而丢掉」的静默丢数据。
            fingerprint = normalise_content(candidate.content)
            if fingerprint in seen:
                rejected.append(RejectedCandidate(candidate.content, "duplicate_in_batch"))
                continue
            seen.add(fingerprint)
            accepted.append(candidate)
        else:
            rejected.append(RejectedCandidate(candidate.content, reason))
    return accepted, rejected


class MemoryExtractor:
    """从对话消息里抽取长期记忆候选。"""

    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self._settings = settings
        self._llm = llm

    async def extract(
        self, messages: Sequence[StoredMessage]
    ) -> tuple[list[MemoryCandidate], list[RejectedCandidate]]:
        """抽取并过滤；模型调用失败时返回空结果（由调用方记 ``degraded``）。"""
        text = self._render(messages)
        if not text.strip():
            return [], []
        response = await self._call_llm(text)
        candidates = parse_candidates(response)
        return accept_candidates(candidates, self._settings)

    async def _call_llm(self, text: str) -> str:
        try:
            response = await self._llm.complete(
                [
                    LLMMessage(role="system", content=EXTRACT_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=text),
                ],
                model=self._llm.resolve_model(None),
                temperature=0.0,
                max_tokens=512,
            )
        except Exception as exc:
            raise map_llm_exception(exc) from exc
        return response.content

    def _render(self, messages: Sequence[StoredMessage]) -> str:
        """把消息列表渲染成抽取输入（只取 user 侧，避免把助手的话当用户偏好）。"""
        lines = [
            f"用户: {message.content.strip()}"
            for message in messages
            if message.role == "user" and message.content.strip()
        ]
        return "\n".join(lines[-20:])


__all__ = [
    "EXTRACT_SYSTEM_PROMPT",
    "MEMORY_KINDS",
    "MemoryCandidate",
    "MemoryExtractor",
    "RejectedCandidate",
    "accept_candidates",
    "parse_candidates",
    "rejection_reason",
]
