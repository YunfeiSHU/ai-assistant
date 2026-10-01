package httpx_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

type probeInput struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

// bindOnce 在一条真实的 gin 请求上跑 BindJSON，返回错误码与 details。
func bindOnce(t *testing.T, body string, opts ...func(*http.Request)) (string, map[string]any, error) {
	t.Helper()
	gin.SetMode(gin.TestMode)

	var capturedErr error
	var captured *gin.Context

	e := gin.New()
	e.POST("/x", func(c *gin.Context) {
		var in probeInput
		capturedErr = httpx.BindJSON(c, &in)
		captured = c
		if capturedErr != nil {
			httpx.Fail(c, capturedErr)
			return
		}
		httpx.OK(c, in)
	})

	req := httptest.NewRequest(http.MethodPost, "/x", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for _, opt := range opts {
		opt(req)
	}
	w := httptest.NewRecorder()
	e.ServeHTTP(w, req)

	if capturedErr == nil {
		return "", nil, nil
	}
	appErr := errs.From(capturedErr)
	_ = captured
	return string(appErr.Code()), appErr.Details(), nil
}

// TestBindJSONDistinguishesEmptyMalformedAndTruncated 是本文件的核心用例。
// 三种失败以前都被报成 `empty_body`，而排查方向完全不同：empty_body 是调用方
// 忘了带 body；malformed_json 是客户端拼错了；truncated_json 是 body 没传完。
// 合并后的后果是「明明发出 9 个字节，服务端却回答『你没有请求体』」，
// 排障的人会去反复检查客户端，而不是去看传输是否被截断。
func TestBindJSONDistinguishesEmptyMalformedAndTruncated(t *testing.T) {
	cases := []struct {
		name       string
		body       string
		wantReason string
	}{
		{"完全空体", "", "empty_body"},
		{"只有空白", "   ", "empty_body"},
		{"对象被截断", `{"email":`, "truncated_json"},
		{"字符串值被截断", `{"email":"a@b`, "truncated_json"},
		{"完整但语法错（键未加引号）", `{email: 1}`, "malformed_json"},
		{"完整但语法错（多余逗号）", `{"email":"a@b.c",}`, "malformed_json"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, details, err := bindOnce(t, tc.body)
			if err != nil {
				t.Fatalf("BindJSON 不应返回裸 error: %v", err)
			}
			if code != string(errs.CodeInvalidArgument) {
				t.Fatalf("code = %s, want INVALID_ARGUMENT", code)
			}
			if got := details["reason"]; got != tc.wantReason {
				t.Errorf("reason = %v, want %s", got, tc.wantReason)
			}
		})
	}
}

// TestBindJSONTypeMismatchProducesFieldError 断言类型错会带字段名。
// 只说「请求参数不合法」会让客户端被迫逐个字段二分查找 ——
// 而「哪个字段类型错了」正是网关唯一比客户端更清楚的上下文。
func TestBindJSONTypeMismatchProducesFieldError(t *testing.T) {
	code, details, err := bindOnce(t, `{"email":123}`)
	if err != nil {
		t.Fatalf("BindJSON 不应返回裸 error: %v", err)
	}
	if code != string(errs.CodeInvalidArgument) {
		t.Fatalf("code = %s, want INVALID_ARGUMENT", code)
	}
	fields, ok := details["fields"].([]map[string]any)
	if !ok {
		// InvalidArgument 可能用 []errs.FieldError；统一转成 JSON 再看。
		raw, mErr := json.Marshal(details["fields"])
		if mErr != nil {
			t.Fatalf("details.fields 类型异常: %T (%v)", details["fields"], details["fields"])
		}
		var generic []map[string]any
		if uErr := json.Unmarshal(raw, &generic); uErr != nil {
			t.Fatalf("details.fields 无法解析: %v", uErr)
		}
		fields = generic
	}
	if len(fields) == 0 {
		t.Fatalf("类型错必须带 details.fields，实际 details=%v", details)
	}
	if fields[0]["field"] != "email" {
		t.Errorf("fields[0].field = %v, want email", fields[0]["field"])
	}
	if fields[0]["reason"] != "invalid_type" {
		t.Errorf("fields[0].reason = %v, want invalid_type", fields[0]["reason"])
	}
}

// TestBindJSONOversizeIsPayloadTooLarge 断言超限映射成 413 而不是 400。
// 「body 太大」是调用方的问题，但不是参数不合法：413 才能触发客户端改成
// 分片上传/减少内容这类正确的补救动作，而不是去改字段格式。
func TestBindJSONOversizeIsPayloadTooLarge(t *testing.T) {
	const limit = 256
	oversize := `{"email":"` + strings.Repeat("a", 1000) + `"}`

	code, details, err := bindOnce(t, oversize, func(req *http.Request) {
		w := httptest.NewRecorder()
		req.Body = http.MaxBytesReader(w, req.Body, limit)
	})
	if err != nil {
		t.Fatalf("BindJSON 不应返回裸 error: %v", err)
	}
	if code != string(errs.CodePayloadTooLarge) {
		t.Fatalf("code = %s, want PAYLOAD_TOO_LARGE", code)
	}
	if got := details["limit_bytes"]; got != int64(limit) {
		t.Errorf("details.limit_bytes = %v (%T), want %d —— 客户端需要知道上限才能正确重试", got, got, limit)
	}
}

// TestBindJSONHappyPath 断言合法请求不被误判。
func TestBindJSONHappyPath(t *testing.T) {
	code, _, err := bindOnce(t, `{"email":"a@b.c","password":"X#12345678"}`)
	if code != "" || err != nil {
		t.Fatalf("合法请求被拒: code=%s err=%v", code, err)
	}
}

// TestBindJSONAcceptsUTF8 断言中文能被正确解析。
// 编码问题在这里很隐蔽：按 latin-1 解码时中文会变成乱码但不报错，
// 于是错误数据一路写进数据库。
func TestBindJSONAcceptsUTF8(t *testing.T) {
	code, _, err := bindOnce(t, `{"email":"中文昵称@b.c","password":"X#12345678"}`)
	if code != "" || err != nil {
		t.Fatalf("含中文的合法请求被拒: code=%s err=%v", code, err)
	}
}

// TestContentTypeIsJSON 覆盖 Content-Type 的宽容规则。
// 空 Content-Type 视为 JSON 是刻意的：`curl -d` 不带 `-H` 是最常见的手工调试方式，
// 契约（docs/02 §1）也没要求显式声明。但 415 仍要留给真正不兼容的类型。
func TestContentTypeIsJSON(t *testing.T) {
	cases := map[string]bool{
		"":                                    true,
		"application/json":                    true,
		"application/json; charset=utf-8":     true,
		"application/json;charset=UTF-8":      true,
		"text/plain":                          false,
		"application/x-www-form-urlencoded":   false,
		"multipart/form-data; boundary=----x": false,
	}
	for ct, want := range cases {
		t.Run("ct="+ct, func(t *testing.T) {
			gin.SetMode(gin.TestMode)
			var got bool
			e := gin.New()
			e.POST("/x", func(c *gin.Context) { got = httpx.ContentTypeIsJSON(c) })
			req := httptest.NewRequest(http.MethodPost, "/x", nil)
			if ct != "" {
				req.Header.Set("Content-Type", ct)
			}
			e.ServeHTTP(httptest.NewRecorder(), req)
			if got != want {
				t.Errorf("ContentTypeIsJSON(%q) = %v, want %v", ct, got, want)
			}
		})
	}
}
