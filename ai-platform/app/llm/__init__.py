"""LLM 抽象层。

分两层，目的是让「换供应商」与「测试替身」都不必碰业务代码：

* :mod:`app.llm.base` —— 纯协议与数据结构（不 import 任何 SDK）；
* :mod:`app.llm.openai_compat` —— OpenAI 兼容协议适配器（DeepSeek / Moonshot /
  本地 vLLM 全都走它，只差 ``base_url``）。
"""

from __future__ import annotations

from app.llm.base import (
    LLMClient,
    LLMDelta,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    map_llm_exception,
)

__all__ = [
    "LLMClient",
    "LLMDelta",
    "LLMMessage",
    "LLMResponse",
    "LLMToolCall",
    "LLMUsage",
    "map_llm_exception",
]
