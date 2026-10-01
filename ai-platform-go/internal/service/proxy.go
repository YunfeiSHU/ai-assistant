package service

import (
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// ProxyHandler 把 KB / 文档 / 检索 / 任务 / 上下文与摘要透传给 ai-platform
// （docs/04-§7）。
//
// 整个 handler 只有一个 `Forward`：透传接口在网关侧只有四件事
// （鉴权 → 归属校验 → 透传 → 原样返回），**没有任何按接口分支的业务逻辑**。
// 所以「一个接口一个方法」在这里不是清晰，而是 19 份必须同步修改的重复代码 ——
// 那正是 docs/04-§7 用一句「MUST NOT 添加业务逻辑」想避免的东西。
type ProxyHandler struct{ svc *biz.AIProxyService }

// NewProxyHandler 构造 handler。
func NewProxyHandler(svc *biz.AIProxyService) *ProxyHandler {
	return &ProxyHandler{svc: svc}
}

// Forward 返回一个把当前请求原样转发的 gin handler。
//
// class 决定用哪一档网关超时（docs/04-§3.3）；ownedConversation 为 true 时
// 先校验路径里的 `conversation_id` 属于当前用户 —— docs/04-§7 里网关侧
// **唯一**需要做归属校验的一类资源（会话台账本来就属于网关）。
func (h *ProxyHandler) Forward(class biz.AIProxyTimeoutClass, ownedConversation bool) gin.HandlerFunc {
	return func(c *gin.Context) {
		in := biz.ProxyInput{
			Method:        c.Request.Method,
			Path:          upstreamPath(c),
			ContentType:   c.GetHeader("Content-Type"),
			UserToken:     middleware.RawToken(c),
			TraceID:       middleware.TraceID(c),
			Class:         class,
			ContentLength: 0,
		}
		if ownedConversation {
			in.OwnedConversationID = c.Param("conversation_id")
		}
		// 只有可能带 body 的方法才转交 body。GET/HEAD 带一个非 nil 的
		// `http.NoBody` 会让上游收到 `Transfer-Encoding: chunked`，
		// 某些框架对它比对 `Content-Length: 0` 严格（FastAPI 不会，但没必要赌）。
		if hasRequestBody(c.Request.Method) {
			in.Body = c.Request.Body
			in.ContentLength = c.Request.ContentLength
		}

		resp, err := h.svc.Forward(c.Request.Context(), middleware.UserID(c), in)
		if err != nil {
			httpx.Fail(c, err)
			return
		}

		// 原样写回：状态码、Content-Type、字节。**不重新编码**是
		// docs/02-§4.2 规则 1/2/3（保留 code / message / details / trace_id 原值）
		// 最省事也最可靠的做法 —— 重新序列化是「把数字变成 float64」
		// 「把 trace_id 换成自己的 request id」这类事故的唯一来源。
		if ct := resp.ContentType; ct != "" {
			c.Header("Content-Type", ct)
		}
		if resp.RetryAfter != "" {
			// 透传上游的重试建议（429/503 才会带）。
			c.Header("Retry-After", resp.RetryAfter)
		}
		c.Status(resp.Status)
		if len(resp.Body) > 0 {
			if _, werr := c.Writer.Write(resp.Body); werr != nil {
				// 写失败只可能是客户端断了：没有可返回的状态码了，
				// 记在 access log 的 status/bytes 里即可。
				_ = c.Error(werr)
			}
		}
	}
}

// upstreamPath 拼出交给 AI 的路径。
//
// 用 `c.Request.URL.Path`（**收到的原始路径**，含网关自己的 `/api/v1`）
// 而不是把 gin 的 `FullPath()` 模板再填一遍参数：模板填空要处理 URL 编码
// （参数里的 `%2F` 会被 gin 解开），而原始路径天然是客户端发来的字节。
//
// query 用 `RawQuery` 而不是 `Query()`：后者会把 `?a=1&a=2` 折叠成单值、
// 重新编码 `%20`，而 AI 的筛选参数（`type` / `status` / `cursor`）不该被网关重写。
func upstreamPath(c *gin.Context) string {
	path := c.Request.URL.Path
	if raw := c.Request.URL.RawQuery; raw != "" {
		path += "?" + raw
	}
	return path
}

// hasRequestBody 报告该方法是否可能带请求体。
func hasRequestBody(method string) bool {
	switch strings.ToUpper(method) {
	case "POST", "PUT", "PATCH", "DELETE":
		return true
	default:
		return false
	}
}
