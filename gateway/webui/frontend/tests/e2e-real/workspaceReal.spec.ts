import { expect, test } from "@playwright/test";

// 直连真实网关（不 mock /api）：工作区会话 turn 渲染行为（BUG B）。
// 数据由 scripts/seed_real_e2e.py 种入（webui:render / 工作区 ws_real_e2e）。
// 工作区与主会话共用同一 TimelineRow 组件（bubble / tool-card / reasoning-card）。

const WID = "ws_real_e2e";

test("real: BUG B — workspace renders the SAME cards as main chat (bubble + tool + reasoning)", async ({ page }) => {
  await page.goto(`#/workspace?id=${WID}`);

  // 工作区导航出现（真实 /api/workspaces）
  const wsItem = page.locator(".ws-item", { hasText: "真实E2E工作区" });
  await expect(wsItem).toBeVisible({ timeout: 10_000 });

  // 若会话树未展开，点击工作区行/切换钮展开
  const sessionItem = page.locator(".ws-session-item");
  if ((await sessionItem.count()) === 0) {
    await wsItem.locator(".ws-tree-toggle").click();
  }
  await expect(sessionItem).toHaveCount(1);
  await sessionItem.click();

  // 等待聊天体渲染
  const chatBody = page.locator(".ws-chat-body");
  await expect(chatBody).toBeVisible({ timeout: 10_000 });

  // 与主会话一致的卡片：最终答复为 .bubble.assistant（非 ws-msg / 运行进度）
  await expect(page.locator(".bubble.assistant")).toHaveCount(1);
  await expect(page.locator(".bubble.assistant")).toContainText("工作区项目概要");
  // 不再有旧的"运行进度"误判形态
  await expect(page.locator(".ws-process-card")).toHaveCount(0);

  // 工具卡片 + 思考卡片（与主会话相同组件/类名）
  await expect(page.locator(".tool-card")).toHaveCount(1);
  await expect(page.locator(".reasoning-card")).toHaveCount(1);

  // 截图留档（真实渲染）
  await page.screenshot({ path: "/tmp/real-workspace.png", fullPage: true });
});
