package data

import (
	"context"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cursor"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// MsgCursor 是消息分页的游标载荷。
// 只用 `seq` 就够了：它在会话内唯一且严格递增（docs/03-§4.2），
// 而「时间戳 + id」的复合游标在同毫秒并发时需要整个元组参与比较才能稳定排序。
type MsgCursor struct {
	Seq int `json:"seq"`
	// ConversationID 只作可读性提示（排障时能看出游标属于哪个会话）。
	ConversationID string `json:"conversation_id,omitempty"`
}

// Encode 序列化成不透明串。
func (c MsgCursor) Encode() (string, error) { return cursor.Encode(c) }

// DecodeMsgCursor 解析消息游标。
func DecodeMsgCursor(s string) (*MsgCursor, error) {
	var c MsgCursor
	if err := cursor.Decode(s, &c); err != nil {
		return nil, errs.New(errs.CodeInvalidArgument).
			WithDetail("cursor", "游标不合法").WithCause(err)
	}
	if c.Seq <= 0 {
		// seq 从 1 开始；<=0 的游标不可能是本系统签发的。
		return nil, errs.New(errs.CodeInvalidArgument).WithDetail("cursor", "游标 seq 必须为正")
	}
	return &c, nil
}

// 消息列表排序方向（取值与 biz.OrderAsc / biz.OrderDesc 一致）。
const (
	// OrderAsc 按 seq 升序。
	OrderAsc = "asc"
	// OrderDesc 按 seq 降序（取最近 N 条）。
	OrderDesc = "desc"
)

// messageRepo 实现 biz.MessageRepo（规范 §六：实现类型不导出）。
type messageRepo struct{ data *Data }

// NewMessageRepo 构造消息仓储。
func NewMessageRepo(d *Data) biz.MessageRepo { return &messageRepo{data: d} }

// Append 在一个事务内原子分配 seq 并写入消息，分配到的序号回填到 `m.Seq`。
// 两步必须在同一事务里：`LAST_INSERT_ID(expr)` 是连接级的，换连接会读到别人的值；
// 而「分配成功但插入失败」会留下 `message_count` 已加一、消息却不存在的空洞，下次直接跳号且不报错。
func (r *messageRepo) Append(ctx context.Context, userID, conversationID string, m *biz.Message) error {
	conv := &conversationRepo{data: r.data}
	return r.data.DB.InTx(ctx, func(tx *gorm.DB) error {
		seq, err := conv.allocSeq(ctx, tx, userID, conversationID, m.CreatedAt)
		if err != nil {
			return err
		}
		m.Seq = seq
		return tx.WithContext(ctx).Create(toMessagePO(m)).Error
	})
}

// GetOwned 取属于该用户的消息。
// JOIN 会话确认未被软删：软删会话后其消息 MUST 视同不存在（docs/03-§6）。
func (r *messageRepo) GetOwned(ctx context.Context, userID, id string) (*biz.Message, error) {
	var po messagePO
	err := r.data.DB.GORM.WithContext(ctx).Model(&messagePO{}).
		Select(quoteIdent(TableMessage)+".*").
		Joins("JOIN "+quoteIdent(TableConversation)+" AS conv ON conv.id = "+
			quoteIdent(TableMessage)+".conversation_id AND conv.deleted_at IS NULL").
		Where(quoteIdent(TableMessage)+".id = ? AND "+quoteIdent(TableMessage)+".user_id = ?", id, userID).
		Take(&po).Error
	if err != nil {
		return nil, translate(err)
	}
	return toMessageDO(&po), nil
}

// ListByConversation 按 seq 分页列出会话消息（JOIN 会话过滤软删）。
// 排序 MUST 用 seq：`created_at` 在 Windows 上粒度约 15.6ms，
// 同一毫秒内的两条消息顺序会退化成随机（docs/03-§4.2）。
func (r *messageRepo) ListByConversation(ctx context.Context, userID, convID string, in biz.ListMessagesInput) (*biz.MessageList, error) {
	limit := in.Limit
	if limit <= 0 || limit > biz.PageLimitMax {
		limit = biz.PageLimitDefault
	}

	q := r.data.DB.GORM.WithContext(ctx).Model(&messagePO{}).
		Select(quoteIdent(TableMessage)+".*").
		Joins("JOIN "+quoteIdent(TableConversation)+" AS conv ON conv.id = "+
			quoteIdent(TableMessage)+".conversation_id AND conv.deleted_at IS NULL").
		Where(quoteIdent(TableMessage)+".user_id = ? AND "+quoteIdent(TableMessage)+".conversation_id = ?", userID, convID)

	var cur *MsgCursor
	if in.Cursor != "" {
		decoded, err := DecodeMsgCursor(in.Cursor)
		if err != nil {
			return nil, err
		}
		cur = decoded
	}

	if in.Order == OrderAsc {
		if cur != nil {
			q = q.Where(quoteIdent(TableMessage)+".seq > ?", cur.Seq)
		}
		q = q.Order(quoteIdent(TableMessage) + ".seq ASC")
	} else {
		if cur != nil {
			q = q.Where(quoteIdent(TableMessage)+".seq < ?", cur.Seq)
		}
		q = q.Order(quoteIdent(TableMessage) + ".seq DESC")
	}

	var rows []messagePO
	if err := q.Limit(limit + 1).Find(&rows).Error; err != nil {
		return nil, err
	}

	out := &biz.MessageList{Items: make([]biz.Message, 0, len(rows))}
	if len(rows) > limit {
		out.HasMore = true
		rows = rows[:limit]
	}
	for i := range rows {
		out.Items = append(out.Items, *toMessageDO(&rows[i]))
	}
	if out.HasMore && len(rows) > 0 {
		// 游标取本页最后一条的 seq：下一页从它继续，与排序方向无关。
		next := MsgCursor{Seq: rows[len(rows)-1].Seq, ConversationID: convID}
		encoded, err := next.Encode()
		if err != nil {
			return nil, err
		}
		out.NextCursor = encoded
	}
	return out, nil
}

// Delete 硬删单条消息（docs/03-§6：不影响 AI 侧上下文）。
// 先 GetOwned 是为了挡住「软删会话的消息」：只按 (id, user_id) 删时，
// 客户端可以拿一个已删会话里的消息 id 把它删掉 —— 不可见的数据更不应该可写。
func (r *messageRepo) Delete(ctx context.Context, userID, id string) error {
	if _, err := r.GetOwned(ctx, userID, id); err != nil {
		return err
	}
	res := r.data.DB.GORM.WithContext(ctx).
		Where("id = ? AND user_id = ?", id, userID).
		Delete(&messagePO{})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return biz.ErrNotFound
	}
	return nil
}

// ---- 映射（PO ↔ DO）----

func toMessagePO(m *biz.Message) *messagePO {
	return &messagePO{
		ID:              m.ID,
		ConversationID:  m.ConversationID,
		UserID:          m.UserID,
		Seq:             m.Seq,
		Role:            m.Role,
		Content:         m.Content,
		Status:          m.Status,
		FinishReason:    m.FinishReason,
		Refs:            NewJSONRaw(string(m.References)),
		ToolCalls:       NewJSONRaw(string(m.ToolCalls)),
		Usage:           toUsagePO(m.Usage),
		Model:           m.Model,
		Degraded:        m.Degraded,
		DegradedReasons: JSONList(orEmptyStrings(m.DegradedReasons)),
		ElapsedMS:       m.ElapsedMS,
		TraceID:         m.TraceID,
		CreatedAt:       m.CreatedAt,
	}
}

func toMessageDO(po *messagePO) *biz.Message {
	return &biz.Message{
		ID:              po.ID,
		ConversationID:  po.ConversationID,
		UserID:          po.UserID,
		Seq:             po.Seq,
		Role:            po.Role,
		Content:         po.Content,
		Status:          po.Status,
		FinishReason:    po.FinishReason,
		References:      refsBytes(po.Refs),
		ToolCalls:       refsBytes(po.ToolCalls),
		Usage:           toUsageDO(po.Usage),
		Model:           po.Model,
		Degraded:        po.Degraded,
		DegradedReasons: po.DegradedReasons.OrEmpty(),
		ElapsedMS:       po.ElapsedMS,
		TraceID:         po.TraceID,
		CreatedAt:       po.CreatedAt,
	}
}

// refsBytes 把 JSON 列还原成原始字节；未设置时返回 nil。
// nil 与 `[]` 的区别要保住：`reference` 事件一条都没来过时（如未开 RAG），
// 响应里应该是 `[]` 而不是 `null`。判断交给 DTO 层，存储层如实回答「有没有值」。
func refsBytes(raw JSONRaw) []byte {
	if !raw.Valid || raw.Raw == "" {
		return nil
	}
	return []byte(raw.Raw)
}

func toUsagePO(u *biz.MessageUsage) *Usage {
	if u == nil {
		return nil
	}
	return &Usage{
		PromptTokens:     u.PromptTokens,
		CompletionTokens: u.CompletionTokens,
		TotalTokens:      u.TotalTokens,
	}
}

func toUsageDO(u *Usage) *biz.MessageUsage {
	if u == nil {
		return nil
	}
	return &biz.MessageUsage{
		PromptTokens:     u.PromptTokens,
		CompletionTokens: u.CompletionTokens,
		TotalTokens:      u.TotalTokens,
	}
}
