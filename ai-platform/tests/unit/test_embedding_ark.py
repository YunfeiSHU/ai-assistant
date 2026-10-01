"""单测：火山方舟（Ark）云端向量化 provider（``EMBEDDING_PROVIDER=ark``）。

这个 provider 的**语义与其它实现不同**，所以断言必须钉住那几条差异，否则换回
"批量端点"的直觉就会把 bug 写回来：

1. **一次请求只发一条文本**（端点把 ``input`` 当"一个多模态输入"，传多条只会聚合成
   一条向量）⇒ 用 MockTransport 数请求数，并断言每次请求体里只有 1 个 text；
2. **顺序保真**：并发完成顺序是不确定的，但返回必须与入参一一对应
   （错位会让 chunk 与向量串行，检索结果莫名其妙）；
3. **两种响应形状都认**（多模态的 ``data.embedding`` 与 OpenAI 风格的 ``data[]``），
   这样以后换成真批量文本模型只需改 base_url + model；
4. **维度自检**：与 ``EMBEDDING_DIM`` 不符时抛 ``VECTOR_DIM_MISMATCH``，
   而不是把坏向量写进 Milvus（写了也查不出来）；
5. **只对 429/5xx 重试**：400/401 立刻失败并带出上游原文，否则"key 过期"会被
   埋成"重试 3 次后 503"。

全部走 ``httpx.MockTransport``：真实 httpx 的请求/响应/头处理都被走到，但不联网。
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AppError, ErrorCode
from app.rag.embedding import CachingEmbeddingProvider, build_embedding_provider
from app.rag.embedding.ark import ArkEmbeddingProvider

MODEL = "doubao-embedding-vision-251215"


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    base: dict[str, object] = {
        "embedding_provider": "ark",
        "embedding_model": MODEL,
        "embedding_dim": 4,  # 小维度便于断言，与 EMBEDDING_MODEL_DIMS 的 2048 无关
        "milvus_vector_dim": 4,
        "ark_api_key": "ark-test-key",
        "ark_embedding_concurrency": 4,
        "ark_embedding_max_retries": 2,
        "embedding_cache_enabled": False,
    }
    base.update(overrides)
    return make_settings(**base)


def _multimodal_response(seed: float) -> httpx.Response:
    """多模态端点的真实形状：``{"data": {"embedding": [float...]}}``。"""
    return httpx.Response(
        200,
        json={
            "id": "x",
            "model": MODEL,
            "data": {"object": "embedding", "embedding": [seed, seed + 1, seed + 2, seed + 3]},
        },
    )


def _install_transport(
    provider: ArkEmbeddingProvider,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """把 provider 的 httpx 客户端换成 MockTransport，并记录所有请求。"""
    seen: list[httpx.Request] = []
    lock = threading.Lock()

    def wrapped(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(request)
        return handler(request)

    provider.close()
    provider._client = httpx.Client(transport=httpx.MockTransport(wrapped), timeout=5.0)
    return seen


# ---------------------------------------------------------------------------
# 1. 每片一次请求
# ---------------------------------------------------------------------------
def test_one_request_per_text(make_settings: Callable[..., Settings]) -> None:
    """N 条文本 ⇒ N 次请求，且**每次只带 1 条 text**（这是本端点的硬语义）。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert len(body["input"]) == 1, "多模态端点一次只能表达一个输入"
        assert body["input"][0]["type"] == "text"
        assert body["model"] == MODEL
        assert request.headers["authorization"] == "Bearer ark-test-key"
        return _multimodal_response(float(body["input"][0]["text"].split("-")[-1]))

    seen = _install_transport(provider, handler)
    vectors = provider.embed([f"t-{i}" for i in range(5)])

    assert len(seen) == 5
    assert len(vectors) == 5
    provider.close()


def test_order_is_preserved_even_when_later_items_finish_first(
    make_settings: Callable[..., Settings],
) -> None:
    """并发完成顺序 ≠ 入参顺序，但返回值必须与入参一一对应。

    断言用 ``v[1]/v[0]`` 这个**归一化不变量**（provider 会 L2 归一化，
    直接比原始值会被归一化"吃掉"）。
    """
    provider = ArkEmbeddingProvider(_settings(make_settings))

    def handler(request: httpx.Request) -> httpx.Response:
        index = int(json.loads(request.content)["input"][0]["text"].split("-")[-1])
        # 故意让前面的慢、后面的快：若实现用 as_completed 收集就会错位
        time.sleep(0.08 if index < 3 else 0.0)
        return httpx.Response(200, json={"data": {"embedding": [1.0, float(index), 0.0, 0.0]}})

    _install_transport(provider, handler)
    vectors = provider.embed([f"t-{i}" for i in range(6)])

    assert [round(vector[1] / vector[0], 6) for vector in vectors] == [float(i) for i in range(6)]
    provider.close()


# ---------------------------------------------------------------------------
# 2. 响应形状
# ---------------------------------------------------------------------------
def test_default_request_asks_for_base64_and_decodes_it(
    make_settings: Callable[..., Settings],
) -> None:
    """默认 ``encoding_format=base64``：请求体带上它，响应按 float32 **小端**解码。

    响应体从 34,240B 降到 11,219B（实测 3.05×），省的是传输与 JSON 解析 ——
    所以这条断言同时钉住"我们要的是 base64"与"解出来仍是同一个向量"。
    """
    expected = [0.25, -0.5, 0.75, 0.125]
    encoded = base64.b64encode(struct.pack("<4f", *expected)).decode()
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"object": "embedding", "embedding": encoded}})

    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(provider, handler)
    vector = provider.embed(["x"])[0]

    assert payloads[0]["encoding_format"] == "base64"
    norm = sum(value * value for value in expected) ** 0.5
    assert vector == pytest.approx([value / norm for value in expected])
    provider.close()


def test_optional_dimensions_and_instructions_are_passed_through(
    make_settings: Callable[..., Settings],
) -> None:
    """``ARK_EMBEDDING_DIMENSIONS`` / ``ARK_EMBEDDING_INSTRUCTIONS`` 非空才发。"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _multimodal_response(1.0)

    provider = ArkEmbeddingProvider(
        _settings(
            make_settings,
            ark_embedding_dimensions=4,
            ark_embedding_instructions="为检索任务生成向量",
        )
    )
    _install_transport(provider, handler)
    provider.embed(["x"])
    assert payloads[0]["dimensions"] == 4
    assert payloads[0]["instructions"] == "为检索任务生成向量"
    provider.close()

    payloads.clear()
    plain = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(plain, handler)
    plain.embed(["x"])
    assert "dimensions" not in payloads[0]
    assert "instructions" not in payloads[0]
    plain.close()


def test_float_encoding_is_still_accepted(make_settings: Callable[..., Settings]) -> None:
    """``encoding_format=float``（或上游忽略该参数时）也要能解析。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"object": "embedding", "embedding": [1, 2, 3, 4]}}
        )

    provider = ArkEmbeddingProvider(_settings(make_settings, ark_embedding_encoding="float"))
    _install_transport(provider, handler)

    expected = [value / 30**0.5 for value in (1, 2, 3, 4)]
    assert provider.embed(["x"])[0] == pytest.approx(expected)
    provider.close()


def test_broken_base64_is_reported_not_silently_zeroed(
    make_settings: Callable[..., Settings],
) -> None:
    """坏 base64 / 非 4 字节倍数必须报错 —— 静默返回零向量会污染整个检索。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(
        provider,
        lambda _r: httpx.Response(
            200, json={"data": {"embedding": base64.b64encode(b"\x01\x02\x03").decode()}}
        ),
    )

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    provider.close()


def test_openai_style_batch_response_is_also_accepted(
    make_settings: Callable[..., Settings],
) -> None:
    """``data`` 是数组时取第一条（为将来换"真批量"文本模型留的兼容口）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"object": "embedding", "embedding": [1, 2, 3, 4]}]}
        )

    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(provider, handler)

    # [1,2,3,4] 的 L2 归一化：除以 √30
    expected = [value / 30**0.5 for value in (1, 2, 3, 4)]
    assert provider.embed(["x"])[0] == pytest.approx(expected)
    provider.close()


def test_malformed_payload_raises_retrieval_failed(
    make_settings: Callable[..., Settings],
) -> None:
    """没有可用向量时明确失败，而不是返回零向量（零向量会静默污染检索）。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(provider, lambda _r: httpx.Response(200, json={"data": {}}))

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    provider.close()


# ---------------------------------------------------------------------------
# 3. 维度与归一化
# ---------------------------------------------------------------------------
def test_dimension_mismatch_is_rejected(make_settings: Callable[..., Settings]) -> None:
    """返回维度与 ``EMBEDDING_DIM`` 不一致 ⇒ ``VECTOR_DIM_MISMATCH``（写进去也查不出来）。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(
        provider, lambda _r: httpx.Response(200, json={"data": {"embedding": [0.0] * 7}})
    )

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.VECTOR_DIM_MISMATCH
    assert excinfo.value.details["actual_dim"] == 7
    assert excinfo.value.details["configured_dim"] == 4
    provider.close()


def test_vectors_are_l2_normalized(make_settings: Callable[..., Settings]) -> None:
    """集合用 COSINE，必须归一化（同时抹平上游是否归一化的差异）。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(
        provider, lambda _r: httpx.Response(200, json={"data": {"embedding": [3, 4, 0, 0]}})
    )

    vector = provider.embed(["x"])[0]
    assert sum(value * value for value in vector) ** 0.5 == pytest.approx(1.0)
    assert vector[:2] == pytest.approx([0.6, 0.8])
    provider.close()


# ---------------------------------------------------------------------------
# 4. 重试与失败语义
# ---------------------------------------------------------------------------
def test_retries_on_rate_limit_then_succeeds(make_settings: Callable[..., Settings]) -> None:
    """429 要退避重试（并尊重 ``Retry-After``），最终成功就算成功。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": {"code": "RateLimit"}}
            )
        return _multimodal_response(1.0)

    provider = ArkEmbeddingProvider(_settings(make_settings, ark_embedding_max_retries=1))
    _install_transport(provider, handler)

    assert provider.embed(["x"])[0][0] == pytest.approx(1 / 30**0.5)  # [1,2,3,4] 归一化后的首元素
    assert calls["n"] == 2
    provider.close()


def test_client_error_fails_fast_without_retry(make_settings: Callable[..., Settings]) -> None:
    """401 不重试：重试只会浪费配额，还会把"key 过期"埋成"重试后 503"。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            401, json={"error": {"code": "AuthenticationError", "message": "bad key"}}
        )

    provider = ArkEmbeddingProvider(_settings(make_settings))
    _install_transport(provider, handler)

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.RETRIEVAL_FAILED
    assert "bad key" in excinfo.value.details["detail"]
    assert calls["n"] == 1
    provider.close()


def test_exhausted_retries_report_dependency_unavailable(
    make_settings: Callable[..., Settings],
) -> None:
    """5xx 重试耗尽 ⇒ ``DEPENDENCY_UNAVAILABLE``（对上层的语义是"稍后再来"）。"""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="upstream down")

    provider = ArkEmbeddingProvider(_settings(make_settings, ark_embedding_max_retries=1))
    _install_transport(provider, handler)

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert calls["n"] == 2
    provider.close()


def test_missing_api_key_is_a_configuration_error(make_settings: Callable[..., Settings]) -> None:
    """没配 key 时给可执行的提示，而不是发出一个必然 401 的请求。"""
    provider = ArkEmbeddingProvider(_settings(make_settings, ark_api_key=""))
    calls = _install_transport(provider, lambda _r: _multimodal_response(1.0))

    with pytest.raises(AppError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "ARK_API_KEY" in excinfo.value.details["hint"]
    assert calls == []
    provider.close()


def test_empty_input_makes_no_request(make_settings: Callable[..., Settings]) -> None:
    """空输入不产生请求（``_embed_and_upsert`` 在空文档时会走到）。"""
    provider = ArkEmbeddingProvider(_settings(make_settings))
    calls = _install_transport(provider, lambda _r: _multimodal_response(1.0))

    assert provider.embed([]) == []
    assert calls == []
    provider.close()


# ---------------------------------------------------------------------------
# 5. 工厂与启动期校验
# ---------------------------------------------------------------------------
def test_factory_builds_ark_provider(make_settings: Callable[..., Settings]) -> None:
    """``EMBEDDING_PROVIDER=ark`` ⇒ Ark 在里层、缓存包在外层。"""
    settings = _settings(make_settings, embedding_cache_enabled=True)
    provider = build_embedding_provider(settings)

    assert isinstance(provider, CachingEmbeddingProvider)
    assert isinstance(provider._inner, ArkEmbeddingProvider)
    assert provider.dim == 4
    provider._inner.close()


def test_startup_validation_rejects_ark_without_key(
    make_settings: Callable[..., Settings],
) -> None:
    """``EMBEDDING_PROVIDER=ark`` 但缺 ``ARK_API_KEY``：启动期就失败，别等第一次检索。"""
    settings = _settings(make_settings, ark_api_key="")
    with pytest.raises(Exception, match="ARK_API_KEY"):
        settings.validate_for_startup()


def test_startup_validation_rejects_out_of_range_concurrency(
    make_settings: Callable[..., Settings],
) -> None:
    """``ARK_EMBEDDING_CONCURRENCY`` 必须为正（0 会让批量入库永远不前进）。"""
    settings = _settings(make_settings, ark_embedding_concurrency=0)
    with pytest.raises(Exception, match="ARK_EMBEDDING_CONCURRENCY"):
        settings.validate_for_startup()


def test_startup_validation_rejects_known_model_dim_mismatch(
    make_settings: Callable[..., Settings],
) -> None:
    """``-251215`` 的真实维度是 2048；写成 1024 时启动期就拦（否则静默查不到）。"""
    settings = _settings(
        make_settings,
        embedding_model=MODEL,
        embedding_dim=1024,
        milvus_vector_dim=1024,
    )
    with pytest.raises(Exception, match="2048"):
        settings.validate_for_startup()
