// 多页面 UI 截图验证：对运行中的网关逐页截图并统计关键 DOM。
// 用法: node scripts/ui_verify.mjs [baseURL]   (默认 http://127.0.0.1:9120)
// 输出: /tmp/ui-shots/<route>.png + 控制台 JSON 摘要
import { chromium } from "@playwright/test";
import { existsSync, mkdirSync } from "node:fs";

const exe = "/home/test/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell";
const base = process.argv[2] || process.env.JKAGENT_REAL_BASE_URL || "http://127.0.0.1:9120";
const outDir = "/tmp/ui-shots";
mkdirSync(outDir, { recursive: true });

const ROUTES = [
  "chat", "plan", "goal", "mcp", "skills", "prompt",
  "status", "cron", "workspace", "agent-editor", "settings",
];

const browser = await chromium.launch(existsSync(exe) ? { executablePath: exe } : {});
const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });

const summary = {};
const consoleErrors = [];
page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200));
});
page.on("pageerror", (err) => consoleErrors.push(String(err).slice(0, 200)));

for (const route of ROUTES) {
  try {
    await page.goto(`${base}/ui/#/${route}`, { waitUntil: "networkidle", timeout: 20000 });
    await page.waitForTimeout(700); // 等待渲染稳定
    const file = `${outDir}/${route}.png`;
    await page.screenshot({ path: file, fullPage: false });
    // 粗略活性指标：页面主体非空白（有可见文本节点）
    const bodyText = (await page.locator("body").innerText()).trim();
    summary[route] = {
      shot: file,
      textLen: bodyText.length,
      blank: bodyText.length < 20,
    };
    console.log(`✅ ${route}: text=${bodyText.length}`);
  } catch (e) {
    summary[route] = { error: String(e).slice(0, 160) };
    console.log(`❌ ${route}: ${String(e).slice(0, 120)}`);
  }
}

summary.__consoleErrors = consoleErrors.slice(0, 10);
console.log("SUMMARY " + JSON.stringify(summary, null, 1));
await browser.close();
