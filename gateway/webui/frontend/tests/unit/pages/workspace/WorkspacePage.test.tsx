import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "@/api/client";
import { WorkspacePage } from "@/pages/workspace/WorkspacePage";

const client = {
  get: vi.fn((path: string) => {
    if (path === "/api/workspaces?limit=200") return Promise.resolve({ workspaces: [{ workspace_id: "w", name: "Demo", project_path: "/tmp/demo" }], total: 1 });
    if (path.endsWith("/sessions")) return Promise.resolve({ sessions: [{ session_id: "s", workspace_id: "w", session_key: "workspace:w:s", name: "Session" }] });
    if (path.endsWith("/files")) return Promise.resolve({ workspace_id: "w", path: "", entries: [{ name: "README.md", path: "README.md", kind: "file", size: 12 }], total: 1, truncated: false });
    if (path.endsWith("/history")) return Promise.resolve({ workspace_id: "w", workspace_session_id: "s", session_key: "workspace:w:s", source: "memory", messages: [], total: 0, start_index: 0, has_more: false, reset_required: false });
    if (path.endsWith("/file")) return Promise.resolve({ workspace_id: "w", path: "README.md", content: "hello", size: 5, truncated: false });
    return Promise.reject(new Error(path));
  }),
  post: vi.fn(() => Promise.resolve({ ok: true })),
} as unknown as ApiClient;

describe("WorkspacePage", () => {
  it("preserves workspace selection, directory browsing, file viewing and refresh", async () => {
    render(<WorkspacePage client={client} />);
    await screen.findByText("Demo");
    fireEvent.click(screen.getByText("Demo"));
    await screen.findByText("README.md");
    fireEvent.click(screen.getByText("README.md"));
    await screen.findByText("hello");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(client.get).toHaveBeenCalledWith("/api/workspaces?limit=200", expect.anything()));
  });
});
