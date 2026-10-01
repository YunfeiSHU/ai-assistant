// Package logx 统一结构化日志。
//
// 契约（docs/06-§5.3）：slog JSON（生产）/ text（本地），输出到 stdout，MUST NOT 自己写文件与轮转；
// 每条日志须带 ts/level/msg/service/version/trace_id/span_id/request_id，
// 并 MUST 按 docs/06-§4.4 脱敏（凭据一律 `***`）。
package logx

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"
)

// 请求级字段在 context 中的键。用私有类型避免与其它包的键冲突。
type ctxKey struct{ name string }

var (
	keyTraceID   = ctxKey{"trace_id"}
	keySpanID    = ctxKey{"span_id"}
	keyRequestID = ctxKey{"request_id"}
	keyUserID    = ctxKey{"user_id"}
	keyRoute     = ctxKey{"route"}
	keyMethod    = ctxKey{"method"}
	// keyLogger 存的是 *slog.Logger 而不是字符串，其余 ctxKey 都是字符串。
	// 判型时靠 `.(*slog.Logger)` 自然区分，不会相互污染。
	keyLogger = ctxKey{"logger"}
)

// Options 是构造 logger 的参数。
type Options struct {
	Level   string
	Format  string
	Service string
	Version string
	Writer  io.Writer
}

// New 构造基础 logger；service / version 作为常量字段附加到每条日志。
func New(opts Options) *slog.Logger {
	if opts.Writer == nil {
		opts.Writer = os.Stdout
	}
	if opts.Service == "" {
		opts.Service = "go-services"
	}
	handlerOpts := &slog.HandlerOptions{Level: ParseLevel(opts.Level)}

	var base slog.Handler
	if strings.EqualFold(opts.Format, "text") {
		base = slog.NewTextHandler(opts.Writer, handlerOpts)
	} else {
		base = slog.NewJSONHandler(opts.Writer, handlerOpts)
	}

	// 脱敏包装放在最外层：任何调用方（含第三方库）写出的日志都会被过滤。
	logger := slog.New(&redactHandler{next: base})
	logger = logger.With(slog.String("service", opts.Service), slog.String("version", opts.Version))
	return logger
}

// ParseLevel 把配置里的级别名转成 slog.Level；未知值回退 INFO。
func ParseLevel(name string) slog.Level {
	switch strings.ToUpper(strings.TrimSpace(name)) {
	case "DEBUG":
		return slog.LevelDebug
	case "WARN", "WARNING":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

// Fields 是需要放进 context 的请求级字段。
type Fields struct {
	TraceID   string
	SpanID    string
	RequestID string
	UserID    string
	Route     string
	Method    string
}

// NewContext 把请求级字段写进 context。
func NewContext(ctx context.Context, f Fields) context.Context {
	set := func(k ctxKey, v string) {
		if v != "" {
			ctx = context.WithValue(ctx, k, v)
		}
	}
	set(keyTraceID, f.TraceID)
	set(keySpanID, f.SpanID)
	set(keyRequestID, f.RequestID)
	set(keyUserID, f.UserID)
	set(keyRoute, f.Route)
	set(keyMethod, f.Method)
	return ctx
}

// FieldsFrom 读回请求级字段（缺失字段为空串）。
func FieldsFrom(ctx context.Context) Fields {
	get := func(k ctxKey) string {
		if v, ok := ctx.Value(k).(string); ok {
			return v
		}
		return ""
	}
	return Fields{
		TraceID:   get(keyTraceID),
		SpanID:    get(keySpanID),
		RequestID: get(keyRequestID),
		UserID:    get(keyUserID),
		Route:     get(keyRoute),
		Method:    get(keyMethod),
	}
}

// WithLogger 把基础 logger 放进 context。
// httpx / middleware 这类下游包无法持有启动时构造的 logger（传参要穿透整条调用链，
// 反向导入又成环），不放进去只能退回 slog.Default() —— 一个未配置的 stdlib handler，
// 输出格式与本服务其它日志不一致，也没有 service / version 字段。
func WithLogger(ctx context.Context, logger *slog.Logger) context.Context {
	if logger == nil {
		return ctx
	}
	return context.WithValue(ctx, keyLogger, logger)
}

// From 返回带上下文字段的 logger；base 为 nil 时优先用 context 里的 logger，
// 再退回 slog.Default()。这样调用方不必手工把 trace_id 抄进每个日志调用
// （漏抄一次就断了链路）。
func From(ctx context.Context, base *slog.Logger) *slog.Logger {
	if base == nil {
		if l, ok := ctx.Value(keyLogger).(*slog.Logger); ok {
			base = l
		}
	}
	if base == nil {
		base = slog.Default()
	}
	f := FieldsFrom(ctx)
	var attrs []slog.Attr
	appendIf := func(key, value string) {
		if value != "" {
			attrs = append(attrs, slog.String(key, value))
		}
	}
	appendIf("trace_id", f.TraceID)
	appendIf("span_id", f.SpanID)
	appendIf("request_id", f.RequestID)
	appendIf("user_id", f.UserID)
	if len(attrs) == 0 {
		return base
	}
	return base.With(attrsToAny(attrs)...)
}

func attrsToAny(attrs []slog.Attr) []any {
	out := make([]any, 0, len(attrs))
	for _, a := range attrs {
		out = append(out, a)
	}
	return out
}

// TraceIDFrom 是只取 trace_id 的便捷方法（错误信封要用）。
func TraceIDFrom(ctx context.Context) string {
	if v, ok := ctx.Value(keyTraceID).(string); ok {
		return v
	}
	return ""
}
