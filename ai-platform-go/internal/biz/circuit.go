package biz

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// 熔断状态（取值与 `gw_circuit_breaker_state` 的 0/1/2 一致，docs/06-§5.2）。
const (
	// CircuitStateClosed 关闭：正常放行。
	CircuitStateClosed = 0
	// CircuitStateHalfOpen 半开：只放行一个探针。
	CircuitStateHalfOpen = 1
	// CircuitStateOpen 打开：直接拒绝，**不发起调用**。
	CircuitStateOpen = 2
)

// CircuitSnapshot 是熔断器的只读快照（`/health` 与日志用）。
type CircuitSnapshot struct {
	State               int
	ConsecutiveFailures int
	// RetryAfter 是「还有多久进入半开」，仅在打开时有意义。
	RetryAfter time.Duration
}

// StateName 返回状态的可读名（日志里用数字看不出所以然）。
func (s CircuitSnapshot) StateName() string {
	switch s.State {
	case CircuitStateOpen:
		return "open"
	case CircuitStateHalfOpen:
		return "half_open"
	default:
		return "closed"
	}
}

// AICircuitBreaker 是对 ai-platform 的**整体**熔断器（docs/04-§3.4）。
//
// 刻意只做一个（不分接口）：网关对 AI 的调用共享同一个进程、同一份配额、
// 同一个上游故障域，分成 N 个熔断器只会让「AI 全挂」时每个入口各自慢 10 次。
//
// 三条容易做错的约定：
//
//  1. **健康检查不参与统计**（docs/04-§3.4 明写）。健康探测本身要能反映
//     「AI 是否活着」，若它也算失败，熔断打开期间探测会一直失败，
//     于是熔断**永远无法自行恢复**。
//  2. 只有「上游故障」计数。参数错误、内容过滤是 AI **正常返回**的结果，
//     把它们算进去会让一个乱填参数的客户端把整个网关熔断。
//  3. 半开只放**一个**探针。放 N 个等于在还没恢复时把压力原样打回去，
//     这正是熔断要避免的事。
type AICircuitBreaker struct {
	target           string
	failureThreshold int
	openFor          time.Duration
	clock            nowFunc
	metrics          Metrics
	log              *slog.Logger

	mu        sync.Mutex
	state     int
	failures  int
	openedAt  time.Time
	probeOpen bool
}

// NewAICircuitBreaker 构造熔断器。
//
// `failureThreshold <= 0` 或 `openFor <= 0` 时返回 nil：配置为 0 表示
// **显式关闭熔断**（本地开发时不想被熔断挡住），而不是「立刻打开」。
// 用 nil 表示「关闭」而不是用一个永远放行的对象，让调用点只判一次 nil。
func NewAICircuitBreaker(target string, failureThreshold int, openFor time.Duration, clock nowFunc, metrics Metrics, log *slog.Logger) *AICircuitBreaker {
	if failureThreshold <= 0 || openFor <= 0 {
		return nil
	}
	if clock == nil {
		clock = time.Now
	}
	if target == "" {
		target = CircuitTargetAI
	}
	metrics = OrNoop(metrics)
	b := &AICircuitBreaker{
		target:           target,
		failureThreshold: failureThreshold,
		openFor:          openFor,
		clock:            clock,
		metrics:          metrics,
		log:              log,
		state:            CircuitStateClosed,
	}
	// 启动时上报一次 closed：否则面板上在「第一次失败之前」没有这条曲线，
	// 而告警规则 `== 2` 在缺失时会静默判为「没问题」。
	metrics.CircuitBreakerState(target, CircuitStateClosed)
	return b
}

// circuitEvent 是一次「已决定、但还没落盘」的状态变化日志。
//
// 把「决定」与「写日志」拆开是为了让日志 I/O 发生在**解锁之后**：
// 写日志可能阻塞（管道满了、磁盘慢、`Out-File` 的读者跟不上），
// 而 `b.mu` 保护的是「是否放行」这条热路径。持锁写日志会把
// 「日志慢」放大成「整个 AI 调用路径不可用」。
//
// 字段是**值拷贝**而不是解锁后再读 `b.state`：后者是数据竞争。
type circuitEvent struct {
	msg      string
	reason   string
	state    int
	failures int
}

func (e circuitEvent) empty() bool { return e.msg == "" }

// stateName 把状态码转成日志里可读的名字。
//
// 用「构造一个临时 Snapshot」而不是 `b.Snapshot()`：后者要加锁，
// 而调用点全都在持锁区里（见 `emit` 的注释）。
func stateName(state int) string {
	return CircuitSnapshot{State: state}.StateName()
}

// Allow 询问是否可以发起调用。
//
// 返回 true 时调用方**必须**在结束时调用 `OnSuccess` 或 `OnFailure`
// （半开态的探针名额靠这个归还，漏掉会让半开永久卡住）。
func (b *AICircuitBreaker) Allow() bool {
	if b == nil {
		return true
	}
	allowed, ev := b.allowLocked()
	// ⚠️ 必须在解锁后 emit：`emit` 会写日志，而日志可能阻塞。
	b.emit(ev)
	return allowed
}

func (b *AICircuitBreaker) allowLocked() (bool, circuitEvent) {
	b.mu.Lock()
	defer b.mu.Unlock()

	now := b.clock()
	switch b.state {
	case CircuitStateOpen:
		if now.Sub(b.openedAt) < b.openFor {
			return false, circuitEvent{}
		}
		// 冷却结束：转入半开并把这个名额给当前请求当探针。
		b.state = CircuitStateHalfOpen
		b.probeOpen = true
		b.metrics.CircuitBreakerState(b.target, CircuitStateHalfOpen)
		return true, circuitEvent{
			msg: "circuit.half_open", reason: "进入半开，放行一个探针",
			state: CircuitStateHalfOpen, failures: b.failures,
		}
	case CircuitStateHalfOpen:
		if b.probeOpen {
			return false, circuitEvent{}
		}
		b.probeOpen = true
		return true, circuitEvent{}
	default:
		return true, circuitEvent{}
	}
}

// OnSuccess 上报一次成功。
func (b *AICircuitBreaker) OnSuccess() {
	if b == nil {
		return
	}
	b.emit(b.onSuccessLocked())
}

func (b *AICircuitBreaker) onSuccessLocked() circuitEvent {
	b.mu.Lock()
	defer b.mu.Unlock()
	wasOpen := b.state != CircuitStateClosed
	b.state = CircuitStateClosed
	b.failures = 0
	b.probeOpen = false
	if !wasOpen {
		return circuitEvent{}
	}
	b.metrics.CircuitBreakerState(b.target, CircuitStateClosed)
	return circuitEvent{
		msg: "circuit.closed", reason: "上游恢复，熔断关闭",
		state: CircuitStateClosed, failures: 0,
	}
}

// OnFailure 上报一次**上游故障**。非上游故障不要调它（见类型注释第 2 条）。
func (b *AICircuitBreaker) OnFailure() {
	if b == nil {
		return
	}
	b.emit(b.onFailureLocked())
}

func (b *AICircuitBreaker) onFailureLocked() circuitEvent {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.probeOpen = false

	if b.state == CircuitStateHalfOpen {
		// 探针失败：立刻回到打开并重置冷却计时。
		// 重置是有意的 —— 否则「冷却已在半开前耗尽」会让下一次 Allow
		// 立刻又放一个探针，等于取消熔断。
		return b.openLocked()
	}
	b.failures++
	if b.failures >= b.failureThreshold {
		return b.openLocked()
	}
	return circuitEvent{}
}

func (b *AICircuitBreaker) openLocked() circuitEvent {
	b.state = CircuitStateOpen
	b.openedAt = b.clock()
	b.failures = b.failureThreshold
	b.metrics.CircuitBreakerState(b.target, CircuitStateOpen)
	return circuitEvent{
		msg: "circuit.open", reason: "上游连续失败，熔断打开",
		state: CircuitStateOpen, failures: b.failures,
	}
}

// Snapshot 返回只读状态（`/health` 与排障用）。
func (b *AICircuitBreaker) Snapshot() CircuitSnapshot {
	if b == nil {
		// 熔断未启用时也返回一个合法视图：`/health` 不必写 if。
		return CircuitSnapshot{State: CircuitStateClosed}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	snap := CircuitSnapshot{State: b.state, ConsecutiveFailures: b.failures}
	if b.state == CircuitStateOpen {
		if remain := b.openFor - b.clock().Sub(b.openedAt); remain > 0 {
			snap.RetryAfter = remain
		}
	}
	return snap
}

// State 返回状态码。
func (b *AICircuitBreaker) State() int { return b.Snapshot().State }

// emit 打印一次状态变化。
//
// ⚠️ **必须在释放 `b.mu` 之后调用**，理由有两条：
//
//  1. 它读 `b.state` / `b.failures`，持锁区里读会数据竞争，所以
//     这些值必须由 `circuitEvent` 带出来（值拷贝）。
//  2. 日志写入可能阻塞（管道满、磁盘慢），而 `b.mu` 保护的是
//     「是否放行」这条热路径；持锁写日志会把「日志慢」放大成
//     「整个 AI 调用路径不可用」。
//
// 历史缺陷（2026-09-30 实测，P0）：这里原先调的 `logf` 内部用
// `b.Snapshot().StateName()` 取状态名，而 `Snapshot` 也要 `b.mu.Lock()`。
// `sync.Mutex` 不可重入，于是**连续 10 次上游失败 → 熔断打开 → 死锁**：
// 第一次状态变化就把锁永久锁死，后续所有请求连 `Allow()` 都进不去，
// 网关对 AI 的调用在「最该工作的那一次」全线不可用，且**一行日志都没有**
// （卡住的正是写日志那一步）。三个调用点（open / half_open / closed）
// 全都在持锁区，所以这不是偶发 —— 熔断器只要需要写一次日志就必死。
func (b *AICircuitBreaker) emit(ev circuitEvent) {
	if ev.empty() || b == nil || b.log == nil {
		return
	}
	b.log.Warn(ev.msg,
		slog.String("target", b.target),
		slog.String("state", stateName(ev.state)),
		slog.Int("failures", ev.failures),
		slog.String("reason", ev.reason))
}

// ---- 与 AI 交互的错误分类 ----

// isUpstreamFailure 报告该错误是否算「上游故障」（参与熔断统计）。
//
// 白名单而不是黑名单：新增错误码时默认**不**参与熔断，
// 而黑名单（默认参与）会在新增一个「客户端错误」码时立刻让熔断行为变坏。
//
// 特别排除 `INTERNAL_ERROR`：那是网关自己的缺陷，10 次之后打开熔断
// 既不能修好缺陷，又会让 AI 明明活着却不可用 —— 把两个故障域混在一起。
func isUpstreamFailure(err error) bool {
	if err == nil {
		return false
	}
	appErr, ok := errs.As(err)
	if !ok {
		// 裸 error 只可能来自「连不上」这类传输层问题（data/ai 会包装，
		// 但保留这一支以防漏包）：算上游故障。
		return true
	}
	switch appErr.Code() {
	case errs.CodeAIUnavailable,
		errs.CodeAIOverloaded,
		errs.CodeAITimeout,
		errs.CodeDependencyUnavailable:
		return true
	default:
		return false
	}
}

// circuitReject 是熔断打开时对外抛出的错误。
//
// `retryable=true` + `Retry-After`：客户端知道「稍后再试有用」，
// 而不是像收到 400 那样去改参数。
func circuitReject(retryAfter time.Duration) *errs.AppError {
	seconds := int(retryAfter.Seconds())
	if seconds <= 0 {
		seconds = 1
	}
	return errs.New(errs.CodeAIUnavailable).
		WithMessage("AI 服务暂时不可用，请稍后重试").
		WithDetail("reason", "circuit_open").
		WithRetryAfter(seconds)
}

// ---- 装饰器 ----

// circuitOrchestrator 给 `ChatOrchestrator` 套上熔断。
type circuitOrchestrator struct {
	next ChatOrchestrator
	cb   *AICircuitBreaker
	m    Metrics
}

// NewCircuitChatOrchestrator 包装非流式对话入口。
//
// 装饰器模式而不是把熔断判断写进 `MessageService`：熔断是**传输层策略**，
// 与「会话、消息、引用」这些业务概念无关；写进业务层会让每个新增的
// AI 调用点都要记得加一次判断。
func NewCircuitChatOrchestrator(next ChatOrchestrator, cb *AICircuitBreaker, m Metrics) ChatOrchestrator {
	if cb == nil {
		return next
	}
	return &circuitOrchestrator{next: next, cb: cb, m: OrNoop(m)}
}

// Chat 在熔断器保护下执行一次非流式对话。
//
// 契约要点：
//   - 熔断打开时**不发起**真实调用，直接返回带 Retry-After 的 AI_OVERLOADED
//     （S7 的核心断言：AI 挂掉后网关不该每个请求都去撞一次连接超时）；
//   - 只有上游类故障（5xx/限流/网络/超时）才计入熔断失败；
//     客户端类错误与业务拒绝仍记一次「调用发生了」，因为它们属于成功率的分母。
func (o *circuitOrchestrator) Chat(ctx context.Context, req ChatRequest) (*ChatResult, error) {
	// 一次 AI 调用一条 span。名字与指标标签（`operation=chat`）刻意保持一致：
	// 从「面板上这条曲线不对」到「Jaeger 里那批慢请求」之间的翻译
	// 不需要额外知识。
	ctx, span := otelx.Tracer("gateway.ai").Start(ctx, "ai.chat",
		trace.WithSpanKind(trace.SpanKindClient))
	defer func() { span.End() }()

	if !o.cb.Allow() {
		// **没有发起调用**这一点是 S7 的核心断言：AI 挂掉后网关不该
		// 每个请求都去撞一次连接超时（那会让 p99 变成 3 秒）。
		retryAfter := o.cb.Snapshot().RetryAfter
		o.m.AIRequest(AIOpChat, MetricResultRejected, 0)
		span.SetAttributes(otelx.Attr("ai.rejected", "circuit_open"))
		span.SetStatus(codes.Error, "circuit_open")
		return nil, circuitReject(retryAfter)
	}
	start := time.Now()
	res, err := o.next.Chat(ctx, req)
	elapsed := time.Since(start)
	span.SetAttributes(otelx.Attr("ai.elapsed_ms", elapsed.Milliseconds()))
	if err != nil {
		span.SetAttributes(otelx.Attr("ai.code", codeOf(err)))
		if isUpstreamFailure(err) {
			o.cb.OnFailure()
			o.m.AIRequest(AIOpChat, failureResult(err), elapsed)
			o.m.AIError(codeOf(err), upstreamStatusOf(err))
			otelx.SpanEnd(span, nil)
			return nil, err
		}
		// 客户端类错误：不算故障，但仍记一次「调用发生了」——
		// 成功率的分母包含它们才是真实的（把它们算成失败会让成功率虚低）。
		o.m.AIRequest(AIOpChat, MetricResultOK, elapsed)
		// 业务错误（参数不对、内容被过滤）不是链路故障：span 状态保持 Unset，
		// 否则 Jaeger 的「错误链路」里会混进一堆完全正常的拒绝。
		return nil, err
	}
	o.cb.OnSuccess()
	o.m.AIRequest(AIOpChat, MetricResultOK, elapsed)
	return res, nil
}

// circuitStreamer 给 `ChatStreamer` 套上熔断。
type circuitStreamer struct {
	next ChatStreamer
	cb   *AICircuitBreaker
	m    Metrics
}

// NewCircuitChatStreamer 包装流式对话入口。
func NewCircuitChatStreamer(next ChatStreamer, cb *AICircuitBreaker, m Metrics) ChatStreamer {
	if cb == nil {
		return next
	}
	return &circuitStreamer{next: next, cb: cb, m: OrNoop(m)}
}

// ChatStream 在熔断器保护下建立流式对话。
//
// 熔断语义与 Chat 相同，但计数对象是**流的建立**：一旦返回了流，
// 后续中途失败由 StreamFailureReporter 上报，不在这里判断
// （那时再改熔断状态已经晚了，也没有第二个响应可以返回）。
func (s *circuitStreamer) ChatStream(ctx context.Context, req ChatRequest) (ChatEventStream, error) {
	ctx, span := otelx.Tracer("gateway.ai").Start(ctx, "ai.chat_stream",
		trace.WithSpanKind(trace.SpanKindClient))
	defer func() { span.End() }()

	if !s.cb.Allow() {
		retryAfter := s.cb.Snapshot().RetryAfter
		s.m.AIRequest(AIOpChatStream, MetricResultRejected, 0)
		span.SetAttributes(otelx.Attr("ai.rejected", "circuit_open"))
		span.SetStatus(codes.Error, "circuit_open")
		return nil, circuitReject(retryAfter)
	}
	start := time.Now()
	stream, err := s.next.ChatStream(ctx, req)
	elapsed := time.Since(start)
	span.SetAttributes(otelx.Attr("ai.elapsed_ms", elapsed.Milliseconds()))
	if err != nil {
		span.SetAttributes(otelx.Attr("ai.code", codeOf(err)))
		if isUpstreamFailure(err) {
			s.cb.OnFailure()
			s.m.AIRequest(AIOpChatStream, failureResult(err), elapsed)
			s.m.AIError(codeOf(err), upstreamStatusOf(err))
			otelx.SpanEnd(span, nil)
			return nil, err
		}
		s.m.AIRequest(AIOpChatStream, MetricResultOK, elapsed)
		return nil, err
	}
	s.m.AIRequest(AIOpChatStream, MetricResultOK, elapsed)
	return &circuitStream{inner: stream, cb: s.cb, m: s.m}, nil
}

// circuitStream 是「观测到结论后上报熔断」的流包装。
//
// 为什么必须在流上观测而不是在 `ChatStream` 返回处：**流式的失败大多发生在
// 建流之后**（上游在建流后立刻报错、或半路断流）。只看建流那一刻的结果，
// 熔断器对「AI 一开流就挂」这种最常见的故障完全无感。
type circuitStream struct {
	inner ChatEventStream
	cb    *AICircuitBreaker
	m     Metrics

	once sync.Once
}

// Events 透传事件通道。
func (s *circuitStream) Events() <-chan StreamEvent { return s.inner.Events() }

// Err 透传终止原因，并把结论上报给熔断器。
//
// 契约保证 `Err()` 只在通道关闭后读，因此此时 nil **确实**意味着正常结束
// （上游发完了 `done`）；不需要额外探测通道状态 —— 那需要非阻塞读，
// 而在「还没读完」的时候去读会**吃掉一个事件**，是个静默的数据丢失陷阱。
func (s *circuitStream) Err() error {
	err := s.inner.Err()
	s.once.Do(func() { s.report(err) })
	return err
}

// Close 透传关闭；若调用方提前放弃（超时/断连），这里不猜结论。
func (s *circuitStream) Close() error {
	err := s.inner.Close()
	s.once.Do(func() {
		// 提前放弃（客户端断连、超时）时 `inner.Err()` 往往是 nil，
		// 把它记成「成功」会掩盖「AI 卡住」这类真实故障；而记成失败
		// 又会把「用户自己按了取消」算到 AI 头上。两难的根源是
		// 装饰器看不到调用方的判定，因此这里**只有在上游确实给了错误时**
		// 才表态，超时类故障由编排层显式上报（`ReportStreamFailure`）。
		if innerErr := s.inner.Err(); innerErr != nil {
			s.report(innerErr)
		}
	})
	return err
}

// ReportStreamFailure 让编排层把「流级故障」显式告诉熔断器。
//
// 覆盖的是装饰器看不到的三条路径：首字节超时、空闲超时、整轮超时。
// 它们都意味着「上游活着但不出活」，正是熔断应当响应的形态。
func (s *circuitStream) ReportStreamFailure(reason string) {
	s.once.Do(func() { s.reportTimeout(reason) })
}

func (s *circuitStream) reportTimeout(reason string) {
	s.cb.OnFailure()
	s.m.AIRequest(AIOpChatStream, MetricResultTimeout, 0)
	s.m.AIError(string(errs.CodeAITimeout), 0)
	if s.cb.log != nil {
		s.cb.log.Warn("circuit.stream_timeout",
			slog.String("target", s.cb.target), slog.String("reason", reason))
	}
}

// StreamFailureReporter 由装饰后的流实现；编排层用它上报超时类故障。
//
// 定义成小接口 + 类型断言，而不是扩大 `ChatEventStream`：
// 后者会让所有实现（含测试假件）都必须实现一个与「事件流」无关的方法。
type StreamFailureReporter interface {
	ReportStreamFailure(reason string)
}

func (s *circuitStream) report(err error) {
	if err == nil {
		s.cb.OnSuccess()
		return
	}
	if isUpstreamFailure(err) {
		s.cb.OnFailure()
		s.m.AIError(codeOf(err), upstreamStatusOf(err))
		return
	}
	// 上游明确拒绝（内容过滤/参数错误）：链路本身是好的。
	s.cb.OnSuccess()
}

// ReportStreamFailure 让调用方（编排层）把超时类故障显式上报给熔断器。
//
// 装饰器在 `Close()` 里刻意不猜结论（见 `circuitStream.Close`），
// 因此这三条路径必须由知道原因的一方上报：首字节超时、空闲超时、整轮超时。
func ReportStreamFailure(stream ChatEventStream, reason string) {
	if r, ok := stream.(StreamFailureReporter); ok {
		r.ReportStreamFailure(reason)
	}
}

func codeOf(err error) string {
	if appErr, ok := errs.As(err); ok {
		return string(appErr.Code())
	}
	return string(errs.CodeInternalError)
}

func upstreamStatusOf(err error) int {
	if appErr, ok := errs.As(err); ok {
		return appErr.UpstreamStatus()
	}
	return 0
}

func failureResult(err error) string {
	if appErr, ok := errs.As(err); ok && appErr.Code() == errs.CodeAITimeout {
		return MetricResultTimeout
	}
	return MetricResultError
}
