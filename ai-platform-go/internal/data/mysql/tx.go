package mysql

import (
	"context"

	"gorm.io/gorm"
)

// InTx 在一个事务内执行 fn。
// ⚠️ 事务不只是「原子性」：`LAST_INSERT_ID(expr)` 是连接级的，而 GORM 非事务模式下
// 会用连接池里的任意连接执行下一条语句，于是 `SELECT LAST_INSERT_ID()` 会读到别的会话的值。
// 凡是用到这个技巧的地方 MUST 走本函数。
func (db *DB) InTx(ctx context.Context, fn func(tx *gorm.DB) error) error {
	return db.GORM.WithContext(ctx).Transaction(fn)
}
