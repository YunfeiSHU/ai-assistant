package redis

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
)

// liveRedisURL 返回真机 Redis 的地址；未显式开启时整组用例跳过。
//
// 门禁不默认开：这一步需要**真 Redis**（Lua 的返回类型、`HEVAL` 与键空间
// 都是服务端行为，假实现替不掉），而 CI 的 Go job 只跑单测。
// 本地用 `$env:GW_LIVE_REDIS='redis://127.0.0.1:6379/1'; go test ./internal/data/redis/`
// 打开它 —— 涉及配额/限流的改动 MUST 跑一遍，因为它覆盖的正是
// 「假实现会静默通过、真机才暴露」的那一层。
func liveRedisURL(t *testing.T) string {
	t.Helper()
	url := os.Getenv("GW_LIVE_REDIS")
	if url == "" {
		t.Skip("未设置 GW_LIVE_REDIS，跳过错真机的 Redis 用例")
	}
	return url
}

func openLiveStore(t *testing.T) (*Store, context.Context) {
	t.Helper()
	st, err := Open(conf.Redis{URL: liveRedisURL(t)}, slog.Default())
	if err != nil {
		t.Fatalf("打开 Redis 失败：%v", err)
	}
	ctx := context.Background()
	if err := st.Ping(ctx); err != nil {
		t.Fatalf("Redis 不可达：%v", err)
	}
	return st, ctx
}

// TestLiveScriptReturnShapes 断言两个 Lua 脚本在**真 Redis** 上的返回形态
// 分别能被 evalNumbers / evalNumber 解析。
//
// 这一条是本组用例的核心：`addScript` 返回标量、`reserveScript` 返回表，
// 用错 helper 时脚本本身不会报错，只是 Go 侧解析失败 —— 而失败会被
// 降级包装吞成一行 WARN，最终表现为「配额拦不住」。
func TestLiveScriptReturnShapes(t *testing.T) {
	st, ctx := openLiveStore(t)
	c := NewRedisQuotaCounter(st)

	key, err := QuotaKey("u_live_probe", "chat_requests", "2026-09-30")
	if err != nil {
		t.Fatalf("构造键失败：%v", err)
	}
	tokenKey, err := QuotaKey("u_live_probe", "llm_tokens", "2026-09-30")
	if err != nil {
		t.Fatalf("构造键失败：%v", err)
	}
	// 真机用例必须自清理：Lua 里的 INCRBY 会落盘，靠 t.Cleanup 之外
	// 残留的键会让**下一次**运行看到非零初值（实测被上一轮注入的 120 绊倒）。
	keys := []string{key, tokenKey}
	if err := st.Del(ctx, keys...); err != nil {
		t.Fatalf("清理旧键失败：%v", err)
	}
	t.Cleanup(func() { _ = st.Del(context.Background(), keys...) })

	// Reserve：表返回，且**上限为 1 时第二次必须被拒**。
	used, ok, err := c.Reserve(ctx, "u_live_probe", "chat_requests", "2026-09-30", 1, 1, time.Minute)
	if err != nil {
		t.Fatalf("Reserve 失败：%v", err)
	}
	if !ok || used != 1 {
		t.Fatalf("首次 Reserve 应放行且用量为 1，实际 ok=%v used=%d", ok, used)
	}
	used, ok, err = c.Reserve(ctx, "u_live_probe", "chat_requests", "2026-09-30", 1, 1, time.Minute)
	if err != nil {
		t.Fatalf("第二次 Reserve 报错：%v", err)
	}
	if ok {
		t.Fatalf("上限 1 时第二次必须被拒，实际放行（used=%d）", used)
	}

	// Add：标量返回。失败会直接体现在返回值上（而不是被降级吞掉）。
	v, err := c.Add(ctx, "u_live_probe", "llm_tokens", "2026-09-30", 120, time.Minute)
	if err != nil {
		t.Fatalf("Add 失败（若报『返回类型异常』即为脚本形态与 helper 不匹配）：%v", err)
	}
	if v != 120 {
		t.Fatalf("Add 后应返回 120，实际 %d", v)
	}
	got, found, err := c.Get(ctx, "u_live_probe", "llm_tokens", "2026-09-30")
	if err != nil || !found {
		t.Fatalf("Get 失败：found=%v err=%v", found, err)
	}
	if got != 120 {
		t.Fatalf("Get 应读回 120，实际 %d", got)
	}
}

// TestLiveReserveThenAddKeepsSingleView 断言 `Add` 不会破坏 `Reserve` 的计数视图。
//
// 这正是被修复的那条链路：`commit` 会在一次提问成功之后调用 `Add(llm_tokens)`。
// 如果 `Add` 失败（脚本形态不匹配），降级包装会把**整个计数器**切到进程内，
// 而下一次 `Reserve` 读到的本地计数是 0 —— 于是预扣形同虚设。
func TestLiveReserveThenAddKeepsSingleView(t *testing.T) {
	st, ctx := openLiveStore(t)
	primary := NewRedisQuotaCounter(st)
	deg := NewDegradedQuotaCounter(primary, slog.Default())

	user := "u_live_probe2"
	period := "2026-09-30"
	chatKey, _ := QuotaKey(user, "chat_requests", period)
	tokenKey, _ := QuotaKey(user, "llm_tokens", period)
	t.Cleanup(func() {
		_ = st.Del(context.Background(), chatKey, tokenKey)
	})
	_ = st.Del(ctx, chatKey, tokenKey)

	if _, ok, err := deg.Reserve(ctx, user, "chat_requests", period, 1, 1, time.Minute); err != nil || !ok {
		t.Fatalf("首次预扣失败：ok=%v err=%v", ok, err)
	}
	// 模拟 commit：写 token 用量。这一句在修复前会打挂计数器（降级）。
	if _, err := deg.Add(ctx, user, "llm_tokens", period, 50, time.Minute); err != nil {
		t.Fatalf("提交 token 用量失败：%v", err)
	}
	if deg.Degraded() {
		t.Fatal("计数器被降级到进程内：Add 与 Reserve 的脚本形态不一致（配额将可被绕过）")
	}
	_, ok, err := deg.Reserve(ctx, user, "chat_requests", period, 1, 1, time.Minute)
	if err != nil {
		t.Fatalf("第二次预扣报错：%v", err)
	}
	if ok {
		t.Fatal("上限 1 且已用 1 时第二次必须被拒；放行说明计数视图被拆成了两套")
	}
}

// TestLiveRateLimiterRejectsUnsafeID 断言不安全的标识在真机上是**报错**而不是静默降级。
//
// 报错是对的：调用方（中间件）必须把标识哈希化。这条用例把「靠降级兜住」
// 与「靠调用方修正」的边界钉住 —— 前者会让限流退化成进程内计数。
func TestLiveRateLimiterRejectsUnsafeID(t *testing.T) {
	st, ctx := openLiveStore(t)
	limiter := NewRedisRateLimiter(st)
	now := time.Now()

	_, _, err := limiter.Allow(ctx, "login_account", "user@example.com", 10, time.Minute, now)
	if !errors.Is(err, ErrUnsafeKeyPart) {
		t.Fatalf("含 @ 的标识应报 ErrUnsafeKeyPart，实际 %v", err)
	}

	allowed, _, err := limiter.Allow(ctx, "login_account", "0123456789abcdef", 2, time.Minute, now)
	if err != nil || !allowed {
		t.Fatalf("哈希后的标识应被放行：allowed=%v err=%v", allowed, err)
	}
	allowed, retryAfter, err := limiter.Allow(ctx, "login_account", "0123456789abcdef", 2, time.Minute, now)
	if err != nil {
		t.Fatalf("第二次判定报错：%v", err)
	}
	if !allowed {
		t.Fatal("上限 2 时第二次应放行（固定窗口计数从 1 到 2）")
	}
	allowed, retryAfter, err = limiter.Allow(ctx, "login_account", "0123456789abcdef", 2, time.Minute, now)
	if err != nil {
		t.Fatalf("第三次判定报错：%v", err)
	}
	if allowed {
		t.Fatal("上限 2 时第三次必须被拒")
	}
	if retryAfter <= 0 || retryAfter > time.Minute {
		t.Fatalf("Retry-After 应在 (0, 窗口] 内，实际 %v", retryAfter)
	}
}
