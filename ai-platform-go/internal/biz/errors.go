package biz

import "errors"

// 领域哨兵错误。
//
// 定义在 biz 而非 data（规范 §四/§六）：依赖方向是 `data → biz`，业务语义
// 由 biz 命名、data 在仓储实现里翻译成它（唯一翻译点 data.translate / IsDuplicate）。
// 若留在 data，biz 单测就得拉起真实仓储。
var (
	// ErrNotFound 表示目标记录不存在（含「存在但不属于你」——刻意不区分）。
	ErrNotFound = errors.New("biz: 记录不存在")

	// ErrEmailTaken 表示邮箱已被注册，**包括被软删用户占用的邮箱**：
	// uk_user_email 不含 deleted_at，已注销用户的邮箱永久占用（REQ-DATA-009）。
	ErrEmailTaken = errors.New("biz: 邮箱已被占用")

	// ErrTokenInvalid 表示 refresh token 不存在 / 已作废 / 已过期。
	// 三种情形共用一个错误是刻意的：区分它们等于给攻击者「这个令牌存在过」的探针。
	ErrTokenInvalid = errors.New("biz: 刷新令牌无效")

	// ErrConversationArchived 表示向已归档会话追加消息。
	// 与 ErrNotFound 必须分开：两者都是「0 行受影响」，但一个是 404（不可见），
	// 一个是 409（可见但不可写）；合一会让客户端以为会话被删了。
	ErrConversationArchived = errors.New("biz: 会话已归档")

	// ErrConversationDeleted 表示会话已被软删。
	// 对外与 ErrNotFound 同是 404，分开只为日志能看出 AllocSeq 0 行的真实原因。
	ErrConversationDeleted = errors.New("biz: 会话已删除")

	// ErrIdemRace 表示幂等键在写入瞬间被另一个请求抢先落库。
	// 它是并发信号而非业务错误：调用方应重新读一次并回放先到者的响应，而不是报 500。
	ErrIdemRace = errors.New("biz: 幂等键并发写入")
)
