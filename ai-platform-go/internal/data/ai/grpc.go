package ai

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"strings"
	"time"

	kratosgrpc "github.com/go-kratos/kratos/v2/transport/grpc"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// MetaAuthorization 是携带 Bearer token 的 gRPC metadata 键（接缝 J1）。
//
// 用标准 HTTP 头名而不是自造的 `x-user-id`：gRPC metadata 本身就是
// 「HTTP/2 头」，而 `authorization` 是 AI 侧已经在用的那一个 ——
// gRPC server 复用同一个 `authenticate()`，不需要第二条鉴权路径。
const MetaAuthorization = "authorization"

// MetaTraceID 是跨服务传递 trace id 的 metadata 键（接缝 J3）。
const MetaTraceID = "x-trace-id"

// TrailerTraceID 是 AI 侧回传 trace id 的 trailer 键（接缝 J3）。
//
// 为什么用 trailer 而不是普通响应头：错误路径下 gRPC 只保证**状态**与
// `status.details` 可靠到达，普通 header 在出错时可能根本没机会写。
// trailer 是「调用结束后必然补齐」的通道，正好用来回传排障必需的信息。
const TrailerTraceID = "x-trace-id"

// grpcChatClient 是用 Kratos gRPC 实现 `biz.ChatOrchestrator` 的客户端。
type grpcChatClient struct {
	client aiplatformv1.AiPlatformClient
	opt    Options
}

// 编译期断言：接口变了要在这里先报错，而不是在 main 的装配处。
var _ biz.ChatOrchestrator = (*grpcChatClient)(nil)

// NewChatOrchestrator 建立到 ai-platform 的 gRPC 通道。
//
// 返回值是 biz 定义的接口：main 只依赖契约，换传输（HTTP 回退 / 直连 / 走注册中心）
// 不需要改业务代码（规范 §六）。
//
// 关于为什么不用 kratos 的 `WithTimeout`：kratos 的 unary 拦截器把该值当作
// **每次调用的 deadline**（`ctx, cancel = context.WithTimeout(ctx, timeout)`），
// 而我们要的是「Chat 70s / 元数据 8s」这种按档位区分的超时。所以这里不设它，
// 由 Chat 自己按档位套 deadline；`ctx` 只用于建连期的名字解析预算。
func NewChatOrchestrator(ctx context.Context, opt Options) (biz.ChatOrchestrator, error) {
	conn, err := dialAI(ctx, opt)
	if err != nil {
		return nil, err
	}
	return newChatOrchestratorFromConn(conn, opt), nil
}

// dialAI 建立到 ai-platform 的 gRPC 通道。
//
// 抽出来给**非流式 `Chat` 与流式 `ChatStream` 共用**：两条路必须用完全相同的
// 拨号参数。一旦分叉，「非流式能通、流式不通」这种故障就只能靠逐个参数比对
// 才能发现，而它恰好是最难查的一类（一个通一个不通，看起来像业务问题）。
func dialAI(ctx context.Context, opt Options) (grpc.ClientConnInterface, error) {
	target := strings.TrimSpace(opt.GRPCTarget)
	if target == "" {
		return nil, errors.New("AI_PLATFORM_GRPC_TARGET 为空：AI_GRPC_ENABLED=true 时必须配置")
	}
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	dialCtx, cancel := ctxWithTimeout(ctx, opt.ConnectTimeout)
	defer cancel()

	conn, err := kratosgrpc.DialInsecure(dialCtx,
		kratosgrpc.WithEndpoint(target),
		// ⚠️ 必须显式把 kratos 的「每次调用 2s」超时关掉（传 0 = 不套 deadline）。
		//
		// kratos 的 `dial()` 把 `timeout` 默认成 **2000ms**，并且**总是**装一个
		// unary 拦截器执行 `ctx, cancel = context.WithTimeout(ctx, timeout)`
		// （transport/grpc/client.go 的 `unaryClientInterceptor`）。也就是说：
		// 「借它拿一条 ClientConn、再自己按档位设 deadline」得到的实际 deadline
		// 是 `min(自己的档位, 2s)` —— 一次 1.2s 的回答能过，一次走了 RAG 或长
		// 回答的 3s 调用就变成 `DeadlineExceeded`；更坏的是 `mapError` 会把
		// 它归类成「AI 侧自己回的超时」（非信封分支），排查方向直接被带偏。
		//
		// kratos 源码里有 `if timeout > 0` 守卫，所以 0 就是「不装这个超时」。
		// 超时只能有一个来源：`Chat` 与 `TimeoutForMeta` 按档位设置的那一个。
		//
		// 对**流式**尤其重要：kratos 只给 unary 装了那个拦截器，但 `timeout`
		// 同时是拨号的 `DialOption`（`grpc.WithBlock` + 连接期 deadline）。
		// 显式传 0 让两条路走同一套参数，不依赖「流式恰好不吃这个坑」。
		kratosgrpc.WithTimeout(0),
		// 关掉 kratos 自带的健康检查均衡器：它会给每个后端加一路健康探测，
		// 而 M3 只有单实例直连，多出来的探测只会在启动期制造噪声。
		kratosgrpc.WithHealthCheck(false),
		kratosgrpc.WithOptions(
			// kratos 默认注入 `round_robin` + `healthCheckConfig`。单实例直连时
			// `pick_first` 语义更简单（一个后端，选它即可）；要多实例负载均衡时
			// 应该配 `AI_DISCOVERY_ENABLED` + `WithDiscovery`，而不是靠这个默认值。
			grpc.WithDefaultServiceConfig(`{"loadBalancingConfig":[{"pick_first":{}}]}`),
			// 长连接 + keepalive 30s（docs/04-§3.1）：AI 侧一次回答可能 60s，
			// 期间没有业务帧，没有 keepalive 时中间的 NAT/LB 会静默掐掉连接。
			grpc.WithKeepaliveParams(keepalive.ClientParameters{
				Time:                30 * time.Second,
				Timeout:             10 * time.Second,
				PermitWithoutStream: false,
			}),
		),
	)
	if err != nil {
		return nil, err
	}
	return conn, nil
}

// newChatOrchestratorFromConn 用一条**已建立的**连接组装客户端。
//
// 抽出来是为了单测能注入 bufconn（内存 pipe）—— 那让「跨进程之后字段还在」
// 这个断言不需要占用真实端口就能成立，而本机恰好有一个端口冲突的历史包袱。
func newChatOrchestratorFromConn(conn grpc.ClientConnInterface, opt Options) biz.ChatOrchestrator {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	return &grpcChatClient{client: aiplatformv1.NewAiPlatformClient(conn), opt: opt}
}

// Chat 调用 AI 完成一次非流式回答。
func (c *grpcChatClient) Chat(ctx context.Context, req biz.ChatRequest) (*biz.ChatResult, error) {
	callCtx, cancel := ctxWithTimeout(ctx, c.opt.ChatTimeout)
	defer cancel()
	callCtx = metadata.AppendToOutgoingContext(callCtx, outgoingMeta(ctx, req)...)

	// `grpc.Trailer` 让失败路径也能拿到 trailer —— trace_id 正是最需要在
	// 失败时拿到的东西（成功时调用方自己就有 trace_id）。
	var trailer metadata.MD
	resp, err := c.client.Chat(callCtx, toProtoRequest(req), grpc.Trailer(&trailer))
	if err != nil {
		return nil, c.mapError(callCtx, err, trailer)
	}
	if resp == nil {
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "empty_result")
	}

	refs, err := marshalReferences(resp.GetReferences())
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "marshal_references")
	}
	calls, err := marshalToolCalls(resp.GetToolCalls())
	if err != nil {
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "marshal_tool_calls")
	}

	return &biz.ChatResult{
		Content:         resp.GetAnswer(),
		FinishReason:    resp.GetFinishReason(),
		ConversationID:  resp.GetConversationId(),
		Degraded:        resp.GetDegraded(),
		DegradedReasons: resp.GetDegradedReasons(),
		References:      refs,
		ToolCalls:       calls,
		Usage:           usageFromProto(resp.GetUsage()),
		Model:           resp.GetModel(),
		ElapsedMS:       int(resp.GetElapsedMs()),
		// AI 侧把 trace id 放在 trailer 里回传（`app/grpc/server.py`）。
		// 成功路径也要取：不一致这件事与成败无关，而失败路径本来
		// 就用同一个函数从 trailer 取值拼错误信封。
		TraceID: traceIDFromTrailer(trailer),
	}, nil
}

// outgoingMeta 组装出站 metadata。
//
// 除了凭据与自有 trace id，还会带上 W3C `traceparent`：
// AI 侧实现了标准的 `traceparent` → span 父子关系，而 `x-trace-id` 只是
// 写日志用的。两个头缺任何一个，都会让「Jaeger 的树」与「日志的 trace_id」
// 里有一个对不上（S8 同时检查两者）。
func outgoingMeta(ctx context.Context, req biz.ChatRequest) []string {
	kv := make([]string, 0, 6)
	if token := strings.TrimSpace(req.UserToken); token != "" {
		kv = append(kv, MetaAuthorization, "Bearer "+token)
	}
	if traceID := strings.TrimSpace(req.TraceID); traceID != "" {
		kv = append(kv, MetaTraceID, traceID)
	}
	if tp := otelx.TraceparentHeader(ctx, req.TraceID); tp != "" {
		kv = append(kv, "traceparent", tp)
	}
	return kv
}

// mapError 把 gRPC 错误翻译成网关统一错误。
//
// 实现是包级函数 `mapAIError`（流式客户端要用同一份），这里只是让它
// 继续以方法形式可用 —— 调用点读起来比 `mapAIError(ctx, err, trailer, c.opt)` 短。
func (c *grpcChatClient) mapError(callCtx context.Context, err error, trailer metadata.MD) error {
	return mapAIError(callCtx, err, trailer, c.opt)
}

// mapAIError 把 gRPC 错误翻译成网关统一错误（接缝 J2）。
//
// 顺序有讲究：
//
//  1. 先看**调用上下文**自己的 Err()：网关 deadline 到了与上游主动回一个
//     `DeadlineExceeded` 都是「超时」，但排障方向完全相反（前者是 AI 卡死、
//     该改 AI；后者是 AI 自己也知道超时了、该看它的内部日志）。只看 `err`
//     无法区分这两者 —— 上游回的那个也是一模一样的 status。
//  2. 再看 AI 侧的错误信封（`status.details` 里的 `AiError`）。
//  3. 最后按「非约定格式」归一化（docs/02-§4.2 规则 5）。
func mapAIError(callCtx context.Context, err error, trailer metadata.MD, opt Options) error {
	traceID := traceIDFromTrailer(trailer)

	if cerr := callCtx.Err(); cerr != nil {
		switch {
		case errors.Is(cerr, context.DeadlineExceeded):
			// 网关超时 MUST 比 AI 遵（docs/04-§3.3），所以走到这里说明 AI 卡死了。
			return errs.New(errs.CodeAITimeout).
				WithTraceID(traceID).
				WithDetail("reason", "gateway_deadline_exceeded").
				WithDetail("timeout_seconds", int(opt.ChatTimeout.Seconds())).
				WithCause(err)
		case errors.Is(cerr, context.Canceled):
			// 客户端断开（M4 会做取消传播的完整处理）。这里如实标注，
			// 免得把它记成「AI 不可用」而拉高熔断计数（M5）。
			return errs.New(errs.CodeAIUnavailable).
				WithTraceID(traceID).
				WithDetail("reason", "client_canceled").
				WithCause(err)
		}
	}

	st, ok := status.FromError(err)
	if !ok {
		return errs.New(errs.CodeAIUnavailable).
			WithTraceID(traceID).
			WithDetail("reason", "transport_error").
			WithCause(err)
	}
	if appErr := aiErrorToAppError(appErrorFromStatus(st), httpStatusForGRPCCode(st.Code())); appErr != nil {
		return appErr
	}

	// 没有信封：AI 侧（或其框架）只回了一个规范码。归一化，
	// 原始规范码与推导出的 HTTP 状态码一起放进 details 供排障。
	code := errs.CodeAIUnavailable
	switch st.Code() {
	case codes.DeadlineExceeded:
		code = errs.CodeAITimeout
	case codes.ResourceExhausted:
		code = errs.CodeAIOverloaded
	}
	opt.Log.WarnContext(callCtx, "ai.chat_non_envelope_status",
		"grpc_code", st.Code().String(),
		"message", st.Message(),
		"trace_id", traceID,
	)
	return errs.New(code).
		WithTraceID(traceID).
		WithDetail("reason", "non_envelope_status").
		WithDetail("grpc_code", st.Code().String()).
		WithDetail("upstream_status", httpStatusForGRPCCode(st.Code())).
		WithCause(err)
}

// appErrorFromStatus 从 `status.details` 里取出第一个 `AiError`。
//
// 取「第一个」而不是「唯一一个」：AI 侧不承诺 details 里只有它，
// 未来加别的详情类型（如 `RetryInfo`）不应该让这里失效。
func appErrorFromStatus(st *status.Status) *aiplatformv1.AiError {
	for _, d := range st.Details() {
		if ae, ok := d.(*aiplatformv1.AiError); ok {
			return ae
		}
	}
	return nil
}

// traceIDFromTrailer 从 trailer 里取 AI 侧的 trace id（接缝 J3）。
func traceIDFromTrailer(md metadata.MD) string {
	if len(md) == 0 {
		return ""
	}
	for _, key := range []string{TrailerTraceID, MetaTraceID, "trace-id"} {
		if vals := md.Get(key); len(vals) > 0 {
			if v := strings.TrimSpace(vals[0]); v != "" {
				return v
			}
		}
	}
	return ""
}

// ---- 请求映射 ----

// toProtoRequest 把领域请求映射成 proto。
//
// 两个刻意的地方：
//
//  1. **不填 `user_id`**：proto 里根本没有这个字段。身份只从
//     `authorization` metadata 来（接缝 J1），一个可被调用方自填的 user_id
//     比没有它更危险（docs/04-§3.2）。
//  2. **不填 `history`**：由 biz 层根据 `use_memory` 决定要不要带
//     （REQ-ORCH-006）。传输层自作主张地补一份历史，会让「跨服务上下文
//     是否重复注入」变得无法从请求看出来。
func toProtoRequest(req biz.ChatRequest) *aiplatformv1.ChatRequest {
	out := &aiplatformv1.ChatRequest{
		Query:     req.Query,
		UseRag:    req.UseRAG,
		KbIds:     req.KBIDs,
		UseMemory: req.UseMemory,
		UseTools:  req.UseTools,
	}
	// `metadata` / `history` 不在这里填：
	//   - `metadata` 是埋点透传（不进 Prompt），M3 没有任何要传的埋点；
	//     而且**空 map 与 nil map 在 protobuf 线格式上不可区分**（都是零条目），
	//     所以「显式给个空 map 便于抓包」是个假理由，己经验证过。
	//   - `history` 由 biz 按 `use_memory` 决定（REQ-ORCH-006）。
	if id := strings.TrimSpace(req.ConversationID); id != "" {
		out.ConversationId = &id
	}
	if m := strings.TrimSpace(req.Model); m != "" {
		out.Model = &m
	}
	out.Temperature = req.Temperature
	out.ScoreThreshold = req.ScoreThreshold
	out.TopK = int32Ptr(req.TopK)
	out.RerankTopN = int32Ptr(req.RerankTopN)

	if len(req.History) > 0 {
		history := make([]*aiplatformv1.ChatMessage, 0, len(req.History))
		for _, m := range req.History {
			history = append(history, &aiplatformv1.ChatMessage{Role: m.Role, Content: m.Content})
		}
		out.History = history
	}
	return out
}

// int32Ptr 把可选的 int 转成可选的 int32，**饱和**而不是回绕。
//
// 为什么不是直接 `int32(*v)`：Go 的窄化转换会回绕，`1<<32 + 5` 会变成 `5`
// —— 一个看起来完全合理的小值，于是「用户传了个荒唐的 top_k」变成
// 「网关悄悄用了 top_k=5」而不报任何错。饱和到 `MaxInt32` 则必然被 AI 侧的
// 范围校验（`le=100`）拒绝，错误反而暴露出来。
func int32Ptr(v *int) *int32 {
	if v == nil {
		return nil
	}
	n := *v
	if n > math.MaxInt32 {
		n = math.MaxInt32
	}
	if n < math.MinInt32 {
		n = math.MinInt32
	}
	out := int32(n)
	return &out
}
