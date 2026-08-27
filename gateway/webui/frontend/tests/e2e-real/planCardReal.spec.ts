import { expect, test } from "@playwright/test";

// 直连真实网关（scripts/seed_real_e2e.py 已在 baseURL 跑，webui:default 已种入
// plan runtime turn）。此处不 mock /api：真实后端 → 真实 /api/conversations →
// 真实浏览器渲染 plan 内联卡片。卡片默认折叠 + 头部摘要 pill（修复后行为）。
test("real gateway: plan runtime turn renders as an inline projection card, not flattened", async ({ page }) => {
  await page.goto("#/chat");

  // 卡片头部（真实后端数据 → 真实 DOM）
  const card = page.locator(".ws-projection-card");
  await expect(card).toBeVisible();
  await expect(card.getByText("📋 Plan")).toBeVisible();
  await expect(card.getByText("已完成")).toBeVisible();

  // 不平铺：不出现普通 assistant 气泡、不出现独立 tool-card
  await expect(page.locator(".bubble.assistant")).toHaveCount(0);
  await expect(page.locator(".tool-card")).toHaveCount(0);

  // 折叠态：头部摘要 pill 已含终态文本（默认折叠，不自动展开）
  await expect(card.locator(".ws-projection-summary")).toContainText("方案完成：读取 a.txt");

  // 点开卡片 → 终态正文
  await card.locator("summary").click();
  await expect(card.locator(".ws-projection-body.md")).toContainText("方案完成：读取 a.txt");

  // 明细从父会话同 turn 节点读取（真实 DB）
  await card.getByRole("button", { name: "查看工具调用详情" }).click();
  const detail = page.locator(".ws-projection-detail");
  await expect(detail).toBeVisible();
  await expect(detail.getByText("🔧 read")).toBeVisible();
  await expect(detail.getByText(/a\.txt/)).toBeVisible();
});
