package service

import (
	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// ConversationHandler 处理 `/conversations*`（docs/03-§2）。
// 本层只做四件事：绑参数 → 调 biz → 映射错误 → 写响应。
// 「校验 / 归属 / 状态机」全在 biz：同一套规则（M3 起）还要给 gRPC 入口用。
type ConversationHandler struct{ svc *biz.ConversationService }

// NewConversationHandler 构造 handler。
func NewConversationHandler(svc *biz.ConversationService) *ConversationHandler {
	return &ConversationHandler{svc: svc}
}

// Create 处理 `POST /conversations`（201）。
// `Idempotency-Key` 的回放由中间件完成（server.Idempotency），它会原样写回第一次的 201 响应体，
// 因此这里看到的永远是「第一次执行」。
func (h *ConversationHandler) Create(c *gin.Context) {
	var in biz.CreateConversationInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	conv, err := h.svc.Create(c.Request.Context(), middleware.UserID(c), in)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.Created(c, ToConversationResponse(conv), conversationLocation(conv.ID))
}

// List 处理 `GET /conversations`（200）。
func (h *ConversationHandler) List(c *gin.Context) {
	page, fields := paginationFrom(c)
	pinned, pinnedFields := optionalBoolFrom(c, "pinned")
	fields = append(fields, pinnedFields...)
	if len(fields) > 0 {
		httpx.Fail(c, errs.InvalidArgument(fields))
		return
	}

	in := biz.ListConversationsInput{
		PaginationInput: page,
		Status:          c.Query("status"),
		Pinned:          pinned,
		Keyword:         c.Query("keyword"),
	}
	out, err := h.svc.List(c.Request.Context(), middleware.UserID(c), in)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, NewPage(ToConversationResponses(out.Items), out.NextCursor, out.HasMore))
}

// Get 处理 `GET /conversations/{conversation_id}`（200）。
func (h *ConversationHandler) Get(c *gin.Context) {
	conv, err := h.svc.Get(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id"))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToConversationResponse(conv))
}

// Update 处理 `PATCH /conversations/{conversation_id}`（200）。
func (h *ConversationHandler) Update(c *gin.Context) {
	var in biz.UpdateConversationInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	conv, err := h.svc.Update(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id"), in)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToConversationResponse(conv))
}

// Archive 处理 `POST /conversations/{conversation_id}/archive`（200）。
func (h *ConversationHandler) Archive(c *gin.Context) { h.setArchived(c, true) }

// Unarchive 处理 `POST /conversations/{conversation_id}/unarchive`（200）。
func (h *ConversationHandler) Unarchive(c *gin.Context) { h.setArchived(c, false) }

func (h *ConversationHandler) setArchived(c *gin.Context, archived bool) {
	conv, err := h.svc.SetArchived(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id"), archived)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToConversationResponse(conv))
}

// Delete 处理 `DELETE /conversations/{conversation_id}`（204）。
func (h *ConversationHandler) Delete(c *gin.Context) {
	if err := h.svc.Delete(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id")); err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.NoContent(c)
}

// conversationLocation 构造新建会话的 Location 头（docs/02-§2.1 SHOULD）。
func conversationLocation(id string) string { return "/api/v1/conversations/" + id }
