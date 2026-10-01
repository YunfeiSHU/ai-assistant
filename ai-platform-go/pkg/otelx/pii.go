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
// 必须是加盐 HMAC 而不是裸 SHA-256：`user_id` 可枚举，裸哈希等于「遍历一遍就能
// 确认是谁」；也必须是固定盐而不是随机盐，否则每次重启哈希都变，
// 「同一用户在两条 span 上能否关联」就没了 —— 而这正是 user_id_hash 的唯一理由。
//
// salt 为空时返回空串，调用方据此不打这个属性：空密钥的 HMAC 退化成裸 SHA-256，
// 给出「看起来已脱敏、其实可反查」的假安全感。生产由 `conf.Validate` 强制要求
// `PII_HASH_SALT`（docs/06-§7 的 AC-NFR-10）。
func UserIDHash(salt, userID string) string {
	if salt == "" || userID == "" {
		return ""
	}
	mac := hmac.New(sha256.New, []byte(salt))
	// 加分隔前缀：避免「salt 后缀 + userID」与「salt + userID 后缀」这类
	// 拼接歧义在密钥轮换后产生相同输入。
	_, _ = mac.Write([]byte("gw-user-id\x00"))
	_, _ = mac.Write([]byte(userID))
	sum := mac.Sum(nil)
	return hex.EncodeToString(sum)[:UserIDHashPrefixLen]
}
