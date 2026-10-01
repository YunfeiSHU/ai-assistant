package service

import (
	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// AuthHandler 处理 `/auth/*`。
type AuthHandler struct{ svc *biz.AuthService }

// NewAuthHandler 构造 handler。
func NewAuthHandler(svc *biz.AuthService) *AuthHandler { return &AuthHandler{svc: svc} }

// Register 处理 `POST /auth/register`（201）。
func (h *AuthHandler) Register(c *gin.Context) {
	var in biz.RegisterInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	user, err := h.svc.Register(c.Request.Context(), in, metaFrom(c))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	// 不设 Location：用户资源只能通过 `GET /me` 访问，没有 `/users/{id}` 端点，
	// 指向一个 404 地址的 Location 头比没有更糟。
	httpx.Created(c, ToUserResponse(user), "")
}

// Login 处理 `POST /auth/login`（200）。
func (h *AuthHandler) Login(c *gin.Context) {
	var in biz.LoginInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	meta := metaFrom(c)
	// device_name 是请求体字段，但设备信息属于「请求元信息」，
	// 在 biz 层会与 User-Agent 一起入库。
	meta.DeviceName = in.DeviceName

	pair, err := h.svc.Login(c.Request.Context(), in, meta)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToTokenResponse(pair))
}

// Refresh 处理 `POST /auth/refresh`（200）。
func (h *AuthHandler) Refresh(c *gin.Context) {
	var in biz.RefreshInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	pair, err := h.svc.Refresh(c.Request.Context(), in, metaFrom(c))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToTokenResponse(pair))
}

// Logout 处理 `POST /auth/logout`（204）。
func (h *AuthHandler) Logout(c *gin.Context) {
	userID := middleware.UserID(c)
	var in biz.LogoutInput
	// body 可选：带 Content-Length 才解析，避免「空 body」被判成 400。
	if c.Request.ContentLength > 0 {
		if err := httpx.BindJSON(c, &in); err != nil {
			httpx.Fail(c, err)
			return
		}
	}
	if err := h.svc.Logout(c.Request.Context(), userID, in, metaFrom(c)); err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.NoContent(c)
}

// ChangePassword 处理 `POST /auth/password`（204）。
func (h *AuthHandler) ChangePassword(c *gin.Context) {
	userID := middleware.UserID(c)
	var in biz.ChangePasswordInput
	if err := httpx.BindJSON(c, &in); err != nil {
		httpx.Fail(c, err)
		return
	}
	if _, err := h.svc.ChangePassword(c.Request.Context(), userID, in, metaFrom(c)); err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.NoContent(c)
}
