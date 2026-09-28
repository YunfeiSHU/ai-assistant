# ai-platform

基于 **FastAPI + LangChain / LangGraph** 的 AI 平台后端，内置 RAG 检索增强（向量召回 + BGE 重排序）、
Agent 工具调用循环（Agent Loop，含步数/时长/重复调用三重护栅）、
MCP（Model Context Protocol）工具接入（多 Server / 两种传输 / 命名空间 / 熔断重连），
以及完整的可观测性（OTel 链路 + Prometheus 指标 + 熔断器 + 独立指标端口）。

## 技术栈

| 层次 | 选型 |
| --- | --- |
| Web 框架 | FastAPI + Uvicorn |
| 数据校验 / 配置 | Pydantic v2 + pydantic-settings |
| LLM 编排 | LangChain + LangGraph |
| 向量化 | Transformers + Sentence Transformers（默认 `BAAI/bge-m3`） |
| 重排序 | FlagEmbedding（默认 `BAAI/bge-reranker-v2-m3`） |
| 向量库 | Milvus（PyMilvus + langchain-milvus） |
| 工具协议 | MCP Python SDK |
| 环境 / 依赖管理 | uv |

## 环境要求

- **Python 3.12**：已由 `.python-version` 固定，uv 会自动使用托管解释器，无需手动安装
  （`torch` / `transformers` / `FlagEmbedding` 生态对刚发布的 3.14 支持尚不完整，故不用系统默认版本）
- **uv** ≥ 0.5：<https://docs.astral.sh/uv/getting-started/installation/>

## 快速开始

```powershell
# 1. 按 uv.lock 创建 .venv 并安装全部依赖
uv sync

# 2. 准备环境变量
Copy-Item .env.example .env
# 然后编辑 .env，至少填入真实的 OPENAI_API_KEY（默认用 DeepSeek，见下方说明）

# 3. 启动开发服务器（热重载）
uv run uvicorn app.main:app --reload

# 4. 接口文档
#    Swagger UI : http://127.0.0.1:8000/docs
#    ReDoc      : http://127.0.0.1:8000/redoc
#    健康检查   : http://127.0.0.1:8000/api/v1/health
```

可选（M7 异步任务用）：把可选依赖与本地基础设施起来，就能跑「API 投递 → Worker 消费」的真形态：

```powershell
# Redis / Kafka 驱动属于可选依赖，默认不装（装了他们，默认的 memory + inline 依然照旧工作）
uv sync --extra redis --extra kafka

# 只起 Redis（任务进度 / 幂等键 / 退避重试 ZSET 都在这里）
docker compose -f deploy/infra/compose.yml up -d redis

# 连 Kafka 一起起（单节点 KRaft，监听 localhost:9092）
docker compose -f deploy/infra/compose.yml --profile kafka up -d

# 然后另开一个终端跑独立 Worker（它自己会拒绝在 INFRA_BACKEND != real 下启动）
$env:INFRA_BACKEND="real"; $env:TASK_RUNNER="kafka"; uv run python -m app.worker
```

## 目录结构

```text
ai-platform/
├── .venv/                  # uv 创建的虚拟环境（不入库）
├── .env                    # 本地环境变量（不入库）
├── .env.example            # 环境变量模板
├── .python-version         # 固定 Python 3.12
├── pyproject.toml          # 依赖与工具配置（唯一事实来源）
├── uv.lock                 # 锁定版本，保证可复现
├── README.md
├── docs/                   # 11 篇接口级 SRS + 实现问题记录 + 技术设计说明
├── deploy/                 # 部署与基础设施脚本
│   ├── mysql/              # 建表 001 + 共享表升级 002 + 自检 verify（同库 15 张表）
│   ├── infra/              # 本地基础设施：Redis（默认）+ Kafka（--profile kafka）
│   └── observability/      # 本地可观测栈：Jaeger + Prometheus + Grafana（含 18 面板看板）
├── tools/
│   └── kafka_e2e_check.py  # 跨进程验真脚本（API 建任务 → Kafka → Worker 执行 → 收到事件）
├── tests/
│   ├── conftest.py         # 夹具：配置 / 应用 / 客户端 / 对话・Agent・MCP 替身
│   ├── support/            # 测试替身（脚本化 LLM / 确定性向量化 / 假 MCP Server / 假消息总线）
│   ├── unit/               # 纯逻辑单测（不启 HTTP）
│   ├── contract/           # 接口契约测试（错误信封 / 鉴权 / SSE / 分页 / 可观测）
│   └── integration/        # 真基础设施（当前：真 Redis）；不可达时 1 秒内 skip
└── app/
    ├── main.py             # FastAPI 入口（create_app + lifespan）
    ├── config.py           # pydantic-settings 全局配置（唯一事实来源）
    ├── core/               # 与框架无关的基础设施
    │   ├── errors.py       # 错误码枚举 + 规格表 + AppError
    │   ├── ids.py          # ULID 资源 ID（同毫秒单调）
    │   ├── context.py      # trace_id / span_id / request_id 上下文
    │   ├── logging.py      # 结构化日志 + 敏感字段脱敏
    │   ├── middleware.py   # 请求上下文、请求体大小限制、访问日志
    │   ├── security.py     # JWT 校验
    │   ├── tokens.py       # token 计数与截断
    │   ├── pagination.py   # 游标分页
    │   ├── sse.py          # SSE 帧构造 + 心跳
    │   └── health.py       # 依赖探活注册表
    ├── api/
    │   ├── deps.py         # 依赖注入（配置 / 当前用户 / 分页 / 服务）
    │   ├── exception_handlers.py
    │   └── routes/         # 路由，统一挂到 api_router
    ├── schemas/            # Pydantic 请求 / 响应模型（逐条对齐 SRS）
    ├── llm/                # LLM 协议 + OpenAI 兼容适配器 + 异常映射
    ├── services/           # 业务编排层（context 装配 / chat 编排 / agent 编排）
    ├── tools/
    │   ├── base.py         # ToolSpec / ToolContext / ToolOutcome / BuiltinTool
    │   ├── registry.py     # 工具注册表（重名 fail-fast、白/黑名单过滤）
    │   ├── executor.py     # 执行器（读并发/写串行、超时、截断、错误归一）
    │   ├── service.py      # 对外列表/调试调用
    │   └── builtin/        # kb_retrieve / calculator / current_time / http_fetch / memory_save / memory_search
    ├── memory/             # 记忆层：短期上下文 / 摘要 / 长期记忆（含 Redis 实现）
    │   ├── context_store.py # ConversationStore 协议 + 内存实现 + 503 占位
    │   ├── redis_store.py  # Redis 实现（LTRIM + TTL + SET NX 锁 + Lua 释放）
    │   ├── long_term.py    # 长期记忆正文/元数据 + 哈希去重 + 游标分页
    │   ├── vector_index.py # 记忆向量索引（只管向量，与关系库按 mem_id 对齐）
    │   ├── summary.py      # 摘要：触发条件 + 四段结构 + 增量合并 + 防抖
    │   ├── extractor.py    # 抽取：候选解析 + 敏感/疑问句/推测过滤
    │   ├── preferences.py  # 用户级开关与 top_n、清空冷静期
    │   └── tasks.py        # summary_build / memory_extract 处理器
    ├── storage/            # 知识库/文档/切片仓储（memory 实现 + real 的 503 占位）
    ├── tasks/              # 异步任务：状态机 + 仓储 + 投递器 + 按类型分派的分发器
    │   ├── models.py       # Task 领域模型 + 状态机（唯一事实来源）
    │   ├── store.py        # 进程内实现；redis_store.py 为跨进程实现
    │   ├── service.py      # 状态迁移 + 事件发布 + 队列容量闸（超限 503 OVERLOADED）
    │   ├── retry.py        # 退避重试队列（Redis ZSET + Lua 原子认领）
    │   ├── compensation.py # 补偿扫描：兜「建好了但没投出去」的 PENDING
    │   ├── events.py       # 事件总线（内存 / Redis pub/sub）
    │   ├── runner.py       # 三个执行器：none / inline / kafka
    │   ├── transport.py    # Kafka Producer/Consumer + 消息体
    │   └── stream.py       # GET /tasks/{id}/events 的进度流（快照 + 增量）
    ├── worker/             # 独立 Worker 进程（python -m app.worker）
    │   ├── __main__.py     # 启动自检（配置不对直接 exit 2）+ 信号优雅退出
    │   ├── loop.py         # 消费循环：领取 → 执行 → 提交偏移
    │   └── offsets.py      # 按分区连续水位线提交（避免乱序完成丢消息）
    ├── rag/
    │   ├── retriever.py    # Retriever 真实实现（召回 → 合并 → 重排 → 裁剪）
    │   ├── base.py         # Retriever 协议 + NullRetriever（降级用）
    │   ├── service.py      # 入库编排：上传 → 解析 → 切片 → 嵌入 → 落库
    │   ├── chunking.py     # 结构感知切片（标题/页码/原子块/合并）
    │   ├── parsers/        # pdf / docx / md / html / txt 解析（扩展名 + 魔数双校验）
    │   ├── embedding/      # EmbeddingService（BGE 向量化，懒加载 + 缓存）
    │   ├── reranker/       # RerankerService（BGE Reranker，懒加载）
    │   └── vectorstore/    # MilvusVectorStore + 进程内实现（测试用）
    ├── agent/
    │   ├── loop.py         # Agent Loop：推理 → 工具调用 → 回注（三重护栅）
    │   └── graph/          # LangGraph 状态图占位（retrieve → generate，未接入）
    ├── mcp/                # MCP 客户端：配置校验 / 两种传输 / 单 Server 状态机 / 多 Server 编排 / 工具适配
    └── observability/      # 可观测：指标门面 / OTel 链路 / 熔断器 / 独立指标端口服务
```

> `app/mcp` 与站点包 `mcp` 同名但不冲突：Python 3 使用绝对导入，`import mcp` 指向已安装的 SDK。

## 已实现接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 汇总健康（含各依赖探活结果） |
| `GET` | `/api/v1/health/live` | 存活探针（进程活着即 200） |
| `GET` | `/api/v1/health/ready` | 就绪探针（依赖不可用时 503） |
| `POST` | `/api/v1/chat` | 非流式对话（`stream=true` 会返回 `400`，请用 `/chat/stream`） |
| `POST` | `/api/v1/chat/stream` | 流式对话（SSE：`meta → reference* → token* → usage → done`） |
| `POST` | `/api/v1/agent/run` | Agent 非流式（工具调用 `tool_calls` 轨迹 + 步数/引用/降级原因） |
| `POST` | `/api/v1/agent/run/stream` | Agent 流式（`meta → (tool_call → tool_result → reference*)* → token* → usage → done`） |
| `GET` | `/api/v1/tools` | 工具清单（含 `parameters` JSON Schema，可按 `source`/`enabled` 过滤） |
| `POST` | `/api/v1/tools/{name}/invoke` | 调试调用（**仅 local/dev**；prod 返回 `404`；`write` 类强制 `dry_run`） |
| `GET` | `/api/v1/models` | 可用模型白名单（静态配置表） |
| `POST` | `/api/v1/knowledge-bases` | 新建知识库（切片策略、`CHUNK_STRATEGY_INVALID` 校验） |
| `GET` | `/api/v1/knowledge-bases` | 知识库列表（游标分页） |
| `GET/PATCH/DELETE` | `/api/v1/knowledge-bases/{kb_id}` | 详情 / 局部更新 / 删除（非空需 `force=true`） |
| `POST` | `/api/v1/knowledge-bases/{kb_id}/documents` | 上传入库（`file` 或 `text` 二选一，`202` + `task_id`） |
| `GET` | `/api/v1/knowledge-bases/{kb_id}/documents` | 文档列表（按状态过滤） |
| `POST` | `/api/v1/knowledge-bases/{kb_id}/search` | 检索调试（召回/重排分数、耗时） |
| `GET` | `/api/v1/documents/{doc_id}` | 文档详情（含切片参数快照） |
| `GET` | `/api/v1/documents/{doc_id}/chunks` | 切片列表（游标分页） |
| `DELETE` | `/api/v1/documents/{doc_id}` | 删除文档（`202`，级联删切片） |
| `GET` | `/api/v1/tasks` | 任务列表（状态/类型/资源过滤） |
| `GET` | `/api/v1/tasks/{task_id}` | 任务详情（进度、阶段、错误、可否取消/重试） |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | 取消（`RUNNING` 只置标记，由 Worker 在检查点退出） |
| `POST` | `/api/v1/tasks/{task_id}/retry` | 重试（`FAILED → QUEUED`，`QUEUED/RUNNING` 幂等不重复投递） |
| `GET` | `/api/v1/tasks/{task_id}/events` | **任务进度流（SSE）**：`progress` / `error` / `done` / `ping`；终态立即 `done` 并关流 |
| `GET` | `/api/v1/memories` | 长期记忆列表（`kind` / `expired` 过滤 + 游标分页） |
| `POST` | `/api/v1/memories` | 手动新增（`201`；重复内容不新建，而是累加 `hit_count`） |
| `GET/PATCH/DELETE` | `/api/v1/memories/{mem_id}` | 详情 / 修改（正文变化重新向量化）/ 删除（关系库 + 向量库双删） |
| `DELETE` | `/api/v1/memories?all=true` | 清空全部记忆（**必须显式带 `all=true`**，否则 `400`） |
| `GET/PUT` | `/api/v1/memory-settings` | 用户级开关与 `top_n`（关闭时读写记忆接口返回 `409`） |
| `GET` | `/api/v1/conversations/{id}/context` | 会话上下文 + 各片段 token 占用 + 裁剪明细（排障用） |
| `DELETE` | `/api/v1/conversations/{id}/context` | 清空短期上下文（幂等；**不删**长期记忆） |
| `GET` | `/api/v1/conversations/{id}/summary` | 取摘要（未生成 → `404 SUMMARY_UNAVAILABLE`） |
| `POST` | `/api/v1/conversations/{id}/summary/rebuild` | 手动重建摘要（`202` + `task_id`） |
| `GET` | `/api/v1/mcp/servers` | MCP Server 列表（状态 / 工具数 / 最近错误 / 往返耗时，游标分页） |
| `GET` | `/api/v1/mcp/servers/{name}/tools` | 该 Server 注册后的工具清单（与 `/tools?source=mcp` 一致） |
| `POST` | `/api/v1/mcp/servers/{name}/reload` | 重载单个 Server（`{ "force": bool }`；不影响其它 Server） |

> 任务进度流的事件字段与广播规则见 `docs/08-§4.5`；`error` 帧里的 `retryable` 是
> 「错误码值得重试 **且** 重试预算没用完」两个条件同时成立才为 `true`，客户端按它决定要不要重试。
> 注意：`FAILED` 之后**不会**关流（`FAILED → QUEUED` 是合法迁移，自动重试可能随后发生）。

本地快速验证：

```powershell
# 非流式
curl.exe -s http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" `
  -d '{\"query\":\"用一句话介绍你自己\",\"use_memory\":false,\"use_rag\":false}'

# 流式（-N 关闭 curl 侧缓冲，才能看到逐段到达）
curl.exe -N http://127.0.0.1:8000/api/v1/chat/stream -H "Content-Type: application/json" `
  -d '{\"query\":\"数到三\",\"use_memory\":false,\"use_rag\":false}'

# Agent：模型会自己决定调 calculator 还是 current_time
curl.exe -s http://127.0.0.1:8000/api/v1/agent/run -H "Content-Type: application/json" `
  -d '{\"query\":\"(12.5-8)/8 是多少百分点\",\"use_memory\":false,\"use_rag\":false}'

# 看看有哪些工具（含发给上游的 JSON Schema）
curl.exe -s http://127.0.0.1:8000/api/v1/tools

# 写一条长期记忆，再用它问一次（第二次对话应带上「记忆」段）
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/memories -H "Content-Type: application/json" `
  -d '{\"content\":\"用户偏好用表格回答\",\"kind\":\"preference\"}'

# 看上下文装配明细（各段 token 与裁剪原因，排障首选）
curl.exe -s http://127.0.0.1:8000/api/v1/conversations/{conversation_id}/context
```

> 短期上下文默认是**进程内**实现（重启即丢）；想用 Redis 托管则需要
> `uv sync --extra redis` 并把 `INFRA_BACKEND` 设为 `real`。`real` 下 Redis 不可用会直接
> `503 DEPENDENCY_UNAVAILABLE`，**不会**静默退化成进程内实现。

## 常用命令

```powershell
uv sync                      # 同步依赖（新增/删除包后执行）
uv add <package>             # 添加运行时依赖
uv add --dev <package>       # 添加开发依赖
uv lock --upgrade            # 全量升级并刷新 uv.lock
uv run python -c "import app"  # 在虚拟环境里执行任意命令
uv run pytest                # 跑测试
uv run pytest --cov=app --cov-report=term-missing   # 带覆盖率（docs/11 §6 的验收命令）
uv run ruff check .          # 代码检查
uv run ruff format .         # 代码格式化
uv run mypy app              # 类型检查
```

> 说明：`uv.lock` 应当入库，以保证任何机器上 `uv sync` 得到完全一致的依赖树。

## 配置说明

配置全部集中在 `app/config.py` 的 `Settings` 类，字段可用同名环境变量覆盖（不区分大小写）。
关键项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | 空 / `https://api.deepseek.com/v1` | **OpenAI 兼容协议**，默认已接 DeepSeek |
| `LLM_MODEL` | `deepseek-flash` | DeepSeek 官方模型：`deepseek-flash`（默认）/ `deepseek-v4-pro` |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | 1024 维、多语言，对应 `EMBEDDING_DIM` |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | BGE 交叉编码器重排模型 |
| `*_DEVICE` | `cpu` | 有 GPU 时改为 `cuda`（会自动启用 fp16） |
| `MILVUS_URI` | `http://localhost:19530` | 有 `MILVUS_TOKEN` 时优先用 Token 鉴权 |
| `CORS_ORIGINS` | `["*"]` | JSON 数组格式 |
| `AGENT_MAX_STEPS` | `8` | Agent 单请求工具调用轮数上限（请求里的 `max_steps` **只能调小**） |
| `AGENT_TIMEOUT_SECONDS` | `30` | Agent 单请求总时长预算（超出返回 `504`，不编答案） |
| `TOOL_TIMEOUT_SECONDS` | `10` | 单个工具默认超时（工具自身 `timeout_seconds` 优先，封顶 60s） |
| `TOOL_HTTP_FETCH_ENABLED` | `false` | `http_fetch` 是**外发请求**能力，默认关闭（需显式开启） |
| `TOOL_DENYLIST` / `TOOL_WRITE_ALLOWLIST` | `[]` | 运维级黑名单；与写操作放行名单不得相交（启动期校验） |
| `DEBUG_TOOLS_ENABLED` | `true`（local/dev） | 为 `false` 时 `/tools/{name}/invoke` 返回 `404` |
| `MEMORY_ENABLED` | `true` | 长期记忆总开关（用户级设置只能在其为 `true` 时打开） |
| `MEMORY_TTL_DAYS` / `MEMORY_MAX_MESSAGES` / `MEMORY_RECENT_TURNS` | `7` / `200` / `10` | 短期上下文：TTL、条数上限、每次注入最近几轮 |
| `MEMORY_TOP_N` / `MEMORY_SCORE_THRESHOLD` | `3` / `0.45` | 长期记忆注入条数与相似度门限 |
| `MEMORY_DEDUPE_THRESHOLD` / `MEMORY_MIN_CONFIDENCE` | `0.92` / `0.7` | 语义去重阈值与抽取置信度下限（`[0.85, 0.92)` 两条并存） |
| `MEMORY_CLEAR_COOLDOWN_HOURS` | `24` | 清空全部记忆后不再重新抽取旧内容的冷静期 |
| `SUMMARY_ENABLED` / `SUMMARY_MIN_NEW_MESSAGES` / `SUMMARY_TRIGGER_RATIO` | `true` / `20` / `0.8` | 摘要开关与两个触发条件（新增消息数 / 历史 token 占比） |
| `SUMMARY_KEEP_RECENT_TURNS` / `SUMMARY_TOKEN_BUDGET` / `SUMMARY_DEBOUNCE_SECONDS` | `10` / `1200` / `300` | 保留原文轮数、摘要长度上限、同会话生成防抖窗口 |
| `REDIS_URL` | `redis://localhost:6379/0` | `INFRA_BACKEND=real` 时的短期上下文载体（需 `uv add redis`） |
| `MCP_SERVERS` | `{}` | MCP Server 配置（JSON）；字段表见 `docs/05-§2.2`，支持 `${VAR}` 引用密钥 |
| `MCP_STARTUP_TIMEOUT_SECONDS` | `15` | 启动期连接全部 MCP Server 的**总**超时（并发建连，超时者标 `unavailable`） |
| `OTEL_ENABLED` / `OTEL_SERVICE_NAME` | `true` / `ai-platform` | 链路追踪开关与服务名（`otel_enabled=false` 时不建 provider） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点（Jaeger / Collector） |
| `OTEL_TRACES_SAMPLER_ARG` | `0.1` | 采样率（`0~1`）；本地想看全量链路就设 `1.0` |
| `METRICS_ENABLED` / `METRICS_HOST` / `METRICS_PORT` | `true` / `0.0.0.0` / `9100` | 指标端口总开关、绑定地址、端口（**`0` = 由 OS 分配随机端口**，不是关闭） |
| `LOG_LEVEL` | `INFO`（local 为 `DEBUG`） | 日志级别；`log_format=console` 时本地彩色输出 |

### MCP 接入

在 `.env` 里写一条 JSON 即可加一个 Server，**不需要改代码**（工具会自动以
`mcp__{server}__{tool}` 注册进统一工具表，Agent 与 `/tools` 立即可见）：

```dotenv
# stdio：本地子进程（注意 command 只能来自服务端配置，不能由请求参数决定）
MCP_SERVERS={"fs":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/docs"],"tools_allowlist":["read_file"],"write_tools":["write_file"]}}

# streamable_http：远端 Server（令牌用 ${VAR} 引用，不落明文）
MCP_SERVERS={"fetch":{"transport":"streamable_http","url":"http://localhost:3100/mcp","headers":{"Authorization":"Bearer ${MCP_FETCH_TOKEN}"}}}
```

```powershell
# 看状态（`required=false` 的 Server 连不上只标 unavailable，不影响启动）
curl.exe -s http://127.0.0.1:8000/api/v1/mcp/servers
# 改完上游工具后重载单个 Server（不影响其它 Server）
curl.exe -s -X POST http://127.0.0.1:8000/api/v1/mcp/servers/fs/reload -H "Content-Type: application/json" -d '{\"force\":false}'
```

### 切换 LLM 供应商

因为走的是 OpenAI 兼容协议，换供应商**只需改两个环境变量**，代码一行不用动：

| 供应商 | `OPENAI_BASE_URL` | `LLM_MODEL` |
| --- | --- | --- |
| DeepSeek（默认） | `https://api.deepseek.com/v1` | `deepseek-flash` / `deepseek-v4-pro` |
| 官方 OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 vLLM / Ollama | `http://localhost:8000/v1` | 本地部署的模型名（key 随便填） |

> ⚠️ **Embedding 不能这样换**：DeepSeek 不提供 embedding 接口，向量化仍由本地 BGE 模型完成，
> 因此 `EMBEDDING_MODEL` 必须保持为可下载的 HuggingFace 模型名。

模型加载是**惰性**的：只有第一次真正调用 `embed` / `rerank` 时才会下载权重，
应用启动不会拉取模型。国内网络可在 `.env` 中打开 `HF_ENDPOINT=https://hf-mirror.com` 加速。

## Milvus

本地起一个 Milvus（Standalone）：

```powershell
# 需要先安装 Docker Desktop
Invoke-WebRequest https://github.com/milvus-io/milvus/releases/download/v2.5.4/milvus-standalone-docker-compose.yml -OutFile docker-compose.yml
docker compose up -d
# 默认端口：19530(gRPC) / 9091(HTTP 健康检查) / 2379(etcd)
```

## MySQL

表结构定义在 `docs/09-数据存储模型.md` §2，已固化为可执行脚本。
**两个服务共用一个库 `ai_platform`，合计 15 张表**（AI 独占 7 + 共享 2 + 网关独占 6）：

```powershell
# 1. 建库 + AI 独占 7 张表 + 共享 2 张表（幂等：全部 CREATE ... IF NOT EXISTS，不含任何 DROP）
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/001_init_schema.sql"

# 2. 仅「已存在旧库」需要：给共享表补 method / request_hash / ip / user_agent 并扩宽 uk_idem
#    （CREATE ... IF NOT EXISTS 对已存在的表是空操作，不会补列）
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/002_align_shared_tables.sql"

# 3. 仅「已存在旧库」需要：给 document_chunk 补 metadata JSON 列（M8 固化切分参数）
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/003_add_chunk_metadata.sql"

# 4. 网关独占的 6 张表（脚本在 go-services 仓库目录下，仍打在同一个库）
mysql --default-character-set=utf8mb4 -u root -p -e "source ../go-services/deploy/mysql/001_gateway_tables.sql"

# 5. 两侧自检：断言值应依次为 1/0/1/1/0/1/4/4 与 6/15/1/0/0/1/2/0/1/0/0/1/0/4/0/1
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/verify_schema.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source ../go-services/deploy/mysql/verify_schema.sql"
```

> `idempotency_record` 与 `audit_log` 是**两个服务共享**的表：同库同名只能有一张表，
> 所以 `001_init_schema.sql` 给出的是**并集**定义（网关要的 `method` / `request_hash` /
> `ip` / `user_agent` 一并包含），网关侧不再重复 `CREATE`。

> **Windows 注意**：不要写 `Get-Content 文件.sql | mysql`（PowerShell 管道会弄坏编码），
> 也尽量不要让脚本路径含中文（mysql 客户端按 ANSI 解释路径，实测直接报
> `Failed to open file ... error: 2`）。用 mysql 自带的 `source` 命令，并保持路径为 ASCII。

建表脚本对 `docs/09` 有三处已记录的偏差（`deleted_key` 生成列、`user_settings.cleared_at`、
`user_memory.source`），原因见 `docs/09-§2.9` 与 `docs/12-§10`。

> 默认 `INFRA_BACKEND=memory` 时不连 MySQL。切到 `real` 后，仓储由
> `app/storage/mysql.py`（知识库/文档/切片）与 `app/memory/mysql_repo.py`（长期记忆）
> 用 **SQLAlchemy Core** 实现，另有一份 `app/memory/milvus_index.py` 负责长期记忆的向量索引。
> 依赖装在可选依赖组里：`uv sync --extra mysql --extra minio --extra redis --extra kafka`。
>
> **任一依赖初始化失败都不会让服务起不来**：仓储退化为 `Unavailable*`（写接口答
> `503 DEPENDENCY_UNAVAILABLE`），启动期自检只记日志；连接池按 DSN 进程内共享并引用计数，
> 关停时统一 dispose（见 `app/core/db.py`）。
> （当前由 `app/storage/unavailable.py` 返回 `503 DEPENDENCY_UNAVAILABLE`）——
> 表先建好，是为了让仓储实现能直接开工。注意这一项**不属于 M6/M7**
> （`docs/01-§7` 定义 M6 = MCP + 可观测、M7 = 异步任务），而是后续工作量。

## 下一步 TODO

- [x] **M3 RAG 闭环**：`Retriever` 真实实现（召回 top-k → 合并相邻 → 重排 top-n → token 预算裁剪）+
      知识库/文档 CRUD + 上传解析入库；回答里的 `[n]` 与 `references[].index` 打通，
      越界引用会被裁剪并记 `chat.citation_out_of_range_removed`
- [x] **M4 Agent 闭环**：工具注册表 + 执行器 + Agent Loop（步数/总时长/重复调用三重护栅）+
      `POST /agent/run` 与 SSE、`GET /tools` 与调试调用；`use_tools=true` 自动转交 Agent 链路；
      工具改走「pydantic 模型 → `model_json_schema()`」单一事实来源，参数不合法作为**可回复结果**回注
- [x] **M5 Memory 闭环**：短期上下文（内存 + Redis 两套实现、`SET NX` 锁、摘要覆盖过滤）、
      摘要压缩（四段结构 / 增量合并 / 5 分钟防抖）、长期记忆（哈希 + 语义两层去重、
      `0.85/0.92` 双阈值、过期与清空冷静期、作为**独立 system 段**注入）、
      `memory_save` / `memory_search` 两个内置工具、`TaskDispatcher` 按任务类型分派；
      轮末抽取与摘要**两条路径都接**（`/chat` 与 `/chat/stream`），幂等键带每轮标记
- [x] **M6 MCP 与可观测**：
      MCP 配置校验（未知字段报错、`${VAR}` 展开、名字/超时约束）、
      stdio 与 `streamable_http` 两种传输（适配层把 SDK 差异关在 `app/mcp/session.py`）、
      多 Server 并发启动与总超时（`required=true` 失败则拒绝启动）、
      工具命名空间 `mcp__{server}__{tool}` + allowlist/denylist + `write_tools` 审计、
      单 Server 状态机（懒重连 + 行内熔断 `mcp:{server}`）、三个 `/mcp/servers*` 接口；
      **可观测**：20 个 Prometheus 指标（`endpoint` 标签取路由模板、无高基数字段）、
      独立指标端口（主应用不暴露 `/metrics`）、OTel 链路（**Jaeger trace id == 日志/响应头**）、
      熔断器状态机、`deploy/observability/` 一键起 Jaeger + Prometheus + Grafana（18 面板）
- [x] **M7 异步任务与 Worker**：三个执行器（`none` / `inline` / `kafka`）、
      Redis 任务仓储（幂等键 + 游标分页 + 乐观锁）、Redis pub/sub 事件总线、
      退避重试 ZSET（Lua 原子认领）、补偿扫描、独立 Worker 进程（`python -m app.worker`，
      配置不对直接 `exit 2`）、`GET /tasks/{id}/events`（SSE 进度：快照 + 增量 + 心跳），
      `deploy/infra/compose.yml` 一键起 Redis / Kafka；真机验证：真 Kafka + 两个真进程跑通
      （`tools/kafka_e2e_check.py`）
- [x] **M8 真实基础设施落地**：
      MySQL 仓储（`app/storage/mysql.py` + `app/memory/mysql_repo.py`，SQLAlchemy Core，
      按 DSN 共享连接池 + 引用计数、唯一键冲突映射成领域错误、原子 `hit_count + 1`、
      行值游标分页）、MinIO 对象存储（原文档字节落桶）、
      Milvus 长期记忆集合（`ai_platform_memories`：HNSW/COSINE + `user_id`/`kind`
      **倒排索引**，没有标量索引就会静默丢召回）、
      `document_chunk.metadata` 迁移（`deploy/mysql/003_add_chunk_metadata.sql`）、
      启动期 MySQL 建表自检、依赖故障统一分类成 `503`（`app/core/db.py`）；
      真机验证：`INFRA_BACKEND=real` 下全部 **42 个「路径 + 方法」组合 / 45 次请求**
      的一键 curl 冒烟（`tools/curl_e2e.ps1`：39 个 2xx + 6 个白名单内的正确负例，
      0 个非预期失败，退出码表示结论）+ 5 个真依赖集成测试文件
      （MySQL 仓储 / MySQL 记忆仓储 / Milvus 记忆索引 / MinIO 对象存储 / Redis），
      逐接口实测响应见 `docs/14-接口 curl 示例.md`
- [x] 真实环境（`INFRA_BACKEND=real`）下 Milvus 集合与长期记忆向量索引的建表/建索引
      （启动期 `ensure_ready()` 幂等建集合 + 建索引，落地情况见 `docs/14-接口 curl 示例.md`）

## 对外接口 curl 示例与一键验证

[`docs/14-接口 curl 示例.md`](docs/14-接口%20curl%20示例.md) 给出**每一个对外接口**的
可直接执行 curl 命令，以及**真实依赖下实测的响应** —— 包括 409 / 404 这些
「设计上的正确负例」（取消已完成的任务、查未配置的 MCP Server），
它们和 2xx 一起被记录下来，否则错误分支等于没验证。

```powershell
# 前提：依赖已起、服务已启动、INFRA_BACKEND=real（见 docs/14-§2）
powershell -NoProfile -ExecutionPolicy Bypass -File tools/curl_e2e.ps1
# LOG=…\curl_log.txt
# REQUESTS=45 PASSED=39 UNEXPECTED=0
```

脚本遍历全部 42 个「路径 + 方法」组合（共 45 次请求，含 3 个 SSE 端点），
把非 2xx 与白名单里的正确负例逐条比对，
最后**用退出码表示结论**：

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部请求要么 2xx，要么落在白名单里的正确负例上 |
| `1` | 存在非预期失败（脚本会打印 `UNEXPECTED:` 明细） |

「跑通了没有」因此是机器可判定的，不靠人眼看日志。

## 异步任务（Redis / Kafka / Worker / SSE）

三个执行器**由配置决定装配**（而不是运行期分支）：

| `TASK_RUNNER` | 谁在跑 | 任务存储要求 | 适用 |
| --- | --- | --- | --- |
| `none` | 只建任务不执行 | 任意 | 契约测试 / 演示「排队」 |
| `inline` | API 进程自己跑 | 任意 | 单进程开发（默认） |
| `kafka` | **独立 Worker 进程** | 必须 `INFRA_BACKEND=real` | 生产形态 |

```powershell
# 1. 起基础设施
docker compose -f deploy/infra/compose.yml up -d redis            # 任务仓储/总线/重试都在 Redis
docker compose -f deploy/infra/compose.yml --profile kafka up -d   # 再加单节点 Kafka

# 2. API（投递方）
$env:INFRA_BACKEND="real"; $env:TASK_RUNNER="kafka"
uv run uvicorn app.main:app --reload

# 3. Worker（消费方，另开终端）
uv run python -m app.worker

# 4. 看进度流（-N 关缓冲，--max-time 自己控制等多久）
curl.exe -sS -N --max-time 30 -D - http://127.0.0.1:8000/api/v1/tasks/{task_id}/events -H "Authorization: Bearer <token>"
```

Worker 启动会先自检，不满足就直接拒绝启动：

```text
$env:INFRA_BACKEND="memory"; uv run python -m app.worker
ERROR app.worker  worker.misconfigured  reason=任务存储不是共享的（INFRA_BACKEND=memory）
      hint=Worker 与 API 必须是两个进程，请用 INFRA_BACKEND=real（Redis）
# 退出码 2
```

这是故意的：内存任务存储是**进程私有**的，Worker 拿到消息后查不到那行任务，
表现为「消息被消费了但任务永远停在 `QUEUED`」—— 一个不报错、只静默转圈的故障。

跨进程验真（不启 uvicorn，用一个脚本把 API 侧与 Worker 侧都跑一遍）：

```powershell
$env:INFRA_BACKEND="real"; $env:TASK_RUNNER="kafka"
uv run python tools/kafka_e2e_check.py   # 建任务 → 投 Kafka → 等 Worker 执行 → 打印事件帧
```

> 脚本用 `memory_extract` 任务类型，因为它不需要 MySQL / MinIO / Milvus，
> 能在只起 Redis + Kafka 的前提下跑通全链路。

## 可观测性

应用侧只需开三个开关（`.env`）：

```dotenv
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
METRICS_ENABLED=true
METRICS_PORT=9100
```

然后一键起本地观测栈（Jaeger 看链路、Prometheus 抓指标、Grafana 看板）：

```powershell
# 在 ai-platform/ 目录下
docker compose -f deploy/observability/compose.yml up -d
# Grafana     http://localhost:3000   （匿名可看；admin/admin）→「ai-platform 总览」
# Prometheus  http://localhost:9090
# Jaeger UI   http://localhost:16686
# 清理（含数据卷）
docker compose -f deploy/observability/compose.yml down -v
```

排障路径（场景 S10）：从响应头或日志里拿 `trace_id` →
Jaeger 搜该 id 得到完整 span 树（`chat.request → rag.retrieve → llm.invoke → tool.call`）→
同一 id 在日志里能查到召回 chunk、重排分数与降级原因。

> Prometheus 抓的是**宿主机**上的 `:9100/metrics`（compose 里用 `host.docker.internal`），
> 所以应用不必跑在容器里。`METRICS_PORT=0` 时端口由 OS 分配，
> 启动日志里的 `app.metrics_ready` 会打出真实端口。

### 指标端口排查（实测踩过）

**`METRICS_PORT` 与 `prometheus.yml` 的 `targets` 必须指同一个端口**。
端口被占时**不会**阻断启动（指标是运维能力，不是可用性前置条件），
只会有这样一对方便自查的日志：

```text
WARNING app.metrics.server  metrics.server_failed  port=9100 error=OSError
INFO    app.main            app.metrics_ready      host=0.0.0.0 port=0     # ← 0 表示没起来
```

**警惕「假绿」**：本机实测 9100 上常驻着另一个 Go exporter，
此时 Prometheus 里该 job 的 `health` 是 `up`、样本数也不为 0 ——
但抓到的全是 `go_*` / `node_*` 指标，`ai_*` 一个都没有。所以

```powershell
# 抓到的到底是不是我们？
curl.exe -s "http://localhost:9090/api/v1/label/__name__/values" | Select-String "ai_build_info"
```

判断手段就这一条：**看目标里有没有 `ai_build_info`**（它是常量 1 的哨兵指标），
而不是看目标是否 `up`。

## 测试

```powershell
uv run pytest                 # 全部（unit + contract + integration）
uv run pytest tests/unit      # 纯逻辑单测
uv run pytest tests/contract  # 接口契约测试
uv run pytest -m "not integration"  # 不碰真基础设施（CI/无 Docker 时用）
```

**集成测试层**（`tests/integration/`，当前只含真 Redis）：

```powershell
$env:AI_TEST_REDIS_URL="redis://localhost:6379/15"   # 默认就是它（db 15，不是 db 0）
uv run pytest tests/integration
```

* Redis 不可达时**1 秒内 skip**（先做 TCP 探测，不叫 redis-py 的重试退避带着 19 个用例耗 78 秒）；
* 每个用例前 `FLUSHDB`，所以**拒绝指向 db 0** —— 宁可 skip 也不帮你把开发库洗了；
* 这里跑的是替身替不掉的东西：真实连接上的 Lua 脚本、pub/sub 的「不重放」语义、
  并发创建同一幂等键只产生一条任务、两个并发认领者身上 `ZSET` 的原子性。

测试纪律（`docs/11-§2`）：

* 每个用例使用独立资源标识，互不干扰；
* 配置一律用 `Settings(_env_file=None, ...)` 构造，**不读开发机 `.env`**；
* 外部依赖（LLM / 向量库）一律走替身：`tests/support/fake_llm.py` 会记录发给模型的
  messages，因此「片段顺序」「断连是否停掉上游」这类事情都能被机械断言。
* MCP 的替身是 `tests/support/fake_mcp.py`：它直接替掉传输层（`open_session`），
  因此用例**不需要 npx / 外网 / 子进程**，却能真实跑完「建连 → 工具发现 → 调用 → 重载」
  与失败分支（按阶段划分故障开关：`fail_connect` / `fail_calls` / `delay`）。
* 可观测性也有契约测试（`tests/contract/test_observability.py`）：真的去独立端口抓一次
  `/metrics`、真的断言标签里不出现用户/路径参数、真的把熔断器打到 `open` 再看指标。
* 记忆相关替身见 `tests/support/memory.py`：`ScriptedEmbedding` 让**每个未登记文本落在互不
  相同的方向上**（否则 N 条不同记忆会在 0.92 语义去重下塌成 1 条，断言就假失败了），
  `MemoryScriptLLM` 按「system 里有没有『摘要』」区分摘要与抽取两类调用。

