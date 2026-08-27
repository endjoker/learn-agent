import { expect, test, type Page, type Route } from "@playwright/test";

const json = (route: Route, body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });

const conv = {
  conversation_id: "conv-perf",
  session_key: "webui:default",
  origin: "webui",
  subtype: "main",
  workspace_id: null,
  execution_scope: "gateway:default",
  route_metadata: {},
  session_version: 0,
  created_at: "",
  updated_at: "",
};

const snap = {
  conversation: conv,
  session_version: 0,
  queue: [],
  live_turn: null,
  turn_version: 0,
  nodes: [],
  queued_nodes: [],
  pending_approvals: [],
  server_time: "",
};

const node = (over: Record<string, unknown>) => ({
  conversation_id: "conv-perf",
  turn_id: "t0",
  position: 1,
  status: "done",
  text: "",
  metadata: {},
  source_channel: null,
  source_message_id: null,
  sender_id: null,
  sender_name: null,
  created_at: "",
  updated_at: "",
  ...over,
});

const turn = (over: Record<string, unknown>) => ({
  turn_id: "t0",
  conversation_id: "conv-perf",
  status: "done",
  turn_version: 1,
  runtime_snapshot_id: null,
  started_at: "2026-01-01T00:00:00+00:00",
  finished_at: "2026-01-01T00:00:01+00:00",
  final_assistant_node_id: null,
  error_code: null,
  parent_conversation_id: null,
  parent_turn_id: null,
  ...over,
});

// 每个用例可覆盖的历史项与游标
let turnsItems: Array<{ turn: Record<string, unknown>; nodes: unknown[] }> = [];
let nextCursor: string | null = null;

const largeResult = "x".repeat(1024 * 1024);

async function installMock(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/events") return route.fulfill({ contentType: "text/event-stream", body: ": connected\n\n" });
    if (path === "/api/sessions") return json(route, { sessions: [{ session_key: "webui:default" }] });
    if (path === "/api/conversations" && route.request().method() === "POST") return json(route, { conversation: conv });
    if (path === "/api/conversations/lookup") return json(route, { conversation: conv });
    if (path === "/api/conversations/conv-perf/snapshot") return json(route, snap);
    if (path === "/api/conversations/conv-perf/turns") {
      return json(route, { items: turnsItems, next_cursor: nextCursor });
    }
    if (path === "/api/questions") return json(route, { questions: [] });
    if (path === "/api/approvals") return json(route, { approvals: [] });
    if (path === "/api/plans") return json(route, { plans: [] });
    if (path === "/api/goals") return json(route, { goals: [] });
    if (path === "/api/commands") return json(route, { commands: [] });
    if (path === "/api/config/models") return json(route, { models: [] });
    if (path.endsWith("/context")) return json(route, { model: "deepseek" });
    if (path.endsWith("/reasoning")) return json(route, { selected: "inherit", effective: "inherit" });
    if (path.endsWith("/permission")) return json(route, { mode: "ask", effective: "ask" });
    return json(route, { ok: true });
  });
}

test.beforeEach(async ({ page }) => {
  // 默认：大历史第一页 20 条（有下一页）
  nextCursor = "perf-cursor-1";
  turnsItems = Array.from({ length: 20 }, (_, index) => ({
    turn: turn({ turn_id: `t-${index}` }),
    nodes: [
      node({ turn_id: `t-${index}`, node_id: `u-${index}`, type: "user", text: `message-${index}-${"x".repeat(80)}`, position: 1 }),
      node({ turn_id: `t-${index}`, node_id: `a-${index}`, type: "assistant", text: `reply-${index}-${"x".repeat(80)}`, position: 2 }),
    ],
  }));
  await installMock(page);
});

test("a large session requests only the first page (limit=20, no cursor) and keeps DOM bounded", async ({ page }) => {
  const historyRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith("/turns")) historyRequests.push(url.search);
  });

  const started = Date.now();
  await page.goto("#/chat");
  await expect(page.getByText(/reply-0/)).toBeVisible();
  const interactiveMs = Date.now() - started;
  expect(interactiveMs).toBeLessThan(1000);
  expect(historyRequests[0]).toContain("limit=20");
  expect(historyRequests[0]).not.toContain("cursor=");

  const virtualNodes = await page.locator("[data-virtual-index]").count();
  expect(virtualNodes).toBeGreaterThan(0);
  expect(virtualNodes).toBeLessThan(60);
});

test("1 MiB tool output expands quickly and repeated toggles keep the page responsive", async ({ page }) => {
  // 一个含超大工具结果的 turn（result_summary > 64KB → LargeResult 触发展开控件）
  nextCursor = null;
  turnsItems = [{
    turn: turn({ turn_id: "t-big" }),
    nodes: [
      node({ turn_id: "t-big", node_id: "u-big", type: "user", text: "读大文件", position: 1 }),
      node({
        turn_id: "t-big", node_id: "cn-big", type: "tool", position: 2, status: "done",
        metadata: { call_id: "call-1", tool: "read", params_summary: "{\"path\":\"large.txt\"}", result_summary: largeResult },
      }),
    ],
  }];
  await page.goto("#/chat");

  const tool = page.locator(".tool-card summary").first();
  await tool.click();
  const expand = page.getByRole("button", { name: "展开全部" });
  await expect(expand).toBeVisible();

  const started = performance.now();
  await expand.click();
  const elapsed = performance.now() - started;
  expect(elapsed).toBeLessThan(200);
  await expect(page.getByRole("button", { name: "收起" })).toBeVisible();

  for (let index = 0; index < 30; index += 1) {
    const button = page.getByRole("button", { name: index % 2 === 0 ? "收起" : "展开全部" });
    await button.click();
  }
  await expect(page.getByRole("button", { name: "收起" })).toBeVisible();
  expect(await page.locator("[data-virtual-index]").count()).toBeLessThan(60);
});
