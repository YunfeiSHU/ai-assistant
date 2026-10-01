package service

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
)

// AIProbe 探测 AI 侧可达性（M3 接入真实客户端后注入）。
//
// 返回 (是否可达, 耗时毫秒, 错误描述)。
type AIProbe func(ctx context.Context) (bool, int64, string)

// CircuitStatus 报告熔断器当前状态（docs/06-§5.4 的 `checks.circuit_breaker`）。
//
// 只暴露「目标 + 可读状态 + 剩余冷却」三个字段，而不是把 `biz` 的具体快照
// 类型透出来：规范 §四的依赖方向是 service → biz，但把一个**数据载体**类型
// 直接塞进 HTTP 响应，会让「改熔断器内部字段」变成「改对外契约」。
// `state` 用可读名（`closed`/`half_open`/`open`），与 docs/06-§5.4 的响应示例
// 逐字对应 —— 面板与排障脚本按字符串消费，数字（0/1/2）留给 Prometheus 指标。
type CircuitStatus struct {
	Target            string
	State             string
	RetryAfterSeconds int
}

// HealthHandler 处理 `/health*`（docs/06-§5.4）。
type HealthHandler struct {
	cfg     *conf.Config
	db      SchemaChecker
	tables  []string
	rdb     RedisPinger
	aiProbe AIProbe
	circuit func() CircuitStatus
	started time.Time
}

// NewHealthHandler 构造 handler。
//
// `tables` 由装配层传入（cmd 给 data.ExpectedTables）：期望的表清单是
// 数据层的事实，service 不应该 import data 去拿到它（规范 §四）。
//
// `circuit` 为 nil 时 `checks.circuit_breaker` 退化为 `{"state":"disabled"}`
// 而不是整个字段消失：docs/06-§5.4 把该字段列为**接口的一部分**，
// 字段缺失会让按字段消费的面板/脚本拿到 undefined，而 `disabled` 是确定且可判的。
func NewHealthHandler(cfg *conf.Config, db SchemaChecker, tables []string, rdb RedisPinger, probe AIProbe, circuit func() CircuitStatus) *HealthHandler {
	return &HealthHandler{cfg: cfg, db: db, tables: tables, rdb: rdb, aiProbe: probe, circuit: circuit, started: clockx.Now()}
}

// Live 处理 `GET /health/live`：只要进程在跑就 200（恒 200）。
func (h *HealthHandler) Live(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"status":  "ok",
		"service": h.cfg.Observ.OTELServiceName,
		"version": h.cfg.App.Version,
	})
}

// Ready 处理 `GET /health/ready`：MySQL 连通 + 表齐全 + Redis 连通。
//
// ⚠️ MUST NOT 把 AI 可达性算进来（docs/06-§3）：AI 挂着时网关仍应接流量
// （会话、历史、配额都还能用）。把 AI 状态当作就绪条件会造成
// 「AI 抖动 → K8s 摘除全部网关 → 全体不可用」的错误放大。
//
// 同理不检查 Redis 的**功能**而只查连通：Redis 挂了只是变慢
// （权威在 MySQL），不该停止接流量。
func (h *HealthHandler) Ready(c *gin.Context) {
	checks := gin.H{}
	okAll := true

	mysqlCheck := gin.H{"ok": false}
	start := time.Now()
	if h.db == nil {
		mysqlCheck["error"] = "db 未初始化"
		okAll = false
	} else {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
		defer cancel()
		if err := h.db.Ping(ctx); err != nil {
			mysqlCheck["error"] = err.Error()
			okAll = false
		} else if err := h.db.VerifyTables(ctx, h.tables); err != nil {
			// 「连上了」≠「表建对了」：这里才是真正的就绪判据。
			mysqlCheck["error"] = err.Error()
			okAll = false
		} else {
			mysqlCheck["ok"] = true
		}
	}
	mysqlCheck["latency_ms"] = time.Since(start).Milliseconds()
	mysqlCheck["schema_version"] = SchemaVersion
	checks["mysql"] = mysqlCheck

	redisCheck := gin.H{"ok": false}
	start = time.Now()
	if h.rdb == nil {
		redisCheck["error"] = "redis 未初始化"
		okAll = false
	} else {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
		defer cancel()
		if err := h.rdb.Ping(ctx); err != nil {
			redisCheck["error"] = err.Error()
			okAll = false
		} else {
			redisCheck["ok"] = true
		}
	}
	redisCheck["latency_ms"] = time.Since(start).Milliseconds()
	checks["redis"] = redisCheck

	status := http.StatusOK
	statusText := "ok"
	if !okAll {
		status = http.StatusServiceUnavailable
		statusText = "unavailable"
	}
	c.JSON(status, gin.H{
		"status":  statusText,
		"service": h.cfg.Observ.OTELServiceName,
		"version": h.cfg.App.Version,
		"commit":  h.cfg.App.Commit,
		"checks":  checks,
	})
}

// Health 处理 `GET /health`：综合信息，**恒 200**（只作为信息展示）。
func (h *HealthHandler) Health(c *gin.Context) {
	checks := gin.H{}

	mysqlCheck := gin.H{"ok": false}
	start := time.Now()
	if h.db != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
		defer cancel()
		if err := h.db.VerifyTables(ctx, h.tables); err != nil {
			mysqlCheck["error"] = err.Error()
		} else {
			mysqlCheck["ok"] = true
		}
	} else {
		mysqlCheck["error"] = "db 未初始化"
	}
	mysqlCheck["latency_ms"] = time.Since(start).Milliseconds()
	mysqlCheck["schema_version"] = SchemaVersion
	checks["mysql"] = mysqlCheck

	redisCheck := gin.H{"ok": false}
	start = time.Now()
	if h.rdb != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
		defer cancel()
		if err := h.rdb.Ping(ctx); err != nil {
			redisCheck["error"] = err.Error()
		} else {
			redisCheck["ok"] = true
		}
	} else {
		redisCheck["error"] = "redis 未初始化"
	}
	redisCheck["latency_ms"] = time.Since(start).Milliseconds()
	checks["redis"] = redisCheck

	// AI 可达性只作信息字段，**不影响状态码**。
	aiCheck := gin.H{"ok": false, "configured": h.aiProbe != nil}
	if h.aiProbe != nil {
		ctx, cancel := context.WithTimeout(c.Request.Context(), h.cfg.AI.ConnectTimeout)
		defer cancel()
		ok, latency, errMsg := h.aiProbe(ctx)
		aiCheck["ok"] = ok
		aiCheck["latency_ms"] = latency
		if errMsg != "" {
			aiCheck["error"] = errMsg
		}
	} else {
		aiCheck["note"] = "AI 探针未接入"
	}
	checks["ai_platform"] = aiCheck

	// 熔断状态：**仅信息**，不影响状态码（docs/06-§5.4 明确「熔断打开」不等于
	// 网关不健康 —— 网关此时恰好是在**正常工作**：它拒绝调用已经挂掉的上游，
	// 既保护客户端不被 3 秒连接超时拖住，也保护上游不被重试打垮）。
	if h.circuit == nil {
		checks["circuit_breaker"] = gin.H{
			"state": "disabled",
			"note":  "未启用熔断（CB_FAILURE_THRESHOLD 或 CB_OPEN_SECONDS 配为 0）",
		}
	} else {
		cs := h.circuit()
		cbCheck := gin.H{"target": cs.Target, "state": cs.State}
		if cs.RetryAfterSeconds > 0 {
			cbCheck["retry_after_seconds"] = cs.RetryAfterSeconds
		}
		checks["circuit_breaker"] = cbCheck
	}

	statusText := "ok"
	if ok, _ := aiCheck["ok"].(bool); !ok {
		statusText = "degraded"
	}
	if ok, _ := mysqlCheck["ok"].(bool); !ok {
		statusText = "degraded"
	}
	if ok, _ := redisCheck["ok"].(bool); !ok {
		statusText = "degraded"
	}

	c.JSON(http.StatusOK, gin.H{
		"status":         statusText,
		"service":        h.cfg.Observ.OTELServiceName,
		"version":        h.cfg.App.Version,
		"commit":         h.cfg.App.Commit,
		"uptime_seconds": int64(time.Since(h.started).Seconds()),
		"checks":         checks,
	})
}
