import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const WEB_URL = process.env.WEB_URL ?? "http://127.0.0.1:3000";
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://127.0.0.1:8080";
const SHOT_KB = fileURLToPath(new URL("../previews/f4-knowledge.png", import.meta.url));
const SHOT_TASKS = fileURLToPath(new URL("../previews/f4-tasks.png", import.meta.url));

const email = `f4+${Date.now()}@example.com`;
const password = "F4!Passw0rd2026";
const kbName = "F4 验收库";

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
    body: { email, password, nickname: "F4 验收" },
  });
  const login = await api("/auth/login", {
    method: "POST",
    body: { email, password, device_name: "f4-e2e" },
  });
  if (!login.access_token) throw new Error("登录响应缺少 access token");

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

    await page.locator("nav.side-nav button").filter({ hasText: "知识库" }).click();
    await page.waitForURL("**/knowledge", { timeout: 10_000 });

    await page.getByTestId("kb-create").click();
    await page.getByTestId("kb-name").fill(kbName);
    await page.getByTestId("kb-desc").fill("F4 端到端验收：退款政策");
    await page.getByTestId("kb-submit").click();
    await page.getByTestId("kb-card").filter({ hasText: kbName }).waitFor({ timeout: 15_000 });

    const paragraph =
      "退款政策：签收后 7 个自然日内，商品未拆封可以申请无理由退款。审核通过后原路退回，通常 1-3 个工作日到账。若商品有质量问题，运费由商家承担。";
    const content = `# 退款政策\n\n${Array.from({ length: 12 }, () => paragraph).join("\n\n")}\n`;
    await page.getByTestId("doc-upload").setInputFiles({
      name: "refund.md",
      mimeType: "text/markdown",
      buffer: Buffer.from(content),
    });
    await page.getByTestId("upload-progress").waitFor({ timeout: 15_000 });
    await page.waitForFunction(
      () =>
        Array.from(document.querySelectorAll('[data-testid="doc-status"]')).some((node) =>
          node.textContent?.includes("已入库"),
        ),
      undefined,
      { timeout: 60_000 },
    );

    const docRow = page.getByTestId("doc-item").filter({ hasText: "refund.md" });
    await docRow.waitFor({ timeout: 15_000 });

    await docRow.getByTestId("doc-chunks").click();
    await page.getByTestId("chunk-item").first().waitFor({ timeout: 15_000 });
    const chunkText = await page.getByTestId("chunk-item").first().innerText();
    if (!chunkText.includes("退款")) throw new Error("切片内容里没有退款文本");
    await page.keyboard.press("Escape");

    await page.getByTestId("kb-search-input").fill("退款多久到账");
    await page.getByTestId("kb-search-submit").click();
    await page.getByTestId("search-hit").first().waitFor({ timeout: 30_000 });
    const hitText = await page.getByTestId("search-results").innerText();
    if (!hitText.includes("退款")) throw new Error("检索结果里没有退款内容");
    await page.screenshot({ path: SHOT_KB, fullPage: false });

    await page.locator("nav.side-nav button").filter({ hasText: "任务" }).click();
    await page.waitForURL("**/tasks", { timeout: 10_000 });
    await page.getByTestId("task-row").first().waitFor({ timeout: 15_000 });
    await page.waitForFunction(
      () =>
        Array.from(document.querySelectorAll('[data-testid="task-status"]')).some((node) =>
          node.textContent?.includes("已完成"),
        ),
      undefined,
      { timeout: 30_000 },
    );
    await page.screenshot({ path: SHOT_TASKS, fullPage: false });

    // 短文档：失败状态 + 重试入口。
    await page.locator("nav.side-nav button").filter({ hasText: "知识库" }).click();
    await page.waitForURL("**/knowledge", { timeout: 10_000 });
    await page.getByTestId("kb-card").filter({ hasText: kbName }).waitFor({ timeout: 15_000 });
    await page.getByTestId("doc-upload").setInputFiles({
      name: "bad.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("太短"),
    });
    await page.waitForFunction(
      () =>
        Array.from(document.querySelectorAll('[data-testid="doc-item"]')).some((row) => {
          const status = row.querySelector('[data-testid="doc-status"]');
          return row.textContent?.includes("bad.txt") && status?.textContent?.includes("失败");
        }),
      undefined,
      { timeout: 40_000 },
    );

    await page.locator("nav.side-nav button").filter({ hasText: "任务" }).click();
    await page.waitForURL("**/tasks", { timeout: 10_000 });
    const failedRow = page.getByTestId("task-row").filter({ hasText: "失败" });
    await failedRow.first().waitFor({ timeout: 15_000 });
    const retryButton = failedRow.first().getByTestId("task-retry");
    await retryButton.waitFor({ timeout: 15_000 });
    const retryResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/tasks/") &&
        response.url().endsWith("/retry"),
      { timeout: 15_000 },
    );
    await retryButton.click();
    const retryResult = await retryResponse;
    if (!retryResult.ok()) throw new Error(`重试接口失败：HTTP ${retryResult.status()}`);
    await page.getByText("已重新入队").waitFor({ timeout: 10_000 });

    if (errors.length) throw new Error(`浏览器控制台错误：\n${errors.join("\n")}`);

    console.log("PASS  F4 建库 → 上传 → 入库 → 切片 → 检索 → 任务 → 失败重试");
    console.log(`      账号：${email}`);
    console.log(`      截图：${SHOT_KB}`);
    console.log(`      截图：${SHOT_TASKS}`);
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
