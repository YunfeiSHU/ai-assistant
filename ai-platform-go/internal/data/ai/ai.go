// Package ai 是 ai-platform 的客户端（网关 → AI 的传输实现）。
//
//	ai.go     公共选项、错误信封转换、响应映射（两种传输共用）
//	grpc.go   Kratos gRPC 客户端：实现 biz.ChatOrchestrator
//	http.go   HTTP 客户端：实现 biz.AIProxy（透传）
//
// 依赖方向 data → biz（规范 §四），所以本包可以 import biz 与 conf，
// 反过来不行。包名 `ai` 而不是 `aiplatform`：目录已经说明了是谁的客户端。
package ai

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"google.golang.org/grpc/codes"

	aiplatformv1 "github.com/YunfeiSHU/ai-assistant/ai-platform-go/api/aiplatform/v1"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// Options 是两种传输共用的连接参数。
//
// 从 `conf.Config` 摊平出来的原因：让传输层只依赖它真正用到的几个值，
// 于是「网关超时比 AI 宽松」这条规则（docs/04-§3.3）能在单测里直接
// 构造极端值验证，而不需要拼一个完整配置。
type Options struct {
	BaseURL    string
	GRPCTarget string

	ConnectTimeout time.Duration
	MetaTimeout    time.Duration
	ChatTimeout    time.Duration
	UploadTimeout  time.Duration
	// TotalTimeout 是一轮流式回答的**整轮上限**（`AI_TOTAL_TIMEOUT_SECONDS`）。
	//
	// 非流式路径不需要它（`ChatTimeout` 就是整轮上限），但流式路径的超时
	// 是「总时长 + 空闲」两个维度，而整轮上限由 biz 的编排循环负责触发
	// （它要在超时时发一条 `error` 帧并落 `partial`）。传输层拿到它的唯一
	// 用途是**兜底**：见 grpc_stream.go 的 `streamDeadline`。
	TotalTimeout time.Duration

	// MaxResponseMB 是上游响应体的上限（docs/04-§3.1）。
	// 超过即当作传输失败：文档切片/检索结果可以很大，但不该无限大，
	// 没有上限时一个异常响应就能把网关的内存吃光。
	MaxResponseMB int

	Log *slog.Logger
}

// FromConfig 把配置摊平成 Options。
func FromConfig(cfg *conf.Config, log *slog.Logger) Options {
	if log == nil {
		log = slog.Default()
	}
	return Options{
		BaseURL:        cfg.AI.BaseURL,
		GRPCTarget:     cfg.AI.GRPCTarget,
		ConnectTimeout: cfg.AI.ConnectTimeout,
		MetaTimeout:    cfg.AI.MetaTimeout,
		ChatTimeout:    cfg.AI.ChatTimeout,
		UploadTimeout:  cfg.AI.UploadTimeout,
		TotalTimeout:   cfg.AI.TotalTimeout,
		MaxResponseMB:  cfg.AI.MaxResponseMB,
		Log:            log,
	}
}

// timeoutFor 把超时档位映射成具体时长（docs/04-§3.3）。
//
// 值全部来自配置（`AI_*_TIMEOUT_SECONDS`），缺省值在 conf 层已经比 AI 侧宽松。
// 未知档位回落到元数据档：宁可比上游早超时（错误码不精确），
// 也不要挂着一个没有上限的请求。
func (o Options) timeoutFor(class biz.AIProxyTimeoutClass) time.Duration {
	switch class {
	case biz.AIProxyTimeoutChat:
		return o.ChatTimeout
	case biz.AIProxyTimeoutUpload:
		return o.UploadTimeout
	default:
		return o.MetaTimeout
	}
}

func (o Options) maxResponseBytes() int64 {
	if o.MaxResponseMB <= 0 {
		return 16 << 20
	}
	return int64(o.MaxResponseMB) << 20
}

// ---- 错误信封 ----

// aiErrorToAppError 把 AI 侧的错误信封翻译成网关统一错误。
//
// 三条不可动摇的规则（docs/02-§4.2）：
//
//  1. `error.code` 原值保留 —— 网关**不认识**的 AI 错误码也必须照传，
//     所以这里从来不做「码表白名单」判断（`errs.Code` 只是字符串别名）。
//  2. `error.trace_id` 沿用 AI 的值，不是网关自己的 request id；
//     否则排障时链路在网关这一跳断开。
//  3. HTTP 状态码沿用上游的。
//
// 关于「信任上游的状态码」：只接受 4xx/5xx。上游若把一个错误报成 200，
// 照抄会让网关以 200 + 错误信封回客户端 —— 那种响应客户端的解析器会直接崩，
// 比状态码不精确危险得多。范围外的值一律回退到按码查表。
func aiErrorToAppError(in *aiplatformv1.AiError, fallbackStatus int) *errs.AppError {
	if in == nil {
		return nil
	}
	code := errs.Code(strings.TrimSpace(in.GetCode()))
	if code == "" {
		return nil
	}
	status := int(in.GetHttpStatus())
	if status < 400 || status > 599 {
		if fallbackStatus >= 400 && fallbackStatus <= 599 {
			status = fallbackStatus
		} else {
			status = 0 // 交给 errs 按码查表
		}
	}
	var details map[string]any
	if raw := strings.TrimSpace(in.GetDetailsJson()); raw != "" {
		// 解不开就丢掉 details 而不是丢掉整个错误：错误码与文案才是
		// 客户端分支的依据，为了一个补充字段把主信息降级成 INTERNAL_ERROR
		// 是明显更差的选择。
		if err := json.Unmarshal([]byte(raw), &details); err != nil {
			details = nil
		}
	}
	return errs.UpstreamError(code, in.GetMessage(), status, in.GetRetryable(), in.GetTraceId(), details)
}

// httpStatusForGRPCCode 是「上游只回了规范码、没带信封」时的兜底映射。
//
// 不作为常规路径：它比信封少一个应用错误码，客户端只能看到网关猜出来的码。
func httpStatusForGRPCCode(c codes.Code) int {
	switch c {
	case codes.InvalidArgument, codes.FailedPrecondition, codes.OutOfRange:
		return 400
	case codes.Unauthenticated:
		return 401
	case codes.PermissionDenied:
		return 403
	case codes.NotFound:
		return 404
	case codes.AlreadyExists, codes.Aborted:
		return 409
	case codes.ResourceExhausted:
		return 429
	case codes.Unimplemented:
		return 501
	case codes.Unavailable:
		return 503
	case codes.DeadlineExceeded:
		return 504
	case codes.Canceled:
		// 499 是 nginx 的事实标准；网关侧「客户端断开」本来就不写响应，
		// 走到这里说明是上游主动取消，用 499 表示即可。
		return 499
	default:
		return 500
	}
}

// retryableForGRPCCode 判断该规范码是否可安全重试（docs/04-§3.4）。
//
// `Chat` 本身 MUST NOT 重试（会重复计费），这里的值只用于填 `error.retryable`
// 让客户端知道「稍后重发是合理的」。
func retryableForGRPCCode(c codes.Code) bool {
	switch c {
	case codes.Unavailable, codes.DeadlineExceeded, codes.ResourceExhausted, codes.Aborted:
		return true
	default:
		return false
	}
}

// ---- 响应映射 ----

// referenceJSON 是 `references[]` 的线上形状（与 ai-platform 的
// `app/schemas/chat.py::Reference` 字段级对齐，AC-ORCH-02）。
//
// 为什么手写结构体而不是 `json.Marshal(proto 结构)`：protoc-gen-go 给**每个**
// 字段都加了 `,omitempty`，于是 `score == 0`、`index == 0`、空 `doc_name`
// 都会被静默丢掉。这些字段在契约里有含义（`score` 0 分与「没这个字段」
// 在展示层是两件事），所以「哪些键可缺省」必须由契约决定，而不是由 Go 的
// omitempty 决定。
type referenceJSON struct {
	Index         int32   `json:"index"`
	ChunkID       string  `json:"chunk_id"`
	DocID         string  `json:"doc_id"`
	KBID          string  `json:"kb_id"`
	DocName       string  `json:"doc_name"`
	Page          *int32  `json:"page,omitempty"`
	HeadingPath   *string `json:"heading_path,omitempty"`
	Score         float64 `json:"score"`
	Snippet       string  `json:"snippet"`
	ContentSHA256 string  `json:"content_sha256"`
}

// toolCallJSON 是 `tool_calls[]` 的线上形状。
//
// `arguments` 是**对象**，而 proto 里传的是 `arguments_json` 字符串
// （proto3 没有「任意 JSON」类型，见 chat.proto 的注释）。转换发生在这一层：
// 网关是唯一同时看得见两种形状的地方，把它放进 biz 就得让领域层认识 JSON 字符串。
type toolCallJSON struct {
	CallID    string         `json:"call_id"`
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
	Status    string         `json:"status"`
	Summary   string         `json:"summary"`
	ElapsedMS int64          `json:"elapsed_ms"`
}

// marshalReferences 把 proto 的引用列表编码成契约要求的 JSON 数组。
//
// 空列表返回 nil（而不是 `[]`）：存储层用「空」表示 NULL，
// 对外视图由 service 统一补成 `[]`（`dto.rawOrEmptyArray`）。
// 在这一层补 `[]` 反而会让「AI 没返回引用」与「返回了空数组」在库里无法区分。
func marshalReferences(refs []*aiplatformv1.Reference) (json.RawMessage, error) {
	if len(refs) == 0 {
		return nil, nil
	}
	out := make([]referenceJSON, 0, len(refs))
	for _, r := range refs {
		if r == nil {
			continue
		}
		out = append(out, referenceJSON{
			Index:         r.GetIndex(),
			ChunkID:       r.GetChunkId(),
			DocID:         r.GetDocId(),
			KBID:          r.GetKbId(),
			DocName:       r.GetDocName(),
			Page:          r.Page,
			HeadingPath:   r.HeadingPath,
			Score:         r.GetScore(),
			Snippet:       r.GetSnippet(),
			ContentSHA256: r.GetContentSha256(),
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return json.Marshal(out)
}

// marshalToolCalls 把 proto 的工具轨迹编码成契约要求的 JSON 数组。
func marshalToolCalls(calls []*aiplatformv1.ToolCallTrace) (json.RawMessage, error) {
	if len(calls) == 0 {
		return nil, nil
	}
	out := make([]toolCallJSON, 0, len(calls))
	for _, c := range calls {
		if c == nil {
			continue
		}
		args := map[string]any{}
		if raw := strings.TrimSpace(c.GetArgumentsJson()); raw != "" {
			// 解析失败时留空对象：`arguments` 在契约里是 dict，
			// 塞一个字符串进去会让客户端反序列化失败（"status" 才对不上）。
			if err := json.Unmarshal([]byte(raw), &args); err != nil {
				args = map[string]any{}
			}
		}
		out = append(out, toolCallJSON{
			CallID:    c.GetCallId(),
			Name:      c.GetName(),
			Arguments: args,
			Status:    c.GetStatus(),
			Summary:   c.GetSummary(),
			ElapsedMS: c.GetElapsedMs(),
		})
	}
	if len(out) == 0 {
		return nil, nil
	}
	return json.Marshal(out)
}

// usageFromProto 转换 token 用量，并补齐 `total = prompt + completion`。
//
// 恒等式由 AI 侧保证（其 `Usage.model_validator`），这里再兜一次是为了配额：
// 配额按 `total_tokens` 累加（接缝 J7），上游偶发只给分项时如果照存 0，
// 结果就是**配额永远不涨且不报错**（docs/03-§4.1 的同一类坑）。
func usageFromProto(u *aiplatformv1.Usage) *biz.MessageUsage {
	if u == nil {
		return nil
	}
	usage := &biz.MessageUsage{
		PromptTokens:     int(u.GetPromptTokens()),
		CompletionTokens: int(u.GetCompletionTokens()),
		TotalTokens:      int(u.GetTotalTokens()),
	}
	if usage.TotalTokens == 0 && (usage.PromptTokens != 0 || usage.CompletionTokens != 0) {
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	}
	if usage.IsZero() {
		return nil
	}
	return usage
}

// ctxWithTimeout 是「按档位设超时」的唯一入口，便于测试直接断言。
func ctxWithTimeout(parent context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	if d <= 0 {
		return context.WithCancel(parent)
	}
	return context.WithTimeout(parent, d)
}
