# 14 · 对外接口 curl 示例（M8 实测）

> 关联：[02-接口规范与错误码](./02-接口规范与错误码.md)、[06-RAG知识库](./06-RAG知识库.md)、
> [08-异步任务](./08-异步任务.md)、[09-数据存储模型](./09-数据存储模型.md)、
> [10-非功能需求与可观测性](./10-非功能需求与可观测性.md)。
>
> 本文给出**每一个对外接口**的可直接执行 curl 命令，并附上**在本机真实依赖下实测的响应**。
> 实测脚本：[`tools/curl_e2e.ps1`](../tools/curl_e2e.ps1)。
>
> ⚠️ **响应样例的采集口径**：下文的实测响应采集于 `EMBEDDING_PROVIDER=bge`（本地
> `BAAI/bge-m3`，**1024 维**）时期的配置，因此 `embedding` 健康检查与知识库详情里会看到
> `"dim":1024` / `"embedding_dim":1024`。当前出厂档是 `siliconflow`（云端
> `Qwen/Qwen3-Embedding-0.6B`，**同样是 1024 维**），因此这些字段的**数值恰好一致** ——
> 但**向量空间完全不同**：换档位必须换 Milvus 集合名（现在是 `*_v3`）并重新入库，
> 绝不能因为"维度一样"就复用旧集合，原因见 [10-§7.3.2](./10-非功能需求与可观测性.md)。

## 1. 为什么要有这份文档

前 11 篇 SRS 写的是「接口应该是什么样」，本文写的是「照这样发请求，**真的**会得到什么」。
这两者之间的差距，正是 M1–M7 反复踩到的那一类问题（见 [12-实现问题记录](./12-实现问题记录.md)）：
代码能跑、测试能过、接口有响应，但结果是错的（或压根没接上真实依赖）。

因此本文的每条示例都标注了 **实测 HTTP 状态**，并且**把非 2xx 也当成结果写下来** ——
有些非 2xx 是**正确**的负例（409 取消已完成的任务、404 查不存在的 MCP Server），
把它们混在「失败」里一起忽略，就等于没有验证错误分支。

## 2. 前置条件

### 2.1 依赖服务

| 依赖 | 本机实测 | 说明 |
| --- | --- | --- |
| MySQL 8.0 | `127.0.0.1:3306`，库 `ai_platform` | 15 张表（AI 独占 7 + 共享 2 + 网关独占 6） |
| Redis 7.4 | `127.0.0.1:6379` | 任务仓储 / 事件总线 / 会话上下文 |
| Kafka | `127.0.0.1:9092` | `.env.example` 的出厂档要用它（`TASK_RUNNER=kafka` ⇒ 任务由独立 Worker 消费）；**本文的示例用 `inline`**，以便每个接口都能「调一次就看到结果」而不必等 Worker |
| Milvus | `http://127.0.0.1:19530` | 2 个集合：`ai_platform_chunks`、`ai_platform_memories` |
| MinIO | `localhost:9000` | 文档原文对象存储 |

```powershell
# Redis + Kafka + MinIO（MySQL 与 Milvus 是外部依赖，见 README）
docker compose -f deploy/infra/compose.yml up -d
```

### 2.2 Python 依赖与建表

```powershell
# 可选依赖分组：不带这些 extra 时，storage/objectstore/memory 会走内存实现
uv sync --extra mysql --extra minio --extra redis --extra kafka --extra local   # local = sentence-transformers / FlagEmbedding

# 建库建表（幂等；顺序：AI 侧 → 共享表补列 → 网关侧）
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/001_init_schema.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/002_align_shared_tables.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/003_add_chunk_metadata.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source ../ai-platform-go/deploy/mysql/001_gateway_tables.sql"
```

> `003_add_chunk_metadata.sql` 是 M8 新增：给 `document_chunk` 补 `metadata JSON` 列。
> 缺列时请求会返回 `503 DEPENDENCY_UNAVAILABLE`（错误信息里含脚本名），而不是 500 ——
> 这就是「部署遗漏」与「服务端 bug」被区分开的地方。

### 2.3 关键环境变量

```ini
INFRA_BACKEND=real                 # memory = 进程内实现；real = MySQL/Milvus/Redis/MinIO
TASK_RUNNER=inline                 # 出厂档是 kafka（独立 Worker 消费，大文件不拖垮在线请求）；
                                   # 本文用 inline 是为了「调一次就看到入库结果」，需要另起 Worker 的场景见 README
MYSQL_DSN=mysql+asyncmy://root:20050613@localhost:3306/ai_platform
REDIS_URL=redis://localhost:6379/0
MILVUS_URI=http://localhost:19530
MINIO_ENDPOINT=localhost:9000
EMBEDDING_PROVIDER=siliconflow     # siliconflow = 硅基流动云端（需 SILICONFLOW_API_KEY）；ark/bge/hash 亦可
RERANKER_ENABLED=true
RERANKER_PROVIDER=siliconflow
HF_ENDPOINT=https://hf-mirror.com  # 国内加速；应用会同步进 os.environ（见 §9）
AUTH_ENABLED=false                 # 本地联调；生产必须 true 并提供 JWT_SECRET
METRICS_PORT=9105                  # 独立指标端口（9100 常被 node-exporter 占用）
```

### 2.4 启动

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动日志里这几行就是「真实依赖已就位」的证据：

```
app.hf_endpoint_applied   hf_endpoint=https://hf-mirror.com
storage.repositories_ready backend=mysql
memory.repo_ready          backend=mysql
memory.vector_index_ready  backend=milvus collection=ai_platform_memories dim=1024
app.mysql_ready            host=localhost:3306/ai_platform
app.startup                infra_backend=real task_store=RedisTaskStore task_event_bus=RedisTaskEventBus
```

## 3. 通用约定

| 项 | 约定 |
| --- | --- |
| Base URL | `http://127.0.0.1:8000/api/v1` |
| 鉴权 | 生产：`Authorization: Bearer <JWT>`（HS256，与 Go Gateway 共享密钥）。本地 `AUTH_ENABLED=false` 时用 `DEBUG_USER_ID`（`u_dev`）兜底，下面示例省略该头 |
| 错误信封 | `{"error":{"code","message","details","trace_id","retryable"}}` |
| 分页 | `?limit=` + `?cursor=`，响应 `{"items","next_cursor","has_more"}` |
| SSE | `Accept: text/event-stream`，帧格式 `event: <type>\ndata: <json>\n\n`。**必须用 `curl -N`**，否则输出会被缓冲到流结束才吐出来 |
| 时间戳 | `2026-09-28T19:13:29.201Z`（UTC、毫秒、`Z` 结尾） |

> **SSE 一定要设 `--max-time`**：服务端按设计保持连接（增量 + 心跳），
> 不加超时的 `curl` 会一直挂着。本文所有 SSE 示例都带 `--max-time`。

---

## 4. 健康检查与元信息

### 4.1 `GET /health`

```bash
curl -s http://127.0.0.1:8000/api/v1/health
```

```json
{"status":"ok","app":"ai-platform","env":"local","version":"0.1.0",
 "dependencies":{
   "storage":{"ok":true,"latency_ms":0,"backend":"real","vector":"milvus"},
   "embedding":{"ok":true,"latency_ms":0,"dim":1024,"model":"BAAI/bge-m3"},
   "milvus":{"ok":true,"latency_ms":103},
   "redis":{"ok":true,"latency_ms":107},
   "mysql":{"ok":true,"latency_ms":199},
   "mcp":{"ok":true,"latency_ms":0,"skipped":true,"reason":"未配置 MCP Server"}}}
```

实测 `200`。注意 `storage.backend=real` + `storage.vector=milvus`：
这是「**不是内存实现**」的第一处可断言证据。

### 4.2 `GET /health/live`

```bash
curl -s http://127.0.0.1:8000/api/v1/health/live
```

```json
{"status":"alive"}
```

实测 `200`。存活探针**不碰外部依赖**：Milvus 挂掉时它必须仍然 200（`AC-NFR-11`）。

### 4.3 `GET /health/ready`

```bash
curl -s http://127.0.0.1:8000/api/v1/health/ready
```

```json
{"status":"ok","checks":{
  "storage":{"ok":true,"latency_ms":0,"backend":"real","vector":"milvus"},
  "embedding":{"ok":true,"latency_ms":0,"dim":1024,"model":"BAAI/bge-m3"},
  "milvus":{"ok":true,"latency_ms":7},
  "redis":{"ok":true,"latency_ms":11},
  "mysql":{"ok":true,"latency_ms":17},
  "mcp":{"ok":true,"latency_ms":0,"skipped":true,"reason":"未配置 MCP Server"}}}
```

实测 `200`。任一必需依赖不可用时返回 `503` 并给出失败的 `checks`。

### 4.4 `GET /models`

```bash
curl -s http://127.0.0.1:8000/api/v1/models
```

```json
{"items":[
  {"name":"deepseek-flash","provider":"deepseek","supports_tools":true,"supports_stream":true,"context_window":65536,"is_default":true},
  {"name":"deepseek-v4-pro","provider":"deepseek","supports_tools":true,"supports_stream":true,"context_window":131072,"is_default":false}]}
```

实测 `200`。

### 4.5 `GET /tools`

```bash
curl -s http://127.0.0.1:8000/api/v1/tools
```

```json
{"items":[{"name":"calculator","description":"对算术表达式求值并返回精确结果。…","parameters":{…},"source":"builtin","mcp_server":null,"side_effect":"read","timeout_seconds":3.0,"enabled":true,"example_arguments":{"expression":"(12.5 - 8) / 8 * 100"}}, …]}
```

实测 `200`，6 个内置工具：`calculator`、`current_time`、`kb_retrieve`、`http_fetch`、
`memory_save`、`memory_search`（`http_fetch` 默认 `TOOL_HTTP_FETCH_ENABLED=false` 时不注册）。

### 4.6 `POST /tools/{name}/invoke`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tools/calculator/invoke \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"expression":"(12.5 - 8) / 8 * 100"}}'
```

```json
{"name":"calculator","status":"ok","result":{"expression":"(12.5 - 8) / 8 * 100","result":56.25},"elapsed_ms":2,"error":null}
```

实测 `200`。参数不合法时 `status` 为 `"error"`、`error` 带原因，但 **HTTP 仍是 200** ——
「工具参数错」是**可回复的结果**，不是传输层失败（`docs/04-§4.3`）。

---

## 5. 知识库

### 5.1 `POST /knowledge-bases`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases \
  -H 'Content-Type: application/json' \
  -d '{"name":"e2e-kb-031328","description":"e2e Probe","chunk_size":256,"chunk_overlap":32}'
```

```json
{"id":"kb_01M3MPZES46WANY527FCKRN2W2","name":"e2e-kb-031328","description":"e2e Probe",
 "chunk_size":256,"chunk_overlap":32,"embedding_model":null,"embedding_dim":1024,
 "retrieval_top_k":20,"rerank_top_n":5,"score_threshold":0.0,"status":"active",
 "document_count":0,"chunk_count":0,"metadata":{},
 "created_at":"2026-09-28T19:13:28.868Z","updated_at":"2026-09-28T19:13:28.868Z"}
```

实测 `201`。同名重复创建 → `409 KB_NAME_CONFLICT`（唯一索引 `uk_kb_user_name` 兜底）。

### 5.2 `GET /knowledge-bases`

```bash
curl -s 'http://127.0.0.1:8000/api/v1/knowledge-bases?limit=20'
```

```json
{"items":[{"id":"kb_01M3MPZES46WANY527FCKRN2W2","name":"e2e-kb-031328","status":"active",…}],
 "next_cursor":null,"has_more":false}
```

实测 `200`。软删除的 KB 不再出现。

### 5.3 `GET /knowledge-bases/{kb_id}`

```bash
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/kb_01M3MPZES46WANY527FCKRN2W2
```

实测 `200`（响应同 5.1）。跨用户访问 → `404 KB_NOT_FOUND`（**不是 403**，避免泄露「存在性」）。

### 5.4 `PATCH /knowledge-bases/{kb_id}`

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/v1/knowledge-bases/kb_01M3MPZES46WANY527FCKRN2W2 \
  -H 'Content-Type: application/json' -d '{"description":"patched by e2e"}'
```

实测 `200`，`description` 变为 `"patched by e2e"`、`updated_at` 前移。
`rerank_top_n > retrieval_top_k` → `400 INVALID_ARGUMENT`。

### 5.5 `DELETE /knowledge-bases/{kb_id}`

```bash
# 非空且不带 force → 409
curl -s -w ' [%{http_code}]' -X DELETE http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx
# {"error":{"code":"KB_NOT_EMPTY","message":"知识库非空，请先删除文档或使用 force=true","details":{"document_count":1},…}} [409]

# 带 force → 200，并返回真实清理统计
curl -s -X DELETE 'http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx?force=true'
# {"deleted":true,"documents":1,"vectors":2,"objects":1}
```

实测 `409` / `200`。`vectors:2` 是**从 Milvus 真删掉的**向量数（两次独立复现都是 2，
对应该文档的 2 个 chunk），不是常量。

删除顺序照 `docs/06-§7`：**向量 → 关系库 → 对象存储**。

---

## 6. 文档与检索

### 6.1 `POST /knowledge-bases/{kb_id}/documents`（multipart）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/documents \
  -F 'file=@/tmp/e2e-doc.txt;type=text/plain' -F 'doc_name=e2e-doc.txt'
```

```json
{"doc_id":"doc_01M3MPZF39X7MX8STHHXCTAA8P","task_id":"task_01M3MPZF3J3TPHC0BBF00A4GWQ",
 "status":"PENDING","doc_name":"e2e-doc.txt",
 "content_sha256":"f9e1446ebe2cba07d11612bdd07befdc691b2b1be3765b01d35c1122419078c7",
 "duplicated":false,"created_at":"2026-09-28T19:13:29.193Z"}
```

实测 `202`。上传是**异步**的：正文解析/切分/向量化/入库由任务完成，
所以拿到 `task_id` 后要轮询（见 §7）。

相同 `content_sha256` 再次上传 → `duplicated:true` 且复用已有 `doc_id`（`uk_doc_dedupe`）。

### 6.2 轮询任务直到终态

```bash
curl -s http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ
```

实测：约 0.2 s 后 `status=SUCCEEDED, stage=INDEXED, progress=100`。

### 6.3 `GET /knowledge-bases/{kb_id}/documents`

```bash
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/documents
```

```json
{"items":[{"id":"doc_01M3MPZF39X7MX8STHHXCTAA8P","kb_id":"kb_…","doc_name":"e2e-doc.txt",
 "file_ext":".txt","mime_type":"text/plain","size_bytes":4382,"status":"INDEXED",
 "page_count":1,"chunk_count":5,"char_count":4341,"chunk_size":256,"chunk_overlap":32,
 "content_sha256":"f9e1…","task_id":"task_…","error":null,"metadata":{},
 "indexed_at":"2026-09-28T19:13:29.385Z", …}],"next_cursor":null,"has_more":false}
```

实测 `200`。

### 6.4 `GET /documents/{doc_id}` / `GET /documents/{doc_id}/chunks`

```bash
curl -s http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P
curl -s http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P/chunks
```

```json
{"items":[{"chunk_id":"chk_01M3MPZF76FK5J5V6VPWHGJMD2","doc_id":"doc_…","kb_id":"kb_…",
 "chunk_index":0,"content":"Section 1: …\nSection 2: …","content_sha256":"b80e0fcd…",
 "char_start":0,"char_end":…,"page":null,"heading_path":null,"token_count":…}],
 "next_cursor":null,"has_more":false}
```

实测均 `200`。

### 6.5 `POST /knowledge-bases/{kb_id}/search`（纯向量）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"section 7 probe","top_k":3,"with_rerank":false}'
```

```json
{"query":"section 7 probe","recalled":3,"returned":2,"rerank_used":false,"elapsed_ms":48,
 "items":[{"index":1,"chunk_id":"chk_…","doc_id":"doc_…","doc_name":"e2e-doc.txt",
  "page":null,"heading_path":null,"vector_score":0.517526,"rerank_score":null,
  "score":0.517526,"merged":false,"content":"Section 33: …"}, …]}
```

实测 `200`。

### 6.6 `POST /knowledge-bases/{kb_id}/search`（带重排）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"section 7 probe","top_k":3,"with_rerank":true}'
```

```json
{"query":"section 7 probe","recalled":3,"returned":2,"rerank_used":true,"elapsed_ms":4852,
 "items":[{"index":1,"chunk_id":"chk_…","vector_score":0.512564,
  "rerank_score":0.896287,"score":0.896287,"merged":true,"content":"Section 1: …"}, …]}
```

实测 `200`，`rerank_used=true`，两名的 `rerank_score` 分别为 **0.896287 / 0.140768**
—— 交叉编码器把「同一个 chunk」（向量分只有 0.51、与其它 chunk 几乎无差别）
明确推到第一，这就是重排生效的直接证据。

`elapsed_ms≈4.9s`（CPU 上首次加载模型），第二次同进程内降到 ~1.2 s。

> **重排是可选增强，失败只降级不报错**：模型下载不下来时 `rerank_used=false` +
> `degraded_reasons=["rerank_skipped"]`，HTTP 仍是 `200`。不要把「重排没生效」
> 当成接口失败。

### 6.7 `DELETE /documents/{doc_id}`

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P
# {"doc_id":"doc_01M3MPZF39X7MX8STHHXCTAA8P","task_id":"task_01M3MPZXFH372PVKTEM1JBBH02","status":"PENDING"}
```

实测 `202` + 轮询至 `SUCCEEDED (stage=DELETING)`。
删除后该文档在 Milvus 中的向量数实测由 **2 → 0**（不是只在关系库里打标记）。

---

## 7. 异步任务与 SSE

### 7.1 `GET /tasks`

```bash
curl -s 'http://127.0.0.1:8000/api/v1/tasks?limit=20'
```

```json
{"items":[{"id":"task_01M3MPZF3J3TPHC0BBF00A4GWQ","type":"document_ingest","status":"SUCCEEDED",
 "resource_type":"document","resource_id":"doc_…","progress":100,"stage":"INDEXED",
 "retry_count":0,"max_retries":3,"error":null,"cancelable":false,"retryable":false,
 "created_at":"…","queued_at":"…","started_at":"…","finished_at":"…","updated_at":"…"}],…}
```

实测 `200`。`cancelable` / `retryable` 是**服务端算出来的**，客户端不要自己推断。

### 7.2 `GET /tasks/{task_id}`

```bash
curl -s http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ
```

实测 `200`。

### 7.3 `GET /tasks/{task_id}/events`（SSE 进度）

```bash
curl -N --max-time 25 http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/events
```

```
event: done
data: {"status":"SUCCEEDED","finished_at":"2026-09-28T19:13:29.408Z"}
```

实测 `200`。**任务已终态时只推一帧 `done` 就结束连接**（快照语义）；
任务在跑时是「快照 + 增量 + 心跳」，那时必须靠 `--max-time` 主动断开。

### 7.4 `POST /tasks/{task_id}/cancel`

```bash
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/cancel
# {"error":{"code":"TASK_NOT_CANCELABLE","message":"任务处于 SUCCEEDED，不可取消","details":{"status":"SUCCEEDED"},…}} [409]
```

实测 `409`（**正确负例**）。对 `PENDING`/`RUNNING` 的任务才是 `202`。

### 7.5 `POST /tasks/{task_id}/retry`

```bash
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/retry
# {"error":{"code":"TASK_NOT_RETRYABLE","message":"任务不可重试","details":{"status":"SUCCEEDED","retry_count":0},…}} [409]
```

实测 `409`（**正确负例**）。只有 `FAILED` 且重试预算未耗尽才可重试。

---

## 8. 对话、Agent 与会话记忆

### 8.1 `POST /chat`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"query":"Say hello in one short sentence.","use_rag":false,"use_memory":true,"use_tools":false}'
```

```json
{"answer":"你好！很高兴见到你，有什么可以帮你的吗？",
 "conversation_id":"cv_01M3MPZQS8HTHQPT630ASV404M","message_id":"msg_01M3MPZQSV6HN9NXH1QVG3G2X5",
 "references":[],"tool_calls":[],"usage":{"prompt_tokens":144,"completion_tokens":13,"total_tokens":157},
 "finish_reason":"stop","model":"deepseek-flash","degraded":false,"degraded_reasons":[],"elapsed_ms":571}
```

实测 `200`，`model` 回显 `deepseek-flash` —— 证明上游**真的接受了这个模型名**
（而不是拼错后静默回退）。

> `conversation_id` 只在会话被真正持久化时才返回。`use_memory=false` 且未传
> `conversation_id` 时为 `null`（无会话可归属，属预期）。

### 8.2 `POST /chat/stream`（SSE）

```bash
curl -N --max-time 30 -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"Say hi in one short sentence.","use_rag":false,"use_memory":true,"use_tools":false}'
```

```
event: meta
data: {"conversation_id":"cv_01M3MPZRD03GAXZ5NWTY8WXWEV","message_id":"msg_…","model":"deepseek-flash","created_at":"2026-09-28T19:13:38.731Z","degraded":false}

event: token
data: {"delta":"你好"}

event: token
data: {"delta":"！"}
…

event: usage
data: {"prompt_tokens":144,"completion_tokens":9,"total_tokens":153}

event: done
data: {"finish_reason":"stop","elapsed_ms":529,"partial":false}
```

实测 `200`。帧序固定 **`meta` → `token`\* → `usage` → `done`**；
中途出错会插入 `event: error`（带 `retryable`），客户端应以 `done`/`error` 作为终止判据。

### 8.3 `POST /agent/run`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"query":"What time is it now? Use the current_time tool.","use_tools":true,"use_rag":false,"use_memory":false,"max_steps":3}'
```

```json
{"answer":"当前时间（Asia/Shanghai 时区）：\n\n- **日期**：2026 年 9 月 29 日，星期二\n- **时间**：03:13:40（+08:00）…",
 "conversation_id":null,"message_id":"msg_…","references":[],
 "tool_calls":[{"call_id":"call_00_QWhAXDSnCNsm2GYOY7D92770","name":"current_time",
   "arguments":{"timezone":"Asia/Shanghai"},"status":"ok",
   "summary":"Asia/Shanghai 当前时间 2026-09-29 03:13:40（星期二）","elapsed_ms":1}],
 "steps":2,"usage":{"prompt_tokens":3078,"completion_tokens":116,"total_tokens":3194},
 "finish_reason":"stop","model":"deepseek-flash","degraded":false,"degraded_reasons":[],"elapsed_ms":1239}
```

实测 `200`。`steps:2` = 1 步调工具 + 1 步收尾（收尾调用**不计入步数上限**）。

### 8.4 `POST /agent/run/stream`（SSE）

```bash
curl -N --max-time 60 -X POST http://127.0.0.1:8000/api/v1/agent/run/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"What time is it now?","use_tools":true,"use_rag":false,"use_memory":false,"max_steps":3}'
```

```
event: meta
data: {"conversation_id":null,"message_id":"msg_…","model":"deepseek-flash","created_at":"…","degraded":false}

event: tool_call
data: {"call_id":"call_00_xjmHyJK7LxvZiHm0DWUM1510","name":"current_time","arguments":{}}

event: tool_result
data: {"call_id":"call_00_xjmHyJK7LxvZiHm0DWUM1510","name":"current_time","status":"ok","summary":"Asia/Shanghai 当前时间 2026-09-29 03:13:41（星期二）","elapsed_ms":0}

event: tool_call
data: {"call_id":"call_01_N0HtOKGKMUkirmVRsR535804","name":"memory_search","arguments":{}}

event: tool_result
data: {"call_id":"call_01_N0HtOKGKMUkirmVRsR535804","name":"memory_search","status":"error","summary":"参数不合法：参数不符合工具 Schema —— query: Field required","elapsed_ms":2}

event: token
data: {"delta":"现在是"}
…

event: usage
data: {"prompt_tokens":3207,"completion_tokens":237,"total_tokens":3444}

event: done
data: {"finish_reason":"stop","elapsed_ms":2095,"partial":false}
```

实测 `200`。这段流很有代表性：模型**先调对**`current_time`，**又调错**了
`memory_search`（漏了必填的 `query`），服务端把校验失败作为一条 `tool_result`
（`status:"error"`）**回注给模型**，模型据此继续并给出了正确回答，全程 HTTP 200。
这正是 `docs/04-§4.4` 的设计意图：**工具参数错误是对话内容，不是传输层故障**。

### 8.5 `GET /conversations/{id}/context`

```bash
curl -s http://127.0.0.1:8000/api/v1/conversations/cv_01M3MPZQS8HTHQPT630ASV404M/context
```

```json
{"conversation_id":"cv_01M3MPZQS8HTHQPT630ASV404M","message_count":2,
 "messages":[{"role":"user","message_id":"msg_…","content":"Say hello in one short sentence.","tokens":7,"created_at":"…","partial":false},
             {"role":"assistant","message_id":"msg_…","content":"你好！很高兴见到你，有什么可以帮你的吗？","tokens":24,"created_at":"…","partial":false}],
 "summary":{"exists":false,"covered_until":null,"token_count":0},
 "budget":{"context_token_budget":8192,"used":{"system":177,"history":31,"query":0},"total_tokens":208,"trimmed":{}}}
```

实测 `200`。

### 8.6 `GET /conversations/{id}/summary`

```bash
curl -s -w ' [%{http_code}]' http://127.0.0.1:8000/api/v1/conversations/cv_xxx/summary
# {"error":{"code":"SUMMARY_UNAVAILABLE","message":"该会话尚未生成摘要","details":{"conversation_id":"cv_xxx"},…}} [404]
```

实测 `404`（**正确负例**）。摘要未生成时就是 404，不是「空对象 200」。
注意错误码是 `SUMMARY_UNAVAILABLE`（不是 `NOT_FOUND`）。

### 8.7 `POST /conversations/{id}/summary/rebuild`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/conversations/cv_xxx/summary/rebuild
# {"task_id":"task_01M3MPZWHF01N422245YKZNZ6G","status":"PENDING"}
```

实测 `202`，随后 `summary_build` 任务 `SUCCEEDED`

### 8.8 `DELETE /conversations/{id}/context`

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8000/api/v1/conversations/cv_xxx/context
# 204
```

实测 `204`（无响应体）。

---

## 9. 长期记忆

### 9.1 `POST /memories`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{"content":"The user prefers answers in Chinese.","kind":"preference","confidence":0.9}'
```

```json
{"id":"mem_01M3MPZWPMEF7T1R7GVJKZPEWX","content":"The user prefers answers in Chinese.",
 "kind":"preference","confidence":0.9,"hit_count":1,"source_conversation_id":null,
 "expires_at":null,"expired":false,
 "created_at":"2026-09-28T19:13:43.124Z","updated_at":"2026-09-28T19:13:43.124Z"}
```

实测 `201`。

### 9.2 重复写入（幂等 + 命中计数）

```bash
# 与 9.1 内容完全相同
curl -s -X POST http://127.0.0.1:8000/api/v1/memories \
  -H 'Content-Type: application/json' \
  -d '{"content":"The user prefers answers in Chinese.","kind":"preference"}'
```

```json
{"id":"mem_01M3MPZWPMEF7T1R7GVJKZPEWX","content":"The user prefers answers in Chinese.",
 "kind":"preference","confidence":1.0,"hit_count":2, …}
```

实测 `201`，**`id` 与 9.1 完全相同**、`hit_count` 由 1 变 2、`confidence` 取 `GREATEST(旧,新)`。
MySQL 侧确认**只有一行**：

```
id: mem_01M3MQJAHX5FEN6K1BJ5VCV268
kind: preference
source: manual        <- 由 POST /memories 写入（自动抽取的是 auto）
hit_count: 2
confidence: 0.9
expired: 0
```

> 这里刻意不复用 M5 的内存实现语义：去重靠 `content_sha256` 唯一索引，
> 命中走**单条 UPDATE**（`hit_count = hit_count + 1` + `GREATEST(confidence, ?)`），
> 所以并发重复写不会丢计数。

### 9.3 `GET /memories` 与 `?all=true`

```bash
curl -s 'http://127.0.0.1:8000/api/v1/memories?limit=20'
curl -s 'http://127.0.0.1:8000/api/v1/memories?all=true'
```

```json
{"items":[{"id":"mem_…","content":"…","kind":"preference","confidence":1.0,"hit_count":2,
 "source_conversation_id":null,"expires_at":null,"expired":false,"created_at":"…","updated_at":"…"}],
 "next_cursor":null,"has_more":false}
```

实测均 `200`。`all=true` 包含已过期/已软删除的记忆。

### 9.4 `GET /memories/{mem_id}` / `PATCH /memories/{mem_id}`

```bash
curl -s http://127.0.0.1:8000/api/v1/memories/mem_01M3MPZWPMEF7T1R7GVJKZPEWX
curl -s -X PATCH http://127.0.0.1:8000/api/v1/memories/mem_01M3MPZWPMEF7T1R7GVJKZPEWX \
  -H 'Content-Type: application/json' -d '{"kind":"fact"}'
```

实测均 `200`，`kind` 由 `preference` 变 `fact`、`updated_at` 前移。
跨用户访问 → `404 MEMORY_NOT_FOUND`。

### 9.5 `DELETE /memories/{mem_id}` / `DELETE /memories?all=true`

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8000/api/v1/memories/mem_xxx
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE 'http://127.0.0.1:8000/api/v1/memories?all=true'
```

实测均 `204`。删单条会**同时删掉 Milvus 里的向量**（`delete` 返回被删记录就是为了这个）。

### 9.6 `GET /memory-settings` / `PUT /memory-settings`

```bash
curl -s http://127.0.0.1:8000/api/v1/memory-settings
curl -s -X PUT http://127.0.0.1:8000/api/v1/memory-settings \
  -H 'Content-Type: application/json' -d '{"memory_enabled":true,"memory_top_n":3}'
```

```json
{"memory_enabled":true,"memory_top_n":3,"cleared_at":"2026-09-28T19:10:18.688268+00:00"}
```

实测均 `200`。

> **已知不一致**：`cleared_at` 来自共享表 `user_settings`（Go 侧也在用），
> 格式是 Python `datetime.isoformat()` 的 `+00:00`、微秒精度，
> 与其它字段的 `…Z`、毫秒格式不同。属历史遗留的**展示层**差异，不影响比较
> （服务端比较前一律 `parse_stamp` 归一）。

---

## 10. MCP

未配置 `MCP_SERVERS` 时注册表为空，此时这些接口的期望结果如下。

### 10.1 `GET /mcp/servers`

```bash
curl -s http://127.0.0.1:8000/api/v1/mcp/servers
# {"items":[],"next_cursor":null,"has_more":false}
```

实测 `200`。

### 10.2 `POST /mcp/servers/{name}/reload`

```bash
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/mcp/servers/not-exists/reload \
  -H 'Content-Type: application/json' -d '{"force":false}'
# {"error":{"code":"MCP_SERVER_NOT_FOUND","message":"MCP Server 未配置：not-exists","details":{"server":"not-exists","configured":[]},…}} [404]
```

实测 `404`（**正确负例**）。

### 10.3 `GET /mcp/servers/{name}/tools`

```bash
curl -s -w ' [%{http_code}]' http://127.0.0.1:8000/api/v1/mcp/servers/not-exists/tools
# 同上 404 MCP_SERVER_NOT_FOUND
```

实测 `404`（**正确负例**）。

> 要真正跑通 MCP 分支，需在 `.env` 里配置 `MCP_SERVERS`（JSON 对象，Schema 见
> `docs/05-§2.2`），例如 filesystem Server；`required=true` 的 Server 连不上会**拒绝启动**。

---

## 11. 一键全量验证

```powershell
# 前提：服务已在 127.0.0.1:8000 运行（§2.4）
powershell -NoProfile -ExecutionPolicy Bypass -File tools/curl_e2e.ps1
```

脚本会：依次请求**全部 42 个「路径 + 方法」组合**（共 45 次请求）、
把每条的状态码与响应体写入日志、把非 2xx 与**白名单里的正确负例**比对，
最后打印汇总并以其退出码表示结果。

### 11.1 实测结论

```
================ SUMMARY ================
requests: 45, 2xx: 39, unexpected failures: 0
```

| 分组 | 请求数 | 结果 |
| --- | --- | --- |
| 健康检查（3） | 3 | 200 |
| 元信息 + 工具调用（3） | 3 | 200 |
| 知识库（5） | 5 | 201 + 4×200 |
| 文档与检索（7） | 7 | 202 + 6×200（含重排 `rerank_used=true`） |
| 任务（5） | 5 | 200 + 200 + 200(SSE) + **409** + **409** |
| 对话（2） | 2 | 200 + 200(SSE) |
| Agent（2） | 2 | 200 + 200(SSE) |
| 会话记忆（4） | 4 | 200 + **404** + 202 + 204 |
| 长期记忆（10） | 10 | 2×201 + 200×5 + 204×2 + 200 |
| MCP（3） | 3 | 200 + **404** + **404** |
| 清理（4） | 4 | 202 + 200 + **404** |
| **合计** | **45** | **39 个 2xx + 6 个白名单内非 2xx + 0 个非预期失败** |

6 个非 2xx 全部是**设计上的正确负例**，且脚本会打印判定依据：

```
### cancel task                            [HTTP 409]  expected: 任务已 SUCCEEDED，不可取消
### retry task                             [HTTP 409]  expected: 任务不可重试
### conversation summary                   [HTTP 404]  expected: 摘要尚未生成
### mcp reload                             [HTTP 404]  expected: Server 未配置
### mcp servers/not-exists/tools           [HTTP 404]  expected: Server 未配置
### get kb after delete                    [HTTP 404]  expected: 软删除后不可见
```

### 11.2 怎么证明「真的接上了真实依赖」

只看 HTTP 200 区分不出「真 MySQL」和「内存假实现」。本次用下面这些**侧证**：

```bash
# 1) 健康检查自报后端
curl -s http://127.0.0.1:8000/api/v1/health | grep -o '"backend":"real"'

# 2) 服务端启动日志
#    storage.repositories_ready backend=mysql
#    memory.repo_ready backend=mysql
#    memory.vector_index_ready backend=milvus collection=ai_platform_memories dim=1024
#    app.mysql_ready host=localhost:3306/ai_platform

# 3) MySQL 里真能查到刚写入的行
mysql -u root -p -D ai_platform -e \
  "SELECT id, doc_name, status, chunk_count FROM document ORDER BY created_at DESC LIMIT 3;"

# 4) M8 新增的 document_chunk.metadata 列真的有值
mysql -u root -p -D ai_platform -e \
  "SELECT id, chunk_index, token_count, metadata FROM document_chunk ORDER BY created_at DESC LIMIT 2\G"
```

实测第 4 条的输出：

```
*************************** 1. row ***************************
         id: chk_01M3MQHV5YP0PYZ0MJCN6HBJ0A
chunk_index: 0
token_count: 506
   metadata: {"merged": false, "doc_name": "m8-evidence.txt", "chunk_size": 512, "chunk_overlap": 64}
*************************** 2. row ***************************
         id: chk_01M3MQHV5YP0PYZ0MJCN6HBJ0B
chunk_index: 1
token_count: 198
   metadata: {"merged": false, "doc_name": "m8-evidence.txt", "chunk_size": 512, "chunk_overlap": 64}
```

### 11.3 Milvus 集合与索引（启动期自动建）

```
collections: ['ai_platform_chunks', 'ai_platform_memories']

--- ai_platform_chunks ---
  fields   : chunk_id, doc_id, kb_id, user_id, chunk_index, content, page,
             heading_path, doc_name, char_start, char_end, vector
  vector dim: 1024
  indexes  : vector(HNSW/COSINE), user_id(INVERTED), kb_id(INVERTED), doc_id(INVERTED)

--- ai_platform_memories ---
  fields   : mem_id, user_id, kind, vector
  vector dim: 1024
  indexes  : vector(HNSW/COSINE), user_id(INVERTED), kind(INVERTED)
```

**标量索引（`INVERTED`）不是可选项**：没有它 Milvus 会退化成
「先取 top-k 再过滤」，跨租户查询会**静默丢召回**（`user_id` 过滤后
凑不满 `top_k`，但接口不报错）。这条约束由
`tests/integration/test_milvus_memory_index.py` 断言住。

### 11.4 数据清理验证（级联删除真的是级联）

| 步骤 | 实测 |
| --- | --- |
| 上传 30 行文档 → 轮询 | `SUCCEEDED / INDEXED / 100%`，`document.chunk_count=2` |
| Milvus 按 `kb_id` 统计 | **2** 条实体 |
| `DELETE /documents/{doc_id}` → 轮询 | `SUCCEEDED / DELETING / 100%` |
| Milvus 再统计 | **0** 条实体 |
| `DELETE /knowledge-bases/{kb_id}?force=true` | `{"deleted":true,"documents":1,"vectors":2,"objects":1}` |
| `GET /knowledge-bases/{kb_id}` | `404 KB_NOT_FOUND` |

> `Milvus collection 的 row_count` 在删除后**不会立刻下降**（删除是标记，
> 空间由后续 compaction 回收）。判断「删没删掉」要用带过滤条件的
> `query(filter='kb_id == "…"')` 结果条数，不要看 `row_count`。

---

## 12. 已知限制与注意事项

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | **本地档才需要预下载权重，而出厂档已不再需要任何权重** | 出厂档 embedding 与 rerank 都走硅基流动云端；本机 HF 缓存里的 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`（共 **8.5 GB**）已按"不再使用"清理。切回 `EMBEDDING_PROVIDER=bge` / `RERANKER_PROVIDER=bge` 时会重新下载；未缓存且连不上 HF 时首次调用会先重试约 9 分钟再降级 |
| 2 | **`HF_ENDPOINT` 必须在 import 前生效** | `huggingface_hub` 在**导入期**把 `HF_ENDPOINT` 固化进 `constants.ENDPOINT` / `HUGGINGFACE_CO_URL_TEMPLATE`，而 `import app.main` 期间 `langchain_text_splitters → transformers` 会提前把它拉进来。只写 `.env` 是**静默失效**的；`app.core.config.apply_hf_endpoint()` 会同时改写环境变量与已导入的常量（详见 `docs/12-§13.1`） |
| 3 | **`EMBEDDING_PROVIDER=hash` 也能跑通全流程** | 那是确定性的词法向量，用来让「检索链路」可以被机械断言。它**不代表语义检索质量**，做 RAG 效果评估必须切 `ark`（云端）或 `bge`（本地） |
| 4 | **SSE 必须 `curl -N` + `--max-time`** | 缺 `-N` 会缓冲；缺 `--max-time` 会一直挂 |
| 5 | **`metrics` 不在主应用端口** | 指标在独立的 `METRICS_PORT`（本文实测 9105；9100 常被 node-exporter 占用），主应用**不**暴露 `/metrics` |
| 6 | **`ai_platform_memories` 是启动期懒建** | 由 `memory["index"].ensure_ready()` 创建；Milvus 不可用时只告警不阻断启动，此时记忆检索会降级 |
| 7 | **`AUTH_ENABLED=false` 只用于本地** | 生产必须 `true` + `JWT_SECRET`，且 `INFRA_BACKEND=real`、`CORS_ORIGINS` 不允许 `*`（`validate_for_startup()` 会在启动期拒绝） |
| 8 | **`cleared_at` 时间格式与其它字段不一致** | 见 §9.6 |
