package ai

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件是 `Chat` 的 gRPC 通道测试。
//
// 最关键的一个是 `TestChatRequestFieldLevelSnapshot`：它用**真实的 grpc.Server**
// 接住请求，再把收到的 proto 逐字段与 `biz.ChatRequest` 比对。这比「打桩断言
// 调用了什么」强得多 —— 它证明的是「跨进程之后字段还在」，也就是
// AC-ORCH-02（proto 与契约的字段级一致性）真正关心的东西。
//
// 为什么用 bufconn 而不是真的监听一个 TCP 端口：测试不该占用端口（本机就有
// 一个端口冲突的历史包袱），bufconn 走内存 pipe，永远不冲突，且更快。

// ---- 假服务端 ----

type fakeAI struct {
	aiplatformv1.UnimplementedAiPlatformServer

	mu       sync.Mutex
	got      *aiplatformv1.ChatRequest
	incoming metadata.MD
	calls    int

	resp     *aiplatformv1.ChatResponse
	err      error
	delay    time.Duration
	trailers map[string]string
}

func (f *fakeAI) Chat(ctx context.Context, in *aiplatformv1.ChatRequest) (*aiplatformv1.ChatResponse, error) {
	f.mu.Lock()
	f.calls++
	f.got = in
	if f.incoming == nil {
		if md, ok := metadata.FromIncomingContext(ctx); ok {
			f.incoming = md
		}
	}
	trailers := f.trailers
	resp, ferr, delay := f.resp, f.err, f.delay
	f.mu.Unlock()

	if len(trailers) > 0 {
		if terr := grpc.SetTrailer(ctx, metadata.New(trailers)); terr != nil {
			return nil, terr
		}
	}
	if delay > 0 {
		// **刻意忽略 ctx**：这一支要模拟的是「上游慢」，而不是「上游也知道超时了」。
		//
		// 原因是一条真实的竞态：gRPC 会把客户端 deadline 通过 `grpc-timeout`
		// 头传给服务端，于是服务端到点也会回一个 `DeadlineExceeded`。
		// 如果这里 `select` 了 `ctx.Done()`，两颗定时器就在同一点竞速 ——
		// 谁的 status 先到客户端是**随机的**，而两者的 `details.reason`
		// 不同（`gateway_deadline_exceeded` vs `non_envelope_status`），
		// 于是断言「网关 deadline 生效」的用例会在机器忙时偶发失败。
		// （实测：单独跑 5 次全绿，与其它包并行跑时挂过一次。）
		//
		// 「上游自己回 DeadlineExceeded」是另一条分支，由
		// TestChatUpstreamDeadlineStatusIsNotOurDeadline 覆盖。
		time.Sleep(delay)
	}
	return resp, ferr
}

func (f *fakeAI) lastRequest(t *testing.T) *aiplatformv1.ChatRequest {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.got == nil {
		t.Fatal("AI 侧没有收到请求")
	}
	return f.got
}

func (f *fakeAI) lastMetadata(t *testing.T) metadata.MD {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.incoming
}

// startFakeAI 起一个内存中的 gRPC 服务端并返回可用的客户端。
func startFakeAI(t *testing.T, fake *fakeAI) biz.ChatOrchestrator {
	t.Helper()

	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	aiplatformv1.RegisterAiPlatformServer(srv, fake)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	conn, err := dialForTest(t, lis)
	if err != nil {
		t.Fatalf("建立 gRPC 连接失败: %v", err)
	}
	return newChatOrchestratorFromConn(conn, testOptions(t))
}

func testOptions(t *testing.T) Options {
	t.Helper()
	return Options{
		ConnectTimeout: time.Second,
		ChatTimeout:    3 * time.Second,
		MetaTimeout:    time.Second,
		MaxResponseMB:  4,
		Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// dialForTest 用 bufconn 建连，其余参数与生产路径一致（见 grpc.go 的注释）。
func dialForTest(t *testing.T, lis *bufconn.Listener) (*grpc.ClientConn, error) {
	t.Helper()
	return grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
}

// ---- 请求映射：字段级快照 ----

func TestChatRequestFieldLevelSnapshot(t *testing.T) {
	fake := &fakeAI{resp: &aiplatformv1.ChatResponse{Answer: "ok"}}
	client := startFakeAI(t, fake)

	convID := "cv_01J8AAAAAAAAAAAAAAAAAAAA"
	temp := 0.25
	topK := 7
	rerank := 4
	threshold := 0.75
	model := "deepseek-flash"

	req := biz.ChatRequest{
		ConversationID: convID,
		UserID:         "u_01J8AAAAAAAAAAAAAAAAAAAA",
		UserToken:      "tok-abc",
		Query:          "什么是向量检索？",
		UseRAG:         true,
		KBIDs:          []string{"kb_1", "kb_2"},
		UseMemory:      false,
		UseTools:       true,
		Model:          model,
		Temperature:    &temp,
		TopK:           &topK,
		RerankTopN:     &rerank,
		ScoreThreshold: &threshold,
		TraceID:        "4bf92f3577b34da6a3ce929d0e0e4736",
		History: []biz.ChatMessage{
			{Role: biz.MessageRoleUser, Content: "上一轮问题"},
			{Role: biz.MessageRoleAssistant, Content: "上一轮回答"},
		},
	}

	if _, err := client.Chat(context.Background(), req); err != nil {
		t.Fatalf("Chat 失败: %v", err)
	}
	got := fake.lastRequest(t)

	// 逐字段断言，不用 reflect.DeepEqual：字段名出现在断言里，
	// 将来 proto 改字段时失败信息能直接指出是哪一项对不上。
	if got.GetQuery() != req.Query {
		t.Errorf("query = %q, 期望 %q", got.GetQuery(), req.Query)
	}
	if got.GetConversationId() != convID {
		t.Errorf("conversation_id = %q, 期望 %q", got.GetConversationId(), convID)
	}
	if got.GetUseRag() != true || got.GetUseMemory() != false || got.GetUseTools() != true {
		t.Errorf("布尔开关不对: use_rag=%v use_memory=%v use_tools=%v",
			got.GetUseRag(), got.GetUseMemory(), got.GetUseTools())
	}
	if len(got.GetKbIds()) != 2 || got.GetKbIds()[0] != "kb_1" {
		t.Errorf("kb_ids = %v", got.GetKbIds())
	}
	if got.GetModel() != model {
		t.Errorf("model = %q", got.GetModel())
	}
	// optional 字段在 proto 里是指针：用 GetX() 取值无法区分「没传」和「传了零值」，
	// 所以这里断言指针本身 —— 这正是 `optional` 存在的意义。
	if got.Temperature == nil || *got.Temperature != temp {
		t.Errorf("temperature = %v, 期望 %v（且非 nil）", got.Temperature, temp)
	}
	if got.TopK == nil || *got.TopK != int32(topK) {
		t.Errorf("top_k = %v, 期望 %d（且非 nil）", got.TopK, topK)
	}
	if got.RerankTopN == nil || *got.RerankTopN != int32(rerank) {
		t.Errorf("rerank_top_n = %v, 期望 %d（且非 nil）", got.RerankTopN, rerank)
	}
	if got.ScoreThreshold == nil || *got.ScoreThreshold != threshold {
		t.Errorf("score_threshold = %v, 期望 %v（且非 nil）", got.ScoreThreshold, threshold)
	}
	// history 只在 use_memory=false 时才有意义（REQ-ORCH-006）。
	if len(got.GetHistory()) != 2 {
		t.Fatalf("history 长度 = %d, 期望 2", len(got.GetHistory()))
	}
	if got.GetHistory()[0].GetRole() != "user" || got.GetHistory()[1].GetContent() != "上一轮回答" {
		t.Errorf("history 内容不对: %+v", got.GetHistory())
	}
	if got.GetMetadata() != nil {
		// 空 map 与 nil map 在 protobuf 线格式上不可区分（都是零条目），
		// 所以这里只能断言「没有捅进去任何埋点」。
		t.Errorf("M3 不应传 metadata，得到 %v", got.GetMetadata())
	}
}

// TestChatRequestNeverCarriesUserIdentity 钉住接缝 J1 的反面：
// **proto 里不允许出现 user_id**，身份只能走 `authorization` metadata。
//
// 这条断言的价值在于它能否证 docs/04-§3.2 明令禁止的做法
// （「网关自造 user_id header 让 AI 信任」）。加了就失败，而不是靠评审时想起来。
func TestChatRequestNeverCarriesUserIdentity(t *testing.T) {
	fake := &fakeAI{resp: &aiplatformv1.ChatResponse{Answer: "ok"}}
	client := startFakeAI(t, fake)

	req := biz.ChatRequest{
		UserID:    "u_01J8AAAAAAAAAAAAAAAAAAAA",
		UserToken: "tok-abc",
		Query:     "hi",
		TraceID:   "trace-1",
	}
	if _, err := client.Chat(context.Background(), req); err != nil {
		t.Fatalf("Chat 失败: %v", err)
	}

	got := fake.lastRequest(t)
	// proto 层面：整个请求里不该出现任何等于 user_id 的字符串。
	if raw := got.String(); strings.Contains(raw, req.UserID) {
		t.Errorf("请求里出现了 user_id（%q），违反接缝 J1：%s", req.UserID, raw)
	}

	md := fake.lastMetadata(t)
	if contains(md.Get("user-id"), req.UserID) || contains(md.Get("x-user-id"), req.UserID) {
		t.Error("metadata 里出现了网关自造的 user-id，违反 docs/04-§3.2")
	}
	// 正向断言：凭据确实到了（否则上面那条「没出现」可能只是因为整个 metadata 都空）。
	if got := md.Get(MetaAuthorization); len(got) != 1 || got[0] != "Bearer tok-abc" {
		t.Errorf("authorization metadata = %v, 期望 [Bearer tok-abc]", got)
	}
	if got := md.Get(MetaTraceID); len(got) != 1 || got[0] != "trace-1" {
		t.Errorf("x-trace-id metadata = %v, 期望 [trace-1]", got)
	}
	// 出站 metadata 必须「只有这两个键」：多出来的键通常意味着某个中间件
	// 把 HTTP 头整批转发了过来（Cookie、X-Forwarded-* 之类）。
	//
	// gRPC 自己会带几个传输层头（`:authority`、`content-type`、`user-agent`，
	// 以及 `grpc-*` / `te`），它们不是我们发的，得先排除 —— 不排除的话
	// 这条断言会永远失败，然后被顺手删掉（那就白写了）。
	for key := range md {
		if strings.HasPrefix(key, ":") || grpcTransportHeader(key) {
			continue
		}
		if key != MetaAuthorization && key != MetaTraceID {
			t.Errorf("出站 metadata 出现了预期外的键 %q = %v", key, md.Get(key))
		}
	}
}

// grpcTransportHeader 报告该 metadata 键是否由 gRPC 传输层自己填充。
func grpcTransportHeader(key string) bool {
	switch key {
	case "content-type", "user-agent", "te", "accept-encoding", "grpc-accept-encoding",
		"grpc-timeout", "grpc-encoding", "grpc-status-details-bin", "grpc-message", "grpc-status":
		return true
	default:
		return false
	}
}

func contains(items []string, want string) bool {
	for _, it := range items {
		if it == want {
			return true
		}
	}
	return false
}

// ---- 响应映射 ----

func TestChatResponseMapsEveryField(t *testing.T) {
	page := int32(3)
	heading := "第一章 > 1.2"
	fake := &fakeAI{resp: &aiplatformv1.ChatResponse{
		Answer:         "答案是 42。",
		ConversationId: strPtr("cv_from_ai"),
		MessageId:      "msg_from_ai",
		References: []*aiplatformv1.Reference{{
			Index:         1,
			ChunkId:       "ck_1",
			DocId:         "doc_1",
			KbId:          "kb_1",
			DocName:       "手册.pdf",
			Page:          &page,
			HeadingPath:   &heading,
			Score:         0, // 显式 0：用来钉住「omitempty 会把它丢掉」这个坑
			Snippet:       "片段预览",
			ContentSha256: "abc123",
		}},
		ToolCalls: []*aiplatformv1.ToolCallTrace{{
			CallId:        "call_1",
			Name:          "kb.search",
			ArgumentsJson: `{"q":"向量","top_k":5}`,
			Status:        "ok",
			Summary:       "命中 5 条",
			ElapsedMs:     37,
		}},
		Usage:           &aiplatformv1.Usage{PromptTokens: 10, CompletionTokens: 20}, // total 缺失：要补齐
		FinishReason:    "stop",
		Model:           "deepseek-flash",
		Degraded:        true,
		DegradedReasons: []string{"rerank_skipped"},
		ElapsedMs:       1234,
	}}
	client := startFakeAI(t, fake)

	got, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	if err != nil {
		t.Fatalf("Chat 失败: %v", err)
	}

	if got.Content != "答案是 42。" || got.FinishReason != "stop" || got.Model != "deepseek-flash" {
		t.Errorf("基础字段不对: %+v", got)
	}
	if got.ConversationID != "cv_from_ai" {
		t.Errorf("conversation_id = %q（网关要靠它做不一致告警）", got.ConversationID)
	}
	if !got.Degraded || len(got.DegradedReasons) != 1 || got.DegradedReasons[0] != "rerank_skipped" {
		t.Errorf("降级信息不对: %+v", got)
	}
	if got.ElapsedMS != 1234 {
		t.Errorf("elapsed_ms = %d", got.ElapsedMS)
	}
	if got.Usage == nil || got.Usage.TotalTokens != 30 {
		t.Errorf("usage 未补齐 total: %+v", got.Usage)
	}

	// references：逐键断言线上 JSON 形状（与 app/schemas/chat.py::Reference 对齐）。
	var refs []map[string]any
	if err := json.Unmarshal(got.References, &refs); err != nil {
		t.Fatalf("references 不是合法 JSON: %v", err)
	}
	if len(refs) != 1 {
		t.Fatalf("references 长度 = %d", len(refs))
	}
	for _, key := range []string{
		"index", "chunk_id", "doc_id", "kb_id", "doc_name",
		"page", "heading_path", "score", "snippet", "content_sha256",
	} {
		if _, ok := refs[0][key]; !ok {
			t.Errorf("references[0] 缺少键 %q（契约里它是必填/有默认值）：%v", key, refs[0])
		}
	}
	if refs[0]["score"] != float64(0) {
		// `score` 是必填键：proto 结构体自带的 `,omitempty` 会把它丢掉，
		// 所以这里必须看到显式的 0。
		t.Errorf("score = %v, 期望显式的 0（omitempty 会把它丢掉）", refs[0]["score"])
	}

	// tool_calls：`arguments` 必须是**对象**（proto 传的是 JSON 字符串）。
	var calls []map[string]any
	if err := json.Unmarshal(got.ToolCalls, &calls); err != nil {
		t.Fatalf("tool_calls 不是合法 JSON: %v", err)
	}
	if len(calls) != 1 {
		t.Fatalf("tool_calls 长度 = %d", len(calls))
	}
	args, ok := calls[0]["arguments"].(map[string]any)
	if !ok {
		t.Fatalf("tool_calls[0].arguments 不是对象: %v", calls[0]["arguments"])
	}
	if args["q"] != "向量" {
		t.Errorf("arguments 内容不对: %v", args)
	}
	if calls[0]["status"] != "ok" || calls[0]["elapsed_ms"] != float64(37) {
		t.Errorf("tool_calls 其它字段不对: %v", calls[0])
	}
}

func TestChatEmptyOptionalSlicesStayNil(t *testing.T) {
	fake := &fakeAI{resp: &aiplatformv1.ChatResponse{Answer: "ok"}}
	client := startFakeAI(t, fake)

	got, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	if err != nil {
		t.Fatalf("Chat 失败: %v", err)
	}
	// 空列表留 nil（存储层用「空」表示 NULL），对外视图由 service 统一补成 `[]`。
	if got.References != nil || got.ToolCalls != nil {
		t.Errorf("空引用/工具轨迹应为 nil，得到 refs=%q calls=%q", got.References, got.ToolCalls)
	}
	if got.Usage != nil {
		t.Errorf("全零 usage 应为 nil（否则会白扣一次配额），得到 %+v", got.Usage)
	}
}

// ---- 错误映射（接缝 J2）----

func TestChatErrorEnvelopeIsPreserved(t *testing.T) {
	envelope := &aiplatformv1.AiError{
		Code:        "CONTEXT_TOO_LONG",
		Message:     "上下文超过模型上限",
		HttpStatus:  400,
		Retryable:   false,
		TraceId:     "ai-trace-1",
		DetailsJson: `{"limit":8192,"used":9000}`,
	}
	st, serr := status.New(codes.InvalidArgument, "CONTEXT_TOO_LONG").WithDetails(envelope)
	if serr != nil {
		t.Fatalf("构造状态失败: %v", serr)
	}
	fake := &fakeAI{err: st.Err()}
	client := startFakeAI(t, fake)

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	if err == nil {
		t.Fatal("期望错误")
	}
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T", err)
	}
	if appErr.Code() != "CONTEXT_TOO_LONG" {
		t.Errorf("code = %q（必须是 AI 的原值，不是网关猜的）", appErr.Code())
	}
	if appErr.Status() != 400 {
		t.Errorf("status = %d, 期望 400（沿用上游）", appErr.Status())
	}
	if appErr.TraceID() != "ai-trace-1" {
		t.Errorf("trace_id = %q（必须沿用 AI 的，不是网关自己的）", appErr.TraceID())
	}
	if appErr.Message() != "上下文超过模型上限" {
		t.Errorf("message = %q", appErr.Message())
	}
	if got := appErr.Details()["limit"]; got != float64(8192) {
		t.Errorf("details 未原样保留: %v", appErr.Details())
	}
	if got := appErr.Details()["gateway"]; got == nil {
		t.Errorf("透传错误应追加 details.gateway（docs/02-§4.2 规则 4）：%v", appErr.Details())
	}
	if !appErr.IsUpstream() {
		t.Error("透传错误应标记为 upstream")
	}
}

// TestChatTrustsOnly4xx5xxFromEnvelope 钉住「不盲信上游状态码」。
//
// AI 若把一个错误报成 200，照抄会让网关以「200 + 错误信封」回客户端 ——
// 客户端解析器会直接崩，比状态码不精确危险得多。
func TestChatTrustsOnly4xx5xxFromEnvelope(t *testing.T) {
	st, serr := status.New(codes.InvalidArgument, "INVALID_ARGUMENT").WithDetails(
		&aiplatformv1.AiError{Code: "INVALID_ARGUMENT", Message: "参数不合法", HttpStatus: 200})
	if serr != nil {
		t.Fatalf("构造状态失败: %v", serr)
	}
	fake := &fakeAI{err: st.Err()}
	client := startFakeAI(t, fake)

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T", err)
	}
	if appErr.Status() == 200 {
		t.Fatal("状态码被上游的 200 污染了：错误响应绝不能是 200")
	}
	if appErr.Status() != 400 {
		t.Errorf("status = %d, 期望按码表回退到 400", appErr.Status())
	}
}

func TestChatNonEnvelopeStatusIsNormalized(t *testing.T) {
	cases := []struct {
		name     string
		code     codes.Code
		wantCode errs.Code
	}{
		{"过载", codes.ResourceExhausted, errs.CodeAIOverloaded},
		{"不可用", codes.Unavailable, errs.CodeAIUnavailable},
		{"超时", codes.DeadlineExceeded, errs.CodeAITimeout},
		{"其它", codes.Internal, errs.CodeAIUnavailable},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeAI{err: status.Error(tc.code, "upstream boom")}
			client := startFakeAI(t, fake)

			_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
			appErr, ok := errs.As(err)
			if !ok {
				t.Fatalf("错误不是 *errs.AppError: %T", err)
			}
			if appErr.Code() != tc.wantCode {
				t.Errorf("code = %q, 期望 %q", appErr.Code(), tc.wantCode)
			}
			if got := appErr.Details()["reason"]; got != "non_envelope_status" {
				t.Errorf("details.reason = %v, 期望 non_envelope_status", got)
			}
			if got := appErr.Details()["grpc_code"]; got != tc.code.String() {
				t.Errorf("details.grpc_code = %v, 期望 %q", got, tc.code.String())
			}
		})
	}
}

func TestChatGatewayDeadlineMapsToAITimeout(t *testing.T) {
	fake := &fakeAI{
		resp:  &aiplatformv1.ChatResponse{Answer: "too late"},
		delay: 300 * time.Millisecond,
	}
	opt := testOptions(t)
	opt.ChatTimeout = 40 * time.Millisecond

	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	aiplatformv1.RegisterAiPlatformServer(srv, fake)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	conn, err := dialForTest(t, lis)
	if err != nil {
		t.Fatalf("建连失败: %v", err)
	}
	client := newChatOrchestratorFromConn(conn, opt)

	start := time.Now()
	_, err = client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	elapsed := time.Since(start)

	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != errs.CodeAITimeout {
		t.Errorf("code = %q, 期望 AI_TIMEOUT", appErr.Code())
	}
	if appErr.Status() != 504 {
		t.Errorf("status = %d, 期望 504", appErr.Status())
	}
	// 必须是我们自己的 deadline 先到（不是 AI 侧的 delay 跑完）。
	if elapsed > 250*time.Millisecond {
		t.Errorf("耗时 %v：看起来是等 AI 跑完而不是网关 deadline 生效", elapsed)
	}
	if got := appErr.Details()["reason"]; got != "gateway_deadline_exceeded" {
		t.Errorf("details.reason = %v", got)
	}
}

// TestChatUpstreamDeadlineStatusIsNotOurDeadline 覆盖「上游自己回 DeadlineExceeded」。
//
// 与 TestChatGatewayDeadlineMapsToAITimeout 是一对：两者最终都归成 AI_TIMEOUT / 504，
// 但 `details.reason` **必须不同** —— 前者是「AI 自己也认为超时了」（该看它的日志），
// 后者是「网关的 deadline 到了」（AI 卡死，该改 AI）。把这两个混成一个，
// 排障方向就被带偏了，而这正是当初写下 `mapError` 里那段顺序注释的原因。
func TestChatUpstreamDeadlineStatusIsNotOurDeadline(t *testing.T) {
	fake := &fakeAI{err: status.Error(codes.DeadlineExceeded, "upstream gave up")}
	client := startFakeAI(t, fake) // 注意：档位是 3s，绝不会是网关自己超时

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != errs.CodeAITimeout {
		t.Errorf("code = %q, 期望 AI_TIMEOUT", appErr.Code())
	}
	if got := appErr.Details()["reason"]; got != "non_envelope_status" {
		t.Errorf("details.reason = %v, 期望 non_envelope_status（上游回的超时不是网关的）", got)
	}
	if got := appErr.Details()["grpc_code"]; got != codes.DeadlineExceeded.String() {
		t.Errorf("details.grpc_code = %v, 期望 %v", got, codes.DeadlineExceeded)
	}
}

func TestChatTraceIDComesFromTrailer(t *testing.T) {
	fake := &fakeAI{
		err:      status.Error(codes.Unavailable, "down"),
		trailers: map[string]string{TrailerTraceID: "ai-trace-from-trailer"},
	}
	client := startFakeAI(t, fake)

	_, err := client.Chat(context.Background(), biz.ChatRequest{Query: "hi"})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T", err)
	}
	// 失败路径也要拿到 trace_id：这正是用 trailer 而不是普通 header 的理由。
	if appErr.TraceID() != "ai-trace-from-trailer" {
		t.Errorf("trace_id = %q, 期望来自 trailer 的值", appErr.TraceID())
	}
}
func TestChatMissingTargetIsConfigError(t *testing.T) {
	_, err := NewChatOrchestrator(context.Background(), Options{})
	if err == nil {
		t.Fatal("target 为空时应报配置错误")
	}
	// 错误信息必须点名那个变量：这是运维唯一能拿到的修好它的线索。
	if !strings.Contains(err.Error(), "AI_PLATFORM_GRPC_TARGET") {
		t.Errorf("错误信息未点名配置项: %v", err)
	}
}

// TestProductionDialHonoursOwnDeadlineNotKratosDefault 是 kratos 默认超时的回归测试。
//
// 踩过的坑：`kratosgrpc.DialInsecure` 的 `timeout` 默认值是 **2000ms**，
// 而且它**总是**装一个 unary 拦截器做 `context.WithTimeout(ctx, timeout)`。
// 于是「借它拿一条 ClientConn、再自己按档位设 deadline」得到的实际 deadline
// 是 `min(自己的档位, 2s)`：1.2s 的回答能过（看起来一切正常），
// 走了 RAG 的 3s 调用就 `DeadlineExceeded`，而且 reason 是
// `non_envelope_status`（被归成 AI 侧自己回的超时），把排查方向带偏。
//
// 这个测试**必须**走真实的 TCP + 生产构造函数 `NewChatOrchestrator`：
// bufconn + `newChatOrchestratorFromConn` 会绕过 kratos 的 dial 选项，
// 正是旧版本「测试全绿也发现不了」的原因。
func TestProductionDialHonoursOwnDeadlineNotKratosDefault(t *testing.T) {
	// 服务端故意睡 2.5s：比 kratos 的 2s 默认长，比第一个子测试的档位短。
	fake := &fakeAI{
		resp:  &aiplatformv1.ChatResponse{Answer: "慢回答", MessageId: "msg_slow"},
		delay: 2500 * time.Millisecond,
	}

	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听随机端口失败: %v", err)
	}
	srv := grpc.NewServer()
	aiplatformv1.RegisterAiPlatformServer(srv, fake)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	newOpts := func(chatTimeout time.Duration) Options {
		return Options{
			GRPCTarget:     lis.Addr().String(),
			ConnectTimeout: 2 * time.Second,
			ChatTimeout:    chatTimeout,
			MetaTimeout:    time.Second,
			MaxResponseMB:  4,
			Log:            slog.New(slog.NewTextHandler(io.Discard, nil)),
		}
	}

	t.Run("档位比 kratos 默认长时不被截断", func(t *testing.T) {
		orch, derr := NewChatOrchestrator(context.Background(), newOpts(6*time.Second))
		if derr != nil {
			t.Fatalf("建立 gRPC 客户端失败: %v", derr)
		}
		start := time.Now()
		got, cerr := orch.Chat(context.Background(), biz.ChatRequest{Query: "慢问题"})
		elapsed := time.Since(start)

		if cerr != nil {
			t.Fatalf("6s 档位下 2.5s 的回答应当成功，实际失败: %v"+
				"（若 code=AI_TIMEOUT 且 reason=non_envelope_status，"+
				"几乎可以肯定是 kratos 的 2s 默认超时被重新装回来了）", cerr)
		}
		if got.Content != "慢回答" {
			t.Errorf("Content = %q, 期望 慢回答", got.Content)
		}
		if elapsed < 2*time.Second {
			t.Errorf("耗时 %v 小于服务端延时，说明请求没有真的等那么久", elapsed)
		}
	})

	t.Run("档位比 kratos 默认短时仍然生效", func(t *testing.T) {
		// 这一条防的是「干脆把超时整个关掉」这种假修复：
		// 我们只是移除 kratos 的那一层，档位本身必须还有效。
		orch, derr := NewChatOrchestrator(context.Background(), newOpts(400*time.Millisecond))
		if derr != nil {
			t.Fatalf("建立 gRPC 客户端失败: %v", derr)
		}
		start := time.Now()
		_, cerr := orch.Chat(context.Background(), biz.ChatRequest{Query: "慢问题"})
		elapsed := time.Since(start)

		appErr, ok := errs.As(cerr)
		if !ok {
			t.Fatalf("错误不是 *errs.AppError: %T (%v)", cerr, cerr)
		}
		if appErr.Code() != errs.CodeAITimeout {
			t.Errorf("code = %q, 期望 AI_TIMEOUT", appErr.Code())
		}
		// 必须是网关自己的 deadline 先到：这一支的 reason 与「AI 侧回的超时」不同。
		if got := appErr.Details()["reason"]; got != "gateway_deadline_exceeded" {
			t.Errorf("details.reason = %v, 期望 gateway_deadline_exceeded", got)
		}
		if elapsed > 2*time.Second {
			t.Errorf("耗时 %v：400ms 的档位没有生效", elapsed)
		}
	})
}

// ---- 小工具 ----

func strPtr(s string) *string { return &s }
