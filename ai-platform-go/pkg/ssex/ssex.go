// Package ssex 是 SSE（`text/event-stream`）的**唯一**帧格式出口。
//
// 与 `httpx` 的关系：`httpx` 管 JSON 信封（`{"error":{"code":...}}` 那一层），
// `ssex` 管流式响应（`event: <名>\ndata: <JSON>\n\n` 那一层）。
// 两者都是**契约适配层** —— 既不属于某个业务层，也不该混进 `internal/`，
// 所以都放在 `pkg/`（docs/08-§1.1 的同一判据）。
//
// 为什么值得单独成包：帧格式必须与 ai-platform 的
// `app/core/sse.py::format_frame` **逐字节一致**，否则「同一请求直连 AI 与经网关，
// 事件序列一致」这条验收就只能靠肉眼比对。把格式收敛到一处之后，
// 两侧的差异只可能出现在这个文件里，且可用一条字节级单测钉死
// （见 ssex_test.go，测试数据直接取自 AI 侧的实现）。
package ssex

import (
	"errors"
	"time"
)

// MediaType 是流式响应的 Content-Type。
//
// 显式带 `charset=utf-8`（docs/04-§4.1）：SSE 的默认编码虽是 UTF-8，
// 但 `fetch`/`EventSource` 之外的原生客户端会按 Content-Type 猜编码，
// 中文回答在 `ISO-8859-1` 解释下就是乱码。
const MediaType = "text/event-stream; charset=utf-8"

// PingEvent 是心跳事件名（docs/04-§4.1：15s 无数据时 SHOULD 发一帧）。
const PingEvent = "ping"

// PingInterval 是与 ai-platform 对齐的心跳间隔。
//
// 与 AI 侧用同一个值时，客户端会看到两路心跳「叠在一起」——
// 这是可接受的（docs/04-§4.1 明确写了），且比让客户端因超时而重连划算。
const PingInterval = 15 * time.Second

// Headers 返回 SSE 响应必须设置的头部（docs/04-§4.1）。
//
// 三个头各有明确目的，缺一个都会出问题：
//
//   - `Cache-Control: no-cache, no-transform`：中间层不得缓存，也不得改写正文
//     （`no-transform` 是防代理压缩/重排的关键，只写 `no-cache` 不够）；
//   - `X-Accel-Buffering: no`：nginx 的缓冲开关。默认开启时 nginx 会攒够一个
//     buffer 才转发，于是「逐字输出」变成「憋几秒一次性吐出」；
//   - `Content-Type` 见 MediaType。
//
// **刻意不带 `Connection: keep-alive`**：HTTP/1.1 下它已是默认，
// 而 HTTP/2 明确禁止该头（RFC 9113 §8.2.2），写了会被某些实现在日志里报协议错误。
func Headers() map[string]string {
	return map[string]string{
		"Content-Type":      MediaType,
		"Cache-Control":     "no-cache, no-transform",
		"X-Accel-Buffering": "no",
	}
}

// ErrNewlineInPayload 表示事件负载里含有裸换行。
//
// 这是**必须早失败**的情况：`data:` 字段以换行结束，负载里的换行会让帧被截成两帧，
// 客户端拿到的是两段无法解析的 JSON。静默接受的话，症状是「偶发解析失败」，
// 排查方向会跑到客户端去。
var ErrNewlineInPayload = errors.New("sse: 事件负载不得包含换行")

// Frame 拼一个 SSE 帧：`event: <event>\ndata: <payload>\n\n`。
//
// `payload` 是**已经序列化好的 JSON 字节**（不是 Go 值）：调用方可能是把
// AI 侧原样传来的字节透出去（未知事件），也可能是自己序列化的 —— 两种情况都不该
// 在这里再解析一次。
//
// 与 AI 侧 `format_frame` 的对应关系逐项写在这里，便于两侧一起改：
//
//	Python                                  Go
//	f"event: {event}\n"                     "event: " + event + "\n"
//	f"data: {payload}\n"                    "data: " + payload + "\n"
//	f"\n"                                    "\n"
//	.encode()                               []byte
func Frame(event string, payload []byte) ([]byte, error) {
	if event == "" {
		return nil, errors.New("sse: 事件名不能为空")
	}
	for _, b := range payload {
		if b == '\n' || b == '\r' {
			return nil, ErrNewlineInPayload
		}
	}
	out := make([]byte, 0, len(event)+len(payload)+12)
	out = append(out, "event: "...)
	out = append(out, event...)
	out = append(out, "\ndata: "...)
	out = append(out, payload...)
	out = append(out, "\n\n"...)
	return out, nil
}

// PingFrame 拼一帧心跳：`event: ping` + `{"ts":"<RFC3339 毫秒>"}`。
//
// 负载形状与 AI 侧一致（`app/core/sse.py::ping_frame`），这样客户端只需要
// 认识一种心跳格式 —— 而不是「AI 的心跳长这样、网关的心跳长那样」。
//
// 时间格式复用 `clockx.Layout` 的等价物（这里手写以避免 pkg 之间互相依赖；
// `clockx` 是纯工具包，但 `ssex` 只用到这一个常量，
// 为它引入一条包依赖反而让「谁都能依赖谁」变得模糊）。
func PingFrame(at time.Time) []byte {
	ts := at.UTC().Format("2006-01-02T15:04:05.000Z")
	// 固定形状的负载不需要走 encoding/json：这里的两个键都是常量，
	// 手写反而能保证字节稳定（`json.Marshal` 对 map 会按 key 排序，
	// 对结构体则依赖字段顺序 —— 都不如直接写清楚）。
	frame, err := Frame(PingEvent, []byte(`{"ts":"`+ts+`"}`))
	if err != nil {
		// 不可能发生：ts 由 Format 生成，不含换行。
		panic("ssex: 心跳负载含换行: " + err.Error())
	}
	return frame
}

// ParsePingTS 从心跳帧的负载里取回时间戳（**仅测试与排障用**）。
//
// 放在包里而不是让调用方各写一份正则：验收脚本要断言「心跳确实是 15s 一次」，
// 得能读出那个时间戳，而读法与写法放在一起才不会漂移。
func ParsePingTS(payload []byte) (time.Time, error) {
	const prefix = `{"ts":"`
	s := string(payload)
	if len(s) < len(prefix)+1 {
		return time.Time{}, errors.New("sse: 心跳负载格式不符")
	}
	if s[:len(prefix)] != prefix {
		return time.Time{}, errors.New("sse: 心跳负载缺少 ts 字段")
	}
	rest := s[len(prefix):]
	idx := len(rest) - len(`"}`)
	if idx <= 0 || rest[idx:] != `"}` {
		return time.Time{}, errors.New("sse: 心跳负载不是以 \"} 收尾")
	}
	return time.Parse("2006-01-02T15:04:05.000Z", rest[:idx])
}
