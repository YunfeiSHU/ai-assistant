import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const SHOT_MEMORY = fileURLToPath(new URL("../previews/f5-memory.png", import.meta.url));
const SHOT_QUOTA = fileURLToPath(new URL("../previews/f5-quota.png", import.meta.url));
const SHOT_CONTEXT = fileURLToPath(new URL("../previews/f5-context.png", import.meta.url));

const email = `f5+${Date.now()}@example.com`;
const password = "F5!Passw0rd2026";
const newPassword = "F5New!Passw0rd2026";
const kbName = "F5 知识库";
const conversationTitle = "F5 上下文会话";

async function api(path, { method = "GET", body, token } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${GATEWAY_URL}/api/v1${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(`${method} ${path} -> ${response.status} ${JSON.stringify(data)}`);
  return data;
}

async function main() {
  const health = await fetch(`${GATEWAY_URL}/api/v1/health`).catch(() => null);
  if (!health?.ok) throw new Error(`网关不可达：${GATEWAY_URL}/api/v1/health`);

  await api("/auth/register", {
    method: "POST",
    body: { email, password, nickname: "F5 验收" },
  });
  const login = await api("/auth/login", {
    method: "POST",
    body: { email, password, device_name: "f5-e2e" },
  });
  const token = login.access_token;
  if (!token) throw new Error("登录响应缺少 access token");

  const kb = await api("/knowledge-bases", {
    method: "POST",
    token,
    body: { name: kbName, description: "F5 KB 关联验收" },
  });
  const conversation = await api("/conversations", {
    method: "POST",
    token,
    body: { title: conversationTitle },
  });
  await api(`/conversations/${conversation.id}/messages`, {
    method: "POST",
    token,
    body: {
      content: "请用一句话说明你能做什么。",
      use_rag: false,
      use_memory: true,
      use_tools: false,
    },
  });

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });

  try {
    await page.goto(`${WEB_URL}/login`, { waitUntil: "domcontentloaded" });
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-password").fill(password);
    await page.getByTestId("auth-submit").click();
    await page.waitForURL("**/chat", { timeout: 15_000 });

    // 长期记忆：新增、编辑、筛选、关闭/开启、清空。
    await page.locator("nav.side-nav button").filter({ hasText: "记忆" }).click();
    await page.waitForURL("**/memory", { timeout: 10_000 });
    await page.getByTestId("memory-add").click();
    await page.getByTestId("memory-content").fill("用户偏好：回答先给结论，再给三条步骤。");
    await page.getByTestId("memory-kind").selectOption("preference");
    await page.getByTestId("memory-save").click();
    await page.getByTestId("memory-item").first().waitFor({ timeout: 15_000 });

    // 对话会抽取事实记忆；切到“偏好”只编辑手动添加的那条。
    await page.getByTestId("memory-filter").filter({ hasText: "偏好" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="memory-item"]').length >= 1,
      undefined,
      { timeout: 15_000 },
    );

    await page.getByTestId("memory-edit").first().click();
    await page.getByTestId("memory-content").fill("用户偏好：回答先给结论，再给三条可执行步骤。");
    await page.getByTestId("memory-save").click();
    await page.waitForFunction(
      () => document.querySelector('[data-testid="memory-item"]')?.textContent?.includes("三条可执行步骤"),
      undefined,
      { timeout: 15_000 },
    );

    await page.getByTestId("memory-enabled").uncheck();
    await page.getByTestId("memory-settings-save").click();
    await page.getByTestId("memory-disabled-banner").waitFor({ timeout: 10_000 });
    await page.getByTestId("memory-enabled").check();
    await page.getByTestId("memory-settings-save").click();
    await page.getByTestId("memory-disabled-banner").waitFor({ state: "detached", timeout: 10_000 });

    await page.screenshot({ path: SHOT_MEMORY, fullPage: false });
    await page.getByTestId("memory-clear").click();
    await page.getByTestId("delete-confirm").click();
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="memory-item"]').length === 0,
      undefined,
      { timeout: 15_000 },
    );

    // 配额与用量。
    await page.locator("nav.side-nav button").filter({ hasText: "配额" }).click();
    await page.waitForURL("**/quota", { timeout: 10_000 });
    await page.getByTestId("quota-metric").first().waitFor({ timeout: 15_000 });
    await page.getByTestId("usage-row").first().waitFor({ timeout: 15_000 });
    const quotaText = await page.locator(".quota-grid").innerText();
    if (!quotaText.includes("对话请求")) throw new Error("配额里没有 chat_requests");
    await page.screenshot({ path: SHOT_QUOTA, fullPage: false });

    // 会话：KB 关联 + 上下文透视。
    await page.getByTestId("conv-item").filter({ hasText: conversationTitle }).click();
    await page.getByTestId("conversation-title").filter({ hasText: conversationTitle }).waitFor();

    await page.getByTestId("kb-select").click();
    await page.getByTestId("kb-option").filter({ hasText: kbName }).click();
    await page.getByTestId("kb-select-save").click();
    await page.getByTestId("kb-select").filter({ hasText: "知识库 1" }).waitFor({ timeout: 15_000 });

    await page.getByTestId("context-open").click();
    await page.getByText("Token 预算").waitFor({ timeout: 15_000 });
    await page.screenshot({ path: SHOT_CONTEXT, fullPage: false });
    await page.getByTestId("context-clear").click();
    await page.getByTestId("context-clear-confirm").click();
    await page.getByText("上下文已清空").waitFor({ timeout: 10_000 });
    await page.keyboard.press("Escape");

    // 设置：主题 + 改密后重新登录。
    await page.locator('.topbar button[aria-label="设置"]').click();
    await page.waitForURL("**/settings", { timeout: 10_000 });
    await page.getByTestId("theme-light").click();
    await page.waitForFunction(
      () => document.documentElement.dataset.theme === "light",
      undefined,
      { timeout: 10_000 },
    );
    await page.getByTestId("theme-dark").click();

    await page.getByTestId("old-password").fill(password);
    await page.getByTestId("new-password").fill(newPassword);
    await page.getByTestId("confirm-password").fill(newPassword);
    await page.getByTestId("change-password").click();
    await page.waitForURL("**/login", { timeout: 20_000 });
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-password").fill(newPassword);
    await page.getByTestId("auth-submit").click();
    await page.waitForURL("**/chat", { timeout: 20_000 });

    if (errors.length) throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);

    console.log("PASS  F5 记忆 CRUD/设置/清空 → 配额/用量 → KB 关联 → 上下文透视 → 改密重新登录");
    console.log(`      账号：${email}`);
    console.log(`      截图：${SHOT_MEMORY}`);
    console.log(`      截图：${SHOT_QUOTA}`);
    console.log(`      截图：${SHOT_CONTEXT}`);
    return 0;
  } finally {
    await browser.close();
  }
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    console.error(`FAIL  ${error.message}`);
    process.exit(1);
  });
