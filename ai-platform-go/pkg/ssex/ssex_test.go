package ssex

import (
	"strings"
	"testing"
	"time"
)

// 以下「期望字节」全部是跨语言契约：ai-platform 的 `app/core/sse.py::format_frame`
// 对同一输入必须产出完全相同的字节。
//
// 字面量写在测试里而不是「用 Python 跑一遍」，因为单测必须能在没有 Python 环境的
// 机器上跑。两侧一致性由两条互补手段保证：这里钉死字节（Go 侧改了立刻红）；
// `tools/curl_stage4.ps1` 真调一次 Python，覆盖「Python 侧改了」这个方向。
// 只做其中一条都会漏掉一半。
func TestFrameMatchesAISideByteForByte(t *testing.T) {
	cases := []struct {
		name    string
		event   string
		payload string
		want    string
	}{
		{
			// f'event: {event}\ndata: {payload}\n\n' —— 最普通的一帧。
			name:    "meta",
			event:   "meta",
			payload: `{"conversation_id":"cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C","model":"deepseek-flash"}`,
			want:    "event: meta\ndata: {\"conversation_id\":\"cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C\",\"model\":\"deepseek-flash\"}\n\n",
		},
		{
			// 中文必须原样（UTF-8 字节），不能转成 \uXXXX —— Python 侧用
			// ensure_ascii=False，网关若转义就会出现「语义相同、字节不同」。
			name:    "token-中文",
			event:   "token",
			payload: `{"delta":"你好"}`,
			want:    "event: token\ndata: {\"delta\":\"你好\"}\n\n",
		},
		{
			// 空对象也是合法负载（占位事件）。
			name:    "done",
			event:   "done",
			payload: `{}`,
			want:    "event: done\ndata: {}\n\n",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Frame(tc.event, []byte(tc.payload))
			if err != nil {
				t.Fatalf("Frame 报错: %v", err)
			}
			if string(got) != tc.want {
				t.Fatalf("帧字节不一致\n got=%q\nwant=%q", got, tc.want)
			}
		})
	}
}

func TestFrameRejectsNewlineInPayload(t *testing.T) {
	// 裸换行会让一帧变成两帧，客户端拿到的是两段无法解析的 JSON。
	// MUST 早失败：静默接受的话症状是「偶发解析失败」，排查方向会跑到客户端去。
	for _, payload := range []string{"a\nb", "a\rb", "{\"delta\":\"a\nb\"}"} {
		if _, err := Frame("token", []byte(payload)); err == nil {
			t.Fatalf("含裸换行的负载应当被拒绝: %q", payload)
		}
	}
}

func TestFrameAcceptsEscapedNewlineInJSON(t *testing.T) {
	// 与上一条相对的另一半：JSON 里的 `\n` 是两个字符（反斜杠 + n）而不是裸换行，
	// 完全合法，模型输出的多行 Markdown 正是靠它表达。拒掉它会让「回答里带换行」直接 500。
	payload := `{"delta":"第一行\n第二行"}`
	got, err := Frame("token", []byte(payload))
	if err != nil {
		t.Fatalf("转义换行应当被接受: %v", err)
	}
	want := "event: token\ndata: {\"delta\":\"第一行\\n第二行\"}\n\n"
	if string(got) != want {
		t.Fatalf("帧字节不一致\n got=%q\nwant=%q", got, want)
	}
}

func TestFrameRejectsEmptyEventName(t *testing.T) {
	// 空事件名会产出 `event: \n`，客户端按 `message` 默认类型处理 ——
	// 静默接受等于让一帧事件消失。
	if _, err := Frame("", []byte(`{}`)); err == nil {
		t.Fatal("空事件名应当被拒绝")
	}
}

func TestPingFrameShapeAndRoundTrip(t *testing.T) {
	at := time.Date(2026, 9, 29, 10, 0, 0, 0, time.UTC)
	got := string(PingFrame(at))
	want := "event: ping\ndata: {\"ts\":\"2026-09-29T10:00:00.000Z\"}\n\n"
	if got != want {
		t.Fatalf("心跳帧不一致\n got=%q\nwant=%q", got, want)
	}

	// 读回时间戳：验收脚本要靠它断言「心跳确实是 15s 一次」。
	payload := []byte(`{"ts":"2026-09-29T10:00:00.000Z"}`)
	parsed, err := ParsePingTS(payload)
	if err != nil {
		t.Fatalf("ParsePingTS 报错: %v", err)
	}
	if !parsed.Equal(at) {
		t.Fatalf("时间戳往返不一致: got=%s want=%s", parsed, at)
	}
}

func TestPingFrameUsesUTCAndMillis(t *testing.T) {
	// 时区必须归一：同一时刻在 +08:00 与 UTC 下格式化出的字符串不同，而客户端只会按一种解析。
	at := time.Date(2026, 9, 29, 18, 30, 45, 123_000_000, time.FixedZone("CST", 8*3600))
	got := string(PingFrame(at))
	if !strings.Contains(got, `"2026-09-29T10:30:45.123Z"`) {
		t.Fatalf("心跳时间未归一为 UTC 毫秒: %q", got)
	}
}

func TestHeaders(t *testing.T) {
	h := Headers()
	// 三个头各有明确目的，少一个就会出问题：
	//   Content-Type   → 客户端按什么解析（含 charset，中文回答不乱码）
	//   Cache-Control  → 中间层不得缓存、不得改写（no-transform 是防压缩的关键）
	//   X-Accel-Buffering → nginx 不缓冲，否则「逐字输出」变成「憋几秒一次性吐出」
	want := map[string]string{
		"Content-Type":      "text/event-stream; charset=utf-8",
		"Cache-Control":     "no-cache, no-transform",
		"X-Accel-Buffering": "no",
	}
	for k, v := range want {
		if h[k] != v {
			t.Fatalf("头部 %s=%q，期望 %q", k, h[k], v)
		}
	}
	if _, ok := h["Connection"]; ok {
		// HTTP/2 明确禁止该头（RFC 9113 §8.2.2），写了会在某些实现里报协议错误。
		t.Fatal("MUST NOT 下发 Connection 头（HTTP/2 禁止）")
	}
	if _, ok := h["Content-Encoding"]; ok {
		t.Fatal("MUST NOT 压缩流式响应（docs/04-§3.1）")
	}
}
