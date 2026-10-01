package biz

import (
	"context"
	"io"
	"strings"
	"testing"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// fakeProxy 记录最后一次调用，用来断言「转发出去的东西」而不是「AI 收到了什么」。
type fakeProxy struct {
	resp  *AIProxyResponse
	err   error
	calls int
	got   AIProxyRequest
	body  []byte
}

func (p *fakeProxy) Do(_ context.Context, req AIProxyRequest) (*AIProxyResponse, error) {
	p.calls++
	p.got = req
	if req.Body != nil {
		p.body, _ = io.ReadAll(req.Body)
	}
	if p.err != nil {
		return nil, p.err
	}
	return p.resp, nil
}

func newProxyService(proxy AIProxy, convs ConversationRepo, logBuf *strings.Builder) *AIProxyService {
	return NewAIProxyService(AIProxyDeps{Proxy: proxy, Conversations: convs, Log: testLogger(logBuf)})
}

func proxyInput() ProxyInput {
	return ProxyInput{
		Method:    "GET",
		Path:      "/api/v1/tasks",
		UserToken: "tok-1",
		TraceID:   "trace-1",
		Class:     AIProxyTimeoutMeta,
	}
}

// ---- 凭据 ----

// TestForwardRejectsEmptyUserToken 是 docs/04-§7 的安全底线：
// 没有用户凭据时**不能**降级用服务间 token 去请求，否则等于把用户资源
// 暴露成一个共享池（用户 A 的调用可能读到用户 B 的知识库）。
//
// 状态码是 **401 而不是 503**：缺凭据是调用方的问题，报成「上游不可用」
// 会把一个自己造成的错误归因到 AI 侧（排查时会往错的方向查）。
func TestForwardRejectsEmptyUserToken(t *testing.T) {
	proxy := &fakeProxy{resp: &AIProxyResponse{Status: 200, Body: []byte(`{}`)}}
	svc := newProxyService(proxy, newFakeConvRepo(), nil)

	in := proxyInput()
	in.UserToken = "   "
	_, err := svc.Forward(context.Background(), "u_1", in)

	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Status() != 401 {
		t.Errorf("status = %d, 期望 401", appErr.Status())
	}
	if appErr.Code() != errs.CodeUnauthenticated {
		t.Errorf("code = %q", appErr.Code())
	}
	if got := appErr.Details()["reason"]; got != "missing_user_token" {
		t.Errorf("details.reason = %v", got)
	}
	if proxy.calls != 0 {
		t.Error("缺少用户凭据时不应发起任何上游请求")
	}
}

// ---- 会话归属 ----

func TestForwardValidatesConversationOwnership(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive}
	convs := newFakeConvRepo(conv)
	proxy := &fakeProxy{resp: &AIProxyResponse{Status: 200}}
	svc := newProxyService(proxy, convs, nil)

	cases := []struct {
		name  string
		user  string
		owned string
		want  bool // 是否放行
	}{
		{"本人会话放行", "u_1", "cv_1", true},
		{"他人会话拒绝", "u_2", "cv_1", false},
		{"会话不存在拒绝", "u_1", "cv_404", false},
		{"不涉及会话资源则跳过校验", "u_1", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			proxy.calls = 0
			in := proxyInput()
			in.OwnedConversationID = tc.owned
			if tc.owned != "" {
				in.Path = "/api/v1/conversations/" + tc.owned + "/summary"
			}
			_, err := svc.Forward(context.Background(), tc.user, in)
			if tc.want {
				if err != nil {
					t.Fatalf("期望放行，得到 %v", err)
				}
				return
			}
			if err == nil {
				t.Fatal("期望拒绝，实际放行")
			}
			appErr, ok := errs.As(err)
			if !ok {
				t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
			}
			if appErr.Status() != 404 {
				t.Errorf("status = %d, 期望 404（不能泄露「存在但不是你的」）", appErr.Status())
			}
			if proxy.calls != 0 {
				t.Error("归属校验失败时不应发起上游请求")
			}
		})
	}
}

// ---- 响应透传 ----

func TestForwardPassesSuccessVerbatim(t *testing.T) {
	body := `{"items":[{"id":"t_1"}],"has_more":false}`
	proxy := &fakeProxy{resp: &AIProxyResponse{
		Status: 200, ContentType: "application/json", Body: []byte(body),
	}}
	svc := newProxyService(proxy, newFakeConvRepo(), nil)

	got, err := svc.Forward(context.Background(), "u_1", proxyInput())
	if err != nil {
		t.Fatalf("Forward 失败: %v", err)
	}
	if string(got.Body) != body {
		t.Errorf("body 被改写: %s", got.Body)
	}
	if proxy.got.UserToken != "tok-1" || proxy.got.TraceID != "trace-1" {
		t.Errorf("凭据/trace 未透传: %+v", proxy.got)
	}
	if proxy.got.Class != AIProxyTimeoutMeta {
		t.Errorf("超时档位 = %q", proxy.got.Class)
	}
}

// TestForwardPassesErrorEnvelopeVerbatim 对应 docs/02-§4.2 规则 4：
// 上游**已经是信封**的错误必须原样返回（连字节都不重排），
// 唯一被改写的东西是 trace_id 的归属（用户看到的追踪号必须是网关的）。
func TestForwardPassesErrorEnvelopeVerbatim(t *testing.T) {
	envelope := `{"error":{"code":"TASK_NOT_FOUND","message":"任务不存在","retryable":false,"trace_id":"upstream-tr"}}`
	proxy := &fakeProxy{resp: &AIProxyResponse{
		Status: 404, ContentType: "application/json", Body: []byte(envelope),
	}}
	svc := newProxyService(proxy, newFakeConvRepo(), nil)

	in := proxyInput()
	in.TraceID = "gw-tr"
	got, err := svc.Forward(context.Background(), "u_1", in)
	if err != nil {
		t.Fatalf("Forward 应把上游错误当数据返回，而不是抛错: %v", err)
	}
	if got.Status != 404 {
		t.Errorf("status = %d, 期望 404", got.Status)
	}
	if string(got.Body) != envelope {
		t.Errorf("信封被改写:\n得到 %s\n期望 %s", got.Body, envelope)
	}
}

// TestForwardNormalizesNonEnvelopeBody 对应 docs/02-§4.2 规则 5：
// 上游回了 HTML（nginx 502 页）、空体、非 JSON —— 都是「反代/上游挂了」的
// 表现，必须归一化成我们自己的信封。
//
// 归一化以 **error** 形式返回（而不是伪造成一个 AIProxyResponse）：
// 只有 handler 用 `httpx.Fail` 出的信封才带网关自己的 request id 和
// 与文档一致的 code→status 映射；把上游的 502 当成「响应体」原样写回去，
// 等于给客户端一个状态码对不上信封的混合体。
func TestForwardNormalizesNonEnvelopeBody(t *testing.T) {
	cases := []struct {
		name       string
		status     int
		body       string
		wantCode   errs.Code
		wantStatus int
		wantReason string
	}{
		{"HTML 502 页", 502, "<html>Bad Gateway</html>", errs.CodeAIUnavailable, 503, "non_envelope_body"},
		{"空体 500", 500, "", errs.CodeAIUnavailable, 503, "non_envelope_body"},
		{"纯文本 400", 400, "bad request", errs.CodeAIUnavailable, 503, "non_envelope_body"},
		{"JSON 但不是信封", 418, `{"hello":"world"}`, errs.CodeAIUnavailable, 503, "non_envelope_body"},
		{"504 归一成超时", 504, "<html>Timeout</html>", errs.CodeAITimeout, 504, "non_envelope_body"},
		{"408 归一成超时", 408, "", errs.CodeAITimeout, 504, "non_envelope_body"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			proxy := &fakeProxy{resp: &AIProxyResponse{Status: tc.status, Body: []byte(tc.body)}}
			var logBuf strings.Builder
			svc := newProxyService(proxy, newFakeConvRepo(), &logBuf)

			_, err := svc.Forward(context.Background(), "u_1", proxyInput())
			appErr, ok := errs.As(err)
			if !ok {
				t.Fatalf("非信封响应应归一化成 *errs.AppError，得到 %T (%v)", err, err)
			}
			if appErr.Code() != tc.wantCode {
				t.Errorf("code = %q, 期望 %q", appErr.Code(), tc.wantCode)
			}
			if appErr.Status() != tc.wantStatus {
				t.Errorf("status = %d, 期望 %d", appErr.Status(), tc.wantStatus)
			}
			if got := appErr.Details()["reason"]; got != tc.wantReason {
				t.Errorf("details.reason = %v, 期望 %q", got, tc.wantReason)
			}
			if got := appErr.Details()["upstream_status"]; got != tc.status {
				t.Errorf("details.upstream_status = %v (%T), 期望 %d", got, got, tc.status)
			}
			// 归一化是「静默改变上游语义」的地方，必须有日志可查。
			if !strings.Contains(logBuf.String(), "ai_proxy.unexpected_upstream_body") {
				t.Errorf("缺少告警日志，实际日志:\n%s", logBuf.String())
			}
		})
	}
}

// ---- 未配置代理 ----

// TestForwardWithoutProxyFailsLoudly：AI 地址没配好时，错误必须是明确的 503，
// 而不是 panic 或者「200 空体」这种让调用方以为是成功的结果。
func TestForwardWithoutProxyFailsLoudly(t *testing.T) {
	svc := NewAIProxyService(AIProxyDeps{Conversations: newFakeConvRepo(), Log: testLogger(nil)})

	_, err := svc.Forward(context.Background(), "u_1", proxyInput())
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Status() != 503 {
		t.Errorf("status = %d, 期望 503", appErr.Status())
	}
}
