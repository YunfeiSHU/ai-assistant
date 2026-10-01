package data

import (
	"context"
	"errors"
	"strings"
	"time"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cursor"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// ConvCursor 是会话列表的游标载荷。
//
// 必须包含**排序键的全部列**（pinned + last_message_at + id）：
// 只带 last_message_at 时，翻页期间有一条会话被置顶，就会出现
// 「第一页的末条」与「第二页的首条」之间漏掉或重复记录。
type ConvCursor struct {
	// Pinned 必须是 bool 而不是 0/1 字符串：它与 SQL 里的比较必须同类型。
	Pinned bool `json:"pinned"`
	// LastMessageAt 为空串表示该会话**还没有消息**（列是 NULL）。
	//
	// 不能省掉这一段：MySQL 里 NULL 在 DESC 排序中排最后，
	// 于是「从未发过消息的会话」全在同一 pinned 组的末尾，
	// 游标必须能表达「我已经翻到 NULL 那一段了」。
	LastMessageAt string `json:"last_message_at"`
	ID            string `json:"id"`
}

// Encode 序列化成不透明串。
func (c ConvCursor) Encode() (string, error) { return cursor.Encode(c) }

// DecodeConvCursor 解析游标串。
func DecodeConvCursor(s string) (*ConvCursor, error) {
	var c ConvCursor
	if err := cursor.Decode(s, &c); err != nil {
		return nil, errs.New(errs.CodeInvalidArgument).
			WithDetail("cursor", "游标不合法").WithCause(err)
	}
	if c.LastMessageAt != "" {
		if _, err := clockx.Parse(c.LastMessageAt); err != nil {
			return nil, errs.New(errs.CodeInvalidArgument).
				WithDetail("cursor", "游标时间格式不合法").WithCause(err)
		}
	}
	return &c, nil
}

// Time 返回解析后的排序时间（NULL 时返回零值）。
func (c ConvCursor) Time() time.Time {
	if c.LastMessageAt == "" {
		return time.Time{}
	}
	t, err := clockx.Parse(c.LastMessageAt)
	if err != nil {
		return time.Time{}
	}
	return t
}

// conversationRepo 实现 biz.ConversationRepo（规范 §六：实现类型不导出）。
type conversationRepo struct{ data *Data }

// NewConversationRepo 构造会话仓储。
func NewConversationRepo(d *Data) biz.ConversationRepo { return &conversationRepo{data: d} }

// Create 插入会话。
func (r *conversationRepo) Create(ctx context.Context, c *biz.Conversation) error {
	return r.data.DB.GORM.WithContext(ctx).Create(toConversationPO(c)).Error
}

// GetOwned 取属于该用户的未删除会话。
//
// 「越权」与「不存在」返回同一个 biz.ErrNotFound → 上层统一 404
// （AC-CONV-02：用 B 的 token 访问 A 的会话必须是 404 而不是 403，
// 否则 403 本身就泄漏了「这个 id 存在」）。
func (r *conversationRepo) GetOwned(ctx context.Context, userID, id string) (*biz.Conversation, error) {
	var po conversationPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("id = ? AND user_id = ? AND deleted_at IS NULL", id, userID).
		Take(&po).Error
	if err != nil {
		return nil, translate(err)
	}
	return toConversationDO(&po), nil
}

// List 按游标分页列出会话（docs/03-§2.2）。
//
// 排序 `pinned DESC, last_message_at DESC, id DESC`，与 `idx_conv_user_list`
// 的列顺序一致。多取一条用来判断 has_more —— 比再跑一次 COUNT 便宜，
// 而且不会因为并发写入而与结果集不一致。
func (r *conversationRepo) List(ctx context.Context, userID string, in biz.ListConversationsInput) (*biz.ConversationList, error) {
	limit := in.Limit
	if limit <= 0 || limit > biz.PageLimitMax {
		// 兜底：正常路径上 biz 已经校验过并返回 400 了。
		limit = biz.PageLimitDefault
	}

	q := r.data.DB.GORM.WithContext(ctx).Model(&conversationPO{}).
		Where("user_id = ? AND deleted_at IS NULL", userID)
	if in.Status != "" {
		q = q.Where("status = ?", in.Status)
	}
	if in.Pinned != nil {
		q = q.Where("pinned = ?", *in.Pinned)
	}
	if in.Keyword != "" {
		q = q.Where("title LIKE ? ESCAPE '\\\\'", "%"+escapeLike(in.Keyword)+"%")
	}

	var cur *ConvCursor
	if in.Cursor != "" {
		decoded, err := DecodeConvCursor(in.Cursor)
		if err != nil {
			return nil, err
		}
		cur = decoded
	}
	if cur != nil {
		q = applyConvCursor(q, cur)
	}

	var rows []conversationPO
	if err := q.Order("pinned DESC, last_message_at DESC, id DESC").
		Limit(limit + 1).Find(&rows).Error; err != nil {
		return nil, err
	}

	out := &biz.ConversationList{Items: make([]biz.Conversation, 0, len(rows))}
	if len(rows) > limit {
		out.HasMore = true
		rows = rows[:limit]
	}
	for i := range rows {
		out.Items = append(out.Items, *toConversationDO(&rows[i]))
	}
	if out.HasMore && len(rows) > 0 {
		last := rows[len(rows)-1]
		next := ConvCursor{Pinned: last.Pinned, ID: last.ID}
		if last.LastMessageAt != nil {
			next.LastMessageAt = clockx.Format(*last.LastMessageAt)
		}
		encoded, err := next.Encode()
		if err != nil {
			return nil, err
		}
		out.NextCursor = encoded
	}
	return out, nil
}

// applyConvCursor 把游标条件翻译成 SQL。
//
// 分成「游标落在 NULL 段」与「游标有真实时间」两种情况，是因为
// MySQL 里 NULL 的比较结果恒为 NULL（既不真也不假），
// `last_message_at < NULL` 一行都匹配不到 —— 写成一条统一的三值逻辑
// 表达式会在「翻到没有消息的那一段」时**静默返回空页**。
func applyConvCursor(q *gorm.DB, cur *ConvCursor) *gorm.DB {
	if cur.LastMessageAt == "" {
		// NULL 在 DESC 里排最后：同一个 pinned 组内，游标之后的记录
		// 只能是「同为 NULL 且 id 更小」的那些，以及 pinned 更小的组。
		return q.Where(
			"pinned < ? OR (pinned = ? AND last_message_at IS NULL AND id < ?)",
			cur.Pinned, cur.Pinned, cur.ID,
		)
	}
	t := cur.Time()
	return q.Where(
		"pinned < ? OR (pinned = ? AND (last_message_at < ? OR last_message_at IS NULL OR (last_message_at = ? AND id < ?)))",
		cur.Pinned, cur.Pinned, t, t, cur.ID,
	)
}

// Update 更新会话可变字段（白名单由 biz 决定，这里只做翻译）。
func (r *conversationRepo) Update(ctx context.Context, userID, id string, patch biz.ConversationPatch, at time.Time) error {
	res := r.data.DB.GORM.WithContext(ctx).Model(&conversationPO{}).
		Where("id = ? AND user_id = ? AND deleted_at IS NULL", id, userID).
		Updates(conversationFields(patch, at))
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		// ⚠️ 这里**不能**直接判定为「不存在」：把 status 改成与当前相同的值时，
		// MySQL 会因为「值没变」而报告 0 行（changed rows 为 0）。
		// 所以再确认真实可见性，否则「取消归档一个本就 active 的会话」会变成 404。
		return r.ensureVisible(ctx, userID, id)
	}
	return nil
}

// SetAutoTitle 在首条消息落库后写自动标题（仅当仍是 auto 且标题为空）。
//
// 条件落在 SQL 里而不是靠「先读后写」：并发下用户可能刚刚手工改过标题，
// 先读后写会把那个改动覆盖掉（AC-CONV-04）。
func (r *conversationRepo) SetAutoTitle(ctx context.Context, userID, id, title string, at time.Time) (bool, error) {
	res := r.data.DB.GORM.WithContext(ctx).Model(&conversationPO{}).
		Where("id = ? AND user_id = ? AND deleted_at IS NULL AND title_source = ? AND title = ''",
			id, userID, TitleSourceAuto).
		Updates(map[string]any{"title": title, "updated_at": at})
	if res.Error != nil {
		return false, res.Error
	}
	return res.RowsAffected > 0, nil
}

// ensureVisible 确认会话对该用户可见；不可见返回 biz.ErrNotFound。
func (r *conversationRepo) ensureVisible(ctx context.Context, userID, id string) error {
	var n int64
	err := r.data.DB.GORM.WithContext(ctx).Model(&conversationPO{}).
		Where("id = ? AND user_id = ? AND deleted_at IS NULL", id, userID).
		Count(&n).Error
	if err != nil {
		return err
	}
	if n == 0 {
		return biz.ErrNotFound
	}
	return nil
}

// SoftDelete 软删会话。
//
// 消息不单独置位：查询侧统一 JOIN 会话的 `deleted_at IS NULL`
// （docs/03-§6），这样「软删会话 → 消息立刻不可见」是原子的一步，
// 不存在「会话删了但消息还能查到」的中间态。
func (r *conversationRepo) SoftDelete(ctx context.Context, userID, id string, at time.Time) error {
	res := r.data.DB.GORM.WithContext(ctx).Model(&conversationPO{}).
		Where("id = ? AND user_id = ? AND deleted_at IS NULL", id, userID).
		Updates(map[string]any{"deleted_at": at, "updated_at": at})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return biz.ErrNotFound
	}
	return nil
}

// allocSeq 原子分配下一个消息序号（docs/03-§4.2）。
//
// ⚠️ MUST 在事务内调用：`LAST_INSERT_ID(expr)` 是**连接级**的，
// 换连接执行 `SELECT LAST_INSERT_ID()` 会读到别的会话的值，
// 表现为「seq 跳号/重复」，且不报任何错。
//
// 表名用常量拼接、不写字面量：这里曾经写成 `UPDATE \`conversationPO\“
// （批量改名把类型名改进了字符串里），编译与单测都发现不了，
// 只会在真跑 SQL 时报表不存在。
//
// 返回：
//   - (seq, nil)                        分配成功；
//   - (0, biz.ErrNotFound)              会话不存在 / 越权；
//   - (0, biz.ErrConversationDeleted)   会话已软删；
//   - (0, biz.ErrConversationArchived)  会话已归档；
//   - (0, err)                          其它错误
func (r *conversationRepo) allocSeq(ctx context.Context, tx *gorm.DB, userID, id string, at time.Time) (int, error) {
	res := tx.WithContext(ctx).Exec(
		"UPDATE "+quoteIdent(TableConversation)+
			" SET message_count = LAST_INSERT_ID(message_count + 1), "+
			"last_message_at = ?, updated_at = ? "+
			"WHERE id = ? AND user_id = ? AND deleted_at IS NULL AND status = ?",
		at, at, id, userID, ConvStatusActive,
	)
	if res.Error != nil {
		return 0, res.Error
	}
	if res.RowsAffected == 0 {
		// 区分三种「0 行」：不存在 / 已归档 / 已软删。
		// 都用**同事务**再查一次，保证判断与 UPDATE 基于同一可见性快照。
		var po conversationPO
		err := tx.WithContext(ctx).Select("status", "deleted_at").
			Where("id = ? AND user_id = ?", id, userID).
			Take(&po).Error
		if err != nil {
			// 查不到 = 不存在，或者不是他的（越权）→ 对外一律 404。
			return 0, translate(err)
		}
		if po.DeletedAt != nil {
			return 0, biz.ErrConversationDeleted
		}
		if po.Status != ConvStatusActive {
			return 0, biz.ErrConversationArchived
		}
		// WHERE 全中却 0 行：只可能是并发下的可见性差异，归为「不存在」。
		return 0, biz.ErrNotFound
	}

	var seq int
	if err := tx.WithContext(ctx).Raw("SELECT LAST_INSERT_ID()").Scan(&seq).Error; err != nil {
		return 0, err
	}
	if seq <= 0 {
		// 走到这里说明两条语句被分到了不同连接（事务没生效）。
		// MUST 报错而不是放行：seq=0 会被写进 `uk_msg_conv_seq`，
		// 第二条消息就会撞唯一键，表现为「莫名 500」。
		return 0, errors.New("data: LAST_INSERT_ID 返回非正数（事务/连接被换掉了？）")
	}
	return seq, nil
}

// ---- 映射（PO ↔ DO）----
//
// 转换只发生在 data 侧（规范 §三.2）：`kb_ids` 这类 JSON 列、
// `metadata` 的 nil 与 `{}` 值差异都在这里统一掉，biz 拿到的是干净的领域对象。

func toConversationPO(c *biz.Conversation) *conversationPO {
	return &conversationPO{
		ID:            c.ID,
		UserID:        c.UserID,
		Title:         c.Title,
		TitleSource:   c.TitleSource,
		Status:        c.Status,
		Model:         c.Model,
		KBIDs:         JSONList(orEmptyStrings(c.KBIDs)),
		MessageCount:  c.MessageCount,
		LastMessageAt: c.LastMessageAt,
		Pinned:        c.Pinned,
		Metadata:      NewJSONMap(c.Metadata),
		CreatedAt:     c.CreatedAt,
		UpdatedAt:     c.UpdatedAt,
		DeletedAt:     c.DeletedAt,
	}
}

func toConversationDO(po *conversationPO) *biz.Conversation {
	return &biz.Conversation{
		ID:            po.ID,
		UserID:        po.UserID,
		Title:         po.Title,
		TitleSource:   po.TitleSource,
		Status:        po.Status,
		Model:         po.Model,
		KBIDs:         po.KBIDs.OrEmpty(),
		MessageCount:  po.MessageCount,
		LastMessageAt: po.LastMessageAt,
		Pinned:        po.Pinned,
		Metadata:      po.Metadata.OrEmpty(),
		CreatedAt:     po.CreatedAt,
		UpdatedAt:     po.UpdatedAt,
		DeletedAt:     po.DeletedAt,
	}
}

// conversationFields 把已校验的补丁翻成列映射。
//
// `Model` 用三态：Present 且 Value 为 nil 要**显式写 NULL**（回到全局默认模型），
// 所以必须在 map 里放一个 nil 值 —— 「不放进 map」与「放进 nil」
// 在 GORM 里是两个完全不同的动作：前者不改，后者把列置空。
func conversationFields(p biz.ConversationPatch, at time.Time) map[string]any {
	fields := map[string]any{"updated_at": at}
	if p.Title != nil {
		fields["title"] = *p.Title
	}
	if p.TitleSource != nil {
		fields["title_source"] = *p.TitleSource
	}
	if p.Status != nil {
		fields["status"] = *p.Status
	}
	if p.Model.Present {
		if p.Model.Value == nil {
			fields["model"] = nil
		} else {
			fields["model"] = *p.Model.Value
		}
	}
	if p.KBIDs != nil {
		fields["kb_ids"] = JSONList(orEmptyStrings(*p.KBIDs))
	}
	if p.Pinned != nil {
		fields["pinned"] = *p.Pinned
	}
	return fields
}

func orEmptyStrings(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

// escapeLike 转义 LIKE 的通配符。
//
// 不转义时，用户搜 `%` 会匹配全部会话（信息泄漏的轻量版：
// 客户端能据此判断「库里有多少条」），搜 `_` 会变成单字符通配。
func escapeLike(s string) string {
	return strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(s)
}
