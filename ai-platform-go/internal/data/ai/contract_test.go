package ai

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// TestContractFieldParity 是 AC-ORCH-02 的**字段级**守卫。
//
// 起因：Go 侧写 proto、Python 侧写 Pydantic，两份契约分别演进。谁在一边加了
// 字段而忘了另一边，编译期毫无反应 —— 表现为「新字段永远是零值/默认值」，
// 而这是最难查的一类 bug（代码看起来都对）。
//
// 所以这里不测行为，只测**名字集合**：每个消息/模型对的名字必须一一对应。
// 允许的差异必须写进 allowlist 并附理由，不能靠"顺手放过"。
func TestContractFieldParity(t *testing.T) {
	protoSrc := readFileOrFail(t, protoPath(t))
	pySrc := readFileOrFail(t, schemaPath(t))

	protoMsgs := parseProtoMessages(protoSrc)
	pyClasses := parsePydanticClasses(pySrc)

	// 解析器自检：解析器一旦失效，下面所有断言都会「通过」。
	// 先证明真的解析到了东西，且规模合理。
	if len(protoMsgs) == 0 {
		t.Fatal("proto 解析结果为空：解析器失效（测试会变成永远通过）")
	}
	if len(pyClasses) == 0 {
		t.Fatal("Pydantic 解析结果为空：解析器失效（测试会变成永远通过）")
	}
	if n := len(protoMsgs["ChatRequest"]); n < 10 {
		t.Fatalf("proto ChatRequest 只解析出 %d 个字段，解析器可疑", n)
	}
	if n := len(pyClasses["ChatRequest"]); n < 12 {
		t.Fatalf("Pydantic ChatRequest 只解析出 %d 个字段，解析器可疑", n)
	}

	cases := []struct {
		name string
		// protoOnly / pyOnly 是**有意**只在一边存在的字段，必须写理由。
		protoOnly map[string]string
		pyOnly    map[string]string
		// rename：两边名字不同但语义相同的字段（proto 名 → python 名）。
		rename map[string]string
	}{
		{
			name: "ChatRequest",
			pyOnly: map[string]string{
				// 流式是 M4 的事；M3 的 `/chat/stream` 仍走 HTTP/SSE，
				// gRPC 契约先在 M4 加 `ChatStream`，届时这里同步放开。
				"stream": "M4 的流式开关，gRPC 侧由独立的 ChatStream RPC 承担",
			},
		},
		{
			name: "ChatResponse",
			// proto 里 `message_id` 与 `conversation_id` 都有；
			// 网关只采信 conversation_id（见 biz.ChatResult 的说明）。
		},
		{name: "ChatMessage"},
		{name: "Reference"},
		{
			name: "ToolCallTrace",
			rename: map[string]string{
				// proto 用字符串承载任意 JSON（protobuf 的 map 不能装嵌套结构），
				// Pydantic 侧是 `dict[str, Any]`。转换在 transport 层做。
				"arguments_json": "arguments",
			},
		},
		{name: "Usage"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, want := protoMsgs[tc.name], pyClasses[tc.name]
			if len(got) == 0 {
				t.Fatalf("proto 里找不到 message %s", tc.name)
			}
			if len(want) == 0 {
				t.Fatalf("chat.py 里找不到 class %s", tc.name)
			}

			// proto → python
			for f := range got {
				name := f
				if renamed, ok := tc.rename[f]; ok {
					name = renamed
				}
				if _, ok := want[name]; !ok {
					if _, allowed := tc.protoOnly[f]; allowed {
						continue
					}
					t.Errorf("proto 有 %s.%s，Pydantic 侧没有对应字段"+
						"（要么补上，要么加入 allowlist 并写理由）", tc.name, f)
				}
			}
			// python → proto
			for _, f := range sortedKeys(want) {
				if _, ok := got[f]; ok {
					continue
				}
				if _, allowed := tc.pyOnly[f]; allowed {
					continue
				}
				// 反向 rename：python 名在前文 rename 的值里出现过就算对应。
				if _, ok := reverseRenameHit(tc.rename, f); ok {
					continue
				}
				t.Errorf("Pydantic 有 %s.%s，proto 侧没有对应字段"+
					"（要么补上，要么加入 allowlist 并写理由）", tc.name, f)
			}
		})
	}
}

// TestContractPydanticFieldTypesAreKnown 守住「解析器只认自己认得的语法」这条线。
//
// 如果 `chat.py` 用了本测试没覆盖的写法（比如 `Annotated[...]` 或
// `model_config` 之外的声明方式），字段会被静默漏掉 —— 那时上面的对照
// 会「少一边」，测试报错的方向就会误导人。这里直接检查可疑语法不存在。
func TestContractPydanticFieldTypesAreKnown(t *testing.T) {
	src := readFileOrFail(t, schemaPath(t))
	for _, marker := range []string{"Annotated[", "class Config", "  schema_extra"} {
		if strings.Contains(src, marker) {
			t.Errorf("schema 里出现 %q：请同步更新字段解析器，否则字段会被静默漏掉", marker)
		}
	}
}

// ---- 路径定位 ----

// schemaPath 定位 `ai-platform/app/schemas/chat.py`。
//
// 两个仓库并列放在同一个父目录下是本项目的既定布局（deploy 脚本也这么假设），
// 但 CI 的检出方式可能不同 —— 所以既支持 `AI_PLATFORM_SCHEMA` 环境变量，
// 也依次尝试几个候选相对路径；全都找不到时**直接失败**而不是 skip：
// 一个静默跳过的契约测试等于没有。
func schemaPath(t *testing.T) string {
	t.Helper()
	if p := os.Getenv("AI_PLATFORM_SCHEMA"); p != "" {
		return p
	}
	candidates := []string{
		filepath.Join("..", "..", "..", "..", "ai-platform", "app", "schemas", "chat.py"),
		filepath.Join("..", "..", "..", "ai-platform", "app", "schemas", "chat.py"),
		filepath.Join("..", "..", "ai-platform", "app", "schemas", "chat.py"),
	}
	for _, c := range candidates {
		if abs, err := filepath.Abs(c); err == nil {
			if _, err := os.Stat(abs); err == nil {
				return abs
			}
		}
	}
	t.Fatalf("找不到 ai-platform/app/schemas/chat.py。已尝试:\n  %s\n"+
		"请把两个仓库并列检出，或用 AI_PLATFORM_SCHEMA 指定路径", strings.Join(candidates, "\n  "))
	return ""
}

func protoPath(t *testing.T) string {
	t.Helper()
	p := filepath.Join("..", "..", "..", "proto", "aiplatform", "v1", "chat.proto")
	abs, err := filepath.Abs(p)
	if err != nil {
		t.Fatalf("proto 路径解析失败: %v", err)
	}
	return abs
}

func readFileOrFail(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取 %s 失败: %v", path, err)
	}
	return string(b)
}

// ---- proto 解析 ----

var (
	protoMessageRe = regexp.MustCompile(`^message\s+(\w+)\s*\{`)
	// 字段行：可选 label + 可选类型参数 + 类型 + 名 + `= 序号;`
	protoFieldRe     = regexp.MustCompile(`^\s*(?:repeated|optional|required)?\s*[.\w<>,\s]*?(\w+)\s*=\s*\d+\s*;`)
	protoLineComment = regexp.MustCompile(`//.*$`)
)

// parseProtoMessages 返回 消息名 → 字段名集合。
//
// 只看顶层 `message X { ... }` 的直接字段：proto 文件里没有嵌套 message，
// 一旦有人加了嵌套，这里的深度计数会把内层字段算到外层 —— 所以
// `TestContractProtoHasNoNestedMessages` 明确禁止嵌套。
func parseProtoMessages(src string) map[string]map[string]struct{} {
	out := map[string]map[string]struct{}{}
	var current map[string]struct{}
	depth := 0
	for _, raw := range strings.Split(src, "\n") {
		line := protoLineComment.ReplaceAllString(raw, "")
		trimmed := strings.TrimSpace(line)
		if depth == 0 {
			if m := protoMessageRe.FindStringSubmatch(trimmed); m != nil {
				current = map[string]struct{}{}
				out[m[1]] = current
				depth = 1
				continue
			}
			continue
		}
		switch {
		case trimmed == "}":
			depth--
			if depth == 0 {
				current = nil
			}
		case strings.HasSuffix(trimmed, "{"):
			depth++
		default:
			if current == nil || trimmed == "" || strings.HasPrefix(trimmed, "reserved") {
				continue
			}
			if m := protoFieldRe.FindStringSubmatch(line); m != nil {
				current[m[1]] = struct{}{}
			}
		}
	}
	return out
}

// ---- Pydantic 解析 ----

var (
	pyClassRe = regexp.MustCompile(`^class\s+(\w+)\s*\(`)
	// 直接挂在类体上的字段：恰好 4 空格缩进 + 合法标识符 + 冒号。
	pyFieldRe = regexp.MustCompile(`^ {4}([a-zA-Z_]\w*)\s*:`)
)

// parsePydanticClasses 返回 类名 → 字段名集合（只取类体第一层的注解赋值）。
//
// 需要处理三件事，漏任何一个都会「少字段」：
//   - 三引号 docstring 里的 `xxx:` 行不是字段；
//   - 方法体（`def` 下面是 8 空格）不参与；
//   - 类在 `class X(` 行之后直到下一个顶层 `class`/`def` 之间都是类体
//     （中间有装饰器/注释/空行，不能一遇到空行就结束）。
func parsePydanticClasses(src string) map[string]map[string]struct{} {
	out := map[string]map[string]struct{}{}
	var current map[string]struct{}
	inDocstring := false
	inTriple := ""
	for _, raw := range strings.Split(src, "\n") {
		line := strings.TrimRight(raw, "\r")
		trimmed := strings.TrimSpace(line)

		// docstring 状态机（三引号成对出现时同行的开闭要一起处理）。
		if inDocstring {
			if strings.Contains(line, inTriple) {
				inDocstring = false
			}
			continue
		}
		if n := strings.Count(line, `"""`); n%2 == 1 {
			inDocstring = true
			inTriple = `"""`
			continue
		}
		if n := strings.Count(line, "'''"); n%2 == 1 {
			inDocstring = true
			inTriple = "'''"
			continue
		}

		if !strings.HasPrefix(line, " ") && !strings.HasPrefix(line, "\t") {
			// 顶层语句：可能是类定义，也可能是别的（import、常量、函数）。
			if m := pyClassRe.FindStringSubmatch(trimmed); m != nil && strings.HasSuffix(trimmed, ":") {
				current = map[string]struct{}{}
				out[m[1]] = current
				continue
			}
			if trimmed != "" && !strings.HasPrefix(trimmed, "#") {
				current = nil
			}
			continue
		}
		if current == nil || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if m := pyFieldRe.FindStringSubmatch(line); m != nil {
			current[m[1]] = struct{}{}
		}
	}
	return out
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func reverseRenameHit(rename map[string]string, pyName string) (string, bool) {
	for protoName, py := range rename {
		if py == pyName {
			return protoName, true
		}
	}
	return "", false
}
