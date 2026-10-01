// Package data 是持久化实现层，按 代码生成规范.md 二 组织：
//
//	data.go / <业务>.go   仓储实现（实现 biz 定义的接口）
//	mysql/                MySQL 引擎：连接、事务、SQL 日志脱敏
//	redis/                Redis 引擎：连接、Key 命名空间、缓存组件
//
// 依赖方向是 data → biz（规范 四），所以本包可以 import biz，反过来绝对不行 ——
// 这也是领域对象（biz.User）与持久化对象（本包 userPO）必须分开、转换只能发生在这里的原因。
//
// 表结构的权威定义是 deploy/mysql/*.sql（docs/05-§2），这里只做映射，**不调用 AutoMigrate**：
// 建表由脚本负责，服务启动只校验表存在。
package data

import (
	"context"
	"errors"
	"log/slog"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/data/mysql"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/data/redis"
)

// ExpectedTables 是本服务启动期 MUST 校验存在的表（REQ-DATA-007 / docs/05-§5）。
// 「连接成功」不等于「表建对了」：只做 SELECT 1 的探活会漏掉缺列缺表，
// 而缺列要等到某条 SQL 才暴露（表现为偶发 500）。
// ⚠️ `idempotency_record` 与 `audit_log` 与 ai-platform 共用，改 DDL 前双方必须评审。
var ExpectedTables = []string{
	TableUser,
	TableRefreshToken,
	TableConversation,
	TableMessage,
	TableQuotaUsage,
	TableUsageRecord,
	TableIdempotencyRecord,
	TableAuditLog,
}

// Data 是数据访问的聚合入口：持有引擎句柄，仓储从这里取到它。
// 只放引擎而不放仓储实例：仓储是无状态无副作用的薄包装，由 NewXxxRepo(d) 现取现用即可。
type Data struct {
	DB    *mysql.DB
	Redis *redis.Store

	log *slog.Logger
}

// Open 建立全部数据层连接。
//
// 两个引擎的失败语义刻意不同：MySQL 连不上直接失败（权威台账，没有它没有任何能力）；
// Redis 连不上只告警（只影响配额精度与 ver 缓存命中率，docs/04-§9）。
func Open(cfg *conf.Config, log *slog.Logger) (*Data, error) {
	if log == nil {
		log = slog.Default()
	}
	db, err := mysql.Open(cfg.MySQL, log)
	if err != nil {
		return nil, err
	}
	rdb, err := redis.Open(cfg.Redis, log)
	if err != nil {
		// Redis URL 写错属于配置错误，必须在启动期暴露；
		// 而「Redis 服务没起来」是运行期事件，由调用方 Ping 后决定降级。
		_ = db.Close()
		return nil, err
	}
	return &Data{DB: db, Redis: rdb, log: log}, nil
}

// Close 关闭两个引擎；Redis 的关闭失败不影响返回值。
func (d *Data) Close() error {
	if d == nil {
		return nil
	}
	if d.Redis != nil {
		if err := d.Redis.Close(); err != nil {
			d.log.Warn("data.redis_close_failed", slog.String("error", err.Error()))
		}
	}
	if d.DB != nil {
		return d.DB.Close()
	}
	return nil
}

// VerifyTables 校验期望的表都存在（启动自检）。
func (d *Data) VerifyTables(ctx context.Context, tables []string) error {
	return d.DB.VerifyTables(ctx, tables)
}

// Ping 探测 MySQL 连通（健康检查用）。
func (d *Data) Ping(ctx context.Context) error {
	return d.DB.Ping(ctx)
}

// ---- 基础设施错误 → 领域哨兵（规范 §四：这个翻译只能发生在 data 侧）----

// translate 把 GORM 的「无记录」错误归一化成 biz.ErrNotFound。
func translate(err error) error {
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return biz.ErrNotFound
	}
	return err
}

// isDuplicate 报告错误是否为唯一键冲突。
// 依赖 gorm.Config{TranslateError: true}：靠 MySQL errno 1062 判断需要 import driver 的错误类型，
// 而「注册撞邮箱」与「刷新令牌撞哈希」必须被区分成 409 与 500。
func isDuplicate(err error) bool { return errors.Is(err, gorm.ErrDuplicatedKey) }
