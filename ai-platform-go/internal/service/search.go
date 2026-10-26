package service

import (
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// SearchHandler 处理 `GET /search/messages`（B-10）。
type SearchHandler struct{ repo biz.MessageSearchRepo }

// NewSearchHandler 构造 handler；repo 为 nil 时路由不注册。
func NewSearchHandler(repo biz.MessageSearchRepo) *SearchHandler {
	return &SearchHandler{repo: repo}
}

type messageSearchItem struct {
	ConversationID string `json:"conversation_id"`
	Title          string `json:"title"`
	MessageID      string `json:"message_id"`
	Role           string `json:"role"`
	Snippet        string `json:"snippet"`
	CreatedAt      string `json:"created_at"`
}

// SearchMessages 返回当前用户消息正文命中；空 q 校验失败。
func (h *SearchHandler) SearchMessages(c *gin.Context) {
	query := strings.TrimSpace(c.Query("q"))
	if query == "" {
		httpx.Fail(c, errs.InvalidArgument([]errs.FieldError{{
			Field: "q", Reason: "required", Message: "q 不能为空",
		}}))
		return
	}
	if len([]rune(query)) > 100 {
		httpx.Fail(c, errs.InvalidArgument([]errs.FieldError{{
			Field: "q", Reason: "too_long", Message: "q 不能超过 100 字符",
		}}))
		return
	}
	limit := 20
	if raw := c.Query("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 50 {
			httpx.Fail(c, errs.InvalidArgument([]errs.FieldError{{
				Field: "limit", Reason: "out_of_range", Message: "limit 必须在 1..50 之间",
			}}))
			return
		}
		limit = parsed
	}
	items, err := h.repo.SearchMessages(c.Request.Context(), middleware.UserID(c), query, limit)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	out := make([]messageSearchItem, 0, len(items))
	for _, hit := range items {
		out = append(out, messageSearchItem{
			ConversationID: hit.ConversationID,
			Title:          hit.Title,
			MessageID:      hit.MessageID,
			Role:           hit.Role,
			Snippet:        hit.Snippet,
			CreatedAt:      hit.CreatedAt,
		})
	}
	httpx.OK(c, gin.H{"items": out, "has_more": false})
}
