package server

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

const idemTestUser = "u_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"

// fakeIdemStore 是内存版幂等存储。必须复刻真实实现的两个关键行为，否则测试会放行真实缺陷：
// `Remember` 撞唯一键返回 `biz.ErrIdemRace`（并发同键）；
// `Recall` 过期视为未命中（真实实现额外查了 expires_at，而不是只靠清理任务）。
type fakeIdemStore struct {
	mu    sync.Mutex
	items map[string]*biz.IdempotentResponse

	recallErr   error
	rememberErr error

	recallCalls   int
	rememberCalls int
}

func newFakeIdemStore() *fakeIdemStore {
	return &fakeIdemStore{items: map[string]*biz.IdempotentResponse{}}
}

func idemStoreKey(k biz.IdempotencyKey) string {
	return strings.Join([]string{k.UserID, k.Method, k.Path, k.Key}, "|")
}

func (s *fakeIdemStore) Recall(_ context.Context, k biz.IdempotencyKey) (*biz.IdempotentResponse, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.recallCalls++
	if s.recallErr != nil {
		return nil, false, s.recallErr
	}
	got, ok := s.items[idemStoreKey(k)]
	return got, ok, nil
}

func (s *fakeIdemStore) Remember(_ context.Context, k biz.IdempotencyKey, resp *biz.IdempotentResponse) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rememberCalls++
	if s.rememberErr != nil {
		return s.rememberErr
	}
	key := idemStoreKey(k)
	if _, exists := s.items[key]; exists {
		return biz.ErrIdemRace
	}
	cp := *resp
	s.items[key] = &cp
	return nil
}

func (s *fakeIdemStore) keys() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.items))
	for k := range s.items {
		out = append(out, k)
	}
	return out
}

// countedHandler 返回一个记录被调用次数的 handler，用来分辨「回放」与「真的执行了第二次」
// —— 两者的响应体可以完全一样。
type countedHandler struct {
	calls  int
	status int
	body   any
}

func (h *countedHandler) handle(location string) gin.HandlerFunc {
	return func(c *gin.Context) {
		h.calls++
		if h.status == http.StatusNoContent {
			c.Status(http.StatusNoContent)
			return
		}
		if h.status >= 400 {
			httpx.Fail(c, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "orchestrator_not_configured"))
			return
		}
		httpx.Created(c, h.body, location)
	}
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// newIdemEngine 组装一个最小可测引擎。
//
// 关键点：路由必须挂在注入了身份的同一个组上（`middleware.UserID` 取不到用户时
// 中间件会直接跳过幂等）—— 另开一个 `e.Group("/api/v1")` 不会继承身份中间件，
// 测试会变成「幂等从来没生效」却看不出原因。
func newIdemEngine(t *testing.T, store biz.IdempotencyStore, routes map[string]gin.HandlerFunc) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)

	e := gin.New()
	g := e.Group("/api/v1")
	// 真实链路里身份由 Auth 中间件写入；这里直接注入，避免测试依赖 JWT。
	g.Use(func(c *gin.Context) {
		middleware.SetUserID(c, idemTestUser)
		c.Next()
	})
	for path, h := range routes {
		g.POST(path, IdempotencyKey(), Idempotency(store, discardLogger()), h)
	}
	return e
}

func doPost(t *testing.T, e *gin.Engine, path, key, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if key != "" {
		req.Header.Set(HeaderIdempotencyKey, key)
	}
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)
	return w
}

// ---- 回放 ----

func TestIdempotencyReplaysStoredResponse(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{
		"/conversations": h.handle("/api/v1/conversations/cv_1"),
	})

	first := doPost(t, e, "/api/v1/conversations", "key-1", `{"title":"a"}`)
	if first.Code != http.StatusCreated {
		t.Fatalf("首次请求状态码应为 201，实际 %d：%s", first.Code, first.Body.String())
	}
	if h.calls != 1 {
		t.Fatalf("首次请求应执行 handler，实际 %d 次", h.calls)
	}
	if got := first.Header().Get(IdempotencyReplayHeader); got != "" {
		t.Errorf("首次请求不该带 %s 头，实际 %q", IdempotencyReplayHeader, got)
	}
	if first.Header().Get("Location") == "" {
		t.Error("首次 201 应带 Location 头")
	}

	second := doPost(t, e, "/api/v1/conversations", "key-1", `{"title":"a"}`)
	if second.Code != http.StatusCreated {
		t.Fatalf("回放状态码应与首次一致，实际 %d：%s", second.Code, second.Body.String())
	}
	// 逐字节相等只在内嵌内存版 store 下成立。真实存储是 MySQL `JSON` 列，读写两侧会规范化
	// 文档（键序、空白），对外承诺的是语义等价 —— 验收脚本对回放体做深度比较而不是字节比较。
	if second.Body.String() != first.Body.String() {
		t.Errorf("回放响应体应与首次逐字节一致\n首次 %s\n回放 %s", first.Body.String(), second.Body.String())
	}
	if got := second.Header().Get(IdempotencyReplayHeader); got != "true" {
		t.Errorf("回放必须带 %s: true（否则「幂等生效」这件事不可观测），实际 %q", IdempotencyReplayHeader, got)
	}
	if h.calls != 1 {
		t.Fatalf("回放 MUST NOT 再次执行 handler（副作用会重复），实际执行 %d 次", h.calls)
	}
	// 回放响应没有 Location（表里不存响应头，契约只要求「响应（含状态码）」一致），
	// 这里把行为钉住，避免以后有人以为它是 bug 而在回放路径上手工造一个。
	if second.Header().Get("Location") != "" {
		t.Errorf("回放不承诺 Location，实际给出 %q", second.Header().Get("Location"))
	}
}

// TestIdempotencyReplayKeepsJSONContentType 回放的 Content-Type 必须是 JSON。
// 首次响应里它是 `c.JSON` 设的，属于另一个请求的 header map；不显式补上时
// net/http 会嗅探 body 并判成 text/plain，客户端的 res.json() 只在重试路径上失败。
func TestIdempotencyReplayKeepsJSONContentType(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	if w := doPost(t, e, "/api/v1/conversations", "key-ct", `{"title":"a"}`); w.Code != http.StatusCreated {
		t.Fatalf("首次请求失败: %d", w.Code)
	}
	w := doPost(t, e, "/api/v1/conversations", "key-ct", `{"title":"a"}`)
	if ct := w.Header().Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Fatalf("回放响应的 Content-Type 应为 JSON，实际 %q", ct)
	}
}

// TestIdempotencyReplaysErrorResponse 失败响应也必须被记住。
// 这是本机制最容易被漏掉的一半：`POST .../messages` 在 AI 不可用时返回 503，
// 而用户的提问已经落库 —— 不记住这个 503，客户端重试就会在台账里多出一条重复提问。
func TestIdempotencyReplaysErrorResponse(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusServiceUnavailable}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	first := doPost(t, e, "/api/v1/conversations", "key-503", `{"content":"你好"}`)
	if first.Code != http.StatusServiceUnavailable {
		t.Fatalf("首次应为 503，实际 %d", first.Code)
	}
	second := doPost(t, e, "/api/v1/conversations", "key-503", `{"content":"你好"}`)
	if second.Code != http.StatusServiceUnavailable {
		t.Fatalf("回放应为 503，实际 %d", second.Code)
	}
	if second.Body.String() != first.Body.String() {
		t.Error("回放错误体应与首次一致（含 details.reason）")
	}
	if second.Header().Get(IdempotencyReplayHeader) != "true" {
		t.Error("错误响应的回放同样要打标记")
	}
	if h.calls != 1 {
		t.Fatalf("503 也必须被记住，实际执行了 %d 次", h.calls)
	}
}

// ---- 同键不同体 ----

func TestIdempotencySameKeyDifferentBodyConflicts(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	if w := doPost(t, e, "/api/v1/conversations", "key-2", `{"title":"a"}`); w.Code != http.StatusCreated {
		t.Fatalf("首次请求失败: %d", w.Code)
	}

	// 同键不同体是误用：直接回放会让调用方拿到一个与本次请求无关的结果，
	// 而且看起来一切正常（正是最危险的失败形态）。
	w := doPost(t, e, "/api/v1/conversations", "key-2", `{"title":"b"}`)
	if w.Code != http.StatusConflict {
		t.Fatalf("应返回 409，实际 %d：%s", w.Code, w.Body.String())
	}

	var env errs.Envelope
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("错误响应不是统一信封: %v（%s）", err, w.Body.String())
	}
	if env.Error.Details["reason"] != "idempotency_key_reused" {
		t.Errorf("details.reason 应为 idempotency_key_reused，实际 %#v", env.Error.Details)
	}
	if h.calls != 1 {
		t.Fatalf("冲突时也不得再次执行 handler，实际 %d 次", h.calls)
	}
}

// ---- 键的作用域 ----

func TestIdempotencyDifferentKeysExecuteHandler(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	doPost(t, e, "/api/v1/conversations", "key-a", `{"title":"a"}`)
	doPost(t, e, "/api/v1/conversations", "key-b", `{"title":"a"}`)
	if h.calls != 2 {
		t.Fatalf("不同键应各自执行，实际 %d 次", h.calls)
	}
	if len(store.keys()) != 2 {
		t.Fatalf("应落 2 条幂等记录，实际 %v", store.keys())
	}
}

// TestIdempotencyScopedByRouteTemplate 同一个键在不同路由上互不影响。
// 键里用路由模板而不是具体路径：否则「同一个键被复用到另一个会话」会被误判成同一条记录
// （把正常调用当成 key 复用而报 409）。
func TestIdempotencyScopedByRouteTemplate(t *testing.T) {
	store := newFakeIdemStore()
	first := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}
	second := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_2"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{
		"/conversations": first.handle(""),
		"/conversations/:conversation_id/archive": second.handle(""),
	})

	if w := doPost(t, e, "/api/v1/conversations", "shared-key", `{"title":"a"}`); w.Code != http.StatusCreated {
		t.Fatalf("创建失败: %d", w.Code)
	}
	if w := doPost(t, e, "/api/v1/conversations/cv_1/archive", "shared-key", `{}`); w.Code != http.StatusCreated {
		t.Fatalf("归档失败: %d", w.Code)
	}

	if first.calls != 1 || second.calls != 1 {
		t.Fatalf("两个路由各自执行 1 次，实际 %d / %d", first.calls, second.calls)
	}
	keys := store.keys()
	if len(keys) != 2 {
		t.Fatalf("应落 2 条记录（路由模板不同），实际 %v", keys)
	}
	for _, k := range keys {
		if strings.Contains(k, "/cv_1/") {
			t.Errorf("键里应记录路由模板 /conversations/:conversation_id/archive，而不是具体路径: %s", k)
		}
	}
}

// ---- 无键 / 非法键 ----

func TestIdempotencyWithoutKeyIsPassthrough(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	doPost(t, e, "/api/v1/conversations", "", `{"title":"a"}`)
	doPost(t, e, "/api/v1/conversations", "", `{"title":"a"}`)
	if h.calls != 2 {
		t.Fatalf("没带键时不该做幂等，实际执行 %d 次", h.calls)
	}
	if store.recallCalls != 0 || store.rememberCalls != 0 {
		t.Fatalf("没带键时不该访问存储，实际 recall=%d remember=%d", store.recallCalls, store.rememberCalls)
	}
}

func TestIdempotencyInvalidKeyIsRejected(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	// 空格与感叹号都不在允许字符集里：它们会进 Redis Key 与数据库列。
	w := doPost(t, e, "/api/v1/conversations", "bad key!", `{"title":"a"}`)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("非法键应 400，实际 %d：%s", w.Code, w.Body.String())
	}
	var env errs.Envelope
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("错误响应不是统一信封: %v", err)
	}
	if env.Error.Details["reason"] != "idempotency_key_invalid_chars" {
		t.Errorf("details.reason 应为 idempotency_key_invalid_chars，实际 %#v", env.Error.Details)
	}
	if h.calls != 0 {
		t.Fatal("非法键应在中间件就被拦下，不该执行 handler")
	}
}

func TestIdempotencyTooLongKeyIsRejected(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	w := doPost(t, e, "/api/v1/conversations", strings.Repeat("k", maxIdempotencyKeyLen+1), `{"title":"a"}`)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("超长键应 400，实际 %d", w.Code)
	}
}

// ---- 存储不可用 / 竞态 ----

// TestIdempotencyStoreFailureIsNonBlocking 存储挂了不阻断业务（幂等是「更好」不是「前提」），
// 但会打 WARN 而不是静默降级。
func TestIdempotencyStoreFailureIsNonBlocking(t *testing.T) {
	store := newFakeIdemStore()
	store.recallErr = context.DeadlineExceeded
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	w := doPost(t, e, "/api/v1/conversations", "key-x", `{"title":"a"}`)
	if w.Code != http.StatusCreated {
		t.Fatalf("存储不可用时业务仍应成功，实际 %d：%s", w.Code, w.Body.String())
	}
}

// TestIdempotencyRememberRaceStillReturnsResponse 并发同键时：
// 本次响应照常返回（副作用已发生，无法回滚），但会记 WARN。
func TestIdempotencyRememberRaceStillReturnsResponse(t *testing.T) {
	store := newFakeIdemStore()
	store.rememberErr = biz.ErrIdemRace
	h := &countedHandler{status: http.StatusCreated, body: gin.H{"id": "cv_1"}}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	w := doPost(t, e, "/api/v1/conversations", "key-race", `{"title":"a"}`)
	if w.Code != http.StatusCreated {
		t.Fatalf("竞态不该把成功改成失败，实际 %d：%s", w.Code, w.Body.String())
	}
	if w.Header().Get(IdempotencyReplayHeader) != "" {
		t.Error("竞态路径不是回放，不该带回放头")
	}
	if w.Body.String() != `{"id":"cv_1"}` {
		t.Errorf("响应体应正常写出，实际 %s", w.Body.String())
	}
}

// ---- 缓冲不能破坏下游读体 ----

// TestIdempotencyBodyStillReadableDownstream 中间件读完体后必须把体放回去。
// 不放回去时下游 `BindJSON` 会读到空体并报「JSON 解析失败」—— 表现是
// 「加了幂等之后所有写接口都 400」。
func TestIdempotencyBodyStillReadableDownstream(t *testing.T) {
	store := newFakeIdemStore()

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{
		"/conversations": func(c *gin.Context) {
			var in struct {
				Title string `json:"title"`
			}
			if err := httpx.BindJSON(c, &in); err != nil {
				return // BindJSON 已经写出错误响应
			}
			httpx.Created(c, gin.H{"echo": in.Title}, "")
		},
	})

	w := doPost(t, e, "/api/v1/conversations", "key-body", `{"title":"标题"}`)
	if w.Code != http.StatusCreated {
		t.Fatalf("下游应能读到请求体，实际 %d：%s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "标题") {
		t.Fatalf("下游没读到原始请求体: %s", w.Body.String())
	}
}

// TestIdempotencyNoContentIsRemembered 204（无响应体）也要能回放，
// 并钉住「回放 204 时不写 body、不写 Content-Type」。
// 注意内存版 store 存的是结构体，测不出真实存储层的坑：落库用的是共享表的
// `response_body json NOT NULL`，空体会被 MySQL 判成非法 JSON（ERROR 3140），
// 所以 data 层把空体编成 JSON `null`（见 data.emptyBodyJSON）。
func TestIdempotencyNoContentIsRemembered(t *testing.T) {
	store := newFakeIdemStore()
	h := &countedHandler{status: http.StatusNoContent}

	e := newIdemEngine(t, store, map[string]gin.HandlerFunc{"/conversations": h.handle("")})

	if w := doPost(t, e, "/api/v1/conversations", "key-204", `{}`); w.Code != http.StatusNoContent {
		t.Fatalf("首次应为 204，实际 %d", w.Code)
	}
	w := doPost(t, e, "/api/v1/conversations", "key-204", `{}`)
	if w.Code != http.StatusNoContent {
		t.Fatalf("回放应为 204，实际 %d", w.Code)
	}
	if w.Body.Len() != 0 {
		t.Errorf("204 回放不该有响应体，实际 %q", w.Body.String())
	}
	if got := w.Header().Get("Content-Type"); got != "" {
		t.Errorf("204 回放不该写 Content-Type，实际 %q", got)
	}
	if h.calls != 1 {
		t.Fatalf("204 也必须被记住，实际执行 %d 次", h.calls)
	}
}

// newIdemEngineWithRecovery 与 newIdemEngine 同理，但把真实的 Recovery 中间件放在最外层 ——
// 这是唯一能测出「panic 被响应缓冲吞掉」的组装方式。
func newIdemEngineWithRecovery(t *testing.T, store biz.IdempotencyStore, path string, h gin.HandlerFunc) *gin.Engine {
	t.Helper()
	gin.SetMode(gin.TestMode)
	e := gin.New()
	e.Use(middleware.Recovery(discardLogger()))
	g := e.Group("/api/v1")
	g.Use(func(c *gin.Context) {
		middleware.SetUserID(c, idemTestUser)
		c.Next()
	})
	g.POST(path, IdempotencyKey(), Idempotency(store, discardLogger()), h)
	return e
}

// TestIdempotencyPanicIsNotSwallowedByBuffer 锁住「panic 必须变成客户端可见的 500」。
//
// 这条曾经真的写错过：还原真实 writer 的语句紧跟在 `c.Next()` 后面，而 handler panic 时
// `c.Next()` 会把栈直接掀到最外层的 `Recovery`，中间那行一行都不执行 —— 于是 Recovery 的
// 500 信封被写进缓冲且永不 flush，客户端拿到 200 + 空响应体。
// 必须靠测试钉住的原因：把错误伪装成成功比直接报错更危险，客户端会把「空 200」当成结果
// 并拿同一个键继续重试，而日志里只有一条 panic。
func TestIdempotencyPanicIsNotSwallowedByBuffer(t *testing.T) {
	store := newFakeIdemStore()
	calls := 0
	e := newIdemEngineWithRecovery(t, store, "/conversations", func(*gin.Context) {
		calls++
		panic("boom")
	})

	w := doPost(t, e, "/api/v1/conversations", "key-panic", `{}`)
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("panic 应变成 500，实际 %d（body=%q）——200 空体说明 500 被响应缓冲吞了",
			w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), string(errs.CodeInternalError)) {
		t.Errorf("500 信封里应含错误码 %s，实际 %s", errs.CodeInternalError, w.Body.String())
	}
	// panic 的 500 由外层 Recovery 生成，不在缓冲里，因此不该进幂等快照：
	// 缓存它等于把一个未知错误当成该键的最终答案钉死 24 小时。
	if got := store.keys(); len(got) != 0 {
		t.Errorf("panic 的 500 不该写幂等快照，实际写了 %v", got)
	}

	// 同键重试必须真的再执行一次（没被缓存成结果），并且仍然报 500。
	w2 := doPost(t, e, "/api/v1/conversations", "key-panic", `{}`)
	if calls != 2 {
		t.Errorf("同键重试应重新执行 handler（panic 不该被记住），实际执行 %d 次", calls)
	}
	if w2.Code != http.StatusInternalServerError {
		t.Errorf("重试仍应得到 500，实际 %d", w2.Code)
	}
}
