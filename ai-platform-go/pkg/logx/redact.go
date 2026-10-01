package logx

import (
	"context"
	"log/slog"
	"strings"
)

// sensitiveKeys 是 MUST 完整替换为 `***` 的属性名（docs/06-§4.4）。
//
// 比对是**大小写不敏感的精确匹配 + 子串匹配**：只做精确匹配会漏掉
// `user_password`、`refresh_token_hash` 这类带前缀/后缀的真实字段名。
var sensitiveKeys = []string{
	"authorization",
	"access_token",
	"refresh_token",
	"id_token",
	"password",
	"passwd",
	"password_hash",
	"token_hash",
	"jwt_secret",
	"internal_service_token",
	"service_token",
	"secret",
	"api_key",
	"apikey",
	"cookie",
	"set-cookie",
	"dsn",
}

// redactStringTail 是日志值的最大长度；超长内容（如模型回答）截断，
// 避免单条日志把存储打爆（docs/06-§4.4：content MUST NOT 出现在 INFO 及以上）。
const redactStringTail = 512

// redactHandler 在写出前替换敏感属性值并截断超长字符串。
//
// 它必须包在**最外层**（见 New）：slog 的 handler 链是「外层先看到记录」，
// 只有包在最外面才能保证连第三方库通过 slog.Default() 写出的日志也被脱敏。
type redactHandler struct{ next slog.Handler }

// Enabled 转发给内层 handler。
//
// 不做额外过滤：级别过滤的语义只应有一处定义（slog.HandlerOptions），
// 在这里再判一次会让「日志为什么没出来」有两个可能的原因。
func (h *redactHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.next.Enabled(ctx, level)
}

// Handle 逐属性脱敏后交给内层写出。
//
// 只重建 Record 的 Attrs、保留原始 Time/Level/Message/PC：
// 时间、级别与调用点必须原样传递，否则日志的时间线与定位信息就不可信了。
func (h *redactHandler) Handle(ctx context.Context, r slog.Record) error {
	out := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	r.Attrs(func(a slog.Attr) bool {
		out.AddAttrs(sanitizeAttr(a))
		return true
	})
	return h.next.Handle(ctx, out)
}

// WithAttrs 在**绑定期**就脱敏后转发。
//
// 绑定期脱敏是必要的：`logger.With("password", x)` 可能被长期持有并反复使用，只在 Handle 里过滤依赖「每次写日志都重新走一遍 Attrs」，
// 一旦某条日志路径绕过了 Handle 的遍历就直接泄漏。
func (h *redactHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	safe := make([]slog.Attr, 0, len(attrs))
	for _, a := range attrs {
		safe = append(safe, sanitizeAttr(a))
	}
	return &redactHandler{next: h.next.WithAttrs(safe)}
}

// WithGroup 转发分组名给内层 handler。
//
// 分组只影响键的命名空间、不影响值的脱敏判据（isSensitive 只看属性键），
// 因此这里不需要额外处理。
func (h *redactHandler) WithGroup(name string) slog.Handler {
	return &redactHandler{next: h.next.WithGroup(name)}
}

func sanitizeAttr(a slog.Attr) slog.Attr {
	if isSensitive(a.Key) {
		return slog.String(a.Key, "***")
	}
	if a.Value.Kind() == slog.KindString {
		s := a.Value.String()
		// 邮箱：`email` / `user_email` / `contact_email` 这类键的值一律脱敏。
		// 用「键名包含 email」而不是「值看起来像邮箱」做判据：
		// 后者会对每条日志跑正则，而且 `a@b` 这种既像邮箱又像路径的串
		// 会被误伤成 `a***@b` —— 在报错信息里那是很有价值的内容。
		if strings.Contains(strings.ToLower(a.Key), "email") {
			return slog.String(a.Key, RedactEmail(s))
		}
		// JWT 的兜底：`eyJ` 是 base64url 的 `{"` 的前三个字符，
		// 任何 JWT 都以它开头。这里只做前缀判断（零成本），
		// 命中就整串打掉 —— docs/06-§4.4 的验收明确要求日志里不出现 `eyJ`，
		// 而「某个忘了走脱敏的调用点直接打了 token」是这条要求唯一的破口。
		if looksLikeJWT(s) {
			return slog.String(a.Key, "***")
		}
		if len(s) > redactStringTail {
			return slog.String(a.Key, s[:redactStringTail]+"...(truncated)")
		}
		return a
	}
	if a.Value.Kind() == slog.KindGroup {
		group := a.Value.Group()
		out := make([]slog.Attr, 0, len(group))
		for _, g := range group {
			out = append(out, sanitizeAttr(g))
		}
		return slog.Group(a.Key, attrsToAny(out)...)
	}
	return a
}

// looksLikeJWT 判断字符串是否是 JWT 形态（三段 base64url，以 `eyJ` 开头）。
//
// 要求「三段且前两段非空」而不是只看前缀：只判前缀会把 `eyJhbGci` 这种用户输入（比如昵称里带这三个字母）也打掉，加上段数判断后误伤概率可以忽略。
func looksLikeJWT(s string) bool {
	if !strings.HasPrefix(s, "eyJ") {
		return false
	}
	first := strings.IndexByte(s, '.')
	if first <= 0 || first == len(s)-1 {
		return false
	}
	second := strings.IndexByte(s[first+1:], '.')
	if second <= 0 {
		return false
	}
	// 签名段必须在：`header.payload.` 这种被截断的串不算完整 JWT。
	return first+1+second+1 < len(s)
}

func isSensitive(key string) bool {
	lower := strings.ToLower(key)
	for _, s := range sensitiveKeys {
		if strings.Contains(lower, s) {
			return true
		}
	}
	return false
}

// Redact 把单个值按敏感键规则脱敏（供非 slog 场景复用，如审计 detail）。
func Redact(key, value string) string {
	if isSensitive(key) {
		return "***"
	}
	return value
}

// RedactMap 返回脱敏后的 map 副本；不修改入参。
func RedactMap(in map[string]any) map[string]any {
	if in == nil {
		return nil
	}
	out := make(map[string]any, len(in))
	for k, v := range in {
		if isSensitive(k) {
			out[k] = "***"
			continue
		}
		out[k] = v
	}
	return out
}

// RedactEmail 把邮箱脱敏成 `a***@b.com`（docs/06-§4.4）。
//
// 保留首字符与域名而不是整串打掉：首字符足以在同一条日志里区分是不是同一个账号，域名在排障时常常就是关键线索，且本身不指向具体自然人。
// 非法输入（无 `@`、`@` 在首尾、含空白/控制字符）**一律返回 `***`**，绝不原样返回 —— 任何一条「想不出来就放过」的路径，都会在最需要它的地方失效。
func RedactEmail(email string) string {
	at := strings.LastIndexByte(email, '@')
	if at <= 0 || at == len(email)-1 {
		return "***"
	}
	// 含空白/控制字符的「邮箱」不是邮箱（很可能是拼接出来的日志文本），
	// 这时不能按 `@` 切分 —— 切出来的「域名」可能包含后面的整段文本。
	if strings.ContainsAny(email, " \t\r\n") {
		return "***"
	}
	local, domain := email[:at], email[at+1:]
	return local[:1] + "***@" + domain
}

// IsSensitiveKey 报告 key 是否会被脱敏（测试与审计层复用）。
func IsSensitiveKey(key string) bool { return isSensitive(key) }
