package middleware

import (
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/metricsx"
)

// Metrics 中间件：记录 `gw_requests_total` 与 `gw_request_duration_seconds`。
//
// 两个口径上的选择值得写下来（docs/06-§5.2）：
//
//  1. **route 用路由模板而不是真实路径**。真实路径含用户 id / 会话 id，
//     基数会随数据量无限增长 —— 这正是「指标系统被业务数据撑爆」的典型事故。
//     未匹配到路由（404/扫描）统一记成 `unmatched`，让探测流量集中成一个点。
//
//  2. **duration 只按 route 分维度，status 只在计数上分维度**。
//     直方图的分桶是**每个标签组合一份**（默认 15 桶 × route × method × status
//     会被乘爆），而「P99 慢在哪个接口」与「哪个接口在报 5xx」是两个可以分开
//     回答的问题 —— 分别用两个指标回答即可，不必交叉。
//
// status 为了少写一个 `c.Next()` 后的分支，用 `c.Writer.Status()`；
// 注意 gin 在 handler 什么都没写时它是 200（不是 0），这正是我们要的语义。
func Metrics(m *metricsx.Metrics) gin.HandlerFunc {
	if m == nil {
		return func(c *gin.Context) { c.Next() }
	}
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		elapsed := time.Since(start)

		route := RouteTemplate(c)
		method := c.Request.Method
		m.ObserveRequest(route, method, c.Writer.Status(), elapsed)
	}
}
