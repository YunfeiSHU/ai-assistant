// Package cursor 编解码游标分页的 cursor 值。
//
// 契约（docs/02-§1）：列表接口统一用游标分页（`items` / `next_cursor` / `has_more`）。
// 游标对客户端是不透明的（base64url(JSON)），客户端不应解析它。
package cursor

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
)

// ErrInvalid 表示游标不是本系统签发的合法值。
// 调用方 MUST 映射成 400 INVALID_ARGUMENT —— 悄悄回退到第一页会让客户端
// 陷入「无限翻页」或「内容重复」，比直接报错更难排查。
var ErrInvalid = errors.New("cursor: 游标不合法")

// Encode 把载荷编码成不透明串。
func Encode(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", fmt.Errorf("cursor: 编码失败: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// Decode 解析游标；同时兼容 URL-safe 与标准 base64（有无填充均可）。
// 宽容解码是有意的：游标会被客户端原样存进 URL 再取出，中间环节可能归一化 `-`/`_`。
func Decode(s string, out any) error {
	if s == "" {
		return fmt.Errorf("%w: 空串", ErrInvalid)
	}
	raw, err := decodeAny(s)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	return nil
}

func decodeAny(s string) ([]byte, error) {
	encodings := []*base64.Encoding{
		base64.RawURLEncoding,
		base64.URLEncoding,
		base64.RawStdEncoding,
		base64.StdEncoding,
	}
	var lastErr error
	for _, enc := range encodings {
		raw, err := enc.DecodeString(s)
		if err == nil {
			return raw, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("%w: base64: %v", ErrInvalid, lastErr)
}
