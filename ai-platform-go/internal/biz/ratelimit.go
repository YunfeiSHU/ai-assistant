package biz

import (
	"context"
	"log/slog"
	"math"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 限流维度（docs/02-§5.3）。
//
// 字符串会进 `gw_rate_limited_total{scope}` 标签与错误信封的 `details.scope`，
// 因此是**有限枚举**：不能把用户 ID、路径这类会无限增长的值当 scope 传进来。
const (
	// RateScopeLoginIP 是「单 IP 登录」：10/min。
	RateScopeLoginIP = "login_ip"
	// RateScopeLoginAccount 是「单账号登录」：20/h（防撞库，比 IP 维度慢得多）。
	RateScopeLoginAccount = "login_account"
	// RateScopeMessage 是「单用户发消息」：20/min。
	RateScopeMessage = "message"
	// RateScopeUpload 是「单用户上传」：10/min。
	RateScopeUpload = "upload"
	// RateScopeConversation 是「单用户建会话」：30/min。
	RateScopeConversation = "conversation"
	// RateScopeGlobalQPS 是「全局 QPS」：500，超出回 `503 AI_OVERLOADED`。
	RateScopeGlobalQPS = "global_qps"
	// RateScopeConcurrency 是并发对话超额（由 QuotaService 打点，不由中间件负责）。
	RateScopeConcurrency = "concurrency"
)

// RateLimiter 是限流计数器的抽象（实现在 data/redis，带本地兜底）。
//
// 语义：在 `scope` + `id` 这个桶上消费 1 个名额，返回是否放行与
// 「最早可以重试」的等待时长（用于 `Retry-After`）。
type RateLimiter interface {
	Allow(ctx context.Context, scope, id string, limit int64, window time.Duration, at time.Time) (allowed bool, retryAfter time.Duration, err error)
}

// RateLimits 是限流阈值（来自配置，MUST NOT 硬编码）。
type RateLimits struct {
	LoginPerMinute      int
	LoginAccountPerHour int
	MsgPerMinute        int
	UploadPerMinute     int
	ConvPerMinute       int
	GlobalQPSLimit      int
}

// RateLimitDeps 是 RateLimitService 的依赖。
type RateLimitDeps struct {
	Limiter RateLimiter
	Limits  RateLimits
	Metrics Metrics
	Log     *slog.Logger
	Clock   nowFunc
}

// RateLimitService 把「维度 → 阈值/窗口」的映射收在一处。
//
// 阈值、窗口绑定、打点、Retry-After 必须**一致地**发生：散在各中间件里漏一个，
// `gw_rate_limited_total` 就会静默偏低（比真实拦截次数少，且不会触发告警）。
type RateLimitService struct {
	d RateLimitDeps
}

// NewRateLimitService 构造服务。
func NewRateLimitService(d RateLimitDeps) *RateLimitService {
	d.Metrics = OrNoop(d.Metrics)
	if d.Clock == nil {
		d.Clock = time.Now
	}
	return &RateLimitService{d: d}
}

// windowFor 返回某维度使用的 (limit, window)。
//
// 窗口与阈值成对出现，不拆成两个函数：拆开后调用方可以传「10 次 / 1 小时」，
// 而那是完全不同的语义（配置里 IP 维度是 10/min）。
func (s *RateLimitService) windowFor(scope string) (int64, time.Duration) {
	l := s.d.Limits
	switch scope {
	case RateScopeLoginIP:
		return int64(l.LoginPerMinute), time.Minute
	case RateScopeLoginAccount:
		return int64(l.LoginAccountPerHour), time.Hour
	case RateScopeMessage:
		return int64(l.MsgPerMinute), time.Minute
	case RateScopeUpload:
		return int64(l.UploadPerMinute), time.Minute
	case RateScopeConversation:
		return int64(l.ConvPerMinute), time.Minute
	case RateScopeGlobalQPS:
		return int64(l.GlobalQPSLimit), time.Second
	default:
		// 未知维度：不限制。比「默默按某个默认值限流」安全 ——
		// 后者会在新增维度却忘了配阈值时立刻误伤线上流量。
		return 0, time.Minute
	}
}

// Allow 判定一次请求是否放行。返回的 error 一定是 `*errs.AppError`：
// 普通维度超额 → `429 RATE_LIMITED`（带 `Retry-After`）；全局 QPS 超额 →
// `503 AI_OVERLOADED`（docs/02-§5.3：这是**整体过载**而不是「你太快了」，
// 客户端应当退避而不是重试）。
func (s *RateLimitService) Allow(ctx context.Context, scope, id string) error {
	limit, window := s.windowFor(scope)
	if limit <= 0 || id == "" {
		return nil
	}
	if s.d.Limiter == nil {
		return nil
	}
	at := s.d.Clock()
	allowed, retryAfter, err := s.d.Limiter.Allow(ctx, scope, id, limit, window, at)
	if err != nil {
		// 限流器整体不可用：**放行**而不是拒绝（与配额相反，那边拒绝），因为两者后果不对称：
		// 放行的最坏结果是短时压力偏大（Redis 兜底本地计数仍在拦），拒绝则是**全部用户不可用**。
		s.log().WarnContext(ctx, "ratelimit.unavailable",
			slog.String("scope", scope), slog.Any("error", err))
		return nil
	}
	if allowed {
		return nil
	}
	s.d.Metrics.RateLimited(scope)
	seconds := int(math.Ceil(retryAfter.Seconds()))
	if seconds <= 0 {
		seconds = 1
	}
	e := errs.New(errs.CodeRateLimited).
		WithDetail("scope", scope).
		WithDetail("limit", limit).
		WithDetail("window_seconds", int(window.Seconds())).
		WithRetryAfter(seconds)
	if scope == RateScopeGlobalQPS {
		// 全局过载：透传成 AI 过载语义，让客户端的退避策略与收到
		// AI `OVERLOADED` 时完全一致（不必为两种 503 写两套逻辑）。
		e = errs.New(errs.CodeAIOverloaded).
			WithMessage("服务繁忙，请稍后重试").
			WithDetail("scope", scope).
			WithRetryAfter(seconds)
	}
	return e
}

func (s *RateLimitService) log() *slog.Logger {
	if s.d.Log != nil {
		return s.d.Log
	}
	return slog.Default()
}
