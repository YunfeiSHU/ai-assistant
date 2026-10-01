package data

import (
	"context"
	"time"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// tokenRepo 实现 biz.TokenRepo（规范 §六）。
type tokenRepo struct{ data *Data }

// NewTokenRepo 构造刷新令牌仓储。
func NewTokenRepo(d *Data) biz.TokenRepo { return &tokenRepo{data: d} }

// Create 插入一条刷新令牌记录。
func (r *tokenRepo) Create(ctx context.Context, rt *biz.RefreshToken) error {
	return r.data.DB.GORM.WithContext(ctx).Create(toRefreshTokenPO(rt)).Error
}

// GetByHash 按哈希取记录（不做有效性判断，由 biz 决定语义）。
func (r *tokenRepo) GetByHash(ctx context.Context, hash string) (*biz.RefreshToken, error) {
	var po refreshTokenPO
	err := r.data.DB.GORM.WithContext(ctx).Where("token_hash = ?", hash).Take(&po).Error
	if err != nil {
		return nil, translate(err)
	}
	return toRefreshTokenDO(&po), nil
}

// Rotate 原子轮换：作废旧令牌并插入新令牌（REQ-AUTH-003 单次有效）。
// 旧令牌已被用过 / 已登出 / 已过期时 RowsAffected == 0 → 返回 biz.ErrTokenInvalid 且事务回滚
// （不会留下新令牌）。
// 判定条件必须写在 WHERE 里而不是先查后改：先查后改之间的窗口会让同一令牌并发刷新两次都通过，
// 最终签发两组令牌（单次有效被破坏）。
func (r *tokenRepo) Rotate(ctx context.Context, oldHash string, next *biz.RefreshToken, at time.Time) error {
	return r.data.DB.InTx(ctx, func(tx *gorm.DB) error {
		res := tx.Model(&refreshTokenPO{}).
			Where("token_hash = ? AND revoked_at IS NULL AND expires_at > ?", oldHash, at).
			Update("revoked_at", at)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return biz.ErrTokenInvalid
		}
		return tx.Create(toRefreshTokenPO(next)).Error
	})
}

// RevokeByHash 作废单个令牌（普通登出）。
// 幂等：令牌不存在或已作废时返回 (false, nil)，调用方仍然回 204。
// ⚠️ 必须带上 userID 条件：否则拿到别人的 refresh 串（比如从日志里）就能把对方踢下线。
// 返回 false 时调用方无法区分「不存在」与「不属于你」，这正是我们想要的（不提供探测能力）。
func (r *tokenRepo) RevokeByHash(ctx context.Context, userID, hash string, at time.Time) (bool, error) {
	res := r.data.DB.GORM.WithContext(ctx).Model(&refreshTokenPO{}).
		Where("token_hash = ? AND user_id = ? AND revoked_at IS NULL", hash, userID).
		Update("revoked_at", at)
	if res.Error != nil {
		return false, res.Error
	}
	return res.RowsAffected > 0, nil
}

// RevokeAll 作废某用户全部未作废令牌，返回作废条数（logout_all）。
func (r *tokenRepo) RevokeAll(ctx context.Context, userID string, at time.Time) (int64, error) {
	res := r.data.DB.GORM.WithContext(ctx).Model(&refreshTokenPO{}).
		Where("user_id = ? AND revoked_at IS NULL", userID).
		Update("revoked_at", at)
	return res.RowsAffected, res.Error
}

// ListActive 列出某用户当前有效的令牌（设备列表，P2 用）。
func (r *tokenRepo) ListActive(ctx context.Context, userID string, at time.Time) ([]biz.RefreshToken, error) {
	var pos []refreshTokenPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("user_id = ? AND revoked_at IS NULL AND expires_at > ?", userID, at).
		Order("created_at DESC").
		Find(&pos).Error
	if err != nil {
		return nil, err
	}
	out := make([]biz.RefreshToken, 0, len(pos))
	for i := range pos {
		out = append(out, *toRefreshTokenDO(&pos[i]))
	}
	return out, nil
}

// DeleteExpired 分批删除已过期或很久前作废的令牌（保留期清理）。
func (r *tokenRepo) DeleteExpired(ctx context.Context, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 1000
	}
	// 先取主键再删：MySQL 的 DELETE ... LIMIT 只支持简单条件，分批删除能避免长事务（docs/05-§4.3）。
	var ids []string
	err := r.data.DB.GORM.WithContext(ctx).Model(&refreshTokenPO{}).
		Select("id").
		Where("expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)", before, before).
		Limit(limit).
		Pluck("id", &ids).Error
	if err != nil || len(ids) == 0 {
		return 0, err
	}
	res := r.data.DB.GORM.WithContext(ctx).Where("id IN ?", ids).Delete(&refreshTokenPO{})
	return res.RowsAffected, res.Error
}

// ---- PO <-> 领域对象 ----

func toRefreshTokenPO(rt *biz.RefreshToken) *refreshTokenPO {
	if rt == nil {
		return nil
	}
	return &refreshTokenPO{
		ID:         rt.ID,
		UserID:     rt.UserID,
		TokenHash:  rt.TokenHash,
		DeviceName: rt.DeviceName,
		UserAgent:  rt.UserAgent,
		IP:         rt.IP,
		ExpiresAt:  rt.ExpiresAt,
		RevokedAt:  rt.RevokedAt,
		CreatedAt:  rt.CreatedAt,
	}
}

func toRefreshTokenDO(po *refreshTokenPO) *biz.RefreshToken {
	if po == nil {
		return nil
	}
	return &biz.RefreshToken{
		ID:         po.ID,
		UserID:     po.UserID,
		TokenHash:  po.TokenHash,
		DeviceName: po.DeviceName,
		UserAgent:  po.UserAgent,
		IP:         po.IP,
		ExpiresAt:  po.ExpiresAt,
		RevokedAt:  po.RevokedAt,
		CreatedAt:  po.CreatedAt,
	}
}
