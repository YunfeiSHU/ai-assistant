package biz

import (
	"strings"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
)

// WeakPasswords 是内置的常见弱口令集合（REQ-AUTH-001）。
// 只列最典型的几十个：完整字典成本高于收益，真正有效的是长度下限 + 限流 + 审计。
var WeakPasswords = map[string]struct{}{
	"12345678":    {},
	"123456789":   {},
	"1234567890":  {},
	"password":    {},
	"password1":   {},
	"password123": {},
	"passw0rd":    {},
	"qwerty123":   {},
	"qwertyuiop":  {},
	"11111111":    {},
	"00000000":    {},
	"88888888":    {},
	"abc12345":    {},
	"abcd1234":    {},
	"a1234567":    {},
	"admin123":    {},
	"admin888":    {},
	"root1234":    {},
	"iloveyou":    {},
	"letmein1":    {},
	"welcome1":    {},
	"monkey123":   {},
	"dragon123":   {},
	"sunshine":    {},
	"princess":    {},
	"football":    {},
	"baseball":    {},
	"superman":    {},
	"1qaz2wsx":    {},
	"zaq12wsx":    {},
	"asdfghjkl":   {},
	"zxcvbnm123":  {},
	"changeme":    {},
	"secret123":   {},
	"test1234":    {},
	"test@123":    {},
	"p@ssw0rd":    {},
	"woaini1314":  {},
	"5201314":     {},
	"123456a":     {},
}

// PasswordPolicy 是密码强度策略（来自配置）。
type PasswordPolicy struct {
	MinLength int
}

// ValidatePassword 校验密码强度，返回空 reason 表示通过。
// reason 供 error.details.fields 使用，message 由调用方组装成中文。
func ValidatePassword(password string, policy PasswordPolicy, email string) (reason, message string) {
	minLen := policy.MinLength
	if minLen < 8 {
		minLen = 8
	}
	if len([]rune(password)) < minLen {
		return "too_short", "密码长度至少 8 个字符"
	}
	if len([]rune(password)) > 128 {
		return "too_long", "密码长度不能超过 128 个字符"
	}
	// 全空白：长度够了但显然不是有意设置的密码。
	if strings.TrimSpace(password) == "" {
		return "blank", "密码不能为空白字符"
	}
	if _, ok := WeakPasswords[strings.ToLower(password)]; ok {
		return "too_common", "密码过于常见，请更换"
	}
	if email != "" && strings.EqualFold(password, email) {
		return "equals_email", "密码不能与邮箱相同"
	}
	if email != "" {
		local := email
		if i := strings.Index(email, "@"); i > 0 {
			local = email[:i]
		}
		if local != "" && strings.EqualFold(password, local) {
			return "equals_email_local", "密码不能与邮箱用户名相同"
		}
	}
	return "", ""
}

// HashPassword 用策略相关的成本参数派生哈希。
func HashPassword(password string, params cryptox.Argon2Params) (string, error) {
	return cryptox.HashPassword(password, params)
}
