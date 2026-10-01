package middleware

import (
	"log/slog"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
)

// AccessLog 中间件：为每个请求注入日志字段并在结束时输出摘要。
//
// 它把 request/trace/user 等字段写进 `c.Request.Context()`，
// 于是**所有**下游（biz、data、httpx.Fail）只要用 `logx.From(ctx, ...)`
// 就能自动带上这些字段 —— 不必手工抄一遍（漏抄一次链路就断了）。
func AccessLog(base *slog.Logger) gin.HandlerFunc {
	if base == nil {
		base = slog.Default()
	}
	return func(c *gin.Context) {
		start := time.Now()
		// 先放 logger 再放字段：两者都进 context，缺一不可。
		// 只放字段的话，httpx.Fail 里的 logx.From(ctx, nil) 会退回
		// slog.Default()（未配置的 stdlib handler）——既丢了 service/version，
		// 输出格式也与其它日志不一致。
		ctx := logx.WithLogger(c.Request.Context(), base)
		c.Request = c.Request.WithContext(logx.NewContext(ctx, ContextFields(c)))

		c.Next()

		log := logx.From(c.Request.Context(), base)
		attrs := []any{
			slog.String("method", c.Request.Method),
			slog.String("route", c.FullPath()),
			slog.Int("status", c.Writer.Status()),
			slog.Int("bytes", c.Writer.Size()),
			slog.Float64("elapsed_ms", float64(time.Since(start).Microseconds())/1000.0),
		}
		// 路由未匹配时 FullPath 为空：把真实路径记下来（已在日志层脱敏/截断），
		// 否则 404 的排查会变成「不知道客户端打了什么」。
		if c.FullPath() == "" {
			attrs = append(attrs, slog.String("path", c.Request.URL.Path))
		}
		if code := httpx.FailedCode(c); code != "" {
			attrs = append(attrs, slog.String("code", string(code)))
		}

		switch status := c.Writer.Status(); {
		case status >= 500:
			log.ErrorContext(c.Request.Context(), "http.request", attrs...)
		case status >= 400:
			log.WarnContext(c.Request.Context(), "http.request", attrs...)
		default:
			log.InfoContext(c.Request.Context(), "http.request", attrs...)
		}
	}
}

// SecurityHeaders 中间件：设置基础安全响应头（docs/06-§4.2）。
func SecurityHeaders() gin.HandlerFunc {
	return func(c *gin.Context) {
		h := c.Writer.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		// 网关只返回 JSON/SSE，不该被任何客户端当成「可嵌入的文档」。
		h.Set("X-Frame-Options", "DENY")
		c.Next()
	}
}

// RealIP 中间件：按可信代理层数解析真实客户端 IP（docs/06-§4.3）。
//
// 取 `X-Forwarded-For` **右起第 N 个**（N = TRUSTED_PROXY_COUNT），
// 而不是第一个：XFF 是「客户端可追加」的头，取首值等于让调用方
// 随便填一个 IP 就绕过按 IP 的限流（AC-NFR-07 就是验这件事）。
//
// 代理层数为 0 时完全忽略 XFF，只用 TCP 对端地址。
func RealIP(trustedProxyCount int) gin.HandlerFunc {
	return func(c *gin.Context) {
		SetClientIP(c, resolveClientIP(c, trustedProxyCount))
		c.Next()
	}
}

func resolveClientIP(c *gin.Context, trustedProxyCount int) string {
	remote := c.Request.RemoteAddr
	if host, _, ok := splitHostPort(remote); ok {
		remote = host
	}
	if trustedProxyCount <= 0 {
		return remote
	}

	xff := c.GetHeader("X-Forwarded-For")
	if xff == "" {
		return remote
	}
	parts := splitAndTrim(xff, ',')
	if len(parts) == 0 {
		return remote
	}
	// 右起第 N 个：右侧是代理链的「深处」，最右侧是离我们最近的代理。
	idx := len(parts) - trustedProxyCount
	if idx < 0 {
		// 实际代理层数少于配置：说明请求没走我们预期的链路（可能是直连），
		// 此时不采信 XFF，用 TCP 对端地址。
		return remote
	}
	candidate := parts[idx]
	if candidate == "" {
		return remote
	}
	return candidate
}

func splitHostPort(addr string) (string, string, bool) {
	for i := len(addr) - 1; i >= 0; i-- {
		if addr[i] == ':' {
			return addr[:i], addr[i+1:], true
		}
	}
	return addr, "", false
}

func splitAndTrim(s string, sep rune) []string {
	var out []string
	cur := make([]rune, 0, 16)
	flush := func() {
		if len(cur) == 0 {
			return
		}
		out = append(out, trimSpaces(string(cur)))
		cur = cur[:0]
	}
	for _, r := range s {
		if r == sep {
			flush()
			continue
		}
		cur = append(cur, r)
	}
	flush()
	return out
}

func trimSpaces(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t') {
		end--
	}
	return s[start:end]
}
