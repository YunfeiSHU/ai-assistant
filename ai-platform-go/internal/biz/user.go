package biz

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// ---- 领域对象与仓储接口 ----

// User 是用户领域对象。
// `user` 表没有 user_id 列（id 即用户 id），引用它的列宽 VARCHAR(64)（docs/05-§2.0）。
// MUST NOT 直接序列化出去：PasswordHash / TokenVersion 都在这里，对外视图由 service 决定（规范 §三.2）。
type User struct {
	ID           string
	Email        string
	Nickname     string
	PasswordHash string
	Plan         string
	Status       string
	TokenVersion int
	LastLoginAt  *time.Time
	CreatedAt    time.Time
	UpdatedAt    time.Time
	DeletedAt    *time.Time
}

// IsActive 报告用户当前是否可用（未禁用、未删除）。
func (u *User) IsActive() bool {
	return u.DeletedAt == nil && u.Status == UserStatusActive
}

// user.status 取值。
const (
	// UserStatusActive 表示账号可正常登录与使用。
	UserStatusActive = "active"
	// UserStatusDisabled 表示被封禁：登录返回 403 USER_DISABLED，且旧令牌不刷新。
	UserStatusDisabled = "disabled"
	// UserStatusDeleted 是软删标记；`user` 表另有 deleted_at 列，两者需保持一致。
	UserStatusDeleted = "deleted"
)

// UserRepo 是用户表的仓储接口（接口在 biz、实现在 data，规范 §六）。
// 实现方 MUST 把基础设施错误翻译成领域哨兵：「记录不存在」→ ErrNotFound，
// 「邮箱撞唯一键」→ ErrEmailTaken —— biz 不认识 gorm.ErrRecordNotFound 或 MySQL 1062。
type UserRepo interface {
	// Create 插入用户；邮箱已存在时返回 ErrEmailTaken。
	Create(ctx context.Context, u *User) error
	// GetByID 按主键取未删除用户；不存在返回 ErrNotFound。
	GetByID(ctx context.Context, id string) (*User, error)
	// GetByEmail 按邮箱（已归一化为小写）取未删除用户，不存在返回 ErrNotFound。
	GetByEmail(ctx context.Context, email string) (*User, error)
	// EmailExists 报告邮箱是否已被**任何**行占用（含软删行）。
	EmailExists(ctx context.Context, email string) (bool, error)
	// TouchLogin 更新最后登录时间。
	TouchLogin(ctx context.Context, id string, at time.Time) error
	// UpdateNickname 更新昵称；不存在返回 ErrNotFound。
	UpdateNickname(ctx context.Context, id, nickname string, at time.Time) error
	// ChangePassword 更新密码哈希并把 token_version 加一，返回新版本。
	ChangePassword(ctx context.Context, userID, hash string, at time.Time) (int, error)
	// BumpTokenVersion 只递增 token_version，返回新版本。
	BumpTokenVersion(ctx context.Context, userID string, at time.Time) (int, error)
	// TokenVersion 读取当前 token_version（版本缓存的**权威**来源）。
	TokenVersion(ctx context.Context, userID string) (int, error)
}

// UserDeps 是用户服务的依赖。
type UserDeps struct {
	Users UserRepo
	Clock nowFunc
	Log   *slog.Logger
}

// UserService 实现用户资料相关能力（REQ-AUTH-005）。
type UserService struct{ d UserDeps }

// NewUserService 构造用户服务。
func NewUserService(d UserDeps) *UserService {
	if d.Clock == nil {
		d.Clock = clockx.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	return &UserService{d: d}
}

// UpdateProfileInput 是 `PATCH /me` 请求体。
// 字段用指针区分「不改」（null/缺省）与「清空」（空串）；用零值判断会把两者合并。
type UpdateProfileInput struct {
	Nickname *string `json:"nickname"`
}

// Me 返回当前用户（REQ-AUTH-005）。返回领域对象而非响应视图，
// 「哪些字段可出厂」交给 service，避免将来接 gRPC 时误泄 PasswordHash。
func (s *UserService) Me(ctx context.Context, userID string) (*User, error) {
	u, err := s.d.Users.GetByID(ctx, userID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			// 令牌有效但用户已被删除：按未认证处理，客户端会去刷新/重新登录。
			return nil, errs.New(errs.CodeUnauthenticated)
		}
		return nil, wrapDB(err)
	}
	if u.Status == UserStatusDisabled {
		return nil, errs.New(errs.CodeUserDisabled)
	}
	return u, nil
}

// UpdateProfile 修改昵称。
func (s *UserService) UpdateProfile(ctx context.Context, userID string, in UpdateProfileInput) (*User, error) {
	if in.Nickname == nil {
		// 没有可改字段：直接返回当前状态（幂等，不报错）。
		return s.Me(ctx, userID)
	}
	nickname := strings.TrimSpace(*in.Nickname)
	if len([]rune(nickname)) > 64 {
		return nil, errs.InvalidArgument([]errs.FieldError{
			{Field: "nickname", Reason: "too_long", Message: "nickname 长度不能超过 64"},
		})
	}

	var at time.Time = s.d.Clock()
	if err := s.d.Users.UpdateNickname(ctx, userID, nickname, at); err != nil {
		if errors.Is(err, ErrNotFound) {
			return nil, errs.New(errs.CodeUnauthenticated)
		}
		return nil, wrapDB(err)
	}
	return s.Me(ctx, userID)
}
