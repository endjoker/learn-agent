import { expect, test } from "@playwright/test";

// 直连真实网关：真实 /api（不 mock）→ 真实浏览器渲染。数据由
// scripts/seed_real_e2e.py 种入（webui:render / webui:default）。

const RENDER_SESSION = "webui:render";

test("real: main chat renders final assistant answer + tool card + reasoning card", async ({ page }) => {
  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await expect(sessionSelect).toBeVisible();
  // 等待内嵌选项加载 /api/sessions 后选择 webui:render
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:render");
  });
  await sessionSelect.selectOption(RENDER_SESSION);

  // 最终答复作为普通 assistant 气泡渲染（修复点）
  const assistant = page.locator(".bubble.assistant");
  await expect(assistant).toHaveCount(1);
  await expect(assistant).toContainText("项目结构已读取");

  // 工具卡片：read
  const toolCard = page.locator(".tool-card");
  await expect(toolCard).toHaveCount(1);
  await expect(toolCard).toContainText("read");

  // 思考卡片
  await expect(page.locator(".reasoning-card")).toHaveCount(1);

  // 用户气泡同步渲染
  await expect(page.locator(".bubble.user")).toHaveCount(1);

  // 截图留档（真实渲染）
  await page.screenshot({ path: "test-results/real-main-chat.png", fullPage: true });
});

test("real: BUG A — model switch persists across reload", async ({ page }) => {
  await page.goto("#/chat");
  // 默认会话 webui:default
  const modelSelect = page.locator('select[aria-label="模型"]');
  await expect(modelSelect).toBeVisible();
  // 等待模型选项加载（/api/config/models）
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="模型"]');
    return sel && Array.from(sel.options).some((o) => o.value === "gpt-5.6-luna");
  });
  await modelSelect.selectOption("gpt-5.6-luna");
  // 切换走 update_prefs -> route_metadata.prefs.model；等 POST 落盘（不依赖 toast）。
  await page.waitForTimeout(1200);

  // 刷新：_make_context 现在优先返回持久化 prefs.model，而非创建时的 agent.llm.model
  await page.reload();
  const modelAfter = page.locator('select[aria-label="模型"]');
  await expect(modelAfter).toBeVisible();
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="模型"]');
    return sel && sel.value === "gpt-5.6-luna";
  });
  await expect(modelAfter).toHaveValue("gpt-5.6-luna");
});
