/* ============================================================
   AI Assistant · 静态原型
   纯前端 mock：无网络请求。交互覆盖：登录、会话、流式、停止、
   引用定位、工具轨迹、降级、知识库、任务、记忆、配额、设置。
   ============================================================ */

"use strict";

/* ------------------------------ 图标 ------------------------------ */

const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',
  task: '<path d="m9 11 3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  brain: '<path d="M12 5a3 3 0 0 0-5.9-.7A3 3 0 0 0 4 10a3 3 0 0 0 0 4 3 3 0 0 0 2.1 5.7A3 3 0 0 0 12 19Z"/><path d="M12 5a3 3 0 0 1 5.9-.7A3 3 0 0 1 20 10a3 3 0 0 1 0 4 3 3 0 0 1-2.1 5.7A3 3 0 0 1 12 19Z"/>',
  gauge: '<path d="M12 14l4-4"/><path d="M3.5 18a9 9 0 1 1 17 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  pin: '<path d="M12 17v5"/><path d="M9 3h6l-1 6 4 4H6l4-4-1-6Z"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v12h14V8"/><path d="M10 12h4"/>',
  context: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  upload: '<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>',
  moon: '<path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
  download: '<path d="M12 4v12"/><path d="m6 10 6 6 6-6"/><path d="M4 20h16"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
};

function icon(name) {
  const body = ICONS[name] || ICONS.spark;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

function initIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
}

/* ------------------------------ 工具函数 ------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function uid() {
  return `m_${Math.random().toString(36).slice(2, 9)}`;
}

function nowTime() {
  const d = new Date();
  return d.toTimeString().slice(0, 5);
}

function scrollBottom(force = false) {
  const el = $("#chatScroll");
  if (!el) return;
  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
  if (force || near) el.scrollTop = el.scrollHeight;
}

function toast(message, kind = "") {
  const stack = $("#toastStack");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  stack.append(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  }, 2600);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制到剪贴板", "ok");
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("已复制到剪贴板", "ok");
    } catch {
      toast("当前环境不允许复制", "warn");
    }
    area.remove();
  }
}

/* ------------------------------ Mock 数据 ------------------------------ */

const REFS = [
  {
    index: 1,
    doc: "13-技术设计说明.md",
    heading: "§6 对话与流式",
    page: null,
    score: 0.82,
    snippet: "首帧超时在网关兜底：StreamSend 等第一帧才写响应头，35s 内没等到就回 504 AI_TIMEOUT，并把本轮落成 partial。",
  },
  {
    index: 2,
    doc: "04-AI服务编排与网关.md",
    heading: "§5 时延表",
    page: null,
    score: 0.78,
    snippet: "首字节 35s、空闲 150s、整轮 360s；心跳 15s。客户端可以用 45s 无帧作为断线判定。",
  },
  {
    index: 3,
    doc: "03-对话与流式输出.md",
    heading: "§4.2 事件序列",
    page: 3,
    score: 0.71,
    snippet: "meta → reference* → tool_call/tool_result* → token* → usage → done；出错时以 error 收尾且不再有 done。",
  },
];

const REF_COLORS = ["var(--accent)", "var(--accent-2)", "#f0abfc", "#fbbf24", "#94a3b8", "#f87171"];

const C1_HTML = `
<p>首帧超时在 <strong>网关</strong> 兜底：<code>StreamSend</code> 要等第一帧才写响应头，35 秒内没等到就回
<code>504 AI_TIMEOUT</code>，并把本轮落成 <code>partial</code>。<button class="cite" data-ref="1">1</button></p>
<h3>判断顺序</h3>
<ol>
  <li>先看 <code>meta</code> 是否到达（SSE 首帧）</li>
  <li>再看 <code>token</code> 是否连续，还是卡在某个工具调用</li>
  <li>最后看 <code>done.partial</code> 与 <code>finish_reason</code></li>
</ol>
<table>
  <tr><th>现象</th><th>更可能的根因</th><th>处理</th></tr>
  <tr><td>35s 后 504</td><td>上游首 token 太慢</td><td>调 <code>AI_FIRST_BYTE_TIMEOUT_SECONDS</code></td></tr>
  <tr><td>流中突然断</td><td>客户端断开 / 空闲超时</td><td>落 <code>partial</code>，不重放</td></tr>
  <tr><td>只有 error 帧</td><td>上游异常</td><td>保留 <code>trace_id</code> 排查</td></tr>
</table>
<p>所以先改网关的首字节超时与错误映射，Python 侧只负责把上游异常归一成 <code>error</code> 帧。<button class="cite" data-ref="2">2</button></p>
`;

const C2_HTML = `
<p>把合并比例从 <code>0.3</code> 提到 <code>1.0</code>，本质是拿<strong>向量化成本</strong>换<strong>正文完整性</strong>。</p>
<table>
  <tr><th>指标</th><th>0.3 × chunk_size</th><th>1.0 × chunk_size</th></tr>
  <tr><td>切片数（8MB 文档）</td><td>16,969</td><td>6,056</td></tr>
  <tr><td>正文丢失</td><td>约 41%</td><td>接近 0</td></tr>
  <tr><td>向量化成本</td><td>基准</td><td>+26% ~ +28%</td></tr>
  <tr><td>检索噪声</td><td>碎片多</td><td>单块更长，需 rerank</td></tr>
</table>
<h3>结论</h3>
<ul>
  <li>先保正文完整性，再谈成本；截断比多花钱更难补救</li>
  <li>用评测集验证 top-5 命中率，而不是只看切片数</li>
  <li>本轮 rerank 没有跑起来，答案按「未重排」降级展示 <button class="cite" data-ref="3">3</button></li>
</ul>
`;

const C3_HTML = `
<p>只按 IP 挡不住分布式撞库，只按账号又会被「多账号分散累加」绕过，所以是<strong>双维度</strong>：</p>
<ul>
  <li><strong>IP 维度</strong>：挡住同一来源的批量尝试</li>
  <li><strong>账号维度</strong>：把同一账号的慢速尝试累计起来</li>
  <li>两者都命中才拒绝；只命中一个时仍然放行并告警</li>
</ul>
<p>配额是「总量」，限流是「速率」，两者不要混在一个计数器里。</p>
`;

const C4_HTML = `
<p>本周主线是把「能跑」变成「可验收」：</p>
<ul>
  <li><strong>阶段 0</strong>：18 项契约检查通过，SSE 帧字节与错误码对齐</li>
  <li><strong>阶段 1</strong>：全栈冒烟 52 帧，首 token 1.24s，断连取消指标 +1</li>
  <li><strong>阶段 2</strong>：10 并发 seq 唯一；55 会话翻 3 页；重启后 partial 仍在</li>
</ul>
<p>下周：先把 JWT 密钥对齐，再进认证与配额；前端原型同步推进。</p>
`;

const C5_HTML = `
<p>MCP 的失败必须变成<strong>工具结果</strong>，而不是整轮 5xx。否则前端连「哪一步失败」都看不到。</p>
<blockquote>策略：单 Server 懒重连 + 行内熔断；失败回注给模型，让它决定继续还是换工具。</blockquote>
<p>这次 <code>filesystem</code> 首次连接超时，轨迹里保留 <code>timeout</code> 状态，正文仍然完成。</p>
`;

const conversations = [
  {
    id: "c1",
    title: "SSE 流式输出排查",
    preview: "首帧超时在网关兜底：35s 内没等到…",
    time: "10:32",
    pinned: true,
    messages: [
      { id: "c1u", role: "user", content: "首帧 35 秒超时是哪一层在兜底？应该改网关还是 Python？", time: "10:30" },
      {
        id: "c1a", role: "assistant", html: C1_HTML, time: "10:32",
        refs: [REFS[0], REFS[1]],
        meta: { model: "deepseek-flash", prompt: 812, completion: 64, total: 876, elapsed: 2043, trace: "4bf92f3577b34da6a3ce929d0e0e4736" },
      },
    ],
  },
  {
    id: "c2",
    title: "RAG 切片策略对比",
    preview: "合并填满 chunk_size 后，正文丢失从 41%…",
    time: "昨天",
    messages: [
      { id: "c2u", role: "user", content: "切片合并填满 chunk_size 有什么代价？", time: "昨天 16:20" },
      {
        id: "c2a", role: "assistant", html: C2_HTML, time: "昨天 16:21",
        refs: [REFS[2]],
        degraded: ["rerank_skipped"],
        tools: [{ name: "kb_retrieve", status: "ok", elapsed: 342, args: { query: "切片合并 fill ratio", top_k: 20 }, result: "召回 20 条，合并相邻后 5 条" }],
        meta: { model: "deepseek-flash", prompt: 1264, completion: 186, total: 1450, elapsed: 3180, trace: "9a1c2e77d4b84f0c" },
      },
    ],
  },
  {
    id: "c3",
    title: "配额与限流设计",
    preview: "双维度不是重复，是两个不同的绕过面…",
    time: "昨天",
    messages: [
      { id: "c3u", role: "user", content: "配额和限流为什么要双维度？", time: "昨天 11:02" },
      {
        id: "c3a", role: "assistant", html: C3_HTML, time: "昨天 11:03",
        refs: [REFS[0]],
        meta: { model: "deepseek-v4-pro", prompt: 428, completion: 132, total: 560, elapsed: 1620, trace: "e7b1c0a2" },
      },
    ],
  },
  {
    id: "c4",
    title: "M1–M3 周报",
    preview: "本周主线是把「能跑」变成「可验收」…",
    time: "10-24",
    messages: [
      { id: "c4u", role: "user", content: "帮我总结一下这周的进展。", time: "10-24 18:10" },
      {
        id: "c4a", role: "assistant", html: C4_HTML, time: "10-24 18:10",
        refs: [],
        meta: { model: "deepseek-flash", prompt: 620, completion: 148, total: 768, elapsed: 1900, trace: "77aa0c" },
      },
    ],
  },
  {
    id: "c5",
    title: "MCP 工具接入",
    preview: "失败要回注成工具结果，而不是整轮 5xx…",
    time: "10-22",
    messages: [
      { id: "c5u", role: "user", content: "MCP Server 连不上时，前端应该看到什么？", time: "10-22 09:41" },
      {
        id: "c5a", role: "assistant", html: C5_HTML, time: "10-22 09:42",
        refs: [],
        tools: [{ name: "mcp__filesystem__read_file", status: "timeout", elapsed: 5001, args: { path: "/docs/faq.md" }, result: "连接超时，已回注给模型" }],
        meta: { model: "deepseek-flash", prompt: 910, completion: 104, total: 1014, elapsed: 6100, trace: "3c9d1e" },
      },
    ],
  },
];

const kbs = [
  { id: "kb1", name: "售后知识库", desc: "售后政策、退款与工单处理规范", docs: 12, chunks: 486, model: "Qwen3-Embedding-0.6B", active: true },
  { id: "kb2", name: "产品文档", desc: "接口契约、部署手册与版本说明", docs: 8, chunks: 212, model: "Qwen3-Embedding-0.6B", active: false },
];

let docs = [
  { id: "d1", name: "售后政策 v3.pdf", type: "PDF", size: "2.4 MB", status: "INDEXED", chunks: 64, pages: 12, truncated: false },
  { id: "d2", name: "产品定价与套餐.md", type: "MD", size: "18 KB", status: "INDEXED", chunks: 22, pages: 4, truncated: false },
  { id: "d3", name: "部署手册.docx", type: "DOCX", size: "860 KB", status: "PARSING", chunks: 0, pages: 18, progress: 45, truncated: false },
  { id: "d4", name: "旧版 FAQ.txt", type: "TXT", size: "120 KB", status: "FAILED", chunks: 0, pages: 0, error: "UNPROCESSABLE_DOCUMENT", truncated: false },
];

let tasks = [
  { id: "t1", type: "document_ingest", resource: "部署手册.docx", status: "RUNNING", progress: 62, stage: "EMBEDDING", chunks: "31/48", cancelable: true, retryable: false },
  { id: "t2", type: "summary_build", resource: "SSE 流式输出排查", status: "QUEUED", progress: 0, stage: "QUEUED", chunks: "—", cancelable: true, retryable: false },
  { id: "t3", type: "document_delete", resource: "旧版 FAQ.txt", status: "FAILED", progress: 14, stage: "PARSING", chunks: "—", cancelable: false, retryable: true, error: "UNPROCESSABLE_DOCUMENT" },
  { id: "t4", type: "memory_extract", resource: "配额与限流设计", status: "SUCCEEDED", progress: 100, stage: "INDEXED", chunks: "6/6", cancelable: false, retryable: false },
];

let memories = [
  { id: "m1", kind: "preference", content: "偏好中文回答；技术结论先给判断，再给依据。", confidence: 0.92, hits: 14, expires: "2027-01-20" },
  { id: "m2", kind: "fact", content: "项目使用 Go 网关 + Python AI 服务，内部默认 HTTP + SSE，gRPC 只覆盖 Chat。", confidence: 0.88, hits: 9, expires: "长期" },
  { id: "m3", kind: "preference", content: "评审时希望看到可机械验证的数字，而不是「感觉快了」。", confidence: 0.81, hits: 6, expires: "2026-12-31" },
  { id: "m4", kind: "fact", content: "前端技术栈：React + Vite + TanStack Query + Zustand。", confidence: 0.76, hits: 4, expires: "长期" },
];

const quotaMetrics = [
  { key: "chat_requests", name: "对话请求", unit: "次", used: 37, limit: 100, note: "自然日 · 次日 00:00 重置" },
  { key: "llm_tokens", name: "Token 用量", unit: "token", used: 148200, limit: 200000, note: "按 usage.total_tokens 累计" },
  { key: "kb_count", name: "知识库数量", unit: "个", used: 1, limit: 3, note: "存量上限 · 删除释放" },
  { key: "documents_count", name: "文档数量", unit: "个", used: 12, limit: 50, note: "存量上限" },
  { key: "storage_bytes", name: "存储用量", unit: "MB", used: 218, limit: 512, note: "原始文件总量" },
  { key: "concurrency", name: "并发对话", unit: "路", used: 0, limit: 5, note: "瞬时值" },
];

const usageDays = [
  { label: "10-20", requests: 18, tokens: 42000 },
  { label: "10-21", requests: 26, tokens: 68000 },
  { label: "10-22", requests: 14, tokens: 31000 },
  { label: "10-23", requests: 31, tokens: 88000 },
  { label: "10-24", requests: 22, tokens: 54000 },
  { label: "10-25", requests: 9, tokens: 21000 },
  { label: "10-26", requests: 37, tokens: 148200 },
];

const usageRows = [
  { at: "10-26 10:21", endpoint: "chat/stream", model: "deepseek-flash", tokens: 908, elapsed: "1.66s" },
  { at: "10-26 10:20", endpoint: "chat/stream", model: "deepseek-flash", tokens: 1450, elapsed: "3.18s" },
  { at: "10-26 10:18", endpoint: "chat", model: "deepseek-v4-pro", tokens: 2104, elapsed: "6.02s" },
  { at: "10-25 22:04", endpoint: "chat/stream", model: "deepseek-flash", tokens: 760, elapsed: "1.40s" },
];

const BUDGET = {
  total: 16000,
  used: 12480,
  parts: [
    { key: "system", label: "system", tokens: 1180 },
    { key: "memory", label: "长期记忆", tokens: 420 },
    { key: "summary", label: "摘要", tokens: 1860 },
    { key: "history", label: "历史", tokens: 6420 },
    { key: "rag", label: "RAG", tokens: 2480 },
    { key: "query", label: "提问", tokens: 120 },
  ],
  trimmed: { history: 4 },
};

const ANSWER = `先给结论：最小闭环只需要三件事——**一条真实请求**、**一个能取消的流**、**一份可对账的台账**。

### 1. 一条真实请求
从登录态拿到 token，建立会话，再把 \`POST /conversations/{id}/messages/stream\` 发出去。先让浏览器逐字收到第一段正文，再谈路由和状态管理。[1]

### 2. 一个能取消的流
\`EventSource\` 在这里不可用：它不能发 POST，也不能带 \`Authorization\`。用 \`fetch\` + \`ReadableStream\`，把 \`abort\` 传到网关；网关断开后会把已生成内容落成 \`partial\`。[2]

### 3. 一份可对账的台账
流式内容只放临时 state，\`done\` 之后以 \`GET /messages\` 为准做一次对账。这样刷新、断线、切页都不会把正文拼两遍。[3]

先把这条链路跑通，再谈引用角标、工具轨迹和 Markdown 渲染。`;

/* ------------------------------ 状态 ------------------------------ */

const state = {
  view: "chat",
  convId: "c1",
  filter: "",
  draft: "",
  streaming: null,
  toggles: { rag: true, memory: true, tools: false },
  model: "deepseek-flash",
  kbCount: 2,
  railTab: "refs",
  theme: localStorage.getItem("proto.theme") || "dark",
  authMode: "login",
  activeKb: "kb1",
  memoryEnabled: true,
  metric: "requests",
  taskFilter: "all",
  sidebarCollapsed: false,
};

const MODELS = ["deepseek-flash", "deepseek-v4-pro"];

/* ------------------------------ 渲染：侧栏 / 顶栏 ------------------------------ */

function renderSidebar() {
  const list = $("#convList");
  const keyword = state.filter.trim().toLowerCase();
  let visible = conversations.filter((c) => {
    if (c.archived) return false;
    if (!keyword) return true;
    return `${c.title} ${c.preview}`.toLowerCase().includes(keyword);
  });
  visible = visible.slice().sort((a, b) => Number(b.pinned || 0) - Number(a.pinned || 0));

  list.innerHTML = visible.length
    ? visible.map((c) => `
      <div class="conv-item ${c.id === state.convId ? "active" : ""}" data-conv="${c.id}" role="button" tabindex="0">
        <span class="conv-dot"></span>
        <div class="conv-main">
          <div class="title">${c.pinned ? icon("pin") : ""}${escapeHtml(c.title)}</div>
          <div class="preview">${escapeHtml(c.preview)}</div>
        </div>
        <span class="time">${escapeHtml(c.time)}</span>
      </div>`).join("")
    : `<div class="conv-empty">没有匹配的会话</div>`;

  $$("#sidebar .side-nav button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.nav === state.view);
  });
}

function pageMeta() {
  const conv = activeConv();
  switch (state.view) {
    case "chat":
      return {
        title: conv ? conv.title : "新对话",
        sub: conv ? `${conv.messages.length} 条消息 · ${state.model}` : state.model,
      };
    case "knowledge":
      return { title: "知识库", sub: `${kbs.length} 个库 · ${docs.length} 个文档` };
    case "tasks":
      return { title: "任务", sub: `${tasks.filter((t) => ["RUNNING", "QUEUED", "PENDING"].includes(t.status)).length} 个进行中` };
    case "memory":
      return { title: "长期记忆", sub: `${memories.length} 条 · ${state.memoryEnabled ? "已开启" : "已关闭"}` };
    case "quota":
      return { title: "配额与用量", sub: "free · 2026-10-27 00:00 重置" };
    case "settings":
      return { title: "设置", sub: "资料、外观与安全" };
    default:
      return { title: "AI Assistant", sub: "" };
  }
}

function renderTopbar() {
  const meta = pageMeta();
  $("#pageTitle").textContent = meta.title;
  $("#pageSubtitle").textContent = meta.sub;

  const actions = $("#topbarActions");
  let html = "";
  if (state.view === "chat") {
    const conv = activeConv();
    html = `
      <button class="icon-btn" data-action="pin" title="${conv && conv.pinned ? "取消置顶" : "置顶"}">${icon("pin")}</button>
      <button class="icon-btn" data-action="archive" title="归档">${icon("archive")}</button>
      <button class="btn ghost sm" data-action="context">${icon("context")}<span>上下文</span></button>
      <button class="icon-btn" data-action="more" title="更多">${icon("more")}</button>`;
  } else if (state.view === "knowledge") {
    html = `<button class="btn primary sm" data-action="new-kb">${icon("plus")}<span>新建知识库</span></button>`;
  } else if (state.view === "tasks") {
    html = `<button class="btn ghost sm" data-action="refresh-tasks">${icon("refresh")}<span>刷新</span></button>`;
  } else if (state.view === "quota") {
    html = `<button class="btn ghost sm" data-action="export">${icon("download")}<span>导出用量</span></button>`;
  }
  actions.innerHTML = html;
  initIcons(actions);
}

/* ------------------------------ 渲染：对话 ------------------------------ */

function activeConv() {
  return conversations.find((c) => c.id === state.convId) || null;
}

function phaseText(turn) {
  if (turn.phase === "retrieval") return "正在检索资料…";
  if (turn.phase === "tool") return `正在调用 ${turn.tools?.[0]?.name || "工具"}…`;
  return "正在生成…";
}

function formatInline(text) {
  let s = escapeHtml(text);
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\[(\d+)\]/g, '<button class="cite" data-ref="$1">$1</button>');
  return s;
}

function formatRich(text) {
  const lines = String(text).split("\n");
  let html = "";
  let list = null;
  const closeList = () => {
    if (list) {
      html += `</${list}>`;
      list = null;
    }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      closeList();
      continue;
    }
    if (/^###\s+/.test(line)) {
      closeList();
      html += `<h3>${formatInline(line.replace(/^###\s+/, ""))}</h3>`;
    } else if (/^##\s+/.test(line)) {
      closeList();
      html += `<h3>${formatInline(line.replace(/^##\s+/, ""))}</h3>`;
    } else if (/^[-*]\s+/.test(line)) {
      if (list !== "ul") {
        closeList();
        html += "<ul>";
        list = "ul";
      }
      html += `<li>${formatInline(line.replace(/^[-*]\s+/, ""))}</li>`;
    } else if (/^\d+\.\s+/.test(line)) {
      if (list !== "ol") {
        closeList();
        html += "<ol>";
        list = "ol";
      }
      html += `<li>${formatInline(line.replace(/^\d+\.\s+/, ""))}</li>`;
    } else {
      closeList();
      html += `<p>${formatInline(line)}</p>`;
    }
  }
  closeList();
  return html;
}

function renderTools(tools) {
  if (!tools || !tools.length) return "";
  return tools.map((tool) => {
    const status = tool.status || "ok";
    const label = status === "ok" ? "ok" : status;
    const args = tool.args ? JSON.stringify(tool.args, null, 2) : "{}";
    return `
      <details class="tool ${status}" ${status === "running" ? "open" : ""}>
        <summary>
          <span class="status-dot"></span>
          <span class="tool-name">${escapeHtml(tool.name)}</span>
          <span class="chip ${status === "ok" ? "ok" : status === "timeout" ? "warn" : ""}">${label}</span>
          <span class="tool-elapsed">${tool.elapsed ? `${tool.elapsed} ms` : "running"}</span>
          <span class="tool-chevron">${icon("chevron")}</span>
        </summary>
        <div class="tool-body"><b>arguments</b>\n${escapeHtml(args)}\n\n<b>result</b>\n${escapeHtml(tool.result || "…")}</div>
      </details>`;
  }).join("");
}

function renderMeta(msg) {
  if (!msg.meta) {
    return msg.live ? `<span>正在生成</span><span class="sep">·</span><span>${phaseText(msg)}</span>` : "";
  }
  const m = msg.meta;
  const bits = [`${m.model || state.model}`, `${m.prompt ?? 0} → ${m.completion ?? 0} tokens`, `${((m.elapsed || 0) / 1000).toFixed(1)}s`];
  if (m.trace) bits.push(`<code title="trace_id">${escapeHtml(m.trace.slice(0, 8))}…</code>`);
  if (msg.partial || m.partial) bits.unshift('<span class="chip warn">partial · canceled</span>');
  return bits.map((b, i) => `${i ? '<span class="sep">·</span>' : ""}<span>${b}</span>`).join("");
}

function renderMessage(msg) {
  if (msg.role === "user") {
    return `
      <article class="msg user" data-id="${msg.id}">
        <div>
          <div class="bubble">${escapeHtml(msg.content)}</div>
          <div class="msg-actions" style="justify-content:flex-end">
            <button class="icon-btn" data-action="copy" title="复制">${icon("copy")}</button>
            <button class="icon-btn" data-action="resend" title="重新发送">${icon("refresh")}</button>
          </div>
        </div>
      </article>`;
  }

  const refsHtml = msg.refs && msg.refs.length ? `
    <div class="refs-pill" data-action="show-refs">
      <span class="stack">${msg.refs.map((r) => `<span>${r.index}</span>`).join("")}</span>
      <span>引用 ${msg.refs.length} 条资料</span>
    </div>` : "";
  const refs = `<div class="refs-slot">${refsHtml}</div>`;

  const tools = (msg.tools && msg.tools.length) || msg.live
    ? `<div class="tool-slot">${renderTools(msg.tools || [])}</div>`
    : "";
  const degraded = msg.degraded && msg.degraded.length
    ? `<button class="degraded" data-action="degraded" title="${escapeHtml(msg.degraded.join(", "))}">${icon("spark")}检索降级 · ${escapeHtml(msg.degraded[0])}</button>`
    : "";

  const content = msg.live
    ? (msg.content
        ? `${formatRich(msg.content)}<span class="cursor"></span>`
        : `<span class="thinking"><i></i><i></i><i></i></span> <span class="phase">${phaseText(msg)}</span>`)
    : (msg.html || (msg.content ? formatRich(msg.content) : `<p class="muted">（未生成内容）</p>`));

  return `
    <article class="msg assistant ${msg.live ? "live" : ""}" data-id="${msg.id}">
      <div class="msg-avatar">AI</div>
      <div class="msg-body">
        <div class="msg-top">
          <span class="model-badge">${escapeHtml(msg.model || state.model)}</span>
          <span class="msg-time">${escapeHtml(msg.time || "")}</span>
          <span class="spacer"></span>
          ${degraded}
        </div>
        ${tools}
        <div class="md">${content}</div>
        ${refs}
        <div class="msg-meta" data-live-meta>${renderMeta(msg)}</div>
        <div class="msg-actions">
          <button class="icon-btn" data-action="copy" title="复制">${icon("copy")}</button>
          <button class="icon-btn" data-action="regenerate" title="重新生成">${icon("refresh")}</button>
          <button class="icon-btn" data-action="delete-msg" title="删除">${icon("trash")}</button>
        </div>
      </div>
    </article>`;
}

function renderComposer() {
  const streaming = Boolean(state.streaming);
  return `
    <div class="composer">
      <div class="composer-box">
        <textarea id="composerInput" rows="1" placeholder="问点什么，或让我查一下你的资料…">${escapeHtml(state.draft)}</textarea>
        <div class="composer-toolbar">
          <button class="toggle ${state.toggles.rag ? "on" : ""}" data-toggle="rag" title="检索知识库">${icon("book")}<span>资料</span></button>
          <button class="toggle ${state.toggles.memory ? "on" : ""}" data-toggle="memory" title="读写记忆">${icon("brain")}<span>记忆</span></button>
          <button class="toggle ${state.toggles.tools ? "on" : ""}" data-toggle="tools" title="启用工具">${icon("spark")}<span>工具</span></button>
          <button class="toggle" data-action="kb-picker">${icon("file")}<span>知识库</span><span class="count">${state.kbCount}</span></button>
          <div class="composer-right">
            <span class="quota-hint">今日 <b>37/100</b></span>
            <button class="toggle" data-action="cycle-model" title="切换模型">${icon("spark")}<span>${escapeHtml(state.model)}</span></button>
            <button class="send-btn ${streaming ? "stop" : ""}" id="sendBtn" title="${streaming ? "停止生成" : "发送"}">
              ${streaming ? icon("stop") : icon("send")}
            </button>
          </div>
        </div>
      </div>
      <div class="composer-foot">
        <div class="left"><kbd>Enter</kbd> 发送 <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</div>
        <div>${streaming ? "正在生成，可随时停止" : "回答由模型生成，重要信息请核对引用"}</div>
      </div>
    </div>`;
}

function renderChat() {
  const conv = activeConv();
  if (!conv) {
    $("#viewRoot").innerHTML = `<div class="chat-empty"><h2>会话不存在</h2></div>`;
    return;
  }

  const messages = conv.messages.length
    ? conv.messages.map(renderMessage).join("")
    : `<div class="chat-empty">
        <div class="glyph">${icon("spark")}</div>
        <h2>今天想从哪里开始？</h2>
        <p>这是一个静态原型：发送后会模拟流式回答、引用与工具轨迹。数据不会离开浏览器。</p>
        <div class="suggestions">
          <button class="suggestion" data-action="suggest" data-prompt="首帧 35 秒超时是哪一层在兜底？"><b>排查流式超时</b>网关与 Python 各负责哪一段</button>
          <button class="suggestion" data-action="suggest" data-prompt="切片合并填满 chunk_size 有什么代价？"><b>对比切片策略</b>正文完整性与向量化成本</button>
          <button class="suggestion" data-action="suggest" data-prompt="配额和限流为什么要双维度？"><b>解释双维度</b>为什么 IP + 账号缺一不可</button>
        </div>
      </div>`;

  $("#viewRoot").innerHTML = `
    <section class="chat-view">
      <div class="chat-scroll" id="chatScroll">
        <div class="chat-inner" id="messageList">${messages}</div>
      </div>
      ${renderComposer()}
    </section>`;

  bindChat();
  initIcons($("#viewRoot"));
  scrollBottom(true);
}

function bindChat() {
  const input = $("#composerInput");
  const send = $("#sendBtn");
  if (!input || !send) return;

  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  };
  resize();
  input.addEventListener("input", () => {
    state.draft = input.value;
    resize();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (state.streaming) {
        toast("正在生成中，先停止再发送", "warn");
        return;
      }
      sendMessage();
    }
  });
  send.addEventListener("click", () => {
    if (state.streaming) stopStream();
    else sendMessage();
  });
  input.focus();
}

/* ------------------------------ 模拟流式 ------------------------------ */

function updateLiveMd() {
  const s = state.streaming;
  if (!s) return;
  const node = document.querySelector(".msg.live .md");
  if (!node) return;
  const turn = s.turn;
  if (!turn.content) {
    node.innerHTML = `<span class="thinking"><i></i><i></i><i></i></span> <span class="phase">${phaseText(turn)}</span>`;
    return;
  }
  node.innerHTML = `${formatRich(turn.content)}<span class="cursor"></span>`;
  const meta = document.querySelector(".msg.live [data-live-meta]");
  if (meta) {
    const seconds = ((performance.now() - s.started) / 1000).toFixed(1);
    meta.innerHTML = `<span>正在生成</span><span class="sep">·</span><span>${escapeHtml(state.model)}</span><span class="sep">·</span><span>${seconds}s</span>`;
  }
}

function updateLiveSlots() {
  const s = state.streaming;
  if (!s) return;
  const live = document.querySelector(".msg.live");
  if (!live) return;
  const slot = live.querySelector(".tool-slot");
  if (slot) slot.innerHTML = renderTools(s.turn.tools);
  initIcons(live);
}

function updateLiveRefs() {
  const s = state.streaming;
  if (!s) return;
  const slot = document.querySelector(".msg.live .refs-slot");
  if (!slot) return;
  const refs = s.turn.refs || [];
  slot.innerHTML = refs.length ? `
    <div class="refs-pill" data-action="show-refs">
      <span class="stack">${refs.map((r) => `<span>${r.index}</span>`).join("")}</span>
      <span>引用 ${refs.length} 条资料</span>
    </div>` : "";
}

function sendMessage() {
  const conv = activeConv();
  if (!conv || state.streaming) return;
  const text = (state.draft || "").trim();
  if (!text) {
    toast("先写点什么，或者点一个建议问题", "warn");
    return;
  }

  state.draft = "";
  conv.messages.push({ id: uid(), role: "user", content: text, time: nowTime() });
  conv.preview = text.slice(0, 26);
  conv.time = nowTime();

  const turn = {
    id: uid(),
    role: "assistant",
    live: true,
    phase: "retrieval",
    content: "",
    refs: [],
    tools: [],
    meta: null,
    model: state.model,
    time: nowTime(),
  };
  conv.messages.push(turn);

  state.streaming = {
    convId: conv.id,
    turn,
    cancelled: false,
    timer: null,
    started: performance.now(),
  };

  renderSidebar();
  renderTopbar();
  renderChat();
  startMockStream(turn);
}

function startMockStream(turn) {
  const s = state.streaming;
  if (!s) return;

  setTimeout(() => {
    if (!s || s.cancelled) return;
    turn.phase = "tool";
    turn.refs = REFS.slice(0, 3);
    updateLiveMd();
    updateLiveRefs();
    if (state.view === "chat") renderRail();
  }, 520);

  setTimeout(() => {
    if (!s || s.cancelled) return;
    turn.tools = [{
      name: "kb_retrieve",
      status: "running",
      elapsed: null,
      args: { query: "流式最小闭环", top_k: 20, rerank: true },
      result: "检索中…",
    }];
    updateLiveSlots();
    updateLiveMd();
  }, 900);

  setTimeout(() => {
    if (!s || s.cancelled) return;
    turn.tools[0].status = "ok";
    turn.tools[0].elapsed = 342;
    turn.tools[0].result = "召回 20 条，合并相邻后 5 条；rerank 保留 3 条";
    turn.phase = "typing";
    updateLiveSlots();
    updateLiveMd();
    typeAnswer(turn);
  }, 1400);
}

function typeAnswer(turn) {
  const s = state.streaming;
  if (!s) return;
  let index = 0;

  s.timer = setInterval(() => {
    if (!s || s.cancelled) return;
    const step = 3 + Math.floor(Math.random() * 4);
    turn.content += ANSWER.slice(index, index + step);
    index += step;
    updateLiveMd();
    scrollBottom(false);
    if (index >= ANSWER.length) {
      finishStream();
    }
  }, 18);
}

function finishStream() {
  const s = state.streaming;
  if (!s) return;
  clearInterval(s.timer);
  const turn = s.turn;
  const elapsed = Math.round(performance.now() - s.started);
  turn.live = false;
  turn.phase = "done";
  turn.meta = {
    model: state.model,
    prompt: 812,
    completion: 96,
    total: 908,
    elapsed,
    trace: Math.random().toString(16).slice(2, 18),
  };
  state.streaming = null;
  renderChat();
  scrollBottom(true);
}

function stopStream() {
  const s = state.streaming;
  if (!s) return;
  s.cancelled = true;
  clearInterval(s.timer);
  const turn = s.turn;
  turn.live = false;
  turn.partial = true;
  turn.meta = {
    model: state.model,
    prompt: 812,
    completion: Math.max(24, Math.round(turn.content.length / 3)),
    elapsed: Math.round(performance.now() - s.started),
    trace: Math.random().toString(16).slice(2, 18),
    partial: true,
  };
  state.streaming = null;
  renderChat();
  toast("已停止生成；内容保留为 partial", "warn");
}

/* ------------------------------ 右侧栏 ------------------------------ */

function latestRefs() {
  const conv = activeConv();
  if (!conv) return [];
  for (let i = conv.messages.length - 1; i >= 0; i -= 1) {
    const msg = conv.messages[i];
    if (msg.role === "assistant" && msg.refs && msg.refs.length) return msg.refs;
  }
  return [];
}

function renderSourceCard(ref) {
  const meta = [ref.heading, ref.page ? `第 ${ref.page} 页` : null].filter(Boolean).join(" · ");
  return `
    <div class="source-card" data-index="${ref.index}" data-action="open-source">
      <div class="source-head">
        <span class="source-index">${ref.index}</span>
        <div style="min-width:0">
          <div class="source-doc">${escapeHtml(ref.doc)}</div>
          <div class="source-meta">${escapeHtml(meta)}</div>
        </div>
        <span class="source-score">${ref.score.toFixed(2)}</span>
      </div>
      <div class="source-snippet">${escapeHtml(ref.snippet)}</div>
      <div class="score-bar"><i style="width:${Math.round(ref.score * 100)}%"></i></div>
      <div class="source-actions">
        <button data-action="locate">定位原文</button>
        <button data-action="copy-source">复制片段</button>
      </div>
    </div>`;
}

function renderContextRail() {
  const used = BUDGET.used;
  const total = BUDGET.total;
  const pct = Math.round((used / total) * 100);
  return `
    <div class="budget-box">
      <div class="budget-head">
        <strong>上下文预算</strong>
        <span>${used.toLocaleString()} / ${total.toLocaleString()} tokens</span>
      </div>
      <div class="stacked">
        ${BUDGET.parts.map((p, i) => `<i style="width:${(p.tokens / total) * 100}%;background:${REF_COLORS[i % REF_COLORS.length]}"></i>`).join("")}
      </div>
      <div class="budget-rows">
        ${BUDGET.parts.map((p, i) => `
          <div class="budget-row">
            <span class="swatch" style="background:${REF_COLORS[i % REF_COLORS.length]}"></span>
            <span class="name">${escapeHtml(p.label)}</span>
            <span class="val">${p.tokens.toLocaleString()}</span>
          </div>`).join("")}
      </div>
      <div class="budget-note">
        已用 <b>${pct}%</b>；历史被裁掉 <b>${BUDGET.trimmed.history}</b> 条消息。
        <br>装配顺序：system → 记忆 → 摘要 → 历史 → RAG → 提问。
      </div>
    </div>
    <div style="display:flex;gap:7px;margin-top:10px">
      <button class="btn ghost sm" data-action="rebuild-summary" style="flex:1">${icon("refresh")}重建摘要</button>
      <button class="btn ghost sm" data-action="context" style="flex:1">${icon("context")}展开</button>
    </div>`;
}

function renderRail() {
  const rail = $("#rail");
  if (state.view !== "chat") {
    rail.innerHTML = "";
    return;
  }

  const refs = latestRefs();
  rail.innerHTML = `
    <div class="rail-tabs">
      <button class="rail-tab ${state.railTab === "refs" ? "active" : ""}" data-action="rail-refs">资料</button>
      <button class="rail-tab ${state.railTab === "context" ? "active" : ""}" data-action="rail-context">上下文</button>
    </div>
    ${state.railTab === "refs"
      ? `<div class="rail-title"><span>本轮引用</span><span class="count">${refs.length} 条</span></div>
         ${refs.length ? refs.map(renderSourceCard).join("") : `
           <div class="rail-empty">
             <div class="glyph">${icon("book")}</div>
             还没有引用。<br>开启「资料」后提问，命中的切片会出现在这里。
           </div>`}`
      : renderContextRail()}`;
  initIcons(rail);
}

function openReference(index) {
  if (window.innerWidth < 1280) {
    const refs = latestRefs();
    openModal({
      title: "本轮引用",
      subtitle: "点击资料卡可以定位原文（mock）",
      body: refs.length ? refs.map(renderSourceCard).join("") : `<p class="muted">还没有引用。</p>`,
    });
    return;
  }
  state.railTab = "refs";
  renderRail();
  const card = document.querySelector(`.source-card[data-index="${index}"]`);
  if (card) {
    $$(".source-card").forEach((c) => c.classList.remove("highlight"));
    card.classList.add("highlight");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

/* ------------------------------ 知识库 ------------------------------ */

function statusChip(status) {
  const map = {
    INDEXED: ["ok", "INDEXED"],
    PARSING: ["warn", "PARSING"],
    CHUNKING: ["warn", "CHUNKING"],
    EMBEDDING: ["warn", "EMBEDDING"],
    PENDING: ["", "PENDING"],
    FAILED: ["bad", "FAILED"],
  };
  const [kind, label] = map[status] || ["", status];
  return `<span class="chip ${kind}">${label}</span>`;
}

function renderKnowledge() {
  const activeKb = kbs.find((k) => k.id === state.activeKb) || kbs[0];
  $("#viewRoot").innerHTML = `
    <div class="page"><div class="page-inner">
      <div class="kb-grid">
        ${kbs.map((k) => `
          <div class="kb-card ${k.id === state.activeKb ? "active" : ""}" data-action="kb-select" data-id="${k.id}">
            <div class="top">${icon("book")}<span class="name">${escapeHtml(k.name)}</span>
              ${k.id === state.activeKb ? '<span class="chip accent">当前</span>' : ""}
            </div>
            <div class="desc">${escapeHtml(k.desc)}</div>
            <div class="kb-stats"><span><b>${k.docs}</b> 文档</span><span><b>${k.chunks}</b> 切片</span><span class="mono">${escapeHtml(k.model)}</span></div>
          </div>`).join("")}
      </div>

      <div class="panel">
        <div class="panel-head">
          <div><h2>文档 · ${escapeHtml(activeKb.name)}</h2><p>上传后进入异步入库，状态会经历 PARSING → CHUNKING → EMBEDDING → INDEXED。</p></div>
          <div class="right"><button class="btn primary sm" data-action="upload">${icon("upload")}上传文档</button></div>
        </div>
        <div class="dropzone" data-action="upload" id="dropzone">
          <div class="icon">${icon("upload")}</div>
          <div><b>拖拽文件到这里</b>，或点击选择<div class="sub">支持 PDF / MD / DOCX / TXT · 单文件上限 50MB · 也可直接粘贴纯文本</div></div>
        </div>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>名称</th><th>类型</th><th>大小</th><th>状态</th><th>切片</th><th></th></tr></thead>
            <tbody>
              ${docs.map((d) => `
                <tr>
                  <td class="name">${escapeHtml(d.name)}${d.truncated ? ' <span class="chip warn">已截断</span>' : ""}</td>
                  <td>${escapeHtml(d.type)}</td>
                  <td>${escapeHtml(d.size)}</td>
                  <td>${statusChip(d.status)}</td>
                  <td>${d.chunks || "—"}</td>
                  <td><div class="actions">
                    <button class="icon-btn" data-action="doc-chunks" data-id="${d.id}" title="查看切片">${icon("context")}</button>
                    <button class="icon-btn" data-action="doc-delete" data-id="${d.id}" title="删除">${icon("trash")}</button>
                  </div></td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>

      <div class="grid-2">
        <div class="panel">
          <div class="panel-head"><div><h2>检索调试</h2><p>看召回与重排的分差，而不是只看最终答案。</p></div></div>
          <div class="search-panel">
            <div class="search-row">
              <input id="kbQuery" placeholder="输入一个问题，例如「退款多久到账」">
              <button class="btn primary sm" data-action="search-kb">${icon("search")}检索</button>
            </div>
            <div id="searchResults" class="search-panel">
              <div class="search-meta">上次：召回 20 条 · 返回 3 条 · rerank 320ms</div>
              <div class="search-hit">
                <div class="hit-head"><b>售后政策 v3.pdf · 第 4 页</b><span class="scores">vec .81 / rerank .92</span></div>
                自签收之日起 7 个自然日内，商品未拆封可申请无理由退款……
              </div>
              <div class="search-hit">
                <div class="hit-head"><b>售后政策 v3.pdf · §3.2 退款到账</b><span class="scores">vec .77 / rerank .88</span></div>
                审核通过后原路退回，银行卡通常 1–3 个工作日到账。
              </div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><div><h2>入库任务</h2><p>任务与在线请求解耦，进度独立可见。</p></div>
            <button class="btn ghost sm" data-nav="tasks">全部任务</button></div>
          <div class="search-panel">
            ${tasks.filter((t) => t.type === "document_ingest").map((t) => `
              <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px">
                  <span>${escapeHtml(t.resource)}</span><span class="chip ${t.status === "FAILED" ? "bad" : "warn"}">${t.status}</span>
                </div>
                <div class="task-progress"><div class="track"><i style="width:${t.progress}%"></i></div><span>${t.progress}%</span></div>
                <div class="search-meta" style="margin-top:6px">阶段 ${escapeHtml(t.stage)} · 分片 ${escapeHtml(t.chunks)}</div>
              </div>`).join("")}
            <div class="search-meta">失败任务会自动退避重试；也可手动重试。</div>
          </div>
        </div>
      </div>
    </div></div>`;

  bindKnowledge();
  initIcons($("#viewRoot"));
}

function bindKnowledge() {
  const fileInput = $("#fileInput");
  if (fileInput) fileInput.remove();

  const input = document.createElement("input");
  input.type = "file";
  input.id = "fileInput";
  input.className = "hidden";
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (file) simulateUpload(file.name);
    input.value = "";
  });
  $("#viewRoot").append(input);

  const dropzone = $("#dropzone");
  if (dropzone) {
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("drag");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("drag");
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      simulateUpload(file ? file.name : "售后政策-v4.pdf");
    });
  }

  const query = $("#kbQuery");
  if (query) {
    query.addEventListener("keydown", (e) => {
      if (e.key === "Enter") runKbSearch();
    });
  }
}

function simulateUpload(name) {
  const ext = (name.split(".").pop() || "pdf").toUpperCase();
  const doc = {
    id: uid(),
    name,
    type: ext,
    size: "1.8 MB",
    status: "PENDING",
    chunks: 0,
    pages: 0,
    progress: 0,
    truncated: false,
  };
  docs.unshift(doc);
  renderKnowledge();
  toast("已受理；任务已在后台排队", "ok");

  const stages = [["PARSING", 18], ["CHUNKING", 46], ["EMBEDDING", 78], ["INDEXED", 100]];
  let i = 0;
  const timer = setInterval(() => {
    if (i >= stages.length) {
      clearInterval(timer);
      return;
    }
    [doc.status, doc.progress] = stages[i];
    i += 1;
    if (doc.status === "INDEXED") {
      doc.chunks = 48;
      clearInterval(timer);
      toast("入库完成：48 个切片", "ok");
    }
    if (state.view === "knowledge") renderKnowledge();
  }, 850);
}

function runKbSearch() {
  const input = $("#kbQuery");
  if (!input) return;
  const query = input.value.trim();
  if (!query) {
    toast("先输入检索语句", "warn");
    input.focus();
    return;
  }
  const results = $("#searchResults");
  results.innerHTML = `
    <div class="search-meta">查询 <b>${escapeHtml(query)}</b> · 召回 <b>20</b> 条 · 返回 <b>3</b> 条 · rerank <b>${280 + Math.round(Math.random() * 80)}ms</b></div>
    <div class="search-hit">
      <div class="hit-head"><b>售后政策 v3.pdf · 第 4 页</b><span class="scores">vec .81 / rerank .92</span></div>
      自签收之日起 7 个自然日内，商品未拆封可申请无理由退款……
    </div>
    <div class="search-hit">
      <div class="hit-head"><b>售后政策 v3.pdf · §3.2 退款到账</b><span class="scores">vec .77 / rerank .88</span></div>
      审核通过后原路退回，银行卡通常 1–3 个工作日到账。
    </div>
    <div class="search-hit">
      <div class="hit-head"><b>产品定价与套餐.md · §2 退款例外</b><span class="scores">vec .69 / rerank .74</span></div>
      已拆封的定制类商品不支持无理由退款，质量问题除外。
    </div>`;
  initIcons(results);
  toast("检索完成（mock）", "ok");
}

/* ------------------------------ 任务 ------------------------------ */

function renderTasks() {
  const filtered = state.taskFilter === "all"
    ? tasks
    : tasks.filter((t) => t.status === state.taskFilter);

  $("#viewRoot").innerHTML = `
    <div class="page"><div class="page-inner">
      <div class="panel">
        <div class="panel-head">
          <div><h2>任务中心</h2><p>状态机：PENDING → QUEUED → RUNNING → SUCCEEDED / FAILED / CANCELED。</p></div>
        </div>
        <div class="filter-bar">
          ${[
            ["all", "全部"],
            ["RUNNING", "运行中"],
            ["QUEUED", "排队"],
            ["FAILED", "失败"],
            ["SUCCEEDED", "已完成"],
          ].map(([key, label]) => `<button class="filter ${state.taskFilter === key ? "active" : ""}" data-action="task-filter" data-filter="${key}">${label}</button>`).join("")}
        </div>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>类型</th><th>资源</th><th>状态</th><th>进度</th><th>分片</th><th></th></tr></thead>
            <tbody>
              ${filtered.length ? filtered.map((t) => `
                <tr>
                  <td class="mono">${escapeHtml(t.type)}</td>
                  <td class="name">${escapeHtml(t.resource)}</td>
                  <td>${statusChip(t.status)}${t.status === "FAILED" ? `<div class="search-meta" style="margin-top:4px">${escapeHtml(t.error || "")}</div>` : ""}</td>
                  <td>
                    <div class="task-progress"><div class="track"><i style="width:${t.progress}%"></i></div><span>${t.progress}%</span></div>
                    <div class="search-meta" style="margin-top:4px">${escapeHtml(t.stage)}</div>
                  </td>
                  <td>${escapeHtml(t.chunks)}</td>
                  <td><div class="actions">
                    ${t.cancelable ? `<button class="btn ghost sm" data-action="task-cancel" data-id="${t.id}">取消</button>` : ""}
                    ${t.retryable ? `<button class="btn ghost sm" data-action="task-retry" data-id="${t.id}">重试</button>` : ""}
                  </div></td>
                </tr>`).join("") : `<tr><td colspan="6"><div class="rail-empty">没有该状态的任务</div></td></tr>`}
            </tbody>
          </table>
        </div>
      </div>
      <div class="grid-3">
        <div class="panel"><div class="search-meta">运行中</div><div style="font-size:24px;font-weight:650;margin-top:6px">${tasks.filter((t) => t.status === "RUNNING").length}</div></div>
        <div class="panel"><div class="search-meta">排队</div><div style="font-size:24px;font-weight:650;margin-top:6px">${tasks.filter((t) => t.status === "QUEUED").length}</div></div>
        <div class="panel"><div class="search-meta">失败待重试</div><div style="font-size:24px;font-weight:650;margin-top:6px">${tasks.filter((t) => t.status === "FAILED").length}</div></div>
      </div>
    </div></div>`;
  initIcons($("#viewRoot"));
}

function cancelTask(id) {
  const task = tasks.find((t) => t.id === id);
  if (!task) return;
  task.status = "CANCELED";
  task.cancelable = false;
  task.stage = "CANCELED";
  renderTasks();
  toast("已请求取消；运行中的批次会在检查点退出", "warn");
}

function retryTask(id) {
  const task = tasks.find((t) => t.id === id);
  if (!task) return;
  task.status = "QUEUED";
  task.progress = 0;
  task.stage = "QUEUED";
  task.retryable = false;
  task.cancelable = true;
  task.error = null;
  renderTasks();
  toast("已重新入队", "ok");

  setTimeout(() => {
    if (task.status !== "QUEUED") return;
    task.status = "RUNNING";
    task.stage = "EMBEDDING";
    const timer = setInterval(() => {
      if (task.status !== "RUNNING") {
        clearInterval(timer);
        return;
      }
      task.progress = Math.min(100, task.progress + 7);
      if (task.progress >= 100) {
        task.status = "SUCCEEDED";
        task.stage = "INDEXED";
        task.cancelable = false;
        clearInterval(timer);
        toast("任务完成", "ok");
      }
      if (state.view === "tasks") renderTasks();
    }, 420);
  }, 700);
}

/* ------------------------------ 记忆 ------------------------------ */

function renderMemory() {
  $("#viewRoot").innerHTML = `
    <div class="page"><div class="page-inner">
      <div class="panel">
        <div class="panel-head">
          <div><h2>记忆设置</h2><p>关闭后本轮不读写短期上下文与长期记忆。</p></div>
          <label class="switch" title="记忆开关">
            <input type="checkbox" id="memoryEnabled" ${state.memoryEnabled ? "checked" : ""}>
            <span></span>
          </label>
        </div>
        <div class="grid-3">
          <div class="search-meta">注入条数上限<br><b style="font-size:16px">3</b></div>
          <div class="search-meta">上次清空<br><b style="font-size:12px">2026-10-20 14:02</b></div>
          <div class="search-meta">去重阈值<br><b style="font-size:12px">0.85 / 0.92</b></div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <div><h2>长期记忆</h2><p>偏好与事实分开存；写入前做哈希 + 语义双层去重。</p></div>
          <button class="btn ghost sm danger" data-action="memory-clear">${icon("trash")}清空</button>
        </div>
        <div class="memory-grid">
          ${memories.map((m) => `
            <div class="memory-card">
              <div class="top">
                <span class="chip ${m.kind === "preference" ? "accent" : ""}">${m.kind === "preference" ? "偏好" : "事实"}</span>
                <span class="search-meta" style="margin-left:auto">命中 ${m.hits} 次</span>
              </div>
              <div class="content">${escapeHtml(m.content)}</div>
              <div class="foot">
                <span>置信 ${m.confidence.toFixed(2)}</span>
                <div class="bar"><i style="width:${Math.round(m.confidence * 100)}%"></i></div>
                <span>${escapeHtml(m.expires)}</span>
                <button class="icon-btn" data-action="memory-delete" data-id="${m.id}" title="删除">${icon("trash")}</button>
              </div>
            </div>`).join("")}
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <input id="memoryInput" placeholder="添加一条记忆，例如「回答尽量给表格」" style="flex:1;min-width:220px;height:34px;padding:0 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg-2);color:var(--text);outline:none;font-size:12.5px">
          <select id="memoryKind" style="height:34px;padding:0 8px;border:1px solid var(--border);border-radius:8px;background:var(--bg-2);color:var(--text);font-size:12px">
            <option value="preference">偏好</option>
            <option value="fact">事实</option>
          </select>
          <button class="btn primary sm" data-action="memory-add">${icon("plus")}添加</button>
        </div>
      </div>
    </div></div>`;

  const toggle = $("#memoryEnabled");
  if (toggle) {
    toggle.addEventListener("change", () => {
      state.memoryEnabled = toggle.checked;
      renderTopbar();
      toast(toggle.checked ? "记忆已开启" : "记忆已关闭", toggle.checked ? "ok" : "warn");
    });
  }
  initIcons($("#viewRoot"));
}

function addMemory() {
  const input = $("#memoryInput");
  const kind = $("#memoryKind");
  if (!input) return;
  const content = input.value.trim();
  if (!content) {
    toast("先写一条记忆", "warn");
    input.focus();
    return;
  }
  memories.unshift({
    id: uid(),
    kind: kind ? kind.value : "fact",
    content,
    confidence: 0.7,
    hits: 1,
    expires: "2027-01-20",
  });
  renderMemory();
  renderTopbar();
  toast("已写入长期记忆（mock）", "ok");
}

/* ------------------------------ 配额 ------------------------------ */

function compactNumber(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(value);
}

function renderQuota() {
  const max = Math.max(...usageDays.map((d) => (state.metric === "requests" ? d.requests : d.tokens)));
  $("#viewRoot").innerHTML = `
    <div class="page"><div class="page-inner">
      <div class="grid-3">
        <div class="panel"><div class="search-meta">当前套餐</div><div style="font-size:20px;font-weight:650;margin-top:6px">Free</div><div class="search-meta" style="margin-top:4px">对话 100 次/天 · 200k tokens/天</div></div>
        <div class="panel"><div class="search-meta">今日对话</div><div style="font-size:24px;font-weight:650;margin-top:6px">37 <span style="font-size:12px;color:var(--muted)">/ 100</span></div><div class="meter" style="margin-top:8px"><i style="width:37%"></i></div></div>
        <div class="panel"><div class="search-meta">今日 tokens</div><div style="font-size:24px;font-weight:650;margin-top:6px">148k <span style="font-size:12px;color:var(--muted)">/ 200k</span></div><div class="meter" style="margin-top:8px"><i class="warn" style="width:74%"></i></div></div>
      </div>

      <div class="panel">
        <div class="panel-head"><div><h2>配额维度</h2><p>存量型指标删除资源后释放；日指标次日 00:00 重置。</p></div></div>
        ${quotaMetrics.map((m) => {
          const pct = Math.min(100, Math.round((m.used / m.limit) * 100));
          const kind = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
          return `<div class="metric-row">
            <div class="metric-name">${escapeHtml(m.name)}<small>${escapeHtml(m.note)}</small></div>
            <div class="meter"><i class="${kind}" style="width:${pct}%"></i></div>
            <div class="metric-val" title="${m.used.toLocaleString()} / ${m.limit.toLocaleString()} ${m.unit}"><b>${compactNumber(m.used)}</b> / ${compactNumber(m.limit)} ${m.unit}</div>
          </div>`;
        }).join("")}
      </div>

      <div class="grid-2">
        <div class="panel">
          <div class="panel-head">
            <div><h2>近 7 天</h2><p>切换指标看趋势；柱形为每日累计。</p></div>
            <div class="right">
              <button class="filter ${state.metric === "requests" ? "active" : ""}" data-action="metric" data-metric="requests">对话次数</button>
              <button class="filter ${state.metric === "tokens" ? "active" : ""}" data-action="metric" data-metric="tokens">tokens</button>
            </div>
          </div>
          <div class="chart">
            ${usageDays.map((d) => {
              const value = state.metric === "requests" ? d.requests : d.tokens;
              return `<div class="bar-wrap" title="${d.label}: ${value}">
                <div class="bar" style="height:${Math.max(4, (value / max) * 100)}%"></div>
                <div class="bar-label">${d.label}</div>
              </div>`;
            }).join("")}
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><div><h2>最近调用</h2><p>用量由网关按 usage 累加。</p></div></div>
          <div class="table-wrap">
            <table class="data-table">
              <thead><tr><th>时间</th><th>接口</th><th>模型</th><th>tokens</th></tr></thead>
              <tbody>
                ${usageRows.map((r) => `<tr><td>${r.at}</td><td class="mono">${r.endpoint}</td><td>${r.model}</td><td>${r.tokens}</td></tr>`).join("")}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div></div>`;
  initIcons($("#viewRoot"));
}

/* ------------------------------ 设置 ------------------------------ */

function renderSettings() {
  $("#viewRoot").innerHTML = `
    <div class="page"><div class="page-inner">
      <div class="settings-grid">
        <div class="panel settings-section">
          <h3>个人资料</h3>
          <p>昵称会显示在侧栏与会话参与者里。</p>
          <label class="field"><span>昵称</span><input id="profileName" value="阿泽"></label>
          <label class="field"><span>邮箱</span><input id="profileEmail" value="demo@ai-assistant.dev" disabled></label>
          <div><button class="btn primary sm" data-action="save-profile">保存资料</button></div>
        </div>

        <div class="panel settings-section">
          <h3>外观</h3>
          <p>暗色为默认；亮色用于演示设计令牌的切换。</p>
          <div class="theme-options">
            <button class="theme-option ${state.theme === "dark" ? "active" : ""}" data-action="theme" data-theme="dark">${icon("moon")}暗色</button>
            <button class="theme-option ${state.theme === "light" ? "active" : ""}" data-action="theme" data-theme="light">${icon("sun")}亮色</button>
          </div>
          <div class="search-meta">当前主题：<b>${state.theme === "dark" ? "暗色" : "亮色"}</b> · 仅前端本地生效</div>
        </div>

        <div class="panel settings-section">
          <h3>修改密码</h3>
          <p>修改成功后，旧 access token 会按版本号失效。</p>
          <label class="field"><span>旧密码</span><input type="password" placeholder="••••••••"></label>
          <label class="field"><span>新密码</span><input type="password" placeholder="至少 8 位"></label>
          <div><button class="btn primary sm" data-action="save-password">更新密码</button></div>
        </div>

        <div class="panel settings-section danger-zone">
          <h3>会话与安全</h3>
          <p>退出所有设备会作废全部 refresh token，并把 token 版本 +1。</p>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn danger sm" data-action="logout-all">${icon("lock")}退出所有设备</button>
            <button class="btn ghost sm" data-action="logout">${icon("x")}退出登录</button>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head"><div><h2>关于这个原型</h2><p>所有数据在内存里，刷新即重置；不发送网络请求。</p></div></div>
        <div class="kv"><span>设计令牌</span><b>OKLCH-like CSS vars · dark / light</b></div>
        <div class="kv"><span>覆盖页面</span><b>对话 · 知识库 · 任务 · 记忆 · 配额 · 设置</b></div>
        <div class="kv"><span>真实接口契约</span><b>/api/v1 · 见 web/docs/01</b></div>
      </div>
    </div></div>`;
  initIcons($("#viewRoot"));
}

/* ------------------------------ 视图切换 ------------------------------ */

function renderView() {
  const app = $("#app");
  if (state.view === "chat") {
    app.classList.remove("rail-off");
    renderChat();
    renderRail();
  } else {
    app.classList.add("rail-off");
    $("#rail").innerHTML = "";
    if (state.view === "knowledge") renderKnowledge();
    else if (state.view === "tasks") renderTasks();
    else if (state.view === "memory") renderMemory();
    else if (state.view === "quota") renderQuota();
    else if (state.view === "settings") renderSettings();
  }
}

function renderAll() {
  document.documentElement.dataset.theme = state.theme;
  renderSidebar();
  renderTopbar();
  renderView();
  initIcons(document);
}

function setView(view) {
  if (state.streaming) stopStream();
  state.view = view;
  $("#app").classList.remove("sidebar-open");
  renderAll();
}

function openConversation(id) {
  if (state.streaming) stopStream();
  const conv = conversations.find((c) => c.id === id);
  if (!conv) return;
  state.convId = id;
  state.view = "chat";
  state.draft = "";
  state.railTab = "refs";
  $("#app").classList.remove("sidebar-open");
  renderAll();
  scrollBottom(true);
}

function newConversation() {
  if (state.streaming) stopStream();
  const current = activeConv();
  if (current && current.messages.length === 0) {
    $("#composerInput")?.focus();
    return;
  }
  const conv = {
    id: uid(),
    title: "新对话",
    preview: "还没有消息",
    time: "刚刚",
    messages: [],
  };
  conversations.unshift(conv);
  state.convId = conv.id;
  state.view = "chat";
  state.draft = "";
  renderAll();
  $("#composerInput")?.focus();
}

/* ------------------------------ 弹窗 ------------------------------ */

function openModal({ title, subtitle = "", body, footer = "" }) {
  const root = $("#modalRoot");
  root.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <div><h2>${escapeHtml(title)}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div>
        <button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon("x")}</button>
      </div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ""}
    </div>`;
  root.classList.remove("hidden");
  initIcons(root);
  root.querySelector(".modal").addEventListener("click", (e) => e.stopPropagation());
  root.onclick = (e) => {
    if (e.target === root) closeModal();
  };
}

function closeModal() {
  $("#modalRoot").classList.add("hidden");
  $("#modalRoot").innerHTML = "";
}

function openContextModal() {
  openModal({
    title: "上下文透视",
    subtitle: "模型这一轮到底看到了什么，哪些被裁掉。",
    body: `
      <div class="budget-box">
        <div class="budget-head"><strong>装配预算</strong><span>${BUDGET.used.toLocaleString()} / ${BUDGET.total.toLocaleString()}</span></div>
        <div class="stacked">${BUDGET.parts.map((p, i) => `<i style="width:${(p.tokens / BUDGET.total) * 100}%;background:${REF_COLORS[i % REF_COLORS.length]}"></i>`).join("")}</div>
        <div class="budget-rows">
          ${BUDGET.parts.map((p, i) => `<div class="budget-row"><span class="swatch" style="background:${REF_COLORS[i % REF_COLORS.length]}"></span><span class="name">${escapeHtml(p.label)}</span><span class="val">${p.tokens.toLocaleString()}</span></div>`).join("")}
        </div>
        <div class="budget-note">历史被裁掉 <b>${BUDGET.trimmed.history}</b> 条；摘要覆盖到第 18 条消息。<br>顺序固定：system → 记忆 → 摘要 → 历史 → RAG → 提问。</div>
      </div>
      <div class="kv"><span>摘要</span><b>存在 · 1,860 tokens</b></div>
      <div class="kv"><span>长期记忆</span><b>3 条 · 420 tokens</b></div>
      <div class="kv"><span>RAG</span><b>3 条引用 · 2,480 tokens</b></div>`,
    footer: `<button class="btn ghost sm" data-action="close-modal">关闭</button><button class="btn primary sm" data-action="rebuild-summary">重建摘要</button>`,
  });
}

function openChunksModal(docId) {
  const doc = docs.find((d) => d.id === docId);
  if (!doc) return;
  openModal({
    title: `切片 · ${doc.name}`,
    subtitle: "切分参数与命中上下文；只有 INDEXED 才可查看。",
    body: `
      <div class="kv"><span>切片数</span><b>${doc.chunks || "—"}</b></div>
      <div class="kv"><span>页数</span><b>${doc.pages || "—"}</b></div>
      <div class="kv"><span>切分</span><b>chunk_size 512 · overlap 64</b></div>
      <div class="chunk"><div class="chunk-head"><span>#0 · page 1</span><span>token 486</span></div>自签收之日起 7 个自然日内，商品未拆封可申请无理由退款。</div>
      <div class="chunk"><div class="chunk-head"><span>#1 · page 1</span><span>token 502</span></div>退款审核通过后原路退回；银行卡通常 1–3 个工作日到账。</div>
      <div class="chunk"><div class="chunk-head"><span>#2 · page 2</span><span>token 441</span></div>定制类商品不支持无理由退款，质量问题按三包处理。</div>`,
    footer: `<button class="btn ghost sm" data-action="close-modal">关闭</button>`,
  });
}

/* ------------------------------ 动作分发 ------------------------------ */

function handleAction(action, el) {
  switch (action) {
    case "new-chat":
      newConversation();
      break;
    case "pin": {
      const conv = activeConv();
      if (!conv) break;
      conv.pinned = !conv.pinned;
      toast(conv.pinned ? "已置顶" : "已取消置顶", "ok");
      renderAll();
      break;
    }
    case "archive": {
      const conv = activeConv();
      if (!conv) break;
      conv.archived = true;
      let next = conversations.find((c) => !c.archived && c.id !== conv.id);
      if (!next) {
        next = { id: uid(), title: "新对话", preview: "还没有消息", time: "刚刚", messages: [] };
        conversations.unshift(next);
      }
      state.convId = next.id;
      state.view = "chat";
      toast("已归档该会话", "ok");
      renderAll();
      break;
    }
    case "context":
      openContextModal();
      break;
    case "more":
      toast("更多操作：导出、分享、删除（原型未实现）");
      break;
    case "copy": {
      const msgEl = el.closest(".msg");
      const msg = findMessage(msgEl?.dataset.id);
      if (msg) copyText(messageText(msg));
      break;
    }
    case "resend": {
      const msgEl = el.closest(".msg");
      const msg = findMessage(msgEl?.dataset.id);
      if (msg && msg.content) {
        state.draft = msg.content;
        sendMessage();
      }
      break;
    }
    case "regenerate":
      toast("原型的「重新生成」只做提示：真实接口会复用原 user 消息，不新增配额之外的记录", "warn");
      break;
    case "delete-msg": {
      const msgEl = el.closest(".msg");
      const conv = activeConv();
      if (!conv || !msgEl) break;
      conv.messages = conv.messages.filter((m) => m.id !== msgEl.dataset.id);
      renderChat();
      toast("已删除（mock）", "ok");
      break;
    }
    case "show-refs":
      if (window.innerWidth < 1280) {
        openReference(1);
      } else {
        state.railTab = "refs";
        renderRail();
      }
      break;
    case "rail-refs":
      state.railTab = "refs";
      renderRail();
      break;
    case "rail-context":
      state.railTab = "context";
      renderRail();
      break;
    case "open-source":
      openReference(Number(el.dataset.index || 1));
      toast("定位到原文（mock）", "ok");
      break;
    case "copy-source": {
      const card = el.closest(".source-card");
      const ref = REFS.find((r) => r.index === Number(card?.dataset.index));
      if (ref) copyText(ref.snippet);
      break;
    }
    case "locate":
      toast("打开原文第 4 页（mock）", "ok");
      break;
    case "degraded":
      toast("降级原因：rerank_skipped；回答仍返回，References 为未重排结果", "warn");
      break;
    case "rebuild-summary":
      toast("摘要重建任务已受理（mock）", "ok");
      break;
    case "suggest": {
      const prompt = el.dataset.prompt || "";
      state.draft = prompt;
      sendMessage();
      break;
    }
    case "cycle-model": {
      const idx = MODELS.indexOf(state.model);
      state.model = MODELS[(idx + 1) % MODELS.length];
      renderChat();
      toast(`已切换到 ${state.model}`);
      break;
    }
    case "kb-picker":
      openKbPicker();
      break;
    case "kb-apply": {
      const checked = $$("#kbPicker input:checked").map((i) => i.value);
      state.kbCount = checked.length;
      closeModal();
      renderChat();
      toast(`已选择 ${checked.length} 个知识库`, "ok");
      break;
    }
    case "new-kb":
      toast("新建知识库（mock）");
      break;
    case "upload":
      $("#fileInput")?.click();
      break;
    case "doc-chunks":
      openChunksModal(el.dataset.id);
      break;
    case "doc-delete":
      docs = docs.filter((d) => d.id !== el.dataset.id);
      renderKnowledge();
      toast("已删除文档（mock）", "ok");
      break;
    case "kb-select":
      state.activeKb = el.dataset.id;
      renderKnowledge();
      break;
    case "search-kb":
      runKbSearch();
      break;
    case "task-filter":
      state.taskFilter = el.dataset.filter;
      renderTasks();
      break;
    case "task-cancel":
      cancelTask(el.dataset.id);
      break;
    case "task-retry":
      retryTask(el.dataset.id);
      break;
    case "refresh-tasks":
      renderTasks();
      toast("已刷新", "ok");
      break;
    case "memory-delete":
      memories = memories.filter((m) => m.id !== el.dataset.id);
      renderMemory();
      renderTopbar();
      toast("已删除记忆", "ok");
      break;
    case "memory-clear":
      openModal({
        title: "清空长期记忆？",
        subtitle: "这是不可撤销的操作；真实接口要求显式 all=true。",
        body: `<p class="muted" style="font-size:12.5px;line-height:1.7">清空后，模型不会再引用已抽取的偏好与事实。摘要与短期上下文不受影响。</p>`,
        footer: `<button class="btn ghost sm" data-action="close-modal">取消</button><button class="btn danger sm" data-action="memory-clear-confirm">确认清空</button>`,
      });
      break;
    case "memory-clear-confirm":
      memories = [];
      closeModal();
      renderMemory();
      renderTopbar();
      toast("已清空长期记忆", "warn");
      break;
    case "memory-add":
      addMemory();
      break;
    case "metric":
      state.metric = el.dataset.metric;
      renderQuota();
      break;
    case "export":
      toast("已生成用量 CSV（mock）", "ok");
      break;
    case "theme":
      state.theme = el.dataset.theme;
      localStorage.setItem("proto.theme", state.theme);
      renderAll();
      break;
    case "save-profile":
      toast("资料已保存（mock）", "ok");
      break;
    case "save-password":
      toast("密码已更新；旧 token 会因版本号失效（mock）", "ok");
      break;
    case "logout-all":
      toast("已退出所有设备（mock）", "ok");
      setTimeout(logout, 500);
      break;
    case "logout":
      logout();
      break;
    case "close-modal":
      closeModal();
      break;
    default:
      break;
  }
}

function findMessage(id) {
  const conv = activeConv();
  return conv ? conv.messages.find((m) => m.id === id) : null;
}

function messageText(msg) {
  if (msg.role === "user") return msg.content;
  if (!msg.html) return msg.content || "";
  const div = document.createElement("div");
  div.innerHTML = msg.html;
  return div.textContent || "";
}

function openKbPicker() {
  openModal({
    title: "选择知识库",
    subtitle: "本轮只在这些库里检索；取消勾选则只靠模型自身知识。",
    body: `
      <div id="kbPicker" style="display:flex;flex-direction:column;gap:8px">
        ${kbs.map((k) => `
          <label style="display:flex;align-items:center;gap:9px;padding:10px;border:1px solid var(--border);border-radius:9px;background:var(--surface-2);font-size:12.5px;cursor:pointer">
            <input type="checkbox" value="${k.id}" ${k.id === "kb1" || k.id === "kb2" ? "checked" : ""}>
            <span style="flex:1"><b style="display:block;font-size:12.5px">${escapeHtml(k.name)}</b><span class="search-meta">${k.docs} 文档 · ${k.chunks} 切片</span></span>
          </label>`).join("")}
      </div>`,
    footer: `<button class="btn ghost sm" data-action="close-modal">取消</button><button class="btn primary sm" data-action="kb-apply">应用</button>`,
  });
}

/* ------------------------------ 登录 / 主题 ------------------------------ */

function enterApp() {
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  document.documentElement.dataset.theme = state.theme;
  renderAll();
  setTimeout(() => $("#composerInput")?.focus(), 50);
}

function logout() {
  if (state.streaming) stopStream();
  $("#app").classList.add("hidden");
  $("#auth").classList.remove("hidden");
  $("#userMenu").classList.add("hidden");
  window.scrollTo(0, 0);
}

function bindAuth() {
  $("#authForm").addEventListener("submit", (e) => {
    e.preventDefault();
    enterApp();
  });
  $("#demoEnter").addEventListener("click", enterApp);
  $("#tabLogin").addEventListener("click", () => setAuthMode("login"));
  $("#tabRegister").addEventListener("click", () => setAuthMode("register"));
}

function setAuthMode(mode) {
  state.authMode = mode;
  const isLogin = mode === "login";
  $("#tabLogin").classList.toggle("active", isLogin);
  $("#tabRegister").classList.toggle("active", !isLogin);
  $("#authTitle").textContent = isLogin ? "欢迎回来" : "创建演示账号";
  $("#authSub").textContent = isLogin
    ? "演示原型：任意邮箱与密码都能进入。"
    : "注册流程也只在浏览器内模拟，不会发送请求。";
  $("#authSubmit").textContent = isLogin ? "登录并进入" : "注册并进入";
  $("#nicknameField").classList.toggle("hidden", isLogin);
}

/* ------------------------------ 全局绑定 ------------------------------ */

function bindGlobal() {
  document.addEventListener("click", (e) => {
    const actionEl = e.target.closest("[data-action]");
    if (actionEl) {
      if (actionEl.tagName === "BUTTON" || actionEl.dataset.action) {
        e.preventDefault();
      }
      handleAction(actionEl.dataset.action, actionEl);
      return;
    }

    const toggleEl = e.target.closest("[data-toggle]");
    if (toggleEl) {
      const key = toggleEl.dataset.toggle;
      state.toggles[key] = !state.toggles[key];
      toggleEl.classList.toggle("on", state.toggles[key]);
      toast(`${key === "rag" ? "资料检索" : key === "memory" ? "记忆" : "工具"}${state.toggles[key] ? "已开启" : "已关闭"}`);
      return;
    }

    const citeEl = e.target.closest(".cite");
    if (citeEl) {
      e.preventDefault();
      openReference(Number(citeEl.dataset.ref));
      return;
    }

    const convEl = e.target.closest("[data-conv]");
    if (convEl) {
      openConversation(convEl.dataset.conv);
      return;
    }

    const navEl = e.target.closest("[data-nav]");
    if (navEl) {
      setView(navEl.dataset.nav);
      return;
    }

    if (!e.target.closest("#userCard") && !e.target.closest("#userMenu")) {
      $("#userMenu").classList.add("hidden");
    }
    if (e.target.id === "overlay") {
      $("#app").classList.remove("sidebar-open");
    }
  });

  $("#newChat").addEventListener("click", newConversation);
  $("#convSearch").addEventListener("input", (e) => {
    state.filter = e.target.value;
    renderSidebar();
  });
  $("#collapseBtn").addEventListener("click", () => {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    $("#app").classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
  });
  $("#menuBtn").addEventListener("click", () => $("#app").classList.toggle("sidebar-open"));
  $("#overlay").addEventListener("click", () => $("#app").classList.remove("sidebar-open"));
  $("#userCard").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#userMenu").classList.toggle("hidden");
  });
  $("#menuTheme").addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    localStorage.setItem("proto.theme", state.theme);
    document.documentElement.dataset.theme = state.theme;
    $("#userMenu").classList.add("hidden");
    if (state.view === "settings") renderSettings();
  });
  $("#menuLogout").addEventListener("click", logout);

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      $("#convSearch")?.focus();
    }
    if (e.key === "Escape") {
      if (!$("#modalRoot").classList.contains("hidden")) closeModal();
      $("#userMenu").classList.add("hidden");
      $("#app").classList.remove("sidebar-open");
    }
  });
}

/* ------------------------------ 启动 ------------------------------ */

function init() {
  initIcons(document);
  document.documentElement.dataset.theme = state.theme;
  bindAuth();
  bindGlobal();
  setAuthMode("login");

  // 方便直接看某个页面：?demo=1&view=knowledge
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  if (view && ["chat", "knowledge", "tasks", "memory", "quota", "settings"].includes(view)) {
    state.view = view;
  }
  const theme = params.get("theme");
  if (theme === "light" || theme === "dark") state.theme = theme;
  if (params.get("demo") === "1") enterApp();

  if (params.get("ask") === "1") {
    setTimeout(() => {
      state.draft = "怎么评估一个 AI 助手前端的第一版范围？";
      sendMessage();
    }, 350);
  }
}

document.addEventListener("DOMContentLoaded", init);
