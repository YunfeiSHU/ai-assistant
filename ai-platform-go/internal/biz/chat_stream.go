package biz

import (
	"context"
	"encoding/json"
	"errors"
)

// 本文件是流式编排的接缝（docs/04-§2.2 的 `ChatStream`，M4）。
//
// 与 chat.go 的关系：`ChatOrchestrator` 是一问一答，本文件是一问边答边收，
// 共用 `ChatRequest`/`MessageUsage`，但事件流不走同一接口 ——
// 让 `Chat` 也返回流会让非流式路径自己把流收完再拼，多一层没人需要的抽象。
// 事件名作为契约常量集中在这里，docs/04-§2.3 的映射表两侧都要遵守。

// 流式事件名（docs/04-§2.3 的事件映射表），与 AI 侧 `app/core/sse.py` 的 `EVENT_*` 逐字对应。
// 改这里必须同时改那里（两侧各有一条常量一致性断言）。
const (
	// StreamEventMeta 携带会话/消息 ID 等元信息，是流的第一帧。
	StreamEventMeta = "meta"
	// StreamEventReference 携带 RAG 命中的引用片段（无检索时不出现）。
	StreamEventReference = "reference"
	// StreamEventToken 是正文增量；客户端按到达顺序拼接。
	StreamEventToken = "token"
	// StreamEventToolCall 表示模型发起了一次工具调用。
	StreamEventToolCall = "tool_call"
	// StreamEventToolResult 是上一条 tool_call 的执行结果（状态见 ToolCallStatus*）。
	StreamEventToolResult = "tool_result"
	// StreamEventUsage 携带 token 用量，通常在收尾前给出。
	StreamEventUsage = "usage"
	// StreamEventError 表示流中途失败；收到它之后不应再期待 done。
	StreamEventError = "error"
	// StreamEventDone 是正常收尾标记，也是客户端判定「回答完整」的唯一依据。
	StreamEventDone = "done"

	// 网关追加的事件（docs/04-§4.1 只允许两类，另一类 `gw_degraded` 等有生产者时再加）。
	// SHOULD 出现在 `done` 之后：插在 `token` 之间会打断客户端的正文拼接。
	StreamEventGwPersistError = "gw_persist_error"
)

// 工具调用状态（docs/03-§4.1 的 `tool_calls[].status`），必须与 AI 侧
// `app/schemas/chat.py` 的 `Literal["ok","error","timeout","forbidden"]` 一致：客户端按它决定图标。
const (
	// ToolCallStatusOK 表示工具调用成功返回。
	ToolCallStatusOK = "ok"
	// ToolCallStatusError 表示工具执行报错（工具自己的失败，不是链路故障）。
	ToolCallStatusError = "error"
	// ToolCallStatusTimeout 表示工具执行超时。
	ToolCallStatusTimeout = "timeout"
	// ToolCallStatusForbidden 表示该工具不被允许调用（策略/权限拒绝）。
	ToolCallStatusForbidden = "forbidden"
)

// errStreamNoDone 表示上游干净地关掉了事件流却没发 `done`。
// 不复用 `io.EOF`：两者对外都落 `partial`，但排障要分清「正常收完」与「少发一帧」。
var errStreamNoDone = errors.New("上游事件流结束但未发送 done")

// StreamEvent 是一条待下发给客户端的事件。
//
// 用「接口 + 未导出标记方法」而非「带 N 个指针字段的大结构体」：后者零值组合无穷
// （`Name=""` 但 `Token` 非空是合法 Go 值却不是合法事件）。标记方法未导出 ⇒
// 包外无法新增实现 ⇒ service 的渲染 switch 能确保覆盖全部情况（新增类型会编译不过）。
type StreamEvent interface {
	// EventName 返回 SSE 的 `event:` 名（取值见上面的常量）。
	EventName() string
	streamEvent()
}

// StreamMetaEvent 是首帧（`event: meta`）。
type StreamMetaEvent struct {
	// ConversationID 是 AI 侧回显的会话 ID（可能为空）。网关 MUST 以自己
	// 传入的为准，不一致时告警（REQ-ORCH-006 / AC-ORCH-07）。
	ConversationID string
	// MessageID 是 AI 侧本条回答的 ID（网关自己的消息 ID 另生成）。
	MessageID string
	Model     string
	// CreatedAt 是 RFC3339 毫秒 UTC 字符串，原样透传（解析只会多一个「格式非法就失败」的分支）。
	CreatedAt string
	Degraded  bool
	// DegradedReasons 是降级原因列表，只用于落库（docs/04-§9），
	// 不会出现在下发给客户端的 `meta` 帧（那帧字段固定为契约的五个，docs/04-§2.3）。
	//
	// 这是网关在 proto 里补出的字段：gRPC 通道 AI 侧会填，HTTP/SSE 兜底通道的 meta 帧只有布尔，此时为空。
	DegradedReasons []string
}

func (StreamMetaEvent) EventName() string { return StreamEventMeta }
func (StreamMetaEvent) streamEvent()      {}

// StreamReferenceEvent 是引用帧（`event: reference`）。
// `References` 是本轮引用集合的完整 JSON 数组而非增量，故累积规则是「后者覆盖前者」
// 而不是追加：AI 侧每次重发全部引用（只发增量会让客户端 `[n]` 编号错位）。
type StreamReferenceEvent struct {
	References json.RawMessage
}

func (StreamReferenceEvent) EventName() string { return StreamEventReference }
func (StreamReferenceEvent) streamEvent()      {}

// StreamTokenEvent 是正文增量帧（`event: token`）。
type StreamTokenEvent struct {
	// Delta 是增量文本，可能为空串（上游偶发空 delta）。空串也必须原样下发：
	// 丢掉会让客户端收到的帧数与上游不一致，而 docs/04-§4.1 要求顺序与数量保真。
	Delta string
}

func (StreamTokenEvent) EventName() string { return StreamEventToken }
func (StreamTokenEvent) streamEvent()      {}

// StreamToolCallEvent 是工具调用开始帧（`event: tool_call`）。
type StreamToolCallEvent struct {
	CallID string
	Name   string
	// Arguments 是对象 JSON（不是字符串）。从 `arguments_json` 字符串转对象的动作在传输层
	//（data/ai）；若传字符串进来，落库与下发都要各自解析一次且都可能失败。
	Arguments json.RawMessage
}

func (StreamToolCallEvent) EventName() string { return StreamEventToolCall }
func (StreamToolCallEvent) streamEvent()      {}

// StreamToolResultEvent 是工具调用结果帧（`event: tool_result`），靠 `CallID` 与
// `StreamToolCallEvent` 配对。只有开始没有结果的调用由累积器兜底成 `status=error`，
// 否则前端会渲染出一个转不完的圈。
type StreamToolResultEvent struct {
	CallID    string
	Name      string
	Status    string
	Summary   string
	ElapsedMS int
}

func (StreamToolResultEvent) EventName() string { return StreamEventToolResult }
func (StreamToolResultEvent) streamEvent()      {}

// StreamUsageEvent 是用量帧（`event: usage`）。
type StreamUsageEvent struct {
	Usage MessageUsage
}

func (StreamUsageEvent) EventName() string { return StreamEventUsage }
func (StreamUsageEvent) streamEvent()      {}

// StreamErrorEvent 是错误帧（`event: error`）—— 流已开始推送后唯一的报错方式（状态码已发出）。
type StreamErrorEvent struct {
	Code      string
	Message   string
	Retryable bool
}

func (StreamErrorEvent) EventName() string { return StreamEventError }
func (StreamErrorEvent) streamEvent()      {}

// StreamDoneEvent 是结束帧（`event: done`）。
type StreamDoneEvent struct {
	// FinishReason 取值见 FinishReason* 常量（stop / length / max_steps / canceled）。
	FinishReason string
	ElapsedMS    int
	// Partial 表示本轮**提前结束**（上游超时/取消），落库状态取 `partial`。
	Partial bool
}

func (StreamDoneEvent) EventName() string { return StreamEventDone }
func (StreamDoneEvent) streamEvent()      {}

// StreamUnknownEvent 是未识别的事件（原样透传）：docs/04-§4.1 要求未知 `event:` 类型
// MUST 透传、MUST NOT 丢弃。丢掉它不报任何错，只是客户端少点信息，属于长期潜伏的静默降级。
type StreamUnknownEvent struct {
	Name string
	// Data 是原始 `data` 的 JSON 字节，原样转发（网关不解析也不重新序列化）。
	Data json.RawMessage
}

func (e StreamUnknownEvent) EventName() string { return e.Name }
func (StreamUnknownEvent) streamEvent()        {}

// StreamPersistErrorEvent 是落库失败时网关追加的事件（`event: gw_persist_error`）。
// 流式响应已发出、改不了状态码，若不告知，客户端会以为「回答已保存」。
type StreamPersistErrorEvent struct {
	// Reason 是**给客户端排障看的**短语，不是完整错误：
	// 完整错误链里可能有 SQL 片段或表名，它属于日志（biz 已经记过一条 ERROR）。
	Reason string
}

func (StreamPersistErrorEvent) EventName() string { return StreamEventGwPersistError }
func (StreamPersistErrorEvent) streamEvent()      {}

// ---- 流的终止原因 ----

// streamEnd 说明「谁结束了这次流」，同时决定落库状态、是否补发 `error` 帧、日志级别。
// 用枚举而非事后看 error 是不是 context.Canceled：「上游静默断开」与「客户端断连」
// 都可能表现为 context.Canceled。
type streamEnd int

const (
	// streamEndUpstream：上游收完（收到 `done` 且通道关闭）。不补发任何帧。
	streamEndUpstream streamEnd = iota
	// streamEndUpstreamError：上游自己发了 `error` 帧。不补发（已经发过了）。
	streamEndUpstreamError
	// streamEndUpstreamBroken：上游没发 `done` 就断了（连接被掐 / AI 进程重启）。
	streamEndUpstreamBroken
	// streamEndTimeout：首字节 / 空闲 / 整轮超时。补发 `error(AI_TIMEOUT)`。
	streamEndTimeout
	// streamEndShutdown：网关优雅退出。补发 `error(SERVICE_SHUTTING_DOWN)`。
	streamEndShutdown
	// streamEndClientGone：客户端断连或写响应失败。**什么都不发**（连接没了）。
	streamEndClientGone
)

// String 让日志里的 `end` 字段可读（枚举默认打印成数字，排障时对不上表）。
func (e streamEnd) String() string {
	switch e {
	case streamEndUpstream:
		return "upstream_done"
	case streamEndUpstreamError:
		return "upstream_error"
	case streamEndUpstreamBroken:
		return "upstream_broken"
	case streamEndTimeout:
		return "timeout"
	case streamEndShutdown:
		return "shutdown"
	case streamEndClientGone:
		return "client_gone"
	default:
		return "unknown"
	}
}

// StreamSink 是流式响应的写出端（实现在 service 层，绑定 HTTP）。
// 接口放在 biz：写出时机由业务决定（首字节前不能报错、done 后才追加 gw_*、心跳只在等待时发），
// 「怎么写到 HTTP」才是 service 的事。
type StreamSink interface {
	// Send 写出一帧并 Flush（docs/04-§4.1：每帧写完 MUST Flush）。
	Send(ev StreamEvent) error
	// Ping 写出一帧心跳。
	Ping() error
	// Started 报告是否已写出过任何一帧。HTTP 状态码只在第一帧之前可改，
	// 故错误处理路径取决于它：没开始 → 正常返回 4xx/5xx 信封；已开始 → 只能发 error 帧。
	Started() bool
}

// ChatStreamer 是流式对话的编排入口。
type ChatStreamer interface {
	// ChatStream 发起一次流式对话并返回事件流。
	//
	// 返回 error 表示**流没有建立**（建连失败、鉴权失败、上游 4xx）：
	// 此时响应头还没发出，调用方可以正常回 `4xx/5xx`。
	// 流建立之后的错误一律从事件流里出来（`error` 事件 / `Err()`）。
	ChatStream(ctx context.Context, req ChatRequest) (ChatEventStream, error)
}

// ChatEventStream 是一条已经建立的事件流。
//
// 用「通道 + Err()」而非 `Recv() (*StreamEvent, error)`：网关等事件时还要发心跳、
// 判超时、响应取消/退出，`Recv()` 会把调用方阻塞住，而「一直没有 Recv 返回」正是最需要心跳的时候。
//
// 契约（实现 MUST 遵守）：`Events()` 只调一次、流结束时通道关闭；`Err()` 只在通道关闭后读
// （正常结束为 nil）；`Close()` 可重复调用以提前放弃；通道关闭后消费者不再需要 Close。
type ChatEventStream interface {
	Events() <-chan StreamEvent
	Err() error
	Close() error
}
