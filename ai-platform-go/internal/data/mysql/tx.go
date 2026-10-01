package mysql

import (
	"context"

	"gorm.io/gorm"
)

// InTx 在一个事务内执行 fn。
//
// ⚠️ 事务不只是「原子性」：`LAST_INSERT_ID(expr)` 是**连接级**的，
// 而 GORM 在非事务模式下会用连接池里的任意连接执行下一条语句 ——
// 于是 `UPDATE ... LAST_INSERT_ID(...)` 之后的 `SELECT LAST_INSERT_ID()`
// 会读到**别的会话**的值。凡是用到这个技巧的地方 MUST 走本函数。
func (db *DB) InTx(ctx context.Context, fn func(tx *gorm.DB) error) error {
	return db.GORM.WithContext(ctx).Transaction(fn)
}
