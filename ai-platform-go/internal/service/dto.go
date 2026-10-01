package service

import (
	"encoding/json"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

// 响应 DTO（规范 §三.2：`Biz Result → Response DTO` 发生在 service）。
// 这些是契约（docs/02-§6）的逐字实现：JSON 字段名、时间格式都不能改。
// 不放 biz 是因为 biz 不知道「今天有没有 HTTP」（M3 起还要接 gRPC），也不知道字段该怎么序列化。

// UserResponse 是对外暴露的用户视图（docs/02-§6.1 / §6.2）。
//
// ⚠️ MUST NOT 包含 `password_hash`、`token_version` 或任何含 `hash` 子串的字段
// （AC-AUTH-09 会直接对响应 JSON 做子串扫描）。
type UserResponse struct {
	ID          string  `json:"id"`
	Email       string  `json:"email"`
	Nickname    string  `json:"nickname"`
	Plan        string  `json:"plan"`
	Status      string  `json:"status"`
	CreatedAt   string  `json:"created_at"`
	UpdatedAt   string  `json:"updated_at"`
	LastLoginAt *string `json:"last_login_at"`
}

// ToUserResponse 把领域对象映射成对外视图。
// 逐字段显式赋值是刻意的：直接 `json.Marshal(领域对象)` 会把 `PasswordHash` / `TokenVersion`
// 一起写出去，而且不会报错。
func ToUserResponse(u *biz.User) *UserResponse {
	if u == nil {
		return nil
	}
	return &UserResponse{
		ID:          u.ID,
		Email:       u.Email,
		Nickname:    u.Nickname,
		Plan:        u.Plan,
		Status:      u.Status,
		CreatedAt:   clockx.Format(u.CreatedAt),
		UpdatedAt:   clockx.Format(u.UpdatedAt),
		LastLoginAt: clockx.FormatPtr(u.LastLoginAt),
	}
}

// TokenResponse 是登录 / 刷新返回的令牌对（docs/02-§6.1）。
type TokenResponse struct {
	AccessToken  string        `json:"access_token"`
	RefreshToken string        `json:"refresh_token"`
	TokenType    string        `json:"token_type"`
	ExpiresIn    int           `json:"expires_in"`
	User         *UserResponse `json:"user,omitempty"`
}

// ToTokenResponse 把 biz 的签发结果映射成对外视图。
func ToTokenResponse(p *biz.TokenPair) *TokenResponse {
	if p == nil {
		return nil
	}
	return &TokenResponse{
		AccessToken:  p.AccessToken,
		RefreshToken: p.RefreshToken,
		TokenType:    p.TokenType,
		ExpiresIn:    p.ExpiresIn,
		User:         ToUserResponse(p.User),
	}
}

// Page 是列表接口的统一响应体（docs/02-§2.1）：`{items, next_cursor, has_more}`。
// `NextCursor` 用指针：契约里它是 `null` 而不是空串，空串会被客户端当成一个（必然解析失败的）游标值。
type Page[T any] struct {
	Items      []T     `json:"items"`
	NextCursor *string `json:"next_cursor"`
	HasMore    bool    `json:"has_more"`
}

// NewPage 构造分页响应；items 为 nil 时输出 `[]`。
func NewPage[T any](items []T, nextCursor string, hasMore bool) Page[T] {
	if items == nil {
		items = []T{}
	}
	p := Page[T]{Items: items, HasMore: hasMore}
	if nextCursor != "" {
		p.NextCursor = &nextCursor
	}
	return p
}

// ConversationResponse 是会话对象（docs/03-§1）。
// `user_id` 保留：它是会话模型的一等字段，而这里的会话一定是调用者自己的，
// 不构成信息泄露；反过来缺了它，客户端缓存／合并多个用户的会话时就得另找字段区分。
type ConversationResponse struct {
	ID            string            `json:"id"`
	UserID        string            `json:"user_id"`
	Title         string            `json:"title"`
	TitleSource   string            `json:"title_source"`
	Status        string            `json:"status"`
	Model         *string           `json:"model"`
	KBIDs         []string          `json:"kb_ids"`
	MessageCount  int               `json:"message_count"`
	LastMessageAt *string           `json:"last_message_at"`
	Pinned        bool              `json:"pinned"`
	Metadata      map[string]string `json:"metadata"`
	CreatedAt     string            `json:"created_at"`
	UpdatedAt     string            `json:"updated_at"`
}

// ToConversationResponse 把领域对象映射成对外视图。
func ToConversationResponse(c *biz.Conversation) *ConversationResponse {
	if c == nil {
		return nil
	}
	kbIDs := c.KBIDs
	if kbIDs == nil {
		kbIDs = []string{}
	}
	metadata := c.Metadata
	if metadata == nil {
		metadata = map[string]string{}
	}
	return &ConversationResponse{
		ID:            c.ID,
		UserID:        c.UserID,
		Title:         c.Title,
		TitleSource:   c.TitleSource,
		Status:        c.Status,
		Model:         c.Model,
		KBIDs:         kbIDs,
		MessageCount:  c.MessageCount,
		LastMessageAt: clockx.FormatPtr(c.LastMessageAt),
		Pinned:        c.Pinned,
		Metadata:      metadata,
		CreatedAt:     clockx.Format(c.CreatedAt),
		UpdatedAt:     clockx.Format(c.UpdatedAt),
	}
}

// ToConversationResponses 批量映射（空结果输出 `[]` 而不是 `null`）。
func ToConversationResponses(items []biz.Conversation) []*ConversationResponse {
	out := make([]*ConversationResponse, 0, len(items))
	for i := range items {
		out = append(out, ToConversationResponse(&items[i]))
	}
	return out
}

// MessageResponse 是消息对象（docs/03-§4.3 的示例形状）。
// `references` / `tool_calls` 用 `json.RawMessage` 原样透传：结构由 ai-platform 定义，
// 网关多解析一层就多一处「AI 改字段名 → 网关挂掉」的隐患。
type MessageResponse struct {
	ID              string            `json:"id"`
	ConversationID  string            `json:"conversation_id"`
	UserID          string            `json:"user_id"`
	Seq             int               `json:"seq"`
	Role            string            `json:"role"`
	Content         string            `json:"content"`
	Status          string            `json:"status"`
	FinishReason    *string           `json:"finish_reason"`
	References      json.RawMessage   `json:"references"`
	ToolCalls       json.RawMessage   `json:"tool_calls"`
	Usage           *biz.MessageUsage `json:"usage"`
	Model           *string           `json:"model"`
	Degraded        bool              `json:"degraded"`
	DegradedReasons []string          `json:"degraded_reasons"`
	ElapsedMS       *int              `json:"elapsed_ms"`
	TraceID         *string           `json:"trace_id"`
	CreatedAt       string            `json:"created_at"`
}

// ToMessageResponse 把领域对象映射成对外视图。
// 「空值必须写成 `[]` 而不是 `null`」的地方在这里统一掉：客户端遍历 `references` 时不会去判空。
func ToMessageResponse(m *biz.Message) *MessageResponse {
	if m == nil {
		return nil
	}
	reasons := m.DegradedReasons
	if reasons == nil {
		reasons = []string{}
	}
	return &MessageResponse{
		ID:              m.ID,
		ConversationID:  m.ConversationID,
		UserID:          m.UserID,
		Seq:             m.Seq,
		Role:            m.Role,
		Content:         m.Content,
		Status:          m.Status,
		FinishReason:    m.FinishReason,
		References:      rawOrEmptyArray(m.References),
		ToolCalls:       rawOrEmptyArray(m.ToolCalls),
		Usage:           m.Usage,
		Model:           m.Model,
		Degraded:        m.Degraded,
		DegradedReasons: reasons,
		ElapsedMS:       m.ElapsedMS,
		TraceID:         m.TraceID,
		CreatedAt:       clockx.Format(m.CreatedAt),
	}
}

// ToMessageResponses 批量映射（空结果输出 `[]`）。
func ToMessageResponses(items []biz.Message) []*MessageResponse {
	out := make([]*MessageResponse, 0, len(items))
	for i := range items {
		out = append(out, ToMessageResponse(&items[i]))
	}
	return out
}

// SendMessageResponse 是 `POST /conversations/{id}/messages` 的响应体。
// 契约（docs/03-§4.3）只规定了请求字段与成功码 200，没规定响应体形状，
// 所以显式给出两个字段而不是只回 assistant：客户端渲染一轮对话需要两条消息的 id 与 seq。
type SendMessageResponse struct {
	UserMessage      *MessageResponse `json:"user_message"`
	AssistantMessage *MessageResponse `json:"assistant_message"`
}

// ToSendMessageResponse 映射一次提问的落库结果。
func ToSendMessageResponse(r *biz.SendResult) *SendMessageResponse {
	if r == nil {
		return nil
	}
	return &SendMessageResponse{
		UserMessage:      ToMessageResponse(r.User),
		AssistantMessage: ToMessageResponse(r.Assistant),
	}
}

// rawOrEmptyArray 把空的 JSON 列写成 `[]`。
func rawOrEmptyArray(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 || string(raw) == "null" {
		return json.RawMessage("[]")
	}
	return raw
}
