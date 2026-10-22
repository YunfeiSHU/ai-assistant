# ai-platform-go · Go 网关（SRS + 实现记录）

> Go 侧对外唯一入口：Gin HTTP/SSE、JWT 签发、用户与会话台账、AI 编排、配额与限流。
> 整体架构、两端分工与快速开始见[根 README](../README.md)，本文只写 Go 侧的边界、选型与索引。
> 平行文档：[`../ai-platform/docs/README.md`](../ai-platform/docs/README.md)（Python 侧 SRS）。

## 1. 定位与边界

| 项 | 说明 |
| --- | --- |
| 覆盖 | 对外 HTTP/SSE 契约、JWT 签发规范、会话与消息台账、对 `ai-platform` 的调用编排、配额与限流、数据模型、非功能指标、验收标准 |
| 不覆盖 | LLM / Agent / RAG / Memory / 异步入库（属 `ai-platform`）、前端、K8s 清单与 CI |
| 不引入 | Milvus / MinIO / Kafka 客户端——这三个基础设施由 Python 独占 |

规模：`internal/server/http.go` **58 处**路由注册（健康检查组在根与 `/api/v1` 下各注册一次，展开为 6 条）；**93** 个非测试 `.go` + **30** 个 `*_test.go`。

## 2. 数据权威归属

设计文档里 Go 与 Python 都声称管理「会话上下文、知识库、任务、MinIO、Redis」，属于重叠。逐条消歧后只剩一条规则：
**每份数据只有一个权威属主，另一方只能透传，禁止双写**。表结构与 Key 规范见 [05-数据模型](./docs/05-数据模型.md)。

| 数据 / 能力 | 权威属主 | Go 侧行为 |
| --- | --- | --- |
| 用户、凭据、刷新令牌 | **Go** | 读写 |
| 会话 `conversation` | **Go** | 读写，且是 ID 生成者 |
| 消息台账 `message` | **Go** | 读写；Python 不建表 |
| 配额、用量、计费 | **Go** | 读写；Python 只上报 `usage`，不扣减 |
| 业务缓存（会话列表、用户信息） | **Go** | 读写，键空间 `gw:` 独占 |
| 对话摘要、长期记忆、短期上下文 `ctx:{id}` | Python | 不碰该 Key 空间 |
| 知识库 / 文档 / 切片元数据、AI 任务 `task` | Python | 透传 + 配额校验，无本地副本 |
| 原始文件（MinIO） | Python | 只转发字节流，不落盘、不写对象 |
| 向量（Milvus）、消息队列（Kafka） | Python | 不感知 |

三条硬约束：

1. **不出现** Milvus / MinIO / Kafka 客户端——设计文档给 Go 列出的后两个依赖据此取消。
2. Redis Key 统一 `gw:` 前缀（`internal/data/redis/keys.go:14`），不与 Python 的 `ctx:` / `lock:` / `rate:` 重名。
3. 调 `ai-platform` 必带 `conversation_id`（权威属主在 Go）；Python 在空值时自建会话只服务于其独立验收，生产链路不得依赖。

## 3. 技术选型（按 `go.mod` 与代码引用核对，不是愿望清单）

| 层次 | 选型 | 为什么是它 / 边界在哪 |
| --- | --- | --- |
| 语言 | Go 1.24（`go 1.24.0`） | **不要上调这一行**：会触发 `GOTOOLCHAIN=auto` 联网下载新工具链，并连带升级 `x/crypto`、`gin`（≥ v1.12 要求 go ≥ 1.25） |
| 对外 HTTP/SSE | Gin v1.11.0 | 路由、中间件链、错误信封、SSE 全在这一层 |
| 对内 gRPC | Kratos v2.8.4，**仅用于拨号** | 只有 `internal/data/ai/grpc.go` 引用：`DialInsecure` + `WithTimeout(0)` + `WithHealthCheck(false)`；对外 HTTP 不经过 Kratos |
| 鉴权 | `golang-jwt/jwt/v5`，HS256 | JWT 由 Go **签发**，Python 只校验；claim 与 [02-§3.2](./docs/02-接口规范与鉴权.md) 逐字对齐 |
| 口令哈希 | `x/crypto` argon2id | 离线工具 `cmd/hashpw` |
| 配置 | `joho/godotenv` + 环境变量 | `.env` 由 `ENV_FILE` 指绝对路径；存在但解析失败即退出，不静默回退默认值 |
| 可观测 | OTel Go SDK v1.31 + Prometheus client | OTLP(gRPC) 导出 + `gw_*` 指标；指标走**独立端口**，主应用不暴露 `/metrics` |
| 测试 | 标准库 `testing` | 30 个 `*_test.go` / 234 个测试函数（231 `Test*` + 3 `Benchmark*`）；真基础设施用例靠 `GW_LIVE_REDIS` 等环境变量开关 |

> **三处与文档旧口径不一致，以代码为准**：
> - **没有 Nacos 实现**。`internal/conf` 只保留 `AI_DISCOVERY_ENABLED` / `NACOS_ADDR` / `NACOS_NAMESPACE` / `NACOS_GROUP` 字段（`load.go:280-283`），默认 `false`，无任何 SDK 依赖，服务按 `AI_PLATFORM_BASE_URL` 直连。
> - **没有 testify、没有 testcontainers-go**。`go.mod` 无这两个依赖，`*_test.go` 里也 0 处引用。
> - **没有 validator 的实际使用**。`go-playground/validator` 只是 Gin 的间接依赖，项目代码 0 处引用。

Kratos 那三个参数一个都不能省：`dial()` 把 timeout 默认成 2000ms 并总是装一个 unary 拦截器，
实际 deadline 就成了 `min(自己的档位, 2s)`——3s 的问答变成 `DeadlineExceeded`，还被归类成「AI 侧自己回的超时」，排查方向直接被带偏；
所以必须传 `0`（kratos 源码有 `if timeout > 0` 守卫），超时只留 `Chat` 与 `TimeoutForMeta` 这一个来源。
自带的健康检查均衡器在单实例直连下只在启动期制造噪声，一并关掉并显式 `pick_first`。复现见 [08-§6.3](./docs/08-实现进度与运行手册.md)。

## 4. 目录结构（`internal/` 5 层 + `pkg/` 11 个可复用包，一级一行）

| 路径 | 职责 |
| --- | --- |
| `cmd/server` | 对外 HTTP/SSE 入口（`cmd/hashpw` 为 argon2id 离线哈希工具） |
| `api/aiplatform/v1/` · `proto/aiplatform/v1/` | 生成物（不手改）与契约真源 `chat.proto`，两侧从同一份生成 |
| `internal/conf` | 配置加载 + 启动期校验，fail-fast |
| `internal/server` | 路由装配与中间件链：`recovery → trace → logging → metrics → cors → 幂等 → 鉴权 → 限流` |
| `internal/service` | 用例层：DTO、编排、SSE 逐帧写入 |
| `internal/biz` | 领域逻辑与**仓储接口**（不 import `data`） |
| `internal/data` | 仓储实现：`mysql/`、`redis/`、`ai/`（HTTP 透传 + gRPC 客户端） |
| `pkg/` | `errs httpx ids clockx cryptox jwtx logx cursor ssex metricsx otelx` |
| `deploy/mysql/` | 网关独占 6 张表、测试账号、自检脚本 |
| `tools/` | `curl_stage1..6` / `run_all_stages` / `cost_report` / `falsify_m4` / `gen_proto` |

依赖方向是硬约束：接口归 `biz`、实现归 `data`，`biz` 不 import `data`——单测只替换仓储即可，不必拉起 MySQL / Redis。

## 5. 关键取舍与端口

| 取舍 | 结论 | 原因 |
| --- | --- | --- |
| 对外框架 | 全部 Gin | 错误信封、`Retry-After`、CORS、安全头、SSE 心跳都在 Gin 中间件链里；换成 Kratos HTTP transport 要重写一遍，且不会出现「Gin 套在 Kratos server 上」 |
| SSE 是否进 gRPC | 不进 | 流式走 server-streaming 或 HTTP 透传；取消传播用连接断开表达，比自建一套流控简单 |
| 是否拆微服务 | 单进程多模块 | 设计文档的 User / Conversation / Task 共享同一 MySQL 事务边界（建会话 + 写首条消息需原子），拆进程就得引入 Saga 或最终一致 |

端口（两侧默认值，同机并跑要避让）：

| 端口 | 归属 | 说明 |
| --- | --- | --- |
| 8080 | Go 网关 HTTP | `HTTP_ADDR=0.0.0.0:8080`，与 AI 的 8000 区分 |
| 9100 | 两侧指标 | `METRICS_PORT`，独立于业务端口；两侧默认都是 9100，同机并跑必须改一侧 |
| 8000 | `ai-platform` HTTP | `AI_PLATFORM_BASE_URL` 默认指向它 |
| 50051 | `ai-platform` gRPC | 两侧默认关闭：`AI_GRPC_ENABLED=false`、`GRPC_ENABLED=false`；Python 侧未开启时 `python -m app.grpc` 以退出码 2 报错 |

## 6. 文档索引

| # | 文档 | 内容 | 需求 ID |
| --- | --- | --- | --- |
| 规范 | [代码生成规范](./代码生成规范.md) | 目录分层与**依赖方向**、命名、错误处理（写码前必读，§四/§六 是硬约束） | — |
| 01 | [总体需求与范围](./docs/01-总体需求与范围.md) | 目标、场景、优先级、里程碑、接缝风险清单 | `REQ-GEN` |
| 02 | [接口规范与鉴权](./docs/02-接口规范与鉴权.md) | 通用约定、错误信封、**JWT 签发契约**、限流与配额 | `REQ-AUTH` |
| 03 | [用户与会话服务](./docs/03-用户与会话服务.md) | 注册登录、用户资料、会话 CRUD、消息台账 | `REQ-CONV` |
| 04 | [AI 服务编排与网关](./docs/04-AI服务编排与网关.md) | 接口契约、调用与超时、SSE 透传、取消传播、降级 | `REQ-ORCH` |
| 05 | [数据模型](./docs/05-数据模型.md) | MySQL 表结构、Redis Key 规范、明确不建的表 | `REQ-DATA` |
| 06 | [非功能需求与可观测性](./docs/06-非功能需求与可观测性.md) | 性能、安全、链路追踪、指标、配置全表、部署 | `REQ-NFR` |
| 07 | [验收标准与测试策略](./docs/07-验收标准与测试策略.md) | 测试分层、**JWT 互操作测试**、跨服务 E2E（CI 部分尚未落地） | `REQ-TEST` |
| 08 | [实现进度与运行手册](./docs/08-实现进度与运行手册.md) | 目录结构、本地运行、测试账号、**分阶段进度与踩坑记录** | — |
| 09 | [优化清单与实测记录](./docs/09-优化清单与实测记录.md) | 耗时归因、OPT-01~10 / UP-01~10 优化清单、本机实测与复现命令、与预期不符的结论 | `REQ-ORCH-005` |

可执行资产：`deploy/mysql/{001_gateway_tables,002_test_accounts,verify_schema}.sql`；`tools/run_all_stages.ps1` 一次跑完 M1→M6（六条脚本合计 474 项断言，**历史记录，未复跑**）；
`tools/cost_report.ps1` 按接口 / 需求算实际耗时；`tools/falsify_m4.ps1` 变异测试（改坏一处源码，验证对应单测真的会红）。

## 7. 已知缺口（不隐瞒）

- **没有 CI、没有 Kubernetes 清单**：仓库里只有 Docker Compose 起的本地基础设施（Redis / MySQL / Milvus / MinIO / Kafka / Jaeger / Prometheus / Grafana）。
- **审计查询接口未做**：`audit_log` 只落库，没有 `GET /audit-logs`。
- **一处长期缺陷**：长驻的 Python gRPC 进程连续跑约 3 小时后，所有 `Chat` 退化为 `503 DEPENDENCY_UNAVAILABLE`，重启即恢复；
  Python 直连探测同样失败，**不是网关的问题**，诊断记录见 [08-§6.4](./docs/08-实现进度与运行手册.md)。
