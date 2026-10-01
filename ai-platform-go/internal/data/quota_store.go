// 本文件把 `internal/data/quota.go` 的 PO 世界翻译成 biz 的领域世界。
//
// 分两层而不合并：`QuotaRepo` 的签名与 SQL 一一对应（列名直接当参数），对账与迁移脚本可直接用；
// `QuotaStore` 只暴露 biz 需要的语义 —— biz 不应该知道有 `limit_value` 这一列。
//
// `ListUsersWithPeriod` 的 SQL 同时出现 `metric IN (...)` 与 `period IN (...)`，
// 是因为两类用户的判据不同：存量指标挂在 `current` 周期，每日指标挂在 `YYYY-MM-DD`。
package data

import (
	"context"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// QuotaStore 实现 biz.QuotaRepo。
type QuotaStore struct {
	repo *QuotaRepo
}

// NewQuotaStore 构造配额台账适配器。
func NewQuotaStore(d *Data) *QuotaStore { return &QuotaStore{repo: NewQuotaRepo(d)} }

// Get 见 biz.QuotaRepo。
func (s *QuotaStore) Get(ctx context.Context, userID, metric, period string) (*biz.QuotaRow, error) {
	po, err := s.repo.Get(ctx, userID, metric, period)
	if err != nil {
		return nil, err
	}
	return toQuotaRow(po), nil
}

// ListByPeriod 见 biz.QuotaRepo。
func (s *QuotaStore) ListByPeriod(ctx context.Context, userID, period string) ([]biz.QuotaRow, error) {
	pos, err := s.repo.ListByPeriod(ctx, userID, period)
	if err != nil {
		return nil, err
	}
	out := make([]biz.QuotaRow, 0, len(pos))
	for i := range pos {
		out = append(out, *toQuotaRow(&pos[i]))
	}
	return out, nil
}

// Upsert 见 biz.QuotaRepo。
// `Limit` 为 nil 时不覆盖库里已有的 limit_value：limit 来自套餐配置，对账只负责 `used`，
// 写成 NULL 会把套餐信息抹掉（在下一次请求写回前，用户看到的是「无上限」）。
func (s *QuotaStore) Upsert(ctx context.Context, row biz.QuotaRow) error {
	repo := s.repo
	if row.Limit == nil {
		return repo.data.DB.GORM.WithContext(ctx).Exec(
			"INSERT INTO `quota_usage` (user_id, metric, period, used, created_at, updated_at) "+
				"VALUES (?, ?, ?, ?, ?, ?) "+
				"ON DUPLICATE KEY UPDATE used = VALUES(used), updated_at = VALUES(updated_at)",
			row.UserID, row.Metric, row.Period, row.Used, row.UpdatedAt, row.UpdatedAt,
		).Error
	}
	return repo.Upsert(ctx, row.UserID, row.Metric, row.Period, row.Used, row.Limit, row.UpdatedAt)
}

// InsertUsage 见 biz.QuotaRepo。
func (s *QuotaStore) InsertUsage(ctx context.Context, entry *biz.UsageEntry) error {
	po := &usageRecordPO{
		UserID:    entry.UserID,
		Metric:    entry.Metric,
		Amount:    entry.Amount,
		CreatedAt: entry.CreatedAt,
	}
	if entry.Ref.ConversationID != "" {
		v := entry.Ref.ConversationID
		po.ConversationID = &v
	}
	if entry.Ref.MessageID != "" {
		v := entry.Ref.MessageID
		po.MessageID = &v
	}
	if entry.Ref.TraceID != "" {
		v := entry.Ref.TraceID
		po.TraceID = &v
	}
	return s.repo.InsertUsageRecord(ctx, po)
}

// ListUsage 见 biz.QuotaRepo。
func (s *QuotaStore) ListUsage(ctx context.Context, userID string, from, to time.Time, metric string, limit int) ([]biz.UsageEntry, error) {
	pos, err := s.repo.ListUsageRecords(ctx, userID, &from, &to, metric, limit)
	if err != nil {
		return nil, err
	}
	out := make([]biz.UsageEntry, 0, len(pos))
	for i := range pos {
		po := &pos[i]
		out = append(out, biz.UsageEntry{
			ID:        po.ID,
			UserID:    po.UserID,
			Metric:    po.Metric,
			Amount:    po.Amount,
			Ref:       derefRef(po.ConversationID, po.MessageID, po.TraceID),
			CreatedAt: po.CreatedAt,
		})
	}
	return out, nil
}

// SumUsage 见 biz.QuotaRepo。
func (s *QuotaStore) SumUsage(ctx context.Context, userID string, from, to time.Time, metric string) (int64, error) {
	return s.repo.SumRecords(ctx, userID, metric, from, to)
}

// ListUsersWithPeriod 见 biz.QuotaRepo。
// 只取「台账里存在该周期行」的用户而不是全表扫 users：从未发起请求的用户没有配额行，
// 也不需要被重建（缺行时用套餐默认上限，Redis 无键 == 用量 0，语义一致）。
func (s *QuotaStore) ListUsersWithPeriod(ctx context.Context, period string, limit int) ([]string, error) {
	if limit <= 0 {
		limit = 1000
	}
	var out []string
	err := s.repo.data.DB.GORM.WithContext(ctx).Model(&quotaUsagePO{}).
		Where("period = ?", period).
		Distinct().
		Order("user_id ASC").
		Limit(limit).
		Pluck("user_id", &out).Error
	return out, err
}

// PurgeUsageBefore 见 biz.QuotaRepo。
func (s *QuotaStore) PurgeUsageBefore(ctx context.Context, before time.Time, limit int) (int64, error) {
	return s.repo.PurgeRecordsOlderThan(ctx, before, limit)
}

func toQuotaRow(po *quotaUsagePO) *biz.QuotaRow {
	if po == nil {
		return nil
	}
	return &biz.QuotaRow{
		UserID:    po.UserID,
		Metric:    po.Metric,
		Period:    po.Period,
		Used:      po.Used,
		Limit:     po.LimitValue,
		UpdatedAt: po.UpdatedAt,
	}
}

func derefRef(conv, msg, trace *string) biz.UsageRef {
	var ref biz.UsageRef
	if conv != nil {
		ref.ConversationID = *conv
	}
	if msg != nil {
		ref.MessageID = *msg
	}
	if trace != nil {
		ref.TraceID = *trace
	}
	return ref
}

// 编译期断言。
var _ biz.QuotaRepo = (*QuotaStore)(nil)
