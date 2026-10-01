// Package jwtx 负责 Access Token 的签发与校验。
//
// 这是**接缝 J1（最高优先级）**：网关签发、ai-platform 校验，契约逐字定在 docs/02-§3.2，
// 任何一处不一致（iss/aud/sub/alg/kid 与 payload 字段）都会导致全链路 401。
package jwtx

import (
	"errors"
	"fmt"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 契约常量。
const (
	// Algorithm 是唯一允许的签名算法。校验方 MUST 固定期望算法，
	// 禁止接受 `none` 或按 token 头部自选算法（防 alg confusion）。
	Algorithm = "HS256"
	// TokenTypeAccess 是 access token 的 token_type 取值。
	TokenTypeAccess = "access"
)

// ErrKeyTooShort 表示密钥短于 32 字节。
var ErrKeyTooShort = errors.New("jwtx: JWT_SECRET 必须 ≥ 32 字节")

// Claims 是 Access Token 的 payload。
//
// 这里不用 jwt.RegisteredClaims：它的 `aud` 是 ClaimStrings（数组），而契约要求输出**字符串** `"aud": "ai-platform"`；
// 显式定义字段能让序列化结果完全可控（J1 不容许「差不多」）。
type Claims struct {
	Issuer    string `json:"iss"`
	Audience  string `json:"aud"`
	Subject   string `json:"sub"`
	ExpiresAt int64  `json:"exp"`
	IssuedAt  int64  `json:"iat"`
	NotBefore int64  `json:"nbf"`
	TokenType string `json:"token_type"`
	Version   int    `json:"ver"`
}

// 编译期断言 Claims 满足 jwt.Claims 接口。
var _ jwt.Claims = (*Claims)(nil)

// GetExpirationTime 实现 jwt.Claims。
func (c *Claims) GetExpirationTime() (*jwt.NumericDate, error) {
	return jwt.NewNumericDate(time.Unix(c.ExpiresAt, 0)), nil
}

// GetIssuedAt 实现 jwt.Claims。
func (c *Claims) GetIssuedAt() (*jwt.NumericDate, error) {
	return jwt.NewNumericDate(time.Unix(c.IssuedAt, 0)), nil
}

// GetNotBefore 实现 jwt.Claims。
func (c *Claims) GetNotBefore() (*jwt.NumericDate, error) {
	return jwt.NewNumericDate(time.Unix(c.NotBefore, 0)), nil
}

// GetIssuer 实现 jwt.Claims。
func (c *Claims) GetIssuer() (string, error) { return c.Issuer, nil }

// GetSubject 实现 jwt.Claims。
func (c *Claims) GetSubject() (string, error) { return c.Subject, nil }

// GetAudience 实现 jwt.Claims。
func (c *Claims) GetAudience() (jwt.ClaimStrings, error) {
	if c.Audience == "" {
		return nil, nil
	}
	return jwt.ClaimStrings{c.Audience}, nil
}

// Config 是签发/校验的静态配置。
type Config struct {
	Secret   string
	Issuer   string
	Audience string
	KID      string
	TTL      time.Duration
	// ClockSkew 是 exp/nbf 的容差（默认 30s，与 AI 侧一致）。
	ClockSkew time.Duration
}

// Signer 按配置签发与校验令牌。
type Signer struct {
	cfg Config
}

// NewSigner 构造 Signer 并做启动期自检。
//
// 密钥短于 32 字节或 TTL 非正 MUST 拒绝启动（REQ-NFR-009）：
// 这类错误若拖到请求期才暴露，表现是「所有令牌都验不过」，很难定位。
func NewSigner(cfg Config) (*Signer, error) {
	if len(cfg.Secret) < 32 {
		return nil, fmt.Errorf("%w（当前 %d 字节）", ErrKeyTooShort, len(cfg.Secret))
	}
	if cfg.TTL <= 0 {
		return nil, errors.New("jwtx: ACCESS_TOKEN_TTL 必须为正")
	}
	if cfg.Issuer == "" || cfg.Audience == "" {
		return nil, errors.New("jwtx: JWT_ISSUER 与 JWT_AUDIENCE 不能为空（接缝 J1）")
	}
	if cfg.KID == "" {
		cfg.KID = "k1"
	}
	if cfg.ClockSkew < 0 {
		cfg.ClockSkew = 0
	}
	return &Signer{cfg: cfg}, nil
}

// Config 返回当前配置的副本（供启动日志与自检输出）。
func (s *Signer) Config() Config { return s.cfg }

// Sign 为用户签发 access token，返回令牌串与过期时刻。
//
// `ver` 是该用户当前的 token_version，改密码/踢下线时递增使旧令牌失效。
func (s *Signer) Sign(userID string, version int, now time.Time) (string, time.Time, error) {
	now = now.UTC().Truncate(time.Second)
	exp := now.Add(s.cfg.TTL)
	claims := &Claims{
		Issuer:    s.cfg.Issuer,
		Audience:  s.cfg.Audience,
		Subject:   userID,
		ExpiresAt: exp.Unix(),
		IssuedAt:  now.Unix(),
		NotBefore: now.Unix(),
		TokenType: TokenTypeAccess,
		Version:   version,
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	token.Header["kid"] = s.cfg.KID
	signed, err := token.SignedString([]byte(s.cfg.Secret))
	if err != nil {
		return "", time.Time{}, fmt.Errorf("jwtx: 签发失败: %w", err)
	}
	return signed, exp, nil
}

// Parse 校验并解析令牌。
//
// 校验顺序与 docs/02-§3.3 一致：算法（Parser 固定）→ iss → aud → exp/nbf（含容差）→ token_type。`ver` 需要查用户状态，由调用方校验。
// 失败一律返回 *errs.AppError：过期给 TOKEN_EXPIRED（客户端据此静默刷新），其余给 UNAUTHENTICATED，且**不泄漏具体原因**（防探测）。
func (s *Signer) Parse(tokenStr string, now time.Time) (*Claims, error) {
	claims := &Claims{}
	parser := jwt.NewParser(
		jwt.WithValidMethods([]string{Algorithm}),
		jwt.WithLeeway(s.cfg.ClockSkew),
		jwt.WithTimeFunc(func() time.Time { return now.UTC() }),
	)
	if _, err := parser.ParseWithClaims(tokenStr, claims, s.keyFunc); err != nil {
		if errors.Is(err, jwt.ErrTokenExpired) {
			return nil, errs.New(errs.CodeTokenExpired).WithCause(err)
		}
		return nil, errs.New(errs.CodeUnauthenticated).WithCause(err)
	}

	if claims.Issuer != s.cfg.Issuer {
		return nil, errs.New(errs.CodeUnauthenticated).WithCause(
			fmt.Errorf("jwtx: iss 不匹配（期望 %q，实际 %q）", s.cfg.Issuer, claims.Issuer))
	}
	if claims.Audience != s.cfg.Audience {
		return nil, errs.New(errs.CodeUnauthenticated).WithCause(
			fmt.Errorf("jwtx: aud 不匹配（期望 %q，实际 %q）", s.cfg.Audience, claims.Audience))
	}
	if claims.TokenType != TokenTypeAccess {
		return nil, errs.New(errs.CodeUnauthenticated).WithCause(
			fmt.Errorf("jwtx: token_type 必须为 %q，实际 %q", TokenTypeAccess, claims.TokenType))
	}
	if claims.Subject == "" {
		return nil, errs.New(errs.CodeUnauthenticated).WithCause(errors.New("jwtx: sub 为空"))
	}
	return claims, nil
}

func (s *Signer) keyFunc(token *jwt.Token) (any, error) {
	// 二次确认算法（Parser 已固定，这里是纵深防御：
	// 万一将来有人改坏了 WithValidMethods，也不会退化成 alg confusion）。
	if token.Method.Alg() != Algorithm {
		return nil, fmt.Errorf("jwtx: 非预期签名算法 %q", token.Method.Alg())
	}
	return []byte(s.cfg.Secret), nil
}
