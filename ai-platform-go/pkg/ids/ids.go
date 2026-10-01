// Package ids 负责带前缀的资源 ID 生成与校验。
//
// 契约（docs/02-§1）：`{前缀}_{26 位 Crockford Base32 ULID}`。
// 网关是以下前缀的唯一生成者：`u_` / `cv_` / `msg_` / `rt_` / `req_`。
package ids

import (
	"crypto/rand"
	"encoding/hex"
	"regexp"
	"strings"
	"time"

	"github.com/oklog/ulid/v2"
)

// 网关持有的 ID 前缀（docs/02-§1）。
const (
	// PrefixUser 是用户 ID 前缀（`u_...`）。
	PrefixUser = "u"
	// PrefixRefreshToken 是刷新令牌记录 ID 前缀（`rt_...`）；明文令牌本身不带前缀。
	PrefixRefreshToken = "rt"
	// PrefixConversation 是会话 ID 前缀（`cv_...`）。
	PrefixConversation = "cv"
	// PrefixMessage 是消息 ID 前缀（`msg_...`）。
	PrefixMessage = "msg"
	// PrefixRequest 是请求 ID 前缀（`req_...`），用于幂等与排障串联。
	PrefixRequest = "req"
)

// entropy 是 ULID 的熵源。用 crypto/rand 保证不可预测
// （math/rand 的默认源可被推断，会话 ID 可预测等于越权风险）。
var entropy = ulid.Monotonic(rand.Reader, 0)

// newIDFunc 允许测试注入确定性 ID（见 SetIDGeneratorForTest）。
var newIDFunc = defaultNewID

func defaultNewID(prefix string) string {
	return prefix + "_" + ulid.MustNew(ulid.Timestamp(time.Now().UTC()), entropy).String()
}

// New 生成 `{prefix}_{ULID}`。
//
// 这里不做前缀白名单校验：调用方传的都是编译期常量；
// 外部输入（如路径参数）要走 Validate，不要走 New。
func New(prefix string) string {
	return newIDFunc(prefix)
}

// NewUser 生成用户 ID，等价于 New(PrefixUser)。
func NewUser() string { return New(PrefixUser) }

// NewRefreshToken 生成刷新令牌记录 ID，等价于 New(PrefixRefreshToken)。
func NewRefreshToken() string { return New(PrefixRefreshToken) }

// NewConversation 生成会话 ID，等价于 New(PrefixConversation)。
func NewConversation() string { return New(PrefixConversation) }

// NewMessage 生成消息 ID，等价于 New(PrefixMessage)。
func NewMessage() string { return New(PrefixMessage) }

// NewRequest 生成请求 ID，等价于 New(PrefixRequest)。
func NewRequest() string { return New(PrefixRequest) }

// pattern 匹配带前缀 ULID。按 docs/02-§1 的要求：
// 前缀取自白名单，体部是 26 位 Crockford Base32（不含 I/L/O/U）。
var pattern = regexp.MustCompile(`^(u|rt|cv|msg|req|kb|doc|chk|task|mem)_[0-9A-HJKMNP-TV-Z]{26}$`)

// ulidPattern 只匹配体部，供「前缀由调用方另行校验」的场景复用。
var ulidPattern = regexp.MustCompile(`^[0-9A-HJKMNP-TV-Z]{26}$`)

// Validate 报告 s 是否是本系统约定的带前缀 ULID。
//
// 注意：这里**接受** AI 侧的前缀（kb/doc/chk/task/mem），因为网关需要校验
// 透传接口上客户端传来的资源 ID 的形状，但网关本身不生成它们。
func Validate(s string) bool { return pattern.MatchString(s) }

// ValidatePrefix 报告 s 是否是以 prefix 开头且体部合法的 ID。
func ValidatePrefix(s, prefix string) bool {
	head, body, ok := strings.Cut(s, "_")
	if !ok || head != prefix {
		return false
	}
	return ulidPattern.MatchString(body)
}

// IsCrockfordBase32 报告 s 的体部是否是 26 位 Crockford Base32（不含前缀）。
func IsCrockfordBase32(s string) bool { return ulidPattern.MatchString(s) }

// SetIDGeneratorForTest 注入确定性 ID 生成器，返回还原函数。
//
// 只供测试使用：生产路径上 ID 必须来自 crypto/rand。
func SetIDGeneratorForTest(fn func(prefix string) string) func() {
	prev := newIDFunc
	newIDFunc = fn
	return func() { newIDFunc = prev }
}

// NewHex 返回 n 字节随机数据的十六进制表示（trace id / span id 用）。
//
// 用 crypto/rand 而不是 math/rand：trace id 会出现在响应头与错误信封里，可预测的值让攻击者能伪造 `traceparent` 去污染别人的链路视图。
// 随机源失败时返回全 0：长度仍然正确，链路只失去唯一性（不至于 panic）。
func NewHex(nBytes int) string {
	if nBytes <= 0 {
		return ""
	}
	buf := make([]byte, nBytes)
	if _, err := rand.Read(buf); err != nil {
		return strings.Repeat("0", nBytes*2)
	}
	return hex.EncodeToString(buf)
}
