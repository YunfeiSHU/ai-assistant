// Package server 装配 HTTP 引擎与生命周期（代码生成规范.md §三.5）。
//
// 三块职责：
//
//	http.go       路由装配（本文件）
//	server.go     Server 包装与优雅退出
//	middleware/   中间件（trace / 鉴权 / 恢复 / 访问日志 / CORS…）
//
// MUST NOT 出现业务逻辑：这里只决定「哪个 handler 挂在哪个路径上」。
package server

import (
	"log/slog"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/service"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/metricsx"
)

// Deps 是路由装配所需的全部依赖。
type Deps struct {
	Config         *conf.Config
	Log            *slog.Logger
	Signer         *jwtx.Signer
	VersionChecker biz.TokenVersionChecker

	Auth   *service.AuthHandler
	User   *service.UserHandler
	Health *service.HealthHandler
	// Quota 提供 `GET /me/quota` 与 `GET /me/usage`（M5）。
	Quota *service.QuotaHandler
	// Upload 转发文档上传（流式，不经内存；M5）。
	Upload *service.UploadHandler

	// RateLimit 是限流服务（M5）。为 nil 时不挂任何限流中间件 ——
	// 但降级与打点仍在 biz/data 内部，因而不存在「半套限流」。
	RateLimit *biz.RateLimitService
	// Metrics 是 Prometheus 指标集（M6）。为 nil 时指标中间件退化为 no-op。
	Metrics *metricsx.Metrics
	// Trace 是 OTel Server span 的配置（M6）。Provider 为 nil 表示 OTEL 未启用。
	Trace middleware.TraceOptions

	Conversations *service.ConversationHandler
	Messages      *service.MessageHandler
	// Proxy 负责对 ai-platform 的透传（KB / 文档 / 检索 / 任务 / 上下文与摘要）。
	// 为 nil 时不注册这些路由（保持「依赖缺失就不挂路由」的一贯做法）。
	Proxy *service.ProxyHandler

	// IdemStore 是幂等记录仓储；为 nil 时两个写接口退化为「不做幂等」。
	IdemStore biz.IdempotencyStore

	// Extra 是后续阶段（编排、配额…）注册路由的钩子。
	//
	// 参数依次为：带 /api/v1 前缀的公开组、需要鉴权的组、以及关闭 CORS
	// 与 BodyLimit 的「裸」组（SSE 与上传需要）。
	Extra func(public, authed, raw *gin.RouterGroup)
}

// NewEngine 组装 Gin 引擎。
func NewEngine(d Deps) *gin.Engine {
	if d.Config.IsProd() {
		gin.SetMode(gin.ReleaseMode)
	} else if d.Config.App.Env == "local" || d.Config.App.Env == "dev" {
		gin.SetMode(gin.DebugMode)
	}

	// 用 gin.New 而不是 gin.Default：后者的 Logger 会往 stdout 打非结构化文本，
	// 与 slog 的 JSON 流混在一起（采集侧会解析失败）。
	engine := gin.New()

	// 关闭尾斜杠重定向：契约里的路径是精确的（`/conversations` vs `/conversations/`），
	// 自动 301 会让客户端的 POST 变成 GET（重定向不保留方法），症状极难排查。
	engine.RedirectTrailingSlash = false
	engine.RedirectFixedPath = false
	// 不启用 405：契约里没有 405 错误码，统一归到 404 更一致。
	engine.HandleMethodNotAllowed = false

	engine.Use(middleware.Recovery(d.Log))
	engine.Use(middleware.WithRequestID())
	engine.Use(middleware.WithTrace())
	// OTel span 必须紧跟在 WithTrace 之后：它把 WithTrace 解析出的
	// trace_id/span_id **反向构造**成远程父上下文（而不是自己再解析一遍
	// traceparent），否则两处对「非法 traceparent」的宽容度差异会让
	// 响应头 X-Trace-Id 与 Jaeger 里的 trace_id 对不上（S8 明确断言两者一致）。
	engine.Use(middleware.Trace(d.Trace))
	// 指标放在 Trace 之后：route 标签要用 `c.FullPath()`，取值时机与 span 相同。
	engine.Use(middleware.Metrics(d.Metrics))
	engine.Use(middleware.RealIP(d.Config.App.TrustedProxyCount))
	engine.Use(middleware.AccessLog(d.Log))
	engine.Use(middleware.SecurityHeaders())
	engine.Use(middleware.CORS(d.Config.App.CORSAllowedOrigins))
	engine.Use(middleware.BodyLimit(int64(d.Config.App.MaxJSONBodyMB) * 1024 * 1024))

	// 幂等键头（docs/02-§7）：只做「提取并放进 context」，回放逻辑由写接口按需挂。
	//
	// MUST 在**任何 `Group()` 调用之前**注册。gin 的 `RouterGroup` 在
	// `Group()` 的那一刻就把当前的中间件链**拷贝**走了（`combineHandlers`），
	// 之后再 `engine.Use(...)` 不会补到已存在的组上 —— 于是
	// 「幂等键提取中间件注册了」与「/api/v1 下的路由拿不到键」同时成立：
	// 幂等**永远不生效**，且没有任何报错（`Idempotency` 见 key 为空即放行）。
	engine.Use(IdempotencyKey())

	// 健康检查同时挂在根路径与版本前缀下：
	// 根路径供 LB/K8s 探活（不随 API 版本变化），前缀下的供契约测试统一访问。
	if d.Health != nil {
		registerHealth(engine.Group("/health"), d.Health)
	}

	api := engine.Group(d.Config.App.APIPrefix)
	if d.Health != nil {
		registerHealth(api.Group("/health"), d.Health)
	}

	registerPublic(api, d)
	authed := api.Group("", middleware.Auth(d.Signer, d.VersionChecker, d.Metrics))
	registerAuthed(authed, d)

	if d.Extra != nil {
		d.Extra(api, authed, engine.Group(""))
	}

	engine.NoRoute(func(c *gin.Context) {
		httpx.Fail(c, errs.New(errs.CodeResourceNotFound).
			WithDetail("path", c.Request.URL.Path).
			WithDetail("method", c.Request.Method))
	})

	// 路由注册全部完成后再预热指标：此时 `engine.Routes()` 是**最终**路由表。
	//
	// 放在 `NoRoute` 之后而不是 `NewEngine` 开头，是因为预热要以「注册结果」
	// 为准而不是以「我们以为注册了什么」为准 —— 前者能发现漏注册
	// （面板上少一条曲线的成因里，这个最难查）。
	d.Metrics.WarmRoutes(routeTemplates(engine))
	return engine
}

// routeTemplates 抽出 gin 注册表里的路由模板（去重，保持稳定顺序）。
//
// 只取 `FullPath` 模板、不取真实路径：真实路径进标签就是高基数
// （docs/06-§5.2 明令禁止），而这里的输入直接来自 gin 的注册表，
// 天然就是模板形式。
func routeTemplates(engine *gin.Engine) []string {
	routes := engine.Routes()
	seen := make(map[string]struct{}, len(routes))
	out := make([]string, 0, len(routes))
	for _, r := range routes {
		if r.Path == "" {
			continue
		}
		if _, ok := seen[r.Path]; ok {
			continue
		}
		seen[r.Path] = struct{}{}
		out = append(out, r.Path)
	}
	return out
}

func registerHealth(g *gin.RouterGroup, h *service.HealthHandler) {
	g.GET("/live", h.Live)
	g.GET("/ready", h.Ready)
	g.GET("", h.Health)
}

// registerPublic 注册免鉴权路由（docs/02-§1.1 白名单）。
func registerPublic(api *gin.RouterGroup, d Deps) {
	if d.Auth == nil {
		return
	}
	auth := api.Group("/auth")
	auth.POST("/register", d.Auth.Register)
	// 登录两级限流（docs/02-§5.3）：单 IP 10/分钟 + 单账号 20/小时。
	//
	// 两个维度**都要**：只按 IP 挡不住分布式撞库（换 IP 很便宜），
	// 只按账号会让攻击者用「一个 IP 扫很多账号」把每个账号的计数分开累加。
	// 顺序是先 IP 后账号：IP 那一级便宜（不读 body），能拦的先拦。
	if d.RateLimit != nil {
		auth.POST("/login",
			middleware.RateLimitByIP(d.RateLimit, biz.RateScopeLoginIP),
			middleware.RateLimitLoginAccount(d.RateLimit),
			d.Auth.Login)
	} else {
		auth.POST("/login", d.Auth.Login)
	}
	auth.POST("/refresh", d.Auth.Refresh)
}

// registerAuthed 注册需要鉴权的路由。
func registerAuthed(authed *gin.RouterGroup, d Deps) {
	if d.Auth != nil {
		auth := authed.Group("/auth")
		auth.POST("/logout", d.Auth.Logout)
		auth.POST("/password", d.Auth.ChangePassword)
	}
	if d.User != nil {
		authed.GET("/me", d.User.Me)
		authed.PATCH("/me", d.User.UpdateMe)
	}
	if d.Quota != nil {
		// 配额查询（REQ-AUTH-007）。
		authed.GET("/me/quota", d.Quota.Quota)
		authed.GET("/me/usage", d.Quota.Usage)
	}
	registerConversations(authed, d)
	registerMessages(authed, d)
	registerPassthrough(authed, d)
}

// registerConversations 注册会话 CRUD（docs/03-§2.2）。
func registerConversations(authed *gin.RouterGroup, d Deps) {
	if d.Conversations == nil {
		return
	}
	// 幂等只挂在「创建会话」上：读接口天然幂等，归档/删除是幂等的
	// 状态转移，给它们记一份响应摘要只会白白撑大 idempotency_record。
	create := []gin.HandlerFunc{Idempotency(d.IdemStore, d.Log), d.Conversations.Create}
	if d.RateLimit != nil {
		// 会话创建限流（单用户 30/分钟，docs/02-§5.3）。
		// 插在幂等之后：重放同一个 `Idempotency-Key` 不应该再消耗一次名额。
		create = []gin.HandlerFunc{
			Idempotency(d.IdemStore, d.Log),
			middleware.RateLimitByUser(d.RateLimit, biz.RateScopeConversation),
			d.Conversations.Create,
		}
	}

	convs := authed.Group("/conversations")
	convs.POST("", create...)
	convs.GET("", d.Conversations.List)
	convs.GET("/:conversation_id", d.Conversations.Get)
	convs.PATCH("/:conversation_id", d.Conversations.Update)
	convs.POST("/:conversation_id/archive", d.Conversations.Archive)
	convs.POST("/:conversation_id/unarchive", d.Conversations.Unarchive)
	convs.DELETE("/:conversation_id", d.Conversations.Delete)
}

// registerMessages 注册消息台账接口（docs/03-§4.3）。
//
// 暂时未注册的还有任务的 `.../events`（SSE，M4 之后的阶段）。
//
// 流式接口 `POST .../messages/stream` 挂在**鉴权组**下而不是 `Deps.Extra` 的
// `raw` 组（那个组刻意关掉了 CORS 与 BodyLimit）：
//
//   - `BodyLimit` 只包请求体（`http.MaxBytesReader`），而流式请求体是一小段 JSON，
//     所以它在这里没有坏处 —— 需要 `raw` 的是**上传**（请求体可能几百 MB）；
//   - `CORS` 反而是**必需**的：浏览器的 `EventSource` 受同源策略约束，
//     少了 CORS 头会让跨域的前端直接连不上（而 `raw` 组没有这个中间件）；
//   - `raw` 组是 `engine.Group("")`，没有 `/api/v1` 前缀也没有鉴权中间件，
//     用它就得在调用点手工补两样东西 —— 而这两样正好是这里的两个需求。
//
// 幂等中间件**不挂**在这个路由上：重放一个已经推了一半的流，客户端会把
// 正文再拼一遍（docs/02-§7 的适用范围不含流式接口）。`Idempotency-Key`
// 头即使带了也不会生效。
func registerMessages(authed *gin.RouterGroup, d Deps) {
	if d.Messages == nil {
		return
	}
	send := []gin.HandlerFunc{Idempotency(d.IdemStore, d.Log), d.Messages.Send}
	if d.RateLimit != nil {
		// 发消息限流（单用户 20/分钟，docs/02-§5.3）。
		send = []gin.HandlerFunc{
			Idempotency(d.IdemStore, d.Log),
			middleware.RateLimitByUser(d.RateLimit, biz.RateScopeMessage),
			d.Messages.Send,
		}
	}

	convs := authed.Group("/conversations")
	convs.GET("/:conversation_id/messages", d.Messages.List)
	convs.POST("/:conversation_id/messages", send...)
	// 流式发消息一直挂在鉴权组下（见上面的注释）：CORS 必须要有。
	// 限流同样要：流式请求是最贵的一种，限流器不能因为它不走 `send`
	// 就被绕过。
	stream := []gin.HandlerFunc{d.Messages.Stream}
	if d.RateLimit != nil {
		stream = []gin.HandlerFunc{
			middleware.RateLimitByUser(d.RateLimit, biz.RateScopeMessage),
			d.Messages.Stream,
		}
	}
	convs.POST("/:conversation_id/messages/stream", stream...)

	msgs := authed.Group("/messages")
	msgs.GET("/:message_id", d.Messages.Get)
	msgs.DELETE("/:message_id", d.Messages.Delete)
}

// registerPassthrough 注册对 ai-platform 的透传路由（docs/04-§7）。
//
// 三条通用规则：
//
//  1. **路径同名**：网关把**收到的原始路径**原样转给 AI（见 service.ProxyHandler.upstreamPath），
//     所以这里注册的路径必须与 AI 的路径一致（AI 侧的权威定义是它的 OpenAPI）。
//     不一致时症状是「网关 404」（AI 的 NoRoute）或「转发到了另一个接口」，
//     两种都不会静默成功，所以同名是可以验收的性质。
//  2. **超时按档位**：元数据类 8s / 非流式对话 70s / 上传建任务 120s（docs/04-§3.3），
//     全部走 `middleware.AIProxyTimeout*`，不在每个路由上写裸时长。
//  3. **只在会话资源上校验归属**：其余资源的属主由 AI 保证（其 `REQ-RAG-011`
//     要求跨用户 404），网关重复校验只会多一次查库。
func registerPassthrough(authed *gin.RouterGroup, d Deps) {
	if d.Proxy == nil {
		return
	}
	meta := func(owned bool) gin.HandlerFunc {
		return d.Proxy.Forward(biz.AIProxyTimeoutMeta, owned)
	}

	// 会话上下文与摘要（docs/04-§7 的「唯一例外」：会话属于网关台账，必须校验归属）。
	convs := authed.Group("/conversations")
	convs.GET("/:conversation_id/context", meta(true))
	convs.DELETE("/:conversation_id/context", meta(true))
	convs.GET("/:conversation_id/summary", meta(true))
	convs.POST("/:conversation_id/summary/rebuild", meta(true))

	// 知识库。
	kbs := authed.Group("/knowledge-bases")
	kbs.POST("", meta(false))
	kbs.GET("", meta(false))
	kbs.GET("/:kb_id", meta(false))
	kbs.PATCH("/:kb_id", meta(false))
	kbs.DELETE("/:kb_id", meta(false))
	kbs.GET("/:kb_id/documents", meta(false))
	kbs.POST("/:kb_id/search", meta(false))

	// 文档（不含上传与下载：上传需要不经 BodyLimit 的裸组，下载要读 presigned URL
	// 再 302，两者都属于 M5 的 docs/04-§8）。
	docs := authed.Group("/documents")
	docs.GET("/:doc_id", meta(false))
	docs.GET("/:doc_id/chunks", meta(false))
	docs.DELETE("/:doc_id", meta(false))

	// 文档上传（M5）：`POST /knowledge-bases/{kb_id}/documents`。
	//
	// 挂在**鉴权组**而不是 `Deps.Extra` 的 `raw` 组：上传必须鉴权，
	// 而 `raw` 组是 `engine.Group("")`，既没有 `/api/v1` 前缀也没有鉴权。
	// 「大于 JSON 上限的 body」这件事已经在 `middleware.BodyLimit` 里按
	// Content-Type 排除掉了（multipart 不走 MaxBytesReader），
	// 因此不需要为上传单开一组。
	//
	// 幂等回放中间件**不挂**在这里：docs/02-§7 要求上传把 `Idempotency-Key`
	// **透传给 AI**，由 AI 返回同一个 `task_id`；网关自己回放一份 202 快照
	// 反而会掩盖「AI 并没有真的建过这个任务」（快照里有 task_id，任务却不存在）。
	if d.Upload != nil {
		kbs := authed.Group("/knowledge-bases")
		upload := []gin.HandlerFunc{d.Upload.Upload}
		if d.RateLimit != nil {
			upload = []gin.HandlerFunc{
				middleware.RateLimitByUser(d.RateLimit, biz.RateScopeUpload),
				d.Upload.Upload,
			}
		}
		kbs.POST("/:kb_id/documents", upload...)
	}

	// 记忆（`/memories*`、`/memory-settings`）。
	mem := authed.Group("/memories")
	mem.GET("", meta(false))
	mem.POST("", meta(false))
	mem.DELETE("", meta(false))
	mem.GET("/:mem_id", meta(false))
	mem.PATCH("/:mem_id", meta(false))
	mem.DELETE("/:mem_id", meta(false))
	authed.GET("/memory-settings", meta(false))
	authed.PUT("/memory-settings", meta(false))

	// 工具（`invoke` 在 `prod` 下由 AI 返回 404，网关不额外处理）。
	authed.GET("/tools", meta(false))
	authed.POST("/tools/:name/invoke", meta(false))

	// MCP。
	mcp := authed.Group("/mcp/servers")
	mcp.GET("", meta(false))
	mcp.GET("/:name/tools", meta(false))
	mcp.POST("/:name/reload", meta(false))

	// 任务（`GET /tasks` 的聚合视图由 AI 提供，网关**不建任务副本表**：REQ-ORCH-007）。
	tasks := authed.Group("/tasks")
	tasks.GET("", meta(false))
	tasks.GET("/:task_id", meta(false))
	tasks.POST("/:task_id/cancel", meta(false))
	tasks.POST("/:task_id/retry", meta(false))

	// 模型列表：对话功能的一部分（前端要拿它填模型下拉框），没有任何本土逻辑。
	authed.GET("/models", meta(false))
}
