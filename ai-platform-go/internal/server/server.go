package server

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
)

// Server 包住 *http.Server，提供优雅退出（docs/06-§3）。
type Server struct {
	cfg    *conf.Config
	engine *gin.Engine
	log    *slog.Logger
	srv    *http.Server
}

// New 构造 HTTP 服务。
func New(cfg *conf.Config, engine *gin.Engine, log *slog.Logger) *Server {
	return &Server{
		cfg:    cfg,
		engine: engine,
		log:    log,
		srv: &http.Server{
			Addr:    cfg.App.HTTPAddr,
			Handler: engine,
			// 不用 ReadTimeout：SSE 是长连接，读超时会误杀。用 ReadHeaderTimeout
			// 防慢速头部攻击，用 IdleTimeout 回收空闲连接。
			ReadHeaderTimeout: 10 * time.Second,
			IdleTimeout:       120 * time.Second,
			MaxHeaderBytes:    1 << 20,
		},
	}
}

// Start 启动并在出错时返回；http.ErrServerClosed 视为正常结束。
func (s *Server) Start() error {
	s.log.Info("http.listening",
		slog.String("addr", s.cfg.App.HTTPAddr),
		slog.String("api_prefix", s.cfg.App.APIPrefix),
	)
	if err := s.srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// Shutdown 优雅退出：停止接受新连接，等待在途请求完成。
// 调用方的 ctx 应带上 GRACEFUL_SHUTDOWN_SECONDS 超时；超时后 Shutdown 会放弃等待，
// 在途的流式请求被断开 —— 断点处的 partial 落库由编排层负责（docs/06-§3 第 ③ 步）。
func (s *Server) Shutdown(ctx context.Context) error {
	s.log.Info("http.shutdown_begin",
		slog.Int("graceful_seconds", s.cfg.App.GracefulShutdownSeconds),
	)
	return s.srv.Shutdown(ctx)
}

// Engine 暴露引擎（测试用）。
func (s *Server) Engine() *gin.Engine { return s.engine }
