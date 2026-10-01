package middleware

import (
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
)

// CORS 中间件：按显式白名单放行跨域请求（docs/06-§4.2）。
// 白名单为空时不加任何 CORS 头（等价于同源）：这样「配置漏了」的表现是浏览器拒绝，
// 而不是服务端默默用 `*` 放行所有站点。prod 下配置成 `*` 会在启动校验时直接失败（AC-NFR-10）。
func CORS(allowedOrigins []string) gin.HandlerFunc {
	allowAll := false
	set := make(map[string]struct{}, len(allowedOrigins))
	for _, o := range allowedOrigins {
		o = strings.TrimSpace(o)
		if o == "" {
			continue
		}
		if o == "*" {
			allowAll = true
		}
		set[o] = struct{}{}
	}

	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		if origin == "" || (!allowAll && len(set) == 0) {
			c.Next()
			return
		}
		_, ok := set[origin]
		if !allowAll && !ok {
			// 不在白名单：不加 CORS 头，让浏览器自行拒绝（不回 403，
			// 因为同源请求不该被跨域策略影响）。
			c.Next()
			return
		}

		h := c.Writer.Header()
		if allowAll {
			h.Set("Access-Control-Allow-Origin", "*")
		} else {
			h.Set("Access-Control-Allow-Origin", origin)
			// 只有回显具体 Origin 时才能配 credentials（`*` 与它互斥）。
			h.Set("Access-Control-Allow-Credentials", "true")
			h.Add("Vary", "Origin")
		}
		h.Set("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
		h.Set("Access-Control-Allow-Headers",
			"Authorization, Content-Type, Idempotency-Key, X-Request-Id, traceparent")
		h.Set("Access-Control-Expose-Headers", "X-Request-Id, X-Trace-Id, Retry-After")
		h.Set("Access-Control-Max-Age", strconv.Itoa(600))

		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}

// BodyLimit 中间件：用 http.MaxBytesReader 限制请求体（docs/06-§4.2）。
//
// MUST 在读取之前包上：先读进内存再判断大小的话，「请求体过大」这件事已经消耗掉等量内存了。
//
// `multipart/form-data` 例外：JSON 上限（`MAX_JSON_BODY_MB`，默认 1MB）与上传上限
// （`UPLOAD_MAX_MB`，默认 50MB）差一个数量级，而这一层跑在路由匹配之前拿不到「这是哪个接口」。
// 用 Content-Type 区分是可靠的（请求会不会被当成 JSON 解析只取决于它），
// 而且 `MaxBytesReader` 一旦套上就无法在 handler 里摘掉（返回的是不可解包的私有 reader）。
//
// 例外带来的敞口由上传接口自己关上：`UploadHandler` 先用 `Content-Length` 预检、
// 再用计数 reader 兜一次（声明撒谎 / chunked），所以 multipart 的上限是「由知道上限的那一层管」。
func BodyLimit(maxBytes int64) gin.HandlerFunc {
	return func(c *gin.Context) {
		if maxBytes > 0 && c.Request.Body != nil && !isMultipart(c) {
			c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxBytes)
		}
		c.Next()
	}
}

// isMultipart 判断请求是否是 multipart（大小写不敏感，允许带 boundary 参数）。
func isMultipart(c *gin.Context) bool {
	ct := strings.TrimSpace(strings.ToLower(c.GetHeader("Content-Type")))
	return strings.HasPrefix(ct, "multipart/")
}
