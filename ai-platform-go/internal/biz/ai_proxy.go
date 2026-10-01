package biz

import (
	"context"
	"io"
	"log/slog"
	"strings"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件是网关 → ai-platform 的 HTTP 透传接缝（docs/04-§7）。
//
// 网关只做四件事：鉴权 → 归属校验 → 透传 → 原样返回（含错误信封）。
//「MUST NOT 添加业务逻辑」= 不解析响应体：AI 的 schema 由其自定义，网关只做原样存取
//（docs/03-§4.1），一旦解析，AI 改字段名就会变成网关故障。
// 唯一例外是「非约定格式的上游响应」，须归一化（docs/02-§4.2 规则 5），
// 故下面有一次极浅的 JSON 探嗅（只看 `error.code` 是否为字符串）。

// AIProxyTimeoutClass 决定透传用哪一档网关超时（docs/04-§3.3「网关 MUST 比 AI 宽松」）。
// 档位到时长的映射放在传输实现里，这条规则只有一处可改、一处需测。
type AIProxyTimeoutClass string

const (
	// AIProxyTimeoutMeta 用于元数据类接口（AI 侧 5s → 网关 8s）。
	AIProxyTimeoutMeta AIProxyTimeoutClass = "meta"
	// AIProxyTimeoutChat 用于非流式对话（AI 侧 60s → 网关 70s）。
	AIProxyTimeoutChat AIProxyTimeoutClass = "chat"
	// AIProxyTimeoutUpload 用于「上传建任务」（AI 侧任务超时 1800s → 网关 120s）。
	// 字节流转发属于 M5（docs/08-§6），此处先保留时长定义，省得 M5 改签名。
	AIProxyTimeoutUpload AIProxyTimeoutClass = "upload"
)

// AIProxyRequest 是一次内网透传请求。
type AIProxyRequest struct {
	// Method 是 HTTP 方法，原样透传。
	Method string
	// Path 是上游路径（含原始 query），以 `/` 开头，如 `/knowledge-bases?page=1`。
	// 不做任何重写：一旦重写就要维护两侧同步的映射表，那正是透传要避免的成本。
	Path string
	// Body 是请求体（可为 nil）。用 io.Reader 而非 []byte：上传 50MB 文档要能流式转发，
	// 读成 []byte 会让内存正比于文件大小（docs/04-§8 / AC-ORCH-05）。
	Body io.Reader
	// ContentLength 是请求体长度（-1 表示未知）。MUST 透传：multipart 缺它上游无法解析。
	ContentLength int64
	ContentType   string

	// UserToken 是提问者的 Bearer token，原样交给 AI 自己校验（接缝 J1）。
	// MUST NOT 换成自造的身份头（如 `X-User-Id`）—— 多一个可伪造入口（docs/04-§3.2）；
	// 也 MUST NOT 写进日志。
	UserToken string

	// TraceID 用于跨服务链路串联（接缝 J3）。
	TraceID string

	// Class 决定网关超时档位。
	Class AIProxyTimeoutClass

	// IdempotencyKey 是调用方的 `Idempotency-Key` 头（可为空）。
	// 上传接口必须透传（docs/02-§7）：AI 侧据此返回同一 `task_id`，否则重试会得到两份文档。
	IdempotencyKey string
}

// AIProxyResponse 是上游响应中透传真正需要的那几项。
// 不做「任意 header 全量复制」：那会把上游的 Set-Cookie、可能与 body 长度不符的
// Content-Length 一起带给客户端。
type AIProxyResponse struct {
	Status      int
	ContentType string
	Body        []byte
	// RetryAfter 是上游的 `Retry-After` 原值（空表示没有）。
	RetryAfter string
	// TraceID 是上游回显的 `X-Trace-Id`（接缝 J3）。只做一个「是否一致」判断（AC-NFR-06），
	// 用 map 会让「哪些头该转发」变成各调用点自己决定，那正是响应头转发最易错的地方。
	TraceID string
}

// AIProxy 是内网 HTTP 透传客户端。
type AIProxy interface {
	// Do 转发一次请求。
	// 上游的非 2xx 不是错误 —— 它带着必须原样转发的错误信封（docs/02-§4.2 规则 1/2/3），
	// 故走返回值；error 只表示「根本没拿到可用响应」（建连失败、超时、响应体超限）。
	// error MUST 已是 `*errs.AppError`。
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

// AIProxyService 承担透传里唯一属于网关自己的逻辑：会话归属校验。
// 其余 KB/文档/任务的属主不重复校验（AI 侧 REQ-RAG-011 已保证跨用户 404，docs/04-§7）。
// 唯独会话属于网关台账 —— 不校验就等于「猜到 conversation_id 就能读别人上下文」。
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

// Forward 校验后原样转发，并处理「上游响应非约定格式」。
// 只有三种情形返回 error（归属校验失败、凭据缺失、上游响应不可用），其余由 handler 原样写回。
func (s *AIProxyService) Forward(ctx context.Context, userID string, in ProxyInput) (*AIProxyResponse, error) {
	if strings.TrimSpace(in.UserToken) == "" {
		// 鉴权中间件没把 token 传下来。不能退化成用服务凭据转发 ——
		// 那会让 AI 把请求当成网关后台任务，用户隔离失效。
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
	// 4xx / 5xx：带约定信封则原样转发。规则 1/2/3 由「不重新编码」天然满足 ——
	// 重新序列化正是 code/message/details/trace_id 被悄悄改值的唯一来源。
	// 判断与重建共用 `errs.ParseEnvelope`，避免两处 JSON 结构体各写一份而分裂。
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

// Upload 转发一次文档上传，并按结果占用/归还 `documents_count` / `storage_bytes`。
//
// 三步「① 预占额度 → ② 转发 → ③ 失败则归还」必须在一起（docs/04-§8 + docs/02-§5.2）：
// ②的成功判据是「AI 未返回 4xx」，而 4xx 是正常返回值（不支持的类型、重复文档）而非 error ——
// 放在 service 层就会出现「看着写了归还，实际只在网络错误时归还」的漏。
func (s *AIProxyService) Upload(ctx context.Context, userID string, in ProxyInput, sizeBytes int64) (*AIProxyResponse, error) {
	if s.d.Quota == nil {
		return s.Forward(ctx, userID, in)
	}
	items := []StockDelta{{Metric: MetricDocumentsCount, Delta: 1}}
	if sizeBytes > 0 {
		// sizeBytes 未知（chunked 上传）时为 0：宁可少算也不能猜 —— 猜大了会让正常用户传不上去。
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
