package middleware

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// HeaderDebugTrace 打开单请求强制采样（docs/06-§5.1）。
//
// 为什么需要它：默认采样率是 0.1，而「手工复现一次问题」恰恰需要
// 这一次被 100% 留下 —— 否则用户拿着 trace_id 去 Jaeger 里搜，
// 只会得到「什么都没找到」，然后开始怀疑上报链路。
const HeaderDebugTrace = "X-Debug-Trace"

// TraceOptions 控制 span 中间件的行为。
type TraceOptions struct {
	// Provider 为 nil 或未启用时，中间件退化成 no-op（不建 span、
	// 不碰 ctx），这样 OTEL_ENABLED=false 时的行为与 M5 完全一致。
	Provider *otelx.Provider

	// ForceSample 为真时所有请求都打上强制采样标记。
	ForceSample bool

	// PIIHashSalt 用于 user_id 的哈希；为空时**不加**用户属性
	// （宁可不记，也不要明文落进 Jaeger）。
	PIIHashSalt string
}

// Trace 中间件：为每个请求建立 Server span，并接续上游的 traceparent。
//
// 顺序上它必须排在 `WithTrace` **之后**：trace id 的解析与生成由
// `WithTrace` 负责（它是 M1 就存在的、错误信封依赖它的输出），
// 这里只是把它解析出的结果**反向构造**成 OTel 的远程父上下文。
//
// 为什么不让 OTel 自己走一遍标准提取：两处解析的宽容度一旦不一致
// （比如对全 0 trace id、对大写十六进制的处理），就会出现
// 「响应头 X-Trace-Id 里的值与 Jaeger 里的不同」—— 而 S8 恰恰要求
// 能用同一个 trace_id 同时在两边查到。以 `WithTrace` 的结果为准，
// 这个不一致从根上不存在。
func Trace(opt TraceOptions) gin.HandlerFunc {
	if opt.Provider == nil || !opt.Provider.Enabled() {
		return func(c *gin.Context) { c.Next() }
	}
	tracer := opt.Provider.Tracer("gateway.http")
	return func(c *gin.Context) {
		ctx := c.Request.Context()

		// 用 WithTrace 的结果构造远程父 span：span_id 可能是空
		// （表示客户端只给了 trace id），这时仍能保持同一个 trace。
		if tid := TraceID(c); tid != "" {
			if sc, ok := otelx.SpanContextFromHex(tid, SpanID(c)); ok {
				ctx = otelx.ContextWithRemoteParent(ctx, sc)
			}
		}

		name := c.Request.Method + " " + routeTemplate(c)
		ctx, span := tracer.Start(ctx, name,
			trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(
				otelx.Attr("http.method", c.Request.Method),
				otelx.Attr("http.target", c.Request.URL.Path),
				otelx.Attr("http.host", c.Request.Host),
				otelx.Attr("client.ip", c.ClientIP()),
			),
		)
		if opt.ForceSample || debugTraceRequested(c) {
			// 这个属性会被 tail sampler 读到（见 otelx/tail.go），
			// 使整条 trace 无论成功失败都被导出。
			span.SetAttributes(otelx.Attr(otelx.AttrForceSample, true))
		}

		c.Request = c.Request.WithContext(ctx)
		c.Set(ctxTraceSpanKey, span)

		c.Next()

		// 路由模板只有跑完才有值（`c.FullPath()` 在 404 时为空）——
		// 这正是 prometheus 的 route 标签口径，两处必须一致，
		// 否则指标与 trace 的维度对不上。
		route := c.FullPath()
		if route == "" {
			route = "unmatched"
		}
		status := c.Writer.Status()
		span.SetAttributes(
			otelx.Attr("http.route", route),
			otelx.Attr("http.status_code", status),
			otelx.Attr("http.response_size", c.Writer.Size()),
		)
		if hash := otelx.UserIDHash(opt.PIIHashSalt, userIDOf(c)); hash != "" {
			// 只放哈希：Jaeger 是「谁都能看的」那一类系统，
			// 明文 user_id 落进去等同于把用户表导出一份。
			span.SetAttributes(attribute.String("user_id_hash", hash))
		}
		if status >= http.StatusInternalServerError {
			span.SetStatus(codes.Error, http.StatusText(status))
		}
		span.End()
	}
}

// ctxTraceSpanKey 是 span 在 gin 上下文里的键（供 auth 中间件补属性）。
const ctxTraceSpanKey = "gw_otel_span"

// SpanFrom 取出当前请求的 span；没有时返回 nil。
//
// 跨包（auth 中间件在 service 层）需要它来补 `enduser.id` 之类的属性，
// 但**不允许**把它当成「span 一定存在」的保证 —— OTEL 关闭时它就是 nil。
func SpanFrom(c *gin.Context) trace.Span {
	v, ok := c.Get(ctxTraceSpanKey)
	if !ok {
		return nil
	}
	span, _ := v.(trace.Span)
	return span
}

// AnnotateSpan 在当前请求的 span 上追加属性（无 span 时为 no-op）。
func AnnotateSpan(c *gin.Context, attrs ...attribute.KeyValue) {
	if span := SpanFrom(c); span != nil && len(attrs) > 0 {
		span.SetAttributes(attrs...)
	}
}

// RouteTemplate 返回 gin 的路由模板（如 `/api/v1/conversations/:id`）。
//
// 不用 `c.Request.URL.Path` 的原因与指标一致：真实路径会让基数爆炸
// （每个 id 一条时间线），而路由模板是有限集合。
// 未匹配到路由时统一为 `unmatched`，避免「探测扫描把指标维度撑爆」。
func RouteTemplate(c *gin.Context) string {
	if route := c.FullPath(); route != "" {
		return route
	}
	return "unmatched"
}

// routeTemplate 是 RouteTemplate 的内部别名（中间件内多处使用）。
func routeTemplate(c *gin.Context) string { return RouteTemplate(c) }

func debugTraceRequested(c *gin.Context) bool {
	v := strings.TrimSpace(c.GetHeader(HeaderDebugTrace))
	return v == "1" || strings.EqualFold(v, "true")
}

func userIDOf(c *gin.Context) string {
	return UserID(c)
}
