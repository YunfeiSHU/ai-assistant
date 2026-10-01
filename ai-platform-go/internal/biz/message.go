package biz

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ids"
)

// ---- 契约常量（docs/03-§4.1）----

// 消息角色。只存两类：工具轨迹是 assistant 消息的附加结构，不单独成行。
const (
	// MessageRoleUser 是用户发来的消息。
	MessageRoleUser = "user"
	// MessageRoleAssistant 是模型回答（其 tool_calls 字段承载工具轨迹）。
	MessageRoleAssistant = "assistant"
)

// 消息状态。
const (
	// MessageStatusCompleted 表示回答完整（收到 done 帧后才写这个状态）。
	MessageStatusCompleted = "completed"
	// MessageStatusPartial 表示流式中断（客户端断连 / 总超时）。
	MessageStatusPartial = "partial"
	// MessageStatusFailed 表示生成失败。
	MessageStatusFailed = "failed"
)

// finish_reason 取值（透传 AI 侧）。
const (
	// FinishReasonStop 表示模型自然结束。
	FinishReasonStop = "stop"
	// FinishReasonLength 表示因达到输出长度上限而截断。
	FinishReasonLength = "length"
	// FinishReasonMaxSteps 表示因达到工具调用步数上限而终止。
	FinishReasonMaxSteps = "max_steps"
	// FinishReasonCanceled 表示被客户端取消或连接断开而终止。
	FinishReasonCanceled = "canceled"
)

// 消息列表的排序方向。
const (
	// OrderAsc 按 seq 升序（对话的自然阅读顺序）。
	OrderAsc = "asc"
	// OrderDesc 按 seq 降序（取最近 N 条时用，避免先扫全表再截断）。
	OrderDesc = "desc"
)

// MessageContentMaxRunes 是提问正文上限（docs/03-§4.3：1..8000 字符）。
const MessageContentMaxRunes = 8000

// Message 是消息领域对象（`message` 表，docs/03-§4.1）。
//
// `References` / `ToolCalls` 刻意保持 `json.RawMessage`：它们的结构由
// ai-platform 定义，网关只做原样存与原样取（ai-platform 改字段不该让网关返工）。
// `Usage` 相反 —— 它是强类型的，因为配额要靠 `total_tokens` 累加（接缝 J7），
// 字段名拼错会变成「配额永远不涨」且不报任何错。
type Message struct {
	ID             string
	ConversationID string
	UserID         string
	// Seq 是会话内序号，从 1 开始，**排序唯一依据**。
	//
	// MUST NOT 用 created_at 排序：Windows 上 time.Now() 的粒度约 15.6ms，
	// 同一毫秒内的两条消息顺序会退化成随机。
	Seq             int
	Role            string
	Content         string
	Status          string
	FinishReason    *string
	References      json.RawMessage
	ToolCalls       json.RawMessage
	Usage           *MessageUsage
	Model           *string
	Degraded        bool
	DegradedReasons []string
	ElapsedMS       *int
	TraceID         *string
	CreatedAt       time.Time
}

// MessageUsage 是一次调用的 token 用量（契约字段名，直接复用为存储结构）。
type MessageUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// IsZero 报告用量是否为空。
//
// 上游可能返回 usage 帧但三个字段都缺省成 0；把它当真实用量累加等于
// 白扣一次配额，所以累加前必须先问这一句。
func (u *MessageUsage) IsZero() bool {
	return u == nil || (u.PromptTokens == 0 && u.CompletionTokens == 0 && u.TotalTokens == 0)
}

// ---- 输入结构 ----

// SendMessageInput 是 `POST /conversations/{id}/messages` 请求体（docs/03-§4.3）。
//
// 布尔字段用指针：`use_rag` 缺省是 `true`，用 bool 零值会把
// 「没传」和「显式传 false」折叠成同一个值，于是默认值永远生效不了。
type SendMessageInput struct {
	Content string `json:"content"`

	UseRAG    *bool    `json:"use_rag"`
	KBIDs     []string `json:"kb_ids"`
	UseMemory *bool    `json:"use_memory"`
	UseTools  *bool    `json:"use_tools"`

	Model          *string  `json:"model"`
	Temperature    *float64 `json:"temperature"`
	TopK           *int     `json:"top_k"`
	RerankTopN     *int     `json:"rerank_top_n"`
	ScoreThreshold *float64 `json:"score_threshold"`

	// Attachments 是附件（P2：需先上传为文档）。M2 只做「非空即拒绝」，
	// 而不是静默忽略 —— 忽略会让用户以为模型看过那个文件。
	Attachments []json.RawMessage `json:"attachments"`
}

// AppendAssistantInput 是写入 assistant 消息的输入。
//
// 它同时服务三个调用方：M3 的非流式成功落库、M4 的流式中断落 `partial`、
// M4 的 AI `error` 帧落 `failed`（docs/03-§5 的落库时机表）。
type AppendAssistantInput struct {
	// ID 是预先生成的消息 ID；为空时由服务生成。
	//
	// 流式路径必须**先**有它：`meta` 帧要把它发给客户端，而那一帧发生在落库
	// 之前（落库要等流结束）。不预生成的话，客户端拿到的是 AI 侧的 message_id，
	// 拿它去 `GET /messages/{id}` 必然 404 —— 网关台账里没有那条消息。
	ID              string
	ConversationID  string
	Content         string
	Status          string
	FinishReason    *string
	References      json.RawMessage
	ToolCalls       json.RawMessage
	Usage           *MessageUsage
	Model           *string
	Degraded        bool
	DegradedReasons []string
	ElapsedMS       *int
	TraceID         *string
}

// ListMessagesInput 是 `GET /conversations/{id}/messages` 的查询参数。
type ListMessagesInput struct {
	PaginationInput
	Order string
}

// MessageList 是消息列表结果。
type MessageList struct {
	Items      []Message
	NextCursor string
	HasMore    bool
}

// SendResult 是一次提问的落库结果。
//
// M2/M3 的非流式路径两者都有；流式路径由 M4 自己组装（它不需要在返回时
// 拿到 assistant 消息，落库发生在响应之后）。
type SendResult struct {
	User      *Message
	Assistant *Message
}

// ---- 仓储接口（规范 §六：接口在 biz，实现在 data）----

// MessageRepo 是消息表的仓储接口。
type MessageRepo interface {
	// Append 在**一个事务内**原子分配 seq 并写入消息，分配到的序号回填到 `m.Seq`。
	//
	// 之所以把「分配 + 插入」合成一个方法而不是暴露 `AllocSeq` + `Insert`：
	// `LAST_INSERT_ID(expr)` 是**连接级**的，两条语句必须在同一连接上执行，
	// 而连接的所有权属于 data（biz 不认识事务）。拆开就意味着 biz 要传一个
	// 事务句柄 —— 那就等于 biz 认识 gorm 了。
	//
	// 返回 ErrNotFound（会话不存在/越权/已软删）或 ErrConversationArchived。
	Append(ctx context.Context, userID, conversationID string, m *Message) error
	// GetOwned 取属于该用户、且其会话未被软删的消息；否则 ErrNotFound。
	GetOwned(ctx context.Context, userID, id string) (*Message, error)
	// ListByConversation 按 seq 分页列出会话消息。
	ListByConversation(ctx context.Context, userID, conversationID string, in ListMessagesInput) (*MessageList, error)
	// Delete 从台账硬删单条消息（不改动 AI 侧上下文，docs/03-§6）。
	Delete(ctx context.Context, userID, id string) error
}

// ---- 服务 ----

// MessageDeps 是消息服务的依赖。
type MessageDeps struct {
	Conversations ConversationRepo
	Messages      MessageRepo
	// Orchestrator 为 nil 表示编排尚未接线（M2）：写 user 消息之后
	// 直接返回 AI_UNAVAILABLE。这是**有意保留**的状态，见 docs/08-§6。
	Orchestrator ChatOrchestrator
	// Streamer 为 nil 表示流式编排未接线：`StreamSend` 同样在写完 user 消息后
	// 返回 AI_UNAVAILABLE。与非流式路径同形，便于 M4 之前也能把路由挂上。
	Streamer ChatStreamer
	Clock    nowFunc
	Log      *slog.Logger
	// AutoTitleMaxChars 来自 `AUTO_TITLE_MAX_CHARS`；<=0 时用默认值。
	AutoTitleMaxChars int
	// HistoryFallbackTurns 是 `use_memory=false` 时补给的轮数，来自
	// `AI_HISTORY_FALLBACK_TURNS`（docs/04-§6，默认 10）；<=0 时用默认值。
	HistoryFallbackTurns int

	// ---- 流式专用（M4，docs/04-§5 的时延表）----
	//
	// 四个时长来自 `AI_FIRST_BYTE_TIMEOUT_SECONDS` / `AI_IDLE_TIMEOUT_SECONDS` /
	// `AI_TOTAL_TIMEOUT_SECONDS` / `STREAM_ACCUMULATE_MAX_CHARS`，
	// 由装配层传入；<=0 时回落成上面的 `Stream*Default` 常量。
	//
	// 为什么不在这里直接读 `conf`：biz 不认识配置结构（规范 §四），
	// 而且「时长」是策略，具体数值属于部署面。
	StreamFirstByteTimeout   time.Duration
	StreamIdleTimeout        time.Duration
	StreamTotalTimeout       time.Duration
	StreamAccumulateMaxChars int
	StreamAccumulateMaxItems int
	// Shutdown 在进程开始优雅退出时被关闭（docs/06-§3 第 ③ 步）。
	//
	// 它的作用是让在途的流**自己**发一条 `error(SERVICE_SHUTTING_DOWN)` 并把
	// 已收到的正文落成 `partial`，而不是被 `http.Server.Shutdown` 到点后一刀切掉
	// ——被切掉的流什么都不会落库。
	Shutdown <-chan struct{}
	// Quota 是配额与并发限额（M5，docs/02-§5.2）。
	//
	// 为 nil 表示**未接线**（M5 之前的形态）：此时跳过预扣与并发控制，
	// 但保留完整的消息台账功能。测试里大量用到这个 nil 形态。
	Quota *QuotaService

	// Metrics 是 Prometheus 埋点口（M6）。
	//
	// 为 nil 时构造期会换成 `NoopMetrics`（`OrNoop`）：接口为 nil 时
	// 调用方法会 panic，而埋点绝不该是「业务能不能跑」的条件。
	Metrics Metrics
}

// HistoryFallbackTurnsDefault 是 `use_memory=false` 时补给的默认轮数。
const HistoryFallbackTurnsDefault = 10

// MessageService 实现消息台账（REQ-CONV-004..006）。
type MessageService struct{ d MessageDeps }

// NewMessageService 构造消息服务。
func NewMessageService(d MessageDeps) *MessageService {
	if d.Clock == nil {
		d.Clock = clockx.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	if d.AutoTitleMaxChars <= 0 {
		d.AutoTitleMaxChars = AutoTitleMaxCharsDefault
	}
	if d.HistoryFallbackTurns <= 0 {
		d.HistoryFallbackTurns = HistoryFallbackTurnsDefault
	}
	// 流式的四个时长/限额同样兜底。零值在这里**不是**「禁用超时」而是
	// 「立即超时」（`time.NewTimer(0)` 立刻就绪），所以必须兜底。
	if d.StreamFirstByteTimeout <= 0 {
		d.StreamFirstByteTimeout = StreamFirstByteTimeoutDefault
	}
	if d.StreamIdleTimeout <= 0 {
		d.StreamIdleTimeout = StreamIdleTimeoutDefault
	}
	if d.StreamTotalTimeout <= 0 {
		d.StreamTotalTimeout = StreamTotalTimeoutDefault
	}
	if d.StreamAccumulateMaxChars <= 0 {
		d.StreamAccumulateMaxChars = StreamAccumulateMaxCharsDefault
	}
	if d.StreamAccumulateMaxItems <= 0 {
		d.StreamAccumulateMaxItems = StreamAccumulateMaxItemsDefault
	}
	// 埋点口兜底：接口为 nil 时调用方法会 panic，而埋点只是观测面，
	// 绝不该成为「落库失败降级路径能不能跑」的条件。
	d.Metrics = OrNoop(d.Metrics)
	return &MessageService{d: d}
}

func (s *MessageService) now() time.Time { return s.d.Clock() }

// Send 发送提问（非流式）。
//
// 落库顺序按 docs/03-§5 的时机表，**不可调换**：
//
//	校验 →（M5：配额预扣）→ 写 user 消息（seq = n）→ 调 AI → 写 assistant 消息（seq = n+1）
//
// 「先落 user 消息」的意义在于：AI 调用失败时用户的提问仍在台账里，
// 重发时由 `Idempotency-Key` 去重（docs/02-§7），而不会因为失败就凭空消失。
func (s *MessageService) Send(ctx context.Context, userID, conversationID string, in SendMessageInput, meta RequestMeta) (*SendResult, error) {
	content := strings.TrimSpace(in.Content)
	if fields := validateSendInput(in, content); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	conv, err := s.d.Conversations.GetOwned(ctx, userID, conversationID)
	if err != nil {
		return nil, conversationLookupError(err)
	}
	if !conv.IsActive() {
		// 归档会话可读不可写（REQ-CONV-003）。这里用 409 而不是 404：
		// 会话对用户是可见的，说「不存在」会让他以为会话被删了。
		return nil, errs.New(errs.CodeConversationArchived)
	}

	now := s.now()

	// 配额预扣（docs/02-§5.2 第 ①→③ 步）：必须在写 user 消息与调用 AI **之前**。
	//
	// 放在 `GetOwned` 之后是因为「归档会话」不该耗配额：那是一次注定 409 的请求。
	// 反过来放在写 user 消息之后就没有意义了 —— 超额的提问已经进了台账。
	reservation, err := s.beginChat(ctx, userID, &UsageRef{ConversationID: conv.ID})
	if err != nil {
		return nil, err
	}

	userMsg := &Message{
		ID:             ids.NewMessage(),
		ConversationID: conv.ID,
		UserID:         userID,
		Role:           MessageRoleUser,
		Content:        content,
		Status:         MessageStatusCompleted,
		CreatedAt:      now,
	}
	traceID := strings.TrimSpace(meta.TraceID)
	if traceID != "" {
		userMsg.TraceID = &traceID
	}
	if err := s.d.Messages.Append(ctx, userID, conv.ID, userMsg); err != nil {
		// 归档竞态（读到 active 后、分配 seq 前被归档）也在这里被兜住：
		// 仓储层的 AllocSeq 带 `status = 'active'` 条件，0 行时返回 ErrConversationArchived。
		//
		// 这里用的是**不归还**的 Rollback：落库失败的根因在于网关自身
		// （或已归档的会话），不是「上游没提供服务」——归还会让重复点击
		// 「归档后发消息」成为免费的配额探测手段。
		reservation.Rollback(ctx, err)
		return nil, conversationLookupError(err)
	}

	s.applyAutoTitle(ctx, userID, conv, content, now)

	if s.d.Orchestrator == nil {
		// 编排未接线：user 消息已经落库（上面那一步），这里如实报「AI 不可用」。
		// 返回的是 `AI_UNAVAILABLE`，所以预扣应当归还（确实没得到服务）。
		err := errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "orchestrator_not_configured")
		reservation.Rollback(ctx, err)
		return nil, err
	}

	chatReq := s.chatRequest(userID, conv, in, content, meta)
	if !chatReq.UseMemory {
		// 只有 use_memory=false 才需要网关补历史（REQ-ORCH-006）。
		// use_memory=true 时 AI 从自己的 Redis 取上下文，网关再传一份
		// 会让同一段对话被注入两次。
		chatReq.History = s.recentHistory(ctx, userID, conv.ID, userMsg.Seq)
	}

	result, err := s.d.Orchestrator.Chat(ctx, chatReq)
	if err != nil {
		// 只对「本来就没得到服务」的失败归还预扣（docs/02-§5.2 第 ⑥ 步）。
		// 参数错误 / 内容过滤**不**归还：AI 已经处理完了这次请求。
		if ShouldRollback(err) {
			reservation.Rollback(ctx, err)
		}
		return nil, err
	}
	if result == nil {
		err := errs.New(errs.CodeAIUnavailable).WithDetail("reason", "empty_result")
		reservation.Rollback(ctx, err)
		return nil, err
	}

	// 会话 ID 的权威在网关（REQ-ORCH-006 / docs/04-§6）。
	//
	// AI 在 `conversation_id` 为空时会自建会话，因此它回显的 ID 未必是我们传的那个。
	// 一旦不一致，多轮对话就会分裂到两个会话下（用户看到的表现是「上一轮不见了」）。
	// 所以：以网关自己的 ID 为准（下面写库用的仍然是 conv.ID）+ 明确告警。
	//
	// 日志里挂上指标名而不是只打一句话：`gw_*` 的 Prometheus 计数器属于 M6
	// （docs/08-§6），现在先靠日志让 AC-ORCH-07 可验收，且这个名字不会变。
	// 日志里挂上指标名并把计数**真的**打上去：AC-ORCH-07 要求这条能
	// 「在监控里看到」，只靠日志的话运维得先知道去搜什么关键词。
	if got := strings.TrimSpace(result.ConversationID); got != "" && got != conv.ID {
		s.d.Metrics.SessionMismatch()
		s.d.Log.ErrorContext(ctx, "message.session_mismatch",
			slog.String("metric", "gw_session_mismatch_total"),
			slog.String("conversation_id", conv.ID),
			slog.String("upstream_conversation_id", got),
			slog.String("user_id", userID),
			slog.String("trace_id", traceID),
		)
	}

	// AI 回显的 trace_id 必须与本侧一致（接缝 J3，S8 用 trace_id 同时查两侧）：
	// 不一致意味着「日志里的 trace_id 指向的链路」不是真正执行的那条，
	// 排障会沿着错误的链路翻半天。
	if got := strings.TrimSpace(result.TraceID); got != "" && traceID != "" && got != traceID {
		s.d.Metrics.TraceMismatch()
		s.d.Log.WarnContext(ctx, "message.trace_mismatch",
			slog.String("metric", "gw_trace_mismatch_total"),
			slog.String("trace_id", traceID),
			slog.String("upstream_trace_id", got),
		)
	}

	// 用量在这里提交：**先结账，再落库**。
	//
	// 顺序有实际影响：AI 的算力已经花掉了，落库失败（下一个分支）不该让这次
	// 用量消失 —— 否则「落库一直失败」就变成一个免费的配额后门。
	ref := UsageRef{ConversationID: conv.ID, MessageID: userMsg.ID, TraceID: traceID}
	s.commitQuota(ctx, reservation, result.Usage, ref)

	assistant, err := s.AppendAssistant(ctx, userID, AppendAssistantInput{
		ConversationID:  conv.ID,
		Content:         result.Content,
		Status:          MessageStatusCompleted,
		FinishReason:    &result.FinishReason,
		References:      result.References,
		ToolCalls:       result.ToolCalls,
		Usage:           result.Usage,
		Model:           nilIfEmpty(result.Model),
		Degraded:        result.Degraded,
		DegradedReasons: result.DegradedReasons,
		ElapsedMS:       intPtrIfPositive(result.ElapsedMS),
		TraceID:         nilIfEmpty(traceID),
	})
	if err != nil {
		// 非流式路径**不能**像流式那样「落库失败也返回成功」：
		// 响应体里的消息必须是能从 `GET /messages/{id}` 取到的那一条，
		// 否则客户端刷新后会发现「回答不见了」。
		s.d.Log.ErrorContext(ctx, "message.persist_assistant_failed",
			slog.String("conversation_id", conv.ID),
			slog.String("error", err.Error()),
		)
		s.d.Metrics.MessagePersistFailed("assistant_insert")
		// 用量**已经发生**（AI 已经生成并返回），所以这里不归还预扣 ——
		// 归还等于「凡是落库失败的请求都免费」。
		return nil, err
	}
	return &SendResult{User: userMsg, Assistant: assistant}, nil
}

// beginChat 是做配额预扣的统一入口（`Quota` 未接线时返回一个空预约）。
//
// 返回的预约对象在两种情形下都是**非 nil 且安全可用**的：
// 未接线（`Quota == nil`）与未配置计数器都会走到「只记台账」的分支。
// 这样调用点不必写 `if reservation != nil`。
func (s *MessageService) beginChat(ctx context.Context, userID string, ref *UsageRef) (*QuotaReservation, error) {
	if s.d.Quota == nil {
		return &QuotaReservation{}, nil
	}
	r := *ref
	return s.d.Quota.BeginChat(ctx, userID, r)
}

// commitQuota 提交一次成功调用的用量（token + 次数）。
//
// `tokens` 取 `usage.total_tokens`；上游没给 usage（或三个字段全 0）时传 0，
// 那一次就只累计 `chat_requests` —— 凭空补一个 token 数会让账目失真。
func (s *MessageService) commitQuota(ctx context.Context, res *QuotaReservation, usage *MessageUsage, ref UsageRef) {
	if res == nil {
		return
	}
	var tokens int64
	if !usage.IsZero() {
		tokens = int64(usage.TotalTokens)
	}
	if err := res.Commit(ctx, tokens, ref); err != nil {
		s.d.Log.WarnContext(ctx, "quota.commit_failed", slog.Any("error", err))
	}
}

// AppendAssistant 写入一条 assistant 消息（M3/M4 的落库入口）。
func (s *MessageService) AppendAssistant(ctx context.Context, userID string, in AppendAssistantInput) (*Message, error) {
	status := in.Status
	if status == "" {
		status = MessageStatusCompleted
	}
	if !isValidMessageStatus(status) {
		return nil, errs.New(errs.CodeInternalError).
			WithMessage("落库状态非法").
			WithDetail("status", status)
	}

	m := &Message{
		ID:              messageIDOrNew(in.ID),
		ConversationID:  in.ConversationID,
		UserID:          userID,
		Role:            MessageRoleAssistant,
		Content:         in.Content,
		Status:          status,
		FinishReason:    in.FinishReason,
		References:      normalizeJSON(in.References),
		ToolCalls:       normalizeJSON(in.ToolCalls),
		Usage:           in.Usage,
		Model:           in.Model,
		Degraded:        in.Degraded,
		DegradedReasons: in.DegradedReasons,
		ElapsedMS:       in.ElapsedMS,
		TraceID:         in.TraceID,
		CreatedAt:       s.now(),
	}
	if err := s.d.Messages.Append(ctx, userID, in.ConversationID, m); err != nil {
		return nil, conversationLookupError(err)
	}
	return m, nil
}

// Get 取单条消息详情。
func (s *MessageService) Get(ctx context.Context, userID, messageID string) (*Message, error) {
	m, err := s.d.Messages.GetOwned(ctx, userID, messageID)
	if err != nil {
		return nil, messageLookupError(err)
	}
	return m, nil
}

// List 分页读取会话历史（`GET /conversations/{id}/messages`）。
//
// 先确认会话存在再列消息：只靠 JOIN 过滤的话，「会话不存在」与
// 「会话存在但没有消息」都会返回空列表，而前者按契约必须是
// 404 CONVERSATION_NOT_FOUND（软删会话的消息 MUST 一并 404，docs/03-§1）。
func (s *MessageService) List(ctx context.Context, userID, conversationID string, in ListMessagesInput) (*MessageList, error) {
	if fields := validateMessageListInput(in); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}
	if _, err := s.d.Conversations.GetOwned(ctx, userID, conversationID); err != nil {
		return nil, conversationLookupError(err)
	}
	out, err := s.d.Messages.ListByConversation(ctx, userID, conversationID, in)
	if err != nil {
		return nil, wrapDB(err)
	}
	if out == nil {
		out = &MessageList{}
	}
	return out, nil
}

// Delete 从台账删除一条消息（docs/03-§6：不改变 AI 侧上下文）。
func (s *MessageService) Delete(ctx context.Context, userID, messageID string) error {
	if err := s.d.Messages.Delete(ctx, userID, messageID); err != nil {
		return messageLookupError(err)
	}
	return nil
}

// ---- 内部 ----

// chatRequest 把会话默认值与本轮覆盖合并成一次 AI 调用参数。
//
// 合并规则（docs/03-§4.3）：请求体字段优先，缺省时回落到会话级设置。
func (s *MessageService) chatRequest(userID string, conv *Conversation, in SendMessageInput, content string, meta RequestMeta) ChatRequest {
	kbIDs := conv.KBIDs
	if in.KBIDs != nil {
		kbIDs = in.KBIDs
	}
	model := ""
	if conv.Model != nil {
		model = *conv.Model
	}
	if in.Model != nil {
		model = strings.TrimSpace(*in.Model)
	}
	return ChatRequest{
		ConversationID: conv.ID,
		UserID:         userID,
		UserToken:      meta.UserToken,
		Query:          content,
		UseRAG:         boolDefault(in.UseRAG, true),
		KBIDs:          normalizeKBIDs(kbIDs),
		UseMemory:      boolDefault(in.UseMemory, true),
		UseTools:       boolDefault(in.UseTools, false),
		Model:          model,
		Temperature:    in.Temperature,
		TopK:           in.TopK,
		RerankTopN:     in.RerankTopN,
		ScoreThreshold: in.ScoreThreshold,
		TraceID:        meta.TraceID,
	}
}

// recentHistory 取最近 `HistoryFallbackTurns` 轮历史（**仅 use_memory=false 时使用**）。
//
// REQ-ORCH-006 / docs/04-§6。两个不显然的地方：
//
//  1. 调用时机必须**在写 user 消息之后**（落库顺序由 docs/03-§5 规定），
//     于是台账里最新的那条就是本次提问本身 —— 必须按 `beforeSeq` 剔除，
//     否则模型会把同一个问题看两遍。
//  2. 判据用 `seq` 而不是消息 ID 或时间：`seq` 与排序同源（docs/03-§4.1），
//     时间戳在 Windows 上粒度约 15.6ms，同一毫秒内的先后会退化成随机。
func (s *MessageService) recentHistory(ctx context.Context, userID, conversationID string, beforeSeq int) []ChatMessage {
	turns := s.d.HistoryFallbackTurns
	if turns <= 0 {
		return nil
	}
	// 一轮 = 一条提问 + 一条回答；多取 1 条是因为要剔除本次提问。
	limit := turns*2 + 1
	if limit > PageLimitMax {
		limit = PageLimitMax
	}
	page, err := s.d.Messages.ListByConversation(ctx, userID, conversationID, ListMessagesInput{
		PaginationInput: PaginationInput{Limit: limit},
		Order:           OrderDesc,
	})
	if err != nil {
		// 历史只是兜底：拿不到就让 AI 少一轮上下文，不该让用户这次提问直接失败。
		// 但必须留痕 —— 静默少上下文会让「AI 忽然不记得上一轮」变成无法解释的现象。
		s.d.Log.WarnContext(ctx, "message.history_fallback_failed",
			slog.String("conversation_id", conversationID),
			slog.String("error", err.Error()),
		)
		return nil
	}

	// 台账按 DESC 返回，这里倒序遍历即时间正序（history 在契约里是正序）。
	out := make([]ChatMessage, 0, len(page.Items))
	for i := len(page.Items) - 1; i >= 0; i-- {
		m := page.Items[i]
		if m.Seq >= beforeSeq {
			continue
		}
		if m.Role != MessageRoleUser && m.Role != MessageRoleAssistant {
			continue
		}
		// 空内容的消息（如失败的 assistant 占位）喂给模型只会浪费 token，
		// 还可能让模型模仿「回答为空」这种没意义的行为。
		if strings.TrimSpace(m.Content) == "" {
			continue
		}
		out = append(out, ChatMessage{Role: m.Role, Content: m.Content})
	}
	return out
}

// applyAutoTitle 在首条用户消息落库后生成标题（REQ-CONV-002）。
//
// 三个前置条件缺一不可：还是 `auto`、标题为空、且写入时条件仍然成立
// （并发下用户可能刚刚手工改过，条件判断交给 SQL）。
func (s *MessageService) applyAutoTitle(ctx context.Context, userID string, conv *Conversation, content string, at time.Time) {
	if conv.TitleSource != TitleSourceAuto || conv.Title != "" {
		return
	}
	title := AutoTitle(content, s.d.AutoTitleMaxChars, conv.CreatedAt)
	applied, err := s.d.Conversations.SetAutoTitle(ctx, userID, conv.ID, title, at)
	if err != nil {
		// 标题只是展示信息：生成失败不该让「提问」失败。
		s.d.Log.WarnContext(ctx, "message.auto_title_failed",
			slog.String("conversation_id", conv.ID),
			slog.String("error", err.Error()),
		)
		return
	}
	if applied {
		conv.Title = title
	}
}

// messageIDOrNew 返回调用方给的 ID（非空时）或新生成的 ID。
func messageIDOrNew(id string) string {
	if trimmed := strings.TrimSpace(id); trimmed != "" {
		return trimmed
	}
	return ids.NewMessage()
}

// conversationLookupError 把仓储错误映射成对外错误。
func conversationLookupError(err error) error {
	if errors.Is(err, ErrConversationArchived) {
		return errs.New(errs.CodeConversationArchived)
	}
	if errors.Is(err, ErrNotFound) || errors.Is(err, ErrConversationDeleted) {
		return errs.New(errs.CodeConversationNotFound)
	}
	return wrapDB(err)
}

// messageLookupError 把仓储错误映射成对外错误。
func messageLookupError(err error) error {
	if errors.Is(err, ErrNotFound) || errors.Is(err, ErrConversationDeleted) {
		return errs.New(errs.CodeMessageNotFound)
	}
	if errors.Is(err, ErrConversationArchived) {
		return errs.New(errs.CodeConversationArchived)
	}
	return wrapDB(err)
}

// ---- 校验 ----

func validateSendInput(in SendMessageInput, content string) []errs.FieldError {
	var fields []errs.FieldError
	if content == "" {
		fields = append(fields, errs.FieldError{
			Field: "content", Reason: "required", Message: "content 不能为空",
		})
	} else if len([]rune(content)) > MessageContentMaxRunes {
		fields = append(fields, errs.FieldError{
			Field: "content", Reason: "too_long", Message: "content 长度不能超过 8000",
		})
	}
	if in.Temperature != nil && (*in.Temperature < 0 || *in.Temperature > 2) {
		fields = append(fields, errs.FieldError{
			Field: "temperature", Reason: "out_of_range", Message: "temperature 必须在 0..2 之间",
		})
	}
	if in.ScoreThreshold != nil && (*in.ScoreThreshold < 0 || *in.ScoreThreshold > 1) {
		fields = append(fields, errs.FieldError{
			Field: "score_threshold", Reason: "out_of_range", Message: "score_threshold 必须在 0..1 之间",
		})
	}
	if in.TopK != nil && *in.TopK <= 0 {
		fields = append(fields, errs.FieldError{
			Field: "top_k", Reason: "out_of_range", Message: "top_k 必须为正整数",
		})
	}
	if in.RerankTopN != nil && *in.RerankTopN <= 0 {
		fields = append(fields, errs.FieldError{
			Field: "rerank_top_n", Reason: "out_of_range", Message: "rerank_top_n 必须为正整数",
		})
	}
	if in.Model != nil {
		if m := strings.TrimSpace(*in.Model); m == "" {
			fields = append(fields, errs.FieldError{
				Field: "model", Reason: "empty", Message: "model 不能为空串",
			})
		} else if len([]rune(m)) > 64 {
			fields = append(fields, errs.FieldError{
				Field: "model", Reason: "too_long", Message: "model 长度不能超过 64",
			})
		}
	}
	fields = append(fields, validateKBIDs(in.KBIDs)...)
	if len(in.Attachments) > 0 {
		fields = append(fields, errs.FieldError{
			Field: "attachments", Reason: "not_supported",
			Message: "attachments 尚未支持（P2：需先把文件上传为文档）",
		})
	}
	return fields
}

func validateMessageListInput(in ListMessagesInput) []errs.FieldError {
	var fields []errs.FieldError
	if in.Limit < 1 || in.Limit > PageLimitMax {
		fields = append(fields, errs.FieldError{
			Field: "limit", Reason: "out_of_range", Message: "limit 必须在 1..100 之间",
		})
	}
	if in.Order != OrderAsc && in.Order != OrderDesc {
		fields = append(fields, errs.FieldError{
			Field: "order", Reason: "invalid_value", Message: "order 只能是 asc 或 desc",
		})
	}
	return fields
}

func isValidMessageStatus(s string) bool {
	switch s {
	case MessageStatusCompleted, MessageStatusPartial, MessageStatusFailed:
		return true
	}
	return false
}

// ---- 小工具 ----

func boolDefault(v *bool, def bool) bool {
	if v == nil {
		return def
	}
	return *v
}

func nilIfEmpty(s string) *string {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return &s
}

func intPtrIfPositive(n int) *int {
	if n <= 0 {
		return nil
	}
	return &n
}

// normalizeJSON 把空的 JSON 列归一化成 nil（写 NULL 而不是 `null` 字面量）。
//
// `json.RawMessage("null")` 会被当成合法 JSON 存进列里，读出来是 `null`，
// 而契约里这两个字段是数组；存 NULL 读出来才是「没有值」，由 DTO 输出 `[]`。
func normalizeJSON(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return nil
	}
	if strings.TrimSpace(string(raw)) == "null" {
		return nil
	}
	return raw
}
