package service

import (
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/httpx"
)

// QuotaHandler 处理 `/me/quota` 与 `/me/usage`（docs/02-§6.2）。
type QuotaHandler struct{ svc *biz.QuotaService }

// NewQuotaHandler 构造 handler。
func NewQuotaHandler(svc *biz.QuotaService) *QuotaHandler { return &QuotaHandler{svc: svc} }

// QuotaPeriodResponse 是配额周期（契约里是 `period.start` / `period.end`）。
type QuotaPeriodResponse struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

// QuotaMetricResponse 是单个指标的配额快照（docs/02-§6.2 的 `metrics[]`）。
// `Limit` 用指针：内部用 `0` 表示「不限量」，直接输出 `0` 会让客户端显示「剩余 0 次」，
// 用户以为自己被禁用了 —— 不限量必须输出 `null`。
type QuotaMetricResponse struct {
	Metric    string  `json:"metric"`
	Limit     *int64  `json:"limit"`
	Used      int64   `json:"used"`
	Remaining int64   `json:"remaining"`
	Unit      string  `json:"unit"`
	ResetAt   *string `json:"reset_at"`
}

// QuotaResponse 是 `GET /me/quota` 的响应体。
type QuotaResponse struct {
	Plan    string                `json:"plan"`
	Period  QuotaPeriodResponse   `json:"period"`
	Metrics []QuotaMetricResponse `json:"metrics"`
}

// ToQuotaResponse 把 biz 结果映射成契约视图。
func ToQuotaResponse(o *biz.QuotaOverview) *QuotaResponse {
	if o == nil {
		return nil
	}
	metrics := make([]QuotaMetricResponse, 0, len(o.Metrics))
	for _, m := range o.Metrics {
		item := QuotaMetricResponse{
			Metric:    m.Metric,
			Used:      m.Used,
			Remaining: m.Remaining,
			Unit:      m.Unit,
			ResetAt:   clockx.FormatPtr(m.ResetAt),
		}
		if m.Limit > 0 {
			limit := m.Limit
			item.Limit = &limit
		}
		metrics = append(metrics, item)
	}
	return &QuotaResponse{
		Plan:    o.Plan,
		Period:  QuotaPeriodResponse{Start: clockx.Format(o.Period.Start), End: clockx.Format(o.Period.End)},
		Metrics: metrics,
	}
}

// Quota 处理 `GET /me/quota`。
func (h *QuotaHandler) Quota(c *gin.Context) {
	overview, err := h.svc.Overview(c.Request.Context(), middleware.UserID(c))
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToQuotaResponse(overview))
}

// UsageItemResponse 是用量明细的一行。
// `trace_id` 原样带出：排障时「这一笔用量对应哪条链路」只能从这里拿，
// 而 amount 是 AI 上报的真实值，对不上时就要顺着 trace 查回去。
type UsageItemResponse struct {
	ID             int64   `json:"id"`
	Metric         string  `json:"metric"`
	Amount         int64   `json:"amount"`
	ConversationID *string `json:"conversation_id"`
	MessageID      *string `json:"message_id"`
	TraceID        *string `json:"trace_id"`
	CreatedAt      string  `json:"created_at"`
}

// UsageTotalsResponse 是区间汇总。
type UsageTotalsResponse struct {
	ChatRequests int64 `json:"chat_requests"`
	LLMTokens    int64 `json:"llm_tokens"`
}

// UsageResponse 是 `GET /me/usage` 的响应体。
type UsageResponse struct {
	From   string              `json:"from"`
	To     string              `json:"to"`
	Metric *string             `json:"metric"`
	Items  []UsageItemResponse `json:"items"`
	Totals UsageTotalsResponse `json:"totals"`
}

// ToUsageResponse 把 biz 结果映射成契约视图。
func ToUsageResponse(r *biz.UsageReport) *UsageResponse {
	if r == nil {
		return nil
	}
	items := make([]UsageItemResponse, 0, len(r.Items))
	for i := range r.Items {
		e := &r.Items[i]
		items = append(items, UsageItemResponse{
			ID:             e.ID,
			Metric:         e.Metric,
			Amount:         e.Amount,
			ConversationID: optStr(e.Ref.ConversationID),
			MessageID:      optStr(e.Ref.MessageID),
			TraceID:        optStr(e.Ref.TraceID),
			CreatedAt:      clockx.Format(e.CreatedAt),
		})
	}
	resp := &UsageResponse{
		From:  clockx.Format(r.From),
		To:    clockx.Format(r.To),
		Items: items,
		Totals: UsageTotalsResponse{
			ChatRequests: r.Totals.ChatRequests,
			LLMTokens:    r.Totals.LLMTokens,
		},
	}
	resp.Metric = optStr(r.Metric)
	return resp
}

// Usage 处理 `GET /me/usage`。
// 契约没规定缺省区间，故缺省给「最近 24 小时」—— 它恰好是日配额周期，
// 于是用户看到的总数与 `/me/quota` 里的 `used` 对得上。
func (h *QuotaHandler) Usage(c *gin.Context) {
	now := clockx.Truncate(time.Now())
	to := now
	from := now.Add(-24 * time.Hour)

	var fields []errs.FieldError
	if raw := c.Query("from"); raw != "" {
		t, err := clockx.Parse(raw)
		if err != nil {
			fields = append(fields, errs.FieldError{
				Field: "from", Reason: "invalid_format", Message: "from 必须是 RFC3339 时间（如 2026-09-28T00:00:00.000Z）",
			})
		} else {
			from = t
		}
	}
	if raw := c.Query("to"); raw != "" {
		t, err := clockx.Parse(raw)
		if err != nil {
			fields = append(fields, errs.FieldError{
				Field: "to", Reason: "invalid_format", Message: "to 必须是 RFC3339 时间（如 2026-09-28T00:00:00.000Z）",
			})
		} else {
			to = t
		}
	}
	if len(fields) > 0 {
		httpx.Fail(c, errs.New(errs.CodeInvalidArgument).
			WithMessage("查询参数不合法").
			WithDetail("fields", fields))
		return
	}

	limit := biz.PageLimitDefault
	if raw := c.Query("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > biz.PageLimitMax {
			// 超上限 MUST 报错而不是静默截断（REQ-AUTH-010）。
			httpx.Fail(c, errs.New(errs.CodeInvalidArgument).
				WithMessage("limit 必须在 1.."+strconv.Itoa(biz.PageLimitMax)+" 之间").
				WithDetail("field", "limit").
				WithDetail("max", biz.PageLimitMax))
			return
		}
		limit = n
	}

	report, err := h.svc.Usage(c.Request.Context(), middleware.UserID(c), from, to, c.Query("metric"), limit)
	if err != nil {
		httpx.Fail(c, err)
		return
	}
	httpx.OK(c, ToUsageResponse(report))
}

// optStr 把空串映射成 nil（契约里这些字段是 `null` 而不是 `""`）。
func optStr(v string) *string {
	if v == "" {
		return nil
	}
	return &v
}
