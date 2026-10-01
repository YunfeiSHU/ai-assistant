// 本文件实现配额与限流的**原子计数**（docs/02-§5.2 / §5.3）。
//
// 两个后端、一套语义：
//
//   - Redis 是主路径，所有操作都是原子的（`Reserve` 用 Lua 做「比较 + 自增」）；
//   - Redis 不可用时降级到**进程内**计数（docs/02-§5.3：「MUST 降级为本地兜底
//     限额，MUST NOT 全部放行」）。
//
// 为什么 `Reserve` 必须是一个原子操作而不是「先 Get 再 Set」：
// 并发的两个请求会同时读到 `used=limit-1` 而双双通过。少发一次的影响很小，
// 但同一个形状用在 `storage_bytes` 上就是「两个上传同时挤进最后 10MB」，
// 而配额超发的责任无法事后追回。
package redis

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// reserveScript 是「不超过上限则自增」的原子脚本。
//
// 返回 {当前用量, 是否放行}：
//
//   - 先读当前值（`GET`），不足则直接返回不放行 —— **不改任何状态**，
//     因此被拒绝的请求不会把自己的失败也记进用量（否则用户会被
//     「被拒绝的次数」越推越远，永远无法恢复）；
//   - 放行时 `INCRBY`，并且**只在键是新建的**（返回值恰好等于 delta）
//     时设置 TTL。这样「反复调用的热键」不会被一直续命而变成永久键。
//
// KEYS[1] = 计数键，KEYS[2] = 脏用户集合（供对账，见 biz.QuotaService.Reconcile）
// ARGV[1] = delta，ARGV[2] = limit，ARGV[3] = TTL 毫秒，ARGV[4] = user_id
const reserveScript = `
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local delta = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
if limit > 0 and cur + delta > limit then
  return {cur, 0}
end
local v = redis.call('INCRBY', KEYS[1], delta)
if v == delta then
  local ttl = tonumber(ARGV[3])
  if ttl and ttl > 0 then
    redis.call('PEXPIRE', KEYS[1], ttl)
  end
end
if KEYS[2] ~= '' and ARGV[4] ~= '' then
  redis.call('SADD', KEYS[2], ARGV[4])
  redis.call('PEXPIRE', KEYS[2], 86400000)
end
return {v, 1}
`

// addScript 是「无条件自增（可为负）」，同样只在新键时设 TTL。
// KEYS[1] = 计数键，KEYS[2] = 脏用户集合
// ARGV[1] = delta，ARGV[2] = TTL 毫秒，ARGV[3] = user_id
const addScript = `
local v = redis.call('INCRBY', KEYS[1], tonumber(ARGV[1]))
if v == tonumber(ARGV[1]) then
  local ttl = tonumber(ARGV[2])
  if ttl and ttl > 0 then
    redis.call('PEXPIRE', KEYS[1], ttl)
  end
end
if KEYS[2] ~= '' and ARGV[3] ~= '' then
  redis.call('SADD', KEYS[2], ARGV[3])
  redis.call('PEXPIRE', KEYS[2], 86400000)
end
return v
`

// ---- Redis 实现 ----

// RedisQuotaCounter 用 Redis 实现配额计数。
type RedisQuotaCounter struct {
	store *Store
}

// NewRedisQuotaCounter 构造 Redis 计数器。
func NewRedisQuotaCounter(s *Store) *RedisQuotaCounter { return &RedisQuotaCounter{store: s} }

// Reserve 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) Reserve(ctx context.Context, userID, metric, period string, delta, limit int64, ttl time.Duration) (int64, bool, error) {
	key, err := QuotaKey(userID, metric, period)
	if err != nil {
		return 0, false, err
	}
	raw, err := c.evalNumbers(ctx, reserveScript, []string{key, QuotaDirtyKey()},
		delta, limit, ttl.Milliseconds(), userID)
	if err != nil {
		return 0, false, err
	}
	if len(raw) < 2 {
		return 0, false, fmt.Errorf("data: 配额脚本返回了 %d 个值", len(raw))
	}
	return raw[0], raw[1] == 1, nil
}

// Add 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) Add(ctx context.Context, userID, metric, period string, delta int64, ttl time.Duration) (int64, error) {
	key, err := QuotaKey(userID, metric, period)
	if err != nil {
		return 0, err
	}
	v, err := c.evalNumber(ctx, addScript, []string{key, QuotaDirtyKey()},
		delta, ttl.Milliseconds(), userID)
	if err != nil {
		return 0, err
	}
	return v, nil
}

// Set 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) Set(ctx context.Context, userID, metric, period string, value int64, ttl time.Duration) error {
	key, err := QuotaKey(userID, metric, period)
	if err != nil {
		return err
	}
	if c.store == nil || c.store.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	return c.store.client.Set(ctx, key, value, ttl).Err()
}

// Get 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) Get(ctx context.Context, userID, metric, period string) (int64, bool, error) {
	key, err := QuotaKey(userID, metric, period)
	if err != nil {
		return 0, false, err
	}
	if c.store == nil || c.store.client == nil {
		return 0, false, errors.New("redis: 客户端未初始化")
	}
	v, err := c.store.client.Get(ctx, key).Int64()
	if errors.Is(err, goredis.Nil) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return v, true, nil
}

// Reset 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) Reset(ctx context.Context, userID, metric, period string) error {
	key, err := QuotaKey(userID, metric, period)
	if err != nil {
		return err
	}
	return c.store.Del(ctx, key)
}

// DirtyUsers 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) DirtyUsers(ctx context.Context, limit int) ([]string, error) {
	if c.store == nil || c.store.client == nil {
		return nil, errors.New("redis: 客户端未初始化")
	}
	if limit <= 0 {
		limit = 512
	}
	// `SRANDMEMBER` 而不是 `SPOP`：对账失败时不能把用户从集合里丢掉，
	// 否则那次失败就再也没有机会被纠正（账实不符会一直留在库里）。
	return c.store.client.SRandMemberN(ctx, QuotaDirtyKey(), int64(limit)).Result()
}

// MarkClean 见 biz.QuotaCounter。
func (c *RedisQuotaCounter) MarkClean(ctx context.Context, users []string) error {
	if len(users) == 0 {
		return nil
	}
	if c.store == nil || c.store.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	members := make([]any, 0, len(users))
	for _, u := range users {
		members = append(members, u)
	}
	return c.store.client.SRem(ctx, QuotaDirtyKey(), members...).Err()
}

// evalNumbers 执行「返回 Lua 表」的脚本，解出整型数组。
//
// 与 evalNumber 分成两个函数，而不是让一个 helper 「兼容两种返回形态」：
// 兼容写法在返回值形状不符时只能猜（把标量当成单元素表？还是报错？），
// 而**猜错的代价是静默降级**——降级包装会把错误吞掉、改用进程内计数，
// 于是「脚本写错了」表现为「配额有时拦不住」（见下方 evalNumber 的注释）。
func (c *RedisQuotaCounter) evalNumbers(ctx context.Context, script string, keys []string, args ...any) ([]int64, error) {
	if c.store == nil || c.store.client == nil {
		return nil, errors.New("redis: 客户端未初始化")
	}
	res, err := c.store.client.Eval(ctx, script, keys, args...).Result()
	if err != nil {
		return nil, err
	}
	list, ok := res.([]any)
	if !ok {
		return nil, fmt.Errorf("data: 脚本 %s 期望返回 Lua 表，实际 %T", scriptName(script), res)
	}
	out := make([]int64, 0, len(list))
	for i, v := range list {
		n, ok := v.(int64)
		if !ok {
			return nil, fmt.Errorf("data: 脚本 %s 第 %d 个返回值期望整数，实际 %T", scriptName(script), i, v)
		}
		out = append(out, n)
	}
	return out, nil
}

// evalNumber 执行「返回单个整数」的脚本。
//
// 必须与 evalNumbers 分开：`addScript` 的最后一句是 `return v`（整数），
// 而 `reserveScript` 是 `return {v, 1}`（表）。早先两者共用一个「按表解析」的
// helper，于是 `Add` 每次都以 `返回类型异常 int64` 失败，被降级包装吞掉后
// 改用**进程内**计数 —— 而进程内计数只有 `Add` 写过的键，
// 看不到 `Reserve` 写在 Redis 里的 `chat_requests`，于是「预扣 1 次」的
// 用户第二次提问时本地计数仍是 0 → **配额上限被静默突破**。
// 实测复现：free 档 `chat_requests=1` 时第 2 次提问正常打给了 AI。
func (c *RedisQuotaCounter) evalNumber(ctx context.Context, script string, keys []string, args ...any) (int64, error) {
	if c.store == nil || c.store.client == nil {
		return 0, errors.New("redis: 客户端未初始化")
	}
	res, err := c.store.client.Eval(ctx, script, keys, args...).Result()
	if err != nil {
		return 0, err
	}
	n, ok := res.(int64)
	if !ok {
		return 0, fmt.Errorf("data: 脚本 %s 期望返回整数，实际 %T", scriptName(script), res)
	}
	return n, nil
}

// scriptName 给错误信息一个可读的脚本标识（只取前几行里的第一句有效代码）。
//
// 直接把整个 Lua 脚本拼进 error 会让日志里出现多行内容，
// 而结构化日志的 `error` 字段出现换行后，行内检索（`Select-String`）会错位。
func scriptName(script string) string {
	for _, line := range strings.Split(script, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "--") {
			continue
		}
		if len(line) > 60 {
			line = line[:60] + "…"
		}
		return line
	}
	return "<empty>"
}

// RedisConcurrencyLimiter 用 Redis 实现并发槽位。
type RedisConcurrencyLimiter struct {
	counter *RedisQuotaCounter
}

// NewRedisConcurrencyLimiter 构造并发限流器。
func NewRedisConcurrencyLimiter(c *RedisQuotaCounter) *RedisConcurrencyLimiter {
	return &RedisConcurrencyLimiter{counter: c}
}

// Acquire 见 biz.ConcurrencyLimiter。
func (l *RedisConcurrencyLimiter) Acquire(ctx context.Context, userID string, limit int64, ttl time.Duration) (int64, bool, error) {
	key, err := ConcurrencyKey(userID)
	if err != nil {
		return 0, false, err
	}
	raw, err := l.counter.evalNumbers(ctx, reserveScript, []string{key, ""},
		1, limit, ttl.Milliseconds(), "")
	if err != nil {
		return 0, false, err
	}
	if len(raw) < 2 {
		return 0, false, fmt.Errorf("data: 并发脚本返回了 %d 个值", len(raw))
	}
	return raw[0], raw[1] == 1, nil
}

// Release 见 biz.ConcurrencyLimiter。
//
// 减到 0 就删键：留在那里的 `0` 会一直占着一个键，
// 而「当前有 0 个并发」与「没有这个键」在本项目里是同义的。
func (l *RedisConcurrencyLimiter) Release(ctx context.Context, userID string) error {
	key, err := ConcurrencyKey(userID)
	if err != nil {
		return err
	}
	if l.counter == nil || l.counter.store == nil || l.counter.store.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	// Lua 保证「DECR 到 <=0 就删除」是原子的：分成两条命令时，
	// 另一台实例可能在这中间 Incr 出一个新槽位并被我们删掉。
	const script = `
local v = redis.call('DECR', KEYS[1])
if v <= 0 then
  redis.call('DEL', KEYS[1])
  return 0
end
return v
`
	// 读返回值只为确认命令执行成功（`DECR` 到 <=0 时脚本自己删键）。
	if _, err := l.counter.store.client.Eval(ctx, script, []string{key}).Int64(); err != nil {
		if errors.Is(err, goredis.Nil) {
			return nil
		}
		return err
	}
	return nil
}

// RedisRateLimiter 用 Redis 实现固定窗口限流。
type RedisRateLimiter struct {
	store *Store
}

// NewRedisRateLimiter 构造限流器。
func NewRedisRateLimiter(s *Store) *RedisRateLimiter { return &RedisRateLimiter{store: s} }

// Allow 见 biz.RateLimiter。
//
// **固定窗口**而不是令牌桶/滑动窗口（docs/02-§5.3 提到的是后者）：
// 固定窗口只要一个 `INCR` + 一个 `EXPIRE`，不需要存时间戳集合，
// 也不需要把 `Eval` 的输入从「计数器」扩展到「时间序列」。
// 代价是窗口边界处可能通过接近 2 倍的名额（`59s` 与 `61s` 各来一轮），
// 对本项目的阈值（10~30 次/分钟）而言完全可接受；
// 换成滑动窗口要维护 ZSET 并按时间戳裁剪，单次成本高一个量级。
func (l *RedisRateLimiter) Allow(ctx context.Context, scope, id string, limit int64, window time.Duration, at time.Time) (bool, time.Duration, error) {
	bucket := at.Truncate(window)
	key, err := RateWindowKey(scope, id, bucket)
	if err != nil {
		return false, 0, err
	}
	counter := &RedisQuotaCounter{store: l.store}
	// 用 `reserveScript` 复用「不超过上限则自增」的原子语义；
	// TTL 给整个窗口：键名已经带了窗口编号，多活一会儿无害。
	raw, err := counter.evalNumbers(ctx, reserveScript, []string{key, ""},
		1, limit, int64(window/time.Millisecond), "")
	if err != nil {
		return false, 0, err
	}
	if len(raw) < 2 {
		return false, 0, fmt.Errorf("data: 限流脚本返回了 %d 个值", len(raw))
	}
	if raw[1] == 1 {
		return true, 0, nil
	}
	retryAfter := bucket.Add(window).Sub(at)
	if retryAfter < 0 {
		retryAfter = 0
	}
	return false, retryAfter, nil
}

// ---- 进程内兜底 ----

// localCounter 是 Redis 不可用时的进程内计数。
//
// 三个已知偏差，都写在这里以便对齐预期：
//
//  1. **每实例一份**：多副本部署时实际限额是 `limit × 副本数`。
//     这比「全部放行」好，但比 Redis 差 —— 它是降级，不是等价实现。
//  2. 进程重启即清零。
//  3. 容量有上限（`maxLocalKeys`）；超出后整表清空，限额会短暂放宽。
//     选择清空而不是「拒绝所有请求」：内存已经异常时，
//     把用户全部挡在门外是更重的故障。
const maxLocalKeys = 100_000

type localItem struct {
	value     int64
	expiresAt time.Time
}

type localCounter struct {
	mu    sync.Mutex
	items map[string]*localItem
	dirty map[string]struct{}
	now   func() time.Time
}

func newLocalCounter() *localCounter {
	return &localCounter{items: make(map[string]*localItem), dirty: make(map[string]struct{}), now: time.Now}
}

func (c *localCounter) key(userID, metric, period string) string {
	return userID + "|" + metric + "|" + period
}

// get 读当前值（顺带清理过期项）。持锁调用。
func (c *localCounter) get(key string) (*localItem, bool) {
	item, ok := c.items[key]
	if !ok {
		return nil, false
	}
	if !item.expiresAt.IsZero() && !c.now().Before(item.expiresAt) {
		delete(c.items, key)
		return nil, false
	}
	return item, true
}

func (c *localCounter) purgeExpired() {
	now := c.now()
	for k, item := range c.items {
		if !item.expiresAt.IsZero() && !now.Before(item.expiresAt) {
			delete(c.items, k)
		}
	}
}

func (c *localCounter) ensureCapacity() {
	if len(c.items) < maxLocalKeys {
		return
	}
	c.purgeExpired()
	if len(c.items) < maxLocalKeys {
		return
	}
	// 仍然超限：整表清空（见类型注释第 3 条）。
	c.items = make(map[string]*localItem)
}

// reserve 实现「不超过上限则自增」。
func (c *localCounter) reserve(key string, delta, limit int64, ttl time.Duration) (int64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ensureCapacity()
	item, ok := c.get(key)
	if !ok {
		item = &localItem{expiresAt: expiry(c.now(), ttl)}
		c.items[key] = item
	}
	if limit > 0 && item.value+delta > limit {
		return item.value, false
	}
	item.value += delta
	return item.value, true
}

func (c *localCounter) add(key string, delta int64, ttl time.Duration) int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.ensureCapacity()
	item, ok := c.get(key)
	if !ok {
		item = &localItem{expiresAt: expiry(c.now(), ttl)}
		c.items[key] = item
	}
	item.value += delta
	return item.value
}

func (c *localCounter) set(key string, value int64, ttl time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.items[key] = &localItem{value: value, expiresAt: expiry(c.now(), ttl)}
}

func (c *localCounter) getValue(key string) (int64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	item, ok := c.get(key)
	if !ok {
		return 0, false
	}
	return item.value, true
}

func (c *localCounter) reset(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.items, key)
}

func (c *localCounter) markDirty(userID string) {
	if userID == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.dirty) >= maxLocalKeys {
		return
	}
	c.dirty[userID] = struct{}{}
}

func (c *localCounter) dirtyUsers(limit int) []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]string, 0, len(c.dirty))
	for u := range c.dirty {
		if len(out) >= limit {
			break
		}
		out = append(out, u)
	}
	return out
}

func (c *localCounter) markClean(users []string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, u := range users {
		delete(c.dirty, u)
	}
}

// expiry 计算过期时刻；ttl <= 0 表示不过期（存量指标的计数键）。
func expiry(now time.Time, ttl time.Duration) time.Time {
	if ttl <= 0 {
		return time.Time{}
	}
	return now.Add(ttl)
}

// ---- 带降级的组合实现 ----

// fallbackRetryInterval 是降级期间重新探测 Redis 的间隔。
//
// 没有它的话，Redis 挂掉之后每个请求都要先等一次连接超时（秒级）——
// 降级反而把服务拖垮。5 秒是一个折中：恢复能在数秒内被感知，
// 而失败期间的额外代价只有 0.2 次/秒。
const fallbackRetryInterval = 5 * time.Second

// DegradedQuotaCounter 是「Redis 优先 + 进程内兜底」的组合。
type DegradedQuotaCounter struct {
	primary biz.QuotaCounter
	local   *localCounter
	log     *slog.Logger
	now     func() time.Time

	mu        sync.Mutex
	degraded  bool
	probeNext time.Time
}

// NewDegradedQuotaCounter 构造带降级的配额计数器。
//
// primary 为 nil（未配置 Redis）时只走本地实现。
func NewDegradedQuotaCounter(primary biz.QuotaCounter, log *slog.Logger) *DegradedQuotaCounter {
	if log == nil {
		log = slog.Default()
	}
	return &DegradedQuotaCounter{primary: primary, local: newLocalCounter(), log: log, now: time.Now}
}

// Degraded 报告当前是否处于降级状态（`/health` 与排障用）。
func (c *DegradedQuotaCounter) Degraded() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.degraded
}

// Reserve 见 biz.QuotaCounter。
func (c *DegradedQuotaCounter) Reserve(ctx context.Context, userID, metric, period string, delta, limit int64, ttl time.Duration) (int64, bool, error) {
	if c.usePrimary() {
		used, ok, err := c.primary.Reserve(ctx, userID, metric, period, delta, limit, ttl)
		if err == nil {
			c.recovered()
			return used, ok, nil
		}
		c.degrade(err)
	}
	c.local.markDirty(userID)
	used, ok := c.local.reserve(c.local.key(userID, metric, period), delta, limit, ttl)
	return used, ok, nil
}

// Add 见 biz.QuotaCounter。
func (c *DegradedQuotaCounter) Add(ctx context.Context, userID, metric, period string, delta int64, ttl time.Duration) (int64, error) {
	if c.usePrimary() {
		v, err := c.primary.Add(ctx, userID, metric, period, delta, ttl)
		if err == nil {
			c.recovered()
			return v, nil
		}
		c.degrade(err)
	}
	c.local.markDirty(userID)
	return c.local.add(c.local.key(userID, metric, period), delta, ttl), nil
}

// Set 见 biz.QuotaCounter。
func (c *DegradedQuotaCounter) Set(ctx context.Context, userID, metric, period string, value int64, ttl time.Duration) error {
	if c.usePrimary() {
		err := c.primary.Set(ctx, userID, metric, period, value, ttl)
		if err == nil {
			c.recovered()
			return nil
		}
		c.degrade(err)
	}
	c.local.set(c.local.key(userID, metric, period), value, ttl)
	return nil
}

// Get 见 biz.QuotaCounter。
//
// 降级期间**不读**本地值：本地值只是本进程看到的部分用量，
// 把它当成真实用量会让「回源 MySQL」这条正确的路径永远不会被走到。
func (c *DegradedQuotaCounter) Get(ctx context.Context, userID, metric, period string) (int64, bool, error) {
	if c.usePrimary() {
		v, ok, err := c.primary.Get(ctx, userID, metric, period)
		if err == nil {
			c.recovered()
			return v, ok, nil
		}
		c.degrade(err)
	}
	// 返回「未命中」而不是本地值：调用方会据此回源 MySQL（权威值）。
	return 0, false, nil
}

// Reset 见 biz.QuotaCounter。
func (c *DegradedQuotaCounter) Reset(ctx context.Context, userID, metric, period string) error {
	c.local.reset(c.local.key(userID, metric, period))
	if c.usePrimary() {
		err := c.primary.Reset(ctx, userID, metric, period)
		if err == nil {
			c.recovered()
			return nil
		}
		c.degrade(err)
	}
	return nil
}

// DirtyUsers 见 biz.QuotaCounter：两个来源合并（降级期间的用量同样要落库）。
func (c *DegradedQuotaCounter) DirtyUsers(ctx context.Context, limit int) ([]string, error) {
	seen := make(map[string]struct{}, limit)
	out := make([]string, 0, limit)
	for _, u := range c.local.dirtyUsers(limit) {
		if _, ok := seen[u]; !ok {
			seen[u] = struct{}{}
			out = append(out, u)
		}
	}
	if c.usePrimary() {
		users, err := c.primary.DirtyUsers(ctx, limit)
		if err == nil {
			c.recovered()
			for _, u := range users {
				if _, ok := seen[u]; !ok && len(out) < limit {
					seen[u] = struct{}{}
					out = append(out, u)
				}
			}
		} else {
			c.degrade(err)
		}
	}
	return out, nil
}

// MarkClean 见 biz.QuotaCounter。
func (c *DegradedQuotaCounter) MarkClean(ctx context.Context, users []string) error {
	c.local.markClean(users)
	if c.usePrimary() {
		if err := c.primary.MarkClean(ctx, users); err != nil {
			c.degrade(err)
		} else {
			c.recovered()
		}
	}
	return nil
}

func (c *DegradedQuotaCounter) usePrimary() bool {
	if c.primary == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.degraded {
		return true
	}
	if c.now().Before(c.probeNext) {
		return false
	}
	// 探测窗口到期：放一次真实调用出去试。
	return true
}

func (c *DegradedQuotaCounter) degrade(cause error) {
	c.mu.Lock()
	already := c.degraded
	c.degraded = true
	c.probeNext = c.now().Add(fallbackRetryInterval)
	c.mu.Unlock()
	if !already {
		c.log.Warn("redis.degraded",
			slog.String("component", "quota_counter"),
			slog.String("fallback", "local_memory"),
			slog.Any("error", cause))
	}
}

func (c *DegradedQuotaCounter) recovered() {
	c.mu.Lock()
	wasDegraded := c.degraded
	c.degraded = false
	c.mu.Unlock()
	if wasDegraded {
		c.log.Info("redis.recovered", slog.String("component", "quota_counter"))
	}
}

// DegradedConcurrencyLimiter 是并发槽位的降级实现。
type DegradedConcurrencyLimiter struct {
	primary biz.ConcurrencyLimiter
	local   *localCounter
	log     *slog.Logger
	now     func() time.Time

	mu        sync.Mutex
	degraded  bool
	probeNext time.Time
}

// NewDegradedConcurrencyLimiter 构造带降级的并发限流器。
func NewDegradedConcurrencyLimiter(primary biz.ConcurrencyLimiter, log *slog.Logger) *DegradedConcurrencyLimiter {
	if log == nil {
		log = slog.Default()
	}
	return &DegradedConcurrencyLimiter{primary: primary, local: newLocalCounter(), log: log, now: time.Now}
}

// Acquire 见 biz.ConcurrencyLimiter。
func (l *DegradedConcurrencyLimiter) Acquire(ctx context.Context, userID string, limit int64, ttl time.Duration) (int64, bool, error) {
	if l.usePrimary() {
		used, ok, err := l.primary.Acquire(ctx, userID, limit, ttl)
		if err == nil {
			l.recovered()
			return used, ok, nil
		}
		l.degrade(err)
	}
	// 本地槽位用统一的 key（`concurrency` + `current`），与配额键区分开。
	used, ok := l.local.reserve("cc|"+userID, 1, limit, ttl)
	return used, ok, nil
}

// Release 见 biz.ConcurrencyLimiter。
func (l *DegradedConcurrencyLimiter) Release(ctx context.Context, userID string) error {
	// 两条路径都尝试：我们无法确定 Acquire 当初落在哪一侧
	// （降级与恢复都可能发生在一次请求的生命周期内），而多减一次 =
	// 「本地减到 0 后被删」/「Redis 侧 DECR 到 <=0 后被删」都是幂等的。
	if l.local.add("cc|"+userID, -1, ttlForConcurrency) <= 0 {
		l.local.reset("cc|" + userID)
	}
	if l.primary != nil {
		if err := l.primary.Release(ctx, userID); err != nil {
			l.degrade(err)
		} else {
			l.recovered()
		}
	}
	return nil
}

// ttlForConcurrency 是本地槽位键的 TTL（与并发键同量级）。
var ttlForConcurrency = 10 * time.Minute

func (l *DegradedConcurrencyLimiter) usePrimary() bool {
	if l.primary == nil {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.degraded {
		return true
	}
	if l.now().Before(l.probeNext) {
		return false
	}
	return true
}

func (l *DegradedConcurrencyLimiter) degrade(cause error) {
	l.mu.Lock()
	already := l.degraded
	l.degraded = true
	l.probeNext = l.now().Add(fallbackRetryInterval)
	l.mu.Unlock()
	if !already {
		l.log.Warn("redis.degraded",
			slog.String("component", "concurrency_limiter"),
			slog.String("fallback", "local_memory"),
			slog.Any("error", cause))
	}
}

func (l *DegradedConcurrencyLimiter) recovered() {
	l.mu.Lock()
	wasDegraded := l.degraded
	l.degraded = false
	l.mu.Unlock()
	if wasDegraded {
		l.log.Info("redis.recovered", slog.String("component", "concurrency_limiter"))
	}
}

// DegradedRateLimiter 是限流的降级实现。
//
// 与配额**不同**的是：这里出错时调用方（biz）会放行（见 biz.RateLimitService.Allow），
// 因此本组合只在「Redis 调用失败」时改走本地计数，不额外制造错误。
type DegradedRateLimiter struct {
	primary biz.RateLimiter
	local   *localCounter
	log     *slog.Logger
	now     func() time.Time

	mu        sync.Mutex
	degraded  bool
	probeNext time.Time
}

// NewDegradedRateLimiter 构造带降级的限流器。
func NewDegradedRateLimiter(primary biz.RateLimiter, log *slog.Logger) *DegradedRateLimiter {
	if log == nil {
		log = slog.Default()
	}
	return &DegradedRateLimiter{primary: primary, local: newLocalCounter(), log: log, now: time.Now}
}

// Allow 见 biz.RateLimiter。
func (l *DegradedRateLimiter) Allow(ctx context.Context, scope, id string, limit int64, window time.Duration, at time.Time) (bool, time.Duration, error) {
	if l.usePrimary() {
		allowed, retryAfter, err := l.primary.Allow(ctx, scope, id, limit, window, at)
		if err == nil {
			l.recovered()
			return allowed, retryAfter, nil
		}
		l.degrade(err)
	}
	bucket := at.Truncate(window)
	used, ok := l.local.reserve("rate|"+scope+"|"+id+"|"+bucket.UTC().Format(time.RFC3339), 1, limit, window)
	if ok {
		return true, 0, nil
	}
	retryAfter := bucket.Add(window).Sub(at)
	if retryAfter < 0 {
		retryAfter = 0
	}
	_ = used
	return false, retryAfter, nil
}

func (l *DegradedRateLimiter) usePrimary() bool {
	if l.primary == nil {
		return false
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.degraded {
		return true
	}
	return !l.now().Before(l.probeNext)
}

func (l *DegradedRateLimiter) degrade(cause error) {
	l.mu.Lock()
	already := l.degraded
	l.degraded = true
	l.probeNext = l.now().Add(fallbackRetryInterval)
	l.mu.Unlock()
	if !already {
		l.log.Warn("redis.degraded",
			slog.String("component", "rate_limiter"),
			slog.String("fallback", "local_memory"),
			slog.Any("error", cause))
	}
}

func (l *DegradedRateLimiter) recovered() {
	l.mu.Lock()
	wasDegraded := l.degraded
	l.degraded = false
	l.mu.Unlock()
	if wasDegraded {
		l.log.Info("redis.recovered", slog.String("component", "rate_limiter"))
	}
}

// 编译期断言：两套后端都必须满足 biz 的语义接口。
var (
	_ biz.QuotaCounter       = (*RedisQuotaCounter)(nil)
	_ biz.QuotaCounter       = (*DegradedQuotaCounter)(nil)
	_ biz.ConcurrencyLimiter = (*RedisConcurrencyLimiter)(nil)
	_ biz.ConcurrencyLimiter = (*DegradedConcurrencyLimiter)(nil)
	_ biz.RateLimiter        = (*RedisRateLimiter)(nil)
	_ biz.RateLimiter        = (*DegradedRateLimiter)(nil)
)
