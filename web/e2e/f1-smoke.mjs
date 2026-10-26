import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const SHOT = fileURLToPath(new URL("../previews/f1-chat.png", import.meta.url));

const email = `f1+${Date.now()}@example.com`;
const password = "F1!Passw0rd2026";

async function assertGateway() {
  const response = await fetch(`${GATEWAY_URL}/api/v1/health`).catch(() => null);
  if (!response?.ok) {
    throw new Error(`网关不可达：${GATEWAY_URL}/api/v1/health`);
  }
}

async function main() {
  await assertGateway();

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  const errors = [];

  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    // 网络 4xx（例如故意登录失败的 401）是预期行为，不算前端异常。
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      errors.push(`console: ${message.text()}`);
    }
  });

  try {
    await page.goto(`${WEB_URL}/login`, { waitUntil: "domcontentloaded" });
    await page.getByTestId("auth-email").waitFor({ timeout: 10_000 });

    await page.getByRole("button", { name: "注册", exact: true }).click();
    await page.getByTestId("auth-nickname").fill("F1 验收");
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-password").fill(password);
    await page.getByTestId("auth-submit").click();

    await page.waitForURL("**/chat", { timeout: 15_000 });
    await page.getByTestId("user-nickname").waitFor({ timeout: 10_000 });
    const nickname = await page.getByTestId("user-nickname").innerText();
    if (!nickname.includes("F1 验收")) {
      throw new Error(`登录后昵称不符：${nickname}`);
    }
    await page.screenshot({ path: SHOT, fullPage: false });

    // B-08：refresh 只在 HttpOnly Cookie 里，JS 读不到。
    const cookiesAfterLogin = await page.context().cookies();
    const refreshCookie = cookiesAfterLogin.find((cookie) => cookie.name === "ai_refresh");
    if (!refreshCookie?.httpOnly) {
      throw new Error("登录后没有 HttpOnly 的 ai_refresh Cookie");
    }
    const legacy = await page.evaluate(() => localStorage.getItem("ai.refresh.v1"));
    if (legacy !== null) throw new Error("refresh token 仍在 localStorage");

    // 刷新后必须通过 Cookie 恢复，而不是退回登录页。
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForURL("**/chat", { timeout: 15_000 });
    await page.getByTestId("user-nickname").waitFor({ timeout: 10_000 });

    // 退出：Cookie 必须被清掉，并回到登录页。
    await page.getByTestId("logout").click();
    await page.waitForURL("**/login", { timeout: 15_000 });
    const cookiesAfterLogout = await page.context().cookies();
    if (cookiesAfterLogout.some((cookie) => cookie.name === "ai_refresh")) {
      throw new Error("退出后 ai_refresh Cookie 仍在");
    }

    // 失败态：真实 401 要把 code 展示出来，而不是吞掉。
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-password").fill("Wrong!Passw0rd2026");
    await page.getByTestId("auth-submit").click();
    await page.waitForFunction(() => {
      const node = document.querySelector('[data-testid="auth-error"]');
      return Boolean(node?.textContent?.trim());
    });
    const errorText = (await page.getByTestId("auth-error").innerText()).trim();
    if (!errorText.includes("INVALID_CREDENTIALS")) {
      throw new Error(`登录失败未展示错误码：${errorText}`);
    }

    if (errors.length) {
      throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);
    }

    console.log("PASS  F1 注册 → 登录 → 刷新恢复 → 退出 → 失败态");
    console.log(`      账号：${email}`);
    console.log(`      截图：${SHOT}`);
    await readFile(SHOT);
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
