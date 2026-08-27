import { expect, test } from "@playwright/test";

// 真实流式回合回归（决策 9）：需要 scripts/seed_streaming_e2e.py 启动的
// 专用网关（真实 LLM + allow 模式 + auto_execute_on_send_next）。
// 覆盖：流式期间展开卡片 → 虚拟列表按真实高度重排不叠压（展开叠压回归）、
// TurnStatus 出现/消失、回合正常完成。
//
// 说明：依赖真实 LLM（config.json 的 llm 段），耗时与模型速度相关。

test.setTimeout(240_000);

test("real: streaming turn — expand card mid-run, virtual rows never overlap", async ({ page }) => {
  await page.goto("#/chat");
  const composer = page.locator("textarea[aria-label='消息']");
  await expect(composer).toBeVisible({ timeout: 15_000 });

  // 发送一个会用工具的短任务（write + read，均在 allow 模式下自动执行）
  await composer.fill("用 write 工具创建 streaming_check.txt 内容为 ok，然后用 read 工具读回它，最后只回复文件内容。");
  await composer.press("Enter");

  // 流式中：Turn 状态条出现（live）
  const liveStatus = page.locator('[data-testid="turn-status"].live');
  await expect(liveStatus).toBeVisible({ timeout: 60_000 });

  // 等第一张工具卡出现（write/read 必经）
  const firstCardSummary = page.locator(".tool-card summary").first();
  await firstCardSummary.waitFor({ state: "visible", timeout: 90_000 });

  // 流式期间展开它（若已展开则先收起再展开，保证触发 open 属性变化）
  await firstCardSummary.click();
  await page.waitForTimeout(300);

  // 核心断言：可见虚拟行按真实高度排列，任意两行不得垂直叠压
  // （展开叠压回归：旧实现重测空转，展开后后续行按折叠旧高度叠压）
  const overlap = await page.evaluate(() => {
    const rects = Array.from(document.querySelectorAll<HTMLElement>("[data-virtual-index]"))
      .map((node) => node.getBoundingClientRect())
      .filter((rect) => rect.height > 0)
      .sort((a, b) => a.top - b.top);
    for (let i = 1; i < rects.length; i++) {
      const cur = rects[i], prev = rects[i - 1];
      if (cur && prev && cur.top < prev.bottom - 1) return true;
    }
    return false;
  });
  expect(overlap, "展开卡片后虚拟行不得叠压").toBe(false);

  // 回合完成：live 状态条消失（含 5s 用时闪现后的收尾）
  await expect(liveStatus).toHaveCount(0, { timeout: 180_000 });

  // 助手最终回复存在
  await expect(page.locator(".bubble.assistant").last()).toContainText(/ok/i, { timeout: 15_000 });
});
