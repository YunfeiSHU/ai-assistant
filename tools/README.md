# tools · 阶段验收脚本

按 [`ROADMAP.md`](../ROADMAP.md) 的约定：**一个能力 = 一个可重复运行的验收脚本**。
脚本失败即阶段未完成；脚本通过才允许进入下一阶段。

| 脚本 | 阶段 | 是否需要服务 | 检查什么 |
| --- | --- | --- | --- |
| `verify_stage0.py` | 0 契约与骨架 | 否 | proto 只暴露 Chat/ChatStream、两侧生成物、SSE 事件名与帧字节、错误信封、错误码、trace、心跳、ChatRequest/Response 字段 |
| `verify_stage1.py` | 1 真实模型闭环 | Python | `/health`、首 token 基线、SSE 响应头、断连后取消上游 |
| `smoke_gateway.py` | 1 全栈冒烟 | Python gRPC + 网关 | 注册/登录/建会话/流式，断言 `meta` 首帧、`usage`、`done`、无 `error` |
| `verify_stage2.py` | 2 会话与台账 | Python gRPC + 网关 + MySQL | 断连 `partial`、10 并发 `seq`、55 会话分页、跨用户 404、归档 409；分 `write`/`read` 两段验重启 |
| `measure_chat.py` | 1 基线 / 后续对比 | Python | 固定 prompt × N 轮的首 token / 总时长 / tokens，输出 JSON |

## 运行

```powershell
# 阶段 0：纯静态，不需要起任何服务
ai-platform\.venv\Scripts\python.exe tools\verify_stage0.py

# 阶段 1：先起 Python（零依赖档），再跑
cd ai-platform
$env:INFRA_BACKEND="memory"; $env:TASK_RUNNER="inline"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另开终端，回到仓库根
ai-platform\.venv\Scripts\python.exe tools\verify_stage1.py --rounds 1
```

经网关时用全栈冒烟脚本（需要 Python gRPC + 网关 + MySQL）：

```powershell
# 1. Python gRPC
cd ai-platform
$env:INFRA_BACKEND="memory"; $env:TASK_RUNNER="inline"; $env:AUTH_ENABLED="false"
$env:GRPC_ENABLED="true"; $env:GRPC_HOST="127.0.0.1"; $env:GRPC_PORT="50051"
.\.venv\Scripts\python.exe -m app.grpc

# 2. 网关（另开终端）
cd ai-platform-go
$env:ENV_FILE="<仓库绝对路径>\ai-platform-go\.env"
$env:AI_GRPC_ENABLED="true"
.\bin\server.exe

# 3. 回到仓库根，跑冒烟
ai-platform\.venv\Scripts\python.exe tools\smoke_gateway.py
```

> `verify_stage1.py` 面向 Python 直连（请求体是 `query`）；网关的请求体是 `content` 且路径带
> `conversation_id`，两者不复用。阶段 2 的脚本走网关。

## 阶段 2：写 → 重启 → 读

```powershell
# 网关需要放宽建会话/发消息限流（55 个会话会被默认 30/min 挡住）
$env:CONV_RATE_PER_MINUTE="100"; $env:MSG_RATE_PER_MINUTE="100"

# 写阶段
ai-platform\.venv\Scripts\python.exe tools\verify_stage2.py --phase write

# 停掉网关并重新启动（Python gRPC 可以不动），然后读阶段
ai-platform\.venv\Scripts\python.exe tools\verify_stage2.py --phase read
```

状态写在 `tools/out/stage2-state.json`（gitignore）。`--phase all` 可以一次跑完，但**不验证重启**。

## 约定

- **退出码**：`0` 通过，`1` 失败；`verify_stage1.py` 会先跑基线再跑断连。
- **基线**：每次有意义的改动前后，用同一组 prompt、同一台机器；结果放 `tools/baselines/`。
  `tools/out/` 是一次性输出，已 gitignore。
- **不要**把「这次感觉快了」当结论；看 `first_token_ms_p50` 与 `total_ms_p50`。
- **Go 缓存**：`verify_stage0.py` 会把 `GOCACHE` / `GOTMPDIR` 指到 `ai-platform-go/.gocache`、
  `.gotmp`（系统临时目录在受限环境下可能不可写），两者均已 gitignore。
- **编码**：脚本会把 stdout 切到 UTF-8；子进程通过 `PYTHONIOENCODING=utf-8` 传递。

## 目录

```text
tools/
├─ verify_stage0.py        # 契约一致性
├─ verify_stage1.py        # Python 直连：真实流式 + 断连
├─ smoke_gateway.py        # 全栈冒烟：Gin → gRPC → Python → SSE
├─ verify_stage2.py        # 会话/台账：partial、并发 seq、分页、重启
├─ measure_chat.py         # 基线测量（可单独用于对比）
├─ baselines/              # 提交进仓库的基线证据
└─ out/                    # 一次性 JSON 输出（gitignore）
```
