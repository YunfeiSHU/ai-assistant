package redis

import (
	"context"
	"errors"
	"log/slog"
	"strconv"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// TokenVersionChecker 校验 JWT 的 `ver` 与用户当前 token_version 一致
// （docs/02-§3.3 第 ⑦ 步，REQ-AUTH-004）。
//
// 它实现 biz.TokenVersionChecker 与 biz.VersionInvalidator：
// 两个接口都定义在 biz（安全规则），这里只是 Redis 加速版的实现
// —— 依赖方向 data → biz（规范 §四）。
//
// 权威在 MySQL，所以本类型的**任何**失败都只能是「变慢」，
// 不能变成「放行已失效令牌」，也不能变成业务错误。
type TokenVersionChecker struct {
	versions biz.UserRepo
	kv       KV
	ttl      time.Duration
	log      *slog.Logger
}

// NewTokenVersionChecker 构造校验器；kv 为 nil 时退化为「每次都查 MySQL」。
func NewTokenVersionChecker(versions biz.UserRepo, kv KV, log *slog.Logger) *TokenVersionChecker {
	if log == nil {
		log = slog.Default()
	}
	return &TokenVersionChecker{versions: versions, kv: kv, ttl: VersionCacheTTL, log: log}
}

// CheckTokenVersion 实现 middleware.TokenVersionChecker。
//
// 不匹配时返回 UNAUTHENTICATED（不是 TOKEN_EXPIRED）：
// 后者会让客户端去刷新令牌，而刷新同样会因为版本不匹配失败 ——
// 客户端应重新登录，所以不该触发静默刷新。
func (c *TokenVersionChecker) CheckTokenVersion(ctx context.Context, userID string, ver int) error {
	current, err := c.currentVersion(ctx, userID)
	if err != nil {
		return err
	}
	if current != ver {
		return errs.New(errs.CodeUnauthenticated).
			WithCause(errors.New("data: token_version 不匹配（令牌已被改密码/登出全部设备作废）"))
	}
	return nil
}

// Invalidate 清除缓存。**递增 token_version 之后必须调用**：
//
// 缓存里存的是「旧版本」，而令牌校验是「相等才算通过」。版本从 1 涨到 2 后，
// 若缓存里还是 1，那个 ver=1 的旧 access token 会与缓存值相等而**继续被放行** ——
// 于是「改密码踢下线」最多失效 10 分钟（正好是 TTL）。
//
// 这里用 DEL 而不是 Set("0")：写 0 是错的（TTL=0 在 Redis 里是「永不过期」，
// 而 0 与任何真实版本都不相等，会把该用户永久锁死到缓存被人工清理为止）。
func (c *TokenVersionChecker) Invalidate(ctx context.Context, userID string) error {
	if c.kv == nil {
		return nil
	}
	key, err := UserVersionKey(userID)
	if err != nil {
		return err
	}
	if err := c.kv.Del(ctx, key); err != nil {
		// 删不掉不代表校验会错：TTL 会在 10 分钟内自然过期。
		// 但不能静默 —— 这是「旧令牌还能用」的窗口来源。
		c.log.WarnContext(ctx, "auth.version_cache_invalidate_failed",
			slog.String("user_id", userID), slog.String("error", err.Error()))
		return err
	}
	return nil
}

func (c *TokenVersionChecker) currentVersion(ctx context.Context, userID string) (int, error) {
	if c.kv != nil {
		if key, err := UserVersionKey(userID); err == nil {
			raw, getErr := c.kv.Get(ctx, key)
			switch {
			case getErr == nil:
				if v, convErr := strconv.Atoi(raw); convErr == nil {
					return v, nil
				}
				// 缓存里是脏值：删掉重查（不返回错误，否则一次脏写会
				// 让该用户永远 401，只能等 TTL 过期）。
				c.log.WarnContext(ctx, "auth.version_cache_corrupt", slog.String("value", raw))
			case errors.Is(getErr, ErrNotFound):
				// 正常的未命中。
			default:
				// Redis 不可用：降级为直接回源（docs/04-§9）。
				// 这里是「变慢但语义正确」的典型：权威在 MySQL，
				// 所以 Redis 挂了只会让鉴权多一次 SQL，不会放行已失效的令牌。
				c.log.WarnContext(ctx, "auth.version_cache_unavailable", slog.String("error", getErr.Error()))
			}
		}
	}

	version, err := c.versions.TokenVersion(ctx, userID)
	if err != nil {
		if errors.Is(err, biz.ErrNotFound) {
			return 0, errs.New(errs.CodeUnauthenticated)
		}
		return 0, errs.Wrap(errs.CodeDependencyUnavailable, err)
	}

	if c.kv != nil {
		if key, keyErr := UserVersionKey(userID); keyErr == nil {
			if setErr := c.kv.Set(ctx, key, strconv.Itoa(version), c.ttl); setErr != nil {
				c.log.WarnContext(ctx, "auth.version_cache_set_failed", slog.String("error", setErr.Error()))
			}
		}
	}
	return version, nil
}
