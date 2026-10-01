// 保留期清理与每日对账（docs/05-§5、docs/02-§5.2）。
//
// 这一组动作有一个共同的特征：**它们都只能"最终一致"**。
// 清理晚一天没关系，对账晚一小时也没关系，但任何一条「跑失败了却当成功」
// 都会累积成不可逆的数据问题（保留期违规、账目长期偏差）。因此：
//
//   - 每一步都独立执行、独立记日志，一步失败不影响下一步；
//   - 返回一份 `RetentionReport`，让调用方（与运维）能看见每一类的实际行数 ——
//     「跑了但什么都没删」和「没跑」在日志里必须能区分开；
//   - 时间窗口全部用**注入的时钟**计算，便于单测断言「保留 90 天」这类边界。
package biz

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

// RetentionRepo 是保留期清理所需的数据库动作。
//
// 刻意声明成「按类别的删除」而不是一个通用 `Delete(table, cond)`：
// 通用删除会让「哪些表可以被定时任务删」这个决定散落在调用点，
// 而这里的每一类都有明确的保留期依据（docs/05-§5）。
type RetentionRepo interface {
	// PurgeDeletedConversations 物理清理软删超过保留期的会话（含其消息，分批）。
	PurgeDeletedConversations(ctx context.Context, before time.Time, limit int) (int64, error)
	// PurgeExpiredIdempotency 清理到期的幂等记录（docs/02-§7）。
	PurgeExpiredIdempotency(ctx context.Context, before time.Time, limit int) (int64, error)
	// CountOrphanMessages 统计会话已不存在的消息（一致性检查，只报不删）。
	CountOrphanMessages(ctx context.Context) (int64, error)
}

// UsagePurger 清理过期的用量明细（实现是 M5 的 QuotaService）。
//
// 窄接口而不是直接依赖 `*QuotaService`：清理任务只需要「按时间删」这一件事，
// 把它绑到整个配额服务上会让本模块的单测必须构造一个完整的配额服务。
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
	// retentionBatchLimit 是单次删除的批大小。
	//
	// 分批而不是 `DELETE ... LIMIT` 不限量：一次删几十万行会长时间持有行锁，
	// 而这张库同时还在服务在线请求（表现为「清理任务一跑，接口就抖动」）。
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
//
// 用具体字段而不是 `map[string]int64`：日志与告警要按字段名引用，
// map 的键拼错是编译期发现不了的（而这里拼错的表现是「面板上少一条曲线」）。
type RetentionReport struct {
	Conversations   int64
	UsageRecords    int64
	IdempotencyRows int64
	AuditLogs       int64
	OrphanMessages  int64
}

// Start 启动后台循环（两个：每小时的「到点了吗」检查，与对账/清理本身）。
//
// 不引入第三方调度库：需要的只是「每天在某个整点跑一次」，
// 而 `time.Ticker` + 一个「今天跑过了吗」的字符串比较就够了 ——
// 引入 cron 表达式解析会让「跑一次」变成「按一个配置字符串跑」，
// 而那个字符串写错时的表现是**静默不跑**。
func (s *RetentionService) Start(ctx context.Context) {
	go s.loop(ctx)
}

func (s *RetentionService) loop(ctx context.Context) {
	ticker := time.NewTicker(s.d.CheckInterval)
	defer ticker.Stop()
	// 启动时先跑一次检查：否则在「进程存活不足一小时」的场景里
	// （本地开发、滚动重启）任务永远不会被触发。
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
		// 先记「今天跑过」，再执行：反过来的话，任务本身报错时会每分钟重试一次，
		// 而重建是**以 MySQL 覆盖 Redis**——重复执行会把这一刻之后的
		// 实时计数抹掉（用户刚发的那次请求凭空消失）。
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

// RunOnce 执行一轮清理与一致性检查，返回各类的实际行数。
//
// 返回 error 只表示「一个都没跑成」（通常是数据库不可用）：
// 单项失败会记 WARN 并继续 —— 会话清理失败不该阻止审计清理。
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

		// 幂等记录「过期即删」：它的过期时刻写在行里（`expires_at`），
		// 所以传的是 `now` 而不是 now-N 天。
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
				// ERROR 而不是 WARN：孤儿行的成因一定是某个逻辑缺陷
				// （有人绕过仓储删了会话），它不是「正常的数据演化」。
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

// Rebuild 以 MySQL 为准重建 Redis 计数（docs/02-§5.2 的「权威性」一条）。
//
// 返回重建的用户数；未接线配额时返回 (0, nil) —— 「没配」不是错误。
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
