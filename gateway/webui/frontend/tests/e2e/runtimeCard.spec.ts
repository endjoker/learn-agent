import { expect, test, type Page, type Route } from "@playwright/test";

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

// 镜像后端当前产物：plan/goal 轮次落在父会话 turn，assistant 节点带 runtime 标记，
// 工具节点带 runtime 标记（供卡片折叠明细识别），不再有独立 system 会话/投影表。
const conversation = {
  conversation_id: "conv-runtime",
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

const snapshot = {
  conversation,
  session_version: 0,
  queue: [],
  live_turn: null,
  turn_version: 0,
  nodes: [],
  queued_nodes: [],
  pending_approvals: [],
  server_time: "",
};

const runtimeTurn: {
  turn_id: string;
  conversation_id: string;
  status: string;
  turn_version: number;
  runtime_snapshot_id: string | null;
  started_at: string;
  finished_at: string | null;
  final_assistant_node_id: string | null;
  error_code: string | null;
  parent_conversation_id: string | null;
  parent_turn_id: string | null;
} = {
  turn_id: "t-runtime",
  conversation_id: "conv-runtime",
  status: "done",
  turn_version: 3,
  runtime_snapshot_id: null,
  started_at: "2026-01-01T00:00:00+00:00",
  finished_at: "2026-01-01T00:00:01+00:00",
  final_assistant_node_id: "n-rt",
  error_code: null,
  parent_conversation_id: null,
  parent_turn_id: null,
};

const toolNode = {
  node_id: "n-tool",
  conversation_id: "conv-runtime",
  turn_id: "t-runtime",
  type: "tool",
  position: 1,
  status: "done",
  text: "",
  metadata: { call_id: "c1", tool: "read", runtime_type: "plan", runtime_id: "plan-1", params_summary: "{\"path\":\"a.txt\"}", result_summary: "total 8" },
  source_channel: null,
  source_message_id: null,
  sender_id: null,
  sender_name: null,
  created_at: "",
  updated_at: "",
};

const finalNode = {
  node_id: "n-rt",
  conversation_id: "conv-runtime",
  turn_id: "t-runtime",
  type: "assistant",
  position: 2,
  status: "done",
  text: "方案完成",
  metadata: { runtime_type: "plan", runtime_id: "plan-1", runtime_status: "done" },
  source_channel: null,
  source_message_id: null,
  sender_id: null,
  sender_name: null,
  created_at: "",
  updated_at: "",
};

// 每个用例可覆盖历史项（运行中 vs 已终态）
let historyItems: Array<{ turn: typeof runtimeTurn; nodes: unknown[] }> = [];

async function installApiMocks(page: Page) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/events") {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: ": connected\n\n" });
      return;
    }
    if (path === "/api/sessions") {
      await json(route, { sessions: [{ session_key: "webui:default", name: "默认会话" }] });
      return;
    }
    if (path === "/api/conversations" && method === "POST") {
      await json(route, { conversation });
      return;
    }
    if (path === "/api/conversations/lookup") {
      await json(route, { conversation });
      return;
    }
    if (path === "/api/conversations/conv-runtime/snapshot") {
      await json(route, snapshot);
      return;
    }
    if (path === "/api/conversations/conv-runtime/turns") {
      await json(route, { items: historyItems, next_cursor: null });
      return;
    }
    if (path === "/api/plans" || path === "/api/goals") {
      await json(route, { plans: [], goals: [] });
      return;
    }
    if (path === "/api/commands") {
      await json(route, { commands: [] });
      return;
    }
    if (path === "/api/config/models") {
      await json(route, { models: [] });
      return;
    }
    if (path.endsWith("/context")) {
      await json(route, { model: "deepseek" });
      return;
    }
    if (path.endsWith("/reasoning")) {
      await json(route, { selected: "inherit", effective: "inherit" });
      return;
    }
    if (path.endsWith("/permission")) {
      await json(route, { mode: "ask", effective: "ask" });
      return;
    }
    await json(route, { ok: true });
  });
}

test.beforeEach(async ({ page }) => {
  // 默认可折叠明细的已终态 plan turn
  historyItems = [{ turn: runtimeTurn, nodes: [toolNode, finalNode] }];
  await installApiMocks(page);
});

test("plan runtime turn renders as an inline projection card, detail expandable, not flattened", async ({ page }) => {
  await page.goto("#/chat");

  // 卡片头部（始终可见）
  const card = page.locator(".ws-projection-card");
  await expect(card).toBeVisible();
  await expect(card.getByText("📋 Plan")).toBeVisible();
  await expect(card.getByText("已完成")).toBeVisible();

  // 不平铺：不出现普通 assistant message 气泡、不出现独立 tool-card
  await expect(page.locator(".bubble.assistant")).toHaveCount(0);
  await expect(page.locator(".tool-card")).toHaveCount(0);

  // 默认折叠：但头部摘要（参考工具卡）显示最终回复，可直接扫读
  await expect(card.getByText("方案完成").first()).toBeVisible();

  // 点开 → 终态正文可见
  await card.locator("summary").click();
  await expect(card.getByText("方案完成").last()).toBeVisible();

  // 明细可展开：从 timeline 传入的父会话工具节点读取
  await card.getByRole("button", { name: "查看工具调用详情" }).click();
  const detail = page.locator(".ws-projection-detail");
  await expect(detail).toBeVisible();
  await expect(detail.getByText("🔧 read")).toBeVisible();
  await expect(detail.getByText(/a\.txt/)).toBeVisible();
  await expect(detail.getByText(/total 8/)).toBeVisible();
});

test("a live runtime turn (no final node yet) shows a running card", async ({ page }) => {
  // 流式阶段：工具节点带 runtime 标记但助手终态尚未到达 → running 卡
  historyItems = [{ turn: { ...runtimeTurn, final_assistant_node_id: null }, nodes: [toolNode] }];
  await page.goto("#/chat");
  const card = page.locator(".ws-projection-card");
  await expect(card).toBeVisible();
  await expect(card.getByText("📋 Plan")).toBeVisible();
  await expect(card.getByText("运行中")).toBeVisible();
});
