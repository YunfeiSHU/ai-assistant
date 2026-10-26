package service

import (
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// TaskEventHandler 透传 ai-platform 的任务进度 SSE（B-01）。
//
// 不复用 AIProxy：那条通道会把响应体读完再返回，SSE 会被缓冲到任务结束，
// 等于没有流。这里只做「鉴权头转发 + 逐块 flush」，业务语义仍由 Python 决定。
type TaskEventHandler struct {
	baseURL string
	client  *http.Client
}

// NewTaskEventHandler 构造任务 SSE 透传。
func NewTaskEventHandler(baseURL string) *TaskEventHandler {
	return &TaskEventHandler{
		baseURL: strings.TrimRight(baseURL, "/"),
		client: &http.Client{
			// 任务流可能跑很久；总超时交给上游自己的 max_seconds。
			Timeout: 0,
			Transport: &http.Transport{
				Proxy:                 http.ProxyFromEnvironment,
				MaxIdleConnsPerHost:   16,
				IdleConnTimeout:       90 * time.Second,
				ResponseHeaderTimeout: 10 * time.Second,
			},
		},
	}
}

// Stream 处理 `GET /tasks/:task_id/events`。
func (h *TaskEventHandler) Stream(c *gin.Context) {
	ctx := c.Request.Context()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.baseURL+c.Request.URL.RequestURI(), nil)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(middleware.RawToken(c)))
	if traceID := middleware.TraceID(c); traceID != "" {
		req.Header.Set("X-Trace-Id", traceID)
	}
	req.Header.Set("Accept", "text/event-stream")

	resp, err := h.client.Do(req)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	defer func() { _ = resp.Body.Close() }()

	// 非 2xx 是普通的 JSON 错误信封；原样写回，不能假装成 SSE。
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		if ct := resp.Header.Get("Content-Type"); ct != "" {
			c.Header("Content-Type", ct)
		}
		c.Status(resp.StatusCode)
		_, _ = io.Copy(c.Writer, resp.Body)
		return
	}

	c.Header("Content-Type", "text/event-stream; charset=utf-8")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	c.Status(http.StatusOK)

	flusher, _ := c.Writer.(http.Flusher)
	buf := make([]byte, 4096)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, writeErr := c.Writer.Write(buf[:n]); writeErr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			return
		}
	}
}
