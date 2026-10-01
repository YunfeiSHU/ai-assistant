package biz

import (
	"context"
	"io"
	"log/slog"
	"strings"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件是**网关 → ai-platform 的 HTTP 透传接缝**（docs/04-§7）。
//
// 透传接口在网关侧只有四件事：鉴权 → 归属校验 → 透传 → 原样返回（含错误信封）。
// 落到代码上，「MUST NOT 添加业务逻辑」= 网关**不解析**响应体：
// 一旦开始解析，AI 侧改一个字段名就会变成网关的故障，而 docs/03-§4.1 已明确
// 「AI 的 schema 由 AI 定义，网关只做原样存、原样取」。
//
// 唯一的例外是「非约定格式的上游响应」—— 那必须归一化（docs/02-§4.2 规则 5），
// 所以下面仍然需要一次**极浅**的 JSON 探嗅（只看 `error.code` 是不是字符串）。

// AIProxyTimeoutClass 决定这次透传用哪一档网关超时。
//
// 分档是 docs/04-§3.3「网关 MUST 比 AI 宽松」的落点：档位到具体时长的映射放在
// 传输实现里，于是这条规则只有一处可改，也只有一处需要测。
type AIProxyTimeoutClass string

const (
	// AIProxyTimeoutMeta 用于元数据类接口（AI 侧 5s → 网关 8s）。
	AIProxyTimeoutMeta AIProxyTimeoutClass = "meta"
	// AIProxyTimeoutChat 用于非流式对话（AI 侧 60s → 网关 70s）。
	AIProxyTimeoutChat AIProxyTimeoutClass = "chat"
	// AIProxyTimeoutUpload 用于「上传建任务」（AI 侧任务超时 1800s → 网关 120s）。
	//
	// 注意上传的**字节流**转发属于 M5（docs/08-§6）；M3 只用到这一档的时长定义，
	// 保留它是为了让 M5 接进来时不需要改这一层的签名。
	AIProxyTimeoutUpload AIProxyTimeoutClass = "upload"
)

// AIProxyRequest 是一次内网透传请求。
type AIProxyRequest struct {
	// Method 是 HTTP 方法，原样透传。
	Method string
	// Path 是上游路径（含原始 query），以 `/` 开头，例如 `/knowledge-bases?page=1`。
	//
	// **不做任何重写**：网关路径与 AI 路径同名是 docs/04-§7 的前提；
	// 一旦开始重写，就得维护一份两侧同步的映射表，那正是透传要避免的成本。
	Path string
	// Body 是请求体（可为 nil）。
	//
	// 用 io.Reader 而不是 []byte：上传 50MB 文档时必须能流式转发
	// （docs/04-§8 / AC-ORCH-05 要求网关进程内存增长远小于文件大小），
	// 读成 []byte 会让内存占用正比于文件大小。M3 的 JSON 接口同样受益
	// —— 少一次全量拷贝。
	Body io.Reader
	// ContentLength 是请求体长度（-1 表示未知）。
	//
	// MUST 透传：multipart 请求缺 Content-Length 时上游无法解析。
	ContentLength int64
	ContentType   string

	// UserToken 是提问者的 Bearer token，**原样**交给 AI（接缝 J1）。
	//
	// MUST NOT 换成网关自造的身份头（如 `X-User-Id`）：AI 侧已按「自己校验 JWT」
	// 实现，多一条「网关声称的 user_id」就多一个可被伪造的入口（docs/04-§3.2）；
	// 也 MUST NOT 写进日志。
	UserToken string

	// TraceID 用于跨服务链路串联（接缝 J3）。
	TraceID string

	// Class 决定网关超时档位。
	Class AIProxyTimeoutClass

	// IdempotencyKey 是调用方的 `Idempotency-Key` 头（可为空）。
	//
	// 上传接口**必须**透传（docs/02-§7）：AI 侧据此返回同一个 `task_id`，
	// 否则客户端一次超时重试就会得到两份文档。
	IdempotencyKey string
}

// AIProxyResponse 是上游响应中透传真正需要的那几项。
//
// 刻意不做「任意 header 全量复制」：那会把上游的 `Set-Cookie`、
// 以及可能与实际 body 长度不一致的 `Content-Length` 一起带给客户端。
type AIProxyResponse struct {
	Status      int
	ContentType string
	Body        []byte
	// RetryAfter 是上游的 `Retry-After` 原值（空表示没有）。
	RetryAfter string
	// TraceID 是上游回显的 `X-Trace-Id`（接缝 J3）。
	//
	// 单独开一个字段而不是加一张 header map：这里只需要「链路 id 是否一致」
	// 这一个判断（AC-NFR-06），而一张 map 会让「哪些头该转发」变成每个
	// 调用点各自决定的事 —— 那正是响应头转发最容易出错的地方。
	TraceID string
}

// AIProxy 是内网 HTTP 透传客户端。
type AIProxy interface {
	// Do 转发一次请求。
	//
	// 上游返回的非 2xx **不是**错误：它带着一份必须原样转发的错误信封
	// （docs/02-§4.2 规则 1/2/3），所以走返回值而不是 error。
	// error 只表示「根本没拿到可用响应」：建连失败、超时、响应体超限。
	//
	// 返回的 error MUST 已经是 `*errs.AppError`。
	Do(ctx context.Context, req AIProxyRequest) (*AIProxyResponse, error)
}

// ---- 服务 ----

// AIProxyDeps 是透传服务的依赖。
type AIProxyDeps struct {
	Proxy         AIProxy
	Conversations ConversationRepo
	// Quota 用于上传时的存量指标预占（documents_count / storage_bytes）。
	// 为 nil 表示配额未接线：上传照常转发，只是不占用额度。
	Quota *QuotaService
	Log   *slog.Logger
}

// AIProxyService 承担透传里**唯一**属于网关自己的逻辑：会话归属校验。
//
// docs/04-§7 明确「网关只做路径合法 + 鉴权 + 透传」，**不重复校验** KB /
// 文档 / 任务的属主（AI 侧的 `REQ-RAG-011` 已保证跨用户 404）。
// 唯一例外是会话相关资源 —— 会话本来就属于网关的台账，不校验就等于
// 「任何登录用户都能凭猜到的 conversation_id 读别人的上下文」。
type AIProxyService struct{ d AIProxyDeps }

// NewAIProxyService 构造透传服务。
func NewAIProxyService(d AIProxyDeps) *AIProxyService {
	if d.Log == nil {
		d.Log = slog.Default()
	}
	return &AIProxyService{d: d}
}

// ProxyInput 是一次透传的输入（`Path` 已由 handler 按上游路径拼好）。
type ProxyInput struct {
	Method        string
	Path          string
	Body          io.Reader
	ContentLength int64
	ContentType   string
	UserToken     string
	TraceID       string
	Class         AIProxyTimeoutClass
	// IdempotencyKey 原样透传给上游（上传接口用，见 AIProxyRequest）。
	IdempotencyKey string

	// OwnedConversationID 非空时，先校验该会话属于 userID 再转发。
	OwnedConversationID string
}

// Forward 校验后原样转发，并处理「上游响应不是约定格式」这一种情况。
//
// 返回 nil + error 的情形只有三种：归属校验失败、凭据缺失、上游响应不可用
// （含非约定格式已归一化）。其余一律原样返回，由 handler 写回客户端。
func (s *AIProxyService) Forward(ctx context.Context, userID string, in ProxyInput) (*AIProxyResponse, error) {
	if strings.TrimSpace(in.UserToken) == "" {
		// 走到这里说明鉴权中间件没有把 token 传下来。**不能**退化成用服务凭据
		// 转发：那会让 AI 侧把这次请求当成网关后台任务，用户隔离直接失效。
		return nil, errs.New(errs.CodeUnauthenticated).WithDetail("reason", "missing_user_token")
	}
	if id := strings.TrimSpace(in.OwnedConversationID); id != "" {
		if _, err := s.d.Conversations.GetOwned(ctx, userID, id); err != nil {
			return nil, conversationLookupError(err)
		}
	}
	if s.d.Proxy == nil {
		// 编排未接线（配置缺失或阶段未到）：如实报告，而不是假装成功。
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "proxy_not_configured")
	}

	resp, err := s.d.Proxy.Do(ctx, AIProxyRequest{
		Method:         in.Method,
		Path:           in.Path,
		Body:           in.Body,
		ContentLength:  in.ContentLength,
		ContentType:    in.ContentType,
		UserToken:      in.UserToken,
		TraceID:        in.TraceID,
		Class:          in.Class,
		IdempotencyKey: in.IdempotencyKey,
	})
	if err != nil {
		return nil, err
	}
	if resp == nil {
		return nil, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "empty_response")
	}

	// 2xx / 3xx：原样转发。
	if resp.Status < 400 {
		return resp, nil
	}
	// 4xx / 5xx：带约定信封则原样转发。规则 1/2/3（保留 code/message/details/trace_id
	// 与状态码）由「不重新编码」天然满足 —— 重新序列化正是这些「原值」
	// 悄悄变化（数字变 float64、trace_id 被换成网关自己的 request id）的唯一来源。
	//
	// 判断与重建共用 `errs.ParseEnvelope`：两处各写一份 JSON 结构体会在
	// AI 侧加字段时出现「一处认、一处不认」的分裂。
	if errs.IsEnvelope(resp.Body, resp.Status) {
		return resp, nil
	}
	// 规则 5：上游给的不是约定格式（如反向代理的 HTML 502 页）。
	// 归一化，并把原始状态码放进 details 供排障。
	code := errs.CodeAIUnavailable
	if resp.Status == 504 || resp.Status == 408 {
		code = errs.CodeAITimeout
	}
	s.d.Log.WarnContext(ctx, "ai_proxy.unexpected_upstream_body",
		slog.String("path", in.Path),
		slog.Int("upstream_status", resp.Status),
		slog.Int("body_bytes", len(resp.Body)),
	)
	return nil, errs.New(code).
		WithDetail("upstream_status", resp.Status).
		WithDetail("reason", "non_envelope_body")
}

// Upload 转发一次文档上传，并按结果决定是否占用 `documents_count` / `storage_bytes`。
//
// 这三步必须在一起（docs/04-§8 + docs/02-§5.2）：
//
//	① 预占存量额度 → ② 转发 → ③ 失败则归还
//
// 为什么不在 service 层拼这三步：②的成功判据是「AI 没有返回 4xx」，
// 而 4xx 是**正常返回值**（不支持的类型、重复文档）而不是 error ——
// 放在 service 里就会出现「看着写了归还，实际上只在网络错误时归还」的漏。
//
// `Idempotency-Key` 必须由调用方原样塞进 `in.IdempotencyKey`（docs/02-§7）：
// AI 侧据此返回同一个 `task_id`，网关的重试才不会造出两份文档。
func (s *AIProxyService) Upload(ctx context.Context, userID string, in ProxyInput, sizeBytes int64) (*AIProxyResponse, error) {
	if s.d.Quota == nil {
		return s.Forward(ctx, userID, in)
	}
	items := []StockDelta{{Metric: MetricDocumentsCount, Delta: 1}}
	if sizeBytes > 0 {
		// 空间按字节预占。`sizeBytes` 未知（chunked 上传）时为 0：
		// 宁可少算也不能猜一个数字 —— 猜大了会让正常用户传不上去。
		items = append(items, StockDelta{Metric: MetricStorageBytes, Delta: sizeBytes})
	}
	release, err := s.d.Quota.ReserveStock(ctx, userID, items)
	if err != nil {
		return nil, err
	}

	resp, err := s.Forward(ctx, userID, in)
	if err != nil {
		release()
		return nil, err
	}
	if resp.Status >= 400 {
		// AI 明确拒绝：文档没有入库，额度必须还回去。
		release()
		return resp, nil
	}
	// 成功：额度保留（不调用 release）。
	return resp, nil
}

// ---- 内部 ----
