// Command server 是 go-services 的 HTTP/SSE 入口（代码生成规范.md §三.6）。
//
// 这里只做两件事：装配与生命周期。装配顺序即依赖顺序（配置 → 日志 → 数据层 →
// 鉴权 → 业务 → 传输 → 生命周期），任何一步失败都直接退出，不带着半可用的状态服务。
//
// 用显式装配而不是 wire 代码生成：依赖图是线性的（七步），引入 wire 只多一层生成物
// 与一个必须在 `go build` 前跑的步骤，收益不值。依赖图变复杂后再换。
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/biz"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/data"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/data/ai"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/data/redis"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/server/middleware"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/service"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/clockx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/jwtx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/logx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/metricsx"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/otelx"
)

// devJWTSecret 是仅供本地开发的兜底密钥（≥ 32 字节）。
// 生产环境下 conf.Validate 已强制要求 JWT_SECRET 非空，所以走到这里一定是非生产环境；
// 但即使如此也会打 WARN —— 「本地生成的令牌拿到别的环境用」是真实会发生的误操作。
const devJWTSecret = "go-services-local-development-secret-key-32bytes-min"

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "go-services 启动失败:", err)
		os.Exit(1)
	}
}

func run() error {
	// ---- 配置 ----
	cfg, err := conf.Load()
	if err != nil {
		return err
	}

	// ---- 日志 ----
	logger := logx.New(logx.Options{
		Level:   cfg.App.LogLevel,
		Format:  cfg.App.LogFormat,
		Service: cfg.Observ.OTELServiceName,
		Version: cfg.App.Version,
	})
	logger.Info("app.config_loaded", slog.Any("config", cfg.Redacted()))

	// ---- 追踪（M6，docs/06-§5.1）----
	//
	// MUST 在数据层之前初始化。`otelx.Init` 设置的是**全局** TracerProvider
	// 与 propagator，而 span 的创建与出站 `traceparent` 注入都从全局取。
	// 顺序反了的表现：网关日志里有 trace_id（那是 WithTrace 给的），
	// 但出站请求的 traceparent 是空的（全局 provider 还是 noop），
	// 于是 Jaeger 里 AI 侧那条 trace 与网关侧的分成两条 —— 两边日志都正常，
	// 只有链路图是断的，最难排查。
	//
	// 返回的错误只有「配置不合法」一种（端点写错、比例越界）；
	// 「连不上 Collector」不在这里失败，否则本地启动就得先起 Jaeger。
	tracer, err := otelx.Init(context.Background(), otelx.Config{
		Enabled:     cfg.Observ.OTELEnabled,
		Endpoint:    cfg.Observ.OTELExporterEndpoint,
		ServiceName: cfg.Observ.OTELServiceName,
		SamplerArg:  cfg.Observ.OTELTracesSamplerArg,
		Version:     cfg.App.Version,
		Commit:      cfg.App.Commit,
		Env:         cfg.App.Env,
		Log:         logger,
	})
	if err != nil {
		return fmt.Errorf("初始化 OTel 失败: %w", err)
	}
	defer func() {
		// 5 秒就够了：Shutdown 内部先 ForceFlush 再关导出器，
		// 给太长会让「信号到来但导出器卡住」拖长整个退出时间。
		otelCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if terr := tracer.Shutdown(otelCtx); terr != nil {
			logger.Warn("app.otel_shutdown_failed", slog.String("error", terr.Error()))
		}
	}()
	logger.Info("app.otel_ready",
		slog.Bool("enabled", tracer.Enabled()),
		slog.String("endpoint", cfg.Observ.OTELExporterEndpoint),
		slog.String("sampling", tracer.Sampling()),
	)

	// ---- 数据层 ----
	//
	// 一个 Open 把两个引擎都建起来：redis 的配置错误（URL 写错）必须在这里就报错，
	// 而「redis 服务没起来」属于运行期事件，下面 Ping 一下只告警不退出。
	d, err := data.Open(cfg, logger)
	if err != nil {
		return err
	}
	defer func() {
		if cerr := d.Close(); cerr != nil {
			logger.Warn("app.db_close_failed", slog.String("error", cerr.Error()))
		}
	}()

	startupCtx, cancelStartup := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelStartup()
	if err := d.VerifyTables(startupCtx, data.ExpectedTables); err != nil {
		return fmt.Errorf("%w\n提示：按顺序执行 ai-platform/deploy/mysql/001_init_schema.sql "+
			"与 ai-platform-go/deploy/mysql/001_gateway_tables.sql", err)
	}
	if err := d.Redis.Ping(startupCtx); err != nil {
		// Redis 挂了不阻止启动（docs/04-§9：只损失配额精度与缓存命中率），
		// 但必须显式告警 —— 静默降级会让「配额在多实例下超发」变成既成事实。
		logger.Warn("app.redis_unavailable_degraded_mode", slog.String("error", err.Error()))
	}

	// ---- 鉴权 ----
	signer, err := newSigner(cfg, logger)
	if err != nil {
		return err
	}

	// ---- 仓储 ----
	//
	// 构造函数返回 biz 定义的接口：main 只依赖契约，
	// 换实现（换库、加缓存）不需要改这里（规范 §六）。
	users := data.NewUserRepo(d)
	tokens := data.NewTokenRepo(d)
	audit := data.NewAuditRepo(d)
	conversations := data.NewConversationRepo(d)
	messages := data.NewMessageRepo(d)
	idemStore := data.NewIdemRepo(d)
	versionChecker := redis.NewTokenVersionChecker(users, d.Redis, logger)

	// ---- AI 客户端（网关 → ai-platform）----
	//
	// 两种传输并存（docs/04-§2.1 的 `REQ-ORCH-002` 第 3 条要求保留 HTTP 通道）：
	//
	//	`Chat`                gRPC（Kratos）—— `AI_GRPC_ENABLED=true`
	//	KB/文档/任务/上下文    HTTP 透传   —— 永远启用（AI 侧只有 HTTP 实现）
	//
	// 两条 HTTP 路径的**共同前提**：两侧的 API 前缀同名（都是 `/api/v1`）。
	// 透传把收到的完整路径原样转发（见 service.ProxyHandler 的 upstreamPath），
	// 所以网关前缀一旦与 AI 不同名就会变成 404 —— 是「响亮地错」而不是「静默地错」。
	aiOpts := ai.FromConfig(cfg, logger)
	var orchestrator biz.ChatOrchestrator
	var streamer biz.ChatStreamer
	if cfg.AI.GRPCEnabled {
		// 非流式与流式共用**同一条** gRPC 连接：它们指向同一个上游
		// （见 ai.NewChatClients 的注释）。
		orch, st, aerr := ai.NewChatClients(context.Background(), aiOpts)
		if aerr != nil {
			return fmt.Errorf("初始化 ai-platform gRPC 客户端失败: %w", aerr)
		}
		orchestrator = orch
		streamer = st
		logger.Info("app.ai_grpc_ready", slog.String("target", cfg.AI.GRPCTarget))
	} else {
		// 关掉 gRPC 不是错误：HTTP 通道仍在（REQ-ORCH-002 第 3 条要求保留它）。
		// 但必须显式告警 —— 默认配置里它是开着的，走过来就说明有人改过。
		logger.Warn("app.ai_grpc_disabled_using_http",
			slog.String("hint", "AI_GRPC_ENABLED=false：Chat 走 HTTP 通道（POST {prefix}/chat）"),
			slog.String("base_url", cfg.AI.BaseURL),
		)
	}
	aiProxy := ai.NewProxy(aiOpts)
	if orchestrator == nil {
		orchestrator = ai.NewChatOrchestratorHTTP(aiProxy, cfg.App.APIPrefix, aiOpts)
	}
	if streamer == nil {
		// 流式的 HTTP 兜底通道（`/chat/stream`，SSE）。
		// 与非流式一样：关掉 gRPC 不是错误，但必须能跑。
		streamer = ai.NewChatStreamerHTTP(cfg.App.APIPrefix, aiOpts)
	}

	// 优雅退出的广播（docs/06-§3 第 ③ 步）。
	//
	// 必须在装配 `MessageDeps` **之前**建好：在途的流靠着它才能在
	// `http.Server.Shutdown` 到点切断之前，自己发一条
	// `error(SERVICE_SHUTTING_DOWN)` 并把已收到的正文落成 `partial`。
	// 被硬切断的流**什么都不会落库**（已经推给客户端的半截回答刷新后就没了）。
	streamShutdown := make(chan struct{})

	// ---- 指标与追踪（M6 的底座，M5 已开始使用）----
	//
	// **无条件**创建 `metricsx.New()`，即使 `METRICS_ENABLED=false`：
	// biz 里的埋点写作 `s.d.Metrics.QuotaExceeded(...)`，而一个**空接口**（nil）
	// 上调用方法会 panic（`*T` 的 nil 接收者才安全）。让 `Metrics` 字段永远
	// 持有一个真实的 `*metricsx.Metrics`，`METRICS_ENABLED` 只决定要不要
	// 起 `/metrics` 这个监听端口 —— 也就是说，关掉的是**暴露**而不是**采集**。
	mx := metricsx.New()
	var appMetrics biz.Metrics = mx

	// 版本与连接池指标：两类**不靠业务埋点**的观测面。
	//
	// 它们存在的理由是「网关内存/连接异常增长」这类现象没有业务指标能看到：
	// `gw_db_pool_wait_total` 涨 = 慢 SQL 把池占了，`gw_db_pool_open`
	// 贴着上限 = 要么调大，要么就是有连接泄漏。
	//
	// 用回调而不是定时 Set：GaugeFunc 只在被抓取时读一次，空闲时零开销。
	mx.SetBuildInfo(cfg.App.Version, cfg.App.Commit, runtime.Version())
	if d.DB != nil && d.DB.SQL != nil {
		sqlDB := d.DB.SQL
		mx.RegisterDBPool(func() metricsx.DBPoolStats {
			st := sqlDB.Stats()
			return metricsx.DBPoolStats{
				Open:      st.OpenConnections,
				InUse:     st.InUse,
				Idle:      st.Idle,
				Max:       st.MaxOpenConnections,
				WaitTotal: st.WaitCount,
			}
		})
	}
	if d.Redis != nil {
		mx.RegisterRedisPool(func() metricsx.RedisPoolStats {
			total, idle, stale := d.Redis.PoolStats()
			return metricsx.RedisPoolStats{TotalConns: total, IdleConns: idle, StaleConns: stale}
		})
	}

	// ---- 配额与限流（M5）----
	//
	// 三层：Redis 实现 → 降级包装（Redis 不可用时走进程内计数）→ biz 服务。
	// 降级包装放在 data 层而不是 biz：biz 只应看到「计数器」这一个概念，
	// 「Redis 挂了怎么办」是基础设施的实现细节（docs/04-§9）。
	quotaCounter := redis.NewDegradedQuotaCounter(redis.NewRedisQuotaCounter(d.Redis), logger)
	concurrencyLimiter := redis.NewDegradedConcurrencyLimiter(
		redis.NewRedisConcurrencyLimiter(redis.NewRedisQuotaCounter(d.Redis)), logger)
	rateLimiter := redis.NewDegradedRateLimiter(redis.NewRedisRateLimiter(d.Redis), logger)
	quotaStore := data.NewQuotaStore(d)

	quotaSvc := biz.NewQuotaService(biz.QuotaDeps{
		Repo:        quotaStore,
		Counter:     quotaCounter,
		Concurrency: concurrencyLimiter,
		Users:       users,
		Audit:       audit,
		PlanLimits:  cfg.Quota.PlanLimits,
		Timezone:    clockx.LoadLocation(cfg.Quota.Timezone),
		Clock:       time.Now,
		Log:         logger,
		Metrics:     appMetrics,
		// span 上的 user_id 一律用哈希（docs/06-§5.1）。
		// 不配盐时入口层会**不写**这个属性，而不是退化成明文。
		PIIHashSalt: cfg.Observ.PIIHashSalt,
	})
	rateSvc := biz.NewRateLimitService(biz.RateLimitDeps{
		Limiter: rateLimiter,
		Limits: biz.RateLimits{
			LoginPerMinute:      cfg.Rate.LoginPerMinute,
			LoginAccountPerHour: cfg.Rate.LoginAccountPerHour,
			MsgPerMinute:        cfg.Rate.MsgPerMinute,
			UploadPerMinute:     cfg.Rate.UploadPerMinute,
			ConvPerMinute:       cfg.Rate.ConvPerMinute,
			GlobalQPSLimit:      cfg.Rate.GlobalQPSLimit,
		},
		Metrics: appMetrics,
		Log:     logger,
		Clock:   time.Now,
	})

	// 熔断器（docs/04-§9、`REQ-ORCH-009`）：同一实例四处共享同一个 breaker ——
	// 分成多个会让「非流式连续失败」不会影响流式，而那正是它要防的场景。
	// 阈值配成非正数时返回 nil（显式关闭），后面的装饰器会原样跳过。
	breaker := biz.NewAICircuitBreaker("ai-platform",
		cfg.AI.CBFailureThreshold, time.Duration(cfg.AI.CBOpenSeconds)*time.Second,
		time.Now, appMetrics, logger)

	// ---- 保留期清理与每日对账（M6，docs/05-§6 与 docs/06-§5.2）----
	//
	// 一个后台循环干两件事：
	//
	//	每日 00:00  以 MySQL 台账为准重建 Redis 配额计数（Redis 丢数据后的自愈）
	//	每日 03:00  过期数据清理 + 孤儿行盘点（`SetOrphanRows`）
	//
	// 两件事共用**一个** service 而不是各起一个 goroutine：它们的触发时间
	// 都在夜间且都很短，分两个循环只会让「日志里同一晚两段噪音」多一份，
	// 却不会带来任何隔离收益（各自 panic 都不会拖垮对方，因为 tick 内部
	// 每步都单独 recover/记录）。
	retentionCtx, cancelRetention := context.WithCancel(context.Background())
	defer cancelRetention()
	retentionSvc := biz.NewRetentionService(biz.RetentionDeps{
		Repo:               data.NewRetentionRepo(d),
		Usage:              quotaSvc,
		Audit:              audit,
		Quota:              quotaSvc,
		Metrics:            appMetrics,
		Clock:              time.Now,
		Log:                logger,
		Timezone:           clockx.LoadLocation(cfg.Quota.Timezone),
		RetentionDays:      cfg.Internal.RetentionDays,
		UsageRetentionDays: cfg.Internal.UsageRetentionDays,
		// 整点必须显式传：`RetentionDeps` 的零值 `0` 是一个**合法**小时，
		// 不传的话「默认 3 点清理」会变成「0 点清理」，而且与重建撞在同一拍
		// （先把 Redis 按 MySQL 重建、紧接着又去清库，两步的中间态没人看过）。
		PurgeHour:     cfg.Internal.PurgeHour,
		RebuildHour:   cfg.Internal.RebuildHour,
		CheckInterval: cfg.Internal.CheckInterval,
	})
	go retentionSvc.Start(retentionCtx)

	// ---- 业务 ----
	argon := cryptox.Argon2Params{
		Memory:      uint32(cfg.Auth.Argon2MemoryMB) * 1024,
		Iterations:  uint32(cfg.Auth.Argon2Iterations),
		Parallelism: uint8(cfg.Auth.Argon2Parallelism),
		SaltLen:     16,
		KeyLen:      32,
	}
	authSvc := biz.NewAuthService(biz.AuthDeps{
		Users:      users,
		Tokens:     tokens,
		Audit:      audit,
		Signer:     signer,
		Argon:      argon,
		Policy:     biz.PasswordPolicy{MinLength: cfg.Auth.PasswordMinLength},
		AccessTTL:  cfg.Auth.AccessTokenTTL(),
		RefreshTTL: cfg.Auth.RefreshTokenTTL(),
		Log:        logger,
		// 递增 token_version 后必须清缓存（否则旧令牌还能再用一个 TTL）。
		Versions: versionChecker,
		Metrics:  appMetrics,
	})
	userSvc := biz.NewUserService(biz.UserDeps{Users: users, Log: logger})
	convSvc := biz.NewConversationService(biz.ConversationDeps{
		Conversations: conversations,
		Log:           logger,
	})
	msgSvc := biz.NewMessageService(biz.MessageDeps{
		Conversations: conversations,
		Messages:      messages,
		// 熔断装饰（S7 / `REQ-ORCH-009`）：
		//
		// 装饰在这里而不是包在 `ai.NewChatClients` 里：熔断的判据是
		// 「上游链路是否整体不可用」，与具体传输（gRPC / HTTP）无关。
		// 包在传输层就会得到两个各自计数的熔断器 —— 一边挂了另一边
		// 还能把请求发出去，而那正是要防的。
		Orchestrator: biz.NewCircuitChatOrchestrator(orchestrator, breaker, appMetrics),
		Streamer:     biz.NewCircuitChatStreamer(streamer, breaker, appMetrics),
		// 配额与并发限额（M5）：预扣 + 併发槽 + 用量提交全在 biz 内部，
		// 传输层不知道有配额这回事（它只负责把 429 写成契约信封）。
		Quota: quotaSvc,
		// 埋点口（M6）：会话/trace 不一致、落库失败、SSE 连接数都在
		// biz 内部打点，聚合与暴露在 pkg/metricsx。
		Metrics: appMetrics,
		// gRPC 通道建立失败 **不会** 降级成 HTTP —— 那是静默改变传输，
		// 上面已经直接返回错误退出（宁可起不来，也不要跑在一个没人知道的配置下）。
		// 流式编排（M4）：`POST .../messages/stream` 的事件循环、超时与
		// 「半截回答也要落库」全在 biz 里（docs/04-§5、docs/03-§5）。
		StreamFirstByteTimeout:   cfg.AI.FirstByteTimeout,
		StreamIdleTimeout:        cfg.AI.IdleTimeout,
		StreamTotalTimeout:       cfg.AI.TotalTimeout,
		StreamAccumulateMaxChars: cfg.AI.StreamAccumulateMaxChars,
		StreamAccumulateMaxItems: cfg.AI.RefAccumulateMaxItems,
		Shutdown:                 streamShutdown,
		Log:                      logger,
		AutoTitleMaxChars:        cfg.App.AutoTitleMaxChars,
		// `use_memory=false` 时网关补给 AI 的历史轮数（REQ-ORCH-006）。
		HistoryFallbackTurns: cfg.AI.HistoryFallbackTurns,
	})
	proxySvc := biz.NewAIProxyService(biz.AIProxyDeps{
		Proxy:         aiProxy,
		Conversations: conversations,
		// 上传时的存量额度预占（documents_count / storage_bytes）。
		Quota: quotaSvc,
		Log:   logger,
	})

	// ---- 传输 ----
	engine := server.NewEngine(server.Deps{
		Config:         cfg,
		Log:            logger,
		Signer:         signer,
		VersionChecker: versionChecker,

		Auth:      service.NewAuthHandler(authSvc),
		User:      service.NewUserHandler(userSvc),
		Quota:     service.NewQuotaHandler(quotaSvc),
		RateLimit: rateSvc,
		Metrics:   mx,
		// OTel Server span 的接入点。Provider 未启用时退化为 no-op，
		// 中间件依然挂在链上（这样开启 OTEL 只需要改配置重启）。
		Trace: middleware.TraceOptions{
			Provider:    tracer,
			ForceSample: cfg.App.Debug,
			PIIHashSalt: cfg.Observ.PIIHashSalt,
		},
		Conversations: service.NewConversationHandler(convSvc),
		Messages:      service.NewMessageHandler(msgSvc),
		Proxy:         service.NewProxyHandler(proxySvc),
		Upload:        service.NewUploadHandler(proxySvc, cfg.AI.UploadMaxMB),
		IdemStore:     idemStore,
		// 期望的表清单由装配层传入：service 不 import data（规范 §四）。
		Health: service.NewHealthHandler(cfg, d, data.ExpectedTables, d.Redis, aiHealthProbe(cfg, logger), circuitStatus(breaker)),
	})
	httpSrv := server.New(cfg, engine, logger)

	// /metrics 是**独立端口**（docs/06-§5.2）：主引擎是公网入口，
	// 把指标端点挂在上面意味着「白名单中间件写错」就变成公网可拉指标。
	// 分开后即使白名单写错，也可以通过防火墙/安全组彻底不暴露那个端口。
	//
	// 注意：`METRICS_ENABLED=false` 关掉的是**暴露**而不是**采集** ——
	// biz 里的埋点照旧写入内存 registry（业务埋点永远不该依赖观测开关）。
	var metricsSrv *server.MetricsServer
	if cfg.Observ.MetricsEnabled {
		ms, merr := server.NewMetricsServer(server.MetricsOptions{
			Addr:       fmt.Sprintf(":%d", cfg.Observ.MetricsPort),
			AllowCIDRs: cfg.Observ.MetricsAllowCIDRs,
			Handler:    mx.Handler(),
			Log:        logger,
		})
		if merr != nil {
			// 白名单配错属于配置错误，直接退出：
			// 「白名单写坏 → 谁都能访问」比「起不来」危险得多。
			return fmt.Errorf("初始化 /metrics 监听器失败: %w", merr)
		}
		if ms != nil {
			metricsSrv = ms
			if mErr := ms.Start(); mErr != nil {
				go func() {
					if err := <-mErr; err != nil {
						logger.Error("metrics.serve_failed", slog.String("error", err.Error()))
					}
				}()
			}
		}
	}

	// ---- 生命周期 ----
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() { errCh <- httpSrv.Start() }()

	select {
	case err := <-errCh:
		if err != nil {
			return err
		}
		return nil
	case <-ctx.Done():
		stop() // 收到第一个信号后恢复默认行为：再按一次 Ctrl-C 可强制退出
		logger.Info("app.signal_received", slog.String("signal", "shutdown"))
	}

	shutdownTimeout := time.Duration(cfg.App.GracefulShutdownSeconds) * time.Second
	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancelShutdown()

	// 先广播「要退出了」，再等 http.Server：顺序反过来时在途的流会在
	// 拿到通知之前就被切断（Shutdown 会等到超时才放弃，但那已经是 20s 之后）。
	close(streamShutdown)
	// 停掉后台循环（保留期清理/对账）与指标端口；它们与业务无关，
	// 不等它们完成，但必须显式取消 —— 否则「进程在等服务退出」
	// 与「服务在等后台任务」会互相等成 20 秒的超时。
	cancelRetention()
	if metricsSrv != nil {
		if err := metricsSrv.Shutdown(shutdownCtx); err != nil {
			logger.Warn("metrics.shutdown_failed", slog.String("error", err.Error()))
		}
	}
	// 优雅退出：等待在途请求完成（含流式）。
	// MUST NOT 直接 os.Exit —— 会丢掉已累积但未落库的回答（docs/06-§3）。
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		logger.Warn("app.shutdown_incomplete",
			slog.String("error", err.Error()),
			slog.Int("graceful_seconds", cfg.App.GracefulShutdownSeconds),
		)
	} else {
		logger.Info("app.shutdown_complete")
	}
	return nil
}

// aiHealthProbe 构造 `/health` 用的 AI 可达性探针（docs/06-§5.4）。
// 不接探针时 `checks.ai_platform` 恒为 `{"ok":false,"configured":false}`，于是 `/health`
// 的 `status` 永远是 `degraded` —— 一个恒定的字段等于没有字段，运维看到 degraded 会去查
// 一个其实好好的 AI。
//
// 用 HTTP 探活而不是 gRPC 通道状态：`/health` 要回答的是「AI 这套服务能不能用」，
// 而透传类接口走的是 HTTP；gRPC 侧也没有可无凭据调用的探活 RPC。
func aiHealthProbe(cfg *conf.Config, logger *slog.Logger) service.AIProbe {
	if strings.TrimSpace(cfg.AI.BaseURL) == "" {
		// AI 未配置（`AI_PLATFORM_BASE_URL` 为空）时**不**接探针：
		// 返回 nil 会让 `/health` 报 `configured=false`，
		// 那正是「这台实例没接 AI」的准确描述，比伪造一个「不可达」要好。
		logger.Info("app.ai_probe_disabled", slog.String("reason", "AI_PLATFORM_BASE_URL 未配置"))
		return nil
	}
	return ai.NewProbe(ai.Options{
		BaseURL:        cfg.AI.BaseURL,
		ConnectTimeout: cfg.AI.ConnectTimeout,
		Log:            logger,
	})
}

// circuitStatus 把熔断器适配成 `/health` 需要的只读视图（docs/06-§5.4）。
// 在装配层做转换而不是让 `biz` 直接实现 `service.CircuitStatus`：「状态码 0/1/2」
// 与「可读名 closed/half_open/open」是两个不同口径（前者给 Prometheus，后者给人和脚本），
// 转换放在装配点让两侧各管自己的口径，也让 service 不依赖 biz 的内部常量。
//
// `breaker` 为 nil（显式关闭熔断）时返回 `disabled` 视图：`/health` 的字段始终存在。
func circuitStatus(breaker *biz.AICircuitBreaker) func() service.CircuitStatus {
	if breaker == nil {
		return nil
	}
	return func() service.CircuitStatus {
		snap := breaker.Snapshot()
		out := service.CircuitStatus{
			Target: biz.CircuitTargetAI,
			State:  snap.StateName(),
		}
		if snap.RetryAfter > 0 {
			out.RetryAfterSeconds = int(snap.RetryAfter.Seconds())
			// 向上取整到 1s：`Retry-After: 0` 会被客户端理解成「立刻重试」，
			// 而熔断还没结束，等于取消冷却。
			if out.RetryAfterSeconds < 1 {
				out.RetryAfterSeconds = 1
			}
		}
		return out
	}
}

func newSigner(cfg *conf.Config, logger *slog.Logger) (*jwtx.Signer, error) {
	secret := cfg.Auth.JWTSecret
	if secret == "" {
		if cfg.IsProd() {
			// conf.Validate 已经拦过，这里只是纵深防御。
			return nil, errors.New("JWT_SECRET 未配置")
		}
		secret = devJWTSecret
		logger.Warn("app.jwt_secret_fallback",
			slog.String("hint", "当前使用内置开发密钥；本地生成的令牌无法在其它环境使用"),
		)
	}
	signer, err := jwtx.NewSigner(jwtx.Config{
		Secret:    secret,
		Issuer:    cfg.Auth.JWTIssuer,
		Audience:  cfg.Auth.JWTAudience,
		KID:       cfg.Auth.JWTKID,
		TTL:       cfg.Auth.AccessTokenTTL(),
		ClockSkew: cfg.Auth.ClockSkew(),
	})
	if err != nil {
		return nil, fmt.Errorf("JWT 配置不合法: %w", err)
	}
	logger.Info("app.jwt_ready",
		slog.String("issuer", cfg.Auth.JWTIssuer),
		slog.String("audience", cfg.Auth.JWTAudience),
		slog.String("kid", cfg.Auth.JWTKID),
		slog.Int("access_ttl_minutes", cfg.Auth.AccessTokenTTLMinutes),
	)
	return signer, nil
}
