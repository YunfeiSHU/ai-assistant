package service

import (
	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// UserHandler 处理 `/me*`。
type UserHandler struct{ svc *biz.UserService }

// NewUserHandler 构造 handler。
func NewUserHandler(svc *biz.UserService) *UserHandler { return &UserHandler{svc: svc} }

// Me 处理 `GET /me`（REQ-AUTH-005）。
func (h *UserHandler) Me(c *gin.Context) {
	user, err := h.svc.Me(c.Request.Context(), middleware.UserID(c))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToUserResponse(user))
}

// UpdateMe 处理 `PATCH /me`。
func (h *UserHandler) UpdateMe(c *gin.Context) {
	var in biz.UpdateProfileInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	user, err := h.svc.UpdateProfile(c.Request.Context(), middleware.UserID(c), in)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToUserResponse(user))
}
