package otelx

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
)

// UserIDHashPrefixLen 是哈希后保留的十六进制字符数（docs/06-§5.1）。
//
// 16 位 hex = 64 bit：碰撞概率在任何现实用户量下可忽略，
// 而它足够短，可以直接当标签打点、写进 span 属性、贴进工单。
const UserIDHashPrefixLen = 16

// UserIDHash 把 user_id 变成不可逆的稳定标识（HMAC-SHA256 前 16 位 hex）。
//
// 为什么不是裸 SHA-256：`user_id` 是可枚举的（`usr_` + ULID 或自增都能猜），
// 裸哈希等于「给定一个哈希值，遍历所有 user_id 就能确认是谁」——
// 加了盐（`PII_HASH_SALT`）之后，攻击者还必须先拿到盐。
//
// 为什么不是随机盐：那样每次进程重启都会得到不同的哈希，
// 「同一个用户在两条 span 上能不能关联起来」就没了 —— 而这个能力
// 正是 `user_id_hash` 存在的唯一理由（docs/06 禁止直接打 user_id）。
//
// **salt 为空时返回空串**，调用方据此**不打这个属性**：
// 空密钥的 HMAC 退化成裸 SHA-256，给出「看起来已脱敏、其实可反查」的
// 假安全感，比直接缺字段更危险。生产环境由 `conf.Validate` 强制要求
// `PII_HASH_SALT`（docs/06-§7 的 AC-NFR-10）。
func UserIDHash(salt, userID string) string {
	if salt == "" || userID == "" {
		return ""
	}
	mac := hmac.New(sha256.New, []byte(salt))
	// 加分隔前缀：避免「salt 后缀 + userID」与「salt + userID 后缀」
	// 这类拼接歧义在密钥轮换后产生相同输入。
	_, _ = mac.Write([]byte("gw-user-id\x00"))
	_, _ = mac.Write([]byte(userID))
	sum := mac.Sum(nil)
	return hex.EncodeToString(sum)[:UserIDHashPrefixLen]
}
