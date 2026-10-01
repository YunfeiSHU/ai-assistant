// Package clockx 统一时间格式与时钟来源。
//
// 契约（docs/02-§1）：一律 RFC 3339 UTC 毫秒、`Z` 结尾。不用 time.RFC3339Nano：
// 它整秒时省略 `.000`，会让客户端严格解析器失败、两侧日志的字符串比较对不上。
package clockx

import "time"

// Layout 是契约规定的序列化格式（固定 3 位毫秒 + 字面量 Z）。
const Layout = "2006-01-02T15:04:05.000Z"

// DateLayout 是配额周期用的日期格式（YYYY-MM-DD）。
const DateLayout = "2006-01-02"

// Now 返回 UTC 当前时间，截断到毫秒（与 MySQL DATETIME(3) 精度一致）。
// 必须截断：纳秒写进 DATETIME(3) 会被四舍五入，「内存里的时间」与「库里的时间」
// 最多差 0.5ms，游标分页会把边界记录判错。
func Now() time.Time { return Truncate(time.Now()) }

// Truncate 把时刻转为 UTC 并截断到毫秒。
func Truncate(t time.Time) time.Time { return t.UTC().Truncate(time.Millisecond) }

// Format 按契约格式序列化。
func Format(t time.Time) string { return t.UTC().Format(Layout) }

// FormatPtr 序列化可空时间（nil 返回 nil，便于 JSON 输出 null）。
func FormatPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := Format(*t)
	return &s
}

// Parse 解析契约格式；同时兼容不带毫秒的 RFC3339（宽松读取，严格写出）。
func Parse(s string) (time.Time, error) {
	if t, err := time.Parse(Layout, s); err == nil {
		return Truncate(t), nil
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}, err
	}
	return Truncate(t), nil
}

// Date 返回 t 在 loc 时区下的 YYYY-MM-DD。
func Date(t time.Time, loc *time.Location) string { return t.In(loc).Format(DateLayout) }

// DayStart 返回 loc 时区下 t 所在自然日的 00:00:00（UTC 表示）。
func DayStart(t time.Time, loc *time.Location) time.Time {
	local := t.In(loc)
	return Truncate(time.Date(local.Year(), local.Month(), local.Day(), 0, 0, 0, 0, loc))
}

// NextDayStart 返回下一个自然日 00:00:00（配额 reset_at）。
func NextDayStart(t time.Time, loc *time.Location) time.Time {
	return DayStart(t, loc).AddDate(0, 0, 1)
}

// LoadLocation 加载配额时区；空串或加载失败回退 UTC。
//
// 配额时区来自配置（QUOTA_TIMEZONE），是运维可控值，因此加载失败只回退不报错；
// 启动期会由 conf 层做一次显式校验（见 conf.Validate）。
func LoadLocation(name string) *time.Location {
	if name == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC
	}
	return loc
}
