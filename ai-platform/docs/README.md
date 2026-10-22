# ai-platform 需求规格说明书（SRS）

> 本目录是 **Python AI 服务 `ai-platform`** 的接口级需求规格：对外 HTTP 契约、SSE 协议、存储模型、AI 能力编排规则、非功能指标与验收口径。
> 全平台分层、技术选型与两端分工见 [`../../README.md`](../../README.md)（根 README）；本目录只覆盖 AI 应用域。

## 1. 范围边界

| 项目 | 说明 |
| --- | --- |
| **覆盖** | `ai-platform`（Python / FastAPI）对外 HTTP 接口契约、SSE 帧协议、数据存储模型、AI 能力编排规则、非功能指标、验收标准 |
| **不覆盖** | Go 网关与用户中心实现、会话/消息业务台账、前端、K8s 编排清单、CI 流水线（仓库内只有 Docker Compose 本地基础设施） |
| **引用但不在本范围** | 用户身份（Go 签发 JWT，Python 只校验并信任 `sub`）、业务侧会话与消息持久化（Go 侧 MySQL）、客户端 SSE 透传 |

**与 Go 的分工**：Python 是「AI 能力提供方」，要求可独立启动、独立压测、独立验收；唯一强耦合是 JWT 约定与 `conversation_id` 语义。Go 侧对外 HTTP/SSE 全部由 Gin 承担，Kratos v2.8.4 只用于对内 gRPC 拨号（`DialInsecure` + `WithTimeout(0)` + 关闭健康检查），不参与对外链路。

## 2. 文档索引

| # | 文档 | 内容 | 主要需求 ID 前缀 |
| --- | --- | --- | --- |
| — | **README.md**（本文件） | 范围、索引、阅读路径、术语、现状与差距 | — |
| 01 | [总体需求与范围](./01-总体需求与范围.md) | 目标、场景、功能范围与优先级、里程碑、追踪矩阵 | `REQ-GEN` |
| 02 | [接口规范与错误码](./02-接口规范与错误码.md) | 通用约定、鉴权、统一错误信封、错误码总表、SSE 通用帧、分页 | `REQ-API` |
| 03 | [对话与流式输出](./03-对话与流式输出.md) | `/chat`、`/chat/stream`、上下文组装、引用溯源 | `REQ-CHAT` |
| 04 | [Agent 与工具调用](./04-Agent与工具调用.md) | Agent Loop 状态机、工具注册契约、内置工具、`/tools` | `REQ-AGENT` |
| 05 | [MCP 接入](./05-MCP接入.md) | MCP 连接生命周期、命名空间、`/mcp/servers` | `REQ-MCP` |
| 06 | [RAG 知识库](./06-RAG知识库.md) | 知识库/文档 CRUD、Chunk 策略、检索与重排、溯源 | `REQ-RAG` |
| 07 | [Memory](./07-Memory.md) | 短期上下文、摘要压缩、长期记忆抽取与注入 | `REQ-MEM` |
| 08 | [异步任务](./08-异步任务.md) | 任务状态机、重试与幂等、`/tasks` | `REQ-TASK` |
| 09 | [数据存储模型](./09-数据存储模型.md) | MySQL 表、Milvus 集合、Redis Key、MinIO 路径、Kafka Topic | `REQ-DATA` |
| 10 | [非功能需求与可观测性](./10-非功能需求与可观测性.md) | 性能、可靠性、安全、埋点、配置项全表 | `REQ-NFR` |
| 11 | [验收标准与测试策略](./11-验收标准与测试策略.md) | 验收用例、RAG 效果评估、测试分层与质量门槛 | `REQ-TEST` |
| 12 | [实现问题记录](./12-实现问题记录.md) | 实际踩到的坑：症状 / 根因 / 修法 / 防线 | — |
| 13 | [技术设计说明](./13-技术设计说明.md) | 分层、装配、错误处理、各链路设计、扩展点与取舍 | — |
| 14 | [接口 curl 示例](./14-接口%20curl%20示例.md) | 每个对外接口的 curl + 真实依赖下的实测响应 + 一键全量验证脚本 | — |

> 01–11 是**需求**（应该是什么），12 是**故障史**（曾错成什么、留了什么防线），13 是**设计**（为什么这么定），14 是**实测**（照这样发请求真的会得到什么，含非 2xx 负例）；12 里的每条坑在 13 都有对应的「所以设计成这样」。
> 原 `15-代码注释规范.md` 在仓库中不存在（原链接为死链），工程约定见 [`../Python项目代码调整规范.md`](../Python项目代码调整规范.md)。

## 3. 阅读路径

- **只想知道要做什么** → 01 → 各模块文档的「功能需求」小节
- **要开始写代码** → 02（先把错误码 / SSE 定死）→ 03 → 06 → 09
- **要把服务跑起来并逐个接口试** → 14（含依赖清单、建表脚本、一键验证脚本）
- **要做验收 / 答辩** → 14 → 11 → 10
- **要接新工具 / 新数据源** → 04 → 05；**要评估工作量** → 01 的优先级表 + 各文档末尾「验收标准」

## 4. 现状与差距

| 模块 | 代码现状 | 差距 / 说明 |
| --- | --- | --- |
| 装配与配置 | `create_app(settings)` + lifespan + CORS + 幂等装配点（`app.state`）；`app/core/config.py` 覆盖 LLM/Embedding/Reranker/Milvus/MySQL/Redis/MinIO/Kafka/Memory/Agent/MCP 与 `validate_for_startup()` | prod 强制 `INFRA_BACKEND=real`；MySQL 仓储未实现，`real` 下由 `app/infrastructure/storage/unavailable.py` 返回 `503 DEPENDENCY_UNAVAILABLE`（不再启动即崩） |
| 健康检查 | `GET /health`、`/health/live`、`/health/ready`（依赖探活注册表） | `INFRA_BACKEND=memory` 时依赖项返回 skipped |
| 错误与鉴权 | 统一错误信封、错误码总表、JWT 校验、trace_id 贯通 | — |
| 对话 | `POST /chat`、`POST /chat/stream`、`GET /models`；`use_rag=true` 注入片段与 `references`，`use_tools=true` 转交 Agent 链路 | — |
| 上下文装配 | system→memory→summary→history→rag→query；按 07-§4.2 确定性裁剪；长期记忆作为**独立** system 段注入 | — |
| Agent | 主链路是自研 `app/agent/loop.py`（步数 / 总时长 / 重复调用三重护栅）+ `POST /agent/run`、`/agent/run/stream` | `app/agent/graph/` 的 LangGraph 图**未接入主链路**，`generate` 节点仍是 TODO |
| 工具 | `app/tools/`（`ToolSpec` + 注册表 + 执行器 + 服务）+ 6 个内置工具（含 `memory_save` / `memory_search`）；`GET /tools`、`POST /tools/{name}/invoke`（仅 local/dev） | `http_fetch` 默认关闭 |
| MCP | `app/mcp/`：`extra="forbid"` + `${VAR}` 展开、stdio 与 `streamable_http` 两种传输、多 Server 并发启动 + 总超时、状态机与懒重连、行内熔断（`mcp:{server}`）、命名空间 `mcp__{server}__{tool}`、allowlist/denylist 与 `write_tools` 审计；`/mcp/servers*` 三个接口 | 不代理 MCP 的 `prompts` / `sampling`（SRS 明示不做） |
| RAG | 解析（pdf/docx/md/html/txt，扩展名 + 魔数双校验）→ 结构感知切片 → 嵌入 → 落库；召回 → 合并相邻 → 重排 → token 预算裁剪；`[n]` ↔ `references[].index` + `content_sha256` 溯源 | 设计原选 Qdrant，实现是 **Milvus**；重排默认 `RERANKER_PROVIDER=bge`（`config.py` 出厂默认，`.env.example` 出厂档改走硅基流动云端），`INFRA_BACKEND=memory` 时检索走进程内实现 |
| Memory | 短期上下文（`ConversationStore`，内存 + Redis）、摘要压缩（四段结构 / 增量合并 / 5 分钟防抖）、长期记忆（哈希 + 语义双层去重、`0.85/0.92` 双阈值、过期与清空 24h 冷静期）；`/memories`、`/memory-settings`、`/conversations/{id}/context` | MySQL 仓储与 Milvus 集合未提供，`real` 下退化为进程内实现并告警 |
| 异步任务 | 状态机（迁移白名单 + 乐观锁 + 幂等键）、内存与 Redis 双仓储、`none`/`inline`/`kafka` 三个执行器、退避重试 ZSET（Lua 原子认领）、补偿扫描、独立 Worker（`python -m app.worker`）、`/tasks` 四接口 + `GET /tasks/{id}/events`（SSE 进度） | `real` 下 KB/文档/切片仍为 `503` 占位，「上传入库」链路只能在 `memory` 下真跑 |
| 可观测 | 结构化日志 + 脱敏 + trace/span；**19** 个 `ai_*` 指标走独立端口 **9100**（主应用不暴露 `/metrics`）；OTel 链路（trace id 与日志/响应头同值）+ 熔断器；`deploy/observability/` 一键 Jaeger + Prometheus + Grafana | 采样率默认 `0.1`，告警规则按部署环境调 |

- **规模（实测）**：HTTP `8000`、指标 `9100`、gRPC `50051`（`GRPC_ENABLED` 默认 `false`、只监听回环）；Python 侧 **41** 条 HTTP 路由、**19** 个 `ai_*` 指标、`pytest` **1698 collected / 1688 passed / 10 skipped**、覆盖率 **89%**；Go 侧对照 **58** 处路由注册、**30** 个 `*_test.go` / **234** 个 `Test*`。
- **未做**：没有 CI、没有 Kubernetes 清单（只有 Docker Compose 本地基础设施）；Go 侧无 Nacos 实现（`AI_DISCOVERY_ENABLED=false` 直连）、`go.mod` 里没有 testify / testcontainers-go；设计上的 User/Conversation/Task 三微服务实现为 Go 侧单进程多模块。
- **已知长期缺陷**：长驻的 Python gRPC 进程跑约 3 小时后，所有 `Chat` 退化为 `503 DEPENDENCY_UNAVAILABLE`（Python 直连同样失败，证明不在网关），重启即恢复；记录见 [`08-实现进度与运行手册.md`](../../ai-platform-go/docs/08-实现进度与运行手册.md) §6.4。

## 5. 术语

| 术语 | 含义 |
| --- | --- |
| KB / 知识库 | Knowledge Base，一组文档的逻辑容器，检索的最小隔离单元 |
| Chunk | 文档切片，向量化与检索的最小单位 |
| Agent Loop | LLM 推理 → 工具调用 → 结果回注 → 再推理的循环 |
| Tool Calling | 由模型输出结构化调用意图、宿主执行并回注结果的机制 |
| MCP | Model Context Protocol，外部工具的标准接入协议 |
| Rerank | 对向量召回结果用交叉编码器重新打分排序 |
| 短期 / 长期 Memory | 当前会话原始上下文（Redis）/ 跨会话保留的偏好与事实（MySQL + Milvus） |
| Conversation Summary | 对早期对话的 LLM 压缩摘要，用于替换原始消息 |
| Task | 异步任务实体，具有状态机与重试语义 |
| Citation / 溯源 | 回答中引用的 chunk 元数据，供前端展示来源 |
| Token 预算 | 单次请求允许占用的上下文 token 上限，按模块分配 |

## 6. 文档约定

- 需求关键词遵循 RFC 2119（MUST / SHOULD / MAY）；需求 ID 形如 `REQ-<模块>-<三位序号>`，全文档唯一，可被测试用例反向引用（见 11）。
- 接口路径省略 `api_prefix`（默认 `/api/v1`），如 `/chat` 实际为 `/api/v1/chat`；JSON 一律 `snake_case`；时间戳一律 RFC 3339 UTC。
- 资源 ID 为带前缀字符串：`kb_` / `doc_` / `chk_` / `task_` / `mem_` / `msg_` / `srv_` + 26 位 Crockford Base32（ULID），如 `doc_01J8ZQ3K7N9P2V6R4T8W1Y5B3C`。
- 表格「默认值」列取自 `app/core/config.py` 现状；新增配置项在 10 中标注 **【新增】**。
