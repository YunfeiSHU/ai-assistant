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
//
// 只存 sha256 哈希（REQ-DATA-003）：明文只在响应体里出现一次。
// 因此**任何**日志/审计都不得打印 `TokenHash` 的原文来源（那是明文令牌）。
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
//
// resource_type / resource_id 用空串而非 NULL（避三值逻辑，docs/05-§2.8）；
// `Detail` 为指针表示「这一列没写」，与「写了个空 JSON」区分开。
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
//
// 动作名是**跨服务契约**：ai-platform 会按同一组字符串写这张共享表，
// 改这里等于改双方共识，必须先改 docs。
const (
	// AuditRegister 记录注册成功（新用户落库）。
	AuditRegister = "register"
	// AuditLogin 记录登录成功（含签发令牌对）。
	AuditLogin = "login"
	// AuditLoginFailed 记录登录失败；detail 只放脱敏后的邮箱，不放密码。
	AuditLoginFailed = "login_failed"
	// AuditLogout 记录单设备登出（作废指定 refresh token）。
	AuditLogout = "logout"
	// AuditLogoutAll 记录全设备登出（作废全部令牌并递增 token_version）。
	AuditLogoutAll = "logout_all"
	// AuditPasswordChange 记录改密码成功（随后旧令牌全部失效）。
	AuditPasswordChange = "password_change"
	// AuditTokenRefresh 记录刷新令牌轮换成功（旧令牌同事务作废）。
	AuditTokenRefresh = "token_refresh"
	// AuditConversationCreate 记录会话创建。
	AuditConversationCreate = "conversation_create"
	// AuditConversationDelete 记录会话删除。
	AuditConversationDelete = "conversation_delete"
	// AuditQuotaExceeded 记录一次配额拦截（资源未创建/消息未落库）。
	AuditQuotaExceeded = "quota_exceeded"
	// AuditRateLimited 记录一次限流拦截。
	AuditRateLimited = "rate_limited"
)

// TokenPair 是登录 / 刷新成功后的签发结果（docs/02-§6.1）。
//
// 它是 biz 的**结果类型**而不是响应 DTO：`User` 是领域对象，
// 「哪些字段能出厂」由 service 决定。
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
	// GetByHash 按哈希取记录；不存在返回 ErrNotFound。
	//
	// 刻意不在这里判断「是否已作废/已过期」：那是业务语义，
	// 仓储只负责「按哈希给我这一行」。
	GetByHash(ctx context.Context, hash string) (*RefreshToken, error)
	// Rotate 原子轮换：作废旧令牌并插入新令牌（REQ-AUTH-003 单次有效）。
	//
	// 旧令牌已被用过 / 已登出 / 已过期时返回 ErrTokenInvalid **且事务回滚**。
	Rotate(ctx context.Context, oldHash string, next *RefreshToken, at time.Time) error
	// RevokeByHash 作废单个令牌，返回是否命中（幂等，不命中不报错）。
	RevokeByHash(ctx context.Context, userID, hash string, at time.Time) (bool, error)
	// RevokeAll 作废某用户全部未作废令牌，返回作废条数。
	RevokeAll(ctx context.Context, userID string, at time.Time) (int64, error)
	// ListActive 列出某用户当前有效的令牌（设备列表，P2 用）。
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

// VersionInvalidator 在 `token_version` 被递增后清掉其缓存。
//
// 接口定义在消费方（biz）而不是实现方，也不直接依赖 data 的具体类型：
// 这样单测可以传 nil（表示没有缓存层）。
//
// 不传入的后果很具体：校验逻辑是「令牌的 ver == 当前版本」才通过，
// 而缓存里存的是**递增前**的版本。若不清缓存，一个 ver=1 的旧令牌
// 会与缓存里的 1 相等而继续被放行 —— 「改密码踢下线」最多失效一个 TTL。
type VersionInvalidator interface {
	Invalidate(ctx context.Context, userID string) error
}

// TokenVersionChecker 校验 JWT 的 `ver` 与用户当前 token_version 是否一致
// （docs/02-§3.3 第 ⑦ 步，REQ-AUTH-004）。
//
// 它是**安全规则**而不是传输细节，所以接口放在 biz 而不是 middleware：
// 中间件只是它的调用方（`server/middleware → biz` 方向合法）。
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
	// Metrics 是登录/刷新成败的埋点口（M6）。为 nil 时构造期换成空实现。
	//
	// 登录失败率与刷新失败率是两个**必看**的面板：前者是撞库的信号，
	// 后者是客户端体验的信号（令牌过期时间配太短时会看到它抬升）。
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

// LogoutInput 是登出请求体（两个字段都可选）。
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

// Register 创建用户（REQ-AUTH-001）。
//
// 自然幂等：同邮箱第二次返回 409 EMAIL_ALREADY_EXISTS（docs/02-§7）。
func (s *AuthService) Register(ctx context.Context, in RegisterInput, meta RequestMeta) (*User, error) {
	email := cryptox.NormalizeEmail(in.Email)
	if fields := validateRegister(in, email, s.d.Policy); len(fields) > 0 {
		return nil, errs.InvalidArgument(fields)
	}

	// 查重必须包含**已软删**的行：uk_user_email 不含 deleted_at，
	// 邮箱永久占用（REQ-DATA-009）。漏掉这一步会在 INSERT 时才撞唯一键，
	// 于是「邮箱已被占用」从 409 退化成 500。
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
//
// 「用户不存在」与「密码错误」返回**同一个** `INVALID_CREDENTIALS`，
// 且两条路径都执行一次等价的 Argon2id 计算 —— 否则响应耗时差
// 会把「这个邮箱存在吗」泄漏出去（AC-NFR-06）。
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
			// 账号不存在归 `bad_credentials` 而不是单独一档：
			// 对外两者本来就不可区分（防用户枚举），面板上也不该让
			// 猜号攻击能通过「哪条曲线在涨」区分出来。
			s.d.Metrics.LoginAttempt(MetricLoginBadCredentials)
			return nil, errs.New(errs.CodeInvalidCredentials)
		}
		return nil, wrapDB(err)
	}

	ok, verifyErr := cryptox.VerifyPassword(in.Password, u.PasswordHash)
	if verifyErr != nil {
		// 编码串损坏：这是数据问题，不能当成「密码错误」糊过去。
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
//
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
		// 已作废的令牌被再次使用有两个完全不同的成因：
		// 客户端并发刷新（无害）与令牌被窃取后重放（严重）。
		// 单看指标分不出来，但两者的**绝对量**差异很大 ——
		// 所以先把它计出来，异常时再去翻日志。
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
	// 原子轮换：作废旧哈希 + 插入新行。
	//
	// 这里能直接用 `next`（而不是先建后查）是因为 buildPair 只组装不落库 ——
	// 「先建后废」看似更安全，但插入失败时用户会同时失去两个令牌，
	// 而单次事务里「先废后建」失败会整体回滚，两者都不会出现「有旧无新」。
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

// Logout 作废刷新令牌（REQ-AUTH-004）。
//
// 语义（按契约的三种情形）：
//   - 给了 `refresh_token`：只作废该设备；
//   - `logout_all=true`：作废全部 + 递增 `token_version`（旧 access 立即失效）；
//   - 都没给：作废全部（不递增版本，access 在剩余 TTL 内仍可用，
//     靠客户端丢弃 —— 这是 docs/01-S9 明确接受的代价）。
//
// 幂等：无论命中与否都返回 nil（接口返回 204）。
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

// ChangePassword 校验旧密码后更新，并踢下线所有设备（REQ-AUTH-004）。
//
// 返回新的 `token_version`（调用方通常不需要，但便于测试断言）。
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
	// 版本缓存同理必须失效：它存的是递增前的值，而旧 access 令牌的 ver
	// 恰好等于那个值 —— 不清就会「改完密码旧令牌还能用满一个 TTL」。
	s.invalidateVersion(ctx, userID)

	s.audit(ctx, &userID, AuditPasswordChange, meta, nil)
	return version, nil
}

// invalidateVersion 尽量清掉版本缓存；失败只告警不阻断。
//
// 不阻断的理由：业务动作（改密码 / 踢下线）本身已经成功，
// 缓存失效失败只是让「安全窗口」从 0 变成最多一个 TTL —— 反过来说，
// 为了缓存失败而回滚一次已生效的密码修改，对用户是更差的体验。
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

// buildPair 只组装令牌对，**不落库**。
//
// 拆出这一步是为了让「刷新」可以在同一个事务里完成「作废旧 + 插入新」
// 而不用先插入再查回来（多一次往返，且中间态对并发可见）。
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

// audit 追加审计记录（best-effort）。
//
// 审计写失败 MUST NOT 影响业务结果：把「登录成功但审计表满」变成登录失败，
// 等于让一个次要依赖决定核心可用性。失败时记 ERROR 让告警能抓到。
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

// looksLikeEmail 是轻量的邮箱格式校验。
//
// 刻意不用「完整 RFC 5322 正则」：它的常见实现要么过度拒绝合法地址，
// 要么长得无法审查。真正验证邮箱有效性的手段只有发信确认，
// 这里只需要挡住明显的手滑输入。
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
//
// 数据库不可用是**可重试**的 503（DEPENDENCY_UNAVAILABLE），
// 而不是 500：客户端重试即可，不该被当成服务端 bug 计入错误率。
func wrapDB(err error) *errs.AppError {
	if err == nil {
		return nil
	}
	if appErr, ok := errs.As(err); ok {
		return appErr
	}
	return errs.Wrap(errs.CodeDependencyUnavailable, err)
}
