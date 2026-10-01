package data

import (
	"bytes"
	"testing"
)

// TestEncodeResponseBodyDistinguishesEmptyFromNull 锁住「空响应体」的落库表示。
//
// `response_body` 是共享表的 MySQL 原生 `JSON` 列（`json NOT NULL`，定义在
// ai-platform 侧，docs/05-§2.7）。空串不是合法 JSON 文档，写入直接报
// **ERROR 3140 Invalid JSON text: "The document is empty."**（已用 mysql 实测）——
// 而失败点只在 `Remember` 里，响应早就写回客户端了，于是现象是
// 「接口一切正常，重试却每次都重新执行一遍副作用」。
//
// 所以空体必须编成 JSON `null`。这条测试的价值全在**边界**上：
// bytes 为 nil、空切片、以及「body 字面量就是 null」三者要能各归各位。
func TestEncodeResponseBodyDistinguishesEmptyFromNull(t *testing.T) {
	cases := []struct {
		name  string
		body  []byte
		store string // 落库文本
	}{
		{"nil body（204）", nil, emptyBodyJSON},
		{"empty slice（204）", []byte{}, emptyBodyJSON},
		{"JSON 对象", []byte(`{"id":"cv_1"}`), `{"id":"cv_1"}`},
		// body 字面量恰好是 `null` 时，存进去与空体无法区分 —— 这是该编码的
		// 已知代价，下面单独说明。
		{"body 就是字面量 null", []byte(`null`), emptyBodyJSON},
		{"JSON 数组", []byte(`[]`), `[]`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := encodeResponseBody(tc.body); got != tc.store {
				t.Errorf("encodeResponseBody(%q) = %q，期望 %q", tc.body, got, tc.store)
			}
		})
	}
}

// TestDecodeResponseBodyRoundTrip 往返：正常响应体都能原样读回来。
//
// 注意这里断言的是**字节相等**，而真实链路里 MySQL 的 JSON 列会规范化文档
// （键序、空白），所以线上回放是语义等价而不是字节相等 ——
// 规范化发生在 MySQL 内部，Go 这一层看不到，只能由 docs 与验收脚本承担说明。
func TestDecodeResponseBodyRoundTrip(t *testing.T) {
	cases := [][]byte{
		[]byte(`{"id":"cv_1","title":"a"}`),
		[]byte(`[]`),
		[]byte(`"中文也要无损"`),
		[]byte(`0`),
	}
	for _, body := range cases {
		stored := encodeResponseBody(body)
		if got := decodeResponseBody(stored); !bytes.Equal(got, body) {
			t.Errorf("往返失败：%q -> %q -> %q", body, stored, got)
		}
	}
}

// TestEncodeResponseBodyNullLiteralIsIndistinguishableFromEmpty 把已知代价**钉出来**。
//
// 「body 字面量就是 `null`」与「没有 body」在存储层无法区分：两者都编成 `null`，
// 回放时都变成「不写 body」。这是该编码的取舍，不是 bug —— 换成别的哨兵
// （`""` / `{}`）只会把歧义挪到另一组输入上，而幂等中间件只挂在非流式 JSON 路由上，
// 那些 handler 从不返回字面量 `null`。
//
// 之所以要有这条测试：让「行为变了」在 CI 里可见，而不是变成一条只有读源码
// 才能发现的隐式约定。
func TestEncodeResponseBodyNullLiteralIsIndistinguishableFromEmpty(t *testing.T) {
	stored := encodeResponseBody([]byte(`null`))
	if stored != emptyBodyJSON {
		t.Fatalf("字面量 null 的编码是 %q，期望与空体哨兵相同（%q）", stored, emptyBodyJSON)
	}
	if got := decodeResponseBody(stored); got != nil {
		t.Errorf("回放时应退化为「无 body」，实际 %#v", got)
	}
}

// TestDecodeResponseBodyEmptyIsNil 空体的解码结果必须是 **nil**（而不是 `[]byte{}`）。
//
// 上游 `replayStored` 用 `len(resp.Body) > 0` 决定要不要写 body 与 Content-Type：
// 两者对它等价，但 nil 更准确 —— 它表达的语义是「这个响应本来就没有 body」，
// 而不是「有一个长度为 0 的 body」。
func TestDecodeResponseBodyEmptyIsNil(t *testing.T) {
	if got := decodeResponseBody(emptyBodyJSON); got != nil {
		t.Errorf("decodeResponseBody(%q) 应为 nil，实际 %#v", emptyBodyJSON, got)
	}
}

// TestEmptyBodyJSONIsPlaceholderIsLoadBearing 钉住哨兵值的**字面量**。
//
// 看着像「常量等于常量」的同义反复，其实不是：其余测试全部引用这个常量，
// 所以把 `null` 改成别的值（比如 `""`）时它们一个都不会红 —— 而那个改动
// 会同时弄坏两个方向：`""` 里读回来是 2 字节的 `""`，与「body 真的是空 JSON 字符串」
// 分不开；而其他占位值（例如 `{}`）会被当成合法响应体回放给客户端。
// 哨兵值是**数据库层契约**的一部分，只能这样钉。
func TestEmptyBodyJSONIsPlaceholderIsLoadBearing(t *testing.T) {
	if emptyBodyJSON != "null" {
		t.Fatalf("空体哨兵必须是 JSON 字面量 null，实际 %q —— 改它等于改「204 能不能落库」这件事", emptyBodyJSON)
	}
}
