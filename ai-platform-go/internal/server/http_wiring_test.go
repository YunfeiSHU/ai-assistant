package server

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
)

// TestEngineWiresIdempotencyKeyMiddleware 是**装配层**的守卫，不是中间件单测。
//
// 背景（真实踩过）：`engine.Use(IdempotencyKey())` 曾写在
// `api := engine.Group("/api/v1")` **之后**。gin 的 `RouterGroup` 在 `Group()`
// 的那一刻就把中间件链拷贝走了，之后 `engine.Use` 补的中间件不会作用到已有组上。
// 结果是：幂等键提取中间件「注册了」，但 `/api/v1` 下的路由拿不到键，
// 而 `Idempotency` 见键为空即放行 —— **幂等永远不生效，且没有任何报错**
// （那一轮 167 项验收断言里，只有幂等组的 12 项红，其余全绿）。
//
// 为什么 `idempotency_test.go` 抓不到：它自己拼了一条链（显式调用
// `IdempotencyKey()`），验证的是「中间件写对了」，不是「引擎接对了」。
// 这类问题只有「用真实引擎跑一次请求」才能发现。
func TestEngineWiresIdempotencyKeyMiddleware(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	engine := newWiringEngine(t, func(public, _, _ *gin.RouterGroup) {
		public.POST("/probe", withTestUser(), Idempotency(store, discardLogger()), h.handle(""))
	})

	body := `{"title":"a"}`

	first := doPost(t, engine, "/api/v1/probe", "wiring-key", body)
	if first.Code != http.StatusCreated {
		t.Fatalf("首次请求应 201，实际 %d：%s", first.Code, first.Body.String())
	}
	if h.calls != 1 {
		t.Fatalf("首次请求应执行 handler，实际 %d 次", h.calls)
	}

	// 第二次：同键同体必须回放。键为空时（装配缺失的现场）`Idempotency`
	// 会直接放行，于是这里看到的是「handler 又被执行了一次」。
	second := doPost(t, engine, "/api/v1/probe", "wiring-key", body)
	if h.calls != 1 {
		t.Fatalf("同键第二次必须回放而不是重新执行（handler 被调用 %d 次）—— "+
			"多半是 engine.Use(IdempotencyKey()) 又挪到 Group() 后面去了", h.calls)
	}
	if second.Code != http.StatusCreated {
		t.Fatalf("回放状态码应为 201，实际 %d", second.Code)
	}
	if second.Body.String() != first.Body.String() {
		t.Errorf("回放响应体应逐字节一致\n首次 %s\n回放 %s", first.Body.String(), second.Body.String())
	}
	if got := second.Header().Get(IdempotencyReplayHeader); got != "true" {
		t.Errorf("回放应带 %s: true，实际 %q", IdempotencyReplayHeader, got)
	}
}

// TestEngineRejectsInvalidIdempotencyKey 走真实引擎验证「非法键在中间件层被拦下」。
//
// 同理：键提取缺失时，非法键不会被任何地方检查（请求照常处理），
// 于是「字符集校验」这件事实质上失效。
func TestEngineRejectsInvalidIdempotencyKey(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	engine := newWiringEngine(t, func(public, _, _ *gin.RouterGroup) {
		public.POST("/probe", withTestUser(), Idempotency(store, discardLogger()), h.handle(""))
	})

	w := doPost(t, engine, "/api/v1/probe", "bad key!", `{"title":"a"}`)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("非法幂等键应 400，实际 %d：%s", w.Code, w.Body.String())
	}
	if h.calls != 0 {
		t.Fatal("非法键必须在中间件被拦下，不该执行 handler")
	}

	var env struct {
		Error struct {
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("错误响应不是统一信封: %v", err)
	}
	if env.Error.Details["reason"] != "idempotency_key_invalid_chars" {
		t.Errorf("details.reason 不符: %#v", env.Error.Details)
	}
}

// TestEngineIdempotencyScopedByRouteTemplate 走真实引擎确认键里存的是**路由模板**。
func TestEngineIdempotencyScopedByRouteTemplate(t *testing.T) {
	store := newFakeIdemStore()
	first := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}
	second := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_2"}}

	engine := newWiringEngine(t, func(public, _, _ *gin.RouterGroup) {
		public.POST("/probe", withTestUser(), Idempotency(store, discardLogger()), first.handle(""))
		public.POST("/probe/:probe_id/archive", withTestUser(), Idempotency(store, discardLogger()), second.handle(""))
	})

	if w := doPost(t, engine, "/api/v1/probe", "shared", `{}`); w.Code != http.StatusCreated {
		t.Fatalf("第一次请求失败: %d", w.Code)
	}
	if w := doPost(t, engine, "/api/v1/probe/cv_1/archive", "shared", `{}`); w.Code != http.StatusCreated {
		t.Fatalf("第二次请求失败: %d", w.Code)
	}
	if first.calls != 1 || second.calls != 1 {
		t.Fatalf("两个不同路由应各自执行一次，实际 %d / %d", first.calls, second.calls)
	}
	if len(store.keys()) != 2 {
		t.Fatalf("应落 2 条幂等记录，实际 %v", store.keys())
	}
	for _, k := range store.keys() {
		if strings.Contains(k, "/cv_1/") {
			t.Errorf("键里应存路由模板而不是具体路径: %s", k)
		}
	}
}

// withTestUser 注入身份（真实链路里由 Auth 中间件完成）。
func withTestUser() gin.HandlerFunc {
	return func(c *gin.Context) {
		middleware.SetUserID(c, idemTestUser)
		c.Next()
	}
}

// newWiringEngine 用真实装配函数 NewEngine 起一个最小引擎，
// 路由由调用方通过 Extra 钩子注册 —— 这样测的就是**真实的中间件链**。
func newWiringEngine(t *testing.T, extra func(public, authed, raw *gin.RouterGroup)) *gin.Engine {
	t.Helper()

	cfg := &conf.Config{App: conf.App{
		Env:           "test",
		APIPrefix:     "/api/v1",
		MaxJSONBodyMB: 1,
	}}
	return NewEngine(Deps{
		Config: cfg,
		Log:    discardLogger(),
		Extra:  extra,
	})
}
