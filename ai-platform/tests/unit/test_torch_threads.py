"""单元测试：本地 CPU 推理的 torch 线程数开关（``TORCH_NUM_THREADS``）。

覆盖三条语义：``0`` = 不干预、``>0`` = 真的设、没装 torch = 跳过 + 警告。
另加两条「接线」断言 —— 配置项最常见的失效方式是**加了没人读**，
所以断言必须落在两个 BGE provider 的**真实加载路径**上，而不是只测工具函数。

最后两条是「默认值」的守门（UP：默认不能再是 ``0``）：``0`` 会让 torch 按逻辑核数
开线程，向量化吃满整机、同机的 MySQL / Redis / 网关一起慢 2~3 倍。
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Callable

import pytest

from app.core.config import Settings, default_torch_num_threads
from app.rag.embedding import bge as embedding_bge
from app.rag.reranker import bge as reranker_bge
from app.rag.torch_threads import apply_torch_num_threads


def _install_fake_torch(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把假 ``torch`` 塞进 ``sys.modules``，返回它收到的调用参数列表。"""
    calls: list[int] = []
    module = types.ModuleType("torch")
    module.set_num_threads = calls.append  # type: ignore[attr-defined]
    module.__version__ = "0.0-fake"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", module)
    return calls


def test_zero_or_negative_means_no_intervention(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0``/负数 = 保持 torch 默认（单机独占时默认值才是吞吐最优的）。"""
    calls = _install_fake_torch(monkeypatch)

    assert apply_torch_num_threads(0) is False
    assert apply_torch_num_threads(-4) is False
    assert calls == []


def test_positive_value_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """``>0`` 时才真的收窄线程数。"""
    calls = _install_fake_torch(monkeypatch)

    assert apply_torch_num_threads(2) is True
    assert calls == [2]


def test_missing_torch_is_skipped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 torch 不该让调用方失败（``EMBEDDING_PROVIDER=hash`` 根本用不到它）。"""
    monkeypatch.setitem(sys.modules, "torch", None)  # ``import torch`` → ImportError

    assert apply_torch_num_threads(2) is False


# ---------------------------------------------------------------------------
# 默认值：核数的一半（而不是 0 = 不干预）
# ---------------------------------------------------------------------------
def test_default_is_half_of_logical_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认取**逻辑核数的一半**；取不到核数、或只有 1 核时兜底为 1。

    为什么不是 ``0``：``0`` = 不干预 = torch 按逻辑核数开线程，一次 8MB 文档的
    向量化会把整机的核吃满，同机 MySQL / Redis / 网关 / 验收脚本一起慢 2~3 倍
    （实测连跑两遍全套 M1 41s→75s、M2 71s→186s，见 ai-platform-go/docs/09 §7.2）。
    """
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    assert default_torch_num_threads() == 10

    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert default_torch_num_threads() == 1, "1 核也不能退化成 0（0 = 不干预）"

    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert default_torch_num_threads() == 1, "取不到核数时宁可设 1，也不要退回不干预"


def test_settings_pick_up_the_cpu_based_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings`` 的默认值真的走这个工厂（而不是模型里写死的 0）。"""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)

    settings = Settings(_env_file=None, app_env="local")

    assert settings.torch_num_threads == 4

    # 显式 0 仍然表示「不干预」——默认变了，语义没变
    assert Settings(_env_file=None, app_env="local", torch_num_threads=0).torch_num_threads == 0


class _FakeSentenceTransformer:
    """只记录构造参数的替身（绝不允许真的去下权重）。"""

    def __init__(self, model_name: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device


class _FakeFlagReranker:
    def __init__(self, model_name: str, use_fp16: bool = False, devices: str = "cpu") -> None:
        self.model_name = model_name


def _record_calls(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> list[int]:
    """把 ``module.apply_torch_num_threads`` 换成记录器。"""
    seen: list[int] = []

    def _record(value: int) -> bool:
        seen.append(value)
        return True

    monkeypatch.setattr(module, "apply_torch_num_threads", _record)
    return seen


def test_embedding_provider_wires_threads_when_model_loads(
    monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]
) -> None:
    """embedding 侧：构造时**不能**加载模型，触碰 ``.model`` 时才收窄线程数。"""
    seen = _record_calls(monkeypatch, embedding_bge)
    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)

    provider = embedding_bge.BgeEmbeddingProvider(make_settings(torch_num_threads=2))

    assert seen == []  # 懒加载：构造阶段不该碰模型
    assert provider.model is not None
    assert seen == [2]


def test_reranker_provider_wires_threads_when_model_loads(
    monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]
) -> None:
    """reranker 侧同理（同一进程、同一份核，必须用同一个上限）。"""
    seen = _record_calls(monkeypatch, reranker_bge)
    fake = types.ModuleType("FlagEmbedding")
    fake.FlagReranker = _FakeFlagReranker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "FlagEmbedding", fake)

    reranker = reranker_bge.BgeReranker(make_settings(torch_num_threads=3))

    assert seen == []
    assert reranker.model is not None
    assert seen == [3]
