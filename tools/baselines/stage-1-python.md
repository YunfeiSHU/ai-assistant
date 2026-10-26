# 阶段 1 基线 · Python 直连

> 目的：给后续所有「优化」一个可对照的起点。没有这条基线，任何「变快了」都只是体感。
>
> 原始数据：[`stage-1-python-20261026.json`](./stage-1-python-20261026.json)

## 环境

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-26 10:20（Asia/Shanghai） |
| 系统 | Windows / PowerShell |
| Python | 3.12.14（仓库 `.venv`） |
| 服务 | `uvicorn app.main:app`，`127.0.0.1:8000` |
| 基础设施 | `INFRA_BACKEND=memory`（无 Redis / MySQL / Milvus / MinIO） |
| 鉴权 | `AUTH_ENABLED=false`（本地档） |
| 模型 | `deepseek-flash`，`https://api.deepseek.com/v1` |
| 请求 | `use_rag=false`、`use_memory=false`、`use_tools=false` |
| 方式 | 直连 Python `/api/v1/chat/stream`，**未经 Go 网关** |

## 结果

命令：`tools/verify_stage1.py --rounds 1`

5 条固定 prompt，各 1 次：

| 指标 | 数值 |
| --- | --- |
| 成功 | **5 / 5** |
| 首 token p50 | **1034.5 ms** |
| 首 token p95 | 1107.6 ms（仅 5 个样本，参考值） |
| 首帧（`meta`）p50 | 约 30 ms |
| 总时长 p50 | 1206.1 ms |
| tokens | 180 ~ 639 |

冷启动对照（同日 10:16 的第一次运行）：首 token p50 1393.6 ms、p95 2348.4 ms，其中首条 6.5 s。
两次都在同一台机器、同一模型；差异主要来自首次运行的路由/连接预热。**以 warm 的 1.03 s 作为基线，
冷启动单独记录。**

## 断连取消

同一命令内置检查：

- 读到第一个 `token` 后主动断开；
- `ai_chat_canceled_total{reason="client_disconnect"}` 从 **1 → 2**；
- 服务日志同时出现 `chat.canceled ... tokens=2`。

结论：`REQ-CHAT-006`（客户端断开即停止上游）在 Python 直达路径上成立。

## 全栈冒烟（同日 10:23）

在 Python 直连之外，补了一次真正的全栈链路：

```text
客户端 → Gin (8080) → gRPC (50051) → Python → DeepSeek → SSE
```

| 项 | 结果 |
| --- | --- |
| 传输 | `AI_GRPC_ENABLED=true`，Python `GRPC_ENABLED=true` |
| 注册 / 登录 / 建会话 | 201 / 200 / 201 |
| 流式响应 | 200 `text/event-stream; charset=utf-8` |
| 事件 | 52 帧，`meta → token* → usage → done`，无 `error` |
| 首 token（warm） | **1242.5 ms** |
| 总时长 | 1665.3 ms |
| tokens | 226 |
| 落库 | `conversation.message_count=2`；`message` 两行，`seq=1/2`，状态 `completed` |

冷启动对照：同一条链路的第一次请求首 token 4450.1 ms、总时长 4873.6 ms；第二次（warm）降到 1.24 s。
模型/连接预热的影响远大于网关转发本身。

复现：`ai-platform\.venv\Scripts\python.exe tools\smoke_gateway.py`

> **环境问题（本次实测发现）**：`ai-platform/.env` 与 `ai-platform-go/.env` 的 `JWT_SECRET` **不一致**。
> 本次因为 Python 侧 `AUTH_ENABLED=false` 才没有暴露；一旦开启鉴权，网关签发的 access token 会被
> Python 拒绝（全链路 401）。阶段 3 之前必须把两侧密钥对齐，并纳入启动前检查。

## 这次没有覆盖的

- **未经 Go 网关**：Gin → SSE、网关首帧改写、`partial` 落库都不在本次证据里；网关侧的
  gRPC/SSE 桥接由 `go test ./internal/data/ai ./internal/service` 覆盖（本机通过），
  真实全栈还需要 MySQL + Redis 起网关。
- **未开 RAG / 记忆 / 工具**：它们是后续阶段，当前基线只代表「纯对话」。
- **单轮、5 个样本**：不足以谈分位数；阶段 9 的压测再补。
- 模型与网络都在外网，绝对数字不可跨机器比较；只用于**同机前后对比**。

## 复现

```powershell
# 1. 起服务（零依赖档）
cd ai-platform
$env:INFRA_BACKEND="memory"; $env:TASK_RUNNER="inline"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2. 另开终端，跑验收
ai-platform\.venv\Scripts\python.exe tools\verify_stage1.py --rounds 1
```

## 下一步

1. 起网关（MySQL + Redis），用同一脚本加 `--base-url http://127.0.0.1:8080/api/v1 --token ...` 跑一遍；
   两者差值就是「网关 + 鉴权 + 台账」的成本。
2. 阶段 2 验证会话/消息台账时，把 `rounds` 提到 3，并记录首字与总时长。
3. 任何后续优化（提示词、上下文裁剪、RAG 重排、Agent 步数）都在**同一台机器、同一组 prompt**上对比，
   写进本目录的下一个基线文件。
