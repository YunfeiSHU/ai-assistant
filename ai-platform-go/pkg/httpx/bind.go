package httpx

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// BindJSON 解析 JSON 请求体并把各类绑定失败映射成契约错误码。
//
// 统一在这里映射，避免每个 handler 各自判断：「body 太大」必须变 413、语法错必须变 400；
// 漏判一处就会出现「超大请求返回 500」这种把调用方问题算成服务端故障的情况。
func BindJSON(c *gin.Context, dst any) error {
	err := c.ShouldBindJSON(dst)
	if err == nil {
		return nil
	}

	var maxErr *http.MaxBytesError
	if errors.As(err, &maxErr) {
		return errs.New(errs.CodePayloadTooLarge).
			WithDetail("limit_bytes", maxErr.Limit)
	}

	var syntaxErr *json.SyntaxError
	if errors.As(err, &syntaxErr) {
		return errs.New(errs.CodeInvalidArgument).
			WithDetail("reason", "malformed_json").
			WithDetail("offset", syntaxErr.Offset).
			WithCause(err)
	}

	var typeErr *json.UnmarshalTypeError
	if errors.As(err, &typeErr) {
		field := typeErr.Field
		if field == "" {
			field = typeErr.Type.String()
		}
		return errs.InvalidArgument([]errs.FieldError{
			{Field: field, Reason: "invalid_type", Message: "字段类型不匹配"},
		}).WithCause(err)
	}

	// 空体与「被截断的体」必须分开报，两者的排查方向完全不同：
	// empty_body → 调用方忘了带 body（或代理吞了它）；truncated_json → body 有内容但没传完（客户端序列化被切断 / 连接中断）。
	// 曾把两者合并成 `empty_body`，于是 `{"email":` 这种「明显有内容」的请求被报成「没有请求体」，排查方向直接跑偏。
	// 注意 `io.ErrUnexpectedEOF` 不满足 `errors.Is(err, io.EOF)`（反之亦然），所以下面两个分支互不干扰、顺序无关。
	if errors.Is(err, io.EOF) {
		return errs.New(errs.CodeInvalidArgument).
			WithDetail("reason", "empty_body").
			WithCause(err)
	}
	if errors.Is(err, io.ErrUnexpectedEOF) {
		return errs.New(errs.CodeInvalidArgument).
			WithDetail("reason", "truncated_json").
			WithCause(err)
	}

	// gin 的 validator 错误（本项目主要手写校验，此处兜住框架级错误）。
	return errs.New(errs.CodeInvalidArgument).
		WithDetail("reason", "invalid_json").
		WithCause(err)
}

// ContentTypeIsJSON 报告请求体的 Content-Type 是否为 JSON。
//
// 兼容带 charset 的写法（`application/json; charset=utf-8`），且对空 Content-Type 返回 true：curl 不带 `-H` 时很常见，契约（docs/02-§1）也没要求必须显式声明。
func ContentTypeIsJSON(c *gin.Context) bool {
	ct := c.ContentType()
	return ct == "" || ct == "application/json"
}
