package metricsx_test

import (
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/metricsx"
)

// docFamilies 是 `docs/06-§5.2` 指标表里由本包负责注册的族名（逐行抄录，2026-09-30）。
//
// `gw_db_pool_*` / `gw_redis_pool_*` 不在表里：它们在 `cmd/server` 里按依赖注入的
// 连接池注册（本包拿不到 `*sql.DB`），族名随驱动而定。
//
// `gw_requests_total` 有意保持惰性：它带 route×method×status 三个标签，都是开放域
// （status 尤甚），预置只能靠编造组合；而它参与的是比率型告警（`status=~"5.."` 占比），
// 不依赖「序列必须提前存在」。
var docFamilies = []string{
	"gw_request_duration_seconds",
	"gw_auth_failures_total",
	"gw_login_attempts_total",
	"gw_token_refresh_total",
	"gw_quota_exceeded_total",
	"gw_rate_limited_total",
	"gw_ai_requests_total",
	"gw_ai_request_duration_seconds",
	"gw_ai_first_token_seconds",
	"gw_ai_errors_total",
	"gw_circuit_breaker_state",
	"gw_sse_connections",
	"gw_sse_client_disconnects_total",
	"gw_message_persist_failed_total",
	"gw_session_mismatch_total",
	"gw_trace_mismatch_total",
	"gw_orphan_rows_total",
	"gw_build_info",
}

// collectedFamilies 把 registry 抓成文本，返回「出现过的族名集合」。
func collectedFamilies(t *testing.T, reg *prometheus.Registry) map[string]bool {
	t.Helper()
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather() 失败：%v", err)
	}
	got := make(map[string]bool, len(families))
	for _, f := range families {
		if len(f.GetMetric()) == 0 {
			// 没有样本的族在抓取结果里不该出现；真出现了说明注册方式有问题。
			t.Errorf("族 %s 没有任何样本", f.GetName())
			continue
		}
		got[f.GetName()] = true
	}
	return got
}

// TestAllDocFamiliesVisibleAtStartup 断言「进程刚起来、一个业务请求都没发」时
// §5.2 的全部指标族就已经能被抓到。
//
// 它挡的是一类静默失效：Prometheus 的 `*Vec` 直到某个标签组合被使用过才产出样本，
// 而未出现的指标在 `docs/06-§5.5` 的告警规则里不报错、只当空结果。更隐蔽的是
// 「一次性事件」：`gw_message_persist_failed_total` 若靠第一次失败才诞生，
// 首个样本已经是 1（丢掉了 0→1 的跳变），于是 `rate(...[5m])` 恒为 0 ——
// P1 告警永远不触发。见 `Metrics.initLabels` 的注释。
func TestAllDocFamiliesVisibleAtStartup(t *testing.T) {
	m := metricsx.New()
	// 路由维度由 `server.NewEngine` 按 gin 注册表预热（本包不知道有哪些路由）。
	m.WarmRoutes([]string{"/api/v1/me", "/api/v1/conversations/:conversation_id/messages"})
	got := collectedFamilies(t, m.Registry())

	for _, name := range docFamilies {
		if !got[name] {
			t.Errorf("启动后抓不到指标族 %s（docs/06-§5.2 要求全部可见）", name)
		}
	}
}

// TestVecFamilyPreSeededWithZero 断言预置的是真实的 0 值样本（而不是靠某个假样本
// 撑场面）：预置的计数器序列值必须恰好是 0。
//
// 不用 `prometheus/testutil`：它会拖进 `go-cmp` / `godebug` 两个新间接依赖，
// 而本仓库对 `go.mod` 的 go 指令有硬约束（`docs/08-§2`）。`Gather()` 回来的
// `*dto.MetricFamily` 直接调 getter 即可，不需要显式 import `client_model`。
func TestVecFamilyPreSeededWithZero(t *testing.T) {
	m := metricsx.New()

	families, err := m.Registry().Gather()
	if err != nil {
		t.Fatalf("Gather() 失败：%v", err)
	}

	var seen int
	for _, f := range families {
		if f.GetName() != "gw_message_persist_failed_total" {
			continue
		}
		for _, metric := range f.GetMetric() {
			seen++
			if v := metric.GetCounter().GetValue(); v != 0 {
				t.Errorf("预置样本 %v 的值 = %v，期望 0", metric.GetLabel(), v)
			}
		}
	}
	if seen != 2 {
		t.Errorf("gw_message_persist_failed_total 预置了 %d 条序列，期望 2", seen)
	}
}

// TestLabelDomainsMatchBizConstants 断言预置的标签值与 biz 侧的常量一致。
// 刻意用字面量再写一遍（而不是 import biz）：`metricsx` 是被依赖方，反向依赖会让
// 「改一个业务常量」不再被这层挡住，而标签值是跨系统契约（告警表达式按它写）。
func TestLabelDomainsMatchBizConstants(t *testing.T) {
	m := metricsx.New()

	// 限流维度：与 biz 的 RateScope* 逐个对齐。
	m.RateLimited("login_ip")
	m.RateLimited("login_account")
	m.RateLimited("message")
	m.RateLimited("upload")
	m.RateLimited("conversation")
	m.RateLimited("global_qps")
	m.RateLimited("concurrency")

	// 配额维度：与 biz 的 Metric* 逐个对齐。
	for _, metric := range []string{
		"chat_requests", "llm_tokens", "kb_count",
		"documents_count", "storage_bytes", "concurrency",
	} {
		m.QuotaExceeded(metric)
	}

	counts := map[string]int{}
	families, err := m.Registry().Gather()
	if err != nil {
		t.Fatalf("Gather() 失败：%v", err)
	}
	for _, f := range families {
		counts[f.GetName()] = len(f.GetMetric())
	}
	// 每个维度恰好一条序列：多出来的是「同义不同名」的重复维度
	// （面板上会出现两条永远只有一条在动的曲线）。
	if counts["gw_rate_limited_total"] != 7 {
		t.Errorf("gw_rate_limited_total 序列数 = %d，期望 7（限流维度只有 7 个）",
			counts["gw_rate_limited_total"])
	}
	if counts["gw_quota_exceeded_total"] != 6 {
		t.Errorf("gw_quota_exceeded_total 序列数 = %d，期望 6（配额维度只有 6 个）",
			counts["gw_quota_exceeded_total"])
	}
}

// TestLabelNormalisation 断言标签归一化的两条语义：空串归 `unknown`，
// 超长值截断但不互相合并。
func TestLabelNormalisation(t *testing.T) {
	m := metricsx.New()

	// 空串：任何维度传空都不该产出一条 `{reason=""}` 的序列。
	m.MessagePersistFailed("")
	m.MessagePersistFailed("assistant_insert")
	m.MessagePersistFailed("persist_failed")
	if cnt := familySeries(t, m, "gw_message_persist_failed_total"); cnt != 3 {
		t.Errorf("空串未归入 unknown：序列数 = %d，期望 3", cnt)
	}

	// 超长值：两个「前 32 字节相同」的路径必须各自成序列 —— 这正是 `route` 单独用
	// 更大上限 + 指纹后缀的原因（否则 `/:conversation_id` 与
	// `/:conversation_id/messages` 会被合并成一条）。
	m.ObserveRequest("/api/v1/conversations/:conversation_id", "GET", 200, time.Millisecond)
	m.ObserveRequest("/api/v1/conversations/:conversation_id/messages", "POST", 201, time.Millisecond)
	if cnt := familySeries(t, m, "gw_request_duration_seconds"); cnt != 2 {
		t.Errorf("长路由被合并：gw_request_duration_seconds 序列数 = %d，期望 2", cnt)
	}
}

func familySeries(t *testing.T, m *metricsx.Metrics, name string) int {
	t.Helper()
	families, err := m.Registry().Gather()
	if err != nil {
		t.Fatalf("Gather() 失败：%v", err)
	}
	for _, f := range families {
		if f.GetName() == name {
			return len(f.GetMetric())
		}
	}
	t.Fatalf("指标族 %s 不存在", name)
	return 0
}

// TestNilMetricsIsUsable 断言「指标禁用」= 传 nil，而不是每个调用点判空。
//
// 这是本包最容易被误解的一条：`var m *metricsx.Metrics` 上的方法调用是合法的
// （接收者是 nil），前提是每个方法开头都判了 `m == nil`。
// 漏一个就会在「关掉指标」的部署上 panic —— 而那种部署恰恰是排障时的最后手段。
func TestNilMetricsIsUsable(t *testing.T) {
	var m *metricsx.Metrics

	m.ObserveRequest("/x", "GET", 200, time.Millisecond)
	m.AuthFailure("x")
	m.LoginAttempt("ok")
	m.TokenRefresh("ok")
	m.QuotaExceeded("chat_requests")
	m.RateLimited("message")
	m.AIRequest("chat", "ok", time.Second)
	m.AIFirstToken(true, time.Second)
	m.AIError("X", 500)
	m.CircuitBreakerState("ai-platform", 0)
	m.SSEConnectionOpened()
	m.SSEConnectionClosed()
	m.SSEClientDisconnect("mid_stream")
	m.MessagePersistFailed("x")
	m.SessionMismatch()
	m.TraceMismatch()
	m.SetOrphanRows("message", 0)
	m.SetBuildInfo("v", "c", "go")

	if m.Handler() == nil {
		t.Fatal("nil Metrics 的 Handler() 不该是 nil（否则 metrics 端口会 404 且看不出原因）")
	}
	if m.Registry() != nil {
		t.Fatal("nil Metrics 的 Registry() 应为 nil")
	}
}
