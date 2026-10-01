package ai

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// 本文件是 gRPC 流式客户端（`ChatStream`），实现 biz.ChatStreamer。
// 与非流式 `Chat` 共用拨号参数、metadata、错误映射、请求映射与各种 `marshal*`；
// 刻意不共用的是超时的归属，见下面 streamDeadline 的注释。

// streamEventBuffer 是事件通道的缓冲深度。
// 1 就够了：消费方（biz.pump）是同步 select 读取的，缓冲只吸收「上游已到、
// 消费方正好在处理上一帧」的那一次抖动；给大了会让「客户端很慢」在内存里堆积（每帧正文可能几 KB）。
const streamEventBuffer = 1

// streamDeadlineSlack 是传输层兜底 deadline 相对整轮上限的宽限。
// 取值必须足够让 biz 先触发：biz 触发时能发 `error(AI_TIMEOUT)` 帧并落 `partial`；
// 传输层触发只能表现为流断开，biz 归成 `upstream_broken`，客户端拿不到原因。
const streamDeadlineSlack = 10 * time.Second

// grpcChatStreamer 用 gRPC server-streaming 实现 biz.ChatStreamer。
type grpcChatStreamer struct {
	client aiplatformv1.AiPlatformClient
	opt    Options
}

// 编译期断言：接口变了要在这里先报错，而不是在 main 的装配处。
var _ biz.ChatStreamer = (*grpcChatStreamer)(nil)

// NewChatClients 用同一条 gRPC 连接组装非流式与流式客户端。
// 共用一个 ClientConn 而不是各拨一次：两条路指向同一个上游、用同一套参数，
// 拨两次只会多一条 TCP 连接，而它们本来就是同一个服务的两个方法，换不来任何隔离。
func NewChatClients(ctx context.Context, opt Options) (biz.ChatOrchestrator, biz.ChatStreamer, error) {
	conn, err := dialAI(ctx, opt)
	if err != nil {
		return nil, nil, err
	}
	return newChatOrchestratorFromConn(conn, opt), newChatStreamerFromConn(conn, opt), nil
}

// newChatStreamerFromConn 用一条已建立的连接组装流式客户端（单测注入 bufconn）。
func newChatStreamerFromConn(conn grpc.ClientConnInterface, opt Options) biz.ChatStreamer {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	return &grpcChatStreamer{client: aiplatformv1.NewAiPlatformClient(conn), opt: opt}
}

// streamDeadline 是传输层的兜底超时：整轮上限 + 宽限。
func (o Options) streamDeadline() time.Duration {
	total := o.TotalTimeout
	if total <= 0 {
		return 0
	}
	return total + streamDeadlineSlack
}

// ChatStream 发起一次流式对话。
//
// 不设整轮 deadline：整轮上限由 biz 的编排循环负责（`pump` 的 `total` 定时器），
// 因为超时那一刻要「发 `error` 帧 + 落 partial」，而传输层超时只会让流无声断掉。
// 这里只加一个更宽松的兜底 deadline（`streamDeadlineSlack`），防的是 biz 的定时器有 bug 时连接永远挂着。
//
// MUST NOT 重试：重试意味着重复计费（docs/04-§3.4 明确把 `ChatStream` 排除在重试策略之外）。
func (c *grpcChatStreamer) ChatStream(parent context.Context, req biz.ChatRequest) (biz.ChatEventStream, error) {
	callCtx := parent
	var cancel context.CancelFunc = func() {}
	if d := c.opt.streamDeadline(); d > 0 {
		callCtx, cancel = context.WithTimeout(parent, d)
	}
	callCtx = metadata.AppendToOutgoingContext(callCtx, outgoingMeta(parent, req)...)

	stream, err := c.client.ChatStream(callCtx, toProtoRequest(req))
	if err != nil {
		// 建流失败（连接没有、上游直接回错误状态）。此时还没有任何事件，
		// 调用方可以照常返回 4xx/5xx 信封，所以这里必须把错误原样交给业务层判断，
		// 而不是伪造成「流里的一个 error 事件」。
		cancel()
		return nil, mapAIError(callCtx, err, nil, c.opt)
	}

	out := &grpcChatEventStream{
		parent: parent,
		ctx:    callCtx,
		cancel: cancel,
		stream: stream,
		opt:    c.opt,
		events: make(chan biz.StreamEvent, streamEventBuffer),
	}
	go out.pump()
	return out, nil
}

// grpcChatEventStream 把 gRPC 的 Recv 循环桥接成 biz.ChatEventStream。
// 需要一条 goroutine 而不是让 biz 直接调 `Recv()`：biz 在等事件的同时还要发心跳、
// 判超时、响应取消（见 biz.ChatEventStream 的契约注释），不能阻塞在一个 Recv 上。
type grpcChatEventStream struct {
	parent context.Context
	ctx    context.Context
	cancel context.CancelFunc
	stream aiplatformv1.AiPlatform_ChatStreamClient
	opt    Options

	events chan biz.StreamEvent

	mu     sync.Mutex
	err    error
	closed bool
}

var _ biz.ChatEventStream = (*grpcChatEventStream)(nil)

// Events 返回事件通道，供 biz 消费。
// 通道由后台接收协程写入、收尾时关闭：读到关闭即表示 gRPC 流已结束（正常由 done 帧结束，异常见 Err）。
func (s *grpcChatEventStream) Events() <-chan biz.StreamEvent { return s.events }

// Err 返回流结束的原因；正常收尾返回 nil。
// 必须在 Events 关闭之后读取才有意义（关闭前它可能还是 nil），加锁是为了与接收协程的写入互斥。
func (s *grpcChatEventStream) Err() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

// Close 取消上游调用（biz 提前放弃时用；正常读完通道后调它也无害）。
func (s *grpcChatEventStream) Close() error {
	s.mu.Lock()
	first := !s.closed
	s.closed = true
	s.mu.Unlock()
	if first {
		s.cancel()
	}
	return nil
}

func (s *grpcChatEventStream) setErr(err error) {
	s.mu.Lock()
	if s.err == nil {
		s.err = err
	}
	s.mu.Unlock()
}

// pump 读上游事件 → 映射 → 投递。通道关闭即表示流结束。
func (s *grpcChatEventStream) pump() {
	defer close(s.events)
	for {
		ev, err := s.stream.Recv()
		if err != nil {
			if errors.Is(err, io.EOF) {
				// 上游正常收尾（server-streaming 的 EOF 等价于「没有更多事件」）。
				// 留 nil：不要把 EOF 当成错误 ——「上游答完了」与「上游断了」在 biz 那里是两种状态。
				return
			}
			s.setErr(mapAIError(s.ctx, err, s.stream.Trailer(), s.opt))
			return
		}
		out, ok := mapChatEvent(ev, s.opt.Log, s.ctx)
		if !ok {
			// 空事件（proto 的 oneof 没设）：跳过这一条但继续读 ——
			// 在这里 return 会因为一个还没定义的事件类型把整轮回答丢掉。
			continue
		}
		select {
		case s.events <- out:
		case <-s.parent.Done():
			// 消费方已经不要了（客户端断开）。主动收尾，不再等上游。
			s.setErr(s.parent.Err())
			return
		}
	}
}

// mapChatEvent 把 proto 事件映射成 biz 事件；`ok=false` 表示这条事件没有内容（oneof 为空）。
// 注意 `unknown` 分支是有内容的：docs/04-§4.1 要求未知事件必须透传，
// 所以它映射成 `biz.StreamUnknownEvent` 原样带出去。
func mapChatEvent(ev *aiplatformv1.ChatEvent, log *slog.Logger, ctx context.Context) (biz.StreamEvent, bool) {
	if ev == nil {
		return nil, false
	}
	switch e := ev.GetEvent().(type) {
	case *aiplatformv1.ChatEvent_Meta:
		m := e.Meta
		return biz.StreamMetaEvent{
			ConversationID:  m.GetConversationId(),
			MessageID:       m.GetMessageId(),
			Model:           m.GetModel(),
			CreatedAt:       m.GetCreatedAt(),
			Degraded:        m.GetDegraded(),
			DegradedReasons: m.GetDegradedReasons(),
		}, true

	case *aiplatformv1.ChatEvent_Reference:
		// 与非流式 `ChatResponse.references` 是同一套字段，复用 `marshalReferences` ——
		// 两条路的落库形状必须一致，否则字段差异会到前端才被发现。
		raw, err := marshalReferences(e.Reference.GetReferences())
		if err != nil {
			return nil, false
		}
		if raw == nil {
			// 空集合：AI 每次重发全量，空集合意味着「本轮没有引用」。
			// 仍要下发（客户端据此清空编号），累积器那边会把它当空处理。
			raw = []byte("[]")
		}
		return biz.StreamReferenceEvent{References: raw}, true

	case *aiplatformv1.ChatEvent_Token:
		return biz.StreamTokenEvent{Delta: e.Token.GetDelta()}, true

	case *aiplatformv1.ChatEvent_ToolCall:
		return biz.StreamToolCallEvent{
			CallID:    e.ToolCall.GetCallId(),
			Name:      e.ToolCall.GetName(),
			Arguments: argumentsJSON(e.ToolCall.GetArgumentsJson(), log, ctx),
		}, true

	case *aiplatformv1.ChatEvent_ToolResult:
		return biz.StreamToolResultEvent{
			CallID:    e.ToolResult.GetCallId(),
			Name:      e.ToolResult.GetName(),
			Status:    e.ToolResult.GetStatus(),
			Summary:   e.ToolResult.GetSummary(),
			ElapsedMS: int(e.ToolResult.GetElapsedMs()),
		}, true

	case *aiplatformv1.ChatEvent_Usage:
		u := usageFromProto(e.Usage)
		if u == nil {
			return nil, false
		}
		return biz.StreamUsageEvent{Usage: *u}, true

	case *aiplatformv1.ChatEvent_Error:
		return biz.StreamErrorEvent{
			Code:      e.Error.GetCode(),
			Message:   e.Error.GetMessage(),
			Retryable: e.Error.GetRetryable(),
		}, true

	case *aiplatformv1.ChatEvent_Done:
		return biz.StreamDoneEvent{
			FinishReason: e.Done.GetFinishReason(),
			ElapsedMS:    int(e.Done.GetElapsedMs()),
			Partial:      e.Done.GetPartial(),
		}, true

	case *aiplatformv1.ChatEvent_Unknown:
		u := e.Unknown
		name := strings.TrimSpace(u.GetEvent())
		if name == "" {
			// 没有事件名的「未知事件」无法下发（SSE 没有 `event:` 名就只能是默认的 `message`，
			// 客户端会当成另一个事件）。只能丢，但要留痕 —— 静默丢帧正是该需求要防的事。
			log.WarnContext(ctx, "ai.stream_unknown_without_name",
				slog.String("data", truncateForLog(string(u.GetDataJson()))))
			return nil, false
		}
		return biz.StreamUnknownEvent{
			Name: name,
			Data: rawOrEmpty(u.GetDataJson()),
		}, true

	default:
		return nil, false
	}
}

// argumentsJSON 把 proto 的 `arguments_json` 字符串转成对象 JSON。
// 转不动就回空对象：`arguments` 在契约里是 dict（docs/03-§4.1），塞字符串会让客户端
// 反序列化失败 —— 失败的将是整条消息的渲染，而不是一个字段。
func argumentsJSON(raw string, log *slog.Logger, ctx context.Context) []byte {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return []byte("{}")
	}
	if !jsonIsObject(trimmed) {
		log.WarnContext(ctx, "ai.stream_tool_arguments_invalid",
			slog.String("arguments", truncateForLog(trimmed)))
		return []byte("{}")
	}
	return []byte(trimmed)
}

// jsonIsObject 判断一段 JSON 文本是不是对象。
// 首字节与 `json.Valid` 两者都要：只看首字节会把 `{"a":1} 后面跟着垃圾` 当合法对象；
// 只看 `json.Valid` 则数组 `[1,2]` 也是「合法 JSON」，而契约要求这里是对象。
func jsonIsObject(s string) bool {
	if s == "" || s[0] != '{' {
		return false
	}
	return json.Valid([]byte(s))
}

// rawOrEmpty 保证未知事件的 `data` 是一个合法 JSON 值。
// 为空时给 `null` 而不是空串：空串会让 SSE 的 `data:` 行为空，客户端 `JSON.parse("")` 直接抛异常。
func rawOrEmpty(raw []byte) []byte {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 {
		return []byte("null")
	}
	return trimmed
}

// truncateForLog 截断日志里的长字段（工具参数可能是几 KB 的 JSON）。
func truncateForLog(s string) string {
	const maxLogField = 256
	if len(s) <= maxLogField {
		return s
	}
	return s[:maxLogField] + "...(truncated)"
}
