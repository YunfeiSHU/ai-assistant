package mysql

import (
	"context"
	"errors"
	"log/slog"
	"regexp"
	"time"

	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
)

// gormSlogLogger 把 GORM 的日志接到 slog，并做两件事：超过 slow 记 WARN（docs/05-§5：> 200ms）；
// 把 SQL 指纹化，日志里只留结构、不留参数值（REQ-NFR-005）。
//
// 第二点是必须的：GORM 默认 logger 走 `ExplainSQL` 把参数插值回 SQL 串，
// 用户邮箱、消息正文会直接进日志 —— 那正是 docs/06-§4.4 明令禁止的。
type gormSlogLogger struct {
	log  *slog.Logger
	slow time.Duration
}

func newGormLogger(log *slog.Logger, slow time.Duration) gormlogger.Interface {
	if log == nil {
		log = slog.Default()
	}
	if slow <= 0 {
		slow = 200 * time.Millisecond
	}
	return &gormSlogLogger{log: log, slow: slow}
}

// LogMode 实现 gormlogger.Interface（本项目日志级别完全由 slog 控制，因此原样返回）。
func (g *gormSlogLogger) LogMode(gormlogger.LogLevel) gormlogger.Interface { return g }

// Info 实现 gormlogger.Interface。
func (g *gormSlogLogger) Info(_ context.Context, msg string, args ...any) {
	g.log.Debug(msg, append([]any{slog.String("component", "gorm")}, args...)...)
}

// Warn 实现 gormlogger.Interface。
func (g *gormSlogLogger) Warn(_ context.Context, msg string, args ...any) {
	g.log.Warn(msg, append([]any{slog.String("component", "gorm")}, args...)...)
}

// Error 实现 gormlogger.Interface。
func (g *gormSlogLogger) Error(_ context.Context, msg string, args ...any) {
	g.log.Error(msg, append([]any{slog.String("component", "gorm")}, args...)...)
}

// Trace 实现 gormlogger.Interface。
func (g *gormSlogLogger) Trace(ctx context.Context, begin time.Time, fc func() (string, int64), err error) {
	elapsed := time.Since(begin)
	sql, rows := fc()
	fields := []any{
		slog.String("component", "gorm"),
		slog.String("sql", fingerprintSQL(sql)),
		slog.Int64("rows", rows),
		slog.Float64("elapsed_ms", float64(elapsed.Microseconds())/1000.0),
	}
	switch {
	case err != nil && !errors.Is(err, gorm.ErrRecordNotFound):
		g.log.ErrorContext(ctx, "db.error", append(fields, slog.String("error", err.Error()))...)
	case elapsed > g.slow:
		g.log.WarnContext(ctx, "db.slow_query", fields...)
	default:
		g.log.DebugContext(ctx, "db.query", fields...)
	}
}

var (
	// 单引号字符串字面量（含转义）→ `?`
	sqlStringLiteral = regexp.MustCompile(`'(?:[^'\\]|\\.|'')*'`)
	// 十六进制字面量 x'...' / 0x... → `?`
	sqlHexLiteral = regexp.MustCompile(`(?i)\b(?:0x[0-9a-f]+|x'[0-9a-f]*')`)
	// 裸数字 → `?`
	sqlNumberLiteral = regexp.MustCompile(`\b\d+(?:\.\d+)?\b`)
	// 连续空白压平，避免日志换行
	sqlWhitespace = regexp.MustCompile(`\s+`)
)

// fingerprintSQL 把 SQL 里的参数值替换成 `?`，得到「结构相同、值无关」的指纹。
//
// 顺序有讲究：必须先处理字符串与十六进制（它们内部可能含数字），
// 再处理裸数字，否则 `'a1'` 会先被数字规则打碎。
func fingerprintSQL(sql string) string {
	s := sqlStringLiteral.ReplaceAllString(sql, "?")
	s = sqlHexLiteral.ReplaceAllString(s, "?")
	s = sqlNumberLiteral.ReplaceAllString(s, "?")
	return sqlWhitespace.ReplaceAllString(s, " ")
}
