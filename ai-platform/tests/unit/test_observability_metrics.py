"""指标门面单测（``REQ-NFR-011``，契约见 ``docs/10`` §5.2）。

这组测试盯住三件容易悄悄坏掉的事：

1. **注册表必须是实例私有的**。``prometheus_client`` 的默认注册表是进程级全局的，
   同名指标注册第二次会抛 ``Duplicated timeseries`` —— 而「一个进程里建多个应用」
   是测试与多租户场景的常态。所以第一条断言就是「连开两个实例不炸」。
2. **标签不允许出现高基数维度**。``docs/10`` §5.2 明令 ``user_id`` / ``doc_id`` /
   ``conversation_id`` / ``task_id`` 不得作为标签；这类违规不会报错，只会让
   Prometheus 的时间序列数无声爆炸，所以必须由测试把住。
3. **枚举型 Gauge 要显式置零**。状态类指标用「当前取值置 1、其余置 0」表达，
   忘记置零会让告警规则 ``== 0`` 永远为真。

实现细节：断言优先读 ``render()`` 的文本输出（等价于 Prometheus 真正看到的东西），
只有「文本里看不出差值」的地方才直读注册表 —— 那属于有意为之的白盒断言。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.observability.metrics import (
    CIRCUIT_VALUES,
    MCP_STATES,
    Metrics,
    configure_metrics,
    get_metrics,
)

#: ``docs/10`` §5.2 禁止出现在标签里的高基数维度
FORBIDDEN_LABELS = frozenset({"user_id", "doc_id", "conversation_id", "task_id", "kb_id"})


def _label_names(metrics: Metrics) -> set[str]:
    """收集注册表里所有样本的标签名（白盒：故意直接读注册表）。"""
    registry: Any = metrics._registry
    names: set[str] = set()
    for metric in registry.collect():
        for sample in metric.samples:
            names.update(sample.labels)
    return names


def _value(metric: Any, *labels: str) -> float:
    """读一条已存在序列的当前值（白盒）。"""
    return float(metric.labels(*labels)._value.get())


def _record_everything(metrics: Metrics) -> None:
    """把所有记录方法各调一次，确保懒创建的标签也被覆盖。"""
    metrics.observe_request(endpoint="/api/v1/chat", method="POST", status=200, seconds=0.2)
    metrics.observe_first_token(model="m", use_rag=True, seconds=0.5)
    metrics.record_cancel("client_disconnect")
    metrics.add_llm_tokens(model="m", prompt=10, completion=20)
    metrics.record_llm_error(model="m", code="UPSTREAM_TIMEOUT")
    metrics.observe_retrieve(kb_count=2, seconds=0.1)
    metrics.observe_rerank(device="cpu", skipped=False, seconds=0.2)
    metrics.observe_recalled(7)
    metrics.observe_embedding(model="m", cache_hit=False, seconds=1.0)
    metrics.observe_agent_steps(finish_reason="stop", steps=2)
    metrics.record_tool_call(tool_name="t", source="mcp", status="ok")
    metrics.record_task(task_type="ingest", status="succeeded", seconds=3.0)
    metrics.set_mcp_server_state(server="fs", state="connected")
    metrics.set_circuit_state(target="llm", state="open")
    metrics.observe_model_load(model="m", seconds=5.0)
    metrics.record_degraded("retrieval_unavailable")


def test_two_instances_do_not_collide() -> None:
    """同进程连开两个实例不抛 ``Duplicated timeseries``，且计数互不污染。"""
    first = Metrics(service="a")
    second = Metrics(service="b")

    assert first.enabled
    assert second.enabled
    line = b'ai_requests_total{endpoint="/x",method="GET",status="200"}'
    first.observe_request(endpoint="/x", method="GET", status=200, seconds=0.01)
    first.observe_request(endpoint="/x", method="GET", status=200, seconds=0.01)
    second.observe_request(endpoint="/x", method="GET", status=200, seconds=0.01)

    assert line + b" 2.0" in first.render()[0]
    assert line + b" 1.0" in second.render()[0]


def test_disabled_metrics_are_noops() -> None:
    """关闭后所有记录方法都是空操作，``render`` 返回说明文本。"""
    metrics = Metrics(enabled=False)

    metrics.observe_request(endpoint="/x", method="GET", status=200, seconds=1.0)
    metrics.record_degraded("x")
    metrics.set_mcp_server_state(server="s", state="connected")
    body, content_type = metrics.render()

    assert not metrics.enabled
    assert b"metrics disabled" in body
    assert content_type.startswith("text/plain")


def test_render_exposes_build_info() -> None:
    """``ai_build_info`` 必须带服务名标签 —— 多服务共用一个 Prometheus 时靠它区分。"""
    metrics = Metrics(service="ai-platform")

    body, _ = metrics.render()

    assert b"ai_build_info" in body
    assert b'service="ai-platform"' in body


def test_no_high_cardinality_labels() -> None:
    """任何样本的标签名都不得包含高基数维度。"""
    metrics = Metrics()

    _record_everything(metrics)

    assert _label_names(metrics) & FORBIDDEN_LABELS == set()


def test_endpoint_label_is_route_template() -> None:
    """``endpoint`` 记的是路由模板：带 id 的路径只会产生一条时间序列。

    这条断言是「高基数」这个坑的回归测试 —— 用原始路径当标签时，
    ``/kb/{id}`` 有多少个知识库就有多少条时间序列。
    """
    metrics = Metrics()

    for _ in range(3):
        metrics.observe_request(
            endpoint="/api/v1/knowledge-bases/{kb_id}",
            method="GET",
            status=200,
            seconds=0.05,
        )

    body, _ = metrics.render()
    assert b"kb_1" not in body
    assert b'ai_requests_total{endpoint="/api/v1/knowledge-bases/{kb_id}"' in body


def test_set_mcp_server_state_zeroes_other_states() -> None:
    """状态 Gauge 表达枚举当前值：切到新状态时旧状态必须归零。"""
    metrics = Metrics()

    metrics.set_mcp_server_state(server="fs", state="connecting")
    metrics.set_mcp_server_state(server="fs", state="connected")

    assert _value(metrics.mcp_server_state, "fs", "connecting") == 0
    assert _value(metrics.mcp_server_state, "fs", "connected") == 1


def test_every_declared_mcp_state_has_a_series() -> None:
    """每个声明过的状态都有一条样本（看板上不会因为缺样本断线）。"""
    metrics = Metrics()
    metrics.set_mcp_server_state(server="fs", state="unavailable")

    body, _ = metrics.render()

    for state in MCP_STATES:
        assert f'server="fs",state="{state}"'.encode() in body


def test_forget_mcp_server_removes_series() -> None:
    """配置里删掉 Server 后要能清掉残留序列（否则永远停在最后一个状态）。"""
    metrics = Metrics()
    metrics.set_mcp_server_state(server="gone", state="connected")

    metrics.forget_mcp_server("gone")

    body, _ = metrics.render()
    assert b'server="gone"' not in body
    # 再删一次不抛异常（幂等）
    metrics.forget_mcp_server("gone")


def test_set_circuit_state_maps_to_numbers() -> None:
    """熔断状态映射成数值（0/1/2），未知取值退化为 0 而不是抛异常。"""
    metrics = Metrics()

    for state, value in CIRCUIT_VALUES.items():
        metrics.set_circuit_state(target="llm", state=state)
        assert _value(metrics.circuit_state, "llm") == value

    metrics.set_circuit_state(target="llm", state="不存在的状态")
    assert _value(metrics.circuit_state, "llm") == 0


def test_negative_durations_are_clamped_to_zero() -> None:
    """耗时/条数取负值时按 0 记录：直方图出现负值会污染分位数。"""
    metrics = Metrics()

    metrics.observe_request(endpoint="/x", method="GET", status=200, seconds=-1.0)
    metrics.observe_rerank(device="cpu", skipped=True, seconds=-0.5)
    metrics.observe_recalled(-5)

    body, _ = metrics.render()
    assert b'ai_request_duration_seconds_sum{endpoint="/x"} 0.0' in body
    assert b'ai_rag_rerank_seconds_sum{device="cpu",skipped="true"} 0.0' in body
    assert b"ai_rag_recalled_total_count 1.0" in body


def test_zero_token_usage_creates_no_series() -> None:
    """token 用量为 0 时不建序列：避免「零用量」也占一条时间序列。"""
    metrics = Metrics()

    metrics.add_llm_tokens(model="m", prompt=0, completion=0)

    body, _ = metrics.render()
    assert b'ai_llm_tokens_total{model="m"' not in body


def test_has_metric_detects_counter_and_histogram() -> None:
    """``has_metric`` 同时认指标本体（``_total``）与其直方图分桶。

    注意直方图的**注册名**是 ``ai_request_duration_seconds``（分桶样本才带
    ``_bucket`` 后缀），写 ``ai_request_duration`` 是查不到的。
    """
    metrics = Metrics()
    metrics.record_degraded("retrieval_unavailable")
    metrics.observe_request(endpoint="/x", method="GET", status=200, seconds=0.1)

    assert metrics.has_metric("ai_degraded")
    assert metrics.has_metric("ai_request_duration_seconds")
    assert not metrics.has_metric("ai_nonexistent")


def test_get_metrics_falls_back_to_noop_instance() -> None:
    """未配置时返回共享的空操作实例（深层调用无需判空）。"""
    import app.infrastructure.observability.metrics as module

    original = module._active
    try:
        module._active = None
        assert get_metrics() is get_metrics()
        assert not get_metrics().enabled
    finally:
        module._active = original


def test_configure_metrics_makes_instance_active() -> None:
    """``configure_metrics`` 之后 ``get_metrics()`` 就是它。"""
    import app.infrastructure.observability.metrics as module

    original = module._active
    metrics = Metrics()
    try:
        configure_metrics(metrics)
        assert get_metrics() is metrics
    finally:
        module._active = original


def test_render_is_prometheus_text_format() -> None:
    """``render`` 产出 Prometheus 文本格式且声明正确的 content-type。"""
    metrics = Metrics()
    metrics.observe_agent_steps(finish_reason="max_steps", steps=8)

    body, content_type = metrics.render()

    assert b"# HELP ai_agent_steps" in body
    assert b"# TYPE ai_agent_steps histogram" in body
    assert content_type == "text/plain; version=0.0.4; charset=utf-8"


@pytest.mark.parametrize("state", ["connected", "connecting", "unavailable", "disabled"])
def test_mcp_state_accepts_all_declared_values(state: str) -> None:
    """声明过的 MCP 状态都能被记录（``McpStatus`` 与指标取值必须对齐）。"""
    metrics = Metrics()

    metrics.set_mcp_server_state(server="fs", state=state)

    assert _value(metrics.mcp_server_state, "fs", state) == 1
