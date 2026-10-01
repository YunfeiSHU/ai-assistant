package service

import (
	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// MessageHandler 处理 `/conversations/{id}/messages` 与 `/messages/{id}`（docs/03-§4）。
type MessageHandler struct{ svc *biz.MessageService }

// NewMessageHandler 构造 handler。
func NewMessageHandler(svc *biz.MessageService) *MessageHandler {
	return &MessageHandler{svc: svc}
}

// Send 处理 `POST /conversations/{conversation_id}/messages`（200）。
//
// M2 的事实行为：编排未接线（配置里没有 AI 客户端）时，用户消息**已经落库**，
// 接口返回 503 `AI_UNAVAILABLE`（`details.reason=orchestrator_not_configured`）。
// 这正是 docs/03-§5 的落库顺序（先写 user 消息再调 AI）在「AI 不可用」下的表现，
// M3 注入编排实现后同一个接口就会返回 200 + assistant 消息。
//
// 重复提交由 `Idempotency-Key` 挡住（docs/02-§7）—— 包括这一次 503：
// 重试不会在台账里多出一条提问。
func (h *MessageHandler) Send(c *gin.Context) {
	var in biz.SendMessageInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	res, err := h.svc.Send(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id"), in, metaFrom(c))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToSendMessageResponse(res))
}

// List 处理 `GET /conversations/{conversation_id}/messages`（200）。
func (h *MessageHandler) List(c *gin.Context) {
	page, fields := paginationFrom(c)
	if len(fields) > 0 {
		httpx.Fail(c, errs.InvalidArgument(fields))
		return
	}
	in := biz.ListMessagesInput{PaginationInput: page, Order: orderFrom(c)}
	out, err := h.svc.List(c.Request.Context(), middleware.UserID(c), c.Param("conversation_id"), in)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, NewPage(ToMessageResponses(out.Items), out.NextCursor, out.HasMore))
}

// Get 处理 `GET /messages/{message_id}`（200）。
func (h *MessageHandler) Get(c *gin.Context) {
	msg, err := h.svc.Get(c.Request.Context(), middleware.UserID(c), c.Param("message_id"))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToMessageResponse(msg))
}

// Delete 处理 `DELETE /messages/{message_id}`（204）。
func (h *MessageHandler) Delete(c *gin.Context) {
	if err := h.svc.Delete(c.Request.Context(), middleware.UserID(c), c.Param("message_id")); err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.NoContent(c)
}
