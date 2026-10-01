package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"
)

// metricsPath 是指标暴露路径（docs/06-§5.2）。
const metricsPath = "/metrics"

// MetricsOptions 是 /metrics 独立监听器的配置。
type MetricsOptions struct {
	// Addr 形如 `:9106`；为空时**不启动**监听器（功能关闭）。
	Addr string
	// AllowCIDRs 是允许访问的白名单（空 = 不限制，仅建议在本地/内网如此）。
	AllowCIDRs []string
	// Handler 是 /metrics 的处理器（通常是 promhttp.HandlerFor(...)）。
	Handler http.Handler
	Log     *slog.Logger
}

// MetricsServer 是独立的指标监听器。
//
// 为什么**不复用主 HTTP 引擎**：docs/06-§5.2 要求 /metrics 只能被
// 内网/白名单访问，而主引擎是公网入口 —— 把两者放一起意味着
// 「白名单中间件写错」就变成「公网可拉指标」（指标里含路由、错误码、
// 用户量级等情报）。分开监听后，即使白名单写错，也只会在另一个端口上
// 暴露，且那个端口可以通过防火墙/安全组彻底不对外。
//
// 另一点：这个监听器**不挂 AccessLog**。指标会被 Prometheus 每 15 秒
// 抓一次，记进访问日志只会把真正的业务日志淹掉。
type MetricsServer struct {
	server *http.Server
	log    *slog.Logger
	allow  []*net.IPNet
}

// NewMetricsServer 构造指标监听器；Addr 为空或 Handler 为 nil 时返回 nil。
func NewMetricsServer(opt MetricsOptions) (*MetricsServer, error) {
	addr := strings.TrimSpace(opt.Addr)
	if addr == "" || opt.Handler == nil {
		return nil, nil
	}
	allow, err := parseCIDRs(opt.AllowCIDRs)
	if err != nil {
		return nil, err
	}
	s := &MetricsServer{log: opt.Log, allow: allow}

	mux := http.NewServeMux()
	mux.Handle(metricsPath, opt.Handler)
	// 探活：让「指标端口活着但 /metrics 被白名单拒了」可被区分开。
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("ok\n"))
	})
	s.server = &http.Server{
		Addr:              addr,
		Handler:           s.guard(mux),
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s, nil
}

// Listener 返回实际监听的地址（测试与日志用；未启动时为空串）。
func (s *MetricsServer) Listener() string {
	if s == nil || s.server == nil {
		return ""
	}
	return s.server.Addr
}

// Start 在后台监听，错误通过返回的 channel 传出（缓冲 1，只报一次）。
func (s *MetricsServer) Start() <-chan error {
	if s == nil {
		return nil
	}
	ln, err := net.Listen("tcp", s.server.Addr)
	if err != nil {
		ch := make(chan error, 1)
		ch <- fmt.Errorf("listen %s: %w", s.server.Addr, err)
		return ch
	}
	// 端口可能配的是 `:0`（测试），回写真实地址。
	s.server.Addr = ln.Addr().String()
	ch := make(chan error, 1)
	go func() {
		defer close(ch)
		if err := s.server.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			ch <- err
		}
	}()
	if s.log != nil {
		s.log.Info("metrics.listening",
			slog.String("addr", s.server.Addr),
			slog.String("path", metricsPath),
			slog.Int("allow_cidrs", len(s.allow)),
		)
	}
	return ch
}

// Shutdown 优雅关闭。
func (s *MetricsServer) Shutdown(ctx context.Context) error {
	if s == nil || s.server == nil {
		return nil
	}
	return s.server.Shutdown(ctx)
}

// guard 实施 CIDR 白名单。
//
// 两种来源都认：`RemoteAddr` 的 IP，以及——当请求直接来自本机时——
// 这已经够了。**刻意不看 `X-Forwarded-For`**：这个头的可信前提是
// 「前面有我们自己的反代」，而指标端口通常直接暴露在 Pod 网络里，
// 信一个客户端可控的头等于没有白名单（AC-NFR-07 是同一个道理）。
func (s *MetricsServer) guard(next http.Handler) http.Handler {
	if len(s.allow) == 0 {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !s.permitted(r.RemoteAddr) {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte("forbidden\n"))
			if s.log != nil {
				s.log.Warn("metrics.denied", slog.String("remote", r.RemoteAddr))
			}
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *MetricsServer) permitted(remoteAddr string) bool {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		host = remoteAddr
	}
	ip := net.ParseIP(strings.TrimSpace(host))
	if ip == nil {
		return false
	}
	for _, n := range s.allow {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// parseCIDRs 解析白名单；支持裸 IP（按 /32 或 /128 处理）。
func parseCIDRs(raw []string) ([]*net.IPNet, error) {
	out := make([]*net.IPNet, 0, len(raw))
	for _, item := range raw {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		if _, n, err := net.ParseCIDR(item); err == nil {
			out = append(out, n)
			continue
		}
		ip := net.ParseIP(item)
		if ip == nil {
			return nil, fmt.Errorf("invalid metrics allow CIDR %q", item)
		}
		// 裸 IP 按单主机处理。IPv4 必须用 4 字节形式配 32 位掩码：
		// `net.IPNet.Contains` 会比较字节长度，16 字节的 IP 配 4 字节掩码
		// 会**永远返回 false** —— 白名单看起来配了，实际把所有人都挡在外面。
		if v4 := ip.To4(); v4 != nil {
			out = append(out, &net.IPNet{IP: v4, Mask: net.CIDRMask(32, 32)})
		} else {
			out = append(out, &net.IPNet{IP: ip, Mask: net.CIDRMask(128, 128)})
		}
	}
	return out, nil
}
