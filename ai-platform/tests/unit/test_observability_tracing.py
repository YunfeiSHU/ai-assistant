"""链路追踪单测（``REQ-NFR-010``，契约见 ``docs/10`` §5.1）。

**本模块最重要的一条断言**：Jaeger 里的 trace id 必须与日志 / ``X-Trace-Id``
里的那个 ``trace_id`` 是**同一个字符串**。若由 OTel 自己生成 trace id，排障时就
得在两套 id 之间人工对照，而 ``REQ-GEN-006``「任一请求可按 trace_id 串起全链路」
正是要避免这件事。所以这里用内存导出器抓真实 span，再比对它的 trace_id。

几个刻意的做法：

* 采样率设成 ``1.0``。默认 ``0.1`` 会让「span 有没有被导出」变成概率事件，
  测试随机红。``request_span`` 走的是带 ``SAMPLED`` 标记的远端父上下文，
  但依赖采样器参数的用例不该靠运气。
* 断言按 span 名过滤。``TracerProvider`` 是**进程级单例**，同一进程里别的用例
  产生的 span 也会进同一个导出器，不能直接 ``assert len(spans) == 1``。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.core.context import request_context
from app.infrastructure.observability import tracing as tracing_module
from app.infrastructure.observability.tracing import (
    Tracing,
    configure_tracing,
    get_tracing,
    setup_tracing,
    shutdown_tracing,
)

#: 一个固定的 trace/span 组合（形状与中间件产出的完全一致）
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


@pytest.fixture
def session_tracing(make_settings: Callable[..., Settings]) -> Tracing:
    """已启用的 Tracing 实例（此时进程里已装入 SDK ``TracerProvider``）。

    ``otel_traces_sampler_arg=1.0``：默认的 0.1 会让「span 有没有被导出」
    变成概率事件，于是测试随机红。
    """
    settings = make_settings(
        otel_enabled=True,
        otel_traces_sampler_arg=1.0,
        otel_service_name="ai-platform-test",
    )
    instance = setup_tracing(settings)
    assert instance.enabled
    return instance


@pytest.fixture
def exporter(session_tracing: Tracing) -> Iterator[InMemorySpanExporter]:
    """把 span 收进内存导出器（依赖 ``session_tracing`` 保证 provider 已装好）。

    挂的是**同步**处理器：``BatchSpanProcessor`` 是异步批量发送的，断言会读到空列表。
    """
    collector = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(collector))
    try:
        yield collector
    finally:
        # 不要 shutdown 全局 provider —— 那会连带关掉别人挂的处理器
        collector.clear()


@pytest.fixture
def enabled_tracing(session_tracing: Tracing, exporter: InMemorySpanExporter) -> Tracing:
    """``session_tracing`` 的别名，同时表达「导出器已就位」这个前置条件。"""
    return session_tracing


def test_disabled_tracing_is_a_noop() -> None:
    """关闭时两个上下文管理器都可安全进入，且 ``enabled`` 为假。"""
    tracing = Tracing(enabled=False)

    with (
        tracing.request_span("root", trace_id=TRACE_ID, span_id=SPAN_ID),
        tracing.span("child", {"k": "v"}),
    ):
        pass

    assert not tracing.enabled


def test_setup_tracing_respects_disabled_flag(make_settings: Callable[..., Settings]) -> None:
    """``OTEL_ENABLED=false`` 时不构造 provider（也就不会往 collector 发数据）。"""
    tracing = setup_tracing(make_settings(otel_enabled=False))

    assert not tracing.enabled


def test_request_span_reuses_the_context_trace_id(
    enabled_tracing: Tracing, exporter: InMemorySpanExporter
) -> None:
    """根 span 的 trace id 必须等于上下文里的 ``trace_id``。"""
    with (
        request_context(TRACE_ID, SPAN_ID, "req_test"),
        enabled_tracing.request_span(
            "GET /api/v1/chat",
            trace_id=TRACE_ID,
            span_id=SPAN_ID,
            attributes={"http.method": "GET"},
        ),
    ):
        pass

    spans = [s for s in exporter.get_finished_spans() if s.name == "GET /api/v1/chat"]
    assert len(spans) == 1
    assert format(spans[0].context.trace_id, "032x") == TRACE_ID
    assert spans[0].attributes["http.method"] == "GET"


def test_child_span_stays_in_the_same_trace(
    enabled_tracing: Tracing, exporter: InMemorySpanExporter
) -> None:
    """子 span（rag.retrieve / llm.invoke）必须挂在同一棵树上。"""
    with (
        request_context(TRACE_ID, SPAN_ID, "req_test"),
        enabled_tracing.request_span("root", trace_id=TRACE_ID, span_id=SPAN_ID),
        enabled_tracing.span("llm.invoke", {"model": "deepseek-flash"}),
    ):
        pass

    children = [s for s in exporter.get_finished_spans() if s.name == "llm.invoke"]
    assert len(children) == 1
    assert format(children[0].context.trace_id, "032x") == TRACE_ID
    assert children[0].parent is not None


def test_attributes_with_none_are_skipped(
    enabled_tracing: Tracing, exporter: InMemorySpanExporter
) -> None:
    """``None`` 属性不写入（OTel 会抛类型错误，且空值没有检索价值）。"""
    with (
        request_context(TRACE_ID, SPAN_ID, "req_test"),
        enabled_tracing.request_span(
            "root",
            trace_id=TRACE_ID,
            span_id=SPAN_ID,
            attributes={"user_id": None, "http.path": "/x"},
        ),
    ):
        pass

    span = next(s for s in exporter.get_finished_spans() if s.name == "root")
    assert "user_id" not in span.attributes
    assert span.attributes["http.path"] == "/x"


def test_invalid_context_ids_fall_back_to_a_new_trace(
    enabled_tracing: Tracing, exporter: InMemorySpanExporter
) -> None:
    """非法/全零 id 不能拼出坏的 ``SpanContext``，退化成新 trace 而不是崩溃。"""
    with enabled_tracing.request_span("root", trace_id="0" * 32, span_id="0" * 16):
        pass

    spans = [s for s in exporter.get_finished_spans() if s.name == "root"]
    assert len(spans) == 1
    assert format(spans[0].context.trace_id, "032x") != "0" * 32


def test_setup_tracing_is_idempotent_across_apps(
    make_settings: Callable[..., Settings],
) -> None:
    """一个进程里建多个应用：provider 被复用，而不是第二次设置被静默忽略。

    OTel 的 ``set_tracer_provider`` 只允许生效一次，重复设置会打
    ``Overriding of current TracerProvider is not allowed`` 并且**丢弃**后来者的 span。
    所以第二次 ``setup_tracing`` 必须复用已有 provider。
    """
    first_settings = make_settings(otel_enabled=True, otel_service_name="first")
    first = setup_tracing(first_settings)
    provider = trace.get_tracer_provider()

    second = setup_tracing(make_settings(otel_enabled=True, otel_service_name="second"))

    assert first.enabled
    assert second.enabled
    assert trace.get_tracer_provider() is provider


def test_shutdown_tracing_is_safe_without_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非本进程创建的 provider 不该被关掉（关了别的应用就再也发不出 span）。"""
    monkeypatch.setattr(tracing_module, "_provider_owned", False)

    shutdown_tracing()  # 不应抛异常，也不应触发 provider.shutdown()


def test_configure_and_get_tracing_never_return_none() -> None:
    """``get_tracing()`` 永远返回可用对象 —— 深层调用无需判空。"""
    import app.infrastructure.observability.tracing as module

    original = module._active
    try:
        module._active = None
        assert get_tracing() is get_tracing()
        assert not get_tracing().enabled
    finally:
        module._active = original


def test_configure_tracing_makes_instance_active() -> None:
    """``configure_tracing`` 之后 ``get_tracing()`` 就是它。"""
    import app.infrastructure.observability.tracing as module

    original = module._active
    instance = Tracing(enabled=False)
    try:
        configure_tracing(instance)
        assert get_tracing() is instance
    finally:
        module._active = original
