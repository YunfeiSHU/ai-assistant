package errs

import (
	"encoding/json"
	"strings"
)

// 本文件处理入站的错误信封：把 ai-platform 返回的 JSON 解析成 AppError。
// 「分类」与「重建」刻意共用同一个函数：两处各写一份 JSON 结构体，会在 AI 侧加字段时
// 出现「一处认、一处不认」的分裂，表现为偶尔把 AI 的错误报成 AI_UNAVAILABLE。

// ParseEnvelope 解析上游错误信封。
// 第二个返回值为 false 表示 body 不是约定格式（HTML 网关错误页、纯文本、
// 或 `error.code` 为空），调用方按 docs/02-§4.2 规则 5 归一化处理。
// 成功时满足规则 1/2/3：各字段取上游原值，`trace_id` 用上游的（不是网关自己的
// request id），状态码用上游的。
func ParseEnvelope(body []byte, status int) (*AppError, bool) {
	if len(body) == 0 {
		return nil, false
	}
	var env Envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, false
	}
	code := Code(strings.TrimSpace(string(env.Error.Code)))
	if code == "" {
		return nil, false
	}
	// 状态码只接受 4xx/5xx：上游若把错误报成 200，照抄会让网关以「200 + 错误信封」
	// 回客户端 —— 那种响应客户端根本没法解析。范围外的值交给 UpstreamError 按码查表。
	upstreamStatus := status
	if upstreamStatus < 400 || upstreamStatus > 599 {
		upstreamStatus = 0
	}
	return UpstreamError(code, env.Error.Message, upstreamStatus,
		env.Error.Retryable, env.Error.TraceID, env.Error.Details), true
}

// IsEnvelope 报告 body 是否是约定格式的错误信封（不重建 AppError）。
//
// 存在它是为了让「只关心是不是信封」的调用方（HTTP 透传：是信封就原样转发字节）
// 不必为了一个布尔值造一个对象，同时仍然只依赖同一份解析逻辑。
func IsEnvelope(body []byte, status int) bool {
	_, ok := ParseEnvelope(body, status)
	return ok
}
