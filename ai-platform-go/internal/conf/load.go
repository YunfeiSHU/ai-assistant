package conf

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	// 内嵌 IANA 时区数据（约 450KB）。不加这一行时 `time.LoadLocation("Asia/Shanghai")`
	// 依赖宿主机 zoneinfo，在 scratch/distroless 镜像里会失败，于是 QUOTA_TIMEZONE
	// 静默回退 UTC —— 表现是「配额在早上 8 点重置」而不是 0 点，且不报任何错。
	_ "time/tzdata"

	"github.com/joho/godotenv"
)

// DefaultPlanLimits 是内置的免费档限额（docs/02-§5.1）。
// 它只是默认值：真正的限额来自 `QUOTA_PLAN_MAP`，MUST NOT 硬编码在业务代码里
// （否则调额度要改代码 + 重新发版）。
func DefaultPlanLimits() map[string]map[string]int64 {
	return map[string]map[string]int64{
		"free": {
			"chat_requests":   100,
			"llm_tokens":      200_000,
			"kb_count":        3,
			"documents_count": 50,
			"storage_bytes":   512 * 1024 * 1024,
			"concurrency":     5,
		},
		"pro": {
			"chat_requests":   10_000,
			"llm_tokens":      20_000_000,
			"kb_count":        50,
			"documents_count": 2_000,
			"storage_bytes":   50 * 1024 * 1024 * 1024,
			"concurrency":     50,
		},
	}
}

// Load 读取配置：`.env` → 环境变量 → 默认值，然后做启动期校验。
// `.env` 只在变量不存在时补齐（godotenv.Load 的语义即如此），
// 因此容器里注入的同名环境变量永远优先。
func Load() (*Config, error) {
	if err := loadDotEnv(); err != nil {
		return nil, err
	}

	l := &loader{}
	cfg := &Config{}
	cfg.App = loadApp(l)
	cfg.MySQL = loadMySQL(l)
	cfg.Redis = loadRedis(l)
	cfg.Auth = loadAuth(l)
	cfg.Quota = loadQuota(l)
	cfg.Rate = loadRate(l)
	cfg.AI = loadAI(l)
	cfg.Observ = loadObserv(l)
	cfg.Internal = loadInternal(l)
	if l.err != nil {
		return nil, l.err
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return cfg, nil
}

// EnvFileEnvVar 是指定 .env 路径的环境变量名。
// .env 的默认位置是进程工作目录，而从 IDE / 任务 / CI 启动时工作目录往往不是
// 项目根 —— 「.env 明明写了却没生效」几乎全部是这个问题。
const EnvFileEnvVar = "ENV_FILE"

func loadDotEnv() error {
	path := os.Getenv(EnvFileEnvVar)
	if path == "" {
		path = ".env"
	}
	if _, err := os.Stat(path); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			// 没有 .env 是正常情况（容器里全部走环境变量注入）。
			return nil
		}
		return fmt.Errorf("读取 %s 失败: %w", path, err)
	}
	if err := godotenv.Load(path); err != nil {
		// 有文件但解析失败必须报错：静默回退到默认值会让「配置写错」
		// 表现成「配置没生效」，而默认值常常也能启动，于是带着错配置上线。
		return fmt.Errorf("解析 %s 失败: %w", path, err)
	}
	return nil
}

// loader 收集第一个解析错误，避免为每个字段写一遍 if err != nil。
type loader struct{ err error }

func (l *loader) fail(format string, args ...any) {
	if l.err == nil {
		l.err = fmt.Errorf(format, args...)
	}
}

func (l *loader) str(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return strings.TrimSpace(v)
	}
	return def
}

// strRaw 与 str 相同但不裁剪空白（密钥类值允许含空格）。
func (l *loader) strRaw(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}

func (l *loader) intVal(key string, def int) int {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return def
	}
	v, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil {
		l.fail("配置 %s=%q 不是合法整数: %v", key, raw, err)
		return def
	}
	return v
}

func (l *loader) floatVal(key string, def float64) float64 {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return def
	}
	v, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		l.fail("配置 %s=%q 不是合法浮点数: %v", key, raw, err)
		return def
	}
	return v
}

func (l *loader) boolVal(key string, def bool) bool {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return def
	}
	v, err := strconv.ParseBool(strings.TrimSpace(raw))
	if err != nil {
		l.fail("配置 %s=%q 不是合法布尔值: %v", key, raw, err)
		return def
	}
	return v
}

func (l *loader) csv(key string, def []string) []string {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return def
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func (l *loader) seconds(key string, def int) time.Duration {
	return time.Duration(l.intVal(key, def)) * time.Second
}

func (l *loader) jsonPlanLimits(key string, def map[string]map[string]int64) map[string]map[string]int64 {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return def
	}
	var parsed map[string]map[string]int64
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		l.fail("配置 %s 不是合法的 plan→metric→limit JSON: %v", key, err)
		return def
	}
	if len(parsed) == 0 {
		l.fail("配置 %s 解析结果为空", key)
		return def
	}
	return parsed
}

func loadApp(l *loader) App {
	return App{
		Env:                     l.str("APP_ENV", "local"),
		HTTPAddr:                l.str("HTTP_ADDR", "0.0.0.0:8080"),
		APIPrefix:               normalisePrefix(l.str("API_PREFIX", "/api/v1")),
		Debug:                   l.boolVal("DEBUG", true),
		LogLevel:                strings.ToUpper(l.str("LOG_LEVEL", "INFO")),
		LogFormat:               strings.ToLower(l.str("LOG_FORMAT", "json")),
		GracefulShutdownSeconds: l.intVal("GRACEFUL_SHUTDOWN_SECONDS", 20),
		MaxJSONBodyMB:           l.intVal("MAX_JSON_BODY_MB", 1),
		MaxResponseMB:           l.intVal("MAX_RESPONSE_MB", 16),
		CORSAllowedOrigins:      l.csv("CORS_ALLOWED_ORIGINS", []string{"http://localhost:3000"}),
		TrustedProxyCount:       l.intVal("TRUSTED_PROXY_COUNT", 1),
		AutoTitleMaxChars:       l.intVal("AUTO_TITLE_MAX_CHARS", 30),
		Version:                 l.str("APP_VERSION", "0.1.0"),
		Commit:                  l.str("APP_COMMIT", "unknown"),
	}
}

func loadMySQL(l *loader) MySQL {
	return MySQL{
		// ⚠️ 这里不能放带口令的默认值（历史上放过，等于把开发机凭据提交进仓库）。
		// 空值时由 Validate 在启动期报错；本地开发由 `.env` 提供。
		DSN:                    l.str("MYSQL_DSN", ""),
		MaxOpenConns:           l.intVal("MYSQL_MAX_OPEN_CONNS", 20),
		MaxIdleConns:           l.intVal("MYSQL_MAX_IDLE_CONNS", 10),
		ConnMaxLifetimeMinutes: l.intVal("MYSQL_CONN_MAX_LIFETIME_MIN", 30),
		AcquireTimeoutSeconds:  l.intVal("MYSQL_ACQUIRE_TIMEOUT_SECONDS", 5),
		SlowQueryThreshold:     200 * time.Millisecond,
	}
}

func loadRedis(l *loader) Redis {
	return Redis{
		// 默认 db 1：与 ai-platform 的 db 0 再隔离一层（docs/06-§7.2）。
		URL:      l.str("REDIS_URL", "redis://127.0.0.1:6379/1"),
		PoolSize: l.intVal("REDIS_POOL_SIZE", 20),
	}
}

func loadAuth(l *loader) Auth {
	return Auth{
		JWTSecret:             l.strRaw("JWT_SECRET", ""),
		JWTAlg:                l.str("JWT_ALG", "HS256"),
		JWTKID:                l.str("JWT_KID", "k1"),
		JWTIssuer:             l.str("JWT_ISSUER", "ai-assistant"),
		JWTAudience:           l.str("JWT_AUDIENCE", "ai-platform"),
		AccessTokenTTLMinutes: l.intVal("ACCESS_TOKEN_TTL_MINUTES", 30),
		RefreshTokenTTLDays:   l.intVal("REFRESH_TOKEN_TTL_DAYS", 30),
		ClockSkewSeconds:      l.intVal("JWT_CLOCK_SKEW_SECONDS", 30),
		Argon2MemoryMB:        l.intVal("ARGON2_MEMORY_MB", 64),
		Argon2Iterations:      l.intVal("ARGON2_ITERATIONS", 3),
		Argon2Parallelism:     l.intVal("ARGON2_PARALLELISM", 4),
		PasswordMinLength:     l.intVal("PASSWORD_MIN_LENGTH", 8),
	}
}

func loadQuota(l *loader) Quota {
	return Quota{
		Timezone:          l.str("QUOTA_TIMEZONE", "Asia/Shanghai"),
		PlanLimits:        l.jsonPlanLimits("QUOTA_PLAN_MAP", DefaultPlanLimits()),
		ReconcileInterval: 5 * time.Minute,
	}
}

func loadRate(l *loader) Rate {
	return Rate{
		LoginPerMinute:       l.intVal("LOGIN_RATE_PER_MINUTE", 10),
		LoginAccountPerHour:  l.intVal("LOGIN_ACCOUNT_RATE_PER_HOUR", 20),
		MsgPerMinute:         l.intVal("MSG_RATE_PER_MINUTE", 20),
		UploadPerMinute:      l.intVal("UPLOAD_RATE_PER_MINUTE", 10),
		ConvPerMinute:        l.intVal("CONV_RATE_PER_MINUTE", 30),
		GlobalQPSLimit:       l.intVal("GLOBAL_QPS_LIMIT", 500),
		ConcurrencyQueueWait: 3 * time.Second,
	}
}

func loadAI(l *loader) AI {
	return AI{
		BaseURL:     l.str("AI_PLATFORM_BASE_URL", "http://127.0.0.1:8000"),
		GRPCEnabled: l.boolVal("AI_GRPC_ENABLED", true),
		GRPCTarget:  l.str("AI_PLATFORM_GRPC_TARGET", "127.0.0.1:50051"),

		DiscoveryEnabled: l.boolVal("AI_DISCOVERY_ENABLED", false),
		NacosAddr:        l.str("NACOS_ADDR", ""),
		NacosNamespace:   l.str("NACOS_NAMESPACE", ""),
		NacosGroup:       l.str("NACOS_GROUP", "DEFAULT_GROUP"),

		// 全部 MUST 比 AI 侧宽松（docs/04-§3.3）：网关先超时会掩盖 AI 的真实错误码。
		ConnectTimeout:   l.seconds("AI_CONNECT_TIMEOUT_SECONDS", 3),
		FirstByteTimeout: l.seconds("AI_FIRST_BYTE_TIMEOUT_SECONDS", 35),
		IdleTimeout:      l.seconds("AI_IDLE_TIMEOUT_SECONDS", 150),
		TotalTimeout:     l.seconds("AI_TOTAL_TIMEOUT_SECONDS", 360),
		ChatTimeout:      l.seconds("AI_CHAT_TIMEOUT_SECONDS", 70),
		MetaTimeout:      l.seconds("AI_META_TIMEOUT_SECONDS", 8),
		UploadTimeout:    l.seconds("AI_UPLOAD_TIMEOUT_SECONDS", 120),

		RetryMax:      l.intVal("AI_RETRY_MAX", 2),
		MaxResponseMB: l.intVal("AI_MAX_RESPONSE_MB", 16),

		CBFailureThreshold: l.intVal("CB_FAILURE_THRESHOLD", 10),
		CBOpenSeconds:      l.intVal("CB_OPEN_SECONDS", 30),

		StreamAccumulateMaxChars: l.intVal("STREAM_ACCUMULATE_MAX_CHARS", 64000),
		HistoryFallbackTurns:     l.intVal("HISTORY_FALLBACK_TURNS", 10),
		UploadMaxMB:              l.intVal("UPLOAD_MAX_MB", 50),
		RefAccumulateMaxItems:    200,
	}
}

func loadObserv(l *loader) Observ {
	return Observ{
		OTELEnabled:          l.boolVal("OTEL_ENABLED", true),
		OTELExporterEndpoint: l.str("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
		OTELServiceName:      l.str("OTEL_SERVICE_NAME", "go-services"),
		OTELTracesSamplerArg: l.floatVal("OTEL_TRACES_SAMPLER_ARG", 0.1),
		MetricsEnabled:       l.boolVal("METRICS_ENABLED", true),
		MetricsPort:          l.intVal("METRICS_PORT", 9100),
		MetricsAllowCIDRs:    l.csv("METRICS_ALLOW_CIDRS", []string{"127.0.0.1/32"}),
		PIIHashSalt:          l.strRaw("PII_HASH_SALT", ""),
	}
}

func loadInternal(l *loader) Internal {
	return Internal{
		ServiceToken:       l.strRaw("INTERNAL_SERVICE_TOKEN", ""),
		RetentionDays:      l.intVal("RETENTION_DAYS", 30),
		UsageRetentionDays: l.intVal("USAGE_RETENTION_DAYS", 90),
		// 默认与 docs/02-§5.2 的「0 点重建、3 点清理」一致。
		PurgeHour:          l.intVal("RETENTION_PURGE_HOUR", 3),
		RebuildHour:        l.intVal("RETENTION_REBUILD_HOUR", 0),
		CheckInterval:      l.seconds("RETENTION_CHECK_INTERVAL_SECONDS", 3600),
		MigrationAutoApply: l.boolVal("MIGRATION_AUTO_APPLY", false),
	}
}

// loadQuotaLocation 校验时区名；无法解析返回 nil。
func loadQuotaLocation(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		return nil
	}
	return loc
}

func normalisePrefix(p string) string {
	if p == "" {
		return "/api/v1"
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return strings.TrimSuffix(p, "/")
}

// IsProd 报告当前是否生产环境。
func (c *Config) IsProd() bool { return c.App.Env == "prod" }

// Validate 做启动期校验。
// 原则：能在启动期发现的配置错误 MUST 在启动期失败（REQ-NFR-009）——拖到请求期
// 才暴露的配置问题，表现往往是「偶发 401」「偶发 503」，极难定位。
func (c *Config) Validate() error {
	var problems []string
	add := func(format string, args ...any) {
		problems = append(problems, fmt.Sprintf(format, args...))
	}

	// ---- 与运行环境无关的硬约束 ----
	if !validEnv(c.App.Env) {
		add("APP_ENV=%q 不合法（可选 local/dev/staging/prod）", c.App.Env)
	}
	if c.App.MaxJSONBodyMB <= 0 {
		add("MAX_JSON_BODY_MB 必须为正")
	}
	if c.App.TrustedProxyCount < 0 {
		add("TRUSTED_PROXY_COUNT 不能为负")
	}
	switch c.App.LogFormat {
	case "json", "text":
	default:
		add("LOG_FORMAT=%q 不合法（可选 json/text）", c.App.LogFormat)
	}
	switch c.App.LogLevel {
	case "DEBUG", "INFO", "WARN", "ERROR":
	default:
		add("LOG_LEVEL=%q 不合法（可选 DEBUG/INFO/WARN/ERROR）", c.App.LogLevel)
	}

	if c.Auth.JWTAlg != "HS256" {
		add("JWT_ALG 目前只支持 HS256（接缝 J1，改动需两侧同步），当前 %q", c.Auth.JWTAlg)
	}
	if c.Auth.JWTIssuer == "" || c.Auth.JWTAudience == "" {
		add("JWT_ISSUER / JWT_AUDIENCE 不能为空（接缝 J1）")
	}
	if c.Auth.AccessTokenTTLMinutes <= 0 {
		add("ACCESS_TOKEN_TTL_MINUTES 必须为正")
	}
	if c.Auth.RefreshTokenTTLDays <= 0 {
		add("REFRESH_TOKEN_TTL_DAYS 必须为正")
	}
	if c.Auth.Argon2MemoryMB < 8 {
		add("ARGON2_MEMORY_MB 至少 8（当前 %d，过低的成本参数等于不设防）", c.Auth.Argon2MemoryMB)
	}
	if c.Auth.PasswordMinLength < 8 {
		add("PASSWORD_MIN_LENGTH 至少 8（契约要求）")
	}

	// 网关超时 MUST 严格大于 AI 侧（docs/04-§3.3），否则网关先超时会掩盖上游错误码。
	if c.AI.FirstByteTimeout <= 30*time.Second {
		add("AI_FIRST_BYTE_TIMEOUT_SECONDS 必须 > AI 侧的 30s（当前 %s）", c.AI.FirstByteTimeout)
	}
	if c.AI.IdleTimeout <= 120*time.Second {
		add("AI_IDLE_TIMEOUT_SECONDS 必须 > AI 侧的 120s（当前 %s）", c.AI.IdleTimeout)
	}
	if c.AI.TotalTimeout <= 300*time.Second {
		add("AI_TOTAL_TIMEOUT_SECONDS 必须 > AI 侧的 300s（当前 %s）", c.AI.TotalTimeout)
	}
	if c.AI.ChatTimeout <= 60*time.Second {
		add("AI_CHAT_TIMEOUT_SECONDS 必须 > AI 侧的 60s（当前 %s）", c.AI.ChatTimeout)
	}
	if c.AI.MetaTimeout <= 5*time.Second {
		add("AI_META_TIMEOUT_SECONDS 必须 > AI 侧的 5s（当前 %s）", c.AI.MetaTimeout)
	}
	if c.AI.GRPCEnabled && strings.TrimSpace(c.AI.GRPCTarget) == "" {
		add("AI_GRPC_ENABLED=true 时 AI_PLATFORM_GRPC_TARGET 必填")
	}
	if c.AI.DiscoveryEnabled && c.AI.NacosAddr == "" {
		add("AI_DISCOVERY_ENABLED=true 时 NACOS_ADDR 必填")
	}
	if c.AI.StreamAccumulateMaxChars <= 0 {
		add("STREAM_ACCUMULATE_MAX_CHARS 必须为正")
	}
	if c.AI.HistoryFallbackTurns < 0 {
		add("HISTORY_FALLBACK_TURNS 不能为负")
	}

	// 小时必须在 0..23：越界时 `NewRetentionService` 会静默回退到默认值，
	// 而「我明明配了 RETENTION_PURGE_HOUR=25，为什么 3 点跑」很难当场想明白。
	if c.Internal.PurgeHour < 0 || c.Internal.PurgeHour > 23 {
		add("RETENTION_PURGE_HOUR=%d 必须在 0..23", c.Internal.PurgeHour)
	}
	if c.Internal.RebuildHour < 0 || c.Internal.RebuildHour > 23 {
		add("RETENTION_REBUILD_HOUR=%d 必须在 0..23", c.Internal.RebuildHour)
	}
	if c.Internal.CheckInterval <= 0 {
		add("RETENTION_CHECK_INTERVAL 必须为正")
	}
	if c.Internal.RetentionDays <= 0 {
		add("RETENTION_DAYS 必须为正")
	}

	if _, ok := c.Quota.PlanLimits["free"]; !ok {
		add("QUOTA_PLAN_MAP 必须包含 free 档（用户默认 plan）")
	}
	if loc := loadQuotaLocation(c.Quota.Timezone); loc == nil {
		add("QUOTA_TIMEZONE=%q 不是合法时区", c.Quota.Timezone)
	}

	if err := validateRedisURL(c.Redis.URL); err != nil {
		add("REDIS_URL 不合法: %v", err)
	}

	// MySQL DSN 没有内置默认值（与 JWT_SECRET 同一策略）：它内含口令，写进源码
	// 就等于把凭据提交进仓库。缺失时在启动期失败，而不是连库时才报错。
	if strings.TrimSpace(c.MySQL.DSN) == "" {
		add("MYSQL_DSN 必填（形如 user:pass@tcp(host:3306)/db?parseTime=true）；出于安全考虑没有内置默认值")
	}

	// ---- prod 专属的硬约束 ----
	if c.IsProd() {
		if c.App.Debug {
			add("prod 下 DEBUG 必须为 false")
		}
		for _, o := range c.App.CORSAllowedOrigins {
			if o == "*" {
				add("prod 下 CORS_ALLOWED_ORIGINS 不能为 *")
			}
		}
		required := map[string]string{
			"JWT_SECRET":             c.Auth.JWTSecret,
			"MYSQL_DSN":              c.MySQL.DSN,
			"REDIS_URL":              c.Redis.URL,
			"AI_PLATFORM_BASE_URL":   c.AI.BaseURL,
			"INTERNAL_SERVICE_TOKEN": c.Internal.ServiceToken,
			"PII_HASH_SALT":          c.Observ.PIIHashSalt,
		}
		for name, value := range required {
			if strings.TrimSpace(value) == "" {
				add("prod 下 %s 必填", name)
			}
		}
	}

	if len(problems) > 0 {
		return fmt.Errorf("配置校验失败:\n  - %s", strings.Join(problems, "\n  - "))
	}
	return nil
}

func validEnv(env string) bool {
	switch env {
	case "local", "dev", "staging", "prod":
		return true
	}
	return false
}

func validateRedisURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if u.Scheme != "redis" && u.Scheme != "rediss" {
		return fmt.Errorf("scheme 必须为 redis/rediss，当前 %q", u.Scheme)
	}
	if u.Host == "" {
		return errors.New("缺少 host:port")
	}
	return nil
}

// Redacted 返回可安全写入日志的配置摘要：凭据一律替换为 `***`（REQ-NFR-005）。
func (c *Config) Redacted() map[string]any {
	return map[string]any{
		"env":              c.App.Env,
		"http_addr":        c.App.HTTPAddr,
		"api_prefix":       c.App.APIPrefix,
		"debug":            c.App.Debug,
		"version":          c.App.Version,
		"commit":           c.App.Commit,
		"mysql_dsn":        redactDSN(c.MySQL.DSN),
		"redis_url":        redactDSN(c.Redis.URL),
		"jwt_issuer":       c.Auth.JWTIssuer,
		"jwt_audience":     c.Auth.JWTAudience,
		"jwt_kid":          c.Auth.JWTKID,
		"jwt_secret_set":   c.Auth.JWTSecret != "",
		"internal_tok_set": c.Internal.ServiceToken != "",
		"ai_base_url":      c.AI.BaseURL,
		"ai_grpc_enabled":  c.AI.GRPCEnabled,
		"ai_grpc_target":   c.AI.GRPCTarget,
		"otel_enabled":     c.Observ.OTELEnabled,
		"metrics_enabled":  c.Observ.MetricsEnabled,
		"metrics_port":     c.Observ.MetricsPort,
		"quota_timezone":   c.Quota.Timezone,
		"ai_upload_max_mb": c.AI.UploadMaxMB,
		"ai_retry_max":     c.AI.RetryMax,
		"cb_threshold":     c.AI.CBFailureThreshold,
		"cb_open_seconds":  c.AI.CBOpenSeconds,
		"stream_accum_max": c.AI.StreamAccumulateMaxChars,
		"history_fallback": c.AI.HistoryFallbackTurns,
		"retention_days":   c.Internal.RetentionDays,
		"usage_ret_days":   c.Internal.UsageRetentionDays,
		"purge_hour":       c.Internal.PurgeHour,
		"rebuild_hour":     c.Internal.RebuildHour,
	}
}

// redactDSN 抹掉连接串里的密码段（user:***@host/db）。
func redactDSN(dsn string) string {
	at := strings.LastIndex(dsn, "@")
	if at < 0 {
		return dsn
	}
	head := dsn[:at]
	colon := strings.LastIndex(head, ":")
	if colon < 0 {
		return dsn
	}
	// 形如 `redis://user:pass` 或 `user:pass`，两处都要处理。
	prefix := ""
	if i := strings.Index(head, "://"); i >= 0 {
		prefix = head[:i+3]
		head = head[i+3:]
		colon = strings.LastIndex(head, ":")
		if colon < 0 {
			return dsn
		}
	}
	return prefix + head[:colon+1] + "***" + dsn[at:]
}
