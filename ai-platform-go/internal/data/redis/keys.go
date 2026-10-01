package redis

import (
	"errors"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// KeyPrefix 是网关所有 Redis Key 的统一前缀（docs/05-§3，接缝 J8）。
// MUST NOT 触碰 ai-platform 的前缀（`ctx:` / `lock:` / `summary:` / `task:` / `cancel:` /
// `embed:` / `dedupe:` / `rate:` / `retry:zset`）：两个服务共用同一个 Redis 实例，前缀撞了就是互相覆盖。
const KeyPrefix = "gw:"

// ErrUnsafeKeyPart 表示要拼进 Key 的值不满足安全字符集。
var ErrUnsafeKeyPart = errors.New("data: Redis Key 片段包含非法字符")

// safeKeyPart 是允许出现在 Key 里的字符（docs/05-§3 强制要求 2）。
// 这不是「防御性编程」：Key 片段里带 `\r\n` 可以伪造出额外的 Redis 命令，超长字符串则会造成 Key 爆炸。
var safeKeyPart = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,128}$`)

// IsSafeKeyPart 报告 s 是否可以安全地拼进 Redis Key。
func IsSafeKeyPart(s string) bool { return safeKeyPart.MatchString(s) }

func sanitizePath(path string) string {
	return strings.NewReplacer("/", "_", ":", "_", "?", "_", "&", "_").Replace(path)
}

// UserVersionKey 返回 `gw:user:ver:{user_id}`（token_version 缓存）。
func UserVersionKey(userID string) (string, error) {
	if !IsSafeKeyPart(userID) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "user:ver:" + userID, nil
}

// QuotaKey 返回 `gw:quota:{user}:{metric}:{period}`（配额计数）。
func QuotaKey(userID, metric, period string) (string, error) {
	if !IsSafeKeyPart(userID) || !IsSafeKeyPart(metric) || !IsSafeKeyPart(period) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "quota:" + userID + ":" + metric + ":" + period, nil
}

// ConcurrencyKey 返回 `gw:cc:{user_id}`（并发对话计数）。
func ConcurrencyKey(userID string) (string, error) {
	if !IsSafeKeyPart(userID) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "cc:" + userID, nil
}

// QuotaDirtyKey 返回 `gw:quota:dirty`（「用量已变、待回写 MySQL」的用户集合）。
//
// 配额计数以 Redis 为准、MySQL 为准时快照（docs/05-§3），两者之间必然有时差
// （预扣后请求可能失败、进程可能被 kill），所以需要一份「谁脏了」的名单供对账落快照。
// 用 SET 而不是 ZSET：不需要顺序也不需要按时间裁剪，每轮对账把处理成功的成员 `SREM` 即可 ——
// 它是待办集合，不是时间线。
const QuotaDirtyKeyName = KeyPrefix + "quota:dirty"

// QuotaDirtyKey 返回脏用户集合的 Key（无需入参）。
func QuotaDirtyKey() string { return QuotaDirtyKeyName }

// RateWindowKey 返回 `gw:rate:{scope}:{id}:{bucket}`（固定窗口限流计数）。
// bucket 是窗口起点的 unix 秒；拼进 Key 而不是用「一个键 + 递增」的好处是窗口切换
// 由 Key 名天然完成，不会出现「上一窗口的残留 TTL 影响下一窗口」的经典 bug。
func RateWindowKey(scope, id string, bucket time.Time) (string, error) {
	base, err := RateKey(scope, id)
	if err != nil {
		return "", err
	}
	return base + ":" + strconv.FormatInt(bucket.Unix(), 10), nil
}

// RateKey 返回 `gw:rate:{scope}:{id}`（限流计数）。
func RateKey(scope, id string) (string, error) {
	if !IsSafeKeyPart(scope) || !IsSafeKeyPart(id) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "rate:" + scope + ":" + id, nil
}

// IdemKey 返回 `gw:idem:{user}:{path}:{key}`（幂等响应快照）。
// 路径里的 `/` 替换成 `_`：它本身安全（只用于分段），但混在冒号分隔的 Key 里会让前缀扫描难以阅读。
func IdemKey(userID, path, idemKey string) (string, error) {
	safePath := sanitizePath(path)
	if !IsSafeKeyPart(userID) || !IsSafeKeyPart(safePath) || !IsSafeKeyPart(idemKey) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "idem:" + userID + ":" + safePath + ":" + idemKey, nil
}

// ConvListCacheKey 返回 `gw:conv:list:{user_id}`（会话列表只读短缓存）。
func ConvListCacheKey(userID string) (string, error) {
	if !IsSafeKeyPart(userID) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "conv:list:" + userID, nil
}

// KBMetaCacheKey 返回 `gw:kbmeta:{user_id}`（AI 侧 KB 元数据只读缓存）。
func KBMetaCacheKey(userID string) (string, error) {
	if !IsSafeKeyPart(userID) {
		return "", ErrUnsafeKeyPart
	}
	return KeyPrefix + "kbmeta:" + userID, nil
}

// CircuitBreakerKey 返回 `gw:cb:ai`（熔断状态标记）。
func CircuitBreakerKey() string { return KeyPrefix + "cb:ai" }

// VersionCacheTTL 是 token_version 缓存的 TTL（docs/05-§3：10 分钟）。
//
// 有 TTL 是刻意的：Redis 只是加速器，不能让缓存成为「第二份权威」——
// 用户改密码后，即使忘了主动失效缓存，最多 10 分钟也会回源拿到新版本。
const VersionCacheTTL = 10 * time.Minute

// CacheTTLShort 是只读短缓存的 TTL（docs/05-§3：≤ 30s）。
const CacheTTLShort = 30 * time.Second
