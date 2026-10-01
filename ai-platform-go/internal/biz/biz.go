// Package biz 是核心业务层（代码生成规范.md §三.3）。
//
// 边界（违反即返工）：
//
//   - MUST NOT import gin / gorm / go-redis，也不 import internal/data。
//     依赖方向只有 `service → biz` 与 `data → biz`（规范 §四）。
//     需要访问外部资源时，在**本包**定义接口，由 data 给出实现（规范 §六）。
//   - 领域对象（User / RefreshToken / AuditLog…）与仓储接口都定义在本包；
//     data 里的 `xxxPO` 是它的持久化表示，转换发生在 data 侧。
//   - 按业务职责分文件（user.go / auth.go / …），不按代码类型分
//     （models.go / repositories.go / helpers.go 这类命名在规范 §五.3 被点名禁止）。
package biz

import (
	"encoding/json"
	"time"
)

// nowFunc 是注入时钟的统一签名（测试用固定时钟）。
//
// 所有取当前时间的路径都必须走它（或 service 层传入的时间），
// 不允许在业务里直接 time.Now()：否则「登录时间」「令牌过期」这些
// 断言在测试里就没法固定，只能靠 sleep。
type nowFunc func() time.Time

// RequestMeta 是从 HTTP 层带下来的请求元信息（审计与设备记录用）。
//
// 它是**输入**而不是响应，所以留在 biz：service 负责从 gin.Context 里
// 把它取出来（见 service.metaFrom），biz 不碰 HTTP 细节。
type RequestMeta struct {
	IP         string
	UserAgent  string
	DeviceName string
	TraceID    string
	// UserToken 是本次请求的 Bearer token（接缝 J1）。
	//
	// 存在的唯一理由是「网关调用 AI 时要用调用方自己的身份」（docs/04-§3.2）：
	// AI 侧自己校验 JWT，网关只负责原样搬运。
	//
	// MUST NOT 写进日志、审计或任何持久化字段。用独立字段而不是塞进 TraceID
	// 之类的字符串，是为了让「它从哪里来、到哪儿去」在类型上就看得见。
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
//
// 这里吞掉错误是刻意的：调用点都是「把可选上下文写进审计 detail」，
// 序列化失败不该让主流程失败，但也不能写入半截 JSON（那样读回来会报错）。
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
	//
	// REQ-AUTH-010 要求超上限返回 INVALID_ARGUMENT **而不是静默截断**：
	// 静默截断时客户端以为拿到了全部数据，翻页逻辑因此漏记录，
	// 而请求参数写错这件事再也没有人会发现。
	PageLimitMax = 100
)

// PaginationInput 是列表接口共用的分页参数（已校验）。
//
// Cursor 是**不透明串**：它的载荷（排序键的具体列）属于存储层，
// 因此编码/解码都留在 data，biz 只负责原样透传。
type PaginationInput struct {
	Limit  int
	Cursor string
}

// ---- 三态补丁字段 ----

// PatchValue 是 PATCH 请求里的三态字段：**未出现** / **显式 null** / **有值**。
//
// 用 `*T` 表示可选字段会把「未出现」和「显式 null」折叠成同一个 nil，
// 于是「把 model 改回全局默认（null）」与「不动 model」变成同一件事 ——
// 前者永远做不到。Present 把两者重新分开。
type PatchValue[T any] struct {
	// Present 为 true 表示请求体里出现了这个字段（哪怕是 null）。
	Present bool
	// Value 为 nil 表示显式 null。
	Value *T
}

// UnmarshalJSON 实现「出现过就置 Present」。
//
// 只有指针接收者能拿到「字段是否出现」的信息，所以反序列化目标
// 必须是 PatchValue 的值（非指针的字段也是可寻址的，encoding/json 会取地址）。
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

// 刻意**不**提供 MarshalJSON：PatchValue 序列化回去会把「未出现」与
// 「显式 null」都写成 `null`，两者一旦被折叠就再也分不开。
// 任何需要「请求体原样」的地方（如幂等键的请求指纹）都应该用**原始字节**，
// 而不是把结构体再序列化一遍。
