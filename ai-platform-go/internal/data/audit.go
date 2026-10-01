package data

import (
	"context"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// auditRepo 实现 biz.AuditRepo（规范 §六）。
//
// 它只追加、不更新：审计记录一旦写入就是不可变的证据，
// 「改审计」比「没有审计」更糟。
type auditRepo struct{ data *Data }

// NewAuditRepo 构造审计仓储。
func NewAuditRepo(d *Data) biz.AuditRepo { return &auditRepo{data: d} }

// Write 追加一条审计记录。
//
// 调用方 MUST 把 detail 先脱敏（docs/06-§4.4）；本方法不再重复处理，
// 因为脱敏需要业务语义（哪些字段是凭据），仓储层无从判断。
func (r *auditRepo) Write(ctx context.Context, entry *biz.AuditLog) error {
	return r.data.DB.GORM.WithContext(ctx).Create(toAuditLogPO(entry)).Error
}

// ListByUser 按用户查审计（排障与自查用）。
func (r *auditRepo) ListByUser(ctx context.Context, userID string, limit int) ([]biz.AuditLog, error) {
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var pos []auditLogPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("user_id = ?", userID).
		Order("id DESC").
		Limit(limit).
		Find(&pos).Error
	if err != nil {
		return nil, err
	}
	out := make([]biz.AuditLog, 0, len(pos))
	for i := range pos {
		out = append(out, *toAuditLogDO(&pos[i]))
	}
	return out, nil
}

// DeleteOlderThan 分批清理过期审计（保留 ≥ 180 天，docs/05-§2.8）。
func (r *auditRepo) DeleteOlderThan(ctx context.Context, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 1000
	}
	res := r.data.DB.GORM.WithContext(ctx).
		Where("created_at < ?", before).
		Limit(limit).
		Delete(&auditLogPO{})
	return res.RowsAffected, res.Error
}

// ---- PO <-> 领域对象 ----

func toAuditLogPO(entry *biz.AuditLog) *auditLogPO {
	if entry == nil {
		return nil
	}
	return &auditLogPO{
		ID:           entry.ID,
		UserID:       entry.UserID,
		Action:       entry.Action,
		ResourceType: entry.ResourceType,
		ResourceID:   entry.ResourceID,
		IP:           entry.IP,
		UserAgent:    entry.UserAgent,
		Detail:       entry.Detail,
		CreatedAt:    entry.CreatedAt,
	}
}

func toAuditLogDO(po *auditLogPO) *biz.AuditLog {
	if po == nil {
		return nil
	}
	return &biz.AuditLog{
		ID:           po.ID,
		UserID:       po.UserID,
		Action:       po.Action,
		ResourceType: po.ResourceType,
		ResourceID:   po.ResourceID,
		IP:           po.IP,
		UserAgent:    po.UserAgent,
		Detail:       po.Detail,
		CreatedAt:    po.CreatedAt,
	}
}
