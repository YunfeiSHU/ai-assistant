package biz

import "context"

// MessageSearchHit 是会话正文检索的一条命中（B-10）。
type MessageSearchHit struct {
	ConversationID string
	Title          string
	MessageID      string
	Role           string
	Snippet        string
	CreatedAt      string
}

// MessageSearchRepo 检索当前用户可见的消息正文。
// 先做 MySQL LIKE 的最小闭环；接口形状不随 FULLTEXT/embedding 替换而变化。
type MessageSearchRepo interface {
	SearchMessages(ctx context.Context, userID, query string, limit int) ([]MessageSearchHit, error)
}
