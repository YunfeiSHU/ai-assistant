package data

import (
	"context"
	"errors"

	"gorm.io/gorm"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

// idemRepo 是共享表 `idempotency_record` 的仓储（规范 §六）。
// ⚠️ 与 ai-platform 共用（docs/05-§2.7）：唯一键 `uk_idem (user_id, method, path, idem_key)`
// 是双方共识，改窄列定义会直接弄挂 AI 侧的写入。
type idemRepo struct{ data *Data }

// NewIdemRepo 构造幂等仓储。
func NewIdemRepo(d *Data) biz.IdempotencyStore { return &idemRepo{data: d} }

// Recall 按唯一键读取幂等响应快照。
//
// 「没有记录」是正常路径（第一次请求），故第二返回值为 false 而不是 biz.ErrNotFound ——
// 调用方要区分的是「首次请求」与「已经回放过」，折叠成错误会让主流程多一层容易写反的判断。
//
// ⚠️ `response_body` 是 MySQL 原生 `JSON` 列，写入时会被规范化（对象键重排、逗号后补空格），
// 读取时按自身格式重新序列化，因此命中回放是语义等价而非字节相等（docs/02-§7 承诺的是
// 「回放首次响应（含状态码）」，不含格式；数值精度不受影响）。所以不要用「字节相等」断言回放，
// 也不要把非 JSON 的响应体挂到这个中间件下 —— 它连存都存不进去。
func (r *idemRepo) Recall(ctx context.Context, key biz.IdempotencyKey) (*biz.IdempotentResponse, bool, error) {
	var po idempotencyRecordPO
	err := r.data.DB.GORM.WithContext(ctx).
		Where("user_id = ? AND method = ? AND path = ? AND idem_key = ?",
			key.UserID, key.Method, key.Path, key.Key).
		Take(&po).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, false, nil
		}
		return nil, false, err
	}

	// 过期记录必须视为未命中。这里不能只依赖清理任务：
	// 清理是「迟早会跑」的，而「24 小时」是给客户端的承诺 ——
	// 记录晚删一天，行为就多回放一天，且没有任何迹象。
	if !po.ExpiresAt.After(clockx.Now()) {
		return nil, false, nil
	}

	resp := &biz.IdempotentResponse{
		StatusCode:  po.StatusCode,
		RequestHash: po.RequestHash,
		CreatedAt:   po.CreatedAt,
		ExpiresAt:   po.ExpiresAt,
	}
	if po.ResponseBody != nil {
		resp.Body = decodeResponseBody(*po.ResponseBody)
	}
	return resp, true, nil
}

// emptyBodyJSON 是「空响应体」在 `response_body json NOT NULL` 里的表示。
//
// 不能存空串：`response_body` 是 MySQL 原生 `JSON` 列，空串不是合法 JSON 文档，
// 写入直接报 ERROR 3140 Invalid JSON text: "The document is empty."（已实测）——
// 于是 204 这类「有状态码、无响应体」的接口永远记不下幂等，而失败只发生在 `Remember` 里，
// 主流程已经把响应写回客户端：现象是「接口返回正常、重试却每次都重新执行一遍副作用」，无报错可见。
// `null` 是合法 JSON 值且列接受它（`IS NULL` 为 0，即「值为 JSON null」而非 SQL NULL），
// 正好用来区分「没存过 body」与「body 本来就是空的」。
const emptyBodyJSON = "null"

// encodeResponseBody 把响应体转成可以写进 `response_body` 的文本。
func encodeResponseBody(body []byte) string {
	if len(body) == 0 {
		return emptyBodyJSON
	}
	return string(body)
}

// decodeResponseBody 把 `response_body` 的文本还原成响应体字节。
func decodeResponseBody(stored string) []byte {
	if stored == emptyBodyJSON {
		return nil
	}
	return []byte(stored)
}

// Remember 写入幂等响应快照。
// 并发下同键的两个请求必有一个撞唯一键，此时返回 biz.ErrIdemRace：
// 调用方要做的动作（重新 Recall 一次，把先到者的响应当成结果）与普通写入失败完全不同。
func (r *idemRepo) Remember(ctx context.Context, key biz.IdempotencyKey, resp *biz.IdempotentResponse) error {
	po := &idempotencyRecordPO{
		UserID:      key.UserID,
		Method:      key.Method,
		Path:        key.Path,
		IdemKey:     key.Key,
		RequestHash: resp.RequestHash,
		StatusCode:  resp.StatusCode,
		CreatedAt:   resp.CreatedAt,
		ExpiresAt:   resp.ExpiresAt,
	}
	// response_body 用 `*string`：空响应体（204）要显式写成 JSON `null`
	// 而不是空串（后者会被 MySQL 判成非法 JSON，见 emptyBodyJSON）。
	body := encodeResponseBody(resp.Body)
	po.ResponseBody = &body

	err := r.data.DB.GORM.WithContext(ctx).Create(po).Error
	if isDuplicate(err) {
		return biz.ErrIdemRace
	}
	return err
}
