"""指标收集（``REQ-NFR-011``，契约见 ``docs/10`` §5.2）。

设计要点：

* **每个实例一套独立注册表**（``CollectorRegistry``）。``prometheus_client`` 的默认
  注册表是进程级全局的，第二次注册同名指标会抛 ``Duplicated timeseries`` ——
  而「同一进程里创建多个应用实例」是测试的常态。用独立注册表后，
  ``app.state.metrics`` 与它记录的指标是**同一个对象**，断言不会串味。
* **标签一律取有限集合**：``endpoint`` 取路由模板（未匹配则 ``unmatched``）、
  ``tool_name`` 取注册表里的名字、``code`` 取错误码枚举。``user_id`` /
  ``conversation_id`` / ``doc_id`` / ``kb_id`` / ``task_id`` MUST NOT 出现在标签里
  （docs/10 §5.2 明令禁止高基数标签），它们只进日志与 trace。
* 依赖缺失时**降级为空操作**而不报错：指标是观测手段，不该成为服务可用性的前置条件。
"""

from __future__ import annotations

import contextlib
from typing import Any, Final

from app.core.logging import get_logger

logger = get_logger("app.metrics")

#: 接口耗时分桶（docs/10 §5.2）
REQUEST_BUCKETS: Final[tuple[float, ...]] = (0.05, 0.1, 0.3, 0.6, 1, 2, 5, 10, 30)
#: 首 token 延迟分桶（红线 1.5s，围绕它加密）
FIRST_TOKEN_BUCKETS: Final[tuple[float, ...]] = (0.1, 0.3, 0.7, 1.0, 1.5, 3.0, 6.0, 12.0)
#: 检索 / 重排耗时（红线 0.3s / 1.5s）
RETRIEVAL_BUCKETS: Final[tuple[float, ...]] = (0.05, 0.1, 0.15, 0.3, 0.6, 1.5, 3.0)
#: 任务耗时（秒级，长尾到 30 分钟）
TASK_BUCKETS: Final[tuple[float, ...]] = (1, 5, 15, 60, 180, 600, 1800)

#: MCP Server 的状态取值（``ai_mcp_server_state`` 的 ``state`` 标签）
MCP_STATES: Final[tuple[str, ...]] = ("connected", "connecting", "unavailable", "disabled")

#: 熔断状态到数值的映射（``ai_circuit_breaker_state``：0 关闭 / 1 半开 / 2 打开）
CIRCUIT_VALUES: Final[dict[str, int]] = {"closed": 0, "half_open": 1, "open": 2}


def _prometheus() -> Any | None:
    """懒导入 ``prometheus_client``；缺失时返回 ``None``（不抛，见模块 docstring）。"""
    try:
        import prometheus_client
    except Exception:  # pragma: no cover - 仅在依赖缺失的环境触发
        return None
    return prometheus_client


class Metrics:
    """指标门面：全部记录方法都**不抛异常**、**不阻塞**。

    调用方（中间件 / 服务层）不需要 try/except，也不需要判断 ``enabled``：
    依赖缺失时对象内所有方法退化成空操作。
    """

    def __init__(self, *, enabled: bool = True, service: str = "ai-platform") -> None:
        self._enabled = False
        self._registry: Any = None
        self._render: Any = None
        if not enabled:
            return
        client = _prometheus()
        if client is None:
            logger.warning(
                "metrics.disabled",
                extra={"reason": "prometheus_client 未安装，/metrics 只返回说明文本"},
            )
            return
        self._registry = client.CollectorRegistry()
        self._render = client.generate_latest
        self._enabled = True
        self._build(client, service)

    # ------------------------------------------------------------------
    # 指标定义
    # ------------------------------------------------------------------
    def _build(self, client: Any, service: str) -> None:
        registry = self._registry

        def counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Any:
            return client.Counter(name, doc, labels, registry=registry)

        def histogram(
            name: str, doc: str, labels: tuple[str, ...], buckets: tuple[float, ...]
        ) -> Any:
            return client.Histogram(name, doc, labels, buckets=buckets, registry=registry)

        def gauge(name: str, doc: str, labels: tuple[str, ...]) -> Any:
            return client.Gauge(name, doc, labels, registry=registry)

        self.info = client.Info("ai_build", "构建信息", registry=registry)
        self.info.info({"service": service})

        self.requests = counter(
            "ai_requests_total", "接口调用总数", ("endpoint", "method", "status")
        )
        self.request_duration = histogram(
            "ai_request_duration_seconds", "接口耗时", ("endpoint",), REQUEST_BUCKETS
        )
        self.first_token = histogram(
            "ai_chat_first_token_seconds",
            "首 token 延迟",
            ("model", "use_rag"),
            FIRST_TOKEN_BUCKETS,
        )
        self.canceled = counter("ai_chat_canceled_total", "对话中断计数", ("reason",))
        self.llm_tokens = counter("ai_llm_tokens_total", "token 用量", ("model", "type"))
        self.llm_errors = counter("ai_llm_errors_total", "上游错误", ("model", "code"))
        self.rag_retrieve = histogram(
            "ai_rag_retrieve_seconds", "向量召回耗时", ("kb_count",), RETRIEVAL_BUCKETS
        )
        self.rag_rerank = histogram(
            "ai_rag_rerank_seconds", "重排耗时", ("device", "skipped"), RETRIEVAL_BUCKETS
        )
        self.rag_recalled = histogram(
            "ai_rag_recalled_total",
            "召回条数分布",
            (),
            buckets=(0, 1, 3, 5, 10, 20, 50, 100),
        )
        self.embedding_batch = histogram(
            "ai_embedding_batch_seconds", "向量化批次耗时", ("model", "cache_hit"), TASK_BUCKETS
        )
        self.agent_steps = histogram(
            "ai_agent_steps",
            "Agent 步数分布",
            ("finish_reason",),
            buckets=(0, 1, 2, 3, 5, 8, 13),
        )
        self.tool_calls = counter(
            "ai_tool_calls_total", "工具调用", ("tool_name", "source", "status")
        )
        self.task_total = counter("ai_task_total", "任务终态计数", ("type", "status"))
        self.task_duration = histogram(
            "ai_task_duration_seconds", "任务耗时", ("type",), TASK_BUCKETS
        )
        self.mcp_server_state = gauge(
            "ai_mcp_server_state", "MCP Server 状态（1=当前状态）", ("server", "state")
        )
        self.circuit_state = gauge("ai_circuit_breaker_state", "熔断状态", ("target",))
        self.model_load = histogram(
            "ai_model_load_seconds",
            "模型懒加载耗时",
            ("model",),
            buckets=(1, 5, 15, 30, 60, 120, 300),
        )
        self.degraded = counter("ai_degraded_total", "降级次数", ("reason",))
        self._mcp_servers: set[str] = set()

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """指标是否真正生效（依赖缺失或显式关闭时为 ``False``）。"""
        return self._enabled

    def observe_request(self, *, endpoint: str, method: str, status: int, seconds: float) -> None:
        """记录一次接口调用（``endpoint`` 必须是路由模板，不是原始路径）。"""
        if not self._enabled:
            return
        self.requests.labels(endpoint, method, str(status)).inc()
        self.request_duration.labels(endpoint).observe(max(seconds, 0.0))

    def observe_first_token(self, *, model: str, use_rag: bool, seconds: float) -> None:
        """记录首 token 延迟（红线指标）。"""
        if not self._enabled:
            return
        self.first_token.labels(model, "true" if use_rag else "false").observe(max(seconds, 0.0))

    def record_cancel(self, reason: str) -> None:
        """记录对话中断（``client_disconnect`` / ``timeout``）。"""
        if not self._enabled:
            return
        self.canceled.labels(reason).inc()

    def add_llm_tokens(self, *, model: str, prompt: int, completion: int) -> None:
        """累加 token 用量（计费依据）。"""
        if not self._enabled:
            return
        if prompt:
            self.llm_tokens.labels(model, "prompt").inc(prompt)
        if completion:
            self.llm_tokens.labels(model, "completion").inc(completion)

    def record_llm_error(self, *, model: str, code: str) -> None:
        """记录一次上游错误。"""
        if not self._enabled:
            return
        self.llm_errors.labels(model, code).inc()

    def observe_retrieve(self, *, kb_count: int, seconds: float) -> None:
        """记录向量召回耗时。"""
        if not self._enabled:
            return
        self.rag_retrieve.labels(str(kb_count)).observe(max(seconds, 0.0))

    def observe_rerank(self, *, device: str, skipped: bool, seconds: float) -> None:
        """记录重排耗时（``skipped=true`` 表示退化成向量分数排序）。"""
        if not self._enabled:
            return
        self.rag_rerank.labels(device, "true" if skipped else "false").observe(max(seconds, 0.0))

    def observe_recalled(self, count: int) -> None:
        """记录召回条数。"""
        if not self._enabled:
            return
        self.rag_recalled.observe(max(count, 0))

    def observe_embedding(self, *, model: str, cache_hit: bool, seconds: float) -> None:
        """记录一次向量化批次。"""
        if not self._enabled:
            return
        self.embedding_batch.labels(model, "true" if cache_hit else "false").observe(
            max(seconds, 0.0)
        )

    def observe_agent_steps(self, *, finish_reason: str, steps: int) -> None:
        """记录 Agent 步数（``finish_reason`` 取 stop / max_steps / timeout）。"""
        if not self._enabled:
            return
        self.agent_steps.labels(finish_reason).observe(max(steps, 0))

    def record_tool_call(self, *, tool_name: str, source: str, status: str) -> None:
        """记录一次工具调用。"""
        if not self._enabled:
            return
        self.tool_calls.labels(tool_name, source, status).inc()

    def record_task(self, *, task_type: str, status: str, seconds: float | None = None) -> None:
        """记录任务终态（可同时记录耗时）。"""
        if not self._enabled:
            return
        self.task_total.labels(task_type, status).inc()
        if seconds is not None:
            self.task_duration.labels(task_type).observe(max(seconds, 0.0))

    def set_mcp_server_state(self, *, server: str, state: str) -> None:
        """设置 MCP Server 状态：当前状态置 1，其余状态置 0。

        一个 ``Gauge`` 表达「枚举当前值」时必须把其它取值显式置零，
        否则上次为 1 的取值会一直留着，告警规则 ``ai_mcp_server_state == 0``
        就会永远为真。
        """
        if not self._enabled:
            return
        self._mcp_servers.add(server)
        for candidate in MCP_STATES:
            self.mcp_server_state.labels(server, candidate).set(1 if candidate == state else 0)

    def forget_mcp_server(self, server: str) -> None:
        """移除某 Server 的状态序列（配置删掉该 Server 后）。"""
        if not self._enabled:
            return
        self._mcp_servers.discard(server)
        for candidate in MCP_STATES:
            with contextlib.suppress(KeyError):  # pragma: no cover - 未记录过的组合
                self.mcp_server_state.remove(server, candidate)

    def set_circuit_state(self, *, target: str, state: str) -> None:
        """设置熔断状态（0 关闭 / 1 半开 / 2 打开）。"""
        if not self._enabled:
            return
        self.circuit_state.labels(target).set(CIRCUIT_VALUES.get(state, 0))

    def observe_model_load(self, *, model: str, seconds: float) -> None:
        """记录模型懒加载耗时（冷启动，不计入接口红线）。"""
        if not self._enabled:
            return
        self.model_load.labels(model).observe(max(seconds, 0.0))

    def record_degraded(self, reason: str) -> None:
        """记录一次降级（核心告警指标）。"""
        if not self._enabled:
            return
        self.degraded.labels(reason).inc()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def render(self) -> tuple[bytes, str]:
        """返回 ``(body, content_type)``；依赖缺失时返回说明文本。"""
        if not self._enabled:
            return (
                b"# metrics disabled: prometheus_client is not installed or METRICS_ENABLED=false\n",
                "text/plain; version=0.0.4; charset=utf-8",
            )
        body: bytes = self._render(self._registry)
        return body, "text/plain; version=0.0.4; charset=utf-8"

    def has_metric(self, name: str) -> bool:
        """该指标是否已注册（测试与自检用）。"""
        if not self._enabled:
            return False
        return any(
            sample.name == name or sample.name.startswith(f"{name}_")
            for metric in self._registry.collect()
            for sample in metric.samples
        )


# ----------------------------------------------------------------------
# 进程级当前实例
# ----------------------------------------------------------------------
#: 深层调用（LLM / RAG / 工具）无法拿到 ``app.state``，因此与日志一样用「当前实例」。
#: 同一进程建多个应用时最后一个生效 —— 与 ``setup_logging`` 的语义一致。
_active: Metrics | None = None


def configure_metrics(metrics: Metrics) -> None:
    """设置进程级当前指标实例（``create_app`` 调用）。"""
    global _active
    _active = metrics


def get_metrics() -> Metrics:
    """取当前指标实例；未配置时返回一个**共享的**空操作实例。"""
    global _active
    if _active is None:
        _active = Metrics(enabled=False)
    return _active


__all__ = [
    "CIRCUIT_VALUES",
    "FIRST_TOKEN_BUCKETS",
    "MCP_STATES",
    "Metrics",
    "configure_metrics",
    "get_metrics",
]
