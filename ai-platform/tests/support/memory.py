"""记忆用例的公共替身（``docs/11`` §3：可脚本化的 Fake LLM / 确定性向量化）。

单测与契约测试都需要**确定性的相似度**：真实 embedding（哪怕 hash 版）算出来的
余弦值不受控，而记忆层的核心逻辑恰恰挂在两个阈值上

* ``MEMORY_DEDUPE_THRESHOLD``（0.92）：以上合并、以下并存；
* ``MEMORY_SCORE_THRESHOLD``（0.45）：以上才注入上下文。

用真向量去测这两个阈值，用例就会随模型版本偶发失败，而失败信息只会说
「期望 2 条实际 1 条」，完全指不到阈值上。所以这里用 :class:`ScriptedEmbedding`
按文本给定向量，把「相似度」变成用例里一个可读的量。

:class:`MemoryScriptLLM` 解决另一类耦合：抽取与摘要共用同一个 LLM 客户端，
若共用一个 ``replies`` 队列，「摘要内容对不对」就会依赖「抽取被调用了几次」。
按**系统提示词**分派（两者问的是不同问题）可以让两条链路各自独立。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from tests.support.fake_llm import FakeLLM

from app.rag.embedding.base import l2_normalize

#: 脚本向量的默认维度（够放下「互相正交的若干条记忆」即可）
DEFAULT_DIM = 8

#: 基准方向：查询向量默认指向它
BASE = tuple([1.0] + [0.0] * (DEFAULT_DIM - 1))

#: 摘要的固定四段（与 ``app.memory.summary.SUMMARY_SECTIONS`` 同源）
SUMMARY_SECTIONS = ("用户目标", "已确认事实", "未决问题", "用户偏好")

#: 一份「格式正确」的摘要样本（模型理想输出）
SUMMARY_TEXT = "\n\n".join(
    [
        "## 用户目标\n- 在会话中讨论长期记忆的实现",
        "## 已确认事实\n- 项目是 ai-platform",
        "## 未决问题\n- 摘要的触发阈值是否需要调小",
        "## 用户偏好\n- 用户偏好简洁回答，不要使用列表",
    ]
)


def blend(axis: int, cosine: float, *, dim: int = DEFAULT_DIM) -> list[float]:
    """构造与 :data:`BASE` 夹角余弦恰为 ``cosine`` 的单位向量。

    把剩下的能量放到 ``axis`` 轴上，于是「同一 cosine、不同 axis」的两个向量
    彼此**正交**（cos=0）—— 这让「多条相似但不等价的记忆」可以精确构造。
    这一条不能省：全都指向同一个方向时，写 5 条记忆会被语义去重合并成 1 条
    （实现是对的，但用例测不到「并存」这个分支）。
    """
    sin = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    values = [cosine] + [0.0] * (dim - 1)
    values[axis % (dim - 1) + 1] = sin
    return l2_normalize(values)


def direction(cosine: float, *, dim: int = DEFAULT_DIM) -> list[float]:
    """与 :data:`BASE` 夹角余弦为 ``cosine`` 的向量（``blend(0, cosine)``）。"""
    return blend(0, cosine, dim=dim)


class ScriptedEmbedding:
    """按文本给定向量的 Embedding 替身。"""

    def __init__(
        self, vectors: dict[str, list[float]] | None = None, *, dim: int = DEFAULT_DIM
    ) -> None:
        self._vectors = dict(vectors or {})
        self._dim = dim
        self._assigned: dict[str, int] = {}

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return "scripted"

    def _vector(self, text: str) -> list[float]:
        registered = self._vectors.get(text)
        if registered is not None:
            return list(registered)
        axis = self._assigned.setdefault(text, len(self._assigned) + 1)
        return blend(axis, 0.0, dim=self._dim)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def candidate(content: str, *, kind: str = "fact", confidence: float = 0.9) -> str:
    """构造抽取器期望的候选 JSON 数组（单条）。"""
    import json

    return json.dumps(
        [{"content": content, "kind": kind, "confidence": confidence}], ensure_ascii=False
    )


class MemoryScriptLLM(FakeLLM):
    """按系统提示词分派的记忆 LLM：抽取返回候选 JSON，摘要返回四段结构。

    两者共用同一个客户端（生产里就是这样），但必须能分别脚本化 —— 否则
    「摘要生成失败」这类用例会被「抽取先/后调用」影响。
    """

    def __init__(
        self,
        *,
        candidates: str = "[]",
        summary: str = SUMMARY_TEXT,
        summary_error: BaseException | None = None,
    ) -> None:
        super().__init__(replies=[""])
        self.candidates = candidates
        self.summary = summary
        self.summary_error = summary_error
        #: 每次调用被归到哪条链路（按调用顺序）
        self.kinds: list[str] = []

    @staticmethod
    def _is_summary(messages: Sequence[object]) -> bool:
        system = "\n".join(
            str(getattr(message, "content", ""))
            for message in messages
            if getattr(message, "role", "") == "system"
        )
        return "摘要" in system

    def _next_reply(self) -> str:
        if self._is_summary(self.calls[-1]):
            self.kinds.append("summary")
            if self.summary_error is not None:
                raise self.summary_error
            return self.summary
        self.kinds.append("extract")
        return self.candidates

    @property
    def summary_calls(self) -> int:
        return self.kinds.count("summary")

    @property
    def extract_calls(self) -> int:
        return self.kinds.count("extract")


__all__ = [
    "BASE",
    "DEFAULT_DIM",
    "SUMMARY_SECTIONS",
    "SUMMARY_TEXT",
    "MemoryScriptLLM",
    "ScriptedEmbedding",
    "blend",
    "candidate",
    "direction",
    "l2_normalize",
]
