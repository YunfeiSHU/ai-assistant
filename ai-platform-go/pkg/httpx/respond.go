package httpx

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
)

// Failed 是错误响应与终止标记的键：即使某个 handler 忘了 `return`，
// 后续 handler 也会因为该标记而不再执行（gin 的 Abort 是跨中间件的）。
const (
	keyFailed   = "gw.failed"
	keyFailedAt = "gw.failed_at"
)

// Fail 按统一信封写出错误并终止后续处理。
//
// 这是唯一允许写 4xx/5xx 的出口：所有错误都必须经过它，才能保证信封字段结构一致，
// 且 trace_id 与响应头 X-Trace-Id、日志三者相同（docs/02-§2.2）。
func Fail(c *gin.Context, err error) {
	writeError(c, err)
	c.Abort()
}

// FailAfter 与 Fail 相同，但在已写出部分响应体后使用（如 SSE 中途出错）：
// 此时响应头已发出，不能再写 JSON 信封，只能记日志并中断。
func FailAfter(c *gin.Context, err error) {
	appErr := errs.From(err)
	logx.From(c.Request.Context(), nil).ErrorContext(c.Request.Context(), "http.fail_after_write",
		slog.String("code", string(appErr.Code())),
		slog.Int("status", appErr.Status()),
		slog.String("error", appErr.Error()),
	)
	c.Abort()
}

func writeError(c *gin.Context, err error) {
	appErr := errs.From(err)

	traceID := appErr.TraceID()
	if traceID == "" {
		traceID = TraceIDOf(c)
	}
	appErr = appErr.WithTraceID(traceID)

	body := errs.Envelope{Error: errs.ErrorBody{
		Code:       appErr.Code(),
		Message:    appErr.Message(),
		Details:    appErr.Details(),
		TraceID:    traceID,
		Retryable:  appErr.Retryable(),
		RetryAfter: appErr.RetryAfter(),
	}}

	status := appErr.Status()
	if status == http.StatusTooManyRequests && appErr.RetryAfter() > 0 {
		c.Header("Retry-After", itoa(appErr.RetryAfter()))
	}
	c.Set(keyFailed, true)
	c.Set(keyFailedAt, appErr.Code())

	// 5xx 记 ERROR（含内部原因），4xx 记 INFO（客户端的问题）。
	// base 传 nil：logger 由 AccessLog 放进 context，这里只补充字段。
	log := logx.From(c.Request.Context(), nil)
	attrs := []any{
		slog.String("code", string(appErr.Code())),
		slog.Int("status", status),
		slog.String("route", c.FullPath()),
	}
	// 路由未匹配时 FullPath 为空：把真实路径记下来，否则 404 的排查会变成
	// 「不知道客户端打了什么」（与 AccessLog 的处理保持一致）。
	if c.FullPath() == "" {
		attrs = append(attrs, slog.String("path", c.Request.URL.Path))
	}
	if appErr.Cause() != nil {
		attrs = append(attrs, slog.String("cause", appErr.Cause().Error()))
	}
	if status >= 500 {
		log.ErrorContext(c.Request.Context(), "http.error", attrs...)
	} else {
		log.InfoContext(c.Request.Context(), "http.rejected", attrs...)
	}

	c.JSON(status, body)
}

// Failed 报告本次请求是否已经写出错误响应。
func Failed(c *gin.Context) bool {
	if v, ok := c.Get(keyFailed); ok {
		b, _ := v.(bool)
		return b
	}
	return false
}

// FailedCode 返回已写出的错误码（未失败时为空串）。
func FailedCode(c *gin.Context) errs.Code {
	if v, ok := c.Get(keyFailedAt); ok {
		if code, ok := v.(errs.Code); ok {
			return code
		}
	}
	return ""
}

// OK 写 200。
func OK(c *gin.Context, payload any) { c.JSON(http.StatusOK, payload) }

// Created 写 201，并设置 Location 头（docs/02-§2.1）。
func Created(c *gin.Context, payload any, location string) {
	if location != "" {
		c.Header("Location", location)
	}
	c.JSON(http.StatusCreated, payload)
}

// Accepted 写 202（异步受理）。
func Accepted(c *gin.Context, payload any) { c.JSON(http.StatusAccepted, payload) }

// NoContent 写 204（无响应体）。
func NoContent(c *gin.Context) { c.Status(http.StatusNoContent) }

// TraceIDOf 从 gin 上下文取 trace id（避免 httpx 依赖整个 middleware 包）。
func TraceIDOf(c *gin.Context) string {
	if v, ok := c.Get("gw.trace_id"); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func itoa(n int) string {
	if n <= 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
