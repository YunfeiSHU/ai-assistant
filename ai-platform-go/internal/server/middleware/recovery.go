package middleware

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"runtime/debug"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
)

// Recovery 中间件：把 panic 转成 500 并记录堆栈。两个细节：
//  1. 必须检查 `c.Writer.Written()`：流式响应已写出部分 SSE 帧时，再写 JSON 信封
//     会污染响应体（客户端会看到「token 帧后面跟着一段 JSON」）。
//  2. 必须重新 panic 掉 `http.ErrAbortHandler`：它是标准库约定的「安静地断开连接」，
//     吞掉会掩盖真实问题。
func Recovery(base *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		defer func() {
			r := recover()
			if r == nil {
				return
			}
			if err, ok := r.(error); ok && errors.Is(err, http.ErrAbortHandler) {
				panic(r)
			}

			log := logx.From(c.Request.Context(), base)
			log.ErrorContext(c.Request.Context(), "http.panic",
				slog.String("route", c.FullPath()),
				slog.String("panic", fmt.Sprint(r)),
				slog.String("stack", string(debug.Stack())),
			)

			if c.Writer.Written() {
				// 响应已经开始，只能中断。
				c.Abort()
				return
			}
			httpx.Fail(c, errs.New(errs.CodeInternalError).WithCause(fmt.Errorf("panic: %v", r)))
		}()
		c.Next()
	}
}
