import { expect, test } from "@playwright/test";

// 抓取主会话截图用于人工核对：最终答复气泡、工具/思考卡、上下文详情弹层。

const SESSION = "webui:render";

test("capture: main chat rendering + context detail popup", async ({ page }) => {
  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:render");
  });
  await sessionSelect.selectOption(SESSION);
  await expect(page.locator(".bubble.assistant")).toHaveCount(1);

  // 主会话整体
  await page.screenshot({ path: "/tmp/real-main-chat.png", fullPage: true });

  // 上下文详情弹层（hover ctx-meter）
  const meter = page.locator(".ctx-meter");
  await expect(meter).toBeVisible();
  await meter.hover();
  const tip = page.locator(".ctx-tip");
  await expect(tip).toBeVisible();
  await page.screenshot({ path: "/tmp/real-ctx-popup.png", fullPage: true });
});
