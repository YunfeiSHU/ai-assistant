package server

import (
	"io"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"gopkg.in/yaml.v3"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/conf"
	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/internal/service"
)

func testEngineRoutes(t *testing.T) map[string]map[string]bool {
	t.Helper()
	gin.SetMode(gin.TestMode)
	d := Deps{
		Config: &conf.Config{App: conf.App{
			Env: "local", APIPrefix: "/api/v1", MaxJSONBodyMB: 1,
		}},
		Log:           slog.New(slog.NewTextHandler(io.Discard, nil)),
		Auth:          &service.AuthHandler{},
		User:          &service.UserHandler{},
		Health:        &service.HealthHandler{},
		Quota:         &service.QuotaHandler{},
		Upload:        &service.UploadHandler{},
		Conversations: &service.ConversationHandler{},
		Messages:      &service.MessageHandler{},
		Proxy:         &service.ProxyHandler{},
		TaskEvents:    &service.TaskEventHandler{},
		Search:        &service.SearchHandler{},
	}
	engine := NewEngine(d)
	out := map[string]map[string]bool{}
	for _, route := range engine.Routes() {
		if !strings.HasPrefix(route.Path, "/api/v1") {
			continue
		}
		path := strings.TrimPrefix(route.Path, "/api/v1")
		if path == "/health" || strings.HasPrefix(path, "/health/") {
			continue
		}
		path = regexp.MustCompile(`:([A-Za-z_]+)`).ReplaceAllString(path, `{$1}`)
		if _, ok := out[path]; !ok {
			out[path] = map[string]bool{}
		}
		out[path][strings.ToLower(route.Method)] = true
	}
	return out
}

// TestOpenAPIRouteParity 是 B-12 的完整守卫：
// OpenAPI 的 path+method 与 Gin 实际注册的 /api/v1 路由必须双向一致。
// health 探针不属于对外契约，已在收集时排除。
func TestOpenAPIRouteParity(t *testing.T) {
	raw, err := os.ReadFile("../../docs/openapi.yaml")
	if err != nil {
		t.Fatalf("读取 openapi.yaml 失败: %v", err)
	}
	var doc struct {
		Paths map[string]map[string]any `yaml:"paths"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("解析 openapi.yaml 失败: %v", err)
	}

	actual := testEngineRoutes(t)
	methods := map[string]bool{"get": true, "post": true, "put": true, "patch": true, "delete": true}
	for path, ops := range actual {
		docOps, ok := doc.Paths[path]
		if !ok {
			t.Errorf("OpenAPI 缺少实际路由 path: %s", path)
			continue
		}
		for method := range ops {
			if !methods[method] {
				continue
			}
			if _, ok := docOps[method]; !ok {
				t.Errorf("OpenAPI 缺少 %s %s", strings.ToUpper(method), path)
			}
		}
	}
	for path, docOps := range doc.Paths {
		actualOps, ok := actual[path]
		if !ok {
			t.Errorf("OpenAPI 有未注册的 path: %s", path)
			continue
		}
		for method := range docOps {
			if !methods[method] {
				continue
			}
			if !actualOps[method] {
				t.Errorf("OpenAPI 有未注册的 %s %s", strings.ToUpper(method), path)
			}
		}
	}
}
