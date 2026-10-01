package otelx

import (
	"context"
	"encoding/binary"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// AttrForceSample 是「强制导出这条 trace」的 span 属性键。
// 由 HTTP 中间件在 `DEBUG=true` 或请求带 `X-Debug-Trace: 1` 时打在根 span 上
// （docs/06-§5.1：这两种情况下采样率必须为 1.0）。用属性而不是 context 值，
// 是因为采样发生在 span 结束时，那时原始请求的 context 可能已取消（客户端断连），
// 而属性随 span 一起留存。
const AttrForceSample = "gw.force_sample"

// 缓冲上限。都是硬边界：这是唯一会把 span 留在内存里的地方，没有上限等价于
// 慢速内存泄漏，而「上游挂了、导出超时」恰好是最容易触发的时候（正是要排障的时刻）。
const (
	defaultMaxTraces     = 4096
	defaultMaxSpansPerTr = 256
	defaultTailTTL       = 15 * time.Second
	defaultSweepInterval = 5 * time.Second
)

// tailSampler 是尾部采样 SpanProcessor：头部一律记录，span 结束时按 trace 决定
// 是否交给下游导出器。
//
// 三条放行路径（顺序即优先级）：
//
//  1. 错误：trace 内任一 span 状态为 `Error` → 整条立即导出（docs/06-§5.1
//     「错误与降级链路 MUST 100% 采样」）。必须等「整条」而非「这个 span」：
//     最有价值的恰恰是错误之前的那些 span。
//  2. 强制：任一 span 带 `AttrForceSample=true` → 立即导出。
//  3. 完成：根 span（Server 类型且无本地父）结束 → trace 的 span 必然已全部结束，
//     可以按比例当场决策，不必等 TTL。
//
// 兜底：TTL 到期按比例导出（覆盖「父在上游服务里、本进程没有根 span」的情形）；
// 超过容量上限时淘汰最旧的一条，同样按比例决策。
//
// parentbased 语义保留：父上下文本来就带「已采样」标记时跳过比例判断，直接导出。
type tailSampler struct {
	next    sdktrace.SpanProcessor
	ratio   float64
	log     *slog.Logger
	maxTr   int
	maxSpan int
	ttl     time.Duration
	sweep   time.Duration

	// now 可注入，单测用它把 TTL 推快（否则测试要么等 15 秒，
	// 要么只能断言「没崩」—— 那不是测试）。
	now func() time.Time

	mu      sync.Mutex
	entries map[trace.TraceID]*tailEntry
	// order 是「最旧在前」的 trace 顺序，用于容量淘汰；
	// 用切片而不是每次都遍历 map 找最旧（map 无序，遍历是 O(n) 且不确定）。
	order []trace.TraceID

	stopOnce sync.Once
	stopCh   chan struct{}
	doneCh   chan struct{}

	// 便于测试与现场排查的计数器（不做持久化，只做日志与断言）。
	exported int64
	dropped  int64
	rescued  int64
}

type tailEntry struct {
	spans []sdktrace.ReadOnlySpan
	// first 是这条 trace 里**最早**收到的 span，用它拿 trace id 与父上下文。
	first sdktrace.ReadOnlySpan
	// deadline 是 TTL 兜底的时间点。
	deadline time.Time
	// sampledByParent 记录上游是否已经决定采样。
	sampledByParent bool
}

func newTailSampler(next sdktrace.SpanProcessor, ratio float64, log *slog.Logger) *tailSampler {
	t := &tailSampler{
		next:    next,
		ratio:   ratio,
		log:     log,
		maxTr:   defaultMaxTraces,
		maxSpan: defaultMaxSpansPerTr,
		ttl:     defaultTailTTL,
		sweep:   defaultSweepInterval,
		now:     time.Now,
		entries: make(map[trace.TraceID]*tailEntry),
		stopCh:  make(chan struct{}),
		doneCh:  make(chan struct{}),
	}
	go t.sweeper()
	return t
}

// OnStart 是 span 开始时的钩子：刻意什么都不做。
// 采样决策在结束时做（见类型注释），所以这里不记录、也不向下游转发 —— 转发要把
// `ReadWriteSpan` 传过去，而它只在 OnStart 期间有效，传它会诱使下游缓存失效引用。
// OTel 自带的 Batch/SimpleSpanProcessor 的 OnStart 都是空实现，不转发不影响它们；
// 而「依赖 OnStart 拿可写 span」的处理器与尾部采样语义上本就冲突。
func (t *tailSampler) OnStart(context.Context, sdktrace.ReadWriteSpan) {
}

// OnEnd 是采样决策点：把 span 暂存到它所属 trace 的缓冲区，够条件时整批导出。
//
// 决策优先级（自高到低）：`Error` 状态 → 整条 trace 无条件下发；span 带
// AttrForceSample → 无条件下发；入口 span 结束 → trace 在本进程内已完整，
// 按比例当场抽签；缓冲区超过 maxTr → 淘汰最旧的一条（被淘汰的那条 trace 会漏）。
//
// 抽签与导出都在锁外做：decide 里有随机数与日志，持锁会把「所有请求的 span 结束」
// 串行化到同一个互斥量上。
func (t *tailSampler) OnEnd(s sdktrace.ReadOnlySpan) {
	if s == nil {
		return
	}
	tid := s.SpanContext().TraceID()
	if !tid.IsValid() {
		// 无 trace id 的 span（理论上不该出现）直接放行：
		// 丢掉它只会让「有问题的 span」变少，而它本来就不占容量。
		t.forward(s)
		return
	}

	var flush []sdktrace.ReadOnlySpan
	var rescue bool

	t.mu.Lock()
	e := t.entries[tid]
	if e == nil {
		e = &tailEntry{
			first:    s,
			deadline: t.now().Add(t.ttl),
		}
		// 父上下文在「第一个 span」上最有代表性：根 span 的父就是整条 trace 的父。
		// 用 Parent() 的采样标记实现 parentbased 语义。
		if p := s.Parent(); p.IsValid() && p.IsSampled() {
			e.sampledByParent = true
		}
		t.entries[tid] = e
		t.order = append(t.order, tid)
	}
	if len(e.spans) < t.maxSpan {
		e.spans = append(e.spans, s)
	}

	errored := s.Status().Code == codes.Error
	forced := hasBoolAttr(s.Attributes(), AttrForceSample, true)

	switch {
	case errored:
		rescue = true
		flush, e.spans = e.spans, nil
		delete(t.entries, tid)
		t.removeOrderLocked(tid)
	case forced:
		flush, e.spans = e.spans, nil
		delete(t.entries, tid)
		t.removeOrderLocked(tid)
	case isEntrySpan(s):
		// trace 在本进程内已完整：当场决策。
		flush, e.spans = e.spans, nil
		delete(t.entries, tid)
		t.removeOrderLocked(tid)
	case len(t.entries) > t.maxTr:
		// 容量兜底：淘汰最旧的一条，避免无界增长。
		flush = t.evictOldestLocked()
	}
	sampledByParent := e.sampledByParent
	if rescue {
		t.rescued++
	}
	t.mu.Unlock()

	if flush == nil {
		return
	}
	// 决策在锁外做：`decide` 里有随机数与日志，持锁期间做这些会把
	// 「所有请求的 span 结束」串行化到同一个互斥量上。
	if rescue || sampledByParent || t.decide(tid) {
		t.export(flush)
		return
	}
	t.mu.Lock()
	t.dropped++
	t.mu.Unlock()
}

// isEntrySpan 判断「这个 span 是不是整条 trace 的入口」（Server 类型 + 没有本地父）。
// 无父（`!IsValid()`）时本进程就是链路起点；父是远程的（`IsRemote()`）时父在别的
// 服务里，本进程对被调用方而言同样是入口。反过来，本地父（网关内部的出站 Client
// span）不是入口：它结束时父 HTTP span 还没结束，trace 还不完整。
func isEntrySpan(s sdktrace.ReadOnlySpan) bool {
	if s.SpanKind() != trace.SpanKindServer {
		return false
	}
	p := s.Parent()
	return !p.IsValid() || p.IsRemote()
}

// Shutdown 停止后台清扫并冲刷缓冲区，然后把关闭转发给下游处理器。
// 关停时把缓冲区里的全部导出：「按比例丢弃」此时已没意义（进程要退了）。
// 可重复调用（stopOnce 保护）；等待 doneCh 时尊重 ctx，避免清扫协程卡住导致
// 整个进程无法退出。
func (t *tailSampler) Shutdown(ctx context.Context) error {
	t.stopOnce.Do(func() { close(t.stopCh) })
	select {
	case <-t.doneCh:
	case <-ctx.Done():
	}
	// 关停时把缓冲区里的全部导出：此时「按比例丢弃」已经没意义
	// （进程要退了），而这些 span 恰好是「关停前最后几秒」的链路。
	t.mu.Lock()
	var all []sdktrace.ReadOnlySpan
	for _, tid := range t.order {
		if e := t.entries[tid]; e != nil {
			all = append(all, e.spans...)
		}
	}
	t.entries = make(map[trace.TraceID]*tailEntry)
	t.order = nil
	t.mu.Unlock()
	t.export(all)

	if t.next != nil {
		return t.next.Shutdown(ctx)
	}
	return nil
}

// ForceFlush 把当前缓冲的 span 全部下发，但仍继续接收新 span。
// 与 Shutdown 的区别是不停止采样器，因此两者不能互相替代：用 Shutdown 顶替会让
// 进程收到信号后彻底停止采集；用 ForceFlush 顶替会留下清扫协程与未关闭的导出器。
func (t *tailSampler) ForceFlush(ctx context.Context) error {
	t.mu.Lock()
	var all []sdktrace.ReadOnlySpan
	for _, tid := range t.order {
		if e := t.entries[tid]; e != nil {
			all = append(all, e.spans...)
		}
	}
	t.entries = make(map[trace.TraceID]*tailEntry)
	t.order = nil
	t.mu.Unlock()
	t.export(all)
	if t.next != nil {
		return t.next.ForceFlush(ctx)
	}
	return nil
}

// ---- 内部 ----

func (t *tailSampler) sweeper() {
	defer close(t.doneCh)
	interval := t.sweep
	if interval <= 0 {
		interval = defaultSweepInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-t.stopCh:
			return
		case <-ticker.C:
			t.sweepExpired()
		}
	}
}

func (t *tailSampler) sweepExpired() {
	now := t.now()
	t.mu.Lock()
	var expired []sdktrace.ReadOnlySpan
	for len(t.order) > 0 {
		tid := t.order[0]
		e := t.entries[tid]
		if e == nil {
			t.order = t.order[1:]
			continue
		}
		if e.deadline.After(now) {
			// order 按插入顺序，后面的只会更晚，可以直接停。
			break
		}
		expired = append(expired, e.spans...)
		delete(t.entries, tid)
		t.order = t.order[1:]
	}
	t.mu.Unlock()

	// TTL 到期按比例决策：它覆盖的是「父在上游、本进程没有入口 span」的长链路。
	for _, s := range expired {
		if t.decide(s.SpanContext().TraceID()) {
			t.forward(s)
		} else {
			t.mu.Lock()
			t.dropped++
			t.mu.Unlock()
		}
	}
}

// evictOldestLocked 淘汰最旧的一条 trace 并返回它的 span。持锁调用。
func (t *tailSampler) evictOldestLocked() []sdktrace.ReadOnlySpan {
	if len(t.order) == 0 {
		return nil
	}
	tid := t.order[0]
	t.order = t.order[1:]
	e := t.entries[tid]
	if e == nil {
		return nil
	}
	delete(t.entries, tid)
	return e.spans
}

func (t *tailSampler) removeOrderLocked(tid trace.TraceID) {
	for i, id := range t.order {
		if id == tid {
			t.order = append(t.order[:i], t.order[i+1:]...)
			return
		}
	}
}

// decide 按比例决定是否采样。
// 与 OTel 自带的 `TraceIDRatioBased` 用同一种判定：取 trace id 的低 8 字节当无符号
// 整数，与 `ratio × 2^63` 比较。用 trace id 而不是随机数，是为了让同一条 trace 的
// 多次决策结果一致 —— 否则它在不同进程/不同批次里会有不同结论。
func (t *tailSampler) decide(tid trace.TraceID) bool {
	if t.ratio >= 1 {
		return true
	}
	if t.ratio <= 0 {
		return false
	}
	x := binary.BigEndian.Uint64(tid[8:]) >> 1
	return x < uint64(t.ratio*float64(uint64(1)<<63))
}

func (t *tailSampler) export(spans []sdktrace.ReadOnlySpan) {
	for _, s := range spans {
		t.forward(s)
	}
	if len(spans) > 0 {
		t.mu.Lock()
		t.exported += int64(len(spans))
		t.mu.Unlock()
	}
}

func (t *tailSampler) forward(s sdktrace.ReadOnlySpan) {
	if t.next == nil || s == nil {
		return
	}
	t.next.OnEnd(s)
}

func hasBoolAttr(attrs []attribute.KeyValue, key string, want bool) bool {
	for _, a := range attrs {
		if string(a.Key) == key && a.Value.AsBool() == want {
			return true
		}
	}
	return false
}
