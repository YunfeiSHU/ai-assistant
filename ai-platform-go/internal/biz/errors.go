package biz

import "errors"

// 领域哨兵错误。
//
// 定义在 biz 而不是 data，是 代码生成规范.md §四/§六 的直接结果：
// 依赖方向是 `data → biz`，所以「记录不存在」「邮箱被占用」这类**业务语义**
// 必须由 biz 命名，由 data 在实现仓储时翻译成它。
//
// 反过来说，如果这些哨兵留在 data，调用方就得 `errors.Is(err, data.ErrNoRows)`，
// 依赖箭头立刻反向；而一旦反向，biz 单测就必须拉起一个真实仓储。
//
// 翻译点只有一个：data.translate / data 各仓储里的 IsDuplicate 判断。
var (
	// ErrNotFound 表示目标记录不存在（含「存在但不属于你」——刻意不区分）。
	ErrNotFound = errors.New("biz: 记录不存在")

	// ErrEmailTaken 表示邮箱已被注册，**包括被软删用户占用的邮箱**。
	//
	// 之所以强调软删：`uk_user_email` 唯一键不含 deleted_at，
	// 已注销用户的邮箱永久占用（REQ-DATA-009）。
	ErrEmailTaken = errors.New("biz: 邮箱已被占用")

	// ErrTokenInvalid 表示 refresh token 不存在 / 已作废 / 已过期。
	//
	// 三种情形共用一个错误是刻意的：契约只定义 `INVALID_REFRESH_TOKEN`，
	// 区分它们等于给攻击者一个「这个令牌存在过」的探针。
	ErrTokenInvalid = errors.New("biz: 刷新令牌无效")

	// ErrConversationArchived 表示向已归档会话追加消息。
	//
	// 它与 ErrNotFound 必须分开：两者都表现为「0 行受影响」，
	// 但一个是 404（会话不可见），一个是 409（会话可见但不可写）。
	// 合成一个会让「归档后发消息」返回 404，客户端会以为会话被删了。
	ErrConversationArchived = errors.New("biz: 会话已归档")

	// ErrConversationDeleted 表示会话已被软删。
	//
	// 与 ErrNotFound 分开的理由只有一条：`AllocSeq` 的 UPDATE 里
	// `deleted_at IS NULL` 与 `id/user_id` 不匹配都会导致 0 行，
	// 用同一个错误把两种原因吞掉是安全的（对外都是 404），
	// 保留它单纯是为了日志能看出真实原因。
	ErrConversationDeleted = errors.New("biz: 会话已删除")

	// ErrIdemRace 表示幂等键在写入瞬间被另一个请求抢先落库。
	//
	// 它不是业务错误而是**并发信号**：调用方应当重新读一次，
	// 把先到者的响应当成结果返回，而不是给客户端报 500。
	ErrIdemRace = errors.New("biz: 幂等键并发写入")
)
