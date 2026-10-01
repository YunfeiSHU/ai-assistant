"""单测：硅基流动重排（``RERANKER_PROVIDER=siliconflow``）。

三条必须钉住的行为：

1. **``results[].index`` 是片内下标** ⇒ 分片时必须加回偏移量。不加偏移的后果是
   "第 2 片之后的候选全部指向前 100 条"，而现象只是"排序看起来有点怪"。
2. **失败必须退化**（``applied=False`` + ``reason``）：重排是可选增强，一次网络抖动
   不能变成对话 500 —— 与本地 BGE 档同一策略（``docs/06`` §5.2）。
3. **``documents`` 发字符串数组**：实测传 ``[{"text": ...}]`` 会 400
   （``Input should be a valid string``），那是 VL 端点的形状。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.rag.reranker import IdentityReranker, SiliconFlowReranker, build_reranker
from app.rag.reranker.bge import BgeReranker

MODEL = "Qwen/Qwen3-Reranker-0.6B"


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    base: dict[str, object] = {
        "reranker_enabled": True,
        "reranker_provider": "siliconflow",
        "reranker_model": MODEL,
        "reranker_top_n": 3,
        "siliconflow_api_key": "sk-test",
        "siliconflow_rerank_max_documents": 2,
        "siliconflow_rerank_max_retries": 0,
    }
    base.update(overrides)
    return make_settings(**base)


def _scores(*pairs: tuple[int, float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "rerank-1",
            "results": [
                {"index": index, "document": None, "relevance_score": score}
                for index, score in pairs
            ],
            "meta": {"tokens": {"input_tokens": 10}},
        },
    )


def _install(
    reranker: SiliconFlowReranker,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[dict]:
    seen: list[dict] = []
    lock = threading.Lock()

    def wrapped(request: httpx.Request) -> httpx.Response:
        with lock:
            payload = json.loads(request.content)
            seen.append(payload)
            assert request.headers["authorization"] == "Bearer sk-test"
        return handler(request)

    reranker.close()
    reranker._client = httpx.Client(transport=httpx.MockTransport(wrapped), timeout=5.0)
    return seen


# ---------------------------------------------------------------------------
# 1. 基本映射
# ---------------------------------------------------------------------------
def test_scores_are_mapped_back_to_original_indices(
    make_settings: Callable[..., Settings],
) -> None:
    """上游回的下标是**片内**下标，必须映回原候选列表的位置（错位等于引用张冠李戴）。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings, siliconflow_rerank_max_documents=10))
    _install(reranker, lambda _r: _scores((2, 0.9), (0, 0.4), (1, 0.1)))

    result = asyncio.run(reranker.rerank("q", ["a", "b", "c"], top_n=3))

    assert result.applied is True
    assert result.ranked == [(2, 0.9), (0, 0.4), (1, 0.1)]
    reranker.close()


def test_top_n_truncates_and_result_is_sorted(make_settings: Callable[..., Settings]) -> None:
    """即使上游没按分数排序，返回也必须降序且不超过 ``top_n``。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings, siliconflow_rerank_max_documents=10))
    _install(reranker, lambda _r: _scores((0, 0.1), (1, 0.7), (2, 0.3)))

    result = asyncio.run(reranker.rerank("q", ["a", "b", "c"], top_n=2))

    assert result.ranked == [(1, 0.7), (2, 0.3)]
    reranker.close()


def test_documents_are_sent_as_plain_strings(make_settings: Callable[..., Settings]) -> None:
    """``[{text}]`` 会被上游 400 拒；必须发字符串数组。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings, siliconflow_rerank_max_documents=10))
    seen = _install(reranker, lambda _r: _scores((0, 0.5)))

    asyncio.run(reranker.rerank("退款几天？", ["doc-a", "doc-b"], top_n=1))

    payload = seen[0]
    assert payload["documents"] == ["doc-a", "doc-b"]
    assert payload["query"] == "退款几天？"
    assert payload["model"] == MODEL
    assert payload["return_documents"] is False  # 正文我们本来就有，不必回传
    assert payload["top_n"] == 1
    assert "instruction" not in payload
    reranker.close()


def test_instruction_is_sent_when_configured(make_settings: Callable[..., Settings]) -> None:
    """配置了 ``instruction`` 才发该字段 —— 它是可选参数，不配就不该出现在请求体里。"""
    import asyncio

    reranker = SiliconFlowReranker(
        _settings(
            make_settings,
            siliconflow_rerank_max_documents=10,
            siliconflow_rerank_instruction="Please rerank the documents based on the query.",
        )
    )
    seen = _install(reranker, lambda _r: _scores((0, 0.5)))

    asyncio.run(reranker.rerank("q", ["a"], top_n=1))

    assert seen[0]["instruction"].startswith("Please rerank")
    reranker.close()


# ---------------------------------------------------------------------------
# 2. 分片：index 必须加回偏移
# ---------------------------------------------------------------------------
def test_sharding_adds_offset_to_indices(make_settings: Callable[..., Settings]) -> None:
    """5 条候选、每片 2 条 ⇒ 3 次请求；第 2 片的下标要变成 2/3，而不是 0/1。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings))

    def handler(request: httpx.Request) -> httpx.Response:
        # 按**片内大小**返回（最后一片只有 1 条）：上游永远只回它收到的那些下标。
        size = len(json.loads(request.content)["documents"])
        pairs = [(0, 0.9)] + ([(1, 0.1)] if size > 1 else [])
        return _scores(*pairs)

    seen = _install(reranker, handler)

    result = asyncio.run(reranker.rerank("q", ["a", "b", "c", "d", "e"], top_n=5))

    assert [len(payload["documents"]) for payload in seen] == [2, 2, 1]
    # 每片的第 0 条最相关 ⇒ 全局应是 0（片1）、2（片2）、4（片3），其余按 0.1 排在后面
    assert [index for index, _ in result.ranked] == [0, 2, 4, 1, 3]
    reranker.close()


# ---------------------------------------------------------------------------
# 3. 退化语义
# ---------------------------------------------------------------------------
def test_failure_degrades_instead_of_raising(make_settings: Callable[..., Settings]) -> None:
    """网络失败 ⇒ ``applied=False`` + 原顺序，由上层记 ``rerank_skipped``。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _install(reranker, handler)
    result = asyncio.run(reranker.rerank("q", ["a", "b", "c"], top_n=2))

    assert result.applied is False
    assert result.reason.startswith("rerank_error")
    assert result.ranked == [(0, 0.0), (1, 0.0)]
    reranker.close()


def test_missing_api_key_degrades(make_settings: Callable[..., Settings]) -> None:
    """缺 key 也只退化（``applied=False`` 且原因点名 ``SILICONFLOW_API_KEY``），不发请求。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings, siliconflow_api_key=""))
    seen = _install(reranker, lambda _r: _scores((0, 0.9)))

    result = asyncio.run(reranker.rerank("q", ["a", "b"], top_n=2))

    assert result.applied is False
    assert "SILICONFLOW_API_KEY" in result.reason
    assert seen == []
    reranker.close()


def test_empty_documents_is_a_success_no_op(make_settings: Callable[..., Settings]) -> None:
    """空候选不算失败（与本地 BGE 档一致）。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings))
    seen = _install(reranker, lambda _r: _scores((0, 0.9)))

    result = asyncio.run(reranker.rerank("q", [], top_n=3))

    assert result.applied is True
    assert result.ranked == []
    assert seen == []
    reranker.close()


def test_out_of_range_index_degrades(make_settings: Callable[..., Settings]) -> None:
    """越界 index 说明上游行为变了：宁可退化，也不能返回错位的候选。"""
    import asyncio

    reranker = SiliconFlowReranker(_settings(make_settings, siliconflow_rerank_max_documents=10))
    _install(reranker, lambda _r: _scores((99, 0.9)))

    result = asyncio.run(reranker.rerank("q", ["a", "b"], top_n=2))

    assert result.applied is False
    reranker.close()


# ---------------------------------------------------------------------------
# 4. 工厂：显式 provider（不再靠模型名猜）
# ---------------------------------------------------------------------------
def test_factory_prefers_explicit_provider(make_settings: Callable[..., Settings]) -> None:
    """``Qwen/Qwen3-Reranker-0.6B`` 名字里没有 bge —— 老实现会在这里静默不重排。"""
    assert isinstance(
        build_reranker(_settings(make_settings, reranker_provider="siliconflow")),
        SiliconFlowReranker,
    )
    assert isinstance(
        build_reranker(
            _settings(
                make_settings, reranker_provider="bge", reranker_model="BAAI/bge-reranker-v2-m3"
            )
        ),
        BgeReranker,
    )


def test_factory_returns_identity_when_disabled(make_settings: Callable[..., Settings]) -> None:
    """关掉重排时**不**构造任何实现（也就不会因为缺 key 而拦住启动）。"""
    reranker = build_reranker(_settings(make_settings, reranker_enabled=False))

    assert isinstance(reranker, IdentityReranker)
    assert reranker.reason == "reranker_disabled"


def test_disabled_reranker_does_not_require_api_key(
    make_settings: Callable[..., Settings],
) -> None:
    """关掉重排后，配置校验不该再要求 SILICONFLOW_API_KEY。"""
    settings = _settings(
        make_settings, reranker_enabled=False, siliconflow_api_key="", embedding_provider="hash"
    )
    settings.validate_for_startup()


def test_enabled_siliconflow_reranker_requires_api_key(
    make_settings: Callable[..., Settings],
) -> None:
    """重排启用但缺 key ⇒ 启动期校验直接失败（与「关掉就不校验」互为对照）。"""
    settings = _settings(make_settings, siliconflow_api_key="")
    with pytest.raises(Exception, match="SILICONFLOW_API_KEY"):
        settings.validate_for_startup()
