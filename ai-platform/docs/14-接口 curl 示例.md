# 14 · 对外接口 curl 示例

> 关联：[02-接口规范与错误码](./02-接口规范与错误码.md)（错误码总表）、[06-RAG知识库](./06-RAG知识库.md)、[08-异步任务](./08-异步任务.md)、[09-数据存储模型](./09-数据存储模型.md)、[10-非功能需求与可观测性](./10-非功能需求与可观测性.md)、[12-实现问题记录](./12-实现问题记录.md)。全量脚本 [`tools/curl_e2e.ps1`](../tools/curl_e2e.ps1)，用法见 §11。
>
> 每个接口一条可直接复制的 curl + 期望要点（状态码 + 关键字段）；非 2xx 也当结果写：其中一部分是**正确负例**（409 取消已终态任务、404 未生成的摘要），脚本按白名单判定。**采集口径**：样例采集于 `EMBEDDING_PROVIDER=bge`（本地 `BAAI/bge-m3`，**1024 维**）时期，所以健康检查与知识库详情里是 `"dim":1024` / `"embedding_dim":1024`。出厂档 `siliconflow`（云端 `Qwen/Qwen3-Embedding-0.6B`）**同是 1024 维**、数值恰好一致，但**向量空间不同**：换档位必须换 Milvus 集合名（现在是 `*_v3`）并重新入库，不能因「维度一样」复用旧集合，原因见 [10-§7.3.2](./10-非功能需求与可观测性.md)。

## 1. 前置条件

| 依赖 | 本机实测 | 说明 |
| --- | --- | --- |
| MySQL 8.0 | `127.0.0.1:3306`，库 `ai_platform` | 15 张表（AI 独占 7 + 共享 2 + 网关独占 6） |
| Redis 7.4 | `127.0.0.1:6379` | 任务仓储 / 事件总线 / 会话上下文 |
| Kafka | `127.0.0.1:9092` | 只有 `TASK_RUNNER=kafka` 才需要；**本文用 `inline`**，所以每个接口「调一次就看到结果」 |
| Milvus | `http://127.0.0.1:19530` | `ai_platform_chunks`、`ai_platform_memories` |
| MinIO | `localhost:9000` | 文档原文对象存储 |

```powershell
docker compose -f deploy/infra/compose.yml up -d      # Redis + Kafka + MinIO；MySQL/Milvus 是外部依赖（见 README）
uv sync --extra mysql --extra minio --extra redis --extra kafka --extra local-ml   # 不带 extra 时走内存实现
# 建表（幂等；顺序 AI 侧 → 共享表补列 → 网关侧，不能换）
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/001_init_schema.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/002_align_shared_tables.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source deploy/mysql/003_add_chunk_metadata.sql"
mysql --default-character-set=utf8mb4 -u root -p -e "source ../ai-platform-go/deploy/mysql/001_gateway_tables.sql"
```

`003_add_chunk_metadata.sql` 给 `document_chunk` 补 `metadata JSON` 列；缺列时请求回 `503 DEPENDENCY_UNAVAILABLE`（信息里带脚本名）而不是 500 —— 「部署遗漏」与「服务端 bug」由此区分开。

```ini
INFRA_BACKEND=real                 # memory = 进程内；real = MySQL/Milvus/Redis/MinIO
TASK_RUNNER=inline                 # 出厂档 kafka（独立 Worker）
MYSQL_DSN=mysql+asyncmy://root:<pwd>@localhost:3306/ai_platform
REDIS_URL=redis://localhost:6379/0
MILVUS_URI=http://localhost:19530
EMBEDDING_PROVIDER=siliconflow     # 需 SILICONFLOW_API_KEY；ark / bge / hash 亦可
RERANKER_ENABLED=true
RERANKER_PROVIDER=siliconflow
HF_ENDPOINT=https://hf-mirror.com  # 必须在 import 前生效，见 §12-2
AUTH_ENABLED=false                 # 仅本地；生产 true + JWT_SECRET
METRICS_PORT=9105                  # 默认 9100；本机 9100 常被 node-exporter 占用
```

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# 启动日志里这几行是「真实依赖已就位」的证据：
# storage.repositories_ready backend=mysql
# memory.repo_ready          backend=mysql
# memory.vector_index_ready  backend=milvus collection=ai_platform_memories dim=1024
# app.startup                infra_backend=real task_store=RedisTaskStore task_event_bus=RedisTaskEventBus
```

## 2. 通用约定

| 项 | 约定 |
| --- | --- |
| Base URL | `http://127.0.0.1:8000/api/v1` |
| 鉴权头 | 生产 `-H 'Authorization: Bearer <JWT>'`（HS256，与 Go Gateway 共享密钥）。`AUTH_ENABLED=false` 时读 `X-Debug-User-Id`（缺省 `DEBUG_USER_ID`，默认 `u_dev`），换用户就换这个头 —— 跨用户 404 是这么测的。健康检查与文档端点豁免（`app/core/security.py:138`）；下文示例省略该头 |
| 错误信封 | `{"error":{"code","message","details","trace_id","retryable"}}`。各码含义查 [02-§5](./02-接口规范与错误码.md)，本文只记「哪个输入触发哪个码」 |
| 分页 | `?limit=`（1..100，默认 20）+ `?cursor=`，响应 `{"items","next_cursor","has_more"}` |
| SSE | `-H 'Accept: text/event-stream'`，帧格式 `event: <type>\ndata: <json>\n\n`。**必须 `curl -N`**（否则缓冲到流结束才吐）**且必须 `--max-time`**（服务端按设计保持连接，不设就一直挂着） |
| 时间戳 | `2026-09-28T19:13:29.201Z`（UTC、毫秒、`Z` 结尾） |
| 端口 | HTTP `8000`；指标独立端口默认 `9100`（本机 `9105`，主应用**不**暴露 `/metrics`，19 个 `ai_*` 指标见 [10](./10-非功能需求与可观测性.md)）；gRPC `50051`（`GRPC_ENABLED` 默认 `false`，只用于对内拨号） |

## 3. 健康检查与元信息

| 接口（可直接复制） | 实测 | 期望要点 |
| --- | --- | --- |
| `GET /health`<br>`curl -s http://127.0.0.1:8000/api/v1/health` | `200` | `{"status":"ok","app":"ai-platform","env":"local","version":"0.1.0","dependencies":{…}}`；`storage.backend=real` + `storage.vector=milvus` 是「**不是内存实现**」的第一处可断言证据；`embedding.dim=1024`；`mcp.skipped=true`（未配 `MCP_SERVERS`） |
| `GET /health/live`<br>`curl -s http://127.0.0.1:8000/api/v1/health/live` | `200` | `{"status":"alive"}`；**不碰外部依赖**，Milvus 挂掉时仍须 200（`AC-NFR-11`） |
| `GET /health/ready`<br>`curl -s http://127.0.0.1:8000/api/v1/health/ready` | `200` | 同一份 `checks`，实测 `milvus:7ms`、`redis:11ms`、`mysql:17ms`；任一必需依赖不可用 → **`503`** 并返回失败的 `checks` |
| `GET /models`<br>`curl -s http://127.0.0.1:8000/api/v1/models` | `200` | `items[].{name,provider,supports_tools,supports_stream,context_window,is_default}`；实测 2 个：`deepseek-flash`（65536，默认）、`deepseek-v4-pro`（131072） |
| `GET /tools`<br>`curl -s http://127.0.0.1:8000/api/v1/tools` | `200` | `items[].{name,description,parameters,source,mcp_server,side_effect,timeout_seconds,enabled,example_arguments}`；6 个内置工具 `calculator`、`current_time`、`kb_retrieve`、`http_fetch`、`memory_save`、`memory_search`；`http_fetch` 在 `TOOL_HTTP_FETCH_ENABLED=false` 时**不注册** |
| `POST /tools/{name}/invoke`<br>`curl -s -X POST http://127.0.0.1:8000/api/v1/tools/calculator/invoke -H 'Content-Type: application/json'`<br>`  -d '{"arguments":{"expression":"(12.5 - 8) / 8 * 100"}}'` | `200` | `{"name":"calculator","status":"ok","result":{"expression":"…","result":56.25},"elapsed_ms":2,"error":null}`。参数不合法时 `status` 变 `"error"`、`error` 带原因，**HTTP 仍是 200** —— 工具参数错是**可回复的结果**，不是传输层失败（[04-§4.3](./04-Agent与工具调用.md)） |

## 4. 知识库

```bash
# 创建 → 201；同名再建 → 409 KB_NAME_CONFLICT（唯一索引 uk_kb_user_name 兜底）
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases -H 'Content-Type: application/json' \
  -d '{"name":"e2e-kb-031328","description":"e2e Probe","chunk_size":256,"chunk_overlap":32}'
# 列表（软删除的不出现）→ 200
curl -s 'http://127.0.0.1:8000/api/v1/knowledge-bases?limit=20'
# 详情 → 200；越权或不存在 → 404 KB_NOT_FOUND（不是 403，避免泄露存在性）
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/kb_01M3MPZES46WANY527FCKRN2W2
# 改名 → 200；rerank_top_n > retrieval_top_k → 400 INVALID_ARGUMENT
curl -s -X PATCH http://127.0.0.1:8000/api/v1/knowledge-bases/kb_01M3MPZES46WANY527FCKRN2W2 \
  -H 'Content-Type: application/json' -d '{"description":"patched by e2e"}'
# 删除：非空且不带 force → 409 KB_NOT_EMPTY；带 force → 200 + 真实清理统计
curl -s -w ' [%{http_code}]' -X DELETE http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx
curl -s -X DELETE 'http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx?force=true'
```

| 期望 | 关键字段 |
| --- | --- |
| 创建 `201` | `{id,name,description,chunk_size:256,chunk_overlap:32,embedding_model:null,embedding_dim:1024,retrieval_top_k:20,rerank_top_n:5,score_threshold:0,status:"active",document_count:0,chunk_count:0,metadata:{},created_at,updated_at}`，`id` 形如 `kb_01M3MPZES46WANY527FCKRN2W2` |
| 列表 `200` | `{"items":[{"id","name","status",…}],"next_cursor":null,"has_more":false}`；详情 `200` 同创建响应 |
| 删除 `409` | `{"error":{"code":"KB_NOT_EMPTY","details":{"document_count":1},…}}` |
| 删除 `200` | `{"deleted":true,"documents":1,"vectors":2,"objects":1}` |

`vectors:2` 是**从 Milvus 真删掉**的向量数（两次独立复现都是 2，对应该文档的 2 个 chunk），不是常量。删除顺序照 [06-§7](./06-RAG知识库.md)：**向量 → 关系库 → 对象存储**。

## 5. 文档与检索

### 5.1 上传（multipart，异步）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/documents \
  -F 'file=@/tmp/e2e-doc.txt;type=text/plain' -F 'doc_name=e2e-doc.txt'
```

```json
{"doc_id":"doc_01M3MPZF39X7MX8STHHXCTAA8P","task_id":"task_01M3MPZF3J3TPHC0BBF00A4GWQ","status":"PENDING",
 "doc_name":"e2e-doc.txt","duplicated":false,"created_at":"2026-09-28T19:13:29.193Z",
 "content_sha256":"f9e1446ebe2cba07d11612bdd07befdc691b2b1be3765b01d35c1122419078c7"}
```

实测 `202`：解析 / 切分 / 向量化 / 入库都在任务里，拿到 `task_id` 必须轮询（见 §5.2）。同内容再传按已有文档状态分三种（`app/rag/service.py:359-384`）：`INDEXED` → `409 DOCUMENT_DUPLICATE`，`details.doc_id` 给出已有文档（`uk_doc_dedupe` 兜底）；处理中 → `202` + `duplicated:true`，**复用同一个 `doc_id` / `task_id`**（幂等）；`FAILED` → 重新入库（`_reindex_existing`），不复用失败记录。

### 5.2 轮询与文档读取

```bash
curl -s http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ   # 约 0.2s 后 SUCCEEDED / INDEXED / 100
curl -s http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/documents
curl -s http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P
curl -s http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P/chunks
```

均 `200`。文档对象 `{id,doc_name,file_ext,mime_type,size_bytes,status,page_count,chunk_count,char_count,chunk_size,chunk_overlap,content_sha256,task_id,error,metadata,indexed_at}`（实测 `size_bytes:4382`、`chunk_count:5`、`char_count:4341`、`status:INDEXED`）；chunk 对象 `{chunk_id,doc_id,kb_id,chunk_index,content,content_sha256,char_start,char_end,page,heading_path,token_count}`；列表信封同 §4。

### 5.3 检索（带重排）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/kb_xxx/search \
  -H 'Content-Type: application/json' -d '{"query":"section 7 probe","top_k":3,"with_rerank":true}'
```

```json
{"query":"section 7 probe","recalled":3,"returned":2,"rerank_used":true,"elapsed_ms":4852,
 "items":[{"index":1,"chunk_id":"chk_…","doc_id":"doc_…","doc_name":"e2e-doc.txt","page":null,"heading_path":null,
  "vector_score":0.512564,"rerank_score":0.896287,"score":0.896287,"merged":true,"content":"Section 1: …"}, …]}
```

实测 `200`。前两名的 `rerank_score` 是 **0.896287 / 0.140768** —— 交叉编码器把「向量分只有 0.51、与其它 chunk 几乎无差别」的那个 chunk 明确推到第一，这是重排生效的直接证据；`elapsed_ms≈4.9 s` 是 CPU 上首次加载模型，同进程第二次降到 ~1.2 s。`with_rerank` 改 `false` 即纯向量：`rerank_used=false`、`rerank_score=null`、`score==vector_score`（实测 `0.517526`）、`merged=false`，`elapsed_ms` 约 48 ms。**重排是可选增强，失败只降级不报错** —— 模型下载不下来时 `rerank_used=false` + `degraded_reasons=["rerank_skipped"]`，HTTP 仍 `200`，别把「重排没生效」当接口失败。

### 5.4 删除文档

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/documents/doc_01M3MPZF39X7MX8STHHXCTAA8P
# {"doc_id":"doc_01M3MPZF39X7MX8STHHXCTAA8P","task_id":"task_01M3MPZXFH372PVKTEM1JBBH02","status":"PENDING"}
```

实测 `202` + 轮询至 `SUCCEEDED (stage=DELETING)`；该文档在 Milvus 中的向量数实测由 **2 → 0**（不是只在关系库打标记）。

## 6. 异步任务与 SSE

```bash
curl -s 'http://127.0.0.1:8000/api/v1/tasks?limit=20'
curl -s http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ
curl -N --max-time 25 http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/events
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/cancel
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/tasks/task_01M3MPZF3J3TPHC0BBF00A4GWQ/retry
```

| 接口 | 实测 | 期望要点 |
| --- | --- | --- |
| `GET /tasks` | `200` | `{items:[{id,type,status,resource_type,resource_id,progress,stage,retry_count,max_retries,error,cancelable,retryable,created_at,queued_at,started_at,finished_at,updated_at}],next_cursor,has_more}`；单对象同结构，终态实测 `SUCCEEDED / INDEXED / progress=100`；`cancelable`/`retryable` 是**服务端算的**，客户端不要自己推断 |
| `GET /tasks/{id}/events` | `200` | 终态任务**只推一帧就结束**（快照语义）：`event: done` + `data: {"status":"SUCCEEDED","finished_at":"2026-09-28T19:13:29.408Z"}`；运行中才是「快照 + 增量 + 心跳」，只能靠 `--max-time` 断开 |
| `POST /tasks/{id}/cancel` | **`409`** | `{"error":{"code":"TASK_NOT_CANCELABLE","details":{"status":"SUCCEEDED"},…}}`（正确负例）；`PENDING`/`RUNNING` 才是 `202` |
| `POST /tasks/{id}/retry` | **`409`** | `{"error":{"code":"TASK_NOT_RETRYABLE","details":{"status":"SUCCEEDED","retry_count":0},…}}`（正确负例）；只有 `FAILED` 且预算未耗尽才可重试 |

## 7. 对话、Agent 与会话记忆

> Agent 跑的是自研 Agent Loop（`app/agent/loop.py`）；设计初期考虑过 LangGraph 状态图，最终未采用，不要按图编排去理解 `steps`。

### 7.1 `POST /chat`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"query":"Say hello in one short sentence.","use_rag":false,"use_memory":true,"use_tools":false}'
```

```json
{"answer":"你好！很高兴见到你，有什么可以帮你的吗？","conversation_id":"cv_01M3MPZQS8HTHQPT630ASV404M",
 "message_id":"msg_01M3MPZQSV6HN9NXH1QVG3G2X5","references":[],"tool_calls":[],
 "usage":{"prompt_tokens":144,"completion_tokens":13,"total_tokens":157},"finish_reason":"stop",
 "model":"deepseek-flash","degraded":false,"degraded_reasons":[],"elapsed_ms":571}
```

实测 `200`。`model` 回显 `deepseek-flash`，证明上游**真的接受了这个模型名**（不是拼错后静默回退）。`conversation_id` 只在会话真正持久化时返回：`use_memory=false` 且未传 `conversation_id` 时为 `null`（无会话可归属，属预期）。

### 7.2 `POST /chat/stream`（SSE）

```bash
curl -N --max-time 30 -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"Say hi in one short sentence.","use_rag":false,"use_memory":true,"use_tools":false}'
```

```
event: meta
data: {"conversation_id":"cv_01M3MPZRD03GAXZ5NWTY8WXWEV","message_id":"msg_…","model":"deepseek-flash","created_at":"…","degraded":false}
event: token
data: {"delta":"你好"}
event: done
data: {"finish_reason":"stop","elapsed_ms":529,"partial":false}
```

实测 `200`。帧序固定 **`meta` → `token`\* → `usage` → `done`**（上面省略了中间连续的 `token` 帧；`usage` 帧给 `prompt_tokens:144 / completion_tokens:9 / total_tokens:153`）；中途出错插入 `event: error`（带 `retryable`）。客户端以 `done`/`error` 为终止判据，不要用连接关闭判断。

### 7.3 `POST /agent/run`

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/run -H 'Content-Type: application/json' \
  -d '{"query":"What time is it now? Use the current_time tool.","use_tools":true,"use_rag":false,"use_memory":false,"max_steps":3}'
```

```json
{"answer":"当前时间（Asia/Shanghai 时区）：…","conversation_id":null,"message_id":"msg_…","references":[],
 "tool_calls":[{"call_id":"call_00_QWhAXDSnCNsm2GYOY7D92770","name":"current_time","arguments":{"timezone":"Asia/Shanghai"},
   "status":"ok","summary":"Asia/Shanghai 当前时间 2026-09-29 03:13:40（星期二）","elapsed_ms":1}],
 "steps":2,"usage":{"prompt_tokens":3078,"completion_tokens":116,"total_tokens":3194},
 "finish_reason":"stop","model":"deepseek-flash","degraded":false,"degraded_reasons":[],"elapsed_ms":1239}
```

实测 `200`。`steps:2` = 1 步调工具 + 1 步收尾，**收尾调用不计入 `max_steps`**；步数耗尽不是报错，而是 `finish_reason="max_steps"`（[02-§5.2](./02-接口规范与错误码.md)）。

### 7.4 `POST /agent/run/stream`（SSE）

```bash
curl -N --max-time 60 -X POST http://127.0.0.1:8000/api/v1/agent/run/stream \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"query":"What time is it now?","use_tools":true,"use_rag":false,"use_memory":false,"max_steps":3}'
```

```
event: tool_call
data: {"call_id":"call_00_xjmHyJK7LxvZiHm0DWUM1510","name":"current_time","arguments":{}}
event: tool_result
data: {"call_id":"call_00_xjmHyJK7LxvZiHm0DWUM1510","name":"current_time","status":"ok","summary":"Asia/Shanghai 当前时间 2026-09-29 03:13:41（星期二）","elapsed_ms":0}
event: tool_call
data: {"call_id":"call_01_N0HtOKGKMUkirmVRsR535804","name":"memory_search","arguments":{}}
event: tool_result
data: {"call_id":"call_01_N0HtOKGKMUkirmVRsR535804","name":"memory_search","status":"error","summary":"参数不合法：参数不符合工具 Schema —— query: Field required","elapsed_ms":2}
event: done
data: {"finish_reason":"stop","elapsed_ms":2095,"partial":false}
```

帧序：`meta` → (`tool_call` ⇄ `tool_result`) → `token`\* → `usage` → `done`。实测 `200`。这段流很有代表性：模型**先调对** `current_time`，**又调错** `memory_search`（漏了必填 `query`），服务端把校验失败作为一条 `tool_result`（`status:"error"`）**回注给模型**，模型据此继续并给出正确回答，全程 HTTP 200 —— 工具参数错误是对话内容，不是传输层故障（[04-§4.4](./04-Agent与工具调用.md)）。

### 7.5 会话上下文与摘要

```bash
curl -s http://127.0.0.1:8000/api/v1/conversations/cv_01M3MPZQS8HTHQPT630ASV404M/context
curl -s -w ' [%{http_code}]' http://127.0.0.1:8000/api/v1/conversations/cv_xxx/summary
curl -s -X POST http://127.0.0.1:8000/api/v1/conversations/cv_xxx/summary/rebuild
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8000/api/v1/conversations/cv_xxx/context
```

| 接口 | 实测 | 期望要点 |
| --- | --- | --- |
| `GET …/context` | `200` | `{conversation_id,message_count,messages:[{role,message_id,content,tokens,created_at,partial}],summary:{exists,covered_until,token_count},budget:{context_token_budget:8192,used:{system:177,history:31,query:0},total_tokens:208,trimmed:{}}}`；排查 `CONTEXT_TOO_LONG` 先看 `budget` |
| `GET …/summary` | **`404`** | `SUMMARY_UNAVAILABLE`（`app/core/exceptions.py:140`），**不是** `NOT_FOUND`、也不是「空对象 200」；摘要未生成就是这个码（正确负例） |
| `POST …/summary/rebuild` | `202` | `{"task_id":"task_01M3MPZWHF01N422245YKZNZ6G","status":"PENDING"}`，随后 `summary_build` 任务 `SUCCEEDED` |
| `DELETE …/context` | `204` | 无响应体 |

## 8. 长期记忆

```bash
# 写入 → 201；内容完全相同再写 → 仍 201，同 id、hit_count+1、confidence 取 GREATEST
curl -s -X POST http://127.0.0.1:8000/api/v1/memories -H 'Content-Type: application/json' \
  -d '{"content":"The user prefers answers in Chinese.","kind":"preference","confidence":0.9}'
# 列表 → 200；all=true 额外包含已过期 / 已软删除
curl -s 'http://127.0.0.1:8000/api/v1/memories?limit=20'
curl -s 'http://127.0.0.1:8000/api/v1/memories?all=true'
# 详情 / 改 kind → 200；越权 → 404 MEMORY_NOT_FOUND
curl -s http://127.0.0.1:8000/api/v1/memories/mem_01M3MPZWPMEF7T1R7GVJKZPEWX
curl -s -X PATCH http://127.0.0.1:8000/api/v1/memories/mem_01M3MPZWPMEF7T1R7GVJKZPEWX \
  -H 'Content-Type: application/json' -d '{"kind":"fact"}'
# 删单条 / 清空 → 204（删单条会同时删掉 Milvus 里的向量）
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8000/api/v1/memories/mem_xxx
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE 'http://127.0.0.1:8000/api/v1/memories?all=true'
# 记忆设置 → 200
curl -s http://127.0.0.1:8000/api/v1/memory-settings
curl -s -X PUT http://127.0.0.1:8000/api/v1/memory-settings \
  -H 'Content-Type: application/json' -d '{"memory_enabled":true,"memory_top_n":3}'
```

| 实测 | 关键字段 |
| --- | --- |
| 写入 `201` | `{id:"mem_01M3MPZWPMEF7T1R7GVJKZPEWX",content,kind:"preference",confidence:0.9,hit_count:1,source_conversation_id:null,expires_at:null,expired:false,created_at,updated_at}` |
| 重复写 `201` | `id` 与首次相同、`hit_count:2`、`confidence` 取 `GREATEST(旧,新)`；MySQL 侧只有一行（`source: manual`，自动抽取的才是 `auto`） |
| 设置 `200` | `{"memory_enabled":true,"memory_top_n":3,"cleared_at":"2026-09-28T19:10:18.688268+00:00"}`；`cleared_at` 来自共享表 `user_settings`，是 Python `isoformat()` 的 `+00:00`/微秒格式，与其它字段的 `…Z`/毫秒不同 —— 展示层历史遗留，比较前一律 `parse_stamp` 归一 |

> 去重不复用 M5 的内存实现语义：靠 `uk_mem_user_hash (user_id, content_sha256)`，命中走**单条 UPDATE**（`hit_count = hit_count + 1` + `GREATEST(confidence, ?)`），所以并发重复写不会丢计数。

## 9. MCP

未配置 `MCP_SERVERS` 时注册表为空，以下即期望结果。

```bash
curl -s http://127.0.0.1:8000/api/v1/mcp/servers                                   # 200 {"items":[],"next_cursor":null,"has_more":false}
curl -s -w ' [%{http_code}]' http://127.0.0.1:8000/api/v1/mcp/servers/not-exists/tools   # 404
curl -s -w ' [%{http_code}]' -X POST http://127.0.0.1:8000/api/v1/mcp/servers/not-exists/reload \
  -H 'Content-Type: application/json' -d '{"force":false}'                         # 同上 404
```

两个 `404` 是**正确负例**：`{"error":{"code":"MCP_SERVER_NOT_FOUND","message":"MCP Server 未配置：not-exists","details":{"server":"not-exists","configured":[]},…}}`。要真正跑通 MCP 分支需在 `.env` 配 `MCP_SERVERS`（JSON 对象，Schema 见 [05-§2.2](./05-MCP接入.md)），例如 filesystem Server；`required=true` 的 Server 连不上会**拒绝启动**。

## 10. 关键负例速查（401 / 404 / 409 / 429 / 503）

每行都是「一次可复现的失败」，用来验证错误分支；各码的 `retryable` 与客户端处置见 [02-§5](./02-接口规范与错误码.md)。

| 状态码 | 触发方式 | `code` | 要点 |
| --- | --- | --- | --- |
| `401` | `AUTH_ENABLED=true` 起服务，不带 `Authorization` 请求任意业务接口：<br>`curl -s -w ' [%{http_code}]' http://127.0.0.1:8000/api/v1/knowledge-bases` | `UNAUTHENTICATED` | 无 token / 过期 / 签名错 / 缺 `sub` **一律 401**，不区分原因（防探测，`app/core/security.py:5`）；健康检查与文档端点豁免 |
| `404` | 查不存在的 `kb_id` / `doc_id` / `mem_id` / `task_id`，或用 `X-Debug-User-Id` 换成另一个用户访问 | `KB_NOT_FOUND` / `DOCUMENT_NOT_FOUND` / `MEMORY_NOT_FOUND` / `TASK_NOT_FOUND` | 越权与不存在**同样回 404**；摘要未生成是另一个码 `SUMMARY_UNAVAILABLE`（§7.5） |
| `409` | 同名 KB 再建；对已 `SUCCEEDED` 的任务 cancel / retry；同内容文档传到已 `INDEXED` 的 KB | `KB_NAME_CONFLICT` / `TASK_NOT_CANCELABLE` / `TASK_NOT_RETRYABLE` / `DOCUMENT_DUPLICATE` | 全是**正确负例**，e2e 脚本按白名单判定（`tools/curl_e2e.ps1:39-46`）；请求本身没写错，改参数或换状态再试 |
| `429` | 上游 LLM 返回限流，由 `map_llm_exception` 翻译（`app/llm/base.py:166-175`） | `RATE_LIMITED` | `retryable=true`，按 `retry_after` 退避。429 目前只来自上游；02-§7.3 的进程内限流器尚未落地 |
| `503` | ① 入库未结束任务数达 `INGEST_QUEUE_MAX`（`app/tasks/service.py:148`）<br>② LLM 并发闸门排队超时（`app/llm/openai_compat.py:308`）<br>③ MySQL 缺列 / Milvus / Redis 连不上 | `OVERLOADED` / `DEPENDENCY_UNAVAILABLE` | 都 `retryable=true`。**能用 503 就别用 500**：依赖问题与自身 bug 分开计，监控才不会把上游故障算到自己头上 |

## 11. 一键全量验证

```powershell
# 前提：服务已在 127.0.0.1:8000 运行（§1），依赖已就位
powershell -NoProfile -ExecutionPolicy Bypass -File tools/curl_e2e.ps1
# 换地址 / 指定日志：-Base http://127.0.0.1:8000/api/v1 -LogPath D:\tmp\curl_log.txt
```

脚本依次请求登记的 **42 个「路径 + 方法」组合（共 45 次请求，含轮询与参数变体）**，把每条状态码与响应体写进日志，非 2xx 与**白名单里的正确负例**比对，最后打印汇总并以退出码表示结果（有非预期失败即 `exit 1`）。

```
================ SUMMARY ================
requests: 45, 2xx: 39, unexpected failures: 0
```

| 分组 | 实测状态码 |
| --- | --- |
| 健康 / 元信息 / 工具 / 知识库 / 文档与检索 | 200×6；201 + 4×200；202 + 6×200（含重排 `rerank_used=true`） |
| 任务 / 对话 / Agent / 会话记忆 / 长期记忆 | 200×3 + **409**×2；200×2 + 200(SSE)×2；200 + **404** + 202 + 204；201×2 + 200×5 + 204×2 + 200 |
| MCP / 清理 | 200 + **404**×2；202 + 200 + **404** |

6 个非 2xx 全部是设计上的正确负例，脚本会打印判定依据：`cancel task [409]` 任务已 SUCCEEDED 不可取消、`retry task [409]` 任务不可重试、`conversation summary [404]` 摘要尚未生成、`mcp reload / mcp tools [404]` Server 未配置、`get kb after delete [404]` 软删除后不可见。

### 11.1 怎么证明「真的接上了真实依赖」

只看 200 区分不出「真 MySQL」与「内存假实现」，所以留了四条侧证：

```bash
curl -s http://127.0.0.1:8000/api/v1/health | grep -o '"backend":"real"'    # 1) 健康检查自报后端
# 2) 服务端启动日志（§1）
mysql -u root -p -D ai_platform -e "SELECT id, doc_name, status, chunk_count FROM document ORDER BY created_at DESC LIMIT 3;"   # 3) 库里真有刚写入的行
# 4) M8 新增的 document_chunk.metadata 列真的有值（实测 token_count 与 metadata 都对得上切分参数）
mysql -u root -p -D ai_platform -e "SELECT id, chunk_index, token_count, metadata FROM document_chunk ORDER BY created_at DESC LIMIT 2\G"
#    metadata: {"merged": false, "doc_name": "m8-evidence.txt", "chunk_size": 512, "chunk_overlap": 64}
```

### 11.2 Milvus 集合与标量索引（启动期自动建）

| 集合 | 字段 | 维 | 索引 |
| --- | --- | --- | --- |
| `ai_platform_chunks` | `chunk_id, doc_id, kb_id, user_id, chunk_index, content, page, heading_path, doc_name, char_start, char_end, vector` | 1024 | `vector`(HNSW/COSINE)、`user_id`/`kb_id`/`doc_id`(INVERTED) |
| `ai_platform_memories` | `mem_id, user_id, kind, vector` | 1024 | `vector`(HNSW/COSINE)、`user_id`/`kind`(INVERTED) |

**标量索引（`INVERTED`）不是可选项**：没有它 Milvus 会退化成「先取 top-k 再过滤」，跨租户查询会**静默丢召回**（`user_id` 过滤后凑不满 `top_k`，接口却不报错）。这条约束由 `tests/integration/test_milvus_memory_index.py` 断言住，留下它就是防这类「不报错但结果少」的回归。

### 11.3 级联删除验证

| 步骤 | 实测 |
| --- | --- |
| 上传 30 行文档 → 轮询 | `SUCCEEDED / INDEXED / 100%`，`document.chunk_count=2`；Milvus 按 `kb_id` 统计 **2** 条实体 |
| `DELETE /documents/{doc_id}` → 轮询 | `SUCCEEDED / DELETING / 100%`；Milvus 再统计 **0** 条实体 |
| `DELETE /knowledge-bases/{kb_id}?force=true` → 再 `GET` | `{"deleted":true,"documents":1,"vectors":2,"objects":1}`，随后 `404 KB_NOT_FOUND` |

> Milvus 的 `row_count` 在删除后**不会立刻下降**（删除是标记，空间由后续 compaction 回收）。判断「删没删掉」要用带过滤条件的 `query(filter='kb_id == "…"')` 结果条数，不要看 `row_count`。

## 12. 已知限制

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | 本地档才需要预下载权重，出厂档不需要 | 出厂档 embedding 与 rerank 都走硅基流动云端；本机 HF 缓存里的 `BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`（共 **8.5 GB**）已按「不再使用」清理。切回 `EMBEDDING_PROVIDER=bge` / `RERANKER_PROVIDER=bge` 会重新下载；未缓存且连不上 HF 时，首次调用先重试约 9 分钟再降级 |
| 2 | `HF_ENDPOINT` 必须在 import 前生效 | `huggingface_hub` 在**导入期**把 `HF_ENDPOINT` 固化进 `constants.ENDPOINT` / `HUGGINGFACE_CO_URL_TEMPLATE`，而 `import app.main` 期间 `langchain_text_splitters → transformers` 会提前把它拉进来。只写 `.env` 是**静默失效**的；`app.core.config.apply_hf_endpoint()` 同时改写环境变量与已导入常量（[12-§13.1](./12-实现问题记录.md)） |
| 3 | `EMBEDDING_PROVIDER=hash` 也能跑通全流程 | 那是确定性词法向量，只用来让「检索链路」可被机械断言，**不代表语义检索质量**；做 RAG 效果评估必须切 `ark` 或 `bge` |
| 4 | `metrics` 不在主应用端口 | 指标在独立的 `METRICS_PORT`（默认 9100，本机 9105），主应用**不**暴露 `/metrics` |
| 5 | `ai_platform_memories` 是启动期懒建 | 由 `memory["index"].ensure_ready()` 创建；Milvus 不可用时只告警不阻断启动，此时记忆检索降级 |
| 6 | `AUTH_ENABLED=false` 只用于本地 | 生产必须 `true` + `JWT_SECRET`，且 `INFRA_BACKEND=real`、`CORS_ORIGINS` 不允许 `*`（`validate_for_startup()` 在启动期拒绝） |
| 7 | **长驻 Python gRPC 进程会退化** | 跑数小时后 Chat 全部返回 `503`，重启即恢复；根因未定位。以 gRPC 为长期入口前必须先修这个（`GRPC_ENABLED` 默认 `false`，HTTP 主链路不受影响） |
