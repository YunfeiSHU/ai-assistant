package redis

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// 两个 Lua 脚本的**返回形态**不同，而调用它们的 helper 也不同：
//
//	reserveScript → `return {v, 1}`（Lua 表）→ evalNumbers
//	addScript     → `return v`（整数）      → evalNumber
//
// 这条对应关系必须被守住，因为**搞错时的现象是「配额静默失效」**：
// 早先两个脚本共用了一个「按表解析」的 helper，于是 `Add` 每次都报
// `返回类型异常 int64`；错误被降级包装吞掉后，计数器整体改用进程内计数，
// 而进程内计数只有 `Add` 写过的键 —— 它看不到 `Reserve` 写在 Redis 里的
// `chat_requests`，于是「预扣 1 次」的用户第二次提问时本地仍是 0 →
// 实测 free 档 `chat_requests=1` 时第 2 次提问照常打给了 AI。
//
// 这类错误编译通过、类型正确、单测也过，只在真机上表现为「配额拦不住」，
// 因此用一条源码级守卫把它钉死：新增脚本或换 helper 都会在这里失败。
func TestScriptShapeMatchesEvalHelper(t *testing.T) {
	fset := token.NewFileSet()
	path := filepath.Join(".", "counter.go")
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s 失败：%v", path, err)
	}

	scripts := map[string]string{}   // 常量名 → 脚本源码
	calls := map[string]string{}     // 脚本常量名 → 使用的 helper 名
	wantHelper := map[string]string{ // 期望的对应关系
		"reserveScript": "evalNumbers",
		"addScript":     "evalNumber",
	}

	ast.Inspect(file, func(n ast.Node) bool {
		switch node := n.(type) {
		case *ast.ValueSpec:
			for i, name := range node.Names {
				if i >= len(node.Values) {
					continue
				}
				lit, ok := node.Values[i].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				val, uerr := strconv.Unquote(lit.Value)
				if uerr != nil {
					continue
				}
				if strings.Contains(val, "redis.call") {
					scripts[name.Name] = val
				}
			}
		case *ast.CallExpr:
			sel, ok := node.Fun.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			helper := sel.Sel.Name
			if helper != "evalNumbers" && helper != "evalNumber" {
				return true
			}
			// 第一个脚本参数是常量标识符（也可能带 *ast.Ident 之外的包装）。
			for _, arg := range node.Args {
				if id, ok := arg.(*ast.Ident); ok {
					if _, isScript := scripts[id.Name]; isScript {
						calls[id.Name] = helper
					}
				}
			}
		}
		return true
	})

	if len(scripts) < 2 {
		t.Fatalf("只找到 %d 个 Lua 脚本常量，守卫失效（期望 ≥2）", len(scripts))
	}
	for name, helper := range wantHelper {
		if _, ok := scripts[name]; !ok {
			t.Errorf("没找到脚本常量 %s，守卫需要同步更新", name)
			continue
		}
		got, ok := calls[name]
		if !ok {
			t.Errorf("脚本 %s 没有被任何 eval* helper 调用，守卫失效", name)
			continue
		}
		if got != helper {
			t.Errorf("脚本 %s 应由 %s 执行，实际用了 %s", name, helper, got)
		}
	}

	// 反向断言：helper 与脚本的返回形态必须一致（guard 的核心）。
	for name, src := range scripts {
		helper, ok := calls[name]
		if !ok {
			continue
		}
		ret := lastReturnExpression(src)
		if ret == "" {
			t.Errorf("脚本 %s 没找到 `return` 语句", name)
			continue
		}
		isTable := strings.HasPrefix(strings.TrimSpace(ret), "{")
		switch {
		case helper == "evalNumbers" && !isTable:
			t.Errorf("脚本 %s 的 `return %s` 不是 Lua 表，不能用 %s（会得到 int64 并静默降级）", name, ret, helper)
		case helper == "evalNumber" && isTable:
			t.Errorf("脚本 %s 的 `return %s` 是 Lua 表，不能用 %s（会得到 []any）", name, ret, helper)
		}
	}
}

// lastReturnExpression 取脚本里最后一条顶层 `return` 的表达式文本。
func lastReturnExpression(script string) string {
	last := ""
	for _, line := range strings.Split(script, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "return ") {
			last = strings.TrimPrefix(line, "return ")
		}
	}
	return last
}

// TestRateKeyRejectsEmailButAcceptsHashedID 钉住「标识必须落在 Key 白名单内」这条契约。
//
// 它同时是 `middleware.accountBucketKey` 存在的理由：直接用邮箱做标识会让
// `RateKey` 每次都返回 `ErrUnsafeKeyPart`，而降级包装会把这个错误吞成
// 一行 WARN 并改用进程内计数 —— 多实例部署下每个实例各发一份名额。
func TestRateKeyRejectsEmailButAcceptsHashedID(t *testing.T) {
	if _, err := RateKey("login_account", "user@example.com"); err == nil {
		t.Error("含 @ 的邮箱不应被接受为 Key 片段（正是这条约束要求调用方先哈希）")
	}
	hashed := "0123456789abcdef"
	key, err := RateKey("login_account", hashed)
	if err != nil {
		t.Fatalf("哈希后的标识应可入键：%v", err)
	}
	if !strings.HasPrefix(key, KeyPrefix+"rate:login_account:") {
		t.Errorf("键前缀不符合 docs/05-§3：%s", key)
	}
}

// TestScriptNameIsSingleLine 断言错误信息里的脚本标识不含换行。
//
// 结构化日志的 `error` 字段一旦出现换行，行内检索（`Select-String`）会错位，
// 而排障时正是靠 `error=` 这一行过滤。
func TestScriptNameIsSingleLine(t *testing.T) {
	for _, script := range []string{reserveScript, addScript} {
		got := scriptName(script)
		if got == "" {
			t.Fatal("scriptName 返回空串")
		}
		if strings.ContainsAny(got, "\r\n") {
			t.Errorf("scriptName 返回了多行内容：%q", got)
		}
		if strings.HasPrefix(got, "--") {
			t.Errorf("scriptName 返回了注释行：%q", got)
		}
	}
}
