"""熔断器单测（``REQ-NFR-005``，契约见 ``docs/10`` §3.2）。

**时间源可注入**是本模块唯一需要的测试技巧：状态机里有「打开 M 秒后进半开」
这一条，靠 ``sleep`` 测它既慢又偶发失败。注入一个手动推进的假时钟，
所有时序断言都变成确定性的。

覆盖的判定点：

* 连续 N 次失败才打开（第 N-1 次不打开）；
* 打开期间 ``allow()`` 一律拒绝，且 ``retry_after()`` 单调递减；
* 到期后进半开，且**只放行 1 个**试探（放行多个等于取消熔断）；
* 半开失败立刻回到 open 并重置计时；
* 半开成功即闭合、失败计数归零；
* 规则查找：精确匹配 → 前缀族（``mcp:filesystem`` → ``mcp``）→ 默认阈值。
"""

from __future__ import annotations

import pytest

from app.infrastructure.observability.circuit import (
    DEFAULT_RULES,
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
)


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    """默认假时钟。"""
    return FakeClock()


def test_threshold_opens_only_on_the_nth_failure(clock: FakeClock) -> None:
    """连续失败到阈值才打开：第 N-1 次仍是 closed。"""
    breaker = CircuitBreaker("llm", failure_threshold=3, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"

    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.failure_count == 3


def test_success_resets_failure_streak(clock: FakeClock) -> None:
    """成功会把连续失败计数清零 —— 否则「偶发失败」会累积成误熔断。"""
    breaker = CircuitBreaker("llm", failure_threshold=3, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count == 0

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_open_state_blocks_and_reports_retry_after(clock: FakeClock) -> None:
    """打开期间一律拒绝，``retry_after`` 随剩余时间递减。"""
    breaker = CircuitBreaker("llm", failure_threshold=1, recovery_seconds=30.0, clock=clock)
    breaker.record_failure()

    assert not breaker.allow()
    assert breaker.retry_after() == pytest.approx(30.0)

    clock.advance(10.0)
    assert not breaker.allow()
    assert breaker.retry_after() == pytest.approx(20.0)


def test_recovery_window_moves_to_half_open_and_allows_one(clock: FakeClock) -> None:
    """到期进半开，且只放行一个试探调用。"""
    breaker = CircuitBreaker("llm", failure_threshold=1, recovery_seconds=30.0, clock=clock)
    breaker.record_failure()

    clock.advance(30.0)
    assert breaker.state == "half_open"
    assert breaker.allow() is True
    # 第二个并发调用必须被拒，否则「试探」等于没有
    assert breaker.allow() is False


def test_half_open_failure_reopens_and_resets_timer(clock: FakeClock) -> None:
    """半开试探失败 → 立刻回到 open，并且倒计时从此刻重新算。"""
    breaker = CircuitBreaker("llm", failure_threshold=1, recovery_seconds=30.0, clock=clock)
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.allow()

    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.retry_after() == pytest.approx(30.0)
    clock.advance(29.0)
    assert not breaker.allow()


def test_half_open_success_closes_and_clears_failures(clock: FakeClock) -> None:
    """半开成功 → 闭合，失败计数清零，试探额度归还。"""
    breaker = CircuitBreaker("llm", failure_threshold=1, recovery_seconds=30.0, clock=clock)
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.allow()

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.failure_count == 0
    assert breaker.retry_after() == 0.0
    assert breaker.allow() is True


def test_reset_forces_closed(clock: FakeClock) -> None:
    """``reset()`` 用于配置重载/手工恢复：任何状态下都能直接闭合。"""
    breaker = CircuitBreaker("llm", failure_threshold=1, clock=clock)
    breaker.record_failure()

    breaker.reset()

    assert breaker.state == "closed"
    assert breaker.allow() is True


def test_invalid_arguments_are_rejected() -> None:
    """阈值与恢复时长必须为正：0 或负值会让状态机永远停在某个状态。"""
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker("llm", failure_threshold=0)
    with pytest.raises(ValueError, match="recovery_seconds"):
        CircuitBreaker("llm", recovery_seconds=0)


def test_circuit_open_error_carries_target_and_delay() -> None:
    """异常里带目标名与剩余秒数 —— 上层要据此填 ``Retry-After`` 并写日志。"""
    error = CircuitOpenError("mcp:filesystem", 12.5)

    assert error.target == "mcp:filesystem"
    assert error.retry_after == pytest.approx(12.5)
    assert isinstance(error, RuntimeError)


def test_registry_reuses_breaker_per_target(clock: FakeClock) -> None:
    """同一目标只建一个熔断器（否则每次取都是新的，永远熔不断）。"""
    registry = CircuitRegistry(clock=clock)

    first = registry.get("llm")
    second = registry.get("llm")

    assert first is second


def test_registry_uses_exact_rule_when_declared(clock: FakeClock) -> None:
    """精确匹配的规则优先于前缀族。"""
    registry = CircuitRegistry(
        rules={"llm": (2, 5.0), "mcp": (9, 90.0), "mcp:filesystem": (1, 1.0)},
        clock=clock,
    )

    breaker = registry.get("mcp:filesystem")
    breaker.record_failure()
    assert breaker.state == "open"

    # 走了 mcp:filesystem 的 1 秒恢复窗口，而不是 mcp 的 90 秒
    clock.advance(1.0)
    assert breaker.allow() is True


def test_registry_falls_back_to_family_prefix(clock: FakeClock) -> None:
    """``mcp:filesystem`` 未单独声明时，借用 ``mcp`` 这一族的阈值。

    MCP 的熔断器必须**按 Server 独立**（一个 Server 挂了不该连累其它 Server），
    但阈值应当共用文档里的那一行 —— 前缀族就是这两条要求的交点。
    """
    registry = CircuitRegistry(clock=clock)

    first = registry.get("mcp:filesystem")
    second = registry.get("mcp:git")
    assert first is not second

    threshold = DEFAULT_RULES["mcp"][0]
    for _ in range(threshold):
        first.record_failure()
    assert first.state == "open"
    # 另一个 Server 完全不受影响
    assert second.state == "closed"


def test_registry_defaults_for_unknown_target(clock: FakeClock) -> None:
    """没有规则也没前缀族时用默认阈值（5 次 / 30 秒）。"""
    registry = CircuitRegistry(rules={}, clock=clock)

    breaker = registry.get("mystery")
    for _ in range(5):
        breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.retry_after() == pytest.approx(30.0)


def test_registry_snapshot_and_reset_all(clock: FakeClock) -> None:
    """``snapshot`` 给 ``/health`` 用；``reset_all`` 给测试与手工恢复用。"""
    registry = CircuitRegistry(clock=clock)
    registry.get("llm").record_failure()

    assert registry.snapshot() == {"llm": "closed"}

    breaker = registry.get("mcp:git")
    for _ in range(DEFAULT_RULES["mcp"][0]):
        breaker.record_failure()
    assert registry.snapshot()["mcp:git"] == "open"

    registry.reset_all()

    assert registry.snapshot() == {"llm": "closed", "mcp:git": "closed"}
