"""长期记忆抽取与过滤单测（``REQ-MEM-004``，``docs/07`` §5.1）。

这一层是**安全边界**：被记住的内容会在此后每一次对话里被注入 Prompt。
所以用例的重点是「什么必须被挡住」而不是「模型调了几次」：

* 敏感凭证优先级最高（哪怕置信度 0.99）；
* 疑问句 / 推测 / 一次性任务不得入库；
* 解析必须容错（模型带 ```json 包裹、前后加解释是常态）——
  解析失败被当成「没有候选」会让抽取静默失效。
"""

from __future__ import annotations

import pytest
from tests.conftest import build_settings
from tests.support.fake_llm import FakeLLM

from app.core.config import Settings
from app.memory.context_store import StoredMessage
from app.memory.extractor import (
    MemoryCandidate,
    MemoryExtractor,
    accept_candidates,
    parse_candidates,
    rejection_reason,
)
from app.memory.long_term import normalise_content


def _message(content: str, role: str = "user") -> StoredMessage:
    return StoredMessage(
        role=role, content=content, message_id="msg_1", created_at="2026-09-28T10:00:00.000Z"
    )


# ---------------------------------------------------------------------------
# 解析容错
# ---------------------------------------------------------------------------
def test_parse_plain_json_array() -> None:
    """最规范形态（裸 JSON 数组）直接解析成 ``MemoryCandidate``，不要任何预处理。"""
    candidates = parse_candidates(
        '[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9}]'
    )
    assert candidates == [
        MemoryCandidate(content="用户偏好简洁回答", kind="preference", confidence=0.9)
    ]


def test_parse_strips_json_fence_and_prose() -> None:
    """```json 包裹 + 前后解释是模型的常态输出，必须能解析出来。"""
    raw = (
        "好的，我抽取到以下信息：\n```json\n"
        '[{"content": "用户在成都工作", "kind": "fact", "confidence": "0.8"}]\n'
        "```\n以上。"
    )
    candidates = parse_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0].content == "用户在成都工作"
    # 字符串型置信度要能转成 float，否则会静默变成 0 并被 low_confidence 挡掉
    assert candidates[0].confidence == pytest.approx(0.8)


def test_parse_unknown_kind_falls_back_to_fact() -> None:
    """``kind`` 是文档未定义的取值 ⇒ 归一到 ``fact``，而不是整条丢弃或原样透传。"""
    candidates = parse_candidates('[{"content": "用户养了一只猫", "kind": "preference_v2"}]')
    assert candidates[0].kind == "fact"


def test_parse_skips_non_dict_and_blank_content() -> None:
    """数组里混进非对象元素、或 ``content`` 全空白时只跳过该条，不影响其余候选。"""
    candidates = parse_candidates('[1, {"content": "   "}, {"content": "用户喜欢深色主题"}]')
    assert [candidate.content for candidate in candidates] == ["用户喜欢深色主题"]


def test_parse_unparsable_returns_empty() -> None:
    """非法 JSON 返回空列表（调用方看到的是「没有候选」，但日志里有 extract_unparsable）。"""
    assert parse_candidates("这不是 JSON") == []
    assert parse_candidates("[{'content': 'x'}]") == []


def test_parse_empty_array() -> None:
    """模型明确返回「没有候选」的空数组时结果是空列表，且不算解析失败。"""
    assert parse_candidates("[]") == []


# ---------------------------------------------------------------------------
# 过滤规则
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        "用户的数据库密码是 hunter2",
        "用户的 GitHub token 是 ghp_abcdefghijklmnopqrstuvwxyz",
        "用户身份证号 110101199001011234",
        "用户的银行卡是 6222021234567890123",
        "用户的 API key 记在便签上",
    ],
)
def test_sensitive_content_is_rejected(content: str, settings: Settings) -> None:
    """敏感凭证命中即整条丢弃（不是打码：残片仍然是泄露）。"""
    assert rejection_reason(content, 0.99, settings) == "sensitive"


def test_sensitive_check_runs_before_length_check() -> None:
    """短到不合法且含密钥时，原因必须是 ``sensitive`` 而不是 ``too_short``。

    顺序反了会让人以为「调长一点就能记住」。
    """
    settings = build_settings(memory_content_min_chars=50)
    assert rejection_reason("密码 abc", 0.9, settings) == "sensitive"


@pytest.mark.parametrize(
    "content",
    [
        "用户是不是喜欢简洁回答？",
        "用户要不要开通知",
        "用户喜欢简洁回答吗",
        "用户能不能用中文回答呢",
    ],
)
def test_question_is_rejected(content: str, settings: Settings) -> None:
    """疑问句必须被挡：问句不是「关于用户的事实」。

    注意后两例**没有问号**：模型的输出是改写过的第三人称句子，大多会把问号去掉，
    如果规则要求标点，这条防线就只在半数情况下生效。
    """
    assert rejection_reason(content, 0.9, settings) == "question"


@pytest.mark.parametrize(
    "content",
    ["用户可能偏好中文回答", "用户大概是做后端的", "用户似乎不喜欢长篇回答"],
)
def test_speculation_is_rejected(content: str, settings: Settings) -> None:
    """模型自己的推测被当事实记住，之后会变成既定前提。"""
    assert rejection_reason(content, 0.9, settings) == "speculation"


@pytest.mark.parametrize(
    "content",
    ["用户本次想要一份周报", "用户今天要发布版本", "用户帮我查一下天气"],
)
def test_one_off_information_is_rejected(content: str, settings: Settings) -> None:
    """一次性任务/临时诉求（"本次""今天""帮我查一下"）不是长期偏好，必须被挡。"""
    assert rejection_reason(content, 0.9, settings) == "one_off"


def test_length_bounds(settings: Settings) -> None:
    """内容长度在 ``memory_content_min_chars`` / ``memory_content_max_chars`` 之外各有专属原因。"""
    assert rejection_reason("短", 0.9, settings) == "too_short"
    assert rejection_reason("很长" * 400, 0.9, settings) == "too_long"


def test_low_confidence_is_rejected(settings: Settings) -> None:
    """低于 ``memory_min_confidence``（默认 0.7）判 ``low_confidence``，达到阈值则放行。"""
    assert rejection_reason("用户偏好深色主题", 0.5, settings) == "low_confidence"
    assert rejection_reason("用户偏好深色主题", 0.7, settings) is None


def test_accept_candidates_splits_accepted_and_rejected(settings: Settings) -> None:
    """``accept_candidates`` 按原因分流：通过的原样保留，被拒的带上 ``reason`` 返回。"""
    accepted, rejected = accept_candidates(
        [
            MemoryCandidate(content="用户偏好简洁回答", kind="preference", confidence=0.9),
            MemoryCandidate(content="用户是不是喜欢简洁回答？", confidence=0.9),
        ],
        settings,
    )
    assert [candidate.content for candidate in accepted] == ["用户偏好简洁回答"]
    assert [(item.content, item.reason) for item in rejected] == [
        ("用户是不是喜欢简洁回答？", "question")
    ]


def test_accept_candidates_dedupes_within_batch(settings: Settings) -> None:
    """同一轮里的重复要去重，否则会白跑一次向量化（跨轮次由唯一索引兜底）。"""
    accepted, rejected = accept_candidates(
        [
            MemoryCandidate(content="用户偏好简洁回答", confidence=0.9),
            MemoryCandidate(content="  用户偏好简洁回答\n", confidence=0.95),
        ],
        settings,
    )
    assert len(accepted) == 1
    assert rejected[0].reason == "duplicate_in_batch"


def test_batch_dedupe_and_repo_index_use_same_normalisation() -> None:
    """批内去重与仓储唯一索引必须用**同一个**规范化函数。

    两处规则差一丝，就会出现「批内不去重 → 反而被存储当成重复丢掉」，
    表现为「明明抽到了两条，只存下一条」，日志里什么都看不到。
    """
    assert normalise_content("用户偏好\n简洁  回答") == "用户偏好 简洁 回答"
    assert normalise_content("用户偏好 简洁回答") == "用户偏好 简洁回答"
    # 词边界不同 = 不同内容（不能把空格直接删掉）
    assert normalise_content("用户偏好简洁回答") != normalise_content("用户偏好 简洁回答")


# ---------------------------------------------------------------------------
# 抽取（LLM 交互）
# ---------------------------------------------------------------------------
async def test_extract_renders_only_user_messages(settings: Settings) -> None:
    """只把 user 侧给模型：把助手的话当用户偏好是这类功能最常见的错误。"""
    llm = FakeLLM(replies=['[{"content": "用户偏好简洁回答", "confidence": 0.9}]'])
    extractor = MemoryExtractor(settings, llm)
    await extractor.extract(
        [
            _message("我喜欢简洁回答"),
            _message("好的，我会保持简洁。", role="assistant"),
        ]
    )
    prompt = llm.calls[0][-1].content
    assert "我喜欢简洁回答" in prompt
    assert "我会保持简洁" not in prompt


async def test_extract_returns_accepted_and_rejected(settings: Settings) -> None:
    """端到端一次抽取要同时给出「采纳的候选」与「被拒的候选 + 原因」，便于审计。"""
    llm = FakeLLM(
        replies=[
            '[{"content": "用户偏好简洁回答", "kind": "preference", "confidence": 0.9},'
            ' {"content": "用户的密钥是 sk-abcdefghijklmnopqrstuvwxyz", "confidence": 0.9}]'
        ]
    )
    extractor = MemoryExtractor(settings, llm)
    accepted, rejected = await extractor.extract([_message("我喜欢简洁回答")])
    assert [candidate.content for candidate in accepted] == ["用户偏好简洁回答"]
    assert rejected[0].reason == "sensitive"


async def test_extract_without_user_messages_skips_llm(settings: Settings) -> None:
    """没有 user 消息时不调模型：空输入调一次只是白花钱。"""
    llm = FakeLLM(replies=["[]"])
    extractor = MemoryExtractor(settings, llm)
    assert await extractor.extract([_message("忽略我", role="assistant")]) == ([], [])
    assert llm.calls == []


async def test_extract_unparsable_output_yields_no_candidates(settings: Settings) -> None:
    """模型输出无法解析 ⇒ 空结果（不抛异常），且失败在日志里可观测而不是静默成功。"""
    llm = FakeLLM(replies=["我不确定该抽什么"])
    extractor = MemoryExtractor(settings, llm)
    assert await extractor.extract([_message("我喜欢简洁回答")]) == ([], [])


async def test_extract_propagates_llm_failure(settings: Settings) -> None:
    """上游失败必须抛出（由 ``MemoryService`` 决定降级方式），不能假装「没抽到」。"""
    from app.core.exceptions import AppError

    llm = FakeLLM(replies=["[]"], complete_error=RuntimeError("上游挂了"))
    extractor = MemoryExtractor(settings, llm)
    with pytest.raises(AppError):
        await extractor.extract([_message("我喜欢简洁回答")])
