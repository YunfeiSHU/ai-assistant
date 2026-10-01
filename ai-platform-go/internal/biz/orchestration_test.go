package biz

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// ---- 测试用日志捕获 ----

// logBuffer 既实现 io.Writer（给 slog 写），又保留一份可读文本。
//
// 需要它是因为「降级/不一致」这类**非致命**情况只能通过日志观察：
// 它们不改变返回值，所以不写日志的话，测试无从断言「这件事被记录下来了」。
type logBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *logBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *logBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// testLogger 返回一个把日志收集到 buf 的 logger；buf 为 nil 时丢弃。
func testLogger(buf *strings.Builder) *slog.Logger {
	if buf == nil {
		return slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return slog.New(slog.NewTextHandler(&sbWriter{sb: buf}, nil))
}

type sbWriter struct{ sb *strings.Builder }

func (w *sbWriter) Write(p []byte) (int, error) { return w.sb.WriteString(string(p)) }

// newOrchFixture 组装一个「已有历史消息」的会话，用于验证历史拼装。
//
// 历史用固定的 user/assistant 交替序列，断言时可以逐条比对顺序与角色，
// 而不是只看条数 —— 只看条数的话「顺序反了」和「角色串了」都发现不了。
func newOrchFixture(t *testing.T, turns int) (*MessageService, *fakeMsgRepo, *fakeOrchestrator, *Conversation, *logBuffer) {
	t.Helper()

	conv := &Conversation{
		ID:          "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
		UserID:      "u_1",
		Status:      ConversationStatusActive,
		TitleSource: TitleSourceAuto,
		CreatedAt:   testNow(),
	}
	convs := newFakeConvRepo(conv)
	msgs := newFakeMsgRepo()

	// 6 轮历史 = 12 条消息（序号 1..12）。
	for i := 1; i <= 6; i++ {
		u := &Message{
			ID: "msg_u" + string(rune('0'+i)), ConversationID: conv.ID, UserID: "u_1",
			Role: MessageRoleUser, Content: "问题" + string(rune('0'+i)),
			Status: MessageStatusCompleted, CreatedAt: testNow(),
		}
		a := &Message{
			ID: "msg_a" + string(rune('0'+i)), ConversationID: conv.ID, UserID: "u_1",
			Role: MessageRoleAssistant, Content: "回答" + string(rune('0'+i)),
			Status: MessageStatusCompleted, CreatedAt: testNow(),
		}
		if err := msgs.Append(context.Background(), "u_1", conv.ID, u); err != nil {
			t.Fatalf("播种历史失败: %v", err)
		}
		if err := msgs.Append(context.Background(), "u_1", conv.ID, a); err != nil {
			t.Fatalf("播种历史失败: %v", err)
		}
	}

	orch := &fakeOrchestrator{result: &ChatResult{
		Content:      "这是回答",
		FinishReason: FinishReasonStop,
		Model:        "deepseek-flash",
		Usage:        &MessageUsage{PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30},
		ElapsedMS:    1234,
	}}

	logs := &logBuffer{}
	svc := NewMessageService(MessageDeps{
		Conversations:        convs,
		Messages:             msgs,
		Orchestrator:         orch,
		Clock:                testNow,
		Log:                  slog.New(slog.NewTextHandler(logs, nil)),
		AutoTitleMaxChars:    AutoTitleMaxCharsDefault,
		HistoryFallbackTurns: turns,
	})
	return svc, msgs, orch, conv, logs
}

// ---- 历史拼装（J4）----

// TestSendSendsHistoryWhenMemoryDisabled：网关侧没有记忆能力（AI 的
// `use_memory` 关掉时），**网关必须自己补历史**，否则模型每次都是无状态的，
// 多轮对话会答非所问（REQ-ORCH-006）。
func TestSendSendsHistoryWhenMemoryDisabled(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 10)

	useMemory := false
	in := SendMessageInput{Content: "新问题", UseMemory: &useMemory, UseRAG: boolPtr(false)}
	got, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{TraceID: "tr", UserToken: "tok"})
	if err != nil {
		t.Fatalf("Send 失败: %v", err)
	}

	if len(orch.got.History) != 12 {
		t.Fatalf("history 条数 = %d, 期望 12（全部历史）", len(orch.got.History))
	}
	// 顺序必须是时间正序，且首条是最早的 user 消息。
	if orch.got.History[0].Role != MessageRoleUser || orch.got.History[0].Content != "问题1" {
		t.Errorf("history 首条 = %+v，期望最早的 user 消息", orch.got.History[0])
	}
	if last := orch.got.History[len(orch.got.History)-1]; last.Role != MessageRoleAssistant || last.Content != "回答6" {
		t.Errorf("history 末条 = %+v，期望最后一条 assistant 消息", last)
	}
	// 刚刚写入的 user 消息（seq=13）不能出现在历史里 —— 它会以 `query` 单独下发，
	// 重复下发同一句话会让模型把它当成「用户重复问了两遍」。
	for _, m := range orch.got.History {
		if m.Content == "新问题" {
			t.Error("当前轮用户消息重复出现在 history 里")
		}
	}
	if got.User.Seq != 13 {
		t.Errorf("user 消息 seq = %d, 期望 13", got.User.Seq)
	}
}

// TestSendSkipsHistoryWhenMemoryEnabled：AI 侧自己有会话记忆时，
// 网关再补一份历史会导致同一段对话被下发两次（token 翻倍且可能矛盾）。
func TestSendSkipsHistoryWhenMemoryEnabled(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 10)

	useMemory := true
	in := SendMessageInput{Content: "新问题", UseMemory: &useMemory, UseRAG: boolPtr(false)}
	if _, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if orch.got.History != nil {
		t.Errorf("use_memory=true 时不应下发 history，实际 %d 条", len(orch.got.History))
	}
}

// TestSendHistoryRespectsTurnCap：历史必须有上限，否则长会话会撑爆上下文窗口。
// 上限单位是**轮**（user+assistant 各算一条），限制 2 轮时最多 2*2+1=5 条。
func TestSendHistoryRespectsTurnCap(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 2)

	useMemory := false
	in := SendMessageInput{Content: "新问题", UseMemory: &useMemory, UseRAG: boolPtr(false)}
	if _, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}

	// 12 条消息，上限 5 条 → 取最近 5 条（seq 9..13）并去掉当轮 user 消息（seq 13），
	// 剩下 seq 9..12 = 问题5 / 回答5 / 问题6 / 回答6。
	if len(orch.got.History) != 4 {
		t.Fatalf("history 条数 = %d, 期望 4（最近 5 条里去掉当轮 user 消息）", len(orch.got.History))
	}
	if orch.got.History[0].Content != "问题5" {
		t.Errorf("history 应从最近的窗口开始（期望首条为「问题5」），实际首条 %q", orch.got.History[0].Content)
	}
}

// TestSendHistoryIsTruncatedNotDropped：histories 被截断时必须是**保留最近的**，
// 而不是保留最早的 —— 一个「保留了最早几条」的实现同样能通过条数断言。
func TestSendHistoryIsTruncatedNotDropped(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 1)

	useMemory := false
	in := SendMessageInput{Content: "新问题", UseMemory: &useMemory, UseRAG: boolPtr(false)}
	if _, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if n := len(orch.got.History); n != 2 {
		t.Fatalf("history 条数 = %d, 期望 2（1 轮 = 3 条里去掉当轮 user 消息）", n)
	}
	if orch.got.History[0].Content != "问题6" || orch.got.History[1].Content != "回答6" {
		t.Errorf("应保留最近一轮，实际 %+v", orch.got.History)
	}
}

// TestSendHistoryFailureIsNonFatal：历史只是**增强**，读不到不该让整轮对话失败。
// 但必须有日志，否则「history 为什么一直是空的」会变成一个查不出来的悬案。
func TestSendHistoryFailureIsNonFatal(t *testing.T) {
	svc, msgs, orch, conv, logs := newOrchFixture(t, 10)
	msgs.listErr = errServer("db down")

	useMemory := false
	in := SendMessageInput{Content: "新问题", UseMemory: &useMemory, UseRAG: boolPtr(false)}
	got, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{})
	if err != nil {
		t.Fatalf("历史读取失败不应让 Send 失败: %v", err)
	}
	if got.Assistant == nil || got.Assistant.Content != "这是回答" {
		t.Errorf("仍应拿到模型回答: %+v", got.Assistant)
	}
	if orch.got.History != nil {
		t.Errorf("读历史失败后不应下发部分历史: %+v", orch.got.History)
	}
	if !strings.Contains(logs.String(), "message.history_fallback_failed") {
		t.Errorf("历史降级缺少日志:\n%s", logs.String())
	}
}

// ---- 凭据透传（J1）----

// TestSendForwardsUserToken：AI 侧用**用户自己的 token** 鉴权，
// 网关不伪造 `X-User-Id`（那样 AI 无法做二次校验，见 docs/04-§7）。
func TestSendForwardsUserToken(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 1)
	in := SendMessageInput{Content: "q"}
	if _, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{UserToken: "user-jwt"}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if orch.got.UserToken != "user-jwt" {
		t.Errorf("UserToken = %q, 期望透传 user-jwt", orch.got.UserToken)
	}
	if orch.got.ConversationID != conv.ID {
		t.Errorf("ConversationID = %q, 期望 %q", orch.got.ConversationID, conv.ID)
	}
}

// ---- 会话 id 归属（REQ-ORCH-006）----

// TestSendKeepsGatewayConversationOnUpstreamMismatch：网关是会话 id 的唯一
// 权威。AI 回了一个不同的 id（自己建了会话、或缓存串了）时，落库仍必须用
// 网关的 id —— 否则用户下次按网关 id 查会话，会看到一条断掉的对话。
// 同时必须留下日志 + 指标名，否则这种不一致会无声无息。
func TestSendKeepsGatewayConversationOnUpstreamMismatch(t *testing.T) {
	svc, _, orch, conv, logs := newOrchFixture(t, 1)
	orch.result.ConversationID = "cv_other_upstream"

	got, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("q"), RequestMeta{TraceID: "tr-1"})
	if err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if got.Assistant.ConversationID != conv.ID {
		t.Errorf("落库会话 = %q, 期望网关的 %q", got.Assistant.ConversationID, conv.ID)
	}
	logsText := logs.String()
	for _, want := range []string{"message.session_mismatch", "gw_session_mismatch_total", "cv_other_upstream"} {
		if !strings.Contains(logsText, want) {
			t.Errorf("日志缺少 %q:\n%s", want, logsText)
		}
	}
}

// TestSendDoesNotWarnWhenUpstreamEchoesNothing：AI 不回 conversation_id 是
// 正常情形（它只是复述），不能每次调用都刷一条 WARN —— 噪音会让真正的
// 不一致被淹没。
func TestSendDoesNotWarnWhenUpstreamEchoesNothing(t *testing.T) {
	svc, _, orch, conv, logs := newOrchFixture(t, 1)
	orch.result.ConversationID = ""

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("q"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if strings.Contains(logs.String(), "message.session_mismatch") {
		t.Errorf("空 conversation_id 不应触发不一致告警:\n%s", logs.String())
	}
}

// TestSendDoesNotWarnWhenUpstreamEchoesSame：正常复述同一 id 时也不能告警。
func TestSendDoesNotWarnWhenUpstreamEchoesSame(t *testing.T) {
	svc, _, orch, conv, logs := newOrchFixture(t, 1)
	orch.result.ConversationID = conv.ID

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("q"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if strings.Contains(logs.String(), "message.session_mismatch") {
		t.Errorf("复述同一 id 不应告警:\n%s", logs.String())
	}
}

// ---- 上游失败 ----

// TestSendUpstreamErrorIsEnvelope：编排层已经把 AI 的错误信封转成
// `*errs.AppError`，biz 不该再包一层（包了就会变成 500 + 丢掉上游 code）。
func TestSendUpstreamErrorIsEnvelope(t *testing.T) {
	svc, _, orch, conv, _ := newOrchFixture(t, 1)
	wantErr := errs.UpstreamError(errs.Code("OVERLOADED"), "AI 侧过载", 503, true, "", nil)
	orch.err = wantErr

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("q"), RequestMeta{})
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("错误不是 *errs.AppError: %T (%v)", err, err)
	}
	if appErr.Code() != "OVERLOADED" || appErr.Status() != 503 {
		t.Errorf("code=%q status=%d，上游错误被改写", appErr.Code(), appErr.Status())
	}
}

func boolPtr(v bool) *bool { return &v }
