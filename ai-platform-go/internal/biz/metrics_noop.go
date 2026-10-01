package biz

import "time"

// NoopMetrics 是一个什么都不做的 Metrics 实现。
//
// 存在的理由与「`Quota` 为 nil 表示未接线」一致：`Metrics` 在 biz 里是
// **可选**依赖（接口注释里写着「单测传 nil 就是不打点」），
// 但 Go 的接口类型为 nil 时调用方法是 **panic** 而不是 no-op。
//
// 这个组合很危险：埋点是「尽力而为的观测面」，而它能让整条业务路径崩掉 ——
// 一次 `Metrics.MessagePersistFailed(...)` 的加分号写错就能把
// 「落库失败但已降级」变成 500。所以构造期一律用 `OrNoop` 兜底，
// 让「没接线」与「接了空实现」在语义上等价。
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
