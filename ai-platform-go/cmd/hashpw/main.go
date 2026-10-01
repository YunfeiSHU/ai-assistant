// Command hashpw 生成 argon2id 密码哈希（用于部署脚本与运维重置密码）。
//
//	go run ./cmd/hashpw -passwords "Aa#123456789,Bb#123456789"
//	go run ./cmd/hashpw -password "Aa#123456789" -format sql
//
// 存在的理由：测试账号要写进 `deploy/mysql/*.sql`，而 SQL 脚本必须是静态的
// （CI 与本地要能重复执行出同样的账号）。哈希只能离线算好再贴进去 ——
// 手工拼 PHC 串几乎一定会拼错，而拼错的症状是「密码看起来对但登录总是 401」。
package main

import (
	"encoding/base64"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/cryptox"
)

func main() {
	var (
		password  = flag.String("password", "", "单个明文密码")
		passwords = flag.String("passwords", "", "逗号分隔的多个明文密码")
		format    = flag.String("format", "plain", "输出格式：plain | sql")
		saltB64   = flag.Bool("salt", false, "额外打印 salt（仅排障用，不要用于生产）")
	)
	flag.Parse()

	var list []string
	if *password != "" {
		list = append(list, *password)
	}
	if *passwords != "" {
		for _, p := range strings.Split(*passwords, ",") {
			if p = strings.TrimSpace(p); p != "" {
				list = append(list, p)
			}
		}
	}
	if len(list) == 0 {
		fmt.Fprintln(os.Stderr, "用法: hashpw -password <明文> 或 -passwords <a,b,c>")
		os.Exit(2)
	}

	params := cryptox.DefaultArgon2Params()
	for _, pw := range list {
		if n := len([]rune(pw)); n < 8 {
			fmt.Fprintf(os.Stderr, "跳过（少于 8 个字符）: %q\n", pw)
			continue
		}
		encoded, err := cryptox.HashPassword(pw, params)
		if err != nil {
			fmt.Fprintf(os.Stderr, "生成失败: %v\n", err)
			os.Exit(1)
		}
		switch *format {
		case "sql":
			fmt.Printf("-- %s\n'%s'\n", pw, encoded)
		default:
			fmt.Printf("%s\t%s\n", pw, encoded)
		}
		if *saltB64 {
			parts := strings.Split(encoded, "$")
			if len(parts) >= 4 {
				raw, _ := base64.RawStdEncoding.DecodeString(parts[3])
				fmt.Printf("  salt=%s (len=%d)\n", parts[3], len(raw))
			}
		}
	}
}
