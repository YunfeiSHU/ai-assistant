package biz

import (
	"context"
	"time"
)

// 幂等（docs/02-§7）。
//
// 契约要求的接口：`POST /conversations`（24h 内同键返回同一会话）与
// `POST /conversations/{id}/messages`（防「重试导致重复提问」）。
//
// 为什么把存储抽象成一个窄接口而不是让中间件直接写表：幂等回放必须
// 在**业务动作之前**发生，因此它天然属于请求链路（server 层）；
// 但它落库的语义（唯一键、过期、并发冲突）属于业务，接口定义在 biz 里，
// 由 data 给出实现（规范 §六）。

// IdempotencyTTL 是幂等记录的保留期（docs/02-§7：24 小时）。
const IdempotencyTTL = 24 * time.Hour

// IdempotencyKey 是幂等记录的业务唯一键：`(user_id, method, path, idem_key)`。
//
// `Path` 用**路由模板**（`/api/v1/conversations/:conversation_id/messages`）
// 而不是具体路径。理由是「同一个键被复用到另一个会话」这件事本身就应该
// 被识别出来（返回 409 而不是悄悄新建一条消息），而具体路径会让它
// 变成两条互不相关的记录，看不出误用。
type IdempotencyKey struct {
	UserID string
	Method string
	Path   string
	Key    string
}

// IdempotentResponse 是被持久化的响应快照。
//
// 存的是**已序列化的字节**而不是领域对象：这样成功与失败
// （错误信封）能用同一套机制回放，不需要让 biz 认识响应信封的构造过程。
type IdempotentResponse struct {
	StatusCode int
	Body       []byte
	// RequestHash 是首次请求体的 sha256 指纹。
	//
	// 同一个键配不同的请求体是**误用**：如果直接回放，调用方会拿到
	// 一个与他这次请求无关的结果，而且看起来一切正常。所以必须能比对。
	RequestHash string
	CreatedAt   time.Time
	ExpiresAt   time.Time
}

// IdempotencyStore 是幂等记录的仓储接口（实现见 internal/data/idem.go）。
type IdempotencyStore interface {
	// Recall 查找已有记录；第二个返回值表示是否命中（未命中不是错误）。
	//
	// 过期记录 MUST 视为未命中：过期即应删除，在删除之前读到它
	// 会让「24 小时」这个承诺变成「只要没清理就一直回放」。
	Recall(ctx context.Context, key IdempotencyKey) (*IdempotentResponse, bool, error)
	// Remember 写入一条记录。
	//
	// 并发下同键写入必有一个撞唯一键，此时 MUST 返回 ErrIdemRace：
	// 调用方要做的动作（重新 Recall 一次，把先到者的响应当结果）
	// 与普通写入失败完全不同。
	Remember(ctx context.Context, key IdempotencyKey, resp *IdempotentResponse) error
}
