package server

import (
	"bytes"
	"errors"
	"io"
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
)

// contextKeyIdempotency 是幂等键在 gin.Context 里的键名。
const contextKeyIdempotency = "gw.idempotency_key"

// HeaderIdempotencyKey 是幂等键请求头（docs/02-§7）。
const HeaderIdempotencyKey = "Idempotency-Key"

// maxIdempotencyKeyLen 是幂等键的长度上限。
// 与 request id 同理：不限长的值会被拼进 Redis Key 与数据库列，超长值等于给调用方一个撑爆索引的手段。
const maxIdempotencyKeyLen = 128

// IdempotencyKey 中间件：提取 `Idempotency-Key` 头并放进上下文。
// 只做提取而不在非法时拒绝：并非所有接口都支持幂等，在不支持的接口上因为「多传了一个头」
// 而报错不合理。支持幂等的接口 SHOULD 用 IdempotencyKeyOf 读取并自行报错。
func IdempotencyKey() gin.HandlerFunc {
	return func(c *gin.Context) {
		if raw := c.GetHeader(HeaderIdempotencyKey); raw != "" {
			c.Set(contextKeyIdempotency, raw)
		}
		c.Next()
	}
}

// IdempotencyKeyOf 返回请求头里的幂等键（未提供时为空串）。
func IdempotencyKeyOf(c *gin.Context) string {
	if v, ok := c.Get(contextKeyIdempotency); ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// ValidateIdempotencyKey 校验幂等键格式。
// 契约建议用 UUID，但这里只做「长度 + 字符集」的宽松校验：强制 UUID 会让用业务单号
// 做幂等键的客户端无法接入，而幂等的正确性并不依赖键的形状（同一个键映射到同一结果）。
func ValidateIdempotencyKey(key string) error {
	if key == "" {
		return nil
	}
	if len(key) > maxIdempotencyKeyLen {
		return errs.New(errs.CodeInvalidArgument).
			WithDetail("reason", "idempotency_key_too_long").
			WithDetail("max_length", maxIdempotencyKeyLen)
	}
	for _, r := range key {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.', r == ':':
		default:
			return errs.New(errs.CodeInvalidArgument).
				WithDetail("reason", "idempotency_key_invalid_chars")
		}
	}
	return nil
}

// IdempotencyReplayHeader 标记「本次响应来自幂等回放」。
// 契约没有要求这个头，但它让「重试真的走了回放」可观测 —— 否则不生效的实现与
// 生效的实现在响应体上完全一样（这正是幂等最容易被写错又看不出来的地方）。
const IdempotencyReplayHeader = "Idempotency-Replayed"

// Idempotency 中间件：把写接口的响应摘要落进 `idempotency_record`，
// 同一个键再次到达时直接回放（docs/02-§7）。
//
// 做成响应缓冲中间件而不是每个 handler 各写一遍：回放必须包含失败响应 ——
// `POST .../messages` 在 AI 不可用时返回 503，而用户的提问已经落库，不记住这个 503，
// 客户端重试会在台账里多出一条重复提问；缓冲让中间件自己拿到「状态码 + 响应体」这一对。
//
// 回放不是字节相等：快照落在 MySQL 原生 `JSON` 列上，读写时会规范化键序与空白，
// 契约要的「回放首次响应（含状态码）」语义等价即满足（细节见 data.idemRepo.Recall）。
//
// 因此只挂在非流式 JSON 路由上：SSE 没有「结束」这个时刻，缓冲它会破坏实时性与内存占用。
func Idempotency(store biz.IdempotencyStore, log *slog.Logger) gin.HandlerFunc {
	return func(c *gin.Context) {
		key := IdempotencyKeyOf(c)
		if key == "" || store == nil {
			c.Next()
			return
		}
		if err := ValidateIdempotencyKey(key); err != nil {
			httpx.Fail(c, err)
			return
		}

		// 路径用路由模板：具体路径会让「同一个键被复用到另一个会话」
		// 变成两条互不相关的记录，看不出误用（见 biz.IdempotencyKey）。
		route := c.FullPath()
		userID := middleware.UserID(c)
		if route == "" || userID == "" {
			// 拼不出唯一键就不做幂等。身份缺失是鉴权中间件的职责（它会给出 401）。
			c.Next()
			return
		}

		hash, ok := bufferAndHashBody(c)
		if !ok {
			// 读不出请求体（多为超过 MAX_JSON_BODY_MB）：交给下游按正常路径报 413，
			// 不要在这里自己造一个错误码。
			c.Next()
			return
		}

		ctx := c.Request.Context()
		ik := biz.IdempotencyKey{UserID: userID, Method: c.Request.Method, Path: route, Key: key}
		stored, hit, err := store.Recall(ctx, ik)
		if err != nil {
			// 存储不可用不阻断业务（幂等是「更好」不是「前提」），但必须告警 ——
			// 静默降级会让客户端以为重试是安全的。
			logx.From(ctx, log).WarnContext(ctx, "idempotency.recall_failed", slog.String("error", err.Error()))
			c.Next()
			return
		}
		if hit {
			if stored.RequestHash != hash {
				// 同键不同体是误用：直接回放会让调用方拿到与本请求无关的结果，而且看起来一切正常。
				httpx.Fail(c, errs.New(errs.CodeConflict).
					WithMessage("Idempotency-Key 已用于另一个请求体").
					WithDetail("reason", "idempotency_key_reused"))
				return
			}
			replayStored(c, stored)
			return
		}

		buf := &bufferingWriter{ResponseWriter: c.Writer}
		real := buf.ResponseWriter
		c.Writer = buf
		// defer 的还原是给 panic 路径用的，不是给正常路径用的：Recovery 在最外层，
		// 它靠 `httpx.Fail(c, ...)` 写 500（写的是 `c.Writer`）。handler panic 时
		// `c.Next()` 把栈直接掀到 Recovery，下面的还原语句一行都不会执行，于是那个 500
		// 信封被写进这个缓冲且永远不 flush —— 客户端拿到「200 + 空响应体」，比 500 更糟，
		// 而且它还会拿同一个键重试。（panic 500 因此不会进幂等快照：那一刻缓冲里没有它。）
		defer func() { c.Writer = real }()

		c.Next()
		// 还原真实 writer 之后才能落库与写出：AccessLog 在更外层读 c.Writer.Status()，
		// writeBuffered 也必须写到真实连接上。
		c.Writer = real

		now := clockx.Now()
		rememberErr := store.Remember(ctx, ik, &biz.IdempotentResponse{
			StatusCode:  buf.Status(),
			Body:        buf.body.Bytes(),
			RequestHash: hash,
			CreatedAt:   now,
			ExpiresAt:   now.Add(biz.IdempotencyTTL),
		})
		switch {
		case rememberErr == nil:
		case errors.Is(rememberErr, biz.ErrIdemRace):
			// 另一个并发请求先落库了。本次响应照常返回（副作用已发生无法回滚），
			// 但后续同键请求会回放先到者的结果 —— 必须告警，
			// 否则「两个请求得到两个不同的会话」会变成一条无法解释的现象。
			logx.From(ctx, log).WarnContext(ctx, "idempotency.race",
				slog.String("path", route),
				slog.String("hint", "并发同键：已有请求先落库，后续回放先到者的响应"))
		default:
			logx.From(ctx, log).ErrorContext(ctx, "idempotency.remember_failed",
				slog.String("error", rememberErr.Error()))
		}
		writeBuffered(c, buf)
	}
}

// bufferAndHashBody 把请求体读进内存、算出指纹，并把 body 原样放回（下游还要 `BindJSON`）。
// 读取失败（超过 BodyLimit 的 MaxBytesError）时用 MultiReader 把「已读到的部分 + 原始 reader」
// 拼回去：原始 MaxBytesReader 会再次抛出同一个错，下游的 httpx.BindJSON 仍能识别成 413。
// 若保留一个已读废的 body，客户端会得到「body 不完整」的 400，把「请求太大」误导成「JSON 写坏了」。
func bufferAndHashBody(c *gin.Context) (string, bool) {
	if c.Request.Body == nil {
		return cryptox.SHA256Hex(""), true
	}
	orig := c.Request.Body
	raw, err := io.ReadAll(orig)
	if err != nil {
		c.Request.Body = io.NopCloser(io.MultiReader(bytes.NewReader(raw), orig))
		return "", false
	}
	c.Request.Body = io.NopCloser(bytes.NewReader(raw))
	return cryptox.SHA256Hex(string(raw)), true
}

// replayStored 原样写回存下来的响应（docs/02-§7：命中则回放首次响应，含状态码）。
//
// `Content-Type` 必须显式补上：首次响应里它是 gin 的 `c.JSON` 设的，而那是另一个请求的
// header map，回放请求上不存在 —— 让 net/http 嗅探 body 会把 `{"id":...}` 判成 `text/plain`，
// 客户端的 `res.json()` 就在重试路径上突然失败。写死的 JSON 是安全的：能挂幂等的只有非流式 JSON 路由。
// `Location` 等其它响应头不回放：表里没存响应头，契约只要求「响应（含状态码）」一致
// （`id` 在响应体里，客户端可以按它取详情）。
func replayStored(c *gin.Context, resp *biz.IdempotentResponse) {
	c.Header(IdempotencyReplayHeader, "true")
	if len(resp.Body) > 0 {
		c.Header("Content-Type", "application/json; charset=utf-8")
	}
	c.Status(resp.StatusCode)
	if len(resp.Body) > 0 {
		_, _ = c.Writer.Write(resp.Body)
	}
	// 不再往下走：下游 handler 一旦执行，副作用就会发生第二次。
	c.Abort()
}

// writeBuffered 把缓冲的响应写给真正在等它的连接。
func writeBuffered(c *gin.Context, buf *bufferingWriter) {
	c.Writer.WriteHeader(buf.Status())
	if buf.body.Len() > 0 {
		_, _ = c.Writer.Write(buf.body.Bytes())
	}
}

// bufferingWriter 把响应拦在内存里，不写给连接。
// 嵌入 `gin.ResponseWriter` 而不是 `http.ResponseWriter`：前者还要求
// `Status()/Size()/Written()/WriteHeaderNow()/Pusher()`，嵌入能把它们原样转发；
// 尤其是 `Header()` 必须与下游共用同一个 map，否则 `c.Header(...)` 设的头会丢。
type bufferingWriter struct {
	gin.ResponseWriter
	status int
	body   bytes.Buffer
}

// WriteHeader 记录状态码但不写出。
// 后到的状态码不覆盖先到的（与 net/http 一致），否则重放时会写出与首次不同的状态码。
func (w *bufferingWriter) WriteHeader(code int) {
	if w.status == 0 {
		w.status = code
	}
}

// WriteHeaderNow 空实现：真正的写出推迟到 writeBuffered。
func (w *bufferingWriter) WriteHeaderNow() {}

// Write 把正文写进内存缓冲，等幂等记录落库成功后再真正下发。
func (w *bufferingWriter) Write(b []byte) (int, error) { return w.body.Write(b) }

// WriteString 与 Write 等价，供 gin 的字符串渲染路径使用。
func (w *bufferingWriter) WriteString(s string) (int, error) { return w.body.WriteString(s) }

// Status 返回业务状态码；从未设置过时按 200 处理（与 net/http 一致）。
func (w *bufferingWriter) Status() int {
	if w.status == 0 {
		return http.StatusOK
	}
	return w.status
}

// Size 返回已缓冲的正文字节数（gin 用它记日志）。
func (w *bufferingWriter) Size() int { return w.body.Len() }

// Written 报告是否已产生任何响应内容。
//
// 「设过状态码」也算已写出：否则 204 这类无正文响应会被判成「还没写」，
// 幂等层会以为需要补一个状态码而覆盖真实的 204。
func (w *bufferingWriter) Written() bool { return w.status != 0 || w.body.Len() > 0 }

// Flush 空实现：这些路由上没有流式响应，缓冲也是为了不中途写出。
func (w *bufferingWriter) Flush() {}
