// Package biz 是核心业务层（代码生成规范.md §三.3）。
//
// 边界：MUST NOT import gin / gorm / go-redis / internal/data（依赖方向只有
// `service → biz` 与 `data → biz`）；需要外部资源时在本包定义接口，由 data 实现（规范 §四/§六）。
// 领域对象与仓储接口都定义在本包，`xxxPO` 是 data 侧的持久化表示。
// 按业务职责分文件（user.go / auth.go …），不用 models.go / helpers.go 这类命名（规范 §五.3）。
package biz

import (
	"encoding/json"
	"time"
)

// nowFunc 是注入时钟的统一签名（测试用固定时钟）。
// 所有取当前时间的路径都走它，不直接 time.Now()，否则时间相关断言只能靠 sleep。
type nowFunc func() time.Time

// RequestMeta 是从 HTTP 层带下来的请求元信息（审计与设备记录用）。
// 它是输入而非响应，所以留在 biz；由 service 从 gin.Context 取出，biz 不碰 HTTP。
type RequestMeta struct {
	IP         string
	UserAgent  string
	DeviceName string
	TraceID    string
	// UserToken 是本次请求的 Bearer token：网关调用 AI 时要用调用方自己的身份
	// 原样搬运（docs/04-§3.2）。MUST NOT 写进日志/审计/任何持久化字段。
	UserToken string
}

// deviceName 返回入库用的设备名，超长截断（列宽 64）。
func (m RequestMeta) deviceName() string {
	return truncateRunes(m.DeviceName, 64)
}

// userAgent 返回入库用的 UA，超长截断（列宽 255）。
func (m RequestMeta) userAgent() string {
	if len(m.UserAgent) > 255 {
		return m.UserAgent[:255]
	}
	return m.UserAgent
}

// truncateRunes 按字符（不是字节）截断，避免把多字节汉字切成非法 UTF-8。
func truncateRunes(s string, max int) string {
	if max <= 0 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= max {
		return s
	}
	return string(runes[:max])
}

// jsonOrEmpty 把值序列化成 JSON 文本；失败返回空串（调用方按「未设置」处理）。
// 吞错是刻意的：调用点都是「把可选上下文写进审计 detail」，但不能写入半截 JSON。
func jsonOrEmpty(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(b)
}

// ---- 分页（docs/02-§1：所有列表统一游标分页）----

const (
	// PageLimitDefault 是 `limit` 缺省值。
	PageLimitDefault = 20
	// PageLimitMax 是 `limit` 上限。
	// REQ-AUTH-010 要求超上限返回 INVALID_ARGUMENT 而非静默截断 ——
	// 截断会让客户端以为拿到了全部数据而漏记录，参数写错也无人察觉。
	PageLimitMax = 100
)

// PaginationInput 是列表接口共用的分页参数（已校验）。
// Cursor 是不透明串：载荷属于存储层，编解码留在 data，biz 只原样透传。
type PaginationInput struct {
	Limit  int
	Cursor string
}

// ---- 三态补丁字段 ----

// PatchValue 是 PATCH 请求里的三态字段：未出现 / 显式 null / 有值。
// 用 `*T` 会把「未出现」与「显式 null」折叠成 nil，导致「改回全局默认」无法表达；
// Present 把两者分开。
type PatchValue[T any] struct {
	// Present 为 true 表示请求体里出现了这个字段（哪怕是 null）。
	Present bool
	// Value 为 nil 表示显式 null。
	Value *T
}

// UnmarshalJSON 实现「出现过就置 Present」—— 只有指针接收者能拿到该信息。
func (p *PatchValue[T]) UnmarshalJSON(b []byte) error {
	p.Present = true
	if string(b) == "null" {
		p.Value = nil
		return nil
	}
	var v T
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	p.Value = &v
	return nil
}

// 刻意不提供 MarshalJSON：序列化会把「未出现」与「显式 null」都写成 null 而无法区分。
// 需要「请求体原样」的地方（如幂等指纹）应用原始字节，而不是重新序列化结构体。
