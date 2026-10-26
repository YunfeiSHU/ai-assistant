import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const STREAM_SHOT = fileURLToPath(new URL("../previews/f3-streaming.png", import.meta.url));
const PARTIAL_SHOT = fileURLToPath(new URL("../previews/f3-partial.png", import.meta.url));

const email = `f3+${Date.now()}@example.com`;
const password = "F3!Passw0rd2026";

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
    body: { email, password, nickname: "F3 验收" },
  });
  const login = await api("/auth/login", {
    method: "POST",
    body: { email, password, device_name: "f3-e2e" },
  });
  const access = login.access_token;
  if (!access) throw new Error("登录响应缺少 access token");

  const conversation = await api("/conversations", {
    method: "POST",
    token: access,
    body: { title: "F3 流式验收" },
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
    await page.getByTestId("conv-item").filter({ hasText: "F3 流式验收" }).click();
    await page.getByTestId("conversation-title").filter({ hasText: "F3 流式验收" }).waitFor();

    const composer = page.getByTestId("composer");
    await composer.fill(
      "请写一篇大约一千字的文章，说明流式输出为什么能改善聊天产品的体验。不要使用列表，分段写。",
    );
    await page.getByTestId("send").click();
    await page.getByTestId("live-turn").waitFor({ timeout: 30_000 });

    await page.waitForFunction(
      () => {
        const node = document.querySelector('[data-testid="live-turn"] .md');
        return Boolean(node && (node.textContent?.length ?? 0) > 5);
      },
      undefined,
      { timeout: 60_000 },
    );
    const firstLength = await page.evaluate(
      () => document.querySelector('[data-testid="live-turn"] .md')?.textContent?.length ?? 0,
    );
    await page.waitForTimeout(350);
    const secondLength = await page.evaluate(
      () => document.querySelector('[data-testid="live-turn"] .md')?.textContent?.length ?? 0,
    );
    if (secondLength <= firstLength) {
      throw new Error(`流式正文没有增长：${firstLength} -> ${secondLength}`);
    }
    await page.screenshot({ path: STREAM_SHOT, fullPage: false });

    // 停止：先看到 live partial，再等服务端落库并替换成历史消息。
    await page.getByTestId("stop").click();
    await page.waitForFunction(
      () => Boolean(document.querySelector('[data-testid="partial-chip"]')),
      undefined,
      { timeout: 20_000 },
    );
    await page.waitForFunction(
      () => !document.querySelector('[data-testid="live-turn"]'),
      undefined,
      { timeout: 20_000 },
    );
    if ((await page.locator('.msg.assistant [data-testid="partial-chip"]').count()) === 0) {
      throw new Error("停止后历史里没有 partial 消息");
    }
    // StrictMode 下不能重复发送：用户消息 + partial 助手消息应恰好 2 条。
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="message-item"]').length === 2,
      undefined,
      { timeout: 20_000 },
    );

    // 刷新后 partial 与正文仍在。
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByTestId("conversation-title").filter({ hasText: "F3 流式验收" }).waitFor({
      timeout: 15_000,
    });
    await page.waitForFunction(
      () => Boolean(document.querySelector('[data-testid="partial-chip"]')),
      undefined,
      { timeout: 15_000 },
    );
    const partialText = await page.locator(".msg.assistant").first().innerText();
    if (partialText.trim().length < 20) throw new Error("刷新后 partial 正文为空");
    await page.screenshot({ path: PARTIAL_SHOT, fullPage: false });

    // 第二轮：完整生成，确认 done 后 live 收起、发送键恢复。
    await page.getByTestId("composer").fill("用一句话说明 SSE 是什么。");
    await page.getByTestId("send").click();
    await page.getByTestId("live-turn").waitFor({ timeout: 30_000 });
    await page.getByTestId("send").waitFor({ timeout: 90_000 });
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="message-item"]').length === 4,
      undefined,
      { timeout: 20_000 },
    );
    if ((await page.getByTestId("stream-error").count()) > 0) {
      throw new Error("完整生成后仍出现 stream-error");
    }

    if (errors.length) throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);

    console.log("PASS  F3 真实流式 → 停止 partial → 刷新保持 → 第二轮 done");
    console.log(`      账号：${email}`);
    console.log(`      会话：${conversation.id}`);
    console.log(`      截图：${STREAM_SHOT}`);
    console.log(`      截图：${PARTIAL_SHOT}`);
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
