package middleware

import (
	"errors"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// Auth 中间件：校验 Bearer Token 并把 user_id 注入上下文。
//
// 校验顺序严格按 docs/02-§3.3：头格式 → 验签（固定 HS256）→ iss → aud → exp/nbf
// → token_type → ver。前六步在 jwtx.Signer.Parse 里，第七步由 biz.TokenVersionChecker 完成。
// 校验器接口定义在 biz 而不是本包：它是一条安全规则，中间件只是调用方（依赖方向 server → biz）。
//
// 失败一律 401 且不泄漏具体失败原因；但 `error.code` 会区分 TOKEN_EXPIRED 与 UNAUTHENTICATED ——
// 前者是客户端触发静默刷新的唯一依据。
func Auth(signer *jwtx.Signer, checker biz.TokenVersionChecker, metrics biz.Metrics) gin.HandlerFunc {
	metrics = biz.OrNoop(metrics)
	return func(c *gin.Context) {
		// `auth.verify` 是链路图上「请求进了网关之后第一件真事」。它的主要意义：
		// 客户端报「偶发 401」时，能在 Jaeger 里直接看到那几秒里 ver 缓存（Redis）的耗时。
		ctx, span := otelx.Tracer("gateway.auth").Start(c.Request.Context(), "auth.verify")
		c.Request = c.Request.WithContext(ctx)
		defer func() { span.End() }()

		token, err := bearerToken(c)
		if err != nil {
			metrics.AuthFailure(authFailureReason(err))
			httpx.Fail(c, err)
			return
		}

		claims, err := signer.Parse(token, time.Now())
		if err != nil {
			metrics.AuthFailure(authFailureReason(err))
			httpx.Fail(c, err)
			return
		}

		if checker != nil {
			if err := checker.CheckTokenVersion(c.Request.Context(), claims.Subject, claims.Version); err != nil {
				// `ver` 不匹配单独一档：它是「改了密码/登出」之后旧令牌被拒，
				// 与「令牌伪造/过期」的告警对象完全不同。
				metrics.AuthFailure("token_version")
				httpx.Fail(c, err)
				return
			}
		}

		SetUserID(c, claims.Subject)
		SetClaims(c, claims)
		// 验签通过后把原始 token 存下来，下游调 ai-platform 时原样转交（接缝 J1）。
		// 放在验签之后而不是提取之后就存，让「认证失败的请求不会让下游拿到 token」在代码顺序上成立。
		SetRawToken(c, token)
		// `enduser.id_hash` 不在这里写：盐在装配层，本中间件拿不到；统一由 `Trace`
		// 中间件在请求收尾时写（那时 UserID 肯定已经有了）。同样的属性写两遍
		// 就会出现「一个用盐、一个不用」的静默差异，而 span 属性写错没有任何症状。
		//
		// 把 user_id 补进日志上下文：鉴权之后的日志才会带上「是谁的请求」，
		// 否则排障要回去翻 access log。
		c.Request = c.Request.WithContext(logx.NewContext(c.Request.Context(), logx.Fields{
			UserID: claims.Subject,
		}))
		c.Next()
	}
}

// authFailureReason 把 401 压成有限几档（`gw_auth_failures_total{reason}`）。
// 直接拿错误码做标签只有 2~3 个值，而真正要区分的是「没带头」（客户端 bug）/
// 「令牌过期」（正常流程）/「验签失败」（可能是篡改）—— 区分不了时，面板上一条小幅度
// 上扬的曲线无法告诉你该找谁。
// 取不到已知原因时统一归 `other`：标签基数不能由外部输入决定。
func authFailureReason(err error) string {
	var appErr *errs.AppError
	if errors.As(err, &appErr) {
		if reason, ok := appErr.Details()["reason"].(string); ok && reason != "" {
			return reason
		}
		switch appErr.Code() {
		case errs.CodeTokenExpired:
			return "token_expired"
		case errs.CodeUnauthenticated:
			return "unauthenticated"
		}
	}
	return "other"
}

// OptionalAuth 中间件：有令牌就解析，没有也放行。
// 目前没有接口需要它，保留是为了将来加公开只读接口时不必复制一遍鉴权逻辑
// （复制出来的那份往往少校验一步）。
func OptionalAuth(signer *jwtx.Signer) gin.HandlerFunc {
	return func(c *gin.Context) {
		token, err := bearerToken(c)
		if err != nil {
			c.Next()
			return
		}
		claims, err := signer.Parse(token, time.Now())
		if err != nil {
			// 令牌坏了但接口允许匿名：不报错，也不注入身份。
			c.Next()
			return
		}
		SetUserID(c, claims.Subject)
		SetClaims(c, claims)
		SetRawToken(c, token)
		c.Next()
	}
}

func bearerToken(c *gin.Context) (string, error) {
	raw := c.GetHeader("Authorization")
	if raw == "" {
		return "", errs.New(errs.CodeUnauthenticated).WithDetail("reason", "missing_authorization")
	}
	const prefix = "bearer "
	if len(raw) <= len(prefix) || !strings.EqualFold(raw[:len(prefix)], prefix) {
		// 契约要求 `Bearer ` 前缀；大小写不敏感（RFC 7235 的 scheme 不区分大小写）。
		return "", errs.New(errs.CodeUnauthenticated).WithDetail("reason", "invalid_authorization_scheme")
	}
	token := strings.TrimSpace(raw[len(prefix):])
	if token == "" {
		return "", errs.New(errs.CodeUnauthenticated).WithDetail("reason", "empty_token")
	}
	return token, nil
}
