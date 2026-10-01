package service

import (
	"strconv"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// 本文件集中处理列表接口的查询参数（`limit` / `cursor` / `order` / 布尔过滤）。
//
// 为什么不复用 gin 的 `ShouldBindQuery` + `form:"..." binding:"lte=100"`：
//
//   - 它的错误信息是「Key: 'Limit' Error:Field validation for 'Limit' failed
//     on the 'lte' tag」，既不能直接展示给客户端，也没法填进契约要求的
//     `details.fields[]`（那需要 field + reason 两个值）；
//   - 它区分不了「没传 limit」与「传了 limit=0」—— 前者要用默认值 20，
//     后者必须报 400（REQ-AUTH-010 明确禁止静默截断）。
//
// 手写解析多几行，换来每一种失败都能给出机器可读的原因。

// paginationFrom 解析 `limit` 与 `cursor`。
//
// 只做「是不是整数」这一层判断，**范围校验（1..100）交给 biz**：
// 同一条规则写在两处，迟早会出现「一处改了另一处没改」。
func paginationFrom(c *gin.Context) (biz.PaginationInput, []errs.FieldError) {
	in := biz.PaginationInput{
		Limit:  biz.PageLimitDefault,
		Cursor: c.Query("cursor"),
	}
	var fields []errs.FieldError
	if raw := c.Query("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			fields = append(fields, errs.FieldError{
				Field: "limit", Reason: "invalid_type", Message: "limit 必须是整数",
			})
		} else {
			in.Limit = n
		}
	}
	return in, fields
}

// optionalBoolFrom 解析三态布尔：缺省表示「不过滤」。
//
// 用 `*bool` 而不是 bool：`?pinned=`（空串）与不传是两件事 ——
// 前者是明显的输入错误，后者是「不限」。把它们折叠成同一个值
// 会让 `?pinned=` 静默变成「不限」。
func optionalBoolFrom(c *gin.Context, name string) (*bool, []errs.FieldError) {
	raw := c.Query(name)
	if raw == "" {
		return nil, nil
	}
	v, err := strconv.ParseBool(raw)
	if err != nil {
		return nil, []errs.FieldError{{
			Field: name, Reason: "invalid_type", Message: name + " 只能是 true 或 false",
		}}
	}
	return &v, nil
}

// orderFrom 解析排序方向，缺省 `desc`（契约默认值，用于「加载更多」）。
func orderFrom(c *gin.Context) string {
	if raw := c.Query("order"); raw != "" {
		return raw
	}
	return biz.OrderDesc
}
