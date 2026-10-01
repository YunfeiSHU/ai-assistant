// Package conf 定义配置结构与加载规则。
//
// 权威定义：docs/06-§7 配置项全表。
//
// 加载规则（12-Factor，REQ-NFR-009）：
//  1. 先读 `.env`（仅当存在，不覆盖真实环境变量）；
//  2. 再读进程环境变量；
//  3. 都没有则用内置默认值。
//
// 这样「本地开发写 .env、容器里注入环境变量」是同一套代码路径。
package conf

import "time"

// Config 是全部配置的根。
type Config struct {
	App      App
	MySQL    MySQL
	Redis    Redis
	Auth     Auth
	Quota    Quota
	Rate     Rate
	AI       AI
	Observ   Observ
	Internal Internal
}

// App 是应用与 HTTP 层配置。
type App struct {
	Env                     string
	HTTPAddr                string
	APIPrefix               string
	Debug                   bool
	LogLevel                string
	LogFormat               string
	GracefulShutdownSeconds int
	MaxJSONBodyMB           int
	MaxResponseMB           int
	CORSAllowedOrigins      []string
	TrustedProxyCount       int
	AutoTitleMaxChars       int
	// Version / Commit 由构建期 -ldflags 注入，用于 /health 与 gw_build_info。
	Version string
	Commit  string
}

// MySQL 是业务台账数据库配置。
type MySQL struct {
	// DSN 必须带 parseTime=true&loc=UTC —— 否则 DATETIME(3) 会被解析成
	// 本地时间，产生「差 8 小时的排序」（docs/05-§2 全局约定）。
	DSN                    string
	MaxOpenConns           int
	MaxIdleConns           int
	ConnMaxLifetimeMinutes int
	AcquireTimeoutSeconds  int
	// SlowQueryThreshold 超过它记 WARN（docs/05-§5：> 200ms）。
	SlowQueryThreshold time.Duration
}

// Redis 是缓存/计数/限流配置。
type Redis struct {
	// URL 用独立 DB（默认 /1），与 ai-platform 的 /0 再隔离一层（docs/06-§7.2）。
	URL      string
	PoolSize int
}

// Auth 是鉴权与密码策略配置（接缝 J1 的关键项在这里）。
type Auth struct {
	JWTSecret             string
	JWTAlg                string
	JWTKID                string
	JWTIssuer             string
	JWTAudience           string
	AccessTokenTTLMinutes int
	RefreshTokenTTLDays   int
	ClockSkewSeconds      int
	Argon2MemoryMB        int
	Argon2Iterations      int
	Argon2Parallelism     int
	PasswordMinLength     int
}

// AccessTokenTTL 返回 access token 有效期。
func (a Auth) AccessTokenTTL() time.Duration {
	return time.Duration(a.AccessTokenTTLMinutes) * time.Minute
}

// RefreshTokenTTL 返回 refresh token 有效期。
func (a Auth) RefreshTokenTTL() time.Duration {
	return time.Duration(a.RefreshTokenTTLDays) * 24 * time.Hour
}

// ClockSkew 返回 exp/nbf 容差。
func (a Auth) ClockSkew() time.Duration {
	return time.Duration(a.ClockSkewSeconds) * time.Second
}

// Quota 是配额配置。
type Quota struct {
	// Timezone 决定「自然日」的边界（默认 Asia/Shanghai）。
	Timezone string
	// PlanLimits 是 plan → metric → limit；MUST NOT 硬编码在代码里（docs/02-§5.1）。
	PlanLimits map[string]map[string]int64
	// ReconcileInterval 是 Redis → MySQL 的对账周期（docs/02-§5.2：5 分钟）。
	ReconcileInterval time.Duration
}

// Rate 是限流阈值配置（docs/02-§5.3）。
type Rate struct {
	LoginPerMinute       int
	LoginAccountPerHour  int
	MsgPerMinute         int
	UploadPerMinute      int
	ConvPerMinute        int
	GlobalQPSLimit       int
	ConcurrencyQueueWait time.Duration
}

// AI 是对 ai-platform 的对接配置。
//
// 传输分工（与 SRS docs/04-§2.1 的默认不同，按项目架构决策执行）：
//   - `Chat` / `ChatStream` 走 **gRPC**（Kratos client → ai-platform gRPC server）；
//   - 其余（KB / 文档 / 任务 / 记忆 / MCP / 上下文摘要）走 **HTTP 透传**。
//
// 这样流式链路的事件映射是显式的（proto），而透传类接口零成本。
type AI struct {
	BaseURL          string
	GRPCEnabled      bool
	GRPCTarget       string
	DiscoveryEnabled bool
	NacosAddr        string
	NacosNamespace   string
	NacosGroup       string

	ConnectTimeout   time.Duration
	FirstByteTimeout time.Duration
	IdleTimeout      time.Duration
	TotalTimeout     time.Duration
	ChatTimeout      time.Duration
	MetaTimeout      time.Duration
	UploadTimeout    time.Duration

	RetryMax      int
	MaxResponseMB int

	CBFailureThreshold int
	CBOpenSeconds      int

	StreamAccumulateMaxChars int
	HistoryFallbackTurns     int
	UploadMaxMB              int
	// RefAccumulateMaxItems 是 references / tool_calls 的累积条数上限（docs/06-§2）。
	RefAccumulateMaxItems int
}

// Observ 是可观测性配置。
type Observ struct {
	OTELEnabled          bool
	OTELExporterEndpoint string
	OTELServiceName      string
	OTELTracesSamplerArg float64
	MetricsEnabled       bool
	MetricsPort          int
	MetricsAllowCIDRs    []string
	PIIHashSalt          string
}

// Internal 是内部服务凭据与保留期配置。
type Internal struct {
	// ServiceToken 是调用 ai-platform 后台接口（如清上下文）用的凭据（接缝 J9）。
	ServiceToken       string
	RetentionDays      int
	UsageRetentionDays int
	// PurgeHour / RebuildHour 是保留期清理与 Redis 重建的触发整点（**本地时区**，
	// 见 `Quota.Timezone`）。
	//
	// 必须显式给值：`NewRetentionService` 的「无效则取默认 3」兜底只能拦住
	// 「填错」，拦不住「没填」—— 零值 `0` 恰好是一个合法小时，
	// 于是「默认 3 点清理」会静默变成「跟重建一起挤在 0 点」。
	PurgeHour   int
	RebuildHour int
	// CheckInterval 是「现在是几点」的轮询周期。
	CheckInterval      time.Duration
	MigrationAutoApply bool
}
