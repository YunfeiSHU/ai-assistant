// Package redis 是网关对 Redis 的全部接触面：连接、键命名空间、缓存组件。
//
// 它与同级的 `data/mysql` 是**反过来的**关系（docs/04-§9）：
// MySQL 是权威台账，Redis 只是配额精度与 ver 缓存的加速器。
// 因此本包的任何失败都 MUST 能降级回 MySQL，不能变成业务错误。
//
// 本包只在下面这一个文件里接触 `go-redis` 的驱动类型：
// 其余文件（keys.go / version_cache.go）看到的是 string / int / time.Duration，
// 于是它们的单测用几行假实现即可，不必起 Redis。
package redis

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
)

// ErrNotFound 表示键不存在（对应驱动层的 `redis.Nil`）。
//
// 把驱动的哨兵值翻译成本包的哨兵值，调用方就不必 import 驱动、
// 也不必知道「空值是 errors.Nil 还是约定值」。
var ErrNotFound = errors.New("redis: 键不存在")

// KV 是本包对外暴露的最小键值能力。
//
// 刻意用普通类型（string / time.Duration）而不是驱动的 `*StringCmd`：
// 返回命令对象会让「必须调用 .Result()/.Err() 才算真的执行」变成调用方的负担，
// 也正是原先把 redis.Nil 与业务错误混在一起判定的来源。
type KV interface {
	Get(ctx context.Context, key string) (string, error)
	Set(ctx context.Context, key, value string, ttl time.Duration) error
	Del(ctx context.Context, keys ...string) error
}

// Store 拥有 Redis 客户端，并同时实现 KV。
type Store struct {
	client *goredis.Client
	log    *slog.Logger
}

// Open 建立 Redis 客户端。
//
// 不做 Ping：Redis 只影响配额精度与 ver 缓存命中率（docs/04-§9），
// 启动期连不上不应阻止服务提供「非 AI 且非配额」的能力；
// 连续性由 /health/ready 判断。
func Open(cfg conf.Redis, log *slog.Logger) (*Store, error) {
	if log == nil {
		log = slog.Default()
	}
	opt, err := goredis.ParseURL(cfg.URL)
	if err != nil {
		return nil, fmt.Errorf("data: REDIS_URL 解析失败: %w", err)
	}
	if cfg.PoolSize > 0 {
		opt.PoolSize = cfg.PoolSize
	}
	return &Store{client: goredis.NewClient(opt), log: log}, nil
}

// Ping 探测连通（健康检查用）。
func (s *Store) Ping(ctx context.Context) error {
	if s == nil || s.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	return s.client.Ping(ctx).Err()
}

// Close 关闭连接池。
func (s *Store) Close() error {
	if s == nil || s.client == nil {
		return nil
	}
	return s.client.Close()
}

// KV 返回键值视图（版本缓存的依赖口）。
func (s *Store) KV() KV { return s }

// PoolStats 返回连接池快照 `(total, idle, stale)`。
//
// 放在本文件是因为它是**唯一**接触 go-redis 类型的文件（见包注释）：
// 暴露三个整数，指标包就不必 import 驱动，也就不会因为驱动版本变更
// 而被动跟着改。返回零而不是报错 —— 指标是尽力而为的观测面，
// 「拿不到连接池数据」不该让 /metrics 整个 500。
func (s *Store) PoolStats() (total, idle, stale int) {
	if s == nil || s.client == nil {
		return 0, 0, 0
	}
	ps := s.client.PoolStats()
	return int(ps.TotalConns), int(ps.IdleConns), int(ps.StaleConns)
}

// Get 取键值；未命中返回 ErrNotFound。
func (s *Store) Get(ctx context.Context, key string) (string, error) {
	if s == nil || s.client == nil {
		// 未初始化视同未命中：上层会因此回源 MySQL，语义仍然正确。
		return "", ErrNotFound
	}
	value, err := s.client.Get(ctx, key).Result()
	switch {
	case errors.Is(err, goredis.Nil):
		return "", ErrNotFound
	case err != nil:
		return "", err
	}
	return value, nil
}

// Set 写键值。ttl <= 0 时驱动会按「永不过期」处理，
// 因此调用方 MUST 显式给出正数 TTL —— 传 0 是「永久」，不是「立刻过期」。
func (s *Store) Set(ctx context.Context, key, value string, ttl time.Duration) error {
	if s == nil || s.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	return s.client.Set(ctx, key, value, ttl).Err()
}

// Del 删键。
func (s *Store) Del(ctx context.Context, keys ...string) error {
	if s == nil || s.client == nil {
		return errors.New("redis: 客户端未初始化")
	}
	return s.client.Del(ctx, keys...).Err()
}

// 编译期断言。
var _ KV = (*Store)(nil)
