package biz

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/ids"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// 本文件是流式提问的编排（docs/03-§4.3 的 `POST .../messages/stream`，M4）。
//
// 与非流式 `Send` 的分工：落库时机表与校验完全相同，差别只在「回答怎么来、怎么写、怎么存半截」。
// 刻意同包同服务：两者共享 `chatRequest`/`recentHistory`/`applyAutoTitle`/`AppendAssistant`。

// 流式路径的默认时长与限额（与 AI 侧默认值对齐，docs/06-§4）。
// 环境变量（`conf`）才是权威来源、由装配层传入；这里只是依赖未传值时的兜底，
// 因为零值会让定时器立刻触发（0 时长 = 立即超时）。
const (
	// StreamFirstByteTimeoutDefault 是首字节超时（`AI_FIRST_BYTE_TIMEOUT_SECONDS`）。
	StreamFirstByteTimeoutDefault = 35 * time.Second
	// StreamIdleTimeoutDefault 是空闲超时（`AI_IDLE_TIMEOUT_SECONDS`）。
	StreamIdleTimeoutDefault = 150 * time.Second
	// StreamTotalTimeoutDefault 是整轮上限（`AI_TOTAL_TIMEOUT_SECONDS`）。
	StreamTotalTimeoutDefault = 360 * time.Second
	// StreamAccumulateMaxCharsDefault 是落库正文上限（`STREAM_ACCUMULATE_MAX_CHARS`）。
	StreamAccumulateMaxCharsDefault = 64000
	// StreamAccumulateMaxItemsDefault 是引用/工具轨迹条数上限（docs/06-§4）。
	StreamAccumulateMaxItemsDefault = 200

	// StreamHeartbeatInterval 是保活帧间隔（docs/04-§4.2：15s）。
	// 与 AI 侧 `sse.py::PING_INTERVAL_SECONDS` 同值但各管各的：那是 AI→LLM 链路，
	// 这是网关→客户端链路（中间还隔着反向代理）。相等只是巧合，不构成耦合。
	StreamHeartbeatInterval = 15 * time.Second
)

// streamPersistTimeout 是流结束后写库的上限。
// 落库用 `context.WithoutCancel`（客户端可能已断），故超时必须自己带：
// 没有 deadline 的写库会在 MySQL 卡住时把已结束的请求永远留在 goroutine 里（占着连接）。
const streamPersistTimeout = 10 * time.Second

// StreamSend 发送一次流式提问。
//
// 落库顺序与非流式 `Send` 一致，不同点三处：
//  1. 事件边到边发；
//  2. 落库发生在响应之后、且中止时也要落，故用 `context.WithoutCancel`（ctx 在客户端
//     断连那一刻已被取消，症状是「用户看到的半截回答刷新后不见了」）；
//  3. 出错时能否回 4xx/5xx 取决于第一帧是否已写出，判据是 `sink.Started()`。
//
// 幂等（`Idempotency-Key`）不适用本接口：重放已推一半的流会让客户端把正文再拼一遍。
//
// 返回值语义：
//
//	nil                              流正常结束（客户端主动离开也算，不是错误）
//	非 nil 且 !sink.Started()         尚未写出任何一帧，调用方应当回 4xx/5xx 信封
//	其它非 nil                        已写出过帧，调用方只记日志，MUST NOT 再写响应体
func (s *MessageService) StreamSend(
	ctx context.Context,
	userID, conversationID string,
	in SendMessageInput,
	meta RequestMeta,
	sink StreamSink,
) error {
	content := strings.TrimSpace(in.Content)
	if fields := validateSendInput(in, content); len(fields) > 0 {
		return errs.InvalidArgument(fields)
	}

	conv, err := s.d.Conversations.GetOwned(ctx, userID, conversationID)
	if err != nil {
		return conversationLookupError(err)
	}
	if !conv.IsActive() {
		return errs.New(errs.CodeConversationArchived)
	}

	now := s.now()
	traceID := strings.TrimSpace(meta.TraceID)

	// 配额预扣（与非流式同一步骤、同一个位置），见 `Send` 的注释。
	reservation, err := s.beginChat(ctx, userID, &UsageRef{ConversationID: conv.ID})
	if err != nil {
		return err
	}

	userMsg := &Message{
		ID:             ids.NewMessage(),
		ConversationID: conv.ID,
		UserID:         userID,
		Role:           MessageRoleUser,
		Content:        content,
		Status:         MessageStatusCompleted,
		CreatedAt:      now,
	}
	if traceID != "" {
		userMsg.TraceID = &traceID
	}
	if err := s.d.Messages.Append(ctx, userID, conv.ID, userMsg); err != nil {
		reservation.Rollback(ctx, err)
		return conversationLookupError(err)
	}

	s.applyAutoTitle(ctx, userID, conv, content, now)

	if s.d.Streamer == nil {
		// 编排未接线。user 消息已落库（与 `Send` 的 orchestrator_not_configured 同形：
		// 提问必须留在台账里，否则用户重发时看不出「刚才问过」）。
		err := errs.New(errs.CodeAIUnavailable).WithDetail("reason", "streamer_not_configured")
		reservation.Rollback(ctx, err)
		return err
	}

	chatReq := s.chatRequest(userID, conv, in, content, meta)
	if !chatReq.UseMemory {
		// 同 `Send`：use_memory=true 时 AI 自取上下文，网关再传一份会让对话被注入两次。
		chatReq.History = s.recentHistory(ctx, userID, conv.ID, userMsg.Seq)
	}

	acc := newStreamAccumulator(s.d.StreamAccumulateMaxChars, s.d.StreamAccumulateMaxItems, s.d.Clock)
	acc.useRAG = chatReq.UseRAG

	// 回答的消息 ID 在开流之前就定下来：`meta` 帧要先告诉客户端，而落库要等流结束。
	// 两处用同一个 ID，客户端才能在 `done` 之后直接 GET /messages/{id} 拿到完整消息。
	assistantID := ids.NewMessage()

	stream, err := s.d.Streamer.ChatStream(ctx, chatReq)
	if err != nil {
		// 流尚未建立（建连失败/上游 4xx）。刻意不落 assistant 消息：上游一次都没回，
		// 库里留一条空回答只会让用户以为「模型答了个空」（与非流式 Chat 报错时同规则）。
		// 归还预扣同样按白名单（`ShouldRollback`）：建连失败通常是 AI_UNAVAILABLE。
		if ShouldRollback(err) {
			reservation.Rollback(ctx, err)
		} else {
			// 上游明确拒绝（参数/内容）：这次提问确实消耗了配额。
			reservation.Commit(ctx, 0, UsageRef{ConversationID: conv.ID, MessageID: assistantID, TraceID: traceID})
		}
		return err
	}
	defer func() { _ = stream.Close() }()

	end, loopErr := s.pump(ctx, stream, acc, sink, conv.ID, assistantID)

	// SSE 连接的关闭与「客户端主动离开」的记账。放这里而不是传输层：
	// 断连原因由 pump 判定（写失败/ctx 取消/上游断开），传输层只拿到一个 error，
	// 分不出「客户端走了」与「上游坏了」—— 而这两件事的告警对象完全不同。
	if acc.sseOpened {
		s.d.Metrics.SSEConnectionClosed()
		if end == streamEndClientGone {
			s.d.Metrics.SSEClientDisconnect(sseDisconnectPhase(acc))
		}
	}
	return s.finishStream(ctx, userID, conv, traceID, assistantID, acc, end, loopErr, sink, reservation)
}

// noteSSEOpened 在响应头真的写出之后记一次「SSE 连接已建立」。
// 判据用 `sink.Started()`：首字节超时之前一帧都没写，那种请求会以 504 信封结束，不该计成 SSE 连接。
func noteSSEOpened(m Metrics, acc *streamAccumulator, sink StreamSink) {
	if acc.sseOpened || !sink.Started() {
		return
	}
	acc.sseOpened = true
	m.SSEConnectionOpened()
}

// sseDisconnectPhase 把断连按「已收到多少内容」分档。
// 首字节前断开是「用户等不及」或探活/压测（调超时与前端），正文过半断开是「模型太慢」（调模型）。
func sseDisconnectPhase(acc *streamAccumulator) string {
	if acc.text.Len() == 0 {
		return SSEPhaseBeforeFirstToken
	}
	return SSEPhaseMidStream
}

// pump 驱动「等事件 / 发心跳 / 判超时 / 响应取消」的主循环。
// 由 biz 自己 select：心跳、首字节/空闲/总超时、客户端取消、进程退出这六件事必须同时成立，
// 而只有第一件与上游有关。放传输层会让「超时」变成连接级实现细节，而它其实是业务策略（docs/04-§5）。
func (s *MessageService) pump(
	ctx context.Context,
	stream ChatEventStream,
	acc *streamAccumulator,
	sink StreamSink,
	conversationID string,
	assistantID string,
) (streamEnd, error) {
	events := stream.Events()

	// `sse.forward` span 盖住「等上游事件 + 向下游写帧」整个阶段。
	// 与 `ai.chat_stream`（建流）分开：流式请求耗时 99% 在这个循环里，建流只几十毫秒，
	// 画在一起会把要看的间隔压成一根竖线。
	ctx, span := otelx.Tracer("gateway.sse").Start(ctx, "sse.forward")
	defer func() { span.End() }()

	firstByte := time.NewTimer(s.d.StreamFirstByteTimeout)
	defer firstByte.Stop()
	idle := time.NewTimer(s.d.StreamIdleTimeout)
	defer idle.Stop()
	total := time.NewTimer(s.d.StreamTotalTimeout)
	defer total.Stop()
	heartbeat := time.NewTicker(StreamHeartbeatInterval)
	defer heartbeat.Stop()

	// nil channel 永远不就绪，正好表示「没有退出通知」（测试里不传就是这种情形）。
	shutdown := s.d.Shutdown

	first := true
	for {
		select {
		case ev, ok := <-events:
			if !ok {
				if err := stream.Err(); err != nil {
					// 上游中途断开且没发 error 帧（连接被掐、AI 进程重启）。
					return streamEndUpstreamBroken, err
				}
				if !acc.doneSeen {
					// 通道干净地关了但没收到 `done`：上游少发了终止帧。
					// 当成中断处理 —— 落 `partial` 比落 `completed` 诚实得多，
					// 因为「不知道有没有答完」和「答完了」在展示上是两回事。
					return streamEndUpstreamBroken, errStreamNoDone
				}
				return streamEndUpstream, nil
			}
			if first {
				first = false
				firstByte.Stop()
				// 首 token 耗时只在第一帧到达时测一次（docs/06-§5.2），起点是累积器创建时刻
				//（建流之前），因此包含 `ai.chat_stream` 建连那一段 —— 正是「AI 卡着不开流」被发现的地方。
				s.d.Metrics.AIFirstToken(acc.useRAG, time.Since(acc.started))
				span.SetAttributes(otelx.Attr("sse.first_frame_ms", time.Since(acc.started).Milliseconds()))
			}
			resetTimer(idle, s.d.StreamIdleTimeout)
			acc.observe(ev)
			ev = s.authoritativeMeta(ctx, ev, conversationID, assistantID)

			if err := sink.Send(ev); err != nil {
				// 写响应失败通常意味着客户端已经走了（RST）。继续把答案流进黑洞毫无意义，
				// 但要落 partial —— 已经收到的正文要保留（docs/03-§5 的断连行）。
				noteSSEOpened(s.d.Metrics, acc, sink)
				return streamEndClientGone, err
			}
			noteSSEOpened(s.d.Metrics, acc, sink)
			if acc.sawError {
				// AI 的 error 帧之后其生成器就返回了（不再有 usage/done），不等通道关闭直接收尾：
				// 等下去只是多一次调度，且「上游已明确报错」还挂着连接不放对客户端无价值。
				return streamEndUpstreamError, nil
			}

		case <-firstByte.C:
			// 超时类故障显式告诉熔断器：装饰器在 `Close()` 里无法判定流是不是被超时打断的，
			// 由知道原因的一方上报。不上报的后果是 AI 每次都卡在首字节超时、熔断器却认为它健康。
			ReportStreamFailure(stream, "first_byte_timeout")
			return streamEndTimeout, errs.New(errs.CodeAITimeout).
				WithDetail("reason", "first_byte_timeout")
		case <-idle.C:
			ReportStreamFailure(stream, "idle_timeout")
			return streamEndTimeout, errs.New(errs.CodeAITimeout).
				WithDetail("reason", "idle_timeout")
		case <-total.C:
			ReportStreamFailure(stream, "total_timeout")
			return streamEndTimeout, errs.New(errs.CodeAITimeout).
				WithDetail("reason", "total_timeout")
		case <-heartbeat.C:
			// 只在已开流后发心跳：第一帧之前发会把响应头写出去，
			//「首字节超时」就再也没法回 504 了（docs/04-§5 的时延表要求能回）。
			if sink.Started() {
				if err := sink.Ping(); err != nil {
					noteSSEOpened(s.d.Metrics, acc, sink)
					return streamEndClientGone, err
				}
				noteSSEOpened(s.d.Metrics, acc, sink)
			}
		case <-shutdown:
			// docs/06-§3 第 ③ 步：先给客户端一个明确的终止原因，再落 partial。
			return streamEndShutdown, errs.New(errs.CodeServiceShuttingDown)
		case <-ctx.Done():
			// 客户端断连 / 请求上下文被取消。什么都不必发（连接已经没了）。
			return streamEndClientGone, ctx.Err()
		}
	}
}

// authoritativeMeta 把 `meta` 帧里的会话 ID 与消息 ID 替换成网关自己的。
// 两个 ID 的权威都在网关（REQ-ORCH-006 / AC-ORCH-07）：转发上游的 conversation_id
// 会让用户看到「AI 忽然失忆」，转发上游的 message_id 必然 404。上游为空时也补上网关的值。
func (s *MessageService) authoritativeMeta(
	ctx context.Context,
	ev StreamEvent,
	conversationID, messageID string,
) StreamEvent {
	meta, ok := ev.(StreamMetaEvent)
	if !ok {
		return ev
	}
	if got := strings.TrimSpace(meta.ConversationID); got != "" && got != conversationID {
		s.d.Metrics.SessionMismatch()
		s.d.Log.ErrorContext(ctx, "message.session_mismatch",
			slog.String("metric", "gw_session_mismatch_total"),
			slog.String("conversation_id", conversationID),
			slog.String("upstream_conversation_id", got),
		)
	}
	meta.ConversationID = conversationID
	meta.MessageID = messageID
	return meta
}

// finishStream 收尾：补发错误帧（若适用）→ 落库 → 报告落库失败。
// 顺序不可调换：错误帧必须在落库之前发出去 —— 落库可能要等 MySQL，而客户端此刻最需要知道「不会有下文了」。
func (s *MessageService) finishStream(
	ctx context.Context,
	userID string,
	conv *Conversation,
	traceID string,
	assistantID string,
	acc *streamAccumulator,
	end streamEnd,
	loopErr error,
	sink StreamSink,
	reservation *QuotaReservation,
) error {
	status, finishReason := streamOutcome(acc, end)

	// 网关自己产生的失败原因（AI 的 error 帧不在这里：它已经随流发过了）。
	appErr := gatewayStreamError(end, loopErr)
	if appErr != nil && sink.Started() {
		if err := sink.Send(StreamErrorEvent{
			Code:      string(appErr.Code()),
			Message:   appErr.Message(),
			Retryable: appErr.Retryable(),
		}); err != nil {
			s.d.Log.InfoContext(ctx, "message.stream_error_frame_failed",
				slog.String("conversation_id", conv.ID),
				slog.String("error", err.Error()),
			)
		}
	}

	if acc.truncated {
		s.d.Log.WarnContext(ctx, "message.stream_accumulate_truncated",
			slog.String("conversation_id", conv.ID),
			slog.Int("max_chars", acc.maxChars),
		)
	}
	if acc.refsDropped > 0 {
		s.d.Log.WarnContext(ctx, "message.stream_references_dropped",
			slog.String("conversation_id", conv.ID),
			slog.Int("dropped", acc.refsDropped),
			slog.Int("max_items", acc.maxItems),
		)
	}
	if acc.toolsDropped > 0 {
		s.d.Log.WarnContext(ctx, "message.stream_tool_calls_dropped",
			slog.String("conversation_id", conv.ID),
			slog.Int("dropped", acc.toolsDropped),
			slog.Int("max_items", acc.maxItems),
		)
	}

	if loopErr != nil {
		// 客户端自己走了不算服务端错误：用 Info 留痕即可（量很大，每次关页面一条），
		// 否则 ERROR 日志会被它淹没。
		level := slog.LevelWarn
		if end == streamEndClientGone {
			level = slog.LevelInfo
		}
		s.d.Log.Log(ctx, level, "message.stream_aborted",
			slog.String("conversation_id", conv.ID),
			slog.String("end", end.String()),
			slog.Int("chars", acc.chars),
			slog.String("error", loopErr.Error()),
		)
	}

	// 落库：断开与请求上下文的联系。`context.WithoutCancel` 保留 ctx 的值（trace_id 等）
	// 但去掉取消信号，正是「客户端走了，回答仍然要存下来」需要的（docs/03-§5 要求断连时落 partial）。
	//「一个事件都没收到」是唯一的例外，见 `hasContent` 的注释。
	if !acc.hasContent() {
		// 一个事件都没收到：本次没有产生任何回答。这时必须结清配额，
		// 不结清就等于「预扣了但永远不还」——而这条路很常见（上游连上就不说话、首字节超时）。
		var cause error
		if appErr != nil {
			cause = appErr
		} else {
			cause = loopErr
		}
		s.settleStreamQuota(ctx, reservation, acc, conv.ID, assistantID, traceID, cause)
		if appErr != nil {
			return appErr
		}
		return loopErr
	}

	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), streamPersistTimeout)
	defer cancel()

	_, err := s.AppendAssistant(writeCtx, userID, AppendAssistantInput{
		ID:              assistantID,
		ConversationID:  conv.ID,
		Content:         acc.text.String(),
		Status:          status,
		FinishReason:    nilIfEmpty(finishReason),
		References:      acc.references,
		ToolCalls:       acc.toolCalls(),
		Usage:           acc.usage,
		Model:           nilIfEmpty(acc.model),
		Degraded:        acc.degraded,
		DegradedReasons: acc.degradedReasons,
		ElapsedMS:       intPtrIfPositive(acc.elapsedMS()),
		TraceID:         nilIfEmpty(traceID),
	})
	if err != nil {
		// 流式路径只能告警 + 追加 `gw_persist_error`：响应已发出，改不了状态码，也补不回已流出的正文，
		// 唯一能做的是让客户端知道「这条回答没进台账」。
		s.d.Log.ErrorContext(ctx, "message.persist_assistant_failed",
			slog.String("conversation_id", conv.ID),
			slog.String("status", status),
			slog.String("error", err.Error()),
		)
		reason := persistErrorReason(err)
		// 落库失败是必须被看到的降级：答案已推给客户端，但「刷新后还能看到」会失败，
		// 不计数就只能靠用户投诉发现。
		s.d.Metrics.MessagePersistFailed(reason)
		if sink.Started() {
			// docs/04-§4.1：网关追加的事件 SHOULD 放在 `done` 之后，这里是流的最末尾，天然满足。
			_ = sink.Send(StreamPersistErrorEvent{Reason: reason})
		}
	}

	// 配额结算放在落库之后（它不影响响应，只是入账）。
	// `cause` 只取非 nil 的 `appErr`：它是 `*errs.AppError`，为 nil 时赋给 error 接口
	// 会得到「非 nil 的接口」，而 `ShouldRollback` 无法从类型上看出这一点。
	var cause error
	if appErr != nil {
		cause = appErr
	}
	s.settleStreamQuota(ctx, reservation, acc, conv.ID, assistantID, traceID, cause)

	if appErr != nil {
		return appErr
	}
	return loopErr
}

// settleStreamQuota 结清一次流式调用的配额：按失败原因决定归还还是计入。
// `usage` 只在计入时使用；归还时不计 token（AI 没产生可计费的输出）。
func (s *MessageService) settleStreamQuota(
	ctx context.Context,
	reservation *QuotaReservation,
	acc *streamAccumulator,
	conversationID, messageID, traceID string,
	cause error,
) {
	if reservation == nil {
		return
	}
	ref := UsageRef{ConversationID: conversationID, MessageID: messageID, TraceID: traceID}
	if cause != nil && ShouldRollback(cause) {
		reservation.Rollback(ctx, cause)
		return
	}
	s.commitQuota(ctx, reservation, acc.usage, ref)
}

// gatewayStreamError 返回网关自己产生的错误（需补发 error 帧并可作为返回值）。
// AI 的 error 帧不在其列（已随流发过，再补会让客户端收到两个 error）；客户端断连也不在（连接没了）。
// `loopErr` 是同一次失败的细节版（带 `reason`），优先返回它：三个超时共用 AI_TIMEOUT 一个码，
// `reason` 是唯一区分手段，丢掉就无法判断是首字节、空闲还是整轮超时。
func gatewayStreamError(end streamEnd, loopErr error) *errs.AppError {
	fallback := func() *errs.AppError {
		switch end {
		case streamEndTimeout:
			return errs.New(errs.CodeAITimeout).WithMessage("上游超时")
		case streamEndShutdown:
			return errs.New(errs.CodeServiceShuttingDown).WithMessage("服务正在退出")
		default:
			return nil
		}
	}
	synth := fallback()
	if synth == nil {
		return nil
	}
	if inner, ok := errs.As(loopErr); ok && inner.Code() == synth.Code() {
		return inner
	}
	return synth
}

// persistErrorReason 把落库错误压成一句话，供 `gw_persist_error` 使用。
// 不下发 `err.Error()` 原样：它可能带 SQL 片段或表名，而响应体是给客户端的；
// 排障要的完整错误在日志里（上面刚打过一条 ERROR）。
func persistErrorReason(err error) string {
	if appErr, ok := errs.As(err); ok && appErr.Code() != "" {
		return string(appErr.Code())
	}
	return "persist_failed"
}

// streamOutcome 把「谁结束了流」翻译成落库状态与 finish_reason（依据 docs/03-§5 时机表 + §4.1 状态枚举）：
//
//	completed  正常答完          done 且未截断
//	partial    内容不完整        断连 / 超时 / 上游没发 done / 累积被截断
//	failed     生成失败          AI 发来 error 帧
func streamOutcome(acc *streamAccumulator, end streamEnd) (string, string) {
	finish := acc.finishReason
	if finish == "" {
		finish = FinishReasonStop
	}
	switch end {
	case streamEndUpstream:
		if acc.truncated {
			// 截断也是 partial（docs/06-§4 的累积上限要求）。
			// finish_reason 保留 AI 的值：截断是**网关的存储策略**，
			// 不是模型提前停了，写成 canceled 会让排障时误判上游。
			return MessageStatusPartial, finish
		}
		return MessageStatusCompleted, finish
	case streamEndUpstreamError:
		// AI 明确报错。finish_reason 留空（NULL）：上游没给值，
		// 编一个 `stop` 出来会让「答了一半失败」看起来像「答完了」。
		return MessageStatusFailed, ""
	default:
		// 超时 / 退出 / 断连 / 上游静默断开：内容不完整。
		return MessageStatusPartial, FinishReasonCanceled
	}
}

// ---- 累积器 ----

// streamAccumulator 把事件流累积成一条可落库的 assistant 消息。
//
// 它是个**纯数据结构**（没有 logger、没有 ctx、没有时钟注入以外的东西）：
// 累积规则是最容易出错的部分（截断、去重、配对），越少依赖越好测。
type streamAccumulator struct {
	maxChars int
	maxItems int
	started  time.Time
	now      nowFunc

	text  strings.Builder
	chars int
	// truncated 表示正文触顶：**继续透传、停止累积**（docs/06-§4）。
	truncated bool

	// references 是本轮引用集合（AI 每次重发**全量**，所以这里整体覆盖）。
	references json.RawMessage
	// refsDropped 是超限被丢掉的条数（仅用于告警）。
	refsDropped int

	// toolOrder / tools 按 call_id 配对 `tool_call` 与 `tool_result`。
	toolOrder    []string
	tools        map[string]*toolCallTraceJSON
	toolsDropped int

	usage           *MessageUsage
	model           string
	degraded        bool
	degradedReasons []string
	finishReason    string
	upstreamElapsed int
	doneSeen        bool

	sawError bool
	errCode  string

	// sseOpened 记录「HTTP 响应头已写出」（即 `gw_sse_connections` 已 +1）。
	// 放在累积器里是因为计数必须由 `StreamSend` 在收尾时配对 -1，两个函数的交接点只有这个结构体。
	sseOpened bool

	// useRAG 是本轮是否开启知识库，用于 `gw_ai_first_token_seconds{use_rag}` 分组
	//（开了 RAG 的首 token 天然更慢，混在一起会把阈值调成两边都不合适）。
	useRAG bool
}

func newStreamAccumulator(maxChars, maxItems int, now nowFunc) *streamAccumulator {
	if maxChars <= 0 {
		maxChars = StreamAccumulateMaxCharsDefault
	}
	if maxItems <= 0 {
		maxItems = StreamAccumulateMaxItemsDefault
	}
	if now == nil {
		now = time.Now
	}
	return &streamAccumulator{
		maxChars: maxChars,
		maxItems: maxItems,
		started:  now(),
		now:      now,
		tools:    make(map[string]*toolCallTraceJSON),
	}
}

// observe 吸收一个事件（**不影响下发**：下发由调用方原样转发）。
func (a *streamAccumulator) observe(ev StreamEvent) {
	switch e := ev.(type) {
	case StreamMetaEvent:
		a.model = e.Model
		a.degraded = e.Degraded
		a.degradedReasons = e.DegradedReasons
	case StreamReferenceEvent:
		a.setReferences(e.References)
	case StreamTokenEvent:
		a.appendText(e.Delta)
	case StreamToolCallEvent:
		a.observeToolCall(e)
	case StreamToolResultEvent:
		a.observeToolResult(e)
	case StreamUsageEvent:
		// 三个字段全 0 的 usage 帧不是用量（`MessageUsage.IsZero` 的理由）。
		if !e.Usage.IsZero() {
			u := e.Usage
			a.usage = &u
		}
	case StreamErrorEvent:
		a.sawError = true
		a.errCode = e.Code
	case StreamDoneEvent:
		a.doneSeen = true
		a.finishReason = e.FinishReason
		a.upstreamElapsed = e.ElapsedMS
	case StreamUnknownEvent:
		// 未知事件原样透传，不参与累积（网关不认识它的语义）。
	}
}

// hasContent 报告累积器里有没有值得落库的东西。
//
// 挡掉一种噪音：上游一个事件都没发就失败了。此时库里不该留一条空回答 ——
// 与非流式路径在 `Chat` 报错时的行为一致（gRPC 把准备阶段失败表达成「流上零事件 + 错误状态」，
// HTTP 回退是 4xx；不判这条会让同一失败在两条传输下留下不同的台账）。
// `sawError`/`doneSeen` 也算内容：那条记录本身就是要保留的事实。
func (a *streamAccumulator) hasContent() bool {
	return a.chars > 0 || len(a.references) > 0 || len(a.toolOrder) > 0 ||
		a.usage != nil || a.doneSeen || a.sawError
}

// appendText 追加正文增量，并在触顶时截断。
func (a *streamAccumulator) appendText(delta string) {
	if delta == "" {
		return
	}
	if !a.truncated {
		n := utf8.RuneCountInString(delta)
		if a.chars+n <= a.maxChars {
			a.text.WriteString(delta)
			a.chars += n
			return
		}
		// 触顶：留住能塞下的前缀。整个 delta 丢掉会让库里少一截已收到的内容，
		// 而「截断」与「缺失」在展示层是两回事。
		if remaining := a.maxChars - a.chars; remaining > 0 {
			a.text.WriteString(string([]rune(delta)[:remaining]))
			a.chars = a.maxChars
		}
		a.truncated = true
	}
	// 已触顶：继续透传给客户端（那是调用方的事），但不再累积。
}

// setReferences 用新集合**整体替换**旧集合。
// 不能追加：AI 侧每次重发的是「本轮引用集合」（只发增量会让客户端的 `[n]` 编号错位），
// 追加会让同一条引用在库里出现多次，而正文里的 `[1]` 只指向其中一个。
func (a *streamAccumulator) setReferences(raw json.RawMessage) {
	if len(raw) == 0 {
		return
	}
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		// 形状不符（不是数组）：原样存，不要丢。丢掉它的症状是「引用忽然全没了」，
		// 而这条分支连日志都不会有。
		a.references = raw
		return
	}
	if len(items) > a.maxItems {
		a.refsDropped = len(items) - a.maxItems
		items = items[:a.maxItems]
	}
	if len(items) == 0 {
		a.references = nil
		return
	}
	out, err := json.Marshal(items)
	if err != nil {
		a.references = raw
		return
	}
	a.references = out
}

// observeToolCall 记录一次工具调用的开始。
func (a *streamAccumulator) observeToolCall(e StreamToolCallEvent) {
	if _, ok := a.tools[e.CallID]; ok {
		return // 同一 call_id 重复的调用帧：保留第一次（后面的通常是同一条的重发）
	}
	if len(a.toolOrder) >= a.maxItems {
		a.toolsDropped++
		return
	}
	args := e.Arguments
	if len(args) == 0 {
		// 契约要求 `arguments` 是对象；`null` 会让严格的反序列化失败。
		args = json.RawMessage(`{}`)
	}
	a.tools[e.CallID] = &toolCallTraceJSON{
		CallID:    e.CallID,
		Name:      e.Name,
		Arguments: args,
	}
	a.toolOrder = append(a.toolOrder, e.CallID)
}

// observeToolResult 补齐一次工具调用的结果。
func (a *streamAccumulator) observeToolResult(e StreamToolResultEvent) {
	call, ok := a.tools[e.CallID]
	if !ok {
		// 没有开始帧的结果帧（上游乱序或开始帧被上限丢掉）。
		// 补一条：宁可留一条 arguments 为空的轨迹，也不要让信息凭空消失。
		if len(a.toolOrder) >= a.maxItems {
			a.toolsDropped++
			return
		}
		call = &toolCallTraceJSON{CallID: e.CallID, Arguments: json.RawMessage(`{}`)}
		a.tools[e.CallID] = call
		a.toolOrder = append(a.toolOrder, e.CallID)
	}
	if e.Name != "" && call.Name == "" {
		call.Name = e.Name
	}
	call.Status = e.Status
	call.Summary = e.Summary
	call.ElapsedMS = e.ElapsedMS
}

// toolCalls 产出 `tool_calls` 列的 JSON（无调用时为 nil）。
func (a *streamAccumulator) toolCalls() json.RawMessage {
	if len(a.toolOrder) == 0 {
		return nil
	}
	out := make([]toolCallTraceJSON, 0, len(a.toolOrder))
	for _, id := range a.toolOrder {
		t := a.tools[id]
		if t.Status == "" {
			// 只有开始没有结果（上游被取消时就会这样）。
			// 落成 `error`：留一个「转不完的圈」比留一条明确的失败更糟。
			t.Status = ToolCallStatusError
		}
		out = append(out, *t)
	}
	raw, err := json.Marshal(out)
	if err != nil {
		return nil
	}
	return raw
}

// elapsedMS 返回本轮耗时：优先用上游 `done` 里的值（不含网络往返，更接近「模型花了多久」）。
// 没有 `done`（中断）时退化成网关自己测的墙钟时间 —— 偏大，但总比 0 好
// （0 会被 `intPtrIfPositive` 丢成 NULL，于是「中断的那次」看起来像没耗时）。
func (a *streamAccumulator) elapsedMS() int {
	if a.upstreamElapsed > 0 {
		return a.upstreamElapsed
	}
	return int(a.now().Sub(a.started) / time.Millisecond)
}

// toolCallTraceJSON 是 `tool_calls[]` 的落库形状（docs/03-§4.1）。
// 与 `internal/data/ai/toolCallJSON` 逐字段一致但不能复用：那份服务于非流式路径
// （一个 proto 结构体 → 一个对象），而流式轨迹是两个事件拼出来的
// （tool_call 给 call_id/name/arguments，tool_result 给 status/summary/elapsed_ms），
// 配对只能发生在同时看得见两个事件的地方。两份形状的一致性由跨包测试盯着。
type toolCallTraceJSON struct {
	CallID    string          `json:"call_id"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
	Status    string          `json:"status"`
	Summary   string          `json:"summary"`
	ElapsedMS int             `json:"elapsed_ms"`
}

// resetTimer 安全地重置一个可能已经触发的 Timer。
//
// Go 1.23 起 Timer 语义变了（通道无缓冲，Reset/Stop 保证不留残留值），且由模块的 `go` 指令门控
// （本模块 go 1.24.0，故生效）。实测：删掉下面的 drain，用例依然通过 —— 这段在当前工具链下是冗余的。
// 保留它只因无副作用，且若有人把 `go` 指令降到 1.23 以下，代码会自动退回安全一侧。
// 不要再把它当成守护 bug 的护栏。
func resetTimer(t *time.Timer, d time.Duration) {
	if !t.Stop() {
		select {
		case <-t.C:
		default:
		}
	}
	t.Reset(d)
}
