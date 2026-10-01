package middleware

import (
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ids"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
)

// HeaderRequestID 是请求追踪头（docs/02-§1）。
const HeaderRequestID = "X-Request-Id"

// maxRequestIDLen 是回显客户端 request id 的长度上限。
//
// 不回显超长/含控制字符的值：它会被写进日志与响应头，
// 长度不限的话等于给了调用方一个日志放大与响应头注入的入口。
const maxRequestIDLen = 128

// WithRequestID 中间件：回显客户端的 X-Request-Id，没有则生成 `req_*`。
//
// 生成的值用于把「客户端看到的一个响应」与「服务端的一批日志」串起来，
// 与 trace_id（跨服务）不同：request_id 只在网关内部有意义。
func WithRequestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		id := sanitiseRequestID(c.GetHeader(HeaderRequestID))
		if id == "" {
			id = ids.NewRequest()
		}
		SetRequestID(c, id)
		// 先写响应头：即使后续 handler 返回错误，客户端仍能拿到它。
		c.Writer.Header().Set(HeaderRequestID, id)
		c.Next()
	}
}

func sanitiseRequestID(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" || len(raw) > maxRequestIDLen {
		return ""
	}
	for _, r := range raw {
		if r < 0x20 || r == 0x7f {
			return ""
		}
	}
	return raw
}

// HeaderTraceID 是跨服务链路 id 的响应头（docs/06-§5.1）。
const HeaderTraceID = "X-Trace-Id"

// WithTrace 中间件：解析 W3C `traceparent`，缺失时新建 trace id。
//
// 格式：`00-<32hex trace-id>-<16hex span-id>-<2hex flags>`。
// 只做「解析 + 生成 + 回写响应头」，OTel 的 span 上报在 M6 接入；
// 但 trace_id 本身从 M1 起就必须稳定存在 —— 否则错误信封里的
// `trace_id` 会是空串，而契约把它标成了必填。
func WithTrace() gin.HandlerFunc {
	return func(c *gin.Context) {
		traceID, spanID := parseTraceparent(c.GetHeader("traceparent"))
		if traceID == "" {
			traceID = newHexID(16)
		}
		SetTraceID(c, traceID)
		SetSpanID(c, spanID)
		c.Writer.Header().Set(HeaderTraceID, traceID)
		c.Header("traceparent", "00-"+traceID+"-"+(firstNonEmpty(spanID, newHexID(8)))+"-01")
		c.Next()
	}
}

// parseTraceparent 按 W3C 规范解析；任何一处不合法都返回空（视为无上游 trace）。
//
// 宽容度很关键：这里有两条容易搞错的地方 ——
//  1. `trace-id` 全 0 是**非法**的（规范明确禁止），必须判掉；
//  2. 版本大于 00 时后续字段可能增多，只取前四段即可。
func parseTraceparent(header string) (traceID, spanID string) {
	header = strings.TrimSpace(header)
	if header == "" {
		return "", ""
	}
	parts := strings.Split(header, "-")
	if len(parts) < 4 {
		return "", ""
	}
	if len(parts[0]) != 2 {
		return "", ""
	}
	tid, sid := strings.ToLower(parts[1]), strings.ToLower(parts[2])
	if len(tid) != 32 || !isHex(tid) || isAllZero(tid) {
		return "", ""
	}
	if len(sid) != 16 || !isHex(sid) || isAllZero(sid) {
		return "", ""
	}
	return tid, sid
}

func isHex(s string) bool {
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9', r >= 'a' && r <= 'f':
		default:
			return false
		}
	}
	return true
}

func isAllZero(s string) bool {
	for _, r := range s {
		if r != '0' {
			return false
		}
	}
	return true
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

// newHexID 生成 n 字节随机数据的十六进制表示。
//
// 用 crypto/rand（ids 包内部）：trace id 会出现在响应头与日志里，
// 可预测的 id 让攻击者可以伪造 traceparent 去污染别人的链路视图。
func newHexID(nBytes int) string {
	return ids.NewHex(nBytes)
}

// ContextFields 把 gin 上下文的字段打包，供 logx 使用。
func ContextFields(c *gin.Context) logx.Fields {
	return logx.Fields{
		TraceID:   TraceID(c),
		SpanID:    SpanID(c),
		RequestID: RequestID(c),
		UserID:    UserID(c),
		Route:     c.FullPath(),
		Method:    c.Request.Method,
	}
}
