package mysql

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	// 本包就叫 mysql，不加别名时 `mysql.Open` 指的是**驱动**而不是本包，
	// 读起来极易误判，所以显式别名。
	gormmysql "gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

// DB 包裹 GORM 句柄与底层 `*sql.DB`（后者用于连接池设置与 Ping）。
type DB struct {
	GORM *gorm.DB
	SQL  *sql.DB
}

// Open 建立业务台账连接并做连通性检查。
//
// 注意两点：
//  1. DSN MUST 带 `parseTime=true&loc=UTC`，否则 DATETIME(3) 会按本地时区解析；
//  2. NowFunc 用 clockx.Now()（UTC + 毫秒截断），保证写库时间与契约序列化一致。
func Open(cfg conf.MySQL, log *slog.Logger) (*DB, error) {
	if cfg.DSN == "" {
		return nil, errors.New("data: MYSQL_DSN 为空")
	}
	gdb, err := gorm.Open(gormmysql.Open(cfg.DSN), &gorm.Config{
		Logger: newGormLogger(log, cfg.SlowQueryThreshold),
		NowFunc: func() time.Time {
			return clockx.Now()
		},
		// 保留默认事务（每条写操作自动包事务）：本项目多处依赖显式事务，
		// 开 SkipDefaultTransaction 只会让「忘了开事务」的路径更危险。
		SkipDefaultTransaction: false,
		// 把 MySQL 的 1062 翻译成 gorm.ErrDuplicatedKey：
		// 靠 errno 判断唯一键冲突需要 import driver 的错误类型，
		// 而「注册撞邮箱」与「刷新令牌撞哈希」必须被区分成 409 / 500。
		TranslateError: true,
		// 表名不做复数化：DDL 里就是 `user` / `conversation` 这些单数名。
		NamingStrategy: nil,
	})
	if err != nil {
		return nil, fmt.Errorf("data: 打开 MySQL 失败: %w", err)
	}

	sqlDB, err := gdb.DB()
	if err != nil {
		return nil, fmt.Errorf("data: 获取 *sql.DB 失败: %w", err)
	}
	sqlDB.SetMaxOpenConns(cfg.MaxOpenConns)
	sqlDB.SetMaxIdleConns(cfg.MaxIdleConns)
	sqlDB.SetConnMaxLifetime(time.Duration(cfg.ConnMaxLifetimeMinutes) * time.Minute)

	acquire := time.Duration(cfg.AcquireTimeoutSeconds) * time.Second
	if acquire <= 0 {
		acquire = 5 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), acquire)
	defer cancel()
	if err := sqlDB.PingContext(ctx); err != nil {
		_ = sqlDB.Close()
		return nil, fmt.Errorf("data: MySQL 连接不可用: %w", err)
	}
	return &DB{GORM: gdb, SQL: sqlDB}, nil
}

// Close 关闭连接池。
func (db *DB) Close() error {
	if db == nil || db.SQL == nil {
		return nil
	}
	return db.SQL.Close()
}

// VerifyTables 校验期望的表都存在（docs/05-§5 启动检查）。
//
// 「能连上」不等于「表建对了」：只做 `SELECT 1` 会漏掉缺表/缺列，
// 而这类问题要等某条业务 SQL 才暴露（表现为偶发 500）。
func (db *DB) VerifyTables(ctx context.Context, tables []string) error {
	if len(tables) == 0 {
		return nil
	}
	var found []string
	err := db.GORM.WithContext(ctx).
		Raw("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ?", tables).
		Scan(&found).Error
	if err != nil {
		return fmt.Errorf("data: 查询 information_schema 失败: %w", err)
	}
	present := make(map[string]struct{}, len(found))
	for _, t := range found {
		present[t] = struct{}{}
	}
	var missing []string
	for _, want := range tables {
		if _, ok := present[want]; !ok {
			missing = append(missing, want)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("data: 缺少表 %v（请先执行 deploy/mysql 下的建表脚本）", missing)
	}
	return nil
}

// Ping 探测数据库连通（健康检查用）。
func (db *DB) Ping(ctx context.Context) error {
	if db == nil || db.SQL == nil {
		return errors.New("data: DB 未初始化")
	}
	return db.SQL.PingContext(ctx)
}

// 编译期断言：gormlogger.Interface 由 gormSlogLogger 实现（见 gormlog.go）。
var _ gormlogger.Interface = (*gormSlogLogger)(nil)
