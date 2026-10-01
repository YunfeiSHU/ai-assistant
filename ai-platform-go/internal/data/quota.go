package data

import (
	"context"
	"time"

	"gorm.io/gorm/clause"
)

// 配额指标常量（docs/02-§5.1）。
//
// 与 `internal/biz` 的同名常量必须逐字一致（它们是 `quota_usage.metric` 的取值）；
// data 刻意不 import biz，因此改一边要同时改另一边，否则查的与写的是两行数据。
const (
	// MetricChatRequests 是对话请求数（按自然日重置）。
	MetricChatRequests = "chat_requests"
	// MetricLLMTokens 是 LLM token 用量（按自然日重置）。
	MetricLLMTokens = "llm_tokens"
	// MetricKBCount 是知识库数量（存量型）。
	MetricKBCount = "kb_count"
	// MetricDocumentsCount 是文档数量（存量型）。
	MetricDocumentsCount = "documents_count"
	// MetricStorageBytes 是存储占用字节数（存量型）。
	MetricStorageBytes = "storage_bytes"
	// MetricConcurrency 是并发请求数（瞬时量）。
	MetricConcurrency = "concurrency"
)

// AllMetrics 是全部指标（按契约顺序，供响应体稳定输出）。
var AllMetrics = []string{
	MetricChatRequests,
	MetricLLMTokens,
	MetricKBCount,
	MetricDocumentsCount,
	MetricStorageBytes,
	MetricConcurrency,
}

// PeriodCurrent 是存量型指标（kb_count / documents_count / storage_bytes）的周期值。
const PeriodCurrent = "current"

// IsDailyMetric 报告该指标是否按自然日重置。
func IsDailyMetric(metric string) bool {
	return metric == MetricChatRequests || metric == MetricLLMTokens
}

// QuotaRepo 是 `quota_usage` / `usage_record` 的仓储。
type QuotaRepo struct{ data *Data }

// NewQuotaRepo 构造仓储。
func NewQuotaRepo(d *Data) *QuotaRepo { return &QuotaRepo{data: d} }

// Get 读取某指标的权威计数；不存在返回 ErrNotFound。
func (r *QuotaRepo) Get(ctx context.Context, userID, metric, period string) (*quotaUsagePO, error) {
	var q quotaUsagePO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("user_id = ? AND metric = ? AND period = ?", userID, metric, period).
		Take(&q).Error
	if err != nil {
		return nil, translate(err)
	}
	return &q, nil
}

// ListByPeriod 列出某周期内的全部指标行。
func (r *QuotaRepo) ListByPeriod(ctx context.Context, userID, period string) ([]quotaUsagePO, error) {
	var out []quotaUsagePO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("user_id = ? AND period = ?", userID, period).
		Find(&out).Error
	return out, err
}

// Upsert 写入绝对计数（对账任务用：以 Redis 快照覆盖 MySQL）。
func (r *QuotaRepo) Upsert(ctx context.Context, userID, metric, period string, used int64, limit *int64, at time.Time) error {
	row := quotaUsagePO{
		UserID: userID, Metric: metric, Period: period,
		Used: used, LimitValue: limit, CreatedAt: at, UpdatedAt: at,
	}
	return r.data.DB.GORM.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "user_id"}, {Name: "metric"}, {Name: "period"}},
		DoUpdates: clause.Assignments(map[string]any{"used": used, "limit_value": limit, "updated_at": at}),
	}).Create(&row).Error
}

// AddDelta 原子累加计数（快路径的对账写入）。
//
// 用 `used = used + ?` 而不是先读后写：并发下先读后写会丢更新，
// 而配额丢更新的表现是「用户实际用了 100 次，台账只记 37 次」。
func (r *QuotaRepo) AddDelta(ctx context.Context, userID, metric, period string, delta int64, limit *int64, at time.Time) error {
	return r.data.DB.GORM.WithContext(ctx).Exec(
		"INSERT INTO `quota_usage` (user_id, metric, period, used, limit_value, created_at, updated_at) "+
			"VALUES (?, ?, ?, ?, ?, ?, ?) "+
			"ON DUPLICATE KEY UPDATE used = used + ?, limit_value = VALUES(limit_value), updated_at = ?",
		userID, metric, period, delta, limit, at, at, delta, at,
	).Error
}

// InsertUsageRecord 追加一条用量明细。
func (r *QuotaRepo) InsertUsageRecord(ctx context.Context, rec *usageRecordPO) error {
	return r.data.DB.GORM.WithContext(ctx).Create(rec).Error
}

// ListUsageRecords 查询用量明细（用量接口的 items 来源）。
func (r *QuotaRepo) ListUsageRecords(ctx context.Context, userID string, from, to *time.Time, metric string, limit int) ([]usageRecordPO, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	q := r.data.DB.GORM.WithContext(ctx).Model(&usageRecordPO{}).Where("user_id = ?", userID)
	if from != nil {
		q = q.Where("created_at >= ?", *from)
	}
	if to != nil {
		q = q.Where("created_at < ?", *to)
	}
	if metric != "" {
		q = q.Where("metric = ?", metric)
	}
	var out []usageRecordPO
	err := q.Order("id DESC").Limit(limit).Find(&out).Error
	return out, err
}

// SumRecords 按指标汇总区间用量（用量接口的 totals 来源）。
func (r *QuotaRepo) SumRecords(ctx context.Context, userID, metric string, from, to time.Time) (int64, error) {
	var sum *int64
	err := r.data.DB.GORM.WithContext(ctx).Model(&usageRecordPO{}).
		Select("SUM(amount)").
		Where("user_id = ? AND metric = ? AND created_at >= ? AND created_at < ?", userID, metric, from, to).
		Scan(&sum).Error
	if err != nil {
		return 0, err
	}
	if sum == nil {
		// SUM 在无行时返回 NULL（不是 0）；直接 Scan 到 int64 会得到 0，
		// 但显式处理能让「没有数据」这件事在代码里可见。
		return 0, nil
	}
	return *sum, nil
}

// PurgeRecordsOlderThan 清理超出保留期的用量明细（分批）。
//
// 删明细不丢总账：聚合值在 `quota_usage` 里（docs/05-§2.6）。
func (r *QuotaRepo) PurgeRecordsOlderThan(ctx context.Context, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 1000
	}
	res := r.data.DB.GORM.WithContext(ctx).
		Where("created_at < ?", before).
		Limit(limit).
		Delete(&usageRecordPO{})
	return res.RowsAffected, res.Error
}
