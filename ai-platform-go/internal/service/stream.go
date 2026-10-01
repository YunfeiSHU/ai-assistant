package service

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ssex"
)

// 本文件是流式提问的传输层（docs/03-§4.3 的 `POST .../messages/stream`，M4）。
//
// 它只做两件事：把 biz 的事件**渲染**成 SSE 帧、把帧**写出去**。
// 「什么时候结束、超时怎么算、落什么状态」全在 biz（docs/04-§5）——
// 传输层碰那些决策会让同一条规则出现在两个地方。

// streamWriteTimeout 是**单帧**写出的上限。
//
// 为什么必须有它：`http.Server` 上没有 `WriteTimeout`（SSE 是长连接，
// 设了会误杀正常的长响应），于是「客户端不读也不断开」时 `Write` 会**永久阻塞** ——
// 一个卡住的客户端就能钉住一条 goroutine + 一个上游连接。
// 逐帧的写超时是这类长连接唯一正确的兜底方式，而且它不会误伤正常的长流
// （每写一帧就重置一次）。
const streamWriteTimeout = 10 * time.Second

// Stream 处理 `POST /conversations/{conversation_id}/messages/stream`（200 + `text/event-stream`）。
//
// 与 `Send` 的三点不同，全部体现在这个函数里：
//
//  1. **不挂幂等中间件**：重放一个已经推了一半的流，客户端会把正文再拼一遍
//     （docs/02-§7 的适用范围不含流式接口）。
//  2. `Idempotency-Key` 头即使带了也**不生效**（路由上没挂那个中间件）。
//  3. 出错时能不能回 `4xx/5xx` 取决于第一帧是否已经写出 —— 见下面 sink.Started()。
func (h *MessageHandler) Stream(c *gin.Context) {
	var in biz.SendMessageInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}

	sink := newSSESink(c)
	err := h.svc.StreamSend(
		c.Request.Context(),
		middleware.UserID(c),
		c.Param("conversation_id"),
		in,
		metaFrom(c),
		sink,
	)
	if err == nil {
		return
	}
	if sink.Started() {
		// 响应已经开始了：HTTP 状态码改不了，失败原因已经在流里以
		// `event: error`（或 `gw_persist_error`）发过了。
		//
		// 这里**刻意不打日志**：biz 已经按中止原因记过
		// （`message.stream_aborted` / `message.persist_assistant_failed`），
		// 再打一条只会让每次「用户关掉页面」都多出一行重复日志，
		// 而重复日志会掩盖真正需要被看见的那条。
		return
	}
	// 一帧都没写出去 ⇒ 响应尚未开始 ⇒ 照常返回统一错误信封
	// （对话不存在 / 归档 / 首字节超时 / 流式未接线都在这里）。
	httpx.Fail(c, err)
}

// sseSink 把 biz 事件渲染成 SSE 帧并写入 HTTP 响应。
type sseSink struct {
	c       *gin.Context
	ctrl    *http.ResponseController
	started bool
}

var _ biz.StreamSink = (*sseSink)(nil)

func newSSESink(c *gin.Context) *sseSink {
	// `http.NewResponseController` 会沿 `Unwrap()` 链找到底层连接，
	// 从而拿到 `SetWriteDeadline` —— gin 的 responseWriter 实现了 `Unwrap()`
	// （v1.9+），所以这里能拿到真正的连接级控制权。
	return &sseSink{c: c, ctrl: http.NewResponseController(c.Writer)}
}

func (s *sseSink) Started() bool { return s.started }

// Send 渲染并写出一帧。
func (s *sseSink) Send(ev biz.StreamEvent) error {
	name, payload, err := renderStreamEvent(ev)
	if err != nil {
		// 渲染失败**不中断整条流**：一帧坏数据不该让用户已经等了几秒的回答全丢。
		// 但必须留痕 —— 静默少一帧的症状是「正文里少了一小段」，事后无法定位。
		s.log().Error("stream.render_failed",
			slog.String("event", ev.EventName()),
			slog.String("error", err.Error()),
		)
		return nil
	}
	frame, err := ssex.Frame(name, payload)
	if err != nil {
		// 事件名或负载里带了裸换行（会让一帧变成两帧，客户端拿到无法解析的 JSON）。
		// 走到这里说明渲染层出了问题，与上一种一样：丢这一帧 + 留痕。
		s.log().Error("stream.frame_encode_failed",
			slog.String("event", name),
			slog.String("error", err.Error()),
		)
		return nil
	}
	return s.writeFrame(frame)
}

// Ping 写出一帧心跳（`event: ping`）。
func (s *sseSink) Ping() error {
	return s.writeFrame(ssex.PingFrame(clockx.Now()))
}

func (s *sseSink) writeFrame(frame []byte) error {
	if !s.started {
		// **第一帧才写响应头**，这是刻意的顺序：
		// 首字节超时要在那之前还能回 504（docs/04-§5 的时延表），
		// 一旦写出 200 就再也改不了状态码了。
		for k, v := range ssex.Headers() {
			s.c.Writer.Header().Set(k, v)
		}
		s.c.Writer.WriteHeader(http.StatusOK)
		s.started = true
	}
	// 每帧重置写超时（不是清掉）：见 streamWriteTimeout 的注释。
	_ = s.ctrl.SetWriteDeadline(time.Now().Add(streamWriteTimeout))
	if _, err := s.c.Writer.Write(frame); err != nil {
		return err
	}
	// 每帧 MUST Flush（docs/04-§4.1）：不 Flush 的话帧会攒在 4KB 的
	// bufio 缓冲里，「流式」就退化成了「一次返回完整答案」——
	// 唯一的区别是 TCP 分段不同，而客户端看到的是卡顿而不是逐字输出。
	s.c.Writer.Flush()
	return nil
}

func (s *sseSink) log() *slog.Logger {
	return logx.From(s.c.Request.Context(), nil)
}

// ---- 事件渲染 ----

// streamMetaDTO 是 `meta` 帧的 data（docs/04-§2.3 的字段列，与 AI 侧逐字段一致）。
//
// `conversation_id` / `message_id` 都是**网关自己的**值：
//
//   - 会话 ID 的权威在网关（REQ-ORCH-006 / AC-ORCH-07）—— 客户端会拿它发下一轮，
//     转发上游回显的值会让用户看到「AI 忽然失忆」；
//   - 消息 ID 同样是网关的：客户端拿它去 `GET /messages/{id}`，
//     转发 AI 侧的 ID 会 404（那条消息在网关的台账里不存在）。
type streamMetaDTO struct {
	ConversationID string `json:"conversation_id"`
	MessageID      string `json:"message_id"`
	Model          string `json:"model"`
	CreatedAt      string `json:"created_at"`
	Degraded       bool   `json:"degraded"`
}

// streamReferenceDTO 是 `reference` 帧的 data。
type streamReferenceDTO struct {
	References json.RawMessage `json:"references"`
}

// streamTokenDTO 是 `token` 帧的 data。
type streamTokenDTO struct {
	Delta string `json:"delta"`
}

// streamToolCallDTO 是 `tool_call` 帧的 data。
//
// `arguments` 是**对象**（不是字符串）：这是客户端侧的契约
// （AI 的 SSE 就是这么发的），gRPC 那边因为 proto3 没有「任意 JSON」
// 而传字符串，传输层负责把两者归一。
type streamToolCallDTO struct {
	CallID    string          `json:"call_id"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

// streamToolResultDTO 是 `tool_result` 帧的 data。
type streamToolResultDTO struct {
	CallID    string `json:"call_id"`
	Name      string `json:"name"`
	Status    string `json:"status"`
	Summary   string `json:"summary"`
	ElapsedMS int    `json:"elapsed_ms"`
}

// streamUsageDTO 是 `usage` 帧的 data。
//
// 复用 `biz.MessageUsage` 而不是再写一个同字段的结构体：它的 json tag 就是
// 契约字段名（`prompt_tokens` 等），多一份只会多一处要同步的地方。
type streamUsageDTO = biz.MessageUsage

// streamErrorDTO 是 `error` 帧的 data。
type streamErrorDTO struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
}

// streamDoneDTO 是 `done` 帧的 data。
//
// `partial` 不在 docs/04-§2.3 的字段列里，但 AI 侧的 `StreamDone` 有它
// （其 pydantic 模型带默认值，序列化时总会输出），而它是有用的：
// 「AI 自己知道这次答得不完整」与「网关猜的」是两件事。
type streamDoneDTO struct {
	FinishReason string `json:"finish_reason"`
	ElapsedMS    int    `json:"elapsed_ms"`
	Partial      bool   `json:"partial"`
}

// streamPersistErrorDTO 是 `gw_persist_error` 帧的 data（网关追加的事件）。
type streamPersistErrorDTO struct {
	Reason string `json:"reason"`
}

// renderStreamEvent 把一个 biz 事件渲染成「事件名 + data 负载」。
//
// 这是**唯一**知道「客户端看到什么」的地方：biz 的 `StreamEvent` 类型
// 只要新增一个实现，这里的 switch 就会编译不过（`StreamEvent` 的标记方法
// 未导出，包外无法新增实现）—— 于是「忘了渲染某个事件」不可能发生。
func renderStreamEvent(ev biz.StreamEvent) (string, []byte, error) {
	switch e := ev.(type) {
	case biz.StreamMetaEvent:
		return streamFrame(e.EventName(), streamMetaDTO{
			ConversationID: e.ConversationID,
			MessageID:      e.MessageID,
			Model:          e.Model,
			CreatedAt:      e.CreatedAt,
			Degraded:       e.Degraded,
		})

	case biz.StreamReferenceEvent:
		// 空引用集合写成 `[]` 而不是 `null`（契约里它是数组，客户端会直接遍历）。
		return streamFrame(e.EventName(), streamReferenceDTO{
			References: rawOrEmptyArray(e.References),
		})

	case biz.StreamTokenEvent:
		return streamFrame(e.EventName(), streamTokenDTO{Delta: e.Delta})

	case biz.StreamToolCallEvent:
		return streamFrame(e.EventName(), streamToolCallDTO{
			CallID:    e.CallID,
			Name:      e.Name,
			Arguments: rawOrEmptyObject(e.Arguments),
		})

	case biz.StreamToolResultEvent:
		return streamFrame(e.EventName(), streamToolResultDTO{
			CallID:    e.CallID,
			Name:      e.Name,
			Status:    e.Status,
			Summary:   e.Summary,
			ElapsedMS: e.ElapsedMS,
		})

	case biz.StreamUsageEvent:
		return streamFrame(e.EventName(), e.Usage)

	case biz.StreamErrorEvent:
		return streamFrame(e.EventName(), streamErrorDTO{
			Code:      e.Code,
			Message:   e.Message,
			Retryable: e.Retryable,
		})

	case biz.StreamDoneEvent:
		return streamFrame(e.EventName(), streamDoneDTO{
			FinishReason: e.FinishReason,
			ElapsedMS:    e.ElapsedMS,
			Partial:      e.Partial,
		})

	case biz.StreamPersistErrorEvent:
		return streamFrame(e.EventName(), streamPersistErrorDTO{Reason: e.Reason})

	case biz.StreamUnknownEvent:
		// 未知事件**原样透传**（docs/04-§4.1），所以负载是原始字节，
		// 网关不重新序列化（那会把数字精度、字段顺序与嵌套结构都改一遍）。
		return streamRawFrame(e.EventName(), e.Data)

	default:
		// 包外无法新增实现，所以这里只可能是「biz 新增了事件类型但忘了渲染」。
		// 留一条 ERROR 让它在验收时立刻暴露，而不是静默少一帧。
		return "", nil, fmt.Errorf("未渲染的流式事件类型: %T", ev)
	}
}

// streamFrame 序列化一个已知结构的事件负载。
func streamFrame(name string, v any) (string, []byte, error) {
	payload, err := json.Marshal(v)
	if err != nil {
		return name, nil, err
	}
	return name, payload, nil
}

// streamRawFrame 直接使用原始负载（未知事件透传）。
func streamRawFrame(name string, raw []byte) (string, []byte, error) {
	payload, err := ensureValidJSON(raw)
	if err != nil {
		return name, nil, err
	}
	return name, payload, nil
}

// ensureValidJSON 保证下发的是合法 JSON。
//
// gRPC 的 `unknown.data_json` 是 `bytes`，上游可以塞任意字节（甚至不是 JSON）。
// 直接把它当负载会产出一帧客户端无法解析的 `data`，而客户端的表现是
// **整条流解析中断**（不是丢一帧）。所以不合法的就包成 JSON 字符串：
// 客户端至少能拿到原始文本，也不会因为一个未知事件把整轮回答的解析搞崩。
func ensureValidJSON(raw []byte) ([]byte, error) {
	if len(raw) == 0 {
		return []byte("null"), nil
	}
	if json.Valid(raw) {
		return raw, nil
	}
	return json.Marshal(string(raw))
}

// rawOrEmptyObject 把空的 `arguments` 写成 `{}`。
//
// 写 `null` 会让客户端在渲染「工具参数」时炸掉（契约里它是对象），
// 而这个字段本来就可能是空的（上游没给参数）。
func rawOrEmptyObject(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 || string(raw) == "null" {
		return json.RawMessage("{}")
	}
	return raw
}
