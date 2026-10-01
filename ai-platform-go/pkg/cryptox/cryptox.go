// Package cryptox 提供密码哈希、令牌生成与常量时间比较。
//
// 契约（docs/06-§4.1）：密码用 Argon2id（m=64MB,t=3,p=4）；对**不存在的用户**也必须执行一次等价耗时的哈希校验（防用户枚举的时序侧信道）；
// Refresh Token 是 32 字节随机串，服务端只存 sha256。
package cryptox

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/crypto/argon2"
)

// ErrInvalidHash 表示编码串不是合法的 argon2id 编码。
var ErrInvalidHash = errors.New("cryptox: 不是合法的 argon2id 编码串")

// ErrIncompatibleVersion 表示哈希由更新版本的 argon2 生成。
var ErrIncompatibleVersion = errors.New("cryptox: argon2 版本不兼容")

// Argon2Params 是 Argon2id 的成本参数（可由环境变量覆盖）。
type Argon2Params struct {
	// Memory 单位 KiB（64MB = 65536）。
	Memory uint32
	// Iterations 是时间成本（遍历次数）。
	Iterations uint32
	// Parallelism 是并行度（线程数）。
	Parallelism uint8
	// SaltLen 是盐长度（字节）。
	SaltLen uint32
	// KeyLen 是派生出密钥的长度（字节）。
	KeyLen uint32
}

// DefaultArgon2Params 是契约规定的默认参数。
func DefaultArgon2Params() Argon2Params {
	return Argon2Params{Memory: 64 * 1024, Iterations: 3, Parallelism: 4, SaltLen: 16, KeyLen: 32}
}

// HashPassword 用 Argon2id 派生并返回标准编码串。
//
// 输出形如 `$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`，
// 满足 AC-AUTH-01 的 `$argon2id$` 前缀断言。
func HashPassword(password string, p Argon2Params) (string, error) {
	salt := make([]byte, p.SaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("cryptox: 生成盐失败: %w", err)
	}
	key := argon2.IDKey([]byte(password), salt, p.Iterations, p.Memory, p.Parallelism, p.KeyLen)
	return fmt.Sprintf(
		"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version,
		p.Memory,
		p.Iterations,
		p.Parallelism,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(key),
	), nil
}

// VerifyPassword 用**常量时间**比较明文与编码串。
//
// 返回 (false, nil) 表示密码不匹配；(false, err) 表示编码串本身非法。
func VerifyPassword(password, encoded string) (bool, error) {
	p, salt, want, err := parseArgon2id(encoded)
	if err != nil {
		return false, err
	}
	got := argon2.IDKey([]byte(password), salt, p.Iterations, p.Memory, p.Parallelism, uint32(len(want)))
	return subtle.ConstantTimeCompare(got, want) == 1, nil
}

// NeedsRehash 报告既有编码串的成本参数是否低于当前配置（登录成功时顺手升级）。
func NeedsRehash(encoded string, params Argon2Params) bool {
	p, _, _, err := parseArgon2id(encoded)
	if err != nil {
		return true
	}
	return p.Memory < params.Memory || p.Iterations < params.Iterations || p.Parallelism < params.Parallelism
}

var (
	dummyOnce    sync.Once
	dummyEncoded string
)

// DummyEncoded 返回一个固定的合法 argon2id 编码串，用于「用户不存在」时执行等价耗时的校验（AC-NFR-06 防时序枚举）。
//
// 它对应的明文随机生成且不对外可见，Verify 恒为 false；关键是**成本参数必须与真实哈希一致**，两条路径的耗时才同量级。
func DummyEncoded() string {
	dummyOnce.Do(func() {
		// 用一个确定的盐，避免每次进程启动结果不同（便于测试断言稳定）。
		salt := []byte("ai-platform-gw0")
		key := argon2.IDKey([]byte("dummy-password-never-matches"), salt, 3, 64*1024, 4, 32)
		dummyEncoded = fmt.Sprintf(
			"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
			argon2.Version, 64*1024, 3, 4,
			base64.RawStdEncoding.EncodeToString(salt),
			base64.RawStdEncoding.EncodeToString(key),
		)
	})
	return dummyEncoded
}

// BurnPasswordHash 执行一次与真实校验等价的 Argon2id 计算（防时序枚举）。
//
// 不关心结果，只消耗与 VerifyPassword 相同的 CPU/内存。
func BurnPasswordHash(password string) {
	_, _ = VerifyPassword(password, DummyEncoded())
}

func parseArgon2id(encoded string) (Argon2Params, []byte, []byte, error) {
	parts := strings.Split(encoded, "$")
	// 形如 ["", "argon2id", "v=19", "m=65536,t=3,p=4", salt, hash]
	if len(parts) != 6 || parts[0] != "" || parts[1] != "argon2id" {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}
	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}
	if version != argon2.Version {
		return Argon2Params{}, nil, nil, ErrIncompatibleVersion
	}

	var p Argon2Params
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &p.Memory, &p.Iterations, &p.Parallelism); err != nil {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}

	salt, err := base64.RawStdEncoding.Strict().DecodeString(parts[4])
	if err != nil {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}
	key, err := base64.RawStdEncoding.Strict().DecodeString(parts[5])
	if err != nil {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}
	if len(key) == 0 {
		return Argon2Params{}, nil, nil, ErrInvalidHash
	}
	p.SaltLen = uint32(len(salt))
	p.KeyLen = uint32(len(key))
	return p, salt, key, nil
}

// NewOpaqueToken 生成 n 字节随机令牌的 base64url（无填充）表示。
//
// Refresh Token 用它：32 字节 → 43 个字符，无 `+`/`/`/`=`，
// 放进 URL 或 JSON 都不需要转义。
func NewOpaqueToken(nBytes int) (string, error) {
	buf := make([]byte, nBytes)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("cryptox: 生成令牌失败: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

// SHA256Hex 返回 s 的 sha256 十六进制小写表示（64 位）。
//
// refresh_token.token_hash 用它与 CHAR(64) 对齐（AC-DATA-03）。
func SHA256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// RandomHex 返回 n 字节随机数据的十六进制表示（用于内部服务令牌等）。
func RandomHex(nBytes int) (string, error) {
	buf := make([]byte, nBytes)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("cryptox: 生成随机值失败: %w", err)
	}
	return hex.EncodeToString(buf), nil
}

// NormalizeEmail 统一邮箱大小写与空白（契约：小写化后比对）。
func NormalizeEmail(email string) string {
	return strings.ToLower(strings.TrimSpace(email))
}

// MaskEmail 按 docs/06-§4.4 把邮箱脱敏成 `a***@b.com` 形式。
func MaskEmail(email string) string {
	at := strings.LastIndex(email, "@")
	if at <= 0 {
		return "***"
	}
	local, domain := email[:at], email[at+1:]
	if len(local) <= 1 {
		return "*@" + domain
	}
	return local[:1] + "***@" + domain
}

// IsHex64 报告 s 是否是 64 位十六进制（校验 token_hash 列的取值形状）。
func IsHex64(s string) bool {
	if len(s) != 64 {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// ParseKeyLen 是给配置层用的小工具：把字符串解析成正整数，失败返回 def。
func ParseKeyLen(s string, def uint32) uint32 {
	v, err := strconv.ParseUint(strings.TrimSpace(s), 10, 32)
	if err != nil || v == 0 {
		return def
	}
	return uint32(v)
}
