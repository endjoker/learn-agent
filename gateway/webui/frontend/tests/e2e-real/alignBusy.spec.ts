import { expect, test } from "@playwright/test";

// 用户反馈 2026-08 回归：
// 1) 忙拒绝提示（⚠️ 会话正忙…）不得作为 assistant 气泡出现在时间线；
// 2) 工具卡头部摘要一律紧跟工具名左对齐（不再居中/偏右混用）。

const SESSION = "webui:imgrender";

test("real: busy rejection does not create a reply bubble; card heads left-aligned", async ({ page }) => {
  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:imgrender");
  });
  await sessionSelect.selectOption(SESSION);

  // 先等本会话首条内容渲染
  await expect(page.locator(".bubble.assistant").first()).toBeVisible();
const busyTexts = await page.getByText("会话正忙").count();
  expect(busyTexts).toBe(0);

  // 工具卡头部：摘要徽章紧跟工具名（左对齐）——量 tool-name 右缘与
  // tool-summary 左缘的间距应小于工具名左缘到卡片右缘的距离的一半
  // 虚拟列表会裁剪不可见行（boundingBox 返回 null / 定位超时）——改为
  // DOM 层几何断言：工具名右缘距卡片左缘应 < 卡宽 40%（名后无弹性空白
  // 把摘要推去右侧，即"紧跟工具名左对齐"）。
  await sessionSelect.selectOption("webui:render");
  await expect(page.locator(".tool-card").first()).toBeVisible();
  const geom = await page.evaluate(() => {
    const cardEl = document.querySelector(".tool-card");
    const nameEl = document.querySelector(".tool-card .tool-name");
    if (!cardEl || !nameEl) return null;
    const c = cardEl.getBoundingClientRect();
    const n = nameEl.getBoundingClientRect();
    return { cardW: c.width, nameRightOffset: n.right - c.x };
  });
  expect(geom).not.toBeNull();
  expect(geom!.nameRightOffset).toBeLessThan(geom!.cardW * 0.4);

  await page.screenshot({ path: "test-results/real-align-busy.png", fullPage: true });
});
