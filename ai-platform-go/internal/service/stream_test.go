package service

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ssex"
)

// 本文件覆盖流式提问的**传输层**：SSE 响应头、逐帧写出的时机、错误帧的追加、
// 以及「一帧都没写出去时仍回 JSON 信封」这条边界。
//
// 用 `httptest` + 真实 gin 引擎而不是直接调 handler：这里要断言的正是
// 「HTTP 状态码什么时候被写出去」，而那个事实只存在于真实响应里。

// ---- 假仓储（只实现流式路径真正会走到的方法）----

type stubConvRepo struct {
	conv *biz.Conversation
	err  error
}

func (r *stubConvRepo) Create(context.Context, *biz.Conversation) error { return nil }

func (r *stubConvRepo) GetOwned(context.Context, string, string) (*biz.Conversation, error) {
	if r.err != nil {
		return nil, r.err
	}
	cp := *r.conv
	return &cp, nil
}

func (r *stubConvRepo) List(context.Context, string, biz.ListConversationsInput) (*biz.ConversationList, error) {
	return &biz.ConversationList{}, nil
}

func (r *stubConvRepo) Update(context.Context, string, string, biz.ConversationPatch, time.Time) error {
	return nil
}

func (r *stubConvRepo) SetAutoTitle(context.Context, string, string, string, time.Time) (bool, error) {
	return false, nil
}

func (r *stubConvRepo) SoftDelete(context.Context, string, string, time.Time) error { return nil }

type stubMsgRepo struct {
	appended []*biz.Message
	// appendCtxErrs 记录每次 Append 看到的 ctx 状态（用来证伪 WithoutCancel）。
	appendCtxErrs []error

	appendErr       error
	failAppendAfter int
	calls           int
	seq             int
}

func (r *stubMsgRepo) Append(ctx context.Context, _, _ string, m *biz.Message) error {
	r.calls++
	r.appendCtxErrs = append(r.appendCtxErrs, ctx.Err())
	if r.appendErr != nil {
		return r.appendErr
	}
	if r.failAppendAfter > 0 && r.calls >= r.failAppendAfter {
		return biz.ErrNotFound // 由 data 层包成 500；这里只要「是个错误」
	}
	r.seq++
	m.Seq = r.seq
	cp := *m
	r.appended = append(r.appended, &cp)
	return nil
}

func (r *stubMsgRepo) GetOwned(context.Context, string, string) (*biz.Message, error) {
	return nil, biz.ErrNotFound
}

func (r *stubMsgRepo) ListByConversation(context.Context, string, string, biz.ListMessagesInput) (*biz.MessageList, error) {
	return &biz.MessageList{}, nil
}

func (r *stubMsgRepo) Delete(context.Context, string, string) error { return nil }

type stubStreamer struct {
	events []biz.StreamEvent
	err    error
	// block 为真时事件推完也不关流（用于验证「首字节超时」这类等待分支）。
	block bool
	// hangForever 为真时一个事件都不给且不关流。
	hangForever bool
}

func (s *stubStreamer) ChatStream(ctx context.Context, _ biz.ChatRequest) (biz.ChatEventStream, error) {
	if s.err != nil {
		return nil, s.err
	}
	ch := make(chan biz.StreamEvent, len(s.events)+1)
	for _, ev := range s.events {
		ch <- ev
	}
	if !s.block && !s.hangForever {
		close(ch)
	}
	return &stubEventStream{events: ch, ctx: ctx}, nil
}

type stubEventStream struct {
	events chan biz.StreamEvent
	ctx    context.Context
	err    error
	closed int
}

func (s *stubEventStream) Events() <-chan biz.StreamEvent { return s.events }
func (s *stubEventStream) Err() error                     { return s.err }
func (s *stubEventStream) Close() error                   { s.closed++; return nil }

func newStreamHandler(t *testing.T, streamer *stubStreamer, msgs *stubMsgRepo, conv *biz.Conversation) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)

	svc := biz.NewMessageService(biz.MessageDeps{
		Conversations:     &stubConvRepo{conv: conv},
		Messages:          msgs,
		Streamer:          streamer,
		Log:               slog.New(slog.NewTextHandler(discard{}, nil)),
		AutoTitleMaxChars: biz.AutoTitleMaxCharsDefault,
	})

	r := gin.New()
	r.POST("/conversations/:conversation_id/messages/stream", func(c *gin.Context) {
		middleware.SetUserID(c, "u_1")
		NewMessageHandler(svc).Stream(c)
	})
	return r
}

type discard struct{}

func (discard) Write(p []byte) (int, error) { return len(p), nil }

func sampleConv() *biz.Conversation {
	return &biz.Conversation{
		ID:          "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
		UserID:      "u_1",
		Status:      biz.ConversationStatusActive,
		TitleSource: biz.TitleSourceAuto,
		CreatedAt:   time.Now(),
	}
}

func postStream(t *testing.T, r *gin.Engine, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost,
		"/conversations/cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C/messages/stream",
		strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// frames 把响应体切成帧（每帧形如 `event: x\ndata: {...}\n\n`）。
func frames(t *testing.T, body string) [][2]string {
	t.Helper()
	var out [][2]string
	for _, block := range strings.Split(body, "\n\n") {
		block = strings.TrimSpace(block)
		if block == "" {
			continue
		}
		var name, data string
		for _, line := range strings.Split(block, "\n") {
			switch {
			case strings.HasPrefix(line, "event: "):
				name = strings.TrimPrefix(line, "event: ")
			case strings.HasPrefix(line, "data: "):
				data = strings.TrimPrefix(line, "data: ")
			}
		}
		out = append(out, [2]string{name, data})
	}
	return out
}

// ---- 正常路径 ----

func TestStreamHandlerRendersEventSequence(t *testing.T) {
	conv := sampleConv()
	msgs := &stubMsgRepo{}
	streamer := &stubStreamer{events: []biz.StreamEvent{
		biz.StreamMetaEvent{ConversationID: conv.ID, MessageID: "msg_ignored", Model: "m", Degraded: true},
		biz.StreamReferenceEvent{References: json.RawMessage(`[{"index":1}]`)},
		biz.StreamTokenEvent{Delta: "你"},
		biz.StreamTokenEvent{Delta: "好"},
		biz.StreamUsageEvent{Usage: biz.MessageUsage{TotalTokens: 3}},
		biz.StreamDoneEvent{FinishReason: biz.FinishReasonStop, ElapsedMS: 42},
	}}
	r := newStreamHandler(t, streamer, msgs, conv)
	w := postStream(t, r, `{"content":"你好"}`)

	if w.Code != http.StatusOK {
		t.Fatalf("状态码应为 200，实际 %d（%s）", w.Code, w.Body.String())
	}
	// SSE 的三个响应头缺一不可：Content-Type 决定客户端把它当流解析，
	// Cache-Control/no-buffer 决定中间的代理与浏览器不把帧攒起来。
	for k, want := range ssex.Headers() {
		if got := w.Header().Get(k); got != want {
			t.Errorf("响应头 %s 期望 %q，实际 %q", k, want, got)
		}
	}

	got := frames(t, w.Body.String())
	want := []string{"meta", "reference", "token", "token", "usage", "done"}
	if len(got) != len(want) {
		t.Fatalf("帧数应为 %d，实际 %d：%s", len(want), len(got), w.Body.String())
	}
	for i, name := range want {
		if got[i][0] != name {
			t.Fatalf("第 %d 帧应为 %s，实际 %s（%s）", i, name, got[i][0], w.Body.String())
		}
	}

	// meta 帧里的 message_id 必须是**网关自己**生成的那个（与落库同一个），
	// 否则客户端拿 done 之后的 ID 去 GET /messages/{id} 会 404。
	var meta struct {
		ConversationID string `json:"conversation_id"`
		MessageID      string `json:"message_id"`
		Degraded       bool   `json:"degraded"`
	}
	if err := json.Unmarshal([]byte(got[0][1]), &meta); err != nil {
		t.Fatalf("meta 帧不是合法 JSON：%v（%s）", err, got[0][1])
	}
	if meta.ConversationID != conv.ID {
		t.Errorf("meta.conversation_id 应为网关的 %q，实际 %q", conv.ID, meta.ConversationID)
	}
	if meta.MessageID == "" || meta.MessageID == "msg_ignored" {
		t.Errorf("meta.message_id 应为网关生成的 ID，实际 %q", meta.MessageID)
	}
	if !meta.Degraded {
		t.Error("degraded 应透传（客户端据此展示「降级」提示）")
	}

	// 引用帧的负载是**对象**（`{"references":[...]}`），不是裸数组：
	// 直接发数组会让 `data.references` 变成 undefined。
	var refPayload struct {
		References json.RawMessage `json:"references"`
	}
	if err := json.Unmarshal([]byte(got[1][1]), &refPayload); err != nil {
		t.Fatalf("reference 帧解析失败：%v", err)
	}
	if !strings.HasPrefix(string(refPayload.References), "[") {
		t.Errorf("references 应为数组，实际 %s", refPayload.References)
	}

	// done 帧必须带 partial（AI 自己知道这轮不完整时客户端要能区分）。
	var done struct {
		FinishReason string `json:"finish_reason"`
		ElapsedMS    int    `json:"elapsed_ms"`
		Partial      bool   `json:"partial"`
	}
	if err := json.Unmarshal([]byte(got[5][1]), &done); err != nil {
		t.Fatalf("done 帧解析失败：%v", err)
	}
	if done.FinishReason != biz.FinishReasonStop || done.ElapsedMS != 42 {
		t.Errorf("done 帧内容错误：%#v", done)
	}

	// 收尾必须落库，且 ID 与 meta 帧预告的一致。
	if len(msgs.appended) != 2 {
		t.Fatalf("应落 user+assistant 两条，实际 %d 条", len(msgs.appended))
	}
	if msgs.appended[1].ID != meta.MessageID {
		t.Errorf("落库 ID 应与 meta 帧一致，实际 %q / %q", msgs.appended[1].ID, meta.MessageID)
	}
	if msgs.appended[1].Content != "你好" {
		t.Errorf("正文应为 %q，实际 %q", "你好", msgs.appended[1].Content)
	}
}

func TestStreamHandlerPassesUnknownEventThroughUnchanged(t *testing.T) {
	conv := sampleConv()
	// 未知事件的原样透传在传输层最容易做错：写成 `data: {"x":1}` 之前
	// 先 unmarshal 成 map 再 marshal 一次就会改变字段顺序/数字精度。
	raw := `{"n":1.10,"z":"——","nested":{"a":[1,2]}}`
	streamer := &stubStreamer{events: []biz.StreamEvent{
		biz.StreamUnknownEvent{Name: "citation_note", Data: json.RawMessage(raw)},
		biz.StreamDoneEvent{FinishReason: biz.FinishReasonStop},
	}}
	r := newStreamHandler(t, streamer, &stubMsgRepo{}, conv)
	w := postStream(t, r, `{"content":"q"}`)

	got := frames(t, w.Body.String())
	if got[0][0] != "citation_note" {
		t.Fatalf("未知事件名必须原样下发，实际 %q", got[0][0])
	}
	if got[0][1] != raw {
		t.Fatalf("未知事件负载必须**逐字节**原样下发，实际 %s", got[0][1])
	}
}

func TestStreamHandlerWrapsInvalidUnknownPayload(t *testing.T) {
	conv := sampleConv()
	// gRPC 的 `unknown.data_json` 是 bytes，上游可以塞非 JSON。
	// 直接下发会让客户端**整条流**解析中断（不是丢一帧），所以这里包成字符串。
	streamer := &stubStreamer{events: []biz.StreamEvent{
		biz.StreamUnknownEvent{Name: "weird", Data: json.RawMessage(`not json`)},
	}}
	r := newStreamHandler(t, streamer, &stubMsgRepo{}, conv)
	w := postStream(t, r, `{"content":"q"}`)

	got := frames(t, w.Body.String())
	if !json.Valid([]byte(got[0][1])) {
		t.Fatalf("下发帧必须是合法 JSON（否则客户端整条流解析中断），实际 %s", got[0][1])
	}
	var s string
	if err := json.Unmarshal([]byte(got[0][1]), &s); err != nil || s != "not json" {
		t.Fatalf("非法负载应包成 JSON 字符串且保留原文，实际 %s（%v）", got[0][1], err)
	}
}

// ---- 边界：还没写出帧时的错误 ----

func TestStreamHandlerNotStartedErrorReturnsJSONEnvelope(t *testing.T) {
	conv := sampleConv()
	// 首字节超时（上游连上了但不回话）：pump 从来不 Send，所以响应头还没写出去，
	// 这里必须能回 504 —— 这正是 `sseSink` 把响应头推迟到第一帧的原因。
	streamer := &stubStreamer{hangForever: true}
	svcSide := newStreamHandlerWithTimeouts(t, streamer, &stubMsgRepo{}, conv, 40*time.Millisecond)
	w := postStream(t, svcSide, `{"content":"q"}`)

	if w.Code != http.StatusGatewayTimeout {
		t.Fatalf("首字节超时应回 504，实际 %d（%s）", w.Code, w.Body.String())
	}
	if ct := w.Header().Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Fatalf("未开流时必须是 JSON 信封，实际 Content-Type=%q", ct)
	}
	if strings.Contains(w.Body.String(), "event:") {
		t.Fatalf("未开流时不应有 SSE 帧，实际 %s", w.Body.String())
	}
}

func TestStreamHandlerStartedErrorDoesNotRewriteStatus(t *testing.T) {
	conv := sampleConv()
	// 已经推过帧之后失败（这里用「上游没发 done 就断了」触发）：
	// 状态码必须**仍然是 200**，且 body 里不能混进 JSON 信封
	// （混进去会让客户端在流里收到一段不是帧的东西）。
	streamer := &stubStreamer{events: []biz.StreamEvent{biz.StreamTokenEvent{Delta: "半"}}}
	r := newStreamHandler(t, streamer, &stubMsgRepo{}, conv)
	w := postStream(t, r, `{"content":"q"}`)

	if w.Code != http.StatusOK {
		t.Fatalf("已开流后状态码必须保持 200，实际 %d", w.Code)
	}
	body := w.Body.String()
	if strings.Contains(body, `"error"`) && !strings.Contains(body, "event: ") {
		t.Fatalf("body 里混进了 JSON 信封：%s", body)
	}
	if !strings.HasSuffix(body, "\n\n") {
		t.Errorf("最后一帧应以空行结束（否则客户端会一直等这一帧的结束），实际 %q", body)
	}
}

// ---- 帧格式 ----

func TestStreamHandlerFrameEndsWithBlankLineAndNoBareNewlines(t *testing.T) {
	conv := sampleConv()
	streamer := &stubStreamer{events: []biz.StreamEvent{
		// 正文里带**裸换行**：SSE 的帧分隔符就是空行，所以它必须被
		// JSON 转义成 `\n`（`ssex.Frame` 负责），否则一帧会变成两帧，
		// 客户端拿到第二段时 JSON 解析失败 —— 表现为「回答从换行处开始乱掉」。
		biz.StreamTokenEvent{Delta: "第一行\n第二行"},
		biz.StreamDoneEvent{FinishReason: biz.FinishReasonStop},
	}}
	r := newStreamHandler(t, streamer, &stubMsgRepo{}, conv)
	w := postStream(t, r, `{"content":"q"}`)

	got := frames(t, w.Body.String())
	if len(got) != 2 {
		t.Fatalf("正文里的换行不应把一帧切成两帧，实际 %d 帧：%q", len(got), w.Body.String())
	}
	var payload struct {
		Delta string `json:"delta"`
	}
	if err := json.Unmarshal([]byte(got[0][1]), &payload); err != nil {
		t.Fatalf("token 帧解析失败：%v（%s）", err, got[0][1])
	}
	if payload.Delta != "第一行\n第二行" {
		t.Fatalf("正文应逐字还原（含换行），实际 %q", payload.Delta)
	}
}

// ---- 落库失败 ----

func TestStreamHandlerAppendsPersistErrorFrame(t *testing.T) {
	conv := sampleConv()
	msgs := &stubMsgRepo{}
	streamer := &stubStreamer{events: []biz.StreamEvent{
		biz.StreamTokenEvent{Delta: "你好"},
		biz.StreamDoneEvent{FinishReason: biz.FinishReasonStop},
	}}
	// 让 assistant 的 Append 失败（第 2 次）。
	r := newStreamHandler(t, streamer, msgs, conv)
	msgs.failAppendAfter = 2
	w := postStream(t, r, `{"content":"q"}`)

	got := frames(t, w.Body.String())
	if len(got) != 3 || got[2][0] != "gw_persist_error" {
		t.Fatalf("落库失败应在 done 之后追加 gw_persist_error，实际 %v", got)
	}
	var payload struct {
		Reason string `json:"reason"`
	}
	if err := json.Unmarshal([]byte(got[2][1]), &payload); err != nil {
		t.Fatalf("gw_persist_error 帧解析失败：%v", err)
	}
	if payload.Reason == "" {
		t.Fatal("reason 不能为空（它是客户端唯一能据此提示用户的线索）")
	}
	// 状态码不能因为落库失败而改（响应早就发出去了）。
	if w.Code != http.StatusOK {
		t.Fatalf("落库失败不应改变已发出的状态码，实际 %d", w.Code)
	}
}

// ---- 校验失败（最早的边界）----

func TestStreamHandlerRejectsEmptyContentBeforeAnyFrame(t *testing.T) {
	conv := sampleConv()
	streamer := &stubStreamer{events: []biz.StreamEvent{biz.StreamDoneEvent{}}}
	r := newStreamHandler(t, streamer, &stubMsgRepo{}, conv)
	w := postStream(t, r, `{"content":"   "}`)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("空内容应回 400，实际 %d（%s）", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Header().Get("Content-Type"), "application/json") {
		t.Fatalf("校验失败必须是 JSON 信封，实际 %q", w.Header().Get("Content-Type"))
	}
}

// newStreamHandlerWithTimeouts 允许覆盖首字节超时（默认 35s 在测试里等不起）。
func newStreamHandlerWithTimeouts(
	t *testing.T, streamer *stubStreamer, msgs *stubMsgRepo, conv *biz.Conversation, firstByte time.Duration,
) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)

	svc := biz.NewMessageService(biz.MessageDeps{
		Conversations:          &stubConvRepo{conv: conv},
		Messages:               msgs,
		Streamer:               streamer,
		Log:                    slog.New(slog.NewTextHandler(discard{}, nil)),
		AutoTitleMaxChars:      biz.AutoTitleMaxCharsDefault,
		StreamFirstByteTimeout: firstByte,
		StreamIdleTimeout:      5 * time.Second,
		StreamTotalTimeout:     10 * time.Second,
	})

	r := gin.New()
	r.POST("/conversations/:conversation_id/messages/stream", func(c *gin.Context) {
		middleware.SetUserID(c, "u_1")
		NewMessageHandler(svc).Stream(c)
	})
	return r
}
