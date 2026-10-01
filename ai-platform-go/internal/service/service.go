// Package service 是 API 与业务之间的适配层（代码生成规范.md §三.2）。
// 只做四件事：绑定/基础校验参数 → 调 biz → 把结果映射成响应 DTO → 把错误交给 httpx 写统一信封。
// MUST NOT 出现：SQL、Redis 调用、复杂业务判断 —— 一旦这里开始查库，
// 同一条规则就会在（M3 起的）gRPC 入口再写一遍。
// 依赖方向 `service → biz`（规范 §四）；本包不 import 任何 data 包，
// 健康检查需要的那点数据库能力通过下面这些窄接口拿到。
package service

import (
	"context"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
)

// SchemaVersion 是网关期望的 schema 版本（对应 deploy/mysql 下的脚本）。
// 本项目用「脚本 + 自检」而不是迁移框架，常量至少能让「运行中的二进制」
// 与「它期望的表结构」在 /health 里对上号。
const SchemaVersion = "20260929_01"

// SchemaChecker 是健康检查所需的最小数据库能力。
// 用窄接口而不是 `*mysql.DB`（规范 §四 禁止 `service → MySQL`）：「就绪」只关心
// 「连得上」与「表齐不齐」，接口开在这里也说明了 service 对数据层的全部要求。
type SchemaChecker interface {
	// Ping 探测连通。
	Ping(ctx context.Context) error
	// VerifyTables 校验期望的表都存在。
	VerifyTables(ctx context.Context, tables []string) error
}

// RedisPinger 是健康检查所需的最小 Redis 能力。
//
// 刻意声明成 `Ping(ctx) error` 而不是返回 `*redis.StatusCmd`：
// 后者会把 go-redis 的驱动类型泄到 service 层，而调用方还容易忘了 .Err()。
type RedisPinger interface {
	Ping(ctx context.Context) error
}

// metaFrom 从 gin 上下文提取请求元信息（审计与设备记录用）。
func metaFrom(c *gin.Context) biz.RequestMeta {
	return biz.RequestMeta{
		IP:        middleware.ClientIP(c),
		UserAgent: c.GetHeader("User-Agent"),
		TraceID:   middleware.TraceID(c),
		UserToken: middleware.RawToken(c),
	}
}
