# 06 · RAG 知识库

> 关联：需求 ID `REQ-RAG-*`；异步任务见 [08](./08-异步任务.md)；存储模型见 [09](./09-数据存储模型.md)；向量库统一使用 **Milvus**（不使用 Qdrant）。

## 1. 端到端流水线

```mermaid
flowchart LR
    subgraph 入库(异步)
        U["POST .../documents<br/>multipart"] --> H["sha256 去重<br/>+ 建 Task(202)"]
        H --> MS["MinIO<br/>原始文件"]
        MS --> P["解析<br/>PDF/MD/TXT/DOCX/HTML"]
        P --> CL["清洗<br/>去页眉页脚/空白/控制符"]
        CL --> CH["Chunk<br/>recursive + 中文标点边界"]
        CH --> EM["Embedding<br/>硅基流动 Qwen3 / bge-m3（默认 1024d）"]
        EM --> MV[("Milvus<br/>ai_platform_chunks")]
        CH --> DB[("MySQL<br/>document / document_chunk")]
    end
    subgraph 检索(同步)
        Q["query"] --> QE["query embedding"]
        QE --> SR["Milvus 向量召回 top_k=20"]
        MV --> SR
        SR --> RR["Rerank<br/>bge-reranker-v2-m3 → top_n=5"]
        RR --> FL["阈值过滤 + 去重<br/>+ 拼 Context"]
        FL --> CT["注入 LLM + 生成引用"]
    end
```

## 2. 知识库

### 2.1 `POST /knowledge-bases`

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `name` | `string` | 是 | — | 1..64 字符，同用户内唯一 | KB 名称 |
| `description` | `string` | 否 | `""` | ≤ 500 | 描述 |
| `chunk_size` | `integer` | 否 | `512` | 128..2048 | 切片 token 目标长度 |
| `chunk_overlap` | `integer` | 否 | `64` | 0..(chunk_size-1) | 重叠长度，必须 < `chunk_size` |
| `embedding_model` | `string` | 否 | `null` | 须与已建集合维度一致 | `null` = 用全局 `embedding_model` |
| `retrieval_top_k` | `integer` | 否 | `20` | 1..100 | 该 KB 默认召回数 |
| `rerank_top_n` | `integer` | 否 | `5` | 1..20 且 ≤ `retrieval_top_k` | 重排保留数 |
| `score_threshold` | `number` | 否 | `0.0` | 0..1 | 重排后最低分 |
| `metadata` | `object` | 否 | `{}` | 键值 ≤ 64 字符 | 自定义标签 |

响应 `201`：KB 对象（`id`,`name`,`description`,`document_count`,`chunk_count`,`status`,`created_at`,`updated_at` + 上述配置字段）。

`status` 取值：`active`（可检索）、`indexing`（有任务在跑）、`failed`（最近一次入库失败且重试耗尽）。

### 2.2 其它 KB 接口

| 方法 | 路径 | 说明 | 成功码 |
| --- | --- | --- | --- |
| `GET` | `/knowledge-bases` | 分页列出当前用户的 KB | 200 |
| `GET` | `/knowledge-bases/{kb_id}` | 详情（含 `document_count`/`chunk_count`/`status`） | 200 |
| `PATCH` | `/knowledge-bases/{kb_id}` | 改 `name`/`description`/`metadata`/检索参数；**改 `chunk_size` 只影响后续文档** | 200 |
| `DELETE` | `/knowledge-bases/{kb_id}?force=false` | 删除 KB；非空且 `force=false` → `409 KB_NOT_EMPTY` | 204 |

### 2.3 需求

| ID | 需求 |
| --- | --- |
| `REQ-RAG-001` | 服务 MUST 提供知识库 CRUD，且所有操作以 `user_id` 为隔离边界 |
| `REQ-RAG-002` | 检索参数 MUST 支持三级覆盖：KB 级配置 > 请求参数 > 全局默认；`chunk_size`/`chunk_overlap` 在文档入库时**固化到 chunk 元数据**，后续改 KB 配置不回溯已有文档 |

## 3. 文档入库

### 3.1 `POST /knowledge-bases/{kb_id}/documents`

`Content-Type: multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | `file` | 与 `text` 二选一 | 上传文件 |
| `text` | `string` | 与 `file` 二选一 | 直接提交纯文本（≤ 1MB） |
| `doc_name` | `string` | 否 | 显示名；文件上传时默认取原文件名 |
| `metadata` | `string`(JSON) | 否 | 附加元数据，如 `{"source":"confluence","url":"..."}` |
| `chunk_size` | `integer` | 否 | 本次覆盖 KB 默认 |
| `chunk_overlap` | `integer` | 否 | 本次覆盖 KB 默认 |

响应 `202 Accepted`：

```json
{
  "doc_id": "doc_01J8ZQ1A2B3C4D5E6F7G8H9J0K",
  "task_id": "task_01J8ZQ7Y3M5R9T2W6B4N8V0C1D",
  "status": "PENDING",
  "doc_name": "售后政策 v3.pdf",
  "content_sha256": "9f2c...",
  "duplicated": false,
  "created_at": "2026-09-28T10:00:00.123Z"
}
```

### 3.2 格式与限制（`REQ-RAG-003`）

| 项 | 值 |
| --- | --- |
| 允许类型 | `.pdf` `.md` `.markdown` `.txt` `.docx` `.html` `.htm` |
| MIME 校验 | MUST 同时校验扩展名与**文件头魔数**（`%PDF-` / `PK\x03\x04` 等），不一致返回 `415` |
| 单文件大小 | ≤ `upload_max_mb`（默认 50 MB）→ 超出 `413 FILE_TOO_LARGE` |
| 单文档页数 | ≤ 500 页（PDF/ DOCX 转换后计），超出 `422 UNPROCESSABLE_DOCUMENT` |
| 解析后文本 | MUST ≥ `min_doc_chars`（默认 50 字符），否则 `422`（典型：扫描版 PDF 无文本层） |
| 单文档 chunk 数 | ≤ 10000，超出则截断并记警告 |
| 单 KB 文档数 | ≤ 1000（超出 `409`，提示新建 KB） |

### 3.3 其它文档接口

| 方法 | 路径 | 说明 | 成功码 |
| --- | --- | --- | --- |
| `GET` | `/knowledge-bases/{kb_id}/documents` | 分页列出文档（可按 `status` 过滤） | 200 |
| `GET` | `/documents/{doc_id}` | 详情：`status`、`chunk_count`、`error`、`indexed_at` | 200 |
| `GET` | `/documents/{doc_id}/chunks?limit=&cursor=` | 查看切片内容（调试用） | 200 |
| `DELETE` | `/documents/{doc_id}` | 删除文档：先删 Milvus 向量，再删 MySQL 记录，最后删 MinIO 对象 | 202（返回删除任务）或 204 |

> **关于 `/ingest`**：早期 README 的待办里写的是 `/ingest`，本 SRS 用上表接口取代它 —— 纯文本入库走同一端点的 `text` 字段（此时 `Content-Type` 为 `multipart/form-data` 或 `application/json` 均可）。若需保留 `/ingest` 作为兼容入口，MUST 仅做转发到同一服务方法，**不允许第二套实现**。

文档 `status` 取值：`PENDING` / `PARSING` / `CHUNKING` / `EMBEDDING` / `INDEXED` / `FAILED`（与任务状态映射见 [08-§2](./08-异步任务.md)）。

### 3.4 需求

| ID | 需求 |
| --- | --- |
| `REQ-RAG-004` | 入库接口 MUST 只做「校验 + 建任务」，P95 ≤ 200ms 返回 `202` + `task_id`；解析与向量化 MUST 在 Worker 中完成 |
| `REQ-RAG-007` | 同一 KB 内 MUST 以 `content_sha256` 去重：命中已 `INDEXED` 的同哈希文档时返回 `409 DOCUMENT_DUPLICATE`（并携带已有 `doc_id`）；命中仍在处理中的文档时幂等返回同一 `doc_id`/`task_id` |

## 4. 解析与 Chunk 规格（`REQ-RAG-005`）

### 4.1 解析

| 格式 | 解析器 | 关键要求 |
| --- | --- | --- |
| PDF | `pypdf` / `pdfplumber` | MUST 保留页码，写入 chunk 元数据 `page`；无文本层时视为不可处理 |
| Markdown | 原生读取 | MUST 保留标题层级，写入 `heading_path`（如 `售后政策 > 退款 > 时效`） |
| TXT | 原生读取 | 编码探测顺序 UTF-8 → UTF-8-BOM → GB18030，失败则 `422` |
| DOCX | `python-docx` | 段落 + 表格文本；MUST NOT 丢失表格内容 |
| HTML | `BeautifulSoup` | 去掉 `script`/`style`/`nav`/`footer`；按 `<h1..h6>` 生成 `heading_path` |

### 4.2 清洗

- 去除零宽字符、连续空行（> 2 个换行折叠为 2）、行首行尾空白。
- PDF 的重复页眉/页脚：同一文本出现在 ≥ 60% 页面顶部/底部 → 判定为页眉页脚并移除。
- MUST NOT 做会改变语义的改写（如同义词替换、翻译）。

### 4.3 Chunk 策略

| 项 | 约定 |
| --- | --- |
| 切分器 | `RecursiveCharacterTextSplitter` |
| 长度度量 | **token 数**（用 `embedding` tokenizer），非字符数 |
| 分隔符优先级 | `\n\n` → `\n` → `。` `！` `？` → `；` → `，` → 空格 → 字符 |
| 默认 `chunk_size` / `chunk_overlap` | 512 / 64 |
| 句边界规则 | MUST NOT 在中文句号/问号/叹号**之前**断开；断开点只能在标点之后 |
| 表格/代码块 | MUST 作为整体保留，不跨块切分；单块超 `chunk_size × 2` 时才允许内部切分 |
| 短块合并 | 相邻 chunk < `chunk_size × 0.3` 时 MUST 与后一块合并（避免碎片）。**这条是下限，不是停止条件**（UP-03）：合并会一直继续到「再加一块就超 `chunk_size`」为止，合并结果既不超 `chunk_size`（比 `AC-RAG-07` 的 1.5 倍更严）、也**不跨页/跨标题**（跨了会让引用指错位置）。旧实现把 0.3 当停止条件，碎段文档的有效块长被钉在配额的 34%、片数放大 2.6 倍（8MB：16,969 → 6,056 片） |
| 元数据 | 每个 chunk MUST 带：`chunk_id`,`doc_id`,`kb_id`,`user_id`,`chunk_index`,`content`,`content_sha256`,`char_start`,`char_end`,`page?`,`heading_path?`,`created_at` |
| 切分参数固化 | `chunk_size`/`chunk_overlap` 写入 chunk 元数据，供后续排障复现 |

> 中文场景下**按字符数切分会系统性破坏语义**（一页 A4 约 800–1200 汉字但 token 数差异大），故长度度量固定为 token。

### 4.4 Embedding

| 项 | 约定 |
| --- | --- |
| 模型 | 默认 **`Qwen/Qwen3-Embedding-0.6B`（硅基流动云端，1024 维，真批量 N 进 N 出）**；本地档为 `BAAI/bge-m3`（1024 维），方舟档见 [10-§7.3.1](./10-非功能需求与可观测性.md)。语义与实现见 [10-§7.3.2](./10-非功能需求与可观测性.md) |
| 归一化 | MUST `normalize_embeddings=True`，度量用 COSINE |
| 批量 | `embedding_batch_size` 默认 16（**调用粒度**）；云端档每条文本一次 HTTP、按 `ARK_EMBEDDING_CONCURRENCY` 并发；单批超时 120s |
| 空文本 | MUST NOT 送入模型；空白 chunk 在切分阶段丢弃 |
| 缓存 | 相同 `content_sha256` 的文本 SHOULD 命中 Redis 缓存（TTL 24h），避免重复计算 |
| 失败 | 单 chunk 失败重试 2 次（间隔 1s/3s），仍失败则任务失败 |

### 4.5 写入 Milvus

- MUST 使用 upsert 语义（集合主键为 `chunk_id`），重跑任务不产生重复数据。
- 写入 MUST 分批（默认 500 条/批），每批成功后上报任务进度。
- `kb_id` / `doc_id` / `user_id` MUST 建标量索引（INVERTED），用于过滤。

## 5. 检索与重排

### 5.1 `POST /knowledge-bases/{kb_id}/search`（调试接口，`REQ-RAG-008`）

| 字段 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `query` | `string` | — | 1..2000 字符 |
| `top_k` | `integer` | KB 默认（20） | 1..100 |
| `rerank_top_n` | `integer` | KB 默认（5） | 1..20 |
| `score_threshold` | `number` | KB 默认（0.0） | 0..1 |
| `with_rerank` | `boolean` | `true` | `false` 时只看向量召回原始结果 |

响应：

```json
{
  "query": "退款政策多少天",
  "recalled": 20,
  "returned": 5,
  "rerank_used": true,
  "elapsed_ms": 412,
  "items": [
    {
      "index": 1,
      "chunk_id": "chk_01J8ZQ9F4H2L7P3T6R8V1B5N0C",
      "doc_id": "doc_01J8ZQ1A2B3C4D5E6F7G8H9J0K",
      "doc_name": "售后政策 v3.pdf",
      "page": 4,
      "heading_path": "售后政策 > 退款 > 时效",
      "vector_score": 0.7341,
      "rerank_score": 0.8123,
      "score": 0.8123,
      "content": "自签收之日起 7 个自然日内……"
    }
  ]
}
```

| 字段语义 | 说明 |
| --- | --- |
| `vector_score` | Milvus 余弦相似度（检索分数） |
| `rerank_score` | 交叉编码器打分（**未归一化**，仅用于排序与阈值，不要跨模型比较） |
| `score` | 最终排序分 = `rerank_score`（`with_rerank=true`）否则 = `vector_score` |

### 5.2 检索规则（`REQ-RAG-006`）

1. 过滤条件 MUST 为 `user_id == <当前用户> AND kb_id in (<目标 KB 集>)`，在 Milvus 侧用 **expr 过滤**（不是取回后再过滤）。
2. 召回 `top_k` → 送入 Reranker → 取 `rerank_top_n`。
3. 阈值过滤：`rerank_score < score_threshold` 的项丢弃。
4. 同文档相邻 chunk 合并：同一 `doc_id` 且 `chunk_index` 相邻（差 ≤ 1）的条目 SHOULD 合并为一条，避免上下文重复（合并后 `chunk_id` 取较小者，`merged=true`）。
5. Context 组装：每条以 `[{index}] {doc_name} · 第{page}页\n{content}` 形式拼接；总 token ≤ `rag_context_token_budget`（默认 2500），超限时按分数从低到高丢弃。
6. Rerank 不可用（模型缺失/OOM）时 MUST 降级为纯向量分数排序，并置 `degraded_reasons` 含 `rerank_skipped`。
7. 相对分数分界（MMR/多样性）为 P2：当 top_n 内同一 `doc_id` 占比 > 60% 时 SHOULD 引入多样性重排。

### 5.3 检索参数默认值汇总

| 参数 | 全局默认 | KB 可覆盖 | 请求可覆盖 |
| --- | --- | --- | --- |
| `retrieval_top_k` | 20 | 是 | 是（`top_k`） |
| `rerank_top_n` | 5 | 是 | 是 |
| `score_threshold` | 0.0 | 是 | 是 |
| `rag_context_token_budget` | 2500 | 否 | 否 |
| 度量 | COSINE | 否 | 否 |

## 6. 引用溯源（`REQ-RAG-009`）

`Reference` 结构（对话响应与 SSE `reference` 帧共用）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `index` | `integer` | 是 | 从 1 开始，与回答中 `[n]` 标注一致 |
| `chunk_id` | `string` | 是 | `chk_*` |
| `doc_id` | `string` | 是 | `doc_*` |
| `kb_id` | `string` | 是 | `kb_*` |
| `doc_name` | `string` | 是 | 文档显示名 |
| `page` | `integer \| null` | 否 | 页码；无分页概念时为 `null` |
| `heading_path` | `string \| null` | 否 | Markdown/HTML 标题路径 |
| `score` | `number` | 是 | 最终排序分 |
| `snippet` | `string` | 是 | 引用片段预览，取 chunk 前 200 字符 |
| `content_sha256` | `string` | 是 | 便于前端做「同一片段」去重高亮 |

生成要求：

- Prompt MUST 要求模型在引用处标注 `[n]`，并在 system 中给出「只使用给定资料、无法回答时明确说明」的约束。
- 回答中出现的 `[n]` MUST NOT 超出 `references` 长度；服务端 SHOULD 校验并剔除越界标注（记警告）。
- `references` 顺序 MUST 与注入 Context 的 `index` 顺序一致（按分数降序）。

## 7. 删除与一致性

| 操作 | 顺序 | 说明 |
| --- | --- | --- |
| 删文档 | Milvus `delete(doc_id)` → MySQL `document_chunk`/`document` → MinIO 对象 | 任一步失败则任务 `FAILED` 可重试；重试为幂等（按 `doc_id` 全量删） |
| 删 KB | 先删全部文档（同上）→ 删 KB 记录 | `force=true` 时跳过 `KB_NOT_EMPTY` 校验 |
| 改 KB chunk 参数 | 只改元数据 | **不**自动重切已有文档；提供 `POST /knowledge-bases/{kb_id}/reindex`（P1）重建索引 |

| ID | 需求 |
| --- | --- |
| `REQ-RAG-010` | 删文档/删 KB MUST 同时清理 Milvus 向量、MySQL 切片记录、MinIO 对象，不允许残留向量（残留会导致已删文档仍被检索到） |
| `REQ-RAG-011` | 所有 RAG 接口 MUST 强制 `user_id` 隔离：跨用户访问一律 `404 *_NOT_FOUND`（不返回 403，避免枚举） |

## 8. 验收标准

| ID | 关联需求 | 验收点 |
| --- | --- | --- |
| `AC-RAG-01` | `REQ-RAG-001/011` | 创建 KB → `GET /knowledge-bases` 能查到；用另一个 `user_id` 访问该 KB 返回 404 |
| `AC-RAG-02` | `REQ-RAG-001` | 同名创建第二次返回 `409 KB_NAME_CONFLICT` |
| `AC-RAG-03` | `REQ-RAG-003` | 上传 `.exe` 返回 415；上传 51MB 的 PDF 返回 413；扩展名为 `.pdf` 但内容是文本的文件返回 415 |
| `AC-RAG-04` | `REQ-RAG-004` | 上传 10MB PDF：接口 P95 ≤ 200ms，返回 202 + `task_id`，此刻 Milvus 中尚无该文档向量 |
| `AC-RAG-05` | `REQ-RAG-007` | 同一文件连续上传两次 → 第二次 `409 DOCUMENT_DUPLICATE` 且 `details.doc_id` 等于第一次的 `doc_id` |
| `AC-RAG-06` | `REQ-RAG-005` | 上传含 3 级标题的 Markdown → 每个 chunk 的 `heading_path` 非空且正确 |
| `AC-RAG-07` | `REQ-RAG-005` | 切分结果中不存在「以 `。` 开头」的 chunk；无 chunk 长度 > `chunk_size × 1.5` |
| `AC-RAG-08` | `REQ-RAG-005` | 同一文档重复跑入库任务 2 次 → Milvus 中该 `doc_id` 的向量条数不变（upsert 生效） |
| `AC-RAG-09` | `REQ-RAG-002/006` | 请求传 `top_k=20, rerank_top_n=5` 时 `recalled = 20`、`returned = 5`（请求参数覆盖 KB 默认）；返回项全部满足 `kb_id ∈ 请求 KB` 且属于当前用户 |
| `AC-RAG-10` | `REQ-RAG-006` | `score_threshold=0.5` 时返回项 `score` 全部 ≥ 0.5 |
| `AC-RAG-11` | `REQ-RAG-006` | 删除 reranker 权重后用 `/search` → 仍返回结果，`rerank_used=false`，`score == vector_score` |
| `AC-RAG-12` | `REQ-RAG-008` | `with_rerank=false` 时 `rerank_used=false` 且 `items[].rerank_score` 为 `null` |
| `AC-RAG-13` | `REQ-RAG-009` | 回答含 `[1]`/`[2]` 时 `references` 长度 ≥ 2，且 `references[i].snippet` 为该 chunk `content` 的前缀 |
| `AC-RAG-14` | `REQ-RAG-009` | mock LLM 故意输出不存在的 `[9]` → 该标注被剔除并在日志产生 warning |
| `AC-RAG-15` | `REQ-RAG-010` | 删除文档后：Milvus 按 `doc_id` 查询为 0 条、MySQL 无 chunk 记录、MinIO 对象不存在；再 `/search` 不含该文档 |
| `AC-RAG-16` | `REQ-RAG-004` | 上传扫描版（无文本层）PDF → 任务最终 `FAILED`，文档 `status=FAILED`，`error.code=UNPROCESSABLE_DOCUMENT` |
