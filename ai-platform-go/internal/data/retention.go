package data

import (
	"context"
	"errors"
	"time"
)

// 保留期与一致性维护（docs/05-§4.1 / §5）。
//
// ⚠️ 这一组函数目前没有调用方：保留期清理是每日任务，属于 M6 的生命周期钩子。
//
// 两点刻意为之：
//
//  1. 全部用原生 SQL + 表名常量：`conversationPO` 有一个名为 `DeletedAt` 的字段，
//     一旦被 GORM 识别成软删字段，所有查询都会自动带上 `deleted_at IS NULL`，
//     而这里要的恰恰是已删除的行（表现为「清理任务永远删不掉东西」，不报任何错）。
//  2. 它们是包级函数而不是仓储方法：仓储接口（biz 定义）是给业务用的，这些是运维动作。

// PurgeMessagesByConversations 删除指定会话的全部消息，返回删除行数。
func PurgeMessagesByConversations(ctx context.Context, d *Data, convIDs []string) (int64, error) {
	if len(convIDs) == 0 {
		return 0, nil
	}
	res := d.DB.GORM.WithContext(ctx).Exec(
		"DELETE FROM "+quoteIdent(TableMessage)+" WHERE conversation_id IN ?",
		convIDs,
	)
	return res.RowsAffected, res.Error
}

// PurgeDeletedConversations 物理清理软删超过保留期的会话（分批）。
//
// 先删消息再删会话：项目没有建物理外键（docs/05-§4.1），
// 顺序保持「先子后父」能让任何中途失败都不留下「孤儿消息指向已删会话」这种更难收拾的状态。
func PurgeDeletedConversations(ctx context.Context, d *Data, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 200
	}
	var ids []string
	if err := d.DB.GORM.WithContext(ctx).Raw(
		"SELECT id FROM "+quoteIdent(TableConversation)+
			" WHERE deleted_at IS NOT NULL AND deleted_at < ? ORDER BY deleted_at LIMIT ?",
		before, limit,
	).Scan(&ids).Error; err != nil {
		return 0, err
	}
	if len(ids) == 0 {
		return 0, nil
	}
	if _, err := PurgeMessagesByConversations(ctx, d, ids); err != nil {
		return 0, err
	}
	res := d.DB.GORM.WithContext(ctx).Exec(
		"DELETE FROM "+quoteIdent(TableConversation)+" WHERE id IN ?", ids,
	)
	return res.RowsAffected, res.Error
}

// CountOrphanMessages 统计「会话已不存在」的消息条数（每日一致性检查）。
// 只统计不删除：孤儿行往往意味着上游有逻辑缺陷（比如谁绕过仓储删了会话），
// 静默清掉等于把线索也删了。
func CountOrphanMessages(ctx context.Context, d *Data) (int64, error) {
	var n int64
	err := d.DB.GORM.WithContext(ctx).Raw(
		"SELECT COUNT(*) FROM " + quoteIdent(TableMessage) + " m WHERE NOT EXISTS (" +
			"SELECT 1 FROM " + quoteIdent(TableConversation) + " c WHERE c.id = m.conversation_id)",
	).Scan(&n).Error
	return n, err
}

// PruneMessageContent 把超长正文截断到 maxRunes 个字符（长消息治理，可选）。
// 用 `CHAR_LENGTH`（字符数）而不是 `LENGTH`（字节数）：汉字是 3 个字节，按字节截断会把一个汉字切成两半，
// 读出来就是乱码；而 `LEFT()` 本身按字符切，两者口径必须一致，否则判断与截断会对不上。
func PruneMessageContent(ctx context.Context, d *Data, id string, maxRunes int) error {
	if maxRunes <= 0 {
		return errors.New("data: maxRunes 必须为正")
	}
	res := d.DB.GORM.WithContext(ctx).Exec(
		"UPDATE "+quoteIdent(TableMessage)+
			" SET content = LEFT(content, ?) WHERE id = ? AND CHAR_LENGTH(content) > ?",
		maxRunes, id, maxRunes,
	)
	return res.Error
}

// PurgeExpiredIdempotencyRecords 清理过期的幂等记录（docs/02-§7：保留 24h）。
// 过期即删：把过期记录留着会诱使调用方依赖「反正它还在」，而某个时刻的清理会让行为突然改变。
func PurgeExpiredIdempotencyRecords(ctx context.Context, d *Data, before time.Time, limit int) (int64, error) {
	if limit <= 0 {
		limit = 1000
	}
	res := d.DB.GORM.WithContext(ctx).Exec(
		"DELETE FROM "+quoteIdent(TableIdempotencyRecord)+" WHERE expires_at < ? LIMIT ?",
		before, limit,
	)
	return res.RowsAffected, res.Error
}

// PurgeUsageRecords 在这里**刻意缺席**：用量明细（`usage_record`）的过期清理
// 已经有唯一实现 —— `QuotaRepo.PurgeRecordsOlderThan`（quota.go，属 M5 配额模块）。
// 同一句 `DELETE FROM usage_record WHERE created_at < ?` 写两遍，
// 迟早会出现「两处保留期不一致，而两边看起来都对」。
