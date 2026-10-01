package biz

import (
	"context"
	"time"
)

// 幂等（docs/02-§7）。
//
// 涉及 `POST /conversations`（24h 内同键返回同一会话）与
// `POST /conversations/{id}/messages`（防重试导致重复提问）。
// 回放在业务动作之前发生（属于 server 请求链路），但落库语义（唯一键、过期、
// 并发冲突）属于业务，故接口定义在 biz、实现在 data（规范 §六）。

// IdempotencyTTL 是幂等记录的保留期（docs/02-§7：24 小时）。
const IdempotencyTTL = 24 * time.Hour

// IdempotencyKey 是幂等记录的业务唯一键：`(user_id, method, path, idem_key)`。
// Path 用路由模板（`/api/v1/conversations/:conversation_id/messages`）而非具体路径，
// 否则「同键复用到另一会话」会变成两条无关记录，看不出误用（应返回 409）。
type IdempotencyKey struct {
	UserID string
	Method string
	Path   string
	Key    string
}

// IdempotentResponse 是被持久化的响应快照。
// 存已序列化的字节而非领域对象，成功与失败（错误信封）就能用同一套机制回放。
type IdempotentResponse struct {
	StatusCode int
	Body       []byte
	// RequestHash 是首次请求体的 sha256 指纹。
	// 同键配不同请求体是误用：直接回放会让调用方拿到无关结果且看起来正常，故必须比对。
	RequestHash string
	CreatedAt   time.Time
	ExpiresAt   time.Time
}

// IdempotencyStore 是幂等记录的仓储接口（实现见 internal/data/idem.go）。
type IdempotencyStore interface {
	// Recall 查找已有记录；第二个返回值表示是否命中（未命中不是错误）。
	// 过期记录 MUST 视为未命中，否则「24 小时」会变成「只要没清理就一直回放」。
	Recall(ctx context.Context, key IdempotencyKey) (*IdempotentResponse, bool, error)
	// Remember 写入一条记录。
	// 并发同键写入必有一个撞唯一键，此时 MUST 返回 ErrIdemRace ——
	// 调用方要重新 Recall 回放先到者的响应，与普通写入失败处理完全不同。
	Remember(ctx context.Context, key IdempotencyKey, resp *IdempotentResponse) error
}
