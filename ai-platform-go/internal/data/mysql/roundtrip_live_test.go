package mysql

import (
	"context"
	"log/slog"
	"os"
	"testing"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
)

// liveMySQLDSN 返回真机 MySQL 的 DSN；未显式开启时整组用例跳过。
//
// 门禁不默认开：这些用例量的是**服务端与本机之间的往返成本**，
// 属于环境事实而不是代码性质 —— 假实现替不掉，但它给出的数字
// 是 docs/09 里「为什么一条 SQL 要 8~9ms」的唯一直接证据。
//
// 本地用法：
//
//	$env:GW_LIVE_MYSQL=$env:MYSQL_DSN
//	go test ./internal/data/mysql/ -run TestLiveRoundTripCost -count=1 -v
func liveMySQLDSN(t *testing.T) string {
	t.Helper()
	dsn := os.Getenv("GW_LIVE_MYSQL")
	if dsn == "" {
		t.Skip("未设置 GW_LIVE_MYSQL，跳过错真机的 MySQL 用例")
	}
	return dsn
}

func TestLiveRoundTripCost(t *testing.T) {
	db, err := Open(conf.MySQL{DSN: liveMySQLDSN(t)}, slog.Default())
	if err != nil {
		t.Fatalf("打开 MySQL 失败：%v", err)
	}
	defer func() { _ = db.SQL.Close() }()

	ctx := context.Background()
	sqlDB := db.SQL

	// 看门狗：本用例的失败模式是「连接被未读完的结果集占住 → 永久阻塞」，
	// 在 `go test` 默认超时下只能表现为「跑了十几分钟被杀」，而且杀完报出的
	// 包耗时是错的（实测 **36254s**），完全指不回是哪一行。
	// 超时后**强制关池**：阻塞中的查询会立刻拿到错误返回，于是挂死变成一条
	// 具名断言失败，而不是一段没有输出的等待。
	//（同一个套路在 `internal/biz/circuit_test.go` 里用于熔断自死锁。）
	const watchdog = 90 * time.Second
	wd := time.AfterFunc(watchdog, func() {
		t.Errorf("%s 内未跑完：多半是某条查询把自己阻塞住了（连接被未读完的结果集占住）", watchdog)
		_ = sqlDB.Close()
	})
	defer wd.Stop()

	// 1) 纯往返地板：同一条连接上连续 SELECT 1。
	//    `SELECT 1` 服务端零 IO，所以它量出来的就是
	//    「本机 → MySQL 一个来回」的成本下限，与表大小、索引都无关。
	const n = 200
	conn, err := sqlDB.Conn(ctx)
	if err != nil {
		t.Fatalf("取连接失败：%v", err)
	}
	defer func() { _ = conn.Close() }()

	// 预热一次，避免把建连/握手算进第一条。
	//
	// ⚠️ 预热的结果集**必须读完并关闭**。`QueryContext` 返回的 `*sql.Rows` 一旦被
	// 直接丢弃，这条连接就被 driver 标成「结果集未结束」，同一连接上的下一次
	// 查询会卡在写锁上**永久阻塞**（日志里只有一行 `busy buffer`）。
	// 实测该写法的后果是 `*** Test killed: ran too long (11m0s)` —— 不是报错，
	// 是静默等到超时。
	warmup, err := conn.QueryContext(ctx, "SELECT 1")
	if err != nil {
		t.Fatalf("预热失败：%v", err)
	}
	if err := warmup.Close(); err != nil {
		t.Fatalf("预热后关闭结果集失败：%v", err)
	}

	start := time.Now()
	for i := 0; i < n; i++ {
		rows, err := conn.QueryContext(ctx, "SELECT 1")
		if err != nil {
			t.Fatalf("第 %d 次查询失败：%v", i, err)
		}
		for rows.Next() {
			var x int
			if err := rows.Scan(&x); err != nil {
				t.Fatalf("扫描失败：%v", err)
			}
		}
		if err := rows.Close(); err != nil {
			t.Fatalf("关闭结果集失败：%v", err)
		}
	}
	perPlain := time.Since(start) / n
	t.Logf("SELECT 1（同连接，%d 次）每次 %8.2f ms", n, ms(perPlain))

	// 2) 经连接池取还的往返（GORM 的默认走法）。
	//    差别就是「从池里拿一条连接」的开销 —— 高并发下这一项会放大。
	start = time.Now()
	for i := 0; i < n; i++ {
		var x int
		if err := sqlDB.QueryRowContext(ctx, "SELECT 1").Scan(&x); err != nil {
			t.Fatalf("池化查询失败：%v", err)
		}
	}
	perPool := time.Since(start) / n
	t.Logf("SELECT 1（经连接池，%d 次）每次 %8.2f ms", n, ms(perPool))

	// 3) 自动事务的代价：GORM 默认每条写都包 BEGIN+COMMIT。
	//    这里用显式事务模拟同一件事（不写业务表，避免污染台账）。
	start = time.Now()
	for i := 0; i < n; i++ {
		tx, err := sqlDB.BeginTx(ctx, nil)
		if err != nil {
			t.Fatalf("开事务失败：%v", err)
		}
		if _, err := tx.ExecContext(ctx, "SELECT 1"); err != nil {
			_ = tx.Rollback()
			t.Fatalf("事务内查询失败：%v", err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatalf("提交失败：%v", err)
		}
	}
	perTx := time.Since(start) / n
	t.Logf("BEGIN+SELECT+COMMIT（%d 次）每次 %8.2f ms", n, ms(perTx))

	t.Logf("倍率：池化/直连 = %.2fx，事务/直连 = %.2fx", perPool.Seconds()/perPlain.Seconds(), perTx.Seconds()/perPlain.Seconds())
}

// TestLiveStatementChoicePlan 顺带确认「按主键查」确实是主键点查，
// 而不是退化成全表扫 —— 解释「为什么同样的往返数，有的接口快有的慢」。
func TestLiveStatementChoicePlan(t *testing.T) {
	db, err := Open(conf.MySQL{DSN: liveMySQLDSN(t)}, slog.Default())
	if err != nil {
		t.Fatalf("打开 MySQL 失败：%v", err)
	}
	defer func() { _ = db.SQL.Close() }()

	ctx := context.Background()
	rows, err := db.SQL.QueryContext(ctx, "EXPLAIN SELECT * FROM `conversation` WHERE id = ? LIMIT 1", "conv_nonexistent")
	if err != nil {
		t.Fatalf("EXPLAIN 失败：%v", err)
	}
	defer func() { _ = rows.Close() }()

	cols, err := rows.Columns()
	if err != nil {
		t.Fatalf("取列名失败：%v", err)
	}
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			t.Fatalf("扫描 EXPLAIN 失败：%v", err)
		}
		for i, c := range cols {
			var v string
			switch x := vals[i].(type) {
			case nil:
				v = "NULL"
			case []byte:
				v = string(x)
			case string:
				v = x
			default:
				v = "?"
			}
			if c == "type" || c == "key" || c == "rows" || c == "table" {
				t.Logf("  %-6s = %s", c, v)
			}
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历 EXPLAIN 失败：%v", err)
	}
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }
