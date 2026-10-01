package ai

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
)

// 本文件覆盖 SSE 回退通道（`AI_GRPC_ENABLED=false` 时使用）。
//
// 重点全在**分帧**：SSE 的帧边界是空行，任何一处处理不当都表现为
// 「正文里少了一截」「JSON 解析失败」「帧数与上游不一致」——
// 而这三种症状在真机上都不会报错，只会让用户看到一段奇怪的回答。

func testLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func decodeAll(t *testing.T, raw string) []*sseFrame {
	t.Helper()
	d := newSSEDecoder(strings.NewReader(raw))
	var out []*sseFrame
	for {
		f, err := d.next()
		if err == io.EOF {
			return out
		}
		if err != nil {
			t.Fatalf("解析失败: %v", err)
		}
		out = append(out, f)
	}
}

func TestSSEDecoderBasicFrame(t *testing.T) {
	got := decodeAll(t, "event: token\ndata: {\"delta\":\"你\"}\n\n")
	if len(got) != 1 {
		t.Fatalf("应有 1 帧，实际 %d", len(got))
	}
	if got[0].Event != "token" || string(got[0].Data) != `{"delta":"你"}` {
		t.Fatalf("帧内容错误：%#v", got[0])
	}
}

func TestSSEDecoderFrameCRLF(t *testing.T) {
	// AI 侧只产 `\n`，但中间的反向代理/网关有可能把行尾规范化成 CRLF。
	// 不处理时 `event` 行会变成 `event: token\r`，事件名带上回车 →
	// 映射表里对不上 → **整条流的每一帧都被当成未知事件**。
	got := decodeAll(t, "event: token\r\ndata: {\"delta\":\"x\"}\r\n\r\n")
	if len(got) != 1 {
		t.Fatalf("CRLF 流应有 1 帧，实际 %d", len(got))
	}
	if got[0].Event != "token" {
		t.Fatalf("事件名不应带回车，实际 %q", got[0].Event)
	}
	if string(got[0].Data) != `{"delta":"x"}` {
		t.Fatalf("负载不应带回车，实际 %q", got[0].Data)
	}
}

func TestSSEDecoderEventDefaultsToMessage(t *testing.T) {
	got := decodeAll(t, "data: {\"a\":1}\n\n")
	if len(got) != 1 || got[0].Event != "message" {
		t.Fatalf("没有 event 字段时应缺省为 message，实际 %#v", got)
	}
}

func TestSSEDecoderJoinsMultiLineDataWithNewline(t *testing.T) {
	// 规范：多行 `data:` 用 `\n` 拼接。**证伪点**：把拼接符写成空串（或漏掉
	// `dataSeen` 的维护）后，JSON 会变成 `{"a":1,"b":2}`（少一个空格的差别看不出来，
	// 但带缩进的负载会直接坏掉）。
	got := decodeAll(t, "event: meta\ndata: {\"a\":1,\ndata: \"b\":2}\n\n")
	if len(got) != 1 {
		t.Fatalf("应有 1 帧，实际 %d", len(got))
	}
	if string(got[0].Data) != "{\"a\":1,\n\"b\":2}" {
		t.Fatalf("多行 data 应用 \\n 拼接，实际 %q", got[0].Data)
	}
	if !json.Valid(got[0].Data) {
		t.Fatalf("拼接结果应是合法 JSON，实际 %q", got[0].Data)
	}
}

func TestSSEDecoderLeadingEmptyDataLineIsPreserved(t *testing.T) {
	// 第一行是空 payload（`data:`）时长度也是 0：用 `data.Len() > 0` 判断
	// 「是否已有 data 行」会漏掉这个分隔用的 `\n`，拼成 `b` 而不是 `\nb`。
	got := decodeAll(t, "data:\ndata: b\n\n")
	if len(got) != 1 {
		t.Fatalf("应有 1 帧，实际 %d", len(got))
	}
	if string(got[0].Data) != "\nb" {
		t.Fatalf("首行为空 payload 时也应保留分隔换行，实际 %q", got[0].Data)
	}
}

func TestSSEDecoderIgnoresCommentsAndUnknownFields(t *testing.T) {
	// 注释行是规范要求忽略的（反向代理会插入）；`id`/`retry` 网关用不到。
	// **证伪点**：注释行如果不跳过，`:` 会被当成字段名，value 变成整行 ——
	// 且在 `event:` 行之前出现时会**污染事件名**。
	got := decodeAll(t, ": keepalive\nevent: token\nid: 42\nretry: 3000\ndata: x\n\n")
	if len(got) != 1 {
		t.Fatalf("应有 1 帧，实际 %d", len(got))
	}
	if got[0].Event != "token" || string(got[0].Data) != "x" {
		t.Fatalf("注释与未知字段都应被忽略，实际 %#v", got[0])
	}
}

func TestSSEDecoderSkipsBlankLineKeepalives(t *testing.T) {
	// 连续空行（有些实现拿它当保活）：不派发空事件。
	// 派发出去会变成一帧 data 为空的 `message` 事件 → 映射成未知事件
	// → 客户端收到一堆无意义的帧。
	got := decodeAll(t, "\n\n\nevent: token\ndata: x\n\n\n")
	if len(got) != 1 {
		t.Fatalf("空行保活不应产生帧，实际 %d 帧", len(got))
	}
}

func TestSSEDecoderTrailingFrameWithoutBlankLineIsNotDispatched(t *testing.T) {
	// 没有结尾空行的帧**不派发**：SSE 的帧边界就是空行，
	// 提前派发会让「还没收全的多行 data」被当成完整帧发出去（半个 JSON）。
	got := decodeAll(t, "event: token\ndata: {\"delta\":\"x\"}")
	if len(got) != 0 {
		t.Fatalf("没有空行终止的帧不应派发，实际 %d 帧", len(got))
	}
}

func TestSSEDecoderStripsOnlyOneLeadingSpace(t *testing.T) {
	// 规范：冒号后**最多**去掉一个空格，其余空格属于值。
	// 全都 trim 掉会改坏以空格开头的正文增量（`trim` 后用户看到的
	// 回答会丢掉缩进）。
	got := decodeAll(t, "data:  two spaces\n\n")
	if len(got) != 1 || string(got[0].Data) != " two spaces" {
		t.Fatalf("只应去掉一个前导空格，实际 %q", got[0].Data)
	}
}

func TestSSEDecoderOversizedLineIsAnError(t *testing.T) {
	// 单行超限必须报错而不是静默截断：截断后是一段坏 JSON，
	// 表现成「偶发地少一帧」，比明确报错难查得多。
	var sb strings.Builder
	sb.WriteString("data: ")
	sb.WriteString(strings.Repeat("x", sseMaxLineBytes+10))
	sb.WriteString("\n\n")

	d := newSSEDecoder(strings.NewReader(sb.String()))
	if _, err := d.next(); err == nil {
		t.Fatal("超长行必须报错")
	}
}

// ---- 帧 → 事件 ----

func TestMapSSEFrameDropsPing(t *testing.T) {
	ev, ok := mapSSEFrame(&sseFrame{Event: "ping", Data: []byte(`{"ts":"t"}`)}, testLog(), context.Background())
	if ok || ev != nil {
		t.Fatalf("ping 必须**不映射**（网关自带心跳，映射出去会让客户端收到两路），实际 %#v", ev)
	}
}

func TestMapSSEFrameUnknownGoesThrough(t *testing.T) {
	ev, ok := mapSSEFrame(&sseFrame{Event: "citation_note", Data: []byte(`{"n":1.10}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("未知事件必须透传（MUST NOT 丢弃）")
	}
	unknown, isUnknown := ev.(biz.StreamUnknownEvent)
	if !isUnknown {
		t.Fatalf("应为 StreamUnknownEvent，实际 %T", ev)
	}
	if unknown.Name != "citation_note" || string(unknown.Data) != `{"n":1.10}` {
		t.Fatalf("未知事件应原样保留，实际 %#v", unknown)
	}
}

func TestMapSSEFrameInvalidJSONIsDropped(t *testing.T) {
	// 一帧坏 JSON 只能丢这一帧，不能中断整条流（由调用方保证 continue）。
	// 丢掉时必须有日志 —— 静默丢帧是最难查的那类问题。
	if _, ok := mapSSEFrame(&sseFrame{Event: "token", Data: []byte(`{`)}, testLog(), context.Background()); ok {
		t.Fatal("坏 JSON 应被丢弃")
	}
}

func TestMapSSEFrameTokenAndDone(t *testing.T) {
	ev, ok := mapSSEFrame(&sseFrame{Event: "token", Data: []byte(`{"delta":"你"}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("token 帧不应被丢")
	}
	if tok, isTok := ev.(biz.StreamTokenEvent); !isTok || tok.Delta != "你" {
		t.Fatalf("token 映射错误：%#v", ev)
	}

	ev, ok = mapSSEFrame(&sseFrame{Event: "done", Data: []byte(`{"finish_reason":"length","elapsed_ms":9,"partial":true}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("done 帧不应被丢")
	}
	done, isDone := ev.(biz.StreamDoneEvent)
	if !isDone || done.FinishReason != "length" || done.ElapsedMS != 9 || !done.Partial {
		t.Fatalf("done 映射错误：%#v", ev)
	}
}

func TestMapSSEFrameToolCallUsesObjectArguments(t *testing.T) {
	// 与 gRPC 通道的差别就在这里：SSE 的 `arguments` 已经是**对象**，
	// proto 那条是 `arguments_json` 字符串。归一以后两条路必须一样。
	raw := `{"call_id":"c1","name":"kb.search","arguments":{"q":"向量"}}`
	ev, ok := mapSSEFrame(&sseFrame{Event: "tool_call", Data: []byte(raw)}, testLog(), context.Background())
	if !ok {
		t.Fatal("tool_call 帧不应被丢")
	}
	call, isCall := ev.(biz.StreamToolCallEvent)
	if !isCall {
		t.Fatalf("应为 StreamToolCallEvent，实际 %T", ev)
	}
	if !jsonIsObject(string(call.Arguments)) {
		t.Fatalf("arguments 应为对象 JSON，实际 %s", call.Arguments)
	}
	if call.CallID != "c1" || call.Name != "kb.search" {
		t.Fatalf("tool_call 字段错误：%#v", call)
	}
}

func TestMapSSEFrameReferencesAreUnwrapped(t *testing.T) {
	// SSE 的 `reference` 负载是 `{"references":[...]}`（对象），
	// 而落库/下发给 biz 的是**数组**本身。忘了解包会让库里的 references
	// 变成 `{"references":[...]}`—— 形状与非流式路径不一致，前端遍历时报错。
	raw := `{"references":[{"index":1,"chunk_id":"ck_1"}]}`
	ev, ok := mapSSEFrame(&sseFrame{Event: "reference", Data: []byte(raw)}, testLog(), context.Background())
	if !ok {
		t.Fatal("reference 帧不应被丢")
	}
	ref, isRef := ev.(biz.StreamReferenceEvent)
	if !isRef {
		t.Fatalf("应为 StreamReferenceEvent，实际 %T", ev)
	}
	if !strings.HasPrefix(string(ref.References), "[") {
		t.Fatalf("引用应解包成数组，实际 %s", ref.References)
	}
	var items []map[string]any
	if err := json.Unmarshal(ref.References, &items); err != nil {
		t.Fatalf("引用应是数组 JSON：%v", err)
	}
	if len(items) != 1 || items[0]["chunk_id"] != "ck_1" {
		t.Fatalf("引用内容错误：%s", ref.References)
	}
}

func TestMapSSEFrameMetaCarriesDegradedReasonsWhenPresent(t *testing.T) {
	// HTTP 版 meta 目前没有 degraded_reasons（只有布尔）；这里留字段是为了
	// 「AI 侧补上时不用改代码」。这条用例同时证明缺失时不会炸。
	raw := `{"conversation_id":"cv_1","message_id":"m","model":"m","created_at":"t","degraded":true,"degraded_reasons":["rerank_skipped"]}`
	ev, ok := mapSSEFrame(&sseFrame{Event: "meta", Data: []byte(raw)}, testLog(), context.Background())
	if !ok {
		t.Fatal("meta 帧不应被丢")
	}
	meta, isMeta := ev.(biz.StreamMetaEvent)
	if !isMeta {
		t.Fatalf("应为 StreamMetaEvent，实际 %T", ev)
	}
	if len(meta.DegradedReasons) != 1 || meta.DegradedReasons[0] != "rerank_skipped" {
		t.Fatalf("degraded_reasons 应透传，实际 %#v", meta.DegradedReasons)
	}

	ev, ok = mapSSEFrame(&sseFrame{Event: "meta", Data: []byte(`{"message_id":"m","degraded":false}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("缺字段的 meta 帧不应被丢")
	}
	if meta, isMeta := ev.(biz.StreamMetaEvent); !isMeta || meta.DegradedReasons != nil {
		t.Fatalf("缺字段时 degraded_reasons 应为空，实际 %#v", ev)
	}
}

func TestMapSSEFrameUsageFillsTotalWhenMissing(t *testing.T) {
	// 上游偶尔只给分项、不给 total。留 0 会让配额统计少算这一轮。
	ev, ok := mapSSEFrame(&sseFrame{Event: "usage", Data: []byte(`{"prompt_tokens":3,"completion_tokens":4}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("usage 帧不应被丢")
	}
	usage, isUsage := ev.(biz.StreamUsageEvent)
	if !isUsage {
		t.Fatalf("应为 StreamUsageEvent，实际 %T", ev)
	}
	if usage.Usage.TotalTokens != 7 {
		t.Fatalf("缺 total 时应按分项求和，实际 %#v", usage.Usage)
	}
}

func TestMapSSEFrameErrorKeepsRetryable(t *testing.T) {
	ev, ok := mapSSEFrame(&sseFrame{Event: "error", Data: []byte(`{"code":"AI_TIMEOUT","message":"超时","retryable":true}`)}, testLog(), context.Background())
	if !ok {
		t.Fatal("error 帧不应被丢")
	}
	e, isErr := ev.(biz.StreamErrorEvent)
	if !isErr || e.Code != "AI_TIMEOUT" || !e.Retryable {
		t.Fatalf("error 映射错误：%#v", ev)
	}
}
