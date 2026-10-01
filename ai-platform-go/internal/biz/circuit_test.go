package biz

import (
	"bytes"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"
)

// TestCircuitBreakerDoesNotDeadlockOnFirstTransition 是**自死锁**的回归守卫。
//
// 历史缺陷（2026-09-30 真机实测，P0）：`logf` 内部用
// `b.Snapshot().StateName()` 取状态名，而 `logf` 的三个调用点
// （熔断打开 / 进入半开 / 恢复关闭）**全都在持有 `b.mu` 的临界区里**，
// `Snapshot()` 又要 `b.mu.Lock()`。`sync.Mutex` 不可重入 →
// 熔断器第一次需要写日志时把自己锁死：
//
//   - 连续 10 次上游失败（默认阈值）→ `openLocked` → 死锁；
//   - `b.mu` 永不释放 → 之后**每一个**请求都卡在 `Allow()` 上；
//   - 网关对 AI 的调用在「最该工作的那一次」全线不可用；
//   - `gw_circuit_breaker_state` 已经是 2、`gw_ai_requests_total{result="rejected"}`
//     却是 0（拒绝路径根本没走到），**且一行日志都没有** —— 卡住的正是写日志那一步。
//
// 三个调用点都要覆盖：只测打开态的话，「半开/关闭也死锁」会被漏掉。
//
// 用「带看门狗的调用」而不是直接同步调：死锁时 go test 默认要等 10 分钟
// 才 panic，而失败信息里只会写「test timed out」，看不出是哪一步。
func TestCircuitBreakerDoesNotDeadlockOnFirstTransition(t *testing.T) {
	var buf bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelWarn}))

	// 可变时钟：用真实时间的话「冷却结束进半开」要等 30s。
	now := time.Date(2026, 9, 30, 12, 0, 0, 0, time.Local)
	var clockMu sync.Mutex
	clock := func() time.Time {
		clockMu.Lock()
		defer clockMu.Unlock()
		return now
	}
	advance := func(d time.Duration) {
		clockMu.Lock()
		defer clockMu.Unlock()
		now = now.Add(d)
	}

	const openFor = 30 * time.Second
	cb := NewAICircuitBreaker(CircuitTargetAI, 2, openFor, clock, nil, logger)

	// 1) 连续失败到阈值 → 熔断打开（原先在这里死锁）。
	watchdog(t, "第 1 次 OnFailure", func() { cb.OnFailure() })
	watchdog(t, "第 2 次 OnFailure（触发熔断打开）", func() { cb.OnFailure() })
	if got := watchdogState(t, cb); got != CircuitStateOpen {
		t.Fatalf("连续 2 次失败后应为打开（2），实际 %d", got)
	}
	// 打开期间放行判定必须**立刻**返回 false（原先卡死在这里）。
	if allowed := watchdogBool(t, "打开期间 Allow", cb.Allow); allowed {
		t.Fatal("熔断打开时 Allow 应返回 false")
	}

	// 2) 冷却结束 → 转入半开并写 `circuit.half_open` 日志（死锁点 2）。
	advance(openFor + time.Second)
	if allowed := watchdogBool(t, "冷却结束后的 Allow（进半开）", cb.Allow); !allowed {
		t.Fatal("冷却结束后第一个请求应被放行当探针")
	}
	// 半开只放一个探针：第二个必须被拒。
	if allowed := watchdogBool(t, "半开第二个 Allow", cb.Allow); allowed {
		t.Fatal("半开只应放行一个探针")
	}

	// 3) 探针成功 → 恢复关闭并写 `circuit.closed` 日志（死锁点 3）。
	watchdog(t, "探针成功后 OnSuccess", cb.OnSuccess)
	if got := watchdogState(t, cb); got != CircuitStateClosed {
		t.Fatalf("探针成功后应回到关闭（0），实际 %d", got)
	}

	out := buf.String()
	for _, want := range []string{"circuit.open", "circuit.half_open", "circuit.closed"} {
		if !strings.Contains(out, want) {
			t.Errorf("缺少 %s 日志；状态变化必须可观测（实际日志：%q）", want, out)
		}
	}
	// 状态名必须是可读名而不是数字：日志写 `state=2` 排障时看不出所以然。
	if !strings.Contains(out, "state=open") || !strings.Contains(out, "state=half_open") {
		t.Errorf("日志里的 state 应为可读名，实际：%q", out)
	}
}

// TestCircuitBreakerRejectsFastAfterOpen 断言熔断打开后**不再发起调用**。
//
// 这是 S7 的核心断言：AI 挂掉后网关不该每个请求都去撞一次连接超时
// （那会让 p99 变成「上游超时」的量级）。
func TestCircuitBreakerRejectsFastAfterOpen(t *testing.T) {
	now := time.Date(2026, 9, 30, 12, 0, 0, 0, time.Local)
	cb := NewAICircuitBreaker(CircuitTargetAI, 3, 30*time.Second, func() time.Time { return now }, nil, nil)

	for i := 0; i < 3; i++ {
		if !watchdogBool(t, "闭合期 Allow", cb.Allow) {
			t.Fatalf("第 %d 次失败前应放行", i+1)
		}
		watchdog(t, "上报失败", cb.OnFailure)
	}
	if got := watchdogState(t, cb); got != CircuitStateOpen {
		t.Fatalf("阈值达到后应打开，实际 %d", got)
	}
	// 连打 20 次：全部立刻拒绝，且不改变状态（说明没有半开的探针泄漏）。
	start := time.Now()
	for i := 0; i < 20; i++ {
		if watchdogBool(t, "打开期 Allow", cb.Allow) {
			t.Fatalf("第 %d 次仍被放行（应一路拒绝到冷却结束）", i+1)
		}
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("20 次拒绝耗时 %v：拒绝路径不应有任何阻塞/重试", elapsed)
	}
	if got := watchdogState(t, cb); got != CircuitStateOpen {
		t.Fatalf("拒绝不应改变状态，实际 %d", got)
	}
}

// TestCircuitBreakerDisabledWhenZeroConfig 断言「配置为 0」是**显式关闭**。
//
// 用 nil 表示关闭而不是「一个永远放行的对象」：让调用点只判一次 nil，
// 也避免「关掉的熔断器」在面板上留下一条永远是 0 的曲线。
func TestCircuitBreakerDisabledWhenZeroConfig(t *testing.T) {
	if cb := NewAICircuitBreaker(CircuitTargetAI, 0, 30*time.Second, nil, nil, nil); cb != nil {
		t.Error("failureThreshold=0 应返回 nil（显式关闭）")
	}
	if cb := NewAICircuitBreaker(CircuitTargetAI, 10, 0, nil, nil, nil); cb != nil {
		t.Error("openFor=0 应返回 nil（显式关闭）")
	}
	var nilCB *AICircuitBreaker
	if !nilCB.Allow() {
		t.Error("nil 熔断器必须放行（调用点不必判 nil）")
	}
	nilCB.OnFailure() // 不得 panic
	nilCB.OnSuccess()
	if got := nilCB.Snapshot().State; got != CircuitStateClosed {
		t.Errorf("nil 熔断器的快照应为关闭，实际 %d", got)
	}
}

// ---- 看门狗：把「死锁」变成「一条带步骤名的失败」----

// watchdogTimeout 是单步调用的容忍上限。
//
// 1s 足够真实实现（纯内存 + 一次日志写），而真死锁会立刻命中。
const watchdogTimeout = 2 * time.Second

func watchdog(t *testing.T, step string, fn func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		fn()
	}()
	select {
	case <-done:
	case <-time.After(watchdogTimeout):
		t.Fatalf("%s 卡住超过 %v：熔断器在临界区里做了会阻塞的事（最常见的成因是重入自己的 mu）", step, watchdogTimeout)
	}
}

func watchdogBool(t *testing.T, step string, fn func() bool) bool {
	t.Helper()
	var got bool
	watchdog(t, step, func() { got = fn() })
	return got
}

func watchdogState(t *testing.T, cb *AICircuitBreaker) int {
	t.Helper()
	var state int
	watchdog(t, "读快照", func() { state = cb.Snapshot().State })
	return state
}
