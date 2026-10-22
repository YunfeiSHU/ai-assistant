# ai-platform

Python 侧 AI 服务。FastAPI 承载全部对外 HTTP/SSE，工具调用主链路是**自研 Agent Loop**
（`app/agent/loop.py`）；RAG（召回 → 重排 → 引用溯源）、Memory（短期上下文 / 摘要 / 长期记忆）、
MCP 工具接入、异步任务（Kafka + 独立 Worker）与可观测（OTel + Prometheus）按模块装配在同一个进程里。

> Go 网关、两侧分工与整体架构图见[仓库根 README](../README.md)，本文不重复。
> 与 SRS 重复的接口明细一律给指针，不复制表格。

## 1. 规模（以代码为准）

| 项 | 数值 | 口径 |
| --- | --- | --- |
| HTTP 路由 | **41** 条 | `app/api/**` 的 `@router.*` 装饰器计数（见 §6） |
| 监听端口 | **8000** HTTP / **9100** 指标 / **50051** gRPC（默认关闭） | §5 |
| `ai_*` 指标 | **19** 个 | `app/infrastructure/observability/metrics.py` |
| 配置字段 | 150 个（`.env.example` 123 个赋值） | `app/core/config.py::Settings` |
| pytest | **1698 collected / 1688 passed / 10 skipped**，112.8 s | 本机 Windows 实测，见 §8 |
| 覆盖率 | **89%**（11561 语句 / 1315 未覆盖，门槛 70%） | `pytest --cov=app` |
| MySQL 表 | **15** 张｜AI 独占 7 + 共享 2 + 网关独占 6 | `deploy/mysql/`、`docs/09` §2 |

## 2. 技术选型

| 层次 | 选型 | 关键事实 |
| --- | --- | --- |
| 语言 / 依赖 | Python **3.12** + uv | `.python-version` 固定（torch / transformers / FlagEmbedding 对 3.14 适配不全）；`uv.lock` 入库 |
| Web | FastAPI + Uvicorn | `create_app(settings)` 可注入配置；全异步 |
| 校验 / 配置 | Pydantic v2 + pydantic-settings | 唯一事实来源 `app/core/config.py::Settings`，启动期 `validate_for_startup()` |
| LLM 客户端 | 自研 OpenAI 兼容适配器（`app/llm/`） | 默认 DeepSeek；换供应商只改 `OPENAI_BASE_URL` + `LLM_MODEL` |
| Agent 主链路 | **自研 Agent Loop**（`app/agent/loop.py`） | 步数 / 总时长 / 重复调用三重护栅；`langgraph` 状态图只在 `app/agent/graph/` 做占位，**未接入主链路**，`generate` 节点仍是 TODO |
| Embedding | 默认 `hash`（词法向量，零下载）｜本地档 `bge`（`BAAI/bge-m3`）｜云端档 `siliconflow` | `.env.example` 出厂档 = 硅基流动 `Qwen/Qwen3-Embedding-0.6B`（1024 维、真批量 N 进 N 出）；本地档也是 1024 维但**向量空间不同，换档必须换集合名** |
| Rerank | 默认 `bge`（`BAAI/bge-reranker-v2-m3` 本地交叉编码器）｜云端 `siliconflow` | 出厂档 = `Qwen/Qwen3-Reranker-0.6B`（20 条候选实测 320ms）；两者都惰性加载，出厂档不下载权重 |
| 向量库 | **Milvus**（PyMilvus + langchain-milvus） | 集合 `ai_platform_chunks` / `ai_platform_memories`，HNSW/COSINE + `user_id`/`kind` 标量倒排索引 |
| 工具协议 | MCP Python SDK | `stdio` 与 `streamable_http` 两种传输，工具命名空间 `mcp__{server}__{tool}` |
| 消息队列 | **Kafka**（aiokafka，可选 extra） | 主题 `ai.task.<type>`、死信 `ai.task.dlq`；`TASK_RUNNER=inline` 时整个 Kafka 不参与 |
| 缓存 / 任务态 | Redis（可选 extra） | 短期上下文、幂等键、退避重试 ZSET、pub/sub 事件总线 |
| 关系库 / 对象存储 | MySQL 8（SQLAlchemy Core + asyncmy）/ MinIO | 都在可选依赖组，按 `--extra` 装 |
| 对内 gRPC | grpcio / grpcio-tools | `python -m app.grpc`；默认 `GRPC_ENABLED=false`、只听回环 |
| 可观测 | OpenTelemetry（OTLP gRPC）+ Prometheus client | 19 个 `ai_*`；指标走独立端口，主应用不暴露 `/metrics` |
| 质量门禁 | ruff + mypy + pytest(asyncio / cov) | 见 §8 |

## 3. 目录结构

| 路径 | 内容 |
| --- | --- |
| `app/core/` | 叶子层：配置、错误码、ULID、trace 上下文、结构化日志 + 脱敏、中间件、JWT、token 计数、游标分页、SSE 帧、探活注册表 |
| `app/api/` | v1 路由（41 条）、依赖注入、统一错误信封；只做参数绑定，不写业务判断 |
| `app/application/` | 用例编排：context 装配 / chat / agent / memory |
| `app/schemas/` | Pydantic 请求 / 响应模型（逐条对齐 SRS） |
| `app/llm/` | LLM 协议 + OpenAI 兼容适配器 + 上游异常映射 |
| `app/agent/` | `loop.py` 主链路；`graph/` 为 LangGraph 占位（未接入） |
| `app/tools/` | ToolSpec / 注册表 / 执行器（读并发、写串行、超时、截断）/ 内置工具：`kb_retrieve`、`calculator`、`current_time`、`http_fetch`、`memory_save`、`memory_search` |
| `app/rag/` | 解析（pdf/docx/md/html/txt，扩展名 + 魔数双校验）、结构感知切片、embedding、reranker、retriever、Milvus 向量库 |
| `app/memory/` | 短期上下文（内存 / Redis 两套）、摘要、长期记忆、抽取、偏好、记忆向量索引 |
| `app/tasks/` | 状态机、仓储（内存 / Redis）、退避重试队列、补偿扫描、事件总线、三个 runner、SSE 进度流 |
| `app/worker/` | 独立 Worker 进程：启动自检、消费循环、按分区连续水位线提交偏移 |
| `app/mcp/` | 配置校验、两种传输、单 Server 状态机（懒重连 + 行内熔断）、多 Server 编排、工具适配 |
| `app/infrastructure/` | 适配层：MySQL、Redis、storage（知识库 / 文档 / 切片 / 对象存储）、observability（指标门面 / OTel / 熔断器 / 指标端口） |
| `app/grpc/` | 对内 gRPC 服务端：`Chat` / `ChatStream`；生成物在 `aiplatform/v1/`（不手改） |
| `deploy/` | `mysql/`（001→004 建表 + verify）、`infra/`（Redis / Kafka）、`observability/`（Jaeger + Prometheus + Grafana 18 面板） |
| `tests/` | `unit/` 纯逻辑、`contract/` 接口契约、`integration/` 5 个真依赖文件、`support/` 替身 |
| `tools/` | `curl_e2e.ps1`、`kafka_e2e_check.py`、`worker_e2e_check.py`、`gen_grpc_stubs.ps1`、`ingest_bench*.py` |
| `docs/` | 01–11 SRS + 12 故障史 + 13 技术设计 + 14 逐接口 curl 实测 |

## 4. 快速开始

### 4.1 起服务（uv sync，零外部依赖）

```powershell
uv sync                        # 按 uv.lock 建 .venv 并安装全部依赖
Copy-Item .env.example .env    # 至少填真实的 OPENAI_API_KEY（默认 DeepSeek）
uv run uvicorn app.main:app --reload
# Swagger UI : http://127.0.0.1:8000/docs
# 健康检查   : http://127.0.0.1:8000/api/v1/health
```

`.env.example` 出厂档是**隔离档**（`INFRA_BACKEND=real` + `TASK_RUNNER=kafka` +
`EMBEDDING_PROVIDER=siliconflow`），真实但要多起 Redis / Kafka / Worker。只想单进程跑通就改成
`INFRA_BACKEND=memory` + `TASK_RUNNER=inline`：代价说清楚——文档解析/切分/向量化跑在 API 进程里，
8MB 文档会让同机请求一起变慢，实测 49MB 能把 `/health` 拖到不应答。

### 4.2 起真基础设施（Redis / Kafka / MySQL / Milvus / MinIO）

```powershell
uv sync --extra redis --extra kafka --extra mysql --extra minio

docker compose -f deploy/infra/compose.yml up -d redis            # 任务仓储 / 总线 / 重试都在 Redis
docker compose -f deploy/infra/compose.yml --profile kafka up -d  # 单节点 KRaft，localhost:9092

# 建表（幂等，全部 CREATE ... IF NOT EXISTS，不含 DROP）：AI 独占 7 + 共享 2
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/001_init_schema.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/002_align_shared_tables.sql"  # 仅旧库
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/003_add_chunk_metadata.sql"   # 仅旧库
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/004_add_ingest_facts.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/verify_schema.sql"            # 断言 1/0/1/1/0/1/4/4

$env:INFRA_BACKEND="real"; $env:TASK_RUNNER="kafka"
uv run uvicorn app.main:app --reload      # 终端 1：API（投递方）
uv run python -m app.worker               # 终端 2：Worker（消费方），与 API 共用同一份 .env

uv run python tools/kafka_e2e_check.py    # 跨进程验真：建任务 → Kafka → Worker → 事件帧
```

`deploy/infra/compose.yml` 只起 Redis / Kafka / MinIO（三者各自独立，不合成一个 project）；MySQL 与 Milvus
用外部实例——Milvus 走官方 standalone compose，默认 `19530`(gRPC) / `9091`(HTTP 健康) / `2379`(etcd)，
集合与索引由启动期 `ensure_ready()` 幂等创建。

Worker 启动先自检，不满足就拒绝启动——这是故意的：

```text
$env:INFRA_BACKEND="memory"; uv run python -m app.worker
ERROR app.worker  worker.misconfigured  reason=任务存储不是共享的（INFRA_BACKEND=memory）
# 退出码 2
```

内存任务存储是**进程私有**的，Worker 拿到消息后查不到那行任务，表现为「消息被消费了但任务永远停在
`QUEUED`」——一个不报错、只静默转圈的故障。

### 4.3 起对内 gRPC（供网关的 `Chat` / `ChatStream`）

```powershell
$env:GRPC_ENABLED="true"   # 默认 false；未开启时 python -m app.grpc 以退出码 2 退出
$env:GRPC_HOST="127.0.0.1" # 默认只听回环；非回环地址必须 AUTH_ENABLED=true，否则启动校验直接拒绝
uv run python -m app.grpc  # 默认端口 50051
```

gRPC 是**独立进程**，不是「HTTP 启动时顺带开个端口」：两者的生命周期、并发模型与故障域都不同，
gRPC 挂了不该连带 SSE 一起挂。`Chat` / `ChatStream` 与 `POST /chat`、`POST /chat/stream` 共用同一套
service 与错误映射，行为等价；并发上限 `GRPC_MAX_CONCURRENCY=64`（LLM 是长尾 IO，给得比 HTTP 小）。
长驻 gRPC 进程的已知缺陷见 §9。

## 5. 配置与端口

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `INFRA_BACKEND` | `memory`（库内）/ `real`（`.env.example`） | `real` 下依赖不可用直接 `503 DEPENDENCY_UNAVAILABLE`，**不静默退化**成进程内实现 |
| `TASK_RUNNER` | `inline`（库内）/ `kafka`（`.env.example`） | `none` / `inline` / `kafka` 三档由配置决定装配，不是运行期分支 |
| `GRPC_ENABLED` / `GRPC_HOST` / `GRPC_PORT` | `false` / `127.0.0.1` / `50051` | 非回环 + `AUTH_ENABLED=false` 会被启动校验拒；无鉴权 gRPC 等于把可写接口开到网卡上 |
| `METRICS_ENABLED` / `METRICS_PORT` | `true` / `9100` | `METRICS_PORT=0` 是**由 OS 分配随机端口**，不是关闭；真实端口见启动日志 `app.metrics_ready` |
| `OTEL_ENABLED` / `OTEL_EXPORTER_OTLP_ENDPOINT` | `true` / `http://localhost:4317` | `OTEL_TRACES_SAMPLER_ARG=0.1` 为采样率，本地看全量设 `1.0` |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `hash` / `BAAI/bge-m3` / `1024` | 换模型必须同时改 `EMBEDDING_DIM` 与 `MILVUS_VECTOR_DIM`，否则启动期报 `VECTOR_DIM_MISMATCH` |
| `RERANKER_PROVIDER` / `RERANKER_MODEL` | `bge` / `BAAI/bge-reranker-v2-m3` | `siliconflow` 档走云端 `/v1/rerank` |
| `MILVUS_URI` / `MILVUS_COLLECTION` | `http://localhost:19530` / `ai_platform_chunks` | 有 `MILVUS_TOKEN` 时优先 Token 鉴权 |
| `MCP_SERVERS` / `MCP_STARTUP_TIMEOUT_SECONDS` | `{}` / `15` | JSON 配置，支持 `${VAR}` 引用密钥；字段表见 `docs/05` §2.2 |
| `AGENT_MAX_STEPS` / `AGENT_TIMEOUT_SECONDS` / `TOOL_TIMEOUT_SECONDS` | `8` / `30` / `10` | 请求里的 `max_steps` **只能调小**；Agent 超时返回 `504`，不编答案 |

| 端口 | 用途 |
| --- | --- |
| 8000 | HTTP / SSE（`/api/v1/*`，前缀由 `API_PREFIX` 决定） |
| 9100 | 指标独立端口（只服务 `GET /metrics`；主应用该路径返回 404） |
| 50051 | 对内 gRPC（默认关闭） |
| 6379 / 9092 / 3306 / 19530 / 9000 | Redis / Kafka / MySQL / Milvus / MinIO |

| 名称空间 | 具体键 / 主题 / 表 |
| --- | --- |
| Redis 短期上下文 | `ctx:{id}`、`ctx:owner:{id}`、`ctx:summary:{id}`、`lock:ctx:{id}` |
| Redis 任务 | `task:{id}`、`task:idem:{key}`、`task:user:{uid}`、`task:open`、`cancel:{id}`、`retry:zset` |
| Kafka | `ai.task.<type>`、死信 `ai.task.dlq` |
| Milvus | `ai_platform_chunks`、`ai_platform_memories`（1024 维） |
| MySQL | 15 张：AI 独占 7 + 共享 2（`idempotency_record` / `audit_log`）+ 网关独占 6 |

> 以上 10 组是真正会动的开关；其余字段（合计 150 个）见 `app/core/config.py` 与 `docs/13` §5，
> 本文不复制全量清单。

### 5.1 换 LLM 供应商

走 OpenAI 兼容协议，换供应商只改两个环境变量，代码一行不动：

| 供应商 | `OPENAI_BASE_URL` | `LLM_MODEL` |
| --- | --- | --- |
| DeepSeek（默认） | `https://api.deepseek.com/v1` | `deepseek-flash` / `deepseek-v4-pro` |
| 官方 OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 Ollama / vLLM | `http://localhost:11434/v1`（Ollama 默认；vLLM 默认 8000 与本服务端口冲突，换一个） | 本地模型名（key 随便填） |

> Embedding **不能**跟着这么换：DeepSeek 不提供 embedding。出厂档走硅基流动云端（需
> `SILICONFLOW_API_KEY`，按 `SILICONFLOW_EMBEDDING_CONCURRENCY` 分批并发），零依赖用 `hash`，
> 本地权重用 `bge`。换档等于换向量空间或维度，必须换 `MILVUS_COLLECTION`（否则召回静默变差）。

### 5.2 MCP 接入

在 `.env` 里写一条 JSON 就加一个 Server，不改代码：工具自动以 `mcp__{server}__{tool}` 注册进统一工具表，
Agent 与 `GET /tools` 立即可见。

```dotenv
# stdio：本地子进程（command 只能来自服务端配置，不能由请求参数决定）
MCP_SERVERS={"fs":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/docs"],"tools_allowlist":["read_file"],"write_tools":["write_file"]}}

# streamable_http：远端 Server（令牌用 ${VAR} 引用，不落明文）
MCP_SERVERS={"fetch":{"transport":"streamable_http","url":"http://localhost:3100/mcp","headers":{"Authorization":"Bearer ${MCP_FETCH_TOKEN}"}}}
```

```powershell
curl.exe -s http://127.0.0.1:8000/api/v1/mcp/servers   # 状态 / 工具数 / 最近错误 / 往返耗时
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/mcp/servers/fs/reload `
  -H "Content-Type: application/json" -d '{\"force\":false}'   # 只重载这一个 Server
```

`required=false` 的 Server 连不上只标 `unavailable`，不阻断启动；单 Server 走懒重连 + 行内熔断
（目标名 `mcp:{server}`）；写类工具由 `write_tools` 声明并留审计。

## 6. 接口索引（41 条）

路由按能力域分组，明细一律指向接口表，避免第二份会漂移的清单：

| 分组 | 条数 | 路径 | 接口表 |
| --- | --- | --- | --- |
| 健康检查 | 3 | `/health`、`/health/live`、`/health/ready` | `docs/14` §4 |
| 对话与模型 | 3 | `/chat`、`/chat/stream`、`/models` | `docs/03` §1 |
| Agent 与工具 | 4 | `/agent/run`、`/agent/run/stream`、`/tools`、`/tools/{name}/invoke` | `docs/04` §4 |
| 知识库与文档 | 11 | `/knowledge-bases*`、`/documents/*` | `docs/06` §2、§3 |
| 异步任务 | 5 | `/tasks*`（含 `/tasks/{id}/events` SSE） | `docs/08` §4 |
| Memory | 12 | `/memories*`、`/memory-settings`、`/conversations/{id}/context`、`/conversations/{id}/summary*` | `docs/07` §6 |
| MCP | 3 | `/mcp/servers*` | `docs/05` §5 |

三点口径，避免「41 / 42」这类数字被反复误读：

* **41** 是 `@router.*` 装饰器数；`tools/curl_e2e.ps1` 按「路径 + 方法」组合算是 **42 个组合 / 45 次请求**
  （含 3 个 SSE 端点），两者差 1 条，待逐条对齐（见 §9）。
* 统一错误信封为 `{"error":{"code","message","details"}}`，错误码总表与 SSE 帧规范见 `docs/02` §5、§6；
  常见：`503 DEPENDENCY_UNAVAILABLE`、`409 KB_NOT_EMPTY`、`404 SUMMARY_UNAVAILABLE`、`404 MCP_SERVER_NOT_FOUND`。
* 逐接口的可执行 curl 与真实依赖下的实测响应（含 409 / 404 这些**设计上的正确负例**）见
  [`docs/14-接口 curl 示例.md`](docs/14-接口%20curl%20示例.md)。

## 7. 关键取舍

| 项 | 选择 | 原因 / 留下的防线 |
| --- | --- | --- |
| Agent 链路 | 自研 Loop，不是 LangGraph | 三重护栅（步数 8 / 总时长 30s / 重复调用回注 `duplicate_call`）需要精确控制「何时回注、何时截断」；`app/agent/graph/` 的图保留为占位，**未接入**，`generate` 仍是 TODO |
| 向量库 | 设计原选 Qdrant，实现是 **Milvus** | `langchain-milvus` 集成与生态更顺；`user_id`/`kind` 标量倒排索引必须建——没有它不会报错，只静默丢召回 |
| 任务执行 | 配置决定装配（`none`/`inline`/`kafka`） | 大文件解析/向量化不拖垮在线请求；Worker 自己在错误配置下 exit 2，因为内存任务存储跨进程只会静默转圈 |
| 两库共存 | 与网关共用一个 MySQL 库 | `idempotency_record` / `audit_log` 同库同名只能一张表，`001_init_schema.sql` 给的是并集定义，网关侧不再重复 `CREATE` |
| 对内通信 | `Chat`/`ChatStream` 走 gRPC，其余 HTTP 透传 | SSE 不进 gRPC，取消传播用连接断开表达；gRPC 独立进程，故障域与 HTTP 分开 |
| 短期上下文 | `memory` / `real` 两套实现，不自动降级 | `real` 下 Redis 不可用直接 503 —— 静默退化成进程内会让「多实例共享上下文」变成假象 |
| 重试 | Redis ZSET 延迟队列 + 补偿扫描 | 退避重试不占 Worker 并发额度；补偿扫描兜「建好了但没投出去」的 `PENDING` |
| 指标端口 | 独立 9100，主应用不暴露 `/metrics` | 指标是运维能力、不是可用性前置条件：端口被占不阻断启动，只用 `app.metrics_ready port=0` 提示；判断「抓到的是不是我们」看 `ai_build_info` 哨兵，不看 `up`（9100 常驻别的 Go exporter 时，job 是 `up` 但全是 `go_*`） |

## 8. 质量门禁（本机实测）

| 命令 | 实测结果 |
| --- | --- |
| `uv run ruff format --check .` | `232 files already formatted`（exit 0） |
| `uv run ruff check .` | `All checks passed!`（exit 0） |
| `uv run mypy app` | `Success: no issues found in 147 source files`（exit 0） |
| `uv run pytest` | **1698 collected / 1688 passed / 10 skipped / 0 failed**，112.8 s |
| `uv run pytest --cov=app --cov-report=term-missing` | **89%**（11561 语句 / 1315 未覆盖） |

* 10 个 `@pytest.mark.integration` 用例在本次运行中**按设计跳过**：未起 Redis / MySQL / Milvus / MinIO，
  不可达时 1 秒内 skip（先 TCP 探测，不叫 redis-py 的重试退避带着用例干等）；覆盖率 89% 同样受此影响。
* 集成层 5 个文件覆盖 4 类真依赖：Redis（Lua 脚本、pub/sub 不重放、并发幂等键、`ZSET` 原子认领）、
  MySQL 仓储、MySQL 记忆仓储、Milvus 记忆索引、MinIO 对象存储。Redis 夹具会 `FLUSHDB`，
  因此**拒绝指向 db 0**（默认 `AI_TEST_REDIS_URL=redis://localhost:6379/15`）。
* 外部依赖一律走替身（`tests/support/`：脚本化 LLM、确定性向量化、假 MCP Server、假消息总线），
  所以「片段顺序」「断连是否停掉上游」「熔断是否真的打开」都能被机械断言。
* 接口级验真另走脚本：`tools/curl_e2e.ps1` 输出 `REQUESTS=45 PASSED=39 UNEXPECTED=0`，
  39 个 2xx + 6 个白名单内正确负例，**退出码即结论**（0 = 无预期外失败，1 = 打印 `UNEXPECTED:` 明细）。

```powershell
uv run pytest                      # unit + contract + integration
uv run pytest -m "not integration" # 不碰真基础设施（无 Docker 时用）
uv run ruff check . ; uv run mypy app
```

## 9. 现状与差距

### 9.1 未实现 / 占位

| 项 | 状态 | 说明 |
| --- | --- | --- |
| LangGraph 状态图 | **占位，未接入** | `app/agent/graph/` 的 `retrieve` / `generate` 两个节点都返回空值，没有调用方；主链路是 `app/agent/loop.py` |
| Kubernetes 清单 | **没有** | 仓库内只有 Docker Compose（本地基础设施 + 可观测栈），没有 Deployment / Service / Helm |
| CI 流水线 | **没有** | 仓库无任何 CI 配置；§8 的数字全部是本机手工执行的结果 |
| 检索评测 | 脚本在，评测集缺 | `tests/eval/` 有入口，真实 recall@5 未跑（需要 BGE 权重与标注集） |
| MCP `prompts` / `sampling` | 按 SRS 明示不做 | 只做 tools 能力 |
| Go 侧未做项 | 见根 README | 审计查询接口、`SIGTERM` 实机验收等不在本仓 |

### 9.2 已知长期缺陷

| 症状 | 影响面 | 现状 |
| --- | --- | --- |
| 长驻 Python gRPC 进程运行约 3 小时后，所有 `Chat` 退化为 `503 DEPENDENCY_UNAVAILABLE`；Python 直连探测同样失败（排除网关侧） | 只影响 gRPC 通道，重启进程即恢复 | **根因未定位**，无自动告警，只表现为「网关突然全红」。现场记录见 `ai-platform-go/docs/08` §6.4；当前手段是监控 503 比例 + 定期重启 |

### 9.3 文档与实测口径不一致（以代码与本文数字为准）

| 项 | 文档口径 | 实测 |
| --- | --- | --- |
| Python 路由数 | 41（openapi）/ 42（curl 冒烟） | `app/api/**` **41** 个 `@router.*`；curl 脚本 42 个路径+方法组合，差 1 条待逐条对齐 |
| `ai_*` 指标数 | 曾记 20 | `metrics.py` **19** 个 |
| pytest 用例数 | 曾记 1284 / 1616 | **1698 collected / 1688 passed / 10 skipped** |
| 覆盖率 | 90% | **89%**（未起基础设施，10 个集成用例跳过） |
| 向量库 | 设计文档写 Qdrant | 实现是 **Milvus** |
| 服务拆分 | 设计原写 User / Conversation / Task 三个微服务 | Go 侧**单进程多模块**；Python 侧同样是单进程内按域分包 |

## 10. 文档索引

| 文档 | 内容 |
| --- | --- |
| [`docs/README.md`](docs/README.md) | SRS 总览：范围边界、阅读路径、术语、现状 |
| [`docs/01`–`11`](docs) | 需求规格：总体需求 · 接口与错误码 · 对话流式 · Agent · MCP · RAG · Memory · 异步任务 · 数据模型 · 非功能与可观测 · 验收标准 |
| [`docs/12-实现问题记录.md`](docs/12-实现问题记录.md) | 故障史：症状 / 根因 / 修法 / 留下的防线 |
| [`docs/13-技术设计说明.md`](docs/13-技术设计说明.md) | 分层与装配、各链路设计、扩展点、已知取舍 |
| [`docs/14-接口 curl 示例.md`](docs/14-接口%20curl%20示例.md) | 逐接口 curl + 真实依赖下实测响应（含正确负例） |
