package ai

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"time"
)

// HealthPath 是 AI 侧的就绪端点（`GET /api/v1/health`，无需鉴权）。
//
// 选它而不是 gRPC 连通性：这条路径与「透传类接口能不能用」是同一个前提，
// 而 gRPC 侧没有可无凭据调用的探活 RPC（`Chat` 要求用户 token）。
// 也不能只探 TCP 端口 —— 「进程活着但应用没起来」时端口同样是开的。
const HealthPath = "/api/v1/health"

// healthProbeTimeout 是探活自身的上限。
//
// 比 `Options.ConnectTimeout` 略宽：探活要覆盖「TCP 已建立但应用不答应」
// 这一档，而那一档的耗时由上游决定，不是连接建立时间。
const healthProbeTimeout = 2 * time.Second

// NewProbe 构造 `/health` 用的 AI 可达性探针（docs/06-§5.4）。
//
// 返回 `(是否可达, 耗时毫秒, 错误描述)`，与 `service.AIProbe` 的形状一致 ——
// 装配层直接把它传进去，不需要中间适配函数。
//
// **必须有超时，且要短**：`/health` 是运维与 LB 高频调用的端点，
// 探针挂住会让「查健康」本身变成一次故障（客户端超时后无法区分
// 「网关没起来」与「网关在等 AI」）。因此这里用独立的 2s，
// 而不是复用对话用的 70s 档位。
//
// 判据只到「拿到了 2xx」：解析 provider 细节是 AI 侧 `/health` 自己的事，
// 网关再解读一遍会让两个服务对「什么叫健康」有两套定义。
func NewProbe(opt Options) func(ctx context.Context) (bool, int64, string) {
	if opt.Log == nil {
		opt.Log = slog.Default()
	}
	base := newURLBase(opt.BaseURL)
	// 探针**不共用**透传的连接池：透传头的 `Timeout` 是按档位设的，
	// 而 `http.Client.Timeout` 与请求级 deadline 叠加后取小者 ——
	// 复用会让探针的 2s 与档位里的较大值之间产生一个随调用顺序变化的实际超时。
	client := &http.Client{
		Timeout:   healthProbeTimeout,
		Transport: newHTTPClient(opt).Transport,
	}
	target, joinErr := base.join(HealthPath)

	return func(ctx context.Context) (bool, int64, string) {
		start := time.Now()
		elapsed := func() int64 { return time.Since(start).Milliseconds() }
		if joinErr != nil {
			// 地址不合法是最常见的部署错误（漏了 scheme、多了路径段），
			// 报出原始错误比「不可达」有用得多。
			return false, elapsed(), "AI_PLATFORM_BASE_URL 不合法: " + joinErr.Error()
		}

		reqCtx, cancel := context.WithTimeout(ctx, healthProbeTimeout)
		defer cancel()
		req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, target, nil)
		if err != nil {
			return false, elapsed(), err.Error()
		}
		resp, err := client.Do(req)
		if err != nil {
			return false, elapsed(), err.Error()
		}
		defer func() {
			// 必须排空并关闭：只 Close 不读会让连接无法复用，
			// 每次探活都新建一条 TCP（高频探活下这是可观的浪费）。
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
			_ = resp.Body.Close()
		}()
		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			return false, elapsed(), "GET " + HealthPath + " 返回 " + resp.Status
		}
		return true, elapsed(), ""
	}
}
