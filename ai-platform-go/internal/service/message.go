package service

import (
	"bytes"
	"encoding/json"
	"strconv"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// MessageHandler 处理 `/conversations/{id}/messages` 与 `/messages/{id}`（docs/03-§4）。
type MessageHandler struct {
	svc   *biz.MessageService
	proxy *biz.AIProxyService
}

// NewMessageHandler 构造 handler；proxy 可为空（只有删除消息的上下文同步需要）。
func NewMessageHandler(svc *biz.MessageService, proxy ...*biz.AIProxyService) *MessageHandler {
	h := &MessageHandler{svc: svc}
	if len(proxy) > 0 {
		h.proxy = proxy[0]
	}
	return h
}

// Send 处理 `POST /conversations/{conversation_id}/messages`（200）。
// 编排未接线时用户消息已经落库，接口返回 503 `AI_UNAVAILABLE`
// （`details.reason=orchestrator_not_configured`）—— 这是 docs/03-§5 的落库顺序
// （先写 user 消息再调 AI）在 AI 不可用下的表现。
// 重复提交由 `Idempotency-Key` 挡住（docs/02-§7），包括这一次 503：重试不会多出一条提问。
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
	afterSeq := 0
	if raw := c.Query("after_seq"); raw != "" {
		parsed, parseErr := strconv.Atoi(raw)
		if parseErr != nil || parsed < 0 {
			httpx.Fail(c, errs.InvalidArgument([]errs.FieldError{{
				Field: "after_seq", Reason: "invalid_value", Message: "after_seq 必须是非负整数",
			}}))
			return
		}
		afterSeq = parsed
	}
	in := biz.ListMessagesInput{
		PaginationInput: page,
		Order:           orderFrom(c),
		AfterSeq:        afterSeq,
	}
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
//
// B-04：先同步 AI 上下文再删台账；上下文删除失败时保留台账，避免「台账没了、模型还记得」。
func (h *MessageHandler) Delete(c *gin.Context) {
	msg, err := h.svc.Get(c.Request.Context(), middleware.UserID(c), c.Param("message_id"))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	if h.proxy != nil {
		if err := h.deleteContextMessage(c, msg.ConversationID, msg.ID, msg.Role, msg.Content); err != nil {
			httpx.Fail(c, err)
			return
		}
	}
	if err := h.svc.Delete(c.Request.Context(), middleware.UserID(c), c.Param("message_id")); err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.NoContent(c)
}

// Regenerate 处理 `POST /messages/{message_id}/regenerate`（B-03）。
// 复用上一条 user 消息，只写新的 assistant；旧回答在新回答落库后删除。
func (h *MessageHandler) Regenerate(c *gin.Context) {
	var in biz.SendMessageInput
	if c.Request.ContentLength > 0 {
		if err := httpx.BindJSON(c, &in); err != nil {
			httpx.Fail(c, err)
			return
		}
	}
	assistant, err := h.svc.Get(c.Request.Context(), middleware.UserID(c), c.Param("message_id"))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	if h.proxy != nil {
		// 让 AI 在生成时不把旧回答再喂给模型；内容匹配兜住两边 message_id 不一致。
		if err := h.deleteContextMessage(
			c, assistant.ConversationID, assistant.ID, assistant.Role, assistant.Content,
		); err != nil {
			httpx.Fail(c, err)
			return
		}
	}
	result, err := h.svc.Regenerate(
		c.Request.Context(), middleware.UserID(c), assistant.ID, in, metaFrom(c),
	)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	if h.proxy != nil {
		// use_memory=true 时 Python 会把重新生成当成新回合，落一条重复的 user；
		// 只删最后一条同内容 user，保留原始提问。
		if err := h.deleteContextMessage(
			c, assistant.ConversationID, "", "user", result.User.Content,
		); err != nil {
			_ = c.Error(err)
		}
	}
	httpx.OK(c, ToSendMessageResponse(result))
}

// deleteContextMessage 调 AI 内部接口删除上下文消息；proxy 未接线时跳过。
func (h *MessageHandler) deleteContextMessage(
	c *gin.Context, conversationID, messageID, role, content string,
) error {
	if h.proxy == nil {
		return nil
	}
	payload, err := json.Marshal(map[string]string{
		"message_id": messageID,
		"role":       role,
		"content":    content,
	})
	if err != nil {
		return err
	}
	resp, err := h.proxy.Forward(c.Request.Context(), middleware.UserID(c), biz.ProxyInput{
		Method:        "POST",
		Path:          "/api/v1/conversations/" + conversationID + "/context/messages/delete",
		Body:          bytes.NewReader(payload),
		ContentLength: int64(len(payload)),
		ContentType:   "application/json",
		UserToken:     middleware.RawToken(c),
		TraceID:       middleware.TraceID(c),
		Class:         biz.AIProxyTimeoutMeta,
	})
	if err != nil {
		return err
	}
	if resp.Status >= 500 {
		return errs.New(errs.CodeAIUnavailable).
			WithDetail("reason", "context_sync_failed").
			WithDetail("upstream_status", resp.Status)
	}
	return nil
}
