package biz

import (
	"context"
	"encoding/json"
)

// 本文件是**网关 → ai-platform 的编排接缝**（docs/04-§2.2 的 `Chat` 调用，接缝 J4/J5）。
//
// 为什么 M2 就要定义它：写消息链路（分配 seq → 写 user 消息 → 自动标题 → 调 AI
// → 写 assistant 消息）是一条**顺序敏感**的流程，其中「先落 user 消息再调 AI」
// 是 docs/03-§5 明确规定的。把 AI 调用抽成一个接口后，M2 可以先把这条链路的
// 前半段跑通并验收（AC-CONV-03/05/07），M3 只需要换一个实现类，不改业务代码。
//
// 传输形态（docs/04-§2.1 的默认建议是 HTTP/SSE，本项目按架构决策改为 gRPC）
// 对**本接口无影响**：请求/结果都是普通结构体，用哪种传输是 M3 的实现细节。

// ChatOrchestrator 是非流式对话的编排入口。
//
// 只定义 M2 用到的一个方法：流式的 `ChatStream`（M4）等真正要用时再加 ——
// 提前塞进接口会让 M2 的每个测试假实现都要写一个空方法。
type ChatOrchestrator interface {
	// Chat 调用 ai-platform 得到一次完整回答。
	//
	// 返回的错误 MUST 已经是 `*errs.AppError`（AI 侧错误码原样透传，接缝 J2）。
	Chat(ctx context.Context, req ChatRequest) (*ChatResult, error)
}

// ChatRequest 是一次非流式对话的输入（对应 ai-platform 的 `ChatRequest`）。
//
// 字段与 docs/04-§2.3 的 proto 一一对应，**多了 `UserID` 与 `UserToken`**：
//
//   - `UserToken` 才是身份来源 —— 网关把调用方的 Bearer token 原样交给 AI，
//     由 AI 自己校验 JWT（接缝 J1，docs/04-§3.2）。MUST NOT 记录到日志。
//   - `UserID` 只用于网关自己的日志、审计与「会话 ID 不一致」的告警定位，
//     **不进 proto**。docs/04-§3.2 明令「网关 MUST NOT 自造 user_id 让 AI 信任
//     （防伪造）」—— 一个可被调用方伪造的 user_id 字段比没有它更危险。
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

// ChatMessage 是发给 AI 的一轮历史消息。
//
// 刻意不复用 `Message`：那条消息带着 seq/状态/用量等台账字段，
// 而 AI 只需要 `role` + `content`（多传的字段只会让两侧容易「以为对方在用」）。
//
// json 标签是给 HTTP 通道用的（`AI_GRPC_ENABLED=false` 时走 `POST /chat`）：
// 没有标签时 `encoding/json` 会输出 `Role`/`Content`，而 AI 的 Pydantic 模型
// 是严格模式 —— 字段名错了不是「忽略」而是 **422**。
type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ChatResult 是 ai-platform 的一次完整回答。
//
// `References` / `ToolCalls` 用 `json.RawMessage` 而不是具体结构体：
// 它们的 schema 由 ai-platform 定义（docs/06-§6），网关只做「原样存、原样取」。
// 一旦网关开始解析，AI 侧改字段名就会变成网关的故障（docs/03-§4.1）。
type ChatResult struct {
	Content      string
	FinishReason string

	// ConversationID 是 AI 侧回显的会话 ID。
	//
	// 网关是会话 ID 的**唯一权威**（REQ-ORCH-006 / docs/04-§6）：AI 在
	// `conversation_id` 为空时会自建，返回的值可能与请求不一致。调用方 MUST
	// 以自己传入的 ID 为准，不一致时记 ERROR + 计数 `gw_session_mismatch_total`。
	//
	// AI 侧自己的 `message_id` 则**有意丢弃**：消息台账属于网关（docs/03-§4.1），
	// 保留一个用不上的外部 ID 只会让人误以为它是权威的。
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

	// TraceID 是 AI 侧**回显**的 trace id（接缝 J3，S8）。
	//
	// 为什么要把一个「日志字段」提到业务返回值里：AC-NFR-06 要求
	// 「用同一个 trace_id 能在网关与 AI 两侧查到同一条链路」，
	// 那就必须在网关侧能**看到** AI 说的那个值 —— 只存在于日志里时，
	// 「两边不一致」这件事无法被代码判定，也就无法被指标计数。
	//
	// 传输层取不到时为空串（例如 AI 未回该头），调用方 MUST 跳过比较。
	TraceID string
}
