// Package middleware 提供 Gin 中间件。
//
// 顺序（在 server/router.go 里装配）：Recovery → RequestID → Trace → Logger
// → Security → CORS → BodyLimit → Auth（按路由组）→ Handler。
// 顺序有语义：Recovery 最外层保证 panic 也能被记成结构化日志；RequestID/Trace 在 Logger
// 之前，日志才能带上这两个字段；BodyLimit 在 Auth 之前，未认证的超大请求不该消耗验签成本。
package middleware

import (
	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
)

// gin.Context 中的键名。集中定义避免各处拼写不一致。
const (
	keyUserID    = "gw.user_id"
	keyRequestID = "gw.request_id"
	keyTraceID   = "gw.trace_id"
	keySpanID    = "gw.span_id"
	keyClaims    = "gw.claims"
	keyClientIP  = "gw.client_ip"
	keyRawToken  = "gw.raw_token"
)

// UserID 返回当前请求的用户 ID（未认证时为空串）。
func UserID(c *gin.Context) string {
	if v, ok := c.Get(keyUserID); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetUserID 写入用户 ID（由鉴权中间件调用）。
func SetUserID(c *gin.Context, userID string) { c.Set(keyUserID, userID) }

// Claims 返回当前请求的 JWT 载荷（未认证时为 nil）。
func Claims(c *gin.Context) *jwtx.Claims {
	if v, ok := c.Get(keyClaims); ok {
		if claims, ok := v.(*jwtx.Claims); ok {
			return claims
		}
	}
	return nil
}

// SetClaims 写入 JWT 载荷。
func SetClaims(c *gin.Context, claims *jwtx.Claims) { c.Set(keyClaims, claims) }

// RequestID 返回本次请求的 X-Request-Id。
func RequestID(c *gin.Context) string {
	if v, ok := c.Get(keyRequestID); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetRequestID 写入 X-Request-Id。
func SetRequestID(c *gin.Context, id string) { c.Set(keyRequestID, id) }

// TraceID 返回本次请求的 trace id（W3C trace context）。
func TraceID(c *gin.Context) string {
	if v, ok := c.Get(keyTraceID); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetTraceID 写入 trace id。
func SetTraceID(c *gin.Context, id string) { c.Set(keyTraceID, id) }

// SpanID 返回当前 span id。
func SpanID(c *gin.Context) string {
	if v, ok := c.Get(keySpanID); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetSpanID 写入 span id。
func SetSpanID(c *gin.Context, id string) { c.Set(keySpanID, id) }

// ClientIP 返回按可信代理层数解析出的真实客户端 IP。
// 不用 `c.ClientIP()`：Gin 默认信任全部 XFF，伪造 `X-Forwarded-For` 就能绕过按 IP 的限流（AC-NFR-07）。
func ClientIP(c *gin.Context) string {
	if v, ok := c.Get(keyClientIP); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetClientIP 写入解析后的客户端 IP。
func SetClientIP(c *gin.Context, ip string) { c.Set(keyClientIP, ip) }

// RawToken 返回当前请求的**原始** Bearer token（未认证时为空串）。
//
// 存在的唯一理由：网关调用 ai-platform 时要把调用方的凭据原样转交
// （接缝 J1，docs/04-§3.2）—— AI 侧自己校验 JWT，网关不替它决定身份。
//
// 把它显式存下来（而不是在需要处重新从 header 里取一遍）是为了让
// 「谁用过原始 token」在代码里可搜：只有透传与编排两条路径可以读它，
// 于是 docs/04-§3.2 明令禁止的另一条路 ——「网关自造 `X-User-Id` 让 AI 相信」
// ——变成一条能靠 grep 否证的事。
//
// MUST NOT 写进日志或任何持久化字段。
func RawToken(c *gin.Context) string {
	if v, ok := c.Get(keyRawToken); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// SetRawToken 记录原始 Bearer token（由鉴权中间件在验签通过后写入）。
//
// 只在验签**通过后**写入：认证失败的请求不该让下游还能拿到 token。
func SetRawToken(c *gin.Context, token string) { c.Set(keyRawToken, token) }
