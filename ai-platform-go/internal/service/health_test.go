package service

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

// TestHealthIncludesCircuitBreakerCheck 守住 `/health` 的 `checks.circuit_breaker`。
//
// 历史缺口（2026-09-30 真机验收实测）：`Health` 只填了 mysql/redis/ai_platform 三项，
// 而 docs/06-§5.4 的端点表把它列为该接口的**组成部分**（连响应示例里都有
// `"circuit_breaker": {"target": "ai-platform", "state": "open"}`）。
// 字段缺失时按字段消费的面板/脚本拿到 undefined —— 而「熔断打开」正是
// docs/06-§5.5 的 P2 告警条件，排障第一步就要看它。
//
// 这条用例同时钉住两个容易做错的点：
//
//  1. **字段恒在**。`circuit` 为 nil（显式关闭熔断）时必须退化成
//     `state=disabled`，而不是让整个 key 消失。
//  2. **不影响状态码**。「熔断打开」不等于网关不健康 —— 它此刻恰好是在
//     正常工作。docs/06-§5.4 明写「仅信息，不影响状态码」，
//     把 open 当成 503 会让熔断期间的健康检查把实例从 LB 里摘掉，
//     于是「拒绝了挂掉的上游」被理解成「自己也挂了」。
func TestHealthIncludesCircuitBreakerCheck(t *testing.T) {
	gin.SetMode(gin.TestMode)

	cases := []struct {
		name       string
		provider   func() CircuitStatus
		wantState  string
		wantTarget string
		wantRetry  bool
	}{
		{
			name:      "熔断关闭",
			provider:  func() CircuitStatus { return CircuitStatus{Target: "ai-platform", State: "closed"} },
			wantState: "closed", wantTarget: "ai-platform",
		},
		{
			name: "熔断打开（带剩余冷却）",
			provider: func() CircuitStatus {
				return CircuitStatus{Target: "ai-platform", State: "open", RetryAfterSeconds: 17}
			},
			wantState: "open", wantTarget: "ai-platform", wantRetry: true,
		},
		{
			name:      "未启用熔断",
			provider:  nil,
			wantState: "disabled", wantTarget: "",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// 只验 `/health`：它的 200 是**恒定的**，因此不必准备 DB/Redis 依赖。
			h := &HealthHandler{
				cfg:     &conf.Config{},
				db:      nil,
				rdb:     nil,
				circuit: tc.provider,
				started: clockx.Now(),
			}
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodGet, "/health", nil)
			h.Health(c)

			if rec.Code != http.StatusOK {
				t.Fatalf("熔断状态（%s）不得影响状态码，实际 %d", tc.wantState, rec.Code)
			}

			var body map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("响应不是 JSON：%v（body=%s）", err, rec.Body.String())
			}
			checks, ok := body["checks"].(map[string]any)
			if !ok {
				t.Fatalf("响应缺少 checks 对象：%s", rec.Body.String())
			}
			cb, ok := checks["circuit_breaker"].(map[string]any)
			if !ok {
				t.Fatalf("checks 缺少 circuit_breaker（docs/06-§5.4 把它列为接口的一部分）：%s", rec.Body.String())
			}
			if got := cb["state"]; got != tc.wantState {
				t.Errorf("circuit_breaker.state 期望 %q，实际 %v", tc.wantState, got)
			}
			if tc.wantTarget != "" {
				if got := cb["target"]; got != tc.wantTarget {
					t.Errorf("circuit_breaker.target 期望 %q，实际 %v", tc.wantTarget, got)
				}
			}
			// 冷却剩余只在打开时有意义：关闭态带上它会让客户端以为要等。
			_, hasRetry := cb["retry_after_seconds"]
			if hasRetry != tc.wantRetry {
				t.Errorf("retry_after_seconds 存在性期望 %v，实际 %v（body=%s）", tc.wantRetry, hasRetry, rec.Body.String())
			}
		})
	}
}
