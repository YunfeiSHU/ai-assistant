package middleware_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
)

const testSecret = "middleware-test-secret-key-at-least-32-bytes"

func newTestSigner(t *testing.T) *jwtx.Signer {
	t.Helper()
	s, err := jwtx.NewSigner(jwtx.Config{
		Secret:    testSecret,
		Issuer:    "ai-assistant",
		Audience:  "ai-platform",
		KID:       "k1",
		TTL:       30 * time.Minute,
		ClockSkew: 30 * time.Second,
	})
	if err != nil {
		t.Fatalf("构造 Signer 失败: %v", err)
	}
	return s
}

// stubChecker 让测试能精确控制第 ⑦ 步（token_version 校验）的结果。
//
// 用桩而不是真 Redis/MySQL：这一层的职责是**编排校验顺序**，
// 不是「还原一次真实鉴权」，把它绑到基础设施上只会让测试变慢变脆弱。
type stubChecker struct {
	calls   []int
	err     error
	lastUID string
}

func (s *stubChecker) CheckTokenVersion(_ context.Context, userID string, ver int) error {
	s.calls = append(s.calls, ver)
	s.lastUID = userID
	return s.err
}

// probeEngine 返回「Auth 中间件 + 一个回显 user_id 的 handler」。
func probeEngine(signer *jwtx.Signer, checker biz.TokenVersionChecker) *gin.Engine {
	gin.SetMode(gin.TestMode)
	e := gin.New()
	g := e.Group("/p", middleware.Auth(signer, checker, nil))
	g.GET("/me", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"user_id": middleware.UserID(c)})
	})
	return e
}

func do(e *gin.Engine, authHeader string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/p/me", nil)
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)
	return w
}

func errorCode(t *testing.T, w *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON: %v (body=%s)", err, w.Body.String())
	}
	return body.Error.Code
}

// TestAuthStep1HeaderFormat 覆盖 docs/02-§3.3 第 ① 步。
func TestAuthStep1HeaderFormat(t *testing.T) {
	signer := newTestSigner(t)
	token, _, err := signer.Sign("u_abc", 1, time.Now())
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	e := probeEngine(signer, &stubChecker{})

	cases := []struct {
		name   string
		header string
	}{
		{"缺少 Authorization 头", ""},
		{"缺少 Bearer 前缀", token},
		{"前缀后为空", "Bearer   "},
		{"换了 scheme", "Token " + token},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := do(e, tc.header)
			if w.Code != http.StatusUnauthorized {
				t.Fatalf("status = %d, want 401", w.Code)
			}
			if got := errorCode(t, w); got != string(errs.CodeUnauthenticated) {
				t.Errorf("code = %s, want UNAUTHENTICATED", got)
			}
		})
	}
}

// TestAuthSchemeIsCaseInsensitive 覆盖 RFC 7235 的 scheme 大小写不敏感。
//
// 这条不是吹毛求疵：不同 HTTP 客户端对 scheme 的大小写写法不一致，
// 严格要求 `Bearer` 会让一部分正常客户端莫名 401。
func TestAuthSchemeIsCaseInsensitive(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())
	e := probeEngine(signer, &stubChecker{})

	for _, scheme := range []string{"Bearer", "bearer", "BEARER", "BeArEr"} {
		if w := do(e, scheme+" "+token); w.Code != http.StatusOK {
			t.Errorf("scheme %q: status = %d, want 200", scheme, w.Code)
		}
	}
}

// TestAuthInjectsUserID 覆盖第 ⑧ 步：把 `sub` 注入上下文。
//
// 注入的是 `sub`，不是整条令牌，也不是请求头里的任何东西 ——
// 后者会让调用方有机会伪造身份。
func TestAuthInjectsUserID(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_01ABC", 7, time.Now())
	checker := &stubChecker{}
	e := probeEngine(signer, checker)

	w := do(e, "Bearer "+token)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", w.Code, w.Body.String())
	}
	var body struct {
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	if body.UserID != "u_01ABC" {
		t.Errorf("user_id = %q, want u_01ABC", body.UserID)
	}
	// 第 ⑦ 步必须真的被调用，且拿到的是令牌里的 ver。
	if len(checker.calls) != 1 || checker.calls[0] != 7 {
		t.Errorf("CheckTokenVersion 调用记录 = %v, want [7]", checker.calls)
	}
	if checker.lastUID != "u_01ABC" {
		t.Errorf("CheckTokenVersion 收到的 userID = %q, want u_01ABC", checker.lastUID)
	}
}

// TestAuthStep2RejectsTamperedAndAlienTokens 覆盖第 ②③④⑥ 步。
func TestAuthStep2RejectsTamperedAndAlienTokens(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())

	// 用另一个密钥签发的令牌（模拟「别的环境签的令牌打进来」）。
	alien, err := jwtx.NewSigner(jwtx.Config{
		Secret: "completely-different-secret-key-32-bytes!!", Issuer: "ai-assistant",
		Audience: "ai-platform", TTL: time.Minute,
	})
	if err != nil {
		t.Fatalf("构造 alien signer 失败: %v", err)
	}
	alienToken, _, _ := alien.Sign("u_abc", 1, time.Now())

	// iss 不符：契约要求 ai-platform 侧校验 iss=ai-assistant。
	badIss, err := jwtx.NewSigner(jwtx.Config{
		Secret: testSecret, Issuer: "someone-else",
		Audience: "ai-platform", TTL: time.Minute,
	})
	if err != nil {
		t.Fatalf("构造 badIss signer 失败: %v", err)
	}
	badIssToken, _, _ := badIss.Sign("u_abc", 1, time.Now())

	// aud 不符：docs/02 §3.2 明确写「写错即全部 401」。
	badAud, err := jwtx.NewSigner(jwtx.Config{
		Secret: testSecret, Issuer: "ai-assistant",
		Audience: "ai-assistant", TTL: time.Minute,
	})
	if err != nil {
		t.Fatalf("构造 badAud signer 失败: %v", err)
	}
	badAudToken, _, _ := badAud.Sign("u_abc", 1, time.Now())

	e := probeEngine(signer, &stubChecker{})
	cases := map[string]string{
		"篡改签名":     token[:len(token)-2] + "xy",
		"完全不是 JWT": "not.a.jwt",
		"空串令牌":     "Bearer ",
		"别的密钥签发":   alienToken,
		"iss 不符":   badIssToken,
		"aud 不符":   badAudToken,
		"三段但中间为空":  "eyJhbGciOiJIUzI1NiJ9..x",
	}
	for name, hdr := range cases {
		t.Run(name, func(t *testing.T) {
			w := do(e, "Bearer "+hdr)
			if w.Code != http.StatusUnauthorized {
				t.Fatalf("status = %d, want 401 (body=%s)", w.Code, w.Body.String())
			}
			if got := errorCode(t, w); got != string(errs.CodeUnauthenticated) {
				t.Errorf("code = %s, want UNAUTHENTICATED", got)
			}
		})
	}
}

// TestAuthStep5ExpiredToken 覆盖第 ⑤ 步，且**必须**是 TOKEN_EXPIRED。
//
// 这是唯一允许泄漏「为什么失败」的场景（docs/02 §3.3 的 MAY）：
// 客户端只有看到 TOKEN_EXPIRED 才会去静默刷新；报成 UNAUTHENTICATED
// 会让用户被直接登出，而令牌其实只是过期了。
func TestAuthStep5ExpiredToken(t *testing.T) {
	signer := newTestSigner(t)
	// TTL 30min，签发时间回拨 2h → 必然过期（远超 30s 容差）。
	token, _, err := signer.Sign("u_abc", 1, time.Now().Add(-2*time.Hour))
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	checker := &stubChecker{}
	e := probeEngine(signer, checker)

	w := do(e, "Bearer "+token)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", w.Code)
	}
	if got := errorCode(t, w); got != string(errs.CodeTokenExpired) {
		t.Errorf("code = %s, want TOKEN_EXPIRED", got)
	}
	// 过期令牌 MUST NOT 走到版本校验：省一次 Redis/MySQL 往返，
	// 也避免「过期令牌在版本校验处变慢」形成放大攻击面。
	if len(checker.calls) != 0 {
		t.Errorf("过期令牌不应触发版本校验，实际调用 %v", checker.calls)
	}
}

// TestAuthStep7VersionMismatch 覆盖第 ⑦ 步。
//
// 关键是**错误码的选择**：版本不匹配必须是 UNAUTHENTICATED 而不是
// TOKEN_EXPIRED，因为「刷新」同样会失败（新令牌还是同一版本）。
// 报成 TOKEN_EXPIRED 会让客户端陷入「刷新→401→刷新」的死循环。
func TestAuthStep7VersionMismatch(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())

	checker := &stubChecker{err: errs.New(errs.CodeUnauthenticated)}
	e := probeEngine(signer, checker)

	w := do(e, "Bearer "+token)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", w.Code)
	}
	if got := errorCode(t, w); got != string(errs.CodeUnauthenticated) {
		t.Errorf("code = %s, want UNAUTHENTICATED（绝不能是 TOKEN_EXPIRED）", got)
	}
	if len(checker.calls) != 1 {
		t.Errorf("版本校验应被调用一次，实际 %v", checker.calls)
	}
}

// TestAuthStep7CheckerErrorPropagates 断言版本校验的基础设施故障不会被吞掉。
//
// Redis + MySQL 同时不可用时若「校验失败就放行」，鉴权就成了摆设；
// 若报成 401，客户端会以为要重新登录。正确做法是 503 可重试。
func TestAuthStep7CheckerErrorPropagates(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())

	checker := &stubChecker{err: errs.New(errs.CodeDependencyUnavailable).WithCause(errors.New("redis down"))}
	e := probeEngine(signer, checker)

	w := do(e, "Bearer "+token)
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503（依赖故障不是鉴权失败）", w.Code)
	}
	if got := errorCode(t, w); got != string(errs.CodeDependencyUnavailable) {
		t.Errorf("code = %s, want DEPENDENCY_UNAVAILABLE", got)
	}
}

// TestAuthNilCheckerSkipsVersionCheck 断言 checker 为 nil 时不 panic。
//
// 本地起服务时可能只想验令牌本身（没有 Redis），此时传 nil 应当可用。
func TestAuthNilCheckerSkipsVersionCheck(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())
	e := probeEngine(signer, nil)

	if w := do(e, "Bearer "+token); w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", w.Code, w.Body.String())
	}
}

// TestAuthACAuth03AudIsHardConstraint 复现 docs/02 §7 的 AC-AUTH-03。
//
// 验收标准要求：「把 aud 改成 ai-assistant 后重新签发 → ai-platform 返回
// 401 UNAUTHENTICATED」。这就是接缝 J1 的互操作测试在网关侧的对应项。
func TestAuthACAuth03AudIsHardConstraint(t *testing.T) {
	// 校验方（网关）期望 aud=ai-platform
	verifier := newTestSigner(t)
	// 签发方把 aud 写成了 ai-assistant（错误配置）
	wrongIssuer, err := jwtx.NewSigner(jwtx.Config{
		Secret: testSecret, Issuer: "ai-assistant",
		Audience: "ai-assistant", TTL: 30 * time.Minute,
	})
	if err != nil {
		t.Fatalf("构造 signer 失败: %v", err)
	}
	token, _, _ := wrongIssuer.Sign("u_abc", 1, time.Now())

	w := do(probeEngine(verifier, &stubChecker{}), "Bearer "+token)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("AC-AUTH-03: status = %d, want 401", w.Code)
	}
	if got := errorCode(t, w); got != string(errs.CodeUnauthenticated) {
		t.Errorf("AC-AUTH-03: code = %s, want UNAUTHENTICATED", got)
	}
}

// TestAuthDoesNotLeakReason 断言 401 的响应体不含失败原因（防探测）。
//
// 失败原因写在 cause 里（只进日志）。若泄漏到响应，攻击者可以据此
// 区分「签名错」「iss 错」「令牌过期」，从而推断服务端配置。
func TestAuthDoesNotLeakReason(t *testing.T) {
	signer := newTestSigner(t)
	token, _, _ := signer.Sign("u_abc", 1, time.Now())
	e := probeEngine(signer, &stubChecker{})

	w := do(e, "Bearer "+token[:len(token)-3]+"zzz")
	body := w.Body.String()
	for _, needle := range []string{"signature", "iss", "aud", "signing method", "hmac"} {
		if strings.Contains(strings.ToLower(body), needle) {
			t.Errorf("401 响应体泄漏了内部原因 %q: %s", needle, body)
		}
	}
}
