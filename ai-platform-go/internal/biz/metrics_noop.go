package biz

import "time"

// NoopMetrics 是空实现的 Metrics。
//
// Metrics 在 biz 里是可选依赖（单测可传 nil），但 Go 的 nil 接口调用方法是 panic
// 而不是 no-op —— 埋点这种「尽力而为的观测面」不该让业务路径崩掉。
// 所以构造期一律用 OrNoop 兜底，让「没接线」与「接了空实现」等价。
type NoopMetrics struct{}

var _ Metrics = NoopMetrics{}

func (NoopMetrics) QuotaExceeded(string)                    {}
func (NoopMetrics) RateLimited(string)                      {}
func (NoopMetrics) AuthFailure(string)                      {}
func (NoopMetrics) LoginAttempt(string)                     {}
func (NoopMetrics) TokenRefresh(string)                     {}
func (NoopMetrics) AIRequest(string, string, time.Duration) {}
func (NoopMetrics) AIFirstToken(bool, time.Duration)        {}
func (NoopMetrics) AIError(string, int)                     {}
func (NoopMetrics) CircuitBreakerState(string, int)         {}
func (NoopMetrics) SSEConnectionOpened()                    {}
func (NoopMetrics) SSEConnectionClosed()                    {}
func (NoopMetrics) SSEClientDisconnect(string)              {}
func (NoopMetrics) MessagePersistFailed(string)             {}
func (NoopMetrics) SessionMismatch()                        {}
func (NoopMetrics) TraceMismatch()                          {}
func (NoopMetrics) SetOrphanRows(string, int64)             {}

// OrNoop 返回 m；m 为 nil 时返回安全的空实现。
func OrNoop(m Metrics) Metrics {
	if m == nil {
		return NoopMetrics{}
	}
	return m
}
