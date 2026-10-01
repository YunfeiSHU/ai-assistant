package ai

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件是 `ChatStream` 的 gRPC 通道测试。
//
// 与 `grpc_test.go` 同一个理由：用**真实的 grpc.Server** + bufconn 接住事件，
// 断言的是「跨进程之后事件还在、还是同一个事件」，而不是「打桩被调用了」。
// 流式还多一层：**顺序**（reference 必须先于第一个 token）与**原样**（未知事件）。

// ---- 假流式服务端 ----

type fakeStreamServer struct {
	aiplatformv1.UnimplementedAiPlatformServer

	events   []*aiplatformv1.ChatEvent
	sendErr  error
	relayErr error
	// sendDelay 是**事件之间**的间隔（第一个事件立即发）。
	//
	// 不能「发之前先睡」：那样连第一帧都要等一个间隔，而这用例要验证的恰是
	// 「第一帧不等后面的帧」——两者混在一起就分不清测的是哪一个了。
	sendDelay time.Duration
	started   chan struct{}
}

func (s *fakeStreamServer) ChatStream(
	_ *aiplatformv1.ChatRequest, stream aiplatformv1.AiPlatform_ChatStreamServer,
) error {
	ctx := stream.Context()
	for i, ev := range s.events {
		if i > 0 && s.sendDelay > 0 {
			select {
			case <-time.After(s.sendDelay):
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := stream.Send(ev); err != nil {
			return err
		}
		if s.started != nil {
			select {
			case s.started <- struct{}{}:
			default:
			}
		}
	}
	if s.sendErr != nil {
		return s.sendErr
	}
	return s.relayErr
}

func startFakeStream(t *testing.T, srv *fakeStreamServer) biz.ChatStreamer {
	t.Helper()

	lis := bufconn.Listen(1 << 20)
	g := grpc.NewServer()
	aiplatformv1.RegisterAiPlatformServer(g, srv)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)

	conn, err := dialForTest(t, lis)
	if err != nil {
		t.Fatalf("建立 gRPC 连接失败: %v", err)
	}
	opt := testOptions(t)
	opt.TotalTimeout = 2 * time.Second
	return newChatStreamerFromConn(conn, opt)
}

// drainStream 收完一条流的全部事件（通道关闭后读 Err）。
func drainStream(t *testing.T, s biz.ChatEventStream) ([]biz.StreamEvent, error) {
	t.Helper()
	var out []biz.StreamEvent
	deadline := time.After(5 * time.Second)
	for {
		select {
		case ev, ok := <-s.Events():
			if !ok {
				return out, s.Err()
			}
			out = append(out, ev)
		case <-deadline:
			t.Fatal("收流超时（多半是 pump 没有关闭通道）")
			return out, nil
		}
	}
}

func eventNames(events []biz.StreamEvent) []string {
	out := make([]string, 0, len(events))
	for _, ev := range events {
		out = append(out, ev.EventName())
	}
	return out
}

func chatRequest() biz.ChatRequest {
	return biz.ChatRequest{Query: "你好", ConversationID: "cv_1", TraceID: "trace-1"}
}

// ---- 事件映射 ----

func TestChatStreamMapsEveryEventKind(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
		{Event: &aiplatformv1.ChatEvent_Meta{Meta: &aiplatformv1.StreamMeta{
			ConversationId: strPtr("cv_upstream"), MessageId: "msg_1", Model: "deepseek-flash",
			CreatedAt: "2026-09-30T10:00:00Z", Degraded: true,
			DegradedReasons: []string{"rerank_skipped", "memory_unavailable"},
		}}},
		{Event: &aiplatformv1.ChatEvent_Reference{Reference: &aiplatformv1.StreamReferences{
			References: []*aiplatformv1.Reference{{
				Index: 1, ChunkId: "ck_1", DocId: "d_1", KbId: "kb_1", DocName: "手册.pdf",
				Score: 0.5, Snippet: "片段", ContentSha256: "abc",
			}},
		}}},
		{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "你"}}},
		{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: ""}}},
		{Event: &aiplatformv1.ChatEvent_ToolCall{ToolCall: &aiplatformv1.StreamToolCall{
			CallId: "call_1", Name: "kb.search", ArgumentsJson: `{"q":"向量"}`,
		}}},
		{Event: &aiplatformv1.ChatEvent_ToolResult{ToolResult: &aiplatformv1.StreamToolResult{
			CallId: "call_1", Name: "kb.search", Status: "ok", Summary: "命中 5 条", ElapsedMs: 37,
		}}},
		{Event: &aiplatformv1.ChatEvent_Usage{Usage: &aiplatformv1.Usage{
			PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30,
		}}},
		{Event: &aiplatformv1.ChatEvent_Error{Error: &aiplatformv1.StreamError{
			Code: "UPSTREAM_TIMEOUT", Message: "上游超时", Retryable: true,
		}}},
		{Event: &aiplatformv1.ChatEvent_Done{Done: &aiplatformv1.StreamDone{
			FinishReason: "stop", ElapsedMs: 1234, Partial: true,
		}}},
	}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, err := drainStream(t, stream)
	if err != nil {
		t.Fatalf("收流失败: %v", err)
	}

	want := []string{"meta", "reference", "token", "token", "tool_call", "tool_result", "usage", "error", "done"}
	got := eventNames(events)
	if len(got) != len(want) {
		t.Fatalf("事件数应为 %d，实际 %d：%v", len(want), len(got), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("事件顺序错误：期望 %v，实际 %v", want, got)
		}
	}

	// meta：degraded 与 degradedReasons 都要过线。后者只给网关落库用
	// （docs/04-§9 要求 AI 附上原因），漏掉它落库的 degraded_reasons 就是空的。
	meta, ok := events[0].(biz.StreamMetaEvent)
	if !ok {
		t.Fatalf("第 0 个事件应为 StreamMetaEvent，实际 %T", events[0])
	}
	if meta.Model != "deepseek-flash" || !meta.Degraded || meta.CreatedAt == "" {
		t.Errorf("meta 字段缺失：%#v", meta)
	}
	if len(meta.DegradedReasons) != 2 || meta.DegradedReasons[0] != "rerank_skipped" {
		t.Errorf("degraded_reasons 必须过线（落库要用），实际 %#v", meta.DegradedReasons)
	}

	// reference：形状必须与非流式 `ChatResponse.references` 一致
	// （两条路落的是同一个 JSON 列，形状不同会在前端才被发现）。
	ref, ok := events[1].(biz.StreamReferenceEvent)
	if !ok {
		t.Fatalf("第 1 个事件应为 StreamReferenceEvent，实际 %T", events[1])
	}
	var decoded []map[string]any
	if err := json.Unmarshal(ref.References, &decoded); err != nil {
		t.Fatalf("引用应是 JSON 数组：%v（%s）", err, ref.References)
	}
	if len(decoded) != 1 || decoded[0]["chunk_id"] != "ck_1" || decoded[0]["doc_name"] != "手册.pdf" {
		t.Errorf("引用内容错误：%s", ref.References)
	}

	// 空 delta 必须保留（丢掉会让客户端收到的帧数与上游不一致）。
	if tok, ok := events[3].(biz.StreamTokenEvent); !ok || tok.Delta != "" {
		t.Errorf("空 delta 帧应原样保留，实际 %#v", events[3])
	}

	// tool_call 的 arguments 必须是**对象**（proto 里是字符串，传输层负责归一）。
	call, ok := events[4].(biz.StreamToolCallEvent)
	if !ok {
		t.Fatalf("第 4 个事件应为 StreamToolCallEvent，实际 %T", events[4])
	}
	if !jsonIsObject(string(call.Arguments)) {
		t.Errorf("arguments 应为对象 JSON，实际 %s", call.Arguments)
	}

	result, ok := events[5].(biz.StreamToolResultEvent)
	if !ok {
		t.Fatalf("第 5 个事件应为 StreamToolResultEvent，实际 %T", events[5])
	}
	if result.Status != "ok" || result.ElapsedMS != 37 || result.Summary != "命中 5 条" {
		t.Errorf("tool_result 字段错误：%#v", result)
	}

	usage, ok := events[6].(biz.StreamUsageEvent)
	if !ok {
		t.Fatalf("第 6 个事件应为 StreamUsageEvent，实际 %T", events[6])
	}
	if usage.Usage.TotalTokens != 30 {
		t.Errorf("usage 错误：%#v", usage.Usage)
	}

	errev, ok := events[7].(biz.StreamErrorEvent)
	if !ok {
		t.Fatalf("第 7 个事件应为 StreamErrorEvent，实际 %T", events[7])
	}
	if errev.Code != "UPSTREAM_TIMEOUT" || !errev.Retryable {
		t.Errorf("error 字段错误：%#v", errev)
	}

	done, ok := events[8].(biz.StreamDoneEvent)
	if !ok {
		t.Fatalf("第 8 个事件应为 StreamDoneEvent，实际 %T", events[8])
	}
	if done.FinishReason != "stop" || done.ElapsedMS != 1234 || !done.Partial {
		t.Errorf("done 字段错误：%#v", done)
	}
}

func TestChatStreamPassesUnknownEventThrough(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
		{Event: &aiplatformv1.ChatEvent_Unknown{Unknown: &aiplatformv1.StreamUnknown{
			Event: "citation_note", DataJson: []byte(`{"n":1.10}`),
		}}},
	}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, err := drainStream(t, stream)
	if err != nil {
		t.Fatalf("收流失败: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("未知事件必须透传（MUST NOT 丢弃），实际 %d 个事件", len(events))
	}
	unknown, ok := events[0].(biz.StreamUnknownEvent)
	if !ok {
		t.Fatalf("应为 StreamUnknownEvent，实际 %T", events[0])
	}
	if unknown.Name != "citation_note" {
		t.Errorf("事件名应原样保留，实际 %q", unknown.Name)
	}
	// **逐字节**保留：来回 unmarshal 一次会把 `1.10` 变成 `1.1`。
	if string(unknown.Data) != `{"n":1.10}` {
		t.Errorf("负载必须原样保留，实际 %s", unknown.Data)
	}
}

func TestChatStreamUnknownWithoutNameIsDroppedLoudly(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
		{Event: &aiplatformv1.ChatEvent_Unknown{Unknown: &aiplatformv1.StreamUnknown{
			Event: "  ", DataJson: []byte(`{"x":1}`),
		}}},
		{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "后一帧"}}},
	}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, err := drainStream(t, stream)
	if err != nil {
		t.Fatalf("收流失败: %v", err)
	}
	// 没有事件名的帧无法作为 SSE 下发（会退化成默认的 `message` 事件，
	// 客户端会把它当成另一种事件）。丢掉它，但**不能**因此中断流。
	if len(events) != 1 || events[0].EventName() != "token" {
		t.Fatalf("无名未知事件应被丢掉且不影响后续帧，实际 %v", eventNames(events))
	}
}

func TestChatStreamEmptyOneofIsSkippedNotFatal(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
		{}, // oneof 没设
		{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "x"}}},
	}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, err := drainStream(t, stream)
	if err != nil {
		t.Fatalf("收流失败: %v", err)
	}
	// **证伪点**：空事件如果 `return` 而不是 `continue`，整轮回答就没了 ——
	// 而这一分支的触发条件只是「AI 侧多发了一个还没定义的事件」。
	if len(events) != 1 || events[0].EventName() != "token" {
		t.Fatalf("空事件应被跳过而不影响后续，实际 %v", eventNames(events))
	}
}

func TestChatStreamToolArgumentsFallBackToEmptyObject(t *testing.T) {
	cases := map[string]string{
		"空字符串":    "",
		"只有空格":    "   ",
		"数组（非对象）": `[1,2]`,
		"对象后面有垃圾": `{"a":1} oops`,
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
				{Event: &aiplatformv1.ChatEvent_ToolCall{ToolCall: &aiplatformv1.StreamToolCall{
					CallId: "c", Name: "n", ArgumentsJson: raw,
				}}},
			}}
			streamer := startFakeStream(t, srv)

			stream, err := streamer.ChatStream(context.Background(), chatRequest())
			if err != nil {
				t.Fatalf("建流失败: %v", err)
			}
			defer func() { _ = stream.Close() }()

			events, err := drainStream(t, stream)
			if err != nil {
				t.Fatalf("收流失败: %v", err)
			}
			call, ok := events[0].(biz.StreamToolCallEvent)
			if !ok {
				t.Fatalf("应为 StreamToolCallEvent，实际 %T", events[0])
			}
			// 退化成 `{}` 而不是把原样字符串塞进去：`arguments` 在契约里是 dict，
			// 塞字符串下去会让客户端**整条消息**渲染失败（而不是丢一个字段）。
			if string(call.Arguments) != "{}" {
				t.Fatalf("非法 arguments 应退化成 {}，实际 %s", call.Arguments)
			}
		})
	}
}

// ---- 流的生命周期 ----

func TestChatStreamEmptyReferenceSetBecomesEmptyArray(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{
		{Event: &aiplatformv1.ChatEvent_Reference{Reference: &aiplatformv1.StreamReferences{}}},
	}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, err := drainStream(t, stream)
	if err != nil {
		t.Fatalf("收流失败: %v", err)
	}
	ref, ok := events[0].(biz.StreamReferenceEvent)
	if !ok {
		t.Fatalf("应为 StreamReferenceEvent，实际 %T", events[0])
	}
	// `[]` 而不是 `null`：客户端会直接遍历这个字段，null 会抛异常。
	// 而且空集合要**下发**（AI 每次重发全量的语义是「本轮引用集合」，
	// 一次空集合意味着「本轮没有引用」，客户端据此清空 [n] 编号）。
	if string(ref.References) != "[]" {
		t.Fatalf("空引用集合应为 []，实际 %s", ref.References)
	}
}

func TestChatStreamRecvErrorBecomesStreamErr(t *testing.T) {
	srv := &fakeStreamServer{
		events:   []*aiplatformv1.ChatEvent{{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "x"}}}},
		relayErr: status.Error(codes.Internal, "boom"),
	}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, streamErr := drainStream(t, stream)
	if streamErr == nil {
		t.Fatal("上游中途断流必须通过 Err() 报出来（否则 biz 会把它当正常收完）")
	}
	// 已收到的事件仍然有效：下游可以据此落 partial（这是「断连也要留正文」的依据）。
	if len(events) != 1 {
		t.Fatalf("断流前的事件应已投递，实际 %d 个", len(events))
	}
	if appErr, ok := errs.As(streamErr); !ok || appErr.Code() == "" {
		t.Fatalf("流内错误也应归一成业务错误码，实际 %T: %v", streamErr, streamErr)
	}
}

func TestChatStreamNormalEOFIsNotAnError(t *testing.T) {
	srv := &fakeStreamServer{events: []*aiplatformv1.ChatEvent{}}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, streamErr := drainStream(t, stream)
	// **证伪点**：把 `io.EOF` 当成错误处理时，biz 会把「上游答完了」判成
	// `upstream_broken`，落库状态变成 partial —— 用户看到的每条回答都带「不完整」标记。
	if streamErr != nil {
		t.Fatalf("上游正常收尾（EOF）不应是错误，实际 %v", streamErr)
	}
	if len(events) != 0 {
		t.Fatalf("空流不应有事件，实际 %d 个", len(events))
	}
}

func TestChatStreamPreStreamErrorArrivesOnFirstRecv(t *testing.T) {
	// gRPC 的 server-streaming 语义：服务端在第一个 `yield` 之前 `abort`（AI 侧的
	// `prepare` 失败就是这种）时，**客户端建流调用本身仍会成功返回** —— 错误是在
	// 第一次 `Recv` 时以 status 形式到达的。这正是 AI 侧那条
	// 「先 prepare 再 yield」的注释所依赖的机制。
	//
	// 对上层而言这仍然等价于「建连失败」：零事件 + `Err()` 非 nil，
	// 而 `sink.Started()` 为 false，所以调用方依旧能回 4xx/5xx 信封。
	lis := bufconn.Listen(1 << 20)
	g := grpc.NewServer()
	aiplatformv1.RegisterAiPlatformServer(g, &rejectingServer{})
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)

	conn, err := dialForTest(t, lis)
	if err != nil {
		t.Fatalf("建立 gRPC 连接失败: %v", err)
	}
	streamer := newChatStreamerFromConn(conn, testOptions(t))

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("server-streaming 下建流本身不会失败，实际 %v", err)
	}
	defer func() { _ = stream.Close() }()

	events, streamErr := drainStream(t, stream)
	if streamErr == nil {
		t.Fatal("AI 侧 abort 必须通过 Err() 报出来，否则上层会当成正常空答案")
	}
	if len(events) != 0 {
		t.Fatalf("准备阶段失败时不应有任何事件，实际 %d 个", len(events))
	}
	// 业务码必须还在（网关据此回 4xx/5xx 而不是笼统的 500）。
	appErr, ok := errs.As(streamErr)
	if !ok || appErr.Code() == "" {
		t.Fatalf("应归一成业务错误码，实际 %T: %v", streamErr, streamErr)
	}
}

type rejectingServer struct {
	aiplatformv1.UnimplementedAiPlatformServer
}

func (r *rejectingServer) ChatStream(
	_ *aiplatformv1.ChatRequest, _ aiplatformv1.AiPlatform_ChatStreamServer,
) error {
	return status.Error(codes.Unavailable, "AI 侧没就绪")
}

func TestChatStreamCloseIsIdempotentAndStopsUpstream(t *testing.T) {
	srv := &fakeStreamServer{
		events: []*aiplatformv1.ChatEvent{
			{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "1"}}},
			{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "2"}}},
		},
		sendDelay: 200 * time.Millisecond,
		started:   make(chan struct{}, 8),
	}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}

	select {
	case <-srv.started:
	case <-time.After(3 * time.Second):
		t.Fatal("第一个事件没有送达")
	}
	// 收到第一帧就放弃（模拟客户端离开）：Close 必须可重复调用，
	// 而且要让通道最终关闭（否则消费方的 select 会永远挂着一个空 case）。
	if err := stream.Close(); err != nil {
		t.Fatalf("Close 不应报错: %v", err)
	}
	if err := stream.Close(); err != nil {
		t.Fatalf("Close 应可重复调用: %v", err)
	}

	select {
	case _, ok := <-stream.Events():
		if ok {
			// 通道里可能还残留一帧（缓冲区），继续读到关闭为止。
			deadline := time.After(3 * time.Second)
			for {
				select {
				case _, still := <-stream.Events():
					if !still {
						return
					}
				case <-deadline:
					t.Fatal("Close 之后事件通道必须关闭")
				}
			}
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Close 之后事件通道必须关闭")
	}
}

func TestChatStreamDeliversEachEventAsItArrives(t *testing.T) {
	// 「边到边发」而不是「攒完再发」：第一个事件到达时，第二个还没被上游发出。
	// 若传输层先收完再投递，首 token 时延就等于整轮时延（docs/04-§5 的 30ms 指标必挂）。
	srv := &fakeStreamServer{
		events: []*aiplatformv1.ChatEvent{
			{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "1"}}},
			{Event: &aiplatformv1.ChatEvent_Token{Token: &aiplatformv1.StreamToken{Delta: "2"}}},
		},
		sendDelay: 300 * time.Millisecond,
	}
	streamer := startFakeStream(t, srv)

	stream, err := streamer.ChatStream(context.Background(), chatRequest())
	if err != nil {
		t.Fatalf("建流失败: %v", err)
	}
	defer func() { _ = stream.Close() }()

	select {
	case ev := <-stream.Events():
		if tok, ok := ev.(biz.StreamTokenEvent); !ok || tok.Delta != "1" {
			t.Fatalf("第一帧应为 token(1)，实际 %#v", ev)
		}
	case <-time.After(250 * time.Millisecond):
		t.Fatal("第一个事件应在第二个事件被发送之前就到达（否则首 token 时延被拉长）")
	}
}
