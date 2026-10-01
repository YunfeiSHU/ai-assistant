// Package otelx 封装 OTel SDK 的启动与关闭（docs/06-§5.1）。
//
// 为什么需要这个包：OTel 的 SDK 有 5 个必须按序装配的部件
// （resource / sampler / span processor / exporter / propagator），
// 而「装配顺序错了」或「忘了装 propagator」**不会报错** ——
// 现象只是「两侧 trace 对不上」或「Jaeger 里一条都看不到」。
// 把这些约定收在一个包里，调用点就只剩 `Init` 与 `Shutdown`。
//
// 关于采样的一处**重要设计**：
//
// docs/06-§5.1 要求「采样率默认 0.1（`parentbased_traceidratio`），
// 但**错误与降级链路 MUST 100% 采样**」。这两条在**头部采样**下无法同时成立：
// 头部采样的决策时刻在 span 创建时，而「这条链路会不会出错」要到它结束才知道。
// 常见的做法是把尾部采样推给 OTel Collector，但那要求部署侧多一个组件。
//
// 因此本包把采样决策**搬到 span 结束时**（见 `tail.go`）：
// 头部一律记录，结束时按 trace 决定是否导出 —— 见过 `Error` 状态的整条 trace
// 全量导出，其余按 `OTEL_TRACES_SAMPLER_ARG` 的比例抽样。
// 配置项的**语义不变**（同一个比例、同样是 parentbased），
// 只是决策点后移，从而让「错误 100%」成为可能。
package otelx

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// Config 是 Init 需要的全部输入（来自 `conf.Observ` 与 `conf.App`）。
type Config struct {
	Enabled     bool
	Endpoint    string // 形如 `http://localhost:4317` 或 `localhost:4317`
	ServiceName string
	SamplerArg  float64

	// Version / Commit / Env 进 resource，便于在 Jaeger 里按版本筛选。
	Version string
	Commit  string
	Env     string

	// Log 用于记录「导出器装配失败」这类只能降级的错误；可为 nil。
	Log *slog.Logger
}

// Provider 是初始化后的句柄。
//
// **零值不可用**：必须用 Init 的返回值。禁用时 Init 返回的也是一个可用句柄
// （内部走 OTel 的 noop 实现），于是调用点永远不需要判断「追踪开没开」。
type Provider struct {
	tp      *sdktrace.TracerProvider
	tail    *tailSampler
	enabled bool
}

// Tracer 返回命名 tracer。禁用时返回 noop tracer（调用其方法无任何开销）。
func (p *Provider) Tracer(name string) trace.Tracer {
	if p == nil || !p.enabled || p.tp == nil {
		return otel.GetTracerProvider().Tracer(name)
	}
	return p.tp.Tracer(name)
}

// Enabled 报告 SDK 是否真的在采集。
func (p *Provider) Enabled() bool { return p != nil && p.enabled }

// Sampling 返回采样的可读描述（启动日志与 /health 用它，便于现场确认配置生效）。
func (p *Provider) Sampling() string {
	if p == nil || !p.enabled {
		return "disabled"
	}
	arg := 0.0
	if p.tail != nil {
		arg = p.tail.ratio
	}
	return fmt.Sprintf("tail:parentbased_traceidratio arg=%.4f errors=always", arg)
}

// Shutdown 冲刷并关闭。禁用时是空操作。
//
// 关停顺序不能颠倒：先 ForceFlush 把缓冲区里的 span 交给导出器，
// 再 Shutdown 导出器。反过来会让最后一批 span 永远丢在网络里 ——
// 而那批 span 恰好是「收到 SIGTERM 之前那几秒」的链路，最有用。
func (p *Provider) Shutdown(ctx context.Context) error {
	if p == nil || !p.enabled || p.tp == nil {
		return nil
	}
	var errs []error
	if err := p.tp.ForceFlush(ctx); err != nil {
		errs = append(errs, fmt.Errorf("flush trace: %w", err))
	}
	if err := p.tp.Shutdown(ctx); err != nil {
		errs = append(errs, fmt.Errorf("shutdown trace: %w", err))
	}
	return errors.Join(errs...)
}

// Init 装配 SDK。
//
// 返回的错误**只在配置本身不合法时**出现（端点写错、比例越界）。
// 「连不上 Collector」不会在这里失败 —— 那样会让「本地起服务时必须先起
// Jaeger」成为硬依赖。导不出去由导出器自己重试并打日志。
func Init(ctx context.Context, cfg Config) (*Provider, error) {
	if !cfg.Enabled {
		// 显式把全局设为 noop：否则进程内若已有别的库（gin/kratos 的插件）
		// 装过 provider，我们的 outbound 注入会带上它们的上下文，
		// 而它们未必有导出器 —— 表现是「设了 OTEL_ENABLED=false 却仍有开销」。
		otel.SetTracerProvider(otel.GetTracerProvider())
		return &Provider{enabled: false}, nil
	}

	endpoint, insecure, err := normalizeEndpoint(cfg.Endpoint)
	if err != nil {
		return nil, err
	}
	ratio := cfg.SamplerArg
	if ratio < 0 || ratio > 1 {
		// 不静默夹紧：比例写错（比如写了 `10` 想表达 10%）的人，
		// 应该看到失败，而不是得到一个「看起来在跑、实际采样率完全不同」的服务。
		return nil, fmt.Errorf("OTEL_TRACES_SAMPLER_ARG 必须在 [0,1] 内，实际 %v", ratio)
	}

	opts := []otlptracegrpc.Option{
		otlptracegrpc.WithEndpoint(endpoint),
		// 不 WithBlock：让「Collector 没起」表现为导出失败而不是启动失败。
		otlptracegrpc.WithTimeout(5 * time.Second),
	}
	if insecure {
		opts = append(opts, otlptracegrpc.WithInsecure())
	}
	exporter, err := otlptracegrpc.New(ctx, opts...)
	if err != nil {
		return nil, fmt.Errorf("创建 OTLP 导出器失败（endpoint=%s）: %w", endpoint, err)
	}

	// SpanProcessor 的下一环用 Batch：逐条导出会让每个请求多一次网络往返，
	// 而跟踪本身不该成为延迟的一部分。
	batcher := sdktrace.NewBatchSpanProcessor(exporter,
		sdktrace.WithBatchTimeout(2*time.Second),
		sdktrace.WithMaxExportBatchSize(512),
		sdktrace.WithMaxQueueSize(8192),
	)
	tail := newTailSampler(batcher, ratio, cfg.Log)

	tp := sdktrace.NewTracerProvider(
		// 头部一律记录，比例由 tail 在结束时决定（见包注释）。
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithSpanProcessor(tail),
		sdktrace.WithResource(buildResource(ctx, cfg)),
	)

	// 全局注册：`otel.Tracer(...)` 与 propagator 的默认实现都读全局，
	// 不注册的话所有手工注入都会静默变成空操作。
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		// W3C traceparent：跨语言（Go ↔ Python）唯一公认的载体。
		propagation.TraceContext{},
		// Baggage 一起传播：排障时可以把「哪个用户/哪次发布」这类
		// 非敏感上下文带过去，而不必新造一个 header。
		propagation.Baggage{},
	))

	return &Provider{tp: tp, tail: tail, enabled: true}, nil
}

// normalizeEndpoint 把配置里的 OTLP 端点规整成 gRPC 需要的 `host:port`。
//
// 默认值给的是 `http://localhost:4317`（URL 形式），因为那一版最直观；
// 但 gRPC 的 `WithEndpoint` 只接受 `host:port`，直接传 URL 会去解析成
// 一个名为 `http` 的主机 —— 一个**连不上但也不报错**的端点。
// 因此这里显式剥掉 scheme，并据此推断要不要 TLS。
func normalizeEndpoint(raw string) (endpoint string, insecure bool, err error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", false, errors.New("OTEL_EXPORTER_OTLP_ENDPOINT 为空")
	}
	if !strings.Contains(raw, "://") {
		// 已经是 host:port；无 scheme 时按「本地/内网明文」处理。
		if _, _, splitErr := net.SplitHostPort(raw); splitErr != nil {
			return "", false, fmt.Errorf("OTEL_EXPORTER_OTLP_ENDPOINT 不是合法的 host:port: %q", raw)
		}
		return raw, true, nil
	}
	u, parseErr := url.Parse(raw)
	if parseErr != nil {
		return "", false, fmt.Errorf("解析 OTEL_EXPORTER_OTLP_ENDPOINT 失败: %w", parseErr)
	}
	host := u.Host
	if host == "" {
		return "", false, fmt.Errorf("OTEL_EXPORTER_OTLP_ENDPOINT 缺少主机: %q", raw)
	}
	if u.Port() == "" {
		host = net.JoinHostPort(u.Hostname(), "4317")
	}
	// `http://` = 明文（本地 Collector / sidecar 的标准形态）；
	// `https://` = 走 TLS，交给 SDK 用系统根证书校验证书。
	return host, u.Scheme != "https", nil
}

// buildResource 描述「这些 span 是谁产生的」。
//
// service.name 必须显式给：SDK 的默认推断会取可执行文件名，
// 于是 `go run ./cmd/server` 上报的是 `server`，
// 而 Grafana 上按 `go-services` 查会一条都查不到（docs/06 默认值就是 go-services）。
func buildResource(ctx context.Context, cfg Config) *resource.Resource {
	attrs := []attribute.KeyValue{
		attribute.String("service.name", firstNonEmpty(cfg.ServiceName, "go-services")),
	}
	if cfg.Version != "" {
		attrs = append(attrs, attribute.String("service.version", cfg.Version))
	}
	if cfg.Commit != "" {
		attrs = append(attrs, attribute.String("git.commit", cfg.Commit))
	}
	if cfg.Env != "" {
		attrs = append(attrs, attribute.String("deployment.environment", cfg.Env))
	}
	if host, err := os.Hostname(); err == nil && host != "" {
		// instance.id 用主机名：单机多进程时能把「是哪个进程」区分开。
		attrs = append(attrs, attribute.String("service.instance.id", host))
	}

	// 用 resource.New（而不是 resource.Default）以**完全掌控**内容：
	// Default 会把 SDK 版本、进程 PID、以及宿主机的 os/arch 一起报上去，
	// 那些字段在高基数上有风险（PID 每次重启都变）。
	// `WithTelemetrySDK` 保留，因为「哪个 SDK 版本上报的」在跨版本排障时有用。
	res, err := resource.New(ctx,
		resource.WithTelemetrySDK(),
		resource.WithAttributes(attrs...),
	)
	if err != nil {
		// 构造失败只可能是 attributes 里有非法值；退回一个仅含 service.name 的
		// resource，保证「跟踪降级」而不是「启动失败」。
		res = resource.NewSchemaless(attribute.String("service.name", firstNonEmpty(cfg.ServiceName, "go-services")))
	}
	return res
}

func firstNonEmpty(vs ...string) string {
	for _, v := range vs {
		if v != "" {
			return v
		}
	}
	return ""
}

// ---- 出站注入与上下文桥接 ----

// Tracer 返回**全局** provider 上的命名 tracer。
//
// 为什么不要求调用方持有 `*Provider`：`biz` 层的调用点（配额、熔断、
// 编排）不应该为了打一条 span 而在构造函数里多一个依赖参数，
// 而 `Init` 已经把 SDK 注册成全局 provider 了 —— 取一次全局即可。
//
// 未启用时全局是 noop 实现，`Start` 返回的 span 所有方法都是空操作，
// 因此「OTEL_ENABLED=false」下这些调用点的开销约等于零，
// 调用方也就不必到处写 `if tracing {...}`。
func Tracer(name string) trace.Tracer { return otel.GetTracerProvider().Tracer(name) }

// SpanEnd 结束 span 并按错误设置状态。
//
// 单独一个函数是为了统一两件事，它们在每个调用点都容易漏：
//
//  1. **必须**调用 `End()`，否则 span 永远不结束（tail sampler 拿不到它，
//     Jaeger 里那条链路直接消失，且内存里的 span 只增不减）；
//  2. 错误要 `RecordError` + `SetStatus`。只设 status 不记 event 的话，
//     Jaeger 的「Errors」筛选能命中但看不到错误详情。
func SpanEnd(span trace.Span, err error) {
	if span == nil {
		return
	}
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
}

// InjectHTTP 把当前 span 上下文写进 HTTP 头（W3C `traceparent`）。
//
// 出站请求**必须**调它：漏掉的表现是「网关侧有 span、AI 侧也有 span，
// 但 Jaeger 里是两条互不相干的 trace」（docs/06-§5.1 的接缝 J3）。
func InjectHTTP(ctx context.Context, h http.Header) {
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(h))
}

// SpanContextFromHex 由十六进制 trace/span id 构造**远程**父上下文。
//
// 用途：网关自己已经解析过 `traceparent`（`middleware.WithTrace`），
// 并把它作为响应头 `X-Trace-Id` 回给客户端。若让它再走一遍标准提取，
// 两处解析的宽容度一旦不一致（比如对全 0 trace id 的处理），
// 就会出现「响应头里的 trace_id 与 Jaeger 里的对不上」——
// 而这正是 S8「用 trace_id 同时查到同一条链路」要保证的事。
// 因此这里以**网关的解析结果为准**，反过来构造 OTel 的父上下文。
//
// spanID 允许为空（这时只当成本地新 trace），返回的 SpanContext 仍携带 trace id。
func SpanContextFromHex(traceIDHex, spanIDHex string) (trace.SpanContext, bool) {
	tid, err := trace.TraceIDFromHex(traceIDHex)
	if err != nil {
		return trace.SpanContext{}, false
	}
	var sid trace.SpanID
	if spanIDHex != "" {
		parsed, sidErr := trace.SpanIDFromHex(spanIDHex)
		if sidErr != nil {
			return trace.SpanContext{}, false
		}
		sid = parsed
	}
	return trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: tid,
		SpanID:  sid,
		// 标记为「来自上游」：SDK 会据此让子 span 继承同一个 trace id。
		Remote: true,
		// 让父上下文带上「已采样」标记 —— 我们的 tail 采样会把它当作
		// parentbased 的输入，从而与上游的决定保持一致。
		TraceFlags: trace.FlagsSampled,
	}), true
}

// ContextWithRemoteParent 把远程父上下文放进 ctx，供 Tracer.Start 使用。
func ContextWithRemoteParent(ctx context.Context, sc trace.SpanContext) context.Context {
	return trace.ContextWithRemoteSpanContext(ctx, sc)
}

// TraceparentHeader 组装出站请求的 `traceparent` 头（接缝 J3）。
//
// 与 `InjectHTTP` 的区别在于**兜底**：`InjectHTTP` 依赖 ctx 里存在一个
// 有效的 span，而 `OTEL_ENABLED=false` 时根本没有 span —— 那时
// `traceparent` 会是空串，AI 侧就会自建一个新 trace id，
// 于是「网关日志里的 trace_id」与「AI 日志里的 trace_id」对不上，
// 而这正是 S8 要断言的场景（且它不该依赖 OTEL 是否开启）。
//
// 因此本函数优先用当前 span（采样位与 span id 都能被子 span 继承），
// 没有 span 时用网关自己的 `X-Trace-Id` 兜底，span id 随机生成 ——
// 那一段父子关系会丢失，但 trace id 一致，日志仍然串得起来。
//
// 返回空串表示「连兜底的 trace id 都不合法」：调用方应当**不设**这个头
// （设一个非法值会让上游直接忽略它，等于白设）。
func TraceparentHeader(ctx context.Context, fallbackTraceID string) string {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		return "00-" + sc.TraceID().String() + "-" + sc.SpanID().String() + "-" + sc.TraceFlags().String()[2:]
	}
	tid := strings.ToLower(strings.TrimSpace(fallbackTraceID))
	if len(tid) != 32 || !isHexString(tid) || isAllZeroHex(tid) {
		return ""
	}
	return "00-" + tid + "-" + randomHex(8) + "-01"
}

// isHexString 报告 s 是否全部由十六进制字符组成。
func isHexString(s string) bool {
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9', r >= 'a' && r <= 'f':
		default:
			return false
		}
	}
	return s != ""
}

// isAllZeroHex 报告 s 是否全为 '0'（W3C 规定全 0 的 trace-id 非法）。
func isAllZeroHex(s string) bool {
	for _, r := range s {
		if r != '0' {
			return false
		}
	}
	return true
}

// randomHex 生成 n 字节随机数据的十六进制串。
//
// 用 crypto/rand：span id 会出现在响应头与日志里，可预测的值会让
// 「伪造一条看起来合法的链路」变得容易（而那会污染排障结论）。
func randomHex(n int) string {
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		// 极端情况下（熵源不可用）退回时间戳，至少保证唯一性不崩塌。
		return fmt.Sprintf("%016x", time.Now().UnixNano())[:n*2]
	}
	return hex.EncodeToString(buf)
}

// TraceIDHex 返回当前 ctx 中 span 的 trace id（32 位十六进制）；无则空串。
func TraceIDHex(ctx context.Context) string {
	return trace.SpanContextFromContext(ctx).TraceID().String()
}

// SpanIDHex 返回当前 ctx 中 span 的 span id（16 位十六进制）；无则空串。
func SpanIDHex(ctx context.Context) string {
	return trace.SpanContextFromContext(ctx).SpanID().String()
}

// Attr 是 span 属性的简写，避免调用点到处 import attribute。
func Attr(key string, value any) attribute.KeyValue {
	switch v := value.(type) {
	case string:
		return attribute.String(key, v)
	case bool:
		return attribute.Bool(key, v)
	case int:
		return attribute.Int(key, v)
	case int64:
		return attribute.Int64(key, v)
	case float64:
		return attribute.Float64(key, v)
	case time.Duration:
		return attribute.Int64(key, v.Milliseconds())
	default:
		return attribute.String(key, fmt.Sprint(value))
	}
}

// JoinPath 把上游前缀与路径拼起来（不含查询串）。
//
// 单独抽出来是因为它在两个地方要**完全一致**：出站请求的构造
// 与出站 span 的 `url.path` 属性。两处各写一遍字符串拼接，
// 就会出现「span 上的路径与实际请求的路径不同」——排障时会把注意力引偏。
func JoinPath(prefix, p string) string {
	prefix = strings.TrimSuffix(prefix, "/")
	p = path.Clean("/" + strings.TrimPrefix(p, "/"))
	if prefix == "" {
		return p
	}
	return prefix + p
}
