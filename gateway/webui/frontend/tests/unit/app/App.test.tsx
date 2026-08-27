import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/app/App";

afterEach(() => vi.restoreAllMocks());

const mainConversation = {
  conversation_id: "conv-main",
  session_key: "webui:default",
  origin: "webui",
  subtype: "main",
  workspace_id: null,
  execution_scope: "gateway:default",
  route_metadata: {},
  session_version: 0,
  created_at: "",
  updated_at: ""};

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  const mockApi = {
    get: vi.fn(async (path: string) => {
      if (path.includes("/snapshot")) {
        return { conversation: mainConversation, session_version: 0, queue: [], live_turn: null, turn_version: 0, nodes: [], queued_nodes: [], pending_approvals: [], server_time: "" };
      }
      if (path.includes("/turns")) return { items: [], next_cursor: null };
      return { conversations: [] };
    }),
    post: vi.fn(async () => ({ conversation: mainConversation })),
    patch: vi.fn(async () => ({})),
    request: vi.fn(async () => ({}))};
  return { ...actual, api: mockApi };
});

describe("App scaffold", () => {
  it("renders main chat as default route", async () => {
    window.location.hash = "#/";
    render(<App />);

    expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument();
    // 主会话 + 其余页面（统一会话页与 Plan/Goal 管理页已移除）
    expect(screen.getAllByRole("link")).toHaveLength(9);
    expect(screen.getByRole("link", { name: "💬 主会话" })).toHaveAttribute("href", "#/chat");
    expect(screen.getByRole("link", { name: /设置/ })).toHaveAttribute("href", "#/settings");
    // 默认路由进入主会话聊天页（旧布局 + 新流式）
    await screen.findByTestId("chat-page");
    await waitFor(() => expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument());
  });
});
