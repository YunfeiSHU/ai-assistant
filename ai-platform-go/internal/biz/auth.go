package biz

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ids"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
)

// TokenTypeBearer 是对外响应里的 token_type（固定值）。
const TokenTypeBearer = "Bearer"

// RefreshTokenBytes 是 refresh token 的随机字节数（契约：32 字节）。
const RefreshTokenBytes = 32

// ---- 领域对象 ----

// RefreshToken 是刷新令牌领域对象（`refresh_token` 表）。
// TokenHash 是 sha256 哈希（REQ-DATA-003）—— 对应的明文令牌绝不可落日志。
type RefreshToken struct {
	ID         string
	UserID     string
	TokenHash  string
	DeviceName string
	UserAgent  string
	IP         string
	ExpiresAt  time.Time
	RevokedAt  *time.Time
	CreatedAt  time.Time
}

// AuditLog 是审计记录领域对象（共享表 `audit_log`）。
// resource_* 用空串而非 NULL（避三值逻辑，docs/05-§2.8）；Detail 用指针区分「未写」与「空 JSON」。
type AuditLog struct {
	ID           int64
	UserID       *string
	Action       string
	ResourceType string
	ResourceID   string
	IP           string
	UserAgent    string
	Detail       *string
	CreatedAt    time.Time
}

// 审计动作（docs/05-§2.8 MUST 记录的动作）。
// 动作名是跨服务契约：ai-platform 按同一组字符串写这张共享表，改动前先改 docs。
const (
	// AuditRegister 记录注册成功。
	AuditRegister = "register"
	// AuditLogin 记录登录成功（含签发令牌对）。
	AuditLogin = "login"
	// AuditLoginFailed 记录登录失败；detail 只放脱敏邮箱，不放密码。
	AuditLoginFailed = "login_failed"
	// AuditLogout 记录单设备登出。
	AuditLogout = "logout"
	// AuditLogoutAll 记录全设备登出（并递增 token_version）。
	AuditLogoutAll = "logout_all"
	// AuditPasswordChange 记录改密码成功。
	AuditPasswordChange = "password_change"
	// AuditTokenRefresh 记录刷新令牌轮换成功。
	AuditTokenRefresh = "token_refresh"
	// AuditConversationCreate 记录会话创建。
	AuditConversationCreate = "conversation_create"
	// AuditConversationDelete 记录会话删除。
	AuditConversationDelete = "conversation_delete"
	// AuditQuotaExceeded 记录一次配额拦截。
	AuditQuotaExceeded = "quota_exceeded"
	// AuditRateLimited 记录一次限流拦截。
	AuditRateLimited = "rate_limited"
)

// TokenPair 是登录 / 刷新成功后的签发结果（docs/02-§6.1）。
// 它是 biz 的结果类型而非响应 DTO，字段能否出厂由 service 决定。
type TokenPair struct {
	AccessToken  string
	RefreshToken string
	TokenType    string
	ExpiresIn    int
	User         *User
}

// ---- 仓储接口（规范 §六：接口在 biz，实现在 data）----

// TokenRepo 是刷新令牌表的仓储接口。
type TokenRepo interface {
	// Create 插入一条刷新令牌记录。
	Create(ctx context.Context, rt *RefreshToken) error
	// GetByHash 按哈希取记录；不存在返回 ErrNotFound。不判断是否作废/过期（那是业务语义）。
	GetByHash(ctx context.Context, hash string) (*RefreshToken, error)
	// Rotate 原子轮换：作废旧令牌并插入新令牌（REQ-AUTH-003 单次有效）。
	// 旧令牌已用过/已登出/已过期时返回 ErrTokenInvalid 且事务回滚。
	Rotate(ctx context.Context, oldHash string, next *RefreshToken, at time.Time) error
	// RevokeByHash 作废单个令牌，返回是否命中（幂等，不命中不报错）。
	RevokeByHash(ctx context.Context, userID, hash string, at time.Time) (bool, error)
	// RevokeAll 作废某用户全部未作废令牌，返回作废条数。
	RevokeAll(ctx context.Context, userID string, at time.Time) (int64, error)
	// ListActive 列出某用户当前有效的令牌（设备列表）。
	ListActive(ctx context.Context, userID string, at time.Time) ([]RefreshToken, error)
	// DeleteExpired 分批删除已过期或很久前作废的令牌（保留期清理）。
	DeleteExpired(ctx context.Context, before time.Time, limit int) (int64, error)
}

// AuditRepo 是共享表 `audit_log` 的仓储接口（只追加，不更新）。
type AuditRepo interface {
	// Write 追加一条审计记录；调用方 MUST 先把 detail 脱敏（docs/06-§4.4）。
	Write(ctx context.Context, entry *AuditLog) error
	// ListByUser 按用户查审计（排障与自查用）。
	ListByUser(ctx context.Context, userID string, limit int) ([]AuditLog, error)
	// DeleteOlderThan 分批清理过期审计（保留 ≥ 180 天，docs/05-§2.8）。
	DeleteOlderThan(ctx context.Context, before time.Time, limit int) (int64, error)
}

// VersionInvalidator 在 `token_version` 递增后清掉其缓存（可为 nil，表示无缓存层）。
// 接口定义在消费方（biz）而非实现方，便于单测传 nil。
//
// 不清的后果：缓存里是递增前的版本，旧令牌的 ver 与之相等仍会被放行 ——
// 「改密码踢下线」最多失效一个 TTL。
type VersionInvalidator interface {
	Invalidate(ctx context.Context, userID string) error
}

// TokenVersionChecker 校验 JWT 的 `ver` 与用户当前 token_version 是否一致
// （docs/02-§3.3 第 ⑦ 步，REQ-AUTH-004）。
// 放在 biz 而非 middleware：它是安全规则，中间件只是调用方。
type TokenVersionChecker interface {
	CheckTokenVersion(ctx context.Context, userID string, ver int) error
}

// AuthDeps 是鉴权服务的依赖。
type AuthDeps struct {
	Users      UserRepo
	Tokens     TokenRepo
	Audit      AuditRepo
	Signer     *jwtx.Signer
	Argon      cryptox.Argon2Params
	Policy     PasswordPolicy
	AccessTTL  time.Duration
	RefreshTTL time.Duration
	Clock      nowFunc
	Log        *slog.Logger
	// Versions 可为 nil：表示没有版本缓存，每次校验直接回源 MySQL。
	Versions VersionInvalidator
	// Metrics 是登录/刷新成败的埋点（为 nil 时构造期换成空实现）。
	// 登录失败率（撞库信号）与刷新失败率（客户端体验信号）是两个必看面板。
	Metrics Metrics
}

// AuthService 实现注册、登录、刷新、登出、改密码（REQ-AUTH-001..005）。
type AuthService struct{ d AuthDeps }

// NewAuthService 构造鉴权服务。
func NewAuthService(d AuthDeps) *AuthService {
	if d.Clock == nil {
		d.Clock = clockx.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	d.Metrics = OrNoop(d.Metrics)
	return &AuthService{d: d}
}

func (s *AuthService) now() time.Time { return s.d.Clock() }

// ---- 输入结构 ----

// RegisterInput 是注册请求体。
type RegisterInput struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Nickname string `json:"nickname"`
}

// LoginInput 是登录请求体。
type LoginInput struct {
	Email      string `json:"email"`
	Password   string `json:"password"`
	DeviceName string `json:"device_name"`
}

// RefreshInput 是刷新请求体。
type RefreshInput struct {
	RefreshToken string `json:"refresh_token"`
}

// LogoutInput 是登出请求体（两字段可选：指定 refresh_token 或 logout_all）。
type LogoutInput struct {
	RefreshToken string `json:"refresh_token"`
	LogoutAll    bool   `json:"logout_all"`
}

// ChangePasswordInput 是改密码请求体。
type ChangePasswordInput struct {
	OldPassword string `json:"old_password"`
	NewPassword string `json:"new_password"`
}

// ---- 注册 ----

// Register 创建用户（REQ-AUTH-001）。同邮箱第二次返回 409 EMAIL_ALREADY_EXISTS（docs/02-§7）。
func (s *AuthService) Register(ctx context.Context, in RegisterInput, meta RequestMeta) (*User, error) {
	email := cryptox.NormalizeEmail(in.Email)
	if fields := validateRegister(in, email, s.d.Policy); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	// 查重须含已软删的行：uk_user_email 不含 deleted_at，邮箱永久占用（REQ-DATA-009）。
	// 漏掉这一步会在 INSERT 时才撞唯一键 → 「邮箱已被占用」从 409 退化成 500。
	exists, err := s.d.Users.EmailExists(ctx, email)
	if err != nil {
		return nil, wrapDB(err)
	}
	if exists {
		return nil, errs.New(errs.CodeEmailAlreadyExists)
	}

	hash, err := cryptox.HashPassword(in.Password, s.d.Argon)
	if err != nil {
		return nil, errs.Wrap(errs.CodeInternalError, err)
	}

	now := s.now()
	u := &User{
		ID:           ids.NewUser(),
		Email:        email,
		Nickname:     defaultNickname(in.Nickname, email),
		PasswordHash: hash,
		Plan:         "free",
		Status:       UserStatusActive,
		TokenVersion: 1,
		CreatedAt:    now,
		UpdatedAt:    now,
	}
	if err := s.d.Users.Create(ctx, u); err != nil {
		if errors.Is(err, ErrEmailTaken) {
			// 并发注册撞唯一键：语义与「先查到已存在」一致。
			return nil, errs.New(errs.CodeEmailAlreadyExists)
		}
		return nil, wrapDB(err)
	}

	s.audit(ctx, &u.ID, AuditRegister, meta, map[string]any{"email": cryptox.MaskEmail(email)})
	return u, nil
}

// ---- 登录 ----

// Login 校验凭据并签发令牌对（REQ-AUTH-002）。
// 「用户不存在」与「密码错误」同返回 INVALID_CREDENTIALS 且都做一次等价 Argon2id，
// 否则耗时差会泄漏「邮箱是否存在」（AC-NFR-06）。
func (s *AuthService) Login(ctx context.Context, in LoginInput, meta RequestMeta) (*TokenPair, error) {
	email := cryptox.NormalizeEmail(in.Email)
	if fields := validateLogin(in, email, s.d.Policy); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	u, err := s.d.Users.GetByEmail(ctx, email)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			cryptox.BurnPasswordHash(in.Password)
			s.audit(ctx, nil, AuditLoginFailed, meta, map[string]any{"email": cryptox.MaskEmail(email)})
			// 归 `bad_credentials` 而非单独一档：对外本就不可区分，
			// 面板也不该让猜号攻击据此分辨。
			s.d.Metrics.LoginAttempt(MetricLoginBadCredentials)
			return nil, errs.New(errs.CodeInvalidCredentials)
		}
		return nil, wrapDB(err)
	}

	ok, verifyErr := cryptox.VerifyPassword(in.Password, u.PasswordHash)
	if verifyErr != nil {
		// 编码串损坏是数据问题，不能当成「密码错误」。
		s.d.Log.ErrorContext(ctx, "auth.password_hash_corrupt", slog.String("user_id", u.ID))
		return nil, errs.Wrap(errs.CodeInternalError, verifyErr)
	}
	if !ok {
		s.audit(ctx, &u.ID, AuditLoginFailed, meta, nil)
		s.d.Metrics.LoginAttempt(MetricLoginBadCredentials)
		return nil, errs.New(errs.CodeInvalidCredentials)
	}
	if u.DeletedAt != nil {
		return nil, errs.New(errs.CodeInvalidCredentials)
	}
	if u.Status == UserStatusDisabled {
		s.audit(ctx, &u.ID, AuditLoginFailed, meta, map[string]any{"reason": "disabled"})
		s.d.Metrics.LoginAttempt(MetricLoginLocked)
		return nil, errs.New(errs.CodeUserDisabled)
	}

	pair, err := s.issuePair(ctx, u, meta, "")
	if err != nil {
		return nil, err
	}

	if err := s.d.Users.TouchLogin(ctx, u.ID, s.now()); err != nil {
		// 登录时间只是运营信息，写失败不该让用户登不进来。
		s.d.Log.WarnContext(ctx, "auth.touch_login_failed", slog.String("error", err.Error()))
	}

	s.audit(ctx, &u.ID, AuditLogin, meta, nil)
	s.d.Metrics.LoginAttempt(MetricLoginOK)
	return pair, nil
}

// ---- 刷新 ----

// Refresh 用 refresh token 换取新令牌对（REQ-AUTH-003）。
// 单次有效：旧令牌在同一次调用里被作废，重复使用返回 401。
func (s *AuthService) Refresh(ctx context.Context, in RefreshInput, meta RequestMeta) (*TokenPair, error) {
	if strings.TrimSpace(in.RefreshToken) == "" {
		return nil, errs.InvalidArgument([]errs.FieldError{
			{Field: "refresh_token", Reason: "required", Message: "refresh_token 不能为空"},
		})
	}
	hash := cryptox.SHA256Hex(in.RefreshToken)
	now := s.now()

	old, err := s.d.Tokens.GetByHash(ctx, hash)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			s.d.Metrics.TokenRefresh(MetricRefreshInvalid)
			return nil, errs.New(errs.CodeInvalidRefreshToken)
		}
		return nil, wrapDB(err)
	}
	if old.RevokedAt != nil || !old.ExpiresAt.After(now) {
		// 已作废令牌被再次使用可能是无害的并发刷新，也可能是令牌重放（严重）。
		// 这里分不出来，先计数，异常时再翻日志。
		s.d.Metrics.TokenRefresh(MetricRefreshInvalid)
		return nil, errs.New(errs.CodeInvalidRefreshToken)
	}

	u, err := s.d.Users.GetByID(ctx, old.UserID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return nil, errs.New(errs.CodeInvalidRefreshToken)
		}
		return nil, wrapDB(err)
	}
	if u.Status == UserStatusDisabled {
		return nil, errs.New(errs.CodeUserDisabled)
	}

	pair, next, err := s.buildPair(u, meta, old.DeviceName)
	if err != nil {
		return nil, err
	}
	// 原子轮换：作废旧哈希 + 插入新行。直接用 `next`（buildPair 只组装不落库）：
	// 单事务「先废后建」失败会整体回滚，不会出现「有旧无新」。
	if err := s.d.Tokens.Rotate(ctx, hash, next, now); err != nil {
		if errors.Is(err, ErrTokenInvalid) {
			return nil, errs.New(errs.CodeInvalidRefreshToken)
		}
		return nil, wrapDB(err)
	}

	s.audit(ctx, &u.ID, AuditTokenRefresh, meta, nil)
	s.d.Metrics.TokenRefresh(MetricRefreshOK)
	return pair, nil
}

// ---- 登出 ----

// Logout 作废刷新令牌（REQ-AUTH-004）。幂等：无论命中与否都返回 nil（接口返回 204）。
//
// 三种情形：给了 refresh_token → 只作废该设备；logout_all=true → 作废全部并递增
// token_version（旧 access 立即失效）；都没给 → 作废全部但不递增版本
// （access 在剩余 TTL 内仍可用，靠客户端丢弃，docs/01-S9 接受的代价）。
func (s *AuthService) Logout(ctx context.Context, userID string, in LogoutInput, meta RequestMeta) error {
	now := s.now()

	if in.LogoutAll {
		if _, err := s.d.Tokens.RevokeAll(ctx, userID, now); err != nil {
			return wrapDB(err)
		}
		if _, err := s.d.Users.BumpTokenVersion(ctx, userID, now); err != nil {
			if !errors.Is(err, ErrNotFound) {
				return wrapDB(err)
			}
		}
		// 版本已变，缓存必须立即失效（否则旧 access 还能再用一个 TTL）。
		s.invalidateVersion(ctx, userID)
		s.audit(ctx, &userID, AuditLogoutAll, meta, nil)
		return nil
	}

	if strings.TrimSpace(in.RefreshToken) != "" {
		if _, err := s.d.Tokens.RevokeByHash(ctx, userID, cryptox.SHA256Hex(in.RefreshToken), now); err != nil {
			return wrapDB(err)
		}
		s.audit(ctx, &userID, AuditLogout, meta, nil)
		return nil
	}

	if _, err := s.d.Tokens.RevokeAll(ctx, userID, now); err != nil {
		return wrapDB(err)
	}
	s.audit(ctx, &userID, AuditLogout, meta, nil)
	return nil
}

// ---- 改密码 ----

// ChangePassword 校验旧密码后更新，并踢下线所有设备（REQ-AUTH-004），返回新的 token_version。
func (s *AuthService) ChangePassword(ctx context.Context, userID string, in ChangePasswordInput, meta RequestMeta) (int, error) {
	u, err := s.d.Users.GetByID(ctx, userID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return 0, errs.New(errs.CodeUnauthenticated)
		}
		return 0, wrapDB(err)
	}

	ok, verifyErr := cryptox.VerifyPassword(in.OldPassword, u.PasswordHash)
	if verifyErr != nil {
		return 0, errs.Wrap(errs.CodeInternalError, verifyErr)
	}
	if !ok {
		return 0, errs.New(errs.CodeInvalidCredentials).
			WithMessage("原密码不正确").
			WithDetail("field", "old_password")
	}

	if reason, msg := ValidatePassword(in.NewPassword, s.d.Policy, u.Email); reason != "" {
		return 0, errs.InvalidArgument([]errs.FieldError{
			{Field: "new_password", Reason: reason, Message: msg},
		})
	}
	if in.NewPassword == in.OldPassword {
		return 0, errs.InvalidArgument([]errs.FieldError{
			{Field: "new_password", Reason: "unchanged", Message: "新密码不能与原密码相同"},
		})
	}

	hash, err := cryptox.HashPassword(in.NewPassword, s.d.Argon)
	if err != nil {
		return 0, errs.Wrap(errs.CodeInternalError, err)
	}

	now := s.now()
	version, err := s.d.Users.ChangePassword(ctx, userID, hash, now)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return 0, errs.New(errs.CodeUnauthenticated)
		}
		return 0, wrapDB(err)
	}
	// 改密码后旧 refresh 全部失效（token_version 已变，旧 access 也失效）。
	if _, err := s.d.Tokens.RevokeAll(ctx, userID, now); err != nil {
		s.d.Log.WarnContext(ctx, "auth.revoke_after_password_change_failed", slog.String("error", err.Error()))
	}
	// 版本缓存也必须失效：它存的是递增前的值，与旧 access 的 ver 相等，不清就还能用满一个 TTL。
	s.invalidateVersion(ctx, userID)

	s.audit(ctx, &userID, AuditPasswordChange, meta, nil)
	return version, nil
}

// invalidateVersion 尽量清掉版本缓存；失败只告警不阻断 ——
// 业务动作已成功，为缓存失败回滚一次已生效的密码修改对用户更糟。
func (s *AuthService) invalidateVersion(ctx context.Context, userID string) {
	if s.d.Versions == nil {
		return
	}
	if err := s.d.Versions.Invalidate(ctx, userID); err != nil {
		s.d.Log.WarnContext(ctx, "auth.version_cache_invalidate_failed",
			slog.String("user_id", userID), slog.String("error", err.Error()))
	}
}

// ---- 内部辅助 ----

// issuePair 签发 access token、生成 refresh token 并落库。
func (s *AuthService) issuePair(ctx context.Context, u *User, meta RequestMeta, deviceName string) (*TokenPair, error) {
	pair, next, err := s.buildPair(u, meta, deviceName)
	if err != nil {
		return nil, err
	}
	if err := s.d.Tokens.Create(ctx, next); err != nil {
		return nil, wrapDB(err)
	}
	return pair, nil
}

// buildPair 只组装令牌对，不落库 —— 好让刷新在同一事务里完成「作废旧 + 插入新」。
func (s *AuthService) buildPair(u *User, meta RequestMeta, deviceName string) (*TokenPair, *RefreshToken, error) {
	now := s.now()
	access, exp, err := s.d.Signer.Sign(u.ID, u.TokenVersion, now)
	if err != nil {
		return nil, nil, errs.Wrap(errs.CodeInternalError, err)
	}

	plain, err := cryptox.NewOpaqueToken(RefreshTokenBytes)
	if err != nil {
		return nil, nil, errs.Wrap(errs.CodeInternalError, err)
	}

	name := meta.deviceName()
	if deviceName != "" {
		name = truncateRunes(deviceName, 64)
	}
	next := &RefreshToken{
		ID:         ids.NewRefreshToken(),
		UserID:     u.ID,
		TokenHash:  cryptox.SHA256Hex(plain),
		DeviceName: name,
		UserAgent:  meta.userAgent(),
		IP:         truncateRunes(meta.IP, 45),
		ExpiresAt:  now.Add(s.d.RefreshTTL),
		CreatedAt:  now,
	}

	expiresIn := int(exp.Sub(now).Seconds())
	if expiresIn <= 0 {
		expiresIn = int(s.d.AccessTTL.Seconds())
	}
	pair := &TokenPair{
		AccessToken:  access,
		RefreshToken: plain,
		TokenType:    TokenTypeBearer,
		ExpiresIn:    expiresIn,
		User:         u,
	}
	return pair, next, nil
}

// audit 追加审计记录（best-effort）。审计写失败 MUST NOT 影响业务结果 ——
// 不该让次要依赖决定核心可用性；失败记 ERROR 供告警。
func (s *AuthService) audit(ctx context.Context, userID *string, action string, meta RequestMeta, detail map[string]any) {
	if s.d.Audit == nil {
		return
	}
	entry := &AuditLog{
		UserID:       userID,
		Action:       action,
		ResourceType: "",
		ResourceID:   "",
		IP:           truncateRunes(meta.IP, 45),
		UserAgent:    meta.userAgent(),
		CreatedAt:    s.now(),
	}
	if len(detail) > 0 {
		if raw := jsonOrEmpty(detail); raw != "" {
			entry.Detail = &raw
		}
	}
	if err := s.d.Audit.Write(ctx, entry); err != nil {
		s.d.Log.ErrorContext(ctx, "audit.write_failed",
			slog.String("action", action),
			slog.String("error", err.Error()),
		)
	}
}

// ---- 校验 ----

func validateRegister(in RegisterInput, email string, policy PasswordPolicy) []errs.FieldError {
	var fields []errs.FieldError
	if email == "" {
		fields = append(fields, errs.FieldError{Field: "email", Reason: "required", Message: "email 不能为空"})
	} else if !looksLikeEmail(email) {
		fields = append(fields, errs.FieldError{Field: "email", Reason: "invalid_format", Message: "email 格式不合法"})
	} else if len(email) > 254 {
		fields = append(fields, errs.FieldError{Field: "email", Reason: "too_long", Message: "email 长度不能超过 254"})
	}
	if reason, msg := ValidatePassword(in.Password, policy, email); reason != "" {
		fields = append(fields, errs.FieldError{Field: "password", Reason: reason, Message: msg})
	}
	if len([]rune(in.Nickname)) > 64 {
		fields = append(fields, errs.FieldError{Field: "nickname", Reason: "too_long", Message: "nickname 长度不能超过 64"})
	}
	return fields
}

func validateLogin(in LoginInput, email string, policy PasswordPolicy) []errs.FieldError {
	var fields []errs.FieldError
	if email == "" {
		fields = append(fields, errs.FieldError{Field: "email", Reason: "required", Message: "email 不能为空"})
	}
	if strings.TrimSpace(in.Password) == "" {
		fields = append(fields, errs.FieldError{Field: "password", Reason: "required", Message: "password 不能为空"})
	} else if len([]rune(in.Password)) > 128 {
		// 登录不校验强度（否则会告诉攻击者「这个密码太长/太弱」），只挡明显异常的长度。
		fields = append(fields, errs.FieldError{Field: "password", Reason: "too_long", Message: "password 长度不能超过 128"})
	}
	_ = policy
	return fields
}

// looksLikeEmail 是轻量格式校验，只挡明显的手滑输入。
// 不用完整 RFC 5322 正则：常见实现要么误拒合法地址，要么长到无法审查；真正的有效性只能靠发信。
func looksLikeEmail(s string) bool {
	at := strings.Index(s, "@")
	if at <= 0 || at != strings.LastIndex(s, "@") {
		return false
	}
	local, domain := s[:at], s[at+1:]
	if local == "" || domain == "" {
		return false
	}
	if !strings.Contains(domain, ".") {
		return false
	}
	if strings.HasPrefix(domain, ".") || strings.HasSuffix(domain, ".") ||
		strings.HasPrefix(domain, "-") || strings.HasSuffix(domain, "-") {
		return false
	}
	for _, r := range s {
		if r == ' ' || r == '\t' || r == '\n' || r == '\r' {
			return false
		}
	}
	return true
}

// defaultNickname 在未提供昵称时用邮箱前缀兜底。
func defaultNickname(nickname, email string) string {
	if strings.TrimSpace(nickname) != "" {
		return truncateRunes(strings.TrimSpace(nickname), 64)
	}
	local := email
	if i := strings.Index(email, "@"); i > 0 {
		local = email[:i]
	}
	return truncateRunes(local, 64)
}

// wrapDB 把仓储层错误归一化成对外错误。
// 数据库不可用给可重试的 503（DEPENDENCY_UNAVAILABLE）而非 500 —— 客户端重试即可，
// 不该计入服务端错误率。
func wrapDB(err error) *errs.AppError {
	if err == nil {
		return nil
	}
	if appErr, ok := errs.As(err); ok {
		return appErr
	}
	return errs.Wrap(errs.CodeDependencyUnavailable, err)
}
