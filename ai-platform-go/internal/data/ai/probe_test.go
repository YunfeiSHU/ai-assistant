package ai

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestProbeReportsReachableOn2xx 锁住「AI 起来了 → 探针报 ok」这一侧。
//
// 这条断言看起来平凡，但它是 `/health` 里 `checks.ai_platform.ok` 的**唯一**
// 数据来源；上一版这里恒为 false（探针没接），运维侧看到的 `status: degraded`
// 与真实状态无关。所以「能报 ok」这件事本身需要守卫。
func TestProbeReportsReachableOn2xx(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	probe := NewProbe(Options{BaseURL: srv.URL})
	ok, ms, detail := probe(context.Background())

	if !ok {
		t.Fatalf("AI 返回 200 时探针应报可达，实际 ok=false detail=%q", detail)
	}
	if detail != "" {
		t.Errorf("可达时不应带错误描述，实际 %q", detail)
	}
	if ms < 0 {
		t.Errorf("耗时不应为负，实际 %d", ms)
	}
	// 探的必须是 AI 的就绪端点，而不是根路径或业务路径：
	// 探到需要用户 token 的路径会稳定 401，从而把「AI 好好的」报成不可达。
	if gotPath != HealthPath {
		t.Fatalf("探针应请求 %s，实际请求了 %s", HealthPath, gotPath)
	}
}

// TestProbeReportsUnreachableOnNon2xx 锁住「非 2xx 一律算不可达」。
//
// 501/500 这类是「进程活着但应用没起来」的典型形态；只探端口连通性会漏掉它。
func TestProbeReportsUnreachableOnNon2xx(t *testing.T) {
	for _, code := range []int{http.StatusInternalServerError, http.StatusServiceUnavailable, http.StatusUnauthorized} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(code)
			}))
			defer srv.Close()

			ok, _, detail := NewProbe(Options{BaseURL: srv.URL})(context.Background())
			if ok {
				t.Fatalf("%d 应算不可达", code)
			}
			// 描述里要带状态码：只写「不可达」会让排查分不清
			// 是连不上、还是连上了被拒。
			if !strings.Contains(detail, http.StatusText(code)) {
				t.Errorf("错误描述应含状态文本 %q，实际 %q", http.StatusText(code), detail)
			}
		})
	}
}

// TestProbeReportsBadBaseURL 锁住「base URL 有问题时不管配置成什么样都不报 ok」。
//
// 这里刻意用三类不同性质的坏输入，因为它们的失败点完全不同，
// 而「全都不能报 ok」是唯一统一的要求：
//
//   - 空串：配置根本没填；
//   - `://bad`：填了但解析不出 scheme；
//   - 漏 scheme 的 `host:port`：`newURLBase` 会**故意**补 `http://`
//     （见 http.go 的注释：补比让每个请求都在建连阶段报错更好）。
//     所以第三种的不变量不是「报不合法」，而是「产出的仍是合法 URL，
//     失败原因必须长得像连接错误」。
func TestProbeReportsBadBaseURL(t *testing.T) {
	t.Run("未配置", func(t *testing.T) {
		ok, _, detail := NewProbe(Options{BaseURL: ""})(context.Background())
		if ok {
			t.Fatal("空 base URL 不应报可达")
		}
		if !strings.Contains(detail, "AI_PLATFORM_BASE_URL") {
			t.Errorf("描述应点明 AI_PLATFORM_BASE_URL，实际 %q", detail)
		}
	})

	t.Run("无法解析", func(t *testing.T) {
		ok, _, detail := NewProbe(Options{BaseURL: "://bad"})(context.Background())
		if ok {
			t.Fatal("`://bad` 不应报可达")
		}
		if detail == "" {
			t.Error("解析失败时应给出错误描述")
		}
	})

	t.Run("漏 scheme 仍按 http 处理", func(t *testing.T) {
		// 1 号端口在 Windows 上必然拒绝连接：用它把「宽容路径」逼到
		// 连接失败这一档。若 newURLBase 的补全逻辑坏了，这里会变成
		// URL 解析错误（含 `不合法`）或干脆报 ok，两种都被下面的断言挡住。
		ok, _, detail := NewProbe(Options{BaseURL: "127.0.0.1:1"})(context.Background())
		if ok {
			t.Fatal("连不上的地址不应报可达")
		}
		if strings.Contains(detail, "不合法") {
			t.Errorf("漏 scheme 的 base 应被补成 http:// 后正常发起请求，不该在拼 URL 阶段失败：%q", detail)
		}
	})
}

// TestProbeDoesNotHangBeyondTimeout 锁住「上游不答应时探针要按时返回」。
//
// 这是本文件里唯一真正重要的守卫：`/health` 是运维与负载均衡高频调用的端点，
// 探针挂住会让「查健康」本身变成一次故障 —— 客户端超时后根本无法区分
// 「网关进程没起来」和「网关在等 AI」，这正是上一轮 M5 排查里最难受的一步。
//
// 断言用「探针返回」而不是「返回内容的耗时字段」：耗时字段是自报的，
// 探针若在 `client.Do` 里挂住，那个字段根本没机会被写出来。
func TestProbeDoesNotHangBeyondTimeout(t *testing.T) {
	// 服务端收到请求后一直不写响应头，直到测试结束。
	// 这正是「TCP 通了但应用不答应」的形态。
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	defer func() {
		close(release)
		srv.Close()
	}()

	done := make(chan struct{})
	var ok bool
	var detail string
	go func() {
		defer close(done)
		ok, _, detail = NewProbe(Options{BaseURL: srv.URL})(context.Background())
	}()

	// 给足余量（超时上限 2s，这里等到 5s）：断言的是「有没有上限」，
	// 不是「上限恰好是 2s」。把上限写成精确值会让这条守卫在慢机器上假失败。
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatalf("上游不响应时探针应在 %v 内返回；已等待 5s 仍未返回，说明超时没生效", healthProbeTimeout)
	}

	if ok {
		t.Fatal("上游不响应时不应报可达")
	}
	if detail == "" {
		t.Error("不可达时应给出错误描述，便于排查")
	}
}
