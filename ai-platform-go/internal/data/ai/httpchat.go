package ai

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件是 `Chat` 的 **HTTP 通道**实现（`AI_GRPC_ENABLED=false` 时启用）。
//
// 为什么两种传输都要有，而不是「M3 定了 gRPC 就把 HTTP 删掉」：
// docs/04-§2.1 的 `REQ-ORCH-002` 第 3 条只有三条允许切到 gRPC 的条件，
// 其中一条就是「保留 HTTP 通道用于独立验收（ai-platform 的 SRS 要求其可独立验收）」。
// 留着它有三个具体好处：
//
//  1. `AI_GRPC_ENABLED` 这个配置项真的有意义 —— 关掉它仍然能跑通对话，
//     而不是变成一个「设错了就 503」的陷阱；
//  2. 它可以和 gRPC 通道做**同一条请求的双通道对拍**（映射层写错了会立刻暴露）；
//  3. AI 侧没起 gRPC server 时（它的 HTTP 服务本来就存在）仍可联调。
//
// 两条通道共用 `biz.ChatResult` 与同一份错误信封解析，所以「换个传输」
// 对上层完全不可见。

// chatResponseJSON 是 AI 的 `POST /chat` 响应（HTTP 通道）。
//
// `references` / `tool_calls` 直接留 `json.RawMessage`：AI 返回的就是
// 契约要求的 JSON，网关多解析一层只会多一处「AI 改字段名 → 网关挂掉」的隐患。
// 这与 gRPC 通道必须逐字段映射（proto 与 JSON 的形状天然不同）形成对照。
type chatResponseJSON struct {
	Answer          string            `json:"answer"`
	ConversationID  string            `json:"conversation_id"`
	MessageID       string            `json:"message_id"`
	References      json.RawMessage   `json:"references"`
	ToolCalls       json.RawMessage   `json:"tool_calls"`
	Usage           *biz.MessageUsage `json:"usage"`
	FinishReason    string            `json:"finish_reason"`
	Model           string            `json:"model"`
	Degraded        bool              `json:"degraded"`
	DegradedReasons []string          `json:"degraded_reasons"`
	ElapsedMS       int               `json:"elapsed_ms"`
}

// httpChatClient 用 HTTP 通道实现 `biz.ChatOrchestrator`。
type httpChatClient struct {
	proxy biz.AIProxy
	// path 是上游 `POST /chat` 的完整路径（含 AI 的 API 前缀）。
	path string
	opt  Options
}

var _ biz.ChatOrchestrator = (*httpChatClient)(nil)

// NewChatOrchestratorHTTP 构造 HTTP 通道的对话客户端。
//
// `apiPrefix` 传网关自己的 `App.APIPrefix`：两侧前缀同名是 docs/04-§7
// 透传的前提（`/api/v1`），这里显式写出来而不是硬编码，是为了让「哪天
// 两侧前缀真的不同了」有一个明确的修改点，而不是散落在各处的字符串。
func NewChatOrchestratorHTTP(proxy biz.AIProxy, apiPrefix string, opt Options) biz.ChatOrchestrator {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	prefix := strings.TrimRight(apiPrefix, "/")
	return &httpChatClient{proxy: proxy, path: prefix + "/chat", opt: opt}
}

// Chat 实现 biz.ChatOrchestrator。
func (c *httpChatClient) Chat(ctx context.Context, req biz.ChatRequest) (*biz.ChatResult, error) {
	body, err := json.Marshal(toHTTPChatRequest(req))
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "marshal_request").
			WithCause(err)
	}

	resp, err := c.proxy.Do(ctx, biz.AIProxyRequest{
		Method:        "POST",
		Path:          c.path,
		Body:          bytes.NewReader(body),
		ContentLength: int64(len(body)),
		ContentType:   "application/json",
		UserToken:     req.UserToken,
		TraceID:       req.TraceID,
		Class:         biz.AIProxyTimeoutChat,
	})
	if err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "empty_response")
	}

	// 先看错误信封：`/chat` 的失败一定带信封（docs/02-§2.2），
	// 而没有信封的非 2xx 说明是反向代理之类的东西在回话。
	if appErr, ok := errs.ParseEnvelope(resp.Body, resp.Status); ok {
		return nil, appErr
	}
	if resp.Status >= 400 {
		return nil, errs.New(codeForStatus(resp.Status)).
			WithTraceID(req.TraceID).
			WithDetail("reason", "non_envelope_body").
			WithDetail("upstream_status", resp.Status)
	}

	var out chatResponseJSON
	if err := json.Unmarshal(resp.Body, &out); err != nil {
		// 200 但 body 不是 JSON：这比 4xx 还难查，必须把状态码带上。
		return nil, errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "malformed_success_body").
			WithDetail("upstream_status", resp.Status).
			WithCause(err)
	}

	return &biz.ChatResult{
		Content:         out.Answer,
		FinishReason:    out.FinishReason,
		ConversationID:  out.ConversationID,
		Degraded:        out.Degraded,
		DegradedReasons: out.DegradedReasons,
		References:      normalizeEmptyRawJSON(out.References),
		ToolCalls:       normalizeEmptyRawJSON(out.ToolCalls),
		Usage:           normalizeUsage(out.Usage),
		Model:           out.Model,
		ElapsedMS:       out.ElapsedMS,
		// AI 侧把入参的 trace_id 原样回显在响应头里（两边同名同值），
		// 拿回来是为了让 biz 能判定「两侧日志里的 trace_id 是否同一个」。
		TraceID: resp.TraceID,
	}, nil
}

// chatRequestJSON 是发给 AI 的 `POST /chat` 请求体。
//
// 字段名与 AI 的 Pydantic 契约为准（不是 proto 的）：
// HTTP 通道的契约真源是 AI 的 OpenAPI（docs/04-§2.1），
// 这份结构体只是「按真源拼 JSON」，不新增任何语义。
type chatRequestJSON struct {
	Query          string            `json:"query"`
	ConversationID string            `json:"conversation_id,omitempty"`
	History        []biz.ChatMessage `json:"history,omitempty"`
	UseRAG         bool              `json:"use_rag"`
	KBIDs          []string          `json:"kb_ids"`
	UseMemory      bool              `json:"use_memory"`
	UseTools       bool              `json:"use_tools"`
	Stream         bool              `json:"stream"`
	Model          string            `json:"model,omitempty"`
	Temperature    *float64          `json:"temperature,omitempty"`
	TopK           *int              `json:"top_k,omitempty"`
	RerankTopN     *int              `json:"rerank_top_n,omitempty"`
	ScoreThreshold *float64          `json:"score_threshold,omitempty"`
}

func toHTTPChatRequest(req biz.ChatRequest) chatRequestJSON {
	out := chatRequestJSON{
		Query:          req.Query,
		ConversationID: strings.TrimSpace(req.ConversationID),
		UseRAG:         req.UseRAG,
		KBIDs:          req.KBIDs,
		UseMemory:      req.UseMemory,
		UseTools:       req.UseTools,
		// 显式 false：`/chat` 只接受 false（`/chat/stream` 才是流式），
		// 带上它能让「这个请求不是流式」在抓包时一目了然。
		Stream:         false,
		Model:          strings.TrimSpace(req.Model),
		Temperature:    req.Temperature,
		TopK:           req.TopK,
		RerankTopN:     req.RerankTopN,
		ScoreThreshold: req.ScoreThreshold,
	}
	if len(req.History) > 0 {
		out.History = req.History
	}
	if out.KBIDs == nil {
		// `kb_ids` 在契约里是数组：null 会被严格模式拒绝（`StrictModel`）。
		out.KBIDs = []string{}
	}
	return out
}

// toHTTPChatStreamRequest 与 `toHTTPChatRequest` 只差一个 `stream` 标志。
//
// 复用同一个结构体而不是复制一份：两边对应的都是 AI 侧**同一个** `ChatRequest`
// 模型（`/chat/stream` 收的 body 与 `/chat` 完全一样），复制一份意味着以后加字段
// 要改两处 —— 而漏掉一处就是「这个开关在流式下静默失效」。
func toHTTPChatStreamRequest(req biz.ChatRequest) chatRequestJSON {
	out := toHTTPChatRequest(req)
	// `/chat/stream` 以**路径**为准（AI 侧注释写明忽略这个字段），
	// 仍然照实传 true：抓包时能一眼看出这是流式请求。
	out.Stream = true
	return out
}

// normalizeEmptyRawJSON 把「没有内容」的几种字面量归一化成 nil。
//
// 需要处理三种写法：`null`（显式无）、`[]`（空列表）、`{}`（空对象）。
// 上游可能用任意一种表达「没有引用/没有工具轨迹」，而它们存储层的表示只该
// 有一种（NULL）—— 对外视图由 service 统一补成 `[]`。
// 不归一化的话，库里会同时存在 `NULL` 与 `'[]'` 两种「空」，
// 而它们在读出时的处理不同，于是「有时多一层判空」这种坑会被带到前端。
func normalizeEmptyRawJSON(raw json.RawMessage) json.RawMessage {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) ||
		bytes.Equal(trimmed, []byte("[]")) || bytes.Equal(trimmed, []byte("{}")) {
		return nil
	}
	return trimmed
}

// normalizeUsage 补 `total = prompt + completion`（与 gRPC 通道同一规则）。
func normalizeUsage(u *biz.MessageUsage) *biz.MessageUsage {
	if u == nil || u.IsZero() {
		return nil
	}
	if u.TotalTokens == 0 {
		u.TotalTokens = u.PromptTokens + u.CompletionTokens
	}
	return u
}

// codeForStatus 把非约定格式的上游状态码归一化成网关错误码（docs/02-§4.2 规则 5）。
func codeForStatus(status int) errs.Code {
	switch status {
	case 504, 408:
		return errs.CodeAITimeout
	case 429:
		return errs.CodeAIOverloaded
	default:
		return errs.CodeAIUnavailable
	}
}
