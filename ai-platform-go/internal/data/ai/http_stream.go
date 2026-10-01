package ai

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// 本文件是 `ChatStream` 的 HTTP/SSE 兜底通道（`AI_GRPC_ENABLED=false` 时启用），
// 与 httpchat.go 共用请求映射、响应映射与错误信封解析。
// 理由同 `httpchat.go`（REQ-ORCH-002 第 3 条），并多一层价值：AI 侧只起 HTTP 服务时也能联调流式。

// sseMaxLineBytes 是单行上限。
// 超长行必须报错而不是截断：截断后的 JSON 一定解析失败，症状会变成「偶发丢一帧」，
// 而真相是上游写坏了流。可能大的是 `reference` 帧（几条 KB），1MB 已远超合理范围。
const sseMaxLineBytes = 1 << 20

// sseEventMessage 是 SSE 规范里「没有 `event:` 字段」时的默认事件名。
// 网关不认识它（走未知事件透传），但必须显式命名 —— 丢掉名字会让客户端落进 default。
const sseEventMessage = "message"

// httpChatStreamer 用 HTTP/SSE 实现 `biz.ChatStreamer`。
type httpChatStreamer struct {
	base *urlBase
	http *http.Client
	// path 是上游 `POST /chat/stream` 的完整路径（含 AI 的 API 前缀）。
	path string
	opt  Options
}

var _ biz.ChatStreamer = (*httpChatStreamer)(nil)

// NewChatStreamerHTTP 构造 HTTP/SSE 通道的流式客户端。
//
// 收 `apiPrefix` 的理由同 `NewChatOrchestratorHTTP`（两侧前缀同名是透传的前提，docs/04-§7）。
// 它自建 `*http.Client` 而不复用 `biz.AIProxy`：那个接口的契约是「把响应体完整读完再交回」
// （返回 `AIProxyResponse{Body []byte}`），而流式要的是尚未读完的响应体；
// 把 `Do` 改成能返回流，等于让每个透传调用方都去关心「这个响应该不该读完」。
func NewChatStreamerHTTP(apiPrefix string, opt Options) biz.ChatStreamer {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	prefix := strings.TrimRight(apiPrefix, "/")
	return &httpChatStreamer{
		base: newURLBase(opt.BaseURL),
		http: newHTTPClient(opt),
		path: prefix + "/chat/stream",
		opt:  opt,
	}
}

// ChatStream 实现 biz.ChatStreamer。
// 超时归属与 gRPC 通道一致：整轮/空闲/首字节超时全由 biz 负责，这里只交出去请求上下文。
// 不设 `Client.Timeout`：它是对「整个请求（含读完 body）」的上限，对 SSE 等于「到点就掉」，
// 而且给出的错误看不出「谁的超时」，会让 biz 归错类。
func (s *httpChatStreamer) ChatStream(parent context.Context, req biz.ChatRequest) (biz.ChatEventStream, error) {
	upstreamURL, err := s.base.join(s.path)
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "invalid_upstream_path").
			WithCause(err)
	}

	body, err := json.Marshal(toHTTPChatStreamRequest(req))
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "marshal_request").
			WithCause(err)
	}

	// 独立的 cancel：`Close()` 需要能主动掐掉上游（biz 提前放弃时）；
	// 只 close resp.Body 对 HTTP/2 可能不够及时，cancel 一定能。
	ctx, cancel := context.WithCancel(parent)

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, upstreamURL, bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "build_request_failed").
			WithCause(err)
	}
	httpReq.ContentLength = int64(len(body))
	httpReq.Header.Set("Content-Type", "application/json")
	// `Accept` 显式声明：AI 侧据此（以及 Body 里的 stream=true）选流式分支，
	// 抓包时也能一眼看出这是流式请求。
	httpReq.Header.Set("Accept", "text/event-stream")
	httpReq.Header.Set("Authorization", "Bearer "+strings.TrimSpace(req.UserToken))
	if traceID := strings.TrimSpace(req.TraceID); traceID != "" {
		httpReq.Header.Set("X-Trace-Id", traceID)
	}
	// 同 http.go：标准头 + 自有头一起发，否则 Jaeger 里会分成两条 trace。
	if tp := otelx.TraceparentHeader(ctx, req.TraceID); tp != "" {
		httpReq.Header.Set("traceparent", tp)
	}
	// 同样不设 Accept-Encoding：手动设置会关掉 Go 的透明解压，而流式响应一旦被压缩就得自己解 ——
	// 压缩破坏增量性（gzip 要攒够一块才能吐），等于引入一整层 buffer 语义。

	resp, err := s.http.Do(httpReq)
	if err != nil {
		cancel()
		return nil, s.mapOpenError(ctx, err, traceID(req))
	}
	if resp == nil {
		cancel()
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "empty_response")
	}

	// 状态码 >= 400：此时**响应还没开始推送**，可以照常回信封/归一化错误。
	if resp.StatusCode >= 400 {
		defer func() { _ = resp.Body.Close() }()
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		if appErr, ok := errs.ParseEnvelope(raw, resp.StatusCode); ok {
			cancel()
			return nil, appErr
		}
		cancel()
		return nil, errs.New(codeForStatus(resp.StatusCode)).
			WithTraceID(traceID(req)).
			WithDetail("reason", "non_envelope_body").
			WithDetail("upstream_status", resp.StatusCode)
	}

	// Content-Type 必须真的是 SSE。若上游回了 200 + JSON（例如路径写错落到 `/chat` 上），
	// 不检查的话表现是「流里一帧都没有，最后落一条空的 partial」，现象无法反推原因。
	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "text/event-stream") {
		defer func() { _ = resp.Body.Close() }()
		cancel()
		return nil, errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID(req)).
			WithDetail("reason", "unexpected_content_type").
			WithDetail("content_type", ct)
	}

	out := &httpChatEventStream{
		ctx:     ctx,
		cancel:  cancel,
		body:    resp.Body,
		decoder: newSSEDecoder(resp.Body),
		opt:     s.opt,
		events:  make(chan biz.StreamEvent, streamEventBuffer),
	}
	go out.pump()
	return out, nil
}

// traceID 从领域请求里取 trace id（只为少写一个局部变量）。
func traceID(req biz.ChatRequest) string { return strings.TrimSpace(req.TraceID) }

// mapOpenError 把「连请求都没发出去 / 流还没建立」的传输错误翻译成统一错误。
func (s *httpChatStreamer) mapOpenError(ctx context.Context, err error, traceID string) error {
	switch {
	case errors.Is(ctx.Err(), context.Canceled) || errors.Is(err, context.Canceled):
		// 客户端断开：不是网关或上游的问题，别归类成 AI_UNAVAILABLE 而拉高熔断计数。
		return errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID).
			WithDetail("reason", "client_canceled").
			WithCause(err)
	case errors.Is(ctx.Err(), context.DeadlineExceeded) || errors.Is(err, context.DeadlineExceeded):
		return errs.New(errs.CodeAITimeout).
			WithTraceID(traceID).
			WithDetail("reason", "gateway_deadline_exceeded").
			WithCause(err)
	default:
		s.opt.Log.WarnContext(ctx, "ai_stream.transport_error", "error", err.Error())
		return errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID).
			WithDetail("reason", "transport_error").
			WithCause(err)
	}
}

// httpChatEventStream 把 SSE 解码循环桥接成 biz.ChatEventStream。
type httpChatEventStream struct {
	ctx     context.Context
	cancel  context.CancelFunc
	body    io.ReadCloser
	decoder *sseDecoder
	opt     Options
	events  chan biz.StreamEvent

	mu     sync.Mutex
	err    error
	closed bool
}

var _ biz.ChatEventStream = (*httpChatEventStream)(nil)

// Events 返回事件通道，供 biz 消费。
// 通道由后台解码协程写入、收尾时关闭：读到关闭即表示上游流已结束（正常由 done 帧结束，异常见 Err）。
func (s *httpChatEventStream) Events() <-chan biz.StreamEvent { return s.events }

// Err 返回流结束的原因；正常收尾返回 nil。
// 必须在 Events 关闭之后读取才有意义（关闭前它可能还是 nil），加锁是为了与解码协程的写入互斥。
func (s *httpChatEventStream) Err() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.err
}

// Close 掐掉上游连接。
func (s *httpChatEventStream) Close() error {
	s.mu.Lock()
	first := !s.closed
	s.closed = true
	s.mu.Unlock()
	if first {
		s.cancel()
	}
	return s.body.Close()
}

func (s *httpChatEventStream) setErr(err error) {
	s.mu.Lock()
	if s.err == nil {
		s.err = err
	}
	s.mu.Unlock()
}

func (s *httpChatEventStream) pump() {
	defer close(s.events)
	for {
		frame, err := s.decoder.next()
		if err != nil {
			if errors.Is(err, io.EOF) {
				// 上游正常关流。这里不判断有没有收到 done —— 那是 biz 的事
				//（它据此区分 completed / partial），传输层越权判断会让这条规则出现在两个地方。
				return
			}
			s.setErr(s.mapReadError(err))
			return
		}
		out, ok := mapSSEFrame(frame, s.opt.Log, s.ctx)
		if !ok {
			continue
		}
		select {
		case s.events <- out:
		case <-s.ctx.Done():
			s.setErr(s.ctx.Err())
			return
		}
	}
}

// mapReadError 把「流中途读失败」翻译成统一错误。
func (s *httpChatEventStream) mapReadError(err error) error {
	switch {
	case errors.Is(s.ctx.Err(), context.Canceled) || errors.Is(err, context.Canceled):
		return errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "client_canceled").
			WithCause(err)
	case errors.Is(s.ctx.Err(), context.DeadlineExceeded):
		return errs.New(errs.CodeAITimeout).
			WithDetail("reason", "gateway_deadline_exceeded").
			WithCause(err)
	default:
		// 连接被上游掐断、响应体被截断、gzip 头非法等等。
		s.opt.Log.WarnContext(s.ctx, "ai_stream.read_error", "error", err.Error())
		return errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "stream_broken").
			WithCause(err)
	}
}

// ---- SSE 解码 ----

// sseFrame 是一帧解析后的 SSE 事件。
type sseFrame struct {
	Event string
	Data  []byte
}

// sseDecoder 逐行解析 SSE（WHATWG EventSource 规则的一个子集）。
// 实现了：注释行忽略、`field: value` 的一个前导空格、多行 `data:` 用 `\n` 拼接、
// 空行派发、未知字段忽略、`event` 缺省为 `message`。
// 没实现：`Last-Event-ID` / `retry`（网关不重连）、`\r` 单独作行结束符（AI 侧只产 `\n`，
// 支持它得自建行缓冲而收益为零）。
type sseDecoder struct {
	sc    *bufio.Scanner
	event string
	data  bytes.Buffer
	// dataSeen 记录「是否已经有 data 行」。用它而不是 `data.Len() > 0`：
	// 首行是空 payload（`data:`）时长度也是 0，后续行会漏掉分隔用的 `\n`，
	// 于是 `data:\ndata: b` 会拼成 `b` 而不是 `\nb`。
	dataSeen bool
}

func newSSEDecoder(r io.Reader) *sseDecoder {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 4096), sseMaxLineBytes)
	return &sseDecoder{sc: sc}
}

// next 返回下一帧；流结束时返回 io.EOF。
// 返回 `io.EOF` 而不是 `(nil, nil)`：调用方必须能区分「流结束」与「这一行还没构成一帧」
// （注释行、保活空行），否则会在循环里空转。
func (d *sseDecoder) next() (*sseFrame, error) {
	for {
		if !d.sc.Scan() {
			if err := d.sc.Err(); err != nil {
				return nil, err
			}
			return nil, io.EOF
		}
		// ScanLines 已经去掉了行尾的 `\n` 与 `\r`（CRLF 与 LF 都能处理）。
		line := d.sc.Text()

		if line == "" {
			if d.event == "" && !d.dataSeen {
				// 连续空行（有些实现用空行保活）。不派发空事件 —— 那会变成一帧 data 为空的 `message`。
				continue
			}
			frame := &sseFrame{Event: d.event, Data: append([]byte(nil), d.data.Bytes()...)}
			if frame.Event == "" {
				frame.Event = sseEventMessage
			}
			d.event = ""
			d.data.Reset()
			d.dataSeen = false
			return frame, nil
		}
		if line[0] == ':' {
			continue // 注释行：规范要求忽略（反向代理有时会插入）
		}
		field, value, _ := strings.Cut(line, ":")
		// 规范：冒号后最多去掉一个空格，后面的空格是值的一部分。
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			d.event = value
		case "data":
			if d.dataSeen {
				d.data.WriteByte('\n')
			}
			d.data.WriteString(value)
			d.dataSeen = true
		default:
			// `id` / `retry` / 未知字段：忽略。这是规范要求的行为，与 biz 的
			//「未知事件必须透传」不冲突 —— 后者说的是 `event:` 类型，不是这里的分帧字段。
		}
	}
}

// mapSSEFrame 把一帧 SSE 映射成 biz 事件。
// 返回 `ok=false` 表示这一帧被有意丢弃（心跳、非法 JSON、未知且空的分帧）。
// 丢帧一律留痕：静默丢是真出问题时最难查的那类。
func mapSSEFrame(f *sseFrame, log *slog.Logger, ctx context.Context) (biz.StreamEvent, bool) {
	switch f.Event {
	case "ping":
		// docs/04-§2.3 的映射表：`ping` 不映射（心跳由传输层自带）。
		// 网关自己按 15s 间隔发 `event: ping`，把上游的也转出去会让客户端收到两路心跳。
		return nil, false

	case "meta":
		var payload struct {
			ConversationID string `json:"conversation_id"`
			MessageID      string `json:"message_id"`
			Model          string `json:"model"`
			CreatedAt      string `json:"created_at"`
			Degraded       bool   `json:"degraded"`
			// 上游 HTTP 版 meta 没有降级原因（只有布尔），留字段是为了「哪天 AI 侧补上就不用改代码」。
			DegradedReasons []string `json:"degraded_reasons"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamMetaEvent{
			ConversationID:  payload.ConversationID,
			MessageID:       payload.MessageID,
			Model:           payload.Model,
			CreatedAt:       payload.CreatedAt,
			Degraded:        payload.Degraded,
			DegradedReasons: payload.DegradedReasons,
		}, true

	case "reference":
		var payload struct {
			References json.RawMessage `json:"references"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		// biz 侧要的是数组本身（与非流式 `references` 列同形），
		// 而 SSE 的 data 是 `{"references": [...]}` 的包一层的结构。
		refs := bytes.TrimSpace(payload.References)
		if len(refs) == 0 {
			refs = []byte("[]")
		}
		return biz.StreamReferenceEvent{References: refs}, true

	case "token":
		var payload struct {
			Delta string `json:"delta"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamTokenEvent{Delta: payload.Delta}, true

	case "tool_call":
		// ⚠️ 与 gRPC 的 `tool_call` 字段名不同：HTTP 版直接给对象
		// （AI 侧 `_tool_call_payload` 返回 `"arguments": {...}`），
		// proto 因为缺「任意 JSON」类型只能传字符串 `arguments_json`。
		// 两边都归一到 biz 的「对象」形态，落库形状才能一致。
		var payload struct {
			CallID    string          `json:"call_id"`
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamToolCallEvent{
			CallID:    payload.CallID,
			Name:      payload.Name,
			Arguments: argumentsJSON(string(payload.Arguments), log, ctx),
		}, true

	case "tool_result":
		var payload struct {
			CallID    string `json:"call_id"`
			Name      string `json:"name"`
			Status    string `json:"status"`
			Summary   string `json:"summary"`
			ElapsedMS int    `json:"elapsed_ms"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamToolResultEvent{
			CallID:    payload.CallID,
			Name:      payload.Name,
			Status:    payload.Status,
			Summary:   payload.Summary,
			ElapsedMS: payload.ElapsedMS,
		}, true

	case "usage":
		var payload struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		usage := biz.MessageUsage{
			PromptTokens:     payload.PromptTokens,
			CompletionTokens: payload.CompletionTokens,
			TotalTokens:      payload.TotalTokens,
		}
		if usage.TotalTokens == 0 {
			usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
		}
		return biz.StreamUsageEvent{Usage: usage}, true

	case "error":
		var payload struct {
			Code      string `json:"code"`
			Message   string `json:"message"`
			Retryable bool   `json:"retryable"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamErrorEvent{
			Code:      payload.Code,
			Message:   payload.Message,
			Retryable: payload.Retryable,
		}, true

	case "done":
		var payload struct {
			FinishReason string `json:"finish_reason"`
			ElapsedMS    int    `json:"elapsed_ms"`
			Partial      bool   `json:"partial"`
		}
		if !decodeFrame(f, &payload, log, ctx) {
			return nil, false
		}
		return biz.StreamDoneEvent{
			FinishReason: payload.FinishReason,
			ElapsedMS:    payload.ElapsedMS,
			Partial:      payload.Partial,
		}, true

	default:
		// 未知事件原样透传（docs/04-§4.1）。给原始 data 字节而不重新序列化 ——
		// 网关不知道它的语义，重新序列化会动数字精度、字段顺序与嵌套结构。
		return biz.StreamUnknownEvent{Name: f.Event, Data: f.Data}, true
	}
}

// decodeFrame 把帧的 data 解析进 out；失败时留痕并返回 false。
// 单帧解析失败不终止整条流：一帧坏数据不该让用户已经等了几秒的回答全丢。
// 但必须留痕 —— 静默少一帧的症状是「正文里少了一小段」，几乎不可能事后定位。
func decodeFrame(f *sseFrame, out any, log *slog.Logger, ctx context.Context) bool {
	if err := json.Unmarshal(f.Data, out); err != nil {
		log.WarnContext(ctx, "ai_stream.frame_decode_failed",
			slog.String("event", f.Event),
			slog.String("data", truncateForLog(string(f.Data))),
			slog.String("error", err.Error()),
		)
		return false
	}
	return true
}
