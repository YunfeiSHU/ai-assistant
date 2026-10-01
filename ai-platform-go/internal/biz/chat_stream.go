package biz

import (
	"context"
	"encoding/json"
	"errors"
)

// 本文件是**流式编排的接缝**（docs/04-§2.2 的 `ChatStream`，M4）。
//
// 与 `chat.go` 的关系：`ChatOrchestrator` 是「一问一答」，本文件是「一问、边答边收」。
// 两者共用 `ChatRequest`（请求侧没有区别）与 `MessageUsage`，但**事件流过不了
// 同一个接口** —— 让 `Chat` 也返回一个流，非流式路径就得自己把流收完再拼，
// 反而多一层没人需要的抽象。
//
// 事件名（`meta` / `token` / …）在这里作为契约常量定义，而不是散在传输层与
// service 层：docs/04-§2.3 的映射表是**两侧共同遵守的契约**，
// 网关侧只有一处引用才可能有「一处改了另一处忘」之外的第二种结果。

// 流式事件名（docs/04-§2.3 的事件映射表）。
//
// 与 AI 侧 `app/core/sse.py` 的 `EVENT_*` 常量逐字对应；改这里必须同时改那里
// （两侧各有一条常量一致性断言：Go 见本包的测试，Python 见其 `test_sse_frames.py`）。
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

	// 下面这个是「网关追加」的事件（docs/04-§4.1 明确只允许两类，
	// 另一类 `gw_degraded` 属于 M5 的降级路径：M4 没有生产者，
	// 与其留一个永远不会出现的常量，不如等它真的存在时再加）。
	//
	// 它 SHOULD 出现在 `done` 之后：插在 `token` 之间会打断客户端的正文拼接
	// （客户端看到未知事件时按约定忽略，而「忽略」意味着这一帧被丢掉）。
	StreamEventGwPersistError = "gw_persist_error"
)

// 工具调用状态（docs/03-§4.1 的 `tool_calls[].status`）。
//
// 取值必须与 AI 侧 `app/schemas/chat.py` 的类型别名
// （`Literal["ok", "error", "timeout", "forbidden"]`）保持一致：
// 它是**枚举**而不是自由文本，客户端按它决定图标与颜色。
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

// errStreamNoDone 表示上游**干净地**关掉了事件流却没发 `done`。
//
// 单独立一个 sentinel 而不是复用 `io.EOF`：两者对外都落 `partial`，
// 但排障时必须能分清「上游正常收完」与「上游少发了一帧」——
// 前者无事发生，后者是 AI 侧的 bug。
var errStreamNoDone = errors.New("上游事件流结束但未发送 done")

// StreamEvent 是一条待下发给客户端的事件。
//
// 用「接口 + 未导出的标记方法」而不是「一个带 N 个指针字段的大结构体」：
// 后者的零值组合是无穷的（`Name=""` 但 `Token` 非空是合法 Go 值却不是合法事件），
// 每个消费者都得先判断「到底哪个字段有值」。接口版把这件事交给类型系统。
//
// 标记方法未导出 ⇒ 包外无法再新增实现 ⇒ `service` 的渲染 switch 可以**确定**
// 自己覆盖了全部情况（新增事件类型会编译不过，而不是静默走到 default 丢帧）。
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
	// CreatedAt 是 RFC3339 毫秒 UTC 字符串（原样透传，不解析）。
	// 解析它只会多一个「格式不合法就失败」的分支，而客户端也不需要网关转格式。
	CreatedAt string
	Degraded  bool
	// DegradedReasons 是降级原因列表，**只用于落库**（docs/04-§9 要求
	// 「AI 返回 degraded=true 时在 assistant 消息的 `degraded_reasons` 中保留」）。
	//
	// 它**不会**出现在下发给客户端的 `meta` 帧里 —— 那一帧的字段是契约固定死的
	// 五个（docs/04-§2.3）；客户端要展示「本次未使用知识库」时读
	// `GET /messages/{id}`，那里有完整的 `degraded_reasons`。
	//
	// ※ 这是网关在 proto 里**补出来**的字段：docs/04-§2.3 的映射表只列了
	// `degraded` 布尔，只满足不了 §9 落库那一条。走 gRPC 时 AI 侧会把原因填进来
	// （它的 `prepared.degraded_reasons` 本来就在手边）；走 HTTP/SSE 兜底通道时
	// 拿不到（其 `meta` 帧只有布尔），此时为空。
	DegradedReasons []string
}

func (StreamMetaEvent) EventName() string { return StreamEventMeta }
func (StreamMetaEvent) streamEvent()      {}

// StreamReferenceEvent 是引用帧（`event: reference`）。
//
// `References` 是**本轮引用集合的完整 JSON 数组**（不是增量）。
//
// AI 侧的行为是「每次重发全部引用」（其 `agent.py` 有明确注释：只发增量会让
// 客户端的 `[n]` 编号错位），所以累积规则是**后者覆盖前者**而不是追加 ——
// 追加会让重复的引用在库里出现两次，而正文里的 `[3]` 只指向其中一个。
type StreamReferenceEvent struct {
	References json.RawMessage
}

func (StreamReferenceEvent) EventName() string { return StreamEventReference }
func (StreamReferenceEvent) streamEvent()      {}

// StreamTokenEvent 是正文增量帧（`event: token`）。
type StreamTokenEvent struct {
	// Delta 是增量文本，**可能为空串**（上游偶发空 delta）。
	// 空串也必须原样下发：丢掉它会让「客户端收到的帧数」与上游的不一致，
	// 而 docs/04-§4.1 要求顺序与数量保真。
	Delta string
}

func (StreamTokenEvent) EventName() string { return StreamEventToken }
func (StreamTokenEvent) streamEvent()      {}

// StreamToolCallEvent 是工具调用开始帧（`event: tool_call`）。
type StreamToolCallEvent struct {
	CallID string
	Name   string
	// Arguments 是**对象** JSON（不是字符串）。
	//
	// 把它从 `arguments_json` 字符串转成对象的动作发生在传输层（data/ai）：
	// 那里本来就同时看得见两种形状（非流式路径的 `marshalToolCalls` 同理）。
	// 传字符串进来的话，落库（契约要求 `arguments` 是对象）与下发
	// （契约要求是对象）就都要各自解析一次 JSON，且两边都可能解析失败。
	Arguments json.RawMessage
}

func (StreamToolCallEvent) EventName() string { return StreamEventToolCall }
func (StreamToolCallEvent) streamEvent()      {}

// StreamToolResultEvent 是工具调用结果帧（`event: tool_result`）。
//
// 与 `StreamToolCallEvent` 靠 `CallID` 配对（落库时拼成一条完整轨迹）。
// 只有开始没有结果的调用由累积器兜底成 `status=error`：上游被取消时
// 就会这样，而库里留一条「没有结果的调用」会让前端渲染出一个转不完的圈。
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

// StreamErrorEvent 是错误帧（`event: error`）。
//
// 流已经开始推送之后唯一的报错方式（HTTP 状态码在那时已经发出去了）。
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

// StreamUnknownEvent 是未识别的事件（原样透传）。
//
// 存在的理由是 docs/04-§4.1 的「未知 `event:` 类型 MUST 透传，MUST NOT 丢弃」：
// AI 侧一旦新增一类事件，走 gRPC 时它会落进 proto 的 `unknown` 分支，
// 网关不认识但必须照发。丢掉它在这里**不会报任何错**，只是客户端少了点信息 ——
// 正是那种「上线半年后才被发现」的静默降级。
type StreamUnknownEvent struct {
	Name string
	// Data 是原始 `data` 的 JSON 字节，原样转发（网关不解析也不重新序列化）。
	Data json.RawMessage
}

func (e StreamUnknownEvent) EventName() string { return e.Name }
func (StreamUnknownEvent) streamEvent()        {}

// StreamPersistErrorEvent 是落库失败时网关追加的事件（`event: gw_persist_error`）。
//
// 存在的意义：流式路径的响应已经发出去了，改不了状态码；如果落库又失败了，
// 客户端会以为「回答已保存」。这一帧是唯一能纠正它的地方。
type StreamPersistErrorEvent struct {
	// Reason 是**给客户端排障看的**短语，不是完整错误：
	// 完整错误链里可能有 SQL 片段或表名，它属于日志（biz 已经记过一条 ERROR）。
	Reason string
}

func (StreamPersistErrorEvent) EventName() string { return StreamEventGwPersistError }
func (StreamPersistErrorEvent) streamEvent()      {}

// ---- 流的终止原因 ----

// streamEnd 说明「谁结束了这次流」。
//
// 它同时决定三件事：落库状态（`completed`/`partial`/`failed`）、
// 要不要补发 `error` 帧、以及日志级别。用一个枚举而不是「事后看 error 是不是
// context.Canceled」：后者要靠字符串或 errors.Is 反推，而
// 「上游静默断开」与「客户端断连」在 Go 里都可能表现为 `context.Canceled`。
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

// StreamSink 是流式响应的写出端（实现在 service 层，绑定 HTTP 响应）。
//
// 接口放在 biz 而不是 service，是因为**写出时机由业务决定**
// （首字节前不能用 SSE 报错、`done` 之后才追加 `gw_*`、心跳只在等待时发），
// 而「怎么写到 HTTP」才是 service 的事。
type StreamSink interface {
	// Send 写出一帧并 Flush（docs/04-§4.1：每帧写完 MUST Flush）。
	Send(ev StreamEvent) error
	// Ping 写出一帧心跳。
	Ping() error
	// Started 报告是否已经写出过任何一帧。
	//
	// HTTP 状态码只在第一帧之前可以改，所以**错误处理的路径取决于它**：
	// 没开始 → 正常返回 `4xx/5xx` JSON 信封；已开始 → 只能发 `error` 帧。
	// 让 sink 记住这件事，比让调用方猜「我刚写了没有」可靠得多。
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
// 为什么是「通道 + Err()」而不是「`Recv() (*StreamEvent, error)`」：
// 网关在等事件的同时还要做三件与事件无关的事 —— 发心跳、判首字节/空闲超时、
// 响应取消与进程退出。`Recv()` 会把调用方**阻塞**在那一个调用上，
// 于是心跳只能在两次 Recv 之间发（而「一直没有 Recv 返回」正是最需要心跳的时候）。
// 通道可以让调用方 select。
//
// 契约（实现 MUST 遵守）：
//
//   - `Events()` 只被调用一次，返回的通道在流结束时**关闭**；
//   - `Err()` 只能在通道关闭后读，返回终止原因（正常结束为 nil）；
//   - `Close()` 可重复调用，用于提前放弃（取消上游）；
//   - 通道关闭后消费者**不再需要** Close（实现必须自己回收连接）。
type ChatEventStream interface {
	Events() <-chan StreamEvent
	Err() error
	Close() error
}
