package ai

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// ---- base url 拼接 ----

func TestURLBaseJoin(t *testing.T) {
	cases := []struct {
		name   string
		base   string
		path   string
		want   string
		wantFa bool
	}{
		{"标准", "http://127.0.0.1:8000", "/api/v1/knowledge-bases", "http://127.0.0.1:8000/api/v1/knowledge-bases", false},
		{"末尾斜杠", "http://127.0.0.1:8000/", "/api/v1/tasks", "http://127.0.0.1:8000/api/v1/tasks", false},
		{"base 自带路径前缀", "https://gw.example.com/ai", "/api/v1/tasks", "https://gw.example.com/ai/api/v1/tasks", false},
		{"漏了 scheme", "127.0.0.1:8000", "/api/v1/tasks", "http://127.0.0.1:8000/api/v1/tasks", false},
		{"带 query", "http://h:1", "/api/v1/tasks?type=x&page=2", "http://h:1/api/v1/tasks?type=x&page=2", false},
		{"路径不以 / 开头", "http://h:1", "api/v1", "", true},
		{"base 为空", "", "/api/v1", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := newURLBase(tc.base).join(tc.path)
			if tc.wantFa {
				if err == nil {
					t.Fatalf("期望错误，得到 %q", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("意外错误: %v", err)
			}
			if got != tc.want {
				t.Errorf("join = %q, 期望 %q", got, tc.want)
			}
		})
	}
}

// ---- 透传 ----

type capturedRequest struct {
	method string
	uri    string
	auth   string
	ctype  string
	trace  string
	body   []byte
}

func newCapturingUpstream(t *testing.T, status int, respBody string, captured *capturedRequest) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		*captured = capturedRequest{
			method: r.Method,
			uri:    r.URL.RequestURI(),
			auth:   r.Header.Get("Authorization"),
			ctype:  r.Header.Get("Content-Type"),
			trace:  r.Header.Get("X-Trace-Id"),
			body:   body,
		}
		if status != 0 {
			w.Header().Set("Content-Type", "application/json")
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(respBody))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestProxyForwardsVerbatim(t *testing.T) {
	var got capturedRequest
	// 上游回一个**错误信封**：它必须被原样带回来（状态码 + 字节）。
	envelope := `{"error":{"code":"KB_NOT_FOUND","message":"知识库不存在","trace_id":"up-trace","retryable":false}}`
	srv := newCapturingUpstream(t, 404, envelope, &got)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	proxy := NewProxy(opt)

	body := `{"name":"我的知识库"}`
	resp, err := proxy.Do(context.Background(), biz.AIProxyRequest{
		Method:        "POST",
		Path:          "/api/v1/knowledge-bases?page=1&page=2",
		Body:          strings.NewReader(body),
		ContentLength: int64(len(body)),
		ContentType:   "application/json",
		UserToken:     "tok-1",
		TraceID:       "trace-1",
		Class:         biz.AIProxyTimeoutMeta,
	})
	if err != nil {
		t.Fatalf("Do 失败: %v", err)
	}

	// 请求侧：方法、原始路径与 query（**不做归一化**）、凭据、trace、body。
	if got.method != "POST" {
		t.Errorf("上游收到 method = %q", got.method)
	}
	if got.uri != "/api/v1/knowledge-bases?page=1&page=2" {
		t.Errorf("上游收到 uri = %q（query 不应被重写）", got.uri)
	}
	if got.auth != "Bearer tok-1" {
		t.Errorf("上游收到 Authorization = %q", got.auth)
	}
	if got.ctype != "application/json" {
		t.Errorf("上游收到 Content-Type = %q", got.ctype)
	}
	if got.trace != "trace-1" {
		t.Errorf("上游收到 X-Trace-Id = %q", got.trace)
	}
	if string(got.body) != body {
		t.Errorf("上游收到 body = %q", got.body)
	}

	// 响应侧：状态码与字节必须逐字节一致（docs/02-§4.2 规则 1/2/3
	// 靠「不重新编码」天然满足）。
	if resp.Status != 404 {
		t.Errorf("status = %d, 期望 404", resp.Status)
	}
	if string(resp.Body) != envelope {
		t.Errorf("body 被改写了:\n得到 %s\n期望 %s", resp.Body, envelope)
	}
}

func TestProxyTimeoutMapsToAITimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-time.After(2 * time.Second):
		case <-r.Context().Done():
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	opt.MetaTimeout = 40 * time.Millisecond
	proxy := NewProxy(opt)

	start := time.Now()
	_, err := proxy.Do(context.Background(), biz.AIProxyRequest{
		Method: "GET", Path: "/api/v1/tasks", UserToken: "t", Class: biz.AIProxyTimeoutMeta,
	})
	elapsed := time.Since(start)

	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != errs.CodeAITimeout || appErr.Status() != 504 {
		t.Errorf("code=%q status=%d, 期望 AI_TIMEOUT/504", appErr.Code(), appErr.Status())
	}
	if elapsed > time.Second {
		t.Errorf("耗时 %v：meta 档超时没生效", elapsed)
	}
}

func TestProxyRejectsOversizedResponse(t *testing.T) {
	big := strings.Repeat("x", (1<<20)+1024) // 略超 1MB
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(big))
	}))
	t.Cleanup(srv.Close)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	opt.MaxResponseMB = 1
	proxy := NewProxy(opt)

	_, err := proxy.Do(context.Background(), biz.AIProxyRequest{
		Method: "GET", Path: "/api/v1/documents/doc_1/chunks", UserToken: "t", Class: biz.AIProxyTimeoutMeta,
	})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if got := appErr.Details()["reason"]; got != "response_too_large" {
		t.Errorf("details.reason = %v, 期望 response_too_large", got)
	}
}

func TestProxyUnreachableMapsToAIUnavailable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	srv.Close() // 立刻关掉：端口上没人监听

	opt := testOptions(t)
	opt.BaseURL = url
	proxy := NewProxy(opt)

	_, err := proxy.Do(context.Background(), biz.AIProxyRequest{
		Method: "GET", Path: "/api/v1/tasks", UserToken: "t", Class: biz.AIProxyTimeoutMeta,
	})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != errs.CodeAIUnavailable || appErr.Status() != 503 {
		t.Errorf("code=%q status=%d, 期望 AI_UNAVAILABLE/503", appErr.Code(), appErr.Status())
	}
}

// ---- HTTP 通道的 Chat ----

func TestHTTPChatMapsResponse(t *testing.T) {
	body := `{
	  "answer": "答案是 42。",
	  "conversation_id": "cv_x",
	  "message_id": "msg_x",
	  "references": [{"index":1,"chunk_id":"ck","doc_id":"d","kb_id":"kb","doc_name":"n","score":0,"snippet":"s","content_sha256":"h"}],
	  "tool_calls": [],
	  "usage": {"prompt_tokens": 3, "completion_tokens": 4},
	  "finish_reason": "stop",
	  "model": "deepseek-flash",
	  "degraded": true,
	  "degraded_reasons": ["memory_unavailable"],
	  "elapsed_ms": 88
	}`
	var got capturedRequest
	srv := newCapturingUpstream(t, 200, body, &got)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	client := NewChatOrchestratorHTTP(NewProxy(opt), "/api/v1", opt)

	res, err := client.Chat(context.Background(), biz.ChatRequest{
		ConversationID: "cv_x", Query: "q", UserToken: "tok", TraceID: "tr",
		UseRAG: true, KBIDs: []string{}, UseMemory: true,
	})
	if err != nil {
		t.Fatalf("Chat 失败: %v", err)
	}

	if got.uri != "/api/v1/chat" {
		t.Errorf("上游路径 = %q, 期望 /api/v1/chat", got.uri)
	}
	if res.Content != "答案是 42。" || res.ConversationID != "cv_x" || res.Model != "deepseek-flash" {
		t.Errorf("字段映射不对: %+v", res)
	}
	if res.Usage == nil || res.Usage.TotalTokens != 7 {
		t.Errorf("usage 未补齐 total: %+v", res.Usage)
	}
	if !res.Degraded || len(res.DegradedReasons) != 1 {
		t.Errorf("降级信息不对: %+v", res)
	}
	if res.ElapsedMS != 88 {
		t.Errorf("elapsed_ms = %d", res.ElapsedMS)
	}
	// 空数组要归一化成 nil（对外视图统一补 `[]`），否则库里会存一个
	// JSON 的 `[]` 而 `nil` 与 `[]` 在读出时的处理不同。
	if res.ToolCalls != nil {
		t.Errorf("空 tool_calls 应为 nil，得到 %q", res.ToolCalls)
	}

	// 请求侧：`stream=false` 必须在请求体里（`/chat` 只接受 false）。
	var sent map[string]any
	if err := json.Unmarshal(got.body, &sent); err != nil {
		t.Fatalf("请求体不是 JSON: %v (%s)", err, got.body)
	}
	if sent["stream"] != false {
		t.Errorf("请求体 stream = %v, 期望 false", sent["stream"])
	}
	if _, ok := sent["kb_ids"]; !ok {
		// `kb_ids` 在契约里是数组：null 会被严格模式拒绝。
		t.Errorf("请求体缺少 kb_ids: %v", sent)
	}
}

func TestHTTPChatEnvelopeBecomesAppError(t *testing.T) {
	envelope := `{"error":{"code":"OVERLOADED","message":"AI 侧过载","details":{"queue":12},"trace_id":"up-tr","retryable":true}}`
	var got capturedRequest
	srv := newCapturingUpstream(t, 503, envelope, &got)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	client := NewChatOrchestratorHTTP(NewProxy(opt), "/api/v1", opt)

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "q", UserToken: "tok"})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != "OVERLOADED" {
		t.Errorf("code = %q（应是上游原值）", appErr.Code())
	}
	if appErr.Status() != 503 {
		t.Errorf("status = %d", appErr.Status())
	}
	if !appErr.Retryable() {
		t.Error("retryable 未保留上游原值")
	}
	if appErr.TraceID() != "up-tr" {
		t.Errorf("trace_id = %q（应沿用上游）", appErr.TraceID())
	}
	if got := appErr.Details()["queue"]; got != float64(12) {
		t.Errorf("details 未原样保留: %v", appErr.Details())
	}
}

func TestHTTPChatNonEnvelopeBodyIsNormalized(t *testing.T) {
	var got capturedRequest
	// 反向代理的 HTML 502 页：docs/02-§4.2 规则 5 要求归一化。
	srv := newCapturingUpstream(t, 502, "<html><body>Bad Gateway</body></html>", &got)

	opt := testOptions(t)
	opt.BaseURL = srv.URL
	client := NewChatOrchestratorHTTP(NewProxy(opt), "/api/v1", opt)

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "q", UserToken: "tok"})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != errs.CodeAIUnavailable {
		t.Errorf("code = %q, 期望 AI_UNAVAILABLE", appErr.Code())
	}
	if got := appErr.Details()["upstream_status"]; got != 502 {
		t.Errorf("details.upstream_status = %v (%T), 期望 502", got, got)
	}
}

// TestHTTPAndGRPCTransportsAgreeOnResult 是「换传输对上层不可见」的证据。
//
// 同一次逻辑响应分别从两个通道进来（gRPC 用 proto，HTTP 用 JSON），
// 得到的 `biz.ChatResult` 必须一致 —— 否则「两条通道」会变成两种行为。
func TestHTTPAndGRPCTransportsAgreeOnResult(t *testing.T) {
	grpcFake := &fakeAI{resp: grpcResponseForEquivalence()}
	grpcClient := startFakeAI(t, grpcFake)
	viaGRPC, err := grpcClient.Chat(context.Background(), biz.ChatRequest{Query: "q"})
	if err != nil {
		t.Fatalf("gRPC 通道失败: %v", err)
	}

	var got capturedRequest
	srv := newCapturingUpstream(t, 200, equivalenceJSON, &got)
	opt := testOptions(t)
	opt.BaseURL = srv.URL
	viaHTTP, err := NewChatOrchestratorHTTP(NewProxy(opt), "/api/v1", opt).
		Chat(context.Background(), biz.ChatRequest{Query: "q"})
	if err != nil {
		t.Fatalf("HTTP 通道失败: %v", err)
	}

	// 逐字段比对，并把「哪一项不同」打出来 —— 只报一句 "not equal" 对
	// 定位「两条通道开始漂移」毫无帮助。
	if viaGRPC.Content != viaHTTP.Content ||
		viaGRPC.ConversationID != viaHTTP.ConversationID ||
		viaGRPC.FinishReason != viaHTTP.FinishReason ||
		viaGRPC.Model != viaHTTP.Model ||
		viaGRPC.Degraded != viaHTTP.Degraded ||
		viaGRPC.ElapsedMS != viaHTTP.ElapsedMS {
		t.Errorf("标量字段不一致:\n gRPC=%+v\n HTTP=%+v", viaGRPC, viaHTTP)
	}
	// 比对用**语义相等**而不是字节相等：两条通道的 JSON 排版本来就不一样
	// （gRPC 侧是我们自己 json.Marshal 的紧凑格式，HTTP 侧原样带着上游的空白）。
	// 用字节比较会得到一个「永远失败、只好删掉」的断言。
	if !jsonEquivalent(t, viaGRPC.References, viaHTTP.References) {
		t.Errorf("references 语义不一致:\n gRPC=%s\n HTTP=%s", viaGRPC.References, viaHTTP.References)
	}
	if !jsonEquivalent(t, viaGRPC.ToolCalls, viaHTTP.ToolCalls) {
		t.Errorf("tool_calls 语义不一致:\n gRPC=%s\n HTTP=%s", viaGRPC.ToolCalls, viaHTTP.ToolCalls)
	}
	if viaGRPC.Usage == nil || viaHTTP.Usage == nil || *viaGRPC.Usage != *viaHTTP.Usage {
		t.Errorf("usage 不一致: gRPC=%+v HTTP=%+v", viaGRPC.Usage, viaHTTP.Usage)
	}
}

// jsonEquivalent 比较两段 JSON 的语义（键顺序与空白无关）。
func jsonEquivalent(t *testing.T, a, b json.RawMessage) bool {
	t.Helper()
	var va, vb any
	if err := json.Unmarshal(a, &va); err != nil {
		t.Fatalf("左侧不是合法 JSON: %v (%s)", err, a)
	}
	if err := json.Unmarshal(b, &vb); err != nil {
		t.Fatalf("右侧不是合法 JSON: %v (%s)", err, b)
	}
	return reflect.DeepEqual(va, vb)
}

// grpcResponseForEquivalence 与 equivalenceJSON 描述**同一个响应**的 proto 形式。
//
// 两处不同通道的映射各自有测试（`TestChatResponseMapsEveryField` /
// `TestHTTPChatMapsResponse`），这个函数只服务于「双通道对拍」。
func grpcResponseForEquivalence() *aiplatformv1.ChatResponse {
	page := int32(3)
	heading := "第一章 > 1.2"
	return &aiplatformv1.ChatResponse{
		Answer:         "答案是 42。",
		ConversationId: strPtr("cv_same"),
		MessageId:      "msg_same",
		References: []*aiplatformv1.Reference{{
			Index: 1, ChunkId: "ck_1", DocId: "doc_1", KbId: "kb_1",
			DocName: "手册.pdf", Page: &page, HeadingPath: &heading,
			Score: 0.87, Snippet: "片段预览", ContentSha256: "abc123",
		}},
		ToolCalls: []*aiplatformv1.ToolCallTrace{{
			CallId: "call_1", Name: "kb.search",
			ArgumentsJson: `{"gap":"x"}`,
			Status:        "ok", Summary: "命中 5 条", ElapsedMs: 37,
		}},
		Usage:           &aiplatformv1.Usage{PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30},
		FinishReason:    "stop",
		Model:           "deepseek-flash",
		Degraded:        true,
		DegradedReasons: []string{"rerank_skipped"},
		ElapsedMs:       1234,
	}
}

// equivalenceJSON 与 grpcResponseForEquivalence 描述**同一个响应**。
//
// 两份定义并列写出来（而不是从一份生成另一份）是刻意的：
// 这个测试要发现的就是「两条通道的契约开始漂移」，如果两边都从同一处生成，
// 它就只能证明「生成器能跑」。
const equivalenceJSON = `{
  "answer": "答案是 42。",
  "conversation_id": "cv_same",
  "message_id": "msg_same",
  "references": [{
    "index": 1, "chunk_id": "ck_1", "doc_id": "doc_1", "kb_id": "kb_1",
    "doc_name": "手册.pdf", "page": 3, "heading_path": "第一章 > 1.2",
    "score": 0.87, "snippet": "片段预览", "content_sha256": "abc123"
  }],
  "tool_calls": [{
    "call_id": "call_1", "name": "kb.search",
    "arguments": {"gap": "x"},
    "status": "ok", "summary": "命中 5 条", "elapsed_ms": 37
  }],
  "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
  "finish_reason": "stop",
  "model": "deepseek-flash",
  "degraded": true,
  "degraded_reasons": ["rerank_skipped"],
  "elapsed_ms": 1234
}`
