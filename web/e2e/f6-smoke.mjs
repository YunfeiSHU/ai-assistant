import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const SHOT = fileURLToPath(new URL("../previews/f6-chat.png", import.meta.url));
const MOBILE_SHOT = fileURLToPath(new URL("../previews/f6-mobile.png", import.meta.url));

const email = `f6+${Date.now()}@example.com`;
const password = "F6!Passw0rd2026";

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(`${GATEWAY_URL}/api/v1${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return response;
}

async function main() {
  const health = await fetch(`${GATEWAY_URL}/api/v1/health`).catch(() => null);
  if (!health?.ok) throw new Error("网关不可达");
  await api("/auth/register", {
    method: "POST",
    body: { email, password, nickname: "F6 验收" },
  });

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
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

    await page.getByRole("button", { name: /新建对话/ }).click();
    await page.getByTestId("conversation-title").waitFor({ timeout: 15_000 });
    await page.getByTestId("composer").fill("请只回复收到。");
    await page.getByTestId("send").click();
    await page.getByTestId("send").waitFor({ timeout: 90_000 });
    await page.getByTestId("message-item").first().waitFor({ timeout: 15_000 });

    // B-03 重新生成
    await page.getByTestId("message-regenerate").last().click();
    await page.getByText("已重新生成回答").waitFor({ timeout: 90_000 });
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="message-item"]').length >= 2,
      undefined,
      { timeout: 20_000 },
    );

    // B-04 删除消息（含 AI 上下文同步）
    await page.getByTestId("message-delete").last().click();
    await page.getByTestId("delete-message-confirm").click();
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="message-item"]').length === 1,
      undefined,
      { timeout: 20_000 },
    );
    await page.waitForFunction(
      () => document.querySelector(".chat-page-head p")?.textContent?.includes("1 条消息"),
      undefined,
      { timeout: 20_000 },
    );

    // B-11：增量刷新用 after_seq 只拉新增；没有新增时提示“已是最新”
    await page.getByTestId("sync-messages").click();
    await page.getByText("已经是最新消息").waitFor({ timeout: 15_000 });

    // F6 键盘：Modal 焦点在对话框内，Esc 关闭并恢复焦点
    await page.getByTestId("rename").click();
    await page.getByRole("dialog").waitFor();
    const focusInside = await page.evaluate(() =>
      Boolean(document.querySelector('[role="dialog"]')?.contains(document.activeElement)),
    );
    if (!focusInside) throw new Error("Modal 打开后焦点不在对话框内");
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "detached", timeout: 10_000 });
    await page.screenshot({ path: SHOT, fullPage: false });

    // F6 移动端：500px 下无横向溢出
    const mobile = await context.newPage();
    await mobile.setViewportSize({ width: 500, height: 900 });
    await mobile.goto(`${WEB_URL}/chat`, { waitUntil: "domcontentloaded" });
    await mobile.waitForURL("**/chat", { timeout: 15_000 });
    await mobile.getByTestId("user-nickname").waitFor({ timeout: 15_000 });
    await mobile.waitForTimeout(500);
    const overflow = await mobile.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    if (overflow) throw new Error("移动端出现横向溢出");
    await mobile.screenshot({ path: MOBILE_SHOT, fullPage: false });

    if (errors.length) throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);

    console.log("PASS  F6 重新生成 → 删除消息 → Modal 键盘 → 500px 移动端");
    console.log(`      账号：${email}`);
    console.log(`      截图：${SHOT}`);
    console.log(`      截图：${MOBILE_SHOT}`);
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
