package data

import (
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
)

// ErrInvalidJSON 表示 JSON 列里的内容不是预期结构。
//
// 这类错误**必须**报出来而不是静默忽略：一旦把 `{}` 读成空数组，
// 上层看到的是「没有引用来源」而不是「数据坏了」，排障会跑偏。
var ErrInvalidJSON = errors.New("data: JSON 列内容不合法")

// JSONList 映射语义为 `string[]` 的 JSON 列（conversation.kb_ids 等）。
//
// nil 与空切片都能写（写 NULL 或 `[]`），读回来时保持区分：
// 上层据此无法得知「没设置」还是「设成了空」，因此两边都按「空」处理。
type JSONList []string

// Value 实现 driver.Valuer。
func (l JSONList) Value() (driver.Value, error) {
	if l == nil {
		return nil, nil
	}
	// 用非 nil 的空切片编码成 `[]` 而不是 `null`：契约里该字段是数组。
	b, err := json.Marshal([]string(l))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	return string(b), nil
}

// Scan 实现 sql.Scanner。
func (l *JSONList) Scan(src any) error {
	if src == nil {
		*l = nil
		return nil
	}
	raw, err := jsonBytes(src)
	if err != nil {
		return err
	}
	if len(raw) == 0 {
		*l = nil
		return nil
	}
	var out []string
	if err := json.Unmarshal(raw, &out); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	*l = out
	return nil
}

// OrEmpty 返回非 nil 的切片（供 JSON 序列化时输出 `[]` 而不是 `null`）。
func (l JSONList) OrEmpty() []string {
	if l == nil {
		return []string{}
	}
	return []string(l)
}

// JSONMap 映射语义为 `map[string]string` 的 JSON 列（conversation.metadata）。
//
// 与 JSONRaw 的区别：metadata 的**键与值都有长度上限**（≤ 64 字符），
// 而校验只能对具体类型做 —— 存成不透明字符串就没法校验了。
type JSONMap map[string]string

// NewJSONMap 把 map 包成列值；nil 也输出 `{}`。
//
// 刻意不写 NULL：契约里 metadata 默认是 `{}`，写 NULL 会让读取侧的
// 「没设过」与「设成了空对象」变成两种状态，多出一处判空分支。
func NewJSONMap(m map[string]string) JSONMap {
	if m == nil {
		return JSONMap{}
	}
	return JSONMap(m)
}

// Value 实现 driver.Valuer。
func (m JSONMap) Value() (driver.Value, error) {
	if m == nil {
		return "{}", nil
	}
	b, err := json.Marshal(map[string]string(m))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	return string(b), nil
}

// Scan 实现 sql.Scanner。
func (m *JSONMap) Scan(src any) error {
	if src == nil {
		*m = JSONMap{}
		return nil
	}
	raw, err := jsonBytes(src)
	if err != nil {
		return err
	}
	if len(raw) == 0 {
		*m = JSONMap{}
		return nil
	}
	var out map[string]string
	if err := json.Unmarshal(raw, &out); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	*m = out
	return nil
}

// OrEmpty 返回非 nil 的 map（契约里 metadata 默认是 `{}`）。
func (m JSONMap) OrEmpty() map[string]string {
	if m == nil {
		return map[string]string{}
	}
	return m
}

// Usage 映射 message.usage 列（docs/03-§4.1）。
//
// 这不是「不透明 JSON」：网关要从它取 `total_tokens` 累加配额（接缝 J7），
// 因此必须强类型解析 —— 把字段名拼错会变成「配额永远不涨」。
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// Value 实现 driver.Valuer（指针接收者，nil 安全）。
func (u *Usage) Value() (driver.Value, error) {
	if u == nil {
		return nil, nil
	}
	b, err := json.Marshal(u)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	return string(b), nil
}

// Scan 实现 sql.Scanner。
func (u *Usage) Scan(src any) error {
	if src == nil {
		*u = Usage{}
		return nil
	}
	raw, err := jsonBytes(src)
	if err != nil {
		return err
	}
	if len(raw) == 0 {
		*u = Usage{}
		return nil
	}
	var out Usage
	if err := json.Unmarshal(raw, &out); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidJSON, err)
	}
	*u = out
	return nil
}

// IsZero 报告 usage 是否为空（全部字段为 0）。
//
// 用于「没有 usage 帧就不累加 token 配额」的判断：
// 上游可能返回 usage 帧但字段缺省，把它当成 0 会让配额白扣一次。
func (u *Usage) IsZero() bool {
	return u == nil || (u.PromptTokens == 0 && u.CompletionTokens == 0 && u.TotalTokens == 0)
}

// JSONRaw 是对「不透明 JSON」列的封装（网关不解释内容的那些）。
//
// refs / tool_calls / metadata / degraded_reasons 用它：
// 这些结构由 ai-platform 定义（docs/03-§3.1 / docs/06-§6），
// 网关只做原样存与原样取 —— 一旦网关开始解析它们，
// AI 侧改字段就会变成网关的故障。
type JSONRaw struct {
	// Raw 是原始 JSON 文本；空串表示「未设置」（写 NULL）。
	Raw string
	// Valid 为 false 时写 NULL。
	Valid bool
}

// Value 实现 driver.Valuer。
func (j JSONRaw) Value() (driver.Value, error) {
	if !j.Valid || j.Raw == "" {
		return nil, nil
	}
	if !json.Valid([]byte(j.Raw)) {
		return nil, fmt.Errorf("%w: 不是合法 JSON", ErrInvalidJSON)
	}
	return j.Raw, nil
}

// Scan 实现 sql.Scanner。
func (j *JSONRaw) Scan(src any) error {
	if src == nil {
		j.Raw, j.Valid = "", false
		return nil
	}
	raw, err := jsonBytes(src)
	if err != nil {
		return err
	}
	j.Raw, j.Valid = string(raw), len(raw) > 0
	return nil
}

// NewJSONRaw 把 JSON 文本包成 JSONRaw；空串或非法 JSON 会成为「未设置」。
func NewJSONRaw(text string) JSONRaw {
	if text == "" || !json.Valid([]byte(text)) {
		return JSONRaw{}
	}
	return JSONRaw{Raw: text, Valid: true}
}

// StringOrNil 返回便于直接写进响应体的值（未设置时为 nil）。
func (j JSONRaw) StringOrNil() *string {
	if !j.Valid {
		return nil
	}
	s := j.Raw
	return &s
}

func jsonBytes(src any) ([]byte, error) {
	switch v := src.(type) {
	case []byte:
		return v, nil
	case string:
		return []byte(v), nil
	default:
		return nil, fmt.Errorf("%w: 不支持的源类型 %T", ErrInvalidJSON, src)
	}
}
