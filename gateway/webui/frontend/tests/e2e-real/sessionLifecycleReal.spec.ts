import { expect, test } from "@playwright/test";

// 真实网关（不 mock /api）：会话新建/删除生命周期。全部用真实 REST + 真实页面。
// 用一次性的 webui:lifecycle 会话，避免污染其它用例。
const KEY = "webui:lifecycle";

test("real: create a session via real API, see it in the list, then delete it", async ({ page, request }) => {
  // 真实 REST 新建会话
  const created = await request.post("/api/conversations", { data: { session_key: KEY } });
  expect(created.ok()).toBeTruthy();

  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await expect(sessionSelect).toBeVisible();
  // 会话下拉应包含新会话
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:lifecycle");
  });
  // 选中新会话 → 空历史提示
  await sessionSelect.selectOption(KEY);
  await expect(page.getByText("（无历史消息，发送第一条开始对话）")).toBeVisible();

  // 真实 REST 删除
  const del = await request.delete(`/api/sessions/${KEY}`);
  expect(del.ok()).toBeTruthy();
  // 刷新后会话下拉不再包含
  await page.reload();
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && !Array.from(sel.options).some((o) => o.value === "webui:lifecycle");
  });
});
