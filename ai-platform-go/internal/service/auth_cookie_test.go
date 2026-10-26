package service

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

func newCookieTestHandler() *AuthHandler {
	return NewAuthHandler(biz.NewAuthService(biz.AuthDeps{RefreshTTL: time.Hour}))
}

func cookieFromRecorder(t *testing.T, recorder *httptest.ResponseRecorder) *http.Cookie {
	t.Helper()
	cookies := recorder.Result().Cookies()
	if len(cookies) == 0 {
		t.Fatal("没有 Set-Cookie")
	}
	return cookies[0]
}

func TestRefreshCookieIsHttpOnlyAndScoped(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", nil)

	newCookieTestHandler().setRefreshCookie(c, "rt_test")

	cookie := cookieFromRecorder(t, recorder)
	if cookie.Name != refreshCookieName || cookie.Value != "rt_test" {
		t.Fatalf("cookie name/value 不符: %#v", cookie)
	}
	if !cookie.HttpOnly {
		t.Fatal("refresh cookie 必须 HttpOnly")
	}
	if cookie.Path != refreshCookiePath {
		t.Fatalf("cookie path = %q, want %q", cookie.Path, refreshCookiePath)
	}
	if cookie.SameSite != http.SameSiteLaxMode {
		t.Fatalf("cookie SameSite = %v, want Lax", cookie.SameSite)
	}
	if cookie.Secure {
		t.Fatal("本地 HTTP 不应设置 Secure，否则浏览器不会回传")
	}
	if cookie.MaxAge <= 0 {
		t.Fatalf("cookie MaxAge = %d, want >0", cookie.MaxAge)
	}
}

func TestRefreshCookieSecureBehindTLSProxy(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/v1/auth/login", nil)
	c.Request.Header.Set("X-Forwarded-Proto", "https")

	newCookieTestHandler().setRefreshCookie(c, "rt_test")

	if !cookieFromRecorder(t, recorder).Secure {
		t.Fatal("X-Forwarded-Proto=https 时必须设置 Secure")
	}
}

func TestClearRefreshCookieExpiresIt(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/v1/auth/logout", nil)

	newCookieTestHandler().clearRefreshCookie(c)

	cookie := cookieFromRecorder(t, recorder)
	if cookie.MaxAge >= 0 {
		t.Fatalf("clear cookie MaxAge = %d, want <0", cookie.MaxAge)
	}
	if !cookie.HttpOnly {
		t.Fatal("clear cookie 仍必须 HttpOnly")
	}
}
