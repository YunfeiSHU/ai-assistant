package errs_test

import (
	"net/http"
	"sort"
	"testing"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// docCodes 是 docs/02-接口规范与鉴权.md §4.1 的网关自有错误码**逐行抄录**。
//
// 这份表是**契约**，不是实现细节：改代码必须同步改文档，改文档必须同步改代码。
// 因此这里刻意把期望值硬编码（而不是从实现里推导）——
// 从实现推导出来的期望值永远会通过，等于没有测试。
//
// 抄录时间：2026-09-29。
var docCodes = []struct {
	code      errs.Code
	status    int
	retryable bool
}{
	{"INVALID_ARGUMENT", http.StatusBadRequest, false},
	{"UNAUTHENTICATED", http.StatusUnauthorized, false},
	{"TOKEN_EXPIRED", http.StatusUnauthorized, false},
	{"INVALID_CREDENTIALS", http.StatusUnauthorized, false},
	{"INVALID_REFRESH_TOKEN", http.StatusUnauthorized, false},
	{"PERMISSION_DENIED", http.StatusForbidden, false},
	{"USER_DISABLED", http.StatusForbidden, false},
	{"RESOURCE_NOT_FOUND", http.StatusNotFound, false},
	{"CONVERSATION_NOT_FOUND", http.StatusNotFound, false},
	{"MESSAGE_NOT_FOUND", http.StatusNotFound, false},
	{"CONFLICT", http.StatusConflict, false},
	{"EMAIL_ALREADY_EXISTS", http.StatusConflict, false},
	{"CONVERSATION_ARCHIVED", http.StatusConflict, false},
	{"PAYLOAD_TOO_LARGE", http.StatusRequestEntityTooLarge, false},
	{"UNSUPPORTED_MEDIA_TYPE", http.StatusUnsupportedMediaType, false},
	{"QUOTA_EXCEEDED", http.StatusTooManyRequests, false},
	{"RATE_LIMITED", http.StatusTooManyRequests, true},
	{"INTERNAL_ERROR", http.StatusInternalServerError, true},
	{"AI_UNAVAILABLE", http.StatusServiceUnavailable, true},
	{"AI_OVERLOADED", http.StatusServiceUnavailable, true},
	{"DEPENDENCY_UNAVAILABLE", http.StatusServiceUnavailable, true},
	{"AI_TIMEOUT", http.StatusGatewayTimeout, true},
}

// TestGatewayCodesMatchDoc 断言网关错误码表与文档完全一致（双向）。
//
// 双向都要查，因为两种偏差的表现完全不同：
//   - 代码多了文档没有的码 → 客户端按文档写的 switch 落到 default 分支；
//   - 文档有的码代码里没有 → 永远不会返回，客户端的分支是死代码。
//
// 第二种尤其危险：它「看起来没问题」，直到某天真的需要那个码时才发现没实现。
func TestGatewayCodesMatchDoc(t *testing.T) {
	for _, want := range docCodes {
		e := errs.New(want.code)
		if got := e.Status(); got != want.status {
			t.Errorf("%s: status = %d, 文档要求 %d", want.code, got, want.status)
		}
		if got := e.Retryable(); got != want.retryable {
			t.Errorf("%s: retryable = %v, 文档要求 %v", want.code, got, want.retryable)
		}
		if msg := e.Message(); msg == "" {
			t.Errorf("%s: message 不能为空（契约要求 message 可直接展示给用户）", want.code)
		}
	}
}

// TestGatewayCodesAreDocumented 反向断言：实现里不存在文档未声明的码。
//
// 唯一的例外在 allowlist 里，并且必须写明理由 —— 加一个码太容易了，
// 没有这道闸门，编码表会慢慢漂移出文档。
func TestGatewayCodesAreDocumented(t *testing.T) {
	// 扩展码：不在 docs/02 §4.1 表内，但有明确用途。
	// 用法：优雅退出期间拒绝新请求（docs/06-§3 第 ② 步），让 LB 把流量切走，
	// 而不是让客户端收到一个含义不明的 500。
	allowlist := map[errs.Code]string{
		"SERVICE_SHUTTING_DOWN": "docs/06-§3 优雅退出：新请求被拒，客户端应重试到其它实例",
	}

	documented := make(map[errs.Code]bool, len(docCodes))
	for _, d := range docCodes {
		documented[d.code] = true
	}

	var undocumented []string
	for _, c := range errs.Codes() {
		if documented[c] || allowlist[c] != "" {
			continue
		}
		undocumented = append(undocumented, string(c))
	}
	sort.Strings(undocumented)
	if len(undocumented) > 0 {
		t.Errorf("以下错误码未在 docs/02 §4.1 声明，也未加入 allowlist: %v\n"+
			"要么补文档，要么加 allowlist 并写明理由", undocumented)
	}

	// allowlist 里的码必须真的存在，否则它是一条失效的豁免。
	for code := range allowlist {
		if !errs.KnownCode(code) {
			t.Errorf("allowlist 里的 %s 已不存在，请删掉这条豁免", code)
		}
	}
}

// TestLookupStatus 覆盖未知码的兜底行为。
//
// 未知码 MUST 落到 500：把「不认识的错误」当成 400 会把服务端缺陷
// 伪装成调用方问题，而 500 至少会进错误告警。
func TestLookupStatus(t *testing.T) {
	if status, ok := errs.LookupStatus(errs.CodeInvalidArgument); !ok || status != http.StatusBadRequest {
		t.Fatalf("已知码应返回 (400, true)，实际 (%d, %v)", status, ok)
	}
	if status, ok := errs.LookupStatus("NOT_A_REAL_CODE"); ok || status != http.StatusInternalServerError {
		t.Fatalf("未知码应返回 (500, false)，实际 (%d, %v)", status, ok)
	}
}

// TestUpstreamErrorPreservesEnvelope 覆盖接缝 J2 的透传规则（docs/02 §4.2）。
//
// 透传时最容易犯的两个错：
//  1. 把上游的 trace_id 换成自己的 → 排障时链路断裂；
//  2. 直接改上游的 details → 丢字段。
//
// 所以这里既断言 trace_id 被保留，也断言 details 的原有键没被动过。
func TestUpstreamErrorPreservesEnvelope(t *testing.T) {
	upstreamDetails := map[string]any{
		"model": "deepseek-flash",
		"usage": map[string]any{"total_tokens": 42},
	}
	e := errs.UpstreamError(
		"UPSTREAM_LLM_ERROR", "模型服务异常", http.StatusBadGateway, true,
		"ai-trace-abc123", upstreamDetails,
	)

	if e.Status() != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", e.Status())
	}
	if e.TraceID() != "ai-trace-abc123" {
		t.Errorf("trace_id = %q, 必须原样透传 AI 侧的值", e.TraceID())
	}
	if got := e.Details()["model"]; got != "deepseek-flash" {
		t.Errorf("details.model = %v, 上游的键 MUST NOT 被修改或删除", got)
	}
	// 网关只 MAY 追加，且必须放在自己的命名空间下，避免与上游键冲突。
	gw, ok := e.Details()["gateway"].(map[string]any)
	if !ok || gw["upstream"] != "ai-platform" {
		t.Errorf("details.gateway.upstream 应为 ai-platform，实际 %v", e.Details()["gateway"])
	}
	// 传进去的 map MUST NOT 被就地修改（调用方可能还要复用）。
	if _, mutated := upstreamDetails["gateway"]; mutated {
		t.Error("UpstreamError 不得就地修改调用方传入的 details map")
	}
}

// TestAIErrorCodesPassThrough 断言 30 个 AI 侧错误码都被认识。
//
// 这些码不需要网关自己的 status 表（status 由上游 HTTP 响应决定），
// 但必须能被 `KnownCode` 认出来 —— 否则在日志与指标里会被当成未知码，
// 按码聚合的告警就统计不到 AI 侧的故障。
func TestAIErrorCodesPassThrough(t *testing.T) {
	codes := errs.AIErrorCodes()
	if len(codes) < 25 {
		t.Fatalf("AI 侧透传码只有 %d 个，docs/02 §4.2 列了 30 个左右，疑似漏抄", len(codes))
	}
	for _, c := range codes {
		if !errs.KnownCode(c) {
			t.Errorf("%s 不在已知码集合内", c)
		}
	}
	// 网关自有码与 AI 透传码不应重叠：重叠意味着同一码有两个权威，
	// 一旦两边定义漂移就无法判断该听谁的。
	own := map[errs.Code]bool{}
	for _, c := range errs.Codes() {
		own[c] = true
	}
	for _, c := range codes {
		if own[c] {
			t.Errorf("%s 同时出现在网关自有码与 AI 透传码里", c)
		}
	}
}
