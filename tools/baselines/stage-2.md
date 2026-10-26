# 阶段 2 验收 · 会话与消息台账

> 目标：证明「多轮历史可回看、断线有半截、并发 seq 不撞、重启不丢」。
>
> 脚本：[`../verify_stage2.py`](../verify_stage2.py)；分 `write` / `read` 两段，中间重启网关。

## 环境

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-26 10:35（Asia/Shanghai） |
| 链路 | 客户端 → Gin (8080) → gRPC (50051) → Python → DeepSeek |
| 存储 | MySQL `ai_platform`（16 张表） |
| Redis | 未启动；网关告警降级（限流走进程内、token 版本回源 MySQL） |
| Python 鉴权 | `AUTH_ENABLED=false`（两侧 `JWT_SECRET` 仍不一致，阶段 3 前必须对齐） |
| 限流 | **临时** `CONV_RATE_PER_MINUTE=100`、`MSG_RATE_PER_MINUTE=100`（默认 30 / 20，55 次建会话会被挡） |

## 写阶段（重启前）

命令：`tools/verify_stage2.py --phase write`

**8 / 8 通过**

| 检查 | 结果 |
| --- | --- |
| 用户 A 注册/登录 | 通过 |
| 断连后 assistant 落 `partial` 且有正文 | 通过 |
| 10 条并发消息全部 200 | 通过 |
| 20 条台账 `seq` 唯一且连续 1..20 | 通过（10 用户 + 10 助手） |
| 55 条会话分 3 页（20/20/15） | 通过 |
| 55 条会话无重复无遗漏 | 通过（55 个唯一 ID） |
| 跨用户读会话返回 404 | 通过（`CONVERSATION_NOT_FOUND`） |
| 归档会话发消息返回 409 | 通过（`CONVERSATION_ARCHIVED`） |

## 读阶段（重启网关后）

命令：`tools/verify_stage2.py --phase read`

**4 / 4 通过**

| 检查 | 结果 |
| --- | --- |
| 重启后 `partial` 消息仍在且内容一致 | 通过（按 `message_id` + 长度比对） |
| 重启后 20 条消息 `seq` 仍唯一连续 | 通过 |
| 重启后 55 个会话分页仍无重复无遗漏 | 通过（页大小仍为 20/20/15） |
| 重启后跨用户仍 404 | 通过 |

Python gRPC 进程在两次运行之间**没有重启**；重启的只有网关。也就是说，本证据覆盖的是
「网关进程重启 + MySQL 持久化」，不覆盖「MySQL 重启」。

## 这次验证到的实现事实

- 并发写同一会话时，`seq` 由数据库事务内分配，10 路并发没有出现重复或跳号。
- 客户端读到首个 `token` 就断开，网关仍把已生成内容落为 `partial`；重启后按 ID 查得到。
- 游标分页的 `next_cursor` 在两次进程之间可继续使用（游标是 `pinned + last_message_at + id` 的编码，
  不依赖进程内状态）。
- 归档是状态约束，不是前端拦截：直接调接口也会得到 `409`。

## 没有覆盖 / 已知偏差

- **Redis 未启动**：限流与配额精度是降级档；阶段 3 应在 Redis 起来后复跑。
- **限流被临时放宽**：55 次建会话在默认 30/min 下会被 429；这份结果不能当作默认配置的证明。
- **未覆盖 MySQL 重启 / 主从切换**：属于阶段 7/9 的持久化验收。
- **未做消息删除后的一致性**：那是 B-04，阶段 6 处理。
- 测试账号/会话留在本地 MySQL，未清理；`tools/out/stage2-state.json` 保存了两组测试账号
  （gitignore，不提交）。

## 复现

```powershell
# Python gRPC
cd ai-platform
$env:INFRA_BACKEND="memory"; $env:TASK_RUNNER="inline"; $env:AUTH_ENABLED="false"
$env:GRPC_ENABLED="true"; $env:GRPC_HOST="127.0.0.1"; $env:GRPC_PORT="50051"
.\.venv\Scripts\python.exe -m app.grpc

# 网关（临时放宽建会话/发消息限流）
cd ..\ai-platform-go
$env:ENV_FILE="<仓库绝对路径>\ai-platform-go\.env"
$env:AI_GRPC_ENABLED="true"; $env:CONV_RATE_PER_MINUTE="100"; $env:MSG_RATE_PER_MINUTE="100"
.\bin\server.exe

# 写 → 重启网关 → 读
cd ..
ai-platform\.venv\Scripts\python.exe tools\verify_stage2.py --phase write
# 停掉网关再起一次
ai-platform\.venv\Scripts\python.exe tools\verify_stage2.py --phase read
```

## 下一步

阶段 3 的第一件事不是写代码，而是**对齐两侧 `JWT_SECRET`**：

1. 用同一个密钥启动 Python（`AUTH_ENABLED=true`），验证网关签发的 token 被 Python 接受；
2. 再验 refresh 轮换、登出、改密后的旧 token 失效；
3. 最后验配额 429 与登录限流——这两项需要 Redis 或明确的降级口径。
