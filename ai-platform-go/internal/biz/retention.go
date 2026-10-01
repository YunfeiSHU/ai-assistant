// 保留期清理与每日对账（docs/05-§5、docs/02-§5.2）。
//
// 这一组动作只能「最终一致」：晚一天没关系，但「跑失败了却当成功」会累积成
// 不可逆的数据问题。因此每一步独立执行/记日志，返回 `RetentionReport` 让
// 「跑了但什么都没删」与「没跑」在日志里可区分；时间窗口全用注入时钟，便于单测边界。
package biz

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

// RetentionRepo 是保留期清理所需的数据库动作。
// 声明成「按类别的删除」而非通用 `Delete(table, cond)`：后者会让「哪些表可被
// 定时任务删」散落在调用点，而每类删除都有明确的保留期依据（docs/05-§5）。
type RetentionRepo interface {
	// PurgeDeletedConversations 物理清理软删超过保留期的会话（含其消息，分批）。
	PurgeDeletedConversations(ctx context.Context, before time.Time, limit int) (int64, error)
	// PurgeExpiredIdempotency 清理到期的幂等记录（docs/02-§7）。
	PurgeExpiredIdempotency(ctx context.Context, before time.Time, limit int) (int64, error)
	// CountOrphanMessages 统计会话已不存在的消息（一致性检查，只报不删）。
	CountOrphanMessages(ctx context.Context) (int64, error)
}

// UsagePurger 清理过期的用量明细（实现是 M5 的 QuotaService）。
// 用窄接口而非 `*QuotaService`：清理任务只需要「按时间删」，免得单测构造整个配额服务。
type UsagePurger interface {
	PurgeUsageBefore(ctx context.Context, before time.Time, limit int) (int64, error)
}

// QuotaReconciler 是每日重建 Redis 计数所需的能力（实现是 M5 的 QuotaService）。
type QuotaReconciler interface {
	RebuildFromLedger(ctx context.Context, at time.Time, limit int) (int, error)
}

// AuditPurger 清理过期审计（保留 ≥ 180 天，docs/05-§2.8）。
type AuditPurger interface {
	DeleteOlderThan(ctx context.Context, before time.Time, limit int) (int64, error)
}

// 保留期默认值（与 docs/05-§5 一致，单位天）。
const (
	// MessageRetentionDays 是会话软删后消息的保留天数。
	MessageRetentionDays = 30
	// UsageRetentionDaysDefault 是用量明细的保留天数。
	UsageRetentionDaysDefault = 90
	// AuditRetentionDays 是审计日志的保留天数下限（≥180）。
	AuditRetentionDays = 180
	// retentionBatchLimit 是单次删除的批大小。分批以免一次删几十万行长时间持锁
	//（表现为「清理任务一跑，接口就抖动」）。
	retentionBatchLimit = 1000
	// reconcileBatchLimit 是每日重建单轮处理的用户数上限。
	reconcileBatchLimit = 5000
)

// RetentionDeps 是 RetentionService 的依赖。
type RetentionDeps struct {
	Repo     RetentionRepo
	Usage    UsagePurger
	Audit    AuditPurger
	Quota    QuotaReconciler
	Metrics  Metrics
	Clock    nowFunc
	Log      *slog.Logger
	Timezone *time.Location

	// RetentionDays 是会话/消息的保留天数（<=0 时用 MessageRetentionDays）。
	RetentionDays int
	// UsageRetentionDays 是用量明细保留天数（<=0 时用 UsageRetentionDaysDefault）。
	UsageRetentionDays int
	// RebuildHour 是每日重建 Redis 的整点（默认 0，docs/02-§5.2）。
	RebuildHour int
	// PurgeHour 是每日清理的整点（默认 3）。
	PurgeHour int
	// CheckInterval 是「现在是几点」的检查周期（默认 1h）。
	CheckInterval time.Duration
}

// RetentionService 执行保留期清理与每日对账。
type RetentionService struct {
	d RetentionDeps

	lastRebuildDay string
	lastPurgeDay   string
}

// NewRetentionService 构造服务。
func NewRetentionService(d RetentionDeps) *RetentionService {
	if d.Clock == nil {
		d.Clock = time.Now
	}
	if d.Log == nil {
		d.Log = slog.Default()
	}
	if d.Timezone == nil {
		d.Timezone = time.UTC
	}
	if d.RetentionDays <= 0 {
		d.RetentionDays = MessageRetentionDays
	}
	if d.UsageRetentionDays <= 0 {
		d.UsageRetentionDays = UsageRetentionDaysDefault
	}
	if d.RebuildHour < 0 || d.RebuildHour > 23 {
		d.RebuildHour = 0
	}
	if d.PurgeHour < 0 || d.PurgeHour > 23 {
		d.PurgeHour = 3
	}
	if d.CheckInterval <= 0 {
		d.CheckInterval = time.Hour
	}
	return &RetentionService{d: d}
}

// RetentionReport 是一次清理的实际结果（每一项都是行数）。
// 用具体字段而非 `map[string]int64`：日志/告警按字段名引用，map 键拼错编译期发现不了。
type RetentionReport struct {
	Conversations   int64
	UsageRecords    int64
	IdempotencyRows int64
	AuditLogs       int64
	OrphanMessages  int64
}

// Start 启动后台循环（每小时的「到点了吗」检查 + 对账/清理本身）。
// 不引入 cron 库：`time.Ticker` + 「今天跑过了吗」的字符串比较就够，
// cron 表达式写错时的表现是静默不跑。
func (s *RetentionService) Start(ctx context.Context) {
	go s.loop(ctx)
}

func (s *RetentionService) loop(ctx context.Context) {
	ticker := time.NewTicker(s.d.CheckInterval)
	defer ticker.Stop()
	// 启动时先跑一次：否则「进程存活不足一小时」的场景（本地开发、滚动重启）永不触发。
	s.tick(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.tick(ctx)
		}
	}
}

func (s *RetentionService) tick(ctx context.Context) {
	now := s.d.Clock().In(s.d.Timezone)
	today := now.Format("2006-01-02")

	if now.Hour() == s.d.RebuildHour && s.lastRebuildDay != today {
		// 先记「今天跑过」再执行：反过来时任务报错会每分钟重试，而重建是
		//「以 MySQL 覆盖 Redis」——重复执行会抹掉之后的实时计数。
		s.lastRebuildDay = today
		s.rebuild(ctx, now)
	}
	if now.Hour() == s.d.PurgeHour && s.lastPurgeDay != today {
		s.lastPurgeDay = today
		if _, err := s.RunOnce(ctx); err != nil {
			s.log().ErrorContext(ctx, "retention.run_failed", slog.Any("error", err))
		}
	}
}

// RunOnce 执行一轮清理与一致性检查。返回 error 只表示「一个都没跑成」（通常 DB 不可用）：
// 单项失败记 WARN 并继续 —— 会话清理失败不该阻止审计清理。
func (s *RetentionService) RunOnce(ctx context.Context) (RetentionReport, error) {
	now := s.d.Clock().UTC()
	var report RetentionReport
	ran := 0

	if s.d.Repo != nil {
		before := now.AddDate(0, 0, -s.d.RetentionDays)
		n, err := s.d.Repo.PurgeDeletedConversations(ctx, before, retentionBatchLimit)
		if err != nil {
			s.warn(ctx, "retention.conversations_failed", err)
		} else {
			report.Conversations = n
			ran++
		}

		// 幂等记录「过期即删」：过期时刻写在行里（expires_at），故传 `now` 而非 now-N 天。
		n, err = s.d.Repo.PurgeExpiredIdempotency(ctx, now, retentionBatchLimit)
		if err != nil {
			s.warn(ctx, "retention.idempotency_failed", err)
		} else {
			report.IdempotencyRows = n
			ran++
		}

		// 一致性检查（AC-DATA-06）：只统计不删除，并把结果写进指标。
		orphans, err := s.d.Repo.CountOrphanMessages(ctx)
		if err != nil {
			s.warn(ctx, "retention.orphan_check_failed", err)
		} else {
			report.OrphanMessages = orphans
			s.d.Metrics.SetOrphanRows("message", orphans)
			if orphans > 0 {
				// ERROR 而非 WARN：孤儿行一定是逻辑缺陷（绕过仓储删了会话），不是正常数据演化。
				s.log().ErrorContext(ctx, "retention.orphan_rows",
					slog.String("table", "message"),
					slog.Int64("rows", orphans),
				)
			}
			ran++
		}
	}

	if s.d.Usage != nil {
		before := now.AddDate(0, 0, -s.d.UsageRetentionDays)
		n, err := s.d.Usage.PurgeUsageBefore(ctx, before, retentionBatchLimit)
		if err != nil {
			s.warn(ctx, "retention.usage_failed", err)
		} else {
			report.UsageRecords = n
			ran++
		}
	}

	if s.d.Audit != nil {
		before := now.AddDate(0, 0, -AuditRetentionDays)
		n, err := s.d.Audit.DeleteOlderThan(ctx, before, retentionBatchLimit)
		if err != nil {
			s.warn(ctx, "retention.audit_failed", err)
		} else {
			report.AuditLogs = n
			ran++
		}
	}

	s.log().InfoContext(ctx, "retention.completed",
		slog.Int64("conversations", report.Conversations),
		slog.Int64("usage_records", report.UsageRecords),
		slog.Int64("idempotency_rows", report.IdempotencyRows),
		slog.Int64("audit_logs", report.AuditLogs),
		slog.Int64("orphan_messages", report.OrphanMessages),
	)
	if ran == 0 {
		return report, errors.New("biz: 保留期任务一项都没能执行")
	}
	return report, nil
}

// Rebuild 以 MySQL 为准重建 Redis 计数（docs/02-§5.2）。未接线配额时返回 (0, nil)。
func (s *RetentionService) Rebuild(ctx context.Context, at time.Time) (int, error) {
	if s.d.Quota == nil {
		return 0, nil
	}
	n, err := s.d.Quota.RebuildFromLedger(ctx, at, reconcileBatchLimit)
	if err != nil {
		return 0, err
	}
	s.log().InfoContext(ctx, "retention.rebuilt_from_ledger",
		slog.Int("users", n),
		slog.String("at", at.Format(time.RFC3339)),
	)
	return n, nil
}

func (s *RetentionService) rebuild(ctx context.Context, at time.Time) {
	if _, err := s.Rebuild(ctx, at); err != nil {
		s.warn(ctx, "retention.rebuild_failed", err)
	}
}

func (s *RetentionService) warn(ctx context.Context, msg string, err error) {
	s.log().WarnContext(ctx, msg, slog.Any("error", err))
}

func (s *RetentionService) log() *slog.Logger { return s.d.Log }
