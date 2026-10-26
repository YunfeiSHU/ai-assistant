# AI Assistant · Web

真实前端应用（Vite + React + TypeScript）。视觉参照是 [`prototype/`](./prototype)，
它是不接后端的静态原型；本目录是接真实网关的正式实现。

## 状态

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| F0 脚手架 | ✅ | Vite / React / TS / Tailwind / TanStack Router / Query / Zustand；设计令牌复用原型 |
| F1 登录与启动恢复 | ✅ | 真实 `/auth/*`、`/me`；refresh 单飞；路由守卫；E2E 通过 |
| F2 会话与历史 | ✅ | 真实列表/分页/改名/置顶/归档/删除、消息历史、Markdown |
| F3 流式对话 | ✅ | `fetch + ReadableStream`、停止/`partial`、引用角标、工具轨迹 |
| F4 知识库与任务 | ✅ | KB CRUD、XHR 上传进度、文档/切片、检索调试、任务轮询/取消/重试 |
| F5 记忆/配额/设置 | ✅ | 记忆 CRUD/设置、配额/用量、上下文透视、KB 关联、改密/主题、429 倒计时 |
| F6 质量收口 | ✅ | 键盘/焦点、错误码动作、500px 移动端、E2E 串联、首屏 102.37KB |

验收记录：[`docs/03-前端F0-F1验收.md`](./docs/03-前端F0-F1验收.md)、
[`docs/04-前端F2验收.md`](./docs/04-前端F2验收.md)、
[`docs/06-前端F3验收.md`](./docs/06-前端F3验收.md)、
[`docs/07-前端F4验收.md`](./docs/07-前端F4验收.md)、
[`docs/08-前端F5验收.md`](./docs/08-前端F5验收.md)、
[`docs/09-前端F6验收.md`](./docs/09-前端F6验收.md)。
技术实现总览：[`docs/10-前端F0-F6技术实现.md`](./docs/10-前端F0-F6技术实现.md)。
联调问题与修复过程：[`docs/05-联调问题记录.md`](./docs/05-联调问题记录.md)。
阶段计划：[`../ROADMAP.md`](../ROADMAP.md)；后端 B 项与联调决策：[`docs/05-联调问题记录.md`](./docs/05-联调问题记录.md) §5。

**B-01/B-02/B-03/B-04/B-05/B-06/B-07/B-09/B-10/B-11/B-13/B-12** 均已完成：
任务 SSE、查看原文、重新生成、删消息同步上下文、流式 meta 用户 ID、侧栏预览、限流剩余量、
文档附件、消息检索、after_seq、任务取消中、网关 OpenAPI 42 path 路由契约。
B-03 当前为非流式重新生成；B-04 只清短期上下文；B-09 只支持已入库文档附件。

## 命令

```powershell
pnpm install
pnpm dev        # http://127.0.0.1:3000
pnpm typecheck
pnpm build
pnpm e2e:f1     # 登录/刷新/退出/失败态
pnpm e2e:f2     # 55 会话分页、历史、改名/置顶/归档/删除
pnpm e2e:f3     # 真实流式、停止 partial、刷新对账
pnpm e2e:f4     # 建库、上传、切片、检索、任务、失败重试
pnpm e2e:f5     # 记忆、配额、上下文、KB 关联、改密
pnpm e2e:f6     # 重新生成、删消息、Modal 键盘、500px 移动端
```

## 前置

F1 只需要网关与 MySQL；F2 的 E2E 还需要 Python 生成一条历史消息，并把建会话限流临时放宽：

```powershell
# Python（仅 F2 E2E 的历史消息需要）
cd ../ai-platform
$env:INFRA_BACKEND="memory"; $env:TASK_RUNNER="inline"; $env:AUTH_ENABLED="false"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 网关（F2 E2E 需要 55 次建会话）
cd ../ai-platform-go
$env:ENV_FILE="<仓库绝对路径>\ai-platform-go\.env"
$env:AI_GRPC_ENABLED="false"
$env:CONV_RATE_PER_MINUTE="100"; $env:MSG_RATE_PER_MINUTE="100"
.\bin\server.exe
```

前端 dev 固定 `3000`，通过 Vite 代理把 `/api` 转到 `127.0.0.1:8080`。
因此**不需要**修改网关 CORS 白名单。

> Redis：已改用 Docker（`ai-assistant-redis`，`127.0.0.1:6379`），网关 `/health/ready` 200。
> WSL 原生 Redis 只监听 `127.0.0.1`，Windows 不可达，已不再使用。

```powershell
docker run -d --name ai-assistant-redis --restart unless-stopped -p 127.0.0.1:6379:6379 redis:7-alpine
```

F3 流式验收时，Python 用 `AUTH_ENABLED=true` 启动；两侧 `JWT_SECRET` 已对齐，
网关不需要额外参数。

F5 E2E 预置消息用 `use_memory=true` 以便上下文落库；429 定向测试用
`MSG_RATE_PER_MINUTE=1`，只用于测试，测试后恢复。

## 结构

```text
web/
├─ src/
│  ├─ app/            # router / AppLayout（认证门 + 已认证壳）/ query-client
│  ├─ features/
│  │  ├─ auth/        # LoginPage + auth store
│  │  ├─ conversations/ # 会话列表/CRUD/分页查询
│  │  ├─ messages/    # 消息历史查询与类型
│  │  ├─ knowledge/   # KB、文档、切片、检索、上传进度
│  │  ├─ tasks/       # 任务列表与轮询
│  │  ├─ memory/      # 长期记忆、设置、上下文/摘要
│  │  ├─ quota/       # 配额与用量
│  │  ├─ chat/        # ChatPage + ConversationPage + 流式状态机/引用/工具轨迹
│  │  └─ settings/    # 账号信息与退出
│  ├─ shared/
│  │  ├─ api/         # base / error / session / client / types
│  │  ├─ lib/         # 时间与格式化
│  │  └─ ui/          # Icon / Markdown / Modal / StagePage / toast
│  └─ styles/         # Tailwind 入口 + 应用补充样式
├─ e2e/               # F1 / F2 / F3 浏览器验收
├─ previews/          # 验收截图
├─ prototype/         # 静态原型（不接后端）
└─ docs/              # 前端设计文档与阶段验收
```

## 约定

- access token 只在内存；refresh 只在 HttpOnly `ai_refresh` Cookie，JS 不可读（B-08）。
- 401 只在 `TOKEN_EXPIRED` 时刷新并重放一次；`INVALID_REFRESH_TOKEN` 直接回登录。
- 所有后端错误归一成 `ApiError`，UI 展示 `code · message`。
- 不改后端时，先在 `docs/05-联调问题记录.md` §5 记录联调决策。
