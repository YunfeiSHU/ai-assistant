import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const LIST_SHOT = fileURLToPath(new URL("../previews/f2-list.png", import.meta.url));
const CONV_SHOT = fileURLToPath(new URL("../previews/f2-conversation.png", import.meta.url));

const email = `f2+${Date.now()}@example.com`;
const password = "F2!Passw0rd2026";

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
  if (!response.ok) {
    throw new Error(`${method} ${path} -> ${response.status} ${JSON.stringify(data)}`);
  }
  return data;
}

async function main() {
  const health = await fetch(`${GATEWAY_URL}/api/v1/health`).catch(() => null);
  if (!health?.ok) throw new Error(`网关不可达：${GATEWAY_URL}/api/v1/health`);

  await api("/auth/register", {
    method: "POST",
    body: { email, password, nickname: "F2 验收" },
  });
  const login = await api("/auth/login", {
    method: "POST",
    body: { email, password, device_name: "f2-e2e" },
  });
  const access = login.access_token;
  if (!access) throw new Error("登录响应缺少 access token");

  // 顺序创建：后端 pkg/ids 的全局 ulid.Monotonic 并发不安全，已在 ROADMAP §3 记录为待确认缺陷。
  const conversations = [];
  for (let index = 0; index < 55; index += 1) {
    const conversation = await api("/conversations", {
      method: "POST",
      token: access,
      body: { title: `F2-${String(index).padStart(2, "0")}` },
    });
    conversations.push(conversation);
  }

  const target = conversations.find((item) => item.title === "F2-00");
  if (!target) throw new Error("未创建 F2-00 会话");

  await api(`/conversations/${target.id}/messages`, {
    method: "POST",
    token: access,
    body: {
      content: "F2 历史验收：这条消息必须出现在历史列表里。",
      use_rag: false,
      use_memory: false,
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

    await page.getByTestId("conv-item").first().waitFor({ timeout: 15_000 });

    let count = await page.getByTestId("conv-item").count();
    for (let i = 0; i < 5 && count < 55; i += 1) {
      const more = page.getByTestId("conv-more");
      if ((await more.count()) === 0) break;
      await more.click();
      await page.waitForFunction(
        (previous) =>
          document.querySelectorAll('[data-testid="conv-item"]').length > previous,
        count,
      );
      count = await page.getByTestId("conv-item").count();
    }
    if (count !== 55) throw new Error(`55 条会话未全部出现，实际 ${count}`);
    await page.screenshot({ path: LIST_SHOT, fullPage: false });

    await page.getByTestId("conv-item").filter({ hasText: "F2-00" }).click();
    await page.getByTestId("conversation-title").waitFor();
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="message-item"]').length >= 2,
      undefined,
      { timeout: 15_000 },
    );
    const userMessage = page.getByTestId("message-item").filter({ hasText: "F2 历史验收" });
    if ((await userMessage.count()) === 0) throw new Error("会话历史里没有用户消息");
    if ((await page.locator(".msg.assistant").count()) === 0) throw new Error("会话历史里没有助手消息");
    await page.screenshot({ path: CONV_SHOT, fullPage: false });

    // 改名
    await page.getByTestId("rename").click();
    await page.getByTestId("rename-input").fill("F2 重命名验收");
    await page.getByTestId("rename-save").click();
    await page
      .getByTestId("conversation-title")
      .filter({ hasText: "F2 重命名验收" })
      .waitFor({ timeout: 10_000 });

    // 置顶
    await page.getByTestId("pin").click();
    await page.waitForFunction(() =>
      document.querySelector('[data-testid="pin"]')?.textContent?.includes("已置顶"),
    );

    // 归档 + 输入禁用
    await page.getByTestId("archive").click();
    await page.getByTestId("archived-banner").waitFor({ timeout: 10_000 });
    if (!(await page.getByTestId("composer").isDisabled())) {
      throw new Error("归档会话的输入框仍可用");
    }

    // 刷新后仍能看到归档态与标题
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByTestId("archived-banner").waitFor({ timeout: 15_000 });
    await page
      .getByTestId("conversation-title")
      .filter({ hasText: "F2 重命名验收" })
      .waitFor({ timeout: 10_000 });

    // 取消归档
    await page.getByTestId("unarchive").click();
    await page.getByTestId("archived-banner").waitFor({ state: "detached", timeout: 10_000 });

    // 搜索 + 删除
    await page.getByLabel("搜索会话").fill("F2-01");
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="conv-item"]').length === 1,
      undefined,
      { timeout: 10_000 },
    );
    await page.getByTestId("conv-item").filter({ hasText: "F2-01" }).click();
    await page.getByTestId("conversation-title").filter({ hasText: "F2-01" }).waitFor();
    await page.getByTestId("delete").click();
    await page.getByTestId("delete-confirm").click();
    await page.waitForURL("**/chat", { timeout: 10_000 });
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="conv-item"]').length === 0,
      undefined,
      { timeout: 10_000 },
    );

    if (errors.length) throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);

    console.log("PASS  F2 列表 55 / 翻页 / 历史 / 改名 / 置顶 / 归档 / 取消归档 / 搜索 / 删除");
    console.log(`      账号：${email}`);
    console.log(`      截图：${LIST_SHOT}`);
    console.log(`      截图：${CONV_SHOT}`);
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
