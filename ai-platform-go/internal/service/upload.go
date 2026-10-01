package service

import (
	"errors"
	"io"
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// UploadHandler 处理 `POST /knowledge-bases/{kb_id}/documents`（docs/04-§8）。
//
// 网关在这个接口上只做三件事，其余（扩展名/魔数校验、入库、建任务）全在 AI：
//
//	① 大小预检（`Content-Length` 超 `UPLOAD_MAX_MB` 直接 413，不浪费一次转发）
//	② 原样转发**字节流**（`io.Copy` 语义，不经内存、不落盘、不写 MinIO）
//	③ 把 AI 的 `202` 与响应体原样写回（`task_id` / `doc_id` 一个字都不改）
//
// 为什么不在网关校验扩展名：docs/04-§8 明确「避免重复实现文件类型白名单」——
// 两份白名单迟早会分叉，而分叉的表现是「网关放过的文件 AI 全部 422」，
// 排查时两边都觉得自己没错。
type UploadHandler struct {
	svc      *biz.AIProxyService
	maxBytes int64
}

// NewUploadHandler 构造上传 handler；maxMB <= 0 时用 50MB（`UPLOAD_MAX_MB` 的默认值）。
func NewUploadHandler(svc *biz.AIProxyService, maxMB int) *UploadHandler {
	if maxMB <= 0 {
		maxMB = 50
	}
	return &UploadHandler{svc: svc, maxBytes: int64(maxMB) * 1024 * 1024}
}

// Upload 处理上传。
func (h *UploadHandler) Upload(c *gin.Context) {
	if h == nil || h.svc == nil {
		httpx.Fail(c, errs.New(errs.CodeAIUnavailable).WithDetail("reason", "upload_not_configured"))
		return
	}

	// 前置大小校验走 `Content-Length` 而不是「边读边数」：
	// 后者虽然更准（chunked 上传没有长度），但等到数出超限时**上游已经收到了
	// 大半个文件**。两条都要：这里拦「声明就超了」的（省掉整次转发），
	// `uploadReader` 拦「声明撒谎」的（见下）。
	if h.maxBytes > 0 && c.Request.ContentLength > h.maxBytes {
		httpx.Fail(c, errs.New(errs.CodePayloadTooLarge).
			WithMessage("文件过大").
			WithDetail("limit_bytes", h.maxBytes).
			WithDetail("size_bytes", c.Request.ContentLength))
		return
	}

	body := &uploadReader{inner: c.Request.Body, limit: h.maxBytes}

	resp, err := h.svc.Upload(c.Request.Context(), middleware.UserID(c), biz.ProxyInput{
		Method: c.Request.Method,
		Path:   upstreamPath(c),
		Body:   body,
		// **原样**透传：multipart 缺 Content-Length 时上游无法定界，
		// 而 net/http 对未知长度的 body 会用 chunked，FastAPI 能收但不理想。
		ContentLength: c.Request.ContentLength,
		ContentType:   c.Request.Header.Get("Content-Type"),
		UserToken:     middleware.RawToken(c),
		TraceID:       middleware.TraceID(c),
		Class:         biz.AIProxyTimeoutUpload,
		// 幂等键原样交给 AI（docs/02-§7）：重试必须拿到同一个 task_id。
		IdempotencyKey: strings.TrimSpace(c.GetHeader("Idempotency-Key")),
	}, c.Request.ContentLength)
	if err != nil {
		if body.exceeded() {
			// 声明撒谎（或 chunked）：在读的时候才发现超限。
			httpx.Fail(c, errs.New(errs.CodePayloadTooLarge).
				WithMessage("文件过大").
				WithDetail("limit_bytes", h.maxBytes))
			return
		}
		httpx.Fail(c, err)
		return
	}

	// 原样回写：状态码（202）、Content-Type、body。
	if ct := resp.ContentType; ct != "" {
		c.Header("Content-Type", ct)
	}
	c.Status(resp.Status)
	if len(resp.Body) > 0 {
		if _, werr := c.Writer.Write(resp.Body); werr != nil {
			_ = c.Error(werr)
		}
	}
}

// uploadReader 在转发过程中累计字节数并在超限时中断。
//
// 为什么需要它：`Content-Length` 是**客户端声明的**，可以少报
// （甚至用 chunked 完全不报）。只信头部的后果是「限制 50MB 实际上没有限制」，
// 而上游的 `MaxBytesReader` 到那时候已经在读第 500MB 了。
//
// 超限时返回 `errUploadTooLarge`：它会让 `io.Copy` 中断，
// 于是上游收到一个**不完整的 multipart body**，解析失败返回 400 ——
// 这时网关的响应已经被 `body.exceeded()` 改写成了 413。
// 让上游看到半个请求而不是静默等待，是必要的：否则这次请求会一直挂到超时。
type uploadReader struct {
	inner io.Reader
	limit int64
	read  int64
	over  bool
}

var errUploadTooLarge = errors.New("service: 上传大小超过限制")

// Read 边转发边累计字节数，一旦超过 limit 就立刻返回 errUploadTooLarge。
//
// 返回 `n, errUploadTooLarge`（而不是 `0, err`）是有意的：已读到的这部分
// 仍然交给 io.Copy 写出去，让上游尽快看到一个不完整的 body 并立刻失败，
// 而不是继续等一个永远读不完的请求。
func (r *uploadReader) Read(p []byte) (int, error) {
	if r.over {
		return 0, errUploadTooLarge
	}
	n, err := r.inner.Read(p)
	r.read += int64(n)
	if r.limit > 0 && r.read > r.limit {
		r.over = true
		return n, errUploadTooLarge
	}
	return n, err
}

// exceeded 报告是否因超限中断。
func (r *uploadReader) exceeded() bool { return r != nil && r.over }
