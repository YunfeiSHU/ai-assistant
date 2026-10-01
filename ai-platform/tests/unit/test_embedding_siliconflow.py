"""单测：硅基流动向量化 provider（``EMBEDDING_PROVIDER=siliconflow``）。

与方舟那档的测试**刻意不同**的地方：这里要钉的是"**真批量**"——
N 条文本应该合并成 ``ceil(N / EMBEDDING_BATCH_SIZE)`` 次请求，而不是 N 次。
这条一旦退化（有人按方舟的直觉改成"每条一次请求"），吞吐会掉一个数量级
（实测 379.6 片/s → 60 片/s 量级），而且不会有任何报错。

同时钉住"用 ``index`` 归位"而不是"按返回顺序"：上游返回乱序时若按顺序取，
chunk 与向量就会串行，检索结果莫名其妙 —— 这类错位极难从现象反推原因。
"""

from __future__ import annotations

import base64
import json
import struct
import threading
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.rag.embedding import CachingEmbeddingProvider, build_embedding_provider
from app.rag.embedding.siliconflow import SiliconFlowEmbeddingProvider

MODEL = "Qwen/Qwen3-Embedding-0.6B"


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    base: dict[str, object] = {
        "embedding_provider": "siliconflow",
        "embedding_model": MODEL,
        "embedding_dim": 4,
        "milvus_vector_dim": 4,
        "embedding_batch_size": 2,
        "siliconflow_api_key": "sk-test",
        "siliconflow_embedding_concurrency": 4,
        "siliconflow_embedding_max_retries": 1,
        "embedding_cache_enabled": False,
    }
    base.update(overrides)
    return make_settings(**base)


def _response(vectors: list[list[float]], *, indices: list[int] | None = None) -> httpx.Response:
    order = indices if indices is not None else list(range(len(vectors)))
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": MODEL,
            "data": [
                {"object": "embedding", "index": order[i], "embedding": vectors[i]}
                for i in range(len(vectors))
            ],
        },
    )


def _install(
    provider: SiliconFlowEmbeddingProvider,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[dict]:
    """换掉 httpx 客户端并记录每个请求体。"""
    seen: list[dict] = []
    lock = threading.Lock()

    def wrapped(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer sk-test"
        return handler(request)

    provider.close()
    provider._client = httpx.Client(transport=httpx.MockTransport(wrapped), timeout=5.0)
    return seen


# ---------------------------------------------------------------------------
# 1. 批量
# ---------------------------------------------------------------------------
def test_texts_are_batched_not_sent_one_by_one(make_settings: Callable[..., Settings]) -> None:
    """5 条、batch=2 ⇒ **3 次请求**，每次最多 2 条（这是与方舟档最本质的差别）。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    seen = _install(
        provider,
        lambda request: _response(
            [[0.1 * i, 1.0, 2.0, 3.0] for i in range(len(json.loads(request.content)["input"]))]
        ),
    )

    vectors = provider.embed([f"t{i}" for i in range(5)])

    assert [len(payload["input"]) for payload in seen] == [2, 2, 1]
    assert len(vectors) == 5
    provider.close()


def test_batch_payload_shape_matches_the_documented_api(
    make_settings: Callable[..., Settings],
) -> None:
    """请求体就是文档写的那几个字段：``model`` / ``input``（字符串数组）/ ``encoding_format``。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    seen = _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]]))

    provider.embed(["only one"])

    payload = seen[0]
    assert payload["model"] == MODEL
    assert payload["input"] == ["only one"]  # 字符串数组，不是 [{type,text}]（那是方舟）
    assert payload["encoding_format"] == "base64"
    assert "dimensions" not in payload  # 0 = 用原生维度，不发该字段
    provider.close()


def test_dimensions_is_sent_only_when_configured(make_settings: Callable[..., Settings]) -> None:
    """配置了 ``dimensions`` 才发该字段（用于 MRL 降维），否则交给上游原生维度。"""
    provider = SiliconFlowEmbeddingProvider(
        _settings(make_settings, siliconflow_embedding_dimensions=4)
    )
    seen = _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]]))

    provider.embed(["x"])

    assert seen[0]["dimensions"] == 4
    provider.close()


# ---------------------------------------------------------------------------
# 2. 顺序：用 index 归位
# ---------------------------------------------------------------------------
def test_order_follows_index_not_arrival_order(make_settings: Callable[..., Settings]) -> None:
    """上游乱序返回时，必须按 ``index`` 归位。

    断言用 ``v[1]/v[0]`` 这个归一化不变量（provider 会 L2 归一化）。
    """
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings, embedding_batch_size=3))

    def handler(_request: httpx.Request) -> httpx.Response:
        # 故意倒序返回：index=[2,1,0]
        vectors = [[1.0, 2.0, 0.0, 0.0], [1.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
        return _response(vectors, indices=[2, 1, 0])

    _install(provider, handler)
    vectors = provider.embed(["a", "b", "c"])

    assert [round(v[1] / v[0], 6) for v in vectors] == [0.0, 1.0, 2.0]
    provider.close()


def test_count_mismatch_is_rejected(make_settings: Callable[..., Settings]) -> None:
    """返回条数对不上必须报错：否则会静默变成"chunk 与向量错位"。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings, embedding_batch_size=2))
    _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]]))

    with pytest.raises(AppError) as excinfo:
        provider.embed(["a", "b"])

    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    provider.close()


def test_out_of_range_index_is_rejected(make_settings: Callable[..., Settings]) -> None:
    """返回的 ``index`` 超出本批请求范围 ⇒ ``RETRIEVAL_FAILED``（不能悄悄丢弃或错位）。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]], indices=[7]))

    with pytest.raises(AppError) as excinfo:
        provider.embed(["a"])
    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    provider.close()


# ---------------------------------------------------------------------------
# 3. 编码与维度
# ---------------------------------------------------------------------------
def test_base64_encoding_is_decoded_as_little_endian_float32(
    make_settings: Callable[..., Settings],
) -> None:
    """``base64`` 是 float32 小端（实测与 float 逐元素完全一致）。"""
    expected = [0.25, -0.5, 0.75, 0.125]
    payload = base64.b64encode(struct.pack("<4f", *expected)).decode()
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    _install(
        provider,
        lambda _r: httpx.Response(200, json={"data": [{"index": 0, "embedding": payload}]}),
    )

    vector = provider.embed(["x"])[0]

    norm = sum(value * value for value in expected) ** 0.5
    assert vector == pytest.approx([value / norm for value in expected])
    provider.close()


def test_float_encoding_is_also_accepted(make_settings: Callable[..., Settings]) -> None:
    """``encoding_format=float`` 时上游直接给浮点数组，同样要接受并做 L2 归一化。"""
    provider = SiliconFlowEmbeddingProvider(
        _settings(make_settings, siliconflow_embedding_encoding="float")
    )
    _install(
        provider,
        lambda _r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [3, 4, 0, 0]}]}),
    )

    assert provider.embed(["x"])[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    provider.close()


def test_dimension_mismatch_is_rejected(make_settings: Callable[..., Settings]) -> None:
    """维度不符 ⇒ ``VECTOR_DIM_MISMATCH``（写进去也查不出来）。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    _install(
        provider,
        lambda _r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0] * 9}]}),
    )

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.VECTOR_DIM_MISMATCH
    assert excinfo.value.details["actual_dim"] == 9
    provider.close()


# ---------------------------------------------------------------------------
# 4. 失败语义与边界
# ---------------------------------------------------------------------------
def test_missing_api_key_fails_without_any_request(
    make_settings: Callable[..., Settings],
) -> None:
    """缺 ``SILICONFLOW_API_KEY`` ⇒ 提示该配哪个环境变量的 503，且**一个请求都不发**。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings, siliconflow_api_key=""))
    seen = _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]]))

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])

    assert excinfo.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "SILICONFLOW_API_KEY" in excinfo.value.details["hint"]
    assert seen == []
    provider.close()


def test_empty_input_makes_no_request(make_settings: Callable[..., Settings]) -> None:
    """空输入返回空列表且不发请求（批量入库收尾时经常出现空批）。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    seen = _install(provider, lambda _r: _response([[1.0, 0.0, 0.0, 0.0]]))

    assert provider.embed([]) == []
    assert seen == []
    provider.close()


def test_rate_limit_is_retried_then_succeeds(make_settings: Callable[..., Settings]) -> None:
    """429 走共享重试策略（这里只验证"确实重试了并成功"）。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return _response([[1.0, 0.0, 0.0, 0.0]])

    provider = SiliconFlowEmbeddingProvider(
        _settings(make_settings, siliconflow_embedding_max_retries=1)
    )
    _install(provider, handler)

    assert provider.embed(["x"])[0] == pytest.approx([1.0, 0.0, 0.0, 0.0])
    assert calls["n"] == 2
    provider.close()


def test_embed_query_returns_single_vector(make_settings: Callable[..., Settings]) -> None:
    """查询与入库用同一个模型（否则两个向量不在同一空间）。"""
    provider = SiliconFlowEmbeddingProvider(_settings(make_settings))
    _install(provider, lambda _r: _response([[0.0, 1.0, 0.0, 0.0]]))

    assert provider.embed_query("查询") == pytest.approx([0.0, 1.0, 0.0, 0.0])
    provider.close()


# ---------------------------------------------------------------------------
# 5. 工厂
# ---------------------------------------------------------------------------
def test_factory_builds_siliconflow_provider(make_settings: Callable[..., Settings]) -> None:
    """``EMBEDDING_PROVIDER=siliconflow`` 且开缓存 ⇒ 硅基流动在里层、缓存包在外层。"""
    settings = _settings(make_settings, embedding_cache_enabled=True)
    provider = build_embedding_provider(settings)

    assert isinstance(provider, CachingEmbeddingProvider)
    assert isinstance(provider._inner, SiliconFlowEmbeddingProvider)
    assert provider.dim == 4
    provider._inner.close()
