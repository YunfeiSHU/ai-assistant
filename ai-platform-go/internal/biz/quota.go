package biz

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel/trace"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// 配额指标（docs/02-§5.1）。
//
// 这 6 个字符串是**跨系统契约**：`quota_usage.metric` 的值、Redis Key 的一段、
// Prometheus 的 `metric` 标签、客户端展示的分组，四处必须是同一批字面量。
// 因此定义在本包（业务层的语言），由 data 与 pkg 引用，而不是各自拼字符串。
const (
	// MetricChatRequests 是对话请求数（按自然日重置）。
	MetricChatRequests = "chat_requests"
	// MetricLLMTokens 是 LLM token 用量（按自然日重置，取自 usage.total_tokens）。
	MetricLLMTokens = "llm_tokens"
	// MetricKBCount 是知识库数量（存量型，不按日重置）。
	MetricKBCount = "kb_count"
	// MetricDocumentsCount 是文档数量（存量型）。
	MetricDocumentsCount = "documents_count"
	// MetricStorageBytes 是存储占用字节数（存量型，由 AI 侧上报）。
	MetricStorageBytes = "storage_bytes"
	// MetricConcurrency 是并发请求数（瞬时量，用计数器而非累加）。
	MetricConcurrency = "concurrency"
)

// AllQuotaMetrics 是全部指标（按契约顺序，供响应体稳定输出）。
//
// 顺序固定很实际：客户端把 `metrics` 当有序列表渲染，
// 而 map 遍历顺序在 Go 里是随机的 —— 每次刷新面板上指标跳来跳去。
var AllQuotaMetrics = []string{
	MetricChatRequests,
	MetricLLMTokens,
	MetricKBCount,
	MetricDocumentsCount,
	MetricStorageBytes,
	MetricConcurrency,
}

// PeriodCurrent 是存量型指标（kb_count / documents_count / storage_bytes）的周期值。
const PeriodCurrent = "current"

// IsDailyMetric 报告该指标是否按自然日重置（docs/02-§5.1）。
//
// 只有 `chat_requests` 与 `llm_tokens` 按日重置：它们衡量「每天的消耗速率」。
// 另外三个是**存量上限**（一共能有几个知识库），按日重置没有意义 ——
// 重置的那一刻所有用户的存量都会变成 0，限额形同虚设。
func IsDailyMetric(metric string) bool {
	return metric == MetricChatRequests || metric == MetricLLMTokens
}

// IsStockMetric 报告该指标是否为存量上限。
func IsStockMetric(metric string) bool {
	switch metric {
	case MetricKBCount, MetricDocumentsCount, MetricStorageBytes:
		return true
	default:
		return false
	}
}

// QuotaUnit 返回指标的展示单位（docs/02-§6.2 的 `unit`，逐字对齐）。
func QuotaUnit(metric string) string {
	switch metric {
	case MetricChatRequests:
		return "次"
	case MetricLLMTokens:
		return "token"
	case MetricStorageBytes:
		return "字节"
	default:
		// kb_count / documents_count / concurrency 都是「个数」。
		return "个"
	}
}

// PlanFree 是兜底套餐名（注册时的默认值，也是限额表的必备键）。
const PlanFree = "free"

// ---- 周期 ----

// QuotaPeriod 是一个配额周期。
type QuotaPeriod struct {
	// Name 是落到 `quota_usage.period` 与 Redis Key 里的值：
	// 日指标是 `2026-09-30`，存量指标是 `current`。
	Name string
	// Start / End 是该周期的 UTC 边界；存量指标的 End 是零值（没有边界）。
	Start time.Time
	End   time.Time
}

// Resets 报告该周期是否会自然重置（存量指标不会）。
func (p QuotaPeriod) Resets() bool { return !p.End.IsZero() }

// PeriodFor 计算某个指标在 `at` 时刻所属的周期。
//
// 日边界按配置时区（`QUOTA_TIMEZONE`，默认 Asia/Shanghai）取，**不是 UTC**：
// 「今天用了 37 次」对用户而言是他所在时区的今天。
// 若按 UTC 切，北京时间早 8 点就换日，用户会在早饭前后看到计数清零。
func (s *QuotaService) PeriodFor(metric string, at time.Time) QuotaPeriod {
	if !IsDailyMetric(metric) {
		return QuotaPeriod{Name: PeriodCurrent}
	}
	loc := s.location()
	local := at.In(loc)
	start := time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, loc)
	return QuotaPeriod{
		Name:  start.Format(clockx.DateLayout),
		Start: start.UTC(),
		End:   start.AddDate(0, 0, 1).UTC(),
	}
}

// ---- 台账（MySQL，权威） ----

// QuotaRow 是 `quota_usage` 的一行（领域类型）。
type QuotaRow struct {
	UserID    string
	Metric    string
	Period    string
	Used      int64
	Limit     *int64
	UpdatedAt time.Time
}

// UsageRef 是一条用量明细指向的会话/消息/链路。
type UsageRef struct {
	ConversationID string
	MessageID      string
	TraceID        string
}

// UsageEntry 是 `usage_record` 的一条明细（docs/05-§2.6）。
type UsageEntry struct {
	ID        int64
	UserID    string
	Metric    string
	Amount    int64
	Ref       UsageRef
	CreatedAt time.Time
}

// QuotaRepo 是配额台账的仓储（实现在 data）。
//
// 与原生的 `data.QuotaRepo` 相比，这里的签名全部是**领域类型**：
// 仓储若返回表结构的 PO，业务层就得知道列名与空值语义（`LimitValue *int64`），
// 而那是存储细节。转换只发生在 data 侧。
type QuotaRepo interface {
	Get(ctx context.Context, userID, metric, period string) (*QuotaRow, error)
	ListByPeriod(ctx context.Context, userID, period string) ([]QuotaRow, error)
	// Upsert 以绝对值覆盖一行（对账方向：Redis 快照 → MySQL）。
	Upsert(ctx context.Context, row QuotaRow) error
	// InsertUsage 追加一条明细。
	InsertUsage(ctx context.Context, entry *UsageEntry) error
	// ListUsage 按时间范围与指标查明细（新→旧）。
	ListUsage(ctx context.Context, userID string, from, to time.Time, metric string, limit int) ([]UsageEntry, error)
	// SumUsage 汇总某范围内的用量（`/me/usage` 的 totals）。
	SumUsage(ctx context.Context, userID string, from, to time.Time, metric string) (int64, error)
	// ListUsersWithPeriod 列出某周期内有台账的用户（每日重建 Redis 的输入）。
	ListUsersWithPeriod(ctx context.Context, period string, limit int) ([]string, error)
	// PurgeUsageBefore 分批清理过期明细（保留期任务，docs/05-§5）。
	PurgeUsageBefore(ctx context.Context, before time.Time, limit int) (int64, error)
}

// ---- 计数器（Redis，精度加速器） ----

// QuotaCounter 是配额计数的**原子**计数器。
//
// 语义要求（这几条决定了接口长什么样）：
//
//   - `Reserve` 必须是「比较 + 自增」的**一个原子操作**。分成「先读再写」
//     会让并发的两个请求都读到 used=99 而双双通过（超发 1 次是小的，
//     但同一形状的竞态用在 `storage_bytes` 上就是超额上传）。
//   - 降级（Redis 不可用 → 本地内存计数）由**实现方**负责，见 data/redis。
//     调用方看到 err != nil 只意味着「连兜底都失败了」，那时必须拒绝请求
//     （放行等于取消配额）。
type QuotaCounter interface {
	// Reserve 尝试把 (userID,metric,period) 增加 delta；不超过 limit 时返回
	// 自增后的用量与 true。limit <= 0 表示不限制。
	Reserve(ctx context.Context, userID, metric, period string, delta, limit int64, ttl time.Duration) (used int64, ok bool, err error)
	// Add 无条件累加（可为负，用于回滚）。返回累加后的值。
	Add(ctx context.Context, userID, metric, period string, delta int64, ttl time.Duration) (int64, error)
	// Set 写入绝对值（每日「以 MySQL 为准重建 Redis」用）。
	Set(ctx context.Context, userID, metric, period string, value int64, ttl time.Duration) error
	// Get 读当前值；键不存在时 second 为 false。
	Get(ctx context.Context, userID, metric, period string) (int64, bool, error)
	// Reset 删键（下一次读会回源 MySQL）。
	Reset(ctx context.Context, userID, metric, period string) error
	// DirtyUsers 取出一批「计数发生过变化」的用户（对账任务的输入）。
	DirtyUsers(ctx context.Context, limit int) ([]string, error)
	// MarkClean 把已成功落库的用户移出脏集合。
	MarkClean(ctx context.Context, users []string) error
}

// ConcurrencyLimiter 是并发对话槽位（docs/02-§5.2 的 `concurrency`）。
//
// 与 `QuotaCounter` 分开是因为**释放**这件事只有它有：
// 槽位必须在请求结束时归还，否则一个卡住的请求会永久占用一个名额。
type ConcurrencyLimiter interface {
	// Acquire 占用一个槽位；超出 limit 时 ok=false。
	Acquire(ctx context.Context, userID string, limit int64, ttl time.Duration) (used int64, ok bool, err error)
	// Release 归还槽位（幂等：多余释放被夹到 0 并删键）。
	Release(ctx context.Context, userID string) error
}

// ConcurrencyTTL 是并发槽位键的兜底生命周期。
//
// 有 TTL 是**刻意的妥协**：槽位本来就该由 `Release` 归还，
// 但进程被 kill -9 时 defer 不会执行，那个键会永久留在 Redis 里 ——
// 用户从此每次提问都被自己的「幽灵对话」卡住，且**没有任何自愈路径**。
// 因此宁可接受「超长对话超过 1 小时后限额不再精确」，也不能接受永久锁死。
const ConcurrencyTTL = time.Hour

// ---- 快照 ----

// QuotaMetricSnapshot 是单个指标的配额快照（docs/02-§6.2 的 `metrics[]`）。
type QuotaMetricSnapshot struct {
	Metric    string
	Limit     int64
	Used      int64
	Remaining int64
	Unit      string
	// ResetAt 为 nil 表示该指标不会自然重置（存量型）。
	ResetAt *time.Time
}

// QuotaOverview 是 `/me/quota` 的业务结果。
type QuotaOverview struct {
	Plan   string
	Period QuotaPeriod
	// Metrics 按 `AllQuotaMetrics` 顺序排列。
	Metrics []QuotaMetricSnapshot
}

// Metric 按名取一个快照。
func (o *QuotaOverview) Metric(name string) (QuotaMetricSnapshot, bool) {
	if o == nil {
		return QuotaMetricSnapshot{}, false
	}
	for _, m := range o.Metrics {
		if m.Metric == name {
			return m, true
		}
	}
	return QuotaMetricSnapshot{}, false
}

// UsageTotals 是 `/me/usage` 的汇总。
type UsageTotals struct {
	ChatRequests int64
	LLMTokens    int64
}

// UsageReport 是 `/me/usage` 的业务结果。
type UsageReport struct {
	From   time.Time
	To     time.Time
	Metric string
	Items  []UsageEntry
	Totals UsageTotals
}

// ---- 服务 ----

// QuotaDeps 是 QuotaService 的依赖。
type QuotaDeps struct {
	// Repo 是 MySQL 台账（权威）。
	Repo QuotaRepo
	// Counter 是 Redis 计数器（精度加速器，自带降级）。
	Counter QuotaCounter
	// Concurrency 是并发槽位。
	Concurrency ConcurrencyLimiter
	// Users 用于解析套餐（`plan` 决定限额表）。
	Users UserRepo
	// Audit 落审计（配额超限拦截 MUST 记录，docs/05-§2.8）。可为 nil。
	Audit AuditRepo
	// PlanLimits 是 plan → metric → limit（来自配置，MUST NOT 硬编码）。
	PlanLimits map[string]map[string]int64
	// Timezone 决定自然日边界。
	Timezone *time.Location
	// ConcurrencyWait 是并发槽位的排队时长（docs/02-§5.3：默认 3s）。
	// <= 0 时用 `ConcurrencyWaitDefault`。
	ConcurrencyWait time.Duration

	Clock   nowFunc
	Log     *slog.Logger
	Metrics Metrics
	// PIIHashSalt 用于把 user_id 哈希后写进 span（docs/06-§5.1）。
	// 为空时**不写**用户属性 —— 宁可不记，也不要明文落进 Jaeger。
	PIIHashSalt string
}

// QuotaService 是配额读/校验/预扣/累加/回滚/对账的实现（docs/02-§5.2）。
type QuotaService struct {
	d QuotaDeps
}

// NewQuotaService 构造服务。
func NewQuotaService(d QuotaDeps) *QuotaService {
	d.Metrics = OrNoop(d.Metrics)
	if d.Clock == nil {
		d.Clock = time.Now
	}
	return &QuotaService{d: d}
}

func (s *QuotaService) location() *time.Location {
	if s.d.Timezone != nil {
		return s.d.Timezone
	}
	return time.UTC
}

// LimitFor 返回某套餐下某指标的限额。
//
// 返回 0 表示**不限制**，这正是三个「配置漏了」的情形（套餐不存在、
// 指标不存在、值非正）的归宿。选择「漏配 = 放行」而不是「漏配 = 拒绝」：
// 前者最坏是少收钱，后者最坏是**全体用户无法使用**；
// 而配置完整性由 `conf.Validate`（必备 `free` 档）与启动日志兜底。
func (s *QuotaService) LimitFor(plan, metric string) int64 {
	limits, ok := s.d.PlanLimits[plan]
	if !ok {
		limits, ok = s.d.PlanLimits[PlanFree]
		if !ok {
			return 0
		}
	}
	limit, ok := limits[metric]
	if !ok || limit <= 0 {
		return 0
	}
	return limit
}

// PlanFor 解析用户套餐。
//
// 查不到用户（或 plan 为空）时按 `free` 处理：**不能因为读不到套餐就放行**，
// 那等于把「数据库抖动」变成「配额失效」。
func (s *QuotaService) PlanFor(ctx context.Context, userID string) (string, error) {
	if s.d.Users == nil {
		return PlanFree, nil
	}
	u, err := s.d.Users.GetByID(ctx, userID)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return PlanFree, nil
		}
		return "", err
	}
	if u == nil || u.Plan == "" {
		return PlanFree, nil
	}
	return u.Plan, nil
}

// usedOf 读某指标的当前用量：Redis 命中优先，未命中回源 MySQL 并回填。
//
// 回填（把 MySQL 的值 `Set` 进 Redis）是必要的：不做的话，每次进程重启后的
// 第一次请求都会「看到 0」，而那一刻的限额校验就是错的。
func (s *QuotaService) usedOf(ctx context.Context, userID, metric string, period QuotaPeriod) (int64, error) {
	if s.d.Counter != nil {
		if v, ok, err := s.d.Counter.Get(ctx, userID, metric, period.Name); err == nil && ok {
			return v, nil
		}
		// 读失败不算业务错误：继续回源 MySQL（Redis 只是加速器）。
	}
	if s.d.Repo == nil {
		return 0, nil
	}
	row, err := s.d.Repo.Get(ctx, userID, metric, period.Name)
	if err != nil {
		if errors.Is(err, ErrNotFound) {
			return 0, nil
		}
		return 0, err
	}
	if s.d.Counter != nil && row != nil {
		_ = s.d.Counter.Set(ctx, userID, metric, period.Name, row.Used, s.ttlFor(period))
	}
	if row == nil {
		return 0, nil
	}
	return row.Used, nil
}

// ttlFor 返回计数键的 TTL：跨到周期结束之后一小段，供对账与「跨日瞬间」使用。
//
// 加一小时缓冲而不是「正好到周期结束」：0 点整的两三秒里，
// 上一个周期的尾巴可能还在被写（请求在 23:59:59 进来、0:00:01 才落库），
// 键提前消失会让那部分计数直接丢失。
func (s *QuotaService) ttlFor(period QuotaPeriod) time.Duration {
	if !period.Resets() {
		// 存量指标的键不设 TTL：它们的值是「当前有几个」，
		// 过期后回源 MySQL 虽然也能拿到，但那是一次本可避免的查询。
		return 0
	}
	ttl := period.End.Sub(s.d.Clock()) + time.Hour
	if ttl <= 0 {
		return time.Hour
	}
	return ttl
}

// Overview 汇总全部指标的配额快照（`GET /me/quota`）。
func (s *QuotaService) Overview(ctx context.Context, userID string) (*QuotaOverview, error) {
	plan, err := s.PlanFor(ctx, userID)
	if err != nil {
		return nil, err
	}
	at := s.d.Clock()
	out := &QuotaOverview{Plan: plan, Metrics: make([]QuotaMetricSnapshot, 0, len(AllQuotaMetrics))}
	// `period` 字段在契约里是**日周期**（docs/02-§6.2 的例子），
	// 因此取日指标的周期；存量指标在各自的 `reset_at` 里表达「不重置」。
	out.Period = s.PeriodFor(MetricChatRequests, at)

	for _, metric := range AllQuotaMetrics {
		period := s.PeriodFor(metric, at)
		used, err := s.usedOf(ctx, userID, metric, period)
		if err != nil {
			return nil, err
		}
		limit := s.LimitFor(plan, metric)
		out.Metrics = append(out.Metrics, s.snapshot(metric, limit, used, period))
	}
	return out, nil
}

// snapshot 组装单个指标的快照；remaining 的下限夹到 0。
//
// 夹到 0 而不是给出负数：负数会让客户端的进度条算出 >100% 的宽度，
// 而「用超了」这件事已经由 `limit` 与 `used` 表达清楚了。
func (s *QuotaService) snapshot(metric string, limit, used int64, period QuotaPeriod) QuotaMetricSnapshot {
	snap := QuotaMetricSnapshot{Metric: metric, Limit: limit, Used: used, Unit: QuotaUnit(metric)}
	if limit > 0 {
		if remaining := limit - used; remaining > 0 {
			snap.Remaining = remaining
		}
	}
	if period.Resets() {
		reset := period.End
		snap.ResetAt = &reset
	}
	return snap
}

// ---- 预扣 / 累加 / 回滚 ----

// QuotaReservation 是一次「已预扣 chat_requests」的凭据。
//
// 为什么要有这个对象：docs/02-§5.2 的第 ③→⑥ 步是一段**跨调用**的事务
// （先扣、调 AI、再决定累加还是回滚）。把状态放在一个显式对象里，
// 比在调用点用几个局部变量记「扣了多少、该不该还」可靠得多 ——
// 后者在新增一条 early return 时会被静默破坏。
//
// 用法固定为：`Commit` 与 `Rollback` 二者**必有其一**，且只生效一次。
type QuotaReservation struct {
	svc    *QuotaService
	userID string
	period QuotaPeriod
	used   int64
	limit  int64
	// release 是并发槽位的释放函数（可能为 nil）。
	//
	// 把它放在预约对象上而不是让调用点 `defer`：预约与槽位是一对
	// 「必须一起收尾」的资源，分成两句 defer 时任何一条提前 return
	// 都可能只收回一个。
	release func()
	once    sync.Once
}

// Used 是预扣后的用量（含本次）。
func (r *QuotaReservation) Used() int64 { return r.used }

// Period 是本次预扣所属的周期。
func (r *QuotaReservation) Period() QuotaPeriod { return r.period }

// Commit 结束本次调用：累加 token 用量并写入明细。
//
// `tokens <= 0` 时只写 `chat_requests` 明细 —— AI 侧没给 usage 是常见情况
// （部分模型/降级路径不返回），那时不该凭空补一个 0 的明细行。
func (r *QuotaReservation) Commit(ctx context.Context, tokens int64, ref UsageRef) error {
	if r == nil || r.svc == nil {
		// `svc == nil` = 「配额未接线」的空预约（见 MessageService.beginChat）：
		// 调用点不必为了「有没有配额」写分支。
		return nil
	}
	committed := false
	r.once.Do(func() {
		committed = true
		r.svc.commit(ctx, r, tokens, ref)
		r.releaseSlot()
	})
	if !committed {
		return nil
	}
	return nil
}

// Rollback 归还第 ③ 步的预扣（docs/02-§5.2 第 ⑥ 步）。
//
// **只在「网关/上游故障」时调用**：AI 明确回复参数错误或内容被过滤时
// 不归还 —— 那次提问确实消耗了配额（违规内容被拦下不等于没花额度），
// 而「失败就退还」会让客户端重试成为刷配额的手段。判定见 `ShouldRollback`。
func (r *QuotaReservation) Rollback(ctx context.Context, cause error) {
	if r == nil || r.svc == nil {
		return
	}
	r.once.Do(func() {
		r.svc.rollback(ctx, r, cause)
		r.releaseSlot()
	})
}

// releaseSlot 释放并发槽位（幂等，见 `AcquireConcurrency` 的闭包）。
func (r *QuotaReservation) releaseSlot() {
	if r == nil || r.release == nil {
		return
	}
	r.release()
}

// wired 报告本预约是否真的接了配额服务（空预约 = 未接线）。
func (r *QuotaReservation) wired() bool { return r != nil && r.svc != nil }

// ShouldRollback 判定一次 AI 失败是否应当归还预扣（docs/02-§5.2 第 ⑥ 步）。
//
// 白名单式判定：只有「本来就没得到服务」的三类才归还 ——
// 上游不可用、超时、内部错误，以及熔断拒绝（它连调用都没发出去）。
// 其余一律不归还，其中最关键的是参数错误与内容过滤：
// 它们是**正常返回的拒绝**，AI 已经把这次请求处理完了。
func ShouldRollback(err error) bool {
	if err == nil {
		return false
	}
	appErr, ok := errs.As(err)
	if !ok || appErr == nil {
		// 非 AppError（裸 error）或**类型为 `*AppError` 的 nil**（`gatewayStreamError`
		// 在「没有网关侧错误」时返回的就是它，赋给 error 接口后 `err != nil` 为真）
		// → 按「保守不归还」处理：归还错了只是少收一点钱，
		// 而这里选保守是因为无法分类的错误更可能是代码缺陷，
		// 缺陷期间不该额外放大配额消耗。
		return false
	}
	switch appErr.Code() {
	case errs.CodeAIUnavailable,
		errs.CodeAIOverloaded,
		errs.CodeAITimeout,
		errs.CodeInternalError,
		errs.CodeDependencyUnavailable,
		errs.CodeServiceShuttingDown:
		return true
	default:
		return false
	}
}

// ConcurrencyWaitDefault 是并发排队时长（docs/02-§5.3：3s）。
const ConcurrencyWaitDefault = 3 * time.Second

// StockDelta 是一次「存量指标预占」的增量。
type StockDelta struct {
	Metric string
	Delta  int64
}

// ReserveStock 预占若干**存量型**指标（`kb_count` / `documents_count` / `storage_bytes`）。
//
// 与 `ReserveChat` 的区别在于「要不要归还」：
//
//   - 次数类（chat_requests）预扣后一定被消耗（AI 已经被调用了）；
//   - 存量类（文档数、占用空间）在**AI 侧拒绝**时什么都没发生，
//     必须归还，否则用户上传一个不支持的类型就会被永久扣掉一个额度。
//
// 因此本方法返回的是 `release` 而不是 `QuotaReservation`：调用方在
// 「确认资源真的产生了」之前不该把它当已消费。归还通过 `Add(-delta)` 完成，
// 多次调用 `release` 是幂等的（由闭包里的 once 保证）。
//
// 任一指标超限则整体失败并归还已预占的部分：半个预占是有害的
// （文档数够、空间不够时会留下一个永远对不上的 `documents_count`）。
func (s *QuotaService) ReserveStock(ctx context.Context, userID string, items []StockDelta) (func(), error) {
	plan, err := s.PlanFor(ctx, userID)
	if err != nil {
		return nil, errs.New(errs.CodeDependencyUnavailable).
			WithMessage("读取套餐失败，请稍后重试").
			WithCause(err)
	}
	period := QuotaPeriod{Name: PeriodCurrent}
	ttl := s.ttlFor(period)

	var (
		once    sync.Once
		taken   []StockDelta
		release = func() {
			once.Do(func() {
				if s.d.Counter == nil {
					return
				}
				for _, item := range taken {
					if _, err := s.d.Counter.Add(context.WithoutCancel(ctx), userID,
						item.Metric, period.Name, -item.Delta, ttl); err != nil {
						s.log().WarnContext(ctx, "quota.stock_release_failed",
							slog.String("user_id", userID),
							slog.String("metric", item.Metric), slog.Any("error", err))
					}
				}
			})
		}
	)

	if s.d.Counter == nil {
		// 没有计数器：退化为「只记台账」（与 ReserveChat 同）。
		return release, nil
	}

	for _, item := range items {
		if item.Delta <= 0 {
			continue
		}
		limit := s.LimitFor(plan, item.Metric)
		used, ok, rerr := s.d.Counter.Reserve(ctx, userID, item.Metric, period.Name, item.Delta, limit, ttl)
		if rerr != nil {
			release()
			return nil, errs.New(errs.CodeDependencyUnavailable).
				WithMessage("配额服务不可用，请稍后重试").
				WithCause(rerr)
		}
		if !ok {
			release()
			s.d.Metrics.QuotaExceeded(item.Metric)
			s.auditExceeded(ctx, userID, item.Metric, limit, used, period, UsageRef{})
			return nil, s.exceeded(item.Metric, limit, used, period)
		}
		taken = append(taken, item)
	}
	return release, nil
}

// BeginChat 是「发一次提问」的配额入口：解析套餐 → 占并发槽 → 预扣次数。
//
// 三步合成一个方法而不是让调用点自己拼：
//
//   - 套餐只解析一次。分两次调用（`PlanFor` + `ReserveChat`）会让
//     「读用户表」在一条路径上跑两遍，而上游 DB 抖动时这两次可能返回
//     不同结果（例如中间刚好被降级为 free）。
//   - 顺序固定为**先占槽、后扣次**。反过来的话，并发超额时要回滚预扣，
//     而回滚是「Add -1」—— 请求量一大，多出来的那批 `-1` 会把计数刷到负数。
//     先占槽时并发超额只是「没占到槽」，不会动到配额计数。
//   - 失败时**自动**释放已占的槽（`releaseSlot`），调用点只需保证
//     `Commit` / `Rollback` 必居其一。
func (s *QuotaService) BeginChat(ctx context.Context, userID string, ref UsageRef) (*QuotaReservation, error) {
	// 一条 span 盖住「读套餐 + 占并发槽 + 预扣次数」三件事（docs/02-§5.2 的
	// ①→⑤）。不拆成三条的理由：拒结的原因分得清是靠 span 上的属性
	// （`quota.rejected`），而拆开后「到底是哪一步等了 3 秒」反而
	// 要在三个名字之间来回找。
	ctx, span := otelx.Tracer("gateway.quota").Start(ctx, "quota.check",
		trace.WithAttributes(otelx.Attr("enduser.id_hash", s.hashUser(userID))))
	defer func() { span.End() }()

	plan, err := s.PlanFor(ctx, userID)
	if err != nil {
		otelx.SpanEnd(span, err)
		return nil, errs.New(errs.CodeDependencyUnavailable).
			WithMessage("读取套餐失败，请稍后重试").
			WithCause(err)
	}
	wait := s.d.ConcurrencyWait
	if wait <= 0 {
		wait = ConcurrencyWaitDefault
	}
	release, err := s.AcquireConcurrency(ctx, userID, plan, wait)
	if err != nil {
		span.SetAttributes(otelx.Attr("quota.rejected", "concurrency"))
		return nil, err
	}
	res, err := s.ReserveChat(ctx, userID, plan, ref)
	if err != nil {
		// 配额不足：把刚占的槽位还回去，否则用户被拒几次之后
		// 自己的并发槽会被泄漏干净（表现为「额度重置了还是用不了」）。
		release()
		span.SetAttributes(otelx.Attr("quota.rejected", "chat_requests"))
		return nil, err
	}
	res.release = release
	span.SetAttributes(otelx.Attr("quota.plan", plan), otelx.Attr("quota.used", res.used))
	return res, nil
}

// hashUser 返回用户 ID 的哈希前缀（未配置盐时为空）。
//
// span 属性必须用它而不是明文 user_id：Jaeger 的访问面比数据库大得多，
// 把明文 ID 写进去等同于把用户表导出到另一个系统（docs/06-§5.1）。
func (s *QuotaService) hashUser(userID string) string {
	return otelx.UserIDHash(s.d.PIIHashSalt, userID)
}

// ReserveChat 执行 docs/02-§5.2 的第 ①→③ 步：读配额 → 校验 → 预扣。
//
// 配额不足时返回 `429 QUOTA_EXCEEDED`，且**绝不会调用 AI**（接缝 J5）：
// 调用点拿到的就是一个 error，`AIProxy` 那一行根本不会被执行。
func (s *QuotaService) ReserveChat(ctx context.Context, userID, plan string, ref UsageRef) (*QuotaReservation, error) {
	at := s.d.Clock()
	period := s.PeriodFor(MetricChatRequests, at)
	limit := s.LimitFor(plan, MetricChatRequests)
	ttl := s.ttlFor(period)

	if s.d.Counter == nil {
		// 没有计数器（未配置 Redis 视图）时退化为「只记台账」。
		return &QuotaReservation{svc: s, userID: userID, period: period, limit: limit}, nil
	}

	used, ok, err := s.d.Counter.Reserve(ctx, userID, MetricChatRequests, period.Name, 1, limit, ttl)
	if err != nil {
		// 连兜底都失败：必须拒绝，不能放行（放行等于取消配额）。
		return nil, errs.New(errs.CodeDependencyUnavailable).
			WithMessage("配额服务不可用，请稍后重试").
			WithCause(err)
	}
	if !ok {
		s.d.Metrics.QuotaExceeded(MetricChatRequests)
		s.auditExceeded(ctx, userID, MetricChatRequests, limit, used, period, ref)
		return nil, s.exceeded(MetricChatRequests, limit, used, period)
	}
	return &QuotaReservation{svc: s, userID: userID, period: period, used: used, limit: limit}, nil
}

// AcquireConcurrency 占用一个并发对话槽位（docs/02-§5.3：超额排队 3s）。
//
// 排队而不是立即拒绝：并发限额的本意是「别把 AI 打爆」，
// 而用户连点两下的第二个请求等 200ms 就能过 —— 直接 429 会让客户端
// 弹出一个毫无意义的「操作太频繁」。等满 `wait` 仍拿不到才是真的超额。
func (s *QuotaService) AcquireConcurrency(ctx context.Context, userID, plan string, wait time.Duration) (func(), error) {
	limit := s.LimitFor(plan, MetricConcurrency)
	if s.d.Concurrency == nil || limit <= 0 {
		return func() {}, nil
	}
	deadline := s.d.Clock().Add(wait)
	for {
		used, ok, err := s.d.Concurrency.Acquire(ctx, userID, limit, ConcurrencyTTL)
		if err != nil {
			return nil, errs.New(errs.CodeDependencyUnavailable).
				WithMessage("配额服务不可用，请稍后重试").
				WithCause(err)
		}
		if ok {
			released := false
			return func() {
				// 释放必须幂等且只做一次：多次 DECR 会把别人的槽位还掉。
				if released {
					return
				}
				released = true
				if err := s.d.Concurrency.Release(context.WithoutCancel(ctx), userID); err != nil {
					s.log().WarnContext(ctx, "quota.concurrency_release_failed",
						slog.String("user_id", userID), slog.Any("error", err))
				}
			}, nil
		}
		if !s.d.Clock().Before(deadline) {
			s.d.Metrics.RateLimited(RateScopeConcurrency)
			return nil, errs.New(errs.CodeRateLimited).
				WithMessage("同时进行的对话过多，请稍后重试").
				WithDetail("scope", RateScopeConcurrency).
				WithDetail("limit", limit).
				WithDetail("used", used)
		}
		// 50ms 一轮：排队 3s 最多 60 轮，代价可忽略，而轮询间隔再大
		// 会让「槽位刚空出来」的等待时间肉眼可见。
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
}

// settleTimeout 限制「结清」路径本身的最长耗时。
//
// 结清用的是 `context.WithoutCancel`（理由见 `commit`），它把 deadline 也一并去掉 ——
// 不另加超时的话，Redis/DB 卡住会让 goroutine 与连接被无限占住。
const settleTimeout = 5 * time.Second

func (s *QuotaService) commit(ctx context.Context, r *QuotaReservation, tokens int64, ref UsageRef) {
	at := s.d.Clock()
	ttl := s.ttlFor(r.period)

	// 与 `rollback` 同一条理由，但方向相反 —— 这里是**漏了**才出的线上问题：
	//
	// 流式路径的结清发生在**流结束之后**，而客户端断连（`docs/03-§5` 要求落 partial）
	// 时 `ctx` 早已被取消；`MessageService.finishStream` 正是拿原始 ctx 调进来的。
	// 直接用它会让 `Counter.Add` 与 `InsertUsage` **一起**以 `context canceled` 失败：
	// 计数与明细全丢，只留一条 WARN，而**计数正是限额依据** ——
	// 表现为「凡是断连过的对话，这次调用不计入配额」，即配额可被稳定绕过。
	// 实测证据：`quota.usage_insert_failed error="context canceled"`（stage4 的 S6 断连场景）。
	//
	// `WithoutCancel` 保留 trace_id 等值、只去掉取消信号；deadline 由 `settleTimeout` 兜。
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), settleTimeout)
	defer cancel()

	if tokens > 0 {
		if s.d.Counter != nil {
			if _, err := s.d.Counter.Add(ctx, r.userID, MetricLLMTokens, r.period.Name, tokens, ttl); err != nil {
				s.log().WarnContext(ctx, "quota.token_add_failed",
					slog.String("user_id", r.userID), slog.Any("error", err))
			}
		}
		// 明细与计数都要写：计数给限额校验用（快），明细给 `/me/usage` 用（准）。
		s.insertUsage(ctx, r.userID, MetricLLMTokens, tokens, ref, at)
	}
	s.insertUsage(ctx, r.userID, MetricChatRequests, 1, ref, at)
}

func (s *QuotaService) rollback(ctx context.Context, r *QuotaReservation, cause error) {
	if s.d.Counter == nil {
		return
	}
	// 用 `context.WithoutCancel`：回滚发生在请求失败之后，
	// 客户端的 ctx 往往已经取消，而「没扣成」这件事必须落地。
	if _, err := s.d.Counter.Add(context.WithoutCancel(ctx), r.userID, MetricChatRequests, r.period.Name, -1, s.ttlFor(r.period)); err != nil {
		s.log().WarnContext(ctx, "quota.rollback_failed",
			slog.String("user_id", r.userID), slog.Any("error", err))
	}
	s.log().InfoContext(ctx, "quota.rolled_back",
		slog.String("user_id", r.userID),
		slog.String("metric", MetricChatRequests),
		slog.String("period", r.period.Name),
		slog.Any("cause", cause))
}

func (s *QuotaService) insertUsage(ctx context.Context, userID, metric string, amount int64, ref UsageRef, at time.Time) {
	if s.d.Repo == nil || amount == 0 {
		return
	}
	entry := &UsageEntry{UserID: userID, Metric: metric, Amount: amount, Ref: ref, CreatedAt: at}
	if err := s.d.Repo.InsertUsage(ctx, entry); err != nil {
		// 明细写失败不影响主流程（计数才是限额依据），但必须留痕：
		// 它是「账实不符」的唯一线索。
		s.log().WarnContext(ctx, "quota.usage_insert_failed",
			slog.String("user_id", userID), slog.String("metric", metric),
			slog.Int64("amount", amount), slog.Any("error", err))
	}
}

func (s *QuotaService) exceeded(metric string, limit, used int64, period QuotaPeriod) *errs.AppError {
	e := errs.New(errs.CodeQuotaExceeded).
		WithDetail("metric", metric).
		WithDetail("limit", limit).
		WithDetail("used", used)
	if period.Resets() {
		// `reset_at` 逐字按契约给（docs/02-§5.2 要求 details 里带它）：
		// 客户端据此显示「明天 0 点重置」而不是一个干巴巴的「超额」。
		e = e.WithDetail("reset_at", clockx.Format(period.End))
	}
	return e
}

// auditExceeded 落一条配额超限审计（docs/05-§2.8：配额超限拦截 MUST 审计）。
//
// 审计失败只记日志：拦截本身已经完成，审计是旁路。
func (s *QuotaService) auditExceeded(ctx context.Context, userID, metric string, limit, used int64, period QuotaPeriod, ref UsageRef) {
	if s.d.Audit == nil {
		return
	}
	entries := []any{
		slog.String("user_id", userID), slog.String("metric", metric),
		slog.Int64("limit", limit), slog.Int64("used", used),
		slog.String("period", period.Name),
	}
	s.log().InfoContext(ctx, "quota.exceeded", entries...)

	// `audit_log` 没有 `trace_id` 列（docs/05-§2.8），因此把链路 id 放进 detail：
	// 「配额超限」与「当时那条链路」必须能互相跳到，否则排障时只能靠时间戳猜。
	detail := jsonOrEmpty(map[string]any{
		"metric": metric, "limit": limit, "used": used,
		"period": period.Name, "conversation_id": ref.ConversationID,
		"trace_id": ref.TraceID,
	})
	entry := &AuditLog{
		UserID:       &userID,
		Action:       AuditQuotaExceeded,
		ResourceType: "quota",
		ResourceID:   metric,
		Detail:       &detail,
		CreatedAt:    s.d.Clock(),
	}
	if err := s.d.Audit.Write(ctx, entry); err != nil {
		s.log().WarnContext(ctx, "quota.audit_failed", slog.Any("error", err))
	}
}

func (s *QuotaService) log() *slog.Logger {
	if s.d.Log != nil {
		return s.d.Log
	}
	return slog.Default()
}

// ---- 对账 ----

// Reconcile 把 Redis 快照落回 MySQL（docs/02-§5.2 第 ④ 步）。
//
// 为什么需要「脏集合」而不是扫 Redis：`KEYS` 会阻塞整个实例（Redis 单线程），
// `SCAN` 虽然不阻塞但要多次往返且需要游标状态。而计数变化**只发生在
// 少数用户**身上，用 Lua 在自增的同一次往返里 `SADD` 进脏集合，
// 成本是零（同一条命令），对了账的准确性也没有任何损失。
//
// 返回成功落库的用户数。
func (s *QuotaService) Reconcile(ctx context.Context, limit int) (int, error) {
	if s.d.Counter == nil || s.d.Repo == nil {
		return 0, nil
	}
	users, err := s.d.Counter.DirtyUsers(ctx, limit)
	if err != nil {
		return 0, err
	}
	if len(users) == 0 {
		return 0, nil
	}
	at := s.d.Clock()
	done := 0
	cleaned := make([]string, 0, len(users))
	for _, userID := range users {
		ok := true
		for _, metric := range AllQuotaMetrics {
			period := s.PeriodFor(metric, at)
			used, exists, err := s.d.Counter.Get(ctx, userID, metric, period.Name)
			if err != nil {
				ok = false
				break
			}
			if !exists {
				continue
			}
			limit := s.limitPtr(s.LimitFor(PlanFree, metric))
			if u, planErr := s.PlanFor(ctx, userID); planErr == nil {
				limit = s.limitPtr(s.LimitFor(u, metric))
			}
			row := QuotaRow{UserID: userID, Metric: metric, Period: period.Name, Used: used, Limit: limit, UpdatedAt: at}
			if err := s.d.Repo.Upsert(ctx, row); err != nil {
				s.log().WarnContext(ctx, "quota.reconcile_failed",
					slog.String("user_id", userID), slog.String("metric", metric), slog.Any("error", err))
				ok = false
				break
			}
		}
		if ok {
			done++
			cleaned = append(cleaned, userID)
		}
	}
	if len(cleaned) > 0 {
		if err := s.d.Counter.MarkClean(ctx, cleaned); err != nil {
			// 标记失败只会让下一轮重复对账（幂等），不返回错误。
			s.log().WarnContext(ctx, "quota.mark_clean_failed", slog.Any("error", err))
		}
	}
	return done, nil
}

// RebuildFromLedger 以 MySQL 为准重建 Redis 计数（docs/02-§5.2：每日 0 点）。
//
// 这个方向（MySQL → Redis）看着冗余，但它是**唯一能从缓存污染里恢复**的手段：
// Redis 被清库、被误删、或某次降级期间用了本地计数，都会让 Redis 的值
// 小于真实用量 —— 那时限额就形同虚设，而没有任何机制会自动纠正。
//
// 只重置「Redis 里已存在」的键：不存在的键本来就会回源 MySQL，
// 提前写进去只会平白多出一批迟早过期的键。
func (s *QuotaService) RebuildFromLedger(ctx context.Context, at time.Time, limit int) (int, error) {
	if s.d.Counter == nil || s.d.Repo == nil {
		return 0, nil
	}
	period := s.PeriodFor(MetricChatRequests, at)
	users, err := s.d.Repo.ListUsersWithPeriod(ctx, period.Name, limit)
	if err != nil {
		return 0, err
	}
	rebuilt := 0
	for _, userID := range users {
		for _, metric := range AllQuotaMetrics {
			p := s.PeriodFor(metric, at)
			redisVal, ok, err := s.d.Counter.Get(ctx, userID, metric, p.Name)
			if err != nil || !ok {
				continue
			}
			row, err := s.d.Repo.Get(ctx, userID, metric, p.Name)
			if err != nil || row == nil {
				continue
			}
			// 只在「Redis 落后于台账」时纠正：Redis 领先是正常情形
			// （预扣已发生、明细还没落），把它压回旧值会让用户凭空多出配额。
			if redisVal >= row.Used {
				continue
			}
			if err := s.d.Counter.Set(ctx, userID, metric, p.Name, row.Used, s.ttlFor(p)); err != nil {
				continue
			}
			rebuilt++
		}
	}
	return rebuilt, nil
}

// Usage 查询用量明细与汇总（`GET /me/usage`）。
func (s *QuotaService) Usage(ctx context.Context, userID string, from, to time.Time, metric string, limit int) (*UsageReport, error) {
	if metric != "" && !isKnownMetric(metric) {
		return nil, errs.New(errs.CodeInvalidArgument).
			WithMessage("metric 取值非法").
			WithDetail("metric", metric).
			WithDetail("allowed", AllQuotaMetrics)
	}
	if !to.After(from) {
		return nil, errs.New(errs.CodeInvalidArgument).
			WithMessage("to 必须晚于 from").
			WithDetail("from", clockx.Format(from)).
			WithDetail("to", clockx.Format(to))
	}
	if limit <= 0 || limit > PageLimitMax {
		limit = PageLimitMax
	}
	items, err := s.d.Repo.ListUsage(ctx, userID, from, to, metric, limit)
	if err != nil {
		return nil, err
	}
	report := &UsageReport{From: from, To: to, Metric: metric, Items: items}
	if metric == "" || metric == MetricChatRequests {
		total, err := s.d.Repo.SumUsage(ctx, userID, from, to, MetricChatRequests)
		if err != nil {
			return nil, err
		}
		report.Totals.ChatRequests = total
	}
	if metric == "" || metric == MetricLLMTokens {
		total, err := s.d.Repo.SumUsage(ctx, userID, from, to, MetricLLMTokens)
		if err != nil {
			return nil, err
		}
		report.Totals.LLMTokens = total
	}
	return report, nil
}

// PurgeUsageBefore 清理过期用量明细（保留期任务用，docs/05-§5）。
func (s *QuotaService) PurgeUsageBefore(ctx context.Context, before time.Time, limit int) (int64, error) {
	if s.d.Repo == nil {
		return 0, nil
	}
	return s.d.Repo.PurgeUsageBefore(ctx, before, limit)
}

func (s *QuotaService) limitPtr(v int64) *int64 {
	if v <= 0 {
		return nil
	}
	return &v
}

func isKnownMetric(metric string) bool {
	for _, m := range AllQuotaMetrics {
		if m == metric {
			return true
		}
	}
	return false
}
