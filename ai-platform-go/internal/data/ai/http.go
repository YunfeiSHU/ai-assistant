package ai

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// httpProxy 是 `biz.AIProxy` 的 HTTP 实现（docs/04-§7 的透传通道）。
//
// 之所以保留 HTTP 通道而不是「全部走 gRPC」：docs/04-§2.1 的 REQ-ORCH-002
// 第 3 条要求「保留 HTTP 通道用于独立验收（ai-platform 的 SRS 要求其可独立验收）」，
// 而 KB/文档/任务/上下文这些接口在 AI 侧只有 HTTP 实现 —— 为它们各写一个 proto
// 等于把「一份契约」变成两份必须同步维护的东西。
type httpProxy struct {
	base *urlBase
	http *http.Client
	opt  Options
}

var _ biz.AIProxy = (*httpProxy)(nil)

// NewProxy 构造透传客户端。
func NewProxy(opt Options) biz.AIProxy {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	return &httpProxy{
		base: newURLBase(opt.BaseURL),
		opt:  opt,
		http: newHTTPClient(opt),
	}
}

// newHTTPClient 构造 HTTP 传输。
//
// 抽出来给**透传**与**流式兜底通道**共用：两者的连接池、重定向策略、
// keepalive 必须一致，否则「流式比非流式慢/容易断」这类问题只能靠逐项比对
// 才能发现（而它们又都是 HTTP，看起来应该完全一样）。
func newHTTPClient(opt Options) *http.Client {
	return &http.Client{
		// 不透传上游的 3xx：透传接口的语义是「网关替 AI 回话」，
		// 让客户端跟着 AI 的重定向走会让「谁在保护这个接口」变得不明。
		// 例外是文档下载（M5）：那时由网关自己读 presigned URL 再 302。
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
		Transport: &http.Transport{
			// 连接池（docs/04-§3.1）：MaxIdleConnsPerHost=64 与并发量同量级，
			// 否则默认的 2 会让每个并发请求都重新建连。
			MaxIdleConns:        256,
			MaxIdleConnsPerHost: 64,
			IdleConnTimeout:     90 * time.Second,
			DialContext: (&net.Dialer{
				Timeout:   opt.ConnectTimeout,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			ForceAttemptHTTP2: true,
		},
	}
}

// Do 实现 biz.AIProxy。
func (p *httpProxy) Do(ctx context.Context, req biz.AIProxyRequest) (*biz.AIProxyResponse, error) {
	upstreamURL, err := p.base.join(req.Path)
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "invalid_upstream_path").
			WithCause(err)
	}

	callCtx, cancel := ctxWithTimeout(ctx, p.opt.timeoutFor(req.Class))
	defer cancel()

	httpReq, err := http.NewRequestWithContext(callCtx, req.Method, upstreamURL, req.Body)
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "build_request_failed").
			WithCause(err)
	}
	if req.ContentLength >= 0 {
		// 显式设置：multipart 缺 Content-Length 时上游无法解析；
		// 而 Go 对非 *bytes.Buffer/Reader 类型的 body 默认用 chunked。
		httpReq.ContentLength = req.ContentLength
	}
	if ct := strings.TrimSpace(req.ContentType); ct != "" {
		httpReq.Header.Set("Content-Type", ct)
	}
	// 凭据透传（接缝 J1）：原样转发调用方的 Bearer token，AI 侧自己校验。
	httpReq.Header.Set("Authorization", "Bearer "+strings.TrimSpace(req.UserToken))
	if traceID := strings.TrimSpace(req.TraceID); traceID != "" {
		httpReq.Header.Set("X-Trace-Id", traceID)
	}
	// 跨服务链路串联（接缝 J3，S8）：
	//
	// `traceparent` 与 `X-Trace-Id` **两个都要发**。它们不是重复：
	//
	//   - `X-Trace-Id` 是本项目自有的头，AI 侧把它写进日志上下文；
	//   - `traceparent` 是 W3C 标准头，AI 侧的 OTel 用它把它的根 span
	//     接到网关的 span 下面（它已经实现了这个逻辑）。
	//
	// 只发前者时 Jaeger 里会是两条互不相干的 trace，而日志里却对得上 ——
	// 这种「一半对一半不对」最难排查，所以两个一起发。
	if tp := otelx.TraceparentHeader(ctx, req.TraceID); tp != "" {
		httpReq.Header.Set("traceparent", tp)
	}
	// 网关不是浏览器：不透传 Cookie，也不要求上游设置 CORS 头。
	//
	// 刻意**不**自己设 `Accept-Encoding`：手动设置会关掉 Go 的透明解压，
	// 于是我们把上游的 gzip 字节当成响应体原样转发回去，却没有
	// `Content-Encoding` 头 —— 客户端拿到的是一堆无法解析的二进制。
	// 交给 net/http 处理，它保证交到我们手里的一定是明文。

	resp, err := p.http.Do(httpReq)
	if err != nil {
		return nil, p.mapTransportError(ctx, req, err)
	}
	defer func() {
		// 读完再关：连接要靠 drain 才能回到池子里。
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<10))
		_ = resp.Body.Close()
	}()

	limit := p.opt.maxResponseBytes()
	// 多读 1 字节：读满 limit 就说明被截断了，而不是恰好等于上限。
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, p.mapTransportError(ctx, req, err)
	}
	if int64(len(body)) > limit {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "response_too_large").
			WithDetail("limit_bytes", limit)
	}

	out := &biz.AIProxyResponse{
		Status:      resp.StatusCode,
		ContentType: resp.Header.Get("Content-Type"),
		Body:        body,
		RetryAfter:  resp.Header.Get("Retry-After"),
		// AI 侧对每个请求回显 `X-Trace-Id`（接缝 J3）。
		// 只取这一个头而不是整张 map：转发响应头会引入
		// `Transfer-Encoding`/`Content-Length` 这类必须由本层决定的头，
		// 一不小心就构造出「内容变了但长度没变」的非法响应。
		TraceID: resp.Header.Get("X-Trace-Id"),
	}
	return out, nil
}

// mapTransportError 把「根本没拿到响应」的几种情况翻译成统一错误。
//
// 区分超时与其它失败：超时是可重试的 504，而连接被拒是 503，
// 客户端对这两个码的处理不同（前者退避重试，后者直接提示服务不可用）。
func (p *httpProxy) mapTransportError(ctx context.Context, req biz.AIProxyRequest, err error) error {
	traceID := strings.TrimSpace(req.TraceID)

	var netErr net.Error
	switch {
	case errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &netErr) && netErr.Timeout()):
		return errs.New(errs.CodeAITimeout).
			WithTraceID(traceID).
			WithDetail("path", req.Path).
			WithDetail("reason", "gateway_deadline_exceeded").
			WithCause(err)
	case errors.Is(err, context.Canceled):
		return errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID).
			WithDetail("path", req.Path).
			WithDetail("reason", "client_canceled").
			WithCause(err)
	default:
		p.opt.Log.WarnContext(ctx, "ai_proxy.transport_error",
			"path", req.Path,
			"error", err.Error(),
		)
		return errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID).
			WithDetail("path", req.Path).
			WithDetail("reason", "transport_error").
			WithCause(err)
	}
}

// ---- base url 拼接 ----

// urlBase 把一个 base url 拆成 scheme://host + 前缀，避免用字符串拼接时
// 出现 `http://h//api/v1` 这种双斜杠（某些反向代理会因此 404）。
type urlBase struct {
	schemeHost string
	prefix     string
}

func newURLBase(raw string) *urlBase {
	trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
	if trimmed == "" {
		return &urlBase{}
	}
	idx := strings.Index(trimmed, "://")
	if idx < 0 {
		// 配置漏了 scheme：当作 http:// 处理，而不是产出一个非法 URL
		// 让每个请求都在建连阶段报错（那种错误信息看不出是配置问题）。
		return &urlBase{schemeHost: "http://" + trimmed}
	}
	rest := trimmed[idx+3:]
	slash := strings.Index(rest, "/")
	if slash < 0 {
		return &urlBase{schemeHost: trimmed}
	}
	return &urlBase{schemeHost: trimmed[:idx+3+slash], prefix: rest[slash:]}
}

// join 拼出上游完整 URL。
//
// `path` 是**网关收到的完整路径**（含网关自己的 API 前缀，如
// `/api/v1/knowledge-bases`）。之所以不先剥掉网关前缀再拼，是因为两侧前缀
// 同名（都是 `/api/v1`，docs/04-§7 的前提就是「网关路径与 AI 路径同名」）
// —— 传完整路径让这条前提在代码里只有一处体现，而不是
// 「先剥一层、再猜上游的前缀又是什么」两处。
func (b *urlBase) join(path string) (string, error) {
	if b.schemeHost == "" {
		return "", errors.New("AI_PLATFORM_BASE_URL 未配置")
	}
	if path == "" || path[0] != '/' {
		return "", fmt.Errorf("上游路径必须以 / 开头，得到 %q", path)
	}
	return b.schemeHost + b.prefix + path, nil
}
