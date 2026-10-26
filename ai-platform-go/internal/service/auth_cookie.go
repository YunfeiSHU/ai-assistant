package service

import (
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

const (
	// refreshCookieName 是浏览器持有的刷新令牌 Cookie。
	// 它只发给 /api/v1/auth/*，不随普通 API 请求发送。
	refreshCookieName = "ai_refresh"
	refreshCookiePath = "/api/v1/auth"
	// refreshCookieFallbackTTL 只在 AuthService 的 TTL 配置异常（<=0）时兜底。
	refreshCookieFallbackTTL = 30 * 24 * time.Hour
)

// setRefreshCookie 把刷新令牌写入 HttpOnly Cookie。
//
// 不返回给 JS、不放 localStorage：XSS 即使能执行脚本，也读不到这个值。
// SameSite=Lax 阻止跨站 POST 携带；生产 HTTPS 由 TLS 或反代 X-Forwarded-Proto 判定 Secure。
func (h *AuthHandler) setRefreshCookie(c *gin.Context, token string) {
	if token == "" {
		return
	}
	ttl := h.svc.RefreshTTL()
	if ttl <= 0 {
		ttl = refreshCookieFallbackTTL
	}
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     refreshCookieName,
		Value:    token,
		Path:     refreshCookiePath,
		MaxAge:   int(ttl.Seconds()),
		Expires:  time.Now().Add(ttl),
		HttpOnly: true,
		Secure:   secureRequest(c),
		SameSite: http.SameSiteLaxMode,
	})
}

func (h *AuthHandler) clearRefreshCookie(c *gin.Context) {
	http.SetCookie(c.Writer, &http.Cookie{
		Name:     refreshCookieName,
		Value:    "",
		Path:     refreshCookiePath,
		MaxAge:   -1,
		Expires:  time.Unix(1, 0),
		HttpOnly: true,
		Secure:   secureRequest(c),
		SameSite: http.SameSiteLaxMode,
	})
}

func refreshCookieValue(c *gin.Context) string {
	value, err := c.Cookie(refreshCookieName)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(value)
}

// sameSiteRequest 用 Sec-Fetch-Site 做额外的 CSRF 防线：
// SameSite=Lax 已阻止跨站 POST 携带 Cookie，这里再拒绝浏览器明确标记为跨站的请求。
// 非浏览器客户端没有该头（空）时放行，避免破坏 CLI/服务端调用。
func sameSiteRequest(c *gin.Context) bool {
	switch strings.ToLower(strings.TrimSpace(c.GetHeader("Sec-Fetch-Site"))) {
	case "", "same-origin", "same-site", "none":
		return true
	default:
		return false
	}
}

// secureRequest 在本地 HTTP 下不设 Secure（否则浏览器不会回传），
// 生产 HTTPS 直连或反向代理标记 X-Forwarded-Proto=https 时设为 Secure。
func secureRequest(c *gin.Context) bool {
	if c.Request.TLS != nil {
		return true
	}
	return strings.EqualFold(c.GetHeader("X-Forwarded-Proto"), "https")
}
