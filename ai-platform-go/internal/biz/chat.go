package biz

import (
	"context"
	"encoding/json"
)

// 本文件是网关 → ai-platform 的编排接缝（docs/04-§2.2 的 `Chat` 调用，接缝 J4/J5）。
//
// 抽成接口是为了让 M2 先跑通「分配 seq → 写 user 消息 → 自动标题 → 调 AI → 写 assistant
// 消息」这条顺序敏感链路的前半段（AC-CONV-03/05/07），M3 只换实现类。
// 传输形态（HTTP/SSE 或 gRPC）对本接口无影响 —— 请求/结果都是普通结构体。

// ChatOrchestrator 是非流式对话的编排入口。
// 只定义 M2 用到的方法；流式 `ChatStream`（M4）等到要用时再加，避免假实现写空方法。
type ChatOrchestrator interface {
	// Chat 调用 ai-platform 得到一次完整回答。
	//
	// 返回的错误 MUST 已经是 `*errs.AppError`（AI 侧错误码原样透传，接缝 J2）。
	Chat(ctx context.Context, req ChatRequest) (*ChatResult, error)
}

// ChatRequest 是一次非流式对话的输入（对应 ai-platform 的 `ChatRequest`，
// 字段与 docs/04-§2.3 的 proto 一一对应，另多 `UserID` / `UserToken`）。
//
//   - `UserToken` 是身份来源：网关把调用方 Bearer token 原样交给 AI 自己校验 JWT
//     （接缝 J1）。MUST NOT 记录到日志。
//   - `UserID` 仅供网关自己的日志/审计/告警定位，**不进 proto**：可被调用方伪造的
//     user_id 比没有它更危险（docs/04-§3.2）。
type ChatRequest struct {
	// ConversationID 由网关生成（REQ-CONV-001）。MUST 总是携带（接缝 J4），
	// 否则 AI 侧无法把多轮提问归到同一个上下文。
	ConversationID string
	// UserID 是提问者（JWT `sub`），仅供网关侧观测，不随请求下发。
	UserID string
	// UserToken 是提问者的 Bearer token（接缝 J1）。
	UserToken string
	// Query 是用户提问原文（已去首尾空白）。
	Query string

	UseRAG    bool
	KBIDs     []string
	UseMemory bool
	UseTools  bool

	Model          string
	Temperature    *float64
	TopK           *int
	RerankTopN     *int
	ScoreThreshold *float64

	// TraceID 用于跨服务链路串联（接缝 J3）：AI 侧的日志会带上同一个值。
	TraceID string

	// History 是历史消息，**仅在 `UseMemory == false` 时**才允许非空
	// （REQ-ORCH-006 / docs/04-§6）。
	//
	// `UseMemory == true` 时 AI 从自己的 Redis 取上下文，再传一份 history
	// 会让同一段对话被注入两次（模型会「记得」两遍，且 token 白烧）。
	// 这条规则由 `MessageService` 在发请求前落实，传输层不再判断。
	History []ChatMessage
}

// ChatMessage 是发给 AI 的一轮历史消息（不复用带台账字段的 `Message`）。
// json 标签是 HTTP 通道（`AI_GRPC_ENABLED=false` 走 `POST /chat`）必需的：
// 没有标签会输出 `Role`/`Content`，而 AI 侧是严格 Pydantic 模型 —— 字段名错即 422。
type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ChatResult 是 ai-platform 的一次完整回答。
// `References`/`ToolCalls` 用 raw JSON：schema 由 AI 侧定义，网关只「原样存、原样取」，
// 一旦解析，AI 改字段名就会变成网关故障（docs/03-§4.1）。
type ChatResult struct {
	Content      string
	FinishReason string

	// ConversationID 是 AI 侧回显的会话 ID。网关是会话 ID 的唯一权威
	//（REQ-ORCH-006）：AI 在 `conversation_id` 为空时会自建，故调用方 MUST 以传入的 ID 为准，
	// 不一致时记 ERROR + `gw_session_mismatch_total`。
	// AI 自己的 `message_id` 有意丢弃 —— 消息台账属于网关（docs/03-§4.1）。
	ConversationID string

	// Degraded 与 DegradedReasons 来自 AI 侧的降级信息（docs/04-§9）。
	Degraded        bool
	DegradedReasons []string

	References json.RawMessage
	ToolCalls  json.RawMessage
	Usage      *MessageUsage

	// Model 是**实际**使用的模型名（取 AI 返回值，不是请求里那个）。
	Model string
	// ElapsedMS 是 AI 侧耗时，用于排障（网关自己的耗时另算）。
	ElapsedMS int

	// TraceID 是 AI 侧回显的 trace id（接缝 J3，S8）。提到返回值里是因为
	// AC-NFR-06 要求两侧可查到同一条链路 —— 只存在于日志就无法被代码判定与计数。
	// 传输层取不到时为空串，调用方 MUST 跳过比较。
	TraceID string
}
