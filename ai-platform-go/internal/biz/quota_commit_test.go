package biz

import (
	"context"
	"log/slog"
	"testing"
	"time"
)

// recordingCounter / recordingRepo 记录「调用时 ctx 的状态」。
//
// 唯一的目的是**证伪 `commit` 里那句 `context.WithoutCancel`**：
// 客户端断连（SSE 的 S6 场景）时 ctx 已经被取消，如果结清还用它，
// 这里的 err 就会是 `context.Canceled` —— 真实环境里表现为
// `quota.usage_insert_failed error="context canceled"`：
// 计数与明细一起丢，而计数正是限额依据 ⇒ 配额可被稳定绕过。
type recordingCounter struct {
	ctxErrs []error
	adds    []int64
}

func (c *recordingCounter) Reserve(context.Context, string, string, string, int64, int64, time.Duration) (int64, bool, error) {
	return 0, true, nil
}

func (c *recordingCounter) Add(ctx context.Context, _ string, _ string, _ string, delta int64, _ time.Duration) (int64, error) {
	c.ctxErrs = append(c.ctxErrs, ctx.Err())
	c.adds = append(c.adds, delta)
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	return delta, nil
}

func (c *recordingCounter) Set(context.Context, string, string, string, int64, time.Duration) error {
	return nil
}
func (c *recordingCounter) Get(context.Context, string, string, string) (int64, bool, error) {
	return 0, false, nil
}
func (c *recordingCounter) Reset(context.Context, string, string, string) error { return nil }
func (c *recordingCounter) DirtyUsers(context.Context, int) ([]string, error)   { return nil, nil }
func (c *recordingCounter) MarkClean(context.Context, []string) error           { return nil }

type recordingRepo struct {
	ctxErrs []error
	metrics []string
}

func (r *recordingRepo) Get(context.Context, string, string, string) (*QuotaRow, error) {
	return nil, nil
}
func (r *recordingRepo) ListByPeriod(context.Context, string, string) ([]QuotaRow, error) {
	return nil, nil
}
func (r *recordingRepo) Upsert(context.Context, QuotaRow) error { return nil }

func (r *recordingRepo) InsertUsage(ctx context.Context, entry *UsageEntry) error {
	r.ctxErrs = append(r.ctxErrs, ctx.Err())
	r.metrics = append(r.metrics, entry.Metric)
	return ctx.Err()
}

func (r *recordingRepo) ListUsage(context.Context, string, time.Time, time.Time, string, int) ([]UsageEntry, error) {
	return nil, nil
}
func (r *recordingRepo) SumUsage(context.Context, string, time.Time, time.Time, string) (int64, error) {
	return 0, nil
}
func (r *recordingRepo) ListUsersWithPeriod(context.Context, string, int) ([]string, error) {
	return nil, nil
}
func (r *recordingRepo) PurgeUsageBefore(context.Context, time.Time, int) (int64, error) {
	return 0, nil
}

// TestCommitSettlesAfterClientDisconnect 是一条**回归门禁**：
// 结清必须发生在客户端断连（ctx 已取消）之后仍然成功。
func TestCommitSettlesAfterClientDisconnect(t *testing.T) {
	counter := &recordingCounter{}
	repo := &recordingRepo{}
	svc := NewQuotaService(QuotaDeps{
		Counter: counter,
		Repo:    repo,
		Log:     slog.New(slog.NewTextHandler(discardingWriter{}, nil)),
	})

	res := &QuotaReservation{
		svc:    svc,
		userID: "u_test",
		period: QuotaPeriod{Name: "2026-09-30"},
		used:   1,
		limit:  100,
	}

	// 模拟 `finishStream` 的处境：流结束了，客户端早已离开。
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if err := res.Commit(ctx, 1234, UsageRef{ConversationID: "cv_test"}); err != nil {
		t.Fatalf("Commit 不该返回错误（结清是尽力而为的）：%v", err)
	}

	// ① 计数累加：`Add` 必须被调用，且不是在「已取消」的 ctx 上。
	if len(counter.adds) != 1 || counter.adds[0] != 1234 {
		t.Fatalf("token 计数未被累加：adds=%v", counter.adds)
	}
	if err := counter.ctxErrs[0]; err != nil {
		t.Errorf("累加用的是**已取消的** ctx（%v）—— 断连后配额计数会丢，"+
			"而计数是限额依据，等于配额可被绕过；"+
			"`commit` 里应当像 `rollback` 一样用 `context.WithoutCancel`", err)
	}

	// ② 明细两行：llm_tokens + chat_requests。
	if len(repo.metrics) != 2 {
		t.Fatalf("明细行数 = %d，期望 2（llm_tokens + chat_requests）：%v", len(repo.metrics), repo.metrics)
	}
	for i, err := range repo.ctxErrs {
		if err != nil {
			t.Errorf("明细第 %d 行（metric=%s）用的是**已取消的** ctx（%v）—— "+
				"`/me/usage` 会少掉断连那次的用量", i+1, repo.metrics[i], err)
		}
	}
}

// TestCommitWithoutTokensSkipsTokenDetail 保证 `tokens <= 0` 时**不写**
// `llm_tokens` 明细行：部分模型/降级路径不返回 usage，凭空补一个 0 的明细
// 会让 `/me/usage` 里出现无意义的行。
func TestCommitWithoutTokensSkipsTokenDetail(t *testing.T) {
	counter := &recordingCounter{}
	repo := &recordingRepo{}
	svc := NewQuotaService(QuotaDeps{
		Counter: counter,
		Repo:    repo,
		Log:     slog.New(slog.NewTextHandler(discardingWriter{}, nil)),
	})
	res := &QuotaReservation{svc: svc, userID: "u_test", period: QuotaPeriod{Name: "2026-09-30"}}

	res.Commit(context.Background(), 0, UsageRef{ConversationID: "cv_test"})

	if len(counter.adds) != 0 {
		t.Errorf("tokens=0 时不该调用 Add：adds=%v", counter.adds)
	}
	if len(repo.metrics) != 1 || repo.metrics[0] != MetricChatRequests {
		t.Errorf("tokens=0 时明细应只有 chat_requests 一行：%v", repo.metrics)
	}
}

// TestRollbackAlsoSurvivesCanceledCtx 把「回滚同样必须无视取消」也钉住：
// 回滚发生在请求失败之后，那时 ctx 几乎一定已经取消。
func TestRollbackAlsoSurvivesCanceledCtx(t *testing.T) {
	counter := &recordingCounter{}
	svc := NewQuotaService(QuotaDeps{
		Counter: counter,
		Repo:    &recordingRepo{},
		Log:     slog.New(slog.NewTextHandler(discardingWriter{}, nil)),
	})
	res := &QuotaReservation{svc: svc, userID: "u_test", period: QuotaPeriod{Name: "2026-09-30"}}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	res.Rollback(ctx, context.DeadlineExceeded)

	if len(counter.adds) != 1 || counter.adds[0] != -1 {
		t.Fatalf("回滚未被记入：adds=%v", counter.adds)
	}
	if err := counter.ctxErrs[0]; err != nil {
		t.Errorf("回滚用的是**已取消的** ctx（%v）：预扣永远还不回去", err)
	}
}

type discardingWriter struct{}

func (discardingWriter) Write(p []byte) (int, error) { return len(p), nil }
