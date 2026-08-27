import { expect, test } from "@playwright/test";

// 修正版方案 A 真实浏览器验证：图片随消息经统一队列发送，
// 历史渲染缩略图（image-card），图片端点按会话归属回原图。

const SESSION = "webui:imgrender";

test("real: image message renders thumbnail from conversation image endpoint", async ({ page }) => {
  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:imgrender");
  });
  await sessionSelect.selectOption(SESSION);

  // 图片卡片渲染
  const imgCard = page.locator(".image-card");
  await expect(imgCard).toHaveCount(1);
  const thumb = page.locator(".image-card .image-thumb");
  await expect(thumb).toBeVisible();

  // 端点回图成功（200 + image/png + 尺寸>0）
  const src = await thumb.getAttribute("src");
  expect(src).toBeTruthy();
  const resp = await page.request.get(new URL(src!, page.url()).toString());
  expect(resp.status()).toBe(200);
  expect(resp.headers()["content-type"]).toContain("image/png");
  const buf = await resp.body();
  expect(buf.length).toBeGreaterThan(0);

  // 用户消息文本与缩略图同 turn 展示
  await expect(page.locator(".bubble.user").filter({ hasText: "看看这张图" })).toHaveCount(1);

  // 截图留档
  await page.screenshot({ path: "test-results/real-image-chat.png", fullPage: true });
});

test("real: sending image via API appears LIVE without reload, right-aligned", async ({ page }) => {
  await page.goto("#/chat");
  const sessionSelect = page.locator('select[aria-label="会话"]');
  await page.waitForFunction(() => {
    const sel = document.querySelector<HTMLSelectElement>('select[aria-label="会话"]');
    return sel && Array.from(sel.options).some((o) => o.value === "webui:imgrender");
  });
  await sessionSelect.selectOption(SESSION);
  const before = await page.locator(".image-card").count();

  // 1x1 png
  const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==";
  // 经页面同源 API 直发（enqueue images + sendNext），全程不刷新页面
  const convResp = await page.request.get("/api/conversations");
  const convList = await convResp.json();
  const list: Array<{ conversation_id: string; session_key?: string }> =
    convList.conversations ?? convList;
  const conv = list.find((c) => c.session_key === SESSION) ?? list[0];
  if (!conv) throw new Error("webui:imgrender conversation not seeded");
  const cid = conv.conversation_id;
  const enq = await page.request.post(`/api/conversations/${cid}/queue`, {
    data: { text: "实时图片验证", images: [{ data: PNG, media_type: "image/png" }] },
  });
  expect(enq.status()).toBe(200);
  const sn = await page.request.post(`/api/conversations/${cid}/queue/send-next`, { data: {} });
  expect(sn.status()).toBe(200);
  const snBody = await sn.json();
  expect((snBody.image_nodes ?? []).length).toBeGreaterThanOrEqual(1);

  // 缩略图**立即**出现（不刷新）——实时链路（node.image SSE / 响应合并）
  await expect(page.locator(".image-card")).toHaveCount(before + 1, { timeout: 5000 });

  // 右对齐：卡片左边缘必须落在视口中线右侧（用户侧）
  const card = page.locator(".image-card").last();
  const box = await card.boundingBox();
  expect(box).toBeTruthy();
  expect(box!.x).toBeGreaterThan(page.viewportSize()!.width * 0.5);

  await page.screenshot({ path: "test-results/real-image-live.png", fullPage: true });
});
