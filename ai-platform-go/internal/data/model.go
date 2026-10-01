// 持久化对象（PO）：表结构的 Go 侧映射。
//
// 它们与 biz 的领域对象是两套类型，转换只发生在 data 侧（见各仓储文件末尾的 toXxxPO / toXxxDO）。
// 代价是几行笨拙的逐字段赋值，换来的是「表加了一列」与「对外多暴露一个字段」变成两件独立的事 ——
// 共用一套类型时，PasswordHash 这类字段很容易被顺手序列化出去（规范 §三.2 把映射责任给了 service）。
//
// 表结构的权威定义是 deploy/mysql/*.sql（docs/05-§2），这里只做映射，不调用 AutoMigrate。
package data

import "time"

// 表名常量：集中定义避免拼错。
// 取值 MUST 与 deploy/mysql/*.sql 建出的真实表名逐字一致：拼错不会编译失败，只会在运行时报「表不存在」。
const (
	// TableUser 是用户表。
	TableUser = "user"
	// TableRefreshToken 是刷新令牌表（只存 sha256 哈希，不存明文）。
	TableRefreshToken = "refresh_token"
	// TableConversation 是会话表。
	TableConversation = "conversation"
	// TableMessage 是消息表。
	TableMessage = "message"
	// TableQuotaUsage 是配额用量表（当前周期计数）。
	TableQuotaUsage = "quota_usage"
	// TableUsageRecord 是计费/用量明细表（按次追加，供对账）。
	TableUsageRecord = "usage_record"
	// TableIdempotencyRecord 是幂等响应快照表。
	TableIdempotencyRecord = "idempotency_record"
	// TableAuditLog 是审计表；与 ai-platform 共享，双方按同一组 action 写入。
	TableAuditLog = "audit_log"
)

// quoteIdent 给表名/列名加反引号。
// 原生 SQL 里的表名一律用它拼常量，不要写字符串字面量：曾经的批量改名把类型名
// （`conversationPO` / `messagePO`）改进了字符串里，编译器和单测都发现不了，
// 只有真跑 SQL 时才报「表不存在」。守门测试见 TestNoPOTypeNameInSQL。
func quoteIdent(name string) string { return "`" + name + "`" }

// conversationPO 取值。
const (
	// ConvStatusActive 表示会话可继续收发消息。
	ConvStatusActive = "active"
	// ConvStatusArchived 表示会话已归档；归档后不可再发消息（返回 409）。
	ConvStatusArchived = "archived"

	// TitleSourceAuto 表示标题由服务端根据首条用户消息自动生成。
	TitleSourceAuto = "auto"
	// TitleSourceManual 表示标题是用户显式设置的，此后不再被自动改写。
	TitleSourceManual = "manual"
)

// messagePO 取值。
const (
	// MsgRoleUser 是用户消息的角色取值。
	MsgRoleUser = "user"
	// MsgRoleAssistant 是助手消息的角色取值。
	MsgRoleAssistant = "assistant"

	// MsgStatusCompleted 表示该条助手回答已完整落库（收到 done 后才写）。
	MsgStatusCompleted = "completed"
	// MsgStatusPartial 表示流中途中断但已保存部分内容（客户端可见，不参与上下文续写）。
	MsgStatusPartial = "partial"
	// MsgStatusFailed 表示这一轮没有产生可用回答。
	MsgStatusFailed = "failed"

	// FinishReasonStop 表示模型自然结束。
	FinishReasonStop = "stop"
	// FinishReasonLength 表示因达到输出长度上限而截断。
	FinishReasonLength = "length"
	// FinishReasonMaxSteps 表示因达到工具调用步数上限而终止。
	FinishReasonMaxSteps = "max_steps"
	// FinishReasonCanceled 表示客户端取消或连接断开导致终止。
	FinishReasonCanceled = "canceled"
)

// userPO 映射 `user` 表。
// 本表没有 user_id 列（id 就是用户 id）；`refresh_token` 等引用它的列宽是 VARCHAR(64)
// （跨服务字段约定 docs/05-§2.0）。
type userPO struct {
	ID           string     `gorm:"column:id;type:varchar(32);primaryKey"`
	Email        string     `gorm:"column:email;type:varchar(254)"`
	Nickname     string     `gorm:"column:nickname;type:varchar(64)"`
	PasswordHash string     `gorm:"column:password_hash;type:varchar(255)"`
	Plan         string     `gorm:"column:plan;type:varchar(32)"`
	Status       string     `gorm:"column:status;type:varchar(16)"`
	TokenVersion int        `gorm:"column:token_version"`
	LastLoginAt  *time.Time `gorm:"column:last_login_at"`
	CreatedAt    time.Time  `gorm:"column:created_at"`
	UpdatedAt    time.Time  `gorm:"column:updated_at"`
	DeletedAt    *time.Time `gorm:"column:deleted_at"`
}

// TableName 返回表名。
func (userPO) TableName() string { return TableUser }

// refreshTokenPO 映射 `refresh_token` 表。
// 只存 sha256 哈希（REQ-DATA-003）：明文只在响应体里出现一次。
type refreshTokenPO struct {
	ID         string     `gorm:"column:id;type:varchar(32);primaryKey"`
	UserID     string     `gorm:"column:user_id;type:varchar(64)"`
	TokenHash  string     `gorm:"column:token_hash;type:char(64)"`
	DeviceName string     `gorm:"column:device_name;type:varchar(64)"`
	UserAgent  string     `gorm:"column:user_agent;type:varchar(255)"`
	IP         string     `gorm:"column:ip;type:varchar(45)"`
	ExpiresAt  time.Time  `gorm:"column:expires_at"`
	RevokedAt  *time.Time `gorm:"column:revoked_at"`
	CreatedAt  time.Time  `gorm:"column:created_at"`
}

// TableName 返回表名。
func (refreshTokenPO) TableName() string { return TableRefreshToken }

// conversationPO 映射 `conversation` 表。
// `KBIDs` 用 JSONList（语义是 `string[]`，要参与校验与回显）；`Metadata` 用 JSONRaw（透传埋点）。
type conversationPO struct {
	ID            string     `gorm:"column:id;type:varchar(32);primaryKey"`
	UserID        string     `gorm:"column:user_id;type:varchar(64)"`
	Title         string     `gorm:"column:title;type:varchar(100)"`
	TitleSource   string     `gorm:"column:title_source;type:varchar(16)"`
	Status        string     `gorm:"column:status;type:varchar(16)"`
	Model         *string    `gorm:"column:model;type:varchar(64)"`
	KBIDs         JSONList   `gorm:"column:kb_ids;type:json"`
	MessageCount  int        `gorm:"column:message_count"`
	LastMessageAt *time.Time `gorm:"column:last_message_at"`
	Pinned        bool       `gorm:"column:pinned"`
	Metadata      JSONMap    `gorm:"column:metadata;type:json"`
	CreatedAt     time.Time  `gorm:"column:created_at"`
	UpdatedAt     time.Time  `gorm:"column:updated_at"`
	DeletedAt     *time.Time `gorm:"column:deleted_at"`
}

// TableName 返回表名。
func (conversationPO) TableName() string { return TableConversation }

// messagePO 映射 `message` 表。
// 列名 `refs` 而非 `references`（后者是 MySQL 8.0 保留字）。
// `Refs` / `ToolCalls` 用 JSONRaw（结构由 ai-platform 定义，网关只原样存取）；
// `Usage` 相反是强类型 —— 配额要靠 `total_tokens` 累加（接缝 J7）。
type messagePO struct {
	ID              string    `gorm:"column:id;type:varchar(32);primaryKey"`
	ConversationID  string    `gorm:"column:conversation_id;type:varchar(32)"`
	UserID          string    `gorm:"column:user_id;type:varchar(64)"`
	Seq             int       `gorm:"column:seq"`
	Role            string    `gorm:"column:role;type:varchar(16)"`
	Content         string    `gorm:"column:content"`
	Status          string    `gorm:"column:status;type:varchar(16)"`
	FinishReason    *string   `gorm:"column:finish_reason;type:varchar(32)"`
	Refs            JSONRaw   `gorm:"column:refs;type:json"`
	ToolCalls       JSONRaw   `gorm:"column:tool_calls;type:json"`
	Usage           *Usage    `gorm:"column:usage;type:json"`
	Model           *string   `gorm:"column:model;type:varchar(64)"`
	Degraded        bool      `gorm:"column:degraded"`
	DegradedReasons JSONList  `gorm:"column:degraded_reasons;type:json"`
	ElapsedMS       *int      `gorm:"column:elapsed_ms"`
	TraceID         *string   `gorm:"column:trace_id;type:varchar(64)"`
	CreatedAt       time.Time `gorm:"column:created_at"`
}

// TableName 返回表名。
func (messagePO) TableName() string { return TableMessage }

// quotaUsagePO 映射 `quota_usage` 表（配额**权威**计数）。
type quotaUsagePO struct {
	UserID     string    `gorm:"column:user_id;type:varchar(64);primaryKey"`
	Metric     string    `gorm:"column:metric;type:varchar(32);primaryKey"`
	Period     string    `gorm:"column:period;type:varchar(16);primaryKey"`
	Used       int64     `gorm:"column:used"`
	LimitValue *int64    `gorm:"column:limit_value"`
	CreatedAt  time.Time `gorm:"column:created_at"`
	UpdatedAt  time.Time `gorm:"column:updated_at"`
}

// TableName 返回表名。
func (quotaUsagePO) TableName() string { return TableQuotaUsage }

// usageRecordPO 映射 `usage_record` 表（每请求明细）。
type usageRecordPO struct {
	ID             int64     `gorm:"column:id;primaryKey;autoIncrement"`
	UserID         string    `gorm:"column:user_id;type:varchar(64)"`
	Metric         string    `gorm:"column:metric;type:varchar(32)"`
	Amount         int64     `gorm:"column:amount"`
	ConversationID *string   `gorm:"column:conversation_id;type:varchar(32)"`
	MessageID      *string   `gorm:"column:message_id;type:varchar(32)"`
	TraceID        *string   `gorm:"column:trace_id;type:varchar(64)"`
	CreatedAt      time.Time `gorm:"column:created_at"`
}

// TableName 返回表名。
func (usageRecordPO) TableName() string { return TableUsageRecord }

// idempotencyRecordPO 映射共享表 `idempotency_record`。
// ⚠️ 与 ai-platform 共用（docs/05-§2.7）：列定义 MUST 双方评审后再改，
// 改窄（如把 method 变成无默认值的 NOT NULL）会直接弄挂 AI 侧的写入。
// 网关必填 method / request_hash；AI 侧不填（其默认 ”）。
type idempotencyRecordPO struct {
	ID           int64     `gorm:"column:id;primaryKey;autoIncrement"`
	UserID       string    `gorm:"column:user_id;type:varchar(64)"`
	Method       string    `gorm:"column:method;type:varchar(16)"`
	Path         string    `gorm:"column:path;type:varchar(255)"`
	IdemKey      string    `gorm:"column:idem_key;type:varchar(128)"`
	RequestHash  string    `gorm:"column:request_hash;type:char(64)"`
	StatusCode   int       `gorm:"column:status_code"`
	ResponseBody *string   `gorm:"column:response_body"`
	CreatedAt    time.Time `gorm:"column:created_at"`
	ExpiresAt    time.Time `gorm:"column:expires_at"`
}

// TableName 返回表名。
func (idempotencyRecordPO) TableName() string { return TableIdempotencyRecord }

// auditLogPO 映射共享表 `audit_log`。
//
// resource_type / resource_id 用空串而非 NULL（避三值逻辑，docs/05-§2.8）。
type auditLogPO struct {
	ID           int64     `gorm:"column:id;primaryKey;autoIncrement"`
	UserID       *string   `gorm:"column:user_id;type:varchar(64)"`
	Action       string    `gorm:"column:action;type:varchar(64)"`
	ResourceType string    `gorm:"column:resource_type;type:varchar(32)"`
	ResourceID   string    `gorm:"column:resource_id;type:varchar(64)"`
	IP           string    `gorm:"column:ip;type:varchar(45)"`
	UserAgent    string    `gorm:"column:user_agent;type:varchar(255)"`
	Detail       *string   `gorm:"column:detail"`
	CreatedAt    time.Time `gorm:"column:created_at"`
}

// TableName 返回表名。
func (auditLogPO) TableName() string { return TableAuditLog }
