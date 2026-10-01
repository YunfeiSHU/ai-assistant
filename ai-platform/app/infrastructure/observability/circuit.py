"""进程内熔断器（``REQ-NFR-005``，契约见 ``docs/10`` §3.2）。

刻意**不引入额外组件**（如 pybreaker）：规则只有「连续 N 次失败 → 打开 M 秒 → 半开试探」，
进程内计数器足够，而且更可控 —— 时间源可注入，测试不必 sleep。

``closed`` 连续 ``failure_threshold`` 次失败 → ``open``；``recovery_seconds`` 后 → ``half_open``；
半开连续 ``half_open_successes`` 次成功 → ``closed``；半开期间任何一次失败 → ``open``（并重置计时）。

半开只放行 **1 个** 调用（``half_open_max_calls``）：放行多个就等于取消熔断，上游还没恢复时会
把刚积累的缓冲瞬间打满。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final, Literal

from app.core.logging import get_logger
from app.infrastructure.observability.metrics import get_metrics

logger = get_logger("app.circuit")

CircuitState = Literal["closed", "open", "half_open"]

#: 各目标的默认规则（docs/10 §3.2 的表）
DEFAULT_RULES: Final[dict[str, tuple[int, float]]] = {
    # target: (连续失败阈值, 打开时长秒)
    "llm": (5, 30.0),
    "milvus": (10, 30.0),
    "redis": (10, 30.0),
    "embedding": (3, 60.0),
    "rerank": (3, 60.0),
    "mcp": (3, 30.0),
}


class CircuitOpenError(RuntimeError):
    """熔断已打开：本次调用被**本地**拒绝，没有打到上游。"""

    def __init__(self, target: str, retry_after: float) -> None:
        super().__init__(f"熔断已打开：{target}（{retry_after:.0f}s 后重试）")
        self.target = target
        self.retry_after = retry_after


class CircuitBreaker:
    """单个目标的熔断器。"""

    def __init__(
        self,
        target: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold 必须为正数")
        if recovery_seconds <= 0:
            raise ValueError("recovery_seconds 必须为正数")
        self.target = target
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """当前状态（读取时会推进 open → half_open 的时间判断）。"""
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._recovery_seconds:
            return "half_open"
        return "open"

    @property
    def failure_count(self) -> int:
        """连续失败次数。"""
        return self._failures

    def retry_after(self) -> float:
        """距离允许试探还剩多少秒（非 open 状态为 0）。"""
        if self._opened_at is None:
            return 0.0
        remaining = self._recovery_seconds - (self._clock() - self._opened_at)
        return max(remaining, 0.0)

    def allow(self) -> bool:
        """本次调用是否放行（``False`` 表示应直接降级，不打上游）。

        半开时**顺带认领试探额度**：只有 1 个调用能通过，其余继续降级。额度在
        :meth:`record_success` / :meth:`record_failure` 里自动归还，调用方不需要成对调用
        「进入 / 退出」—— 少一个必须记住的约定。
        """
        state = self.state
        if state == "closed":
            return True
        if state == "open":
            return False
        if self._half_open_inflight >= self._half_open_max_calls:
            return False
        self._half_open_inflight += 1
        return True

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        """记录一次成功：半开下任意一次成功即关闭。"""
        was_open = self._opened_at is not None
        self._failures = 0
        self._half_open_inflight = 0
        if was_open:
            self._close()
        else:
            self._sync_metric()

    def record_failure(self) -> None:
        """记录一次失败：半开时立即回到 open，闭合时累加到阈值。"""
        self._half_open_inflight = 0
        if self._opened_at is not None:
            self._open()
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open()
        else:
            self._sync_metric()

    def reset(self) -> None:
        """强制复位（配置重载 / 手工恢复）。"""
        self._close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _open(self) -> None:
        self._opened_at = self._clock()
        self._half_open_inflight = 0
        logger.warning(
            "circuit.opened",
            extra={
                "target": self.target,
                "failures": self._failures,
                "recovery_seconds": self._recovery_seconds,
            },
        )
        self._sync_metric()

    def _close(self) -> None:
        was_open = self._opened_at is not None
        self._failures = 0
        self._opened_at = None
        self._half_open_inflight = 0
        if was_open:
            logger.info("circuit.closed", extra={"target": self.target})
        self._sync_metric()

    def _sync_metric(self) -> None:
        """把状态推到指标（``ai_circuit_breaker_state``）。"""
        get_metrics().set_circuit_state(target=self.target, state=self.state)


class CircuitRegistry:
    """按目标名惰性创建并缓存熔断器。"""

    def __init__(
        self,
        *,
        rules: dict[str, tuple[int, float]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rules = rules if rules is not None else DEFAULT_RULES
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, target: str) -> CircuitBreaker:
        """取（或创建）某目标的熔断器。

        规则查找顺序：**精确匹配 → 前缀族匹配（``mcp:filesystem`` → ``mcp``）→ 默认阈值**。
        前缀族是必要的：MCP 的熔断器必须**按 Server 独立**（一个 Server 挂了不该让其它 Server
        一起降级），但它们应当共用 ``docs/10`` §3.2 里 ``mcp`` 那一行的阈值。
        """
        existing = self._breakers.get(target)
        if existing is not None:
            return existing
        family = target.partition(":")[0]
        threshold, recovery = self._rules.get(target) or self._rules.get(family, (5, 30.0))
        breaker = CircuitBreaker(
            target,
            failure_threshold=threshold,
            recovery_seconds=recovery,
            clock=self._clock,
        )
        self._breakers[target] = breaker
        return breaker

    def snapshot(self) -> dict[str, str]:
        """``{target: state}``（``/health`` 与排障用）。"""
        return {name: breaker.state for name, breaker in self._breakers.items()}

    def reset_all(self) -> None:
        """复位全部熔断器（测试与手工恢复）。"""
        for breaker in self._breakers.values():
            breaker.reset()


__all__ = [
    "DEFAULT_RULES",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitRegistry",
    "CircuitState",
]
