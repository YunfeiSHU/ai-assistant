package biz

import (
	"context"
	"sort"
	"sync"
	"time"
)

// testNow 是测试用固定时钟（UTC）。所有「时间相关」断言都基于它，
// 因此测试里不需要任何 sleep。
var testNow = func() time.Time {
	return time.Date(2026, 9, 29, 10, 0, 0, 0, time.UTC)
}

// ---- 会话仓储假实现 ----

// recordUpdate 记录一次 Update 调用（用于断言服务层到底提交了什么补丁）。
type recordUpdate struct {
	UserID string
	ID     string
	Patch  ConversationPatch
}

type fakeConvRepo struct {
	items map[string]*Conversation

	created []*Conversation
	updates []recordUpdate

	// 调用计数：用来区分「服务层没调用」与「调用了但没生效」。
	autoTitleCalls int
	updateCalls    int
	listCalls      int
	deleteCalls    int

	// 注入的失败点。
	createErr     error
	getErr        error
	updateErr     error
	autoTitleErr  error
	deleteErr     error
	listErr       error
	listResult    *ConversationList
	autoTitleDone bool // SetAutoTitle 返回的 applied 值
}

func newFakeConvRepo(seed ...*Conversation) *fakeConvRepo {
	r := &fakeConvRepo{items: map[string]*Conversation{}, autoTitleDone: true}
	for _, c := range seed {
		r.items[c.ID] = c
	}
	return r
}

func (r *fakeConvRepo) Create(_ context.Context, c *Conversation) error {
	if r.createErr != nil {
		return r.createErr
	}
	cp := *c
	r.items[c.ID] = &cp
	r.created = append(r.created, &cp)
	return nil
}

// GetOwned 返回**副本**：真实 data 层每次返回的都是新 DO，
// 服务层对返回值做原地修改（如 applyAutoTitle 里回填标题）不该影响仓储内状态。
func (r *fakeConvRepo) GetOwned(_ context.Context, userID, id string) (*Conversation, error) {
	if r.getErr != nil {
		return nil, r.getErr
	}
	c, ok := r.items[id]
	if !ok || c.UserID != userID || c.DeletedAt != nil {
		return nil, ErrNotFound
	}
	cp := *c
	return &cp, nil
}

func (r *fakeConvRepo) List(_ context.Context, userID string, in ListConversationsInput) (*ConversationList, error) {
	r.listCalls++
	if r.listErr != nil {
		return nil, r.listErr
	}
	if r.listResult != nil {
		return r.listResult, nil
	}
	var out []Conversation
	for _, c := range r.items {
		if c.UserID != userID || c.DeletedAt != nil {
			continue
		}
		if in.Status != "" && c.Status != in.Status {
			continue
		}
		out = append(out, *c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return &ConversationList{Items: out}, nil
}

func (r *fakeConvRepo) Update(_ context.Context, userID, id string, patch ConversationPatch, _ time.Time) error {
	r.updateCalls++
	if r.updateErr != nil {
		return r.updateErr
	}
	c, ok := r.items[id]
	if !ok || c.UserID != userID || c.DeletedAt != nil {
		return ErrNotFound
	}
	r.updates = append(r.updates, recordUpdate{UserID: userID, ID: id, Patch: patch})
	if patch.Title != nil {
		c.Title = *patch.Title
	}
	if patch.TitleSource != nil {
		c.TitleSource = *patch.TitleSource
	}
	if patch.Status != nil {
		c.Status = *patch.Status
	}
	if patch.Model.Present {
		if patch.Model.Value == nil {
			c.Model = nil
		} else {
			c.Model = patch.Model.Value
		}
	}
	if patch.KBIDs != nil {
		c.KBIDs = *patch.KBIDs
	}
	if patch.Pinned != nil {
		c.Pinned = *patch.Pinned
	}
	return nil
}

// SetAutoTitle 复刻 SQL 里的条件：只有仍是 auto 且标题为空才写入。
func (r *fakeConvRepo) SetAutoTitle(_ context.Context, userID, id, title string, _ time.Time) (bool, error) {
	r.autoTitleCalls++
	if r.autoTitleErr != nil {
		return false, r.autoTitleErr
	}
	c, ok := r.items[id]
	if !ok || c.UserID != userID || c.DeletedAt != nil {
		return false, ErrNotFound
	}
	if !r.autoTitleDone {
		return false, nil
	}
	if c.TitleSource != TitleSourceAuto || c.Title != "" {
		return false, nil
	}
	c.Title = title
	return true, nil
}

func (r *fakeConvRepo) SoftDelete(_ context.Context, userID, id string, at time.Time) error {
	r.deleteCalls++
	if r.deleteErr != nil {
		return r.deleteErr
	}
	c, ok := r.items[id]
	if !ok || c.UserID != userID || c.DeletedAt != nil {
		return ErrNotFound
	}
	t := at
	c.DeletedAt = &t
	return nil
}

// ---- 消息仓储假实现 ----

type fakeMsgRepo struct {
	items map[string]*Message
	// seq 按会话独立计数，模拟 `LAST_INSERT_ID(message_count + 1)`。
	seq map[string]int

	appended []*Message

	appendErr error
	// listErr 用于构造「编排降级」：读历史失败不应让整轮对话失败。
	listErr error
	// failAppendAfter = N 表示第 N 次及之后的 Append 都失败，
	// 用来构造「user 消息写成功、assistant 消息写失败」。
	failAppendAfter int
	appendCalls     int
	listCalls       int

	// appendCtxErrs 记录每次 Append 时**请求上下文**的状态。
	//
	// 存在的唯一目的是证伪 `context.WithoutCancel`：客户端断连后 ctx 已经被取消，
	// 如果落库用的还是它，这里的第 2 项就会是 `context.Canceled`。
	// 「落库到底用哪个 ctx」这件事在返回值上完全看不出来（假仓储不理会 ctx），
	// 所以必须让假仓储把它记下来。
	appendCtxErrs []error
}

func newFakeMsgRepo() *fakeMsgRepo {
	return &fakeMsgRepo{items: map[string]*Message{}, seq: map[string]int{}}
}

func (r *fakeMsgRepo) Append(ctx context.Context, _, conversationID string, m *Message) error {
	r.appendCalls++
	r.appendCtxErrs = append(r.appendCtxErrs, ctx.Err())
	if r.appendErr != nil {
		return r.appendErr
	}
	if r.failAppendAfter > 0 && r.appendCalls >= r.failAppendAfter {
		return errInvalidServer // 由 data 层包成 500；这里只要「是个错误」
	}
	// 每个会话独立自增，模拟 `LAST_INSERT_ID(message_count + 1)`。
	r.seq[conversationID]++
	m.Seq = r.seq[conversationID]
	cp := *m
	r.items[m.ID] = &cp
	r.appended = append(r.appended, &cp)
	return nil
}

func (r *fakeMsgRepo) GetOwned(_ context.Context, userID, id string) (*Message, error) {
	m, ok := r.items[id]
	if !ok || m.UserID != userID {
		return nil, ErrNotFound
	}
	cp := *m
	return &cp, nil
}

func (r *fakeMsgRepo) ListByConversation(_ context.Context, userID, conversationID string, in ListMessagesInput) (*MessageList, error) {
	r.listCalls++
	if r.listErr != nil {
		return nil, r.listErr
	}
	var out []Message
	for _, m := range r.items {
		if m.UserID == userID && m.ConversationID == conversationID {
			out = append(out, *m)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if in.Order == OrderAsc {
			return out[i].Seq < out[j].Seq
		}
		return out[i].Seq > out[j].Seq
	})
	// 必须按 Limit 截断：真实仓储是 `ORDER BY seq DESC LIMIT n`，
	// 假实现若忽略 Limit，会让「历史上限」这类测试**看起来通过**
	// （拿到全量历史却断言了别的字段），而实际上限根本没生效。
	if in.Limit > 0 && len(out) > in.Limit {
		out = out[:in.Limit]
	}
	return &MessageList{Items: out}, nil
}

func (r *fakeMsgRepo) Delete(_ context.Context, userID, id string) error {
	m, ok := r.items[id]
	if !ok || m.UserID != userID {
		return ErrNotFound
	}
	delete(r.items, id)
	return nil
}

// ---- 编排器假实现 ----

type fakeOrchestrator struct {
	result *ChatResult
	err    error

	calls int
	got   ChatRequest
}

func (o *fakeOrchestrator) Chat(_ context.Context, req ChatRequest) (*ChatResult, error) {
	o.calls++
	o.got = req
	if o.err != nil {
		return nil, o.err
	}
	return o.result, nil
}

// errInvalidServer 是「不可归类的服务端错误」，用来验证服务层会把它包成 500 而不是吞掉。
var errInvalidServer = errServer("boom")

type errServer string

func (e errServer) Error() string { return string(e) }

// ---- 流式编排假实现（M4）----

// fakeEventStream 是一条可控的事件流。
//
// 刻意做成「可推、可结束、可保持打开」三态，而不是「用一个 slice 造好再关掉」：
// 流式的关键行为（心跳、首字节超时、空闲超时、退出、断连）**只在流保持打开时**
// 才有意义，一次性关掉的假流根本走不到那些分支 —— 测试会全绿而 bug 还在。
type fakeEventStream struct {
	events chan StreamEvent

	err      error
	closeErr error
	closed   int
}

func newFakeEventStream(buf int, events ...StreamEvent) *fakeEventStream {
	s := newOpenFakeEventStream(buf)
	for _, ev := range events {
		s.events <- ev
	}
	s.finish(nil)
	return s
}

// newOpenFakeEventStream 返回一条**永不自动结束**的流（由测试自己 push/finish）。
func newOpenFakeEventStream(buf int) *fakeEventStream {
	if buf <= 0 {
		buf = 8
	}
	return &fakeEventStream{events: make(chan StreamEvent, buf)}
}

func (s *fakeEventStream) push(ev StreamEvent) { s.events <- ev }

// finish 关闭事件通道并设定 `Err()` 的返回值（nil = 正常收完）。
func (s *fakeEventStream) finish(err error) {
	s.err = err
	close(s.events)
}

func (s *fakeEventStream) Events() <-chan StreamEvent { return s.events }
func (s *fakeEventStream) Err() error                 { return s.err }

func (s *fakeEventStream) Close() error {
	s.closed++
	return s.closeErr
}

type fakeStreamer struct {
	stream ChatEventStream
	err    error

	calls int
	got   ChatRequest
}

func (s *fakeStreamer) ChatStream(_ context.Context, req ChatRequest) (ChatEventStream, error) {
	s.calls++
	s.got = req
	if s.err != nil {
		return nil, s.err
	}
	return s.stream, nil
}

// fakeSink 记录下发过的帧。
//
// `sendErrAfter` 模拟「写到第 N 帧时客户端已经走了」（真实原因是 RST，
// 在单测里无法构造，只能注入写失败 —— 而这条路径恰恰是「必须落 partial」那条）。
//
// **加锁的理由**：`Send` 由 `StreamSend` 的泵 goroutine 调用，而测试要在
// 流还没结束时轮询「到手几帧了」（否则「等到第一帧再制造中断」这件事没法表达）。
// 不加锁时那个轮询与 `append` 并发，是真实的数据竞争。
// 收尾后的直接字段读取（`sent` / `pings` / `sendCalls`）不需要锁：
// 它们发生在 `wait()` 返回之后，而 `wait()` 通过 channel 收到了 goroutine 的结束。
type fakeSink struct {
	mu    sync.Mutex
	sent  []StreamEvent
	pings int

	started   bool
	sendCalls int
	sendErr   error
	sendErrAt int // >0 表示从第 N 次 Send 起失败
	pingErr   error
}

func (s *fakeSink) Send(ev StreamEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sendCalls++
	if s.sendErr != nil && (s.sendErrAt <= 0 || s.sendCalls >= s.sendErrAt) {
		return s.sendErr
	}
	s.sent = append(s.sent, ev)
	s.started = true
	return nil
}

func (s *fakeSink) Ping() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pings++
	if s.pingErr != nil {
		return s.pingErr
	}
	// 与 `Send` 一样置 `started`：心跳也会把响应头写出去，
	// 因此对「首字节超时还能不能回 504」这件事，两者是等价的。
	s.started = true
	return nil
}

func (s *fakeSink) Started() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.started
}

// count 返回已下发帧数（并发安全，供测试在流进行中轮询）。
func (s *fakeSink) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sent)
}

// last 返回最后一帧（没有帧时返回 nil），省掉每个用例里的长度判断。
func (s *fakeSink) last() StreamEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.sent) == 0 {
		return nil
	}
	return s.sent[len(s.sent)-1]
}

// events 返回下发过的帧名序列（`ping` 不计入：它不是 `StreamEvent`）。
func (s *fakeSink) events() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.sent))
	for _, ev := range s.sent {
		out = append(out, ev.EventName())
	}
	return out
}
