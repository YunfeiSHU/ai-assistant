// Package metricsx 是网关全部 Prometheus 指标的唯一定义处（docs/06-§5.2），
// 前缀统一 `gw_`，与 ai-platform 的 `ai_` 并列展示。
//
// 单独成包（与 `logx` / `ssex` / `httpx` 同类）：指标名、标签名与分桶是跨系统契约 ——
// 告警规则、Grafana 面板、Runbook 都按这些字符串写，散在各调用点意味着「改一个标签名
// 要全仓搜」，而漏改的那处不报错，只会让面板上少一条曲线（静默失效）。
//
// 三条设计约束：
//
//  1. 所有方法都接受 nil 接收者（`if m == nil { return }`）。于是「指标没启用」= 传
//     `nil`，调用点不需要写任何 if，测试用 `var m *metricsx.Metrics` 就是全空实现。
//  2. 标签值一律走 `label()` / `routeLabel()` 归一化。这不是防御性编程：Prometheus
//     的标签值是索引键，一个把 user_id 传进来的手误会让序列数按用户数增长
//     （docs/06-§5.2 禁止高基数标签）。路由模板单独用更宽松的 `routeLabel()` ——
//     同一个 32 字节上限会静默合并 `/:conversation_id` 与
//     `/:conversation_id/messages` 两条序列（实测前者 1ms、后者数秒，合并后不可读）。
//  3. 不在本包做任何业务判断：只暴露「记一笔」的方法。
package metricsx

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// durationBuckets 是耗时直方图的分桶（docs/06-§5.2 给定的一组值）。
// 从 5ms 起：网关自身的开销就在这个量级，桶再粗会把「正常」与「慢一倍」归到同一个桶里。
var durationBuckets = []float64{0.005, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30}

// maxLabelLen 是标签值长度上限。
// 标签值的语义是「有限枚举」，超长的只可能是误传（真实路径、ID）；
// 截断比丢掉好：至少能在面板上看出「有人传错了」。
const maxLabelLen = 32

// maxRouteLabelLen 是路由模板的长度上限，单独一个更大的值。
// 取 160：真实路由模板最长约 90 字符，留一倍余量；再长就说明调用方传了真实路径
// 而不是模板 —— 那种情况靠 `trimLabel` 的指纹后缀避免误合，也靠这里控住基数。
const maxRouteLabelLen = 160

// Metrics 持有全部指标。零值不可用，必须经 New 构造；
// 但 `*Metrics` 的 nil 是合法值（表示「禁用」）。
type Metrics struct {
	reg *prometheus.Registry

	requests        *prometheus.CounterVec
	requestDuration *prometheus.HistogramVec
	authFailures    *prometheus.CounterVec
	loginAttempts   *prometheus.CounterVec
	tokenRefresh    *prometheus.CounterVec
	quotaExceeded   *prometheus.CounterVec
	rateLimited     *prometheus.CounterVec

	aiRequests        *prometheus.CounterVec
	aiRequestDuration *prometheus.HistogramVec
	aiFirstToken      *prometheus.HistogramVec
	aiErrors          *prometheus.CounterVec
	cbState           *prometheus.GaugeVec

	sseConnections  prometheus.Gauge
	sseDisconnects  *prometheus.CounterVec
	persistFailed   *prometheus.CounterVec
	sessionMismatch prometheus.Counter
	traceMismatch   prometheus.Counter
	orphanRows      *prometheus.GaugeVec
	buildInfo       *prometheus.GaugeVec
}

// New 构造指标集并注册到私有 registry。
// 刻意不用 prometheus 的默认全局 registry：它会让任何第三方库（含间接依赖）注册的
// 指标一起出现在 /metrics 上，而「板上多了一堆没见过的曲线」的排查成本远高于
// 自己维护一个 registry。
func New() *Metrics {
	reg := prometheus.NewRegistry()
	m := &Metrics{
		reg: reg,
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_requests_total",
			Help: "HTTP 请求总数（route 为路由模板，不含真实 ID）",
		}, []string{"route", "method", "status"}),
		requestDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "gw_request_duration_seconds",
			Help:    "HTTP 请求耗时（含网关自身与上游耗时）",
			Buckets: durationBuckets,
		}, []string{"route"}),
		authFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_auth_failures_total",
			Help: "鉴权失败次数（按原因分布）",
		}, []string{"reason"}),
		loginAttempts: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_login_attempts_total",
			Help: "登录尝试次数",
		}, []string{"result"}),
		tokenRefresh: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_token_refresh_total",
			Help: "令牌刷新次数",
		}, []string{"result"}),
		quotaExceeded: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_quota_exceeded_total",
			Help: "配额拦截次数",
		}, []string{"metric"}),
		rateLimited: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_rate_limited_total",
			Help: "限流拦截次数（按维度）",
		}, []string{"scope"}),

		aiRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_ai_requests_total",
			Help: "对 ai-platform 的调用次数",
		}, []string{"operation", "result"}),
		aiRequestDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "gw_ai_request_duration_seconds",
			Help:    "对 ai-platform 的调用耗时",
			Buckets: durationBuckets,
		}, []string{"operation"}),
		aiFirstToken: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "gw_ai_first_token_seconds",
			Help:    "流式首 token 耗时（含 AI 侧）",
			Buckets: durationBuckets,
		}, []string{"use_rag"}),
		aiErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_ai_errors_total",
			Help: "AI 错误码分布",
		}, []string{"code", "upstream_status"}),
		cbState: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "gw_circuit_breaker_state",
			Help: "熔断状态：0 关闭 / 1 半开 / 2 打开",
		}, []string{"target"}),

		sseConnections: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "gw_sse_connections",
			Help: "当前活跃 SSE 连接数",
		}),
		sseDisconnects: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_sse_client_disconnects_total",
			Help: "SSE 客户端断连分布",
		}, []string{"phase"}),
		persistFailed: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "gw_message_persist_failed_total",
			Help: "消息落库失败次数（必须有告警）",
		}, []string{"reason"}),
		sessionMismatch: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "gw_session_mismatch_total",
			Help: "AI 返回的 conversation_id 与网关不一致（接缝 J4 监控）",
		}),
		traceMismatch: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "gw_trace_mismatch_total",
			Help: "AI 返回的 trace_id 与网关不一致（接缝 J3 监控）",
		}),
		orphanRows: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "gw_orphan_rows_total",
			Help: "每日一致性检查发现的孤儿行数",
		}, []string{"table"}),
		buildInfo: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "gw_build_info",
			Help: "版本信息（值恒 1）",
		}, []string{"version", "commit", "go_version"}),
	}

	reg.MustRegister(
		m.requests, m.requestDuration, m.authFailures, m.loginAttempts,
		m.tokenRefresh, m.quotaExceeded, m.rateLimited,
		m.aiRequests, m.aiRequestDuration, m.aiFirstToken, m.aiErrors, m.cbState,
		m.sseConnections, m.sseDisconnects, m.persistFailed,
		m.sessionMismatch, m.traceMismatch, m.orphanRows, m.buildInfo,
		// Go 运行时与进程指标：它们便宜且标准，排障时（GC 停顿、goroutine 泄漏、
		// 文件描述符耗尽）往往是唯一线索 —— 而这些都不在 docs/06 的清单里，
		// 却正好是「网关内存异常增长」这类现象唯一能看到的地方。
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	m.initLabels()
	return m
}

// initLabels 把标签域有限的指标先「摸」一遍，让它们在第一次抓取时就存在 ——
// 否则是静默失效，不是「少一条曲线」：
//
//	Prometheus 的 `*Vec` 直到某个标签组合被使用过才会产出样本 ——
//	`NewCounterVec(...)` + `MustRegister` 之后，一个从未 `WithLabelValues` 过的
//	Vec 在 `/metrics` 里**完全不存在**。而 `docs/06-§5.5` 的告警规则是按名字写的
//	（`rate(gw_message_persist_failed_total[5m]) > 0`），规则表达式里的指标不存在时
//	Prometheus **不报错、只当作空结果** —— 于是「落库失败」这条 P1 告警
//	永远不会触发（它恰好是「一次都不该发生、一旦发生必须立刻知道」的那类）。
//	预置 0 值样本等于把「没发生」与「指标没注册」在现象上区分开：
//	0 = 采集通了且确实没发生；查不到 = 配置/注册错了。
//
// 只预置有限枚举：路由（`route`）与上游错误码（`code`）是开放域，给它们编造
// route×method×status 组合会让零值序列数失控；那类指标「有流量才出现」是可接受的。
func (m *Metrics) initLabels() {
	// 鉴权失败原因（docs/06-§5.1 的 auth.verify span 属性 + middleware 的
	// `WithDetail("reason", ...)` 三处早期 401）。
	for _, r := range []string{
		"missing_authorization", "invalid_authorization_scheme", "empty_token",
		"token_expired", "unauthenticated", "token_version", "other",
	} {
		m.authFailures.WithLabelValues(r)
	}

	// 登录结果（docs/06-§5.2 明确枚举）。
	for _, r := range []string{"ok", "bad_credentials", "rate_limited", "locked"} {
		m.loginAttempts.WithLabelValues(r)
	}

	// 刷新结果。
	for _, r := range []string{"ok", "invalid"} {
		m.tokenRefresh.WithLabelValues(r)
	}

	// 配额维度（与 biz 的 Metric* 常量一一对应）。
	for _, k := range []string{
		"chat_requests", "llm_tokens", "kb_count",
		"documents_count", "storage_bytes", "concurrency",
	} {
		m.quotaExceeded.WithLabelValues(k)
	}

	// 限流维度（与 biz 的 RateScope* 常量一一对应）。
	for _, s := range []string{
		"login_ip", "login_account", "message",
		"upload", "conversation", "global_qps", "concurrency",
	} {
		m.rateLimited.WithLabelValues(s)
	}

	// AI 调用：两种操作 × 四种结果。
	for _, op := range []string{"chat", "chat_stream"} {
		m.aiRequestDuration.WithLabelValues(op)
		for _, res := range []string{"ok", "error", "timeout", "rejected"} {
			m.aiRequests.WithLabelValues(op, res)
		}
	}

	// 首 token：两种 RAG 取值。
	for _, rag := range []string{"false", "true"} {
		m.aiFirstToken.WithLabelValues(rag)
	}

	// 熔断目标（docs/06-§5.5 的规则按 target="ai-platform" 写）。
	m.cbState.WithLabelValues("ai-platform")

	// SSE 断连阶段：只预置实现真的会产出的两个值 ——
	// docs/06-§5.2 还列了 `after_done`，但正常关闭不计入断连（见 docs/08-§9.3），
	// 预置它会造出一条永远为 0、看起来像「正常关闭从未发生」的假序列。
	for _, p := range []string{"before_first_token", "mid_stream"} {
		m.sseDisconnects.WithLabelValues(p)
	}

	// 落库失败原因：`assistant_insert` 是调用点写死的，
	// `persist_failed` 是 `persistErrorReason` 在拿不到错误码时的兜底值。
	for _, r := range []string{"assistant_insert", "persist_failed"} {
		m.persistFailed.WithLabelValues(r)
	}

	// 一致性检查的表名（当前只查 message）。
	m.orphanRows.WithLabelValues("message")

	// 上游错误码 × `upstream_status="other"`。
	//
	// `code` 是开放域（任何错误码都可能出现），但**网关自己**能产出的那几种是有限的，
	// 而它们恰好是排障时最先要看的（「是上游挂了还是网关自己错了」）。
	// 这里只预置这几种，不穷举：它挡的是「第一次出现错误码 X 时，
	// `rate(gw_ai_errors_total{code="X"}[5m])` 因为序列刚诞生而没有基线、
	// 算出来的 rate 恰好是 0」这一类漏报。
	for _, code := range []string{
		"AI_UNAVAILABLE", "UPSTREAM_TIMEOUT", "DEPENDENCY_UNAVAILABLE",
		"UPSTREAM_LLM_ERROR", "RATE_LIMITED", "INTERNAL_ERROR",
	} {
		m.aiErrors.WithLabelValues(code, "other")
	}

	// 版本信息：先给一个占位三元组，`SetBuildInfo` 会把它换掉。
	// 不预置的话，`gw_build_info` 会一直缺席到 `SetBuildInfo` 被调用 ——
	// 而「构建信息面板空白」很容易被当成采集坏了。
	m.buildInfo.WithLabelValues(defaultBuildVersion, defaultBuildCommit, defaultBuildGo).Set(1)
}

// 占位构建信息：`New()` 先写上，`SetBuildInfo` 再按需替换。
const (
	defaultBuildVersion = "dev"
	defaultBuildCommit  = "unknown"
	defaultBuildGo      = "unknown"
)

// WarmRoutes 按路由表预置耗时直方图。
//
// `route` 标签在这个指标上是有限域：取值就是 gin 注册表里的模板串，一个进程内不会变。
// 预置的价值是让面板上「从未被访问过的路由」显示 0 而不是 `No data` —— 后者与
// 「这个路由根本没注册上」在现象上完全一样。
//
// 刻意不预置 `gw_requests_total`：它还要 `method` 与 `status` 两个标签，编造组合会造出
// 成片的假序列（真实流量到来时它们自己就会出现）。
func (m *Metrics) WarmRoutes(routes []string) {
	if m == nil {
		return
	}
	for _, r := range routes {
		if r == "" {
			continue
		}
		m.requestDuration.WithLabelValues(routeLabel(r))
	}
}

// Handler 返回 /metrics 的 HTTP handler。
func (m *Metrics) Handler() http.Handler {
	if m == nil || m.reg == nil {
		// 禁用指标时给一个空 registry，而不是 nil handler：
		// 让「端口开着但没数据」与「端口没开」在现象上可区分。
		return promhttp.HandlerFor(prometheus.NewRegistry(), promhttp.HandlerOpts{})
	}
	return promhttp.HandlerFor(m.reg, promhttp.HandlerOpts{
		// 采集失败时也返回已采集到的部分：一个坏掉的 collector 不该让
		// 整块面板变空（那会把「某个指标坏了」升级成「监控全瞎」）。
		ErrorHandling: promhttp.ContinueOnError,
	})
}

// Registry 暴露内部 registry（单测用来抓取指标文本）。
func (m *Metrics) Registry() *prometheus.Registry {
	if m == nil {
		return nil
	}
	return m.reg
}

// ---- 记录方法 ----

// ObserveRequest 记录一次 HTTP 请求。
//
// `status` 传整型而不是字符串：调用方（中间件）手上就是 int，
// 让上层做格式化会把「HTTP 状态码」这种显然的约定复制到多处。
func (m *Metrics) ObserveRequest(route, method string, status int, d time.Duration) {
	if m == nil {
		return
	}
	route = routeLabel(route)
	m.requests.WithLabelValues(route, label(method), statusLabel(status)).Inc()
	m.requestDuration.WithLabelValues(route).Observe(d.Seconds())
}

// AuthFailure 记录一次鉴权失败。
func (m *Metrics) AuthFailure(reason string) {
	if m == nil {
		return
	}
	m.authFailures.WithLabelValues(label(reason)).Inc()
}

// LoginAttempt 记录一次登录尝试。
func (m *Metrics) LoginAttempt(result string) {
	if m == nil {
		return
	}
	m.loginAttempts.WithLabelValues(label(result)).Inc()
}

// TokenRefresh 记录一次令牌刷新。
func (m *Metrics) TokenRefresh(result string) {
	if m == nil {
		return
	}
	m.tokenRefresh.WithLabelValues(label(result)).Inc()
}

// QuotaExceeded 记录一次配额拦截。
func (m *Metrics) QuotaExceeded(metric string) {
	if m == nil {
		return
	}
	m.quotaExceeded.WithLabelValues(label(metric)).Inc()
}

// RateLimited 记录一次限流拦截。
func (m *Metrics) RateLimited(scope string) {
	if m == nil {
		return
	}
	m.rateLimited.WithLabelValues(label(scope)).Inc()
}

// AIRequest 记录一次对 AI 的调用（结果与耗时）。
// `result` 取值固定为 ok / error（见本包常量），具体错误码走 `AIError` ——
// 两件事分开是因为前者是面板上的成功率，后者是排障时的分布，
// 混在一起会让成功率被码表撑成几十条曲线。
func (m *Metrics) AIRequest(operation, result string, d time.Duration) {
	if m == nil {
		return
	}
	op := label(operation)
	m.aiRequests.WithLabelValues(op, label(result)).Inc()
	m.aiRequestDuration.WithLabelValues(op).Observe(d.Seconds())
}

// AIFirstToken 记录流式首 token 耗时（含 AI 侧，docs/06 的告警规则按 use_rag 分组）。
func (m *Metrics) AIFirstToken(useRAG bool, d time.Duration) {
	if m == nil {
		return
	}
	m.aiFirstToken.WithLabelValues(boolLabel(useRAG)).Observe(d.Seconds())
}

// AIError 记录一次 AI 错误。
// `upstreamStatus` 只在 100..599 之间原样记，其余归 `other`：上游可能回一个非 HTTP 的
// 整数（协议错乱、`0`），而每个异常值都会新开一条时间序列。
func (m *Metrics) AIError(code string, upstreamStatus int) {
	if m == nil {
		return
	}
	m.aiErrors.WithLabelValues(label(code), statusLabel(upstreamStatus)).Inc()
}

// CircuitBreakerState 设置熔断状态（0/1/2）。
func (m *Metrics) CircuitBreakerState(target string, state int) {
	if m == nil {
		return
	}
	m.cbState.WithLabelValues(label(target)).Set(float64(state))
}

// SSEConnectionOpened 增加一个活跃 SSE 连接。
func (m *Metrics) SSEConnectionOpened() { m.sseConnectionDelta(1) }

// SSEConnectionClosed 减少一个活跃 SSE 连接。
func (m *Metrics) SSEConnectionClosed() { m.sseConnectionDelta(-1) }

func (m *Metrics) sseConnectionDelta(delta float64) {
	if m == nil {
		return
	}
	m.sseConnections.Add(delta)
}

// SSEClientDisconnect 记录一次客户端断连（按阶段）。
func (m *Metrics) SSEClientDisconnect(phase string) {
	if m == nil {
		return
	}
	m.sseDisconnects.WithLabelValues(label(phase)).Inc()
}

// MessagePersistFailed 记录一次落库失败。
func (m *Metrics) MessagePersistFailed(reason string) {
	if m == nil {
		return
	}
	m.persistFailed.WithLabelValues(label(reason)).Inc()
}

// SessionMismatch 记录一次「AI 回显的会话 ID 与网关不一致」（接缝 J4）。
func (m *Metrics) SessionMismatch() {
	if m == nil {
		return
	}
	m.sessionMismatch.Inc()
}

// TraceMismatch 记录一次「AI 回显的 trace_id 与网关不一致」（接缝 J3）。
func (m *Metrics) TraceMismatch() {
	if m == nil {
		return
	}
	m.traceMismatch.Inc()
}

// SetOrphanRows 设置某表的孤儿行数（每日一致性检查输出）。
func (m *Metrics) SetOrphanRows(table string, n int64) {
	if m == nil {
		return
	}
	m.orphanRows.WithLabelValues(label(table)).Set(float64(n))
}

// SetBuildInfo 设置版本信息（值恒 1）。
// 一次性调用；随后被 Prometheus 按 scrape 反复读取 —— 因此它是一条不随版本变化的
// 时间序列，正是 `up{job=...}` 与「现在跑的是哪个 commit」这两件事的数据来源。
//
// 写完之后删掉 `New()` 留下的占位三元组：留着的话面板上会同时出现 `version="dev"`
// 与真实版本两条，而「哪个是真的」只能靠人判断。
func (m *Metrics) SetBuildInfo(version, commit, goVersion string) {
	if m == nil {
		return
	}
	version = label(version)
	commit = label(commit)
	goVersion = label(goVersion)
	m.buildInfo.WithLabelValues(version, commit, goVersion).Set(1)
	if version != defaultBuildVersion || commit != defaultBuildCommit || goVersion != defaultBuildGo {
		m.buildInfo.DeleteLabelValues(defaultBuildVersion, defaultBuildCommit, defaultBuildGo)
	}
}

// ---- 连接池 ----

// DBPoolStats 是数据库连接池快照（docs/06-§5.2 的 `gw_db_pool_*`）。
// 用「采样函数」而不是让本包持有 `*sql.DB`：metricsx 不认识任何具体基础设施
// （它只认数字），否则一个纯指标包就会变成「必须 import gorm 才能编译」。
type DBPoolStats struct {
	Open      int
	InUse     int
	Idle      int
	Max       int
	WaitTotal int64
}

// RedisPoolStats 是 Redis 连接池快照（docs/06-§5.2 的 `gw_redis_pool_*`）。
type RedisPoolStats struct {
	TotalConns int
	IdleConns  int
	StaleConns int
}

// RegisterDBPool 注册一组按需采样的数据库连接池指标。
// 用 GaugeFunc 而不是「定时 Set」：GaugeFunc 只在被抓取时读一次，因此值永远是最新的，
// 且服务空闲时没有任何后台开销；定时 Set 会在没有 scrape 时也一直跑。
func (m *Metrics) RegisterDBPool(sample func() DBPoolStats) {
	if m == nil || sample == nil {
		return
	}
	gauge := func(name, help string, pick func(DBPoolStats) float64) {
		m.reg.MustRegister(prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: name, Help: help,
		}, func() float64 { return pick(sample()) }))
	}
	gauge("gw_db_pool_open", "已建立的数据库连接数", func(s DBPoolStats) float64 { return float64(s.Open) })
	gauge("gw_db_pool_in_use", "正在使用的数据库连接数", func(s DBPoolStats) float64 { return float64(s.InUse) })
	gauge("gw_db_pool_idle", "空闲的数据库连接数", func(s DBPoolStats) float64 { return float64(s.Idle) })
	gauge("gw_db_pool_max", "数据库连接池上限", func(s DBPoolStats) float64 { return float64(s.Max) })
	gauge("gw_db_pool_wait_total", "等待数据库连接的累计次数", func(s DBPoolStats) float64 { return float64(s.WaitTotal) })
}

// RegisterRedisPool 注册一组按需采样的 Redis 连接池指标。
func (m *Metrics) RegisterRedisPool(sample func() RedisPoolStats) {
	if m == nil || sample == nil {
		return
	}
	gauge := func(name, help string, pick func(RedisPoolStats) float64) {
		m.reg.MustRegister(prometheus.NewGaugeFunc(prometheus.GaugeOpts{
			Name: name, Help: help,
		}, func() float64 { return pick(sample()) }))
	}
	gauge("gw_redis_pool_total_conns", "Redis 连接池总连接数", func(s RedisPoolStats) float64 { return float64(s.TotalConns) })
	gauge("gw_redis_pool_idle_conns", "Redis 连接池空闲连接数", func(s RedisPoolStats) float64 { return float64(s.IdleConns) })
	gauge("gw_redis_pool_stale_conns", "Redis 连接池被判定为陈旧的连接数", func(s RedisPoolStats) float64 { return float64(s.StaleConns) })
}

// ---- 指标标签的固定取值 ----

// AI 调用结果（`gw_ai_requests_total{result}`）。
const (
	// AIResultOK 表示调用成功返回（流式以正常收尾计）。
	AIResultOK = "ok"
	// AIResultError 表示调用失败（上游错误、协议错乱等）。
	AIResultError = "error"
	// AIResultTimeout 表示超时；与 error 分开是因为处置动作不同（加时/降级 vs 查上游）。
	AIResultTimeout = "timeout"
	// AIResultRejected 表示熔断打开、**没有发起**真实调用。
	//
	// 单独一档是刻意的：面板上「AI 调用失败率上升」与「根本没调上去」
	// 是完全不同的两件事，前者要查 AI，后者要查网关的熔断状态。
	AIResultRejected = "rejected"
)

// 登录结果（`gw_login_attempts_total{result}`，取值由 docs/06-§5.2 固定）。
const (
	// LoginOK 表示凭据校验通过并签发令牌对。
	LoginOK = "ok"
	// LoginBadCredentials 同时覆盖「账号不存在」与「密码错」，不区分以防用户枚举。
	LoginBadCredentials = "bad_credentials"
	// LoginRateLimited 表示登录被限流前置拦截（登录风暴的信号）。
	LoginRateLimited = "rate_limited"
	// LoginLocked 表示账号被禁用而拒绝登录。
	LoginLocked = "locked"
)

// 令牌刷新结果（`gw_token_refresh_total{result}`）。
const (
	// RefreshOK 表示轮换成功（旧令牌已作废、新令牌已签发）。
	RefreshOK = "ok"
	// RefreshInvalid 覆盖不存在/已作废/已被用过/已过期四种成因，量级异常时再翻日志细分。
	RefreshInvalid = "invalid"
)

// 熔断状态（`gw_circuit_breaker_state` 的值）。
const (
	// CircuitClosed 表示正常放行（0）。
	CircuitClosed = 0
	// CircuitHalfOpen 表示探测放行（1）：只放少量请求试探上游是否恢复。
	CircuitHalfOpen = 1
	// CircuitOpen 表示熔断打开（2）：请求被快速拒绝，不落到上游。
	CircuitOpen = 2
)

// SSE 断连阶段（`gw_sse_client_disconnects_total{phase}`）。
const (
	// SSEPhaseBeforeFirstToken 表示首 token 之前就断连，通常是上游太慢或客户端放弃等待。
	SSEPhaseBeforeFirstToken = "before_first_token"
	// SSEPhaseMidStream 表示流中途断连，用户已看到部分内容。
	SSEPhaseMidStream = "mid_stream"
	// SSEPhaseAfterDone 表示 AI 已收尾之后的断连；正常关闭不计入，留作区分异常收尾。
	SSEPhaseAfterDone = "after_done"
)

// AI 调用类型（`gw_ai_requests_total{operation}`）。
const (
	// AIOpChat 表示非流式对话调用。
	AIOpChat = "chat"
	// AIOpChatStream 表示流式对话调用。
	AIOpChatStream = "chat_stream"
	// AIOpProxy 表示透传型代理调用（不落消息）。
	AIOpProxy = "proxy"
	// AIOpUpload 表示文件上传/索引类调用。
	AIOpUpload = "upload"
)

// ---- 内部 ----

// label 归一化「枚举型」标签值（错误码、reason、result…）。
// 空值归 `unknown` 而不是留空：Prometheus 里空串是合法标签值，
// 于是「忘了传」与「传了空」在面板上无法区分。
func label(v string) string { return trimLabel(v, maxLabelLen) }

// routeLabel 归一化 `route` 标签。
//
// 路由模板不能用 maxLabelLen：真实路由模板普遍长于 32 字符，截断会让不同的路由撞成
// 同一个标签值 —— `/api/v1/conversations/:conversation_id` 与
// `/api/v1/conversations/:conversation_id/messages` 的前 32 字节完全一样，于是
// 「看会话详情」与「发消息」的 P99 被合进同一条曲线（实测差两个数量级）。
// 这种错误没有任何运行时症状：指标照样在涨，只是分不开。
func routeLabel(v string) string { return trimLabel(v, maxRouteLabelLen) }

// trimLabel 截断超长标签值，并在截断处附上内容指纹。
//
// 只截断不加指纹是不够的：两个不同的长值截断后可能**完全相等**，
// 于是「两个东西」在面板上变成一个。附上哈希后缀后，
// 相同内容仍得到相同标签（基数可控），不同内容一定不同（不会误合）。
func trimLabel(v string, max int) string {
	if v == "" {
		return "unknown"
	}
	if len(v) <= max {
		return v
	}
	sum := sha256.Sum256([]byte(v))
	return v[:max] + "~" + hex.EncodeToString(sum[:4])
}

func boolLabel(v bool) string {
	if v {
		return "true"
	}
	return "false"
}

// statusLabel 把状态码归一化成有限集合。
//
// 保留**具体数字**而不是归成 `4xx` / `5xx`：
// docs/06-§5.3 的告警规则写的是 `status=~"5.."`（具体数字与三位分组都匹配），
// 而排障时「429 涨了」与「404 涨了」是两条完全不同的线索，
// 归成 `4xx` 会把这个区别永久丢掉，且事发后无法补回。
//
// 100..599 之外（0、999、负数）归 `other` —— 那是协议异常，
// 让每个异常值新开一条时间序列没有意义。
func statusLabel(status int) string {
	if status >= 100 && status <= 599 {
		return strconv.Itoa(status)
	}
	return "other"
}
