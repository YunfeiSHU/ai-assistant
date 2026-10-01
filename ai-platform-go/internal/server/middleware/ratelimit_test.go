package middleware

import (
	"regexp"
	"strings"
	"testing"
)

// safeKeyPart 是 `internal/data/redis` 的 Key 片段白名单（逐字抄录）。
// 再写一份而不是 import 那个包的私有变量：本测试要钉的是「中间件交给限流器的标识
// 必须能落进 Redis 键」这条跳层契约，而 import 进来会让契约变成「看实现心情」——
// 数据层把白名单放宽一次，这里就跟着一起放宽，等于没有守卫。
var safeKeyPart = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

// TestAccountBucketKeyIsSafeKeyPart 断言登录账号维度的计数标识通过 Key 白名单。
// 不变量来自真实故障（2026-09-30 实测）：原先直接把邮箱当标识传给限流器，而 `@` 不在白名单里，
// 于是每一次判定都以「Key 片段包含非法字符」失败，被降级包装吞成一行 WARN 后静默改用进程内计数 ——
// 多实例部署时每个实例各发一份名额，撞库防线形同虚设。
func TestAccountBucketKeyIsSafeKeyPart(t *testing.T) {
	cases := []string{
		"User@Example.COM",
		"  mixed.Case+tag@sub.domain.co  ",
		"a@b.c",
		"tenant/user@example.com",
	}
	for _, raw := range cases {
		key := accountBucketKey(raw)
		if key == "" {
			t.Errorf("%q 归一化后不应为空", raw)
			continue
		}
		if !safeKeyPart.MatchString(key) {
			t.Errorf("%q → %q 不是合法 Key 片段", raw, key)
		}
	}
}

// TestAccountBucketKeyCollapsesEquivalentEmails 断言同一邮箱的不同写法共用一个桶。
// 不合并的话，撞库者把 `A@b.com` 与 `a@B.com` 交替使用就能拿到双倍名额 ——
// 而登录比对本身是归一化后进行的（`cryptox.NormalizeEmail`），两边口径不一致正是防线被绕的来源。
func TestAccountBucketKeyCollapsesEquivalentEmails(t *testing.T) {
	a := accountBucketKey("User@Example.COM")
	b := accountBucketKey("  user@example.com  ")
	if a != b {
		t.Errorf("等价邮箱落到不同桶：%q vs %q", a, b)
	}
	if strings.Contains(a, "@") {
		t.Errorf("桶标识里不应保留邮箱原文：%q", a)
	}
	if accountBucketKey("") != "" {
		t.Error("空邮箱必须返回空串（调用方据此跳过这一级限流并回落到 IP）")
	}
	if accountBucketKey("   ") != "" {
		t.Error("纯空白邮箱必须返回空串")
	}
}
