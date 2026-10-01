package errs

import (
	"errors"
	"fmt"
	"net/http"
)

// ErrorBody 是错误信封里的 error 对象（docs/02-§2.2）。
type ErrorBody struct {
	Code       Code           `json:"code"`
	Message    string         `json:"message"`
	Details    map[string]any `json:"details,omitempty"`
	TraceID    string         `json:"trace_id"`
	Retryable  bool           `json:"retryable"`
	RetryAfter int            `json:"retry_after,omitempty"`
}

// Envelope 是统一失败响应体：{"error": {...}}。
type Envelope struct {
	Error ErrorBody `json:"error"`
}

// AppError 是网关内部流转的统一错误类型。
//
// 它是一个「可以安全地把 message 展示给客户端」的错误：内部原因放在 cause 里，
// 只记日志，不进响应体（REQ-NFR-005）。
type AppError struct {
	code           Code
	message        string
	details        map[string]any
	traceID        string
	retryable      bool
	retryAfter     int
	status         int
	upstreamStatus int
	cause          error
	// upstream 标记该错误来自 ai-platform（透传），用于决定是否追加
	// details.gateway（docs/02-§4.2 第 4 条）与指标标签。
	upstream bool
}

// Error 实现 error 接口。
func (e *AppError) Error() string {
	if e.cause != nil {
		return fmt.Sprintf("%s: %s: %v", e.code, e.message, e.cause)
	}
	return fmt.Sprintf("%s: %s", e.code, e.message)
}

// Unwrap 支持 errors.Is / errors.As 沿 cause 链查找。
func (e *AppError) Unwrap() error { return e.cause }

// Code 返回错误码。
func (e *AppError) Code() Code { return e.code }

// Message 返回面向客户端的文案。
func (e *AppError) Message() string { return e.message }

// Details 返回结构化补充信息。
func (e *AppError) Details() map[string]any { return e.details }

// TraceID 返回该错误应携带的 trace_id（接缝 J2：透传上游时必须沿用上游值）。
func (e *AppError) TraceID() string { return e.traceID }

// Status 返回应写出的 HTTP 状态码。
func (e *AppError) Status() int {
	if e.status > 0 {
		return e.status
	}
	if st, ok := LookupStatus(e.code); ok {
		return st
	}
	return http.StatusInternalServerError
}

// Retryable 报告客户端可否安全重试。
func (e *AppError) Retryable() bool { return e.retryable }

// RetryAfter 返回 Retry-After 秒数（0 表示不输出该字段）。
func (e *AppError) RetryAfter() int { return e.retryAfter }

// UpstreamStatus 返回上游真实状态码（仅上游性质错误有值）。
func (e *AppError) UpstreamStatus() int { return e.upstreamStatus }

// IsUpstream 报告该错误是否来自 ai-platform。
func (e *AppError) IsUpstream() bool { return e.upstream }

// Cause 返回内部原因（可能为 nil）。
func (e *AppError) Cause() error { return e.cause }

// WithTraceID 返回带 trace_id 的副本。
//
// trace_id 通常在中间件层统一注入，因此构造函数允许先不填。
func (e *AppError) WithTraceID(traceID string) *AppError {
	clone := *e
	if traceID != "" {
		clone.traceID = traceID
	}
	return &clone
}

// WithDetail 返回追加单个 details 键的副本。
func (e *AppError) WithDetail(key string, value any) *AppError {
	clone := *e
	clone.details = make(map[string]any, len(e.details)+1)
	for k, v := range e.details {
		clone.details[k] = v
	}
	clone.details[key] = value
	return &clone
}

// WithMessage 返回替换文案的副本（用于同一错误码下的更具体提示）。
func (e *AppError) WithMessage(msg string) *AppError {
	clone := *e
	clone.message = msg
	return &clone
}

// WithCause 返回挂上内部原因的副本（原因不进响应体，只进日志）。
func (e *AppError) WithCause(err error) *AppError {
	clone := *e
	clone.cause = err
	return &clone
}

// WithRetryAfter 返回带 `Retry-After` 秒数的副本。
//
// 只有 `429` 会把它写进响应头与信封（见 httpx.writeError）—— 这是 HTTP 既有约定，不该给 503 配 `Retry-After` 再指望客户端理解；
// 用秒数而非时间点：客户端与服务端的时钟差可以到分钟级，绝对时间会让「等 3 秒」变成「等 40 秒」或「立刻重试」。
func (e *AppError) WithRetryAfter(seconds int) *AppError {
	clone := *e
	if seconds < 0 {
		seconds = 0
	}
	clone.retryAfter = seconds
	return &clone
}

// New 按错误码构造，使用该码的默认文案与重试语义。
func New(code Code) *AppError {
	s, ok := specs[code]
	if !ok {
		// 未知码：按内部错误处理，但保留原码以便排障。
		st, _ := LookupStatus(code)
		return &AppError{code: code, message: string(code), status: st, retryable: true}
	}
	return &AppError{code: code, message: s.message, retryable: s.retryable, status: s.status}
}

// Newf 按错误码构造并格式化文案。
func Newf(code Code, format string, args ...any) *AppError {
	return New(code).WithMessage(fmt.Sprintf(format, args...))
}

// Wrap 按错误码包装内部错误。
func Wrap(code Code, cause error) *AppError {
	return New(code).WithCause(cause)
}

// Wrapf 按错误码包装内部错误并格式化文案。
func Wrapf(code Code, cause error, format string, args ...any) *AppError {
	return New(code).WithCause(cause).WithMessage(fmt.Sprintf(format, args...))
}

// InvalidArgument 构造 400，details.fields 承载字段级原因（docs/02-§4.1）。
//
// fields 为空时**不写** `details.fields`：写 `"fields": null` 会让客户端的「有字段错误吗」判断变得不可靠（null 在 JS 里是 falsy，在其它语言里不是）。
func InvalidArgument(fields []FieldError) *AppError {
	err := New(CodeInvalidArgument)
	if len(fields) == 0 {
		return err
	}
	list := make([]map[string]any, 0, len(fields))
	for _, f := range fields {
		item := map[string]any{"field": f.Field, "reason": f.Reason}
		if f.Message != "" {
			item["message"] = f.Message
		}
		list = append(list, item)
	}
	return err.WithDetail("fields", list)
}

// FieldError 是一个字段级校验失败。
type FieldError struct {
	Field   string
	Reason  string
	Message string
}

// NotFound 按资源类型返回 404（会话与消息有专用码）。
func NotFound(code Code) *AppError { return New(code) }

// UpstreamError 构造「原样透传上游」的错误（接缝 J2，docs/02-§4.2）。
//
// status 用上游真实状态码；message / details / retryable / traceID 全部取上游原值。
// **不修改 details 原有键**，只在没有 gateway 键时追加 `details.gateway`（§4.2 第 4 条）。
func UpstreamError(code Code, message string, status int, retryable bool, traceID string, details map[string]any) *AppError {
	if status <= 0 || status > 599 {
		if st, ok := LookupStatus(code); ok {
			status = st
		} else {
			status = http.StatusBadGateway
		}
	}
	merged := make(map[string]any, len(details)+1)
	for k, v := range details {
		merged[k] = v
	}
	if _, exists := merged["gateway"]; !exists {
		merged["gateway"] = map[string]any{"upstream": "ai-platform"}
	}
	return &AppError{
		code:      code,
		message:   message,
		details:   merged,
		traceID:   traceID,
		retryable: retryable,
		status:    status,
		upstream:  true,
	}
}

// As 从错误链里提取 *AppError。
func As(err error) (*AppError, bool) {
	var target *AppError
	if errors.As(err, &target) {
		return target, true
	}
	return nil, false
}

// From 把任意错误归一化成 *AppError。
//
// 已经是 *AppError 的原样返回；nil 返回 nil；其余归到 INTERNAL_ERROR。
func From(err error) *AppError {
	if err == nil {
		return nil
	}
	if appErr, ok := As(err); ok {
		return appErr
	}
	return Wrap(CodeInternalError, err)
}

// IsNotFound 报告错误是否为 404 族（供幂等/缓存层判断）。
func IsNotFound(err error) bool {
	appErr, ok := As(err)
	if !ok {
		return false
	}
	switch appErr.Code() {
	case CodeResourceNotFound, CodeConversationNotFound, CodeMessageNotFound:
		return true
	}
	return appErr.Status() == http.StatusNotFound
}
