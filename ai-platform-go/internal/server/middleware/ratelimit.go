package middleware

import (
	"bytes"
	"encoding/json"
	"io"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// RateLimit 按 scope 对请求限流。
//
// `idOf` 决定「按什么维度计数」：登录按 IP、消息按用户。
// 把它做成函数而不是在中间件里写 switch，是因为同一条路由**可能要挂两次**
// （登录同时按 IP 与账号限流），每次用不同的维度函数。
//
// 失败语义：返回值为 nil 表示放行。未鉴权时 `UserID(c)` 为空 ——
// 此时**按 IP 兜底**而不是放行：空 id 会让所有匿名请求共用一个计数器，
// 一旦有一台机器刷，全部匿名请求都会被拒（比「不限制」更坏）。
func RateLimit(svc *biz.RateLimitService, scope string, idOf func(*gin.Context) string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if svc == nil {
			c.Next()
			return
		}
		id := idOf(c)
		if id == "" {
			id = ClientIP(c)
		}
		if err := svc.Allow(c.Request.Context(), scope, id); err != nil {
			httpx.Fail(c, err)
			c.Abort()
			return
		}
		c.Next()
	}
}

// RateLimitByUser 按当前用户限流（未鉴权时回落到 IP）。
func RateLimitByUser(svc *biz.RateLimitService, scope string) gin.HandlerFunc {
	return RateLimit(svc, scope, func(c *gin.Context) string { return UserID(c) })
}

// RateLimitByIP 按客户端 IP 限流（登录尝试用）。
func RateLimitByIP(svc *biz.RateLimitService, scope string) gin.HandlerFunc {
	return RateLimit(svc, scope, func(c *gin.Context) string { return ClientIP(c) })
}

// RateLimitLoginAccount 按「登录请求体里的邮箱」限流（docs/02-§5.3 的第二级阈值）。
//
// 为什么需要这一级：撞库者换 IP 的成本远低于换账号名，
// 只按 IP 限流等于给分布式撞库开门。
//
// **邮箱不能直接作为计数标识**（实测踩过）：Redis 的 Key 片段有字符集白名单
// （`^[A-Za-z0-9_.-]{1,128}$`，`internal/data/redis/keys.go`），
// 而邮箱含 `@` —— 于是每一次限流判定都以 `Redis Key 片段包含非法字符` 失败，
// 被降级包装吞掉后静默改用**进程内**计数（日志里只有一行 WARN）。
// 多实例部署时这等于每个实例各给一份名额，撞库防线形同虚设。
//
// 因此这里先归一化（大小写/空白）再取 sha256 前 16 位十六进制：
//   - 落在安全字符集内，键永远合法；
//   - 不可逆，`SCAN gw:rate:*` 也拿不到邮箱（PII 不进 Redis 键名）；
//   - 同一邮箱的不同大小写写法仍然共用一个桶（与登录比对的口径一致）。
//
// 实现上的难点在于**位置**：邮箱在请求体里，而中间件跑在 handler 之前。
// 这里把 body 读出来、解析出 `email`，再把内容塞回 `c.Request.Body`
// （并恢复 `GetBody`），后续 `httpx.BindJSON` 才能读到完整内容。
// 读入量由上游的 `BodyLimit` 限死（JSON 接口 ≤ `MaxJSONBodyMB`），
// 因此不存在「读爆内存」的风险 —— 这也是本中间件**只许挂在 JSON 接口上**的原因。
//
// 解析失败一律放行：真正的参数错误由 handler 报（那里能带上 `fields[]`，
// 比「限流不通过」可诊断得多）。
func RateLimitLoginAccount(svc *biz.RateLimitService) gin.HandlerFunc {
	return func(c *gin.Context) {
		if svc == nil {
			c.Next()
			return
		}
		email := peekJSONField(c, "email")
		if email == "" {
			c.Next()
			return
		}
		if err := svc.Allow(c.Request.Context(), biz.RateScopeLoginAccount, accountBucketKey(email)); err != nil {
			httpx.Fail(c, err)
			c.Abort()
			return
		}
		c.Next()
	}
}

// accountBucketKey 把登录邮箱映射成安全的计数标识（见 RateLimitLoginAccount 的注释）。
//
// 用 `cryptox.SHA256Hex` 而不是自己拼：它的口径与 `refresh_token.token_hash`
// 一致（小写十六进制、64 位），系统里只需要记住一种哈希写法。
//
// **不要再加 `acct:` 之类的前缀**：那个冒号同样会撞上 Key 片段白名单 ——
// 命名空间已经由 scope 承担（`gw:rate:login_account:<hash>`）。
func accountBucketKey(email string) string {
	normalized := cryptox.NormalizeEmail(email)
	if normalized == "" {
		return ""
	}
	// 取前 16 位：撞库场景下 2^64 的空间足够避免碰撞，
	// 而键短一半（键名会进每一条慢查询日志）。
	return cryptox.SHA256Hex(normalized)[:16]
}

// peekJSONField 读取请求体中的某个字符串字段，并把请求体还原。
func peekJSONField(c *gin.Context, field string) string {
	if c.Request == nil || c.Request.Body == nil {
		return ""
	}
	raw, err := io.ReadAll(c.Request.Body)
	if err != nil {
		return ""
	}
	c.Request.Body = io.NopCloser(bytes.NewReader(raw))
	c.Request.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(raw)), nil
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ""
	}
	v, ok := payload[field].(string)
	if !ok {
		return ""
	}
	return v
}
