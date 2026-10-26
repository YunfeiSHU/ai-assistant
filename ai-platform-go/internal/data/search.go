package data

import (
	"context"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

type messageSearchRepo struct{ data *Data }

// NewMessageSearchRepo 构造消息检索仓储。
func NewMessageSearchRepo(d *Data) biz.MessageSearchRepo { return &messageSearchRepo{data: d} }

// SearchMessages 用 LIKE 做最小闭环：转义 `%`/`_`，按 seq 倒序，最多 50 条。
func (r *messageSearchRepo) SearchMessages(
	ctx context.Context, userID, query string, limit int,
) ([]biz.MessageSearchHit, error) {
	if limit <= 0 || limit > 50 {
		limit = 20
	}
	escaped := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(query)
	sql := "SELECT m.conversation_id, c.title, m.id AS message_id, m.role, " +
		"SUBSTR(m.content, 1, 200) AS snippet, m.created_at " +
		"FROM " + quoteIdent(TableMessage) + " AS m " +
		"JOIN " + quoteIdent(TableConversation) + " AS c ON c.id = m.conversation_id " +
		"WHERE m.user_id = ? AND c.user_id = ? AND c.deleted_at IS NULL " +
		"AND m.content LIKE ? ESCAPE '\\\\' " +
		"ORDER BY m.seq DESC LIMIT ?"
	var rows []struct {
		ConversationID string    `gorm:"column:conversation_id"`
		Title          string    `gorm:"column:title"`
		MessageID      string    `gorm:"column:message_id"`
		Role           string    `gorm:"column:role"`
		Snippet        string    `gorm:"column:snippet"`
		CreatedAt      time.Time `gorm:"column:created_at"`
	}
	if err := r.data.DB.GORM.WithContext(ctx).Raw(
		sql, userID, userID, "%"+escaped+"%", limit,
	).Scan(&rows).Error; err != nil {
		return nil, err
	}
	out := make([]biz.MessageSearchHit, 0, len(rows))
	for _, row := range rows {
		out = append(out, biz.MessageSearchHit{
			ConversationID: row.ConversationID,
			Title:          row.Title,
			MessageID:      row.MessageID,
			Role:           row.Role,
			Snippet:        row.Snippet,
			CreatedAt:      clockx.Format(row.CreatedAt),
		})
	}
	return out, nil
}
