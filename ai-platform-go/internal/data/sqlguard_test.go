package data

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// poTypeNames 是不允许出现在 SQL 字符串字面量里的 PO 类型名。
//
// 成因（真实踩过）：一次全局按词边界重命名把 `conversation` 表名改成了 `conversationPO`，
// 因为 PO 结构体恰好在同一批里重命名，**字符串字面量里的表名也被一起改掉了**。
// 这种错误编译能过、类型也对、单测也过，只有连上 MySQL 才报
// `Table 'xxx.conversationPO' doesn't exist`。
//
// 约定：SQL 里的表名一律走 `quoteIdent(TableXxx)` 常量，绝不手写表名，
// 也绝不写 PO 类型名。
var poTypeNames = []string{
	"userPO",
	"refreshTokenPO",
	"conversationPO",
	"messagePO",
	"idempotencyRecordPO",
	"quotaUsagePO",
	"usageRecordPO",
	"auditLogPO",
}

// sqlStringLiterals 抽出文件里所有字符串字面量（含行号）。
//
// 为什么不用 grep：注释、标识符（如 `toConversationPO`）里出现 PO 名字是
// **合法且常见**的，只有「字符串字面量」才是错误信号。grep 会把前者一起报出来，
// 假阳性多了之后就没人看这个测试了。
func sqlStringLiterals(t *testing.T, fset *token.FileSet, file *ast.File) []stringLit {
	t.Helper()

	var out []stringLit
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		// lit.Value 带引号，先解出真实内容再比较，避免反引号/转义写法被漏掉。
		val, err := strconv.Unquote(lit.Value)
		if err != nil {
			val = lit.Value
		}
		out = append(out, stringLit{Line: fset.Position(lit.Pos()).Line, Value: val})
		return true
	})
	return out
}

type stringLit struct {
	Line  int
	Value string
}

// findPOTypeNamesInSQL 返回违规字面量（行号、命中的 PO 名、内容）。
func findPOTypeNamesInSQL(t *testing.T, fset *token.FileSet, file *ast.File) []string {
	t.Helper()

	var bad []string
	for _, lit := range sqlStringLiterals(t, fset, file) {
		for _, po := range poTypeNames {
			if strings.Contains(lit.Value, po) {
				bad = append(bad, fmt.Sprintf("%d: %s => %s", lit.Line, po, strings.TrimSpace(lit.Value)))
			}
		}
	}
	return bad
}

// TestNoPOTypeNameInSQL 用 AST 扫本包所有字符串字面量，禁止出现 PO 类型名。
func TestNoPOTypeNameInSQL(t *testing.T) {
	fset := token.NewFileSet()
	pkgs, err := parser.ParseDir(fset, ".", func(fi os.FileInfo) bool {
		return !strings.HasSuffix(fi.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatalf("解析本包源码失败: %v", err)
	}
	if len(pkgs) == 0 {
		t.Fatal("没有解析到任何包，测试自身可能失效了")
	}

	checked := 0
	for _, pkg := range pkgs {
		for name, file := range pkg.Files {
			base := filepath.Base(name)
			checked += len(sqlStringLiterals(t, fset, file))
			for _, bad := range findPOTypeNamesInSQL(t, fset, file) {
				t.Errorf("%s:%s\n"+
					"SQL 字符串里出现 PO 类型名。请改用 quoteIdent(TableXxx) 常量拼 SQL"+
					"（表名常量见 model.go 的 Table* 定义）", base, bad)
			}
		}
	}

	// 扫描数量为 0 说明 AST 没走到字符串字面量（例如解析参数写错），
	// 那这个测试会「静默全绿」——比失败更糟。
	if checked == 0 {
		t.Fatal("没有扫描到任何字符串字面量，测试没起作用")
	}
}

// TestPOTypeNameGuardDetectsViolation 自证守卫有效：合成源码必须被判违规。
//
// 守卫类测试最大的风险是「永远绿」——所以先用合成输入证明它会红。
func TestPOTypeNameGuardDetectsViolation(t *testing.T) {
	const src = `package data

import "fmt"

type messagePO struct{}

// 注释里写 messagePO 是允许的（toMessagePO 也一样）。
func bad(m *messagePO) string {
	_ = m
	q := "SELECT messagePO.* FROM messagePO"
	return fmt.Sprintf("%s", q)
}
`
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "synthetic.go", src, 0)
	if err != nil {
		t.Fatalf("合成源码解析失败: %v", err)
	}

	bad := findPOTypeNamesInSQL(t, fset, file)
	// 该字面量里 messagePO 出现两次，但按「字面量 × PO 名」去重后只报一处
	// （同一句 SQL 里报两遍没有额外信息量）。
	// 关键反例：类型名 `messagePO`、参数名 `m *messagePO`、注释里的 messagePO
	// 都在字面量之外，不得计入。若实现退化成 grep，这里会变成 3~4 处。
	if len(bad) != 1 {
		t.Fatalf("期望命中 1 处 SQL 字面量违规，实际 %d 处: %v", len(bad), bad)
	}
	for _, b := range bad {
		if !strings.Contains(b, "messagePO") {
			t.Errorf("违规信息里应包含命中的 PO 名: %s", b)
		}
	}
}

// TestPOTypeNameGuardAllowsCleanSQL 合法写法（quoteIdent / 注释里的 PO 名）不得误报。
func TestPOTypeNameGuardAllowsCleanSQL(t *testing.T) {
	const src = `package data

// toConversationPO 把 DO 转成 conversationPO（注释允许出现）。
func good() string {
	return "SELECT " + quoteIdent(TableConversation) + " WHERE id = ?"
}
`
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "synthetic_clean.go", src, 0)
	if err != nil {
		t.Fatalf("合成源码解析失败: %v", err)
	}
	if bad := findPOTypeNamesInSQL(t, fset, file); len(bad) != 0 {
		t.Fatalf("合法写法被误报: %v", bad)
	}
}
