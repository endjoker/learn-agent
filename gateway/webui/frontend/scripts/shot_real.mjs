import { chromium } from "@playwright/test";
import { existsSync } from "node:fs";

const exe = "/home/test/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell";
const launch = { executablePath: existsSync(exe) ? exe : undefined };
const base = process.env.JKAGENT_REAL_BASE_URL || "http://127.0.0.1:9120";

const browser = await chromium.launch(launch);
const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
await page.goto(`${base}/ui/#/chat`, { waitUntil: "networkidle" });
await page.waitForSelector(".ws-projection-card", { timeout: 15000 });
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/shot-1-collapsed.png" });

// 展开卡片
await page.locator(".ws-projection-card summary").click();
await page.waitForTimeout(300);
await page.screenshot({ path: "/tmp/shot-2-expanded.png" });

// 拉取工具调用明细
await page.locator(".ws-projection-card").getByRole("button", { name: "查看工具调用详情" }).click();
await page.waitForTimeout(500);
await page.screenshot({ path: "/tmp/shot-3-detail.png" });

// 取证：卡片标题/正文/明细都真实渲染
const info = {
  cardCount: await page.locator(".ws-projection-card").count(),
  bubbleAssistant: await page.locator(".bubble.assistant").count(),
  toolCard: await page.locator(".tool-card").count(),
  detailTool: await page.locator(".ws-projection-detail .ws-proj-tool-name").count(),
};
console.log("INFO", JSON.stringify(info));
await browser.close();
