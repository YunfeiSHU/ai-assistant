package data

import (
	"context"
	"time"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// userRepo 实现 biz.UserRepo（规范 §六：实现在 data，实现类型不导出）。
// 只依赖 Data 而不是具体的 *gorm.DB：引擎换代不需要改仓储签名，单测里也能整体替换。
type userRepo struct{ data *Data }

// NewUserRepo 构造用户仓储。
// 返回 biz.UserRepo 而不是 *userRepo：调用方只应看到接口，否则会把实现细节泄露给 main。
func NewUserRepo(d *Data) biz.UserRepo { return &userRepo{data: d} }

// Create 插入用户；邮箱已存在时返回 biz.ErrEmailTaken。
func (r *userRepo) Create(ctx context.Context, u *biz.User) error {
	err := r.data.DB.GORM.WithContext(ctx).Create(toUserPO(u)).Error
	if isDuplicate(err) {
		return biz.ErrEmailTaken
	}
	return err
}

// GetByID 按主键取未删除用户。
func (r *userRepo) GetByID(ctx context.Context, id string) (*biz.User, error) {
	var po userPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("id = ? AND deleted_at IS NULL", id).
		Take(&po).Error
	if err != nil {
		return nil, translate(err)
	}
	return toUserDO(&po), nil
}

// GetByEmail 按邮箱（小写）取未删除用户，供登录使用。
func (r *userRepo) GetByEmail(ctx context.Context, email string) (*biz.User, error) {
	var po userPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("email = ? AND deleted_at IS NULL", email).
		Take(&po).Error
	if err != nil {
		return nil, translate(err)
	}
	return toUserDO(&po), nil
}

// EmailExists 报告邮箱是否已被任何行占用（含软删行）。
// 刻意不过滤 `deleted_at`：`uk_user_email` 不含 deleted_at，已注销用户的邮箱永久占用
// （REQ-DATA-009）。注册必须用本方法查重，否则「软删后重新注册」会在 INSERT 时才撞唯一键，
// 错误从 409 退化成 500。
func (r *userRepo) EmailExists(ctx context.Context, email string) (bool, error) {
	var count int64
	err := r.data.DB.GORM.WithContext(ctx).Model(&userPO{}).
		Where("email = ?", email).
		Count(&count).Error
	if err != nil {
		return false, err
	}
	return count > 0, nil
}

// TouchLogin 记录登录时间。
func (r *userRepo) TouchLogin(ctx context.Context, id string, at time.Time) error {
	return r.data.DB.GORM.WithContext(ctx).Model(&userPO{}).
		Where("id = ? AND deleted_at IS NULL", id).
		Updates(map[string]any{"last_login_at": at, "updated_at": at}).Error
}

// UpdateNickname 更新昵称。
func (r *userRepo) UpdateNickname(ctx context.Context, id, nickname string, at time.Time) error {
	res := r.data.DB.GORM.WithContext(ctx).Model(&userPO{}).
		Where("id = ? AND deleted_at IS NULL", id).
		Updates(map[string]any{"nickname": nickname, "updated_at": at})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return biz.ErrNotFound
	}
	return nil
}

// ChangePassword 更新密码哈希并递增 token_version，返回新的版本号。
// 用 `LAST_INSERT_ID(token_version + 1)` 单次往返拿到新值（同 docs/03-§4.2 的 seq 技巧）：
// 先 UPDATE 再 SELECT 有并发竞态（两次改密码可能拿到同一个版本号）。
func (r *userRepo) ChangePassword(ctx context.Context, userID, hash string, at time.Time) (int, error) {
	var version int
	err := r.data.DB.InTx(ctx, func(tx *gorm.DB) error {
		res := tx.Exec(
			"UPDATE `user` SET password_hash = ?, token_version = LAST_INSERT_ID(token_version + 1), updated_at = ? "+
				"WHERE id = ? AND deleted_at IS NULL",
			hash, at, userID,
		)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return biz.ErrNotFound
		}
		return tx.Raw("SELECT LAST_INSERT_ID()").Scan(&version).Error
	})
	if err != nil {
		return 0, err
	}
	return version, nil
}

// BumpTokenVersion 只递增 token_version（踢下线所有设备），返回新版本号。
func (r *userRepo) BumpTokenVersion(ctx context.Context, userID string, at time.Time) (int, error) {
	var version int
	err := r.data.DB.InTx(ctx, func(tx *gorm.DB) error {
		res := tx.Exec(
			"UPDATE `user` SET token_version = LAST_INSERT_ID(token_version + 1), updated_at = ? "+
				"WHERE id = ? AND deleted_at IS NULL",
			at, userID,
		)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return biz.ErrNotFound
		}
		return tx.Raw("SELECT LAST_INSERT_ID()").Scan(&version).Error
	})
	if err != nil {
		return 0, err
	}
	return version, nil
}

// TokenVersion 读取当前 token_version（JWT 的 `ver` 校验用）。
// 用 Take 而不是 Scan：Scan 在无行时会把版本读成 0 且不报错，那样「用户不存在」
// 与「version=0」同形，鉴权会从「拒绍」退化成「放行」。
func (r *userRepo) TokenVersion(ctx context.Context, userID string) (int, error) {
	var po userPO
	err := r.data.DB.GORM.WithContext(ctx).
		Select("token_version").
		Where("id = ? AND deleted_at IS NULL", userID).
		Take(&po).Error
	if err != nil {
		return 0, translate(err)
	}
	return po.TokenVersion, nil
}

// ---- PO <-> 领域对象 ----
//
// 刻意写得很笨（逐字段赋值）：一旦用 JSON 往返或反射做批量拷贝，
// `biz.User` 新增字段时会静默丢失（表现为「加了个字段但永远是零值」）。

func toUserPO(u *biz.User) *userPO {
	if u == nil {
		return nil
	}
	return &userPO{
		ID:           u.ID,
		Email:        u.Email,
		Nickname:     u.Nickname,
		PasswordHash: u.PasswordHash,
		Plan:         u.Plan,
		Status:       u.Status,
		TokenVersion: u.TokenVersion,
		LastLoginAt:  u.LastLoginAt,
		CreatedAt:    u.CreatedAt,
		UpdatedAt:    u.UpdatedAt,
		DeletedAt:    u.DeletedAt,
	}
}

func toUserDO(po *userPO) *biz.User {
	if po == nil {
		return nil
	}
	return &biz.User{
		ID:           po.ID,
		Email:        po.Email,
		Nickname:     po.Nickname,
		PasswordHash: po.PasswordHash,
		Plan:         po.Plan,
		Status:       po.Status,
		TokenVersion: po.TokenVersion,
		LastLoginAt:  po.LastLoginAt,
		CreatedAt:    po.CreatedAt,
		UpdatedAt:    po.UpdatedAt,
		DeletedAt:    po.DeletedAt,
	}
}
