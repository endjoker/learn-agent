import { expect, test, type Page, type Route } from "@playwright/test";

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const baseConv = {
  conversation_id: "conv-chat",
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
  conversation: baseConv,
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
  conversation_id: "conv-chat",
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
  conversation_id: "conv-chat",
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

const currentPage = {
  items: [{
    turn: turn({ turn_id: "t0" }),
    nodes: [
      node({ turn_id: "t0", node_id: "u1", type: "user", text: "当前问题", position: 1 }),
      node({ turn_id: "t0", node_id: "a1", type: "assistant", text: "当前回答", position: 2 }),
    ],
  }],
  next_cursor: "cursor-1",
};

const olderPage = {
  items: [{
    turn: turn({ turn_id: "t-old" }),
    nodes: [
      node({ turn_id: "t-old", node_id: "u-old", type: "user", text: "更早的问题", position: 1 }),
      node({ turn_id: "t-old", node_id: "a-old", type: "assistant", text: "更早的回答", position: 2 }),
    ],
  }],
  next_cursor: null,
};

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
    // ---- 统一会话契约（当前 ChatPage 数据源）----
    if (path === "/api/conversations" && method === "POST") {
      await json(route, { conversation: baseConv });
      return;
    }
    if (path === "/api/conversations/lookup") {
      await json(route, { conversation: baseConv });
      return;
    }
    if (path === "/api/conversations/conv-chat/snapshot") {
      await json(route, snap);
      return;
    }
    if (path === "/api/conversations/conv-chat/turns") {
      const cursor = url.searchParams.get("cursor");
      await json(route, cursor ? olderPage : currentPage);
      return;
    }
    if (path === "/api/conversations/conv-chat/queue" && method === "POST") {
      await json(route, { queue_item: { queue_item_id: "q-sent", conversation_id: "conv-chat", position: 1, revision: 1, status: "waiting", text: "E2E 用户消息", created_at: "", updated_at: "" } });
      return;
    }
    if (path === "/api/conversations/conv-chat/queue/send-next") {
      // 乐观回显 user_node（chat.done/assistant 回复经 SSE；此处校验发送链路与回显）
      await json(route, {
        user_node: node({ turn_id: "t-send", node_id: "u-send", type: "user", text: "E2E 用户消息", status: "dispatched" }),
        turn: turn({ turn_id: "t-send", status: "queued", turn_version: 1 }),
      });
      return;
    }
    // ---- 其余页面契约（保留）----
    if (path === "/api/plans") {
      await json(route, { plans: [] });
      return;
    }
    if (path === "/api/goals") {
      await json(route, { goals: [] });
      return;
    }
    if (path === "/api/approvals" && method === "GET") {
      await json(route, { approvals: [] });
      return;
    }
    if (path === "/api/questions" && method === "GET") {
      await json(route, {
        questions: [{
          id: "q-e2e", question_id: "q-e2e", session_key: "webui:default",
          question: "请选择迁移策略", required: true, allow_custom: true,
          options: [
            { id: "safe", label: "安全切换", recommended: true },
            { id: "fast", label: "快速切换" },
          ],
        }],
      });
      return;
    }
    if (path === "/api/questions/q-e2e" && method === "POST") {
      await json(route, { ok: true, id: "q-e2e", question_id: "q-e2e" });
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
    if (path === "/api/config") {
      await json(route, {
        config: {
          llm: { model_id: "deepseek", models: { deepseek: { base_url: "https://example.invalid", api_key: "…" } } },
          workspace: { path: "./workspace" }, prompt: {}, gateway: { sessions: {} },
        },
        rev: 3,
      });
      return;
    }
    if (path.startsWith("/api/config/") && ["PATCH", "PUT", "POST", "DELETE"].includes(method)) {
      await json(route, { ok: true, rev: 4 });
      return;
    }
    if (path === "/api/mcp") {
      await json(route, { servers: [], status: {} });
      return;
    }
    if (path === "/api/skills/meta") {
      await json(route, { loaded: 0, directory: "SKILLS" });
      return;
    }
    if (path === "/api/skills") {
      await json(route, { skills: [] });
      return;
    }
    if (path === "/api/prompt/files") {
      await json(route, { files: [] });
      return;
    }
    if (path === "/api/prompt/main-session") {
      await json(route, { tools: null, skills: null, mcp_servers: null, catalog: { tools: [], skills: [], mcp: { servers: [] } } });
      return;
    }
    if (path === "/api/status") {
      await json(route, { executor: { workers: 1, pending: 0 }, sessions: { active: 0, max: 50, list: [] }, channels: {} });
      return;
    }
    if (path === "/api/scheduler/channels") {
      await json(route, { channels: [], webhooks: [], targets: {} });
      return;
    }
    if (path === "/api/scheduler/jobs") {
      await json(route, method === "GET" ? { jobs: [] } : { ok: true });
      return;
    }
    if (path === "/api/scheduler/history") {
      await json(route, { history: [] });
      return;
    }
    if (path === "/api/workspaces") {
      await json(route, { workspaces: [{ workspace_id: "w-e2e", name: "E2E 工作区", project_path: "/tmp/e2e" }], total: 1 });
      return;
    }
    if (path === "/api/workspaces/w-e2e/sessions") {
      await json(route, { sessions: [{ session_id: "s-e2e", workspace_id: "w-e2e", session_key: "workspace:w-e2e:s-e2e", name: "E2E 会话" }] });
      return;
    }
    if (path === "/api/workspaces/w-e2e/files") {
      await json(route, { workspace_id: "w-e2e", path: "", entries: [{ name: "README.md", path: "README.md", kind: "file", size: 5 }], total: 1, truncated: false });
      return;
    }
    if (path === "/api/workspaces/w-e2e/file") {
      await json(route, { workspace_id: "w-e2e", path: "README.md", content: "hello e2e", size: 9, truncated: false });
      return;
    }
    if (path === "/api/workspaces/w-e2e/sessions/s-e2e/history") {
      await json(route, { workspace_id: "w-e2e", workspace_session_id: "s-e2e", session_key: "workspace:w-e2e:s-e2e", source: "memory", messages: [], total: 0, start_index: 0, has_more: false, reset_required: false });
      return;
    }
    if (path === "/api/agents" || path.startsWith("/api/agents?")) {
      await json(route, { agents: [] });
      return;
    }
    if (path === "/api/agents/catalog") {
      await json(route, { tools: [], skills: [], mcp: { servers: [] }, models: [] });
      return;
    }
    // 会话级偏好/上下文
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
  await installApiMocks(page);
});

test("all 11 migrated routes render under /ui and survive direct refresh", async ({ page }) => {
  const routes = [
    "chat", "plan", "goal", "mcp", "skills", "prompt", "status", "cron", "workspace", "agent-editor", "settings",
  ] as const;

  for (const hash of routes) {
    await page.goto(`#/${hash}`);
    await expect(page.locator(`#main[data-route="${hash}"]`)).toBeAttached();
    await page.reload();
    await expect(page.locator(`#main[data-route="${hash}"]`)).toBeAttached();
  }
});

test("chat restores history, prepends older messages, sends through the unified queue, and answers a structured question", async ({ page }) => {
  const posts: Array<{ path: string; body: unknown }> = [];
  page.on("request", (request) => {
    if (request.method() === "POST") posts.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
  });

  await page.goto("#/chat");
  // 历史恢复（统一 /turns 契约）
  await expect(page.getByText("当前回答")).toBeVisible();

  // 向上翻 → 加载更早的历史（cursor 分页）
  const messageArea = page.locator(".virtual-msg-area");
  await messageArea.evaluate((element) => { element.scrollTop = 0; element.dispatchEvent(new Event("scroll")); });
  await expect(page.getByText("更早的回答")).toBeVisible();

  // 结构化问题弹窗 + 作答
  await expect(page.getByRole("dialog", { name: /请选择迁移策略/ })).toBeVisible();
  await page.getByRole("radio", { name: /安全切换/ }).click();
  await page.getByRole("button", { name: "提交" }).click();
  await expect(page.getByRole("dialog", { name: /请选择迁移策略/ })).toBeHidden();

  // 发送 → 乐观回显用户气泡 + 走统一 queue 契约
  await page.getByLabel("消息").fill("E2E 用户消息");
  await page.locator(".chat-send").click();
  await expect(page.getByText("E2E 用户消息")).toBeVisible();

  expect(posts.some((item) => item.path === "/api/conversations/conv-chat/queue"
    && (item.body as { text?: string }).text === "E2E 用户消息")).toBeTruthy();
  expect(posts.some((item) => item.path === "/api/conversations/conv-chat/queue/send-next")).toBeTruthy();
  expect(posts.some((item) => item.path === "/api/questions/q-e2e"
    && (item.body as { selected_option_ids?: string[] }).selected_option_ids?.includes("safe"))).toBeTruthy();
});

test("workspace selection, session history, directory browsing and file preview remain connected", async ({ page }) => {
  await page.goto("#/workspace");
  await page.locator(".ws-workspace-item", { hasText: "E2E 工作区" }).click();
  await expect(page.getByText("README.md")).toBeVisible();
  await page.getByText("README.md").click();
  await expect(page.getByText("hello e2e")).toBeVisible();
  await page.locator(".ws-session-item", { hasText: "E2E 会话" }).click();
  await expect(page.getByLabel("工作区消息")).toBeVisible();
});

test("settings save and cron creation use the migrated API contracts", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", (request) => {
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });

  await page.goto("#/settings");
  const workspacePath = page.getByLabel("workspace.path");
  await workspacePath.fill("./workspace-e2e");
  await workspacePath.locator("xpath=ancestor::div[contains(@class,'settings-card')]").getByRole("button", { name: "保存" }).click();
  await expect.poll(() => writes).toContain("PATCH /api/config/workspace");

  await page.goto("#/cron");
  await page.getByRole("button", { name: "＋ 添加任务" }).click();
  await page.getByLabel("名称").fill("e2e-job");
  await page.getByLabel("cron 表达式").fill("0 0 * * *");
  await page.getByLabel("prompt").fill("E2E 定时任务");
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => writes).toContain("POST /api/scheduler/jobs");
});
