package biz

import "time"

// Metrics 是 biz 层需要打的指标（docs/06-§5.2）。
//
// 在 biz 定义接口而非直接依赖 `pkg/metricsx`：规范 §四的依赖方向是 `service/data → biz`，
// 让 biz 依赖打点包会把可观测性变成业务层的编译期依赖。实现方在 pkg、装配点在 cmd。
// 方法名与 Prometheus 指标名一一对应，便于从面板反查到打点代码。
type Metrics interface {
	// QuotaExceeded 记一次配额拦截（`gw_quota_exceeded_total{metric}`）。
	QuotaExceeded(metric string)
	// RateLimited 记一次限流拦截（`gw_rate_limited_total{scope}`）。
	RateLimited(scope string)
	// AuthFailure 记一次鉴权失败（`gw_auth_failures_total{reason}`）。
	AuthFailure(reason string)
	// LoginAttempt 记一次登录尝试（`gw_login_attempts_total{result}`）。
	LoginAttempt(result string)
	// TokenRefresh 记一次令牌刷新（`gw_token_refresh_total{result}`）。
	TokenRefresh(result string)
	// AIRequest 记一次对 AI 的调用（`gw_ai_requests_total` / `_duration_seconds`）。
	AIRequest(operation, result string, d time.Duration)
	// AIFirstToken 记流式首 token 耗时（`gw_ai_first_token_seconds{use_rag}`）。
	AIFirstToken(useRAG bool, d time.Duration)
	// AIError 记一次 AI 错误（`gw_ai_errors_total{code,upstream_status}`）。
	AIError(code string, upstreamStatus int)
	// CircuitBreakerState 设置熔断状态（`gw_circuit_breaker_state{target}`）。
	CircuitBreakerState(target string, state int)
	// SSEConnectionOpened / SSEConnectionClosed 维护 `gw_sse_connections`。
	SSEConnectionOpened()
	SSEConnectionClosed()
	// SSEClientDisconnect 记一次客户端断连（`gw_sse_client_disconnects_total{phase}`）。
	SSEClientDisconnect(phase string)
	// MessagePersistFailed 记落库失败（`gw_message_persist_failed_total{reason}`）。
	MessagePersistFailed(reason string)
	// SessionMismatch 记「AI 回显的会话 ID 与网关不一致」（接缝 J4）。
	SessionMismatch()
	// TraceMismatch 记「AI 回显的 trace_id 与本侧不一致」（接缝 J3）。
	TraceMismatch()
	// SetOrphanRows 输出孤儿行数（`gw_orphan_rows_total{table}`）。
	SetOrphanRows(table string, n int64)
}

// 指标标签的固定取值（与 `pkg/metricsx` 的常量保持同值）。
//
// 两边各留一份是不可避免的重复（biz 不能 import metricsx），
// 故用单测断言两组常量相等（metrics_test.go）—— 不一致时面板会少一条曲线且无运行时症状。
const (
	// MetricResultOK 是 AI 调用成功。
	MetricResultOK = "ok"
	// MetricResultError 是 AI 调用失败。
	MetricResultError = "error"
	// MetricResultTimeout 是 AI 调用超时（与失败分开：两者的处置不同）。
	MetricResultTimeout = "timeout"
	// MetricResultRejected 是熔断打开、**没有发起**真实调用。
	MetricResultRejected = "rejected"

	// CircuitTargetAI 是熔断器目标名（`target="ai-platform"`）。
	CircuitTargetAI = "ai-platform"

	// AIOpChat / AIOpChatStream 是 `gw_ai_requests_total{operation}` 的取值，
	// 与 `pkg/metricsx` 的同名常量一致。
	AIOpChat = "chat"
	// AIOpChatStream 是流式对话的 operation 取值（非流式见 AIOpChat）。
	AIOpChatStream = "chat_stream"

	// SSE 断连的阶段（`gw_sse_client_disconnects_total{phase}`）。
	// 只有两档：`after_done` 属于正常收尾，计成断连只会制造永远为 0 的噪音。
	SSEPhaseBeforeFirstToken = "before_first_token"
	// SSEPhaseMidStream 表示首 token 之后、AI 收尾之前断开（用户已看到部分内容）。
	SSEPhaseMidStream = "mid_stream"

	// 登录与刷新的结果（`gw_login_attempts_total{result}` / `gw_token_refresh_total{result}`）。
	// `locked` 与「限流拒绝」分开：锁是账号维度、限流是流量维度，要动的配置不同。

	// MetricLoginOK 表示凭据校验通过并签发令牌对。
	MetricLoginOK = "ok"
	// MetricLoginBadCredentials 同时覆盖「账号不存在」与「密码错」，不区分以防用户枚举。
	MetricLoginBadCredentials = "bad_credentials"
	// MetricLoginRateLimited 表示登录请求被限流前置拦截（与账号锁定不是一回事）。
	MetricLoginRateLimited = "rate_limited"
	// MetricLoginLocked 表示账号被禁用而拒绝登录。
	MetricLoginLocked = "locked"

	// MetricRefreshOK 表示刷新成功（旧令牌已作废、新令牌已签发）。
	MetricRefreshOK = "ok"
	// MetricRefreshInvalid 覆盖刷新令牌不存在/已作废/已被用过/已过期四种成因。
	MetricRefreshInvalid = "invalid"
)
