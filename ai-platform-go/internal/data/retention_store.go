package data

import (
	"context"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// retentionStore 把 data 层的保留期清理函数适配成 biz.RetentionRepo。
//
// 这一层是必要的而不是多余的：`retention.go` 里那几个函数接收 `*Data`
// （它们是运维动作，不属于任何业务仓储），而 biz 不能 import data
// （规范 §四）。适配器的存在让「哪些表允许被定时任务删」这个决定
// 只写在 biz 的接口上，data 侧只是把它接到现成的 SQL 上。
type retentionStore struct{ data *Data }

// NewRetentionRepo 构造保留期仓储。
func NewRetentionRepo(d *Data) biz.RetentionRepo { return &retentionStore{data: d} }

// PurgeDeletedConversations 见 biz.RetentionRepo。
func (r *retentionStore) PurgeDeletedConversations(ctx context.Context, before time.Time, limit int) (int64, error) {
	return PurgeDeletedConversations(ctx, r.data, before, limit)
}

// PurgeExpiredIdempotency 见 biz.RetentionRepo。
func (r *retentionStore) PurgeExpiredIdempotency(ctx context.Context, before time.Time, limit int) (int64, error) {
	return PurgeExpiredIdempotencyRecords(ctx, r.data, before, limit)
}

// CountOrphanMessages 见 biz.RetentionRepo。
func (r *retentionStore) CountOrphanMessages(ctx context.Context) (int64, error) {
	return CountOrphanMessages(ctx, r.data)
}

// 编译期断言。
var _ biz.RetentionRepo = (*retentionStore)(nil)
