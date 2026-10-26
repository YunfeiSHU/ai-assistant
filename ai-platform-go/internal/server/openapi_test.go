package server

import (
	"os"
	"testing"

	"gopkg.in/yaml.v3"
)

// TestOpenAPIContractCoversCoreRoutes 是 B-12 的最小守卫：
// 契约文件必须存在且覆盖对外核心路由。完整 schema 仍由人工评审，
// 但“删掉/改错一个核心 path 而没人发现”会在这里失败。
func TestOpenAPIContractCoversCoreRoutes(t *testing.T) {
	raw, err := os.ReadFile("../../docs/openapi.yaml")
	if err != nil {
		t.Fatalf("读取 openapi.yaml 失败: %v", err)
	}
	var doc struct {
		OpenAPI string                    `yaml:"openapi"`
		Paths   map[string]map[string]any `yaml:"paths"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("解析 openapi.yaml 失败: %v", err)
	}
	if doc.OpenAPI == "" {
		t.Fatal("openapi 版本为空")
	}
	required := []string{
		"/auth/login",
		"/auth/refresh",
		"/auth/logout",
		"/me",
		"/conversations",
		"/conversations/{conversation_id}",
		"/conversations/{conversation_id}/messages",
		"/conversations/{conversation_id}/messages/stream",
		"/messages/{message_id}",
		"/messages/{message_id}/regenerate",
		"/search/messages",
		"/tasks",
		"/tasks/{task_id}",
		"/tasks/{task_id}/events",
		"/tasks/{task_id}/cancel",
		"/tasks/{task_id}/retry",
		"/documents/{doc_id}/download",
		"/knowledge-bases",
	}
	for _, path := range required {
		if _, ok := doc.Paths[path]; !ok {
			t.Errorf("OpenAPI 缺少核心 path: %s", path)
		}
	}
}
