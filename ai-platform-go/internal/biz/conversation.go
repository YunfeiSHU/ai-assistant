package biz

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ids"
)

// ---- 契约常量（docs/03-§1 / §2.1 / §3）----

// 会话状态与标题来源取值（与 deploy/mysql/001_gateway_tables.sql 的列注释一致）。
const (
	// ConversationStatusActive 表示会话可继续收发消息。
	ConversationStatusActive = "active"
	// ConversationStatusArchived 表示已归档；再发消息返回 409 CONVERSATION_ARCHIVED。
	ConversationStatusArchived = "archived"

	// TitleSourceAuto 表示标题由服务端自动生成（可被后续消息再次改写）。
	TitleSourceAuto = "auto"
	// TitleSourceManual 表示标题由用户指定；一旦置为该值就不再自动改写。
	TitleSourceManual = "manual"
)

// 会话字段上限。入参校验与列宽用同一来源：放宽校验却不改列宽会被 MySQL 静默截断。
const (
	// ConversationTitleMaxRunes 是标题最大字符数（按 rune 计，不是字节）。
	ConversationTitleMaxRunes = 100
	// ConversationKBIDsMax 是单会话可关联的知识库数量上限。
	ConversationKBIDsMax = 10
	// ConversationMetadataKeyMax 是自定义 metadata 键长上限（字节）。
	ConversationMetadataKeyMax = 64
	// ConversationKeywordMaxRunes 是检索关键词的字符数上限。
	ConversationKeywordMaxRunes = 100
	// AutoTitleMaxCharsDefault 是自动标题的默认截断长度（AUTO_TITLE_MAX_CHARS）。
	AutoTitleMaxCharsDefault = 30
	// AutoTitleFallback 是自动标题兜底值的前缀，完整形如 `新对话 09-28`。
	AutoTitleFallback = "新对话"
	// AutoTitleEllipsis 是截断标记。
	AutoTitleEllipsis = "…"
)

// Conversation 是会话领域对象（`conversation` 表）。
// 虽无敏感列，仍由 service 逐字段映射成响应 DTO —— 将来加内部键或软删标记时不会顺手写出去。
type Conversation struct {
	ID            string
	UserID        string
	Title         string
	TitleSource   string
	Status        string
	Model         *string
	KBIDs         []string
	MessageCount  int
	LastMessageAt *time.Time
	Pinned        bool
	Metadata      map[string]string
	CreatedAt     time.Time
	UpdatedAt     time.Time
	DeletedAt     *time.Time
}

// IsActive 报告会话是否可写（只有 active 能追加消息，docs/03-§1 不变式）。
func (c *Conversation) IsActive() bool {
	return c.DeletedAt == nil && c.Status == ConversationStatusActive
}

// ---- 输入结构 ----

// CreateConversationInput 是 `POST /conversations` 请求体（docs/03-§2.1）。
// Title 用指针：「不传」与「传空串」都按「不设标题」处理（随后被自动标题覆盖）。
type CreateConversationInput struct {
	Title    *string           `json:"title"`
	Model    *string           `json:"model"`
	KBIDs    []string          `json:"kb_ids"`
	Metadata map[string]string `json:"metadata"`
}

// UpdateConversationInput 是 `PATCH /conversations/{id}` 请求体。
// Model/KBIDs 用 PatchValue 三态：`model: null` 是有效语义（回到全局默认），不算「不改」。
// Title 用普通指针：标题列 NOT NULL，null 直接判为非法。
type UpdateConversationInput struct {
	Title  *string              `json:"title"`
	Model  PatchValue[string]   `json:"model"`
	KBIDs  PatchValue[[]string] `json:"kb_ids"`
	Pinned *bool                `json:"pinned"`
}

// ListConversationsInput 是 `GET /conversations` 的查询参数（已归一化），
// 同时充当仓储过滤条件，省掉一层会慢慢漂移的「DTO → 过滤条件」转换。
type ListConversationsInput struct {
	PaginationInput
	Status  string
	Pinned  *bool
	Keyword string
}

// ConversationList 是会话列表结果。
type ConversationList struct {
	Items      []Conversation
	NextCursor string
	HasMore    bool
}

// ConversationPatch 是仓储要落库的已校验字段集（nil / 未 Present = 不改）。
// 不用 `map[string]any`：列名写错字母 map 不报错而结构体字段会，data 负责翻成列映射。
type ConversationPatch struct {
	Title       *string
	TitleSource *string
	Status      *string
	Model       PatchValue[string]
	KBIDs       *[]string
	Pinned      *bool
}

// Empty 报告补丁是否没有任何要改的字段。
func (p ConversationPatch) Empty() bool {
	return p.Title == nil && p.TitleSource == nil && p.Status == nil &&
		!p.Model.Present && p.KBIDs == nil && p.Pinned == nil
}

// ---- 仓储接口（规范 §六：接口在 biz，实现在 data）----

// ConversationRepo 是会话表的仓储接口。
// 所有方法都带 `userID`：归属校验是查询条件而非事后判断，避免出现
// 「越权返回 403 还是 404」的分支（AC-CONV-02 要求不存在该分支）。
type ConversationRepo interface {
	// Create 插入会话。
	Create(ctx context.Context, c *Conversation) error
	// GetOwned 取属于该用户的未删除会话；不存在/越权返回 ErrNotFound。
	GetOwned(ctx context.Context, userID, id string) (*Conversation, error)
	// List 按游标分页列出会话。
	List(ctx context.Context, userID string, in ListConversationsInput) (*ConversationList, error)
	// Update 更新给定字段；会话不存在/越权/已删返回 ErrNotFound。
	Update(ctx context.Context, userID, id string, patch ConversationPatch, at time.Time) error
	// SetAutoTitle 在「仍是 auto 且标题为空」时写入自动标题，返回是否写入。
	// 条件必须落在 SQL 里：先读后写会把用户刚手工改过的标题覆盖掉（AC-CONV-04）。
	SetAutoTitle(ctx context.Context, userID, id, title string, at time.Time) (bool, error)
	// SoftDelete 软删会话；消息靠 JOIN 会话过滤，因此这里是原子的一步。
	SoftDelete(ctx context.Context, userID, id string, at time.Time) error
}

// ---- 服务 ----

// ConversationDeps 是会话服务的依赖。
// 不含 `AutoTitleMaxChars`：自动标题的触发点在 MessageService，配置项只放一处。
type ConversationDeps struct {
	Conversations ConversationRepo
	Clock         nowFunc
	Log           *slog.Logger
}

// ConversationService 实现会话 CRUD、归档与软删（REQ-CONV-001..003）。
type ConversationService struct{ d ConversationDeps }

// NewConversationService 构造会话服务。
func NewConversationService(d ConversationDeps) *ConversationService {
	if d.Clock == nil {
		d.Clock = clockx.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	return &ConversationService{d: d}
}

func (s *ConversationService) now() time.Time { return s.d.Clock() }

// Create 创建会话（REQ-CONV-001）。
// MUST NOT 调用 ai-platform：一次网络往返会让 P95 变成依赖 AI 的可用性。
func (s *ConversationService) Create(ctx context.Context, userID string, in CreateConversationInput) (*Conversation, error) {
	if fields := validateConversationInput(in.Title, in.Model, in.KBIDs, in.Metadata); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	now := s.now()
	c := &Conversation{
		// ID 只由网关生成（REQ-CONV-001）：CreateConversationInput 没有 id 字段，
		// 「客户端自定义 ID」在类型层面就不可能。
		ID:          ids.NewConversation(),
		UserID:      userID,
		Title:       titleOrEmpty(in.Title),
		TitleSource: TitleSourceAuto,
		Status:      ConversationStatusActive,
		Model:       normalizeOptional(in.Model),
		KBIDs:       normalizeKBIDs(in.KBIDs),
		Metadata:    normalizeMetadata(in.Metadata),
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.d.Conversations.Create(ctx, c); err != nil {
		return nil, wrapDB(err)
	}
	return c, nil
}

// Get 取会话详情。
func (s *ConversationService) Get(ctx context.Context, userID, id string) (*Conversation, error) {
	c, err := s.d.Conversations.GetOwned(ctx, userID, id)
	if err != nil {
		return nil, s.conversationError(ctx, err)
	}
	return c, nil
}

// List 分页列出会话（REQ-AUTH-010）。
func (s *ConversationService) List(ctx context.Context, userID string, in ListConversationsInput) (*ConversationList, error) {
	if fields := validateListInput(in.PaginationInput, in.Status, in.Keyword); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}
	out, err := s.d.Conversations.List(ctx, userID, in)
	if err != nil {
		return nil, wrapDB(err)
	}
	if out == nil {
		out = &ConversationList{}
	}
	return out, nil
}

// Update 修改会话（标题 / 模型 / 检索范围 / 置顶）。
// 改 title 会把 title_source 置为 manual：手工改过的标题 MUST NOT 再被自动标题覆盖（AC-CONV-04）。
func (s *ConversationService) Update(ctx context.Context, userID, id string, in UpdateConversationInput) (*Conversation, error) {
	var kbIDs []string
	if in.KBIDs.Present && in.KBIDs.Value != nil {
		kbIDs = *in.KBIDs.Value
	}
	if fields := validateConversationInput(in.Title, in.Model.Value, kbIDs, nil); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	patch := ConversationPatch{
		Model:  in.Model,
		Pinned: in.Pinned,
	}
	if in.Title != nil {
		title := strings.TrimSpace(*in.Title)
		manual := TitleSourceManual
		patch.Title = &title
		patch.TitleSource = &manual
	}
	if in.KBIDs.Present {
		kb := normalizeKBIDs(kbIDs)
		patch.KBIDs = &kb
	}
	if patch.Empty() {
		// 没有可改字段：按幂等处理，直接回显当前状态。
		return s.Get(ctx, userID, id)
	}

	if err := s.d.Conversations.Update(ctx, userID, id, patch, s.now()); err != nil {
		return nil, s.conversationError(ctx, err)
	}
	return s.Get(ctx, userID, id)
}

// SetArchived 归档 / 取消归档（REQ-CONV-003）。
func (s *ConversationService) SetArchived(ctx context.Context, userID, id string, archived bool) (*Conversation, error) {
	status := ConversationStatusActive
	if archived {
		status = ConversationStatusArchived
	}
	if err := s.d.Conversations.Update(ctx, userID, id, ConversationPatch{Status: &status}, s.now()); err != nil {
		return nil, s.conversationError(ctx, err)
	}
	return s.Get(ctx, userID, id)
}

// Delete 软删会话（REQ-CONV-003）。
// 删不存在的资源返回 404 而非 204（docs/02-§7：204 会掩盖越权）。
func (s *ConversationService) Delete(ctx context.Context, userID, id string) error {
	if err := s.d.Conversations.SoftDelete(ctx, userID, id, s.now()); err != nil {
		return s.conversationError(ctx, err)
	}
	return nil
}

// conversationError 把仓储错误映射成对外错误。
// 「不存在」与「越权」共用 404（AC-CONV-02）：状态码差异本身就是「id 存在」的探针。
func (s *ConversationService) conversationError(ctx context.Context, err error) error {
	if errors.Is(err, ErrNotFound) {
		return errs.New(errs.CodeConversationNotFound)
	}
	return wrapDB(err)
}

// ---- 自动标题（REQ-CONV-002 / docs/03-§3）----

// AutoTitle 由首条用户消息推导会话标题（docs/03-§3）。
// MUST NOT 调用 LLM：会让「发第一条消息」多一次模型调用，收益不抵成本。
//
// 规则：① 换行/制表符 → 空格；② 去 Markdown 标记（` # * [ ]）；③ 折叠空白并去首尾；
// ④ 超 maxChars 截断补 `…`；⑤ 不足 2 字符时用 `新对话 MM-DD` 兜底。
// createdAt 只用于兜底日期，按 UTC 取。
func AutoTitle(content string, maxChars int, createdAt time.Time) string {
	if maxChars <= 0 {
		maxChars = AutoTitleMaxCharsDefault
	}

	// ① 换行/制表符先变空格：直接删除会把两行粘成一个词。
	replaced := strings.Map(func(r rune) rune {
		switch r {
		case '\n', '\r', '\t':
			return ' '
		}
		return r
	}, content)

	// ② 去 Markdown 标记：这些字符在标题里没有意义。
	sb := strings.Builder{}
	sb.Grow(len(replaced))
	for _, r := range replaced {
		switch r {
		case '`', '#', '*', '[', ']':
			continue
		}
		sb.WriteRune(r)
	}

	// ③ 折叠空白。
	title := strings.Join(strings.Fields(sb.String()), " ")

	// ④ 截断按字符而非字节，否则会把汉字切成非法 UTF-8。
	runes := []rune(title)
	if len(runes) > maxChars {
		title = string(runes[:maxChars]) + AutoTitleEllipsis
	}

	// ⑤ 兜底。
	if len([]rune(title)) < 2 {
		title = AutoTitleFallback + " " + createdAt.UTC().Format("01-02")
	}
	return title
}

// ---- 校验 ----

func validateConversationInput(title, model *string, kbIDs []string, metadata map[string]string) []errs.FieldError {
	var fields []errs.FieldError
	if title != nil && len([]rune(strings.TrimSpace(*title))) > ConversationTitleMaxRunes {
		fields = append(fields, errs.FieldError{
			Field: "title", Reason: "too_long",
			Message: "title 长度不能超过 100",
		})
	}
	if model != nil {
		m := strings.TrimSpace(*model)
		if m == "" {
			fields = append(fields, errs.FieldError{
				Field: "model", Reason: "empty", Message: "model 不能为空串（要清空请传 null）",
			})
		} else if len([]rune(m)) > 64 {
			fields = append(fields, errs.FieldError{
				Field: "model", Reason: "too_long", Message: "model 长度不能超过 64",
			})
		}
	}
	fields = append(fields, validateKBIDs(kbIDs)...)
	for k, v := range metadata {
		if len([]rune(k)) > ConversationMetadataKeyMax || len([]rune(v)) > ConversationMetadataKeyMax {
			fields = append(fields, errs.FieldError{
				Field: "metadata", Reason: "too_long",
				Message: "metadata 的键与值长度都不能超过 64",
			})
			break
		}
	}
	return fields
}

func validateKBIDs(kbIDs []string) []errs.FieldError {
	if len(kbIDs) > ConversationKBIDsMax {
		return []errs.FieldError{{
			Field: "kb_ids", Reason: "too_many",
			Message: "kb_ids 最多 10 个",
		}}
	}
	for _, id := range kbIDs {
		if !ids.Validate(id) {
			return []errs.FieldError{{
				Field: "kb_ids", Reason: "invalid_format",
				Message: "kb_ids 必须是 kb_ 前缀的 ULID",
			}}
		}
	}
	return nil
}

func validateListInput(p PaginationInput, status, keyword string) []errs.FieldError {
	var fields []errs.FieldError
	if p.Limit < 1 || p.Limit > PageLimitMax {
		fields = append(fields, errs.FieldError{
			Field: "limit", Reason: "out_of_range",
			Message: "limit 必须在 1..100 之间",
		})
	}
	if status != "" && status != ConversationStatusActive && status != ConversationStatusArchived {
		fields = append(fields, errs.FieldError{
			Field: "status", Reason: "invalid_value",
			Message: "status 只能是 active 或 archived",
		})
	}
	if len([]rune(keyword)) > ConversationKeywordMaxRunes {
		fields = append(fields, errs.FieldError{
			Field: "keyword", Reason: "too_long",
			Message: "keyword 长度不能超过 100",
		})
	}
	return fields
}

// ---- 归一化 ----

func titleOrEmpty(title *string) string {
	if title == nil {
		return ""
	}
	return strings.TrimSpace(*title)
}

// normalizeOptional 把空串归一化成 nil：两者语义相同（都是「用默认」），
// 同时存在会让判空写出 `x == nil || x == ""` 而迟早漏掉一处。
func normalizeOptional(s *string) *string {
	if s == nil {
		return nil
	}
	v := strings.TrimSpace(*s)
	if v == "" {
		return nil
	}
	return &v
}

// normalizeKBIDs 保证返回非 nil 切片（JSON 序列化时输出 `[]` 而不是 `null`）。
func normalizeKBIDs(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

// normalizeMetadata 保证返回非 nil map（契约里 metadata 默认是 `{}`）。
func normalizeMetadata(in map[string]string) map[string]string {
	if in == nil {
		return map[string]string{}
	}
	return in
}
