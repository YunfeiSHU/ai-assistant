package biz

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件覆盖 M4 的流式编排（`POST .../messages/stream`）。
//
// 测试策略：**全部用可控的假事件流**（`fakeEventStream`），不碰网络。
// 重点不是「正常路径能跑」，而是那些「不写测试就一定会错」的地方：
// 落库用的 ctx 有没有脱离取消、引用是替换还是追加、被截断后状态是 partial、
// 写失败之后有没有停下、以及超时/退出/断连三条路径各发什么帧。

// streamFixture 是流式用例的公共装配。
type streamFixture struct {
	svc      *MessageService
	convs    *fakeConvRepo
	msgs     *fakeMsgRepo
	streamer *fakeStreamer
	stream   *fakeEventStream
	sink     *fakeSink
	conv     *Conversation
	shutdown chan struct{}
}

func newStreamFixture(t *testing.T) *streamFixture {
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
	stream := newOpenFakeEventStream(16)
	streamer := &fakeStreamer{stream: stream}
	shutdown := make(chan struct{})

	svc := NewMessageService(MessageDeps{
		Conversations:     convs,
		Messages:          msgs,
		Streamer:          streamer,
		Clock:             testNow,
		AutoTitleMaxChars: AutoTitleMaxCharsDefault,
		Shutdown:          shutdown,
	})
	return &streamFixture{
		svc:      svc,
		convs:    convs,
		msgs:     msgs,
		streamer: streamer,
		stream:   stream,
		sink:     &fakeSink{},
		conv:     conv,
		shutdown: shutdown,
	}
}

// start 在后台发起一次流式提问，返回一个「等结束」的函数。
//
// 必须放到 goroutine 里：`StreamSend` 会一直阻塞在等事件上，而测试要在
// 这期间往假流里 push 事件（这正是流式的本质，也是非流式测试里没有的形状）。
func (f *streamFixture) start(t *testing.T, ctx context.Context) func() error {
	t.Helper()
	done := make(chan error, 1)
	go func() {
		done <- f.svc.StreamSend(ctx, "u_1", f.conv.ID, sendInput("你好"), RequestMeta{TraceID: "trace-1"}, f.sink)
	}()
	return func() error {
		select {
		case err := <-done:
			return err
		case <-time.After(5 * time.Second):
			t.Fatal("StreamSend 超时未返回（多半是某个分支没被唤醒）")
			return nil
		}
	}
}

// persistAssistant 返回落库的 assistant 消息（没有则返回 nil）。
func (f *streamFixture) persistAssistant() *Message {
	for _, m := range f.msgs.appended {
		if m.Role == MessageRoleAssistant {
			return m
		}
	}
	return nil
}

// waitForFrames 等到至少 n 帧下发出去（或超时失败）。
//
// 流式用例都靠它把「流到一半」这个状态表达出来：不加等待就只能靠
// `time.Sleep` 猜，或者接受另一个随机结果（见 shutdown 用例里的注释）。
func (f *streamFixture) waitForFrames(t *testing.T, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for f.sink.count() < n {
		if time.Now().After(deadline) {
			t.Fatalf("等不到第 %d 帧（已收到 %v）", n, f.sink.events())
		}
		time.Sleep(time.Millisecond)
	}
}

// assertDetail 断言错误的 `details[key]`。
//
// 三个超时（首字节/空闲/整轮）共用 `AI_TIMEOUT` 一个码，**只有 reason 能区分**
// 它们：不区分的话线上看到「AI 超时」根本不知道该调哪一档超时配置。
func assertDetail(t *testing.T, err error, key, want string) {
	t.Helper()
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("期望 *errs.AppError，实际 %T: %v", err, err)
	}
	if got := appErr.Details()[key]; got != want {
		t.Fatalf("details[%q] 期望 %q，实际 %#v（全部 details：%#v）", key, want, got, appErr.Details())
	}
}

func metaEvent(conversationID string) StreamMetaEvent {
	return StreamMetaEvent{
		ConversationID: conversationID,
		MessageID:      "msg_upstream_1",
		Model:          "deepseek-flash",
		Degraded:       false,
	}
}

func tokenEvent(delta string) StreamTokenEvent { return StreamTokenEvent{Delta: delta} }

func doneEvent() StreamDoneEvent {
	return StreamDoneEvent{FinishReason: FinishReasonStop, ElapsedMS: 1234}
}

// ---- 正常路径 ----

func TestStreamSendForwardsFramesAndPersistsCompleted(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(metaEvent("cv_upstream_other"))
	f.stream.push(StreamReferenceEvent{References: json.RawMessage(
		`[{"index":1,"chunk_id":"ck_1","doc_id":"doc_1","kb_id":"kb_1","doc_name":"手册.pdf","score":0.5,"snippet":"片段","content_sha256":"abc"}]`)})
	f.stream.push(tokenEvent("你好"))
	f.stream.push(tokenEvent("，世界"))
	f.stream.push(StreamUsageEvent{Usage: MessageUsage{PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30}})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("流式提问应正常结束，实际 %v", err)
	}

	// 帧序列必须与上游一致（docs/04-§4.1 的保真要求）。
	if got := f.sink.events(); strings.Join(got, ",") != "meta,reference,token,token,usage,done" {
		t.Fatalf("下发帧序应为 meta,reference,token,token,usage,done，实际 %v", got)
	}

	// meta 帧里的两个 ID 必须被换成网关自己的（REQ-ORCH-006）：
	// 转发上游的值 → 客户端下一轮会问到一个空会话，且 GET /messages/{id} 必然 404。
	meta, ok := f.sink.sent[0].(StreamMetaEvent)
	if !ok {
		t.Fatalf("第一帧应为 meta，实际 %T", f.sink.sent[0])
	}
	if meta.ConversationID != f.conv.ID {
		t.Errorf("meta.conversation_id 应为网关的 %q，实际 %q", f.conv.ID, meta.ConversationID)
	}
	if meta.MessageID == "" || meta.MessageID == "msg_upstream_1" {
		t.Errorf("meta.message_id 应为网关预生成的 ID，实际 %q", meta.MessageID)
	}
	// 上游给的就是网关的会话 id 时不该打「不一致」日志之外的东西，这里只断言相等。

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("正常收完必须落一条 assistant 消息")
	}
	// 落库的消息 ID 必须与 meta 帧里预告的**同一个**：客户端拿到 done 之后
	// 会直接用它去 GET /messages/{id}，两个 ID 不同就会 404。
	if msg.ID != meta.MessageID {
		t.Errorf("落库 ID 应与 meta 帧预告的一致，实际 %q / %q", msg.ID, meta.MessageID)
	}
	if msg.Content != "你好，世界" {
		t.Errorf("正文应由 token 拼接，实际 %q", msg.Content)
	}
	if msg.Status != MessageStatusCompleted {
		t.Errorf("正常收完的状态应为 completed，实际 %q", msg.Status)
	}
	if msg.FinishReason == nil || *msg.FinishReason != FinishReasonStop {
		t.Errorf("finish_reason 应透传 stop，实际 %#v", msg.FinishReason)
	}
	if msg.Usage == nil || msg.Usage.TotalTokens != 30 {
		t.Errorf("用量应落库，实际 %#v", msg.Usage)
	}
	if msg.ElapsedMS == nil || *msg.ElapsedMS != 1234 {
		t.Errorf("elapsed_ms 应优先用上游 done 的值，实际 %#v", msg.ElapsedMS)
	}
	if msg.Model == nil || *msg.Model != "deepseek-flash" {
		t.Errorf("model 应落库，实际 %#v", msg.Model)
	}
	if msg.TraceID == nil || *msg.TraceID != "trace-1" {
		t.Errorf("trace_id 应落库，实际 %#v", msg.TraceID)
	}
	if len(msg.References) == 0 {
		t.Error("引用应落库（否则正文里的 [1] 无处可点）")
	}
	// 落库顺序仍是非流式那条规则：先 user 再 assistant。
	if len(f.msgs.appended) != 2 || f.msgs.appended[0].Role != MessageRoleUser {
		t.Fatalf("落库顺序应为 user → assistant，实际 %#v", f.msgs.appended)
	}
}

func TestStreamSendReplacesReferencesInsteadOfAppending(t *testing.T) {
	f := newStreamFixture(t)
	first := `[{"index":1,"chunk_id":"ck_1","doc_id":"d","kb_id":"kb","doc_name":"a.pdf","score":1,"snippet":"s","content_sha256":"h1"}]`
	second := `[{"index":1,"chunk_id":"ck_2","doc_id":"d","kb_id":"kb","doc_name":"a.pdf","score":1,"snippet":"s","content_sha256":"h2"}]`
	f.stream.push(StreamReferenceEvent{References: json.RawMessage(first)})
	f.stream.push(StreamReferenceEvent{References: json.RawMessage(second)})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	// **证伪点**：把 `setReferences` 改成 append（或 `acc.references = append(...)`），
	// 这条断言立刻变成 2 条，而且症状在真实环境里是「引用面板里同一个文档出现两遍，
	// 正文里的 [1] 只指向其中一个」—— 只在 AI 侧重发了引用（也就是每轮都发生）时出现。
	var items []map[string]any
	if err := json.Unmarshal(msg.References, &items); err != nil {
		t.Fatalf("引用应为 JSON 数组，实际 %s（%v）", msg.References, err)
	}
	if len(items) != 1 {
		t.Fatalf("引用应整体替换为最后一帧的内容（1 条），实际 %d 条：%s", len(items), msg.References)
	}
	if items[0]["chunk_id"] != "ck_2" {
		t.Errorf("引用应为最后一条 reference 帧的内容，实际 %v", items[0]["chunk_id"])
	}
}

func TestStreamSendDropsZeroUsageFrame(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(StreamUsageEvent{Usage: MessageUsage{}})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	// 全 0 的 usage 帧不是「用量为 0」而是「没有用量」：落成 0 会让配额统计
	// 把一个没报用量的回合算成 0 token，看起来像「这次没花钱」。
	if msg := f.persistAssistant(); msg == nil || msg.Usage != nil {
		t.Fatalf("全 0 的 usage 帧不应落库，实际 %#v", f.persistAssistant())
	}
}

// ---- 累积与限额 ----

func TestStreamSendTruncatesAtCharCapButKeepsForwarding(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamAccumulateMaxChars = 5
	f.stream.push(tokenEvent("abc"))
	f.stream.push(tokenEvent("defg"))
	f.stream.push(tokenEvent("hi"))
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	// 触顶后**保留能塞下的前缀**（不是整段丢掉）：丢掉整段会让库里少一截
	// 已经收到的内容，用户看到「回答忽然断了一截」。
	if msg.Content != "abcde" {
		t.Errorf("正文应截断为 %q，实际 %q", "abcde", msg.Content)
	}
	if msg.Status != MessageStatusPartial {
		t.Errorf("被截断的落库状态应为 partial，实际 %q", msg.Status)
	}
	// finish_reason 保留上游的值：截断是**网关的存储策略**，不是模型提前停了。
	if msg.FinishReason == nil || *msg.FinishReason != FinishReasonStop {
		t.Errorf("截断不应篡改上游的 finish_reason，实际 %#v", msg.FinishReason)
	}
	// **证伪点**：截断只影响落库，不影响下发。若实现改成「触顶后不再下发」，
	// 客户端会看到回答突然中断 —— 而那是 docs/06-§4 明确不允许的。
	if got := f.sink.events(); strings.Join(got, ",") != "token,token,token,done" {
		t.Fatalf("触顶后仍应转发所有帧，实际 %v", got)
	}
}

func TestStreamSendTruncationNeverSplitsARune(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamAccumulateMaxChars = 3
	f.stream.push(tokenEvent("你好世界"))
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	if msg.Content != "你好世" {
		t.Errorf("应按**字符**截断为 %q，实际 %q", "你好世", msg.Content)
	}
	// **证伪点**：把截断写成按字节（`delta[:remaining]`）时，"你好世" 的 3 个字符
	// 是 9 字节，`maxChars=3` 会切出半个汉字 → 这里 utf8.ValidString 变 false，
	// 而落库时 MySQL 会报 `Incorrect string value`（或者更糟：静默替换成 U+FFFD）。
	if !utf8.ValidString(msg.Content) {
		t.Fatalf("截断后的正文必须是合法 UTF-8，实际 %q", msg.Content)
	}
}

func TestStreamSendCapsReferencesAndTools(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamAccumulateMaxItems = 1
	f.stream.push(StreamReferenceEvent{References: json.RawMessage(`[{"index":1},{"index":2}]`)})
	f.stream.push(StreamToolCallEvent{CallID: "c1", Name: "kb.search"})
	f.stream.push(StreamToolCallEvent{CallID: "c2", Name: "kb.search"})
	f.stream.push(StreamToolResultEvent{CallID: "c1", Name: "kb.search", Status: ToolCallStatusOK})
	f.stream.push(StreamToolResultEvent{CallID: "c2", Name: "kb.search", Status: ToolCallStatusOK})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	var refs []map[string]any
	if err := json.Unmarshal(msg.References, &refs); err != nil {
		t.Fatalf("引用应为数组：%v", err)
	}
	if len(refs) != 1 {
		t.Errorf("引用条数应受上限约束（1），实际 %d", len(refs))
	}
	var tools []map[string]any
	if err := json.Unmarshal(msg.ToolCalls, &tools); err != nil {
		t.Fatalf("工具轨迹应为数组：%v", err)
	}
	if len(tools) != 1 {
		t.Errorf("工具轨迹条数应受上限约束（1），实际 %d", len(tools))
	}
}

func TestStreamSendPairsToolCallsAndMarksUnpairedAsError(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(StreamToolCallEvent{
		CallID:    "c1",
		Name:      "kb.search",
		Arguments: json.RawMessage(`{"q":"向量"}`),
	})
	f.stream.push(StreamToolCallEvent{CallID: "c2", Name: "kb.search"})
	f.stream.push(StreamToolResultEvent{
		CallID: "c1", Name: "kb.search", Status: ToolCallStatusOK, Summary: "命中 5 条", ElapsedMS: 37,
	})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	type toolTrace struct {
		CallID    string          `json:"call_id"`
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
		Status    string          `json:"status"`
		Summary   string          `json:"summary"`
		ElapsedMS int             `json:"elapsed_ms"`
	}
	var tools []toolTrace
	if err := json.Unmarshal(msg.ToolCalls, &tools); err != nil {
		t.Fatalf("工具轨迹解析失败: %v", err)
	}
	if len(tools) != 2 {
		t.Fatalf("两条调用都应落库（顺序保留），实际 %d 条", len(tools))
	}
	if tools[0].CallID != "c1" || tools[0].Status != ToolCallStatusOK || tools[0].Summary != "命中 5 条" || tools[0].ElapsedMS != 37 {
		t.Errorf("c1 的结果未正确配对：%#v", tools[0])
	}
	if string(tools[0].Arguments) != `{"q":"向量"}` {
		t.Errorf("arguments 应为对象 JSON，实际 %s", tools[0].Arguments)
	}
	// 只有开始没有结果（上游被取消时就会这样）→ 落 `error`。
	// 落成空状态会让前端渲染出一个**转不完的圈**，比明确的失败更糟。
	if tools[1].CallID != "c2" || tools[1].Status != ToolCallStatusError {
		t.Errorf("没有结果的调用应落 status=error，实际 %#v", tools[1])
	}
	// 缺 arguments 时补 `{}` 而不是 `null`：契约要求是对象，
	// `null` 会让严格的反序列化在客户端直接炸。
	if string(tools[1].Arguments) != `{}` {
		t.Errorf("缺 arguments 时应补 {}，实际 %s", tools[1].Arguments)
	}
}

// ---- 上游结束的三种形态 ----

func TestStreamSendUpstreamErrorFrameFinishesAsFailed(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(tokenEvent("半截"))
	f.stream.push(StreamErrorEvent{Code: string(errs.CodeAIUnavailable), Message: "上游炸了", Retryable: true})
	f.stream.finish(nil) // AI 发完 error 帧就返回了，不会再有 done

	wait := f.start(t, context.Background())
	err := wait()
	// 上游已明确报错 → 不再补发网关的 error 帧，也不把它当成网关的失败返回。
	if err != nil {
		t.Fatalf("上游 error 帧不是网关的失败，应返回 nil，实际 %v", err)
	}

	// error 帧必须**原样**下发（客户端要靠它决定要不要重试）。
	if got := f.sink.events(); strings.Join(got, ",") != "token,error" {
		t.Fatalf("帧序应为 token,error，实际 %v", got)
	}
	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("上游报错也已有半截正文，应落 partial/failed，不能什么都不留")
	}
	if msg.Status != MessageStatusFailed {
		t.Errorf("上游 error 帧对应的状态应为 failed，实际 %q", msg.Status)
	}
	// finish_reason 留空：上游没给值，编一个 stop 会让「答了一半失败」看起来像「答完了」。
	if msg.FinishReason != nil {
		t.Errorf("failed 的 finish_reason 应为 NULL，实际 %#v", *msg.FinishReason)
	}
	if msg.Content != "半截" {
		t.Errorf("已收到的正文应落库，实际 %q", msg.Content)
	}
}

func TestStreamSendMissingDoneIsPartial(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(tokenEvent("半截"))
	f.stream.finish(nil) // 干净关闭但没 done

	wait := f.start(t, context.Background())
	err := wait()
	// 上游少发一帧 ≠ 正常收完：对外都落 partial，但排障要能分清（这是 AI 侧的 bug）。
	if !errors.Is(err, errStreamNoDone) {
		t.Fatalf("应返回 errStreamNoDone，实际 %v", err)
	}
	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	if msg.Status != MessageStatusPartial {
		t.Errorf("没有 done 时应落 partial，实际 %q", msg.Status)
	}
	if msg.FinishReason == nil || *msg.FinishReason != FinishReasonCanceled {
		t.Errorf("中断应以 canceled 收尾，实际 %#v", msg.FinishReason)
	}
}

func TestStreamSendUpstreamBrokenIsPartial(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(tokenEvent("半截"))
	f.stream.finish(errInvalidServer)

	wait := f.start(t, context.Background())
	if err := wait(); !errors.Is(err, errInvalidServer) {
		t.Fatalf("上游断开的原因应原样返回，实际 %v", err)
	}
	if msg := f.persistAssistant(); msg == nil || msg.Status != MessageStatusPartial {
		t.Fatalf("上游静默断开应落 partial，实际 %#v", f.persistAssistant())
	}
}

// TestStreamSendZeroEventFailureLeavesNoAssistantRow 钉住「两条传输行为一致」。
//
// 同一个失败（上游拒绝/超时）在 gRPC 之下表现为「流上零事件 + 一个错误状态」，
// 在 HTTP 回退之下表现为 4xx。如果只在有事件时才落库，两条路的台账就一致了；
// 不判这一条的话，走 gRPC 时会话里会多出一条空白 assistant 消息。
func TestStreamSendZeroEventFailureLeavesNoAssistantRow(t *testing.T) {
	for _, tc := range []struct {
		name string
		push func(f *streamFixture)
	}{
		{
			name: "上游报错且未发任何事件",
			push: func(f *streamFixture) { f.stream.finish(errInvalidServer) },
		},
		{
			name: "干净关闭但没有 done",
			push: func(f *streamFixture) { f.stream.finish(nil) },
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newStreamFixture(t)
			tc.push(f)

			wait := f.start(t, context.Background())
			if err := wait(); err == nil {
				t.Fatal("零事件失败应返回错误（调用方要能据此回信封）")
			}
			if msg := f.persistAssistant(); msg != nil {
				t.Fatalf("上游一个事件都没发时不应落 assistant 消息，实际 %#v", msg)
			}
		})
	}
}

// TestStreamSendErrorFrameWithoutContentStillPersistsFailed 是上一条的对照：
// 上游**发了 error 帧**（还没给正文）时，那条 failed 记录本身就是事实，必须落库。
func TestStreamSendErrorFrameWithoutContentStillPersistsFailed(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(StreamErrorEvent{Code: string(errs.CodeAIUnavailable), Message: "模型不可用"})
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("上游 error 帧不是网关的失败，应返回 nil，实际 %v", err)
	}
	msg := f.persistAssistant()
	if msg == nil || msg.Status != MessageStatusFailed {
		t.Fatalf("上游明确报错应落 failed，实际 %#v", msg)
	}
	if msg.Content != "" {
		t.Errorf("没有正文时内容应为空，实际 %q", msg.Content)
	}
}

// ---- 超时 / 退出 / 断连 ----

func TestStreamSendFirstByteTimeoutLeavesEnvelopeToCaller(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamFirstByteTimeout = 40 * time.Millisecond
	// 事件流保持打开、一个事件都不给：正是「上游连上了但不回话」。

	wait := f.start(t, context.Background())
	err := wait()
	assertAppError(t, err, errs.CodeAITimeout, 504)
	assertDetail(t, err, "reason", "first_byte_timeout")

	// **证伪点**：首字节之前**一帧都不许发**。若实现在超时时先发一帧 error，
	// `Started()` 就变 true，调用方再也没法回 504 信封了 ——
	// 而这恰恰是 docs/04-§5 时延表要求「首字节超时回 504」的原因。
	if len(f.sink.sent) != 0 {
		t.Fatalf("首字节超时不应下发任何帧（状态码还要用），实际 %v", f.sink.events())
	}
	if f.sink.pings != 0 {
		t.Errorf("首帧之前不应发心跳（发心跳会写掉响应头），实际 %d 次", f.sink.pings)
	}
	// **一个事件都没收到 ⇒ MUST NOT 落空回答**。
	// 落一条空 partial 会让会话里多出一条空白 assistant 消息，而用户看到的
	// 是一个已回滚的错误 —— 非流式路径在同样情形下（`Chat` 报错）也不落
	// assistant 消息，两条传输必须一致。
	if msg := f.persistAssistant(); msg != nil {
		t.Fatalf("零事件失败时不应落 assistant 消息，实际 %#v", msg)
	}
	// user 消息仍然要在台账里（否则用户重发时看不出「刚才问过」）。
	if len(f.msgs.appended) != 1 || f.msgs.appended[0].Role != MessageRoleUser {
		t.Fatalf("应只落 user 消息，实际 %#v", f.msgs.appended)
	}
}

func TestStreamSendIdleTimeoutAfterFirstFrameSendsErrorFrame(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamFirstByteTimeout = 5 * time.Second
	f.svc.d.StreamIdleTimeout = 60 * time.Millisecond
	f.stream.push(tokenEvent("第一段"))

	wait := f.start(t, context.Background())
	err := wait()
	assertAppError(t, err, errs.CodeAITimeout, 504)
	assertDetail(t, err, "reason", "idle_timeout")

	// 已经开流 → 只能靠 error 帧报错（HTTP 状态码早就发出去了）。
	if got := f.sink.events(); strings.Join(got, ",") != "token,error" {
		t.Fatalf("帧序应为 token,error，实际 %v", got)
	}
	ev, ok := f.sink.last().(StreamErrorEvent)
	if !ok {
		t.Fatalf("最后一帧应为 error，实际 %T", f.sink.last())
	}
	if ev.Code != string(errs.CodeAITimeout) || !ev.Retryable {
		t.Errorf("error 帧应是可重试的 AI_TIMEOUT，实际 %#v", ev)
	}
	if msg := f.persistAssistant(); msg == nil || msg.Status != MessageStatusPartial {
		t.Fatalf("空闲超时应落 partial，实际 %#v", f.persistAssistant())
	}
	// 空闲超时的 finish_reason 是 canceled（不是上游的 stop）：这一轮确实没答完。
	if msg := f.persistAssistant(); msg.FinishReason == nil || *msg.FinishReason != FinishReasonCanceled {
		t.Errorf("超时应以 canceled 收尾，实际 %#v", msg.FinishReason)
	}
}

func TestStreamSendTotalTimeoutIsAnUpperBound(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamFirstByteTimeout = 5 * time.Second
	f.svc.d.StreamIdleTimeout = 5 * time.Second
	f.svc.d.StreamTotalTimeout = 60 * time.Millisecond
	// 持续推事件：空闲计时器一直被重置，只有整轮上限能救场。
	go func() {
		for i := 0; i < 100; i++ {
			f.stream.push(tokenEvent("x"))
			time.Sleep(5 * time.Millisecond)
		}
	}()

	wait := f.start(t, context.Background())
	err := wait()
	assertAppError(t, err, errs.CodeAITimeout, 504)
	assertDetail(t, err, "reason", "total_timeout")
}

func TestStreamSendShutdownSendsErrorFrameAndPersistsPartial(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(tokenEvent("半截"))

	wait := f.start(t, context.Background())
	// **必须先等到第一帧真的下发出去，再发出退出信号。**
	//
	// 这一条不是「为了稳」的细节，而是用例语义的一部分：它要验证的是
	// 「流到一半时进程开始优雅退出」——「一半」意味着已经有一帧到了客户端手上。
	//
	// 直接 `close(f.shutdown)` 再 start 会**变成另一个用例**：
	// 泵的 select 里 `events`（缓冲区里那个 token）与 `shutdown`（已关）同时就绪，
	// Go 在多个就绪分支之间**随机**挑一个，于是「一帧都没发就退出」与
	// 「发了 token 再退出」各占一半 —— 一个 50% 概率失败的用例，
	// 既不能证明「退出时会补发 error 帧」，也会在半年后被人当成「偶发」而忽略掉。
	f.waitForFrames(t, 1)
	close(f.shutdown) // 进程开始优雅退出

	err := wait()
	assertAppError(t, err, errs.CodeServiceShuttingDown, 503)

	// docs/06-§3 第 ③ 步：先给客户端一个**明确的终止原因**，再落 partial。
	// 直接掐断连接的话，客户端只会看到「网络错误」，没有任何可展示的信息。
	if got := f.sink.events(); strings.Join(got, ",") != "token,error" {
		t.Fatalf("帧序应为 token,error，实际 %v", got)
	}
	if ev, ok := f.sink.last().(StreamErrorEvent); !ok || ev.Code != string(errs.CodeServiceShuttingDown) {
		t.Fatalf("最后一帧应为 SERVICE_SHUTTING_DOWN 的 error，实际 %#v", f.sink.last())
	}
	if msg := f.persistAssistant(); msg == nil || msg.Status != MessageStatusPartial || msg.Content != "半截" {
		t.Fatalf("退出时应落 partial 且保留已收正文，实际 %#v", f.persistAssistant())
	}
}

func TestStreamSendClientGoneStopsFramesButStillPersistsPartial(t *testing.T) {
	f := newStreamFixture(t)
	f.stream.push(tokenEvent("半截"))
	ctx, cancel := context.WithCancel(context.Background())

	// 让流先动起来（拿到第一帧），再模拟客户端离开。
	wait := f.start(t, ctx)
	f.waitForFrames(t, 1)
	cancel()

	if err := wait(); err == nil {
		t.Fatal("客户端断连应返回错误（service 层据此知道不必再写响应）")
	}

	// 连接已经没了 → 一帧都不该再发（尤其**不许**发 error 帧：没地方发，且它会
	// 让日志里出现一条假的「已开流的错误」）。
	if got := f.sink.events(); strings.Join(got, ",") != "token" {
		t.Fatalf("断连后不应再下发任何帧，实际 %v", got)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("断连也必须落库（docs/03-§5 的断连行）")
	}
	if msg.Status != MessageStatusPartial || msg.Content != "半截" {
		t.Fatalf("断连应落 partial 且保留已收正文，实际 %#v", msg)
	}
	// **证伪点（本文件最重要的一条）**：落库必须用 `context.WithoutCancel`。
	// 把 `finishStream` 里的 `context.WithoutCancel(ctx)` 去掉，这次 Append 的
	// ctx 就已经被取消，`appendCtxErrs[1]` 变成 `context.Canceled` —— 真实环境里
	// MySQL 立刻报错，症状是「用户看到的半截回答刷新后不见了」。
	//
	// 注意两次 Append 的差别就是证据本身：第 1 次（user）在取消**之前**发生，
	// 用的是请求 ctx；第 2 次（assistant）在取消**之后**发生，必须是干净的 ctx。
	if len(f.msgs.appendCtxErrs) != 2 {
		t.Fatalf("应有 user/assistant 两次 Append，实际 %d 次", len(f.msgs.appendCtxErrs))
	}
	if err := f.msgs.appendCtxErrs[0]; err != nil {
		t.Errorf("user 消息在取消前落库，ctx 本就不该被取消，实际 %v", err)
	}
	if err := f.msgs.appendCtxErrs[1]; err != nil {
		t.Fatalf("落库用的是已取消的 ctx（%v）：断连时的正文会丢失", err)
	}
}

func TestStreamSendWriteFailureStopsStreamingAndPersistsPartial(t *testing.T) {
	f := newStreamFixture(t)
	f.sink.sendErr = errInvalidServer
	f.sink.sendErrAt = 2 // 第一帧写得出去，第二帧失败
	f.stream.push(tokenEvent("第一段"))
	f.stream.push(tokenEvent("第二段"))
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); !errors.Is(err, errInvalidServer) {
		t.Fatalf("写失败应原样返回，实际 %v", err)
	}

	// 写失败只有一种常见原因：客户端已经走了。继续把答案流进黑洞毫无意义，
	// 所以**立刻停**（一次都不该再试）。若实现忽略 Send 的错误继续循环，
	// 这里会看到 sendCalls 一直涨到帧耗尽（并且还会多发一帧 error）。
	if f.sink.sendCalls != 2 {
		t.Fatalf("写失败后应立即停止，Send 调用次数应为 2，实际 %d", f.sink.sendCalls)
	}
	// 注意正文是**两段都在**：`pump` 是「先 observe 再 Send」，所以写失败的那一帧
	// 也已经进了累积器。这是刻意的 —— 那是模型真真切切生成出来的内容，
	// 而写失败的原因（客户端走了）与「这段内容该不该存」是两回事。
	msg := f.persistAssistant()
	if msg == nil || msg.Status != MessageStatusPartial || msg.Content != "第一段第二段" {
		t.Fatalf("写失败应落 partial 且保留已生成的正文，实际 %#v", f.persistAssistant())
	}
	// 上游的流必须被关掉（defer stream.Close()）：不关就是泄漏一个还在跑的上游。
	if f.stream.closed != 1 {
		t.Errorf("事件流应被关闭 1 次，实际 %d 次", f.stream.closed)
	}
}

// ---- 落库失败 ----

func TestStreamSendPersistFailureAppendsGwPersistErrorAfterDone(t *testing.T) {
	f := newStreamFixture(t)
	f.msgs.failAppendAfter = 2 // 第 2 次 Append（assistant）失败
	f.stream.push(tokenEvent("你好"))
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		// 流已经发完了，落库失败**不能**改变返回给客户端的结局：
		// 唯一能做的是追加一帧 gw_persist_error（响应里没有别的表达手段）。
		t.Fatalf("落库失败不应让调用方以为整轮失败，实际 %v", err)
	}

	got := f.sink.events()
	if strings.Join(got, ",") != "token,done,gw_persist_error" {
		t.Fatalf("应在 done 之后追加 gw_persist_error，实际 %v", got)
	}
	ev, ok := f.sink.last().(StreamPersistErrorEvent)
	if !ok {
		t.Fatalf("最后一帧应为 gw_persist_error，实际 %T", f.sink.last())
	}
	// reason 是**给客户端看的短语**，不是完整错误链（里面可能有 SQL 片段/表名）。
	if ev.Reason == "" || strings.Contains(ev.Reason, "boom") {
		t.Errorf("reason 应为稳定的原因码而不是原始错误，实际 %q", ev.Reason)
	}
}

// ---- 未开流前的失败（调用方要写信封）----

func TestStreamSendNotStartedErrorsLeaveEnvelopeToCaller(t *testing.T) {
	cases := []struct {
		name  string
		setup func(f *streamFixture)
		code  errs.Code
		want  int
	}{
		{
			name:  "会话已归档",
			setup: func(f *streamFixture) { f.conv.Status = ConversationStatusArchived },
			code:  errs.CodeConversationArchived,
			want:  409,
		},
		{
			name: "建流失败",
			setup: func(f *streamFixture) {
				f.streamer.stream = nil
				f.streamer.err = errors.New("dial failed")
			},
			code: errs.CodeInternalError,
			want: 500,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newStreamFixture(t)
			tc.setup(f)

			wait := f.start(t, context.Background())
			err := wait()
			if err == nil {
				t.Fatal("期望错误")
			}
			if appErr, ok := errs.As(err); ok {
				if appErr.Code() != tc.code || appErr.Status() != tc.want {
					t.Fatalf("期望 %s/%d，实际 %s/%d", tc.code, tc.want, appErr.Code(), appErr.Status())
				}
			} else if tc.code != errs.CodeInternalError {
				t.Fatalf("期望 *errs.AppError，实际 %T: %v", err, err)
			}

			// 一帧都没发 → 调用方**应当**回 4xx/5xx JSON 信封（这是它唯一的报错手段）。
			if len(f.sink.sent) != 0 || f.sink.started {
				t.Fatalf("未开流前不应下发任何帧，实际 %v", f.sink.events())
			}
			// 上游一次都没回 → MUST NOT 落 assistant 消息。
			// 落一条空回答会让用户以为「模型答了个空」，而实际上请求根本没到达模型。
			if msg := f.persistAssistant(); msg != nil {
				t.Fatalf("未开流前不得落 assistant 消息，实际 %#v", msg)
			}
			// 归档会话连 user 消息都不该落（可读不可写）。
			if tc.code == errs.CodeConversationArchived && len(f.msgs.appended) != 0 {
				t.Fatalf("归档会话不得落库任何消息，实际 %d 条", len(f.msgs.appended))
			}
		})
	}
}

func TestStreamSendWithoutStreamerPersistsUserThenFails(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.Streamer = nil

	wait := f.start(t, context.Background())
	err := wait()
	assertAppError(t, err, errs.CodeAIUnavailable, 503)
	assertDetail(t, err, "reason", "streamer_not_configured")
	// user 消息必须留在台账里（与非流式路径同形）：不留的话用户重发时
	// 看不出「刚才其实问过一次」。
	if len(f.msgs.appended) != 1 || f.msgs.appended[0].Role != MessageRoleUser {
		t.Fatalf("应只落 user 消息，实际 %#v", f.msgs.appended)
	}
}

func TestStreamSendContentValidationHappensBeforeAnything(t *testing.T) {
	f := newStreamFixture(t)
	err := f.svc.StreamSend(context.Background(), "u_1", f.conv.ID,
		SendMessageInput{Content: "   "}, RequestMeta{}, f.sink)
	assertFieldError(t, err, "content")
	if len(f.msgs.appended) != 0 || len(f.sink.sent) != 0 {
		t.Fatal("校验失败时不得落库、不得开流")
	}
	if f.streamer.calls != 0 {
		t.Fatal("校验失败时不得调用上游（会白烧配额）")
	}
}

// ---- 纯函数 ----

// TestResetTimerFiresAtTheNewDeadline 钉住 `resetTimer` 的**可观察**行为：
// 重置之后按新的时长触发，而不是按旧时长。
//
// 关于「排空残留旧值」那一段：Go 1.23 起 `Timer.Reset` 的新语义（随模块的
// `go` 指令门控；本模块是 go 1.24.0）本身就保证不会残留旧值 —— **实测**
// 把那几行 drain 删掉后本用例依然通过（已用 tools/falsify_m4.ps1 跑过）。
// 所以这里不声称它在守护那个 bug，只守住「新时长生效」这件在 pump 里
// 真正被依赖的事（空闲计时器每个事件重置一次）。
func TestResetTimerFiresAtTheNewDeadline(t *testing.T) {
	timer := time.NewTimer(10 * time.Millisecond)
	defer timer.Stop()
	time.Sleep(30 * time.Millisecond) // 让它触发并留下未取走的值

	resetTimer(timer, 80*time.Millisecond)

	select {
	case <-timer.C:
		t.Fatal("重置后按旧时长（10ms）立即触发了，新时长没生效")
	case <-time.After(40 * time.Millisecond):
		// 正确：新的 80ms 还没到。
	}

	select {
	case <-timer.C:
		// 正确：新时长生效。
	case <-time.After(200 * time.Millisecond):
		t.Fatal("重置后的计时器没有触发")
	}
}

// TestStreamSendIdleTimerIsResetByEachEvent 是 `resetTimer` 在 pump 里
// **真正的**护栏：空闲计时器必须被每个事件重置，否则一次「持续出字但每次间隔
// 都小于阈值」的正常长响应会被判成空闲超时（用户看到回答推到一半断掉）。
//
// 证伪方式：删掉 `pump` 里的 `resetTimer(idle, s.d.StreamIdleTimeout)` 一行，
// 本用例立刻失败。
func TestStreamSendIdleTimerIsResetByEachEvent(t *testing.T) {
	f := newStreamFixture(t)
	f.svc.d.StreamFirstByteTimeout = 5 * time.Second
	f.svc.d.StreamIdleTimeout = 80 * time.Millisecond
	f.svc.d.StreamTotalTimeout = 8 * time.Second

	// 10 个 token，间隔 30ms ⇒ 总耗时约 300ms，远超空闲阈值，
	// 但任何**相邻两个事件**的间隔都远小于阈值。
	go func() {
		for i := 0; i < 10; i++ {
			f.stream.push(tokenEvent("x"))
			time.Sleep(30 * time.Millisecond)
		}
		f.stream.push(doneEvent())
		f.stream.finish(nil)
	}()

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("每个事件都应重置空闲计时器（否则 300ms 的正常响应会被判超时）：%v", err)
	}
	msg := f.persistAssistant()
	if msg == nil || msg.Status != MessageStatusCompleted {
		t.Fatalf("应正常收完，实际 %#v", f.persistAssistant())
	}
	if msg.Content != "xxxxxxxxxx" {
		t.Errorf("正文应完整累积，实际 %q", msg.Content)
	}
}

func TestStreamAccumulatorElapsedFallsBackToWallClock(t *testing.T) {
	now := testNow()
	acc := newStreamAccumulator(0, 0, func() time.Time { return now })

	now = now.Add(1500 * time.Millisecond)
	// 没有 done（上游中途断了）时退化成网关墙钟时间：返回 0 会被
	// `intPtrIfPositive` 丢成 NULL，于是「中断的那次」看起来像没耗时。
	if got := acc.elapsedMS(); got != 1500 {
		t.Fatalf("没有上游耗时时应用墙钟时间，实际 %d", got)
	}

	acc.observe(StreamDoneEvent{FinishReason: FinishReasonStop, ElapsedMS: 900})
	if got := acc.elapsedMS(); got != 900 {
		t.Fatalf("有上游耗时时应优先用它（不含网络往返），实际 %d", got)
	}
}

func TestStreamAccumulatorKeepsNonArrayReferences(t *testing.T) {
	acc := newStreamAccumulator(0, 0, testNow)
	// 形状不符（不是数组）时**原样存**：丢掉它的症状是「引用忽然全没了」，
	// 而且这条分支连日志都不会有。
	acc.observe(StreamReferenceEvent{References: json.RawMessage(`{"oops":true}`)})
	if string(acc.references) != `{"oops":true}` {
		t.Fatalf("非数组引用应原样保留，实际 %s", acc.references)
	}
	// 但空负载不该覆盖已有的集合（上游偶尔发一帧空的）。
	acc.observe(StreamReferenceEvent{References: json.RawMessage(`[{"index":1}]`)})
	acc.observe(StreamReferenceEvent{References: nil})
	if string(acc.references) != `[{"index":1}]` {
		t.Fatalf("空引用帧不应清空已有集合，实际 %s", acc.references)
	}
}

func TestStreamAccumulatorIgnoresUnknownEvents(t *testing.T) {
	acc := newStreamAccumulator(0, 0, testNow)
	// 未知事件原样透传但不参与累积（网关不认识它的语义，硬塞会把库写脏）。
	acc.observe(StreamUnknownEvent{Name: "citation_note", Data: json.RawMessage(`{"x":1}`)})
	if acc.text.Len() != 0 || acc.doneSeen || acc.sawError || len(acc.toolOrder) != 0 {
		t.Fatalf("未知事件不应影响累积状态：%#v", acc)
	}
}

func TestStreamSendDeduplicatesRepeatedToolCallFrame(t *testing.T) {
	f := newStreamFixture(t)
	call := StreamToolCallEvent{CallID: "c1", Name: "kb.search", Arguments: json.RawMessage(`{"q":"a"}`)}
	f.stream.push(call)
	// 同一 call_id 的第二条开始帧（上游重发）：保留第一次。
	f.stream.push(StreamToolCallEvent{CallID: "c1", Name: "kb.search", Arguments: json.RawMessage(`{"q":"b"}`)})
	f.stream.push(StreamToolResultEvent{CallID: "c1", Name: "kb.search", Status: ToolCallStatusOK})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	msg := f.persistAssistant()
	if msg == nil {
		t.Fatal("应落一条 assistant 消息")
	}
	var tools []map[string]any
	if err := json.Unmarshal(msg.ToolCalls, &tools); err != nil {
		t.Fatalf("工具轨迹解析失败: %v", err)
	}
	if len(tools) != 1 {
		t.Fatalf("同一 call_id 只应留一条轨迹，实际 %d 条：%s", len(tools), msg.ToolCalls)
	}
	if got := tools[0]["arguments"].(map[string]any)["q"]; got != "a" {
		t.Errorf("重发时应保留第一次的参数，实际 %v", got)
	}
}

func TestStreamSendUsesConversationHistoryWhenMemoryDisabled(t *testing.T) {
	f := newStreamFixture(t)
	// 先塞两条历史消息，验证网关会把它们带给上游（use_memory=false 时）。
	_ = f.msgs.Append(context.Background(), "u_1", f.conv.ID, &Message{
		ID: "msg_old", ConversationID: f.conv.ID, UserID: "u_1",
		Role: MessageRoleUser, Content: "上一句", Status: MessageStatusCompleted, CreatedAt: testNow(),
	})
	f.stream.push(doneEvent())
	f.stream.finish(nil)

	wait := f.start(t, context.Background())
	if err := wait(); err != nil {
		t.Fatalf("StreamSend 失败: %v", err)
	}

	if f.streamer.calls != 1 {
		t.Fatalf("上游应被调用 1 次，实际 %d 次", f.streamer.calls)
	}
	// 网关必须传**本地**会话 id（不是客户端传来的那个原始值之外的东西），
	// 且 use_memory 为 false 时要带上历史（REQ-ORCH-006）。
	if f.streamer.got.ConversationID != f.conv.ID {
		t.Errorf("传给上游的 conversation_id 应为网关的 %q，实际 %q", f.conv.ID, f.streamer.got.ConversationID)
	}
	if f.streamer.got.UseMemory {
		t.Skip("本用例假设默认关闭记忆（AI 侧自取上下文时网关不得重复注入）")
	}
	if len(f.streamer.got.History) != 1 || f.streamer.got.History[0].Content != "上一句" {
		t.Fatalf("应带上本地历史，实际 %#v", f.streamer.got.History)
	}
}
